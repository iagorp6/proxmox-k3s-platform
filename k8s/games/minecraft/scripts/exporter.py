"""Exporter Prometheus do Minecraft (RCON), só stdlib.

Segue o contrato de métricas da plataforma (game_*, label game="minecraft"):
  game_up, game_players_online, game_player_online{player},
  game_player_playtime_seconds_total{player} (persistido no volume do jogo).
O comando RCON `list` é consultado em segundo plano; /metrics devolve o último estado.
"""
import json
import os
import re
import signal
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

GAME = os.getenv("GAME", "minecraft")
RCON_HOST = os.getenv("RCON_HOST", "127.0.0.1")
RCON_PORT = int(os.getenv("RCON_PORT", "25575"))
RCON_PASSWORD = os.environ["RCON_PASSWORD"]
PORT = int(os.getenv("EXPORTER_PORT", "9105"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "15"))
STATE_FILE = os.getenv("STATE_FILE", "/data/exporter/state.json")
# Ex.: "There are 2 of a max of 6 players online: IguinhoGamerPvP, joao"
LIST_RE = re.compile(r"There are (\d+) of a max(?: of)? \d+ players online:?\s*(.*)", re.S)

lock = threading.Lock()
state = {"up": 0, "names": [], "last_poll": 0.0, "rcon_seconds": 0.0, "playtime": {}}


def _packet(req_id, ptype, body):
    payload = body.encode("utf-8") + b"\x00\x00"
    return struct.pack("<iii", len(payload) + 8, req_id, ptype) + payload


def _recv_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("RCON connection closed")
        data += chunk
    return data


def _read_packet(sock):
    (size,) = struct.unpack("<i", _recv_exact(sock, 4))
    data = _recv_exact(sock, size)
    req_id, ptype = struct.unpack("<ii", data[:8])
    return req_id, ptype, data[8:-2].decode("utf-8", errors="replace")


def rcon(command):
    with socket.create_connection((RCON_HOST, RCON_PORT), timeout=5) as sock:
        sock.sendall(_packet(1, 3, RCON_PASSWORD))
        req_id, _, _ = _read_packet(sock)
        if req_id == -1:
            raise PermissionError("RCON authentication failed")
        sock.sendall(_packet(2, 2, command))
        _, _, body = _read_packet(sock)
        return body


def parse_list(output):
    clean = re.sub(r"§.", "", output)  # remove códigos de cor do Minecraft
    m = LIST_RE.search(clean)
    if not m:
        return None
    return [n.strip() for n in m.group(2).split(",") if n.strip()]


def poll():
    start = time.time()
    up, names = 0, []
    try:
        parsed = parse_list(rcon("list"))
        if parsed is not None:
            up, names = 1, parsed
        else:
            print("resposta inesperada do comando list", flush=True)
    except Exception as exc:  # noqa: BLE001 - qualquer falha = servidor indisponível
        print(f"rcon error: {exc}", flush=True)
    with lock:
        previous = state["last_poll"]
        state.update(up=up, names=names, last_poll=time.time(), rcon_seconds=time.time() - start)
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
             "rcon_seconds": state["rcon_seconds"], "playtime": dict(state["playtime"])}
    g = f'game="{_esc(GAME)}"'
    out = [
        "# HELP game_up 1 if the game server answered on the last poll.", "# TYPE game_up gauge",
        f"game_up{{{g}}} {s['up']}",
        "# HELP game_players_online Connected players.", "# TYPE game_players_online gauge",
        f"game_players_online{{{g}}} {len(s['names'])}",
        "# HELP game_exporter_last_poll_timestamp_seconds Last poll.", "# TYPE game_exporter_last_poll_timestamp_seconds gauge",
        f"game_exporter_last_poll_timestamp_seconds{{{g}}} {s['last_poll']:.0f}",
        "# HELP game_exporter_probe_duration_seconds Probe round-trip.", "# TYPE game_exporter_probe_duration_seconds gauge",
        f"game_exporter_probe_duration_seconds{{{g}}} {s['rcon_seconds']:.4f}",
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
    print(f"minecraft exporter listening on :{PORT}", flush=True)
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
