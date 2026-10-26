# DR runbook: rebuild the host from zero

This is the full rebuild from [ADR 0005](adr/0005-host-recovery-by-rebuild.md): the host is lost (or
reinstalled on purpose, as in the DR drill), and everything comes back from Git, AWS SSM and the
restic snapshots in S3. Run it top to bottom and write the clock time at the end of each phase in the
table at the bottom; the sum is the measured RTO.

The pool is a **ZFS mirror** since 2026-10-26 (converted in place, see ADR 0005), and phase 1 keeps it one. For a
single failed disk, do not rebuild: follow [disk-replacement.md](disk-replacement.md).

Commands run from the control node (WSL, `~/homelab-infra`) unless a phase says otherwise.

## 0. Before you start (drill only)

Skip this phase in a real outage.

1. Nobody is playing. Note the active game: `cat platform/active-game.yaml`.
2. Take a fresh backup of the active game, so the drill measures the restore and not a 24h RPO:
   ```bash
   GAME=minecraft   # the active game
   kubectl -n "$GAME" create job --from=cronjob/"$GAME"-backup "$GAME"-backup-pre-dr
   kubectl -n "$GAME" wait --for=condition=complete job/"$GAME"-backup-pre-dr --timeout=30m
   ```
3. Prove that snapshot restores before wiping anything: `make restore-drill GAME=$GAME RECORD=1`.
4. Have at hand: Bitwarden unlocked (`export BW_SESSION="$(bw unlock --raw)"`), the Proxmox VE ISO on
   a USB stick, and `~/.proxmox.env` (it will need a new API token after the reinstall).

**Start the clock** when the host goes down (drill: when you boot the installer).

## 1. Reinstall Proxmox VE on a ZFS mirror

On the server console (iLO or monitor):

1. Boot the Proxmox VE installer. Target: **ZFS (RAID1)** with both disks. Same hostname (`pve-01`),
   same management IP, gateway and DNS as before, so the DHCP reservation, the firewall VIP and the
   inventory keep working.
2. After the first boot, from the web UI shell:
   ```bash
   zpool status rpool   # expect: mirror-0 with both disks ONLINE
   ```
3. Repositories (ADR 0005: no-subscription on purpose):
   ```bash
   sed -i 's/^deb/#deb/' /etc/apt/sources.list.d/pve-enterprise.list /etc/apt/sources.list.d/ceph.list 2>/dev/null || true
   echo "deb http://download.proxmox.com/debian/pve $(. /etc/os-release; echo "$VERSION_CODENAME") pve-no-subscription" \
     > /etc/apt/sources.list.d/pve-no-subscription.list
   apt update && apt -y full-upgrade
   ```
4. Root SSH key from the control node: `ssh-copy-id root@<PVE_HOST>` (the `pve_host` role refuses to
   disable password login without it).
5. API token for Terraform (UI: Datacenter > Permissions > API Tokens), then update
   `PROXMOX_VE_API_TOKEN` in `~/.proxmox.env`.

## 2. Host baseline (Ansible)

```bash
cd ansible
ansible-playbook playbooks/pve-host.yml        # SSH hardening, ZFS ARC limit, datacenter firewall (with auto-rollback)
ansible-playbook playbooks/host-exporters.yml  # node_exporter, smartctl_exporter, ZFS, EDAC and pending-patch metrics
```

## 3. VM (Terraform)

The state in S3 still lists the old VM and cloud image; `terraform apply` sees they are gone and
creates them again.

```bash
source ~/.proxmox.env && cd terraform
terraform plan    # expect: create the VM and the downloaded cloud image, nothing to destroy
terraform apply
```

Wait for cloud-init to finish: `$(terraform output -raw ssh_command) cloud-init status --wait`.

## 4. Cluster (Ansible)

```bash
cd ansible
ansible-playbook playbooks/k3s.yml             # K3s pinned, secrets encrypted at rest, kubeconfig written to the control node
ansible-playbook playbooks/gameserver-net.yml  # MTU and RakNet clamp for the game ports
ansible-playbook playbooks/auto-updates.yml    # security updates, weekly reboot window (saves the game first)
kubectl get nodes                              # Ready
```

## 5. GitOps bootstrap

1. Argo CD (the only Helm release outside Argo CD, ADR 0007): `make deploy-argocd`.
2. The two bootstrap secrets, the only state that does not come from Git or SSM:
   ```bash
   # External Secrets credential (read-only on /homelab/*), from the terraform-aws outputs
   kubectl create namespace external-secrets
   kubectl -n external-secrets create secret generic aws-ssm-credentials \
     --from-literal=access-key-id="$(terraform -chdir=terraform-aws output -raw eso_access_key_id)" \
     --from-literal=secret-access-key="$(terraform -chdir=terraform-aws output -raw eso_secret_access_key)"

   # Argo CD read access to this repository (deploy key kept in Bitwarden; set the item name)
   ARGO_KEY_ITEM=argocd-deploy-key
   kubectl -n argocd create secret generic repo-proxmox-k3s-platform \
     --from-literal=type=git \
     --from-literal=url=git@github.com:iagorp6/proxmox-k3s-platform.git \
     --from-literal=sshPrivateKey="$(bw get notes "$ARGO_KEY_ITEM")"
   kubectl -n argocd label secret repo-proxmox-k3s-platform argocd.argoproj.io/secret-type=repository
   ```
3. Root app: `kubectl apply -f k8s/argocd/root.yaml`. Everything except the games syncs on its own
   ([ADR 0009](adr/0009-auto-sync-for-third-party-charts.md)); the games stay manual.
4. Wait for the charts to converge: `kubectl -n argocd get applications -w` until
   `external-secrets`, `kube-prometheus-stack`, `loki` and `alloy` are `Synced`/`Healthy`. The first
   syncs retry with backoff while CRDs and webhooks from the other apps come up, so a few minutes of
   `Degraded` or failed syncs is expected.
5. Check that every ExternalSecret resolved: `kubectl get externalsecrets -A` (all `SecretSynced`).
6. Now that the ServiceMonitor CRD exists, run `make deploy-argocd` again: the Argo CD chart skips
   its ServiceMonitor while the CRD is missing, and without it the `gitops` alerts have no data and
   `ArgoCDMetricsMissing` fires. `external-secrets` re-renders its own ServiceMonitor on the next
   refresh, no action needed.

## 6. Game data (restic)

The active game must not start on an empty volume and stay that way: it would create a new world,
and the next nightly backup would make that empty world the latest snapshot. So the game comes up
stopped, gets its world back, and only then starts.

```bash
GAME=minecraft   # the active game from platform/active-game.yaml
```

1. Sync the game app in Argo CD (it brings the StatefulSet, the `$GAME-backup` Secret and the backup
   CronJob), then stop the server right away:
   ```bash
   kubectl -n "$GAME" scale statefulset "$GAME" --replicas=0
   ```
   If the pod already started and wrote a fresh world, that is fine: the restore moves it aside.
2. Restore the latest snapshot into the game's own volume:
   ```bash
   make restore-game GAME=$GAME
   ```
   It refuses while the server is running, creates the PVC from the StatefulSet template if it does
   not exist yet, renames whatever is in the backed-up paths to `<path>.pre-restore-<date>`, restores,
   and checks the paths and the `backup.verify` world file from the catalog.
3. Start the server: `kubectl -n "$GAME" scale statefulset "$GAME" --replicas=1` (or Sync the app).
4. Join the game and check the world is the one from the snapshot. Delete the `*.pre-restore-*`
   folders once you are sure.

The inactive games keep their data in S3 and are restored the same way the first time someone
switches to them (`make switch-game` starts them on an empty volume otherwise).

## 7. Done when

- `make status` shows the active game Ready with players able to join.
- Grafana dashboard **AsunBoid** shows the host (SMART, ZFS mirror) and the game; no alert firing
  except the expected ones during the rebuild.
- The Healthchecks.io watchdog is green again.
- The Discord panel in `#games` answers.

**Stop the clock.**

## Timing

Copy this table into the drill record (`docs/restore-drills.md`, section "Full rebuild").

| Phase | Started | Finished | Minutes | Notes |
|---|---|---|---|---|
| 1. Proxmox on ZFS mirror | | | | |
| 2. Host baseline | | | | |
| 3. VM | | | | |
| 4. Cluster | | | | |
| 5. GitOps bootstrap | | | | |
| 6. Game data | | | | |
| **RTO** | | | | |

## After the drill

- Update ADR 0005 with the measured RTO.
- Tick the DR drill item in the README roadmap and publish the RTO in "Backup and disaster recovery".
- Anything you had to do by hand that is not in this runbook is a bug: put it in code or here.
