"""Exporter Prometheus do Terraria (TShock REST), só stdlib.

Segue o contrato de métricas da plataforma (game_*, label game="terraria"):
  game_up, game_players_online, game_player_online{player},
  game_player_playtime_seconds_total{player} (persistido no volume do jogo).
A API REST do TShock é consultada em segundo plano; /metrics devolve o último estado.
"""
import json
import os
import signal
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

GAME = os.getenv("GAME", "terraria")
TOKEN = os.environ["TSHOCK_TOKEN"]
REST = os.getenv("TSHOCK_REST", "http://127.0.0.1:7878")
PORT = int(os.getenv("EXPORTER_PORT", "9105"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "15"))
STATE_FILE = os.getenv("STATE_FILE", "/data/exporter/state.json")

lock = threading.Lock()
state = {"up": 0, "names": [], "last_poll": 0.0, "rest_seconds": 0.0, "playtime": {}}


def rest(path):
    url = f"{REST}{path}?{urllib.parse.urlencode({'token': TOKEN})}"
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.load(r)


def player_names(data):
    names = []
    for p in data.get("players", []) or []:
        name = p if isinstance(p, str) else (p.get("nickname") or p.get("name") or p.get("username"))
        if name:
            names.append(str(name))
    return names


def poll():
    start = time.time()
    up, names = 0, []
    try:
        data = rest("/v2/players/list")
        if str(data.get("status")) == "200":
            up, names = 1, player_names(data)
        else:
            print(f"rest status inesperado: {data.get('status')} {data.get('error', '')}", flush=True)
    except Exception as exc:  # noqa: BLE001 - qualquer falha = servidor indisponível
        print(f"rest error: {exc}", flush=True)
    with lock:
        previous = state["last_poll"]
        state.update(up=up, names=names, last_poll=time.time(), rest_seconds=time.time() - start)
        if previous:
            elapsed = min(time.time() - previous, POLL_SECONDS * 4)
            for n in names:
                state["playtime"][n] = state["playtime"].get(n, 0.0) + elapsed


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state["playtime"].update(json.load(f).get("playtime", {}))
        print(f"state loaded: {len(state['playtime'])} players", flush=True)
    except FileNotFoundError:
        print("state file not found, starting fresh", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"state load error: {exc}", flush=True)


def save_state():
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with lock:
        data = {"playtime": dict(state["playtime"])}
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def worker():
    last_save = time.time()
    while True:
        poll()
        if time.time() - last_save >= 60:
            try:
                save_state()
                last_save = time.time()
            except Exception as exc:  # noqa: BLE001
                print(f"state save error: {exc}", flush=True)
        time.sleep(POLL_SECONDS)


def _esc(value):
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def render():
    with lock:
        s = {"up": state["up"], "names": list(state["names"]), "last_poll": state["last_poll"],
             "rest_seconds": state["rest_seconds"], "playtime": dict(state["playtime"])}
    g = f'game="{_esc(GAME)}"'
    out = [
        "# HELP game_up 1 if the game server answered on the last poll.", "# TYPE game_up gauge",
        f"game_up{{{g}}} {s['up']}",
        "# HELP game_players_online Connected players.", "# TYPE game_players_online gauge",
        f"game_players_online{{{g}}} {len(s['names'])}",
        "# HELP game_exporter_last_poll_timestamp_seconds Last poll.", "# TYPE game_exporter_last_poll_timestamp_seconds gauge",
        f"game_exporter_last_poll_timestamp_seconds{{{g}}} {s['last_poll']:.0f}",
        "# HELP game_exporter_probe_duration_seconds Probe round-trip.", "# TYPE game_exporter_probe_duration_seconds gauge",
        f"game_exporter_probe_duration_seconds{{{g}}} {s['rest_seconds']:.4f}",
        "# HELP game_player_online Connected player (one series per player).", "# TYPE game_player_online gauge",
    ]
    out += [f'game_player_online{{{g},player="{_esc(n)}"}} 1' for n in s["names"]]
    out += ["# HELP game_player_playtime_seconds_total All-time playtime per player (persisted).",
            "# TYPE game_player_playtime_seconds_total counter"]
    out += [f'game_player_playtime_seconds_total{{{g},player="{_esc(p)}"}} {v:.0f}' for p, v in s["playtime"].items()]
    return "\n".join(out) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        body = render().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def shutdown(*_):
    try:
        save_state()
        print("state saved, exiting", flush=True)
    finally:
        sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    load_state()
    threading.Thread(target=worker, daemon=True).start()
    print(f"terraria exporter listening on :{PORT}", flush=True)
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
