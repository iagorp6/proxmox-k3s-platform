"""Exporter do Valheim: contrato game_*, estado e /metrics vêm da lib comum (gameexporter.py).

O driver sai de GAME_DRIVER no StatefulSet (a2s). Para um protocolo próprio (REST, log...),
troque a última linha por gameexporter.main(players=minha_funcao): ela devolve a lista de nomes
conectados, ou None se o servidor não respondeu.
"""
import gameexporter

gameexporter.main()
