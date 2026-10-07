#!/usr/bin/env python3
"""Compara semanticamente o `kubectl kustomize` de dois diretórios (ex.: main vs branch).

Ignora diferenças só de espaçamento/quebra de linha em strings (ex.: scripts em args).
Uso: scripts/manifest-diff.py <dir_antes> <dir_depois>   (sai com 1 se houver diferenças)
"""
import difflib
import json
import re
import subprocess
import sys

import yaml


def build(directory):
    out = subprocess.run(["kubectl", "kustomize", directory], capture_output=True, text=True, check=True).stdout
    return {(o["kind"], o["metadata"].get("namespace", ""), o["metadata"]["name"]): o
            for o in yaml.safe_load_all(out) if o}


def norm(o):
    if isinstance(o, dict):
        return {k: norm(v) for k, v in o.items()}
    if isinstance(o, list):
        return [norm(v) for v in o]
    if isinstance(o, str):
        return re.sub(r"\s+", " ", o.replace("\\\n", " ")).strip()
    return o


def main():
    before, after = build(sys.argv[1]), build(sys.argv[2])
    changes = 0
    for key in sorted(set(before) | set(after)):
        name = "/".join(k for k in key if k)
        if key not in before:
            print(f"+ novo: {name}"); changes += 1
        elif key not in after:
            print(f"- removido: {name}"); changes += 1
        elif norm(before[key]) != norm(after[key]):
            changes += 1
            print(f"~ alterado: {name}")
            a = json.dumps(norm(before[key]), indent=1, sort_keys=True, ensure_ascii=False).splitlines()
            b = json.dumps(norm(after[key]), indent=1, sort_keys=True, ensure_ascii=False).splitlines()
            for line in difflib.unified_diff(a, b, lineterm="", n=1):
                if not line.startswith(("---", "+++")):
                    print("    " + line)
    print("EQUIVALENTES" if changes == 0 else f"{changes} recurso(s) com diferença")
    return 0 if changes == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
