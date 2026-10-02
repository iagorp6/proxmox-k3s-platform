# Security Policy

This is a personal homelab project, but security reports are welcome.

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's
[private vulnerability reporting](https://github.com/iagorp6/proxmox-k3s-platform/security/advisories/new)
instead. You can expect a first response within a few days.

## Scope

- Secrets or credentials accidentally committed to the repository
- Insecure defaults in the Terraform, Ansible or Kubernetes manifests
- Issues in the custom exporter or helper scripts

Environment-specific values (IPs, hostnames, credentials) are intentionally kept out of the
repository; see `.env.local.example` and the `*.example` files.
