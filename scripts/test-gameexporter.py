#!/usr/bin/env python3
"""Testes da lib comum de exporter (platform/exporter/gameexporter.py), só stdlib.

Sobe um servidor Source RCON falso em localhost para conferir auth, comando e as duas formas de
regex, e abre sockets TCP e UDP de verdade para o driver listen. Uso: python3 scripts/test-gameexporter.py
"""
import importlib.util
import pathlib
import re
import socket
import sys
import tempfile
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gameexporter", ROOT / "platform" / "exporter" / "gameexporter.py")
ge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ge)

MINECRAFT_LIST = r"There are \d+ of a max(?: of)? \d+ players online:?\s*(?P<list>.*)"
ZOMBOID_PLAYERS = r"^-(?P<name>.+)$"


class FakeRcon:
    """Servidor Source RCON mínimo: responde `responses[comando]`; empty_first imita o pacote vazio do Source."""

    def __init__(self, password, responses, empty_first=False):
        self.password, self.responses, self.empty_first = password, responses, empty_first
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            conn, _ = self.sock.accept()
            with conn:
                req_id, _, body = ge._read_packet(conn)
                if self.empty_first:
                    conn.sendall(ge._packet(req_id, 0, ""))
                conn.sendall(ge._packet(req_id if body == self.password else -1, 2, ""))
                if body != self.password:
                    continue
                req_id, _, command = ge._read_packet(conn)
                conn.sendall(ge._packet(req_id, 0, self.responses.get(command, "Unknown command")))


class RconTest(unittest.TestCase):
    def test_minecraft_list_with_color_codes(self):
        srv = FakeRcon("s3nha", {"list": "There are 2 of a max of 6 players online: §aIguinho, joao"})
        out = ge.rcon("127.0.0.1", srv.port, "s3nha", "list")
        self.assertEqual(ge.parse_players(out, re.compile(MINECRAFT_LIST, re.M)), ["Iguinho", "joao"])

    def test_list_mode_empty_server(self):
        names = ge.parse_players("There are 0 of a max of 6 players online:", re.compile(MINECRAFT_LIST, re.M))
        self.assertEqual(names, [])

    def test_list_mode_unexpected_format_is_down(self):
        self.assertIsNone(ge.parse_players("Unknown command", re.compile(MINECRAFT_LIST, re.M)))

    def test_name_mode_one_line_per_player_and_source_auth(self):
        srv = FakeRcon("pw", {"players": "Players connected (2):\n-Ana\n-Bia\n"}, empty_first=True)
        out = ge.rcon("127.0.0.1", srv.port, "pw", "players")
        self.assertEqual(ge.parse_players(out, re.compile(ZOMBOID_PLAYERS, re.M)), ["Ana", "Bia"])

    def test_name_mode_no_players_is_up(self):
        self.assertEqual(ge.parse_players("Players connected (0):\n", re.compile(ZOMBOID_PLAYERS, re.M)), [])

    def test_wrong_password(self):
        srv = FakeRcon("certa", {})
        with self.assertRaises(PermissionError):
            ge.rcon("127.0.0.1", srv.port, "errada", "list")


class ListenTest(unittest.TestCase):
    def test_tcp_listen_and_closed(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        self.assertFalse(ge.port_open(port, "TCP"))   # bind sem listen ainda não é servidor
        s.listen()
        self.assertTrue(ge.port_open(port, "TCP"))
        s.close()
        self.assertFalse(ge.port_open(port, "TCP"))

    def test_udp_open(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        self.assertTrue(ge.port_open(port, "UDP"))
        s.close()
        self.assertFalse(ge.port_open(port, "UDP"))


class ContractTest(unittest.TestCase):
    def setUp(self):
        ge.state.update(up=0, names=[], last_poll=0.0, probe_seconds=0.0, playtime={})

    def test_poll_render_and_state_roundtrip(self):
        ge.poll(lambda: ["Ana"])
        ge.state["last_poll"] -= 10   # finge que o poll anterior foi há 10 s
        ge.poll(lambda: ["Ana", 'B"ia'])
        text = ge.render("exemplo")
        self.assertIn('game_up{game="exemplo"} 1', text)
        self.assertIn('game_players_online{game="exemplo"} 2', text)
        self.assertIn('game_player_online{game="exemplo",player="B\\"ia"} 1', text)
        self.assertRegex(text, r'game_player_playtime_seconds_total\{game="exemplo",player="Ana"\} 1\d')
        with tempfile.TemporaryDirectory() as d:
            ge.STATE_FILE = f"{d}/exporter/state.json"
            ge.save_state()
            ge.state["playtime"] = {}
            ge.load_state()
        self.assertGreaterEqual(ge.state["playtime"]["Ana"], 10)

    def test_probe_failure_is_down_and_keeps_playtime(self):
        ge.state["playtime"] = {"Ana": 100.0}

        def boom():
            raise ConnectionRefusedError("server down")
        ge.poll(boom)
        text = ge.render("exemplo")
        self.assertIn('game_up{game="exemplo"} 0', text)
        self.assertIn('game_players_online{game="exemplo"} 0', text)
        self.assertIn('player="Ana"} 100', text)


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False, verbosity=1).result.wasSuccessful() else 1)
