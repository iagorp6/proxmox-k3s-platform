#!/usr/bin/env python3
"""games-bot: painel do #games para trocar o jogo ativo sem depender do mantenedor (ADR 0006).

Mantém uma única mensagem no canal #games com o jogo ativo, os jogadores online, a fila e um botão
por jogo. Quem tem o cargo Players clica; o bot confere se o jogo atual está vazio (Prometheus) e
roda o mesmo fluxo do `make switch-game` (scripts/switch-game.py) a partir de um clone do repo,
commitando como games-bot com o nome de quem pediu. Cada troca vira uma linha no #alerts.

Configuração por variáveis de ambiente (todas com padrão, exceto o token):
  DISCORD_TOKEN_FILE   arquivo com o token do bot              (/etc/games-bot/discord-token)
  DEPLOY_KEY_FILE      chave SSH com escrita no repositório     (/etc/games-bot/deploy-key)
  REPO_URL             repositório da plataforma (SSH)
  WORKDIR              onde o clone vive                        (/work)
  PROMETHEUS_URL       API do Prometheus
  GAMES_CHANNEL, AUDIT_CHANNEL, PLAYERS_ROLE   nomes no Discord (games, alerts, Players)
  GAME_HOST            endereço mostrado no painel (opcional, fora do Git)
"""
import asyncio
import datetime as dt
import json
import logging
import os
import pathlib
import re
import sys
import urllib.parse
import urllib.request

import discord
import yaml

log = logging.getLogger("games-bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)

ENV = os.environ.get
TOKEN_FILE = pathlib.Path(ENV("DISCORD_TOKEN_FILE", "/etc/games-bot/discord-token"))
DEPLOY_KEY_FILE = pathlib.Path(ENV("DEPLOY_KEY_FILE", "/etc/games-bot/deploy-key"))
REPO_URL = ENV("REPO_URL", "git@github.com:iagorp6/proxmox-k3s-platform.git")
WORKDIR = pathlib.Path(ENV("WORKDIR", "/work"))
REPO = WORKDIR / "repo"
PROMETHEUS = ENV("PROMETHEUS_URL", "http://kps-kube-prometheus-stack-prometheus.monitoring.svc:9090")
GAMES_CHANNEL = ENV("GAMES_CHANNEL", "games")
AUDIT_CHANNEL = ENV("AUDIT_CHANNEL", "alerts")
PLAYERS_ROLE = ENV("PLAYERS_ROLE", "Players")
GAME_HOST = ENV("GAME_HOST", "")
REFRESH_SECONDS = int(ENV("REFRESH_SECONDS", "60"))
COOLDOWN = dt.timedelta(minutes=int(ENV("COOLDOWN_MINUTES", "10")))
QUEUE_TTL = dt.timedelta(minutes=int(ENV("QUEUE_MINUTES", "30")))
SWITCH_TIMEOUT = int(ENV("SWITCH_TIMEOUT_SECONDS", "1800"))
PANEL_TITLE = "🎮 Servidor de Jogos"
# Chave pública ed25519 do github.com (docs.github.com, "GitHub's SSH key fingerprints")
GITHUB_HOST_KEY = "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"

GREEN, YELLOW, RED, GREY = 0x2ECC71, 0xF1C40F, 0xE74C3C, 0x95A5A6


def now():
    return dt.datetime.now(dt.timezone.utc)


def ago(t):
    """'há 5 min', 'há 3h12', 'há 2 dias'."""
    s = int((now() - t).total_seconds())
    if s < 3600:
        return f"há {max(1, s // 60)} min"
    if s < 48 * 3600:
        return f"há {s // 3600}h{(s % 3600) // 60:02d}"
    return f"há {s // 86400} dias"


# ---------------------------------------------------------------- git e catálogo (via clone do repo)

async def sh(*cmd, timeout=120, cwd=None):
    """Roda um comando e devolve (código, saída combinada)."""
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT, env=git_env())
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, f"timeout após {timeout}s: {' '.join(cmd[:3])}"
    return proc.returncode, out.decode(errors="replace")


def git_env():
    ssh_dir = WORKDIR / ".ssh"
    env = dict(os.environ)
    env["GIT_SSH_COMMAND"] = (f"ssh -i {ssh_dir / 'id_ed25519'} -o IdentitiesOnly=yes "
                              f"-o UserKnownHostsFile={ssh_dir / 'known_hosts'} -o StrictHostKeyChecking=yes")
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "games-bot"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "games-bot@users.noreply.github.com"
    return env


def prepare_ssh():
    """O Secret é montado como root:fsGroup; o ssh exige a chave só legível pelo dono."""
    ssh_dir = WORKDIR / ".ssh"
    ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = ssh_dir / "id_ed25519"
    key.write_bytes(DEPLOY_KEY_FILE.read_bytes().rstrip(b"\n") + b"\n")
    key.chmod(0o600)
    (ssh_dir / "known_hosts").write_text(GITHUB_HOST_KEY + "\n", encoding="utf-8")


async def sync_repo():
    """Clone na primeira vez; depois alinha com origin/main (o clone nunca guarda mudança local)."""
    if not (REPO / ".git").exists():
        code, out = await sh("git", "clone", "--depth", "20", "--branch", "main", REPO_URL, str(REPO), timeout=300)
    else:
        code, out = await sh("git", "fetch", "--depth", "20", "origin", "main", cwd=REPO)
        if code == 0:
            code, out = await sh("git", "reset", "-q", "--hard", "origin/main", cwd=REPO)
    if code != 0:
        raise RuntimeError(f"git falhou: {out.strip()[-300:]}")


def catalog():
    """Jogos do catálogo (k8s/games/*/game.yaml e legados), lidos do clone a cada atualização."""
    games = {}
    for f in sorted([*REPO.glob("k8s/*/game.yaml"), *REPO.glob("k8s/games/*/game.yaml")]):
        g = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        if g.get("status") in ("active", "legacy") and g.get("namespace"):
            sw = g.get("switch") or {}
            games[g["name"]] = {
                "title": g["displayName"], "ns": g["namespace"], "sts": g["name"],
                "port": g["ports"][0]["port"], "metric": sw.get("playersMetric", "game_players_online"),
            }
    return games


def active_game():
    f = REPO / "platform" / "active-game.yaml"
    m = re.search(r"^active:\s*(\S+)", f.read_text(encoding="utf-8"), re.M) if f.exists() else None
    return m.group(1) if m else None


def last_switch():
    """(quando, quem pediu) da última troca, lidos do histórico do Git."""
    try:
        import subprocess
        out = subprocess.run(["git", "log", "-1", "--format=%cI%x09%s", "--", "platform/active-game.yaml"],
                             cwd=REPO, capture_output=True, text=True, timeout=10).stdout.strip()
        when, _, subject = out.partition("\t")
        who = re.search(r"pedido por (.+) via Discord", subject)
        return dt.datetime.fromisoformat(when), (who.group(1) if who else "terminal")
    except Exception:  # noqa: BLE001 - informação do painel, não pode derrubar o bot
        return None, None


# ---------------------------------------------------------------- Prometheus

def prom(query):
    """Primeiro valor de uma consulta instantânea, ou None (sem dado ou Prometheus fora)."""
    url = f"{PROMETHEUS}/api/v1/query?" + urllib.parse.urlencode({"query": query})
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.load(r)["data"]["result"]
        return float(data[0]["value"][1]) if data else None
    except Exception as exc:  # noqa: BLE001
        log.warning("prometheus: %s", exc)
        return None


def players(g):
    v = prom(f'max({g["metric"]}{{namespace="{g["ns"]}"}})')
    return None if v is None else int(v)


def started_at(g):
    v = prom(f'max(kube_pod_start_time{{namespace="{g["ns"]}",pod="{g["sts"]}-0"}})')
    return dt.datetime.fromtimestamp(v, dt.timezone.utc) if v else None


def is_ready(g):
    v = prom(f'max(kube_pod_status_ready{{namespace="{g["ns"]}",pod="{g["sts"]}-0",condition="true"}})')
    return bool(v)


# ---------------------------------------------------------------- estado do painel

class State:
    def __init__(self):
        self.lock = asyncio.Lock()          # uma troca por vez
        self.switching = None               # {"game", "who", "since"}
        self.queue = None                   # {"game", "user_id", "who", "since"}
        self.cooldown_until = now()
        self.error = None                   # última falha, mostrada no painel até a próxima troca


STATE = State()


def snapshot():
    games = catalog()
    active = active_game()
    g = games.get(active)
    info = {"games": games, "active": active, "players": None, "since": None, "ready": False}
    if g:
        info["players"], info["since"], info["ready"] = players(g), started_at(g), is_ready(g)
    info["last_when"], info["last_who"] = last_switch()
    return info


def build_embed(info):
    games, active = info["games"], info["active"]
    g = games.get(active)
    lines = []
    if STATE.switching:
        s = STATE.switching
        lines.append(f"🔄 **Trocando para {games[s['game']]['title']}** (pedido por {s['who']}, {ago(s['since'])})")
        color = YELLOW
    elif g and info["ready"]:
        up = f" · online {ago(info['since'])}" if info["since"] else ""
        lines.append(f"🟢 **Ativo: {g['title']}**{up}")
        color = GREEN
    elif g:
        lines.append(f"🟡 **{g['title']}** iniciando ou fora do ar")
        color = YELLOW
    else:
        lines.append("⚪ Nenhum jogo ativo")
        color = GREY
    if g and GAME_HOST:
        lines.append(f"🔌 Endereço: `{GAME_HOST}:{g['port']}`")
    n = info["players"]
    if g and not STATE.switching:
        lines.append(f"👥 Jogando: {n}" if n is not None else "👥 Jogando: sem dados")
        if n:
            lines.append("🔒 Troca bloqueada: tem alguém jogando")
        elif now() < STATE.cooldown_until:
            lines.append(f"⏱️ Próxima troca liberada <t:{int(STATE.cooldown_until.timestamp())}:R>")
    if STATE.queue:
        q = STATE.queue
        lines.append(f"⏳ Na fila: {games.get(q['game'], {}).get('title', q['game'])} (pedido por {q['who']})")
    if info["last_when"]:
        lines.append(f"🕓 Última troca: {ago(info['last_when'])}, por {info['last_who']}")
    if STATE.error:
        lines.append(f"⚠️ {STATE.error}")
        color = RED
    e = discord.Embed(title=PANEL_TITLE, description="\n".join(lines), color=color)
    e.set_footer(text=f"Clique no jogo para pedir a troca (cargo {PLAYERS_ROLE}). Atualizado")
    e.timestamp = now()
    return e


class Panel(discord.ui.View):
    """Um botão por jogo do catálogo; custom_id fixo para sobreviver a restarts do bot."""

    def __init__(self, info):
        super().__init__(timeout=None)
        for name, g in info["games"].items():
            on = name == info["active"]
            b = discord.ui.Button(label=g["title"] + (" ✓" if on else ""), custom_id=f"switch:{name}",
                                  style=discord.ButtonStyle.success if on else discord.ButtonStyle.secondary,
                                  disabled=on or STATE.switching is not None)
            b.callback = self.make_callback(name)
            self.add_item(b)

    @staticmethod
    def make_callback(name):
        async def cb(interaction: discord.Interaction):
            await BOT.request(interaction, name)
        return cb


# ---------------------------------------------------------------- bot

class GamesBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents(guilds=True))
        self.panel_msg = None
        self.games_ch = None
        self.audit_ch = None
        self.info = None

    async def setup_hook(self):
        prepare_ssh()
        await sync_repo()
        self.info = await asyncio.to_thread(snapshot)
        self.add_view(Panel(self.info))     # cliques em painéis antigos continuam funcionando
        self.loop.create_task(self.refresher())

    async def on_ready(self):
        guild = self.guilds[0] if len(self.guilds) == 1 else discord.utils.get(self.guilds, name=ENV("GUILD_NAME"))
        if guild is None:
            log.error("o bot precisa estar em exatamente um servidor (ou defina GUILD_NAME)")
            return
        self.games_ch = discord.utils.get(guild.text_channels, name=GAMES_CHANNEL)
        self.audit_ch = discord.utils.get(guild.text_channels, name=AUDIT_CHANNEL)
        if self.games_ch is None:
            log.error("canal #%s não encontrado", GAMES_CHANNEL)
            return
        async for m in self.games_ch.history(limit=50):
            if m.author == self.user and m.embeds and m.embeds[0].title == PANEL_TITLE:
                if self.panel_msg is None:
                    self.panel_msg = m
                else:
                    await m.delete()          # só um painel no canal
        await self.render()
        log.info("pronto: painel em #%s, auditoria em #%s", GAMES_CHANNEL, AUDIT_CHANNEL)

    async def render(self):
        if self.games_ch is None or self.info is None:
            return
        embed, view = build_embed(self.info), Panel(self.info)
        try:
            if self.panel_msg is None:
                self.panel_msg = await self.games_ch.send(embed=embed, view=view)
            else:
                await self.panel_msg.edit(embed=embed, view=view)
        except discord.NotFound:
            self.panel_msg = await self.games_ch.send(embed=embed, view=view)

    async def refresh(self):
        if not STATE.lock.locked():
            try:
                await sync_repo()
            except RuntimeError as exc:
                log.warning("%s", exc)
        self.info = await asyncio.to_thread(snapshot)
        await self.render()

    async def refresher(self):
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self.refresh()
                await self.process_queue()
            except Exception:  # noqa: BLE001 - o loop do painel não pode morrer
                log.exception("falha ao atualizar o painel")
            await asyncio.sleep(REFRESH_SECONDS)

    async def audit(self, text):
        if self.audit_ch:
            try:
                await self.audit_ch.send(text, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as exc:
                log.warning("auditoria: %s", exc)

    # ------------------------------------------------------------ pedidos

    async def request(self, interaction: discord.Interaction, game):
        reply = lambda text: interaction.response.send_message(text, ephemeral=True)  # noqa: E731
        member = interaction.user
        if not isinstance(member, discord.Member) or not any(r.name == PLAYERS_ROLE for r in member.roles):
            return await reply(f"Você precisa do cargo **{PLAYERS_ROLE}** para trocar o jogo.")
        info = self.info
        title = info["games"].get(game, {}).get("title", game)
        if STATE.switching:
            return await reply("Já tem uma troca em andamento. Acompanhe pelo painel.")
        if game == info["active"]:
            return await reply(f"**{title}** já está ligado, é só entrar.")
        if STATE.queue and STATE.queue["game"] != game:
            q = STATE.queue
            return await reply(f"Já existe um pedido na fila ({info['games'][q['game']]['title']}, por {q['who']}). "
                               "Ele expira em até 30 min.")
        who = member.display_name
        n = info["players"]
        cur = info["games"].get(info["active"], {}).get("title", "o jogo atual")
        if n is None and info["active"]:
            STATE.queue = {"game": game, "user_id": member.id, "who": who, "since": now()}
            await reply(f"Não consegui confirmar quantas pessoas estão em **{cur}**. "
                        f"Seu pedido para **{title}** ficou na fila; troco assim que der para confirmar que está vazio.")
        elif n:
            STATE.queue = {"game": game, "user_id": member.id, "who": who, "since": now()}
            await reply(f"Ocupado: **{cur}** com {n} jogador(es). Seu pedido para **{title}** ficou na fila; "
                        "quando o servidor esvaziar, eu troco e te aviso.")
        elif now() < STATE.cooldown_until:
            STATE.queue = {"game": game, "user_id": member.id, "who": who, "since": now()}
            await reply(f"Teve uma troca há pouco. Seu pedido para **{title}** ficou na fila e sai "
                        f"<t:{int(STATE.cooldown_until.timestamp())}:R>.")
        else:
            await reply(f"Trocando para **{title}**. Acompanhe pelo painel; te aviso aqui quando estiver pronto.")
            self.loop.create_task(self.switch(game, member.id, who))
        await self.render()

    async def process_queue(self):
        q = STATE.queue
        if not q or STATE.switching:
            return
        if now() - q["since"] > QUEUE_TTL:
            STATE.queue = None
            await self.audit(f"⌛ Pedido de troca para {q['game']} (por {q['who']}) expirou na fila.")
            return await self.render()
        if self.info["players"] == 0 and now() >= STATE.cooldown_until:
            STATE.queue = None
            self.loop.create_task(self.switch(q["game"], q["user_id"], q["who"]))

    async def switch(self, game, user_id, who):
        async with STATE.lock:
            info = self.info
            prev = info["active"]
            titles = {k: v["title"] for k, v in info["games"].items()}
            STATE.switching, STATE.error = {"game": game, "who": who, "since": now()}, None
            await self.render()
            await self.audit(f"🔄 Troca {titles.get(prev, prev)} → {titles[game]} pedida por {who} no #{GAMES_CHANNEL}.")
            started = now()
            code, out = 1, ""
            try:
                await sync_repo()
                code, out = await sh(sys.executable, "scripts/switch-game.py", game, "--no-notify",
                                     "--requester", who, "--require-player-count",
                                     cwd=REPO, timeout=SWITCH_TIMEOUT)
            except Exception as exc:  # noqa: BLE001
                out = str(exc)
            minutes = max(1, round((now() - started).total_seconds() / 60))
            STATE.switching = None
            STATE.cooldown_until = now() + COOLDOWN
            if code == 0:
                await self.audit(f"✅ {titles[game]} no ar ({minutes} min). Pedido por {who}.")
                await self.notify_ready(game, user_id, titles[game])
            else:
                tail = "\n".join(l for l in out.strip().splitlines()[-12:])
                reason = next((l for l in reversed(out.splitlines()) if l.startswith("ERRO")), "falha na troca")
                STATE.error = f"A última troca falhou: {reason[:150]}"
                await self.audit(f"❌ Troca para {titles[game]} falhou ({minutes} min), pedida por {who}.\n```\n{tail[-1500:]}\n```")
        await self.refresh()   # fora do lock: o refresh só sincroniza o clone quando não há troca

    async def notify_ready(self, game, user_id, title):
        """Menção curta no #games, apagada depois de 5 min para o canal ficar só com o painel."""
        if self.games_ch:
            g = self.info["games"].get(game, {})
            where = f" Endereço: `{GAME_HOST}:{g.get('port')}`." if GAME_HOST and g else ""
            await self.games_ch.send(f"<@{user_id}> **{title}** está pronto.{where}", delete_after=300,
                                     allowed_mentions=discord.AllowedMentions(users=True))


BOT = GamesBot()

if __name__ == "__main__":
    token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    BOT.run(token, log_handler=None)
