# Replacing a disk in the host's ZFS mirror

The host pool (`rpool`) is a two-way ZFS mirror since 2026-10-26 ([ADR 0005](adr/0005-host-recovery-by-rebuild.md)).
Losing one disk degrades the pool but nothing stops; losing the second one before the first is replaced loses the
host, and then the path is the [DR runbook](dr-runbook.md).

Replace a disk when:

- `DiskSmartFailing` fires (SMART predicts a failure), or
- `DiskReallocatedSectors` keeps growing, or
- `ZfsPoolNotOnline` fires with `DEGRADED`, or `zpool status` shows READ/WRITE/CKSUM errors on one disk.

Each disk has the layout the Proxmox installer creates: `part1` BIOS boot, `part2` the ESP (kept in sync by
`proxmox-boot-tool`, so either disk boots the host alone) and `part3` the `rpool` member. The new disk needs the same
three partitions, so its size must be **1 TB or more**.

Commands run on the host (Proxmox shell or `ssh root@<host>`). In the examples, `OLD` is the serial of the disk
being replaced and `GOOD` the serial of the healthy one; take both from step 1.

## 1. Before touching anything

```bash
zpool status rpool
ls -l /dev/disk/by-id/ | grep -v part | grep ata-
```

Note which serial is failing (the `ata-MB001000GWFGF_<serial>` name in `zpool status`) and check the other one is
`ONLINE` with zero errors. If **both** disks show errors, stop here: take a backup and go to the DR runbook instead.

Take a fresh backup of the active game:

```bash
GAME=minecraft   # the active game: cat platform/active-game.yaml
kubectl -n "$GAME" create job --from=cronjob/"$GAME"-backup "$GAME"-backup-pre-disk
kubectl -n "$GAME" wait --for=condition=complete job/"$GAME"-backup-pre-disk --timeout=30m
```

## 2. Take the disk out of the pool

If the failing disk is still in the pool, take it offline so ZFS stops using it:

```bash
zpool offline rpool ata-MB001000GWFGF_OLD-part3
```

Shut the host down from the Proxmox UI (the VM shuts down cleanly with it). The B140i runs in AHCI mode
(see the README engineering notes), so do not count on hot swap. Swap the disk and boot.

## 3. Partition the new disk

Copy the partition table from the healthy disk, then give the copy new GUIDs:

```bash
ls -l /dev/disk/by-id/ | grep -v part | grep ata-   # the new serial shows up here
NEW=/dev/disk/by-id/ata-<new disk>
GOOD=/dev/disk/by-id/ata-MB001000GWFGF_GOOD
sgdisk "$GOOD" -R "$NEW"
sgdisk -G "$NEW"
```

## 4. Resilver

`zpool status` shows the missing disk by its old name or by a numeric GUID; use exactly what it shows:

```bash
zpool replace rpool ata-MB001000GWFGF_OLD-part3 "$NEW-part3"
zpool wait -t resilver rpool
zpool status rpool   # expect: mirror-0 with both disks ONLINE, "resilvered ... with 0 errors"
```

The pool uses ~30 GB, so the resilver takes minutes, not hours.

## 5. Make the new disk bootable

```bash
proxmox-boot-tool format "$NEW-part2"
proxmox-boot-tool init "$NEW-part2"
proxmox-boot-tool clean    # drops the ESP of the removed disk from the list
proxmox-boot-tool status   # expect: two ESPs, both with kernels
```

If the server ever fails to boot from the new disk alone, check the UEFI boot order in the RBSU (F9 at boot).

## 6. Done when

- `zpool status -x` says `all pools are healthy`.
- A scrub is clean: `zpool scrub -w rpool && zpool status rpool`.
- Grafana **AsunBoid**, host row: pool ONLINE, SMART OK for both disks, and both disks with similar busy time.
- `ZfsPoolNotOnline` and the SMART alerts are resolved in Discord.
