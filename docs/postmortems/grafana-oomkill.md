# Grafana OOMKilled 15 times in a day, silently

- **Date:** 2026-10-01 to 2026-10-02 · **Duration:** about a day of repeated restarts, 13:31 to 13:34 the next day (America/Sao_Paulo) · **Severity:** monitoring degraded, no player impact
- **Detected by:** chance, while working on the dashboard; no alert fired

## Summary

Grafana's container memory limit had been set too low from the start. A heavier version of the
AsunBoid dashboard pushed it over the limit on every load, and Kubernetes killed and restarted it 15
times in one day. Every restart was quick enough that nobody noticed, and no alert covered OOM kills or
restarts of monitoring components.

## Impact

- Dashboards failed to load or reset mid-session, intermittently, for about a day.
- Alerting was not affected (Prometheus and Alertmanager are separate pods), and no player noticed.
- The real impact was the blind spot: any workload could have been OOM-looping the same way.

## Timeline

Times in America/Sao_Paulo, from the commit history. When the restarts were first noticed was not
recorded; it was some time before the fix.

| Time | Event |
|---|---|
| 2026-10-01 13:31 | `48230c1`: AsunBoid dashboard v2 (plus Loki and Alloy) deployed |
| next ~24h | Grafana restarts 15 times, reason `OOMKilled` |
| 2026-10-02, before 13:34 | Restart count noticed in `kubectl get pods -n monitoring` |
| 2026-10-02 13:34 | `d8ad4a4`: Grafana memory limit raised to 768Mi |
| 2026-10-02 14:11 | `c299aee`: alerting review adds `ContainerOOMKilled` |

## Root cause

The limit was a guess, not a measurement, and the guess only held for a light dashboard. The
alert rules at the time only watched the game servers (`GameRestarted`, `GameMemoryHigh`), so a crash
loop anywhere else in the cluster was invisible as long as the pod kept coming back.

## What went well

- Restarts are cheap: Grafana is stateless here (dashboards come from ConfigMaps), so nothing was lost.

## What went badly

- Resource limits were set without looking at real usage.
- Alerting covered the workload, not the platform that watches it.

## Action items

| Action | Type | Status |
|---|---|---|
| Raise Grafana's memory limit based on measured working set | mitigate | done |
| `ContainerOOMKilled`: any container OOMKilled in the last 15 min, cluster-wide | detect | done |
| Size limits from `container_memory_working_set_bytes` history before tightening them | prevent | practice |
| Daily health check lists pod restarts in the last 24h, so a quiet crash loop shows up in Discord | detect | done (#84) |
