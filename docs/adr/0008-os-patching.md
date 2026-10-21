# ADR 0008: OS patching, automatic on the VM, manual on the host

- Status: accepted
- Date: 2026-10

## Context
Nothing patched the operating systems. The K3s VM (Ubuntu) and the Proxmox host (Debian) only got
updates when someone remembered to log in. A daily reboot "just in case" was considered and
rejected: it does not install anything by itself, it costs a restart of the game every day, and
a single-node cluster has no other node to move the workload to.

The two machines fail differently. A VM reboot takes the game, Prometheus and Alertmanager down for
a few minutes and they come back on their own. A host reboot also stops the VM, and if the host
does not come back (kernel, ZFS, firmware), nothing does until someone is at the console.

## Decision
- **VM:** `unattended-upgrades` installs updates from the security pockets only (the role fails if
  `-updates`, `-proposed` or `-backports` is allowed). It never reboots by itself. A systemd timer
  (`homelab-patch-reboot.timer`, Monday 04:30 America/Sao_Paulo) reboots only when a package
  created `/run/reboot-required`, after running each running game's `switch.saveCommand` from the
  catalog. The window sits before the backups (05:15) and the Zomboid daily restart (06:00), and
  Alertmanager mutes it (`patch-semanal`) so the restart does not page.
- **Host:** no automatic upgrade or reboot. `prometheus-node-exporter-collectors` exports
  `apt_upgrades_pending` and `node_reboot_required`; a small textfile script adds
  `homelab_kernel_reboot_pending`, because Proxmox kernels do not create `/run/reboot-required`.
  `HostSecurityUpdatesPending` and `HostRebootPending` fire after 7 days, and
  `playbooks/pve-upgrade.yml` applies the upgrade, with the reboot behind
  `-e pve_upgrade_reboot=true` (it saves the games on the VM first).

## Consequences
- Security fixes reach the VM within a day; at most one unplanned-looking restart a week, only
  when a kernel or core library changed.
- Host patching still needs a person, but it can no longer be forgotten silently: the daily check
  shows the pending count and the alerts escalate after a week.
- Alerts that start during the Monday window are delayed until 05:15, including critical ones.
- Non-security updates on the VM (and K3s itself) stay manual: K3s is pinned in Ansible and
  upgraded through Renovate PRs.
