#!/usr/bin/env python3
"""Troca o jogo ativo da plataforma (um jogo por vez), sempre pelo Git.

Fluxo: confere jogadores online no jogo atual -> salva o mundo -> grava os patches de
réplicas + platform/active-game.yaml -> commit/push -> sincroniza no Argo CD primeiro o
jogo que sai (preStop salva de novo) e depois o que entra -> espera ficar pronto.

Uso:
  scripts/switch-game.py <jogo>            troca de verdade
  scripts/switch-game.py <jogo> --force    ignora a checagem de jogadores online
  scripts/switch-game.py <jogo> --dry-run  só mostra/grava os arquivos, sem git/kubectl
"""
import argparse
import json
import pathlib
import re
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
ACTIVE_FILE = ROOT / "platform" / "active-game.yaml"

# Registro dos jogos implantados (a fase de generalização vai derivar isto do catálogo)
GAMES = {
    "zomboid": {
        "dir": "k8s/zomboid", "ns": "zomboid", "sts": "zomboid", "app": "zomboid",
        "players_metric": "zomboid_players_online", "exporter": "exporter",
        "suspend_when_idle": ["zomboid-daily-restart"],
    },
    "terraria": {
        "dir": "k8s/games/terraria", "ns": "terraria", "sts": "terraria", "app": "terraria",
        "players_metric": None, "exporter": None,
        "suspend_when_idle": [],
    },
}


def run(cmd, check=True, capture=True):
    r = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=capture)
    if check and r.returncode != 0:
        sys.exit(f"ERRO: {' '.join(cmd)}\n{(r.stderr or r.stdout).strip()}")
    return r


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
    m = re.search(rf"^{g['players_metric']}\s+(\d+)", r.stdout or "", re.M)
    return int(m.group(1)) if m else None


def save_world(g):
    if not pod_running(g):
        return
    r = run(["kubectl", "-n", g["ns"], "exec", f"{g['sts']}-0", "-c", "server", "--",
             "sh", "-c", "echo save > /tmp/console"], check=False)
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
    path = ROOT / g["dir"] / "replicas.yaml"
    path.write_text("---\n".join(docs), encoding="utf-8")
    return path


def argo_sync(app, revision):
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("game", choices=sorted(GAMES))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    target, previous = a.game, current_active()
    print(f"Jogo ativo: {previous or '(nenhum)'} -> {target}")
    if target == previous and not a.dry_run:
        print("Nada a fazer: esse jogo já é o ativo.")
        return 0

    if not a.dry_run:
        if run(["git", "status", "--porcelain"]).stdout.strip():
            sys.exit("ERRO: há mudanças não commitadas no repositório; commite ou descarte antes.")
        if run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip() != "main":
            sys.exit("ERRO: rode a troca a partir do branch main.")
        run(["git", "pull", "--ff-only"])
        if previous in GAMES:
            n = players_online(GAMES[previous])
            if n is None:
                print(f"  AVISO: contagem de jogadores indisponível para {previous}")
            elif n > 0 and not a.force:
                sys.exit(f"ERRO: {n} jogador(es) online em {previous}. Use --force para trocar mesmo assim.")
            else:
                print(f"  jogadores online em {previous}: {n}")
            save_world(GAMES[previous])

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
    run(["git", "commit", "-m", f"platform: switch active game {previous} -> {target}"])
    run(["git", "push"])
    sha = run(["git", "rev-parse", "HEAD"]).stdout.strip()

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
    run(["kubectl", "-n", g["ns"], "rollout", "status", f"statefulset/{g['sts']}", "--timeout=900s"], capture=False)
    print(f"OK: {target} é o jogo ativo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
