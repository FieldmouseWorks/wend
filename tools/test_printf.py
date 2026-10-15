"""The harness's printf() and the EL adapter's {keys} by behaviour: every file they write holds exactly the content
asked for, under the shells hops run in (sh, bash, and EL7's bash 4.2 when WEND_EL7_BASH names one), also for content
that starts with "-" (every armoured key does) or holds %, a backslash or "\\n" as text; and {keys} on the alma9 hop
removes the AlmaLinux 8 keys a box has (a stand-in rpm on PATH) and nothing else. It runs the PR's own
backends.py and adapter text, so the gate's schema job (which never runs the PR's code) doesn't run it: `wend check`
and the client job do.
python3 tools/test_printf.py"""
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import backends  # noqa: E402

shells = [s for s in ("sh", "bash") if shutil.which(s)] + \
    ([os.environ["WEND_EL7_BASH"]] if os.environ.get("WEND_EL7_BASH") else [])
samples = {"dash-first": "-----BEGIN PGP PUBLIC KEY BLOCK-----\nabc\n-----END PGP PUBLIC KEY BLOCK-----\n",
           "percent": "100% sure %s %b %d\n", "backslash": "a\\b \\n \\c \\0101 end\n", "plain": "x=1\ny=2",
           "dashes-only": "--", "empty": ""}
with open(os.path.join(os.path.dirname(HERE), "adapters", "el.toml"), "rb") as f:
    el = tomllib.load(f)
bad = 0
for sh in shells:
    with tempfile.TemporaryDirectory() as d:
        for name, content in samples.items():
            p = os.path.join(d, name)
            r = subprocess.run([sh, "-c", backends.printf(content, p)], capture_output=True, text=True)
            got = open(p).read() if os.path.exists(p) else None
            ok = r.returncode == 0 and got == content
            bad += not ok
            print(("passed  " if ok else f"WRONG   rc={r.returncode} {r.stderr.strip()[:80]} got={got!r} ") +
                  f"{sh}: printf() writes {name!r} content exactly")
        # the adapter's own [keys], through write_keys() as set_release's {keys} runs it
        saved, backends.KEYS = backends.KEYS, os.path.join(d, "keys")
        try:
            r = subprocess.run([sh, "-c", backends.write_keys(el, "alma8")], capture_output=True, text=True)
        finally:
            backends.KEYS = saved
        for n, a in el.get("keys", {}).items():
            p = os.path.join(d, "keys", n)
            ok = r.returncode == 0 and os.path.exists(p) and open(p).read() == a
            bad += not ok
            print(("passed  " if ok else f"WRONG   rc={r.returncode} {r.stderr.strip()[:80]} ") +
                  f"{sh}: {{keys}} writes {n} exactly")
        os.makedirs(os.path.join(d, "bin"))
        with open(os.path.join(d, "bin", "rpm"), "w") as f:  # has 3abb34f8 and an unrelated key; logs what it removes
            f.write(f'#!/bin/sh\ncase "$*" in "-q gpg-pubkey-3abb34f8"|"-q gpg-pubkey-f4a80eb5") exit 0;; -q*) exit 1;; esac\n'
                    f'echo "$*" >> {d}/removed\n')
        os.chmod(os.path.join(d, "bin", "rpm"), 0o755)
        saved, backends.KEYS = backends.KEYS, os.path.join(d, "keys")
        try:
            r = subprocess.run([sh, "-c", backends.write_keys(el, "alma9")], capture_output=True, text=True,
                               env={**os.environ, "PATH": f"{d}/bin:{os.environ['PATH']}"})
        finally:
            backends.KEYS = saved
        got = open(f"{d}/removed").read() if os.path.exists(f"{d}/removed") else ""
        ok = r.returncode == 0 and got == "-e --allmatches gpg-pubkey-3abb34f8\n"
        bad += not ok
        print(("passed  " if ok else f"WRONG   rc={r.returncode} removed={got!r} ") +
              f"{sh}: {{keys}} on the alma9 hop removes only the dropped keys the box has")
sys.exit(1 if bad else 0)
