#!/usr/bin/env python3
"""Gera os manifests padrão da plataforma para cada jogo do catálogo (k8s/games/*/game.yaml).

Para cada jogo com status "active" e campo "namespace", grava:
  k8s/games/<jogo>/platform.generated.yaml  Service do jogo, métricas (Service + ServiceMonitor),
                                            backup (ExternalSecret + CronJob restic)
  k8s/argocd/apps/<jogo>.yaml               Application do Argo CD (sync manual)
O StatefulSet continua escrito à mão em cada jogo (é a parte realmente específica).
Uso: python3 scripts/render-games.py [--check]   (--check falha se algo gerado estiver desatualizado)
"""
import pathlib
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


def render_game(g):
    name, ns = g["name"], g["namespace"]
    uid = g.get("runAsUser", 1000)
    labels = {"app.kubernetes.io/name": name, "platform.homelab/game": name}
    docs = [{
        "apiVersion": "v1", "kind": "Service",
        "metadata": {"name": name, "namespace": ns, "labels": labels},
        "spec": {"type": "LoadBalancer", "selector": {"app.kubernetes.io/name": name},
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
                    "securityContext": {"runAsNonRoot": True, "runAsUser": uid, "runAsGroup": uid,
                                        "seccompProfile": {"type": "RuntimeDefault"}},
                    "containers": [{
                        "name": "restic", "image": DEFAULTS["resticImage"],
                        "command": ["/bin/sh", "-c"], "args": [script],
                        "envFrom": [{"secretRef": {"name": f"{name}-backup"}}],
                        "env": [{"name": "RESTIC_CACHE_DIR", "value": "/cache"}],
                        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                            "capabilities": {"drop": ["ALL"]}},
                        "resources": {"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"memory": "512Mi"}},
                        "volumeMounts": [{"name": "data", "mountPath": "/data", "readOnly": True},
                                         {"name": "cache", "mountPath": "/cache"}],
                    }],
                    "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": b["pvc"], "readOnly": True}},
                                {"name": "cache", "emptyDir": {}}],
                }}}},
            },
        }]
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


def main():
    check = "--check" in sys.argv
    errors = catalog.problems()
    if errors:
        print("ERRO: catálogo de jogos inválido:\n" + "\n".join(f"  {e}" for e in errors))
        return 1
    outputs = {}
    for g in catalog.games():
        if g["status"] == "active":
            outputs.update(render_game(g))
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
