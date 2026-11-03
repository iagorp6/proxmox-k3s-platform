# Every backup job failing for three days because of a stale restic lock

- **Date:** 2026-10-31 to 2026-11-03 · **Duration:** about 3 days (first failed run 2026-10-31 05:00, fix on 2026-11-03) · **Severity:** backups degraded, no data lost, no game downtime
- **Detected by:** `BackupLastRunFailed` within 30 minutes of the first failure; acted on three days later

## Summary

The Zomboid wipe on 2026-10-30 checked its archive snapshot with `restic ls ... | grep -q`. `grep -q`
exits on the first match, restic died of SIGPIPE, and the dead process left its lock in the shared S3
repository. Every later `restic forget` (daily, in each game's backup job) and the weekly `prune`/`check`
need an exclusive lock, waited up to 10/30 minutes and failed. `restic unlock` removed it.

## Impact

- New snapshots kept being written: `restic backup` only needs a shared lock, and it ran before the
  `forget` that failed. The latest world of every game was in S3 the whole time.
- Each failed job retried twice (`backoffLimit: 2`), so each game got three snapshots per day, and none
  expired because `forget` never finished. The repository is deduplicated, so the extra size was small.
- The weekly `restic-check` (prune, 5% data check, size check) did not run on 2026-11-01.
- About 15 alerts in `#ops` (`BackupLastRunFailed`, `BackupStale`, `KubeJobFailed`, `ArgoAppDegraded`,
  `ResticCheckFailed`), all from this one cause. No SLO impact.

## Timeline (America/Sao_Paulo)

| Time | Event |
|---|---|
| 2026-10-30 10:52 | `make wipe-game GAME=zomboid`: the `restic ls` killed by SIGPIPE leaves lock `16360a24` |
| 2026-10-30 10:54 | Wipe reports success; the archive snapshot is fine |
| 2026-10-31 05:00 | First daily backup after the wipe (Valheim): snapshot saved, `forget` fails on the lock |
| 2026-10-31 05:30 | `BackupLastRunFailed` fires; the other three games follow by 06:15 |
| 2026-11-01 | Weekly `restic-check` fails on the same lock (`ResticCheckFailed`, `KubeJobFailed`) |
| 2026-11-03 08:00 | Daily health check: 15 alerts firing, last Terraria backup 4 days old |
| 2026-11-03 08:35 | Job logs read: `repository is already locked by PID 22 on zomboid-wipe-t6ssn`, created 2026-10-30 |
| 2026-11-03 | `restic unlock`, manual backups of all four games, failed Jobs deleted |

## Root cause

A shell pipeline whose reader exits early. `restic ls --json "$id" | grep -qF '"path":"..."'` is a
normal way to test for a line, but `grep -q` closes the pipe on the first match and restic is killed
by SIGPIPE before it can delete its lock. restic never removes another process's lock on its own,
however old: it waits (`--retry-lock`) and then fails with "the `unlock` command can be used to remove
stale locks". All games share one repository, so one stale lock blocked every game's maintenance.

## What went well

- The alerts fired within 30 minutes, and the `BackupLastRunFailed` action command (the job's log)
  showed the cause in its last lines.
- The backup itself kept running before the failing step, so recovery points stayed current.

## What went badly

- The alerts sat for three days over a long weekend. Fifteen alerts from one cause read as noise, not as
  one clear problem.
- The wipe job reported success: the lock was a side effect nothing checked.

## Action items

| Action | Type | Status |
|---|---|---|
| Wipe writes `restic ls` to a file and greps the file, so restic always exits on its own | prevent | done (this PR) |
| Each backup job and the weekly `restic-check` start with `restic unlock`, which removes only stale locks (no refresh for 30 minutes), so the next scheduled run recovers by itself | mitigate | done (this PR) |
