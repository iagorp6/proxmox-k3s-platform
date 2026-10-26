#!/usr/bin/env python3
"""Testes da checagem diária (k8s/platform/daily_health.py), só stdlib.

Troca a consulta ao Prometheus (q) por respostas fixas por PromQL e confere a mensagem do Discord:
tudo OK, cada ponto de atenção, os restarts do reboot de patch e, principalmente, que uma falha
do Prometheus vira mensagem de erro em vez de silêncio. Uso: python3 scripts/test-daily-health.py
"""
import importlib.util
import os
import pathlib
import sys
import unittest
import urllib.error

ROOT = pathlib.Path(__file__).resolve().parent.parent
os.environ.setdefault("DISCORD_WEBHOOK_URL", "https://discord.invalid/webhook")
os.environ["GAMES_FILE"] = str(ROOT / "k8s" / "platform" / "games.generated.json")
spec = importlib.util.spec_from_file_location("daily_health", ROOT / "k8s" / "platform" / "daily_health.py")
dh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dh)

H = 3600


def sample(value, **labels):
    return {"metric": labels, "value": [0, str(value)]}


def healthy():
    """Respostas de um dia normal: Minecraft no ar, backup de 5 h, discos folgados, nada reiniciou."""
    return {
        dh.ALERTS: [],
        dh.GAME_UP: [sample(1, game="minecraft")],
        dh.BACKUP_AGE: [sample(5 * H, cronjob="minecraft-backup"), sample(200 * H, cronjob="zomboid-backup")],
        dh.DISK_FREE: [sample(0.42, job="node-exporter"), sample(0.60, job="proxmox-node")],
        dh.RESTARTS: [],
        dh.HOST_UPTIME: [sample(30 * 24 * H)],
        dh.VM_UPTIME: [sample(10 * 24 * H)],
        dh.HOST_SECURITY: [sample(0)],
        dh.HOST_REBOOT: [sample(0)],
        dh.DISK_BUSY_P99: [sample(0.12, device="sdb"), sample(0.11, device="sda")],
        dh.ZFS_SCRUB_AGE: [sample(14 * 24 * H)],
        dh.ZFS_ERRORS: [sample(0)],
    }


class DailyHealthTest(unittest.TestCase):
    def run_with(self, answers):
        original = dh.q
        dh.q = lambda expr: answers[expr]
        try:
            return dh.payload()["embeds"][0]
        finally:
            dh.q = original

    @staticmethod
    def field(embed, name):
        return next(f["value"] for f in embed["fields"] if f["name"] == name)

    def test_all_good_is_a_green_ok(self):
        e = self.run_with(healthy())
        self.assertEqual(e["title"], "✅ Tudo OK no AsunBoid")
        self.assertEqual(self.field(e, "🎮 Jogo"), "🟢 Minecraft")
        # O backup de um jogo parado (zomboid, 200 h) não conta: só o do jogo ativo faz backup diário
        self.assertEqual(self.field(e, "💾 Último backup"), "🟢 Minecraft · há 5 h")
        self.assertEqual(self.field(e, "⏱️ Host no ar"), "30 dias")
        self.assertEqual(self.field(e, "🩹 Patches do host"), "Em dia")
        self.assertEqual(self.field(e, "💽 Discos do host (p99, 7 dias)"), "sda 11% · sdb 12%")

    def test_each_problem_is_listed(self):
        a = healthy()
        a[dh.ALERTS] = [sample(1, alertname="GameUnresponsive", game="minecraft"),
                        sample(1, alertname="HostDiskSpaceLow")]
        a[dh.GAME_UP] = [sample(0, game="minecraft")]
        a[dh.BACKUP_AGE] = [sample(30 * H, cronjob="minecraft-backup")]
        a[dh.DISK_FREE] = [sample(0.10, job="node-exporter")]
        a[dh.RESTARTS] = [sample(3, namespace="minecraft", pod="minecraft-0")]
        e = self.run_with(a)
        self.assertEqual(e["title"], "⚠️ 5 ponto(s) de atenção")
        self.assertEqual(e["description"].splitlines(), [
            "• 2 alerta(s) disparando",
            "• Minecraft fora do ar",
            "• backup de Minecraft atrasado",
            "• disco (VM) com 10% livre",
            "• 1 pod(s) reiniciaram",
        ])
        self.assertIn("GameUnresponsive (minecraft)", self.field(e, "🚨 Alertas"))
        self.assertEqual(self.field(e, "🔁 Restarts em 24 h"), "minecraft/minecraft-0 · 3x")

    def test_no_game_answering_is_a_problem(self):
        a = healthy()
        a[dh.GAME_UP] = []
        e = self.run_with(a)
        self.assertIn("• nenhum jogo respondendo", e["description"])
        self.assertEqual(self.field(e, "🎮 Jogo"), "🔴 Nenhum no ar")

    def test_restarts_after_patch_reboot_are_not_a_problem(self):
        a = healthy()
        a[dh.VM_UPTIME] = [sample(3 * H)]
        a[dh.RESTARTS] = [sample(1, namespace="monitoring", pod="prometheus-0")]
        e = self.run_with(a)
        self.assertEqual(e["title"], "✅ Tudo OK no AsunBoid")
        self.assertEqual(self.field(e, "⏱️ VM no ar"), "3 h (reiniciou por patch)")
        self.assertEqual(self.field(e, "🔁 Restarts em 24 h"), "monitoring/prometheus-0 · 1x")

    def test_pending_host_patches_are_informative(self):
        a = healthy()
        a[dh.HOST_SECURITY] = [sample(3)]
        a[dh.HOST_REBOOT] = [sample(1)]
        e = self.run_with(a)
        self.assertEqual(e["title"], "✅ Tudo OK no AsunBoid")
        self.assertEqual(self.field(e, "🩹 Patches do host"), "3 de segurança pendente(s) · reboot pendente")

    def test_disk_load_is_informative_and_optional(self):
        a = healthy()
        a[dh.DISK_BUSY_P99] = [sample(0.95, device="sda"), sample(0.90, device="sdb")]
        e = self.run_with(a)
        self.assertEqual(e["title"], "✅ Tudo OK no AsunBoid")
        a[dh.DISK_BUSY_P99] = []
        e = self.run_with(a)
        self.assertNotIn("💽 Discos do host (p99, 7 dias)", [f["name"] for f in e["fields"]])

    def test_zfs_scrub_and_errors_are_informative_and_optional(self):
        e = self.run_with(healthy())
        self.assertEqual(self.field(e, "🧽 ZFS"), "scrub há 14 dias · sem erros")
        a = healthy()
        a[dh.ZFS_ERRORS] = [sample(3)]
        e = self.run_with(a)
        self.assertEqual(e["title"], "✅ Tudo OK no AsunBoid")
        self.assertEqual(self.field(e, "🧽 ZFS"), "scrub há 14 dias · 3 erro(s) nos discos")
        a[dh.ZFS_SCRUB_AGE], a[dh.ZFS_ERRORS] = [], []
        e = self.run_with(a)
        self.assertNotIn("🧽 ZFS", [f["name"] for f in e["fields"]])

    def test_prometheus_down_is_never_silence(self):
        original = dh.q

        def down(_expr):
            raise urllib.error.URLError("connection refused")
        dh.q = down
        try:
            e = dh.payload()["embeds"][0]
        finally:
            dh.q = original
        self.assertEqual(e["title"], "❌ Checagem diária falhou")
        self.assertIn("make status", e["description"])
        self.assertIn("URLError", e["fields"][0]["value"])


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False, verbosity=1).result.wasSuccessful() else 1)
