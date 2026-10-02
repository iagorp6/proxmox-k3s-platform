#!/usr/bin/env bash
# Instala/atualiza o Argo CD com versão de chart fixada em chart-version-argocd.txt
set -euo pipefail
cd "$(dirname "$0")"
helm repo add argo https://argoproj.github.io/argo-helm >/dev/null 2>&1 || true
helm repo update argo >/dev/null
if [[ ! -s chart-version-argocd.txt ]]; then
  helm search repo argo/argo-cd -o json | jq -r '.[] | select(.name=="argo/argo-cd") | .version' | head -1 > chart-version-argocd.txt
fi
VERSION="$(cat chart-version-argocd.txt)"
echo ">> argocd: argo/argo-cd ${VERSION}"
helm upgrade --install argocd argo/argo-cd \
  --namespace argocd --create-namespace \
  --version "${VERSION}" -f values-argocd.yaml \
  --wait --timeout 15m
