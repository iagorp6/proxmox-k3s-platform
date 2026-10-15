#!/usr/bin/env python3
"""Cria o esqueleto de um jogo novo em k8s/games/<jogo>/ (status draft, fora do cluster).

Gera game.yaml, namespace, kustomization, replicas (0), um StatefulSet com o container "server" e o
sidecar "exporter" já ligados, e um exporter genérico no contrato game_* que marca game_up pelo socket
em LISTEN na porta do jogo (lido de /proc/net, sem abrir conexão). Contagem de jogadores fica em 0 até
o exporter ganhar o protocolo do jogo (RCON, REST, log...).

Uso:
  scripts/new-game.py <jogo> --title "Valheim" --image repo:tag@sha256:<64 hex> --port 16261 \
      [--protocol UDP] [--memory 4Gi] [--icon 🪓] [--run-as 1000]
  make new-game GAME=valheim TITLE="Valheim" IMAGE=... PORT=16261 PROTOCOL=UDP

Depois: complete backup.paths e o comando do servidor, troque status para active e rode
python3 scripts/render-games.py (Service, backup, Argo, alertas e RBAC do bot saem do catálogo).
"""
import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE_EXPORTER = ROOT / "scripts" / "templates" / "exporter.py"
NAME = re.compile(r"[a-z][a-z0-9-]{1,30}")
IMAGE_REF = re.compile(r"[a-z0-9][a-z0-9._/-]*(?::[0-9]+)?(?:/[a-z0-9._-]+)*:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}")
QUANTITY = re.compile(r"[0-9]+(Mi|Gi)")
PYTHON_IMAGE = "python:3.13-slim@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b"


def files(a):
    n, port, proto = a.name, a.port, a.protocol
    return {
        "game.yaml": f"""\
# Catálogo da plataforma: o render-games.py gera Service, métricas, backup e o App do Argo CD.
# Criado por scripts/new-game.py. Enquanto status for draft, nada deste jogo vai para o cluster.
name: {n}
displayName: {a.title}
icon: {a.icon}            # botão do painel no Discord e resumo semanal
status: draft            # troque para active quando o StatefulSet estiver pronto
memory: {a.memory}           # igual ao limite do container server (o render-games.py --check confere)
monitoring: listen-probe  # exporter genérico; troque quando o exporter falar o protocolo do jogo
namespace: {n}
runAsUser: {a.run_as}
ports:
  - {{ name: game, port: {port}, protocol: {proto} }}
backup:
  schedule: "0 5 * * *"
  pvc: data-{n}-0
  paths: [/data/world]     # PVC montado em /data no job de backup: aponte para o mundo do jogo
  verify: [data/world/*]   # restore drill: o que precisa voltar (scripts/restore-drill.py)
metrics: true
# switch:
#   saveCommand: echo save > /tmp/console; sleep 10   # roda no container server antes do backup e da troca
#   joinHint: "Como entrar no servidor"
""",
        "namespace.yaml": f"""\
apiVersion: v1
kind: Namespace
metadata:
  name: {n}
""",
        "replicas.yaml": f"""\
# Gerado por scripts/switch-game.py: não edite à mão.
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: {n}
  namespace: {n}
spec:
  replicas: 0
""",
        "kustomization.yaml": f"""\
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: {n}
resources:
  - namespace.yaml
  - statefulset.yaml
  - platform.generated.yaml   # gerado por scripts/render-games.py
patches:
  - path: replicas.yaml       # gerado por scripts/switch-game.py
configMapGenerator:
  - name: {n}-scripts
    files:
      - exporter.py=scripts/exporter.py
generatorOptions:
  disableNameSuffixHash: true
""",
        "statefulset.yaml": f"""\
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: {n}
  namespace: {n}
  labels:
    app.kubernetes.io/name: {n}
    platform.homelab/game: {n}
spec:
  serviceName: {n}
  replicas: 0
  selector:
    matchLabels:
      app.kubernetes.io/name: {n}
  template:
    metadata:
      labels:
        app.kubernetes.io/name: {n}
        platform.homelab/game: {n}
    spec:
      terminationGracePeriodSeconds: 60
      securityContext:
        runAsNonRoot: true
        runAsUser: {a.run_as}
        runAsGroup: {a.run_as}
        fsGroup: {a.run_as}
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: server            # o nome "server" é usado por alertas, dashboard, backup e troca de jogo
          image: {a.image}
          # Ajuste comando, variáveis e caminhos conforme a imagem do jogo.
          ports:
            - name: game
              containerPort: {port}
              protocol: {proto}
          readinessProbe:
            exec:
              # Sonda passiva: socket em LISTEN (TCP) ou aberto (UDP) na porta do jogo, sem ocupar vaga
              command: ["sh", "-c", "{readiness_command(port, proto)}"]
            periodSeconds: 15
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
          resources:
            requests:
              cpu: 200m
              memory: {a.memory}
            limits:
              memory: {a.memory}
          volumeMounts:
            - name: data
              mountPath: /data
        - name: exporter
          image: {PYTHON_IMAGE}
          command: ["python", "/scripts/exporter.py"]
          env:
            - name: GAME
              value: {n}
            - name: GAME_PORT
              value: "{port}"
            - name: GAME_PROTOCOL
              value: {proto}
            - name: STATE_FILE
              value: /data/exporter/state.json
          ports:
            - name: metrics
              containerPort: 9105
              protocol: TCP
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
          resources:
            requests:
              cpu: 10m
              memory: 32Mi
            limits:
              memory: 96Mi
          volumeMounts:
            - name: scripts
              mountPath: /scripts
              readOnly: true
            - name: data
              mountPath: /data/exporter
              subPath: exporter
      volumes:
        - name: scripts
          configMap:
            name: {n}-scripts
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: ["ReadWriteOnce"]
        storageClassName: local-path
        resources:
          requests:
            storage: 10Gi
""",
        "scripts/exporter.py": TEMPLATE_EXPORTER.read_text(encoding="utf-8"),
    }


def readiness_command(port, proto):
    """Mesmo teste do exporter: socket da porta em /proc/net (TCP em LISTEN, UDP aberto)."""
    hexport = f"{port:04X}"
    if proto == "TCP":
        return f"grep -sqiE ':{hexport} [0-9A-F]+:0000 0A' /proc/net/tcp /proc/net/tcp6"
    return f"grep -sqiE ':{hexport} [0-9A-F]+:0000 07' /proc/net/udp /proc/net/udp6"


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("name")
    p.add_argument("--title", required=True, help="nome exibido (displayName)")
    p.add_argument("--image", required=True, help="imagem do servidor, repo:tag@sha256:<digest>")
    p.add_argument("--port", required=True, type=int)
    p.add_argument("--protocol", default="TCP", type=str.upper, choices=["TCP", "UDP"])
    p.add_argument("--memory", default="4Gi")
    p.add_argument("--icon", default="🎮")
    p.add_argument("--run-as", default=1000, type=int)
    a = p.parse_args()
    errors = []
    if not NAME.fullmatch(a.name):
        errors.append(f"nome '{a.name}' inválido: minúsculas, números e hífen (vira namespace e StatefulSet)")
    if not IMAGE_REF.fullmatch(a.image):
        errors.append(f"imagem '{a.image}' sem tag e digest (use repo:tag@sha256:<64 hex>; scripts/pin-images.sh ajuda)")
    if not 1 <= a.port <= 65535:
        errors.append(f"porta {a.port} inválida")
    if not QUANTITY.fullmatch(a.memory):
        errors.append(f"memória '{a.memory}' inválida (ex.: 4Gi, 2560Mi)")
    target = ROOT / "k8s" / "games" / a.name
    if target.exists():
        errors.append(f"{target.relative_to(ROOT)} já existe")
    if errors:
        print("ERRO:\n" + "\n".join(f"  {e}" for e in errors))
        return 1
    for rel, content in files(a).items():
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(f"  criado {path.relative_to(ROOT)}")
    print(f"""
Próximos passos para {a.title}:
  1. statefulset.yaml: comando e variáveis do servidor; backup.paths/verify no game.yaml
  2. Se precisar de senha ou token: externalsecrets.yaml no jogo + entrada em scripts/push-secrets-to-ssm.sh
  3. Exporter: k8s/games/{a.name}/scripts/exporter.py já marca game_up pela porta; troque players() pelo protocolo do jogo
  4. status: active no game.yaml e python3 scripts/render-games.py
  5. Porta {a.port}/{a.protocol}: fora de 16261/16262 precisa de VIP no FortiGate
  6. make ci, PR, e depois make switch-game GAME={a.name}""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
