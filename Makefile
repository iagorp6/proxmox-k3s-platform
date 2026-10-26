# Atalhos do dia a dia. Rode `make help` para listar.
# Usa ">" como prefixo de receita (evita problemas de tab ao colar).
.RECIPEPREFIX = >
.DEFAULT_GOAL := help
.PHONY: help require-main ci dashboard secrets plan deploy-monitoring deploy-eso deploy-argocd switch-game new-game restore-drill restore-game console status sync-game host-status guardrails

help: ## Lista os alvos disponíveis
> @grep -E '^[a-zA-Z_%-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# Os alvos que aplicam algo dependem deste: param se o clone não está no main atualizado (FORCE=1 pula)
require-main:
> @./scripts/require-main.sh

ci: ## Roda localmente todas as verificações do CI
> ./scripts/ci-local.sh

ci-%: ## Roda um grupo do CI (terraform, ansible, kubernetes, scripts, security)
> ./scripts/ci-local.sh $*

dashboard: ## Regenera o dashboard do Grafana a partir do gerador
> python3 scripts/build-dashboard.py

secrets: require-main ## Publica no AWS SSM os segredos do Bitwarden (exige BW_SESSION)
> ./scripts/push-secrets-to-ssm.sh

plan: require-main ## terraform plan nos dois stacks (Proxmox e AWS)
> bash -c 'source ~/.proxmox.env && cd terraform && terraform plan'
> cd terraform-aws && terraform plan

deploy-monitoring: require-main ## Break-glass: stack de observabilidade via Helm (no dia a dia o Argo CD aplica no merge)
> ./k8s/monitoring/deploy.sh

deploy-eso: require-main ## Break-glass: External Secrets via Helm (no dia a dia o Argo CD aplica no merge)
> ./k8s/external-secrets/deploy.sh

deploy-argocd: require-main ## Instala/atualiza o Argo CD (único release fora do Argo)
> ./k8s/argocd/deploy.sh

guardrails: require-main ## Namespaces com Pod Security, AppProjects e app raiz (fora do autosync; rode após jogo novo)
> ./k8s/argocd/apply-guardrails.sh

switch-game: ## Troca o jogo ativo (ex.: make switch-game GAME=terraria)
> ./scripts/switch-game.py $(GAME)

# Em vez do Sync pela UI do Argo: confere jogadores e salva antes, como a troca
sync-game: ## Aplica no jogo o que mudou no main, sem trocar (whitelist, imagem; ex.: make sync-game GAME=minecraft)
> $(if $(GAME),,$(error uso: make sync-game GAME=minecraft))
> ./scripts/switch-game.py $(GAME) --resync

# PLAYERS_REGEX com $ precisa virar $$ no make; com regex complexa, chame scripts/new-game.py direto
new-game: ## Esqueleto de jogo novo, status draft (ex.: make new-game GAME=valheim TITLE=Valheim IMAGE=repo:tag@sha256:... PORT=16261 PROTOCOL=UDP)
> ./scripts/new-game.py $(GAME) --title "$(TITLE)" --image "$(IMAGE)" --port $(PORT) $(if $(PROTOCOL),--protocol $(PROTOCOL)) $(if $(MEMORY),--memory $(MEMORY)) $(if $(ICON),--icon "$(ICON)") $(if $(DRIVER),--driver $(DRIVER)) $(if $(RCON_PORT),--rcon-port $(RCON_PORT)) $(if $(QUERY_PORT),--query-port $(QUERY_PORT)) $(if $(PLAYERS_COMMAND),--players-command "$(PLAYERS_COMMAND)") $(if $(PLAYERS_REGEX),--players-regex '$(PLAYERS_REGEX)')

restore-drill: require-main ## Restaura o último backup num PVC temporário e mede o tempo (ex.: make restore-drill GAME=terraria RECORD=1)
> ./scripts/restore-drill.py $(GAME) $(if $(RECORD),--record)

restore-game: require-main ## Restaura o último backup no volume do jogo, com o servidor parado (DR; ex.: make restore-game GAME=minecraft)
> ./scripts/restore-drill.py $(GAME) --live

# CMD vai literal para o FIFO /tmp/console do container server (Zomboid e Terraria); a resposta sai no log
console: ## Comando no console do servidor (ex.: make console GAME=zomboid CMD='servermsg "restart em 5 min"')
> $(if $(and $(GAME),$(CMD)),,$(error uso: make console GAME=zomboid CMD='comando'))
> kubectl -n $(GAME) exec $(GAME)-0 -c server -- sh -c 'test -p /tmp/console && printf "%s\n" "$$1" > /tmp/console' _ '$(CMD)'
> @sleep 3; kubectl -n $(GAME) logs $(GAME)-0 -c server --since=10s | tail -n 20

status: ## Estado da plataforma (jogo ativo, réplicas, alertas)
> ./scripts/platform-status.sh

host-status: ## Estado do host Proxmox, só leitura (pool, scrub, SMART, kernel, patches, ESPs)
> cd ansible && ansible-playbook playbooks/host-status.yml
