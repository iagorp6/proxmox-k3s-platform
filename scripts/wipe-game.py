#!/usr/bin/env python3
"""Wipe de um jogo: arquiva o mundo atual no restic e só então apaga os caminhos de wipe do catálogo.

Para recomeçar um mundo do zero (mods novos, temporada nova) sem perder o antigo. Fluxo, num Job só,
com o servidor parado (StatefulSet em 0 réplicas) e o PVC do jogo montado em /data como no backup:
  restic backup de backup.paths com as tags "archive" e "<jogo>-wipe-<data>" (sem a tag do jogo, então o
  forget diário/semanal do CronJob não apaga: o archive fica até alguém tirar à mão)
  -> confere que cada caminho de wipe que existe no disco está no snapshot
  -> apaga os caminhos de wipe (game.yaml: wipe, subconjunto de backup.paths)
O que não está em wipe (config do servidor, sandbox) fica, e o jogo sobe com mundo novo no próximo start.
Desfazer: make restore-game GAME=<jogo> SNAPSHOT=<id impresso no fim>.

Uso:
  scripts/wipe-game.py <jogo>             pede para digitar o nome do jogo antes de apagar
  scripts/wipe-game.py <jogo> --dry-run   só imprime o Job (o CI valida com kubeconform)
"""
import argparse
import datetime
import importlib
import json
import shlex
import sys

import yaml

import catalog

restore = importlib.import_module("restore-drill")   # mesmo Job, Secret e checagens do restore --live


def wipe_paths(g):
    """Caminhos de wipe do catálogo; cada um precisa estar dentro de algum backup.paths (senão não teria archive)."""
    paths = g.get("wipe") or []
    for p in paths:
        if not any(p == b or p.startswith(b.rstrip("/") + "/") for b in g["backup"]["paths"]):
            sys.exit(f"ERRO: wipe {p} de {g['dir']}/game.yaml está fora de backup.paths ({', '.join(g['backup']['paths'])})")
    return paths


def wipe_script(g, label):
    name, paths = g["name"], wipe_paths(g)
    q = shlex.quote
    # restic ls --json: uma linha por nó com "path" absoluto; o grep confere o caminho exato
    checks = "".join(
        f'if [ -e {q(p)} ]; then restic ls --json "$id" | grep -qF {q(chr(34) + "path" + chr(34) + ":" + chr(34) + p + chr(34))} '
        f'|| {{ echo "FALHA: {p} não está no snapshot $id; nada foi apagado"; exit 1; }}; fi\n'
        for p in paths)
    removes = "".join(
        f'if [ -e {q(p)} ]; then rm -rf {q(p)} && echo "apagado: {p}"; else echo "não existia: {p}"; fi\n'
        for p in paths)
    return (
        "set -eu\n"
        f"restic backup {' '.join(q(p) for p in g['backup']['paths'])} --host {name} --tag archive --tag {q(label)} "
        "--retry-lock 10m --json > /tmp/backup.json\n"
        # a última linha do --json é o resumo, com o snapshot_id
        "id=$(tail -n 1 /tmp/backup.json | sed -n 's/.*\"snapshot_id\":\"\\([0-9a-f]*\\)\".*/\\1/p')\n"
        '[ -n "$id" ] || { echo "FALHA: backup sem snapshot_id; nada foi apagado"; tail -n 5 /tmp/backup.json; exit 1; }\n'
        'echo "archive: $id"\n'
        f"{checks}"
        f"{removes}"
        'echo "RESULT {\\"snapshot\\": \\"$id\\"}"\n'
    )


def manifest(g, label):
    """Job do restore --live (mesma imagem, Secret, securityContext e PVC) com o script de wipe."""
    _, job = restore.manifests(g, live=True)
    drill = f"{g['name']}-wipe"
    job["metadata"]["name"] = drill
    for labels in (job["metadata"]["labels"], job["spec"]["template"]["metadata"]["labels"]):
        labels["app.kubernetes.io/name"] = drill
    job["spec"]["template"]["spec"]["containers"][0]["args"] = [wipe_script(g, label)]
    return job


def main():
    games = {g["name"]: g for g in catalog.games() if g.get("backup") and g.get("wipe")}
    ap = argparse.ArgumentParser(description="Arquiva e apaga o mundo de um jogo do catálogo")
    ap.add_argument("game", choices=sorted(games))
    ap.add_argument("--dry-run", action="store_true", help="só imprime o Job")
    ap.add_argument("--timeout", type=int, default=1800, help="limite em segundos para o Job (padrão 1800)")
    args = ap.parse_args()
    g = games[args.game]
    label = f"{g['name']}-wipe-{datetime.date.today():%Y%m%d}"
    job = manifest(g, label)
    if args.dry_run:
        print(yaml.safe_dump(job, sort_keys=False, allow_unicode=True, width=1000), end="")
        return 0

    ns, name = g["namespace"], job["metadata"]["name"]
    if restore.kubectl("-n", ns, "get", "job", name, check=False).returncode == 0:
        sys.exit(f"ERRO: já existe um Job {name} em {ns}. Veja os logs e apague com: kubectl -n {ns} delete job {name}")
    if restore.kubectl("-n", ns, "get", "pvc", g["backup"]["pvc"], check=False).returncode != 0:
        sys.exit(f"ERRO: PVC {g['backup']['pvc']} não existe em {ns}: não há mundo para apagar")
    sts = restore.preflight_live(g)
    print(f"ATENÇÃO: WIPE de {g['displayName']}. Apaga do PVC {g['backup']['pvc']}:")
    for p in wipe_paths(g):
        print(f"  {p}")
    print(f"  Antes, snapshot restic com as tags archive e {label} (não expira), conferido.")
    try:
        answer = input(f"  Digite {g['name']} para continuar: ").strip()
    except EOFError:
        answer = ""
    if answer != g["name"]:
        sys.exit("cancelado")

    print(f">> wipe de {g['displayName']} (namespace {ns})")
    try:
        restore.kubectl("apply", "-f", "-", stdin=yaml.safe_dump(job, sort_keys=False, allow_unicode=True, width=1000))
        ok = restore.wait_job(ns, name, args.timeout)
        logs = restore.kubectl("-n", ns, "logs", f"job/{name}", check=False).stdout
        line = next((l for l in logs.splitlines() if l.startswith("RESULT ")), None)
        for l in logs.splitlines():
            if l.startswith(("archive: ", "apagado: ", "não existia: ", "FALHA")):
                print(f"   {l}")
        if not ok or not line:
            print(logs[-3000:])
            print("FALHOU: veja acima. Se o archive não terminou, nada foi apagado.")
            return 1
        snap = json.loads(line[len("RESULT "):])["snapshot"]
        print(f"OK: mundo arquivado em {snap[:8]} e apagado.\n"
              f"   Desfazer: make restore-game GAME={g['name']} SNAPSHOT={snap[:8]}\n"
              f"   Mundo novo: make switch-game GAME={g['name']} (ou kubectl -n {ns} scale statefulset {sts} --replicas=1 se já é o ativo)")
        return 0
    finally:
        restore.kubectl("-n", ns, "delete", "job", name, "--ignore-not-found", "--wait=true", check=False)


if __name__ == "__main__":
    sys.exit(main())
