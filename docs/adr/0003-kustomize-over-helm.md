# ADR 0003: Kustomize per game, no Helm chart

- Status: accepted, refined by ADR 0004
- Date: 2026-10

## Context
Every game shares the same shape (StatefulSet, Service, exporter sidecar, backup, secrets) with
small differences (image, ports, memory, probes, init steps).

## Decision
Each game is a Kustomize directory with plain YAML; no Helm chart is written for the games. The
repository already used Kustomize, and rendered manifests stay easy to diff in pull requests and
to validate with kubeconform.

The original plan was a shared Kustomize base plus optional components (exporter sidecar, backup,
RCON secret). It was dropped while adding the second game: names and ports differ per game, and
components cannot template them without a pile of patches. ADR 0004 replaces that part with a
generator.

## Consequences
- A Helm library chart remains the right choice only if the platform is ever published for others.
- Third-party charts can still be consumed through Argo CD when needed.
