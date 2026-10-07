#!/usr/bin/env python3
"""Valida e testa as regras de alerta com o promtool.

O promtool lê arquivos de regras do Prometheus, não o recurso PrometheusRule. Este script extrai o
.spec de cada k8s/monitoring/alerts-*.yaml para um diretório temporário e então:
  1. confere que todo alerta critical ou warning tem summary, description e action
     (a "primeira ação" que vai junto na mensagem do Discord);
  2. roda `promtool check rules` em todas as regras (sintaxe e PromQL);
  3. roda `promtool test rules` nos testes de k8s/monitoring/tests/*.test.yaml, que simulam
     séries e conferem quando cada alerta dispara, com quais labels e com qual texto.
Uso: python3 scripts/test-alerts.py
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
MONITORING = ROOT / "k8s" / "monitoring"
REQUIRED_ANNOTATIONS = ("summary", "description", "action")


def main():
    promtool = shutil.which("promtool")
    if not promtool:
        print("ERRO: promtool não encontrado (instale com scripts/install-ci-tools.sh <diretório> promtool)")
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        rule_files, alerts, problems = [], 0, []
        for f in sorted(MONITORING.glob("alerts-*.yaml")):
            doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            if doc.get("kind") != "PrometheusRule":
                continue
            for group in doc["spec"]["groups"]:
                for rule in group["rules"]:
                    if "alert" not in rule:
                        continue
                    alerts += 1
                    if rule.get("labels", {}).get("severity") in ("critical", "warning"):
                        missing = [a for a in REQUIRED_ANNOTATIONS if not rule.get("annotations", {}).get(a)]
                        if missing:
                            problems.append(f"{f.name}: {rule['alert']} sem {', '.join(missing)}")
            out = tmp / f.name
            out.write_text(yaml.safe_dump(doc["spec"], sort_keys=False, allow_unicode=True, width=1000), encoding="utf-8")
            rule_files.append(out)
        tests = []
        for f in sorted((MONITORING / "tests").glob("*.test.yaml")):
            shutil.copy(f, tmp / f.name)
            tests.append(tmp / f.name)
        if not rule_files or not tests:
            print("ERRO: nenhuma regra ou nenhum teste encontrado em k8s/monitoring")
            return 1
        if problems:
            print("ERRO: alertas sem as anotações obrigatórias:\n" + "\n".join(f"  {p}" for p in problems))
            return 1
        for cmd in ([promtool, "check", "rules", *map(str, rule_files)],
                    [promtool, "test", "rules", *map(str, tests)]):
            result = subprocess.run(cmd, cwd=tmp, text=True, capture_output=True)
            if result.returncode != 0:
                print((result.stdout + result.stderr).strip())
                print(f"ERRO: promtool {cmd[1]} rules falhou")
                return 1
    print(f"OK: {alerts} alertas em {len(rule_files)} arquivos, {len(tests)} arquivos de teste passaram")
    return 0


if __name__ == "__main__":
    sys.exit(main())
