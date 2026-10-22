# Postmortems

Blameless write-ups of incidents that hurt players or left the platform blind. The goal is the
action items: each one is a pull request, an alert or a check that keeps the same failure from
coming back silently.

**When to write one** (from the [error budget policy](../slo.md#error-budget-policy)): any event that
spent 20% of the 28-day budget or more (about 80 minutes of the active game down), and any failure
that no alert caught, whatever its duration.

| Date | Incident | Impact | Duration |
|---|---|---|---|
| 2026-10-14 | [Discord bot stuck in `InvalidImageName`](2026-10-14-games-bot-invalid-image.md) | Self-service game switch unavailable | ~1h |
| 2026-10-02 | [Zomboid blocked on an admin password prompt](zomboid-admin-prompt.md) | Zomboid down, pod showed `Running` | ~1h |
| 2026-10-01 | [Grafana OOMKilled 15 times in a day](grafana-oomkill.md) | Dashboards intermittently unavailable, no alert | ~1 day |

Dates and clock times come from the commit and pull request history. When a moment left no trace
(when someone noticed, when an alert fired), the timeline gives the window it falls in instead of a
guess.

## Template

```markdown
# <Title: what broke, in user terms>

- **Date:** YYYY-MM-DD · **Duration:** start to end (timezone) · **Severity:** players affected / blind spot
- **Detected by:** alert name, a player, or by chance

## Summary
Two or three sentences: what users saw, why, how it was fixed.

## Impact
Who could not do what, for how long. Budget spent, if the SLO was affected.

## Timeline
| Time | Event |

## Root cause
The technical chain, down to the decision that allowed it. No names.

## What went well / what went badly

## Action items
| Action | Type (prevent / detect / mitigate) | Status |
```
