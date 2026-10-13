# Rebuild and carry: design, first case old-XFS CentOS 7

Status: design, 2026-10-13. Not normative yet. Each piece moves into SPEC.md in the PR that builds it, as AGENTS.md
asks (spec before ops). Background: the "defunct to living" notes (option 2) and wend#67.

## The problem this case brings

Every CentOS 7 machine installed from 7.0, 7.1 or 7.2 media has XFS in the old v4 format (`crc=0`, and `ftype=0`)
on `/` and `/boot`, and on any other XFS volume made then (`/home` on a default install with a big disk). Updates
never convert it, and XFS has no in-place v4 to v5 conversion: `xfs_admin` can't add CRCs, and XFS can't shrink, so
there's no make-room-and-reformat trick either. AlmaLinux 10's kernel is built without `XFS_SUPPORT_V4`, so it can't
mount any of those filesystems. Preflight (wend#67) therefore stops these machines at Alma 9. Those were the 2014-2016
installs, which is a lot of the ~1.6M EL7 machines EPEL still counts.

Nothing about their packages is wrong. The in-place route (centos7-lamp, 24 fixes) would carry their userland to
Alma 10 fine. What they need is their bytes on a filesystem Alma 10 can read.

## Blunt call: two stages, relocate first

Rebuild and carry has two halves that have nothing to do with each other:

1. **Placement**: find room for a second root, make a filesystem, flip the bootloader to it, keep the old root
   bootable for rollback, and commit or roll back later. Disk, LVM and bootloader work.
2. **Carry**: fill the new root with a fresh target install plus the box's state, moved by typed state movers
   (users, sshd, cron, httpd vhosts, MariaDB data...).

For XFS v4, half 2 is the wrong tool. A fresh Alma 10 root plus movers rebuilds by hand what the in-place route
already does, proven, and the movers would be per-service work for a box whose only defect is its disk format. So
the first case does placement only, filled with a **copy** of the old root:

> **relocate**: copy the running root onto a fresh v5 filesystem beside it, flip the boot to the copy, keep the old
> root untouched as rollback. The copy is the same CentOS 7.9 machine on a filesystem every later kernel mounts.
> Then the existing in-place route walks the copy, with its usual reboots. Commit drops the old root.

That proves everything risky about rebuild and carry (room, filesystems, the flip on BIOS and UEFI, rollback, the
one-shot trial boot, preflight) on the case with a proven route to compare against. Movers come second, on a case
that actually needs them: Foresight Linux (no living parent, nothing to walk) or EL7 to Debian (a family switch).
Their new root is made the same way; only what fills it differs. Nothing below is EL-only except the adapter's table.

The obvious objection, "just walk to Alma 9 in place and rebuild from there", buys nothing: the placement work is the
same, and Alma 9 is where these boxes already stop.

## Where the new root lives

Order of preference, picked by preflight from the machine's facts. Refused when none fits; the machine owner then adds
a disk (on a VM, which most old fleets are, that's a minute in the hypervisor) and runs preflight again.

1. **Free extents in the root's volume group**: a new LV, `<vg>/wend-<release>` (`root` stays the old one's name).
   A default EL7 install gives every extent to `root`, `swap` and maybe `home`, so this is the rare case.
2. **An unused whole disk**: no partition table, no filesystem signature (`wipefs -n` empty), not mounted, not a PV,
   not in an md array, and named by the owner on the command line (`--new-root-disk /dev/disk/by-id/...`). Never
   picked on its own: an "unused" disk can be someone's SAN LUN. With LVM, it becomes a PV, joins the root's VG, and
   the new LV is made on it (`lvcreate ... <vg> <pv>`), so after commit the old root's extents free up in the same VG.
   Without LVM (a plain `/dev/sda2` root), the disk gets one partition and the root goes on it.
3. Nothing else. No shrinking (XFS can't), no swap reuse (too small and a running system needs it), no loopback file
   on the old root (that root is the thing we're leaving).

Size: what the old root uses (`df` used, every v4 XFS that gets carried, see below), plus the route's `disk_mib`
summed over its hops, plus 20% for the logs and caches the walk leaves behind. Preflight states the number.

**Which filesystems move.** Every mounted filesystem a hop's kernel can't mount (wend#67's `fs` table) moves into the
new root as a directory: v4 XFS `/home`, `/var`, `/srv` become part of the new root. A filesystem every hop can mount
stays where it is and keeps its fstab line. A v4 filesystem that isn't mounted now (`noauto`) is named in the verdict
and blocks: wend doesn't know what's on it.

**Making the filesystem.** The new root is made by the **old** release's `mkfs.xfs` (EL7.9's xfsprogs 4.5: v5,
`crc=1`, `ftype=1`, no `reflink`, `bigtime` or `inobtcount`). It has to mount under both the kernel that's running
now (3.10, while the copy is made and on every rollback check) and every kernel the route boots; the oldest tool that
must read it makes it. Alma 10 mounts that format (it warns that dates end in 2038; a later `xfs_admin -O bigtime=1`
once committed can fix that and is a commit-time option, not part of relocate).

## The copy

Services keep running for the first pass. Two passes:

1. `rsync -aHAXS --numeric-ids -x` of `/` and each moving filesystem into the new root, live.
2. Stop every service the box enables (the `service.record` list), `sync`, run rsync again with `--delete`, then
   leave them stopped until the reboot. Downtime is the second pass plus one boot.

Then, in the new root only: fstab gets the new root's UUID and drops the lines of filesystems that moved; `/boot`
becomes a directory in the new root holding a copy of the old `/boot` (see the flip); SELinux gets `/.autorelabel`
(rsync carries the labels, but files written between the passes may not have them, and a relabel on the trial boot is
cheap); the
initramfs is rebuilt for the new root (`dracut -f` in a chroot of the copy, with the root's LV in `rd.lvm.lv`).
`/etc/machine-id` and the ssh host keys are the old ones on purpose: it's the same machine.

## The flip

**BIOS (GRUB 2, EL7's).** The MBR's `core.img` points at the old `/boot` partition. Leave it alone. On the old
`/boot`:

- move `grub2/grub.cfg` to `grub2/grub.cfg.wend-old`;
- write a small `grub2/grub.cfg` stub: entry 0 is the new root (`insmod lvm xfs`, `search --fs-uuid <new>`,
  `configfile /boot/grub2/grub.cfg`), entry 1 is "wend rollback", which does `configfile /grub2/grub.cfg.wend-old`;
- the stub starts with the same `next_entry` handling `grub2-mkconfig`'s header writes (use it as the default, then
  clear it with `save_env`), and relocate sets `saved_entry=1` and `next_entry=0` in that `/boot`'s `grubenv`. So the
  **trial boot** goes to the new root once, and any reset after that comes back to the old root. GRUB writes `grubenv`
  in place on a plain partition (it refuses on LVM and md), which is one more reason the stub lives on the old `/boot`.

The new root's own `grub.cfg` is EL7's, regenerated in the chroot with `root=` on the new LV and no `/boot` partition.
The VM tier proves EL7's GRUB 2.02 reads it from LVM. When the trial boot comes up and the box's checks pass, the
commit-to-trial step sets `saved_entry=0`: the new root is now the default and the old one is the rollback entry.

Nothing on the old root or in the MBR changes. Undoing the flip from a rescue shell is "rename one file back".

**UEFI (shim, EL7's grubx64.efi).** The ESP (vfat) is mounted at `/boot/efi` on the old root and every kernel mounts
it, so it stays where it is and both roots mount it. The new root gets its own GRUB config directory on the ESP
(`EFI/centos/` is the old one's; the copy's goes to `EFI/wend/`, with its own `grub.cfg` pointing at the new root), a
new NVRAM entry (`efibootmgr -c ... -L wend`), and `BootNext` set to it. Firmware clears `BootNext` on use, so the
trial is the same one-shot as on BIOS, done by the firmware instead. Commit puts the new entry first in `BootOrder`.
Preflight keeps refusing UEFI until a UEFI VM proof (that's already the rule for every route).

**Afterwards, the route.** The in-place route walks the copy, booted, exactly as it walks any 7.3+ box. One gap shows
up here that the in-place route has too: on BIOS, nothing reinstalls GRUB, so once the box is on EL9 its `core.img`
is still EL7's 2.02, which can't read BLS entries (`blscfg`). Leapp runs `grub2-install` on BIOS for this. wend's
in-place EL7 route needs the same harness step before its VM proof can pass; relocate inherits it. On a relocated
box that step installs the release's GRUB with the new root's `/boot/grub2` as its prefix, and the rollback entry
moves into the new root's `/etc/grub.d/40_custom` (`search --fs-uuid <old /boot>`, `configfile
/grub2/grub.cfg.wend-old`; GRUB 2 still reads v4 XFS even where the kernel can't).

## Rollback and commit

The old root and old `/boot` are never written after the flip, except for the stub and `grubenv` on the old `/boot`.
So:

- **Before the trial boot passes**: reset. The trial was one-shot.
- **After**: boot the "wend rollback" entry (`wend rollback` sets it as the next boot). Data written on the new root
  since the flip stays there, readable from the old root only through the new LV (the old kernel mounts the new root
  fine, it made it). The run says this in words: rollback loses what happened after the flip, unless you copy it
  back.
- **Commit** (`wend commit`, never automatic): drops the rollback entry, removes the old root LV (or tells the owner
  which partition is now free; wend never repartitions), and on BIOS reinstalls GRUB with the new root as its prefix so
  the old `/boot` is no longer needed. The old `/boot` partition is left unused and named. Reformatting it is the
  owner's call, since nothing needs it.

## State movers (the second stage, sketched so the first doesn't block it)

When the new root is a fresh target install instead of a copy, these fill it. They're fixes in shape: preconditions,
typed ops, checks. Each one names its source path in the old root, which is mounted read-only at `/run/wend/old`
while the movers run in a chroot of the new root. The LAMP box needs, at least:

| mover | carries | how |
|---|---|---|
| `users.carry` | local users and groups (uid ≥ 1000 and named system users the box's services own), passwords, homes | merge by name; refuse a uid/gid collision with the target's own system accounts |
| `ssh.carry` | host keys, `sshd_config` keys the owner set (`Port`...) | the keys as files; options through `kv.set`, never the old file |
| `cron.carry` | user crontabs, `/etc/cron.d` entries no package owns | spool files as they are |
| `httpd.carry-sites` | vhosts in `conf.d` no package owns, `DocumentRoot` trees | copy, then the existing hop fixes adapt them (2.2 to 2.4) |
| `php.carry-ini` | the owner's `php.ini` changes | the diff against the old package's pristine file, replayed as `kv.set` (the same thing wend#54 needs in place) |
| `mysql.carry-data` | databases and grants | dump on the old root's own server, restore into the new one. Datadir copy plus `mysql.upgrade-data` is shorter but MariaDB 5.5 to 10.11 skips five majors, and upstream supports one at a time |
| `postfix.carry`, `firewalld.carry`, `fail2ban.carry`, `chrony.carry` | `postconf -n`, zone XML, `jail.local`, sources | as their own parsed form, never whole files over the target's |
| `net.carry` | interfaces | `net.ifcfg-to-keyfile`, which exists |
| `service.carry` | what starts at boot | `service.record` on the old root, `service.enable` on the new |

The box's checks are the acceptance test, unchanged: they don't care how the root was made. A service with no mover
blocks the move and is named (or is sheltered, option 3 in the notes, once that exists). Family switchers need movers
per family pair and a name map (httpd to apache2); none of that changes placement, flip or rollback. The machine-state
idea lands here too: a fresh target root is exactly "`/usr` by the target's hash", and the movers are the `/etc` and
`/var` half. When the fresh root comes from a prebuilt image (OSTree or casync, never our own CAS), placement still
works as above.

## What preflight checks first

Before anything is written, and in this order, refusing with the reason:

1. The route's own verdict with relocate assumed: the moving filesystems count as fresh v5, everything else as
   wend#67 judges it. If the in-place route already fits, relocate isn't offered.
2. Room: free extents or a named disk, as above, at least the computed size.
3. Boot: BIOS with GRUB 2 and a separate `/boot` (EL7's default), or UEFI with an ESP once proven. GRUB legacy,
   `/boot` on md or LVM, LUKS anywhere, and a root that isn't on LVM or a plain partition are refused for now.
4. Nothing on the old root that relocate can't see: no v4 filesystem it would leave unmounted, no bind or overlay
   mounts of `/` (containers' storage gets copied like any directory, but `ftype=0` overlay stores are named).
5. No pending kernel update, no running rpm/yum, and the box's last boot is the running kernel (so the copy's
   initramfs is built for the kernel that will boot).

## How it's proven

The chroot tier can't: a chroot box has no disks, no LVM, no bootloader. CI keeps replaying the in-place route as it
does; relocate is a harness step like the kernel steps, proven by the VM tier and maintainer-run only, never from a
runner a public PR can trigger.

The VM proof needs:

- a box `el/centos7-lamp-xfsv4`: centos7-lamp's packages and checks, installed by kickstart from the 7.9 tree with
  every XFS made `-m crc=0 -n ftype=0` (exactly what 7.0 to 7.2's mkfs made; the format is the format), a 500 MB
  `/boot`, LVM `root` and `swap` filling the disk as a default install does, and a second empty 30 GB disk. Today
  vm.py's kickstart is ext3 on a plain partition: the box needs `[vm]` keys for the layout (`fs`, `mkfs_options`,
  `lvm`, `extra_disks`), which is a VM-tier spec change in its own PR;
- relocate as a vm.py step before the first hop, then the route, rebooting as usual, then a forced reset during the
  trial boot (it must come back on the old root), and a rollback boot after the last hop (the old root's checks pass,
  as they did at the start);
- the in-place centos7-lamp route's VM proof first (or alongside), because its GRUB reinstall gap is relocate's too,
  and it's the baseline the relocated box is compared against: same checks, same final package set, plus downtime
  and disk used.

## Order of work

1. This doc (spec PR).
2. VM tier: layout keys in `[vm]`, the `el/centos7-lamp-xfsv4` VM box (box PR on its own).
3. Harness: BIOS GRUB reinstall step for EL8+ hops (in-place route needs it anyway); remi proves the in-place
   centos7-lamp VM run.
4. Harness: `relocate` in `tools/vm.py` (adapter `[relocate]` table: the mkfs command, the GRUB stub and config
   commands, the initramfs rebuild), with its SPEC section. remi proves BIOS: relocate, trial reset, route, rollback.
5. Preflight: the relocate verdict (room, boot layout, moving filesystems) in the Rust client; `wend commit` and
   `wend rollback` once the client walks live roots.
6. UEFI: OVMF box, `BootNext` trial, remi proof.
7. Then movers, on Foresight or EL7 to Debian.
