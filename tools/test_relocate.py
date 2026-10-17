"""Relocate (adapters/el.toml [relocate], SPEC "Relocate") by behaviour, the parts that don't need a machine: preflight
passes a default EL7 install with an empty second disk and writes the facts the later steps read, and refuses, with
its reason, every machine it can't relocate yet; the copy's fstab and kernel line name the new root alone; the flip
writes the stub GRUB parses, keeps the old config beside it, leaves the old root the default and arms the trial last.
The VM tier (tools/vm.py relocate) proves the rest on a real install. It runs the PR's own adapter text, so the gate's
schema job doesn't run it: `wend check` and the client job do. The flip and the stub need grub-editenv and
grub-script-check (grub-common), and are skipped without them. The scripts run in EL7's own shell, bash 4.2, which
parses some things newer shells accept differently (a case pattern's bare `)` inside `$( )`): with WEND_EL7_BASH set
to a bash 4.2 binary, every relocate script is parsed with it and the tests run under it; the client job builds one.
python3 tools/test_relocate.py"""
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
SH = os.environ.get("WEND_EL7_BASH") or "sh"
if SH == "sh":
    print("note    running under sh: set WEND_EL7_BASH to a bash 4.2 to parse and run the scripts as EL7 does")
else:
    for t in ("relocate", "relocate_grub_legacy"):
        for k, v in a[t].items():
            for i, c in enumerate(v if isinstance(v, list) else [v]):
                p = subprocess.run([SH, "-n", "-c", fill(c, {"new_disk": "/dev/vdb", "route_mib": 0, "boot_mib": 0})],
                                   capture_output=True, text=True)
                print(("passed  " if p.returncode == 0 else "WRONG   ") + f"EL7's bash parses {t}.{k}[{i}]" +
                      ("" if p.returncode == 0 else f"\n        {p.stderr.strip()[-300:]}"))
                bad += p.returncode != 0
UUID = "0b7e1a52-36f4-4d55-9f0e-3c1d2e5a6b70"


def local(script, t):
    """The script with the machine's paths moved under t (and a regular file standing for the disk)."""
    script = script.replace("($root)/boot/grub2", "($root)/BOOT-GRUB2")  # the stub's own paths, in GRUB's terms
    script = script.replace("($wend_new)/var/lib/wend", "($wend_new)/VAR-LIB-WEND")
    script = script.replace("$N/var/lib/wend", "$N/VAR-LIB-WEND")  # the copy's, not this machine's
    for old, new in (("/run/wend/new", f"{t}/new"), ("/var/lib/wend", f"{t}/state"), ("/boot/grub2", f"{t}/grub2"),
                     ("/usr/lib/grub/i386-pc", f"{t}/i386-pc"), ("/sys/firmware/efi", f"{t}/efi"),
                     (" /etc/fstab", f" {t}/fstab"), ("/proc/mounts", f"{t}/mounts"),
                     ("/var/run/yum.pid", f"{t}/yum.pid"), ('[ -b "$D" ]', '[ -f "$D" ]')):
        script = script.replace(old, new)
    return script.replace("($root)/BOOT-GRUB2", "($root)/boot/grub2").replace("($wend_new)/VAR-LIB-WEND",
                                                                               "($wend_new)/var/lib/wend").replace(
        "$N/VAR-LIB-WEND", "$N/var/lib/wend")


def shims(t, cmds):
    os.makedirs(f"{t}/bin", exist_ok=True)
    for name, body in cmds.items():
        open(f"{t}/bin/{name}", "w").write("#!/bin/sh\n" + body + "\n")
        os.chmod(f"{t}/bin/{name}", 0o755)


def run(script, t):
    env = {**os.environ, "PATH": f"{t}/bin:{os.environ['PATH']}"}
    p = subprocess.run([SH, "-c", script], capture_output=True, text=True, env=env)
    return p.returncode, p.stdout + p.stderr


# A default CentOS 7.0-7.2 install as preflight sees it: / on LVM centos/root and /boot on vda1, both XFS v4, BIOS,
# GRUB 2, the newest kernel running, and an empty 30 GB vdb.
GOOD = {"efi": False, "boot_type": "part", "root_lv": "centos root", "wend_root": False, "crypt": False,
        "home": False, "fstab_unmounted": False, "newest": "kernel-3.10.0-1160.el7.x86_64", "yum": False,
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
    mounts = "/ /dev/mapper/centos-root\n/boot /dev/vda1\n/dev devtmpfs\n/run tmpfs\n" + \
        ("/home /dev/mapper/centos-home\n" if m["home"] else "")
    shims(t, {
        "grub2-install": "exit 0", "rsync": "exit 0",
        "mountpoint": 'case "$2" in /boot) exit 0;; /data) exit 1;; esac; exit 0',
        "findmnt": f'case "$*" in "-no SOURCE /boot") echo /dev/vda1;; "-no SOURCE /") echo /dev/mapper/centos-root;;'
                   f' "-rno TARGET,SOURCE") printf "{mounts}";; esac',
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
        ("another filesystem from a disk mounted (/home): not carried, and both roots would share it", {"home": True},
         "/home is mounted from a disk"),
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
                ("the stub's entry 1 is the old config, from the old /boot even after a failed entry 0",
                 "set wend_boot=$root\nset wend_prefix=$prefix\n" in stub and
                 "set root=$wend_boot\n  set prefix=$wend_prefix\n  configfile /grub2/grub.cfg.wend-old" in stub),
                ("the stub never touches the configs' own saved_entry or next_entry",
                 "saved_entry" not in stub and "next_entry" not in stub),
                ("the stub falls back to the old root unless wend_default says 0",
                 stub.rstrip().endswith("}") and 'elif [ "${wend_default}" = 0 ]; then\n  set default=0\nelse\n  set '
                 'default=1\nfi' in stub),
                ("a rollback asked for on the new root wins over wend_default, after the one-shot boots",
                 f"search --no-floppy --fs-uuid --set=wend_new {UUID}" in stub and
                 "load_env -f ($wend_new)/var/lib/wend/relocate.grubenv wend_rollback" in stub and
                 stub.index('"${wend_next}" = 1') < stub.index('"${wend_rollback}" = 1') <
                 stub.index('"${wend_default}" = 0')),
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

# rollback writes a grubenv block of its own on the new root that GRUB (and grub-editenv) reads; forward removes it
if not shutil.which("grub-editenv"):
    print("skipped rollback and forward: no grub-editenv here (grub-common)")
else:
    with tempfile.TemporaryDirectory() as t:
        os.makedirs(f"{t}/state")
        os.makedirs(f"{t}/new/var/lib/wend")
        open(f"{t}/state/relocate.env", "w").write(f"VG=centos\nOLD_LV=root\nNEW_UUID={UUID}\n")
        for on, want in (("root", False), ("wend-root", True)):
            shims(t, {"lvs": f"echo '  {on}'", "findmnt": "echo /dev/mapper/centos-x", "xfs_io": "exit 0"})
            rc, out = run(local(r["rollback"], t), t)
            env = subprocess.run(["grub-editenv", f"{t}/state/relocate.grubenv", "list"], capture_output=True,
                                 text=True).stdout if os.path.exists(f"{t}/state/relocate.grubenv") else ""
            ok = (rc == 0 and env == "wend_rollback=1\n" and os.path.getsize(f"{t}/state/relocate.grubenv") == 1024
                  if want else rc != 0 and "not booted from the relocated root" in out and not env)
            print(("passed  " if ok else "WRONG   ") + ("rollback writes wend_rollback=1 in a 1024-byte grubenv block"
                                                      if want else "rollback refuses on the old root") +
                  ("" if ok else f"\n        rc={rc} {out[-300:]!r} {env!r}"))
            bad += not ok
        shutil.copy(f"{t}/state/relocate.grubenv", f"{t}/new/var/lib/wend/relocate.grubenv")
        shims(t, {"lvchange": "exit 0", "mount": "exit 0", "umount": "exit 0"})
        rc, out = run(local(r["forward"], t), t)
        ok = rc == 0 and not os.path.exists(f"{t}/new/var/lib/wend/relocate.grubenv")
        print(("passed  " if ok else "WRONG   ") + "forward removes the rollback from the new root" +
              ("" if ok else f"\n        rc={rc} {out[-300:]!r}"))
        bad += not ok

# GRUB legacy ([relocate_grub_legacy]): the copy's fstab and grub.conf on a stock CentOS 5 install, the stub the flip
# writes (the old config beside it, the saved default set to the old system before the trial is armed), and the
# grub shell commands the helper sends from the old root
L = a["relocate_grub_legacy"]
C5_FSTAB = ("LABEL=/                 /                       ext3    defaults        1 1\n"
            "LABEL=/boot             /boot                   ext3    defaults        1 2\n"
            "tmpfs                   /dev/shm                tmpfs   defaults        0 0\n"
            "LABEL=SWAP-vda2         swap                    swap    defaults        0 0\n")
C5_GRUB = ("# grub.conf generated by anaconda\n#boot=/dev/vda\ndefault=0\ntimeout=5\n"
           "splashimage=(hd0,0)/grub/splash.xpm.gz\nhiddenmenu\n"
           "title CentOS (2.6.18-419.el5)\n\troot (hd0,0)\n\tkernel /vmlinuz-2.6.18-419.el5 ro root=LABEL=/ console=ttyS0\n"
           "\tinitrd /initrd-2.6.18-419.el5.img\n"
           "title CentOS (2.6.18-398.el5)\n\troot (hd0,0)\n\tkernel /vmlinuz-2.6.18-398.el5 ro root=LABEL=/ panic=30\n"
           "\tinitrd /initrd-2.6.18-398.el5.img\n")
C5_ENV = "BOOT_DISK=/dev/vda\nBOOT_N=1\nROOT_FS=ext3\nNEW_DISK=/dev/vdb\nNEW_HD=hd1\nOLD_UUID=01d\n"


def legacy(script, t):
    """The script with the machine's paths moved under t (the copy's /boot/grub stays the copy's)."""
    script = script.replace("$N/boot/grub", "$N/BOOT-GRUB")
    for old, new in (("/mnt/wend/new", f"{t}/new"), ("/var/lib/wend", f"{t}/state"), ("/boot/grub", f"{t}/grub")):
        script = script.replace(old, new)
    return script.replace("$N/BOOT-GRUB", "$N/boot/grub")


with tempfile.TemporaryDirectory() as t:
    os.makedirs(f"{t}/new/etc")
    os.makedirs(f"{t}/new/boot/grub")
    os.makedirs(f"{t}/state")
    open(f"{t}/new/etc/fstab", "w").write(C5_FSTAB)
    open(f"{t}/new/boot/grub/grub.conf", "w").write(C5_GRUB)
    open(f"{t}/state/relocate.env", "w").write(C5_ENV)
    open(f"{t}/state/grub.map", "w").write("(hd0) /dev/vda\n(hd1) /dev/vdb\n")
    rc, out = run(legacy(L["fixup"][0], t), t)
    fstab = [line.split()[:3] for line in open(f"{t}/new/etc/fstab")]
    g = open(f"{t}/new/boot/grub/grub.conf").read()
    kernels = [x for x in g.splitlines() if x.split()[:1] == ["kernel"]]
    ok = rc == 0 and fstab == [["LABEL=wend-root", "/", "ext3"], ["LABEL=wend-boot", "/boot", "ext3"],
                               ["tmpfs", "/dev/shm", "tmpfs"], ["LABEL=SWAP-vda2", "swap", "swap"]] and \
        "(hd0," not in g and "splashimage=(hd1,0)/grub/splash.xpm.gz" in g and g.count("root (hd1,0)") == 2 and \
        kernels == ["\tkernel /vmlinuz-2.6.18-419.el5 ro root=LABEL=wend-root console=ttyS0 panic=10",
                    "\tkernel /vmlinuz-2.6.18-398.el5 ro root=LABEL=wend-root panic=30"] and \
        open(f"{t}/new/boot/grub/device.map").read() == "(hd0) /dev/vda\n(hd1) /dev/vdb\n"
    print(("passed  " if ok else "WRONG   ") + "GRUB legacy: the copy's fstab and grub.conf boot the new root from the new /boot" +
          ("" if ok else f"\n        rc={rc} {out[-300:]!r} {fstab} {g!r}"))
    bad += not ok

with tempfile.TemporaryDirectory() as t:
    os.makedirs(f"{t}/grub")
    os.makedirs(f"{t}/state")
    open(f"{t}/grub/grub.conf", "w").write(C5_GRUB)
    open(f"{t}/state/relocate.env", "w").write(C5_ENV)
    open(f"{t}/state/relocate-grub", "w").write(f'#!/bin/sh\necho "$*" >> {t}/calls\n')
    os.chmod(f"{t}/state/relocate-grub", 0o755)
    rc, out = run(legacy(L["flip"][0], t), t)
    stub = open(f"{t}/grub/grub.conf").read()
    calls = open(f"{t}/calls").read() if os.path.exists(f"{t}/calls") else ""
    ok = rc == 0 and open(f"{t}/grub/grub.conf.wend-old").read() == C5_GRUB and calls == "set 1\nset 0 --once\n" and \
        os.path.exists(f"{t}/state/relocate-flipped") and stub.splitlines()[1:] == [
            "timeout=5", "splashimage=(hd0,0)/grub/splash.xpm.gz", "hiddenmenu", "default saved",
            "title wend: relocated root", "\tconfigfile (hd1,0)/grub/grub.conf",
            "title wend rollback: the old root", "\tconfigfile (hd0,0)/grub/grub.conf.wend-old"]
    print(("passed  " if ok else "WRONG   ") + "GRUB legacy: the flip writes the stub, keeps the old config, and arms the "
          "trial after the old system is the saved default" + ("" if ok else f"\n        rc={rc} {out[-300:]!r} {stub!r} {calls!r}"))
    bad += not ok

# the helper, on a stage2 of its own: the header bytes it writes from the old root and (through the old /boot it
# mounts) from the new one, a one-shot that keeps the saved entry, and every write it refuses
def stage2(path, saved):
    b = bytearray(1024)
    b[518:520] = b"\x03\x02"
    b[524] = saved
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").write(bytes(b))


def header(path):
    return list(open(path, "rb").read()[524:527])


helper = L["refresh"][0].split("<<'EOF'\n", 1)[1].split("\nEOF\n", 1)[0]
for name, on, cmd, start, want_rc, want in (
        ("a one-shot of 0 from the old root keeps the saved 1", "01d", "set 0 --once", 1, 0, ("boot", [1, 0, 1])),
        ("a sticky 1 from the old root clears the one-shot", "01d", "set 1", 0, 0, ("boot", [1, 0, 0])),
        ("a sticky 0 from the new root lands in the old /boot's stage2", "9e1", "set 0", 1, 0, ("oldboot", [0, 0, 0])),
        ("a one-shot of 1 from the new root", "9e1", "set 1 --once", 0, 0, ("oldboot", [0, 1, 1])),
        ("an entry the stub hasn't", "01d", "set 2", 1, 2, ("boot", [1, 0, 0])),
        ("booted from neither root", "beef", "set 0", 1, 1, ("boot", [1, 0, 0])),
        ("a stage2 replaced since the flip", "01d", "set 0", 1, 1, ("boot", [1, 0, 0]))):
    with tempfile.TemporaryDirectory() as t:
        os.makedirs(f"{t}/state")
        os.makedirs(f"{t}/sys/block/vda/vda3")
        open(f"{t}/sys/block/vda/vda3/dev", "w").write("253:3\n")
        for d in ("boot", "oldboot"):
            stage2(f"{t}/{d}/grub/stage2", start)
        ino = os.stat(f"{t}/{'oldboot' if on == '9e1' else 'boot'}/grub/stage2")
        if name.startswith("a stage2 replaced"):
            os.rename(f"{t}/boot/grub/stage2", f"{t}/boot/grub/stage2.old")
            stage2(f"{t}/boot/grub/stage2", start)
        open(f"{t}/state/relocate.env", "w").write(C5_ENV + f'NEW_UUID=9e1\nSTUB_UUID=b00t\nSTAGE2="{ino.st_ino} 1024"\n')
        h = helper.replace("O=/boot\n", f"O={t}/boot\n").replace("/mnt/wend/oldboot", f"{t}/oldboot")
        for old, new in (("/sys/block", f"{t}/sys/block"), ("/var/lib/wend", f"{t}/state"),
                         ("/proc/sys/vm/drop_caches", f"{t}/drop")):
            h = h.replace(old, new)
        open(f"{t}/state/relocate-grub", "w").write(h)
        os.chmod(f"{t}/state/relocate-grub", 0o755)
        dev = (253 << 8) | 3
        shims(t, {"stat": f'[ "$*" = "-c %d /" ] && echo {dev} || exec /usr/bin/stat "$@"', "blkid": f"echo {on}",
                  "findfs": "echo /dev/vda1", "mount": f"echo \"$*\" >> {t}/mounted", "umount": "exit 0",
                  "sync": "exit 0"})
        rc, out = run(f"{t}/state/relocate-grub {cmd}", t)
        mounted = open(f"{t}/mounted").read() if os.path.exists(f"{t}/mounted") else ""
        where, bytes_ = want
        other = "oldboot" if where == "boot" else "boot"
        ok = rc == want_rc and header(f"{t}/{where}/grub/stage2") == bytes_ and \
            header(f"{t}/{other}/grub/stage2") == [start, 0, 0] and bool(mounted) == (on == "9e1" and rc == 0)
        print(("passed  " if ok else "WRONG   ") + f"GRUB legacy helper: {name}" +
              ("" if ok else f"\n        rc={rc} {out[-300:]!r} {header(f'{t}/boot/grub/stage2')} "
                             f"{header(f'{t}/oldboot/grub/stage2')} {mounted!r}"))
        bad += not ok

# an LV root (CentOS 5's default install): the copy's fstab and kernel lines name the new plain root, the swap LV stays
with tempfile.TemporaryDirectory() as t:
    os.makedirs(f"{t}/new/etc")
    os.makedirs(f"{t}/new/boot/grub")
    os.makedirs(f"{t}/state")
    open(f"{t}/new/etc/fstab", "w").write(
        "/dev/VolGroup00/LogVol00 /                       ext3    defaults        1 1\n"
        "LABEL=/boot             /boot                   ext3    defaults        1 2\n"
        "/dev/VolGroup00/LogVol01 swap                    swap    defaults        0 0\n")
    open(f"{t}/new/boot/grub/grub.conf", "w").write(C5_GRUB.replace("root=LABEL=/", "root=/dev/VolGroup00/LogVol00"))
    open(f"{t}/state/relocate.env", "w").write(C5_ENV)
    open(f"{t}/state/grub.map", "w").write("(hd0) /dev/vda\n(hd1) /dev/vdb\n")
    rc, out = run(legacy(L["fixup"][0], t), t)
    fstab = [line.split()[:3] for line in open(f"{t}/new/etc/fstab")]
    g = open(f"{t}/new/boot/grub/grub.conf").read()
    ok = rc == 0 and fstab == [["LABEL=wend-root", "/", "ext3"], ["LABEL=wend-boot", "/boot", "ext3"],
                               ["/dev/VolGroup00/LogVol01", "swap", "swap"]] and \
        "VolGroup00" not in g and g.count("root=LABEL=wend-root") == 2
    print(("passed  " if ok else "WRONG   ") + "GRUB legacy, LV root: the copy boots the plain new root, its swap LV stays" +
          ("" if ok else f"\n        rc={rc} {out[-300:]!r} {fstab!r} {g!r}"))
    bad += not ok

# device-mapper's name for the root's device number, as EL5's dmsetup and later ones print it (a minor past 255 too),
# in preflight and in the helper, which then tells the old LV root by its UUID
pre_l = L["preflight"][0]
dmof = next(x for x in pre_l.splitlines() if x.startswith("dmof() "))
for name, ls, dev, want in (("EL5's dmsetup", "VolGroup00-LogVol01\t(253, 1)\nVolGroup00-LogVol00\t(253, 0)", 253 << 8,
                             "VolGroup00-LogVol00"),
                            ("a later dmsetup", "vg-swap\t(253:1)\nvg-root\t(253:300)",
                             (300 & 255) | (253 << 8) | ((300 >> 8) << 20), "vg-root"),
                            ("no LV of that number", "vg-root\t(253:1)", 253 << 8, "")):
    with tempfile.TemporaryDirectory() as t:
        shims(t, {"stat": f'echo {dev}', "dmsetup": f"printf '{ls}\\n'"})
        rc, out = run(dmof + "\ndmof /", t)
        ok = out.strip() == want and (rc == 0) == bool(want)
        print(("passed  " if ok else "WRONG   ") + f"GRUB legacy: the root's LV by its device number, {name}" +
              ("" if ok else f"\n        rc={rc} {out!r}"))
        bad += not ok
with tempfile.TemporaryDirectory() as t:
    os.makedirs(f"{t}/state")
    os.makedirs(f"{t}/sys/block/vda/vda1")
    open(f"{t}/sys/block/vda/vda1/dev", "w").write("253:1\n")
    open(f"{t}/state/relocate.env", "w").write(C5_ENV + "NEW_UUID=9e1\n")
    h = helper
    for old, new in (("/sys/block", f"{t}/sys/block"), ("/var/lib/wend", f"{t}/state")):
        h = h.replace(old, new)
    shims(t, {"stat": f'echo {253 << 8}', "dmsetup": "printf 'VolGroup00-LogVol00\\t(253, 0)\\n'",
              "blkid": f'[ "$5" = /dev/mapper/VolGroup00-LogVol00 ] && echo 01d || echo vda1'})
    rc, out = run(f"set -- which\n{h}", t)
    ok = rc == 0 and out.strip() == "old"
    print(("passed  " if ok else "WRONG   ") + "GRUB legacy helper: an old root on an LV is the old root" +
          ("" if ok else f"\n        rc={rc} {out[-300:]!r}"))
    bad += not ok

# refresh checks the layout with the grub shell before any write: Red Hat's savedefault lands where the helper writes,
# a GRUB whose shell has no --once (upstream's) is refused, and either way the header is back as it was
probe = L["refresh"][0].split("\nEOF\n", 1)[1].split('echo "STAGE2=', 1)[1].split("\n", 1)[1]
REDHAT = ('read r; read c d o; n=${d#--default=}; set -- $(od -An -tu1 -j 524 -N1 "$F")\n'
          'if [ "$o" = --once ]; then printf "\\\\$(printf %03o $1)\\\\00$n\\\\001"; else printf "\\\\00$n\\\\00\\\\00"; fi'
          ' | dd of="$F" bs=1 seek=524 count=3 conv=notrunc 2>/dev/null')
UPSTREAM = ('read r; read c d o; n=${d#--default=}; [ -z "$o" ] || { echo "Error 12: Invalid device requested"; exit 0; }\n'
            'printf "\\\\00$n" | dd of="$F" bs=1 seek=524 count=1 conv=notrunc 2>/dev/null')
for name, shell, want_rc in (("Red Hat's stage2 passes the layout check", REDHAT, 0),
                             ("a stage2 whose grub shell has no --once is refused", UPSTREAM, 1)):
    with tempfile.TemporaryDirectory() as t:
        stage2(f"{t}/grub/stage2", 0)
        F = f"{t}/grub/stage2"
        shims(t, {"grub": f'F={F}\n{shell}', "sync": "exit 0"})
        h = probe.replace("/boot/grub/stage2", F).replace("/proc/sys/vm/drop_caches", f"{t}/drop")
        rc, out = run("set -e\nBOOT_N=1\n" + h, t)
        ok = rc == want_rc and header(F) == [0, 0, 0]
        print(("passed  " if ok else "WRONG   ") + f"GRUB legacy refresh: {name}, the header put back" +
              ("" if ok else f"\n        rc={rc} {out[-300:]!r} {header(F)}"))
        bad += not ok

# the room a relocated root needs for the CentOS 7 route: both hops' disk and /boot room, and EL8's transit kernel
import vm  # noqa: E402
route = tomllib.load(open(os.path.join(ROOT, "routes", "el", "centos7-to-alma10.toml"), "rb"))
fixes = {}
for dirpath, _, files in os.walk(os.path.join(ROOT, "fixes", "el")):
    for f in files:
        doc = tomllib.load(open(os.path.join(dirpath, f), "rb"))
        fixes[vm.check.content_id(doc)] = doc
hw = a["hardware"]
want = sum(hw[h]["disk_mib"] + hw[h]["boot_mib"] for h in ("alma9", "alma10")) + a["releases"]["alma8"]["transit"]["boot_mib"]
got = vm.route_mib(a, route, [fixes[x["id"]] for x in route["fixes"]])
print(("passed  " if got == want else "WRONG   ") + f"route_mib for centos7-to-alma10 is {want} MiB" +
      ("" if got == want else f" (got {got})"))
bad += got != want

sys.exit(1 if bad else 0)
