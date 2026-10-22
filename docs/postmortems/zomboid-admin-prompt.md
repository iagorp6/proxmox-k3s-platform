# Project Zomboid blocked on an admin password prompt

- **Date:** before 2026-10-11 (exact date to confirm) · **Duration:** about 1h · **Severity:** active game down
- **Detected by:** the game alert at the time (Zomboid-specific rule, since replaced by `GameUnresponsive`)

## Summary

A hardening change removed the default admin account from the Zomboid server configuration. On the
next start the server found no admin, asked for a password on standard input and waited. The container
had no TTY, so nothing ever answered: the pod stayed `Running`, the JVM stayed alive, and no game was
served. The fix was to pass the admin credentials on every start, from SSM.

## Impact

Nobody could join Project Zomboid for about an hour. The world was not affected. Under today's SLO
that is ~15% of the 28-day error budget from one event.

## Timeline

| Time | Event |
|---|---|
| (to confirm) | Change removing the default admin is deployed; the server restarts |
| +minutes | Server prints the admin password prompt and blocks; pod `Running`, no game port answering |
| (to confirm) | Game alert fires (server not answering) |
| ~+1h | Admin password passed at boot; server starts normally |

## Root cause

Project Zomboid creates the admin account interactively the first time a server starts without one.
The hardening assumed "no admin" meant "more secure", but for this server it means "ask a human". In a
container with no TTY the prompt blocks forever, and nothing in Kubernetes saw a problem: the process
was alive, and there were no probes, so the pod was also `Ready`.

A second issue came out while fixing it: the launcher (`pzexe`) prints every command-line argument,
which would put the admin password in the logs (and in Loki) once it was passed as an argument.

## What went well

- The game-level alert (based on the server answering, not on the pod state) caught it.

## What went badly

- Pod state said everything was fine. Any check based on `Running` or `Ready` was blind.
- The change was not tried against a fresh start before it reached the live server.

## Action items

| Action | Type | Status |
|---|---|---|
| Always pass `-adminusername admin -adminpassword` from an SSM-backed Secret (`start.sh`) | prevent | done |
| Filter `pzexe: arg:` lines out of the server output, so the password never reaches Loki | prevent | done |
| Entrypoint keeps the console FIFO open and exits when the game exits, so a dead server restarts the pod | mitigate | done |
| Startup and readiness probes: the pod is `Ready` only after `SERVER STARTED` and the UDP socket is open, so `GameDown` sees a stuck boot | detect | done (#87) |
| Generic `GameUnresponsive` (pod ready, server silent for 5 min) for every catalog game | detect | done (#68) |
