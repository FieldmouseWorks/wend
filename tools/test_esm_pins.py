"""The Ubuntu op apt.drop-esm-pins (SPEC.md, Debian ops) by behaviour: the pin ubuntu-advantage-tools writes for the
release being left goes, and nothing else does (another release's pin, a file a package owns, a symlink, a directory,
a file with any line u-a-t never writes, one that pins to anything but never). It runs the PR's own step text, so the
gate's schema job (which judges a PR with the base branch's tools and never runs the PR's code) doesn't run it:
`wend check` and the client job do.
python3 tools/test_esm_pins.py"""
import os
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import backends  # noqa: E402

# ubuntu-advantage-tools 19.7ubuntu0.2 (trusty-updates), postinst configure_esm, as written
UAT = "# Written by ubuntu-advantage-tools\nPackage: *\nPin: release o=UbuntuESM, n=trusty\nPin-Priority: never\n"
step = backends.Apt().step({"op": "apt.drop-esm-pins", "release": "trusty"}, {})
bad = 0
for name, files, owned, want_gone in (
        ("u-a-t's ESM pin for the release being left goes", {"ubuntu-esm-infra-trusty": UAT}, [],
         ["ubuntu-esm-infra-trusty"]),
        ("the older name (ubuntu-esm-trusty) goes too", {"ubuntu-esm-trusty": UAT}, [], ["ubuntu-esm-trusty"]),
        ("another release's pin stays", {"ubuntu-esm-infra-xenial": UAT.replace("trusty", "xenial")}, [], []),
        ("a pin naming another release stays", {"ubuntu-esm-infra-trusty": UAT.replace("n=trusty", "n=xenial")}, [],
         []),
        ("a pin a package owns stays", {"ubuntu-esm-infra-trusty": UAT}, ["ubuntu-esm-infra-trusty"], []),
        ("a pin with a line u-a-t never writes stays",
         {"ubuntu-esm-infra-trusty": UAT + "Package: openssl\n"}, [], []),
        ("a file with only the priority line stays",
         {"ubuntu-esm-infra-trusty": "# local\nPin-Priority: never\n"}, [], []),
        ("a pin without its Pin: line stays",
         {"ubuntu-esm-infra-trusty": "Package: *\nPin-Priority: never\n"}, [], []),
        ("a pin without its Package: line stays",
         {"ubuntu-esm-infra-trusty": "Pin: release o=UbuntuESM, n=trusty\nPin-Priority: never\n"}, [], []),
        ("a pin with a numeric priority stays", {"ubuntu-esm-infra-trusty": UAT.replace("never", "-32768")}, [], []),
        ("comments and blank lines are u-a-t's shape", {"ubuntu-esm-apps-trusty": "\n" +
                                                         UAT.replace("UbuntuESM", "UbuntuESMApps")}, [],
         ["ubuntu-esm-apps-trusty"]),
        ("a symlink stays", {"ubuntu-esm-infra-trusty": None}, [], []),
        ("a directory stays", {"ubuntu-esm-infra-trusty": "dir"}, [], []),
        ("other pin files stay", {"local-pins": UAT, "ubuntu-esm-infra-trusty.bak": UAT}, [], []),
        ("nothing there: nothing to do", {}, [], [])):
    with tempfile.TemporaryDirectory() as t:
        os.makedirs(f"{t}/bin")
        os.makedirs(f"{t}/prefs")
        with open(f"{t}/target2", "w") as out:
            out.write(UAT)
        for f, body in files.items():
            if body is None:
                os.symlink(f"{t}/target2", f"{t}/prefs/{f}")
            elif body == "dir":
                os.makedirs(f"{t}/prefs/{f}")
            else:
                with open(f"{t}/prefs/{f}", "w") as out:
                    out.write(body)
        with open(f"{t}/bin/dpkg", "w") as out:
            out.write("#!/bin/sh\n[ \"$1\" = -S ] || exit 2\ncase \"$2\" in " +
                      "".join(f"{t}/prefs/{o}) echo \"ubuntu-advantage-tools: $2\"; exit 0;; " for o in owned) +
                      "esac\necho \"dpkg-query: no path found matching pattern $2\" >&2; exit 1\n")
        os.chmod(f"{t}/bin/dpkg", 0o755)
        p = subprocess.run(["sh", "-c", step.replace("/etc/apt/preferences.d/", f"{t}/prefs/")],
                           capture_output=True, text=True,
                           env={**os.environ, "PATH": f"{t}/bin:{os.environ['PATH']}"})
        gone = sorted(f for f in files if not os.path.lexists(f"{t}/prefs/{f}"))
        ok = p.returncode == 0 and gone == sorted(want_gone) and os.path.exists(f"{t}/target2")
        print(("passed  " if ok else f"WRONG   rc={p.returncode} gone {gone} {p.stdout}{p.stderr} ") + name)
        bad += not ok
sys.exit(1 if bad else 0)
