#!/usr/bin/env bash
# Publica no AWS SSM Parameter Store (SecureString) os segredos cuja fonte da verdade é o Bitwarden.
# Uso: export BW_SESSION="$(bw unlock --raw)" && scripts/push-secrets-to-ssm.sh
set -euo pipefail
PROFILE="${AWS_PROFILE:-homelab}"
REGION="${AWS_REGION:-sa-east-1}"
: "${BW_SESSION:?Rode antes: export BW_SESSION=\"\$(bw unlock --raw)\"}"

synced=false
for attempt in 1 2 3; do
  if bw sync >/dev/null 2>&1; then synced=true; break; fi
  echo "AVISO: bw sync falhou (tentativa $attempt/3)" >&2
  sleep 5
done
$synced || echo "AVISO: seguindo com a cópia local do cofre (último sync bem-sucedido)" >&2

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

declare -A MAP=(
  [/homelab/zomboid/admin-password]=zomboid-admin
  [/homelab/zomboid/server-password]=zomboid-entrada
  [/homelab/terraria/server-password]=terraria-entrada
  [/homelab/zomboid/rcon-password]=zomboid-rcon
  [/homelab/backup/restic-password]=restic-zomboid
  [/homelab/monitoring/grafana-admin-password]=grafana-admin
  [/homelab/monitoring/discord-webhook-url]=discord-webhook-alertas
  [/homelab/monitoring/healthchecks-watchdog-url]=healthchecks-watchdog
)

for param in "${!MAP[@]}"; do
  item="${MAP[$param]}"
  value="$(bw get password "$item" 2>/dev/null)" || { echo "ERRO: item '$item' não encontrado no Bitwarden" >&2; exit 1; }
  [[ -n "$value" ]] || { echo "ERRO: item '$item' está vazio" >&2; exit 1; }
  jq -n --arg n "$param" --arg v "$value" '{Name: $n, Value: $v, Type: "SecureString", Overwrite: true}' > "$TMP"
  aws ssm put-parameter --cli-input-json "file://$TMP" --profile "$PROFILE" --region "$REGION" >/dev/null
  : > "$TMP"
  echo "OK: $param <- $item (${#value} caracteres)"
done

aws ssm put-parameter --name /homelab/monitoring/grafana-admin-user --type String --value admin \
  --overwrite --profile "$PROFILE" --region "$REGION" >/dev/null
echo "OK: /homelab/monitoring/grafana-admin-user"
