#!/usr/bin/env bash
# Confere o SHA-256 do kubectl fixado em platform/defaults.yaml contra o publicado em dl.k8s.io.
# O Renovate sobe a versão nos três lugares que baixam o kubectl (defaults.yaml, Dockerfile do bot e
# daily-restart do Zomboid) mas não sabe calcular o hash: o PR dele fica vermelho aqui, com o comando
# que troca o hash em todos eles, e o render-games.py --check confere que ficaram iguais.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEFAULTS="$ROOT/platform/defaults.yaml"

version="$(grep -oP '^kubectl:.*version:\s*\K[^,\s}]+' "$DEFAULTS" || true)"
pinned="$(grep -oP '^kubectl:.*sha256:\s*"\K[0-9a-f]{64}' "$DEFAULTS" || true)"
if [[ -z "$version" || -z "$pinned" ]]; then
  echo "ERRO: não achei versão e sha256 do kubectl em platform/defaults.yaml"
  exit 1
fi
upstream="$(curl -fsSL --retry 3 "https://dl.k8s.io/release/${version}/bin/linux/amd64/kubectl.sha256")"
if [[ "$pinned" != "$upstream" ]]; then
  echo "ERRO: o sha256 do kubectl ${version} em platform/defaults.yaml não confere com dl.k8s.io."
  echo "      Para corrigir (troca o hash antigo em todo o repo e regenera os manifests):"
  echo "      git grep -l ${pinned} | xargs sed -i 's/${pinned}/${upstream}/' && python3 scripts/render-games.py"
  exit 1
fi
echo "OK: kubectl ${version} com o sha256 publicado em dl.k8s.io"
