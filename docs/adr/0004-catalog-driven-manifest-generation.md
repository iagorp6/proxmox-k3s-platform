# ADR 0004: Generate the shared game manifests from the catalog

- Status: accepted (refines ADR 0003)
- Date: 2026-10

## Context
After two games (Project Zomboid, Terraria), the shared pieces are clear: the game Service, the
metrics Service and ServiceMonitor, the restic backup (ExternalSecret + CronJob) and the Argo CD
Application. What changes between games is data: names, ports, backup paths, schedules and the
runtime user. Kustomize components cannot template names and ports without a pile of patches and
replacements that are hard to read and review.

## Decision
Each game declares its needs in `k8s/games/<game>/game.yaml`. `scripts/render-games.py` renders the
shared manifests into `platform.generated.yaml` and the Argo CD Application. The StatefulSet and
game-specific Secrets stay hand-written, because that is where games genuinely differ. CI runs the
generator with `--check` and fails if a generated file is stale, the same pattern used for the
generated Grafana dashboard.

## Consequences
- Adding a game is a catalog entry, a StatefulSet and its own Secrets; everything else is uniform.
- Generated files are plain YAML in the repository, so reviews and Argo CD diffs stay readable.
- Migrations are verified with `scripts/manifest-diff.py`, a semantic comparison of `kustomize`
  output before and after (Terraria migrated with no effective change).
- Project Zomboid was migrated the same way (2026-10): the only effective changes were the backup
  (now generated, with the pre-backup save hook) and an extra pod label for the generated Services.
