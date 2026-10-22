# SLO: game availability

One service level objective, for the thing players actually notice: **can they join the active game?**

| | |
|---|---|
| **SLI** | Fraction of minutes in which the active game (the catalog game with replicas > 0) answers its exporter (`game_up == 1`) |
| **Objective** | 99% over a rolling 28-day window |
| **Error budget** | 1% of 28 days = **6h43min** of unavailability |
| **Rules** | [`k8s/monitoring/alerts-slo.yaml`](../k8s/monitoring/alerts-slo.yaml), tested in [`tests/slo.test.yaml`](../k8s/monitoring/tests/slo.test.yaml) |
| **Dashboard** | AsunBoid, section *SLO*: availability, budget left, burn rate |

## What counts as down

The SLI is measured from the player's side, so planned work spends budget on purpose:

- **Game switches.** From the moment the old game stops until the new one answers. A Zomboid start with
  26 mods takes minutes; that is real time nobody can play.
- **The daily restart** (Zomboid, 06:00) and the **Monday patch reboot** (only when a security update
  asked for one, [ADR 0008](adr/0008-os-patching.md)).
- **No active game at all**, or an active game whose exporter is missing. "We can't tell" is not "up".

Alertmanager mute windows hide the *alerts* during the restart and patch windows; they do not hide the
minutes from the SLI. Excluding planned work would make the number look better and tell us less: if
switches start taking 20 minutes, that should show up in the budget.

**Why 99%:** the platform is a single node with one active game, so 99.9% (43 min a month) would be
spent by the daily restarts alone. 99% leaves room for restarts, a few switches a day and one bad
evening, and still catches a week of flapping.

## Recording rules

Evaluated every minute in the `slo` group:

| Rule | Meaning |
|---|---|
| `game_slo:active_up` | 1 if the active game answers, else 0 (also 0 with no active game) |
| `game_slo:unavailable:ratio_avg{5m,30m,6h,3d}` | `1 - avg_over_time(game_slo:active_up[w])` |
| `game_slo:unavailable:ratio_avg28d` | Average of the 30-minute ratios over 28 days (a subquery with a 30m step: same window, 1,344 points instead of 40,320) |
| `game_slo:burn_rate:avg{30m,6h,3d}` | Unavailability divided by the 1% budget. Burn rate 1 spends exactly the budget in 28 days |

Prometheus keeps 30 days (`retention: 30d`), so the 28-day window fits. Right after the rules are first
deployed the windows only hold the data recorded so far.

Minutes when Prometheus itself is down (a VM reboot, for example) are not recorded at all, so they
count neither as up nor as down. That outage is covered elsewhere: the Healthchecks.io dead man's switch
alerts when the whole monitoring stack goes quiet.

## Alerts

Multiwindow, multi-burn-rate alerts, as in chapter 5 of the Google *SRE Workbook*. The long window says
the budget is really being spent; the short window says it is still happening, so the alert clears soon
after the game comes back.

| Alert | Condition | Budget spent when it fires | Severity |
|---|---|---|---|
| `GameSLOBurnRateHigh` | burn rate > 6 over 6h **and** over 30m | ≥ 36 min down in 6h (5% of the budget) | warning |
| `GameSLOBurnRateSlow` | burn rate > 1 over 3d **and** over 6h | > 43 min down in 3 days | warning |
| `GameErrorBudgetExhausted` | 28-day unavailability > 1% for 1h | the whole budget | warning |

Each alert also requires enough history to cover its long window (6 hours, 3 days, and 7 days for
the exhausted budget). Without that guard, a five-minute game switch right after the rules are deployed
would read as 16% unavailability and fire both burn-rate alerts.

The workbook's fastest tier (14.4× over 1h, a page) is left out on purpose. `GameDown` (pod not ready
for 10 min) and `GameUnresponsive` (pod ready, server silent for 5 min) are `critical` and already catch
an acute outage faster, and a slow but healthy Zomboid start would trip a 1-hour tier for nothing. The
burn-rate alerts are for the slow problems those two miss: a game that crashes and comes back every
hour, switches that keep getting longer, restarts that hang for a while.

## Error budget policy

The budget is a decision tool, not a score:

- **Budget left:** ship game changes as usual (mods, image updates, tuning).
- **Budget exhausted** (`GameErrorBudgetExhausted`): risky changes to the game itself wait until the
  window recovers. Fixes that improve availability, security patches and platform work that does not
  restart the game still go in.
- **Any single event that spent 20% of the budget or more** (about 80 minutes) gets a postmortem in
  [`postmortems/`](postmortems/), whether or not an alert fired.

## Review

Look at the SLO section of the dashboard once a month, together with the weekly summary. If the budget
is always nearly untouched, the objective can tighten; if the planned work alone eats most of it, the
fix is faster starts and switches, not a looser number.
