# ADR 0007: Third-party Helm releases managed by Argo CD

- Status: accepted
- Date: 2026-10

## Context
kube-prometheus-stack, Loki, Alloy and External Secrets were installed with `helm upgrade --install`
from `deploy.sh` scripts on the operator's machine. That left the most important part of the
cluster outside GitOps: a values change merged to `main` did nothing until someone ran
`make deploy-monitoring`, Argo CD could not show drift on those releases, and the Proxmox scrape
target lived in a git-ignored `values-*.local.yaml` that only existed on one laptop.

## Decision
- Each release is an Argo CD Application in `k8s/argocd/apps/` with two sources: the upstream chart
  pinned by `targetRevision`, and this repository as a `ref` so the values files stay where they
  were. `releaseName` keeps the names of the resources Helm created, so the first Sync adopts them
  instead of recreating them.
- Sync stays manual. Monitoring and secrets are what every other app depends on; a chart upgrade
  arrives as a Renovate PR (the `argocd` manager reads `targetRevision`) and reaches the cluster
  when someone presses Sync. Server-side apply is on because the operator CRDs exceed the
  client-side annotation limit.
- The Proxmox scrape configuration comes from an ExternalSecret that templates the jobs with the
  host address stored in SSM (`/homelab/monitoring/proxmox-host`). The repository is public, so the
  address stays out of Git, and nothing depends on a local file any more.
- Argo CD itself stays on Helm (`make deploy-argocd`): it has to exist before it can apply
  anything. The other `deploy.sh` scripts remain as break-glass tools that read the chart version
  from the Application, so a manual install matches what Argo CD will apply next.

## Consequences
- `main` is the desired state for the whole cluster except Argo CD and the two bootstrap secrets.
- Helm no longer tracks these releases. `helm list` stops being useful for them, and `helm uninstall`
  must never be run: it would delete the resources Argo CD now owns, CRDs included.
- Charts that rely on Helm `lookup` or on install-only hooks behave differently under Argo CD; the
  four charts in use only use hooks for the admission webhook certificates, which Argo CD maps to
  sync hooks, and the generated CA bundles are ignored in the diff.
