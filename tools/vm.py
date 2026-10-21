"""The VM tier (SPEC.md "VM tier"): replay a route on a real install in a KVM guest, with a real reboot after every hop.

A chroot can't see kernels, bootloaders, init or networking. This can: the box is installed by the release's own
installer (a kickstart or a preseed made from box.toml), the hop's commands are the ones the chroot harness runs
(walk.Walk's hop_cmds, the same compile), they run inside the running guest over ssh, and after each hop the guest
reboots and has to come back reachable on its own address with its services up.

Maintainer-run only. It needs /dev/kvm, qemu-system-x86_64 and qemu-img, a few GB per box, and it runs the guest's
code on the host that runs it: never wire it to a CI runner a pull request can trigger. Its runs go to .wend/vm/,
not runs/ (runs/ holds what CI proved).

  python3 tools/vm.py build BOX            install the box in a VM (cached by box file + adapter)
  python3 tools/vm.py replay ROUTE BOX     walk ROUTE on a fresh copy of it, rebooting after every hop (a box on a
                                           rung ROUTE passes through walks the hops after it: SPEC "Route")
  python3 tools/vm.py shim-stop PID        stop the mirror shim (tools/mirror.py) running as PID, unless a VM run is
                                           using it (exit 3: left running); runs that start meanwhile wait, then start
                                           a fresh one from their own checkout
  python3 tools/vm.py replay ROUTE BOX --from RUN HOP
                                           the same from RUN's disk after the hop before HOP (partial-pass at best)
  python3 tools/vm.py --extra-disk GB relocate BOX
                                           relocate a fresh copy of BOX onto the first extra disk and prove the trial
                                           boot, a reset back to the old root, commit and a rollback boot (SPEC
                                           "Relocate"; with --root-fs xfs-v4 --lvm, an EL 7.0-7.2 install; on a
                                           CentOS 5 or 6 box, the adapter's [relocate_grub_legacy])
  python3 tools/vm.py rehearse ROUTE BOX --client WEND
                                           `wend rehearse` (the client binary WEND, run as root on this host) from a
                                           fresh copy of BOX, running: copies its root over ssh, walks ROUTE on the
                                           copy pinned by CI's newest passing run in runs/, writes the plan, and
                                           passes only if each hop fired that run's fixes and the guest was only read
  python3 tools/vm.py --extra-disk GB --relocate replay ROUTE BOX
                                           the same relocate (trial, commit) before ROUTE's first hop, every hop on the
                                           new root, then a rollback to the old root and forward again after the last
  --uefi (before the command)              the same on UEFI firmware (OVMF) instead of BIOS: its own box, install
                                           and NVRAM, and after every boot the guest must still be running on UEFI
  --no-hwaddr                              (before the command, EL7 kickstart) the installer isn't told the NIC
                                           by MAC, so ifcfg-eth0 has no HWADDR, as a stock install (wend#74)
  --selinux enforcing|none                 (before replay, EL) the box with SELinux as its owner set it, made from a
                                           copy of the installed box: enforcing (the targeted policy and a relabel),
                                           or none (no policy package, no /etc/selinux/config: CentOS 4 to 6). Every
                                           replay holds a box to the SELinux mode it started in, after every hop
  --root-fs FS, --lvm, --extra-disk GB     (before the command, EL7 kickstart on) the box installed another way:
                                           FS overrides [vm].root_fs; --lvm puts / and swap on LVM in a VG `centos`
                                           filling the disk, beside /boot, as a default EL7 install does (on CentOS
                                           5 and 6, as their installers name them: releases.<release>.install.lvm);
                                           --extra-disk adds an empty disk of GB (repeatable; on CentOS 5 and 6,
                                           added after the installer). Its own box and runs

A box with a [shelter] plan (SPEC "Sheltering an app", Proof) needs root: its shelter is built and proved in a chroot copy
of the box first, copied into the guest before the hop that drops Python 2, and probed for confinement after every
reboot from that hop on, as installed (SELinux permissive) and in an enforcing window.

Networking is QEMU user mode: nothing on the host changes. The guest reaches the mirror shim at 10.0.2.2. Each guest's
ssh is forwarded to a free port on 127.0.0.1 of its own, so one host runs several VM replays side by side (each guest
takes 2 CPUs and 3 GB); they share the mirror shim and the installed boxes. A box is installed once per cache key
(the box file, the adapter, and the part of this file the install runs: build_source), and a run that needs a box
another run is installing waits for it. A run holds a shared lock on /var/tmp/wend-shim/<port>.users for each shim
port the adapters name, for as long as it lives, so the shim isn't stopped under it: stop the shim only with
`shim-stop`, which refuses while any run holds its port's lock. The lock follows the port, not $WEND_WORK: runs with
different work directories still share the one shim listening there.
Disks go under $WEND_WORK/vm (default /var/tmp/wend/vm, which the chroot
harness, run as root, may own: point WEND_WORK somewhere the user can write). Every run records the commit it ran
(run.json "head"), so a run on a stale head shows as one.
"""
import ast
import fcntl
import glob
import gzip
import hashlib
import http.client
import http.server
import json
import os
import re
import select
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request

sys.dont_write_bytecode = True
TOOLS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TOOLS)
sys.path.insert(0, TOOLS)
import backends  # noqa: E402
import check  # noqa: E402
import sandbox  # noqa: E402
import shelter  # noqa: E402
import walk  # noqa: E402

WORK = os.path.join(sandbox.WORK, "vm")
GUEST_HOST = "10.0.2.2"  # the host, as QEMU user-mode networking shows it to the guest
MAC = "52:54:00:12:34:56"
# The box's static address. Not 10.0.2.15, which slirp's DHCP hands out: the forwards point here, so a box that falls
# back to DHCP is unreachable and fails the hop, instead of passing because DHCP happened to give the same address.
ADDR = "10.0.2.20"
# The firmware the guests boot: "bios" (SeaBIOS) or "uefi" (OVMF without Secure Boot, the host's edk2-ovmf build).
# A UEFI guest's NVRAM (boot entries) is a copy of OVMF's variable store next to each disk, carried along every overlay.
FIRMWARE = "bios"
OVMF_CODE, OVMF_VARS = "/usr/share/OVMF/OVMF_CODE_4M.fd", "/usr/share/OVMF/OVMF_VARS_4M.fd"
# what the guest says it booted with; a UEFI box that comes up on anything else fell back to a path wend didn't prove
FW_NOW = "{ [ -d /sys/firmware/efi ] && echo uefi || echo bios; } 2>/dev/null"
# The disk layout, from the command line (main): how anaconda lays out the first disk, and the empty disks beside it.
# The chroot box is the same either way (a chroot has no disks), so these are install choices, not other boxes: CentOS 7
# from 7.0 to 7.2 media is XFS v4 on LVM, and rebuild-and-carry (docs/rebuild-and-carry.md) needs room on a second disk.
# no_hwaddr (--no-hwaddr): the installer isn't told the NIC by MAC (no ifname=), so its ifcfg-eth0 has no HWADDR, as
# a plain kickstart or interactive EL7 install writes it (wend#74); an install choice too: the chroot box writes its own.
LAYOUT = {"root_fs": None, "lvm": False, "extra_disks": [], "no_hwaddr": False}
# --selinux MODE (main): the box as its owner set SELinux, made from the installed (permissive) box rather than a second
# install, as an admin would turn it: "enforcing" (the targeted policy, SELINUX=enforcing and a full relabel, as a stock
# install runs) or "none" (no policy package and no /etc/selinux/config, so no policy loads: CentOS 4 without
# selinux-policy-targeted, wend#158). None: the box as installed. Its own disk beside the box's, never its cache key.
SELINUX = None
# what SELinux is doing now, read from selinuxfs (EL4 to 6 mount it at /selinux, EL7 on at /sys/fs/selinux), so a box
# without getenforce reads too; then the config's SELINUX= ("none" without the file, "unset" without the line). After every boot of a replay the
# mode has to be the one the box started in (SPEC "VM tier"). Disabled is also no policy loaded: EL4 to 6 mount
# selinuxfs and say enforce 0 even when init loaded none, and then every process is still the kernel's initial SID,
# a context with no ":" (a loaded policy gives the shell a user:role:type one)
SELINUX_NOW = ("( m=Disabled; for d in /sys/fs/selinux /selinux; do if [ -e $d/enforce ]; then "
               "tr -d '\\0' < /proc/self/attr/current 2>/dev/null | grep -q : || break; "
               "[ \"$(cat $d/enforce)\" = 1 ] && m=Enforcing || m=Permissive; break; fi; done; echo $m; "
               "if [ -f /etc/selinux/config ]; then v=$(sed -n 's/^SELINUX=//p' /etc/selinux/config | tail -1); "
               "echo ${v:-unset}; else echo none; fi )")
# how each --selinux mode is made on a running copy of the box (then a reboot: the relabel, or the boot without a policy)
SELINUX_MAKE = {
    "enforcing": "rpm -q selinux-policy-targeted >/dev/null || yum -y install selinux-policy-targeted || exit 1; "
                 "if [ -f /etc/selinux/config ]; then sed -i -e 's/^SELINUX=.*/SELINUX=enforcing/' "
                 "-e 's/^SELINUXTYPE=.*/SELINUXTYPE=targeted/' /etc/selinux/config; else "
                 "printf 'SELINUX=enforcing\\nSELINUXTYPE=targeted\\n' > /etc/selinux/config; fi; "
                 # sshd on a port the policy doesn't give it (every EL box's 2222) can't bind once enforcing: an owner
                 # who moved it labelled the port. EL4 has no semanage, and its targeted policy leaves sshd unconfined
                 "for p in $(awk 'tolower($1) == \"port\" && $2 != 22 {print $2}' /etc/ssh/sshd_config); do "
                 "command -v semanage >/dev/null || yum -y install policycoreutils-python || "
                 "yum -y install policycoreutils-python-utils || true; if command -v semanage >/dev/null; then "
                 "semanage port -a -t ssh_port_t -p tcp $p || semanage port -m -t ssh_port_t -p tcp $p || exit 1; "
                 "semanage port -l | grep -q \"^ssh_port_t .*[ ,]$p\\(,\\|$\\)\" || "
                 "{ semanage port -l | grep ^ssh_port_t; exit 1; }; "
                 "elif grep -q 'release 4' /etc/redhat-release; then echo \"no semanage: sshd's port $p stays unlabelled\"; "
                 "else echo \"sshd listens on $p and semanage can't be had to label it\"; exit 1; fi; done; "
                 "grep -qx SELINUX=enforcing /etc/selinux/config && touch /.autorelabel",
    "none": "for p in selinux-policy-targeted selinux-policy-strict selinux-policy-mls; do "
            "! rpm -q $p >/dev/null || rpm -e $p || exit 1; done; rm -f /etc/selinux/config"}
SELINUX_IS = {"enforcing": "Enforcing enforcing", "none": "Disabled none"}
# replay --relocate (main): relocate the box onto the first extra disk before the route's first hop, and roll back to
# the old root and forward again after its last (SPEC "Relocate", "Walking a route on a relocated root")
RELOCATE = False
# on UEFI, the boot chain a hop leaves: the firmware's boot entries and every EFI binary on the system partition, by
# hash, so a replay shows whether each release refreshed shim and GRUB or booted on what an older one left
EFI_CHAIN = ("{ efibootmgr -v || echo 'no efibootmgr'; } 2>/dev/null | grep -E '^(BootCurrent|BootOrder|no efibootmgr)|HD\\('; "
             "cd /boot/efi 2>/dev/null && find EFI -type f -iname '*.efi' | sort | xargs -r sha256sum")
# the chroot harness's environment for box commands (tools/sandbox.py ENV), so a hop runs the same in both tiers
ENV = ("export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin LANG=C HOME=/root TERM=dumb "
       "DEBIAN_FRONTEND=noninteractive; umask 022; ")
# The emulated hardware, per start release (releases.<id>.install): a machine keeps its disk and NIC through every hop,
# so an old installer's kernel has to know them. etch's 2.6.18 has no virtio and doesn't know QEMU's AHCI; an LSI SCSI
# disk and an e1000 work from etch's installer to trixie, and keep the disk sda all the way.
DISKS = {"virtio": ["-drive", "file={disk},if=virtio,format=qcow2"],
         "lsi53c895a": ["-drive", "id=d0,file={disk},if=none,format=qcow2", "-device", "lsi53c895a,id=scsi0",
                        "-device", "scsi-hd,drive=d0,bus=scsi0.0"]}
# each extra disk ({i} from 1), on the same controller as the first; and the first disk's name in the guest
DISKS_EXTRA = {"virtio": ["-drive", "file={disk},if=virtio,format=qcow2"],
               "lsi53c895a": ["-drive", "id=d{i},file={disk},if=none,format=qcow2", "-device",
                              "scsi-hd,drive=d{i},bus=scsi0.0"]}
FIRST_DISK = {"virtio": "vda", "lsi53c895a": "sda"}
NICS = {"virtio": "virtio-net-pci", "e1000": "e1000"}
# the guest's network as the live checks compare it after every boot: addresses, default route, resolvers
NET = ("(ip -o -4 addr 2>/dev/null || /sbin/ip -o -4 addr) | "
       "awk '$2 != \"lo\" {print $2, $4}' | sort; "
       "(ip route 2>/dev/null || /sbin/ip route) | awk '/^default/ {print $1, $2, $3}'; "
       # the resolvers: resolv.conf's, except systemd-resolved's local stub, which stands for the upstream servers
       # resolved lists in its own /run/systemd/resolve/resolv.conf (bionic on); so the stub appearing isn't a change
       "{ grep '^nameserver' /etc/resolv.conf | grep -vx 'nameserver 127.0.0.53'; "
       "if grep -qx 'nameserver 127.0.0.53' /etc/resolv.conf; then grep '^nameserver' /run/systemd/resolve/resolv.conf; fi; "
       "} 2>/dev/null | sort -u")


def shelter_bridges(g, plan):
    """The NET lines of the shelters' own bridges ("<interface> <gateway>/<len>"), as podman reports each app's
    network wend-<app> (shelter.start creates it on 10.90.<K>.0/24): the hop that brings a shelter up adds that
    bridge, and it isn't the box's network changing. Only those exact lines; any other new address still counts."""
    out = set()
    for app in sorted(plan):
        rc, o = g.ssh(f"podman network inspect wend-{app} --format "
                      "'{{.NetworkInterface}} {{range .Subnets}}{{.Gateway}} {{end}}' 2>/dev/null")
        f = o.split()
        if rc == 0 and len(f) >= 2 and f[0] and all(x.startswith("10.90.") for x in f[1:]):
            out |= {f"{f[0]} {gw}/" for gw in f[1:]}
    return out


def net_read(g, plan):
    """NET, without the run's own shelter bridges (shelter_bridges)."""
    o = g.ssh(NET)[1]
    skip = shelter_bridges(g, plan) if plan else set()
    return "".join(x for x in o.splitlines(keepends=True) if not any(x.startswith(b) for b in skip))


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def free_port():
    """A port on 127.0.0.1 nothing listens on right now, for a guest's ssh forward. Another process can take it before
    QEMU binds it: Guest.start asks again when QEMU says so."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Locked:
    """An exclusive flock on `path` (created if need be) for the with block: one run installs a box, the others wait
    for it and then use it. Said once when it has to wait."""

    def __init__(self, path, what):
        self.path, self.what = path, what

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.f = open(self.path, "a")
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log(f"another run is {self.what}; waiting for it")
            fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        self.f.close()  # closing the file drops the lock


# Where the shim locks live: one per port, the same for every run on the host whatever its $WEND_WORK, since the
# port is what runs share (the first run's shim serves them all). Read-only opens, so runs as different users share it.
SHIM_LOCKS = "/var/tmp/wend-shim"
HOLDING = []  # this run's own holds


def shim_lock(port):
    """The open lock file for a shim port, in SHIM_LOCKS. Not /var/tmp itself: anyone can unlink and recreate a file in
    a sticky world-writable directory, and a shim-stop that locked the replacement would stop a shim a run is using.
    So the directory must be a real directory owned by root or by this user (or the user sudo ran as), writable by
    nobody else; anything else is refused, never used."""
    try:
        os.mkdir(SHIM_LOCKS)
        os.chmod(SHIM_LOCKS, 0o755)  # whatever the umask: runs as other users open the locks read-only
    except FileExistsError:
        pass
    st = os.lstat(SHIM_LOCKS)
    owners = {0, os.geteuid()} | ({int(os.environ["SUDO_UID"])} if os.environ.get("SUDO_UID", "").isdigit() else set())
    if not stat.S_ISDIR(st.st_mode) or st.st_uid not in owners or st.st_mode & 0o022:
        raise SystemExit(f"{SHIM_LOCKS} isn't a directory owned by root or this user and writable by nobody else; "
                         "refusing to use the mirror shim's locks there")
    fd = os.open(os.path.join(SHIM_LOCKS, f"{int(port)}.users"), os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    if os.fstat(fd).st_uid == os.geteuid():  # whatever the umask, as the directory
        os.fchmod(fd, 0o644)
    return fd


def shim_ports():
    """The ports of every adapter's [mirror] shim."""
    out = set()
    for f in sorted(os.listdir(os.path.join(ROOT, "adapters"))):
        if f.endswith(".toml"):
            a = load(f"adapters/{f}")
            if "mirror" in a:
                out.add(int(a["mirror"]["shim"].rsplit(":", 1)[1]))
    return sorted(out)


def use_shim(ports=None):
    """Hold a shared lock for each shim port (shim_ports) until this process exits (the kernel drops them then,
    however it ends), before anything starts or reads a shim. Waits while a shim-stop holds one exclusively."""
    if not HOLDING:
        for port in shim_ports() if ports is None else ports:
            fd = shim_lock(port)
            fcntl.flock(fd, fcntl.LOCK_SH)
            HOLDING.append(fd)


def shim_stop(pid):
    """Stop the mirror shim running as pid, only while no VM run holds the lock of the port it listens on (from its
    own command line): the exclusive lock is held through the kill, so a run that starts meanwhile waits for it and
    then starts its own shim. 0 stopped (or wasn't running), 3 in use and left running, 2 pid isn't a mirror shim,
    1 it didn't exit."""
    fd = None
    try:
        # a pidfd names this process, not its number: if it exits and the number is reused, signals through it fail
        # instead of reaching the newcomer. /proc/<pid> is read after it's open and before the first signal, while
        # the pidfd says the process is still alive, so the files read are that process's.
        try:
            pfd = os.pidfd_open(pid)
        except ProcessLookupError:
            print(f"no process {pid}: nothing to stop")
            return 0
        try:
            try:
                cmd = [a.decode(errors="replace") for a in open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")]
                cwd = os.readlink(f"/proc/{pid}/cwd")
                signal.pidfd_send_signal(pfd, 0)
            except (FileNotFoundError, ProcessLookupError):
                print(f"no process {pid}: nothing to stop")
                return 0
            # a wend checkout's tools/mirror.py (a tools/ directory with vm.py and sandbox.py beside it), not any
            # mirror.py
            # mirror.py, then its PORT and CACHE_DIR
            at = next((i for i in (1, 2) if i < len(cmd) and os.path.basename(cmd[i]) == "mirror.py"), None)
            t = os.path.dirname(os.path.join(cwd, cmd[at])) if at else ""
            if not (at and os.path.basename(t) == "tools" and
                    all(os.path.exists(os.path.join(t, f)) for f in ("vm.py", "sandbox.py")) and
                    at + 1 < len(cmd) and cmd[at + 1].isdigit()):
                print(f"process {pid} isn't a wend checkout's tools/mirror.py: {' '.join(cmd)[:200]}")
                return 2
            fd = shim_lock(cmd[at + 1])
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print(f"the mirror shim on port {cmd[at + 1]} is in use by a VM run; left running (pid {pid})")
                return 3
            for sig, wait, said in ((signal.SIGTERM, 10, "stopped"),
                                    (signal.SIGKILL, 10, "killed (it ignored SIGTERM)")):
                try:
                    signal.pidfd_send_signal(pfd, sig)
                except ProcessLookupError:  # gone already
                    sig = None
                if sig is None or select.select([pfd], [], [], wait)[0]:  # a pidfd reads ready once its process exits
                    print(f"{said if sig else 'stopped'} the mirror shim (pid {pid})")
                    return 0
            print(f"the mirror shim (pid {pid}) didn't exit after SIGKILL")
            return 1
        finally:
            os.close(pfd)
    finally:
        if fd is not None:
            os.close(fd)


def head():
    """The commit this checkout is on and whether tracked files differ from it, or None outside a git checkout. As
    root (sudo -E) in a checkout someone else owns, git refuses the repository unless told it's safe; this only reads,
    and with what the checkout's own config could make git run switched off (core.fsmonitor is a command `git status`
    runs, and submodules would run git in other repositories)."""
    git = ["git", "-c", "safe.directory=*", "-c", "core.fsmonitor=false", "-C", ROOT]
    try:
        rev = subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True, text=True, timeout=60)
        st = subprocess.run(git + ["status", "--porcelain", "--untracked-files=no", "--ignore-submodules"], capture_output=True, text=True,
                            timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if rev.returncode or st.returncode:
        return None
    return {"commit": rev.stdout.strip(), "dirty": bool(st.stdout.strip())}


def build_source(path=None):
    """This file's part of the box cache key: the source of build() and of every top-level function, class and
    assignment in this file that it reaches by name, through any of them. A change to what only a replay runs (the
    hops, shelters, relocate, prune) keeps the installed boxes; a change to anything the install runs (the kickstart,
    the preseed, Guest, the box's checks) installs them again."""
    src = open(path or os.path.abspath(__file__)).read()
    defs = {}
    for node in ast.parse(src).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            defs.setdefault(node.name, []).append(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        defs.setdefault(n.id, []).append(node)
    seen, todo = set(), ["build"]
    while todo:
        name = todo.pop()
        if name in seen or name not in defs:
            continue
        seen.add(name)
        todo += [n.id for node in defs[name] for n in ast.walk(node) if isinstance(n, ast.Name)]
    nodes = {id(node): node for name in sorted(seen) for node in defs[name]}
    return "\n".join(ast.get_source_segment(src, n) for n in sorted(nodes.values(), key=lambda n: n.lineno)).encode()


def load(path):
    return tomllib.load(open(os.path.join(ROOT, path), "rb"))


def guest_adapter(family):
    """The adapter with its mirror shim, if it has one, moved to where the guest sees the host."""
    a = load(f"adapters/{family}.toml")
    if "mirror" in a:
        port = a["mirror"]["shim"].rsplit(":", 1)[1]
        a["mirror"] = {**a["mirror"], "shim": f"http://{GUEST_HOST}:{port}"}
    return a


def nvram(disk):
    return disk[:-len(".qcow2")] + ".vars.fd"


def extras(disk):
    """The extra disks that go with disk (LAYOUT["extra_disks"]), named after it."""
    return [disk[:-len(".qcow2")] + f".x{i}.qcow2" for i in range(1, len(LAYOUT["extra_disks"]) + 1)]


def layout_tag():
    """LAYOUT as a short name for directories (empty for the box's own layout), e.g. xfs-v4-lvm-x1."""
    return "-".join(([LAYOUT["root_fs"]] if LAYOUT["root_fs"] else []) + (["lvm"] if LAYOUT["lvm"] else []) +
                    ([f"x{len(LAYOUT['extra_disks'])}"] if LAYOUT["extra_disks"] else []) +
                    (["nohwaddr"] if LAYOUT["no_hwaddr"] else []))


def overlay(prev, disk):
    """A new disk on top of prev (and of each of its extra disks), and on UEFI a copy of prev's NVRAM: each hop boots
    from what the last one left."""
    sandbox.host(f"qemu-img create -q -f qcow2 -b {prev} -F qcow2 {disk}")
    for p, x in zip(extras(prev), extras(disk)):
        sandbox.host(f"qemu-img create -q -f qcow2 -b {p} -F qcow2 {x}")
    if FIRMWARE == "uefi":
        shutil.copyfile(nvram(prev), nvram(disk))


def hardware(adapter, release):
    inst = adapter["releases"][release]["install"]
    return inst.get("disk", "virtio"), inst.get("nic", "virtio")


FORCED_RESET = ("sleep 2; kill -TERM -1; sleep 10; kill -KILL -1; sleep 3; sync; echo 1 > /proc/sys/kernel/sysrq; "
                "echo s > /proc/sysrq-trigger; sleep 2; echo u > /proc/sysrq-trigger; sleep 3; echo b > /proc/sysrq-trigger\n")
# apt boxes: each installed package's version, unless `apt-cache madison` lists it from the box's sources (the release
# pocket, set by set_release, and the pockets the box asks for in [vm].pockets). A box with versions from elsewhere
# isn't the box the route starts from.
OFF_POCKET = (r"""l=$(dpkg-query -W -f '${Package} ${Version}\n') && [ -n "$l" ] || { echo "dpkg-query listed nothing"; """
              r"""exit 1; }; printf '%s\n' "$l" | while read p v; do apt-cache madison "$p" 2>/dev/null | """
              r"""awk -F'|' -v v="$v" '{gsub(/ /, "", $2); if ($2 == v) f = 1} END {exit !f}' || echo "$p $v"; done""")
GUESTS = []  # every qemu this run started: killed on the way out, whatever happened


class Guest:
    def __init__(self, disk, key, ssh_port, hw=("virtio", "virtio")):
        self.disk, self.key, self.ssh_port, self.hw = disk, key, ssh_port, hw
        self.fwd = None  # the host port forwarded to the guest's ssh, picked when it starts
        base = disk[:-len(".qcow2")]
        self.console, self.monitor_path, self.proc = base + ".console.log", None, None
        self.reboot_notes = []  # what went wrong reading boots in the last reboot(), for the run record
        self.attach_extras = True  # LAYOUT's extra disks (install() leaves them off for an installer that can't skip them)

    def argv(self, extra=()):
        fw = [] if FIRMWARE == "bios" else [
            "-drive", f"if=pflash,format=raw,readonly=on,file={OVMF_CODE}",
            "-drive", f"if=pflash,format=raw,file={nvram(self.disk)}"]
        argv = ["qemu-system-x86_64", "-machine", "pc,accel=kvm", "-cpu", "host", "-smp", "2", "-m", "3072", *fw,
                *[x.format(disk=self.disk) for x in DISKS[self.hw[0]]],
                *[x.format(disk=e, i=i) for i, e in enumerate(extras(self.disk) if self.attach_extras else [], 1)
                  for x in DISKS_EXTRA[self.hw[0]]],
                "-netdev",
                f"user,id=n0,hostfwd=tcp:127.0.0.1:{self.fwd}-{ADDR}:{self.ssh_port}",
                "-device", f"{NICS[self.hw[1]]},netdev=n0,mac={MAC}", "-display", "none",
                "-serial", f"file:{self.console}", "-monitor", f"unix:{self.monitor_path},server,nowait", *extra]
        return argv

    def start(self, extra=()):
        for attempt in range(5):
            # QEMU's monitor socket: a unix socket path has to fit in 108 bytes, which a run directory under a
            # checkout doesn't, so it lives in a short directory of its own (removed on the way out, with the guest)
            self.monitor_path = os.path.join(tempfile.mkdtemp(prefix="wend-vm-"), "monitor")
            self.fwd = free_port()
            err = self.console + ".qemu"
            at = os.path.getsize(err) if os.path.exists(err) else 0
            self.proc = subprocess.Popen(self.argv(extra), stdout=subprocess.DEVNULL, stderr=open(err, "a"))
            GUESTS.append(self)
            time.sleep(2)
            if self.proc.poll() is None:
                return
            said = open(err, errors="replace").read()[at:]
            shutil.rmtree(os.path.dirname(self.monitor_path), ignore_errors=True)
            if "host forwarding" not in said:  # anything but another process taking the port first
                break
        raise RuntimeError(f"qemu exited at once: {said[-800:]}")

    def ssh(self, cmd, timeout=600, stdin=None):
        try:
            p = subprocess.run(self.ssh_argv(cmd), input=stdin, capture_output=True, text=True, timeout=timeout,
                               errors="replace")
            return p.returncode, p.stdout + p.stderr
        except subprocess.TimeoutExpired:
            return 124, "TIMEOUT"

    def push_tree(self, root, path, timeout=1800):
        """Copy the tree at `path` under the host directory `root` into the guest at the same path, numeric owners and
        modes kept, replacing what was there."""
        with tempfile.TemporaryFile() as err:  # a file, not a pipe: tar's warnings can't fill a buffer and stall it
            tar = subprocess.Popen(["tar", "-C", root, "--numeric-owner", "-cf", "-", path.lstrip("/")],
                                   stdout=subprocess.PIPE, stderr=err)
            try:
                p = subprocess.run(self.ssh_argv(f"rm -rf {path} && tar -C / --numeric-owner -xpf -"),
                                   stdin=tar.stdout, capture_output=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                tar.kill()
                return 124, "TIMEOUT"
            finally:
                tar.stdout.close()
                tar.wait()
            err.seek(0)
            return p.returncode or tar.returncode, (p.stdout + p.stderr + err.read()).decode(errors="replace")

    def ssh_argv(self, cmd):
        return ["ssh", "-q", "-i", self.key, "-p", str(self.fwd), "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=15", "-o", "LogLevel=ERROR",
                # CentOS 5's OpenSSH 4.3 offers only these; later releases negotiate the modern ones
                "-o", "KexAlgorithms=+diffie-hellman-group1-sha1,diffie-hellman-group14-sha1",
                "-o", "HostKeyAlgorithms=+ssh-rsa", "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa",
                "root@127.0.0.1", ENV + cmd]

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def wait_ssh(self, timeout=900):
        t = time.time()
        while time.time() - t < timeout and self.running():
            if self.ssh("true", timeout=30)[0] == 0:
                return True
            time.sleep(5)
        return False

    def boot_id(self):
        rc, out = self.ssh("cat /proc/sys/kernel/random/boot_id", timeout=30)
        return out.strip() if rc == 0 and out.strip() else None

    def which_boot(self, before, since):
        """Which boot answers: "new" (after `since`, host time), "old" (before it), or None (no answer). New is a
        boot_id that isn't `before` or, when the boot we started from couldn't be read, an uptime shorter than the
        time since."""
        now = self.boot_id()
        if now is None:
            return None
        if before is not None:
            return "new" if now != before else "old"
        rc, out = self.ssh("cat /proc/uptime", timeout=30)
        try:
            up = float(out.split()[0]) if rc == 0 else None
        except (ValueError, IndexError):
            up = None
        return None if up is None else "new" if up < time.time() - since else "old"

    def reboot(self):
        """A real reboot, proven by a new boot_id. Returns (came back, forced). If the release's own `reboot` does
        nothing (the first boot onto systemd: PID 1 is still the old init, the new tools can't talk to it), sync,
        remount read-only and reset with sysrq (SPEC "VM tier"). If the boot it starts from can't be read even after
        retries, the reboot goes ahead and is proven by the guest's uptime instead; why the read failed stays in
        reboot_notes."""
        self.reboot_notes, before = [], None
        for i in range(5):  # one ssh that fails right after a hop's last step mustn't cost the reboot
            rc, out = self.ssh("cat /proc/sys/kernel/random/boot_id", timeout=30)
            if rc == 0 and out.strip():
                before = out.strip()
                break
            self.reboot_notes.append(f"reading boot_id before the reboot, try {i + 1}: rc={rc} {out.strip()[-300:]}")
            if i < 4:
                time.sleep(5 * (i + 1))
        if before is None:
            self.reboot_notes.append("rebooting anyway: the new boot is proven by its uptime")
        since = time.time()
        self.ssh("sync; (sleep 2; reboot) >/dev/null 2>&1 &", timeout=30)
        forced, t, last, silent = False, time.time(), "old", []
        while time.time() - t < 120:
            time.sleep(10)
            last = self.which_boot(before, since)
            if last == "new":
                break
            if not self.running():
                return False, False
            if last is None:
                silent.append(f"no answer {round(time.time() - t)}s after the reboot was sent")
        if last is None:  # noted only now: a poll or two with no answer is every real reboot
            self.reboot_notes += silent
        if last is None and self.wait_ssh(timeout=300):  # silent at the end (EL8's half-stopped old boot): ask again
            last = self.which_boot(before, since)
            self.reboot_notes.append(f"answered again after the window: {last or 'no answer'} boot")
        if last == "old":  # the old boot still answers after two minutes: `reboot` did nothing
            forced = True
            # Stop every process first, while the old kernel still runs: a reset with the daemons up leaves every
            # replaced library and socket as an orphan inode, and EL7's kernel, cleaning those up at its first mount
            # of the ext3 root, leaves extended-attribute refcounts too high (EL9's fsck then stops the boot).
            # Only PID 1's own files stay orphaned. A detached script: the kill takes this ssh session too.
            self.ssh("cat > /root/.wend-reset", stdin=FORCED_RESET)
            self.ssh("setsid nohup sh /root/.wend-reset >/dev/null 2>&1 </dev/null &", timeout=30)
            time.sleep(40)
        return self.wait_ssh() and self.which_boot(before, since) == "new", forced

    def reset(self):
        """A hard reset from outside, as a power cut would be (QEMU's monitor): no sync, no shutdown. Returns whether
        the guest came back on a new boot."""
        before = self.boot_id()
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(self.monitor_path)
            s.sendall(b"system_reset\n")
            time.sleep(1)
        time.sleep(20)
        return before is not None and self.wait_ssh() and self.boot_id() not in (None, before)

    def stop(self):
        if not self.running():
            return
        # a guest that doesn't answer (hung at boot) won't power off either; one that answered gets its 300 s
        try:
            self.proc.wait(300 if self.ssh("sync; (sleep 1; poweroff) >/dev/null 2>&1 &", timeout=30)[0] == 0 else 60)
        except subprocess.TimeoutExpired:
            self.kill()

    def kill(self):
        if self.running():
            self.proc.kill()
            self.proc.wait(30)


def kickstart(box_doc, adapter, key_pub):
    """anaconda's answers. inst["packages"] is what the install needs besides the box's own (the release's bootloader
    package), inst["append"] kernel arguments for the installer and the installed system (EL7's net.ifnames=0, so the
    NIC is eth0 as the box's network role expects), inst["ifname"] that the installer's boot line names the NIC by MAC
    (ifname=eth0:<mac>: EL7's installer then pins the ifcfg to it with HWADDR), inst["boot_part_mib"] the size of
    /boot as the release's installer makes it by default (7.9's: 1 GiB); unset, 200 MiB, a size chosen for CentOS 5,
    whose 5.11 installer makes 100 MiB, inst["ks_end"] whether sections end with %end (EL7 on; EL5's
    anaconda doesn't know it), inst["nobase"] (default on) whether %packages takes --nobase, which EL8's installer
    no longer accepts, inst["repos"] the trees besides `tree` the install reads (EL8's AppStream, where httpd, php
    and mariadb live), each a kickstart `repo` through the shim, inst["packages_flags"] more %packages options (EL4's
    anaconda stops to ask about dependencies unless it gets --resolvedeps), inst["ks"] more kickstart commands (EL4's
    anaconda stops at its language support screen unless it gets langsupport). On UEFI, an EFI system partition. The box's customize runs in %post after the release's
    repos are set (set_release), as the chroot build runs it, with the mirror shim where the guest sees the host."""
    rel = adapter["releases"][box_doc["release"]]
    inst = rel["install"]
    end = ["%end"] if inst.get("ks_end") else []
    # unset, the first filesystem the installer makes (CentOS 6's ext4), else ext3
    fs = ROOT_FS[LAYOUT["root_fs"] or box_doc.get("vm", {}).get("root_fs") or (inst.get("root_fs") or ["ext3"])[0]]
    efi = ["part /boot/efi --fstype efi --size=200"] if FIRMWARE == "uefi" else []
    boot = f"part /boot {fs} --size={inst.get('boot_part_mib', 200)}"
    if LAYOUT["lvm"]:  # anaconda's autopart: /boot, then one PV with swap and a root that fills it, named as it names them
        lv = inst.get("lvm", {"vg": "centos", "root": "root", "swap": "swap"})
        parts = [boot, *efi, "part pv.01 --size=1 --grow", f"volgroup {lv['vg']} pv.01",
                 f"logvol swap --vgname={lv['vg']} --name={lv['swap']} --size=1024",
                 f"logvol / {fs} --vgname={lv['vg']} --name={lv['root']} --size=1 --grow"]
    else:
        parts = [boot, *efi, "part swap --size=1024", f"part / {fs} --size=1 --grow"]
    # with extra disks, anaconda touches only the first; the others stay empty (no label, no signature). An installer
    # that can't be told so (before EL7's) never sees them: install() adds them after it.
    only = FIRST_DISK[hardware(adapter, box_doc["release"])[0]]
    disks = ([f"ignoredisk --only-use={only}"], f" --drives={only}", f" --boot-drive={only}") \
        if LAYOUT["extra_disks"] and inst.get("ks_end") else ([], "", "")
    v = backends.of(adapter).hop_vars(adapter, box_doc["release"])
    shim = f"http://{GUEST_HOST}:{adapter['mirror']['shim'].rsplit(':', 1)[1]}"
    return "\n".join([
        "install", f"url --url {shim}/{inst['tree']}",
        *(f"repo --name={name} --baseurl={shim}/{path}" for name, path in inst.get("repos", {}).items()),
        "text", "skipx", "lang en_US.UTF-8", "keyboard us", *inst.get("ks", []),
        # a static address, as servers have: the live checks compare the guest's addresses with it after every boot
        f"network --device eth0 --bootproto static --ip {ADDR} --netmask 255.255.255.0 --gateway 10.0.2.2 "
        "--nameserver 10.0.2.3 --hostname wend-vm.example.org",
        "rootpw --iscrypted !", "firewall --disabled", "selinux --permissive", "authconfig --enableshadow --enablemd5",
        "timezone --utc UTC",
        f'bootloader --location=mbr{disks[2]} --append="console=tty0 console=ttyS0,115200 {inst.get("append", "")}"'
        .replace(" \"", "\""),
        *disks[0], "zerombr", f"clearpart --all --initlabel{disks[1]}", *parts, "poweroff", "",
        " ".join(["%packages", *(["--nobase"] if inst.get("nobase", True) else []), *inst.get("packages_flags", [])]), "kernel", *inst.get("packages", []), *box_doc["packages"], *end, "",
        "%post --log=/root/ks-post.log",
        post_install(box_doc, key_pub, before=backends.fill(adapter["hop"]["set_release"], v),
                     shim=(load(f"adapters/{box_doc['family']}.toml")["mirror"]["shim"], adapter["mirror"]["shim"])),
        *end, ""])


# [vm].root_fs: the filesystem anaconda makes for / and /boot. xfs-v4 is EL 7.0 to 7.2's XFS (no metadata CRCs, no
# d_type), which later releases still mount but can't make by default; xfs is v5, EL 7.3's default and on.
ROOT_FS = {"ext3": "--fstype ext3", "ext4": "--fstype ext4", "xfs": "--fstype xfs",
           "xfs-v4": '--fstype xfs --mkfsoptions="-m crc=0 -n ftype=0"'}
ROOT_FS_IS = {"ext3": "for m in / /boot; do [ \"$(findmnt -no FSTYPE $m)\" = ext3 ] || { findmnt $m; exit 1; }; done",
              # CentOS 6's util-linux may have no findmnt
              "ext4": "for m in / /boot; do awk -v m=$m '$2 == m && $1 != \"rootfs\" {t = $3} END {exit t != \"ext4\"}' "
                      "/proc/mounts || { grep \" $m \" /proc/mounts; exit 1; }; done",
              **{k: "for m in / /boot; do X=$(xfs_info $m) || exit 1; echo \"$X\" | grep -q 'crc=%s' && "
                    "echo \"$X\" | grep -q 'ftype=%s' || { echo \"$X\"; exit 1; }; done" % c
                 for k, c in (("xfs", ("1", "1")), ("xfs-v4", ("0", "0")))}}


def post_install(box_doc, key_pub, before="", shim=None):
    """What every installer runs in the new system at the end: the harness's ssh key, then the box's own setup (after
    `before`). shim = (the mirror shim as the host sees it, as the guest does): a box's customize names the first."""
    custom = box_doc["customize"].replace(*shim) if shim else box_doc["customize"]
    # a broken `before` (the release's set_release) must stop the build where it broke, on the console too, not let
    # the box's setup carry on without its repos and fail somewhere unrelated
    stop = "{ echo 'wend: the release setup (set_release) failed, the box is not built' | tee /dev/console; exit 1; }"
    return "\n".join(["set -x", "mkdir -p /root/.ssh && chmod 700 /root/.ssh",
                      f"echo '{key_pub}' > /root/.ssh/authorized_keys", "chmod 600 /root/.ssh/authorized_keys",
                      f"{{ {before}\n}} || {stop}" if before else "", custom, box_doc.get("start_hook", ""), ""])


def preseed(box_doc, adapter, inst, proxy=None):
    """debian-installer's answers, read from the initrd (no network is needed to fetch them): a static address, the
    start release's mirror (or the release-pocket proxy in front of it), one root partition, the box's packages and
    debconf answers, and post_install() at the end.
    inst["preseed"] holds the lines that differ by installer version (keymap, mirror country, disk path, partman names),
    inst["packages"] what the install needs besides the box's own, inst["late"] what the release's bootloader needs for
    a serial console. The mirror is the start release's own (the archive, or the current mirror)."""
    m = urllib.parse.urlsplit(backends.of(adapter).hop_vars(adapter, box_doc["release"])["mirror"])
    host = f"{GUEST_HOST}:{proxy.port}" if proxy else m.netloc  # the proxy serves the mirror host's paths
    late = "cp /wend-post.sh /target/root/wend-post.sh; in-target sh -c 'sh /root/wend-post.sh > /root/wend-post.log 2>&1'"
    return "\n".join([
        "d-i debian-installer/locale string en_US",
        # a static address, as servers have: the live checks compare the guest's addresses with it after every boot
        "d-i netcfg/choose_interface select eth0", "d-i netcfg/disable_dhcp boolean true",
        f"d-i netcfg/get_ipaddress string {ADDR}", "d-i netcfg/get_netmask string 255.255.255.0",
        f"d-i netcfg/get_gateway string {GUEST_HOST}", "d-i netcfg/get_nameservers string 10.0.2.3",
        "d-i netcfg/confirm_static boolean true", "d-i netcfg/get_hostname string wend-vm",
        "d-i netcfg/get_domain string example.org",
        f"d-i mirror/http/hostname string {host}", f"d-i mirror/http/directory string {m.path}",
        "d-i mirror/http/proxy string",
        f"d-i mirror/suite string {box_doc['release']}",
        # the archive's keys expired long ago; the harness's apt config allows the same (hop.apt_conf)
        "d-i debian-installer/allow_unauthenticated boolean true",
        "d-i clock-setup/utc boolean true", "d-i time/zone string UTC", "d-i partman-auto/method string regular",
        "d-i partman/confirm_write_new_label boolean true", "d-i partman/confirm boolean true",
        "d-i passwd/root-login boolean true", "d-i passwd/make-user boolean false",
        "d-i passwd/root-password-crypted password $1$wendvm$NoPasswordOpensThisAcc", "d-i apt-setup/security_host string",
        "tasksel tasksel/first multiselect",
        f"d-i pkgsel/include string {' '.join(box_doc['packages'] + inst.get('packages', []))}",
        "popularity-contest popularity-contest/participate boolean false",
        "d-i grub-installer/only_debian boolean true", "d-i finish-install/reboot_in_progress note",
        "d-i debian-installer/exit/poweroff boolean true", *inst.get("preseed", []),
        *box_doc.get("debconf_selections", []),
        f"d-i preseed/late_command string {late}" + (f"; {inst['late']}" if inst.get("late") else ""), ""])


def newc(files):
    """A cpio archive (newc) of name -> bytes, all mode 0644 in the root: what an initramfs appended to another one
    adds. The kernel unpacks every archive in the initrd in turn, so this adds files without unpacking the original."""
    out = b""
    for i, (name, data) in enumerate(list(files.items()) + [("TRAILER!!!", b"")]):
        n = name.encode() + b"\0"
        mode = 0o100644 if name != "TRAILER!!!" else 0
        hdr = "070701" + "".join(f"{x:08X}" for x in (i + 1, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(n), 0))
        out += hdr.encode() + n + b"\0" * (-(110 + len(n)) % 4) + data + b"\0" * (-len(data) % 4)
    return out


class Serve:
    """A one-directory http server on the host for the kickstart, reachable from the installer at 10.0.2.2."""

    def __init__(self, directory):
        h = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(*a, directory=directory, **kw)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), h)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


class PocketProxy:
    """An http proxy on the host for debian-installer that offers a mirror's release pocket and nothing else: the
    -updates, -security, -backports and -proposed suites answer 404. Ubuntu's installer always adds -updates (its
    50mirror.ubuntu writes it whatever the preseed says) and installs from it, so without this a trusty box comes up
    with the release's updates, while the route, and the chroot box, read the release pocket only. A box that is meant
    to start from them (wend#47) names the pockets it wants in [vm].pockets, and those suites pass: backports too, which
    trusty's installer also writes into sources.list, so a box that runs apt-get update in customize needs it."""

    OTHER_POCKET = re.compile(r"/dists/[^/]+-(updates|security|backports|proposed)/")

    def __init__(self, upstream, allow=()):
        up = upstream.rstrip("/")

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"  # keep-alive: apt pipelines requests

            def do_GET(self):
                # only a path on the mirror: "host:port/x" or ".evil/x" would otherwise reach another server
                other = PocketProxy.OTHER_POCKET.search(self.path)
                if not self.path.startswith("/") or (other and other.group(1) not in allow) or ".." in self.path:
                    return self.send_error(404)
                try:
                    with urllib.request.urlopen(up + self.path, timeout=300) as rsp:
                        if rsp.geturl() != up + self.path:  # a redirect could lead to another pocket
                            return self.send_error(502)
                        body = rsp.read()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except urllib.error.HTTPError as e:
                    self.send_error(e.code)
                except (OSError, http.client.HTTPException):
                    self.send_error(502)

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


def fetch(url, out):
    rc, msg = sandbox.host(f"curl -sf -o {out} {url}")
    if rc:
        raise RuntimeError(f"fetching {url} failed: {msg[-300:]}")


def build(box_id):
    """Install BOX in a VM from its release's own installer (anaconda for EL, debian-installer for Debian and Ubuntu), then bring
    it to the release's last packages, as the chroot build does. Cached by box file and adapter, like
    sandbox.build_box."""
    box_doc = load(f"boxes/{box_id}/box.toml")
    fam = box_doc["family"]
    raw = open(os.path.join(ROOT, f"boxes/{box_id}/box.toml"), "rb").read() + \
        open(os.path.join(ROOT, f"adapters/{fam}.toml"), "rb").read() + build_source() + \
        FIRMWARE.encode() + (json.dumps(LAYOUT, sort_keys=True).encode() if layout_tag() else b"")
    tag = "-".join(filter(None, [FIRMWARE, layout_tag()]))  # the default layout keeps the cache key it had
    d = os.path.join(WORK, f"{box_id.replace('/', '_')}-{tag}-{hashlib.sha256(raw).hexdigest()[:12]}")
    if not os.path.exists(os.path.join(d, "base.qcow2")):
        with Locked(d + ".lock", f"installing {box_id} for this key"):
            if not os.path.exists(os.path.join(d, "base.qcow2")):  # it may have, while this one waited
                install(box_id, box_doc, d)
    return d, box_doc


def install(box_id, box_doc, d):
    """build()'s install, into the cache directory d, under its lock."""
    fam = box_doc["family"]
    base = os.path.join(d, "base.qcow2")
    inst = load(f"adapters/{fam}.toml")["releases"][box_doc["release"]]["install"]
    root_fs = LAYOUT["root_fs"] or box_doc.get("vm", {}).get("root_fs")
    if root_fs and root_fs not in inst.get("root_fs", []):
        raise SystemExit(f"{box_doc['release']}'s installer doesn't make {root_fs} (releases.<release>.install.root_fs)")
    if LAYOUT["lvm"] and not (load(f"adapters/{fam}.toml")["backend"] == "rpm" and (inst.get("ks_end") or inst.get("lvm"))):
        raise SystemExit("--lvm is a kickstart choice (logvol) for EL7 and later, or a release whose install names its "
                         "LVs (releases.<release>.install.lvm)")
    if LAYOUT["extra_disks"] and load(f"adapters/{fam}.toml")["backend"] != "rpm":
        raise SystemExit("--extra-disk is a kickstart choice (EL)")
    # EL7's installer is told to leave the extra disks alone (ignoredisk --only-use); an older one doesn't see them
    later = LAYOUT["extra_disks"] and not inst.get("ks_end")
    if LAYOUT["no_hwaddr"] and not inst.get("ifname"):
        raise SystemExit(f"--no-hwaddr is for a release whose installer is told the NIC by MAC (releases."
                         f"{box_doc['release']}.install.ifname)")
    if FIRMWARE == "uefi" and not inst.get("uefi"):
        raise SystemExit(f"{box_doc['release']}'s installer doesn't install on UEFI (adapter: releases."
                         f"{box_doc['release']}.install.uefi)")
    os.makedirs(d, exist_ok=True)
    a = guest_adapter(fam)
    sandbox.ensure_mirror(load(f"adapters/{fam}.toml"))
    key = os.path.join(d, "key")
    if not os.path.exists(key):
        sandbox.host(f"ssh-keygen -q -t rsa -b 3072 -N '' -C wend-vm -f {key}")
    inst = a["releases"][box_doc["release"]]["install"]
    disk = os.path.join(d, "install.qcow2")
    sandbox.host(f"qemu-img create -q -f qcow2 {disk} 30G")
    for x, gb in zip(extras(disk), [] if later else LAYOUT["extra_disks"]):
        sandbox.host(f"qemu-img create -q -f qcow2 {x} {gb}G")
    if FIRMWARE == "uefi":
        shutil.copyfile(OVMF_VARS, nvram(disk))
    hw = hardware(a, box_doc["release"])
    g = Guest(disk, key, box_doc.get("vm", {}).get("ssh_port", 22), hw)
    g.attach_extras = not later
    log(f"installing {box_id} ({box_doc['release']}) in a VM; this takes 10 to 20 minutes")
    key_pub = open(key + ".pub").read().strip()
    if isinstance(backends.of(a), backends.Rpm):  # anaconda: the kickstart over http from the host, the tree via the shim
        shim = load(f"adapters/{fam}.toml")["mirror"]["shim"]
        for f in ("vmlinuz", "initrd.img"):
            fetch(f"{shim}/{inst['tree']}/{inst['pxeboot']}/{f}", os.path.join(d, f))
        ks = os.path.join(d, "ks")
        os.makedirs(ks, exist_ok=True)
        open(os.path.join(ks, "ks.cfg"), "w").write(kickstart(box_doc, a, key_pub))
        srv = Serve(ks)
        try:
            g.start(["-kernel", os.path.join(d, "vmlinuz"), "-initrd", os.path.join(d, "initrd.img"), "-no-reboot",
                     "-append", f"ks=http://{GUEST_HOST}:{srv.port}/ks.cfg ksdevice=eth0 console=ttyS0,115200 text "
                     f"{inst.get('append', '')}{f' ifname=eth0:{MAC}' if inst.get('ifname') and not LAYOUT['no_hwaddr'] else ''}"
                     .strip()])
            g.proc.wait(3600)
        finally:
            srv.httpd.shutdown()
            srv.httpd.server_close()
    else:  # debian-installer: netboot from the start release's mirror, the preseed and post_install() in its initrd
        host_mirror = backends.Apt().hop_vars(load(f"adapters/{fam}.toml"), box_doc["release"])["mirror"]
        for f in ("linux", "initrd.gz"):  # fetched on the host: a mirror through the shim is the host's 127.0.0.1
            fetch(f"{host_mirror}/{inst['netboot']}/{f}", os.path.join(d, f))
        u = urllib.parse.urlsplit(host_mirror)  # the proxy runs on the host, so it reaches the shim at 127.0.0.1
        proxy = (PocketProxy(f"{u.scheme}://{u.netloc}", box_doc.get("vm", {}).get("pockets", []))
                 if inst.get("release_pocket_only") else None)
        extra = gzip.compress(newc({"preseed.cfg": preseed(box_doc, a, inst, proxy).encode(),
                                    "wend-post.sh": post_install(box_doc, key_pub).encode()}))
        open(os.path.join(d, "initrd-wend.gz"), "wb").write(open(os.path.join(d, "initrd.gz"), "rb").read() + extra)
        try:
            g.start(["-kernel", os.path.join(d, "linux"), "-initrd", os.path.join(d, "initrd-wend.gz"), "-no-reboot",
                     "-append", "priority=critical console=ttyS0,115200n8"])
            g.proc.wait(3600)
        finally:
            if proxy:
                proxy.httpd.shutdown()
                proxy.httpd.server_close()
    for x, gb in zip(extras(disk), LAYOUT["extra_disks"] if later else []):  # empty, as the installer would have left them
        sandbox.host(f"qemu-img create -q -f qcow2 {x} {gb}G")
    g = Guest(disk, key, g.ssh_port, hw)
    g.start()
    if not g.wait_ssh():
        raise RuntimeError(f"the installed box never answered ssh; console: {g.console}")
    v = backends.of(a).hop_vars(a, box_doc["release"])
    if isinstance(backends.of(a), backends.Rpm):
        last = backends.fill(a["hop"]["set_release"], v) + " && yum -y update && yum clean all"
    else:  # the harness's apt config first, as the chroot build writes it (tools/sandbox.py)
        rc, out = g.ssh("mkdir -p /etc/apt/apt.conf.d && cat > /etc/apt/apt.conf.d/00wend", stdin=a["hop"]["apt_conf"])
        if rc:
            raise RuntimeError(f"writing the harness's apt config in the box failed: {out}")
        last = " && ".join([backends.fill(a["hop"]["set_release"], v),
                            *pocket_sources(box_doc["release"], box_doc.get("vm", {}).get("pockets", [])),
                            backends.fill(a["hop"]["refresh"], v), "apt-get dist-upgrade", "apt-get clean"])
    rc, out = g.ssh(last, timeout=3600)
    if rc:
        raise RuntimeError(f"updating the installed box failed:\n{out[-2000:]}")
    if not isinstance(backends.of(a), backends.Rpm):  # every installed version is one the box's sources offer
        rc, out = g.ssh(OFF_POCKET, timeout=1800)
        if rc or out.strip():
            raise RuntimeError(f"the installed box has packages its release pocket doesn't offer:\n{out[-2000:]}")
    if not g.reboot()[0]:
        raise RuntimeError(f"the installed box didn't come back from its first reboot; console: {g.console}")
    fw = g.ssh(FW_NOW)[1].strip()
    if fw != FIRMWARE:
        raise RuntimeError(f"the installed box booted on {fw!r}, not {FIRMWARE}")
    if LAYOUT["no_hwaddr"]:  # the installer wrote no MAC pin, or this isn't the install it says
        rc, out = g.ssh("test -f /etc/sysconfig/network-scripts/ifcfg-eth0 && "
                        "! grep -E '^[[:space:]]*HWADDR=' /etc/sysconfig/network-scripts/ifcfg-eth0")
        if rc:
            raise RuntimeError(f"--no-hwaddr: the installed ifcfg-eth0 is missing or has a HWADDR: {out[-600:]}")
    if root_fs:  # the box has the filesystem it says, or it isn't that box
        rc, out = g.ssh(ROOT_FS_IS[root_fs])
        if rc:
            raise RuntimeError(f"the installed box's / and /boot aren't {root_fs}: {out[-600:]}")
    if LAYOUT["lvm"]:  # / is the LV the kickstart named (mtab: EL5's /proc/mounts says /dev/root)
        lv = inst.get("lvm", {"vg": "centos", "root": "root"})
        rc, out = g.ssh(f"awk '$2 == \"/\" {{r = $1}} END {{print r; exit !(r == \"/dev/mapper/{lv['vg']}-{lv['root']}\" "
                        f"|| r == \"/dev/{lv['vg']}/{lv['root']}\")}}' /etc/mtab")
        if rc:
            raise RuntimeError(f"--lvm: the installed box's / isn't {lv['vg']}/{lv['root']}: {out[-600:]}")
    log("installed and updated; checking the box")
    failed = {n: o for n, (ok, o) in checks(g, box_doc, a).items() if not ok}
    g.stop()
    if failed:
        raise RuntimeError(f"box {box_id} fails its own checks in the VM: {sorted(failed)}\n" +
                           "\n".join(f"{n}: {o[-600:]}" for n, o in failed.items()))
    # base.qcow2 last: a run that sees it (build(), without the lock) finds its extra disks and NVRAM beside it
    for x, b in zip(extras(disk), extras(base)):
        os.rename(x, b)
        sandbox.host(f"chmod a-w {b}")
    if FIRMWARE == "uefi":
        os.rename(nvram(disk), nvram(base))
    os.rename(disk, base)
    sandbox.host(f"chmod a-w {base}")


def selinux_start(d, box_doc, a):
    """The disk a run starts from: the box's base.qcow2, or with --selinux (SELINUX) a disk beside it made from a copy
    of the box once per mode and per this function's commands (SELINUX_MAKE), rebooted into that mode and holding
    every check the box passed as installed. Built under the box's lock, like build()."""
    base = os.path.join(d, "base.qcow2")
    if not SELINUX:
        return base
    if a["backend"] != "rpm":
        raise SystemExit("--selinux is for EL boxes")
    if SELINUX == "none" and a["releases"][box_doc["release"]]["major"] >= 7:
        # EL7's systemd freezes at boot with SELINUX=enforcing and no policy; there is no such box to start from
        raise SystemExit("--selinux none is for a box on CentOS 4 to 6 (wend#158)")
    h = hashlib.sha256((SELINUX_MAKE[SELINUX] + SELINUX_NOW).encode()).hexdigest()[:12]
    disk = os.path.join(d, f"selinux-{SELINUX}-{h}.qcow2")
    if os.path.exists(disk):
        return disk
    with Locked(os.path.join(d, "selinux.lock"), f"making the box's --selinux {SELINUX} disk"):
        if os.path.exists(disk):
            return disk
        work = disk[:-len(".qcow2")] + ".work.qcow2"
        for f in [work, *extras(work)]:
            if os.path.exists(f):
                os.remove(f)
        overlay(base, work)
        g = Guest(work, os.path.join(d, "key"), box_doc.get("vm", {}).get("ssh_port", 22),
                  hardware(a, box_doc["release"]))
        g.start()
        try:
            if not g.wait_ssh():
                raise RuntimeError(f"the box didn't come up to be made --selinux {SELINUX}; console: {g.console}")
            log(f"making the box SELinux {SELINUX}")
            rc, out = g.ssh(SELINUX_MAKE[SELINUX], timeout=1800)
            if rc:
                raise RuntimeError(f"--selinux {SELINUX}: setting it up failed:\n{out[-2000:]}")
            if not g.reboot()[0]:  # a relabel first, then the boot in that mode
                raise RuntimeError(f"--selinux {SELINUX}: the box didn't come back; console: {g.console}")
            now = " ".join(g.ssh(SELINUX_NOW)[1].split())
            if now != SELINUX_IS[SELINUX]:
                raise RuntimeError(f"--selinux {SELINUX}: the box came up with SELinux {now!r}, not {SELINUX_IS[SELINUX]!r}")
            failed = {n: o for n, (ok, o) in checks(g, box_doc, a).items() if not ok}
            if failed:  # an owner who set SELinux so had a box that works so; this one doesn't, so it isn't that box
                raise RuntimeError(f"--selinux {SELINUX}: the box fails its own checks: {sorted(failed)}\n" +
                                   "\n".join(f"{n}: {o[-600:]}" for n, o in failed.items()))
        finally:
            g.stop()
        for x, b in zip(extras(work), extras(disk)):
            os.rename(x, b)
            sandbox.host(f"chmod a-w {b}")
        if FIRMWARE == "uefi":
            os.rename(nvram(work), nvram(disk))
        os.rename(work, disk)
        sandbox.host(f"chmod a-w {disk}")
    return disk


def pocket_sources(release, pockets):
    """Shell that adds the [vm].pockets a box asks for to the sources.list set_release just wrote: each `deb` line of
    the release, again for {release}-{pocket}, from the same mirror with the same components. check.py holds release
    and pockets to plain words."""
    return [f"sed -n 's/^deb \\([^ ]*\\) {release} \\(.*\\)$/deb \\1 {release}-{p} \\2/p' /etc/apt/sources.list > /tmp/wend-pocket"
            f" && cat /tmp/wend-pocket >> /etc/apt/sources.list && rm /tmp/wend-pocket" for p in pockets]


def checks(g, box_doc, adapter):
    """The box's offline checks that make sense on a running box (box.toml [vm].skip lists roles that start their
    own daemons), then its live ones ([vm].live), each as (ok, output)."""
    k = box_doc.get("vm", {})
    named = {n: c for n, c in sandbox.checks_for(box_doc, adapter).items() if n not in k.get("skip", [])}
    named.update({f"live:{n}": c for n, c in k.get("live", {}).items()})
    out = {}
    for n, c in named.items():
        rc, o = g.ssh(f"( {c} ) >/tmp/.wend-check 2>&1; rc=$?; tail -c 800 /tmp/.wend-check; exit $rc", timeout=300)
        out[n] = (rc == 0, o)
    return out


RUNNER = r"""cd /root/wend-hop
for f in $(ls main-*.sh | sort); do sh $f > $f.out 2>&1; rc=$?; echo $rc > $f.rc; [ $rc = 0 ] || break; done
for f in $(ls after-*.sh 2>/dev/null | sort); do sh $f > $f.out 2>&1; echo $? > $f.rc; done
ls main-*.rc | while read r; do cat $r; done | grep -qv '^0$' && echo 1 > DONE || echo 0 > DONE
"""


# how long a hop's guest may go without answering ssh before the hop fails: the transaction restarts sshd, briefly
SILENT_HOP = 1800


def run_hop(g, cmds, after):
    """The hop's commands inside the guest, detached: the transaction replaces sshd and glibc under the session.
    Stops at the first failing command, then runs `after` whatever happened (SPEC "How a hop runs")."""
    rc, o = g.ssh("rm -rf /root/wend-hop && mkdir -p /root/wend-hop && test ! -e /root/wend-hop/DONE")
    if rc:  # the previous hop's directory (and its DONE) is on this overlay: never read it as this hop's result
        raise RuntimeError(f"clearing the previous hop's work dir in the guest failed: {o}")
    files = {"runner.sh": RUNNER, **{f"main-{i:02d}.sh": c + "\n" for i, c in enumerate(cmds)},
             **{f"after-{i:02d}.sh": c + "\n" for i, c in enumerate(after)}}
    for name, body in files.items():
        rc, o = g.ssh(f"cat > /root/wend-hop/{name}", stdin=ENV + "\n" + body if name != "runner.sh" else body)
        if rc:
            raise RuntimeError(f"copying {name} into the guest failed: {o}")
    rc, o = g.ssh("cd /root/wend-hop && (setsid nohup sh runner.sh >runner.log 2>&1 </dev/null &)", timeout=30)
    if rc:
        raise RuntimeError(f"starting the hop in the guest failed: {o}")
    t = answered = time.time()
    while time.time() - t < 4 * 3600 and g.running():
        time.sleep(20)
        rc, o = g.ssh("cat /root/wend-hop/DONE 2>/dev/null", timeout=60)
        if rc == 0 and o.strip() in ("0", "1"):
            break
        if rc not in (124, 255):  # ssh's own failures; anything else is the guest answering, DONE or not
            answered = time.time()
        elif time.time() - answered > SILENT_HOP:  # sshd is replaced in seconds, not this long (wend#158)
            return False, (f"the guest stopped answering ssh {round(time.time() - answered)}s ago, mid-hop; "
                           f"console: {g.console}")
    else:
        return False, "the hop didn't finish in 4 hours, or the guest stopped"
    rc, o = g.ssh("cd /root/wend-hop; for f in main-*.sh after-*.sh; do [ -f $f.rc ] || continue; "
                  "echo \"### $f rc=$(cat $f.rc)\"; tail -c 3000 $f.out; done", timeout=120)
    return g.ssh("cat /root/wend-hop/DONE")[1].strip() == "0", o


def shelter_build(box_id, route, drop):
    """SPEC "Sheltering an app" on the VM tier: shelter.build and shelter.prove, the chroot harness's own (tools/walk.py),
    on a chroot copy of the box at the route's start, then the built shelters are copied into the guest before the drop
    hop. The guest was installed from the same box file on the same release, and shelter_copy_check holds the copy to
    the guest's own user and group lines and carried files. Returns (the walk, its stop-native and start steps for the
    drop hop, written for the guest's adapter), or (the walk, None) when the build or proof refused."""
    fam = route["family"]
    repo = type("Repo", (), {})()
    repo.adapters = check.load_adapters()
    repo.boxes = {box_id: load(f"boxes/{box_id}/box.toml")}
    repo.raw = {k: open(os.path.join(ROOT, k), "rb").read() for k in (f"boxes/{box_id}/box.toml", f"adapters/{fam}.toml")}
    old, rw = os.umask(0o022), None  # as `wend replay` builds its walks (SPEC "How a hop runs")
    try:
        rw = walk.Walk(repo, route, box_id, log=log)
        if not rw.shelter_before(drop):
            return rw, None
    except BaseException:
        if rw:  # a chroot copy is a few GB: never leave one behind
            rw.close()
        raise
    finally:
        os.umask(old)
    a, keeps = guest_adapter(fam), shelter.keeps_runtimes_on(rw.adapter, route)
    plan = rw.shelter_plan
    pre = [c for app in sorted(plan) for c in shelter.stop_native(app, plan[app])]
    post = [c for app in sorted(plan) for c in shelter.start(app, plan[app], a, drop, keeps)]
    return rw, (pre, post)


def shelter_copy_check(rw, app, plan):
    """Shell for the guest: its own passwd and group lines for the app's user, and each carried file, are the ones the
    shelter's lock was built from (the chroot copy), by sha256."""
    lock = tomllib.load(open(f"{os.path.realpath(rw.box.root)}{shelter.SHELTER}/{app}/shelter.lock", "rb"))
    sha = lambda t: hashlib.sha256((t + "\n").encode()).hexdigest()  # noqa: E731
    gname = lock["group"].split(":", 1)[0]
    # SPEC "How it runs on the host": idmapped mounts need ext4, XFS or btrfs (the kernel mounts ext3 as ext3 type,
    # which can't be idmapped)
    out = [f"for d in {' '.join([plan['code']] + plan['data'])}; do case \"$(findmnt -no FSTYPE -T $d)\" in "
           "ext4|xfs|btrfs) ;; *) echo \"$d is on $(findmnt -no FSTYPE -T $d), which can't be idmapped\"; exit 1;; "
           "esac; done",
           f"[ \"$(grep '^{plan['user']}:' /etc/passwd | sha256sum | cut -d' ' -f1)\" = {sha(lock['user'])} ] || "
           f"{{ echo 'the guest has another passwd line for {plan['user']} than the shelter was built with'; exit 1; }}",
           f"[ \"$(grep \"^$(id -gn {plan['user']}):\" /etc/group | sha256sum | cut -d' ' -f1)\" = {sha(lock['group'])} ] || "
           f"{{ echo 'the guest has another group line than the shelter was built with ({gname})'; exit 1; }}"]
    for c in lock.get("carried", []):
        q = shlex.quote(c["path"])
        out.append(f"[ \"sha256:$(sha256sum {q} | cut -d' ' -f1)\" = {c['sha256']} ] || "
                   f"{{ echo carried file {q} differs in the guest; exit 1; }}")
    return "\n".join(out) + "\necho the copy matches the guest\n"


def shelter_confine(g, box_doc, a, plan, passing):
    """The VM tier's confinement proof after a reboot (SPEC "Sheltering an app", Proof): shelter.vm_probe as the box
    booted (SELinux permissive, as installed), then an enforcing window: the shelter restarted under enforcing, the
    box's checks again (none that passed may fail), the probe again, and the window's AVC denials (none may touch
    the shelter). Returns {app: record}."""
    outside = f"{GUEST_HOST}:{a['mirror']['shim'].rsplit(':', 1)[1]}"  # the mirror shim: the guest reaches it
    out = {}
    for app, p in sorted(plan.items()):
        rc, o = g.ssh(shelter.vm_probe(app, p, outside), timeout=1200)
        r = {"permissive": {"ok": rc == 0, "log": o[-6000:]},
             "facts": dict(x[5:].split("=", 1) for x in o.splitlines() if x.startswith("fact ") and "=" in x)}
        rc, o = g.ssh(shelter.vm_enforce_on(app), timeout=600)
        r["enforce_on"] = {"ok": rc == 0, "log": o[-2000:]}
        lost = []
        if rc == 0:
            ch = checks(g, box_doc, a)
            # [vm].permissive_only: the box's own stack as its owner set it up on a permissive box (content with no
            # httpd label, a proxy to a port httpd_t may not reach); the window judges the shelter, not those
            po = set(box_doc.get("vm", {}).get("permissive_only", []))
            r["enforcing_excused"] = sorted(n for n in passing if n in po or n.removeprefix("live:") in po)
            lost = sorted(n for n in passing if not ch.get(n, (False,))[0] and n not in r["enforcing_excused"])
            r["enforcing_lost"], r["enforcing_failed_logs"] = lost, {n: ch.get(n, (False, ""))[1][-1500:] for n in lost}
            rc, o = g.ssh(shelter.vm_probe(app, p, outside), timeout=1200)
            # the guard sets permissive after 15 minutes: a probe that ran after it proves nothing about enforcing
            r["enforcing"] = {"ok": rc == 0 and {"fact selinux=Enforcing", "fact selinux_end=Enforcing"} <= set(
                o.splitlines()), "log": o[-6000:]}
        for _ in range(20):  # if enforcing cut ssh off, the guard sets permissive again within 15 minutes
            rc, o = g.ssh(shelter.VM_ENFORCE_OFF, timeout=300)
            if rc not in (124, 255):
                break
            time.sleep(60)
        r["avc"], r["denials"] = o[-4000:], shelter.vm_denials(app, o)
        r["ok"] = (r["permissive"]["ok"] and r["enforce_on"]["ok"] and not lost and
                   r.get("enforcing", {}).get("ok", False) and not r["denials"] and "selinux now Permissive" in o)
        log(f"shelter {app}: confinement {'proved' if r['ok'] else 'NOT proved'}")
        out[app] = r
    return out


def joins(a, route, release):
    """The rung a box at RELEASE stands on, and the hops it walks on ROUTE from there: all of them from the route's
    `from`, the ones after it from a rung the route passes through short of `to` (SPEC "Route": a CentOS 6 box joins
    centos5-to-alma10 at centos6), none from anywhere else. The run says the rung it started at."""
    start = check.rung(a, release)
    if start == route["from"]:
        return start, list(route["hops"])
    return start, list(route["hops"][route["hops"].index(start) + 1:]) if start in route["hops"][:-1] else []


def replay(route_path, box_id, resume=None):
    """resume = (an earlier run's directory, HOP): start from that run's disk after the hop before HOP, to iterate on
    a late hop without replaying the early ones. Such a run is recorded as partial and never counts as a pass."""
    d, box_doc = build(box_id)
    route = load(route_path)
    a = guest_adapter(route["family"])
    sandbox.ensure_mirror(load(f"adapters/{route['family']}.toml"))
    fixes = {}
    for dirpath, _, files in os.walk(os.path.join(ROOT, "fixes", route["family"])):
        for f in files:
            doc = tomllib.load(open(os.path.join(dirpath, f), "rb"))
            fixes[check.content_id(doc)] = doc
    pinned = [fixes[r["id"]] for r in route["fixes"]]
    w = type("W", (), {})()  # the harness's own hop_cmds (tools/walk.py), on a walk with no chroot box
    w.adapter = a
    if SELINUX and box_doc.get("shelter"):  # the shelter's enforcing window sets the box back to permissive
        raise SystemExit("--selinux on a box with a [shelter] plan isn't built yet")
    key, prev = os.path.join(d, "key"), selinux_start(d, box_doc, a)
    start, todo = joins(a, route, box_doc["release"])
    if not todo:
        raise SystemExit(f"{box_id} starts at {start}: {route_path} neither starts there nor passes through it")
    if resume:
        if resume[1] not in todo[1:]:
            raise SystemExit(f"--from needs one of {todo[1:]}; for the whole route, replay without it")
        i = route["hops"].index(resume[1])
        # only from a hop that passed, on the same box: a failed hop's disk would make lost checks vacuous
        if not os.path.exists(os.path.join(resume[0], "run.json")):
            raise SystemExit(f"{resume[0]} has no run.json to say its hops passed")
        src = json.load(open(os.path.join(resume[0], "run.json")))
        done = {h["to"]: h for h in src["hops"]}
        if src["box"] != box_id or not done.get(route["hops"][i - 1], {}).get("ok"):
            raise SystemExit(f"{resume[0]} has no passing {route['hops'][i - 1]} hop on {box_id} to resume from")
        if src.get("firmware", "bios") != FIRMWARE:
            raise SystemExit(f"{resume[0]} ran on {src.get('firmware', 'bios')}, not {FIRMWARE}")
        if src.get("layout", {}) != {k: v for k, v in LAYOUT.items() if v}:
            raise SystemExit(f"{resume[0]} ran with layout {src.get('layout', {})}, not this one")
        if src.get("selinux") != SELINUX:
            raise SystemExit(f"{resume[0]} ran with --selinux {src.get('selinux')}, not {SELINUX}")
        prev = os.path.abspath(os.path.join(resume[0], f"{route['hops'][i - 1]}.qcow2"))
        todo = route["hops"][i:]
        if not os.path.exists(prev):
            raise SystemExit(f"no disk to resume from: {prev}")
        # that disk carries the ssh key of the box build it came from, which a changed adapter or vm.py has replaced
        chain = json.loads(subprocess.run(["qemu-img", "info", "-U", "--backing-chain", "--output=json", prev],
                                          capture_output=True, text=True, check=True).stdout)
        key = os.path.join(os.path.dirname(chain[-1]["filename"]), "key")
    reloc = None
    if RELOCATE:
        relocate_ready(a)
        reloc = Relocation(a, box_doc, box_doc.get("shelter", {}))
        joined = joins(a, route, box_doc["release"])[1]
        reloc.fills = {"new_disk": new_disk(hardware(a, box_doc["release"])), "route_mib": route_mib(a, route, pinned, joined),
                       "boot_mib": route_boot_mib(a, route, pinned, joined)}
        if resume and not all(x["ok"] for x in src.get("relocate", {}).get("stages", [{"ok": False}])):
            raise SystemExit(f"{resume[0]} didn't relocate the box: resume a --relocate run from one that did")
        if resume:  # the table that relocated it: a resumed run never runs Relocation.start's pick before its hops
            reloc.table = src["relocate"].get("table", "relocate")
            reloc.r, reloc.picked = a[reloc.table], True
    elif resume and "relocate" in src:
        raise SystemExit(f"{resume[0]} walked a relocated root: resume it with --relocate")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tag = "-".join(filter(None, [FIRMWARE, layout_tag(), f"selinux-{SELINUX}" if SELINUX else "",
                                 "relocate" if RELOCATE else ""]))
    out_dir = os.path.join(ROOT, ".wend", "vm", f"{stamp}-{box_id.replace('/', '_')}-{tag}-{os.getpid()}")
    w.head = head()
    at = w.head or {}
    log(f"replaying at {at.get('commit', 'no git head')}{' (with local changes)' if at.get('dirty') else ''}")
    os.makedirs(out_dir)
    port = box_doc.get("vm", {}).get("ssh_port", 22)
    hw = hardware(a, box_doc["release"])
    # SPEC "Sheltering an app": a box with a plan gets its shelters built (in a chroot copy, so as root) before the hop
    # that drops Python 2, and every hop from that one on proves confinement after its reboot
    plan = box_doc.get("shelter", {})
    drop = shelter.drop_hop(a, route) if plan else None
    sheltered = set(route["hops"][route["hops"].index(drop):]) if drop else set()
    rw = sh_steps = None
    if drop in todo:
        if shelter.keeps_runtimes_on(a, route) != route["from"] or start != route["from"]:
            raise SystemExit("the VM tier builds a shelter only on the route's start release so far")
        if os.geteuid() != 0:
            raise SystemExit(f"{box_id} has a shelter plan: building it takes a chroot copy of the box, so run this as "
                             "root (sudo -E)")
        rw, sh_steps = shelter_build(box_id, route, drop)
        w.shelter_steps = {drop: sh_steps} if sh_steps else {}
    try:
        return walk_vm(route, route_path, box_id, box_doc, a, w, pinned, todo, resume, out_dir, stamp, key, prev,
                       port, hw, plan, drop, sheltered, rw, sh_steps, reloc, start)
    finally:
        if rw:
            rw.close()


def selinux_read(g):
    """SELinux on the running guest (SELINUX_NOW): its mode and its config's SELINUX= ("none": no config). A read
    that fails has mode None, which is never a mode kept."""
    rc, out = g.ssh(SELINUX_NOW, timeout=60)
    words = out.split()
    if rc or len(words) != 2 or words[0] not in ("Enforcing", "Permissive", "Disabled"):
        return {"mode": None, "config": None, "error": f"rc={rc} {out[-300:]}"}
    return {"mode": words[0], "config": words[1]}


def kernel_is(running, newest):
    """Whether `uname -r` (running) names the release's newest kernel (the adapter's kernel_newest): EL7 and later put
    the arch on both, EL5's uname -r has none while the rpm query does, so a known arch suffix is dropped from both
    sides before they are compared."""
    def bare(k):
        return re.sub(r"\.(x86_64|i[3-6]86|noarch|aarch64|ppc64le|s390x)$", "", k)
    if not running or not newest:
        return False
    return newest in (running, running.rsplit(".", 1)[0]) or bare(newest) == bare(running)


def walk_vm(route, route_path, box_id, box_doc, a, w, pinned, todo, resume, out_dir, stamp, key, prev, port, hw,
            plan, drop, sheltered, rw, sh_steps, reloc=None, start=None):
    """replay's hops, once the shelters (if any) are built; with reloc (replay --relocate), on the relocated root."""
    hops, start_net, efi_start, start_selinux = [], None, None, None
    if reloc and not resume:
        moved = relocate_first(reloc, prev, key, port, hw, out_dir)
        todo, prev = (todo, moved) if moved else ([], prev)
    for hop in todo:
        disk = os.path.join(out_dir, f"{hop}.qcow2")
        overlay(prev, disk)
        g = Guest(disk, key, port, hw)
        g.start()
        if not g.wait_ssh():
            hops.append({"to": hop, "ok": False, "stage": "the guest didn't come up before the hop"})
            g.kill()  # free the forwarded port; the overlay and console stay for a look
            break
        if start_net is None:
            start_net = net_read(g, plan)
            if FIRMWARE == "uefi":  # the boot chain the first hop starts from
                efi_start = g.ssh(EFI_CHAIN)[1][-3000:]
            if f"default via {GUEST_HOST}" not in start_net or ADDR not in start_net:  # never compare two empty reads
                raise RuntimeError(f"the box's network can't be read at the start (no `ip`?): {start_net!r}")
        if start_selinux is None:
            start_selinux = selinux_read(g)
            if start_selinux["mode"] is None:
                raise RuntimeError(f"the box's SELinux can't be read at the start: {start_selinux['error']}")
        before = checks(g, box_doc, a)
        if hop == drop and not sh_steps:  # the build or the proof refused: the box stays on keeps_runtimes_on
            hops.append({"to": hop, "ok": False, "stage": "shelter refused: " +
                         next((x["stage"] for x in reversed(rw.steps) if not x["ok"]), "")})
            g.stop()
            break
        if hop == drop:
            for app in sorted(plan):
                rc, o = g.push_tree(os.path.realpath(rw.box.root), f"{shelter.SHELTER}/{app}")
                if not rc:
                    rc, o = g.ssh(shelter_copy_check(rw, app, plan[app]))
                log(f"shelter {app}: copied into the guest: {o.strip()[-300:]}")
                if rc:
                    hops.append({"to": hop, "ok": False, "stage": f"copying shelter {app} into the guest: {o[-1500:]}"})
                    break
            else:
                rc = 0
            if rc:
                g.stop()
                break
        matched = []
        for f in (f for f in pinned if f["hop"] == hop):
            rc = g.ssh(f"{backends.pred_sh(f['when'], a)} >/dev/null 2>&1")[0]
            if rc in (124, 255):  # ssh itself failed or timed out: not an answer about the precondition
                raise RuntimeError(f"couldn't evaluate {f['meta']['name']}'s precondition: ssh failed")
            if rc == 0:
                matched.append(f)
        log(f"hop to {hop}: {[f['meta']['name'] for f in matched]}")
        first, second = walk.Walk.hop_parts(w, hop, matched)
        after = [backends.fill(c, w.vars) for c in a["hop"].get("after", [])]
        t0 = time.time()
        h = {"to": hop, "fired_names": [f["meta"]["name"] for f in matched]}
        if (a["releases"].get(hop) or {}).get("reboot") == "before-post":
            # boot the new kernel before the post steps run the release's binaries (SPEC "Kernels and the bootloader")
            ok, steps = run_hop(g, first, [])  # hop.after runs once, after everything (SPEC "How a hop runs")
            if not ok:  # ...whatever happened
                steps += run_hop(g, [], after)[1]
            if ok:
                back, forced = g.reboot()
                h["mid_reboot"], h["mid_reboot_forced"] = back, forced
                if g.reboot_notes:
                    h["mid_reboot_notes"] = g.reboot_notes
                if back:  # the post steps run in the mode the box started in, too; an enforcing box runs the hop
                    # permissive (hop.before_transaction, wend#158)
                    h["mid_selinux"] = selinux_read(g)
                    mode = start_selinux["mode"]
                    back = h["mid_selinux"]["mode"] in ({mode, "Permissive"} if mode == "Enforcing" else {mode})
                ok = back
                if back:
                    ok, rest = run_hop(g, second, after)
                    steps += "\n##### after the reboot into the new kernel\n" + rest
        else:
            ok, steps = run_hop(g, first + second, after)
        open(os.path.join(out_dir, f"{hop}.steps.log"), "w").write(steps)
        h["commands_ok"], h["seconds"] = ok, round(time.time() - t0)
        back, forced = g.reboot() if ok else (None, False)  # None: commands failed, no reboot tried
        h["rebooted"], h["reboot_forced"] = back, forced
        if ok and g.reboot_notes:
            h["reboot_notes"] = g.reboot_notes
        if back:
            net = net_read(g, plan)
            after_checks = checks(g, box_doc, a)
            lost = sorted(n for n, (good, _) in before.items() if good and not after_checks.get(n, (False,))[0])
            h["net_kept"] = net == start_net
            # the release's newest kernel must be the one running (SPEC "Kernels and the bootloader"): an old kernel
            # under the new userland is what GRUB legacy did before the harness owned the boot entries
            rc, kv = g.ssh("uname -r; " + a["hop"]["kernel_newest"])
            lines = kv.split()
            h["kernel"] = lines[0] if lines else None
            running, newest = (lines + [None, None])[:2]
            h["kernel_newest"] = newest
            # every VM box has a kernel: no answer from the query is a failure, never a pass
            h["kernel_current"] = kernel_is(running, newest)
            h["lost"] = lost
            h["failed_logs"] = {n: after_checks[n][1][-1500:] for n in lost}
            h["firmware"] = g.ssh(FW_NOW)[1].strip()
            if FIRMWARE == "uefi":
                h["efi_chain"] = g.ssh(EFI_CHAIN)[1][-3000:]
            # SELinux runs as it did when the walk started: enforcing stays enforcing, a box with no policy stays without
            h["selinux"] = selinux_read(g)
            h["selinux_kept"] = h["selinux"]["mode"] is not None and h["selinux"]["mode"] == start_selinux["mode"]
            h["ok"] = ok and h["net_kept"] and not lost and h["kernel_current"] and h["firmware"] == FIRMWARE and \
                h["selinux_kept"]
            if reloc:  # every hop's reboot has to land on the relocated root again, through the stub
                h["root"] = g.ssh(reloc.r["booted"])[1].strip()
                h["ok"] = h["ok"] and h["root"] == "new"
            if not h["net_kept"]:
                h["net"] = {"start": start_net, "now": net}
            if hop in sheltered:
                h["shelter"] = shelter_confine(g, box_doc, a, plan,
                                               sorted(n for n, (good, _) in after_checks.items() if good))
                h["ok"] = h["ok"] and all(r["ok"] for r in h["shelter"].values())
        else:
            h["ok"] = False
            h["console_tail"] = open(g.console, errors="replace").read()[-4000:]
        log(f"hop to {hop}: {'OK' if h['ok'] else 'FAILED'} (commands {'ok' if ok else 'failed'}, "
            f"rebooted {back}{' (forced)' if forced else ''}, network kept {h.get('net_kept')}, lost {h.get('lost')}, "
            f"firmware {h.get('firmware')}, selinux {h.get('selinux', {}).get('mode')})")
        hops.append(h)
        g.stop()  # a failed hop's disk and console stay next to the run for a look (boot it with the same qemu line)
        if not h["ok"]:
            break
        prev = disk
    walked = bool(todo) and len(hops) == len(todo) and all(h["ok"] for h in hops)
    if reloc and walked:  # the old root still boots after the last hop, and the relocated one again after that
        back = relocate_back(reloc, prev, key, port, hw, out_dir)
        walked, prev = bool(back), back or prev
    if walked and route.get("finish", {}).get("prune_obsolete"):
        hops += prune(a, w, route["to"], box_doc, out_dir, prev, key, port, hw, start_net, plan)
    complete = bool(todo) and len(hops) == len(todo) + bool(route.get("finish", {}).get("prune_obsolete"))
    if reloc:
        complete = complete and walked and reloc.ok()
    run = {"schema": "wend.vmrun/0", "route": check.content_id(route), "box": box_id, "from": start or route["from"],
           "to": route["to"], "started": stamp, "head": w.head, "firmware": FIRMWARE,
           **({"efi_start": efi_start} if FIRMWARE == "uefi" else {}),
           "layout": {k: v for k, v in LAYOUT.items() if v}, **({"selinux": SELINUX} if SELINUX else {}),
           "selinux_start": start_selinux,
           **({"relocate": {**reloc.fills, "table": reloc.table, "stages": reloc.stages}} if reloc else {}), "hops": hops,
           "result": "pass" if complete and all(h["ok"] for h in hops) and not resume else
           "partial-pass" if complete and all(h["ok"] for h in hops) else "fail",
           "runner": {"kind": "vm", "host": os.uname().nodename, "kernel": os.uname().release}}
    if plan:  # SPEC "What a run records", from the guest's own lock and id range after the last hop it reached
        last = next((h["shelter"] for h in reversed(hops) if "shelter" in h), {})
        built = {r["app"]: r for r in (rw.shelters if rw else [])}
        run["shelters"] = [{"app": app, "release": built.get(app, {}).get("release"),
                            "lock_sha256": "sha256:" + last[app]["facts"]["lock"] if last.get(app, {}).get(
                                "facts", {}).get("lock") else built.get(app, {}).get("lock_sha256"),
                            "image": built.get(app, {}).get("image"), "tier": "vm",
                            "built": "chroot copy of the box on the route's start release, copied into the guest",
                            "confinement_proved": bool(last) and complete and any(  # from the drop hop, in this run
                                h["to"] == drop and "shelter" in h for h in hops) and all(
                                h.get("ok") for h in hops) and all(r["ok"] for r in last.values()),
                            "subuid": last.get(app, {}).get("facts", {}).get("subuid")} for app in sorted(plan)]
    if resume:
        run["resumed"] = {"from_run": os.path.abspath(resume[0]), "at_hop": resume[1]}
    json.dump(run, open(os.path.join(out_dir, "run.json"), "w"), indent=1)
    log(f"{run['result'].upper()}: {box_id} on {route_path}; run and disks in {os.path.relpath(out_dir, ROOT)}")
    return 0 if run["result"] == "pass" else 1


class Relocation:
    """Relocate (SPEC "Relocate") on one guest at a time: the adapter's [relocate] steps, and the boots between, each
    recorded in `stages` with which root came up (the adapter's `booted`), the box's checks against `before` (taken
    once, on the box as it was), its network against `start_net` and its firmware."""

    def __init__(self, a, box_doc, plan=None):
        self.r, self.a, self.box_doc, self.plan = a["relocate"], a, box_doc, plan or {}
        self.table, self.picked = "relocate", False
        self.stages, self.g, self.before, self.start_net = [], None, None, None

    def start(self, g):
        """The box as it is before anything moves: what every later boot is held to. The table is the adapter's
        [relocate], or [relocate_grub_legacy] on a machine that boots from /boot/grub/grub.conf with no /boot/grub2,
        picked once, on the box as it was (a route's later releases add /boot/grub2 beside GRUB legacy's config)."""
        self.g = g
        if not self.picked and "relocate_grub_legacy" in self.a and \
                g.ssh("[ -f /boot/grub/grub.conf ] && [ ! -e /boot/grub2 ]")[0] == 0:
            self.table, self.r = "relocate_grub_legacy", self.a["relocate_grub_legacy"]
        self.picked = True
        self.start_net = net_read(g, self.plan)
        if f"default via {GUEST_HOST}" not in self.start_net or ADDR not in self.start_net:  # never compare two empties
            raise RuntimeError(f"the box's network can't be read at the start (no `ip`?): {self.start_net!r}")
        self.before = checks(g, self.box_doc, self.a)

    def steps(self, fills, out_dir):
        t0 = time.time()
        ok, steps = run_hop(self.g, [backends.fill(c, fills) for k in check.RELOCATE_STEPS if k != "cleanup"
                                     for c in self.r[k]], [backends.fill(c, fills) for c in self.r["cleanup"]])
        open(os.path.join(out_dir, "relocate.steps.log"), "w").write(steps)
        self.stages.append({"stage": "relocate", "ok": ok, "seconds": round(time.time() - t0), "table": self.table,
                            "route_mib": fills["route_mib"], "boot_mib": fills["boot_mib"]})
        log(f"relocate: {'OK' if ok else 'FAILED'} in {round(time.time() - t0)} s (route_mib {fills['route_mib']})")
        return ok

    def came_up(self, name, want, back, forced=False, **extra):
        """Which root the guest came up on after a stage, and whether it's the box it was."""
        g = self.g
        h = {"stage": name, "want": want, "came_back": back, "forced": forced, **extra}
        if back:
            after = checks(g, self.box_doc, self.a)
            h["booted"] = g.ssh(self.r["booted"])[1].strip()
            h["lost"] = sorted(n for n, (good, _) in self.before.items() if good and not after.get(n, (False,))[0])
            h["failed_logs"] = {n: after[n][1][-1500:] for n in h["lost"]}
            h["net_kept"] = net_read(g, self.plan) == self.start_net
            h["firmware"] = g.ssh(FW_NOW)[1].strip()
            h["ok"] = h["booted"] == want and not h["lost"] and h["net_kept"] and h["firmware"] == FIRMWARE
        else:
            h["ok"] = False
            h["console_tail"] = open(g.console, errors="replace").read()[-4000:]
        log(f"{name}: {'OK' if h['ok'] else 'FAILED'} (came back {back}{' (forced)' if forced else ''}, root "
            f"{h.get('booted')!r}, wanted {want!r}, lost {h.get('lost')}, network kept {h.get('net_kept')})")
        self.stages.append(h)
        return h["ok"]

    def boot(self, name, cmd, want):
        """Run one of the adapter's commands between boots, reboot, and check the root that came up."""
        rc, out = self.g.ssh(cmd)
        if rc:
            self.stages.append({"stage": name, "ok": False, "output": out[-3000:]})
            log(f"{name}: FAILED ({out[-300:]})")
            return False
        return self.came_up(name, want, *self.g.reboot())

    def trial(self):
        """The trial boot right after the steps (the new root), timed."""
        t0 = time.time()
        back, forced = self.g.reboot()
        return self.came_up("trial", "new", back, forced, seconds=round(time.time() - t0))

    def ok(self):
        return bool(self.stages) and all(x["ok"] for x in self.stages)


def route_mib(a, route, pinned, hops=None):
    """The room a relocated root needs for ROUTE on top of the copy (docs/rebuild-and-carry.md, "Where the new root
    lives"): every hop's disk_mib and boot_mib (/boot is a directory on the new root, and SPEC leaves a /boot on / to
    disk_mib), and the kernel of each release a pinned fix passes through with rpm.transit. HOPS, when given, are the
    ones a replay walks: a box that joins the route partway needs room for those only."""
    hw = a.get("hardware", {})
    n = sum(hw.get(h, {}).get("disk_mib", 0) + hw.get(h, {}).get("boot_mib", 0)
            for h in route["hops"] if hops is None or h in hops)
    transit = {s["release"] for f in pinned if hops is None or f["hop"] in hops
               for k in ("pre", "post") for s in f.get(k, []) if s["op"] == "rpm.transit"}
    return n + sum(a["releases"][t]["transit"]["boot_mib"] for t in sorted(transit))


def route_boot_mib(a, route, pinned, hops=None):
    """The part of route_mib that lands in /boot: every hop's boot_mib and each transit kernel's. GRUB legacy's
    relocate gives the new root a /boot of its own, sized like the old one plus this."""
    hw = a.get("hardware", {})
    transit = {s["release"] for f in pinned if hops is None or f["hop"] in hops
               for k in ("pre", "post") for s in f.get(k, []) if s["op"] == "rpm.transit"}
    return sum(hw.get(h, {}).get("boot_mib", 0) for h in route["hops"] if hops is None or h in hops) + \
        sum(a["releases"][t]["transit"]["boot_mib"] for t in sorted(transit))


def new_disk(hw):
    """The first extra disk, as the guest names it: the new root's."""
    first = FIRST_DISK[hw[0]]
    return f"/dev/{first[:-1]}{chr(ord(first[-1]) + 1)}"


def relocate_ready(a):
    if not a.get("relocate"):
        raise SystemExit("this family's adapter has no [relocate] table")
    if FIRMWARE != "bios":
        raise SystemExit("relocate is BIOS-only for now (docs/rebuild-and-carry.md: UEFI comes after its own proof)")
    if not LAYOUT["extra_disks"]:
        raise SystemExit("relocate needs a disk for the new root: --extra-disk GB")


def relocate(box_id):
    """Relocate (SPEC "Relocate"): on a fresh copy of the box, the adapter's [relocate] steps copy the running root onto
    the first extra disk and flip the boot to it for one trial. Then, each followed by a check of which root came up
    (and the box's checks, its network and its firmware): the trial boot (the new root), a hard reset (the trial was
    one-shot: the old root), the trial again (the new root), commit to the trial and reboot (the new root, now the
    default), a one-shot rollback (the old root), one more reboot (the new root), a rollback that sticks (the old
    root, twice) and forward again (the new root). No route: `replay --relocate` walks one on the relocated root."""
    d, box_doc = build(box_id)
    a = guest_adapter(box_doc["family"])
    relocate_ready(a)
    hw = hardware(a, box_doc["release"])
    fills = {"new_disk": new_disk(hw), "route_mib": 0, "boot_mib": 0}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tag = "-".join(filter(None, [FIRMWARE, layout_tag()]))
    out_dir = os.path.join(ROOT, ".wend", "vm", f"{stamp}-{box_id.replace('/', '_')}-{tag}-relocate-{os.getpid()}")
    os.makedirs(out_dir)
    disk = os.path.join(out_dir, "relocate.qcow2")
    overlay(os.path.join(d, "base.qcow2"), disk)
    g = Guest(disk, os.path.join(d, "key"), box_doc.get("vm", {}).get("ssh_port", 22), hw)
    g.start()
    rel = Relocation(a, box_doc)
    run = {"schema": "wend.vmrelocate/0", "box": box_id, "started": stamp, "head": head(), "firmware": FIRMWARE,
           "layout": {k: v for k, v in LAYOUT.items() if v}, "new_disk": fills["new_disk"], "stages": rel.stages,
           "runner": {"kind": "vm", "host": os.uname().nodename, "kernel": os.uname().release}}

    def finish(result):
        run["result"] = result
        json.dump(run, open(os.path.join(out_dir, "run.json"), "w"), indent=1)
        log(f"{result.upper()}: relocate {box_id}; run and disk in {os.path.relpath(out_dir, ROOT)}")
        g.stop()  # a failed stage's disk and console stay for a look
        return 0 if result == "pass" else 1

    if not g.wait_ssh():
        rel.stages.append({"stage": "start", "ok": False,
                           "console_tail": open(g.console, errors="replace").read()[-4000:]})
        return finish("fail")
    rel.start(g)
    r, run["table"] = rel.r, rel.table
    if not rel.steps(fills, out_dir) or not rel.trial():
        return finish("fail")
    if not rel.came_up("reset during the trial", "old", g.reset()):
        return finish("fail")
    for name, cmd, want in (("trial again", r["arm"], "new"), ("commit to the trial", r["commit_trial"], "new"),
                            ("rollback once", r["rollback_next"], "old"), ("after the rollback", "true", "new"),
                            ("rollback", r["rollback"], "old"), ("still rolled back", "true", "old"),
                            ("forward", r["forward"], "new")):
        if not rel.boot(name, cmd, want):
            return finish("fail")
    run["new_root_used_mib"] = g.ssh("df -Pm / | awk 'NR == 2 {print $3}'")[1].strip()
    return finish("pass")


def relocate_first(rel, prev, key, port, hw, out_dir):
    """replay --relocate, before the first hop: relocate's steps on a copy of prev, the trial boot, and the commit to
    it (the relocated root is the default from here on). Returns the disk the route walks on, or None."""
    disk = os.path.join(out_dir, "relocate.qcow2")
    overlay(prev, disk)
    g = Guest(disk, key, port, hw)
    g.start()
    try:
        if not g.wait_ssh():
            rel.stages.append({"stage": "start", "ok": False,
                               "console_tail": open(g.console, errors="replace").read()[-4000:]})
            return None
        rel.start(g)
        ok = rel.steps(rel.fills, out_dir) and rel.trial() and rel.boot("commit to the trial", rel.r["commit_trial"],
                                                                        "new")
        return disk if ok else None
    finally:
        g.stop()  # a failed stage's disk and console stay for a look


def relocate_back(rel, prev, key, port, hw, out_dir):
    """replay --relocate, after the last hop: a rollback from the route's last release (whose kernel may not mount the
    old /boot, so it sticks) must boot the old root as it was at the start, and forward must boot the walked root again.
    Returns the disk after that, or None."""
    disk = os.path.join(out_dir, "rollback.qcow2")
    overlay(prev, disk)
    g = Guest(disk, key, port, hw)
    g.start()
    try:
        if not g.wait_ssh():
            rel.stages.append({"stage": "rollback after the route", "ok": False,
                               "console_tail": open(g.console, errors="replace").read()[-4000:]})
            return None
        rel.g = g
        if rel.before is None:  # resumed: the old root is held to the walked one's checks
            rel.start(g)
        ok = rel.boot("rollback after the route", rel.r["rollback"], "old") and \
            rel.boot("forward after the route", rel.r["forward"], "new")
        return disk if ok else None
    finally:
        g.stop()


def prune(a, w, to, box_doc, out_dir, prev, key, port, hw, start_net, plan):
    """The route's finish step, as the chroot walk's prune (tools/walk.py): purge the packages the target release no
    longer ships, then autoremove, splitting the set in halves whenever a step fails or a passing check is lost.
    Every attempt runs on its own overlay of the last good disk and reboots; a kept attempt becomes the next base.
    Returns its steps, each like a hop's."""
    steps, base, n = [], [prev], [0]

    def attempt(pkgs):
        n[0] += 1
        disk = os.path.join(out_dir, f"prune-{n[0]:02d}.qcow2")
        overlay(base[0], disk)
        g = Guest(disk, key, port, hw)
        g.start()
        h = {"to": f"prune-{n[0]:02d}", "asked": pkgs, "packages": pkgs}
        if not g.wait_ssh():  # the last good disk booted a moment ago: an infrastructure fault, not these packages'
            g.kill()
            raise RuntimeError(f"prune-{n[0]:02d}: the guest didn't come up from the last good disk")
        rc, listed = g.ssh(a["hop"]["installed_list"])
        if rc:
            g.kill()
            raise RuntimeError(f"prune-{n[0]:02d}: couldn't list the installed packages: {listed[-300:]}")
        pkgs = h["packages"] = [p for p in pkgs if p in set(listed.split())]  # an earlier autoremove took the rest
        if not pkgs:
            g.stop()
            h.update(ok=True, stage="already gone")
            return h
        before = checks(g, box_doc, a)
        cmds = [check.compile_step({"op": "remove", "packages": pkgs, "purge": True}, a, to),
                a["hop"]["autoremove"]]
        ok, out = run_hop(g, cmds, [backends.fill(c, w.vars) for c in a["hop"].get("after", [])])
        open(os.path.join(out_dir, f"prune-{n[0]:02d}.steps.log"), "w").write(out)
        back, forced = g.reboot() if ok else (None, False)
        h.update(commands_ok=ok, rebooted=back, reboot_forced=forced)
        if ok and g.reboot_notes:
            h["reboot_notes"] = g.reboot_notes
        if back:
            after = checks(g, box_doc, a)
            h["lost"] = sorted(k for k, (good, _) in before.items() if good and not after.get(k, (False,))[0])
            h["net_kept"] = net_read(g, plan) == start_net  # start_net leaves the shelters' bridges out too
            h["firmware"] = g.ssh(FW_NOW)[1].strip()
            h["ok"] = ok and h["net_kept"] and not h["lost"] and h["firmware"] == FIRMWARE
        else:
            h["ok"] = False
        g.stop()
        if h["ok"]:
            base[0] = disk
        log(f"prune {len(pkgs)} packages: {'kept' if h['ok'] else 'dropped'}")
        return h

    def go(pkgs):
        if not pkgs:
            return
        h = attempt(pkgs)
        steps.append(h)
        left = h["packages"]  # what was still installed when the attempt ran, as walk.prune splits
        if not h["ok"] and len(left) > 1:
            go(left[:len(left) // 2])
            go(left[len(left) // 2:])

    g = Guest(os.path.join(out_dir, "prune-00.qcow2"), key, port, hw)
    overlay(prev, g.disk)
    g.start()
    if not g.wait_ssh():
        g.kill()
        return [{"to": "prune", "ok": False, "stage": "the guest didn't come up"}]
    def obsolete(g):
        (rc1, listed), (rc2, out) = g.ssh(a["hop"]["installed_list"]), g.ssh(a["hop"]["obsolete"])
        if rc1 or rc2:  # an ssh failure must not read as "nothing to prune"
            g.kill()
            raise RuntimeError(f"couldn't read the obsolete packages for the prune: {(listed + out)[-300:]}")
        return [p for p in out.split() if p in set(listed.split())]

    try:
        pkgs = obsolete(g)
        g.stop()
        log(f"prune: {len(pkgs)} obsolete packages")
        go(pkgs)
        g = Guest(os.path.join(out_dir, "prune-end.qcow2"), key, port, hw)  # what's left, as walk.prune reports it
        overlay(base[0], g.disk)
        g.start()
        if not g.wait_ssh():
            g.kill()
            return [{"to": "prune", "ok": False, "stage": "the guest didn't come up after the prune", "attempts": steps}]
        left = obsolete(g)
        g.stop()
    except RuntimeError as e:  # recorded, so the run and its hop disks stay resumable
        return [{"to": "prune", "ok": False, "stage": str(e)[:500], "attempts": steps}]
    # the chroot walk keeps what it could and reports the rest; a step that was split and then kept in halves passed
    return [{"to": "prune", "ok": True, "obsolete_before": len(pkgs), "obsolete_after": len(left), "attempts": steps}]


def rehearse(route_path, box_id, client):
    """SPEC "Order of work" 4: `wend rehearse` from this host on a fresh copy of BOX, running. The client copies the
    guest's root over ssh, walks ROUTE on the copy, verified against CI's newest passing run of ROUTE on BOX
    (runs/), and writes a plan; it passes only if each hop fired the fixes that run did and the guest was only read."""
    if os.geteuid() != 0:
        raise SystemExit("rehearse runs the client's walk, which needs root (sudo -E)")
    d, box_doc = build(box_id)
    route = load(route_path)
    a = guest_adapter(route["family"])
    sandbox.ensure_mirror(load(f"adapters/{route['family']}.toml"))
    rid = check.content_id(route)
    name = os.path.basename(route_path)[:-len(".toml")]
    runs = sorted(glob.glob(os.path.join(ROOT, "runs", route["family"], name, box_id.split("/")[1], "*.json")))
    ci = [p for p in runs if (j := json.load(open(p)))["route"] == rid and j["result"] == "pass"]
    if not ci:
        raise SystemExit(f"runs/ has no passing CI run of this {route_path} on {box_id} to pin the rehearsal by")
    ci_path, ci_run = ci[-1], json.load(open(ci[-1]))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join(ROOT, ".wend", "vm", f"{stamp}-{box_id.replace('/', '_')}-{FIRMWARE}-rehearse-{os.getpid()}")
    os.makedirs(out_dir)
    disk = os.path.join(out_dir, "rehearse.qcow2")
    overlay(os.path.join(d, "base.qcow2"), disk)
    g = Guest(disk, os.path.join(d, "key"), box_doc.get("vm", {}).get("ssh_port", 22), hardware(a, box_doc["release"]))
    g.start()
    run = {"schema": "wend.vmrehearse/0", "box": box_id, "route": rid, "started": stamp, "head": head(),
           "firmware": FIRMWARE, "pinned_by": os.path.relpath(ci_path, ROOT),
           "runner": {"kind": "vm", "host": os.uname().nodename, "kernel": os.uname().release}}

    def finish(result):
        run["result"] = result
        json.dump(run, open(os.path.join(out_dir, "run.json"), "w"), indent=1)
        log(f"{result.upper()}: rehearse {box_id} on {route_path}; run and plan in {os.path.relpath(out_dir, ROOT)}")
        g.stop()
        return 0 if result == "pass" else 1

    if not g.wait_ssh():
        run["console_tail"] = open(g.console, errors="replace").read()[-4000:]
        return finish("fail")
    # read only: the boot and the package set (the adapter's installed_versions, the fingerprint's), before and after
    iv = load(f"adapters/{route['family']}.toml")["hop"]["installed_versions"]
    seen = f"cat /proc/sys/kernel/random/boot_id; ( {iv} ) | sha256sum"
    rc, before = g.ssh(seen)
    ssh = " ".join(shlex.quote(x) for x in g.ssh_argv("")[:-2])
    argv = [client, "--atlas", ROOT, "rehearse", route_path, "--from", "root@127.0.0.1", "--ssh", ssh,
            "--root", os.path.join(out_dir, "copy"), "--run", ci_path, "--checks", f"boxes/{box_id}/box.toml",
            "--plan", os.path.join(out_dir, "plan"), "--out", os.path.join(out_dir, "walk.json"),
            "--steps", os.path.join(out_dir, "steps.json"), "--work", os.path.join(out_dir, "work")]
    log("rehearsing: " + " ".join(shlex.quote(x) for x in argv))
    t0 = time.time()
    with open(os.path.join(out_dir, "rehearse.log"), "w") as f:
        p = subprocess.run(argv, stdout=f, stderr=subprocess.STDOUT, cwd=ROOT)
    run["client_rc"], run["seconds"] = p.returncode, round(time.time() - t0)
    rc2, after = g.ssh(seen)
    # the machine is only read: the same boot, the same packages
    run["machine_untouched"] = rc == 0 and rc2 == 0 and before == after
    plan_path = os.path.join(out_dir, "plan", "plan.json")
    if p.returncode != 0 or not os.path.exists(plan_path):
        return finish("fail")
    plan = json.load(open(plan_path))
    want = {h["to"]: sorted(h["fired"]) for h in ci_run["hops"]}
    got = {h["to"]: sorted(h["fired"]) for h in plan["hops"]}
    run["fired"] = {h: {"ci": want.get(h), "rehearsal": got.get(h)} for h in sorted(set(want) | set(got))}
    run["fired_match"] = want == got
    run["plan"] = {k: plan[k] for k in ("machine", "fingerprint_sha256", "checks_sha256", "cache_files",
                                         "generated_checks")}
    run["plan"]["kernel"] = plan["fingerprint"]["kernel"]
    log(f"fired {'the same fixes as' if want == got else 'other fixes than'} {os.path.relpath(ci_path, ROOT)}; "
        f"{plan['cache_files']} files cached; machine untouched: {run['machine_untouched']}")
    # the rehearsal reproduces CI's walk only if every hop fired the fixes CI proved on this box
    # and the plan holds the guest to what it ran (the client writes a plan only if they all held)
    gen = bool(a.get("hop", {}).get("generated")) <= bool(plan["generated_checks"])
    log(f"generated checks: {', '.join(plan['generated_checks']) or 'none'}")
    return finish("pass" if run["machine_untouched"] and run["fired_match"] and gen else "fail")


def main(args):
    global FIRMWARE, RELOCATE, SELINUX
    while args[:1] and args[0].startswith("--"):
        if args[0] == "--relocate":
            RELOCATE, args = True, args[1:]
        elif args[0] == "--uefi":
            FIRMWARE, args = "uefi", args[1:]
            for f in (OVMF_CODE, OVMF_VARS):
                if not os.path.exists(f):
                    raise SystemExit(f"--uefi needs OVMF ({f}; Debian and Ubuntu: the ovmf package)")
        elif args[0] == "--no-hwaddr":
            LAYOUT["no_hwaddr"], args = True, args[1:]
        elif args[0] == "--selinux" and len(args) > 1 and args[1] in SELINUX_MAKE:
            SELINUX, args = args[1], args[2:]
        elif args[0] == "--lvm":
            LAYOUT["lvm"], args = True, args[1:]
        elif args[0] == "--root-fs" and len(args) > 1 and args[1] in ROOT_FS:
            LAYOUT["root_fs"], args = args[1], args[2:]
        elif args[0] == "--extra-disk" and len(args) > 1 and args[1].isdigit() and 1 <= int(args[1]) <= 500:
            LAYOUT["extra_disks"], args = LAYOUT["extra_disks"] + [int(args[1])], args[2:]
        else:
            return __doc__
    if SELINUX and (args[:1] != ["replay"] or RELOCATE):
        # relocate's boots aren't held to the mode yet, and a build is the box as installed
        raise SystemExit("--selinux is for replay, without --relocate, so far")
    if args[:1] == ["shim-stop"] and len(args) == 2 and args[1].isdigit():
        return shim_stop(int(args[1]))
    if args[:1] in (["build"], ["replay"], ["relocate"], ["rehearse"]):
        use_shim()
    if args[:1] == ["build"] and len(args) == 2:
        build(args[1])
        return 0
    if args[:1] == ["replay"] and len(args) == 3:
        return replay(args[1], args[2])
    if args[:1] == ["replay"] and len(args) == 6 and args[3] == "--from":
        return replay(args[1], args[2], (args[4], args[5]))
    if args[:1] == ["rehearse"] and len(args) == 5 and args[3] == "--client" and not RELOCATE:
        return rehearse(args[1], args[2], os.path.abspath(args[4]))
    if args[:1] == ["relocate"] and len(args) == 2 and not RELOCATE:
        return relocate(args[1])
    return __doc__


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))  # through the finally below, like Ctrl-C
    signal.signal(signal.SIGHUP, lambda *_: sys.exit(129))
    try:
        sys.exit(main(sys.argv[1:]))
    finally:
        for guest in GUESTS:  # an exception or Ctrl-C must not leave a guest holding 3 GB and its forwarded port
            guest.kill()
            shutil.rmtree(os.path.dirname(guest.monitor_path), ignore_errors=True)
