"""Exporter Prometheus genérico de jogo (modelo do scripts/new-game.py), só stdlib.

Segue o contrato de métricas da plataforma (game_*, label game=<jogo>):
  game_up, game_players_online, game_player_online{player},
  game_player_playtime_seconds_total{player} (persistido no volume do jogo).

game_up vem do socket da porta do jogo em /proc/net (o sidecar divide a rede com o servidor):
TCP em LISTEN ou UDP aberto. Não abre conexão, então não ocupa vaga de jogador. Para contar
jogadores, troque players() pela consulta do protocolo do jogo (RCON, REST, log...): ela devolve
a lista de nomes conectados, ou None se o servidor não respondeu.
"""
import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

GAME = os.environ["GAME"]
GAME_PORT = int(os.environ["GAME_PORT"])
GAME_PROTOCOL = os.getenv("GAME_PROTOCOL", "TCP").upper()
PORT = int(os.getenv("EXPORTER_PORT", "9105"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "15"))
STATE_FILE = os.getenv("STATE_FILE", "/data/exporter/state.json")

lock = threading.Lock()
state = {"up": 0, "names": [], "last_poll": 0.0, "probe_seconds": 0.0, "playtime": {}}


def port_open():
    """Socket da porta do jogo em /proc/net: estado 0A (LISTEN) no TCP, 07 (aberto, sem peer) no UDP."""
    files, wanted = (("tcp", "tcp6"), "0A") if GAME_PROTOCOL == "TCP" else (("udp", "udp6"), "07")
    for name in files:
        try:
            with open(f"/proc/net/{name}", encoding="ascii") as f:
                next(f)   # cabeçalho
                for line in f:
                    cols = line.split()
                    if int(cols[1].rsplit(":", 1)[1], 16) == GAME_PORT and cols[3] == wanted:
                        return True
        except FileNotFoundError:
            continue
    return False


def players():
    """Jogadores conectados. Sem protocolo do jogo ainda: lista vazia com o servidor no ar."""
    return [] if port_open() else None


def poll():
    start = time.time()
    try:
        names = players()
    except Exception as exc:  # noqa: BLE001 - qualquer falha = servidor indisponível
        print(f"probe error: {exc}", flush=True)
        names = None
    with lock:
        previous = state["last_poll"]
        state.update(up=int(names is not None), names=names or [], last_poll=time.time(),
                     probe_seconds=time.time() - start)
        if previous:
            elapsed = min(time.time() - previous, POLL_SECONDS * 4)
            for n in names or []:
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
             "probe_seconds": state["probe_seconds"], "playtime": dict(state["playtime"])}
    g = f'game="{_esc(GAME)}"'
    out = [
        "# HELP game_up 1 if the game server answered on the last poll.", "# TYPE game_up gauge",
        f"game_up{{{g}}} {s['up']}",
        "# HELP game_players_online Connected players.", "# TYPE game_players_online gauge",
        f"game_players_online{{{g}}} {len(s['names'])}",
        "# HELP game_exporter_last_poll_timestamp_seconds Last poll.", "# TYPE game_exporter_last_poll_timestamp_seconds gauge",
        f"game_exporter_last_poll_timestamp_seconds{{{g}}} {s['last_poll']:.0f}",
        "# HELP game_exporter_probe_duration_seconds Probe round-trip.", "# TYPE game_exporter_probe_duration_seconds gauge",
        f"game_exporter_probe_duration_seconds{{{g}}} {s['probe_seconds']:.4f}",
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
    print(f"{GAME} exporter listening on :{PORT}", flush=True)
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
