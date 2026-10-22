# ADR 0009: Auto-sync for third-party charts, manual sync for games

- Status: accepted (supersedes the "sync stays manual" point of [ADR 0007](0007-helm-releases-under-argocd.md))
- Date: 2026-10

## Context
ADR 0007 moved kube-prometheus-stack, Loki, Alloy and External Secrets under Argo CD with manual
sync. The reason was that a broken values file or an incompatible chart upgrade only showed up when
the chart rendered in the cluster, so a person had to watch each Sync. In practice it also meant
that a merged change did nothing until someone remembered to press Sync, and twice a merged change
sat undeployed while the alerts that depended on it fired.

Since then, CI renders every pinned chart with the repository's values and validates the output
with kubeconform (`scripts/check-charts.py`). A broken value or a chart that no longer accepts a
key now fails the pull request, not the Sync. The review and CI on `main` became the gate the manual
Sync used to be.

The game servers are different. Their manifests can be correct and still not be safe to apply:
any change restarts the pod and disconnects whoever is playing, and a game switch has to happen in
order (save, stop the old game, start the new one), which `scripts/switch-game.py` drives app by
app. Whether it is a good moment depends on who is online, which Git does not know.

## Decision
- **Charts auto-sync** with `selfHeal: true` and a retry with backoff (the first sync after a
  rebuild races CRDs and webhooks between apps).
- **`prune: false` on charts.** A resource that disappears in a new chart version leaves the app
  `OutOfSync`; `ArgoAppOutOfSync` fires after 30 minutes and someone runs a Sync with prune,
  reviewing what gets deleted. Automatic pruning of operator resources (CRDs included) is the one
  failure that is hard to undo.
- **Games stay manual.** `switch-game.py` (from the terminal or the Discord bot) is the only thing
  that syncs a game app, so a restart happens through the path that checks for players and saves
  the world first. A manifest change to the active game goes live with the next switch or a manual
  Sync at a quiet moment.
- **Argo CD stays on Helm** (`make deploy-argocd`), as in ADR 0007.

## Consequences
- Merging to `main` deploys everything except the game servers and Argo CD itself. Renovate chart
  bumps still never auto-merge, so the PR review is where an upgrade is accepted.
- A chart upgrade that passes CI but fails in the cluster surfaces as `ArgoAppDegraded` or, for
  External Secrets, `ExternalSecretNotReady`, instead of waiting for someone to open the UI.
- Monitoring changes need nothing after the merge. The DR runbook no longer has a manual
  Sync order for the charts; Argo CD converges them with retries.
- A kube-prometheus-stack upgrade restarts Prometheus and Alertmanager for a few seconds at merge
  time. The dead man's switch covers the case where they do not come back.
