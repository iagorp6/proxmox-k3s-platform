# ADR 0005: Recover the host by rebuilding it, not by restoring it

- Status: accepted
- Date: 2026-10

## Context
The Proxmox host has two 1 TB HDDs in a ZFS stripe (RAID0). Losing either disk loses the pool, the
host and the VM. The pool uses about 1% of its capacity, so a mirror would cost nothing in space,
but converting a stripe to a mirror needs extra disks or a reinstall. There is no Proxmox Backup
Server and no second storage target; a `vzdump` to the same pool would die with it.

Everything above the disks is already code: the VM (Terraform + cloud-init), the host and VM
configuration (Ansible), the cluster state (Argo CD from Git), and secrets (SSM, synced by External
Secrets). The only state that is not reproducible is game data, and that goes to S3 with restic
every day.

## Decision
No image-level backups of the VM. Recovery is a rebuild: reinstall Proxmox, run the Ansible host
playbooks, `terraform apply`, bootstrap Argo CD, and restore game data from restic. The risk of the
stripe is accepted for now and made visible instead of hidden: SMART, reallocated sectors, pool state
and ECC errors alert to Discord, and the annotations say that one disk means the whole pool.

The host uses the `pve-no-subscription` repository on purpose (the "non production-ready" warning
in the UI is expected); the enterprise repositories are disabled.

## Consequences
- RTO is the time of a full rebuild, which is only known after a timed DR drill (roadmap). Until
  then this decision is a hypothesis.
- RPO for game data is up to 24 hours (daily restic snapshot); everything else has an RPO of zero
  because it lives in Git and SSM.
- Anything changed by hand on the host or in the VM is lost in a rebuild. That is the incentive to
  keep it in code, and the reason the host baseline (SSH, ZFS ARC, firewall) is an Ansible role.
- The next reinstall (the DR drill is the natural moment) should switch the pool to a mirror. The
  space cost is irrelevant and it removes the most likely full outage.
