"""The Ubuntu old-kernel purge (adapters/ubuntu.toml, hop.kernel_before, SPEC "Kernels and the bootloader") by
behaviour: every package of a kernel older than the running one goes, in one dpkg call, and nothing else does (the
running kernel, a newer one not booted yet, the metapackages, linux-firmware, removed-but-configured leftovers, an
old kernel on hold or one dpkg would refuse to purge because something still depends on it, and every kernel while
the running one isn't Ubuntu's), and a kept kernel stops the hop when the release's kernel (its boot_mib) won't fit
beside it. It runs
the PR's own adapter text, so the gate's schema job (which judges a PR with the base branch's tools and never runs the
PR's code) doesn't run it: `wend check` and the client job do.
python3 tools/test_kernels_apt.py"""
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

GUARD = "[ / -ef /proc/1/root ] || { echo \"not booted from this root: kernels left as they are\"; exit 0; }; "
OLD = ["linux-image-3.13.0-24-generic", "linux-image-extra-3.13.0-24-generic", "linux-headers-3.13.0-24",
       "linux-headers-3.13.0-24-generic", "linux-signed-image-4.4.0-9-generic", "linux-image-4.4.0-9-generic",
       "linux-modules-4.4.0-9-generic"]
KEEP = ["linux-image-4.4.0-21-generic", "linux-image-extra-4.4.0-21-generic", "linux-headers-4.4.0-21",
        "linux-image-4.4.0-210-generic", "linux-image-generic", "linux-headers-generic", "linux-firmware",
        "linux-libc-dev", "linux-tools-common"]
GONE = ["linux-image-3.2.0-23-generic"]  # removed, config files left: not installed, nothing to purge

a = tomllib.load(open(os.path.join(ROOT, "adapters", "ubuntu.toml"), "rb"))
step = fill(a["hop"]["kernel_before"][0], backends.of(a).hop_vars(a, "bionic"))
bad = 0
if not step.startswith(GUARD):
    print("WRONG   the purge doesn't start with the live-root guard")
    bad += 1
T13 = [q for q in OLD if "3.13.0-24" in q]
BOOT = int(a["hardware"]["bionic"]["boot_mib"])  # the room bionic's kernel needs, filled in as {boot_mib}
for name, running, refuse, held, free, dpkg_rc, want_rc, want in (
        ("kernels older than the running one go, with all their packages", "4.4.0-21-generic", [], [], 1, 0, 0,
         sorted(OLD)),
        ("nothing older than the running kernel: dpkg isn't called", "3.13.0-24-generic", [], [], 1, 0, 0, None),
        ("a metapackage still on an old kernel: that kernel stays, the others go", "4.4.0-21-generic",
         ["linux-image-3.13.0-24-generic"], [], BOOT, 0, 0, sorted(set(OLD) - set(T13))),
        ("an old kernel on hold stays, the others go", "4.4.0-21-generic", [], ["linux-headers-3.13.0-24"], BOOT, 0, 0,
         sorted(set(OLD) - set(T13))),
        ("a kept kernel leaves too little room for the hop's: the hop stops before its transaction",
         "4.4.0-21-generic", [], ["linux-headers-3.13.0-24"], BOOT - 1, 0, 1, sorted(set(OLD) - set(T13))),
        ("every old kernel refused: dpkg purges nothing", "4.4.0-21-generic", OLD, [], BOOT, 0, 0, None),
        ("a provider's kernel is running: every Ubuntu kernel stays", "6.1.10-x86_64-linode158", [], [], 1, 0, 0,
         None),
        ("the purge itself fails: the hop fails", "4.4.0-21-generic", [], [], BOOT, 2, 1, sorted(OLD))):
    with tempfile.TemporaryDirectory() as t:
        os.makedirs(f"{t}/bin")
        listing = ([f"{'hold' if p in held else 'install'} ok installed {p}" for p in OLD + KEEP] +
                   [f"deinstall ok config-files {p}" for p in GONE])
        shims = {"uname": f"echo {running}",
                 "dpkg-query": "cat <<'EOF'\n" + "\n".join(listing) + "\nEOF",
                 # --no-act: dpkg refuses when a package something still depends on is among those named
                 "dpkg": (f"if [ \"$1\" = --no-act ]; then for q in \"$@\"; do case ' {' '.join(refuse)} ' in "
                          f"*\" $q \"*) echo \" linux-image-generic depends on $q.\" >&2; exit 1;; esac; done; exit 0; "
                          f"fi; echo \"$@\" > {t}/dpkg-args; exit {dpkg_rc}"),
                 "df": f"echo 'Filesystem 1048576-blocks Used Available Capacity Mounted on'; "
                       f"echo '/dev/sda1 236 120 {free} 54% /boot'"}
        for tool, body in shims.items():
            with open(f"{t}/bin/{tool}", "w") as out:
                out.write(f"#!/bin/sh\n{body}\n")
            os.chmod(f"{t}/bin/{tool}", 0o755)
        p = subprocess.run(["sh", "-c", step[len(GUARD):]], capture_output=True, text=True,
                           env={**os.environ, "PATH": f"{t}/bin:{os.environ['PATH']}"})
        got = open(f"{t}/dpkg-args").read().split() if os.path.exists(f"{t}/dpkg-args") else None
        got = sorted(got[1:]) if got and got[0] == "--purge" else got
        ok = p.returncode == want_rc and got == want
        print(("passed  " if ok else f"WRONG   rc={p.returncode} dpkg {got} {p.stdout}{p.stderr} ") + name)
        bad += not ok
sys.exit(1 if bad else 0)
