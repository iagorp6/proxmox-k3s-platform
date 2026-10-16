"""Exporter do Minecraft: contrato game_*, estado e /metrics vêm da lib comum (gameexporter.py).

Driver rcon: o comando `list` e a regex dos nomes ficam no env do sidecar (statefulset.yaml).
"""
import gameexporter

gameexporter.main()
