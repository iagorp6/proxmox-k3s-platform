#!/usr/bin/env python3
"""games-bot: painel do #games para trocar o jogo ativo sem depender do mantenedor (ADR 0006).

Mantém uma única mensagem no canal #games com o jogo ativo, os jogadores online, a fila e um botão
por jogo, mais um "Como funciona". Quem tem o cargo de jogador clica e confirma; com o jogo atual
vazio (Prometheus), o bot roda o mesmo fluxo do `make switch-game` (scripts/switch-game.py) a partir
de um clone do repo, commitando como games-bot com o nome de quem pediu, e mostra as etapas no
painel. Ocupado, o pedido entra na fila e sai sozinho quando o servidor esvaziar. Cada troca vira
uma linha no #alerts; quem pediu recebe o aviso de pronto por DM. Respostas a cliques são privadas.

Configuração por variáveis de ambiente (todas com padrão, exceto o token):
  DISCORD_TOKEN_FILE   arquivo com o token do bot              (/etc/games-bot/discord-token)
  DEPLOY_KEY_FILE      chave SSH com escrita no repositório     (/etc/games-bot/deploy-key)
  REPO_URL             repositório da plataforma (SSH)
  WORKDIR              onde o clone vive                        (/work)
  PROMETHEUS_URL       API do Prometheus
  GAMES_CHANNEL, AUDIT_CHANNEL, PLAYERS_ROLE   nomes no Discord (games, alerts, Player)
  GAME_HOST            endereço mostrado no painel (opcional, fora do Git)
  ADMIN_NAME           a quem recorrer quando uma troca falha (padrão: "o admin")
  STATE_CONFIGMAP      ConfigMap onde fila e cooldown sobrevivem a um restart (games-bot-state)
  POD_NAMESPACE        namespace do bot, onde fica esse ConfigMap (platform)
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
PLAYERS_ROLE = ENV("PLAYERS_ROLE", "Player")
ADMIN_NAME = ENV("ADMIN_NAME", "o admin")
GAME_HOST = ENV("GAME_HOST", "")
REFRESH_SECONDS = int(ENV("REFRESH_SECONDS", "60"))
COOLDOWN = dt.timedelta(minutes=int(ENV("COOLDOWN_MINUTES", "10")))
QUEUE_TTL = dt.timedelta(minutes=int(ENV("QUEUE_MINUTES", "30")))
SWITCH_TIMEOUT = int(ENV("SWITCH_TIMEOUT_SECONDS", "1800"))
STATE_CONFIGMAP = ENV("STATE_CONFIGMAP", "games-bot-state")
NAMESPACE = ENV("POD_NAMESPACE", "platform")
PANEL_TITLE = "🎮 Servidor de Jogos"
# Chave pública ed25519 do github.com (docs.github.com, "GitHub's SSH key fingerprints")
GITHUB_HOST_KEY = "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"

GREEN, YELLOW, RED, GREY = 0x2ECC71, 0xF1C40F, 0xE74C3C, 0x95A5A6
ICONS = {}   # preenchido pelo catálogo (campo icon do game.yaml); jogo sem ícone usa 🎮


def now():
    return dt.datetime.now(dt.timezone.utc)


def icon(name):
    return ICONS.get(name, "🎮")


def mins(td):
    return max(1, int(td.total_seconds() + 59) // 60)


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
            if g.get("icon"):
                ICONS[g["name"]] = g["icon"]
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
        return dt.datetime.fromisoformat(when), (who.group(1) if who else "admin (terminal)")
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
        self.switching = None               # {"game", "prev", "who", "user_id", "since", "step"}
        self.queue = None                   # {"game", "user_id", "who", "since"}
        self.cooldown_until = now()
        self.error = None                   # título do jogo da última troca que falhou


STATE = State()


def dump_state():
    """Fila e cooldown em JSON: o que precisa sobreviver a um restart do pod (troca em andamento não volta)."""
    q = STATE.queue
    return json.dumps({"queue": {**q, "since": q["since"].isoformat()} if q else None,
                       "cooldown_until": STATE.cooldown_until.isoformat()}, sort_keys=True)


def load_state(text):
    data = json.loads(text)
    q = data.get("queue")
    STATE.queue = {**q, "since": dt.datetime.fromisoformat(q["since"])} if q else None
    STATE.cooldown_until = dt.datetime.fromisoformat(data["cooldown_until"])


def snapshot():
    games = catalog()
    active = active_game()
    g = games.get(active)
    info = {"games": games, "active": active, "players": None, "since": None, "ready": False}
    if g:
        info["players"], info["since"], info["ready"] = players(g), started_at(g), is_ready(g)
    info["last_when"], info["last_who"] = last_switch()
    return info


def label(info, name):
    g = info["games"].get(name)
    return f"{icon(name)} {g['title']}" if g else name


def switch_lines(info):
    """Etapas da troca em andamento: ✅ feitas, ⏳ a atual, ⬜ as próximas."""
    s = STATE.switching
    games = info["games"]
    prev, target = s["prev"], s["game"]
    t_title = games.get(target, {}).get("title", target)
    steps = []
    if prev in games:
        p_title = games[prev]["title"]
        steps += [f"Mundo do {p_title} salvo", f"{p_title} desligado"]
    else:
        steps += ["", ""]
    steps += [f"Ligando {t_title}", "Pronto para entrar"]
    lines = [f"🔄 **TROCANDO PARA {label(info, target).upper()}** · pedido por {s['who']}", ""]
    for i, text in enumerate(steps):
        if not text:
            continue
        if i < s["step"]:
            lines.append(f"✅ {text}")
        elif i == s["step"]:
            extra = f" ({ago(s['since']).removeprefix('há ')})" if i == 2 else ""
            lines.append(f"⏳ {text}...{extra}")
        else:
            lines.append(f"⬜ {text}")
    return lines


def build_embed(info):
    games, active = info["games"], info["active"]
    g = games.get(active)
    lines = []
    if STATE.switching:
        lines += switch_lines(info)
        color = YELLOW
    else:
        if STATE.error:
            lines += [f"🔴 A troca para {STATE.error} não terminou. O admin já foi avisado.",
                      f"Tente de novo em alguns minutos ou chame {ADMIN_NAME}.", ""]
        if g and info["ready"]:
            up = f" · ligado {ago(info['since'])}" if info["since"] else ""
            lines.append(f"**Ativo:** {label(info, active)}{up}")
            color = GREEN
        elif g:
            lines.append(f"**Ativo:** {label(info, active)} · 🟡 iniciando ou fora do ar")
            color = YELLOW
        else:
            lines.append("**Ativo:** nenhum jogo ligado")
            color = GREY
        if g and GAME_HOST:
            lines.append(f"**Endereço:** `{GAME_HOST}:{g['port']}`")
        if g:
            n = info["players"]
            if n is None:
                lines.append("**Jogando:** sem dados no momento")
            elif n:
                lines.append(f"**Jogando:** {n}  🔒 troca bloqueada enquanto houver alguém jogando")
            else:
                lines.append("**Jogando:** 0")
        if STATE.queue:
            q = STATE.queue
            left = mins(q["since"] + QUEUE_TTL - now())
            lines.append(f"**Fila:** {label(info, q['game'])}, pedido por {q['who']} (expira em {left} min)")
        else:
            lines.append("**Fila:** vazia")
        if now() < STATE.cooldown_until:
            lines.append(f"**Próxima troca liberada:** <t:{int(STATE.cooldown_until.timestamp())}:R>")
        if info["last_when"]:
            lines.append(f"**Última troca:** {ago(info['last_when'])}, por {info['last_who']}")
        if STATE.error:
            color = RED
    e = discord.Embed(title=PANEL_TITLE, description="\n".join(lines), color=color)
    e.set_footer(text="Clique no jogo que você quer jogar. Atualizado")
    e.timestamp = now()
    return e


def help_text():
    return (
        "❓ **Como funciona**\n"
        "• Clique no jogo que você quer jogar e confirme.\n"
        "• O jogo atual é salvo antes de desligar; a troca leva alguns minutos e as etapas aparecem no painel.\n"
        f"• Se alguém estiver jogando, seu pedido entra na fila e a troca acontece sozinha quando o servidor "
        f"esvaziar. O pedido vale por {mins(QUEUE_TTL)} min.\n"
        "• Quando o jogo estiver pronto, aviso você por DM.\n"
        f"• Só quem tem o cargo **{PLAYERS_ROLE}** pode trocar. Entre uma troca e outra há um intervalo de "
        f"{mins(COOLDOWN)} min."
    )


class Panel(discord.ui.View):
    """Um botão por jogo do catálogo e o ❓; custom_id fixo para sobreviver a restarts do bot."""

    def __init__(self, info):
        super().__init__(timeout=None)
        busy = STATE.switching is not None
        for name, g in info["games"].items():
            on = name == info["active"]
            b = discord.ui.Button(label=g["title"] + (" ✓" if on else ""), emoji=icon(name), custom_id=f"switch:{name}",
                                  style=discord.ButtonStyle.success if on else discord.ButtonStyle.secondary,
                                  disabled=on or busy)
            b.callback = self.on_game(name)
            self.add_item(b)
        h = discord.ui.Button(label="Como funciona", emoji="❓", custom_id="help", style=discord.ButtonStyle.primary)
        h.callback = self.on_help
        self.add_item(h)

    @staticmethod
    def on_game(name):
        async def cb(interaction: discord.Interaction):
            await BOT.request(interaction, name)
        return cb

    @staticmethod
    async def on_help(interaction: discord.Interaction):
        await interaction.response.send_message(help_text(), ephemeral=True)


class Confirm(discord.ui.View):
    """Confirmação privada antes de derrubar o jogo atual."""

    def __init__(self, game):
        super().__init__(timeout=120)
        self.game = game

    @discord.ui.button(label="Trocar agora", emoji="✅", style=discord.ButtonStyle.success)
    async def go(self, interaction: discord.Interaction, _button):
        await BOT.confirm(interaction, self.game)

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button):
        await interaction.response.edit_message(content="Troca cancelada. Nada mudou.", view=None)


class QueueTicket(discord.ui.View):
    """Resposta privada de quem entrou na fila, com a opção de desistir."""

    def __init__(self):
        super().__init__(timeout=QUEUE_TTL.total_seconds())

    @discord.ui.button(label="Cancelar meu pedido", emoji="✖️", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, _button):
        q = STATE.queue
        if q and q["user_id"] == interaction.user.id:
            STATE.queue = None
            await interaction.response.edit_message(content="Pedido cancelado e retirado da fila.", view=None)
            await BOT.render()
        else:
            await interaction.response.edit_message(content="Esse pedido não está mais na fila.", view=None)


# ---------------------------------------------------------------- bot

class GamesBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents(guilds=True))
        self.panel_msg = None
        self.games_ch = None
        self.audit_ch = None
        self.info = None
        self.saved_state = None
        self.restored = False

    async def setup_hook(self):
        prepare_ssh()
        await sync_repo()
        await self.restore()
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

    async def restore(self):
        """Fila e cooldown do pod anterior. Pedido vencido sai na próxima volta do process_queue, com aviso."""
        code, out = await sh("kubectl", "-n", NAMESPACE, "get", "configmap", STATE_CONFIGMAP,
                             "-o", "jsonpath={.data.state}", timeout=20)
        if code != 0 and "NotFound" not in out:
            # API fora agora: tenta de novo no próximo persist, sem sobrescrever o que estiver salvo
            log.warning("não consegui ler o estado salvo: %s", out.strip()[-200:])
            return
        self.restored = True
        if code != 0 or not out.strip():
            log.info("sem estado salvo")
            return
        try:
            load_state(out)
            self.saved_state = dump_state()
            log.info("estado restaurado: fila %s", STATE.queue and STATE.queue["game"])
        except (ValueError, KeyError, TypeError) as exc:
            log.warning("estado salvo inválido, ignorado: %s", exc)

    async def persist(self):
        """Grava fila e cooldown no ConfigMap quando mudam. Falha só vira log: o painel segue funcionando."""
        if not self.restored:
            await self.restore()
            if not self.restored:
                return
        text = dump_state()
        if text == self.saved_state:
            return
        code, out = await sh("kubectl", "-n", NAMESPACE, "patch", "configmap", STATE_CONFIGMAP, "--type", "merge",
                             "-p", json.dumps({"data": {"state": text}}), timeout=20)
        if code != 0 and "NotFound" in out:
            code, out = await sh("kubectl", "-n", NAMESPACE, "create", "configmap", STATE_CONFIGMAP,
                                 f"--from-literal=state={text}", timeout=20)
        if code == 0:
            self.saved_state = text
        else:
            log.warning("estado não salvo: %s", out.strip()[-300:])

    async def render(self):
        await self.persist()   # todo caminho que muda fila ou cooldown termina num render
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

    def blocker(self):
        """Por que não dá para trocar agora (texto para a resposta privada), ou None se está livre."""
        info = self.info
        cur = label(info, info["active"]) if info["active"] in info["games"] else "o jogo atual"
        n = info["players"]
        if n is None and info["active"] in info["games"]:
            return f"Não consegui confirmar quantas pessoas estão em **{cur}**."
        if n:
            return f"**{cur}** tem {n} jogador(es) online."
        if now() < STATE.cooldown_until:
            return f"Teve uma troca há pouco; a próxima fica liberada <t:{int(STATE.cooldown_until.timestamp())}:R>."
        return None

    async def request(self, interaction: discord.Interaction, game):
        member = interaction.user
        reply = lambda text, view=None: interaction.response.send_message(  # noqa: E731
            text, ephemeral=True, **({"view": view} if view else {}))
        if not isinstance(member, discord.Member) or not any(r.name == PLAYERS_ROLE for r in member.roles):
            return await reply(f"Você precisa do cargo **{PLAYERS_ROLE}** para trocar o jogo.")
        info = self.info
        target = label(info, game)
        if STATE.switching:
            return await reply("Já tem uma troca em andamento. Acompanhe as etapas no painel.")
        if game == info["active"]:
            return await reply(f"**{target}** já está ligado, é só entrar.")
        q = STATE.queue
        if q and q["game"] != game:
            return await reply(f"Já existe um pedido na fila ({label(info, q['game'])}, por {q['who']}). "
                               f"Ele expira em {mins(q['since'] + QUEUE_TTL - now())} min.")
        why = self.blocker()
        if why:
            STATE.queue = {"game": game, "user_id": member.id, "who": member.display_name, "since": now()}
            await reply(f"⏳ Seu pedido para **{target}** está na fila. {why}\n"
                        f"Troco assim que der e te aviso por DM. O pedido expira em {mins(QUEUE_TTL)} min.",
                        QueueTicket())
            return await self.render()
        cur = label(info, info["active"]) if info["active"] in info["games"] else None
        text = (f"Trocar de **{cur}** para **{target}**? O {info['games'][info['active']]['title']} será salvo e "
                "desligado (ninguém está jogando)." if cur else f"Ligar **{target}**?")
        await reply(text, Confirm(game))

    async def confirm(self, interaction: discord.Interaction, game):
        """Segundo clique: confere de novo (o estado pode ter mudado) e começa a troca ou vai para a fila."""
        member = interaction.user
        target = label(self.info, game)
        if STATE.switching:
            return await interaction.response.edit_message(content="Outra troca começou antes. Acompanhe pelo painel.", view=None)
        if game == self.info["active"]:
            return await interaction.response.edit_message(content=f"**{target}** já está ligado.", view=None)
        why = self.blocker()
        if why:
            STATE.queue = {"game": game, "user_id": member.id, "who": member.display_name, "since": now()}
            await interaction.response.edit_message(
                content=f"⏳ {why} Seu pedido para **{target}** foi para a fila; te aviso por DM.", view=QueueTicket())
            return await self.render()
        await interaction.response.edit_message(
            content=f"🔄 Trocando para **{target}**. Acompanhe as etapas no painel; te aviso por DM quando estiver pronto.",
            view=None)
        self.start_switch(game, member.id, member.display_name)

    def start_switch(self, game, user_id, who):
        """Marca a troca antes de qualquer await: dois cliques simultâneos não disparam duas trocas."""
        if STATE.switching:
            return False
        prev = self.info["active"]
        STATE.switching = {"game": game, "prev": prev, "who": who, "user_id": user_id, "since": now(),
                           "step": 0 if prev in self.info["games"] else 2}
        if STATE.queue and STATE.queue["game"] == game:
            STATE.queue = None
        self.loop.create_task(self.switch())
        return True

    async def process_queue(self):
        q = STATE.queue
        if not q or STATE.switching:
            return
        if now() - q["since"] > QUEUE_TTL:
            STATE.queue = None
            await self.audit(f"⌛ Pedido de troca para {q['game']} (por {q['who']}) expirou na fila.")
            return await self.render()
        if self.blocker() is None:
            self.start_switch(q["game"], q["user_id"], q["who"])

    # ------------------------------------------------------------ troca

    async def run_switch(self, game, who):
        """Roda o switch-game.py lendo a saída linha a linha para avançar as etapas do painel."""
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "scripts/switch-game.py", game, "--no-notify", "--requester", who,
            "--require-player-count", cwd=REPO, env=git_env(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        lines = []

        async def pump():
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").rstrip()
                lines.append(line)
                if self.advance(line):
                    await self.render()
            return await proc.wait()

        try:
            code = await asyncio.wait_for(pump(), SWITCH_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            code = 124
            lines.append(f"ERRO: troca passou de {SWITCH_TIMEOUT // 60} min")
        return code, "\n".join(lines)

    @staticmethod
    def advance(line):
        """Marcos impressos pelo switch-game.py -> etapa concluída. Devolve True se a etapa mudou."""
        s = STATE.switching
        step = s["step"]
        if "save enviado" in line or line.startswith("Parando "):
            step = max(step, 1)
        if line.startswith("Subindo "):
            step = max(step, 2)
        if line.startswith("OK: "):
            step = max(step, 3)
        changed, s["step"] = step != s["step"], step
        return changed

    async def ticker(self):
        """Durante a troca, atualiza o tempo decorrido no painel."""
        while STATE.switching:
            await asyncio.sleep(15)
            if STATE.switching:
                try:
                    await self.render()
                except discord.HTTPException as exc:
                    log.warning("painel: %s", exc)

    async def switch(self):
        s = STATE.switching
        game, prev, who, user_id = s["game"], s["prev"], s["who"], s["user_id"]
        titles = {k: v["title"] for k, v in self.info["games"].items()}
        async with STATE.lock:
            STATE.error = None
            await self.render()
            await self.audit(f"🔄 Troca {titles.get(prev, prev)} → {titles[game]} pedida por {who} no #{GAMES_CHANNEL}.")
            tick = self.loop.create_task(self.ticker())
            started = now()
            code, out = 1, ""
            try:
                await sync_repo()
                code, out = await self.run_switch(game, who)
            except Exception as exc:  # noqa: BLE001
                out = f"ERRO: {exc}"
            minutes = max(1, round((now() - started).total_seconds() / 60))
            STATE.switching = None
            tick.cancel()
            STATE.cooldown_until = now() + COOLDOWN
            if code == 0:
                await self.audit(f"✅ {titles[game]} no ar ({minutes} min). Pedido por {who}.")
                await self.notify_ready(game, user_id, titles[game])
            else:
                STATE.error = titles[game]
                tail = "\n".join(out.strip().splitlines()[-12:])
                await self.audit(f"❌ Troca para {titles[game]} falhou ({minutes} min), pedida por {who}.\n```\n{tail[-1500:]}\n```")
        await self.refresh()   # fora do lock: o refresh só sincroniza o clone quando não há troca

    async def notify_ready(self, game, user_id, title):
        """Avisa só quem pediu, por DM. Com a DM fechada, menção no #games que some em 2 min."""
        g = self.info["games"].get(game, {})
        where = f" Endereço: `{GAME_HOST}:{g.get('port')}`." if GAME_HOST and g else ""
        try:
            user = self.get_user(user_id) or await self.fetch_user(user_id)
            await user.send(f"{icon(game)} **{title}** está pronto, pode entrar.{where}")
            return
        except discord.HTTPException as exc:   # Forbidden: DM fechada para membros do servidor
            log.info("DM para %s indisponível (%s); avisando no #%s", user_id, exc.status, GAMES_CHANNEL)
        if self.games_ch:
            await self.games_ch.send(f"<@{user_id}> **{title}** está pronto.{where}", delete_after=120,
                                     allowed_mentions=discord.AllowedMentions(users=True))


BOT = GamesBot()

if __name__ == "__main__":
    token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    BOT.run(token, log_handler=None)
