"""The harness's RPM signature check (backends.checksig, SPEC "Signed packages") by behaviour: made-up RPMs signed by a
throwaway key's signing subkey (as AlmaLinux 8 signs) pass; a tampered one, one signed by a key the release doesn't
list, one with no header+payload signature and a file that isn't an RPM are refused, by name. It needs gpg and runs
the PR's own backends.py, so the gate's schema job (which never runs the PR's code) doesn't run it: `wend check` and
the client job do.
python3 tools/test_checksig.py"""
import os
import shutil
import struct
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backends  # noqa: E402

if not (shutil.which("gpg") and shutil.which("gpgv")):
    print("skipped  no gpg and gpgv on this machine")
    sys.exit(0)


def gpg(home, *args, data=None):
    return subprocess.run(["gpg", "--homedir", home, "--batch", "--yes", "--pinentry-mode", "loopback",
                           "--passphrase", "", *args], input=data, capture_output=True, check=True).stdout


def key(home, name):
    """A primary key that only certifies and a subkey that signs, as AlmaLinux 8's: its armoured public key."""
    os.makedirs(home, mode=0o700)
    gpg(home, "--quick-gen-key", name, "rsa2048", "cert", "never")
    fpr = [ln.split(":")[9] for ln in gpg(home, "--with-colons", "--list-keys").decode().splitlines()
           if ln.startswith("fpr:")][0]
    gpg(home, "--quick-add-key", fpr, "rsa2048", "sign", "never")
    return gpg(home, "--armor", "--export", fpr).decode()


def rpm(home, body, tag=1002):
    """A lead, a signature header holding a detached signature of body under `tag`, then body (header+payload)."""
    sig = gpg(home, "--detach-sign", data=body)
    head = b"\x8e\xad\xe8\x01" + b"\0" * 4 + struct.pack(">II", 1, len(sig)) + struct.pack(">IIII", tag, 7, 0, len(sig))
    head += sig
    pad = (8 - (96 + len(head)) % 8) % 8
    return b"\xed\xab\xee\xdb" + b"\0" * 92 + head + b"\0" * pad + body


with tempfile.TemporaryDirectory() as d:
    vendor, other = key(f"{d}/vendor", "vendor"), key(f"{d}/other", "other")
    keys = f"{d}/keys"
    os.makedirs(keys)
    with open(f"{keys}/VENDOR", "w") as f:  # two blocks in one file, as AlmaLinux 8's: the signer second
        f.write(key(f"{d}/old", "old") + vendor)
    pkgs = f"{d}/pkgs"
    os.makedirs(pkgs)
    body = b"\x8e\xad\xe8\x01" + os.urandom(20000)
    files = {"good-1.rpm": rpm(f"{d}/vendor", body), "good-2.rpm": rpm(f"{d}/vendor", body[:7777]),
             "tampered.rpm": None, "other-key.rpm": rpm(f"{d}/other", body), "no-sig.rpm": rpm(f"{d}/vendor", body, 268),
             "not-an-rpm.rpm": b"hello\n"}
    t = bytearray(files["good-1.rpm"])
    t[-100] ^= 1
    files["tampered.rpm"] = bytes(t)
    for n, b in files.items():
        with open(f"{pkgs}/{n}", "wb") as f:
            f.write(b)
    adapter = {"releases": {"r": {"gpgkeys": ["VENDOR"]}}, "keys": {}}
    saved, backends.KEYS = backends.KEYS, keys
    try:
        script = backends.checksig(adapter, "r", f"{d}/keydb", f"{pkgs}/*.rpm")
    finally:
        backends.KEYS = saved
    bad = 0
    for sh in [s for s in ("sh", "bash") if shutil.which(s)] + \
            ([os.environ["WEND_EL7_BASH"]] if os.environ.get("WEND_EL7_BASH") else []):
        r = subprocess.run([sh, "-c", script], capture_output=True, text=True)
        refused = {os.path.basename(x) for x in r.stdout.split() if x.endswith(".rpm")}
        want = {"tampered.rpm", "other-key.rpm", "no-sig.rpm", "not-an-rpm.rpm"}
        ok = r.returncode == 1 and refused == want and "refusing, not signed" in r.stdout
        bad += not ok
        print(("passed  " if ok else f"WRONG   rc={r.returncode} refused={sorted(refused)} {r.stderr.strip()[:200]} ") +
              f"{sh}: checksig() passes subkey-signed RPMs and refuses tampered, foreign-key, unsigned and non-RPM files")
        r = subprocess.run([sh, "-c", script.replace(f"{pkgs}/*.rpm", f"{pkgs}/good-*.rpm")], capture_output=True,
                           text=True)
        ok = r.returncode == 0
        bad += not ok
        print(("passed  " if ok else f"WRONG   rc={r.returncode} {r.stdout.strip()[:200]} ") +
              f"{sh}: checksig() passes when every RPM is signed by a listed key")
    if backends.checksig({"releases": {"r": {}}}, "r", f"{d}/keydb", "*.rpm") != "":
        bad += 1
        print("WRONG   checksig() isn't empty for a release that lists no keys")
sys.exit(1 if bad else 0)
