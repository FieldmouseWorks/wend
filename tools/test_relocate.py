"""Relocate (adapters/el.toml [relocate], SPEC "Relocate") by behaviour, the parts that don't need a machine: preflight
passes a default EL7 install with an empty second disk and writes the facts the later steps read, and refuses, with
its reason, every machine it can't relocate yet; the copy's fstab and kernel line name the new root alone; the flip
writes the stub GRUB parses, keeps the old config beside it, leaves the old root the default and arms the trial last.
The VM tier (tools/vm.py relocate) proves the rest on a real install. It runs the PR's own adapter text, so the gate's
schema job doesn't run it: `wend check` and the client job do. The flip and the stub need grub-editenv and
grub-script-check (grub-common), and are skipped without them. python3 tools/test_relocate.py"""
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib

sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from backends import fill  # noqa: E402

a = tomllib.load(open(os.path.join(ROOT, "adapters", "el.toml"), "rb"))
r = a["relocate"]
bad = 0
UUID = "0b7e1a52-36f4-4d55-9f0e-3c1d2e5a6b70"


def local(script, t):
    """The script with the machine's paths moved under t (and a regular file standing for the disk)."""
    script = script.replace("($root)/boot/grub2", "($root)/BOOT-GRUB2")  # the stub's own path, in GRUB's terms
    for old, new in (("/run/wend/new", f"{t}/new"), ("/var/lib/wend", f"{t}/state"), ("/boot/grub2", f"{t}/grub2"),
                     ("/usr/lib/grub/i386-pc", f"{t}/i386-pc"), ("/sys/firmware/efi", f"{t}/efi"),
                     (" /etc/fstab", f" {t}/fstab"), ("/proc/mounts", f"{t}/mounts"),
                     ("/var/run/yum.pid", f"{t}/yum.pid"), ('[ -b "$D" ]', '[ -f "$D" ]')):
        script = script.replace(old, new)
    return script.replace("($root)/BOOT-GRUB2", "($root)/boot/grub2")


def shims(t, cmds):
    os.makedirs(f"{t}/bin", exist_ok=True)
    for name, body in cmds.items():
        open(f"{t}/bin/{name}", "w").write("#!/bin/sh\n" + body + "\n")
        os.chmod(f"{t}/bin/{name}", 0o755)


def run(script, t):
    env = {**os.environ, "PATH": f"{t}/bin:{os.environ['PATH']}"}
    p = subprocess.run(["sh", "-c", script], capture_output=True, text=True, env=env)
    return p.returncode, p.stdout + p.stderr


# A default CentOS 7.0-7.2 install as preflight sees it: / on LVM centos/root and /boot on vda1, both XFS v4, BIOS,
# GRUB 2, the newest kernel running, and an empty 30 GB vdb.
GOOD = {"efi": False, "boot_type": "part", "root_lv": "centos root", "wend_root": False, "crypt": False,
        "v4_home": False, "fstab_unmounted": False, "newest": "kernel-3.10.0-1160.el7.x86_64", "yum": False,
        "disk_type": "disk", "disk_parts": 0, "disk_sig": "", "disk_mounted": False, "disk_pv": False,
        "disk_gb": 30, "used_k": 2_000_000, "stub": False}


def machine(t, m):
    disk = f"{t}/vdb"
    open(disk, "w").close()
    for d in ("i386-pc", "grub2", "state"):
        os.makedirs(f"{t}/{d}", exist_ok=True)
    if m["efi"]:
        os.makedirs(f"{t}/efi")
    open(f"{t}/grub2/grub.cfg", "w").write("menuentry 'CentOS Linux' {\n}\n")
    open(f"{t}/grub2/grubenv", "w").write("# GRUB Environment Block\n" + "#" * 999)
    if m["stub"]:
        open(f"{t}/grub2/grub.cfg.wend-old", "w").close()
    open(f"{t}/fstab", "w").write("/dev/mapper/centos-root / xfs defaults 0 0\nUUID=b00t /boot xfs defaults 0 0\n" +
                                  ("/dev/sdc1 /data xfs defaults 0 0\n" if m["fstab_unmounted"] else ""))
    open(f"{t}/mounts", "w").write("/dev/mapper/centos-root / xfs rw 0 0\n" +
                                   (f"{disk} /mnt ext4 rw 0 0\n" if m["disk_mounted"] else ""))
    xfs = "/\n/boot\n" + ("/home\n" if m["v4_home"] else "")
    shims(t, {
        "grub2-install": "exit 0", "rsync": "exit 0",
        "mountpoint": 'case "$2" in /boot) exit 0;; /data) exit 1;; esac; exit 0',
        "findmnt": f'case "$*" in "-no SOURCE /boot") echo /dev/vda1;; "-no SOURCE /") echo /dev/mapper/centos-root;;'
                   f' "-rno TARGET -t xfs") printf "{xfs}";; esac',
        "lsblk": f'case "$*" in "-no TYPE /dev/vda1") echo {m["boot_type"]};; "-no PKNAME /dev/vda1") echo vda;;'
                 f' "-rno TYPE") printf "disk\\npart\\nlvm\\n{"crypt" if m["crypt"] else ""}\\n";;'
                 f' "-dno TYPE {disk}") echo {m["disk_type"]};;'
                 f' "-no NAME {disk}") echo vdb; i=0; while [ $i -lt {m["disk_parts"]} ]; do echo vdb$i; i=$((i+1));'
                 ' done;; esac',
        "lvs": f'case "$*" in *vg_name,lv_name*) [ -n "{m["root_lv"]}" ] || exit 5; echo "  {m["root_lv"]}";;'
               f' *wend-root*) exit {0 if m["wend_root"] else 5};; esac',
        "xfs_info": 'echo "meta-data=x isize=256 agcount=4"; echo "         =  crc=0 finobt=0"',
        "rpm": f'echo "{m["newest"]}   Thu 01 Oct 2020"',
        "uname": "echo 3.10.0-1160.el7.x86_64",
        "pgrep": f'exit {0 if m["yum"] else 1}',
        "blkid": f'case "$*" in "-p {disk}") [ -n "{m["disk_sig"]}" ] || exit 2;; *TYPE*) echo {m["disk_sig"]};;'
                 f' *UUID*) echo b00t;; esac',
        "pvs": f'exit {0 if m["disk_pv"] else 5}',
        "blockdev": f'echo {m["disk_gb"] * 1024 ** 3}',
        "df": f'echo "Filesystem 1024-blocks Used Available Capacity Mounted"; echo "/dev/x 9 {m["used_k"]} 9 1% $3"'})
    return disk


pre = r["preflight"][0]
for name, change, want in (
        ("a default 7.0-7.2 install with an empty second disk", {}, None),
        ("UEFI", {"efi": True}, "UEFI isn't built yet"),
        ("/boot on LVM", {"boot_type": "lvm"}, "isn't a plain partition"),
        ("/ not on LVM", {"root_lv": ""}, "isn't on LVM"),
        ("a stub from an earlier relocate", {"stub": True}, "a relocate stub is already"),
        ("an earlier relocate's LV", {"wend_root": True}, "centos/wend-root exists already"),
        ("LUKS", {"crypt": True}, "LUKS isn't handled"),
        ("another XFS v4 filesystem mounted (/home)", {"v4_home": True}, "/home is XFS v4 too"),
        ("an XFS filesystem in fstab that isn't mounted", {"fstab_unmounted": True}, "isn't mounted"),
        ("a newer kernel waiting for a reboot", {"newest": "kernel-3.10.0-1160.119.1.el7.x86_64"}, "reboot first"),
        ("yum running", {"yum": True}, "yum is running"),
        ("a partition instead of a disk", {"disk_type": "part"}, "isn't a whole disk"),
        ("a disk with partitions", {"disk_parts": 1}, "isn't a whole disk"),
        ("a disk with a filesystem on it", {"disk_sig": "ext4"}, "has a signature on it (ext4)"),
        ("a mounted disk", {"disk_mounted": True}, "is mounted"),
        ("a disk that's a PV already", {"disk_pv": True}, "is an LVM PV already"),
        ("a disk too small for the copy", {"disk_gb": 2}, "the copy needs")):
    with tempfile.TemporaryDirectory() as t:
        disk = machine(t, {**GOOD, **change})
        rc, out = run(local(fill(pre, {"new_disk": disk, "route_mib": 0}), t), t)
        env = open(f"{t}/state/relocate.env").read() if os.path.exists(f"{t}/state/relocate.env") else ""
        if want is None:
            facts = dict(line.split("=", 1) for line in env.split())
            ok = rc == 0 and facts == {"VG": "centos", "OLD_LV": "root", "BOOT_DISK": "/dev/vda", "BOOT_UUID": "b00t",
                                       "NEW_DISK": disk}
        else:
            ok = rc != 0 and f"relocate refused: " in out and want in out and not env
        print(("passed  " if ok else "WRONG   ") + ("" if want is None else "refused ") + name +
              ("" if ok else f"\n        rc={rc} {out.strip()[-400:]!r} facts={env!r}"))
        bad += not ok

# the copy's fstab and kernel line: the new root alone, by UUID; /boot gone (a directory now); swap and the rest kept
FSTAB = """#
# /etc/fstab
#
/dev/mapper/centos-root /                       xfs     defaults        0 0
UUID=4f3c2a1b-0000-4000-8000-000000000001 /boot                   xfs     defaults        0 0
/dev/mapper/centos-swap swap                    swap    defaults        0 0
"""
GRUB = 'GRUB_TIMEOUT=5\nGRUB_CMDLINE_LINUX="crashkernel=auto rd.lvm.lv=centos/root rd.lvm.lv=centos/rootx rd.lvm.lv=centos/swap rhgb"\n'
for name, grub, want in (
        ("the old root's LV in the middle of the kernel line", GRUB,
         "rd.lvm.lv=centos/wend-root rd.lvm.lv=centos/rootx rd.lvm.lv=centos/swap rhgb"),
        ("the old root's LV last on the kernel line", 'GRUB_CMDLINE_LINUX="rhgb rd.lvm.lv=centos/root"\n',
         'rhgb rd.lvm.lv=centos/wend-root"'),
        ("no rd.lvm.lv at all", 'GRUB_CMDLINE_LINUX="rhgb quiet"\n', '"rhgb quiet"')):
    with tempfile.TemporaryDirectory() as t:
        os.makedirs(f"{t}/new/etc/default")
        os.makedirs(f"{t}/state")
        open(f"{t}/new/etc/fstab", "w").write(FSTAB)
        open(f"{t}/new/etc/default/grub", "w").write(grub)
        open(f"{t}/state/relocate.env", "w").write(f"VG=centos\nOLD_LV=root\nNEW_UUID={UUID}\n")
        rc, out = run(local(r["fixup"][0], t), t)
        fstab = [line.split() for line in open(f"{t}/new/etc/fstab") if line.strip() and not line.startswith("#")]
        g = open(f"{t}/new/etc/default/grub").read()
        ok = rc == 0 and fstab == [[f"UUID={UUID}", "/", "xfs", "defaults", "0", "0"],
                                   ["/dev/mapper/centos-swap", "swap", "swap", "defaults", "0", "0"]] and \
            want in g and "centos/root " not in g and 'centos/root"' not in g and \
            open(f"{t}/new/etc/fstab").read().startswith("#\n# /etc/fstab")
        print(("passed  " if ok else "WRONG   ") + f"the copy's fstab and kernel line: {name}" +
              ("" if ok else f"\n        rc={rc} {out[-300:]!r} {fstab} {g!r}"))
        bad += not ok

# the flip, with the real grub-editenv: the stub GRUB parses, the old config beside it as it was, the old root still
# the default, the trial armed
if not (shutil.which("grub-editenv") and shutil.which("grub-script-check")):
    print("skipped the flip and the stub: no grub-editenv or grub-script-check here (grub-common)")
else:
    with tempfile.TemporaryDirectory() as t:
        os.makedirs(f"{t}/grub2")
        os.makedirs(f"{t}/state")
        subprocess.run(["grub-editenv", f"{t}/grub2/grubenv", "create"], check=True)
        subprocess.run(["grub-editenv", f"{t}/grub2/grubenv", "set", "saved_entry=CentOS Linux 7"], check=True)
        old = "set default=\"${saved_entry}\"\nmenuentry 'CentOS Linux 7' {\n  linux16 /vmlinuz root=/dev/x\n}\n"
        open(f"{t}/grub2/grub.cfg", "w").write(old)
        open(f"{t}/state/relocate.env", "w").write(f"VG=centos\nOLD_LV=root\nNEW_UUID={UUID}\n")
        shims(t, {"grub2-editenv": 'exec grub-editenv "$@"', "fsfreeze": "exit 0"})
        script = local(r["flip"][0], t).replace("fsfreeze -f /boot", "fsfreeze -f x").replace("-u /boot", "-u x")
        rc, out = run(script, t)
        env = subprocess.run(["grub-editenv", f"{t}/grub2/grubenv", "list"], capture_output=True, text=True).stdout
        stub = open(f"{t}/grub2/grub.cfg").read()
        chk = subprocess.run(["grub-script-check", f"{t}/grub2/grub.cfg"], capture_output=True, text=True)
        for name, ok in (
                ("the flip runs", rc == 0),
                ("the old config moves beside the stub unchanged", open(f"{t}/grub2/grub.cfg.wend-old").read() == old),
                ("GRUB parses the stub", chk.returncode == 0),
                ("the stub's entry 0 goes to the new root by its UUID and sets the prefix there",
                 f"search --no-floppy --fs-uuid --set=root {UUID}" in stub and "set prefix=($root)/boot/grub2" in stub),
                ("the stub's entry 1 is the old config", "configfile /grub2/grub.cfg.wend-old" in stub),
                ("the stub never touches the configs' own saved_entry or next_entry",
                 "saved_entry" not in stub and "next_entry" not in stub),
                ("the stub falls back to the old root unless wend_default says 0",
                 stub.rstrip().endswith("}") and 'elif [ "${wend_default}" = 0 ]; then\n  set default=0\nelse\n  set '
                 'default=1\nfi' in stub),
                ("the old root stays the default and the trial is armed, the configs' variables kept",
                 sorted(env.split("\n")) == sorted(["saved_entry=CentOS Linux 7", "wend_default=1", "wend_next=0",
                                                    ""])),
                ("the flip is recorded", os.path.exists(f"{t}/state/relocate-flipped"))):
            print(("passed  " if ok else "WRONG   ") + name +
                  ("" if ok else f"\n        {out[-300:]!r} {env!r} {chk.stdout + chk.stderr!r}\n{stub}"))
            bad += not ok
    # arming is the flip's last write: an earlier failure leaves the old config booting and no trial armed
    lines = [line for line in r["flip"][0].splitlines() if line.strip()]
    ok = "set wend_next=0" in lines[-1] and all("wend_next=0" not in line for line in lines[:-1])
    print(("passed  " if ok else "WRONG   ") + "arming the trial is the flip's last write")
    bad += not ok

sys.exit(1 if bad else 0)
