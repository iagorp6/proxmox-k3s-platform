#!/usr/bin/env bash
# Estado da plataforma de jogos: jogo ativo, réplicas, restart diário e alertas disparados.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
echo "== jogo ativo (Git): $(grep -oP '^active:\s*\K\S+' "$ROOT/platform/active-game.yaml")"
for g in "zomboid zomboid" "terraria terraria" "minecraft minecraft"; do
  # shellcheck disable=SC2086  # separar "namespace statefulset" em dois argumentos é intencional
  set -- $g
  kubectl -n "$1" get sts "$2" -o jsonpath="   $2: réplicas={.spec.replicas} prontas={.status.readyReplicas}{'\n'}"
done
kubectl -n zomboid get cronjob zomboid-daily-restart -o jsonpath='   restart diário do Zomboid suspenso: {.spec.suspend}{"\n"}'
echo "== alertas disparados (fora Watchdog e jogadores):"
kubectl -n monitoring port-forward svc/kps-kube-prometheus-stack-prometheus 9091:9090 >/dev/null 2>&1 &
PF=$!
trap 'kill "$PF" 2>/dev/null' EXIT
sleep 3
if OUT="$(curl -sf 'http://localhost:9091/api/v1/alerts')"; then
  echo "$OUT" | jq -r '.data.alerts[] | select(.labels.alertname | test("Watchdog|InfoInhibitor|PlayerOnline") | not) | "   \(.labels.alertname): \(.state)"' | grep . || echo "   (nenhum)"
else
  echo "   ERRO: não consegui consultar o Prometheus"
  exit 1
fi
