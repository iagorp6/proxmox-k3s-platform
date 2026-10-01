#!/usr/bin/env bash
# Instala/atualiza o kube-prometheus-stack com versão de chart fixada em chart-version.txt
set -euo pipefail
cd "$(dirname "$0")"
helm repo update prometheus-community >/dev/null
if [[ ! -s chart-version.txt ]]; then
  helm search repo prometheus-community/kube-prometheus-stack -o json | jq -r '.[0].version' > chart-version.txt
fi
VERSION="$(cat chart-version.txt)"
echo "kube-prometheus-stack chart version: ${VERSION}"
helm upgrade --install kps prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  --version "${VERSION}" \
  -f values-kube-prometheus-stack.yaml \
  --wait --timeout 15m
