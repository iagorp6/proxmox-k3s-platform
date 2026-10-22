# Project Zomboid blocked on an admin password prompt

- **Date:** 2026-10-02 · **Duration:** about 1h, ~08:13 to ~09:15 (America/Sao_Paulo) · **Severity:** active game down
- **Detected by:** the game alert at the time (Zomboid-specific rule, since replaced by `GameUnresponsive`)

## Summary

A hardening change stopped passing the admin credentials after the first boot, to keep the password
out of the logs. On the next start of the existing world the server found no admin credentials, asked
for a password on standard input and waited. The container had no TTY, so nothing ever answered: the
pod stayed `Running`, the JVM stayed alive, and no game was served. The fix was to pass the admin credentials on every start, from SSM.

## Impact

Nobody could join Project Zomboid for about an hour. The world was not affected. Under today's SLO
that is ~15% of the 28-day error budget from one event.

## Timeline

Times in America/Sao_Paulo, from the commit history. The start is inferred: it is the first change
that recreated the pod after the latent bug shipped.

| Time | Event |
|---|---|
| 2026-09-28 14:55 | `a65a38a`: the admin password is passed only on the first boot (to keep it out of the logs). Latent: nothing restarts the server with an existing world yet |
| 2026-10-02 08:12 | `7839113` pins every image by digest; the Zomboid pod is recreated on the existing world |
| ~08:13 | Server prints the admin password prompt and blocks; pod `Running`, no game port answering |
| between 08:13 and 09:14 | Game alert fires (server not answering) |
| 09:14 | `1a354a1`: admin password passed on every boot, `pzexe: arg:` lines filtered, container exits when the server dies |
| ~09:15 | Server starts normally |

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
