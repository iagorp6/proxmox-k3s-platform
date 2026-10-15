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
python3 scripts/new-game.py exemplo --title Exemplo --image "$image" --port 2456 --protocol UDP --memory 1Gi >/dev/null
if python3 scripts/catalog.py dirs | grep -qx k8s/games/exemplo; then
  echo "ERRO: jogo em draft apareceu no catálogo"; exit 1
fi
python3 scripts/render-games.py --check >/dev/null
sed -i 's/^status: draft .*/status: active/' k8s/games/exemplo/game.yaml
python3 scripts/render-games.py >/dev/null
python3 scripts/render-games.py --check >/dev/null
kubectl kustomize k8s/games/exemplo | kubeconform -strict -summary -schema-location default -schema-location "$CRDS"
python3 scripts/restore-drill.py exemplo --dry-run | kubeconform -strict -summary -schema-location default
python3 -m py_compile k8s/games/exemplo/scripts/exporter.py
grep -q '"exemplo"' k8s/platform/games.generated.json
echo "OK: scaffold do new-game.py gera um jogo válido"
