#!/usr/bin/env bash
# Teste do scripts/new-game.py: cria um jogo de exemplo numa cópia dos arquivos versionados, confere
# que em draft ele fica fora do catálogo, ativa, gera os manifests e valida tudo com kustomize e
# kubeconform. Nada é gravado no repositório. Uso: scripts/test-new-game.sh <schema-location de CRDs>
set -euo pipefail
CRDS="${1:?uso: $0 <schema-location dos CRDs para o kubeconform>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
git -C "$ROOT" ls-files -z | (cd "$ROOT" && xargs -0 cp --parents -t "$WORK")
cd "$WORK"
image="$(awk '/^resticImage:/ {print $2}' platform/defaults.yaml)"   # qualquer imagem fixada serve para o teste
# Um jogo por driver: listen (porta UDP) e rcon (ExternalSecret da senha + regex com grupo name)
python3 scripts/new-game.py exemplo --title Exemplo --image "$image" --port 2456 --protocol UDP --memory 1Gi >/dev/null
python3 scripts/new-game.py exemplo-rcon --title "Exemplo RCON" --image "$image" --port 27015 \
  --driver rcon --rcon-port 27020 --players-command players --players-regex '^-(?P<name>.+)$' >/dev/null
for game in exemplo exemplo-rcon; do
  if python3 scripts/catalog.py dirs | grep -qx "k8s/games/$game"; then
    echo "ERRO: jogo em draft apareceu no catálogo ($game)"; exit 1
  fi
done
python3 scripts/render-games.py --check >/dev/null   # a cópia da lib já nasce com o draft
for game in exemplo exemplo-rcon; do
  sed -i 's/^status: draft .*/status: active/' "k8s/games/$game/game.yaml"
done
# Porta fora do VIP do FortiGate (platform/defaults.yaml publishedPorts) barra o jogo active
if python3 scripts/render-games.py --check >"$WORK/ports.log" 2>&1 || ! grep -q "fora do VIP do FortiGate" "$WORK/ports.log"; then
  echo "ERRO: --check não barrou porta fora de publishedPorts"; cat "$WORK/ports.log"; exit 1
fi
sed -i 's/port: 2456,/port: 16261,/' k8s/games/exemplo/game.yaml
sed -i 's/port: 27015,/port: 16262,/' k8s/games/exemplo-rcon/game.yaml
python3 scripts/render-games.py >/dev/null
python3 scripts/render-games.py --check >/dev/null
# O jogo novo entra no guardrails (namespace restricted, AppProject games) e o scaffold cumpre o restricted
grep -q "name: exemplo-rcon" k8s/argocd/guardrails.generated.yaml
python3 scripts/check-guardrails.py >/dev/null
for game in exemplo exemplo-rcon; do
  kubectl kustomize "k8s/games/$game" | kubeconform -strict -summary -schema-location default -schema-location "$CRDS"
  python3 scripts/restore-drill.py "$game" --dry-run | kubeconform -strict -summary -schema-location default
  python3 -m py_compile "k8s/games/$game/scripts/exporter.py" "k8s/games/$game/scripts/gameexporter.py"
  grep -q "\"$game\"" k8s/platform/games.generated.json
done
# O ConfigMap leva a lib e o exporter fino, e o env do driver rcon chega ao sidecar com a regex intacta
kubectl kustomize k8s/games/exemplo-rcon > rendered.yaml
python3 - rendered.yaml <<'PY'
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
cm = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "exemplo-rcon-scripts")
assert {"exporter.py", "gameexporter.py"} <= set(cm["data"]), cm["data"].keys()
sts = next(d for d in docs if d["kind"] == "StatefulSet")
env = {e["name"]: e for c in sts["spec"]["template"]["spec"]["containers"] if c["name"] == "exporter" for e in c["env"]}
assert env["GAME_DRIVER"]["value"] == "rcon" and env["RCON_PORT"]["value"] == "27020"
assert env["RCON_PLAYERS_REGEX"]["value"] == "^-(?P<name>.+)$", env["RCON_PLAYERS_REGEX"]
assert env["RCON_PASSWORD"]["valueFrom"]["secretKeyRef"]["name"] == "exemplo-rcon-secrets"
assert any(d["kind"] == "ExternalSecret" for d in docs)
PY
# Exporter fino + lib rodam de verdade: o sidecar responde /metrics com game_up do driver listen
(cd k8s/games/exemplo/scripts && GAME=exemplo GAME_PORT=2456 GAME_PROTOCOL=UDP EXPORTER_PORT=19105 \
  STATE_FILE="$WORK/state/state.json" POLL_SECONDS=1 timeout 10 python3 exporter.py >/dev/null 2>&1 &)
for _ in $(seq 20); do
  metrics="$(curl -fs http://127.0.0.1:19105/metrics || true)"
  [[ "$metrics" == *'game_up{game="exemplo"}'* ]] && break
  sleep 0.3
done
[[ "$metrics" == *'game_up{game="exemplo"}'* ]] || { echo "ERRO: exporter do scaffold não respondeu /metrics"; exit 1; }
echo "OK: scaffold do new-game.py gera jogos válidos (drivers listen e rcon)"
