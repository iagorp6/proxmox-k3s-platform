# ADR 0002: One active game at a time, switched through Git

- Status: accepted
- Date: 2026-10

## Context
The group never plays two games at once, and the VM only fits one heavy game. Switching must be
safe (no players online, world saved) and auditable.

## Decision
A single file, `platform/active-game.yaml`, names the active game. `scripts/switch-game.py`
(`make switch-game GAME=<name>`) refuses to switch while anyone is online, saves the world, writes
the replica patch of every game (1 for the active one, 0 for the others) together with the
active-game file, commits, and syncs the Argo CD Applications: first the game that stops, then the
one that starts. Discord is notified when the switch begins and when the new game is ready. Game
Applications keep manual sync, so unrelated commits never restart a session.

## Alternatives rejected
- **Runtime ownership of replicas** (a bot scaling StatefulSets, Argo CD ignoring
  `/spec/replicas`): needed only to juggle several games at once; it makes Git stop describing
  what is running.
- **KEDA scale-to-zero:** cannot wake UDP games, since nothing receives the first packet at zero
  replicas.

## Consequences
- Git always describes what is running; every switch has an author and a timestamp.
- Alerts use the desired state (`kube_statefulset_replicas`), so stopped games never page.
- Because only one game runs at a time, games can reuse the ports already forwarded by the
  firewall. The Services of stopped games have no ready endpoints, so a custom Argo CD health
  check treats them as healthy and a `ServiceLBPortConflict` alert covers the unexpected case.
- Switching needs someone with repository and cluster access; self-service for players is not
  implemented.
