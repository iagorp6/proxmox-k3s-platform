#!/usr/bin/env bash
# Instala/atualiza o External Secrets Operator (versão fixada em chart-version-eso.txt) e o ClusterSecretStore
set -euo pipefail
cd "$(dirname "$0")"
helm repo add external-secrets https://charts.external-secrets.io >/dev/null 2>&1 || true
helm repo update external-secrets >/dev/null
if [[ ! -s chart-version-eso.txt ]]; then
  helm search repo external-secrets/external-secrets -o json | jq -r '.[] | select(.name=="external-secrets/external-secrets") | .version' | head -1 > chart-version-eso.txt
fi
VERSION="$(cat chart-version-eso.txt)"
echo ">> external-secrets: ${VERSION}"
helm upgrade --install external-secrets external-secrets/external-secrets \
  --namespace external-secrets --create-namespace \
  --version "${VERSION}" -f values-external-secrets.yaml \
  --wait --timeout 10m
kubectl apply -f cluster-secret-store.yaml
