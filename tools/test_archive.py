"""The archive's store by behaviour (docs/archive.md): offline, on a made-up repo. Only bytes that hash right
against a passing run's pins go in, only what a download list names comes along, and verify catches a bad blob.
python3 tools/test_archive.py"""
import gzip
import hashlib
import json
import lzma
import os
import shutil
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
used_rpm, other_rpm, old_rpm = b"\xed\xab\xee\xdb used", b"\xed\xab\xee\xdb not used", b"\xed\xab\xee\xdb sha1"
sha1 = lambda b: hashlib.sha1(b).hexdigest()
primary = gzip.compress(("<metadata>" + "".join(
    f'<package type="rpm"><checksum type="{t}" pkgid="YES">{h(b)}</checksum><location href="Packages/{n}"/>'
    f'</package>' for n, b, t, h in (("a-1.rpm", used_rpm, "sha256", sha), ("b-1.rpm", other_rpm, "sha256", sha),
                                     ("c-1.rpm", old_rpm, "sha", sha1),
                                     ("d-1.rpm", b"md5 only", "md5", lambda b: hashlib.md5(b).hexdigest())))
    + "</metadata>").encode())
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
put(f"{repo}/Packages/c-1.rpm", old_rpm)  # CentOS 5 style: its index names it only by SHA-1
os.makedirs(f"{w}/fetched/run.fetched/blobs")
open(f"{w}/fetched/run.fetched/blobs/{sha(repomd)}", "wb").write(repomd)
u = f"https://{repo}"
open(f"{w}/fetched/run.fetched/fetched.txt", "w").write(
    f"{u}/repodata/repomd.xml sha256:{sha(repomd)} {len(repomd)}\n{u}/Packages/a-1.rpm sha256:{sha(used_rpm)} 9\n"
    f"{u}/Packages/c-1.rpm sha256:{sha(old_rpm)} 9\n"
    f"{u}/Packages/d-1.rpm sha256:{sha(b'md5 only')} 8\n")
put(f"{repo}/Packages/d-1.rpm", b"md5 only")
os.makedirs(f"{w}/runs")
run = lambda result, digest: {"result": result, "box": "el/x", "started": "1",
                              "hops": [{"sources": {f"{u}/repodata/repomd.xml": f"sha256:{digest}"}}]}
json.dump(run("pass", sha(repomd)), open(f"{w}/runs/pass.json", "w"))
json.dump(run("fail", sha(b"<repomd>newer</repomd>")), open(f"{w}/runs/fail.json", "w"))

ingest = lambda *extra: subprocess.run([sys.executable, "-B", f"{HERE}/archive.py", "ingest", f"{w}/store", "--offline",
                                        "--tree", f"{w}/tree", "--runs", f"{w}/runs", *extra],
                                       capture_output=True, text=True)
p = ingest()
check(p.returncode == 1 and f"missing {u}/repodata/repomd.xml" in p.stdout and not os.path.exists(f"{w}/store/blob"),
      "a pin no source has at its digest is reported missing, and the newer bytes at its url are refused")
p = ingest("--fetched", f"{w}/fetched")
idx = json.load(open(f"{w}/store/index/latest.json"))["files"]
check(p.returncode == 0 and "0 referenced files not found" in p.stdout, "with the download list, the whole chain is found")
check(sorted(idx) == sorted([f"{u}/repodata/repomd.xml", f"{u}/repodata/{sha(primary)}-primary.xml.gz",
                             f"{u}/Packages/a-1.rpm", f"{u}/Packages/c-1.rpm"]),
      "stored: the pin, its package index and the packages the walk used; not filelists, unused packages, or a "
      "package its index names only by md5 (the client ignores those too)")
check(list(idx[f"{u}/repodata/repomd.xml"]) == [f"sha256:{sha(repomd)}"] and
      idx[f"{u}/Packages/a-1.rpm"][f"sha256:{sha(used_rpm)}"]["runs"] == ["el/x@1"],
      "the index maps each url to its digest and the passing runs that reference it")
# a later passing run pins a newer repomd at the same url, and no source has it: still missing, despite the old one
json.dump({**run("pass", sha(b"<repomd>vanished</repomd>")), "started": "2"}, open(f"{w}/runs/later.json", "w"))
p = ingest("--fetched", f"{w}/fetched")
check(f"missing {u}/repodata/repomd.xml" in p.stdout,
      "a newer pin no source has is missing, though an older digest at its url is stored")
os.remove(f"{w}/runs/later.json")
# a second passing run pins another repomd listing the same primary: the package gets both runs
repomd2 = repomd.replace(b"</repomd>", b"<!-- 2 --></repomd>")
open(f"{w}/fetched/run.fetched/blobs/{sha(repomd2)}", "wb").write(repomd2)
json.dump({**run("pass", sha(repomd2)), "started": "3"}, open(f"{w}/runs/second.json", "w"))
p = ingest("--fetched", f"{w}/fetched")
idx = json.load(open(f"{w}/store/index/latest.json"))["files"]
check(p.returncode == 0 and idx[f"{u}/Packages/a-1.rpm"][f"sha256:{sha(used_rpm)}"]["runs"] == ["el/x@1", "el/x@3"],
      "a file two pins' chains share is referenced by both runs")
os.remove(f"{w}/runs/second.json")
p = ingest("--fetch", f"{w}/fetched")
check(p.returncode == 2 and "isn't an option" in p.stdout, "a mistyped option stops the ingest")
import fcntl  # noqa: E402
with open(f"{w}/store/.lock", "w") as held:
    fcntl.flock(held, fcntl.LOCK_EX)
    p = ingest("--fetched", f"{w}/fetched")
check(p.returncode == 2 and "another ingest" in p.stdout, "a second ingest on the same store refuses to start")
check(open(f"{w}/store/alias/sha1/{sha1(old_rpm)}").read() == f"sha256:{sha(old_rpm)}\n",
      "a package its index names by SHA-1 gets an alias naming its blob")
os.remove(f"{w}/tree/{repo}/Packages/c-1.rpm")
p = ingest("--fetched", f"{w}/fetched")
check("0 referenced files not found" in p.stdout, "a later ingest finds a SHA-1-named package through its alias")
shutil.rmtree(f"{w}/store/alias")
os.remove(f"{w}/store/blob/sha256/{sha(old_rpm)[:2]}/{sha(old_rpm)}")
open(f"{w}/fetched/run.fetched/blobs/{sha(old_rpm)}", "wb").write(old_rpm)
p = ingest("--fetched", f"{w}/fetched")
check("0 referenced files not found" in p.stdout,
      "a SHA-1-named file gone from its url is found in a download list's blobs, under the sha256 it logged")
blob = f"{w}/store/blob/sha256/{sha(used_rpm)[:2]}/{sha(used_rpm)}"
check(open(blob, "rb").read() == used_rpm, "a blob is the upstream bytes, under blob/sha256/<2 hex>/<64 hex>")
v = subprocess.run([sys.executable, "-B", f"{HERE}/archive.py", "verify", f"{w}/store"], capture_output=True, text=True)
check(v.returncode == 0, "verify passes a good store")
open(blob, "wb").write(b"rot")
v = subprocess.run([sys.executable, "-B", f"{HERE}/archive.py", "verify", f"{w}/store"], capture_output=True, text=True)
check(v.returncode == 1 and f"WRONG   {u}/Packages/a-1.rpm" in v.stdout, "verify names a blob that rotted")
ingest("--fetched", f"{w}/fetched")
check(open(blob, "rb").read() == used_rpm, "an ingest that finds good bytes elsewhere replaces a rotted blob")
# wheezy: a plain index pinned uncompressed, served only as .gz; and a primary in a compression nothing reads
plain = b"Package: x\nFilename: pool/x.deb\nSHA256: " + sha(b"x").encode() + b"\n"
wheezy = "archive.example.org/debian/dists/wheezy/main/binary-amd64/Packages"
put(f"{wheezy}.gz", gzip.compress(plain))
zck = b"(zchunk bytes)"
repomd3 = (f'<repomd><data type="primary"><checksum type="sha256">{sha(zck)}</checksum>'
           f'<location href="repodata/primary.xml.zck"/></data></repomd>').encode()
put("new.example.org/10/repodata/repomd.xml", repomd3)
put("new.example.org/10/repodata/primary.xml.zck", zck)
os.makedirs(f"{w}/runs2")
json.dump({"result": "pass", "box": "x", "started": "4", "hops": [{"sources": {
    f"http://{wheezy}": f"sha256:{sha(plain)}",
    "https://new.example.org/10/repodata/repomd.xml": f"sha256:{sha(repomd3)}"}}]}, open(f"{w}/runs2/r.json", "w"))
p = subprocess.run([sys.executable, "-B", f"{HERE}/archive.py", "ingest", f"{w}/store2", "--offline", "--tree",
                    f"{w}/tree", "--runs", f"{w}/runs2"], capture_output=True, text=True)
check(open(f"{w}/store2/blob/sha256/{sha(plain)[:2]}/{sha(plain)}", "rb").read() == plain,
      "a plain index pinned uncompressed is found through the .gz a tree holds")
check(p.returncode == 1 and "unreadable https://new.example.org/10/repodata/primary.xml.zck" in p.stdout,
      "an index in a compression nothing reads is reported, not silently skipped")
# sources: an EL repo's .src.rpm from its source repo (SOURCE_REPOS), a Debian package's .dsc and files from the
# Sources index its pinned Release lists; checked against that metadata, with a gap reported where there's none
el = "vault.centos.org/7.9.2009/os"
bin_rpm, src_rpm, lone_rpm = b"\xed\xab\xee\xdb bin", b"\xed\xab\xee\xdb src", b"\xed\xab\xee\xdb lone"
pkg = lambda n, b, src: (f'<package type="rpm"><checksum type="sha256" pkgid="YES">{sha(b)}</checksum>'
                         f'<location href="Packages/{n}"/><format><rpm:sourcerpm>{src}</rpm:sourcerpm></format></package>')
prim = gzip.compress(f"<metadata>{pkg('t-1.rpm', bin_rpm, 't-1.src.rpm')}{pkg('l-1.rpm', lone_rpm, 'l-1.src.rpm')}"
                     f"</metadata>".encode())
md = lambda p: (f'<repomd><data type="primary"><checksum type="sha256">{sha(p)}</checksum>'
                f'<location href="repodata/{sha(p)}-primary.xml.gz"/></data></repomd>').encode()
put(f"{el}/x86_64/repodata/repomd.xml", md(prim))
put(f"{el}/x86_64/repodata/{sha(prim)}-primary.xml.gz", prim)
put(f"{el}/x86_64/Packages/t-1.rpm", bin_rpm)
put(f"{el}/x86_64/Packages/l-1.rpm", lone_rpm)
sprim = gzip.compress(f"<metadata>{pkg('t-1.src.rpm', src_rpm, 'x')}</metadata>".encode())
put(f"{el}/Source/repodata/repomd.xml", md(sprim))
put(f"{el}/Source/repodata/{sha(sprim)}-primary.xml.gz", sprim)
put(f"{el}/Source/Packages/t-1.src.rpm", src_rpm)
deb_bin, dsc, tar = b"!<arch> deb", b"Format: 3.0 (quilt)\n", b"tarball"
packages = (f"Package: x\nSource: xsrc (1.0-1)\nVersion: 1.0-1+b1\nFilename: pool/main/x/xsrc/x_1.0-1+b1_amd64.deb\n"
            f"SHA256: {sha(deb_bin)}\n").encode()
sources = (f"Package: xsrc\nVersion: 1.0-1\nDirectory: pool/main/x/xsrc\nChecksums-Sha256:\n"
           f" {sha(dsc)} {len(dsc)} xsrc_1.0-1.dsc\n {sha(tar)} {len(tar)} xsrc_1.0.orig.tar.xz\n").encode()
pz, sz = lzma.compress(packages), lzma.compress(sources)
release = (f"Suite: test\nSHA256:\n {sha(pz)} {len(pz)} main/binary-amd64/Packages.xz\n"
           f" {sha(sz)} {len(sz)} main/source/Sources.xz\n").encode()
deb = "deb.example.org/debian"
put(f"{deb}/dists/test/Release", release)
put(f"{deb}/dists/test/main/binary-amd64/Packages.xz", pz)
put(f"{deb}/dists/test/main/source/Sources.xz", sz)
put(f"{deb}/pool/main/x/xsrc/x_1.0-1+b1_amd64.deb", deb_bin)
put(f"{deb}/pool/main/x/xsrc/xsrc_1.0-1.dsc", dsc)
put(f"{deb}/pool/main/x/xsrc/xsrc_1.0.orig.tar.xz", tar)
os.makedirs(f"{w}/runs3")
os.makedirs(f"{w}/fetched3/r.fetched")
open(f"{w}/fetched3/r.fetched/fetched.txt", "w").write(
    f"https://{el}/x86_64/Packages/t-1.rpm sha256:{sha(bin_rpm)} 1\n"
    f"https://{el}/x86_64/Packages/l-1.rpm sha256:{sha(lone_rpm)} 1\n"
    f"https://{deb}/pool/main/x/xsrc/x_1.0-1+b1_amd64.deb sha256:{sha(deb_bin)} 1\n")
json.dump({"result": "pass", "box": "s", "started": "5", "hops": [{"sources": {
    f"https://{el}/x86_64/repodata/repomd.xml": f"sha256:{sha(md(prim))}",
    f"https://{deb}/dists/test/Release": f"sha256:{sha(release)}"}}]}, open(f"{w}/runs3/r.json", "w"))
p = subprocess.run([sys.executable, "-B", f"{HERE}/archive.py", "ingest", f"{w}/store3", "--offline", "--tree",
                    f"{w}/tree", "--runs", f"{w}/runs3", "--fetched", f"{w}/fetched3"], capture_output=True, text=True)
idx = json.load(open(f"{w}/store3/index/latest.json"))["files"]
entry = lambda u, b: idx[u][f"sha256:{sha(b)}"]
check(entry(f"https://{el}/x86_64/Packages/t-1.rpm", bin_rpm)["source"] ==
      {"url": f"https://{el}/Source/Packages/t-1.src.rpm", "digest": f"sha256:{sha(src_rpm)}"} and
      entry(f"https://{el}/Source/Packages/t-1.src.rpm", src_rpm)["runs"] == ["s@5"],
      "an rpm's .src.rpm is stored from its source repo, and named on the rpm's index entry")
check(entry(f"https://{deb}/pool/main/x/xsrc/x_1.0-1+b1_amd64.deb", deb_bin)["source"]["url"] ==
      f"https://{deb}/pool/main/x/xsrc/xsrc_1.0-1.dsc" and
      entry(f"https://{deb}/pool/main/x/xsrc/xsrc_1.0.orig.tar.xz", tar)["runs"] == ["s@5"],
      "a deb's .dsc and the files it lists are stored from the Sources index its pinned Release lists")
check(p.returncode == 0 and f"no source https://{el}/x86_64/Packages/l-1.rpm" in p.stdout and
      "missing" in entry(f"https://{el}/x86_64/Packages/l-1.rpm", lone_rpm)["source"],
      "a package whose source isn't there is a reported gap, recorded in the index, not a failure")
put(f"{deb}/pool/main/x/xsrc/xsrc_1.0.orig.tar.xz", b"evil")
p = subprocess.run([sys.executable, "-B", f"{HERE}/archive.py", "ingest", f"{w}/store4", "--offline", "--tree",
                    f"{w}/tree", "--runs", f"{w}/runs3", "--fetched", f"{w}/fetched3"], capture_output=True, text=True)
check(f"no source https://{deb}/pool/main/x/xsrc/x_1.0-1+b1_amd64.deb" in p.stdout and
      not os.path.exists(f"{w}/store4/blob/sha256/{sha(b'evil')[:2]}/{sha(b'evil')}"),
      "a source file that doesn't hash to what Sources lists is refused")
import shutil as sh  # noqa: E402
if sh.which("zstd"):  # EPEL 10 serves its primary only as .zst; CI's runners have zstd, not every laptop does
    sys.path.insert(0, HERE)
    import archive  # noqa: E402
    z = subprocess.run(["zstd", "-c"], input=b"<metadata/>", capture_output=True).stdout
    check(archive.decompress("x/primary.xml.zst", z) == b"<metadata/>", "a .zst index is read")
subprocess.run(["rm", "-rf", w])
sys.exit(0 if all(results) else 1)
