# Gerado por scripts/render-games.py a partir de platform/exporter/gameexporter.py: não edite à mão.
"""Lib comum dos exporters de jogo (só stdlib): contrato game_*, estado persistido e /metrics.

Cada jogo tem um exporter.py curto que chama main(); o "driver" diz como descobrir se o servidor
está no ar e quem está conectado. Esta é a fonte: o scripts/render-games.py copia o arquivo para
k8s/games/<jogo>/scripts/gameexporter.py de todo jogo com `exporter: shared` no game.yaml
(o kustomize não lê arquivo fora do diretório do jogo).

Métricas (label game=<jogo>):
  game_up, game_players_online, game_player_online{player},
  game_player_playtime_seconds_total{player} (persistido em STATE_FILE, no volume do jogo),
  game_exporter_last_poll_timestamp_seconds, game_exporter_probe_duration_seconds.

Drivers (GAME_DRIVER):
  listen  game_up pelo socket da porta do jogo em /proc/net (TCP em LISTEN ou UDP aberto). Não abre
          conexão, então não ocupa vaga. Jogadores ficam em 0.
  rcon    protocolo Source RCON (Minecraft, Zomboid, Palworld, ARK, CS2...). RCON_PLAYERS_COMMAND é
          o comando que lista jogadores e RCON_PLAYERS_REGEX tira os nomes da resposta, de um de
          dois jeitos: grupo "list" (nomes separados por vírgula, ex.: o `list` do Minecraft) ou
          grupo "name" (um casamento por jogador, ex.: uma linha por nome). No modo "list", resposta
          que não casa conta como servidor fora, para mudança de formato não virar "0 jogadores";
          no modo "name", qualquer resposta do RCON já é servidor no ar (zero casamentos = vazio).
Para um protocolo próprio (REST, log...), passe main(players=função): ela devolve a lista de nomes
conectados, ou None se o servidor não respondeu.
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

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "15"))
STATE_FILE = os.getenv("STATE_FILE", "/data/exporter/state.json")

SERVERDATA_AUTH, SERVERDATA_EXECCOMMAND = 3, 2
COLOR_CODES = re.compile(r"§.")   # códigos de cor do Minecraft; inofensivo nos outros jogos

lock = threading.Lock()
state = {"up": 0, "names": [], "last_poll": 0.0, "probe_seconds": 0.0, "playtime": {}}


# ---------- driver listen ----------
def port_open(port, protocol):
    """Socket da porta em /proc/net: estado 0A (LISTEN) no TCP, 07 (aberto, sem peer) no UDP."""
    files, wanted = (("tcp", "tcp6"), "0A") if protocol.upper() == "TCP" else (("udp", "udp6"), "07")
    for name in files:
        try:
            with open(f"/proc/net/{name}", encoding="ascii") as f:
                next(f)   # cabeçalho
                for line in f:
                    cols = line.split()
                    if int(cols[1].rsplit(":", 1)[1], 16) == port and cols[3] == wanted:
                        return True
        except FileNotFoundError:
            continue
    return False


def listen_driver():
    port, protocol = int(os.environ["GAME_PORT"]), os.getenv("GAME_PROTOCOL", "TCP")
    return lambda: [] if port_open(port, protocol) else None


# ---------- driver rcon ----------
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


def rcon(host, port, password, command, timeout=5.0):
    """Uma conexão por comando: auth, comando, resposta (um pacote)."""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(_packet(1, SERVERDATA_AUTH, password))
        while True:   # alguns servidores mandam um RESPONSE_VALUE vazio antes do AUTH_RESPONSE
            req_id, ptype, _ = _read_packet(sock)
            if req_id == -1:
                raise PermissionError("RCON authentication failed")
            if ptype == 2:
                break
        sock.sendall(_packet(2, SERVERDATA_EXECCOMMAND, command))
        return _read_packet(sock)[2]


def parse_players(output, regex):
    """Nomes conectados pela regex (grupo "list" separado por vírgula, ou um grupo "name" por jogador)."""
    clean = COLOR_CODES.sub("", output)
    if "list" in regex.groupindex:
        m = regex.search(clean)
        if not m:
            return None
        return [n.strip() for n in (m.group("list") or "").split(",") if n.strip()]
    return [m.group("name").strip() for m in regex.finditer(clean) if m.group("name").strip()]


def rcon_driver():
    host, port = os.getenv("RCON_HOST", "127.0.0.1"), int(os.environ["RCON_PORT"])
    password, command = os.environ["RCON_PASSWORD"], os.environ["RCON_PLAYERS_COMMAND"]
    regex = re.compile(os.environ["RCON_PLAYERS_REGEX"], re.M)
    if not {"list", "name"} & set(regex.groupindex):
        raise SystemExit("RCON_PLAYERS_REGEX precisa de um grupo nomeado 'list' ou 'name'")
    timeout = float(os.getenv("RCON_TIMEOUT", "5"))

    def players():
        names = parse_players(rcon(host, port, password, command, timeout), regex)
        if names is None:
            print(f"resposta inesperada de '{command}' (confira RCON_PLAYERS_REGEX)", flush=True)
        return names
    return players


DRIVERS = {"listen": listen_driver, "rcon": rcon_driver}


# ---------- estado e contrato ----------
def poll(players):
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


def worker(players):
    last_save = time.time()
    while True:
        poll(players)
        if time.time() - last_save >= 60:
            try:
                save_state()
                last_save = time.time()
            except Exception as exc:  # noqa: BLE001
                print(f"state save error: {exc}", flush=True)
        time.sleep(POLL_SECONDS)


def _esc(value):
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def render(game):
    with lock:
        s = {"up": state["up"], "names": list(state["names"]), "last_poll": state["last_poll"],
             "probe_seconds": state["probe_seconds"], "playtime": dict(state["playtime"])}
    g = f'game="{_esc(game)}"'
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


def main(players=None):
    game = os.environ["GAME"]
    port = int(os.getenv("EXPORTER_PORT", "9105"))
    if players is None:
        driver = os.getenv("GAME_DRIVER", "listen")
        if driver not in DRIVERS:
            raise SystemExit(f"GAME_DRIVER '{driver}' desconhecido (use {', '.join(DRIVERS)})")
        players = DRIVERS[driver]()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/metrics":
                self.send_response(404)
                self.end_headers()
                return
            body = render(game).encode("utf-8")
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

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    load_state()
    threading.Thread(target=worker, args=(players,), daemon=True).start()
    print(f"{game} exporter listening on :{port}", flush=True)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
