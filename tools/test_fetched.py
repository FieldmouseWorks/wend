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
         "vault.centos.org/7.9.2009/os/x86_64/Packages/bash-4.2.46-34.el7.x86_64.rpm": b"\xed\xab\xee\xdb" + b"x" * 5000,
         "dl.fedoraproject.org/pub/epel/9/Everything/x86_64/Packages/f/fail2ban-1.1.0-6.el9.noarch.rpm": b"\xed\xab\xee\xdb epel"}
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
    md = next(r for r in files if r.endswith(".xml"))
    rpm = next(r for r in files if r.startswith("vault") and r.endswith(".rpm"))
    epel = next(r for r in files if r.startswith("dl.fedoraproject.org"))
    check(get(md) == files[md] and get(rpm, "bytes=0-3") == files[rpm][:4] and get(rpm) == files[rpm] and
          get(epel) == files[epel],
          "the shim serves cached files whole and by range")
    log = open(f"{cache}/.fetched.log").read().splitlines()
    want = {f"https://{rel} sha256:{sha[rel]} {len(files[rel])}" for rel in files}
    check(set(log) == want and len(log) == 4, "every GET is logged as <https url> sha256:<hex> <size>")
    check(all(os.path.samefile(f"{cache}/.sha256/{sha[rel]}", f"{cache}/{rel}") for rel in files),
          "files cached before the store existed are hard-linked under .sha256/ when first served")
    try:
        get(".sha256/" + sha[md])
        check(False, "the store isn't reachable through the shim")
    except urllib.error.HTTPError as e:
        check(e.code == 403, "the store isn't reachable through the shim")
    out = f"{work}/run.fetched"
    os.makedirs(f"{out}/blobs")  # a stale or planted one: junk, and a symlink out of the directory
    open(f"{out}/blobs/junk", "w").write("old")
    open(f"{work}/target", "w").write("untouched")
    os.symlink(f"{work}/target", f"{out}/fetched.txt")
    n = fetched.collect(since, out)
    check(open(f"{work}/target").read() == "untouched" and not os.path.exists(f"{out}/blobs/junk"),
          "an existing download list is replaced, and its symlinks aren't followed")
    listed = open(f"{out}/fetched.txt").read().splitlines()
    check(n == 3 and listed == sorted(want), "collect lists each distinct file once, sorted")
    check(sorted(os.listdir(f"{out}/blobs")) == sorted([sha[md], sha[epel]]) and
          open(f"{out}/blobs/{sha[md]}", "rb").read() == files[md],
          "collect copies the metadata's and live EPEL packages' bytes, and leaves other packages to the archive")
    os.makedirs(f"{work}/nothing")
    check(fetched.collect(fetched.mark(), f"{work}/nothing") == 0 and not os.path.exists(f"{work}/nothing"),
          "a walk that fetched nothing writes nothing, and leaves no stale list")
    sys.path.insert(0, HERE)
    import importlib.util
    spec = importlib.util.spec_from_file_location("shim", f"{HERE}/mirror.py")
    shim_mod = importlib.util.module_from_spec(spec)
    sys.argv[1:] = ["0", f"{work}/race"]
    spec.loader.exec_module(shim_mod)
    os.makedirs(f"{work}/race")
    open(f"{work}/race/old", "w").write("old")
    st_old = os.stat(f"{work}/race/old")
    open(f"{work}/race/new", "w").write("new")
    os.replace(f"{work}/race/new", f"{work}/race/old")  # a refetch replaced the file that was hashed
    shim_mod.keep(f"{work}/race/old", "ab" * 32, st_old)
    check(not os.path.exists(f"{work}/race/.sha256/{'ab' * 32}"),
          "a file a refetch replaced after it was hashed is never linked under the old digest")
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
