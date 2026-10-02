"""Resumo semanal do servidor no Discord (stdlib only)."""
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

PROM = os.getenv("PROMETHEUS_URL", "http://kps-kube-prometheus-stack-prometheus.monitoring.svc:9090")
WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
BRT = timezone(timedelta(hours=-3))
UA = {"User-Agent": "AsunBoid-WeeklySummary/1.0"}


def q(expr):
    url = f"{PROM}/api/v1/query?{urllib.parse.urlencode({'query': expr})}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        return json.load(r)["data"]["result"]


def scalar(expr, default=0.0):
    res = q(expr)
    return float(res[0]["value"][1]) if res else default


def by_player(expr):
    return sorted(((r["metric"].get("player", "?"), float(r["value"][1])) for r in q(expr)),
                  key=lambda x: x[1], reverse=True)


def hm(seconds):
    h, m = divmod(int(seconds) // 60, 60)
    return f"{h}h{m:02d}" if h else f"{m} min"


def build():
    hours = by_player("sum by (player) (count_over_time(zomboid_player_online[7d])) * 30")
    total = sum(v for _, v in hours)
    peak = scalar("max_over_time(max(zomboid_players_online)[7d:1m])")
    availability = scalar("avg_over_time((max(zomboid_up) or vector(0))[7d:5m])")
    deaths = [(p, v) for p, v in by_player("sum by (player) (increase(zomboid_player_deaths_total[7d]))") if round(v) > 0]
    medals = ["🥇", "🥈", "🥉"]

    ranking = "\n".join(f"{medals[i] if i < 3 else '▫️'} **{p}** · {hm(v)}" for i, (p, v) in enumerate(hours[:6])) \
        or "Ninguém jogou nesta semana."
    fields = [
        {"name": "⏱️ Horas jogadas", "value": hm(total), "inline": True},
        {"name": "👥 Jogadores", "value": str(len(hours)), "inline": True},
        {"name": "📈 Pico simultâneo", "value": f"{int(peak)} / 6", "inline": True},
        {"name": "🏆 Ranking da semana", "value": ranking, "inline": False},
    ]
    if deaths:
        fields.append({"name": "💀 Mortes", "value": "\n".join(f"**{p}** · {round(v)}x" for p, v in deaths), "inline": False})
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
    req = urllib.request.Request(WEBHOOK, data=payload, method="POST",
                                 headers={**UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        print(f"discord: HTTP {r.status}", flush=True)


if __name__ == "__main__":
    main()
