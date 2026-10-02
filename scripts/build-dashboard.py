#!/usr/bin/env python3
"""Gera k8s/monitoring/dashboard-asunboid.yaml (dashboard AsunBoid como código).
Uso: python3 scripts/build-dashboard.py
"""
import json
import pathlib

DS = {"type": "prometheus", "uid": "${datasource}"}
LOKI = {"type": "loki", "uid": "loki"}
GREEN, ORANGE, RED = "green", "orange", "red"

def th(*steps):
    return {"mode": "absolute", "steps": [{"color": c, "value": v} for c, v in steps]}

def prom(expr, ref="A", legend=None, instant=False, fmt=None):
    t = {"expr": expr, "refId": ref}
    if legend: t["legendFormat"] = legend
    if instant: t["instant"] = True
    if fmt: t["format"] = fmt
    return t

def stat(title, desc, x, y, w, h, targets, unit=None, steps=None, mappings=None,
         color="background", graph="none", decimals=None, no_value=None):
    d = {"thresholds": steps or th((GREEN, None))}
    if unit: d["unit"] = unit
    if mappings: d["mappings"] = mappings
    if decimals is not None: d["decimals"] = decimals
    if no_value: d["noValue"] = no_value
    return {"type": "stat", "title": title, "description": desc, "datasource": DS,
            "gridPos": {"x": x, "y": y, "w": w, "h": h}, "targets": targets,
            "fieldConfig": {"defaults": d, "overrides": []},
            "options": {"colorMode": color, "graphMode": graph, "textMode": "value",
                        "justifyMode": "center", "reduceOptions": {"calcs": ["lastNotNull"]}}}

def vmap(pairs):
    return [{"type": "value", "options": {k: {"text": t, "color": c, "index": i}
             for i, (k, t, c) in enumerate(pairs)}}]

def row(title, y, collapsed=False, panels=None):
    return {"type": "row", "title": title, "collapsed": collapsed,
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": panels or []}

ONLINE = vmap([("0", "OFFLINE", RED), ("1", "ONLINE", GREEN)])
P = []

# ---------------- Guia de leitura ----------------
P.append({"type": "text", "title": "", "gridPos": {"x": 0, "y": 0, "w": 24, "h": 2},
          "options": {"mode": "markdown", "content":
          "**Como ler:** leia de cima para baixo, do geral ao detalhe. "
          "**Verde** = saudável · **laranja** = atenção · **vermelho** = agir. "
          "Passe o mouse no **(i)** de cada painel para saber o que ele mede. "
          "Seções de hardware e logs ficam recolhidas: abra quando precisar investigar."}})

# ---------------- 1. Agora ----------------
y = 2
P.append(row("Agora", y)); y += 1
P += [
  stat("Servidor", "O servidor responde ao RCON? Se OFFLINE por mais de 3 min, o alerta ZomboidDown dispara no Discord.",
       0, y, 4, 4, [prom("max(zomboid_up) or vector(0)")], mappings=ONLINE, steps=th((RED, None), (GREEN, 1))),
  stat("Players online", "Jogadores conectados agora, de no máximo 6 vagas.",
       4, y, 4, 4, [prom("max(zomboid_players_online)")], unit="suffix: / 6", decimals=0,
       steps=th(("blue", None)), color="value", graph="area"),
  stat("Disponibilidade (7 dias)", "SLO: % do tempo em que o servidor respondeu nos últimos 7 dias. Meta: 99%. O restart diário das 6h consome cerca de 0,3%.",
       8, y, 4, 4, [prom("avg_over_time((max(zomboid_up) or vector(0))[7d:5m])")], unit="percentunit", decimals=2,
       steps=th((RED, None), (ORANGE, 0.95), (GREEN, 0.99))),
  stat("No ar há", "Tempo desde que o processo do servidor iniciou (restart diário, deploy ou crash).",
       12, y, 4, 4, [prom('time() - max(kube_pod_container_state_started{namespace="zomboid",container="server"})')],
       unit="dtdurations", steps=th(("text", None)), color="value"),
  stat("Último backup", "Tempo desde o último backup bem-sucedido do mundo (restic → AWS S3, diário às 5h30). Vermelho acima de 26h.",
       16, y, 4, 4, [prom('time() - max(kube_cronjob_status_last_successful_time{namespace="zomboid",cronjob="zomboid-backup"})')],
       unit="dtdurations", steps=th((GREEN, None), (ORANGE, 90000), (RED, 93600))),
  stat("Alertas ativos", "Problemas disparados agora (os mesmos enviados ao Discord). Entradas de jogadores não contam.",
       20, y, 4, 4, [prom('count(ALERTS{alertstate="firing",alertname!~"Watchdog|InfoInhibitor|PlayerOnline"}) or vector(0)')],
       decimals=0, mappings=vmap([("0", "Nenhum", GREEN)]), steps=th((GREEN, None), (RED, 1))),
]
y += 4
P.append({"type": "state-timeline", "title": "Servidor no ar (período selecionado)",
          "description": "Faixa verde = servidor respondendo; vermelha = fora do ar. Mostra quando e por quanto tempo houve quedas.",
          "datasource": DS, "gridPos": {"x": 0, "y": y, "w": 24, "h": 3},
          "targets": [prom("max(zomboid_up) or vector(0)", legend="Servidor")],
          "fieldConfig": {"defaults": {"mappings": ONLINE, "thresholds": th((RED, None), (GREEN, 1)),
                                       "custom": {"fillOpacity": 85, "lineWidth": 0}}, "overrides": []},
          "options": {"showValue": "never", "mergeValues": True, "rowHeight": 0.8,
                      "legend": {"showLegend": False}, "tooltip": {"mode": "single"}}})
y += 3

# ---------------- 2. Jogadores ----------------
P.append(row("Jogadores", y)); y += 1
P.append({"type": "table", "title": "Online agora",
          "description": "Quem está conectado neste momento e quanto jogou nas últimas 24h.",
          "datasource": DS, "gridPos": {"x": 0, "y": y, "w": 8, "h": 8},
          "targets": [prom("(sum by (player) (count_over_time(zomboid_player_online[24h])) * 30) "
                           "and on (player) max by (player) (zomboid_player_online)", instant=True, fmt="table")],
          "fieldConfig": {"defaults": {"unit": "dtdurations", "noValue": "Ninguém online agora"},
                          "overrides": [{"matcher": {"id": "byName", "options": "Jogador"},
                                         "properties": [{"id": "unit", "value": "string"}]}]},
          "transformations": [{"id": "organize", "options": {"excludeByName": {"Time": True},
                               "renameByName": {"player": "Jogador", "Value": "Jogou nas últimas 24h"}}}],
          "options": {"showHeader": True}})
P.append({"type": "timeseries", "title": "Players online ao longo do tempo",
          "description": "Quantos jogadores estavam conectados em cada momento. Linha tracejada = limite de 6 vagas.",
          "datasource": DS, "gridPos": {"x": 8, "y": y, "w": 16, "h": 8},
          "targets": [prom("max(zomboid_players_online)", legend="players")],
          "fieldConfig": {"defaults": {"min": 0, "max": 6, "decimals": 0, "color": {"mode": "fixed", "fixedColor": "blue"},
                                       "thresholds": th((GREEN, None), (RED, 6)),
                                       "custom": {"lineInterpolation": "stepAfter", "fillOpacity": 25, "lineWidth": 2,
                                                  "thresholdsStyle": {"mode": "dashed"}}}, "overrides": []},
          "options": {"legend": {"showLegend": False}, "tooltip": {"mode": "single"}}})
y += 8
P.append({"type": "state-timeline", "title": "Linha do tempo de sessões",
          "description": "Cada faixa é um jogador; o trecho colorido é o período em que ele esteve online.",
          "datasource": DS, "gridPos": {"x": 0, "y": y, "w": 24, "h": 7},
          "targets": [prom("max by (player) (zomboid_player_online)", legend="{{player}}")],
          "fieldConfig": {"defaults": {"mappings": vmap([("1", "online", GREEN)]), "thresholds": th((GREEN, None)),
                                       "custom": {"fillOpacity": 80, "lineWidth": 0, "insertNulls": 90000}}, "overrides": []},
          "options": {"showValue": "never", "mergeValues": True, "rowHeight": 0.7,
                      "legend": {"showLegend": False}, "tooltip": {"mode": "single"}}})
y += 7
dur = lambda r, ref: prom(f"sum by (player) (count_over_time(zomboid_player_online[{r}])) * 30", ref=ref, instant=True, fmt="table")
P.append({"type": "table", "title": "Tempo online por jogador",
          "description": "Tempo jogado em cada período e o último acesso. A barra da coluna de 30 dias funciona como ranking.",
          "datasource": DS, "gridPos": {"x": 0, "y": y, "w": 24, "h": 8},
          "targets": [prom("max by (player) (last_over_time(timestamp(zomboid_player_online)[30d:1m])) * 1000",
                           ref="A", instant=True, fmt="table"),
                      dur("24h", "B"), dur("7d", "C"), dur("30d", "D")],
          "fieldConfig": {"defaults": {"unit": "dtdurations", "custom": {"align": "auto"}},
                          "overrides": [
                              {"matcher": {"id": "byName", "options": "Jogador"}, "properties": [{"id": "unit", "value": "string"}]},
                              {"matcher": {"id": "byName", "options": "Último acesso"}, "properties": [{"id": "unit", "value": "dateTimeFromNow"}]},
                              {"matcher": {"id": "byName", "options": "Últimos 30 dias"}, "properties": [
                                  {"id": "custom.cellOptions", "value": {"type": "gauge", "mode": "gradient", "valueDisplayMode": "text"}},
                                  {"id": "color", "value": {"mode": "continuous-BlPu"}},
                                  {"id": "min", "value": 0}]}]},
          "transformations": [
              {"id": "merge", "options": {}},
              {"id": "organize", "options": {"excludeByName": {"Time": True},
                  "indexByName": {"player": 0, "Value #A": 1, "Value #B": 2, "Value #C": 3, "Value #D": 4},
                  "renameByName": {"player": "Jogador", "Value #A": "Último acesso", "Value #B": "Últimas 24h",
                                   "Value #C": "Últimos 7 dias", "Value #D": "Últimos 30 dias"}}},
              {"id": "sortBy", "options": {"sort": [{"field": "Últimos 30 dias", "desc": True}]}}],
          "options": {"showHeader": True, "sortBy": [{"displayName": "Últimos 30 dias", "desc": True}]}})
y += 8

# ---------------- 3. Servidor do jogo ----------------
P.append(row("Servidor do jogo (recursos)", y)); y += 1
P.append({"type": "timeseries", "title": "Memória do servidor",
          "description": "Memória usada pelo jogo contra o limite do container (linha vermelha). Se encostar no limite, o Kubernetes mata o processo (OOMKilled). Alerta acima de 90%.",
          "datasource": DS, "gridPos": {"x": 0, "y": y, "w": 10, "h": 8},
          "targets": [prom('max(container_memory_working_set_bytes{namespace="zomboid",container="server"})', legend="em uso"),
                      prom('max(kube_pod_container_resource_limits{namespace="zomboid",container="server",resource="memory"})', ref="B", legend="limite")],
          "fieldConfig": {"defaults": {"unit": "bytes", "min": 0, "custom": {"fillOpacity": 20, "lineWidth": 2}},
                          "overrides": [{"matcher": {"id": "byName", "options": "limite"}, "properties": [
                              {"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}},
                              {"id": "custom.fillOpacity", "value": 0},
                              {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}}]}]},
          "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}})
P.append({"type": "timeseries", "title": "CPU do servidor",
          "description": "Núcleos de CPU usados pelo jogo. A VM tem 4 vCPUs (linha tracejada). Picos durante a carga do mundo são normais.",
          "datasource": DS, "gridPos": {"x": 10, "y": y, "w": 10, "h": 8},
          "targets": [prom('sum(rate(container_cpu_usage_seconds_total{namespace="zomboid",container="server"}[5m]))', legend="cores")],
          "fieldConfig": {"defaults": {"unit": "short", "min": 0, "softMax": 4, "decimals": 1,
                                       "thresholds": th((GREEN, None), (RED, 4)),
                                       "custom": {"fillOpacity": 20, "lineWidth": 2, "axisLabel": "cores",
                                                  "thresholdsStyle": {"mode": "dashed"}}}, "overrides": []},
          "options": {"legend": {"showLegend": False}, "tooltip": {"mode": "single"}}})
P.append(stat("Crashes (7 dias)", "Reinícios inesperados do processo do jogo (crash ou OOMKilled). Restarts planejados não contam.",
              20, y, 4, 8, [prom('sum(increase(kube_pod_container_status_restarts_total{namespace="zomboid",container="server"}[7d])) or vector(0)')],
              decimals=0, mappings=vmap([("0", "Nenhum", GREEN)]), steps=th((GREEN, None), (ORANGE, 1), (RED, 3))))
y += 8

# ---------------- 4. Host (recolhido) ----------------
host_y = y + 1
host = [
  stat("SMART dos discos", "Autoteste dos discos. FALHA = disco com falha prevista. No ZFS RAID0, perder um disco é perder o pool.",
       0, host_y, 4, 4, [prom("min(smartctl_device_smart_status)")],
       mappings=vmap([("0", "FALHA", RED), ("1", "OK", GREEN)]), steps=th((RED, None), (GREEN, 1)), no_value="sem dados"),
  stat("Pool ZFS", "Estado do pool de armazenamento (rpool).",
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
P.append(row("Hardware do host (HPE DL20 Gen9)", y, collapsed=True, panels=host)); y += 1

# ---------------- 5. Logs e alertas (recolhido) ----------------
ly = y + 1
logs = [
  {"type": "table", "title": "Alertas disparados agora",
   "description": "Lista dos problemas ativos (os mesmos enviados ao Discord).",
   "datasource": DS, "gridPos": {"x": 0, "y": ly, "w": 24, "h": 5},
   "targets": [prom('ALERTS{alertstate="firing",alertname!~"Watchdog|InfoInhibitor|PlayerOnline"}', instant=True, fmt="table")],
   "fieldConfig": {"defaults": {"noValue": "Nenhum alerta ativo"}, "overrides": []},
   "transformations": [{"id": "organize", "options": {
       "excludeByName": {"Time": True, "Value": True, "__name__": True, "alertstate": True},
       "renameByName": {"alertname": "Alerta", "severity": "Severidade"}}}]},
  {"type": "logs", "title": "Entradas e saídas de jogadores",
   "description": "Linhas do log do servidor sobre conexões.",
   "datasource": LOKI, "gridPos": {"x": 0, "y": ly + 5, "w": 12, "h": 10},
   "targets": [{"expr": '{namespace="zomboid", container="server"} |~ "(?i)(connected|disconnect|logged)"', "refId": "A"}],
   "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending", "enableLogDetails": True}},
  {"type": "logs", "title": "Erros e avisos do servidor",
   "description": "Somente linhas WARN e ERROR. Senhas nunca chegam ao Loki (filtradas na coleta).",
   "datasource": LOKI, "gridPos": {"x": 12, "y": ly + 5, "w": 12, "h": 10},
   "targets": [{"expr": '{namespace="zomboid", container="server", level=~"ERROR|WARN"}', "refId": "A"}],
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
  "title": "AsunBoid - Servidor de Jogo", "uid": "asunboid",
  "description": "Project Zomboid B42 em K3s sobre Proxmox. Dashboard como código (GitOps via Argo CD).",
  "tags": ["homelab", "zomboid", "k3s"], "timezone": "America/Sao_Paulo",
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
