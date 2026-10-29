# ADR 0010: AppProjects, Pod Security and game egress, applied outside autosync

- Status: proposed (applied after a dry-run on the cluster)
- Date: 2026-10

## Context
Every Application used the `default` AppProject, which accepts any source repository, any
destination and any resource, and no namespace had Pod Security labels. With autosync, whatever
reached `main` reached the cluster without limits: a mistake, a Renovate PR, or a push with the
games-bot deploy key, which bypasses the `main` ruleset (ADR 0006) and is limited to its allowed
paths only by the client (`switch-game.py`). CI checks the shape of the manifests, not the intent.

The game servers are also exposed to the internet and run third-party mods (Workshop, Modrinth),
and their egress was open: a compromised server could reach the Proxmox GUI, the router and every
other namespace.

## Decision
- **AppProjects per group** (`platform`, `monitoring`, `games`), each limited to this repository
  plus the chart repositories it uses, to its own namespaces, and to the cluster-scoped kinds its
  charts install (CRDs, RBAC, webhooks). No project may create `Application`, `ApplicationSet` or
  `AppProject`, and no project may manage a `Namespace`.
- **The root app** moves to a `bootstrap` project that can only create `Application` objects in
  `argocd`. The `default` project is emptied, so an Application that falls back to it applies
  nothing.
- **Namespaces carry Pod Security labels**: `restricted` where the pods already comply (`platform`,
  `external-secrets`, Minecraft, Terraria), `baseline` plus `warn`/`audit=restricted` for Project
  Zomboid (its install init runs as root to `chown` the volume), and `privileged` for `monitoring`
  (node-exporter and Alloy read the host).
- **Namespaces, projects and the root app live outside autosync.** `k8s/argocd/guardrails.generated.yaml`
  (rendered from the catalog by `render-games.py`) and `k8s/argocd/root.yaml` are applied by hand
  with `make guardrails`, also run by `make deploy-argocd`. A push to `main` can change an
  Application, but not the project that bounds it, the namespaces, or their Pod Security level.
- **Game egress** is limited to cluster DNS, the Kubernetes API port (the backup save hook runs
  `kubectl exec`) and the public internet. RFC 1918, CGNAT and link-local ranges are excluded, so a
  game pod cannot reach the host, the home network or other pods.
- **CI enforces the fit** (`scripts/check-guardrails.py`, and `check-charts.py` for the charts):
  every Application is in a non-default project that allows its repositories, destination and
  rendered resources, and every pod meets the Pod Security level of its namespace, including the
  Jobs that `restore-drill.py` creates.

## Consequences
- A new game needs `make guardrails` after its PR is merged (namespace and project destination)
  before the first `make switch-game`. `new-game.py` prints this step.
- Changing a project or a Pod Security level is a reviewed change to a generated file, then a manual
  apply: the same two-step path as the root app.
- The Kubernetes API rule allows TCP 6443 to any address, because the node IP is private and the
  repository is public. Only the kube-apiserver listens on that port.
- Residual risk: a push can still change what runs inside a game or platform namespace, within its
  Pod Security level. Review and CI remain the gate for that.
