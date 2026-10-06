"""Init container: prepara os diretórios no volume e grava/atualiza o config.json do TShock.

Só as chaves gerenciadas pela plataforma são escritas; o TShock completa o resto com os
padrões dele na primeira subida e preserva o que já existir.
"""
import json
import os
import pathlib

DATA = pathlib.Path("/data")
for sub in ("tshock", "worlds", "plugins"):
    (DATA / sub).mkdir(parents=True, exist_ok=True)

cfg_path = DATA / "tshock" / "config.json"
cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
settings = cfg.setdefault("Settings", {})
settings.update({
    "ServerPassword": os.environ["SERVER_PASSWORD"],
    "ServerPort": 7777,
    "MaxSlots": int(os.environ.get("MAX_SLOTS", "6")),
    "RestApiEnabled": False,
    "SoftcoreOnly": True,   # só personagens Clássicos: morrer perde metade do dinheiro, não os itens
})
cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
print(f"config.json atualizado ({len(settings)} chaves em Settings)")
