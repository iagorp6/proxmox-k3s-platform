#!/usr/bin/env python3
"""Gera os manifests padrão da plataforma para cada jogo do catálogo (k8s/games/*/game.yaml).

Para cada jogo com status "active" e campo "namespace", grava:
  k8s/games/<jogo>/platform.generated.yaml  Service do jogo, métricas (Service + ServiceMonitor),
                                            NetworkPolicy de ingress, backup (ExternalSecret + CronJob restic)
                                            e restore drill mensal (CronJob)
  k8s/argocd/apps/<jogo>.yaml               Application do Argo CD (sync manual, AppProject games)
E, para o conjunto desses jogos:
  k8s/argocd/guardrails.generated.yaml        Namespaces com Pod Security e os AppProjects platform,
                                              monitoring e games (aplicado à mão: make guardrails)
  k8s/monitoring/alerts-games.generated.yaml  alertas por jogo (fora do ar, sem resposta, sem métricas, restart, memória)
  k8s/platform/games-bot-rbac.generated.yaml  RBAC do bot de troca de jogo em cada namespace (ADR 0006)
  k8s/platform/games.generated.json           nome e ícone de cada jogo (resumo semanal no Discord)
O StatefulSet continua escrito à mão em cada jogo (é a parte realmente específica).
Uso: python3 scripts/render-games.py [--check]   (--check falha se algo gerado estiver desatualizado)
"""
import importlib.util
import json
import pathlib
import re
import shlex
import sys
import zlib

import yaml

import catalog

ROOT = pathlib.Path(__file__).resolve().parent.parent
# O CronJob do drill mensal roda o mesmo script do make restore-drill (restore, checagens e linha RESULT)
_spec = importlib.util.spec_from_file_location("restore_drill", ROOT / "scripts" / "restore-drill.py")
restore_drill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(restore_drill)
DEFAULTS = yaml.safe_load((ROOT / "platform" / "defaults.yaml").read_text(encoding="utf-8"))
ALERTMANAGER_VALUES = ROOT / "k8s" / "monitoring" / "values-kube-prometheus-stack.yaml"


def _str(dumper, data):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


yaml.SafeDumper.add_representer(str, _str)
HEADER = "# Gerado por scripts/render-games.py a partir de game.yaml: não edite à mão.\n"
IMAGE_REF = re.compile(r"[a-z0-9][a-z0-9._/-]*(?::[0-9]+)?(?:/[a-z0-9._-]+)*:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}")
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


def drill_cronjob(g):
    """Restore drill mensal: o último snapshot volta num emptyDir e passa pelas checagens do catálogo.

    Mesmo script do make restore-drill, sem tocar no PVC do jogo, então roda com o jogo no ar ou parado.
    O dia do mês sai do nome do jogo (crc32), para os jogos não baixarem do S3 ao mesmo tempo. Falha cai
    no KubeJobFailed; sem sucesso há mais de 35 dias, no RestoreDrillStale (alerts-homelab.yaml).
    """
    name, ns, uid = g["name"], g["namespace"], g.get("runAsUser", 1000)
    target = restore_drill.TARGET
    # O dado restaurado sai do disco do node ao fim, com sucesso ou falha (o Job fica para os logs)
    script = f"trap 'rm -rf {target}/*' EXIT\n" + restore_drill.drill_script(g)
    day = 1 + zlib.crc32(name.encode()) % 28
    return {
        "apiVersion": "batch/v1", "kind": "CronJob",
        "metadata": {"name": f"{name}-restore-drill", "namespace": ns},
        "spec": {
            "schedule": f"0 7 {day} * *", "timeZone": "America/Sao_Paulo", "concurrencyPolicy": "Forbid",
            "successfulJobsHistoryLimit": 1, "failedJobsHistoryLimit": 1,
            "jobTemplate": {"spec": {"backoffLimit": 1, "activeDeadlineSeconds": 3600, "ttlSecondsAfterFinished": 259200,
                                     "template": {"metadata": {"labels": {"app.kubernetes.io/name": f"{name}-restore-drill"}},
                                                  "spec": {
                "restartPolicy": "Never", "automountServiceAccountToken": False,
                "securityContext": {"runAsNonRoot": True, "runAsUser": uid, "runAsGroup": uid,
                                    "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [{
                    "name": "restic", "image": DEFAULTS["resticImage"],
                    "command": ["/bin/sh", "-c"], "args": [script],
                    "envFrom": [{"secretRef": {"name": f"{name}-backup"}}],
                    "env": [{"name": "RESTIC_CACHE_DIR", "value": "/cache"}, {"name": "TMPDIR", "value": "/tmp"}],
                    "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                        "capabilities": {"drop": ["ALL"]}},
                    "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"memory": "512Mi"}},
                    "volumeMounts": [{"name": "restore", "mountPath": target},
                                     {"name": "cache", "mountPath": "/cache"},
                                     {"name": "tmp", "mountPath": "/tmp"}],
                }],
                # emptyDir com teto: um mundo maior que o teto é despejado (Evicted) e o drill falha, em vez
                # de encher o disco da VM. Ajuste com backup.drillSize no game.yaml.
                "volumes": [{"name": "restore", "emptyDir": {"sizeLimit": g["backup"].get("drillSize", "20Gi")}},
                            {"name": "cache", "emptyDir": {}},
                            {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}}],
            }}}},
        },
    }


# Faixas que um pod de jogo não alcança: rede da casa e do rack (host Proxmox, FortiGate, outras
# máquinas), rede de pods e services do K3s, CGNAT e link-local. A internet pública continua aberta.
PRIVATE_RANGES = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16"]
# API do Kubernetes: o service kubernetes (10.43.0.1:443) vira <IP do node>:6443 no DNAT, antes da
# NetworkPolicy, e o IP do node é privado. Liberar a porta 6443 em qualquer destino evita fixar o IP
# do node num repositório público; nada além do kube-apiserver escuta nela.
KUBE_API_PORT = 6443


def egress_policy(name, ns):
    """Egress do namespace do jogo: DNS do cluster, API do Kubernetes e internet sem as faixas privadas.

    Os jogos ficam expostos na internet e rodam mods de terceiros (Workshop, Modrinth): um servidor
    comprometido não chega ao host Proxmox, à GUI do FortiGate nem aos outros namespaces. Quem precisa
    de cada regra: DNS (todos), API (hook de save do backup, kubectl exec), internet (Steam e mods no
    servidor, restic no S3, download do kubectl no hook).
    """
    return {
        "apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
        "metadata": {"name": f"{name}-egress", "namespace": ns},
        "spec": {"podSelector": {}, "policyTypes": ["Egress"], "egress": [
            {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                     "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
             "ports": [{"port": 53, "protocol": "UDP"}, {"port": 53, "protocol": "TCP"}]},
            {"ports": [{"port": KUBE_API_PORT, "protocol": "TCP"}]},
            {"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": PRIVATE_RANGES}}]},
        ]},
    }


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
    # Ingress default-deny no namespace do jogo: entram só as portas do jogo (de qualquer origem: jogadores
    # via ServiceLB, com ou sem externalTrafficPolicy Local) e o scrape do Prometheus na porta de métricas.
    # Sondas são exec e exporters/RCON falam por localhost, então nada mais precisa entrar.
    ingress = [{"ports": [{"port": p.get("targetPort", p["name"]), "protocol": p["protocol"]} for p in g["ports"]]}]
    if g.get("metrics"):
        ingress.append({"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "monitoring"}}}],
                        "ports": [{"port": "metrics", "protocol": "TCP"}]})
    docs += ([{
        "apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
        "metadata": {"name": f"{name}-ingress", "namespace": ns},
        "spec": {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": ingress},
    }, egress_policy(name, ns)])
    b = g.get("backup")
    if b:
        keep = DEFAULTS["backupRetention"]
        excludes = "".join(f" --exclude {e}" for e in b.get("exclude", []))
        # Só forget: o prune (lock exclusivo, lê e reescreve packs no S3) roda uma vez por semana no
        # restic-check (k8s/platform/restic-check.yaml), não três vezes por dia no repositório compartilhado
        script = (
            "set -e\n"
            f"restic backup {' '.join(b['paths'])}{excludes} --tag {name} --host {name} --retry-lock 10m\n"
            f"restic forget --tag {name} --host {name} --keep-daily {keep['daily']} --keep-weekly {keep['weekly']} --retry-lock 10m\n"
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
                # Job encerrado (ok ou falha) some depois de 3 dias: dá tempo de investigar e evita que
                # uma falha antiga já resolvida mantenha o KubeJobFailed disparando para sempre.
                "jobTemplate": {"spec": {"backoffLimit": 2, "ttlSecondsAfterFinished": 259200, "template": {"spec": {
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
        }, drill_cronjob(g)] + (hook[2] if hook else [])
    app = {
        "apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
        "metadata": {"name": name, "namespace": "argocd"},
        "spec": {"project": "games",
                 "source": {"repoURL": DEFAULTS["repoURL"], "targetRevision": "main", "path": f"k8s/games/{name}"},
                 "destination": {"server": "https://kubernetes.default.svc", "namespace": ns}},
    }
    return {
        ROOT / "k8s" / "games" / name / "platform.generated.yaml": dump(docs),
        ROOT / "k8s" / "argocd" / "apps" / f"{name}.yaml": HEADER + "# Sync MANUAL: quem liga e desliga é o scripts/switch-game.py\n" + dump([app])[len(HEADER):],
    }


def game_rules(games, mute):
    """Alertas que todo jogo recebe, para um grupo de jogos com a mesma janela de silêncio (alerts.muteDuring)."""
    ns = "|".join(g["namespace"] for g in games)
    server = f'namespace=~"{ns}",container="server"'
    # game_up vem do exporter com a label game; o namespace do jogo é igual ao nome (catalog.py garante)
    ready = (f'label_replace(kube_statefulset_status_replicas_ready{{namespace=~"{ns}"}} > 0, '
             '"game", "$1", "namespace", "(.+)")')
    labels = {"mute": mute} if mute else {}
    return [{
        "alert": "GameDown",
        "expr": (f'kube_statefulset_replicas{{namespace=~"{ns}"}} > 0\n'
                 "unless on (namespace, statefulset)\n"
                 f'kube_statefulset_status_replicas_ready{{namespace=~"{ns}"}} > 0\n'),
        "for": "10m",
        "labels": {"severity": "critical", **labels},
        "annotations": {
            "summary": "Servidor de {{ $labels.namespace }} fora do ar",
            "description": "{{ $labels.namespace }} é o jogo ativo, mas o pod não está pronto há mais de 10 minutos.",
            "action": "kubectl -n {{ $labels.namespace }} get pods e kubectl -n {{ $labels.namespace }} logs {{ $labels.statefulset }}-0 -c server --tail=30",
        },
    }, {
        # Pod pronto, mas o servidor não responde ao exporter (RCON, REST...). Cobre jogo sem readinessProbe.
        "alert": "GameUnresponsive",
        "expr": f'game_up{{game=~"{ns}"}} == 0\nand on (game)\n{ready}\n',
        "for": "5m",
        "labels": {"severity": "critical", **labels},
        "annotations": {
            "summary": "Servidor de {{ $labels.game }} não responde",
            "description": "O pod de {{ $labels.game }} está pronto, mas o servidor não responde ao exporter há mais de 5 minutos. Os jogadores não conseguem entrar.",
            "action": "kubectl -n {{ $labels.game }} logs {{ $labels.game }}-0 -c server --tail=30",
        },
    }, {
        "alert": "GameExporterMissing",
        "expr": f"{ready}\nunless on (game)\ngame_up\n",
        "for": "5m",
        "labels": {"severity": "warning", **labels},
        "annotations": {
            "summary": "Sem métricas do servidor de {{ $labels.game }}",
            "description": "O pod de {{ $labels.game }} está pronto, mas o Prometheus não coleta o exporter. Painel, alertas de jogador e fila do bot ficam cegos.",
            "action": "kubectl -n {{ $labels.game }} logs {{ $labels.game }}-0 -c exporter --tail=30",
        },
    }, {
        "alert": "GameRestarted",
        "expr": f"increase(kube_pod_container_status_restarts_total{{{server}}}[15m]) > 0",
        "labels": {"severity": "warning", **labels},
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
        "labels": {"severity": "warning", **labels},
        "annotations": {
            "summary": "Servidor de {{ $labels.namespace }} perto do limite de memória",
            "description": "Uso de {{ $value | humanizePercentage }} do limite. Risco de OOMKilled.",
            "action": "Comparar o pico no dashboard e avaliar subir o limite de memória no StatefulSet de {{ $labels.namespace }}.",
        },
    }]


def render_alerts(games):
    """PrometheusRule com os alertas que todo jogo do catálogo recebe, sem lista escrita à mão.

    Jogos com alerts.muteDuring ganham a label mute=<janela>; o Alertmanager silencia essa label
    durante o time_interval de mesmo nome (k8s/monitoring/values-kube-prometheus-stack.yaml).
    """
    by_mute = {}
    for g in games:
        by_mute.setdefault((g.get("alerts") or {}).get("muteDuring"), []).append(g)
    groups = []
    for mute, members in sorted(by_mute.items(), key=lambda kv: kv[0] or ""):
        groups.append({"name": f"games-catalog-{mute}" if mute else "games-catalog", "rules": game_rules(members, mute)})
    doc = {
        "apiVersion": "monitoring.coreos.com/v1", "kind": "PrometheusRule",
        "metadata": {"name": "homelab-game-alerts", "namespace": "monitoring"},
        "spec": {"groups": groups},
    }
    return {ROOT / "k8s" / "monitoring" / "alerts-games.generated.yaml":
            HEADER + f"# Jogos cobertos (status active no catálogo): {', '.join(g['name'] for g in games)}\n" + dump([doc])[len(HEADER):]}


def mute_problems(games):
    """Toda janela citada em alerts.muteDuring precisa existir no Alertmanager, com a rota que a aplica."""
    values = yaml.safe_load(ALERTMANAGER_VALUES.read_text(encoding="utf-8"))["alertmanager"]["config"]
    intervals = {t["name"] for t in values.get("time_intervals", [])}
    routed = {i for r in values["route"].get("routes", []) for i in r.get("mute_time_intervals", [])
              if f'mute = "{i}"' in r.get("matchers", [])}
    errors = []
    for g in games:
        mute = (g.get("alerts") or {}).get("muteDuring")
        if not mute:
            continue
        where = f"{g['dir']}/game.yaml"
        if mute not in intervals:
            errors.append(f"{where}: alerts.muteDuring '{mute}' não existe nos time_intervals do Alertmanager")
        elif mute not in routed:
            errors.append(f"{where}: falta a rota com matcher mute = \"{mute}\" e mute_time_intervals [{mute}] no Alertmanager")
    return errors


def render_bot_rbac(games):
    """RBAC do games-bot (ADR 0006): só o que o scripts/switch-game.py usa, por jogo do catálogo.

    Fica no k8s/platform (sync automático), não nos jogos (sync manual): um jogo novo no catálogo
    já ganha a permissão no próximo sync da plataforma.
    """
    sa = {"kind": "ServiceAccount", "name": "games-bot", "namespace": "platform"}
    docs = [{
        "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
        "metadata": {"name": "games-bot", "namespace": "argocd"},
        # Só as Applications dos jogos: patch numa Application qualquer daria deploy arbitrário
        "rules": [{"apiGroups": ["argoproj.io"], "resources": ["applications"],
                   "resourceNames": sorted(g["name"] for g in games), "verbs": ["get", "patch"]}],
    }, {
        "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
        "metadata": {"name": "games-bot", "namespace": "argocd"},
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "games-bot"},
        "subjects": [sa],
    }]
    for g in games:
        ns, pod = g["namespace"], f'{g["name"]}-0'
        docs += [{
            "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
            "metadata": {"name": "games-bot", "namespace": ns},
            "rules": [
                # leitura para rollout status / wait --for=delete (list e watch não filtram por nome)
                {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch"]},
                {"apiGroups": ["apps"], "resources": ["statefulsets"], "verbs": ["get", "list", "watch"]},
                # save do mundo e contagem de jogadores: exec só no pod do jogo
                {"apiGroups": [""], "resources": ["pods/exec"], "resourceNames": [pod], "verbs": ["get", "create"]},
            ],
        }, {
            "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
            "metadata": {"name": "games-bot", "namespace": ns},
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "games-bot"},
            "subjects": [sa],
        }]
    return {ROOT / "k8s" / "platform" / "games-bot-rbac.generated.yaml": dump(docs)}


# Pod Security Admission por namespace. restricted onde os pods já cumprem (non-root, sem escalada,
# drop ALL, seccomp); monitoring é privileged porque o node-exporter e o Alloy leem o host (hostPath,
# hostNetwork, hostPID). Jogos: podSecurity do game.yaml (padrão restricted).
INFRA_NAMESPACES = {"platform": "restricted", "external-secrets": "restricted", "monitoring": "privileged"}
POD_SECURITY = ("restricted", "baseline", "privileged")
IN_CLUSTER = "https://kubernetes.default.svc"
CHART_REPOS = {
    "platform": ["https://charts.external-secrets.io"],
    "monitoring": ["https://prometheus-community.github.io/helm-charts", "https://grafana.github.io/helm-charts"],
}
# Recursos de cluster que cada grupo instala (CRDs, RBAC e webhooks dos charts). Namespace não entra:
# os namespaces e os labels de Pod Security vêm só deste arquivo, que o autosync não altera.
CLUSTER_KINDS = {
    "platform": [("apiextensions.k8s.io", "CustomResourceDefinition"), ("rbac.authorization.k8s.io", "ClusterRole"),
                 ("rbac.authorization.k8s.io", "ClusterRoleBinding"),
                 ("admissionregistration.k8s.io", "ValidatingWebhookConfiguration"),
                 ("external-secrets.io", "ClusterSecretStore")],
    "monitoring": [("apiextensions.k8s.io", "CustomResourceDefinition"), ("rbac.authorization.k8s.io", "ClusterRole"),
                   ("rbac.authorization.k8s.io", "ClusterRoleBinding"),
                   ("admissionregistration.k8s.io", "MutatingWebhookConfiguration"),
                   ("admissionregistration.k8s.io", "ValidatingWebhookConfiguration")],
    "games": [],
}
# Nenhum projeto cria Applications ou AppProjects: isso é só do root (projeto bootstrap, root.yaml)
NO_ARGO = [{"group": "argoproj.io", "kind": k} for k in ("Application", "ApplicationSet", "AppProject")]


def pod_security_labels(level):
    labels = {"pod-security.kubernetes.io/enforce": level}
    if level == "baseline":
        # Avisa no apply (warn) e registra no audit o que ainda impede o restricted
        labels.update({"pod-security.kubernetes.io/warn": "restricted", "pod-security.kubernetes.io/audit": "restricted"})
    return labels


def render_guardrails(games):
    """Namespaces (com Pod Security) e AppProjects: o limite do que um commit no main consegue aplicar.

    Fica fora do autosync de propósito: o root (projeto bootstrap) só cria Applications, então um push
    no main (Renovate, deploy key do games-bot) não muda projeto, namespace nem label de Pod Security.
    Aplicado à mão com make guardrails (também no make deploy-argocd). O check-guardrails.py do CI
    confere que cada Application cabe no projeto dela.
    """
    game_ns = sorted(g["namespace"] for g in games)
    levels = {**INFRA_NAMESPACES, **{g["namespace"]: g.get("podSecurity", "restricted") for g in games}}
    docs = [{
        "apiVersion": "v1", "kind": "Namespace",
        "metadata": {"name": ns, "labels": pod_security_labels(level),
                     # Saiu dos manifests dos apps: o Argo não apaga o namespace ao deixar de gerenciá-lo
                     "annotations": {"argocd.argoproj.io/sync-options": "Prune=false"}},
    } for ns, level in sorted(levels.items())]
    destinations = {
        # games-bot (k8s/platform) tem Role no argocd e em cada jogo
        "platform": ["platform", "external-secrets", "argocd", *game_ns],
        # o kube-prometheus-stack cria Services de scrape (CoreDNS, kubelet) no kube-system
        "monitoring": ["monitoring", "kube-system"],
        "games": game_ns,
    }
    descriptions = {
        "platform": "Plataforma (games-bot, CronJobs, resumo) e External Secrets",
        "monitoring": "Prometheus, Alertmanager, Grafana, Loki e Alloy",
        "games": "Um Application por jogo do catálogo (sync manual, scripts/switch-game.py)",
    }
    for name in ("platform", "monitoring", "games"):
        docs.append({
            "apiVersion": "argoproj.io/v1alpha1", "kind": "AppProject",
            "metadata": {"name": name, "namespace": "argocd"},
            "spec": {
                "description": descriptions[name],
                "sourceRepos": [DEFAULTS["repoURL"], *CHART_REPOS.get(name, [])],
                "destinations": [{"server": IN_CLUSTER, "namespace": ns} for ns in destinations[name]],
                "clusterResourceWhitelist": [{"group": g, "kind": k} for g, k in CLUSTER_KINDS[name]],
                "namespaceResourceBlacklist": NO_ARGO,
            },
        })
    return {ROOT / "k8s" / "argocd" / "guardrails.generated.yaml":
            HEADER.replace("a partir de game.yaml", "a partir do catálogo de jogos")
            + "# Aplicado à mão (make guardrails), fora do autosync: veja render_guardrails no gerador.\n"
            + dump(docs)[len(HEADER):]}


def pod_security_problems(games):
    return [f"{g['dir']}/game.yaml: podSecurity '{g['podSecurity']}' inválido (use {', '.join(POD_SECURITY)})"
            for g in games if g.get("podSecurity", "restricted") not in POD_SECURITY]


def render_games_json(games):
    """Nome e ícone por jogo para o resumo semanal (ConfigMap weekly-summary), sem lista escrita à mão."""
    data = {g["name"]: {"title": g["displayName"], "icon": g.get("icon", "🎮")} for g in games}
    return {ROOT / "k8s" / "platform" / "games.generated.json":
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"}


def render_shared_exporters(entries):
    """Cópia da lib de exporter em cada jogo com `exporter: shared` (drafts incluídos: o scaffold já nasce com ela)."""
    source = ROOT / "platform" / "exporter" / "gameexporter.py"
    content = ("# Gerado por scripts/render-games.py a partir de platform/exporter/gameexporter.py: não edite à mão.\n"
               + source.read_text(encoding="utf-8"))
    return {ROOT / g["dir"] / "scripts" / "gameexporter.py": content
            for g in entries if g.get("exporter") == "shared" and g["dir"].startswith("k8s/games/")}


def exporter_problems(entries):
    """exporter: shared precisa do gameexporter.py no ConfigMap do jogo, senão o import falha só no pod."""
    errors = []
    for g in entries:
        if "exporter" not in g:
            continue
        where = f"{g['dir']}/game.yaml"
        if g["exporter"] != "shared":
            errors.append(f"{where}: exporter '{g['exporter']}' inválido (o único valor é shared; sem o campo, o exporter é do jogo)")
            continue
        kust = (ROOT / g["dir"] / "kustomization.yaml").read_text(encoding="utf-8")
        if "gameexporter.py=scripts/gameexporter.py" not in kust:
            errors.append(f"{g['dir']}/kustomization.yaml: falta gameexporter.py=scripts/gameexporter.py no configMapGenerator")
    return errors


def memory_problems(games):
    """O memory do game.yaml é o limite do container server no StatefulSet: os dois precisam bater."""
    errors = []
    for g in games:
        if g["status"] != "active" or not g.get("memory"):
            continue
        path = ROOT / g["dir"] / "statefulset.yaml"
        sts = [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d and d.get("kind") == "StatefulSet"]
        servers = [c for d in sts for c in d["spec"]["template"]["spec"]["containers"] if c["name"] == "server"]
        if not servers:
            errors.append(f"{g['dir']}/statefulset.yaml: nenhum container chamado server (alertas e dashboard dependem do nome)")
            continue
        limit = (servers[0].get("resources") or {}).get("limits", {}).get("memory")
        if str(limit) != str(g["memory"]):
            errors.append(f"{g['dir']}/game.yaml: memory {g['memory']} diferente do limite do container server ({limit})")
    return errors


def port_name_problems(games):
    """Service (targetPort) e NetworkPolicy apontam as portas pelo nome do game.yaml: o nome precisa existir
    num containerPort do StatefulSet, com o mesmo protocolo. Senão o Service fica sem endpoint e a
    NetworkPolicy bloqueia o jogador, os dois em silêncio."""
    errors = []
    for g in games:
        if g["status"] != "active":
            continue
        path = ROOT / g["dir"] / "statefulset.yaml"
        sts = [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d and d.get("kind") == "StatefulSet"]
        declared = [cp for d in sts for c in d["spec"]["template"]["spec"]["containers"] for cp in c.get("ports", [])]
        by_name = {cp["name"]: cp for cp in declared if cp.get("name")}
        by_number = {cp["containerPort"]: cp for cp in declared}
        wanted = [(p.get("targetPort", p["name"]), p["protocol"]) for p in g["ports"]]
        if g.get("metrics"):
            wanted.append(("metrics", "TCP"))
        for target, protocol in wanted:
            cp = by_number.get(target) if isinstance(target, int) else by_name.get(target)
            if cp is None:
                errors.append(f"{g['dir']}/game.yaml: porta '{target}' não existe em nenhum containerPort de {g['dir']}/statefulset.yaml")
            elif cp.get("protocol", "TCP") != protocol:
                errors.append(f"{g['dir']}/game.yaml: porta '{target}' é {protocol} no catálogo e {cp.get('protocol', 'TCP')} no statefulset.yaml")
    return errors


def port_problems(games):
    """Jogo active só é alcançável nas portas que o VIP do FortiGate encaminha (publishedPorts)."""
    published = set(DEFAULTS["publishedPorts"])
    return [f"{g['dir']}/game.yaml: porta {p['port']}/{p['protocol']} ({p['name']}) fora do VIP do FortiGate "
            f"({', '.join(map(str, sorted(published)))}); crie o encaminhamento e adicione em platform/defaults.yaml publishedPorts"
            for g in games if g["status"] == "active" for p in g["ports"] if p["port"] not in published]


def main():
    check = "--check" in sys.argv
    errors = (catalog.problems() + mute_problems(catalog.games()) + memory_problems(catalog.games())
              + port_problems(catalog.games()) + port_name_problems(catalog.games())
              + exporter_problems(catalog.load()) + pod_security_problems(catalog.games()))
    for key in ("resticImage", "curlImage"):
        if not IMAGE_REF.fullmatch(str(DEFAULTS[key])):
            errors.append(f"platform/defaults.yaml: {key} sem tag e digest válidos (use repo:tag@sha256:<64 hex>)")
    if not re.fullmatch(r"[0-9a-f]{64}", str(DEFAULTS["kubectl"]["sha256"])):
        errors.append("platform/defaults.yaml: kubectl.sha256 precisa ser o SHA-256 do binário (64 hex)")
    # Manifests escritos à mão e Dockerfiles que baixam o kubectl precisam usar a mesma versão e o mesmo hash
    for f in sorted([*(ROOT / "k8s").rglob("*.yaml"), *(ROOT / "apps").rglob("Dockerfile")]):
        text = f.read_text(encoding="utf-8")
        if not text.strip():
            errors.append(f"{f.relative_to(ROOT)}: arquivo vazio (truncado?); o Argo CD apagaria os recursos dele")
        # Toda imagem fixada por tag e digest: uma referência malformada passa no kubeconform e só
        # quebra no cluster (InvalidImageName), como no deploy do games-bot em 14/10/2026
        for ref in re.findall(r"^\s*(?:- )?image:\s*(\S+)", text, re.M) + re.findall(r"^FROM\s+(\S+)", text, re.M):
            if not IMAGE_REF.fullmatch(ref):
                errors.append(f"{f.relative_to(ROOT)}: imagem sem tag e digest válidos: {ref} (use repo:tag@sha256:<64 hex>)")
        # restic escrito à mão (restic-check) usa a mesma imagem do backup gerado: versões diferentes
        # do restic no mesmo repositório são um risco desnecessário
        for ref in re.findall(r"^\s*(?:- )?image:\s*(restic/restic\S*)", text, re.M):
            if ref != DEFAULTS["resticImage"]:
                errors.append(f"{f.relative_to(ROOT)}: restic {ref} diferente do resticImage de platform/defaults.yaml")
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
    outputs.update(render_bot_rbac(catalog.games()))
    outputs.update(render_guardrails(catalog.games()))
    outputs.update(render_games_json(catalog.games()))
    outputs.update(render_shared_exporters(catalog.load()))
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
