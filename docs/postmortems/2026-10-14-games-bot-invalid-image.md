# Discord bot stuck in `InvalidImageName` after an image bump

- **Date:** 2026-10-14 · **Duration:** about 1h, 17:01 to about 18:06 (America/Sao_Paulo) · **Severity:** feature unavailable, no game downtime
- **Detected by:** a person noticing the panel stopped responding; no alert covered the bot yet

## Summary

A pull request bumped the games-bot image to the new panel version, but the image reference was
committed as `ghcr.io/iagorp6/games-bot:f85fc5b11e57@`, a tag followed by an empty digest. Argo CD
synced it automatically, the Deployment's `Recreate` strategy removed the running bot first, and the new
pod could never start: `InvalidImageName`. A follow-up PR with the full `tag@sha256:...` reference
fixed it.

## Impact

- The `#games` panel stopped answering: no game switch, queue or status from Discord for about an hour.
- `make switch-game` still worked for the admin, and the active game kept running. No SLO impact.

## Timeline (America/Sao_Paulo)

| Time | Event |
|---|---|
| 17:01 | #60 merged: image `f85fc5b11e57@` (digest missing) and a new `ADMIN_NAME` variable |
| ~17:04 | Argo CD auto-syncs `platform`; `Recreate` terminates the old bot; the new pod fails with `InvalidImageName` |
| (to confirm) | Panel noticed unresponsive |
| 18:05 | #61 merged with `f85fc5b11e57@sha256:3476a3a0...` |
| ~18:06 | Argo CD syncs; the bot starts and redraws the panel |
| 18:09 | #62 merged: CI fails on any image not pinned as `tag@sha256:<64 hex>` |
| 18:14 | #63 merged: `GamesBotDown` alert |

## Root cause

The digest of a new image is only known after the image workflow publishes it, so the bump was written
in two steps and the second step was missed. Nothing in CI parsed image references: kubeconform
validates the schema, and `image` is just a string. With `Recreate` (chosen on purpose, two bots would
draw two panels and run concurrent switches) the old pod is gone before the new one proves it can
start, so a bad reference becomes an outage instead of a stuck rollout.

## What went well

- Git history made the cause obvious: one line changed.
- The fix and both prevention PRs landed within the hour.

## What went badly

- No alert: the bot had no availability signal, so detection depended on someone using the panel.
- The `platform` app auto-syncs, so a bad manifest goes live seconds after merge.

## Action items

| Action | Type | Status |
|---|---|---|
| CI fails on images not pinned as `tag@sha256:<64 hex>` (`render-games.py --check`) | prevent | done (#62) |
| `GamesBotDown`: Deployment without available replicas for 10 min | detect | done (#63) |
| `ArgoAppDegraded` alert, which would also have caught the stuck Deployment | detect | done (#88) |
| Renovate opens image bumps with tag and digest together, so nobody writes a digest by hand | prevent | open (Renovate app not installed) |
