"""The archive's capture by behaviour (docs/archive.md, "Capture"): the mirror shim logs what it serves and keeps it
under its sha256, and tools/fetched.py turns the log into a run's download list with its metadata bytes. Offline: the
shim only serves files already in its cache. Like the other tools/test_*.py, the gate's schema job never runs this
(it judges a PR with the base's tools); `wend check` and the client job do.
python3 tools/test_fetched.py"""
import hashlib
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
work = tempfile.mkdtemp(prefix="wend-test-fetched-")
os.environ["WEND_WORK"] = work
sys.path.insert(0, HERE)
import fetched  # noqa: E402

cache = f"{work}/mirror"
files = {"vault.centos.org/7.9.2009/os/x86_64/repodata/repomd.xml": b"<repomd/>\n",
         "vault.centos.org/7.9.2009/os/x86_64/Packages/bash-4.2.46-34.el7.x86_64.rpm": b"\xed\xab\xee\xdb" + b"x" * 5000}
for rel, data in files.items():
    os.makedirs(os.path.dirname(f"{cache}/{rel}"), exist_ok=True)
    with open(f"{cache}/{rel}", "wb") as f:
        f.write(data)
sha = {rel: hashlib.sha256(data).hexdigest() for rel, data in files.items()}

with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
shim = subprocess.Popen([sys.executable, "-B", f"{HERE}/mirror.py", str(port), cache])
results = []


def check(ok, what):
    results.append(ok)
    print(("passed  " if ok else "WRONG   ") + what)


def get(rel, rng=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/{rel}", headers={"Range": rng} if rng else {})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read()


try:
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            break
        except OSError:
            time.sleep(0.1)
    since = fetched.mark()
    rpm, md = sorted(files)[1], sorted(files)[0]
    rpm, md = (rpm, md) if rpm.endswith(".rpm") else (md, rpm)
    check(get(md) == files[md] and get(rpm, "bytes=0-3") == files[rpm][:4] and get(rpm) == files[rpm],
          "the shim serves cached files whole and by range")
    log = open(f"{cache}/.fetched.log").read().splitlines()
    want = {f"https://{rel} sha256:{sha[rel]} {len(files[rel])}" for rel in files}
    check(set(log) == want and len(log) == 3, "every GET is logged as <https url> sha256:<hex> <size>")
    check(all(os.path.samefile(f"{cache}/.sha256/{sha[rel]}", f"{cache}/{rel}") for rel in files),
          "files cached before the store existed are hard-linked under .sha256/ when first served")
    try:
        get(".sha256/" + sha[md])
        check(False, "the store isn't reachable through the shim")
    except urllib.error.HTTPError as e:
        check(e.code == 403, "the store isn't reachable through the shim")
    out = f"{work}/run.fetched"
    n = fetched.collect(since, out)
    listed = open(f"{out}/fetched.txt").read().splitlines()
    check(n == 2 and listed == sorted(want), "collect lists each distinct file once, sorted")
    check(os.listdir(f"{out}/blobs") == [sha[md]] and open(f"{out}/blobs/{sha[md]}", "rb").read() == files[md],
          "collect copies the metadata's bytes and leaves packages to the archive")
    check(fetched.collect(fetched.mark(), f"{work}/nothing") == 0 and not os.path.exists(f"{work}/nothing"),
          "a walk that fetched nothing writes nothing")
    os.remove(f"{cache}/.sha256/{sha[md]}")
    with open(f"{cache}/.sha256/{sha[md]}", "wb") as f:
        f.write(b"tampered")
    n = fetched.collect(since, f"{work}/again")
    check(not os.path.exists(f"{work}/again/blobs/{sha[md]}") and
          open(f"{work}/again/missing.txt").read().split() == [f"https://{md}"],
          "a store file that doesn't hash to its name is reported missing, never copied")
finally:
    shim.terminate()
    shim.wait()
    subprocess.run(["rm", "-rf", work])
sys.exit(0 if all(results) else 1)
