# Rebuild and carry: design, first case old-XFS CentOS 7

Status: design, 2026-10-13. Not normative yet. Each piece moves into SPEC.md in the PR that builds it, as AGENTS.md
asks (spec before ops). Background: the "defunct to living" notes (option 2) and wend#67.

## The problem this case brings

Every default (anaconda) CentOS 7 install from 7.0, 7.1 or 7.2 media has XFS in the old v4 format (`crc=0`, and
`ftype=0`) on `/` and `/boot`, and on any other XFS volume made then (`/home` on a default install with a big disk).
Updates never convert it, and XFS has no in-place v4 to v5 conversion: `xfs_admin` can't add CRCs, and XFS can't
shrink, so there's no make-room-and-reformat trick either. AlmaLinux 10's kernel is built without `XFS_SUPPORT_V4`, so
it can't mount any of those filesystems. Preflight (wend#67) therefore stops these machines at Alma 9. Those were the
2014-2016 installs, which is a lot of the ~1.6M EL7 machines EPEL still counts.

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

Size: what the old root uses (`df` used, every v4 XFS that gets carried, see below), plus what the old `/boot` uses,
plus the route's `disk_mib` and `boot_mib` summed over its hops (`/boot` is now a directory on `/`, and SPEC leaves a
`/boot` on `/` to `disk_mib`), plus 20% for the logs and caches the walk leaves behind. Preflight states the number.

Booting from the new root on a second disk needs the firmware to expose that disk (BIOS int13, or UEFI's own driver).
QEMU does; some HBA option ROMs and hypervisors expose only the boot disk. A VM proof proves QEMU, so the trial boot
(below) is what protects a real machine here. After commit the machine depends on both disks.

**Which filesystems move.** Every mounted filesystem a hop's kernel can't mount (wend#67's `fs` table) moves into the
new root as a directory: v4 XFS `/home`, `/var`, `/srv` become part of the new root. A filesystem every hop can mount
stays where it is and keeps its fstab line. A v4 filesystem that isn't mounted now (`noauto`) is named in the verdict
and blocks: wend doesn't know what's on it.

**Making the filesystem.** The new root is made by the **old** release's `mkfs.xfs` (EL7.9's xfsprogs 4.5: v5,
`crc=1`, `ftype=1`, no `reflink`, `bigtime` or `inobtcount`). It has to mount under both the kernel that's running now
(3.10, while the copy is made and on every rollback check) and every kernel the route boots; the oldest tool that must
read it makes it. Alma 10 mounts that format (it warns that dates end in 2038; `xfs_admin -O bigtime=1` fixes that,
but needs xfsprogs 5.10+ and the filesystem unmounted, so only from rescue media, and it isn't part of relocate).

## Refresh GRUB first (BIOS)

EL7 updates never run `grub2-install`, so `core.img` in the MBR and the modules in `/boot/grub2/i386-pc` are whatever
the machine was installed with. GRUB's XFS v5 reader is from mid-2015 (CentOS carries it as c7 grub2 patch 0170), so a
7.0 or 7.1 machine's GRUB can't read the new root at all, and 7.2's may not. So once preflight passes and before
anything else, the running 7.9's own `grub2-install` goes onto the boot disk with the old `/boot/grub2` as its prefix
(what leapp's grub core update does): same prefix, same config, a GRUB that reads v5. It is the only write to the MBR,
it runs unconditionally (a 7.9-installed machine just gets the same GRUB again), and a failure here stops relocate
before anything else changes, with the old config still booting. If relocate stops later, the refresh is harmless: the
refreshed GRUB reads the old config as it is.

It runs before the copy so the new root's `/boot/grub2` carries the refreshed modules too. `core.img` and every module
under whatever prefix is active must come from the same build: GRUB modules have no version check, and once the stub
points `prefix` at the new root, every module not yet loaded (`linux16`, `gzio`, the entries' `insmod`s) is read from
there.

## The copy

The new root is mounted at `/run/wend/new`. Services keep running for the first pass. Two passes:

1. `rsync -aHAXS --numeric-ids` from a bind mount of `/` (`mount --bind / /run/wend/src`, so files hidden under
   mount points such as `/dev`'s static nodes come too) and of each moving filesystem, into the new root, live.
2. Stop every service the box enables (the `service.record` list) except the ones the walk itself needs (`sshd`,
   `journald`, `auditd`), `sync`, run rsync again with `--delete`, then leave them stopped until the reboot. Downtime
   is the second pass plus one boot.

Then, in a chroot of the new root only:

- fstab gets the new root's UUID and drops the lines of the filesystems that moved, including `/boot`, which becomes
  a directory in the new root holding a copy of the old `/boot` (see the flip);
- SELinux labels are fixed offline, from outside the chroot: `setfiles -r /run/wend/new
  /run/wend/new/etc/selinux/targeted/contexts/files/file_contexts /run/wend/new` (skipped when the copy's config says
  `SELINUX=disabled`). Never
  `/.autorelabel`: EL7's relabel service reboots when it's done, even in permissive mode, and that reboot would land
  on the old root and end the trial;
- `GRUB_CMDLINE_LINUX` in the copy's `/etc/default/grub` gets the new LV in `root=` and `rd.lvm.lv=` in place of the
  old root's (swap kept), and `grub2-mkconfig` writes the copy's `grub.cfg` from it;
- the initramfs of every kernel that `grub.cfg` lists is rebuilt (`dracut -f --kver <v>`, with `--add lvm`), since an
  old host-only initramfs activates only the old LVs and hangs. A kernel that can't be rebuilt is dropped from the
  copy's `grub.cfg`.

`/etc/machine-id` and the ssh host keys are the old ones on purpose: it's the same machine.

## The flip

**BIOS (GRUB 2, refreshed above).** The MBR's `core.img` points at the old `/boot` partition, and keeps doing so.

1. **The stub.** On the old `/boot`, move `grub2/grub.cfg` to `grub2/grub.cfg.wend-old` and write a small
   `grub2/grub.cfg`. Entry 0 is the new root: `insmod lvm xfs`, `search --no-floppy --fs-uuid --set=root <new>`,
   `set prefix=($root)/boot/grub2`, `configfile $prefix/grub.cfg`. Setting `prefix` matters: the new root's config
   then loads its own `grubenv` (its `saved_entry`, and on EL8 its `kernelopts`), not the old `/boot`'s. Entry 1 is
   "wend rollback", `configfile /grub2/grub.cfg.wend-old`, with the old `prefix` and `grubenv`, so the old root
   comes up on its own saved default.
2. **The trial.** The stub has variables of its own in the old `/boot`'s `grubenv`, never `saved_entry` or
   `next_entry`, which belong to the two configs: `wend_default=1` and `wend_next=0`. The stub boots `wend_next` if
   set and clears it with `save_env` first, else `wend_default`. So the **trial boot** goes to the new root once, and
   any reset after that comes back to the old root. GRUB writes `grubenv` in place on a plain partition (it refuses on
   LVM and md), which is why the stub lives on the old `/boot`.
3. **Flush.** GRUB doesn't replay the XFS log, so after writing the stub and `grubenv`, `fsfreeze -f /boot && fsfreeze
   -u /boot`, which writes the metadata home (a known EL7 trap with `grub2-reboot` on an XFS `/boot`).

When the trial boot comes up and the box's checks pass, the commit-to-trial step mounts the old `/boot` (the new
root's fstab no longer does; EL7's kernel, still running here, reads v4), sets `wend_default=0`, flushes it the same
way, and unmounts it. The new root is now the default and the old one is the rollback entry.

Nothing on the old root changes. The old `/boot` changes by the refreshed GRUB (its modules, plus the MBR and the gap
after it), the stub, the renamed config and `grubenv`. Undoing the flip from a rescue shell is still "rename one file
back": the refreshed GRUB reads the old config as it is.

**UEFI (shim, EL7's grubx64.efi).** The ESP (vfat) is mounted at `/boot/efi` on the old root and every kernel mounts
it, so it stays where it is and both roots mount it. No refresh is needed: `grubx64.efi` is a file of the `grub2-efi`
package with every module its config uses built in (`lvm`, `xfs`, `loadenv`, `search_fs_uuid`, `linuxefi`...), so
7.9's updates already put a GRUB that reads v5 on the ESP, and there are no loose modules to mismatch (under Secure
Boot GRUB loads none from disk at all). A second loader directory with copied shim and GRUB plus `BootNext` would work
too, since EL7's GRUB reads the `grub.cfg` beside its own image first, but it means NVRAM writes, which some firmware
handles badly, and a second trial mechanism. So UEFI uses the same stub as BIOS: `EFI/centos/grub.cfg` becomes the
stub (the old config moves to `grub.cfg.wend-old` beside it), with the same `wend_default`/`wend_next` in
`EFI/centos/grubenv` (GRUB writes `grubenv` in place on vfat), the same entry 0 into the new root and entry 1 into the
old config. No NVRAM change at all, and vfat needs no log flush, just `sync`. In the copy, `/etc/grub2-efi.cfg` (which
grubby and `grub2-mkconfig` write through, and which points at `EFI/centos/grub.cfg`) is repointed at the copy's own
`/boot/grub2/grub.cfg`, or every kernel install in the new root would overwrite the stub. The copy's
`/boot/grub2/grubenv` may be a symlink to `../efi/EFI/centos/grubenv` (EL8+ makes it one; check on a 7.9 UEFI
install): it becomes a real file in the copy, so the new root's config and its tools read and write the env file entry
0's `prefix` points at, which matters once EL8 keeps `kernelopts` there. EL8+'s shim and GRUB install to
`EFI/almalinux/` with that prefix, and nothing creates an NVRAM entry for them on an in-place walk (the fallback
loader, `EFI/BOOT/fbx64.efi` with `BOOTX64.CSV`, does it only when the machine boots from the removable path), so the
in-place route needs a UEFI step there too, the counterpart of the BIOS GRUB reinstall below, and on a relocated box
that step carries the rollback entry over. Preflight keeps refusing UEFI until a UEFI VM proof (that's already the
rule for every route).

**Afterwards, the route.** The in-place route walks the copy, booted, exactly as it walks any 7.3+ box. One gap shows
up here that the in-place route has too: on BIOS, nothing reinstalls GRUB, so once the box is on EL8 or later
(where BLS entries start) its `core.img` is still EL7's, which has no `blscfg`. Leapp runs `grub2-install` on BIOS
for this. wend's in-place EL7 route needs the same harness step before its VM proof can pass; relocate inherits it.
On a relocated box:

- the install target is the disk the firmware boots, the one whose MBR holds the stub's `core.img`, which with a
  second disk is not the disk `/boot` is now on. Picking it by `grub-probe -t device /boot`, as the Debian step
  does, would leave EL7's `core.img` in the first disk's MBR and brick the box. Relocate records the boot disk (by
  `/dev/disk/by-id`) and this step uses it;
- while the stub is in charge (until the old root is dropped, so through every hop `wend upgrade` walks, before and
  after commit: SPEC.md, "Upgrading a running machine"), the install goes into the stub's home, with the stub's prefix, never the new root's
  `/boot/grub2`: the new root is on LVM, where GRUB can't `save_env`, and the stub's one-shot trials and one-shot
  rollback need it to. The stub's home is a plain partition every kernel on the route mounts (the old `/boot`
  unless it's XFS v4, which Alma 9 and later can't mount; then a new XFS v5 partition on the spare disk, see the
  SPEC). From then on the stub's entry 0 no longer moves `prefix` (the new root's modules are a newer build than
  `core.img`) and reads the new root's `grubenv` (`load_env -f`, read-only) before its BLS config, for EL8's
  `kernelopts`; entry 1 runs EL7's entries (`linux16`, `initrd16`) under the EL8+ GRUB, and since the stub never
  writes `saved_entry`, the old config falls to its entry 0. The rollback boot after the last hop is what proves
  this works;
- once the old root is dropped (below), the stub is gone and the prefix is the new root's `/boot/grub2`, like any
  in-place box. Commit alone doesn't do that.

## Rollback and commit

The old root and old `/boot` are never written after the flip, except for the stub's home when the old `/boot` is
that home (its `grubenv`, and the modules each GRUB reinstall puts under its prefix).
So:

- **Before the trial boot passes**: reset. The trial was one-shot, on BIOS and UEFI alike.
- **After**: boot the stub's old entry (`wend rollback` sets it as the next boot, or as the default; the stub stays in
  charge, with its home on a partition GRUB can write, through every hop until commit, see above). Data written on the
  new root since the flip stays there, readable from the old root only through the new LV (the old kernel mounts the
  new root fine, it made it). The run says this in words: rollback loses what happened after the flip, unless you copy
  it back. A rollback that stays for good (`wend rollback --permanent`) also renames `grub.cfg.wend-old` back over the
  stub: the old root's `/etc/grub2.cfg` and `/etc/grub2-efi.cfg` still point at that path, so its next kernel update
  would otherwise have grubby edit the stub.
- **Commit** (`wend commit`, never automatic): makes the new root the stub's default (`commit_trial`) and nothing
  else. It deletes nothing: the stub, its old entry and the old root stay, so `wend rollback` still works after it.
- **Drop the old root** (a separate step after commit, never automatic, and never part of commit): moves the prefix
  to the new root's `/boot/grub2` with the running release's own `grub2-install` onto the boot disk, which retires
  the stub and its rollback entry, then removes the old LVs that moved (`root`, and `home` or `var` if they were v4),
  or tells the owner which partitions are now free (wend never repartitions). The old `/boot` partition is left
  unused and named. Reformatting it is the owner's call, since nothing needs it.

## State movers (the second stage, sketched so the first doesn't block it)

When the new root is a fresh target install instead of a copy, these fill it. They're fixes in shape: preconditions,
typed ops, checks. Each one names its source path in the old root, which is mounted read-only at `/run/wend/old`
while the movers run in a chroot of the new root. Several need ops that don't exist yet (copying a file out of
`/run/wend/old`, running the old root's own server to dump its data), and AGENTS.md's rule 1 holds: each mover is an
op proposal with its SPEC section before any fix uses it. Relocate itself is different: like the kernel steps, it is
harness work with the adapter's command lists, reviewed as a spec change. The LAMP box needs, at least:

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
  `/boot`, LVM `root` and `swap` filling the disk as a default install does, and a second empty 30 GB disk. Its GRUB
  is 7.9's, so it can't show the old-`core.img` failure the GRUB refresh exists for. The refresh is unconditional for
  that reason, and a second VM case gets its `core.img` and modules from 7.0 media's grub2 (install from 7.0's
  `pxeboot`, or put 7.0's `grub2-install` output on the disk before the update) and boots all the way to the new root
  to prove the refresh. Today the EL7 kickstart (remi's, landing separately) is ext3: the box needs `[vm]` keys for
  the layout (`fs`, `mkfs_options`, `lvm`, `extra_disks`), which is a VM-tier spec change in its own PR;
- relocate as a vm.py step before the first hop, a forced reset during the trial boot (it must come back on the old
  root), the trial again, all the way to the new root's checks, and the commit-to-trial, then the route, rebooting as
  usual, and a rollback boot after the last hop (the old root's checks pass, as they did at the start);
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
6. UEFI: OVMF box, the stub on the ESP, the EL8+ loader step, remi proof.
7. Then movers, on Foresight or EL7 to Debian.
