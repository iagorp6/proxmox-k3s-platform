#!/usr/bin/env bash
# BREAK-GLASS: no dia a dia quem aplica kps, Loki e Alloy é o Argo CD (k8s/argocd/apps/, ADR 0007).
# Use só com o Argo fora do ar. Versão e values são os mesmos dos Applications, então o próximo Sync
# do Argo não vê diferença. Nunca rode `helm uninstall`: apagaria Prometheus, PVCs e CRDs.
set -euo pipefail
cd "$(dirname "$0")"
APPS=../argocd/apps

# targetRevision da fonte de chart do Application (a primeira, que vem antes do repo Git)
chart_version() {
  awk '/^ *chart:/{c=1} c && /targetRevision:/{print $2; exit}' "$APPS/$1.yaml"
}

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo add grafana https://grafana.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update >/dev/null

install() {
  local release="$1" chart="$2" app="$3" values="$4" version
  version="$(chart_version "$app")"
  [[ -n "$version" ]] || { echo "ERRO: versão do chart não encontrada em $APPS/$app.yaml" >&2; exit 1; }
  echo ">> ${release}: ${chart} ${version}"
  helm upgrade --install "$release" "$chart" \
    --namespace monitoring --create-namespace \
    --version "$version" -f "$values" --wait --timeout 15m
}

install kps   prometheus-community/kube-prometheus-stack kube-prometheus-stack values-kube-prometheus-stack.yaml
install loki  grafana/loki                               loki                  values-loki.yaml
install alloy grafana/alloy                              alloy                 values-alloy.yaml

kubectl apply -f alerts-homelab.yaml -f alerts-games.generated.yaml -f dashboard-asunboid.yaml -f externalsecrets.yaml
echo "OK: stack de observabilidade aplicada"
