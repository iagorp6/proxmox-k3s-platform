#!/usr/bin/env python3
"""Gera os manifests padrão da plataforma para cada jogo do catálogo (k8s/games/*/game.yaml).

Para cada jogo com status "active" e campo "namespace", grava:
  k8s/games/<jogo>/platform.generated.yaml  Service do jogo, métricas (Service + ServiceMonitor),
                                            backup (ExternalSecret + CronJob restic)
  k8s/argocd/apps/<jogo>.yaml               Application do Argo CD (sync manual)
E, para o conjunto desses jogos:
  k8s/monitoring/alerts-games.generated.yaml  alertas por jogo (fora do ar, restart, memória)
O StatefulSet continua escrito à mão em cada jogo (é a parte realmente específica).
Uso: python3 scripts/render-games.py [--check]   (--check falha se algo gerado estiver desatualizado)
"""
import pathlib
import re
import shlex
import sys

import yaml

import catalog

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULTS = yaml.safe_load((ROOT / "platform" / "defaults.yaml").read_text(encoding="utf-8"))


def _str(dumper, data):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


yaml.SafeDumper.add_representer(str, _str)
HEADER = "# Gerado por scripts/render-games.py a partir de game.yaml: não edite à mão.\n"
BACKUP_KEYS = [
    ("RESTIC_REPOSITORY", "/homelab/backup/restic-repository"),
    ("RESTIC_PASSWORD", "/homelab/backup/restic-password"),
    ("AWS_ACCESS_KEY_ID", "/homelab/backup/restic-aws-access-key-id"),
    ("AWS_SECRET_ACCESS_KEY", "/homelab/backup/restic-aws-secret-access-key"),
    ("AWS_DEFAULT_REGION", "/homelab/backup/aws-region"),
]


def dump(docs):
    return HEADER + "---\n".join(yaml.safe_dump(d, sort_keys=False, allow_unicode=True, width=1000) for d in docs)


def save_hook(g):
    """initContainer que roda o switch.saveCommand no servidor antes do restic (mundo consistente no disco).

    Servidor parado: pula, o backup leva o último save. Save falhou com o servidor rodando: avisa e segue,
    porque um backup do último autosave vale mais do que nenhum backup.
    """
    name, ns, pod = g["name"], g["namespace"], f"{g['name']}-0"
    k = DEFAULTS["kubectl"]
    cmd = shlex.quote(g["switch"]["saveCommand"])   # roda com sh -c, como no switch-game.py
    script = (
        "set -eu\n"
        "cd /tmp\n"
        f"curl -fsSLo kubectl https://dl.k8s.io/release/{k['version']}/bin/linux/amd64/kubectl\n"
        f"echo '{k['sha256']}  kubectl' | sha256sum -c -\n"
        "chmod +x kubectl\n"
        f"phase=$(./kubectl -n {ns} get pod {pod} -o jsonpath='{{.status.phase}}' 2>/dev/null || true)\n"
        'if [ "$phase" = Running ]; then\n'
        f"  ./kubectl -n {ns} exec {pod} -c server -- sh -c {cmd} || echo 'AVISO: save falhou; o backup usa o último autosave'\n"
        "else\n"
        f"  echo \"{pod} não está rodando (${{phase:-ausente}}): backup do último save em disco\"\n"
        "fi\n"
    )
    container = {
        "name": "save-world", "image": DEFAULTS["curlImage"],
        "command": ["/bin/sh", "-c"], "args": [script],
        "env": [{"name": "HOME", "value": "/tmp"}],  # cache do kubectl; o root FS é read-only
        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]}},
        "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
        "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
    }
    sa = f"{name}-backup"
    rbac = [{
        "apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": sa, "namespace": ns},
    }, {
        "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
        "metadata": {"name": sa, "namespace": ns},
        "rules": [{"apiGroups": [""], "resources": ["pods"], "resourceNames": [pod], "verbs": ["get"]},
                  # exec via WebSocket (kubectl recente) pede get, além do create do SPDY
                  {"apiGroups": [""], "resources": ["pods/exec"], "resourceNames": [pod], "verbs": ["get", "create"]}],
    }, {
        "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
        "metadata": {"name": sa, "namespace": ns},
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": sa},
        "subjects": [{"kind": "ServiceAccount", "name": sa, "namespace": ns}],
    }]
    return container, sa, rbac


def render_game(g):
    name, ns = g["name"], g["namespace"]
    uid = g.get("runAsUser", 1000)
    labels = {"app.kubernetes.io/name": name, "platform.homelab/game": name}
    docs = [{
        "apiVersion": "v1", "kind": "Service",
        "metadata": {"name": name, "namespace": ns, "labels": labels},
        "spec": {"type": "LoadBalancer", **g.get("service", {}), "selector": {"app.kubernetes.io/name": name},
                 "ports": [{"name": p["name"], "port": p["port"], "targetPort": p.get("targetPort", p["name"]),
                            "protocol": p["protocol"]} for p in g["ports"]]},
    }]
    if g.get("metrics"):
        port = DEFAULTS["metricsPort"]
        docs += [{
            "apiVersion": "v1", "kind": "Service",
            "metadata": {"name": f"{name}-metrics", "namespace": ns, "labels": {"app.kubernetes.io/name": f"{name}-metrics"}},
            "spec": {"clusterIP": "None", "selector": {"app.kubernetes.io/name": name},
                     "ports": [{"name": "metrics", "port": port, "targetPort": "metrics"}]},
        }, {
            "apiVersion": "monitoring.coreos.com/v1", "kind": "ServiceMonitor",
            "metadata": {"name": name, "namespace": ns, "labels": {"release": "kps"}},
            "spec": {"selector": {"matchLabels": {"app.kubernetes.io/name": f"{name}-metrics"}},
                     "endpoints": [{"port": "metrics", "interval": "30s"}]},
        }]
    b = g.get("backup")
    if b:
        keep = DEFAULTS["backupRetention"]
        excludes = "".join(f" --exclude {e}" for e in b.get("exclude", []))
        script = (
            "set -e\n"
            f"restic backup {' '.join(b['paths'])}{excludes} --tag {name} --host {name} --retry-lock 10m\n"
            f"restic forget --tag {name} --host {name} --keep-daily {keep['daily']} --keep-weekly {keep['weekly']} --prune --retry-lock 10m\n"
            f"restic snapshots --tag {name} --latest 1\n"
        )
        hook = save_hook(g) if g.get("switch", {}).get("saveCommand") else None
        docs += [{
            "apiVersion": "external-secrets.io/v1", "kind": "ExternalSecret",
            "metadata": {"name": f"{name}-backup", "namespace": ns},
            "spec": {"refreshInterval": "1h",
                     "secretStoreRef": {"kind": "ClusterSecretStore", "name": "aws-ssm"},
                     "target": {"name": f"{name}-backup", "creationPolicy": "Owner"},
                     "data": [{"secretKey": k, "remoteRef": {"key": v}} for k, v in BACKUP_KEYS]},
        }, {
            "apiVersion": "batch/v1", "kind": "CronJob",
            "metadata": {"name": f"{name}-backup", "namespace": ns},
            "spec": {
                "schedule": b["schedule"], "timeZone": "America/Sao_Paulo", "concurrencyPolicy": "Forbid",
                "successfulJobsHistoryLimit": 1, "failedJobsHistoryLimit": 2,
                "jobTemplate": {"spec": {"backoffLimit": 2, "template": {"spec": {
                    "restartPolicy": "Never",
                    **({"serviceAccountName": hook[1], "initContainers": [hook[0]]} if hook else
                       {"automountServiceAccountToken": False}),
                    "securityContext": {"runAsNonRoot": True, "runAsUser": uid, "runAsGroup": uid,
                                        "seccompProfile": {"type": "RuntimeDefault"}},
                    "containers": [{
                        "name": "restic", "image": DEFAULTS["resticImage"],
                        "command": ["/bin/sh", "-c"], "args": [script],
                        "envFrom": [{"secretRef": {"name": f"{name}-backup"}}],
                        # Root FS é read-only: o restic grava os packs temporários em TMPDIR, então /tmp é um emptyDir
                        "env": [{"name": "RESTIC_CACHE_DIR", "value": "/cache"}, {"name": "TMPDIR", "value": "/tmp"}],
                        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                            "capabilities": {"drop": ["ALL"]}},
                        "resources": {"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"memory": "512Mi"}},
                        "volumeMounts": [{"name": "data", "mountPath": "/data", "readOnly": True},
                                         {"name": "cache", "mountPath": "/cache"},
                                         {"name": "tmp", "mountPath": "/tmp"}],
                    }],
                    "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": b["pvc"], "readOnly": True}},
                                {"name": "cache", "emptyDir": {}},
                                {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}}],
                }}}},
            },
        }] + (hook[2] if hook else [])
    app = {
        "apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
        "metadata": {"name": name, "namespace": "argocd"},
        "spec": {"project": "default",
                 "source": {"repoURL": DEFAULTS["repoURL"], "targetRevision": "main", "path": f"k8s/games/{name}"},
                 "destination": {"server": "https://kubernetes.default.svc", "namespace": ns}},
    }
    return {
        ROOT / "k8s" / "games" / name / "platform.generated.yaml": dump(docs),
        ROOT / "k8s" / "argocd" / "apps" / f"{name}.yaml": HEADER + "# Sync MANUAL: quem liga e desliga é o scripts/switch-game.py\n" + dump([app])[len(HEADER):],
    }


def render_alerts(games):
    """PrometheusRule com os alertas que todo jogo do catálogo recebe, sem lista escrita à mão."""
    ns = "|".join(g["namespace"] for g in games)
    server = f'namespace=~"{ns}",container="server"'
    rules = [{
        "alert": "GameDown",
        "expr": (f'kube_statefulset_replicas{{namespace=~"{ns}"}} > 0\n'
                 "unless on (namespace, statefulset)\n"
                 f'kube_statefulset_status_replicas_ready{{namespace=~"{ns}"}} > 0\n'),
        "for": "10m",
        "labels": {"severity": "critical"},
        "annotations": {
            "summary": "Servidor de {{ $labels.namespace }} fora do ar",
            "description": "{{ $labels.namespace }} é o jogo ativo, mas o pod não está pronto há mais de 10 minutos.",
            "action": "kubectl -n {{ $labels.namespace }} get pods e kubectl -n {{ $labels.namespace }} logs {{ $labels.statefulset }}-0 -c server --tail=30",
        },
    }, {
        "alert": "GameRestarted",
        "expr": f"increase(kube_pod_container_status_restarts_total{{{server}}}[15m]) > 0",
        "labels": {"severity": "warning"},
        "annotations": {
            "summary": "Servidor de {{ $labels.namespace }} reiniciou sozinho",
            "description": "O processo do jogo caiu e o Kubernetes o reiniciou. Trocas de jogo e deploys não disparam este alerta.",
            "action": "kubectl -n {{ $labels.namespace }} logs {{ $labels.pod }} -c server --previous --tail=40",
        },
    }, {
        "alert": "GameMemoryHigh",
        "expr": (f"max by (namespace, pod) (container_memory_working_set_bytes{{{server}}})\n"
                 f'/ max by (namespace, pod) (kube_pod_container_resource_limits{{{server},resource="memory"}})\n'
                 "> 0.9\n"),
        "for": "10m",
        "labels": {"severity": "warning"},
        "annotations": {
            "summary": "Servidor de {{ $labels.namespace }} perto do limite de memória",
            "description": "Uso de {{ $value | humanizePercentage }} do limite. Risco de OOMKilled.",
            "action": "Comparar o pico no dashboard e avaliar subir o limite de memória no StatefulSet de {{ $labels.namespace }}.",
        },
    }]
    doc = {
        "apiVersion": "monitoring.coreos.com/v1", "kind": "PrometheusRule",
        "metadata": {"name": "homelab-game-alerts", "namespace": "monitoring"},
        "spec": {"groups": [{"name": "games-catalog", "rules": rules}]},
    }
    return {ROOT / "k8s" / "monitoring" / "alerts-games.generated.yaml":
            HEADER + f"# Jogos cobertos (status active no catálogo): {', '.join(g['name'] for g in games)}\n" + dump([doc])[len(HEADER):]}


def main():
    check = "--check" in sys.argv
    errors = catalog.problems()
    if not re.fullmatch(r"[0-9a-f]{64}", str(DEFAULTS["kubectl"]["sha256"])):
        errors.append("platform/defaults.yaml: kubectl.sha256 precisa ser o SHA-256 do binário (64 hex)")
    # Manifests escritos à mão que baixam o kubectl precisam usar a mesma versão e o mesmo hash
    for f in sorted((ROOT / "k8s").rglob("*.yaml")):
        text = f.read_text(encoding="utf-8")
        if "dl.k8s.io/release/" in text and "generated" not in f.name:
            if f"/release/{DEFAULTS['kubectl']['version']}/" not in text or DEFAULTS["kubectl"]["sha256"] not in text:
                errors.append(f"{f.relative_to(ROOT)}: kubectl com versão ou SHA-256 diferente de platform/defaults.yaml")
    if errors:
        print("ERRO: catálogo de jogos inválido:\n" + "\n".join(f"  {e}" for e in errors))
        return 1
    outputs = {}
    generated = [g for g in catalog.games() if g["status"] == "active"]
    for g in generated:
        outputs.update(render_game(g))
    if generated:
        outputs.update(render_alerts(generated))
    stale = []
    for path, content in outputs.items():
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current != content:
            stale.append(path.relative_to(ROOT))
            if not check:
                path.write_text(content, encoding="utf-8")
    if check and stale:
        print("ERRO: manifests gerados desatualizados (rode python3 scripts/render-games.py):")
        print("\n".join(f"  {p}" for p in stale))
        return 1
    print(f"OK: {len(outputs)} arquivos gerados" + (f", {len(stale)} atualizados" if stale and not check else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
