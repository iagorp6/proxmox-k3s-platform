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
  # tflint, kubeconform, trivy e promtool: mesmas versões e hashes do CI (reinstala se a versão local for outra)
  "$ROOT/scripts/install-ci-tools.sh" "$BIN" || { echo "ERRO: falha ao instalar as ferramentas do CI"; exit 2; }
  if ! command -v ansible-lint >/dev/null; then
    echo ">> instalando ansible-lint"
    pipx install --include-deps "ansible-lint==26.9.0" >/dev/null   # mesma versão do ci.yml
  fi
  command -v shellcheck >/dev/null || { echo "ERRO: instale o shellcheck (sudo apt-get install -y shellcheck)"; exit 2; }
  local out tool
  for tool in terraform tflint kubeconform trivy promtool ansible-lint shellcheck; do
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
  local dir dirs
  dirs="$(python3 scripts/catalog.py dirs)" || return 1
  for dir in $dirs; do   # todos os jogos do catálogo (k8s/**/game.yaml)
    kubectl kustomize "$dir" | kubeconform -strict -summary -schema-location default -schema-location "$CRD_CATALOG" || return 1
  done
  kubeconform -strict -summary -schema-location default -schema-location "$CRD_CATALOG" \
    k8s/monitoring/alerts-homelab.yaml k8s/monitoring/alerts-games.generated.yaml k8s/monitoring/dashboard-asunboid.yaml k8s/monitoring/externalsecrets.yaml \
    k8s/argocd/root.yaml k8s/argocd/apps/*.yaml k8s/external-secrets/cluster-secret-store.yaml || return 1
  python3 scripts/test-alerts.py || return 1
  cp k8s/monitoring/dashboard-asunboid.yaml /tmp/dashboard-committed.yaml
  python3 scripts/build-dashboard.py >/dev/null || return 1
  if ! cmp -s /tmp/dashboard-committed.yaml k8s/monitoring/dashboard-asunboid.yaml; then
    echo "dashboard YAML fora de sincronia com scripts/build-dashboard.py"
    return 1
  fi
}

scripts_check() {
  shellcheck scripts/*.sh k8s/*/deploy.sh && python3 scripts/render-games.py --check && python3 -m py_compile k8s/zomboid/exporter/*.py k8s/games/*/scripts/*.py scripts/*.py
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
