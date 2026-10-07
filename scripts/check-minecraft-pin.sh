#!/usr/bin/env bash
# Confere que o instalador do cliente (docs/minecraft/install-cobbleverse.ps1) instala a mesma
# versão do modpack que o servidor roda (MODRINTH_VERSION no StatefulSet do Minecraft).
# Sem isso, trocar a versão do servidor deixaria os jogadores instalando a versão antiga.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSTALLER="$ROOT/docs/minecraft/install-cobbleverse.ps1"
STATEFULSET="$ROOT/k8s/games/minecraft/statefulset.yaml"

client="$(grep -oP "^\\\$PinnedVersion = '\K[^']*" "$INSTALLER" || true)"
server="$(grep -A1 'name: MODRINTH_VERSION' "$STATEFULSET" | grep -oP 'value: "?\K[^"[:space:]]+' || true)"

if [[ -z "$client" || -z "$server" ]]; then
  echo "ERRO: não achei a versão do modpack (instalador: '${client}', servidor: '${server}')"
  exit 1
fi
if [[ "$client" != "$server" ]]; then
  echo "ERRO: o instalador fixa a versão '$client', mas o servidor roda '$server'."
  echo "      Atualize \$PinnedVersion em docs/minecraft/install-cobbleverse.ps1."
  exit 1
fi
echo "OK: instalador e servidor na mesma versão do modpack ($server)"
