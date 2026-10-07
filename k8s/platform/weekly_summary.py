"""Resumo semanal da plataforma de jogos no Discord (stdlib only).

Multi-jogo: une as métricas zomboid_* (Zomboid) e game_* (contrato da plataforma).
Com DRY_RUN=1 só imprime o que enviaria, sem postar no Discord (para testar o CronJob).
"""
import json
import os
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

PROM = os.getenv("PROMETHEUS_URL", "http://kps-kube-prometheus-stack-prometheus.monitoring.svc:9090")
WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
BRT = timezone(timedelta(hours=-3))
UA = {"User-Agent": "AsunBoid-WeeklySummary/2.0"}
TITLES = {"zomboid": "Project Zomboid", "terraria": "Terraria", "minecraft": "Minecraft"}

HOURS_7D = ('label_replace(sum by (player) (count_over_time(zomboid_player_online[7d])) * 30, "game", "zomboid", "", "") '
            'or sum by (game, player) (count_over_time(game_player_online[7d])) * 30')
PEAK_7D = "max_over_time(((sum(zomboid_players_online) or vector(0)) + (sum(game_players_online) or vector(0)))[7d:1m])"
AVAIL_7D = "avg_over_time(((max(zomboid_up) or vector(0)) + (max(game_up) or vector(0)) > bool 0)[7d:5m])"
DEATHS_7D = "sum by (player) (increase(zomboid_player_deaths_total[7d]))"


def q(expr):
    url = f"{PROM}/api/v1/query?{urllib.parse.urlencode({'query': expr})}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        return json.load(r)["data"]["result"]


def scalar(expr, default=0.0):
    res = q(expr)
    return float(res[0]["value"][1]) if res else default


def hm(seconds):
    h, m = divmod(int(seconds) // 60, 60)
    return f"{h}h{m:02d}" if h else f"{m} min"


def build():
    per_game, per_player, breakdown = defaultdict(float), defaultdict(float), defaultdict(dict)
    for r in q(HOURS_7D):
        game, player, sec = r["metric"].get("game", "?"), r["metric"].get("player", "?"), float(r["value"][1])
        per_game[game] += sec
        per_player[player] += sec
        breakdown[player][game] = sec
    total = sum(per_game.values())
    peak = scalar(PEAK_7D)
    availability = scalar(AVAIL_7D)
    deaths = sorted(((r["metric"].get("player", "?"), round(float(r["value"][1]))) for r in q(DEATHS_7D)),
                    key=lambda x: x[1], reverse=True)
    deaths = [d for d in deaths if d[1] > 0]
    medals = ["🥇", "🥈", "🥉"]

    ranking_lines = []
    for i, (player, sec) in enumerate(sorted(per_player.items(), key=lambda x: x[1], reverse=True)[:6]):
        parts = breakdown[player]
        detail = f" ({', '.join(f'{TITLES.get(g, g)} {hm(s)}' for g, s in sorted(parts.items(), key=lambda x: -x[1]))})" if len(parts) > 1 else ""
        ranking_lines.append(f"{medals[i] if i < 3 else '▫️'} **{player}** · {hm(sec)}{detail}")
    games_lines = [f"**{TITLES.get(g, g)}** · {hm(s)}" for g, s in sorted(per_game.items(), key=lambda x: -x[1])]

    fields = [
        {"name": "⏱️ Horas jogadas", "value": hm(total), "inline": True},
        {"name": "👥 Jogadores", "value": str(len(per_player)), "inline": True},
        {"name": "📈 Pico simultâneo", "value": f"{int(peak)} / 6", "inline": True},
    ]
    if games_lines:
        fields.append({"name": "🎮 Por jogo", "value": "\n".join(games_lines), "inline": False})
    fields.append({"name": "🏆 Ranking da semana", "value": "\n".join(ranking_lines) or "Ninguém jogou nesta semana.", "inline": False})
    if deaths:
        fields.append({"name": "💀 Mortes no Zomboid", "value": "\n".join(f"**{p}** · {n}x" for p, n in deaths), "inline": False})
    fields.append({"name": "🟢 Servidor no ar", "value": f"{availability * 100:.2f}% do tempo", "inline": True})

    end = datetime.now(BRT)
    start = end - timedelta(days=7)
    return {
        "username": "AsunBoid",
        "embeds": [{
            "title": "📊 Resumo da semana no AsunBoid",
            "description": f"{start:%d/%m} a {end:%d/%m}",
            "color": 0x5865F2,
            "fields": fields,
            "footer": {"text": "Dados do Prometheus · enviado automaticamente todo domingo"},
            "timestamp": end.isoformat(),
        }],
    }


def main():
    payload = json.dumps(build()).encode("utf-8")
    if os.getenv("DRY_RUN"):
        print(json.dumps(json.loads(payload), indent=2, ensure_ascii=False), flush=True)
        return
    req = urllib.request.Request(WEBHOOK, data=payload, method="POST",
                                 headers={**UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        print(f"discord: HTTP {r.status}", flush=True)


if __name__ == "__main__":
    main()
