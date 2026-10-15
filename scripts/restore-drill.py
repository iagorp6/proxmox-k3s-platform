#!/usr/bin/env python3
"""Restore drill: restaura o último snapshot restic de um jogo num PVC temporário e mede o tempo.

Prova que o backup volta, sem tocar no jogo: o PVC do servidor nem é montado. Fluxo:
  PVC <jogo>-restore-drill (local-path) + Job com a mesma imagem e o mesmo Secret do CronJob de
  backup -> restic restore do último snapshot (tag e host = nome do jogo) -> confere que cada
  backup.paths do catálogo voltou com conteúdo e que cada backup.verify (glob) existe -> imprime
  snapshot, tamanho e tempos -> apaga Job e PVC.

O tempo medido é o RTO do dado (do apply ao mundo restaurado e conferido no cluster), não o de
um rebuild completo do host (ADR 0005).

Uso:
  scripts/restore-drill.py <jogo>              roda o drill e limpa tudo no fim
  scripts/restore-drill.py <jogo> --keep       mantém o PVC e o Job para inspecionar
  scripts/restore-drill.py <jogo> --dry-run    só imprime os manifests (o CI valida com kubeconform)
  scripts/restore-drill.py <jogo> --record     acrescenta o resultado em docs/restore-drills.md
"""
import argparse
import datetime
import json
import pathlib
import re
import shlex
import subprocess
import sys
import time

import yaml

import catalog

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULTS = yaml.safe_load((ROOT / "platform" / "defaults.yaml").read_text(encoding="utf-8"))
RECORD_FILE = ROOT / "docs" / "restore-drills.md"
TARGET = "/restore"


def drill_script(g):
    """Script do Job: restaura, confere e imprime uma linha RESULT em JSON para o drill ler dos logs."""
    name, b = g["name"], g["backup"]
    checks = "".join(
        f'[ -n "$(ls -A {TARGET}{shlex.quote(p)} 2>/dev/null)" ] || {{ echo "FALHA: {p} vazio ou ausente"; exit 1; }}\n'
        for p in b["paths"])
    # Glob sem aspas de propósito: o shell expande; sem match, o ls do padrão literal falha
    checks += "".join(
        f'ls -d {TARGET}/{v.lstrip("/")} >/dev/null 2>&1 || {{ echo "FALHA: nada casa com {v}"; exit 1; }}\n'
        for v in b.get("verify", []))
    return (
        "set -eu\n"
        f"restic snapshots --tag {name} --host {name} --latest 1 --json > /tmp/snap.json\n"
        "start=$(date +%s)\n"
        f"restic restore latest --tag {name} --host {name} --target {TARGET}\n"
        "end=$(date +%s)\n"
        f"{checks}"
        f"bytes=$(( $(du -sk {TARGET} | cut -f1) * 1024 ))\n"   # du do busybox não tem -b
        f"files=$(find {TARGET} -type f | wc -l)\n"
        "snap=$(tr -d '\\n' < /tmp/snap.json)\n"
        'echo "RESULT {\\"restore_seconds\\": $((end - start)), \\"bytes\\": $bytes, \\"files\\": $files, \\"snapshots\\": $snap}"\n'
    )


def manifests(g):
    name, ns, uid = g["name"], g["namespace"], g.get("runAsUser", 1000)
    drill = f"{name}-restore-drill"
    labels = {"app.kubernetes.io/name": drill, "platform.homelab/game": name}
    pvc = {
        "apiVersion": "v1", "kind": "PersistentVolumeClaim",
        "metadata": {"name": drill, "namespace": ns, "labels": labels},
        "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": "local-path",
                 "resources": {"requests": {"storage": g["backup"].get("restoreSize", "5Gi")}}},
    }
    job = {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": drill, "namespace": ns, "labels": labels},
        "spec": {"backoffLimit": 0, "ttlSecondsAfterFinished": 86400, "template": {
            "metadata": {"labels": labels},
            "spec": {
                "restartPolicy": "Never", "automountServiceAccountToken": False,
                "securityContext": {"runAsNonRoot": True, "runAsUser": uid, "runAsGroup": uid, "fsGroup": uid,
                                    "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [{
                    "name": "restic", "image": DEFAULTS["resticImage"],
                    "command": ["/bin/sh", "-c"], "args": [drill_script(g)],
                    "envFrom": [{"secretRef": {"name": f"{name}-backup"}}],
                    "env": [{"name": "RESTIC_CACHE_DIR", "value": "/cache"}, {"name": "TMPDIR", "value": "/tmp"}],
                    "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                        "capabilities": {"drop": ["ALL"]}},
                    "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"memory": "512Mi"}},
                    "volumeMounts": [{"name": "restore", "mountPath": TARGET},
                                     {"name": "cache", "mountPath": "/cache"},
                                     {"name": "tmp", "mountPath": "/tmp"}],
                }],
                "volumes": [{"name": "restore", "persistentVolumeClaim": {"claimName": drill}},
                            {"name": "cache", "emptyDir": {}},
                            {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}}],
            }}},
    }
    return [pvc, job]


def kubectl(*args, stdin=None, check=True):
    r = subprocess.run(["kubectl", *args], input=stdin, text=True, capture_output=True)
    if check and r.returncode != 0:
        sys.exit(f"ERRO: kubectl {' '.join(args)}\n{(r.stderr or r.stdout).strip()}")
    return r


def wait_job(ns, job, timeout):
    """Espera o Job terminar; devolve True (Complete) ou False (Failed ou timeout)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = kubectl("-n", ns, "get", "job", job, "-o", "json", check=False)
        if r.returncode == 0:
            for c in json.loads(r.stdout).get("status", {}).get("conditions", []):
                if c["status"] == "True" and c["type"] in ("Complete", "Failed"):
                    return c["type"] == "Complete"
        time.sleep(3)
    print(f"ERRO: o Job não terminou em {timeout}s")
    return False


def snapshot_time(snap):
    """Horário do snapshot em UTC (o restic grava nanossegundos e o fuso do container)."""
    text = re.sub(r"\.\d+", "", snap["time"]).replace("Z", "+00:00")
    return datetime.datetime.fromisoformat(text).astimezone(datetime.timezone.utc)


def human(n):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def record(g, result, total):
    snap = result["snapshots"][0]
    if not RECORD_FILE.exists():
        RECORD_FILE.write_text(
            "# Restore drills\n\n"
            "Generated by `make restore-drill GAME=<game> RECORD=1` (`scripts/restore-drill.py`). Each row restores the\n"
            "latest restic snapshot from S3 into a temporary PVC, checks the catalog paths, and deletes it.\n"
            "**Total** is apply to verified data (includes PVC provisioning and image pull); **restic** is the restore alone.\n\n"
            "| Date | Game | Snapshot | Snapshot age | Size | Files | restic | Total |\n"
            "|---|---|---|---|---|---|---|---|\n", encoding="utf-8")
    age = datetime.datetime.now(datetime.timezone.utc) - snapshot_time(snap)
    row = (f"| {datetime.date.today().isoformat()} | {g['name']} | `{snap['short_id']}` | {age.total_seconds() / 3600:.0f} h "
           f"| {human(result['bytes'])} | {result['files']} | {result['restore_seconds']} s | {total:.0f} s |\n")
    with RECORD_FILE.open("a", encoding="utf-8") as f:
        f.write(row)
    print(f"registrado em {RECORD_FILE.relative_to(ROOT)}")


def main():
    games = {g["name"]: g for g in catalog.games() if g.get("backup")}
    ap = argparse.ArgumentParser(description="Restore drill de um jogo do catálogo")
    ap.add_argument("game", choices=sorted(games))
    ap.add_argument("--keep", action="store_true", help="não apaga o PVC e o Job no fim")
    ap.add_argument("--dry-run", action="store_true", help="só imprime os manifests")
    ap.add_argument("--record", action="store_true", help="acrescenta o resultado em docs/restore-drills.md")
    ap.add_argument("--timeout", type=int, default=900, help="limite em segundos para o Job (padrão 900)")
    args = ap.parse_args()
    g = games[args.game]
    docs = yaml.safe_dump_all(manifests(g), sort_keys=False, allow_unicode=True, width=1000)
    if args.dry_run:
        print(docs, end="")
        return 0

    ns, drill = g["namespace"], f"{g['name']}-restore-drill"
    if kubectl("-n", ns, "get", "job", drill, check=False).returncode == 0:
        sys.exit(f"ERRO: já existe um Job {drill} em {ns} (drill anterior com --keep?). Apague com:\n"
                 f"  kubectl -n {ns} delete job,pvc {drill}")
    print(f">> restore drill de {g['displayName']} (namespace {ns})")
    start = time.monotonic()
    ok = False
    try:
        kubectl("apply", "-f", "-", stdin=docs)
        ok = wait_job(ns, drill, args.timeout)
        total = time.monotonic() - start
        logs = kubectl("-n", ns, "logs", f"job/{drill}", check=False).stdout
        line = next((l for l in logs.splitlines() if l.startswith("RESULT ")), None)
        if not ok or not line:
            print(logs[-3000:])
            print("FALHOU: o restore não terminou ou não passou nas verificações")
            ok = False
            return 1
        result = json.loads(line[len("RESULT "):])
        snap = result["snapshots"][0]
        print(f"   snapshot   {snap['short_id']} de {snapshot_time(snap):%Y-%m-%d %H:%M} UTC ({', '.join(snap.get('paths', []))})")
        print(f"   tamanho    {human(result['bytes'])} em {result['files']} arquivos")
        print(f"   restic     {result['restore_seconds']} s")
        print(f"   total      {total:.0f} s (apply até o dado restaurado e conferido)")
        print("OK: backup restaurado e conferido")
        if args.record:
            record(g, result, total)
        return 0
    finally:
        if args.keep:
            print(f"--keep: inspecione com kubectl -n {ns} get job,pvc {drill}; apague com kubectl -n {ns} delete job,pvc {drill}")
        else:
            kubectl("-n", ns, "delete", "job", drill, "--ignore-not-found", "--wait=true", check=False)
            kubectl("-n", ns, "delete", "pvc", drill, "--ignore-not-found", "--wait=false", check=False)
            print("   Job e PVC do drill removidos" + ("" if ok else " (rode com --keep para inspecionar)"))


if __name__ == "__main__":
    sys.exit(main())
