#!/usr/bin/env bash
# Roda localmente as mesmas verificações do CI (.github/workflows/ci.yml),
# sobre uma cópia limpa dos arquivos versionados (sem .env.local, tfvars, state etc.).
# Uso: scripts/ci-local.sh [terraform|ansible|kubernetes|scripts|security]...
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$HOME/.local/bin"
mkdir -p "$BIN"
export PATH="$BIN:$PATH"
CRD_CATALOG='https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'

# ---------- ferramentas (instala em ~/.local/bin se faltar) ----------
ensure_tools() {
  if ! command -v tflint >/dev/null; then
    echo ">> instalando tflint"
    curl -fsSL -o /tmp/tflint.zip https://github.com/terraform-linters/tflint/releases/latest/download/tflint_linux_amd64.zip
    python3 -c "import zipfile,sys; zipfile.ZipFile('/tmp/tflint.zip').extract('tflint', sys.argv[1])" "$BIN"
    chmod +x "$BIN/tflint"
  fi
  if ! command -v kubeconform >/dev/null; then
    echo ">> instalando kubeconform"
    curl -fsSL https://github.com/yannh/kubeconform/releases/latest/download/kubeconform-linux-amd64.tar.gz | tar xz -C "$BIN" kubeconform
  fi
  if ! command -v trivy >/dev/null; then
    echo ">> instalando trivy"
    curl -sfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh | sh -s -- -b "$BIN" >/dev/null
  fi
  if ! command -v ansible-lint >/dev/null; then
    echo ">> instalando ansible-lint"
    pipx install --include-deps ansible-lint >/dev/null
  fi
  command -v shellcheck >/dev/null || { echo "ERRO: instale o shellcheck (sudo apt-get install -y shellcheck)"; exit 2; }
  local out tool
  for tool in terraform tflint kubeconform trivy ansible-lint shellcheck; do
    out="$(tool_version "$tool" 2>&1 || true)"
    [[ "$out" =~ [0-9]+\.[0-9]+ ]] || { echo "ERRO: $tool não responde com a versão (instalação corrompida?)"; exit 2; }
  done
}

tool_version() {
  case "$1" in
    terraform)   terraform version ;;
    kubeconform) kubeconform -v ;;
    *)           "$1" --version ;;
  esac
}

# ---------- cópia limpa: só o que o Git versionaria ----------
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
(cd "$ROOT" && git ls-files -z -co --exclude-standard | tar --null -T - -cf -) | tar -xf - -C "$WORK"

FAILED=()
step() {
  local name="$1"; shift
  printf '\n=== %s\n' "$name"
  if (cd "$WORK" && "$@"); then printf -- '--- OK: %s\n' "$name"; else printf -- '--- FALHOU: %s\n' "$name"; FAILED+=("$name"); fi
}

tf_check() {
  local dir="$1"
  cd "$dir" || return 1
  terraform fmt -check -recursive -diff &&
    terraform init -backend=false -input=false >/dev/null &&
    terraform validate -no-color &&
    tflint --init --config "$WORK/.tflint.hcl" >/dev/null &&
    tflint --config "$WORK/.tflint.hcl" --format compact
}

k8s_check() {
  kubectl kustomize k8s/zomboid | kubeconform -strict -summary -schema-location default -schema-location "$CRD_CATALOG" || return 1
  kubectl kustomize k8s/games/terraria | kubeconform -strict -summary -schema-location default -schema-location "$CRD_CATALOG" || return 1
  kubeconform -strict -summary -schema-location default -schema-location "$CRD_CATALOG" \
    k8s/monitoring/alerts-homelab.yaml k8s/monitoring/dashboard-asunboid.yaml k8s/monitoring/externalsecrets.yaml \
    k8s/argocd/root.yaml k8s/argocd/apps/*.yaml k8s/external-secrets/cluster-secret-store.yaml || return 1
  cp k8s/monitoring/dashboard-asunboid.yaml /tmp/dashboard-committed.yaml
  python3 scripts/build-dashboard.py >/dev/null || return 1
  if ! cmp -s /tmp/dashboard-committed.yaml k8s/monitoring/dashboard-asunboid.yaml; then
    echo "dashboard YAML fora de sincronia com scripts/build-dashboard.py"
    return 1
  fi
}

scripts_check() {
  shellcheck scripts/*.sh k8s/*/deploy.sh && python3 -m py_compile k8s/zomboid/exporter/*.py k8s/games/terraria/scripts/*.py scripts/*.py
}

security_check() {
  trivy fs --quiet --scanners secret --exit-code 1 --no-progress . &&
    trivy config --quiet --severity HIGH,CRITICAL --exit-code 0 . | tail -25
}

ensure_tools
TARGETS=("$@")
[[ ${#TARGETS[@]} -eq 0 ]] && TARGETS=(terraform ansible kubernetes scripts security)
for t in "${TARGETS[@]}"; do
  case "$t" in
    terraform)  step "terraform (proxmox)" tf_check terraform; step "terraform (aws)" tf_check terraform-aws ;;
    ansible)    step "ansible-lint" bash -c 'cd ansible && ansible-lint' ;;
    kubernetes) step "kubernetes" k8s_check ;;
    scripts)    step "scripts" scripts_check ;;
    security)   step "security" security_check ;;
    *) echo "alvo desconhecido: $t"; exit 2 ;;
  esac
done

printf '\n==============================\n'
if [[ ${#FAILED[@]} -eq 0 ]]; then echo "TUDO OK"; else printf 'FALHARAM: %s\n' "${FAILED[@]}"; exit 1; fi
