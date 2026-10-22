#!/usr/bin/env python3
"""Testes do games-bot (apps/games-bot/bot.py), só stdlib.

O discord.py não é instalado no CI de scripts: um módulo `discord` mínimo (as classes que o bot usa)
entra em sys.modules antes do import. Os testes cobrem a lógica que decide quem troca e quando:
cargo, jogo já ativo, bloqueio com jogador online ou sem contagem, cooldown, fila (um pedido por
vez, expiração, cancelamento), a trava contra duas trocas simultâneas e as etapas lidas da saída
do switch-game.py. Uso: python3 scripts/test-games-bot.py
"""
import asyncio
import datetime as dt
import importlib.util
import pathlib
import sys
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def fake_discord():
    """Só o que o bot.py toca no import e nos caminhos testados."""
    d = types.ModuleType("discord")

    class View:
        def __init__(self, timeout=None):
            self.timeout, self.children = timeout, []

        def add_item(self, item):
            self.children.append(item)

    class Button:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class Embed:
        def __init__(self, title=None, description=None, color=None):
            self.title, self.description, self.color, self.footer = title, description, color, None

        def set_footer(self, text):
            self.footer = text

    class Client:
        def __init__(self, **kw):
            self.loop = None

    class HTTPException(Exception):
        status = 0

    d.ui = types.SimpleNamespace(View=View, Button=Button, button=lambda **kw: (lambda f: f))
    d.ButtonStyle = types.SimpleNamespace(success=1, secondary=2, primary=3, danger=4)
    d.Embed, d.Client, d.HTTPException = Embed, Client, HTTPException
    d.NotFound = type("NotFound", (HTTPException,), {})
    d.Intents = lambda **kw: kw
    d.Interaction = object
    d.Member = type("Member", (), {})
    d.AllowedMentions = types.SimpleNamespace(none=lambda: None)
    d.utils = types.SimpleNamespace(get=lambda *a, **kw: None)
    return d


sys.modules["discord"] = fake_discord()
spec = importlib.util.spec_from_file_location("bot", ROOT / "apps" / "games-bot" / "bot.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)
discord = sys.modules["discord"]

GAMES = {
    "minecraft": {"title": "Minecraft", "ns": "minecraft", "sts": "minecraft", "port": 25565, "metric": "game_players_online"},
    "terraria": {"title": "Terraria", "ns": "terraria", "sts": "terraria", "port": 7777, "metric": "game_players_online"},
    "zomboid": {"title": "Project Zomboid", "ns": "zomboid", "sts": "zomboid", "port": 16261, "metric": "game_players_online"},
}


class Member(discord.Member):
    def __init__(self, uid=1, name="Ana", roles=("Player",)):
        self.id, self.display_name = uid, name
        self.roles = [types.SimpleNamespace(name=r) for r in roles]


class Response:
    def __init__(self):
        self.sent, self.edited = [], []

    async def send_message(self, text, ephemeral=False, view=None):
        self.sent.append((text, view))

    async def edit_message(self, content=None, view=None):
        self.edited.append((content, view))


class Interaction:
    def __init__(self, member):
        self.user, self.response = member, Response()


class BotTest(unittest.TestCase):
    def setUp(self):
        bot.STATE = bot.State()
        bot.ICONS.update({"minecraft": "⛏️", "terraria": "🌳", "zomboid": "🧟"})   # vêm do catálogo no bot real
        self.b = bot.BOT
        self.b.info = {"games": GAMES, "active": "minecraft", "players": 0, "since": None, "ready": True,
                       "last_when": None, "last_who": None}
        self.switches = []

        async def switch():
            self.switches.append(dict(bot.STATE.switching))
        self.b.switch = switch
        self.audits = []

        async def audit(text):
            self.audits.append(text)
        self.b.audit = audit

    def tearDown(self):
        for attr in ("switch", "audit"):
            self.b.__dict__.pop(attr, None)

    def run_async(self, coro_fn):
        async def go():
            self.b.loop = asyncio.get_running_loop()
            result = await coro_fn()
            await asyncio.sleep(0)          # deixa rodar a task criada por start_switch
            return result
        return asyncio.run(go())

    def click(self, game, member=None):
        it = Interaction(member or Member())
        self.run_async(lambda: self.b.request(it, game))
        return it.response.sent[-1]

    def confirm(self, game, member=None):
        it = Interaction(member or Member())
        self.run_async(lambda: self.b.confirm(it, game))
        return it.response.edited[-1]

    # ------------------------------------------------------------ quem pode e quando

    def test_role_is_required(self):
        text, _ = self.click("terraria", Member(roles=("Visitante",)))
        self.assertIn("cargo **Player**", text)
        self.assertIsNone(bot.STATE.queue)

    def test_active_game_is_not_switched(self):
        text, _ = self.click("minecraft")
        self.assertIn("já está ligado", text)

    def test_free_server_asks_for_confirmation(self):
        text, view = self.click("terraria")
        self.assertIn("Trocar de **⛏️ Minecraft** para **🌳 Terraria**?", text)
        self.assertIsInstance(view, bot.Confirm)
        self.assertIsNone(bot.STATE.queue)

    def test_confirm_starts_the_switch(self):
        content, _ = self.confirm("terraria", Member(uid=7, name="Bia"))
        self.assertIn("Trocando para", content)
        self.assertEqual(len(self.switches), 1)
        s = self.switches[0]
        self.assertEqual((s["game"], s["prev"], s["who"], s["user_id"], s["step"]), ("terraria", "minecraft", "Bia", 7, 0))

    def test_switch_without_previous_game_starts_at_boot_step(self):
        self.b.info["active"] = None
        self.confirm("terraria")
        self.assertEqual(self.switches[0]["step"], 2)

    def test_players_online_send_the_request_to_the_queue(self):
        self.b.info["players"] = 2
        text, view = self.click("terraria", Member(uid=9, name="Caio"))
        self.assertIn("está na fila", text)
        self.assertIn("2 jogador(es) online", text)
        self.assertIsInstance(view, bot.QueueTicket)
        self.assertEqual((bot.STATE.queue["game"], bot.STATE.queue["user_id"]), ("terraria", 9))
        self.assertEqual(self.switches, [])

    def test_unknown_player_count_blocks(self):
        self.b.info["players"] = None
        self.assertIn("Não consegui confirmar", self.b.blocker())

    def test_cooldown_blocks(self):
        bot.STATE.cooldown_until = bot.now() + dt.timedelta(minutes=5)
        self.assertIn("Teve uma troca há pouco", self.b.blocker())
        bot.STATE.cooldown_until = bot.now() - dt.timedelta(seconds=1)
        self.assertIsNone(self.b.blocker())

    def test_queue_holds_one_game_at_a_time(self):
        bot.STATE.queue = {"game": "zomboid", "user_id": 2, "who": "Duda", "since": bot.now()}
        text, _ = self.click("terraria")
        self.assertIn("Já existe um pedido na fila (🧟 Project Zomboid, por Duda)", text)
        self.assertEqual(bot.STATE.queue["game"], "zomboid")

    def test_confirm_rechecks_and_queues_if_someone_joined(self):
        self.b.info["players"] = 1
        content, view = self.confirm("terraria")
        self.assertIn("foi para a fila", content)
        self.assertIsInstance(view, bot.QueueTicket)
        self.assertEqual(self.switches, [])

    def test_no_request_during_a_switch(self):
        bot.STATE.switching = {"game": "zomboid"}
        text, _ = self.click("terraria")
        self.assertIn("Já tem uma troca em andamento", text)

    def test_two_simultaneous_confirms_start_one_switch(self):
        async def both():
            first = self.b.start_switch("terraria", 1, "Ana")
            second = self.b.start_switch("zomboid", 2, "Caio")
            return first, second
        self.assertEqual(self.run_async(both), (True, False))
        self.assertEqual([s["game"] for s in self.switches], ["terraria"])

    # ------------------------------------------------------------ fila

    def test_queue_runs_when_the_server_empties(self):
        bot.STATE.queue = {"game": "terraria", "user_id": 3, "who": "Eva", "since": bot.now()}
        self.run_async(self.b.process_queue)
        self.assertEqual(self.switches[0]["who"], "Eva")
        self.assertIsNone(bot.STATE.queue)

    def test_queue_waits_while_blocked(self):
        self.b.info["players"] = 1
        bot.STATE.queue = {"game": "terraria", "user_id": 3, "who": "Eva", "since": bot.now()}
        self.run_async(self.b.process_queue)
        self.assertEqual(self.switches, [])
        self.assertIsNotNone(bot.STATE.queue)

    def test_queue_expires(self):
        bot.STATE.queue = {"game": "terraria", "user_id": 3, "who": "Eva",
                           "since": bot.now() - bot.QUEUE_TTL - dt.timedelta(minutes=1)}
        self.run_async(self.b.process_queue)
        self.assertIsNone(bot.STATE.queue)
        self.assertEqual(self.switches, [])
        self.assertIn("expirou na fila", self.audits[0])

    def test_only_the_requester_cancels_the_queue(self):
        bot.STATE.queue = {"game": "terraria", "user_id": 3, "who": "Eva", "since": bot.now()}
        other = Interaction(Member(uid=4))
        self.run_async(lambda: bot.QueueTicket.cancel(None, other, None))
        self.assertIn("não está mais na fila", other.response.edited[-1][0])
        self.assertIsNotNone(bot.STATE.queue)
        owner = Interaction(Member(uid=3))
        self.run_async(lambda: bot.QueueTicket.cancel(None, owner, None))
        self.assertIn("Pedido cancelado", owner.response.edited[-1][0])
        self.assertIsNone(bot.STATE.queue)

    # ------------------------------------------------------------ etapas e painel

    def test_switch_output_advances_the_steps(self):
        bot.STATE.switching = {"step": 0}
        self.assertFalse(bot.GamesBot.advance("Jogo ativo: minecraft -> terraria"))
        self.assertTrue(bot.GamesBot.advance("  save enviado ao console de minecraft"))
        self.assertEqual(bot.STATE.switching["step"], 1)
        self.assertTrue(bot.GamesBot.advance("Subindo terraria..."))
        self.assertFalse(bot.GamesBot.advance("Parando minecraft..."))   # nunca volta etapa
        self.assertTrue(bot.GamesBot.advance("OK: terraria é o jogo ativo."))
        self.assertEqual(bot.STATE.switching["step"], 3)

    def test_panel_shows_players_lock_and_queue(self):
        self.b.info["players"] = 2
        bot.STATE.queue = {"game": "terraria", "user_id": 3, "who": "Eva", "since": bot.now()}
        e = bot.build_embed(self.b.info)
        self.assertIn("**Ativo:** ⛏️ Minecraft", e.description)
        self.assertIn("**Jogando:** 2  🔒 troca bloqueada", e.description)
        self.assertIn("**Fila:** 🌳 Terraria, pedido por Eva (expira em 30 min)", e.description)
        self.assertEqual(e.color, bot.GREEN)

    def test_panel_disables_buttons_during_a_switch(self):
        bot.STATE.switching = {"game": "terraria", "prev": "minecraft", "who": "Ana", "user_id": 1,
                               "since": bot.now(), "step": 1}
        panel = bot.Panel(self.b.info)
        games = [b for b in panel.children if b.custom_id.startswith("switch:")]
        self.assertTrue(all(b.disabled for b in games))
        e = bot.build_embed(self.b.info)
        self.assertIn("✅ Mundo do Minecraft salvo", e.description)
        self.assertIn("⏳ Minecraft desligado...", e.description)
        self.assertEqual(e.color, bot.YELLOW)


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False, verbosity=1).result.wasSuccessful() else 1)
