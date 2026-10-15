"""The harness's RPM signature check (backends.checksig, SPEC "Signed packages") by behaviour: made-up RPMs signed by a
throwaway key's signing subkey (as AlmaLinux 8 signs) pass, with a header+payload signature or a header-only one (as
newer EPEL builds carry); a tampered one (header or payload), one signed by a key the release doesn't list, one with
no signature, a header-only one without a payload digest and a file that isn't an RPM are refused, by name. The
caller's variables survive it (rpm.swap-release's $f once didn't). It needs gpg and runs
the PR's own backends.py, so the gate's schema job (which never runs the PR's code) doesn't run it: `wend check` and
the client job do.
python3 tools/test_checksig.py"""
import hashlib
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


def sighead(entries):
    """A lead and a signature header holding entries {tag: bytes}, padded to 8."""
    idx, store = b"", b""
    for t, v in entries.items():
        idx += struct.pack(">IIII", t, 7, len(store), len(v))
        store += v
    head = b"\x8e\xad\xe8\x01" + b"\0" * 4 + struct.pack(">II", len(entries), len(store)) + idx + store
    return b"\xed\xab\xee\xdb" + b"\0" * 92 + head + b"\0" * ((8 - (96 + len(head)) % 8) % 8)


def rpm(home, body):
    """Signed header+payload (tag 1002) over body, as AlmaLinux 8's packages are."""
    return sighead({1002: gpg(home, "--detach-sign", data=body)}) + body


def header_only(home, payload, digest=True):
    """A main header holding the payload's sha256 (5092, algorithm 5093 = 8) signed alone (tag 268), as newer EPEL
    builds are, then the payload."""
    store = struct.pack(">I", 8) + hashlib.sha256(payload).hexdigest().encode() + b"\0"
    idx = struct.pack(">IIII", 5093, 4, 0, 1) + struct.pack(">IIII", 5092 if digest else 1000, 8, 4, 1)
    hdr = b"\x8e\xad\xe8\x01" + b"\0" * 4 + struct.pack(">II", 2, len(store)) + idx + store
    return sighead({268: gpg(home, "--detach-sign", data=hdr)}) + hdr + payload


with tempfile.TemporaryDirectory() as d:
    vendor, other = key(f"{d}/vendor", "vendor"), key(f"{d}/other", "other")
    keys = f"{d}/keys"
    os.makedirs(keys)
    with open(f"{keys}/VENDOR", "w") as f:  # two blocks in one file, as AlmaLinux 8's: the signer second
        f.write(key(f"{d}/old", "old") + vendor)
    pkgs = f"{d}/pkgs"
    os.makedirs(pkgs)
    body = b"\x8e\xad\xe8\x01" + os.urandom(20000)
    payload = os.urandom(30000)
    files = {"good-1.rpm": rpm(f"{d}/vendor", body), "good-2.rpm": rpm(f"{d}/vendor", body[:7777]),
             "good-header-only.rpm": header_only(f"{d}/vendor", payload),
             "other-key.rpm": rpm(f"{d}/other", body), "no-sig.rpm": sighead({1000: b"1234"}) + body,
             "header-only-no-digest.rpm": header_only(f"{d}/vendor", payload, digest=False),
             "not-an-rpm.rpm": b"hello\n"}
    for n, src, at in (("tampered.rpm", "good-1.rpm", -100), ("tampered-payload.rpm", "good-header-only.rpm", -100),
                       ("tampered-header.rpm", "good-header-only.rpm", -len(payload) - 5)):
        t = bytearray(files[src])
        t[at] ^= 1
        files[n] = bytes(t)
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
        want = {n for n in files if not n.startswith("good-")}
        ok = r.returncode == 1 and refused == want and "refusing, not signed" in r.stdout
        bad += not ok
        print(("passed  " if ok else f"WRONG   rc={r.returncode} refused={sorted(refused)} {r.stderr.strip()[:200]} ") +
              f"{sh}: checksig() passes subkey-signed RPMs and refuses tampered, foreign-key, unsigned and non-RPM files")
        r = subprocess.run([sh, "-c", "f=keep r=keep2 k=keep3; " + script.replace(f"{pkgs}/*.rpm", f"{pkgs}/good-*.rpm")
                            + '; echo "[$f $r $k]"'], capture_output=True, text=True)
        ok = r.returncode == 0 and "[keep keep2 keep3]" in r.stdout
        bad += not ok
        print(("passed  " if ok else f"WRONG   rc={r.returncode} {r.stdout.strip()[-200:]} ") +
              f"{sh}: checksig() leaves the caller's $f, $r and $k alone")
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
