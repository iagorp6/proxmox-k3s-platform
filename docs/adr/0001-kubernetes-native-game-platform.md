# ADR 0001: Kubernetes-native game platform (no Agones, no game panel)

- Status: accepted
- Date: 2026-10

## Context
The homelab hosts persistent-world games (Project Zomboid, Terraria, Minecraft) for a small group,
one game at a time, on a single K3s node (4 cores). The first deployment, Project Zomboid, already
had GitOps, observability, secrets and backups in place.

## Options considered
1. **Agones.** Built for fleets of short-lived sessions with allocation and matchmaking. Persistent
   worlds would sit "Allocated" indefinitely, StatefulSet semantics (stable identity,
   volumeClaimTemplates, ordered shutdown) would be lost, every game would need SDK integration,
   and the control plane competes for the same 4 cores.
2. **Game panel (Pelican, Pterodactyl, AMP).** State lives in the panel database instead of Git,
   Wings needs its own Docker daemon and iptables rules next to K3s, and backups and observability
   would be duplicated.
3. **Plain Kubernetes.** One StatefulSet per game in its own namespace, reusing the existing
   GitOps, observability, secrets and backup stack.

## Decision
Option 3. Every game is described by a catalog entry (`k8s/games/<game>/game.yaml`) and reconciled
by Argo CD. How the shared manifests are produced is covered by ADR 0003 and ADR 0004.

## Consequences
- Adding a game is a pull request, not a new tool to operate.
- No wake-on-connect for UDP games; switching games is explicit (see ADR 0002).
- Agones remains a possible spike if the platform ever needs session-based games.
