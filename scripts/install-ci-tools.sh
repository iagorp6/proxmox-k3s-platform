#!/usr/bin/env bash
# Instala as ferramentas do CI em versões fixas, conferindo o SHA-256 de cada download.
# É a única fonte dessas versões: o GitHub Actions e o scripts/ci-local.sh chamam este script,
# então o que roda na sua máquina é o mesmo binário que roda no CI.
# Uso: scripts/install-ci-tools.sh <diretório de destino> [tflint|kubeconform|trivy|promtool|helm]...
# Para atualizar uma ferramenta: troque a versão e o SHA-256 (publicado na página da release).
set -euo pipefail

TFLINT_VERSION=0.64.0
TFLINT_SHA256=cca9d13e2e1d7a2c627af60ff899a3c9b74212899416aeb96ec764d2ef954537
KUBECONFORM_VERSION=0.8.0
KUBECONFORM_SHA256=9bc2bffbf71f261128533edaf912153948b7ff238f9a531ae6d34466ec287883
TRIVY_VERSION=0.75.0
TRIVY_SHA256=c6e65abddb348e25f10549df887045629cf28cc72453cd1c63acb717316b3f3f
PROMTOOL_VERSION=3.15.0   # vem no pacote do Prometheus
PROMTOOL_SHA256=2a542df32eac02ee17b9d844fb2aa1de00dafa5476579ba8a3ba862e9d572ea0
HELM_VERSION=3.22.1       # só para o scripts/check-charts.py (helm template); o cluster usa o Helm do Argo CD
HELM_SHA256=896cf63684a42a723d61c980369d6e588330927deb13676ca0d85006dd6da0ea

BIN="${1:?uso: $0 <diretório de destino> [tflint|kubeconform|trivy|promtool|helm]...}"
shift
[[ $# -eq 0 ]] && set -- tflint kubeconform trivy promtool helm
mkdir -p "$BIN"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fetch() { # <url> <sha256> <arquivo>: baixa e só segue se o hash bater
  curl -fsSL --retry 3 -o "$3" "$1"
  if ! echo "$2  $3" | sha256sum --check --quiet - 2>/dev/null; then
    # Mostra o hash obtido para conferir com o publicado na release antes de atualizar este script
    echo "ERRO: SHA-256 de $1 não confere: esperado $2, obtido $(sha256sum "$3" | cut -d' ' -f1)" >&2
    return 1
  fi
}

installed_version() { # saída de versão do binário em $BIN (vazia se não existir)
  case "$1" in
    kubeconform) "$BIN/kubeconform" -v 2>/dev/null || true ;;
    helm) "$BIN/helm" version --short 2>/dev/null || true ;;
    *) "$BIN/$1" --version 2>/dev/null || true ;;
  esac
}

for tool in "$@"; do
  case "$tool" in
    tflint) want="$TFLINT_VERSION" ;;
    kubeconform) want="$KUBECONFORM_VERSION" ;;
    trivy) want="$TRIVY_VERSION" ;;
    promtool) want="$PROMTOOL_VERSION" ;;
    helm) want="$HELM_VERSION" ;;
    *)
      echo "ERRO: ferramenta desconhecida: $tool" >&2
      exit 2
      ;;
  esac
  if [[ "$(installed_version "$tool")" == *"$want"* ]]; then
    echo "ok: $tool $want"
    continue
  fi
  echo ">> instalando $tool $want em $BIN"
  case "$tool" in
    tflint)
      fetch "https://github.com/terraform-linters/tflint/releases/download/v${want}/tflint_linux_amd64.zip" "$TFLINT_SHA256" "$TMP/tflint.zip"
      python3 -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extract('tflint', sys.argv[2])" "$TMP/tflint.zip" "$BIN"
      chmod +x "$BIN/tflint"
      ;;
    kubeconform)
      fetch "https://github.com/yannh/kubeconform/releases/download/v${want}/kubeconform-linux-amd64.tar.gz" "$KUBECONFORM_SHA256" "$TMP/kubeconform.tar.gz"
      tar -xzf "$TMP/kubeconform.tar.gz" -C "$BIN" kubeconform
      ;;
    trivy)
      fetch "https://github.com/aquasecurity/trivy/releases/download/v${want}/trivy_${want}_Linux-64bit.tar.gz" "$TRIVY_SHA256" "$TMP/trivy.tar.gz"
      tar -xzf "$TMP/trivy.tar.gz" -C "$BIN" trivy
      ;;
    promtool)
      fetch "https://github.com/prometheus/prometheus/releases/download/v${want}/prometheus-${want}.linux-amd64.tar.gz" "$PROMTOOL_SHA256" "$TMP/prometheus.tar.gz"
      tar -xzf "$TMP/prometheus.tar.gz" -C "$BIN" --strip-components=1 "prometheus-${want}.linux-amd64/promtool"
      ;;
    helm)
      fetch "https://get.helm.sh/helm-v${want}-linux-amd64.tar.gz" "$HELM_SHA256" "$TMP/helm.tar.gz"
      tar -xzf "$TMP/helm.tar.gz" -C "$BIN" --strip-components=1 linux-amd64/helm
      ;;
  esac
done
