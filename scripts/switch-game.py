#!/usr/bin/env python3
"""Troca o jogo ativo da plataforma (um jogo por vez), sempre pelo Git.

Fluxo: confere jogadores online no jogo atual -> salva o mundo -> grava os patches de
réplicas + platform/active-game.yaml -> commit/push -> avisa o Discord -> sincroniza no
Argo CD primeiro o jogo que sai (preStop salva de novo) e depois o que entra -> espera
ficar pronto -> avisa o Discord com o endereço de conexão.

Uso:
  scripts/switch-game.py <jogo>              troca de verdade
  scripts/switch-game.py <jogo> --force      ignora a checagem de jogadores online
  scripts/switch-game.py <jogo> --dry-run    só mostra/grava os arquivos, sem git/kubectl
  scripts/switch-game.py <jogo> --no-notify  não avisa o Discord
  scripts/switch-game.py <jogo> --requester NOME --require-player-count
                                             usado pelo games-bot (ADR 0006): registra quem pediu
                                             no commit e recusa a troca sem contagem de jogadores
  scripts/switch-game.py <jogo> --resync     não troca: aplica no jogo o que mudou no main (whitelist,
                                             imagem), com a mesma checagem de jogadores e o save antes
                                             do Sync no Argo CD (make sync-game)

Configuração local (.env.local, fora do Git) ou variáveis de ambiente (games-bot): GAME_HOST
(endereço que os jogadores usam), DISCORD_ROLE_ID (cargo mencionado quando o jogo fica pronto;
opcional) e DISCORD_WEBHOOK_URL (senão lê o Secret discord-webhook do cluster).
"""
import argparse
import base64
import fnmatch
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.request

import catalog

ROOT = pathlib.Path(__file__).resolve().parent.parent
ACTIVE_FILE = ROOT / "platform" / "active-game.yaml"
# Únicos caminhos que uma troca pode commitar (a deploy key do games-bot ignora o ruleset do main)
ALLOWED_PATHS = ("platform/active-game.yaml", "k8s/games/*/replicas.yaml", "k8s/*/replicas.yaml")


def _from_catalog(g):
    """Converte uma entrada do catálogo (game.yaml) no que a troca precisa saber do jogo."""
    sw = g.get("switch") or {}
    entry = {
        "title": g["displayName"], "port": g["ports"][0]["port"],
        "dir": g["dir"], "ns": g["namespace"], "sts": g["name"], "app": g["name"],
        "players_metric": sw.get("playersMetric", "game_players_online"), "exporter": "exporter",
        "suspend_when_idle": sw.get("suspendWhenIdle", []),
    }
    if sw.get("saveCommand"):
        entry["save_cmd"] = sw["saveCommand"]
    if sw.get("joinHint"):
        entry["join_hint"] = sw["joinHint"]
    return entry


# Jogos implantados, lidos do catálogo (k8s/**/game.yaml): não há lista de jogos neste script.
GAMES = {g["name"]: _from_catalog(g) for g in catalog.games()}
COLOR_INFO, COLOR_OK, COLOR_FAIL = 0xF1C40F, 0x2ECC71, 0xE74C3C


def run(cmd, check=True, capture=True):
    r = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=capture)
    if check and r.returncode != 0:
        sys.exit(f"ERRO: {' '.join(cmd)}\n{(r.stderr or r.stdout).strip()}")
    return r


def env_local():
    """Variáveis de ambiente (games-bot) com o .env.local por cima (uso local)."""
    env = {k: v for k, v in os.environ.items() if k in ("GAME_HOST", "DISCORD_ROLE_ID", "DISCORD_WEBHOOK_URL")}
    path = ROOT / ".env.local"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def notify(title, description, color, mention=False):
    """Envia um embed ao Discord pelo webhook guardado no cluster (Secret vindo do SSM)."""
    url = env_local().get("DISCORD_WEBHOOK_URL")
    if not url:
        r = run(["kubectl", "-n", "platform", "get", "secret", "discord-webhook",
                 "-o", "jsonpath={.data.webhook-url}"], check=False)
        if r.returncode != 0 or not r.stdout.strip():
            print("  AVISO: webhook do Discord indisponível; aviso não enviado")
            return
        url = base64.b64decode(r.stdout.strip()).decode()
    payload = {"username": "GamesPlatform", "embeds": [{"title": title, "description": description, "color": color}]}
    role = env_local().get("DISCORD_ROLE_ID")
    if mention and role:
        payload["content"] = f"<@&{role}>"
        payload["allowed_mentions"] = {"roles": [role]}
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "GamesPlatform-SwitchGame/1.0"})
    try:
        urllib.request.urlopen(req, timeout=10)
        print("  aviso enviado ao Discord")
    except Exception as exc:  # noqa: BLE001 - aviso é best-effort, não interrompe a troca
        print(f"  AVISO: falha ao avisar o Discord: {exc}")


def current_active():
    if not ACTIVE_FILE.exists():
        return None
    m = re.search(r"^active:\s*(\S+)", ACTIVE_FILE.read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else None


def pod_running(g):
    r = run(["kubectl", "-n", g["ns"], "get", "pod", f"{g['sts']}-0", "-o", "jsonpath={.status.phase}"], check=False)
    return r.returncode == 0 and r.stdout.strip() == "Running"


def players_online(g):
    if not g["players_metric"] or not pod_running(g):
        return None
    code = ("import urllib.request;print(urllib.request.urlopen("
            "'http://127.0.0.1:9105/metrics',timeout=5).read().decode())")
    r = run(["kubectl", "-n", g["ns"], "exec", f"{g['sts']}-0", "-c", g["exporter"], "--",
             "python", "-c", code], check=False)
    m = re.search(rf"^{g['players_metric']}(?:\{{[^}}]*\}})?\s+(\d+)", r.stdout or "", re.M)
    return int(m.group(1)) if m else None


def save_world(g):
    if not pod_running(g):
        return
    r = run(["kubectl", "-n", g["ns"], "exec", f"{g['sts']}-0", "-c", "server", "--",
             "sh", "-c", g.get("save_cmd", "echo save > /tmp/console")], check=False)
    print(f"  save enviado ao console de {g['sts']}" if r.returncode == 0 else f"  AVISO: não consegui enviar save para {g['sts']}")


def declares_namespace(g):
    """O patch precisa ter (ou não) namespace igual ao declarado nos manifests do jogo."""
    sts = (ROOT / g["dir"] / "statefulset.yaml").read_text(encoding="utf-8")
    return re.search(r"^metadata:\n(?:  .*\n)*?  namespace:", sts, re.M) is not None


def write_patch(name, active):
    g = GAMES[name]
    on = name == active
    ns = f"  namespace: {g['ns']}\n" if declares_namespace(g) else ""
    docs = [
        "# Gerado por scripts/switch-game.py: não edite à mão.\n"
        "apiVersion: apps/v1\nkind: StatefulSet\nmetadata:\n"
        f"  name: {g['sts']}\n{ns}spec:\n  replicas: {1 if on else 0}\n"
    ]
    for cj in g["suspend_when_idle"]:
        docs.append(
            "apiVersion: batch/v1\nkind: CronJob\nmetadata:\n"
            f"  name: {cj}\n{ns}spec:\n  suspend: {'false' if on else 'true'}\n"
        )
    if not on and g["dir"].startswith("k8s/games/"):
        # Jogo parado sai do ServiceLB: o svclb de um LoadBalancer reserva as portas no nó mesmo sem
        # endpoint, e dois jogos na mesma porta e protocolo (Zomboid e Valheim) não sobem juntos
        docs.append(
            "apiVersion: v1\nkind: Service\nmetadata:\n"
            f"  name: {g['app']}\n{ns}spec:\n  type: ClusterIP\n  externalTrafficPolicy: null\n"
        )
    path = ROOT / g["dir"] / "replicas.yaml"
    path.write_text("---\n".join(docs), encoding="utf-8")
    return path


def wait_application(app, timeout=300):
    """Espera o Application existir. Jogo recém-adicionado: o app raiz cria o Application no ciclo dele
    (até ~3 min depois do merge), então uma troca logo após o merge pede um refresh do root e espera."""
    deadline = time.time() + timeout
    asked = False
    while run(["kubectl", "-n", "argocd", "get", "application", app, "-o", "name"], check=False).returncode != 0:
        if time.time() >= deadline:
            sys.exit(f"ERRO: Application {app} não apareceu no Argo CD em {timeout}s (confira o app root)")
        if not asked:
            print(f"  {app}: Application ainda não existe; pedindo refresh do app root e esperando")
            run(["kubectl", "-n", "argocd", "annotate", "application", "root",
                 "argocd.argoproj.io/refresh=hard", "--overwrite"], check=False)
            asked = True
        time.sleep(5)


def argo_sync(app, revision):
    wait_application(app)
    run(["kubectl", "-n", "argocd", "annotate", "application", app,
         "argocd.argoproj.io/refresh=hard", "--overwrite"])
    time.sleep(5)
    op = {"operation": {"initiatedBy": {"username": "switch-game"}, "sync": {"revision": revision}}}
    run(["kubectl", "-n", "argocd", "patch", "application", app, "--type", "merge", "-p", json.dumps(op)])
    deadline = time.time() + 600
    while time.time() < deadline:
        r = run(["kubectl", "-n", "argocd", "get", "application", app, "-o",
                 "jsonpath={.status.operationState.phase} {.status.operationState.syncResult.revision}"], check=False)
        phase, _, rev = (r.stdout or "").partition(" ")
        if rev.strip() == revision and phase in ("Succeeded", "Failed", "Error"):
            if phase != "Succeeded":
                sys.exit(f"ERRO: sync de {app} terminou como {phase}")
            print(f"  {app}: sync ok")
            return
        time.sleep(5)
    sys.exit(f"ERRO: timeout esperando o sync de {app}")


def out_of_sync(g):
    """Atualiza o app no Argo CD e diz se o cluster está diferente do Git."""
    wait_application(g["app"])
    run(["kubectl", "-n", "argocd", "annotate", "application", g["app"],
         "argocd.argoproj.io/refresh=hard", "--overwrite"])
    time.sleep(5)
    r = run(["kubectl", "-n", "argocd", "get", "application", g["app"], "-o", "jsonpath={.status.sync.status}"])
    return r.stdout.strip() != "Synced"


def sync_game(g, sha, running):
    argo_sync(g["app"], sha)
    if running:
        run(["kubectl", "-n", g["ns"], "rollout", "status", f"statefulset/{g['sts']}", "--timeout=1200s"], capture=False)


def apply_switch(previous, target, sha):
    if previous in GAMES:
        print(f"Parando {previous}...")
        argo_sync(GAMES[previous]["app"], sha)
        run(["kubectl", "-n", GAMES[previous]["ns"], "wait", "--for=delete",
             f"pod/{GAMES[previous]['sts']}-0", "--timeout=300s"], check=False)
    for name in GAMES:
        if name not in (previous, target):
            argo_sync(GAMES[name]["app"], sha)
    print(f"Subindo {target}...")
    argo_sync(GAMES[target]["app"], sha)
    g = GAMES[target]
    run(["kubectl", "-n", g["ns"], "rollout", "status", f"statefulset/{g['sts']}", "--timeout=1200s"], capture=False)


def prepare_repo():
    if run(["git", "status", "--porcelain"]).stdout.strip():
        sys.exit("ERRO: há mudanças não commitadas no repositório; commite ou descarte antes.")
    if run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip() != "main":
        sys.exit("ERRO: rode a troca a partir do branch main.")
    run(["git", "pull", "--ff-only"])


def check_players(name, a):
    """Sai com erro se houver gente jogando (ou sem contagem, no games-bot); senão manda salvar o mundo."""
    n = players_online(GAMES[name])
    if n is None and a.require_player_count and not a.force:
        sys.exit(f"ERRO: não consegui contar os jogadores de {name}; troca recusada.")
    if n is None:
        print(f"  AVISO: contagem de jogadores indisponível para {name}")
    elif n > 0 and not a.force:
        sys.exit(f"ERRO: {n} jogador(es) online em {name}. Use --force para trocar mesmo assim.")
    else:
        print(f"  jogadores online em {name}: {n}")
    save_world(GAMES[name])


def resync(name, a, send):
    """Sync do jogo com o main sem trocar o ativo. Sem diferença no Argo, não mexe no servidor."""
    g = GAMES[name]
    prepare_repo()
    sha = run(["git", "rev-parse", "HEAD"]).stdout.strip()
    if not out_of_sync(g):
        print(f"Nada a fazer: {g['app']} já está sincronizado com o main.")
        return 0
    active = name == current_active()
    if active:
        check_players(name, a)
        send(f"🔁 Atualizando {g['title']}",
             "Aplicando uma atualização no servidor; ele pode reiniciar e voltar em alguns minutos.", COLOR_INFO)
    try:
        sync_game(g, sha, active)
    except SystemExit as exc:
        if active:
            send(f"⚠️ A atualização de {g['title']} não terminou",
                 "Algo deu errado. O administrador já foi avisado pelo terminal.", COLOR_FAIL)
        raise exc
    if active:
        send(f"🎮 {g['title']} está no ar!", "Atualização aplicada. Use o endereço de sempre.", COLOR_OK)
    print(f"OK: {name} sincronizado com {sha[:7]}.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("game", choices=sorted(GAMES))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--requester", help="quem pediu a troca (vai na mensagem do commit)")
    ap.add_argument("--require-player-count", action="store_true",
                    help="recusa a troca se não der para contar os jogadores do jogo atual")
    ap.add_argument("--resync", action="store_true",
                    help="não troca: sincroniza o jogo com o main (checa jogadores e salva antes)")
    a = ap.parse_args()
    requester = " ".join((a.requester or "").split())[:40]
    send = (lambda *args, **kw: None) if a.no_notify else notify
    if a.resync:
        return resync(a.game, a, send)

    target, previous = a.game, current_active()
    print(f"Jogo ativo: {previous or '(nenhum)'} -> {target}")
    if target == previous and not a.dry_run:
        print(f"Nada a fazer: esse jogo já é o ativo. Para aplicar mudanças do main nele: make sync-game GAME={target}")
        return 0

    if not a.dry_run:
        prepare_repo()
        if previous in GAMES:
            check_players(previous, a)

    written = [write_patch(name, target) for name in GAMES]
    ACTIVE_FILE.parent.mkdir(exist_ok=True)
    text = ACTIVE_FILE.read_text(encoding="utf-8") if ACTIVE_FILE.exists() else "active: none\n"
    text = re.sub(r"^active:.*$", f"active: {target}", text, count=1, flags=re.M) if "active:" in text else text + f"active: {target}\n"
    ACTIVE_FILE.write_text(text, encoding="utf-8")
    for p in written + [ACTIVE_FILE]:
        print(f"  gravado: {p.relative_to(ROOT)}")
    if a.dry_run:
        return 0

    files = [str(p.relative_to(ROOT)) for p in written + [ACTIVE_FILE]]
    run(["git", "add", *files])
    staged = run(["git", "diff", "--cached", "--name-only"]).stdout.split()
    extra = [f for f in staged if not any(fnmatch.fnmatch(f, pat) for pat in ALLOWED_PATHS)]
    if extra:
        run(["git", "reset", "-q"])
        sys.exit(f"ERRO: a troca só pode commitar réplicas e o jogo ativo; recusado: {', '.join(extra)}")
    message = f"platform: switch active game {previous} -> {target}"
    if requester:
        message += f" (pedido por {requester} via Discord)"
    run(["git", "-c", "core.hooksPath=/dev/null", "commit", "-m", message])
    run(["git", "push"])
    sha = run(["git", "rev-parse", "HEAD"]).stdout.strip()

    t_name = GAMES[target]["title"]
    p_name = GAMES[previous]["title"] if previous in GAMES else "nenhum"
    send(f"🔄 Trocando de {p_name} para {t_name}",
         "O servidor fica indisponível por alguns minutos. Aviso aqui quando estiver pronto.", COLOR_INFO)
    started = time.time()
    try:
        apply_switch(previous, target, sha)
    except SystemExit as exc:
        send(f"⚠️ A troca para {t_name} não terminou",
             "Algo deu errado na troca. O administrador já foi avisado pelo terminal.", COLOR_FAIL)
        raise exc

    minutes = max(1, round((time.time() - started) / 60))
    host = env_local().get("GAME_HOST")
    where = f"Conecte em **`{host}:{GAMES[target]['port']}`**." if host else "Use o endereço de sempre."
    hint = GAMES[target].get("join_hint")
    body = f"{where}\n{hint}\nA troca levou cerca de {minutes} min." if hint else f"{where}\nA troca levou cerca de {minutes} min."
    send(f"🎮 {t_name} está no ar!", body, COLOR_OK, mention=True)
    print(f"OK: {target} é o jogo ativo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
