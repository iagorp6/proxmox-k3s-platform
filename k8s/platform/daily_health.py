"""Checagem diária da plataforma no Discord (stdlib only): um "está tudo OK" por dia.

Lê o Prometheus: alertas warning/critical disparando, jogo no ar, idade do último backup,
espaço em disco, restarts de container nas últimas 24 h, uptime do host e da VM e patches
pendentes no host. Dia normal = uma mensagem curta, sem campos; o detalhe completo só aparece
quando há ponto de atenção. Se o Prometheus não responder, a falha também vai para o Discord:
silêncio nunca significa "tudo OK".
Com DRY_RUN=1 só imprime o que enviaria, sem postar no Discord (para testar o CronJob).
"""
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

PROM = os.getenv("PROMETHEUS_URL", "http://kps-kube-prometheus-stack-prometheus.monitoring.svc:9090")
WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
BRT = timezone(timedelta(hours=-3))
UA = {"User-Agent": "GamesPlatform-DailyHealth/1.0"}
GAMES_FILE = os.getenv("GAMES_FILE", "/app/games.json")
try:
    with open(GAMES_FILE, encoding="utf-8") as f:
        TITLES = {name: g["title"] for name, g in json.load(f).items()}
except FileNotFoundError:
    TITLES = {}

BACKUP_MAX_H = 26      # mesmo limite do alerta BackupStale
DISK_MIN_FREE = 0.15   # abaixo de 15% livre vira ponto de atenção

ALERTS = 'ALERTS{alertstate="firing", severity=~"warning|critical"}'
GAME_UP = "max by (game) (game_up)"
BACKUP_AGE = 'time() - max by (cronjob) (kube_cronjob_status_last_successful_time{cronjob=~".+-backup"})'
DISK_FREE = ('min by (job) (node_filesystem_avail_bytes{mountpoint="/", fstype!~"tmpfs|overlay"}'
             ' / node_filesystem_size_bytes{mountpoint="/", fstype!~"tmpfs|overlay"})')
RESTARTS = "sum by (namespace, pod) (increase(kube_pod_container_status_restarts_total[24h])) >= 1"
HOST_UPTIME = 'time() - node_boot_time_seconds{job="proxmox-node"}'
VM_UPTIME = 'time() - max(node_boot_time_seconds{job="node-exporter"})'
HOST_SECURITY = 'sum(apt_upgrades_pending{job="proxmox-node", origin=~".*-security.*"})'
HOST_REBOOT = ('max(max(node_reboot_required{job="proxmox-node"})'
               ' or max(homelab_kernel_reboot_pending{job="proxmox-node"}))')
# Textfile homelab-zfs do host (role host_exporters). Erros já viram alerta (ZfsVdevErrors/ZfsDataErrors)
ZFS_SCRUB_AGE = "time() - max(homelab_zfs_last_scrub_timestamp_seconds)"
ZFS_ERRORS = "sum(homelab_zfs_vdev_errors) + (sum(homelab_zfs_pool_data_errors) or vector(0))"


def q(expr):
    url = f"{PROM}/api/v1/query?{urllib.parse.urlencode({'query': expr})}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        return json.load(r)["data"]["result"]


def val(r):
    return float(r["value"][1])


def age(seconds):
    h = seconds / 3600
    return f"{h:.0f} h" if h < 48 else f"{h / 24:.0f} dias"


def build():
    """Retorna (problemas, campos do embed detalhado, resumo de um dia normal, notas informativas)."""
    problems, fields, summary, notes = [], [], [], []

    alerts = sorted({f"{r['metric'].get('alertname', '?')}"
                     + (f" ({r['metric'].get('game') or r['metric'].get('namespace')})"
                        if r['metric'].get('game') or r['metric'].get('namespace') else "")
                     for r in q(ALERTS)})
    if alerts:
        problems.append(f"{len(alerts)} alerta(s) disparando")
    fields.append({"name": "🚨 Alertas", "value": "\n".join(alerts[:10]) or "Nenhum", "inline": False})

    games = {r["metric"].get("game", "?"): val(r) for r in q(GAME_UP)}
    down = [g for g, v in games.items() if v < 1]
    if not games:
        problems.append("nenhum jogo respondendo")
    problems += [f"{TITLES.get(g, g)} fora do ar" for g in down]
    fields.append({"name": "🎮 Jogo", "value": "\n".join(
        f"{'🟢' if v >= 1 else '🔴'} {TITLES.get(g, g)}" for g, v in sorted(games.items())) or "🔴 Nenhum no ar",
        "inline": True})
    summary += [f"🟢 {TITLES.get(g, g)} no ar" for g, v in sorted(games.items()) if v >= 1]

    backups = {r["metric"].get("cronjob", "?").removesuffix("-backup"): val(r) for r in q(BACKUP_AGE)}
    # Só o jogo ativo faz backup diário; os parados guardam o último snapshot (o alerta BackupStale cobre o resto)
    active = [g for g in games if g in backups]
    for g in active:
        if backups[g] > BACKUP_MAX_H * 3600:
            problems.append(f"backup de {TITLES.get(g, g)} atrasado")
    fields.append({"name": "💾 Último backup", "value": "\n".join(
        f"{'🟢' if backups[g] <= BACKUP_MAX_H * 3600 else '🔴'} {TITLES.get(g, g)} · há {age(backups[g])}"
        for g in active) or "Sem dado do jogo ativo", "inline": True})
    summary += [f"💾 backup há {age(backups[g])}" for g in active]

    disks = {r["metric"].get("job", "?"): val(r) for r in q(DISK_FREE)}
    names = {"proxmox-node": "Host", "node-exporter": "VM"}
    for job, free in disks.items():
        if free < DISK_MIN_FREE:
            problems.append(f"disco ({names.get(job, job)}) com {free:.0%} livre")
    fields.append({"name": "🗄️ Disco livre (/)", "value": "\n".join(
        f"{'🟢' if f >= DISK_MIN_FREE else '🟡'} {names.get(j, j)} · {f:.0%}" for j, f in sorted(disks.items()))
        or "Sem dado", "inline": True})
    if disks:
        summary.append("🗄️ livre: " + ", ".join(f"{names.get(j, j)} {f:.0%}" for j, f in sorted(disks.items())))

    vm_uptime = q(VM_UPTIME)
    # Reboot da VM nas últimas 24 h (janela semanal de patch): os restarts vêm dele, não de falha
    vm_rebooted = bool(vm_uptime) and val(vm_uptime[0]) < 24 * 3600
    restarts = sorted(((r["metric"].get("namespace", "?"), r["metric"].get("pod", "?"), round(val(r)))
                       for r in q(RESTARTS)), key=lambda x: -x[2])
    if restarts:
        if not vm_rebooted:
            problems.append(f"{len(restarts)} pod(s) reiniciaram")
        fields.append({"name": "🔁 Restarts em 24 h", "value": "\n".join(
            f"{ns}/{pod} · {n}x" for ns, pod, n in restarts[:5]), "inline": False})
    if vm_rebooted:
        notes.append("VM reiniciou na janela de patch" + (f" ({len(restarts)} pod(s) voltaram junto)" if restarts else ""))

    uptime = q(HOST_UPTIME)
    if uptime:
        fields.append({"name": "⏱️ Host no ar", "value": age(val(uptime[0])), "inline": True})
    if vm_uptime:
        fields.append({"name": "⏱️ VM no ar", "value": age(val(vm_uptime[0]))
                       + (" (reiniciou por patch)" if vm_rebooted else ""), "inline": True})

    # Informativo: os alertas HostSecurityUpdatesPending/HostRebootPending cobrem o que passa de 7 dias
    security, reboot = q(HOST_SECURITY), q(HOST_REBOOT)
    if security or reboot:
        n = round(val(security[0])) if security else 0
        pending_reboot = bool(reboot) and val(reboot[0]) > 0
        fields.append({"name": "🩹 Patches do host", "value": (f"{n} de segurança pendente(s)" if n else "Em dia")
                       + (" · reboot pendente" if pending_reboot else ""), "inline": True})
        if n or pending_reboot:
            notes.append("Host: " + " e ".join(([f"{n} patch(es) de segurança"] if n else [])
                                              + (["reboot"] if pending_reboot else [])) + " pendente(s)")

    # Informativo: o ZfsScrubStale e os alertas de erro cobrem o que exige ação
    scrub, zfs_errors = q(ZFS_SCRUB_AGE), q(ZFS_ERRORS)
    if scrub or zfs_errors:
        n = round(val(zfs_errors[0])) if zfs_errors else 0
        fields.append({"name": "🧽 ZFS", "value": (f"scrub há {age(val(scrub[0]))}" if scrub else "sem scrub registrado")
                       + (f" · {n} erro(s) nos discos" if n else " · sem erros"), "inline": True})
        if n:
            notes.append(f"ZFS: {n} erro(s) corrigido(s) nos discos")

    return problems, fields, summary, notes


def payload():
    now = datetime.now(BRT)
    try:
        problems, fields, summary, notes = build()
    except Exception as e:  # noqa: BLE001 - qualquer falha vira mensagem, nunca silêncio
        title, color, fields = "❌ Checagem diária falhou", 0xED4245, [
            {"name": "Erro", "value": f"Não consegui consultar o Prometheus: {type(e).__name__}: {e}"[:1000]}]
        desc = "O monitoramento pode estar fora do ar. Rode `make status` no WSL."
    else:
        if problems:
            title, color = f"⚠️ {len(problems)} ponto(s) de atenção", 0xFEE75C
            desc = "\n".join(f"• {p}" for p in problems)
        else:
            # Dia normal: uma linha de resumo e as notas, sem campos (o detalhe fica no Grafana)
            title, color, fields = "✅ Tudo OK", 0x57F287, []
            desc = "\n".join([" · ".join(summary)] + [f"ℹ️ {n}" for n in notes])
    return {
        "username": "GamesPlatform",
        "embeds": [{
            "title": title,
            "description": desc,
            "color": color,
            "fields": fields,
            "footer": {"text": "Checagem diária · dados do Prometheus"},
            "timestamp": now.isoformat(),
        }],
    }


def main():
    body = json.dumps(payload()).encode("utf-8")
    if os.getenv("DRY_RUN"):
        print(json.dumps(json.loads(body), indent=2, ensure_ascii=False), flush=True)
        return
    req = urllib.request.Request(WEBHOOK, data=body, method="POST",
                                 headers={**UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        print(f"discord: HTTP {r.status}", flush=True)


if __name__ == "__main__":
    main()
