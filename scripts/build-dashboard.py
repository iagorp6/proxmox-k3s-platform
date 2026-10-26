#!/usr/bin/env python3
"""Gera k8s/monitoring/dashboard-asunboid.yaml (dashboard da plataforma de jogos como código).

Multi-jogo: o topo mostra o jogo ativo (réplicas > 0) e o status dele; as seções de jogadores
juntam todos os jogos com uma coluna "Jogo". Todo jogo segue o contrato game_* (label game),
então as consultas não dependem de qual jogo está ativo.
Uso: python3 scripts/build-dashboard.py
"""
import json
import pathlib

import catalog

DS = {"type": "prometheus", "uid": "${datasource}"}
LOKI = {"type": "loki", "uid": "loki"}
GREEN, ORANGE, RED = "green", "orange", "red"
# Jogos lidos do catálogo (k8s/**/game.yaml): o namespace de cada jogo é o próprio nome dele.
NS = "|".join(g["namespace"] for g in catalog.games())
TITLES = [(g["name"], g["displayName"]) for g in catalog.games()]

# ---------- PromQL: contrato game_* ----------
ACTIVE = (f'label_replace(max by (namespace) (kube_statefulset_replicas{{namespace=~"{NS}"}}) > 0, '
          '"game", "$1", "namespace", "(.+)")')
UP = "max by (game) (game_up)"
PLAYERS = "max by (game) (game_players_online)"
PLAYER_ONLINE = "max by (game, player) (game_player_online)"
ACTIVE_UP = f"max(({UP}) * on (game) ({ACTIVE})) or vector(0)"
ACTIVE_PLAYERS = f"sum(({PLAYERS}) * on (game) ({ACTIVE})) or vector(0)"


def dur(r):
    """Tempo online (s) por jogo/jogador na janela r (coleta a cada 30s)."""
    return f"sum by (game, player) (count_over_time(game_player_online[{r}])) * 30"


LAST_SEEN = "max by (game, player) (last_over_time(timestamp(game_player_online)[30d:1m])) * 1000"
TOTAL = "max by (game, player) (game_player_playtime_seconds_total)"
DEATHS = "round(sum by (game, player) (increase(game_player_deaths_total[30d])))"
ALERT_FILTER = 'alertname!~"Watchdog|InfoInhibitor|.*PlayerOnline"'


def th(*steps):
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for c, v in steps]}


def prom(expr, ref="A", legend=None, instant=False, fmt=None):
    t = {"expr": expr, "refId": ref}
    if legend: t["legendFormat"] = legend
    if instant: t["instant"] = True
    if fmt: t["format"] = fmt
    return t


def stat(title, desc, x, y, w, h, targets, unit=None, steps=None, mappings=None,
         color="background", graph="none", decimals=None, no_value=None, text_mode="value"):
    d = {"thresholds": steps or th((GREEN, None))}
    if unit: d["unit"] = unit
    if mappings: d["mappings"] = mappings
    if decimals is not None: d["decimals"] = decimals
    if no_value: d["noValue"] = no_value
    return {"type": "stat", "title": title, "description": desc, "datasource": DS,
            "gridPos": {"x": x, "y": y, "w": w, "h": h}, "targets": targets,
            "fieldConfig": {"defaults": d, "overrides": []},
            "options": {"colorMode": color, "graphMode": graph, "textMode": text_mode,
                        "justifyMode": "center", "reduceOptions": {"calcs": ["lastNotNull"]}}}


def vmap(pairs):
    return [{"type": "value", "options": {k: {"text": t, "color": c, "index": i}
             for i, (k, t, c) in enumerate(pairs)}}]


def row(title, y, collapsed=False, panels=None):
    return {"type": "row", "title": title, "collapsed": collapsed,
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": panels or []}


ONLINE = vmap([("0", "OFFLINE", RED), ("1", "ONLINE", GREEN)])
GAME_NAMES = vmap([(k, v, "text") for k, v in TITLES])
P = []

# ---------------- 1. Agora ----------------
y = 0
P.append(row("Agora", y)); y += 1
active_name = ACTIVE
for key, title in TITLES:
    active_name = f'label_replace({active_name}, "jogo", "{title}", "game", "{key}")'
P += [
  stat("Jogo ativo", "Jogo no ar agora (troca com make switch-game GAME=<jogo>).",
       0, y, 4, 4, [prom(active_name, legend="{{jogo}}")], text_mode="name",
       steps=th(("blue", None)), color="background", no_value="nenhum"),
  stat("Servidor", "O jogo ativo responde (RCON no Zomboid, API REST no Terraria)? Alerta no Discord se cair.",
       4, y, 4, 4, [prom(ACTIVE_UP)], mappings=ONLINE, steps=th((RED, None), (GREEN, 1))),
  stat("Players online", "Jogadores conectados no jogo ativo, de no máximo 6 vagas.",
       8, y, 4, 4, [prom(ACTIVE_PLAYERS)], unit="suffix: / 6", decimals=0,
       steps=th(("blue", None)), color="value", graph="area"),
  stat("Disponibilidade (7 dias)", "% do tempo em que o jogo ativo respondeu nos últimos 7 dias. Trocas de jogo e o restart diário contam como indisponibilidade. O SLO oficial (28 dias, com error budget) fica na seção SLO.",
       12, y, 4, 4, [prom(f"avg_over_time(({ACTIVE_UP})[7d:5m])")], unit="percentunit", decimals=2,
       steps=th((RED, None), (ORANGE, 0.95), (GREEN, 0.99))),
  stat("Backup mais antigo", "Tempo desde o último backup bem-sucedido do jogo mais atrasado (restic → AWS S3, diário). Vermelho acima de 26h.",
       16, y, 4, 4, [prom('time() - min(kube_cronjob_status_last_successful_time{cronjob=~".+-backup"})')],
       unit="dtdurations", steps=th((GREEN, None), (ORANGE, 90000), (RED, 93600))),
  stat("Alertas ativos", "Problemas disparados agora (os mesmos enviados ao Discord). Entradas de jogadores não contam.",
       20, y, 4, 4, [prom(f"count(ALERTS{{alertstate=\"firing\",{ALERT_FILTER}}}) or vector(0)")],
       decimals=0, mappings=vmap([("0", "Nenhum", GREEN)]), steps=th((GREEN, None), (RED, 1))),
]
y += 4
P.append({"type": "state-timeline", "title": "Jogo ativo e disponibilidade",
          "description": "Linha de cima: qual jogo estava no ar. Linha de baixo: verde = respondendo, vermelho = fora (inclui o tempo de cada troca).",
          "datasource": DS, "gridPos": {"x": 0, "y": y, "w": 24, "h": 5},
          "targets": [prom(f"max by (game) ({ACTIVE})", legend="{{game}}"),
                      prom(ACTIVE_UP, ref="B", legend="Servidor no ar")],
          "fieldConfig": {"defaults": {"thresholds": th((RED, None), (GREEN, 1)),
                                       "custom": {"fillOpacity": 85, "lineWidth": 0, "insertNulls": 90000}},
                          "overrides": [
                              {"matcher": {"id": "byName", "options": "Servidor no ar"},
                               "properties": [{"id": "mappings", "value": ONLINE}]},
                              {"matcher": {"id": "byRegexp", "options": "^(zomboid|terraria|minecraft)$"},
                               "properties": [{"id": "mappings", "value": vmap([("1", "ativo", "blue")])},
                                              {"id": "color", "value": {"mode": "fixed", "fixedColor": "blue"}}]}]},
          "options": {"showValue": "never", "mergeValues": True, "rowHeight": 0.8,
                      "legend": {"showLegend": False}, "tooltip": {"mode": "single"}}})
y += 5

# ---------------- SLO (k8s/monitoring/alerts-slo.yaml, docs/slo.md) ----------------
# Lê as recording rules game_slo:*, as mesmas dos alertas de burn rate: painel e alerta nunca discordam.
P.append(row("SLO: 99% do tempo no ar em 28 dias", y)); y += 1
P += [
  stat("Disponibilidade (28 dias)", "SLI do SLO: fração do tempo em que o jogo ativo respondeu ao exporter. Trocas, restart diário e reboot de patch contam como fora. Meta: 99%.",
       0, y, 6, 6, [prom("1 - game_slo:unavailable:ratio_avg28d")], unit="percentunit", decimals=2,
       steps=th((RED, None), (ORANGE, 0.99), (GREEN, 0.995))),
  stat("Error budget restante", "Quanto sobra dos 1% de indisponibilidade permitidos em 28 dias (6h43min). Abaixo de 0, a política de docs/slo.md congela mudanças de risco no jogo.",
       6, y, 6, 6, [prom("1 - game_slo:unavailable:ratio_avg28d / 0.01")], unit="percentunit", decimals=0,
       steps=th((RED, None), (ORANGE, 0), (GREEN, 0.25))),
]
P.append({"type": "timeseries", "title": "Burn rate do error budget",
          "description": "Velocidade de consumo do budget: 1 = acaba em exatos 28 dias. GameSLOBurnRateHigh: acima de 6 em 6h e 30min. GameSLOBurnRateSlow: acima de 1 em 3 dias e 6h.",
          "datasource": DS, "gridPos": {"x": 12, "y": y, "w": 12, "h": 6},
          "targets": [prom("game_slo:burn_rate:avg30m", legend="30 min"),
                      prom("game_slo:burn_rate:avg6h", ref="B", legend="6 h"),
                      prom("game_slo:burn_rate:avg3d", ref="C", legend="3 dias")],
          "fieldConfig": {"defaults": {"min": 0, "decimals": 1,
                                       "thresholds": th((GREEN, None), (ORANGE, 1), (RED, 6)),
                                       "custom": {"fillOpacity": 10, "lineWidth": 2, "thresholdsStyle": {"mode": "dashed"}}},
                          "overrides": []},
          "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}})
y += 6

# ---------------- Restore drills ----------------
# Evidência de que o backup volta: o Job do drill some em 3 dias e o log no Loki em 7, mas as séries do
# kube-state-metrics ficam 30 dias no Prometheus. Pega o drill mensal (<jogo>-restore-drill-<n>, do
# CronJob) e o manual do make restore-drill (<jogo>-restore-drill); o --live (restore-live) fica de fora.
# completion_time só existe em Job que terminou com sucesso.
DRILL_JOBS = 'job_name=~".+-restore-drill(-[0-9]+)?"'
DRILL_AGE = (f"min by (namespace) (time() - max by (namespace) (last_over_time(kube_job_status_completion_time{{{DRILL_JOBS}}}[30d]))"
             ' or time() - max by (namespace) (kube_cronjob_status_last_successful_time{cronjob=~".+-restore-drill"}))')
P.append(row("Restore drills", y)); y += 1
P += [
  stat("Último restore testado", "Tempo desde o último restore drill que passou em cada jogo (mensal automático ou make restore-drill). Acima de 35 dias dispara o RestoreDrillStale.",
       0, y, 8, 6, [prom(DRILL_AGE, legend="{{namespace}}")],
       unit="dtdurations", steps=th((GREEN, None), (ORANGE, 30 * 86400), (RED, 35 * 86400)), no_value="nenhum drill ainda"),
  {"type": "table", "title": "Restore drills (30 dias)",
   "description": "Cada execução do drill: restaura o último snapshot do S3 num volume temporário e confere os arquivos do catálogo. Duração = do início do Job ao mundo conferido.",
   "datasource": DS, "gridPos": {"x": 8, "y": y, "w": 16, "h": 6},
   "targets": [prom(f"max by (namespace, job_name) (last_over_time(kube_job_status_succeeded{{{DRILL_JOBS}}}[30d]))", instant=True, fmt="table"),
               prom(f"max by (namespace, job_name) (last_over_time(kube_job_status_completion_time{{{DRILL_JOBS}}}[30d])"
                    f" - last_over_time(kube_job_status_start_time{{{DRILL_JOBS}}}[30d]))", ref="B", instant=True, fmt="table"),
               prom(f"max by (namespace, job_name) (last_over_time(kube_job_status_start_time{{{DRILL_JOBS}}}[30d])) * 1000",
                    ref="C", instant=True, fmt="table")],
   "fieldConfig": {"defaults": {"noValue": "Nenhum drill nos últimos 30 dias"}, "overrides": [
       {"matcher": {"id": "byName", "options": "Resultado"},
        "properties": [{"id": "mappings", "value": vmap([("0", "FALHOU", RED), ("1", "OK", GREEN)])},
                       {"id": "custom.cellOptions", "value": {"type": "color-background"}}]},
       {"matcher": {"id": "byName", "options": "Duração"}, "properties": [{"id": "unit", "value": "s"}]},
       {"matcher": {"id": "byName", "options": "Início"}, "properties": [{"id": "unit", "value": "dateTimeAsLocal"}]}]},
   "transformations": [
       {"id": "merge", "options": {}},
       {"id": "organize", "options": {
           "excludeByName": {"Time": True},
           "indexByName": {"namespace": 0, "Value #C": 1, "Value #A": 2, "Value #B": 3, "job_name": 4},
           "renameByName": {"namespace": "Jogo", "job_name": "Job", "Value #A": "Resultado",
                            "Value #B": "Duração", "Value #C": "Início"}}},
       {"id": "sortBy", "options": {"sort": [{"field": "Início", "desc": True}]}}],
   "options": {"showHeader": True}},
]
y += 6

# ---------------- 2. Jogadores ----------------
P.append(row("Jogadores", y)); y += 1
game_col = {"matcher": {"id": "byName", "options": "Jogo"},
            "properties": [{"id": "unit", "value": "string"}, {"id": "mappings", "value": GAME_NAMES}]}
P.append({"type": "table", "title": "Online agora",
          "description": "Quem está conectado neste momento e quanto jogou nas últimas 24h.",
          "datasource": DS, "gridPos": {"x": 0, "y": y, "w": 8, "h": 8},
          "targets": [prom(f"({dur('24h')}) and on (game, player) ({PLAYER_ONLINE})", instant=True, fmt="table")],
          "fieldConfig": {"defaults": {"unit": "dtdurations", "noValue": "Ninguém online agora"},
                          "overrides": [game_col, {"matcher": {"id": "byName", "options": "Jogador"},
                                                   "properties": [{"id": "unit", "value": "string"}]}]},
          "transformations": [{"id": "organize", "options": {"excludeByName": {"Time": True},
                               "indexByName": {"game": 0, "player": 1, "Value": 2},
                               "renameByName": {"game": "Jogo", "player": "Jogador", "Value": "Jogou nas últimas 24h"}}}],
          "options": {"showHeader": True}})
P.append({"type": "timeseries", "title": "Players online ao longo do tempo",
          "description": "Jogadores conectados em cada jogo. Linha tracejada = limite de 6 vagas.",
          "datasource": DS, "gridPos": {"x": 8, "y": y, "w": 16, "h": 8},
          "targets": [prom(PLAYERS, legend="{{game}}")],
          "fieldConfig": {"defaults": {"min": 0, "max": 6, "decimals": 0,
                                       "thresholds": th((GREEN, None), (RED, 6)),
                                       "custom": {"lineInterpolation": "stepAfter", "fillOpacity": 25, "lineWidth": 2,
                                                  "thresholdsStyle": {"mode": "dashed"}}}, "overrides": []},
          "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}})
y += 8
P.append({"type": "state-timeline", "title": "Linha do tempo de sessões",
          "description": "Cada faixa é um jogador em um jogo; o trecho colorido é o período em que ele esteve online.",
          "datasource": DS, "gridPos": {"x": 0, "y": y, "w": 24, "h": 7},
          "targets": [prom(PLAYER_ONLINE, legend="{{player}} ({{game}})")],
          "fieldConfig": {"defaults": {"mappings": vmap([("1", "online", GREEN)]), "thresholds": th((GREEN, None)),
                                       "custom": {"fillOpacity": 80, "lineWidth": 0, "insertNulls": 90000}}, "overrides": []},
          "options": {"showValue": "never", "mergeValues": True, "rowHeight": 0.7,
                      "legend": {"showLegend": False}, "tooltip": {"mode": "single"}}})
y += 7
P.append({"type": "table", "title": "Tempo online por jogador",
          "description": "Por jogo: tempo jogado em cada período, total desde o início, mortes (Zomboid) e último acesso. A barra dos 30 dias funciona como ranking.",
          "datasource": DS, "gridPos": {"x": 0, "y": y, "w": 24, "h": 9},
          "targets": [prom(LAST_SEEN, ref="A", instant=True, fmt="table"),
                      prom(dur("24h"), ref="B", instant=True, fmt="table"),
                      prom(dur("7d"), ref="C", instant=True, fmt="table"),
                      prom(dur("30d"), ref="D", instant=True, fmt="table"),
                      prom(TOTAL, ref="E", instant=True, fmt="table"),
                      prom(DEATHS, ref="F", instant=True, fmt="table")],
          "fieldConfig": {"defaults": {"unit": "dtdurations", "custom": {"align": "auto"}},
                          "overrides": [
                              game_col,
                              {"matcher": {"id": "byName", "options": "Jogador"}, "properties": [{"id": "unit", "value": "string"}]},
                              {"matcher": {"id": "byName", "options": "Último acesso"}, "properties": [{"id": "unit", "value": "dateTimeFromNow"}]},
                              {"matcher": {"id": "byName", "options": "Mortes (30 dias)"}, "properties": [{"id": "unit", "value": "none"}, {"id": "decimals", "value": 0}]},
                              {"matcher": {"id": "byName", "options": "Últimos 30 dias"}, "properties": [
                                  {"id": "custom.cellOptions", "value": {"type": "gauge", "mode": "gradient", "valueDisplayMode": "text"}},
                                  {"id": "color", "value": {"mode": "continuous-BlPu"}},
                                  {"id": "min", "value": 0}]}]},
          "transformations": [
              {"id": "merge", "options": {}},
              {"id": "organize", "options": {"excludeByName": {"Time": True},
                  "indexByName": {"game": 0, "player": 1, "Value #A": 2, "Value #B": 3, "Value #C": 4,
                                  "Value #D": 5, "Value #E": 6, "Value #F": 7},
                  "renameByName": {"game": "Jogo", "player": "Jogador", "Value #A": "Último acesso",
                                   "Value #B": "Últimas 24h", "Value #C": "Últimos 7 dias", "Value #D": "Últimos 30 dias",
                                   "Value #E": "Total no servidor", "Value #F": "Mortes (30 dias)"}}},
              {"id": "sortBy", "options": {"sort": [{"field": "Últimos 30 dias", "desc": True}]}}],
          "options": {"showHeader": True, "sortBy": [{"displayName": "Últimos 30 dias", "desc": True}]}})
y += 9

# ---------------- 3. Servidor do jogo ----------------
P.append(row("Servidor do jogo (recursos)", y)); y += 1
P.append({"type": "timeseries", "title": "Memória do servidor",
          "description": "Memória usada pelo jogo ativo contra o limite do container (linha vermelha). Encostar no limite = OOMKilled.",
          "datasource": DS, "gridPos": {"x": 0, "y": y, "w": 10, "h": 8},
          "targets": [prom(f'max by (namespace) (container_memory_working_set_bytes{{namespace=~"{NS}",container="server"}})', legend="{{namespace}} em uso"),
                      prom(f'max by (namespace) (kube_pod_container_resource_limits{{namespace=~"{NS}",container="server",resource="memory"}})', ref="B", legend="{{namespace}} limite")],
          "fieldConfig": {"defaults": {"unit": "bytes", "min": 0, "custom": {"fillOpacity": 20, "lineWidth": 2}},
                          "overrides": [{"matcher": {"id": "byRegexp", "options": ".*limite$"}, "properties": [
                              {"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}},
                              {"id": "custom.fillOpacity", "value": 0},
                              {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}}]}]},
          "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}})
P.append({"type": "timeseries", "title": "CPU do servidor",
          "description": "Núcleos usados pelo jogo ativo. A VM tem 4 vCPUs (linha tracejada).",
          "datasource": DS, "gridPos": {"x": 10, "y": y, "w": 10, "h": 8},
          "targets": [prom(f'sum by (namespace) (rate(container_cpu_usage_seconds_total{{namespace=~"{NS}",container="server"}}[5m]))', legend="{{namespace}}")],
          "fieldConfig": {"defaults": {"unit": "short", "min": 0, "softMax": 4, "decimals": 1,
                                       "thresholds": th((GREEN, None), (RED, 4)),
                                       "custom": {"fillOpacity": 20, "lineWidth": 2, "axisLabel": "cores",
                                                  "thresholdsStyle": {"mode": "dashed"}}}, "overrides": []},
          "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}})
P.append(stat("Crashes (7 dias)", "Reinícios inesperados do processo de algum jogo (crash ou OOMKilled). Trocas e restarts planejados não contam.",
              20, y, 4, 8, [prom(f'sum(increase(kube_pod_container_status_restarts_total{{namespace=~"{NS}",container="server"}}[7d])) or vector(0)')],
              decimals=0, mappings=vmap([("0", "Nenhum", GREEN)]), steps=th((GREEN, None), (ORANGE, 1), (RED, 3))))
y += 8

# ---------------- 4. Host (recolhido) ----------------
host_y = y + 1
host = [
  stat("SMART dos discos", "Autoteste dos discos. FALHA = disco com falha prevista. O pool é um mirror: aguenta perder um disco, mas fica sem redundância até a troca.",
       0, host_y, 4, 4, [prom("min(smartctl_device_smart_status)")],
       mappings=vmap([("0", "FALHA", RED), ("1", "OK", GREEN)]), steps=th((RED, None), (GREEN, 1)), no_value="sem dados"),
  stat("Pool ZFS", "Estado do pool de armazenamento (rpool, mirror de 2 discos). DEGRADADO = um disco fora.",
       4, host_y, 4, 4, [prom('max(node_zfs_zpool_state{zpool="rpool",state="online"})')],
       mappings=vmap([("0", "DEGRADADO", RED), ("1", "ONLINE", GREEN)]), steps=th((RED, None), (GREEN, 1)), no_value="sem dados"),
  stat("Erros de memória ECC (24h)", "Erros detectados pela memória ECC. Alguns corrigidos são toleráveis; não corrigidos exigem ação.",
       8, host_y, 4, 4, [prom("(sum(increase(node_edac_correctable_errors_total[24h])) + sum(increase(node_edac_uncorrectable_errors_total[24h]))) or vector(0)")],
       decimals=0, mappings=vmap([("0", "Nenhum", GREEN)]), steps=th((GREEN, None), (ORANGE, 1), (RED, 10)), no_value="sem dados"),
  stat("Temperatura dos discos", "Maior temperatura entre os discos. Acima de 50 °C reduz a vida útil.",
       12, host_y, 4, 4, [prom('max(smartctl_device_temperature{temperature_type="current"})')],
       unit="celsius", steps=th((GREEN, None), (ORANGE, 45), (RED, 50)), color="value", graph="area", no_value="sem dados"),
  stat("Espaço livre no pool", "Espaço livre no armazenamento do host (inclui o disco da VM).",
       16, host_y, 4, 4, [prom('min(node_filesystem_avail_bytes{job="proxmox-node",mountpoint="/"} / node_filesystem_size_bytes{job="proxmox-node",mountpoint="/"})')],
       unit="percentunit", decimals=0, steps=th((RED, None), (ORANGE, 0.15), (GREEN, 0.25)), no_value="sem dados"),
  stat("RAM disponível no host", "Memória livre no Proxmox (24 GB no total; a VM do K3s reserva 18 GB).",
       20, host_y, 4, 4, [prom('node_memory_MemAvailable_bytes{job="proxmox-node"} / node_memory_MemTotal_bytes{job="proxmox-node"}')],
       unit="percentunit", decimals=0, steps=th((RED, None), (ORANGE, 0.1), (GREEN, 0.2)), no_value="sem dados"),
]
# Carga dos discos: no mirror cada disco recebe todas as escritas (as leituras se dividem), então os
# painéis de pico olham o disco mais carregado, não a soma, que contaria cada escrita duas vezes.
# max by (device): uma série por disco mesmo quando um label do alvo muda (instance), senão a janela
# de 7 dias mostra o mesmo disco duas vezes.
DISK = 'job="proxmox-node",device=~"sd[a-z]+"'


def per_disk(metric):
    return f"max by (device) (rate({metric}{{{DISK}}}[5m]))"


BUSY = per_disk("node_disk_io_time_seconds_total")
WRITE_IOPS = f'sum({per_disk("node_disk_writes_completed_total")})'
dy = host_y + 4
host += [
  {"type": "timeseries", "title": "Ocupação dos discos",
   "description": "Fração do tempo em que cada disco esteve ocupado. Perto de 100% = fila e latência subindo.",
   "datasource": DS, "gridPos": {"x": 0, "y": dy, "w": 8, "h": 7},
   "targets": [prom(BUSY, legend="{{device}}")],
   "fieldConfig": {"defaults": {"unit": "percentunit", "min": 0, "max": 1, "custom": {"fillOpacity": 15, "lineWidth": 1}},
                   "overrides": []},
   "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}},
  {"type": "timeseries", "title": "IOPS dos discos (soma)",
   "description": "Operações por segundo somando os discos. No mirror, cada escrita aparece duas vezes (vai para os dois discos).",
   "datasource": DS, "gridPos": {"x": 8, "y": dy, "w": 8, "h": 7},
   "targets": [prom(WRITE_IOPS, legend="escrita"),
               prom(f'sum({per_disk("node_disk_reads_completed_total")})', ref="B", legend="leitura")],
   "fieldConfig": {"defaults": {"unit": "iops", "min": 0, "custom": {"fillOpacity": 15, "lineWidth": 1}}, "overrides": []},
   "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}},
  {"type": "timeseries", "title": "Latência média de escrita",
   "description": "Tempo médio de cada escrita por disco, incluindo a fila. Em HDD, poucos ms é normal.",
   "datasource": DS, "gridPos": {"x": 16, "y": dy, "w": 8, "h": 7},
   "targets": [prom(f'{per_disk("node_disk_write_time_seconds_total")} / {per_disk("node_disk_writes_completed_total")}',
                    legend="{{device}}")],
   "fieldConfig": {"defaults": {"unit": "s", "min": 0, "custom": {"fillOpacity": 15, "lineWidth": 1}}, "overrides": []},
   "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}},
  stat("Pico de ocupação (p99, 7 dias)", "Maior p99 de ocupação entre os discos na última semana. Abaixo de 50%, o disco não é gargalo.",
       0, dy + 7, 8, 4, [prom(f"max(quantile_over_time(0.99, {BUSY}[7d:5m]))")],
       unit="percentunit", decimals=0, steps=th((GREEN, None), (ORANGE, 0.5), (RED, 0.8)), no_value="sem dados"),
  stat("Escritas por disco (p99, 7 dias)", "IOPS de escrita do disco mais carregado. Um HDD de 7200 rpm aguenta uns 100 a 150 IOPS aleatórios.",
       8, dy + 7, 8, 4, [prom(f'max(quantile_over_time(0.99, {per_disk("node_disk_writes_completed_total")}[7d:5m]))')],
       unit="iops", decimals=0, steps=th((GREEN, None), (ORANGE, 75), (RED, 120)), no_value="sem dados"),
  stat("Vazão de escrita por disco (p99, 7 dias)", "Bytes escritos por segundo no disco mais carregado. Um HDD sustenta uns 150 MB/s sequenciais.",
       16, dy + 7, 8, 4, [prom(f'max(quantile_over_time(0.99, {per_disk("node_disk_written_bytes_total")}[7d:5m]))')],
       unit="Bps", decimals=1, steps=th((GREEN, None), (ORANGE, 75e6), (RED, 120e6)), no_value="sem dados"),
  stat("Erros nos discos do ZFS", "Soma dos contadores READ/WRITE/CKSUM do zpool status. No mirror o ZFS corrige pelo outro disco e o pool segue ONLINE, então é aqui que um disco falhando aparece primeiro. Zera com zpool clear.",
       0, dy + 11, 12, 4, [prom("sum(max by (pool, vdev, type) (homelab_zfs_vdev_errors)) + (sum(homelab_zfs_pool_data_errors) or vector(0))")],
       decimals=0, mappings=vmap([("0", "Nenhum", GREEN)]), steps=th((GREEN, None), (ORANGE, 1), (RED, 10)), no_value="sem dados"),
  stat("Último scrub do pool", "Quando o ZFS leu e conferiu todos os blocos pela última vez (cron mensal, segundo domingo). Acima de 40 dias dispara o ZfsScrubStale.",
       12, dy + 11, 12, 4, [prom("(time() - max(homelab_zfs_last_scrub_timestamp_seconds)) / 86400")],
       unit="d", decimals=0, steps=th((GREEN, None), (ORANGE, 36), (RED, 40)), no_value="sem dados"),
  {"type": "table", "title": "Benchmark dos discos (fio)",
   "description": "Resultado de ansible-playbook playbooks/disk-bench.yml, uma linha por teste e layout do pool. O layout=stripe é de antes da conversão para mirror (26/10/2026).",
   "datasource": DS, "gridPos": {"x": 0, "y": dy + 15, "w": 24, "h": 6},
   "targets": [prom("homelab_disk_bench_iops", instant=True, fmt="table"),
               prom("homelab_disk_bench_bytes_per_second", ref="B", instant=True, fmt="table"),
               prom("homelab_disk_bench_latency_p99_seconds", ref="C", instant=True, fmt="table"),
               prom("homelab_disk_bench_timestamp_seconds * 1000", ref="D", instant=True, fmt="table")],
   "fieldConfig": {"defaults": {"noValue": "Ainda não rodou"}, "overrides": [
       {"matcher": {"id": "byName", "options": "IOPS"}, "properties": [{"id": "unit", "value": "iops"}, {"id": "decimals", "value": 0}]},
       {"matcher": {"id": "byName", "options": "Vazão"}, "properties": [{"id": "unit", "value": "Bps"}, {"id": "decimals", "value": 1}]},
       {"matcher": {"id": "byName", "options": "Latência p99"}, "properties": [{"id": "unit", "value": "s"}]},
       {"matcher": {"id": "byName", "options": "Medido em"}, "properties": [{"id": "unit", "value": "dateTimeAsLocal"}]}]},
   "transformations": [
       {"id": "merge", "options": {}},
       {"id": "organize", "options": {
           "excludeByName": {"Time": True, "__name__": True, "instance": True, "job": True},
           "indexByName": {"test": 0, "layout": 1, "Value #A": 2, "Value #B": 3, "Value #C": 4, "Value #D": 5},
           "renameByName": {"test": "Teste", "layout": "Pool", "Value #A": "IOPS", "Value #B": "Vazão",
                            "Value #C": "Latência p99", "Value #D": "Medido em"}}},
       {"id": "sortBy", "options": {"sort": [{"field": "Teste"}]}}],
   "options": {"showHeader": True}},
]
P.append(row("Hardware do host (HPE DL20 Gen9)", y, collapsed=True, panels=host)); y += 1

# ---------------- 5. Logs e alertas (recolhido) ----------------
ly = y + 1
logs = [
  {"type": "table", "title": "Alertas disparados agora",
   "description": "Lista dos problemas ativos (os mesmos enviados ao Discord).",
   "datasource": DS, "gridPos": {"x": 0, "y": ly, "w": 24, "h": 5},
   "targets": [prom(f'ALERTS{{alertstate="firing",{ALERT_FILTER}}}', instant=True, fmt="table")],
   "fieldConfig": {"defaults": {"noValue": "Nenhum alerta ativo"}, "overrides": []},
   "transformations": [{"id": "organize", "options": {
       "excludeByName": {"Time": True, "Value": True, "__name__": True, "alertstate": True},
       "renameByName": {"alertname": "Alerta", "severity": "Severidade"}}}]},
  {"type": "logs", "title": "Entradas e saídas de jogadores",
   "description": "Linhas dos logs dos servidores sobre conexões.",
   "datasource": LOKI, "gridPos": {"x": 0, "y": ly + 5, "w": 12, "h": 10},
   "targets": [{"expr": f'{{namespace=~"{NS}", container="server"}} |~ "(?i)(connected|disconnect|logged|has joined|has left)"', "refId": "A"}],
   "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending", "enableLogDetails": True}},
  {"type": "logs", "title": "Erros e avisos dos servidores",
   "description": "Linhas com erro, exceção ou aviso. Senhas nunca chegam ao Loki (filtradas na coleta).",
   "datasource": LOKI, "gridPos": {"x": 12, "y": ly + 5, "w": 12, "h": 10},
   "targets": [{"expr": f'{{namespace=~"{NS}", container="server"}} |~ "(?i)(error|exception|warn)"', "refId": "A"}],
   "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending", "enableLogDetails": True}},
]
P.append(row("Logs e alertas", y, collapsed=True, panels=logs))

# ids sequenciais
i = 1
def assign(ps):
    global i
    for p in ps:
        p["id"] = i; i += 1
        if p.get("panels"): assign(p["panels"])
assign(P)

dash = {
  "title": "AsunBoid - Plataforma de Jogos", "uid": "asunboid",
  "description": "Plataforma de jogos em K3s sobre Proxmox (um jogo ativo por vez). Dashboard como código (GitOps via Argo CD).",
  "tags": ["homelab", "games", "k3s"], "timezone": "America/Sao_Paulo",
  "refresh": "1m", "time": {"from": "now-24h", "to": "now"}, "schemaVersion": 39,
  "graphTooltip": 1,
  "templating": {"list": [{"name": "datasource", "label": "Prometheus", "type": "datasource",
                           "query": "prometheus", "current": {}, "hide": 2}]},
  "panels": P,
}

js = json.dumps(dash, indent=2, ensure_ascii=False)
out = ("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: dashboard-asunboid\n  namespace: monitoring\n"
       "  labels:\n    grafana_dashboard: \"1\"\ndata:\n  asunboid.json: |\n"
       + "\n".join("    " + l for l in js.splitlines()) + "\n")
dest = pathlib.Path(__file__).resolve().parent.parent / "k8s" / "monitoring" / "dashboard-asunboid.yaml"
dest.write_text(out, encoding="utf-8")
print(f"OK: {dest} ({i - 1} painéis)")
