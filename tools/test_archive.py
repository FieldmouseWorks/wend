"""The archive's store by behaviour (docs/archive.md): offline, on a made-up repo. Only bytes that hash right
against a passing run's pins go in, only what a download list names comes along, and verify catches a bad blob.
python3 tools/test_archive.py"""
import gzip
import hashlib
import json
import os
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
w = tempfile.mkdtemp(prefix="wend-test-archive-")
sha = lambda b: hashlib.sha256(b).hexdigest()
results = []


def check(ok, what):
    results.append(ok)
    print(("passed  " if ok else "WRONG   ") + what)


def put(rel, data):
    os.makedirs(os.path.dirname(f"{w}/tree/{rel}"), exist_ok=True)
    open(f"{w}/tree/{rel}", "wb").write(data)


repo = "vault.example.org/7/os/x86_64"
used_rpm, other_rpm = b"\xed\xab\xee\xdb used", b"\xed\xab\xee\xdb not used"
primary = gzip.compress(("<metadata>" + "".join(
    f'<package type="rpm"><checksum type="sha256" pkgid="YES">{sha(b)}</checksum><location href="Packages/{n}"/>'
    f'</package>' for n, b in (("a-1.rpm", used_rpm), ("b-1.rpm", other_rpm))) + "</metadata>").encode())
filelists = gzip.compress(b"<filelists/>")
repomd = (f'<repomd><data type="primary"><checksum type="sha256">{sha(primary)}</checksum>'
          f'<location href="repodata/{sha(primary)}-primary.xml.gz"/></data>'
          f'<data type="filelists"><checksum type="sha256">{sha(filelists)}</checksum>'
          f'<location href="repodata/{sha(filelists)}-filelists.xml.gz"/></data></repomd>').encode()
# the live repo has moved on: the tree serves a newer repomd; the pinned one survives only in a download list
put(f"{repo}/repodata/repomd.xml", b"<repomd>newer</repomd>")
put(f"{repo}/repodata/{sha(primary)}-primary.xml.gz", primary)
put(f"{repo}/repodata/{sha(filelists)}-filelists.xml.gz", filelists)
put(f"{repo}/Packages/a-1.rpm", used_rpm)
put(f"{repo}/Packages/b-1.rpm", other_rpm)
os.makedirs(f"{w}/fetched/run.fetched/blobs")
open(f"{w}/fetched/run.fetched/blobs/{sha(repomd)}", "wb").write(repomd)
u = f"https://{repo}"
open(f"{w}/fetched/run.fetched/fetched.txt", "w").write(
    f"{u}/repodata/repomd.xml sha256:{sha(repomd)} {len(repomd)}\n{u}/Packages/a-1.rpm sha256:{sha(used_rpm)} 9\n")
os.makedirs(f"{w}/runs")
run = lambda result, digest: {"result": result, "box": "el/x", "started": "1",
                              "hops": [{"sources": {f"{u}/repodata/repomd.xml": f"sha256:{digest}"}}]}
json.dump(run("pass", sha(repomd)), open(f"{w}/runs/pass.json", "w"))
json.dump(run("fail", sha(b"<repomd>newer</repomd>")), open(f"{w}/runs/fail.json", "w"))

ingest = lambda *extra: subprocess.run([sys.executable, "-B", f"{HERE}/archive.py", "ingest", f"{w}/store", "--offline",
                                        "--tree", f"{w}/tree", "--runs", f"{w}/runs", *extra],
                                       capture_output=True, text=True)
p = ingest()
check(p.returncode == 0 and f"missing {u}/repodata/repomd.xml" in p.stdout and not os.path.exists(f"{w}/store/blob"),
      "a pin no source has at its digest is reported missing, and the newer bytes at its url are refused")
p = ingest("--fetched", f"{w}/fetched")
idx = json.load(open(f"{w}/store/index/latest.json"))["files"]
check(p.returncode == 0 and "0 referenced files not found" in p.stdout, "with the download list, the whole chain is found")
check(sorted(idx) == sorted([f"{u}/repodata/repomd.xml", f"{u}/repodata/{sha(primary)}-primary.xml.gz",
                             f"{u}/Packages/a-1.rpm"]),
      "stored: the pin, its package index and the package the walk used; not filelists or unused packages")
check(list(idx[f"{u}/repodata/repomd.xml"]) == [f"sha256:{sha(repomd)}"] and
      idx[f"{u}/Packages/a-1.rpm"][f"sha256:{sha(used_rpm)}"]["runs"] == ["el/x@1"],
      "the index maps each url to its digest and the passing runs that reference it")
blob = f"{w}/store/blob/sha256/{sha(used_rpm)[:2]}/{sha(used_rpm)}"
check(open(blob, "rb").read() == used_rpm, "a blob is the upstream bytes, under blob/sha256/<2 hex>/<64 hex>")
v = subprocess.run([sys.executable, "-B", f"{HERE}/archive.py", "verify", f"{w}/store"], capture_output=True, text=True)
check(v.returncode == 0, "verify passes a good store")
open(blob, "wb").write(b"rot")
v = subprocess.run([sys.executable, "-B", f"{HERE}/archive.py", "verify", f"{w}/store"], capture_output=True, text=True)
check(v.returncode == 1 and f"WRONG   {u}/Packages/a-1.rpm" in v.stdout, "verify names a blob that rotted")
subprocess.run(["rm", "-rf", w])
sys.exit(0 if all(results) else 1)
