"""Prometheus exporter for Project Zomboid (stdlib only).

- Players online via Source RCON (polled in background).
- All-time playtime per player, persisted on the game volume.
- Player deaths parsed from the server PerkLog files.
"""
import glob
import json
import os
import re
import signal
import socket
import struct
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

RCON_HOST = os.getenv("RCON_HOST", "127.0.0.1")
RCON_PORT = int(os.getenv("RCON_PORT", "27015"))
RCON_PASSWORD = os.environ["RCON_PASSWORD"]
RCON_TIMEOUT = float(os.getenv("RCON_TIMEOUT", "5"))
LISTEN_PORT = int(os.getenv("EXPORTER_PORT", "9105"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "15"))
LOG_DIR = os.getenv("PZ_LOG_DIR", "/pz/data/Logs")
STATE_FILE = os.getenv("STATE_FILE", "/pz/data/exporter/state.json")
SEED_URL = os.getenv("SEED_PROMETHEUS_URL", "")
MILESTONES = [int(h) for h in os.getenv("MILESTONES_HOURS", "10,25,50,100,200,500,1000").split(",")]

SERVERDATA_AUTH = 3
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_AUTH_RESPONSE = 2
PLAYERS_RE = re.compile(r"Players connected \((\d+)\)")
# Ex.: [02-10-26 21:14:05.123] [76561198000000000][iago][10512,9876,0][Died][Hours Survived: 34].
DEATH_RE = re.compile(
    r"\[(?P<player>[^\[\]]+)\]\[-?[\d.]+,-?[\d.]+,-?[\d.]+\]\[Died\](?:\[Hours Survived: (?P<hours>[\d.]+)\])?"
)

lock = threading.Lock()
state = {
    "up": 0, "players": 0, "names": [], "rcon_seconds": 0.0, "last_poll": 0.0,
    "playtime": {},          # player -> seconds (all time, persisted)
    "deaths": {},            # player -> count since exporter start
    "last_death_ts": {},     # player -> unix ts (persisted)
    "last_death_hours": {},  # player -> in-game hours survived (persisted)
}
offsets = {}


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
    with socket.create_connection((RCON_HOST, RCON_PORT), timeout=RCON_TIMEOUT) as sock:
        sock.sendall(_packet(1, SERVERDATA_AUTH, RCON_PASSWORD))
        while True:
            req_id, ptype, _ = _read_packet(sock)
            if ptype == SERVERDATA_AUTH_RESPONSE:
                if req_id == -1:
                    raise PermissionError("RCON authentication failed")
                break
        sock.sendall(_packet(2, SERVERDATA_EXECCOMMAND, command))
        _, _, body = _read_packet(sock)
        return body


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        for key in ("playtime", "last_death_ts", "last_death_hours"):
            state[key].update(saved.get(key, {}))
        print(f"state loaded: {len(state['playtime'])} players", flush=True)
    except FileNotFoundError:
        print("state file not found, starting fresh", flush=True)
        if SEED_URL:
            seed_from_prometheus()
    except Exception as exc:  # noqa: BLE001
        print(f"state load error: {exc}", flush=True)


def seed_from_prometheus():
    """Primeira execução: semeia o tempo de jogo com o histórico que o Prometheus já tem (30 dias)."""
    expr = "sum by (player) (count_over_time(zomboid_player_online[30d])) * 30"
    url = f"{SEED_URL}/api/v1/query?{urllib.parse.urlencode({'query': expr})}"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            result = json.load(r)["data"]["result"]
        for item in result:
            state["playtime"][item["metric"]["player"]] = float(item["value"][1])
        print(f"seeded playtime for {len(result)} players from Prometheus", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"seed error (starting from zero): {exc}", flush=True)


def save_state():
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with lock:
        data = {k: dict(state[k]) for k in ("playtime", "last_death_ts", "last_death_hours")}
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def poll_rcon():
    start = time.time()
    up, players, names = 0, 0, []
    try:
        output = rcon("players")
        match = PLAYERS_RE.search(output)
        if match:
            up = 1
            players = int(match.group(1))
            names = [l[1:].strip() for l in output.splitlines() if l.startswith("-") and l[1:].strip()]
    except Exception as exc:  # noqa: BLE001 - qualquer falha = servidor indisponível
        print(f"rcon error: {exc}", flush=True)
    with lock:
        previous = state["last_poll"]
        state.update(up=up, players=players, names=names, rcon_seconds=time.time() - start, last_poll=time.time())
        if previous:
            elapsed = min(time.time() - previous, POLL_SECONDS * 4)
            for n in names:
                state["playtime"][n] = state["playtime"].get(n, 0.0) + elapsed
                state["deaths"].setdefault(n, 0)


def scan_deaths(first):
    for path in sorted(glob.glob(os.path.join(LOG_DIR, "*PerkLog*.txt"))):
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if path not in offsets:
            offsets[path] = size if first else 0
        if size < offsets[path]:
            offsets[path] = 0
        if size == offsets[path]:
            continue
        with open(path, "rb") as f:
            f.seek(offsets[path])
            chunk = f.read(size - offsets[path])
        end = chunk.rfind(b"\n")
        if end < 0:
            continue
        offsets[path] += end + 1
        for line in chunk[: end + 1].decode("utf-8", errors="replace").splitlines():
            m = DEATH_RE.search(line)
            if not m:
                continue
            player = m.group("player").strip()
            with lock:
                state["deaths"][player] = state["deaths"].get(player, 0) + 1
                state["last_death_ts"][player] = time.time()
                if m.group("hours"):
                    state["last_death_hours"][player] = float(m.group("hours"))
            print(f"death detected: {player} ({m.group('hours') or '?'}h survived)", flush=True)


def worker():
    first, last_save = True, time.time()
    while True:
        poll_rcon()
        try:
            scan_deaths(first)
        except Exception as exc:  # noqa: BLE001
            print(f"log scan error: {exc}", flush=True)
        first = False
        if time.time() - last_save >= 60:
            try:
                save_state()
                last_save = time.time()
            except Exception as exc:  # noqa: BLE001
                print(f"state save error: {exc}", flush=True)
        time.sleep(POLL_SECONDS)


def _escape(value):
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _milestone(hours):
    reached = [m for m in MILESTONES if hours >= m]
    return reached[-1] if reached else 0


def render():
    with lock:
        s = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v) for k, v in state.items()}
    out = [
        "# HELP zomboid_up 1 if the game server answered RCON on the last poll.",
        "# TYPE zomboid_up gauge",
        f"zomboid_up {s['up']}",
        "# HELP zomboid_players_online Number of connected players.",
        "# TYPE zomboid_players_online gauge",
        f"zomboid_players_online {s['players']}",
        "# HELP zomboid_rcon_scrape_duration_seconds RCON round-trip time.",
        "# TYPE zomboid_rcon_scrape_duration_seconds gauge",
        f"zomboid_rcon_scrape_duration_seconds {s['rcon_seconds']:.4f}",
        "# HELP zomboid_exporter_last_poll_timestamp_seconds Last RCON poll.",
        "# TYPE zomboid_exporter_last_poll_timestamp_seconds gauge",
        f"zomboid_exporter_last_poll_timestamp_seconds {s['last_poll']:.0f}",
        "# HELP zomboid_player_online Connected player (one series per player).",
        "# TYPE zomboid_player_online gauge",
    ]
    out += [f'zomboid_player_online{{player="{_escape(n)}"}} 1' for n in s["names"]]
    out += ["# HELP zomboid_player_playtime_seconds_total All-time playtime per player (persisted).",
            "# TYPE zomboid_player_playtime_seconds_total counter"]
    out += [f'zomboid_player_playtime_seconds_total{{player="{_escape(p)}"}} {v:.0f}' for p, v in s["playtime"].items()]
    out += ["# HELP zomboid_player_milestone_hours Highest playtime milestone reached.",
            "# TYPE zomboid_player_milestone_hours gauge"]
    out += [f'zomboid_player_milestone_hours{{player="{_escape(p)}"}} {_milestone(v / 3600)}' for p, v in s["playtime"].items()]
    out += ["# HELP zomboid_player_deaths_total Character deaths since exporter start.",
            "# TYPE zomboid_player_deaths_total counter"]
    out += [f'zomboid_player_deaths_total{{player="{_escape(p)}"}} {v}' for p, v in s["deaths"].items()]
    out += ["# HELP zomboid_player_last_death_timestamp_seconds Time of the last character death.",
            "# TYPE zomboid_player_last_death_timestamp_seconds gauge"]
    out += [f'zomboid_player_last_death_timestamp_seconds{{player="{_escape(p)}"}} {v:.0f}' for p, v in s["last_death_ts"].items()]
    out += ["# HELP zomboid_player_last_death_hours_survived In-game hours survived by the last dead character.",
            "# TYPE zomboid_player_last_death_hours_survived gauge"]
    out += [f'zomboid_player_last_death_hours_survived{{player="{_escape(p)}"}} {v}' for p, v in s["last_death_hours"].items()]
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
    print(f"zomboid exporter listening on :{LISTEN_PORT}", flush=True)
    HTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()
