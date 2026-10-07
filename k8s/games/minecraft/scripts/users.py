"""Init: gera whitelist.json e ops.json com UUIDs offline (online-mode=false).

A imagem do servidor só resolve nomes de contas originais; com online-mode=false o servidor
identifica cada jogador pelo UUID offline, calculado a partir do nome (diferencia maiúsculas).
Os nomes vêm de PLAYERS e OPERATORS (separados por vírgula). A autenticação real é do EasyAuth.
"""
import hashlib
import json
import os
import pathlib
import uuid


def offline_uuid(name):
    h = bytearray(hashlib.md5(f"OfflinePlayer:{name}".encode("utf-8")).digest())
    h[6] = (h[6] & 0x0F) | 0x30
    h[8] = (h[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(h)))


def names(var):
    return [n.strip() for n in os.environ.get(var, "").split(",") if n.strip()]


data = pathlib.Path("/data")
players, ops = names("PLAYERS"), names("OPERATORS")
everyone = list(dict.fromkeys(players + ops))
(data / "whitelist.json").write_text(json.dumps(
    [{"uuid": offline_uuid(n), "name": n} for n in everyone], indent=2), encoding="utf-8")
(data / "ops.json").write_text(json.dumps(
    [{"uuid": offline_uuid(n), "name": n, "level": 4, "bypassesPlayerLimit": False} for n in ops], indent=2), encoding="utf-8")
print(f"whitelist.json: {len(everyone)} jogadores | ops.json: {len(ops)} operadores")
