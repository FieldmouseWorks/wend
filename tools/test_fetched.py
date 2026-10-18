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
    # a live repo's repomd: collect also takes every index it lists (but changelogs and deltas) from the origin
    live = "https://repo.almalinux.org/almalinux/9/BaseOS/x86_64/os/"
    fl, ui, oth, gone = b"<filelists/>", b"<updates/>", b"<otherdata/>", b"<gone/>"
    lmd = "".join(f'<data type="{t}"><checksum type="sha256">{hashlib.sha256(b).hexdigest()}</checksum>'
                  f'<location href="repodata/{n}"/></data>'
                  for t, n, b in (("filelists", "x-filelists.xml.gz", fl), ("updateinfo", "y-updateinfo.xml.gz", ui),
                                  ("other", "z-other.xml.gz", oth), ("modules", "m-modules.yaml.xz", gone),
                                  ("primary", "p-primary.xml.gz", b"<metadata/>"))).encode()
    lh = hashlib.sha256(lmd).hexdigest()
    os.makedirs(f"{work}/live/blobs")
    open(f"{work}/live/blobs/{lh}", "wb").write(lmd)
    served = {live + "repodata/x-filelists.xml.gz": fl, live + "repodata/y-updateinfo.xml.gz": b"<replaced/>",
              live + "repodata/z-other.xml.gz": oth}
    asked = []
    fetched.origin = lambda u: asked.append(u) or served[u]
    seen = {(live + "repodata/repomd.xml", f"sha256:{lh}", str(len(lmd))),
            (live + "repodata/p-primary.xml.gz", "sha256:" + "0" * 64, "11")}
    miss = []
    got = fetched.indexes(seen, f"{work}/live", miss)
    check(got == {(live + "repodata/x-filelists.xml.gz", f"sha256:{hashlib.sha256(fl).hexdigest()}", str(len(fl)))}
          and open(f"{work}/live/blobs/{hashlib.sha256(fl).hexdigest()}", "rb").read() == fl,
          "a live repo's filelists the walk never asked for are fetched, checked and kept")
    check(sorted(miss) == [live + "repodata/m-modules.yaml.xz", live + "repodata/y-updateinfo.xml.gz"],
          "an index the origin no longer has, or serves different bytes for, is reported missing")
    check(live + "repodata/z-other.xml.gz" not in asked and live + "repodata/p-primary.xml.gz" not in asked,
          "changelogs aren't fetched, nor an index the walk already listed")
    vault = {("https://vault.centos.org/7.9.2009/os/x86_64/repodata/repomd.xml", f"sha256:{lh}", "1")}
    asked.clear()
    check(fetched.indexes(vault, f"{work}/live", []) == set() and not asked, "a vault's repomd is left alone: it keeps "
          "its indexes")
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
    import http.server
    import threading
    import sandbox
    mine = hashlib.sha256(open(f"{HERE}/mirror.py", "rb").read()).hexdigest()
    check(get(".wend-shim").decode() == mine, "the shim names its own source's sha256 at /.wend-shim")
    try:
        sandbox.ensure_mirror({"mirror": {"shim": f"http://127.0.0.1:{port}", "probe": md}})
        check(True, "ensure_mirror reuses a running shim that is this checkout's tools/mirror.py")
    except RuntimeError as e:
        check(False, f"ensure_mirror refused this checkout's own shim: {e}")

    class Old(http.server.BaseHTTPRequestHandler):  # a shim from before /.wend-shim: 403 for any unknown host
        def do_GET(self):
            self.send_error(403)

        def log_message(self, *a):
            pass
    old = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Old)
    threading.Thread(target=old.serve_forever, daemon=True).start()
    try:
        sandbox.ensure_mirror({"mirror": {"shim": f"http://127.0.0.1:{old.server_address[1]}", "probe": md}})
        check(False, "ensure_mirror reused a shim from another checkout")
    except RuntimeError as e:
        check("another checkout" in str(e), "ensure_mirror refuses a shim from another checkout, naming shim-stop")
    old.shutdown()
finally:
    shim.terminate()
    shim.wait()
    subprocess.run(["rm", "-rf", work])
sys.exit(0 if all(results) else 1)
