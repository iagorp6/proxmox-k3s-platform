#!/usr/bin/env python3
"""Testes do scripts/require-main.sh, só stdlib e git.

Cada teste monta um repositório git temporário com um "origin" local e uma cópia do script em
scripts/, e confere: passa no main igual ao origin; recusa em outro branch, com arquivo versionado
modificado e com o main atrás do origin/main; deixa passar com FORCE=1, com arquivo não versionado,
com commit local à frente (só avisa) e sem acesso ao origin (só avisa).
Uso: python3 scripts/test-require-main.py
"""
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent / "require-main.sh"
# Sem manutenção automática do git nos repositórios temporários: o git novo do runner a dispara
# destacada (em segundo plano) depois de commit/push, ela escreve em objects/pack enquanto o tearDown
# apaga o diretório e o teste falha com "Directory not empty". Vale também para os git do script testado.
os.environ.update({"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "maintenance.auto", "GIT_CONFIG_VALUE_0": "false",
                   "GIT_CONFIG_KEY_1": "gc.auto", "GIT_CONFIG_VALUE_1": "0"})


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class RequireMainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.origin, self.repo, self.other = base / "origin.git", base / "repo", base / "other"
        git(base, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(base, "init", "-q", "-b", "main", str(self.repo))
        self.configure(self.repo)
        (self.repo / "scripts").mkdir()
        shutil.copy(SCRIPT, self.repo / "scripts" / "require-main.sh")
        (self.repo / "Makefile").write_text("all:\n", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "init")
        git(self.repo, "remote", "add", "origin", str(self.origin))
        git(self.repo, "push", "-q", "-u", "origin", "main")

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def configure(repo):
        for k, v in (("user.name", "test"), ("user.email", "test@example.invalid"), ("commit.gpgsign", "false")):
            git(repo, "config", k, v)

    def run_check(self, **env):
        full = {k: v for k, v in os.environ.items() if k != "FORCE"} | env
        # roda de outro diretório, como o Ansible faz (cwd em ansible/)
        return subprocess.run(["bash", str(self.repo / "scripts" / "require-main.sh")], cwd=self.tmp.name,
                              env=full, capture_output=True, text=True)

    def push_from_other_clone(self):
        git(self.tmp.name, "clone", "-q", str(self.origin), str(self.other))
        self.configure(self.other)
        (self.other / "novo.txt").write_text("x\n", encoding="utf-8")
        git(self.other, "add", ".")
        git(self.other, "commit", "-q", "-m", "merge de um PR")
        git(self.other, "push", "-q")

    def test_up_to_date_main_passes(self):
        r = self.run_check()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("OK: clone no main", r.stdout)

    def test_other_branch_is_refused(self):
        git(self.repo, "checkout", "-q", "-b", "dependabot/velho")
        r = self.run_check()
        self.assertEqual(r.returncode, 1)
        self.assertIn("branch 'dependabot/velho'", r.stderr)
        self.assertIn("git checkout main", r.stderr)

    def test_modified_tracked_file_is_refused(self):
        (self.repo / "Makefile").write_text("mudado:\n", encoding="utf-8")
        r = self.run_check()
        self.assertEqual(r.returncode, 1)
        self.assertIn("Makefile", r.stderr)
        self.assertIn("não commitadas", r.stderr)

    def test_untracked_file_is_allowed(self):
        (self.repo / "values-local.yaml").write_text("x: 1\n", encoding="utf-8")
        self.assertEqual(self.run_check().returncode, 0)

    def test_behind_origin_is_refused(self):
        self.push_from_other_clone()
        r = self.run_check()
        self.assertEqual(r.returncode, 1)
        self.assertIn("1 commit(s) atrás", r.stderr)
        self.assertIn("git pull --ff-only", r.stderr)
        git(self.repo, "pull", "-q", "--ff-only")
        self.assertEqual(self.run_check().returncode, 0)

    def test_ahead_of_origin_only_warns(self):
        (self.repo / "local.txt").write_text("x\n", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "local")
        r = self.run_check()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("1 commit(s) que não estão no origin/main", r.stderr)

    def test_unreachable_origin_only_warns(self):
        git(self.repo, "remote", "set-url", "origin", str(pathlib.Path(self.tmp.name) / "sumiu.git"))
        r = self.run_check()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("não consegui falar com o origin", r.stderr)

    def test_force_skips_everything(self):
        git(self.repo, "checkout", "-q", "-b", "outro")
        r = self.run_check(FORCE="1")
        self.assertEqual(r.returncode, 0)
        self.assertIn("FORCE=1", r.stderr)


if __name__ == "__main__":
    unittest.main()
