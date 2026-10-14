# ADR 0006: Player self-service game switch through a Discord panel

- Status: accepted
- Date: 2026-10
- Builds on: [ADR 0002](0002-one-active-game-git-driven-switch.md)

## Context
ADR 0002 makes Git the only way to change the active game, and `scripts/switch-game.py` the tool
that does it. Only someone with repository and cluster access can run it, so every switch waits for
the maintainer. Players asked to switch on their own, with the same safety rule: never switch while
someone is playing the current game.

## Decision
A Discord bot (`games-bot`) runs in the `platform` namespace and keeps one panel message in the
`#games` channel: active game, players online, a queued request, and one button per game. Members
cannot write in the channel; they only click.

On a click the bot checks that the member has the `Players` role, reads the player count of the
active game from Prometheus, and refuses (queuing the request) while anyone is online. When the game
is empty it runs the ADR 0002 flow from inside the cluster: save the world, write the replica
patches and `platform/active-game.yaml`, commit and push as `games-bot` with the requester in the
commit message, then sync the Argo CD Applications. Every switch is also logged in `#alerts`.

Push access uses a deploy key scoped to this repository, stored in Bitwarden, published to SSM and
read through External Secrets. The `protect-main` ruleset lists deploy keys as a bypass actor, the
same way the repository admin role already bypasses it for `make switch-game`.

## Alternatives rejected
- **Runtime ownership of replicas** (Argo CD ignoring `/spec/replicas`): still rejected for the
  reasons in ADR 0002; the bot only automates who runs the Git flow.
- **Personal access token of the maintainer:** a fine-grained token expires and needs rotation, and
  commits would show the maintainer as the author of every switch. A classic token reaches every
  repository of the account.
- **Pull request per switch:** keeps the ruleset untouched, but adds minutes of CI before a game can
  start, for a change limited to replica counts.
- **Wake on connect:** only Minecraft lets a proxy answer the client with a message; Project Zomboid
  and Terraria would only show a connection error. May come later for Minecraft only.

## Consequences
- Players switch games without the maintainer; Git still describes what is running and every
  switch has an author (`games-bot`), the requester and a timestamp.
- The bot refuses to commit any path other than `platform/active-game.yaml` and
  `k8s/games/*/replicas.yaml`, which limits what the deploy key is used for.
- Commits from the bot skip the pull request rule but not CI: `ci.yml` also runs on push to `main`.
- A leaked deploy key allows pushes to this repository only; rotation is a new key pair, a new
  Bitwarden note and `make secrets`.
