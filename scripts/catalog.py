#!/usr/bin/env python3
"""Catálogo de jogos da plataforma: a única lista de jogos do repositório.

Lê k8s/games/<jogo>/game.yaml (jogos com manifests gerados, status "active") e
k8s/<jogo>/game.yaml (jogos ainda com manifests escritos à mão, status "legacy"). O gerador de
manifests, o dashboard, a troca de jogo, o status e o CI consultam este módulo em vez de manter
listas próprias: adicionar um jogo é criar o diretório dele com um game.yaml.

Uso como comando:
  scripts/catalog.py dirs        diretórios kustomize dos jogos, um por linha
  scripts/catalog.py workloads   "<namespace> <statefulset>" por linha
  scripts/catalog.py check       valida o catálogo e o platform/active-game.yaml
"""
import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
ACTIVE_FILE = ROOT / "platform" / "active-game.yaml"
REQUIRED = ("name", "displayName", "status", "namespace", "ports")
DEPLOYED = ("active", "legacy")


def load():
    """Todas as entradas do catálogo, em ordem alfabética, com "dir" (relativo à raiz)."""
    entries = []
    for f in [*ROOT.glob("k8s/*/game.yaml"), *ROOT.glob("k8s/games/*/game.yaml")]:
        g = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        g["dir"] = f.parent.relative_to(ROOT).as_posix()
        entries.append(g)
    return sorted(entries, key=lambda g: str(g.get("name", "")))


def games():
    """Jogos implantados no cluster (gerados ou legados)."""
    return [g for g in load() if g.get("status") in DEPLOYED]


def active_game():
    if not ACTIVE_FILE.exists():
        return None
    m = re.search(r"^active:\s*(\S+)", ACTIVE_FILE.read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else None


def problems():
    """Lista de erros do catálogo (vazia se estiver tudo certo)."""
    errors, seen = [], {}
    for g in load():
        where = f"{g['dir']}/game.yaml"
        missing = [k for k in REQUIRED if not g.get(k)]
        if missing:
            errors.append(f"{where}: faltam os campos {', '.join(missing)}")
            continue
        if g["name"] in seen:
            errors.append(f"{where}: nome '{g['name']}' repetido (já usado em {seen[g['name']]})")
        seen[g["name"]] = where
        # Dashboard e alertas derivam o jogo do namespace: os dois precisam ser iguais.
        if g["namespace"] != g["name"]:
            errors.append(f"{where}: namespace '{g['namespace']}' precisa ser igual ao nome '{g['name']}'")
        if g["status"] == "active" and not g["dir"].startswith("k8s/games/"):
            errors.append(f"{where}: jogo com status active precisa ficar em k8s/games/")
    active = active_game()
    names = [g["name"] for g in games()]
    if active not in (None, "none") and active not in names:
        errors.append(f"platform/active-game.yaml: '{active}' não é um jogo implantado ({', '.join(names)})")
    return errors


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "dirs":
        print("\n".join(g["dir"] for g in games()))
    elif cmd == "workloads":
        print("\n".join(f"{g['namespace']} {g['name']}" for g in games()))
    elif cmd == "check":
        errors = problems()
        if errors:
            print("ERRO: catálogo de jogos inválido:\n" + "\n".join(f"  {e}" for e in errors))
            return 1
        print(f"OK: {len(games())} jogos no catálogo ({', '.join(g['name'] for g in games())}); ativo: {active_game()}")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
