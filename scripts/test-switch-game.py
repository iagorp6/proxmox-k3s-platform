#!/usr/bin/env python3
"""Testes da troca de jogo (scripts/switch-game.py), só stdlib e git.

Cada teste roda o main() de verdade num repositório git temporário com um "origin" local: confere
os patches de réplicas, o commit (mensagem e só os arquivos permitidos), o push e as recusas
(jogadores online, contagem indisponível, árvore suja, arquivo fora do ALLOWED_PATHS). kubectl e
Argo CD ficam de fora: players_online, save_world e apply_switch são trocados por fakes.
Uso: python3 scripts/test-switch-game.py
"""
import contextlib
import importlib.util
import io
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("switch_game", ROOT / "scripts" / "switch-game.py")
sw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sw)

STS_WITH_NS = "apiVersion: apps/v1\nkind: StatefulSet\nmetadata:\n  name: minecraft\n  namespace: minecraft\n"
STS_NO_NS = "apiVersion: apps/v1\nkind: StatefulSet\nmetadata:\n  name: terraria\n"


def game(name, dir_, suspend=()):
    return {"title": name.title(), "port": 25565, "dir": dir_, "ns": name, "sts": name, "app": name,
            "players_metric": "game_players_online", "exporter": "exporter", "suspend_when_idle": list(suspend)}


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class SwitchGameTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.origin, self.repo = base / "origin.git", base / "repo"
        git(base, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(base, "init", "-q", "-b", "main", str(self.repo))
        for k, v in (("user.name", "test"), ("user.email", "test@example.invalid"), ("commit.gpgsign", "false")):
            git(self.repo, "config", k, v)
        files = {
            "platform/active-game.yaml": "# jogo ativo\nactive: minecraft\n",
            "k8s/games/minecraft/statefulset.yaml": STS_WITH_NS,
            "k8s/games/terraria/statefulset.yaml": STS_NO_NS,
            "games-fora/statefulset.yaml": STS_NO_NS,
        }
        for rel, text in files.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text, encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "init")
        git(self.repo, "remote", "add", "origin", str(self.origin))
        git(self.repo, "push", "-q", "-u", "origin", "main")

        self.saved = {k: getattr(sw, k) for k in ("ROOT", "ACTIVE_FILE", "GAMES", "players_online",
                                                    "save_world", "apply_switch", "notify")}
        sw.ROOT, sw.ACTIVE_FILE = self.repo, self.repo / "platform" / "active-game.yaml"
        sw.GAMES = {"minecraft": game("minecraft", "k8s/games/minecraft", ["minecraft-backup"]),
                    "terraria": game("terraria", "k8s/games/terraria")}
        self.players, self.saves, self.applied = 0, [], []
        sw.players_online = lambda g: self.players
        sw.save_world = lambda g: self.saves.append(g["sts"])
        sw.apply_switch = lambda prev, target, sha: self.applied.append((prev, target, sha))
        sw.notify = lambda *a, **kw: None

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(sw, k, v)
        self.tmp.cleanup()

    def main(self, *args):
        old, sys.argv = sys.argv, ["switch-game.py", *args]
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = sw.main()
            return code, out.getvalue()
        finally:
            sys.argv = old

    def refused(self, *args):
        with self.assertRaises(SystemExit) as ctx:
            self.main(*args)
        return str(ctx.exception.code)

    def commits(self):
        return int(git(self.repo, "rev-list", "--count", "HEAD"))

    def read(self, rel):
        return (self.repo / rel).read_text(encoding="utf-8")

    def test_patches_follow_the_active_game(self):
        self.main("terraria", "--dry-run")
        self.assertEqual(self.read("k8s/games/terraria/replicas.yaml"),
                         "# Gerado por scripts/switch-game.py: não edite à mão.\napiVersion: apps/v1\nkind: StatefulSet\n"
                         "metadata:\n  name: terraria\nspec:\n  replicas: 1\n")
        mc = self.read("k8s/games/minecraft/replicas.yaml")
        # namespace só quando o StatefulSet do jogo declara um; CronJobs do jogo parado ficam suspensos
        self.assertIn("  name: minecraft\n  namespace: minecraft\nspec:\n  replicas: 0\n", mc)
        self.assertIn("kind: CronJob\nmetadata:\n  name: minecraft-backup\n  namespace: minecraft\nspec:\n  suspend: true\n", mc)
        self.assertEqual(self.read("platform/active-game.yaml"), "# jogo ativo\nactive: terraria\n")

    def test_dry_run_does_not_commit(self):
        self.main("terraria", "--dry-run")
        self.assertEqual(self.commits(), 1)
        self.assertEqual(self.applied, [])

    def test_switch_commits_only_allowed_files_and_pushes(self):
        code, out = self.main("terraria", "--no-notify", "--requester", "  Ana \n  Maria  ")
        self.assertEqual(code, 0)
        self.assertIn("OK: terraria é o jogo ativo.", out)
        self.assertEqual(git(self.repo, "log", "-1", "--format=%s"),
                         "platform: switch active game minecraft -> terraria (pedido por Ana Maria via Discord)")
        self.assertEqual(sorted(git(self.repo, "show", "--name-only", "--format=", "HEAD").split()),
                         ["k8s/games/minecraft/replicas.yaml", "k8s/games/terraria/replicas.yaml",
                          "platform/active-game.yaml"])
        sha = git(self.repo, "rev-parse", "HEAD")
        self.assertEqual(git(self.origin, "rev-parse", "main"), sha)
        self.assertEqual(self.saves, ["minecraft"])
        self.assertEqual(self.applied, [("minecraft", "terraria", sha)])

    def test_requester_is_cut_to_40_characters(self):
        self.main("terraria", "--no-notify", "--requester", "x" * 60)
        self.assertIn(f"(pedido por {'x' * 40} via Discord)", git(self.repo, "log", "-1", "--format=%s"))

    def test_players_online_block_the_switch(self):
        self.players = 2
        self.assertIn("2 jogador(es) online em minecraft", self.refused("terraria", "--no-notify"))
        self.assertEqual(self.commits(), 1)
        self.assertEqual(self.saves, [])

    def test_force_switches_with_players_online(self):
        self.players = 2
        code, _ = self.main("terraria", "--no-notify", "--force")
        self.assertEqual(code, 0)
        self.assertEqual(self.commits(), 2)

    def test_bot_refuses_without_player_count(self):
        self.players = None
        self.assertIn("não consegui contar os jogadores de minecraft",
                      self.refused("terraria", "--no-notify", "--require-player-count"))
        self.assertEqual(self.commits(), 1)

    def test_terminal_switches_without_player_count(self):
        self.players = None
        code, out = self.main("terraria", "--no-notify")
        self.assertEqual(code, 0)
        self.assertIn("contagem de jogadores indisponível", out)

    def test_dirty_tree_is_refused(self):
        (self.repo / "README.md").write_text("rascunho\n", encoding="utf-8")
        self.assertIn("mudanças não commitadas", self.refused("terraria", "--no-notify"))
        self.assertEqual(self.commits(), 1)

    def test_file_outside_allowed_paths_is_never_committed(self):
        # Um game.yaml com dir fora de k8s/ geraria réplicas num caminho que a deploy key não deve tocar
        sw.GAMES["terraria"] = game("terraria", "games-fora")
        self.assertIn("recusado: games-fora/replicas.yaml", self.refused("terraria", "--no-notify"))
        self.assertEqual(self.commits(), 1)
        self.assertEqual(git(self.repo, "diff", "--cached", "--name-only"), "")
        self.assertEqual(git(self.origin, "rev-list", "--count", "main"), "1")

    def test_same_game_is_a_no_op(self):
        code, out = self.main("minecraft", "--no-notify")
        self.assertEqual(code, 0)
        self.assertIn("Nada a fazer", out)
        self.assertEqual(self.commits(), 1)


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False, verbosity=1).result.wasSuccessful() else 1)
