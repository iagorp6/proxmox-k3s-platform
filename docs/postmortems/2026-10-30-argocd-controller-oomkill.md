# Argo CD application controller OOMKilled after a burst of merges

- **Date:** 2026-10-30 · **Duration:** one morning, fix merged at 10:50 (America/Sao_Paulo) · **Severity:** GitOps degraded, no player impact
- **Detected by:** `ContainerOOMKilled`, after the kill

## Summary

Seven pull requests were merged in 75 minutes. Each merge makes Argo CD refresh its applications, and the
application controller keeps a cache of every resource in the cluster, which had grown with the new games,
AppProjects and Pod Security guardrails added the day before. It went over its 768Mi limit and was
OOMKilled. The limit was raised to 1Gi from measured usage, and a new alert now warns before a container
reaches its limit instead of after.

## Impact

- While the controller restarted, reconciliation paused: merged changes reached the cluster late.
- Running workloads were not affected. Argo CD going down does not stop pods, and game servers sync only
  through the game switch.
- The only signal came after the kill. Outside the game servers, no alert looked at memory against the
  limit, so the same thing could happen to Prometheus, Loki or Grafana without warning.

## Timeline (America/Sao_Paulo)

| Time | Event |
|---|---|
| 2026-10-29 17:15 to 18:00 | Valheim added to the catalog, AppProjects, Pod Security labels and per-game egress policies merged (#123 to #126): more Applications and more resources in the controller's cache |
| 2026-10-30 09:24 to 10:36 | Seven merges (#127 to #133) |
| 2026-10-30, before 10:50 | `application-controller` OOMKilled at 768Mi, `ContainerOOMKilled` fires |
| 2026-10-30 10:50 | #134: limit 1Gi, request 384Mi, applied with `make deploy-argocd` (Argo CD is installed by Helm, not by itself) |
| 2026-10-30 13:55 | #136: `ContainerMemoryNearLimit` (over 90% of the limit for 30 minutes, every non-game container) |

## Root cause

The controller's memory follows the size of the cluster state it watches, not the traffic. The 768Mi limit
was sized when the cluster had fewer Applications and no guardrail objects, and nothing re-checked it as
the platform grew. A burst of syncs was the peak that crossed it. The alerting gap was older: the memory
warning existed only for game servers (`GameMemoryHigh`), and every other component was covered only by
the after-the-fact OOM alert added in the [Grafana postmortem](grafana-oomkill.md).

## What went well

- `ContainerOOMKilled`, the action item of the previous OOM postmortem, caught it right away.
- The fix was one values change, reviewed and applied the same morning.

## What went badly

- The second OOM of a monitoring/platform component in a month. The first one fixed the detection, not the
  missing early warning.

## Action items

| Action | Type | Status |
|---|---|---|
| Controller limit 1Gi, request 384Mi, with the reason in `k8s/argocd/values-argocd.yaml` | mitigate | done (#134) |
| `ContainerMemoryNearLimit`: warning in `#ops` when any non-game container stays above 90% of its memory limit for 30 minutes, with a promtool test and the file to change in the action | detect | done (#136) |
| If it warns again, measure the peak and exclude noisy resource kinds from the controller cache (`resource.exclusions`) before raising the limit further | follow-up | open, waiting for data |
