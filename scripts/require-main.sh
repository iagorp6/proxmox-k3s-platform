#!/usr/bin/env bash
# Para o comando se o clone não está no main atualizado. Os alvos do make que aplicam algo (deploy,
# plan, segredos, restore) e os playbooks do Ansible chamam isto antes de começar: um clone num branch
# velho ou atrás do origin/main aplica a versão antiga sem erro nenhum, e o sintoma parece bug
# (aconteceu em 18/10, 22/10 e 23/10).
# Recusa quando: o branch não é main, há mudança não commitada em arquivo versionado, ou o main local
# está atrás do origin/main. O docs/restore-drills.md é exceção: o make restore-drill RECORD=1 escreve
# nele, e um drill por jogo em sequência não deve exigir commit entre um e outro.
# Sem rede, o fetch falha e o aviso vira só um alerta.
# FORCE=1 pula tudo, para emergência (ex.: sem internet com o cluster fora do ar).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [[ "${FORCE:-}" == "1" ]]; then
  echo "AVISO: FORCE=1, pulando a conferência do clone (branch, mudanças locais e origin/main)." >&2
  exit 0
fi

g() { git -C "$ROOT" "$@"; }
fail() {
  echo "ERRO: $1" >&2
  echo "      Para corrigir: $2" >&2
  echo "      (FORCE=1 na frente do comando pula esta conferência)" >&2
  exit 1
}

branch="$(g rev-parse --abbrev-ref HEAD)"
[[ "$branch" == "main" ]] || fail "o clone está no branch '$branch', não no main." "git checkout main && git pull --ff-only"

DRILL_LOG="docs/restore-drills.md"
if [[ -n "$(g status --porcelain --untracked-files=no -- . ":(exclude)$DRILL_LOG")" ]]; then
  g status --short --untracked-files=no -- . ":(exclude)$DRILL_LOG" >&2
  fail "há mudanças não commitadas nos arquivos acima." "git stash (guarda) ou git checkout -- . (descarta), depois git pull --ff-only"
fi
if [[ -n "$(g status --porcelain --untracked-files=no -- "$DRILL_LOG")" ]]; then
  echo "AVISO: $DRILL_LOG tem drills registrados sem commit; suba num PR quando terminar." >&2
fi

if ! timeout 20 git -C "$ROOT" fetch --quiet origin main 2>/dev/null; then
  echo "AVISO: não consegui falar com o origin (sem rede?); seguindo com o main local, sem saber se está atualizado." >&2
  exit 0
fi

behind="$(g rev-list --count HEAD..origin/main)"
if [[ "$behind" != "0" ]]; then
  fail "o main local está $behind commit(s) atrás do origin/main." "git pull --ff-only"
fi
ahead="$(g rev-list --count origin/main..HEAD)"
if [[ "$ahead" != "0" ]]; then
  echo "AVISO: o main local tem $ahead commit(s) que não estão no origin/main." >&2
fi
echo "OK: clone no main, igual ao origin/main ($(g rev-parse --short HEAD))."
