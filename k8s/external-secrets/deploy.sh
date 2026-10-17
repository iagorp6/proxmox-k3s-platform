#!/usr/bin/env bash
# BREAK-GLASS: no dia a dia o External Secrets é aplicado pelo Argo CD (k8s/argocd/apps/external-secrets.yaml,
# ADR 0007). Use só com o Argo fora do ar; versão e values são os mesmos do Application.
# Nunca rode `helm uninstall`: os CRDs saem junto e levam todos os ExternalSecrets do cluster.
set -euo pipefail
cd "$(dirname "$0")"
VERSION="$(awk '/^ *chart:/{c=1} c && /targetRevision:/{print $2; exit}' ../argocd/apps/external-secrets.yaml)"
[[ -n "$VERSION" ]] || { echo "ERRO: versão do chart não encontrada em ../argocd/apps/external-secrets.yaml" >&2; exit 1; }
helm repo add external-secrets https://charts.external-secrets.io >/dev/null 2>&1 || true
helm repo update external-secrets >/dev/null
echo ">> external-secrets: ${VERSION}"
helm upgrade --install external-secrets external-secrets/external-secrets \
  --namespace external-secrets --create-namespace \
  --version "${VERSION}" -f values-external-secrets.yaml \
  --wait --timeout 10m
kubectl apply -f cluster-secret-store.yaml
