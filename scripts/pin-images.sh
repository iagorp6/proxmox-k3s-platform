#!/usr/bin/env bash
# Fixa imagens de containers pelo digest (imagem:tag@sha256:...) usando as imagens já presentes no node K3s.
# Uso: scripts/pin-images.sh [diretório] (padrão: k8s/)
set -euo pipefail
NODE="${NODE:-ops@192.0.2.5}"
DIR="${1:-$(dirname "$0")/../k8s}"

mapfile -t IMAGES < <(grep -rhoE 'image:\s*[^ @]+' "$DIR" --include='*.yaml' | awk '{print $2}' | grep -v '@' | sort -u)
[[ ${#IMAGES[@]} -eq 0 ]] && { echo "Nada para fixar (todas as imagens já têm digest)."; exit 0; }

for img in "${IMAGES[@]}"; do
  ref="$img"
  [[ "$ref" == */* ]] || ref="library/$ref"
  [[ "${ref%%/*}" == *.* ]] || ref="docker.io/$ref"
  digest="$(ssh "$NODE" "sudo k3s crictl inspecti -o json '$ref' 2>/dev/null" | jq -r '.status.repoDigests[0] // empty' | sed 's/.*@//')"
  if [[ -z "$digest" ]]; then
    echo "AVISO: $img não está no node (rode o workload uma vez e repita)"; continue
  fi
  echo "$img -> $digest"
  grep -rlE "image:\s*${img//./\\.}\s*$" "$DIR" --include='*.yaml' | while read -r f; do
    sed -i -E "s|(image:\s*)${img//./\\.}\s*$|\1${img}@${digest}|" "$f"
  done
done
echo "Pronto. Revise com: git diff"
