# ADR 0005: Recover the host by rebuilding it, not by restoring it

- Status: accepted (updated 2026-10-26: the pool is now a mirror, see the update at the end)
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
  space cost is irrelevant and it removes the most likely full outage. (Done without a reinstall on
  2026-10-26, see below.)

## Update 2026-10-26: the pool is a mirror
The context above was wrong on one point: converting the stripe did not need extra disks or a
reinstall. With `feature@device_removal`, `zpool remove` evacuated one disk of the stripe onto the
other with everything running (13.9 GB in 2 minutes), and `zpool attach` turned the remaining disk
into a two-way mirror. The removal leaves an `indirect-1` vdev, a permanent mapping of the moved
blocks (607 KB of RAM); it is expected and harmless.

What pushed it forward: both disks had about 40,960 power-on hours (4.7 years) and come from the
same batch, so the "lose either disk, lose the host" risk was growing. Before converting, the
dashboard showed that a single disk doing the work of both would have peaked at 18% busy (p99 over
7 days), and `playbooks/disk-bench.yml` measured the pool before and after:

| fio test | Stripe | Mirror |
|---|---|---|
| 4k random write, fsync after each (K3s datastore, game saves) | 99 IOPS, fsync p99 39.6 ms | 66 IOPS, fsync p99 79.2 ms |
| 1M sequential write | 179 MB/s | 145 MB/s |
| 4k random read, queue depth 16 | 136 IOPS, p99 215 ms | 143 IOPS, p99 251 ms |

Writes got slower, as expected: every write goes to both disks and a sync write waits for the
slower of the two. The fsync p99 doubled, above the 60 ms threshold set before the test. The mirror
was kept anyway: that number comes from a disk doing nothing but fsyncs for a minute, while the real
load leaves the disks idle over 80% of the time, and a stripe of two 4.7-year-old disks was the
single most likely way to lose the whole host. Write latency is watched on the dashboard; if it
ever hurts, the fix is a small SSD as a SLOG for sync writes, not going back to a stripe.

Consequences now:
- One failed disk degrades the pool instead of losing the host. Replacement is a documented
  procedure ([docs/disk-replacement.md](../disk-replacement.md)), and the SMART and pool alerts
  point to it.
- The rebuild strategy above still covers losing the whole host (both disks, the controller, the
  board), and its RTO is still unmeasured until the timed DR drill.
- Usable space is one disk (about 930 GB) instead of two; the pool uses about 3% of it.
