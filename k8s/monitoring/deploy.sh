#!/usr/bin/env bash
# Instala/atualiza a stack de observabilidade com versões de chart fixadas em chart-version-*.txt
set -euo pipefail
cd "$(dirname "$0")"

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo add grafana https://grafana.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update >/dev/null

pin() {
  local chart="$1" file="$2"
  if [[ ! -s "$file" ]]; then
    helm search repo "$chart" -o json | jq -r --arg c "$chart" '.[] | select(.name==$c) | .version' | head -1 > "$file"
  fi
  cat "$file"
}

install() {
  local release="$1" chart="$2" file="$3" values="$4" version
  version="$(pin "$chart" "$file")"
  echo ">> ${release}: ${chart} ${version}"
  local extra=()
  [[ -f "${values%.yaml}.local.yaml" ]] && extra=(-f "${values%.yaml}.local.yaml")
  helm upgrade --install "$release" "$chart" \
    --namespace monitoring --create-namespace \
    --version "$version" -f "$values" "${extra[@]}" --wait --timeout 15m
}

[[ -s chart-version-kps.txt ]] || { [[ -s chart-version.txt ]] && mv chart-version.txt chart-version-kps.txt; }

install kps   prometheus-community/kube-prometheus-stack chart-version-kps.txt   values-kube-prometheus-stack.yaml
install loki  grafana/loki                               chart-version-loki.txt  values-loki.yaml
install alloy grafana/alloy                              chart-version-alloy.txt values-alloy.yaml

kubectl apply -f alerts-homelab.yaml -f dashboard-asunboid.yaml
echo "OK: stack de observabilidade aplicada"
