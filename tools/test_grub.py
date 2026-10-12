"""The EL GRUB legacy step (adapters/el.toml, hop.kernel_after, SPEC "Kernels and the bootloader") by behaviour: the
release kernel's new entry copies the root stanza and the arguments from GRUB's default entry, not from the first entry
in the file, and becomes the default. It runs the PR's own adapter text, so the gate's schema job (which judges a PR
with the base branch's tools and never runs the PR's code) doesn't run it: `wend check` and the client job do.
python3 tools/test_grub.py"""
import os
import subprocess
import sys
import tempfile
import tomllib

sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from backends import fill  # noqa: E402
import backends  # noqa: E402

GUARD = "[ / -ef /proc/1/root ] || exit 0; "
NEW = "2.6.32-754.el6.x86_64"
CONF = """default={d}
timeout=5
title Other system
\troot (hd1,0)
\tkernel /vmlinuz-2.6.18-1.el5 ro root=/dev/sdb1
\tinitrd /initrd-2.6.18-1.el5.img
title CentOS (2.6.18-419.el5)
\troot (hd0,0)
\tkernel /vmlinuz-2.6.18-419.el5 ro root=LABEL=/ quiet
\tinitrd /initrd-2.6.18-419.el5.img
"""

a = tomllib.load(open(os.path.join(ROOT, "adapters", "el.toml"), "rb"))
step = fill(a["hop"]["kernel_after"][1], backends.of(a).hop_vars(a, "centos6"))
bad = 0
if not step.startswith(GUARD):
    print("WRONG   the GRUB step doesn't start with the live-root guard")
    bad += 1
for d, root, args in ((1, "(hd0,0)", "ro root=LABEL=/ quiet"), (0, "(hd1,0)", "ro root=/dev/sdb1")):
    with tempfile.TemporaryDirectory() as t:
        os.makedirs(f"{t}/boot/grub")
        os.makedirs(f"{t}/bin")
        for f in (f"vmlinuz-{NEW}", f"initramfs-{NEW}.img", "vmlinuz-2.6.18-1.el5", "vmlinuz-2.6.18-419.el5"):
            open(f"{t}/boot/{f}", "w").close()
        with open(f"{t}/boot/grub/grub.conf", "w") as out:
            out.write(CONF.format(d=d))
        with open(f"{t}/bin/rpm", "w") as out:  # rpm -q --qf '%{INSTALLTIME} %{VERSION}-%{RELEASE}.%{ARCH}\n' kernel
            out.write(f"#!/bin/sh\necho '100 {NEW}'\n")
        os.chmod(f"{t}/bin/rpm", 0o755)
        p = subprocess.run(["sh", "-c", step[len(GUARD):].replace("/boot/", f"{t}/boot/")], capture_output=True,
                           text=True, env={**os.environ, "PATH": f"{t}/bin:{os.environ['PATH']}"})
        lines = open(f"{t}/boot/grub/grub.conf").read().splitlines()
        want = ["default=0", "timeout=5", f"title {NEW}", f"\troot {root}", f"\tkernel /vmlinuz-{NEW} {args} ",
                f"\tinitrd /initramfs-{NEW}.img"]
        ok = p.returncode == 0 and lines[:6] == want and len(lines) == 14
        print(("passed  " if ok else f"WRONG   rc={p.returncode} {p.stdout}{p.stderr} {lines[:6]} ") +
              f"GRUB legacy, default={d}: the new entry takes the default entry's root {root} and arguments")
        bad += not ok
sys.exit(1 if bad else 0)
