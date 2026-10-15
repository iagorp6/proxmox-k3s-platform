#!/usr/bin/env python3
r"""Cria o esqueleto de um jogo novo em k8s/games/<jogo>/ (status draft, fora do cluster).

Gera game.yaml, namespace, kustomization, replicas (0), um StatefulSet com o container "server" e o
sidecar "exporter" já ligados, e o exporter.py do jogo, que usa a lib comum (platform/exporter/
gameexporter.py, copiada para o jogo pelo render-games.py). O driver escolhe como medir o servidor:
  listen  game_up pelo socket da porta do jogo em /proc/net, sem abrir conexão; jogadores em 0
  rcon    Source RCON: comando que lista jogadores + regex com grupo "list" ou "name"
          (gera também o ExternalSecret da senha em /homelab/<jogo>/rcon-password)

Uso:
  scripts/new-game.py <jogo> --title "Valheim" --image repo:tag@sha256:<64 hex> --port 2456 \
      [--protocol UDP] [--memory 4Gi] [--icon 🪓] [--run-as 1000]
  scripts/new-game.py palworld --title Palworld --image ... --port 8211 --protocol UDP \
      --driver rcon --rcon-port 25575 --players-command ShowPlayers \
      --players-regex '^(?P<name>[^,\n]+),\d+,'
  make new-game GAME=valheim TITLE="Valheim" IMAGE=... PORT=2456 PROTOCOL=UDP [DRIVER=rcon ...]

Depois: complete backup.paths e o comando do servidor, troque status para active e rode
python3 scripts/render-games.py (Service, backup, Argo, alertas e RBAC do bot saem do catálogo).
"""
import argparse
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
NAME = re.compile(r"[a-z][a-z0-9-]{1,30}")
IMAGE_REF = re.compile(r"[a-z0-9][a-z0-9._/-]*(?::[0-9]+)?(?:/[a-z0-9._-]+)*:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}")
QUANTITY = re.compile(r"[0-9]+(Mi|Gi)")
PYTHON_IMAGE = "python:3.13-slim@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b"


def exporter_env(a):
    """Variáveis do sidecar exporter conforme o driver (YAML já indentado para o StatefulSet)."""
    env = [("GAME", a.name), ("GAME_DRIVER", a.driver), ("GAME_PORT", str(a.port)), ("GAME_PROTOCOL", a.protocol)]
    if a.driver == "rcon":
        env += [("RCON_PORT", str(a.rcon_port)), ("RCON_PLAYERS_COMMAND", a.players_command),
                ("RCON_PLAYERS_REGEX", a.players_regex)]
    env.append(("STATE_FILE", "/data/exporter/state.json"))
    out = "".join(f"            - name: {k}\n              value: {json.dumps(v, ensure_ascii=False)}\n" for k, v in env)
    if a.driver == "rcon":
        out += f"""\
            - name: RCON_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: {a.name}-secrets
                  key: rcon-password
"""
    return out


def files(a):
    n, port, proto = a.name, a.port, a.protocol
    rcon = a.driver == "rcon"
    out = {
        "game.yaml": f"""\
# Catálogo da plataforma: o render-games.py gera Service, métricas, backup e o App do Argo CD.
# Criado por scripts/new-game.py. Enquanto status for draft, nada deste jogo vai para o cluster.
name: {n}
displayName: {a.title}
icon: {a.icon}            # botão do painel no Discord e resumo semanal
status: draft            # troque para active quando o StatefulSet estiver pronto
memory: {a.memory}           # igual ao limite do container server (o render-games.py --check confere)
monitoring: {a.driver}  # driver do exporter (platform/exporter/gameexporter.py)
exporter: shared          # o render-games.py copia a lib comum para scripts/gameexporter.py
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
{'  - externalsecrets.yaml' + chr(10) if rcon else ''}  - statefulset.yaml
  - platform.generated.yaml   # gerado por scripts/render-games.py
patches:
  - path: replicas.yaml       # gerado por scripts/switch-game.py
configMapGenerator:
  - name: {n}-scripts
    files:
      - exporter.py=scripts/exporter.py
      - gameexporter.py=scripts/gameexporter.py   # lib comum, gerada pelo render-games.py
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
{exporter_env(a)}          ports:
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
        "scripts/exporter.py": f'''"""Exporter do {a.title}: contrato game_*, estado e /metrics vêm da lib comum (gameexporter.py).

O driver sai de GAME_DRIVER no StatefulSet ({a.driver}). Para um protocolo próprio (REST, log...),
troque a última linha por gameexporter.main(players=minha_funcao): ela devolve a lista de nomes
conectados, ou None se o servidor não respondeu.
"""
import gameexporter

gameexporter.main()
''',
    }
    if rcon:
        out["externalsecrets.yaml"] = f"""\
# Senha do RCON: fonte no Bitwarden, publicada no SSM pelo scripts/push-secrets-to-ssm.sh
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: {n}-secrets
  namespace: {n}
spec:
  refreshInterval: 1h
  secretStoreRef:
    kind: ClusterSecretStore
    name: aws-ssm
  target:
    name: {n}-secrets
    creationPolicy: Owner
  data:
    - secretKey: rcon-password
      remoteRef:
        key: /homelab/{n}/rcon-password
"""
    return out


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
    p.add_argument("--driver", default="listen", choices=["listen", "rcon"], help="como o exporter mede o servidor")
    p.add_argument("--rcon-port", type=int, help="porta RCON (driver rcon)")
    p.add_argument("--players-command", help="comando RCON que lista jogadores (driver rcon)")
    p.add_argument("--players-regex", help='regex com grupo "list" (nomes por vírgula) ou "name" (um por jogador)')
    a = p.parse_args()
    errors = []
    if a.driver == "rcon":
        if not (a.rcon_port and a.players_command and a.players_regex):
            errors.append("driver rcon precisa de --rcon-port, --players-command e --players-regex")
        else:
            try:
                if not {"list", "name"} & set(re.compile(a.players_regex).groupindex):
                    errors.append("--players-regex precisa de um grupo nomeado 'list' ou 'name'")
            except re.error as exc:
                errors.append(f"--players-regex inválida: {exc}")
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
    # A lib comum (scripts/gameexporter.py) é cópia gerada: o render já a escreve no jogo em draft
    render = subprocess.run([sys.executable, str(ROOT / "scripts" / "render-games.py")], capture_output=True, text=True)
    print("  " + (render.stdout or render.stderr).strip().replace("\n", "\n  "))
    if render.returncode:
        return render.returncode
    rcon_steps = f"""
  RCON: ligue o RCON no servidor (porta {a.rcon_port}, senha do Secret {a.name}-secrets) e adicione
     [/homelab/{a.name}/rcon-password]={a.name}-rcon em scripts/push-secrets-to-ssm.sh""" if a.driver == "rcon" else ""
    print(f"""
Próximos passos para {a.title}:
  1. statefulset.yaml: comando e variáveis do servidor; backup.paths/verify no game.yaml
  2. Se precisar de senha ou token: externalsecrets.yaml no jogo + entrada em scripts/push-secrets-to-ssm.sh
  3. Exporter: driver {a.driver} já ligado (k8s/games/{a.name}/scripts/exporter.py usa a lib comum){rcon_steps}
  4. status: active no game.yaml e python3 scripts/render-games.py
  5. Porta {a.port}/{a.protocol}: fora de 16261/16262 precisa de VIP no FortiGate
  6. make ci, PR, e depois make switch-game GAME={a.name}""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
