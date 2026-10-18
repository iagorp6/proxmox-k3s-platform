# Atalhos do dia a dia. Rode `make help` para listar.
# Usa ">" como prefixo de receita (evita problemas de tab ao colar).
.RECIPEPREFIX = >
.DEFAULT_GOAL := help
.PHONY: help ci dashboard secrets plan deploy-monitoring deploy-eso deploy-argocd switch-game new-game restore-drill restore-game console status

help: ## Lista os alvos disponíveis
> @grep -E '^[a-zA-Z_%-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

ci: ## Roda localmente todas as verificações do CI
> ./scripts/ci-local.sh

ci-%: ## Roda um grupo do CI (terraform, ansible, kubernetes, scripts, security)
> ./scripts/ci-local.sh $*

dashboard: ## Regenera o dashboard do Grafana a partir do gerador
> python3 scripts/build-dashboard.py

secrets: ## Publica no AWS SSM os segredos do Bitwarden (exige BW_SESSION)
> ./scripts/push-secrets-to-ssm.sh

plan: ## terraform plan nos dois stacks (Proxmox e AWS)
> bash -c 'source ~/.proxmox.env && cd terraform && terraform plan'
> cd terraform-aws && terraform plan

deploy-monitoring: ## Break-glass: stack de observabilidade via Helm (no dia a dia é Sync no Argo CD)
> ./k8s/monitoring/deploy.sh

deploy-eso: ## Break-glass: External Secrets via Helm (no dia a dia é Sync no Argo CD)
> ./k8s/external-secrets/deploy.sh

deploy-argocd: ## Instala/atualiza o Argo CD (único release fora do Argo)
> ./k8s/argocd/deploy.sh

switch-game: ## Troca o jogo ativo (ex.: make switch-game GAME=terraria)
> ./scripts/switch-game.py $(GAME)

# PLAYERS_REGEX com $ precisa virar $$ no make; com regex complexa, chame scripts/new-game.py direto
new-game: ## Esqueleto de jogo novo, status draft (ex.: make new-game GAME=valheim TITLE=Valheim IMAGE=repo:tag@sha256:... PORT=16261 PROTOCOL=UDP)
> ./scripts/new-game.py $(GAME) --title "$(TITLE)" --image "$(IMAGE)" --port $(PORT) $(if $(PROTOCOL),--protocol $(PROTOCOL)) $(if $(MEMORY),--memory $(MEMORY)) $(if $(ICON),--icon "$(ICON)") $(if $(DRIVER),--driver $(DRIVER)) $(if $(RCON_PORT),--rcon-port $(RCON_PORT)) $(if $(PLAYERS_COMMAND),--players-command "$(PLAYERS_COMMAND)") $(if $(PLAYERS_REGEX),--players-regex '$(PLAYERS_REGEX)')

restore-drill: ## Restaura o último backup num PVC temporário e mede o tempo (ex.: make restore-drill GAME=terraria RECORD=1)
> ./scripts/restore-drill.py $(GAME) $(if $(RECORD),--record)

restore-game: ## Restaura o último backup no volume do jogo, com o servidor parado (DR; ex.: make restore-game GAME=minecraft)
> ./scripts/restore-drill.py $(GAME) --live

# CMD vai literal para o FIFO /tmp/console do container server (Zomboid e Terraria); a resposta sai no log
console: ## Comando no console do servidor (ex.: make console GAME=zomboid CMD='servermsg "restart em 5 min"')
> $(if $(and $(GAME),$(CMD)),,$(error uso: make console GAME=zomboid CMD='comando'))
> kubectl -n $(GAME) exec $(GAME)-0 -c server -- sh -c 'test -p /tmp/console && printf "%s\n" "$$1" > /tmp/console' _ '$(CMD)'
> @sleep 3; kubectl -n $(GAME) logs $(GAME)-0 -c server --since=10s | tail -n 20

status: ## Estado da plataforma (jogo ativo, réplicas, alertas)
> ./scripts/platform-status.sh
