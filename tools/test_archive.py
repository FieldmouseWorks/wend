"""The archive's store by behaviour (docs/archive.md): offline, on a made-up repo. Only bytes that hash right
against a passing run's pins go in, only what a download list names comes along, and verify catches a bad blob.
python3 tools/test_archive.py"""
import bz2
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
os.environ["WEND_ARCHIVE_SIGNERS"] = ""  # these made-up stores aren't signed by the archive's key
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
                             f"{u}/repodata/{sha(filelists)}-filelists.xml.gz",
                             f"{u}/Packages/a-1.rpm", f"{u}/Packages/c-1.rpm"]),
      "stored: the pin, its package index, its filelists (a live walk can ask for them) and the packages the walk "
      "used; not unused packages, or a package its index names only by md5 (the client ignores those too)")
os.rename(f"{w}/tree/{repo}/repodata/{sha(filelists)}-filelists.xml.gz", f"{w}/fl")
shutil.rmtree(f"{w}/store")
p = ingest("--fetched", f"{w}/fetched")
check(p.returncode == 0 and f"walk gap {u}/repodata/{sha(filelists)}-filelists.xml.gz" in p.stdout,
      "filelists no source has are a walk gap, not a failure")
os.rename(f"{w}/fl", f"{w}/tree/{repo}/repodata/{sha(filelists)}-filelists.xml.gz")
p = ingest("--fetched", f"{w}/fetched")
idx = json.load(open(f"{w}/store/index/latest.json"))["files"]
check(list(idx[f"{u}/repodata/repomd.xml"]) == [f"sha256:{sha(repomd)}"] and
      idx[f"{u}/Packages/a-1.rpm"][f"sha256:{sha(used_rpm)}"]["runs"] == ["el/x@1"],
      "the index maps each url to its digest and the passing runs that reference it")
# a later passing run pins a newer repomd at the same url, and no source has it: still missing, despite the old one
json.dump({**run("pass", sha(b"<repomd>vanished</repomd>")), "started": "2"}, open(f"{w}/runs/later.json", "w"))
p = ingest("--fetched", f"{w}/fetched")
check(f"missing {u}/repodata/repomd.xml" in p.stdout,
      "a newer pin no source has is missing, though an older digest at its url is stored")
open(f"{w}/lost.txt", "w").write(f"# gone\n{u}/repodata/repomd.xml sha256:{sha(b'<repomd>vanished</repomd>')}  # replaced\n")
p = ingest("--fetched", f"{w}/fetched", "--lost", f"{w}/lost.txt")
check(p.returncode == 0 and "0 referenced files not found" in p.stdout and
      f"lost {u}/repodata/repomd.xml sha256:{sha(b'<repomd>vanished</repomd>')}" in p.stdout,
      "a pin lost.txt lists as lost is reported, and doesn't fail the ingest")
open(f"{w}/lost.txt", "w").write(f"{u}/repodata/repomd.xml sha256:{sha(b'another')}\n")
p = ingest("--fetched", f"{w}/fetched", "--lost", f"{w}/lost.txt")
check(p.returncode == 1 and f"missing {u}/repodata/repomd.xml" in p.stdout,
      "lost.txt excuses only the digest it names, not every pin at its url")
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
check(p.returncode == 2 and "is running on this store" in p.stdout, "a second ingest on the same store refuses to start")
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
tr = b"Package: x\nDescription-md5: 0\nDescription-en: x\n"
buster = "archive.example.org/debian/dists/buster/main/i18n/Translation-en"
put(f"{buster}.bz2", bz2.compress(tr))
zck = b"(zchunk bytes)"
repomd3 = (f'<repomd><data type="primary"><checksum type="sha256">{sha(zck)}</checksum>'
           f'<location href="repodata/primary.xml.zck"/></data></repomd>').encode()
put("new.example.org/10/repodata/repomd.xml", repomd3)
put("new.example.org/10/repodata/primary.xml.zck", zck)
os.makedirs(f"{w}/runs2")
json.dump({"result": "pass", "box": "x", "started": "4", "hops": [{"sources": {
    f"http://{wheezy}": f"sha256:{sha(plain)}",
    f"http://{buster}": f"sha256:{sha(tr)}",
    "https://new.example.org/10/repodata/repomd.xml": f"sha256:{sha(repomd3)}"}}]}, open(f"{w}/runs2/r.json", "w"))
p = subprocess.run([sys.executable, "-B", f"{HERE}/archive.py", "ingest", f"{w}/store2", "--offline", "--tree",
                    f"{w}/tree", "--runs", f"{w}/runs2"], capture_output=True, text=True)
check(open(f"{w}/store2/blob/sha256/{sha(plain)[:2]}/{sha(plain)}", "rb").read() == plain,
      "a plain index pinned uncompressed is found through the .gz a tree holds")
check(os.path.isfile(f"{w}/store2/blob/sha256/{sha(tr)[:2]}/{sha(tr)}"),
      "a plain index served only as .bz2 (buster's Translation-en) is found through it")
check(p.returncode == 1 and "unreadable https://new.example.org/10/repodata/primary.xml.zck" in p.stdout,
      "an index in a compression nothing reads is reported, not silently skipped")
# sources: an EL repo's .src.rpm from its source repo (SOURCE_REPOS), a Debian package's .dsc and files from the
# Sources index its pinned Release lists; checked against that metadata, with a gap reported where there's none
el = "vault.centos.org/7.9.2009/os"
bin_rpm, src_rpm, lone_rpm = b"\xed\xab\xee\xdb bin", b"\xed\xab\xee\xdb src", b"\xed\xab\xee\xdb lone"
old_rpm, old_src = b"\xed\xab\xee\xdb old", b"\xed\xab\xee\xdb old src"  # its source is only under an older minor
pkg = lambda n, b, src: (f'<package type="rpm"><checksum type="sha256" pkgid="YES">{sha(b)}</checksum>'
                         f'<location href="Packages/{n}"/><format><rpm:sourcerpm>{src}</rpm:sourcerpm></format></package>')
prim = gzip.compress(f"<metadata>{pkg('t-1.rpm', bin_rpm, 't-1.src.rpm')}{pkg('l-1.rpm', lone_rpm, 'l-1.src.rpm')}"
                     f"{pkg('o-1.rpm', old_rpm, 'o-1.src.rpm')}</metadata>".encode())
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
put(f"{el}/x86_64/Packages/o-1.rpm", old_rpm)
el76 = "vault.centos.org/7.6.1810/os"
oprim = gzip.compress(f"<metadata>{pkg('o-1.src.rpm', old_src, 'x')}</metadata>".encode())
put(f"{el76}/Source/repodata/repomd.xml", md(oprim))
put(f"{el76}/Source/repodata/{sha(oprim)}-primary.xml.gz", oprim)
put(f"{el76}/Source/Packages/o-1.src.rpm", old_src)
deb_bin, dsc, tar = b"!<arch> deb", b"Format: 3.0 (quilt)\n", b"tarball"
zbin, zdsc = b"!<arch> z", b"Format: z\n"
packages = (f"Package: x\nSource: xsrc (1.0-1)\nVersion: 1.0-1+b1\nFilename: pool/main/x/xsrc/x_1.0-1+b1_amd64.deb\n"
            f"SHA256: {sha(deb_bin)}\n\n"  # z: a binNMU whose stanza names no source version
            f"Package: z\nVersion: 2-1+b3\nFilename: pool/main/z/z/z_2-1+b3_amd64.deb\nSHA256: {sha(zbin)}\n").encode()
sources = (f"Package: xsrc\nVersion: 1.0-1\nDirectory: pool/main/x/xsrc\nChecksums-Sha256:\n"
           f" {sha(dsc)} {len(dsc)} xsrc_1.0-1.dsc\n {sha(tar)} {len(tar)} xsrc_1.0.orig.tar.xz\n\n"
           f"Package: z\nVersion: 2-1\nDirectory: pool/main/z/z\nChecksums-Sha256:\n {sha(zdsc)} {len(zdsc)} z_2-1.dsc\n").encode()
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
put(f"{deb}/pool/main/z/z/z_2-1+b3_amd64.deb", zbin)
put(f"{deb}/pool/main/z/z/z_2-1.dsc", zdsc)
os.makedirs(f"{w}/runs3")
os.makedirs(f"{w}/fetched3/r.fetched")
open(f"{w}/fetched3/r.fetched/fetched.txt", "w").write(
    f"https://{el}/x86_64/Packages/t-1.rpm sha256:{sha(bin_rpm)} 1\n"
    f"https://{el}/x86_64/Packages/l-1.rpm sha256:{sha(lone_rpm)} 1\n"
    f"https://{el}/x86_64/Packages/o-1.rpm sha256:{sha(old_rpm)} 1\n"
    f"https://{deb}/pool/main/x/xsrc/x_1.0-1+b1_amd64.deb sha256:{sha(deb_bin)} 1\n"
    f"https://{deb}/pool/main/z/z/z_2-1+b3_amd64.deb sha256:{sha(zbin)} 1\n")
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
check(entry(f"https://{el}/x86_64/Packages/o-1.rpm", old_rpm)["source"] ==
      {"url": f"https://{el76}/Source/Packages/o-1.src.rpm", "digest": f"sha256:{sha(old_src)}"},
      "a CentOS 7 rpm whose source only the minor release that built it lists finds it there")
check(entry(f"https://{deb}/pool/main/x/xsrc/x_1.0-1+b1_amd64.deb", deb_bin)["source"]["url"] ==
      f"https://{deb}/pool/main/x/xsrc/xsrc_1.0-1.dsc" and
      entry(f"https://{deb}/pool/main/x/xsrc/xsrc_1.0.orig.tar.xz", tar)["runs"] == ["s@5"],
      "a deb's .dsc and the files it lists are stored from the Sources index its pinned Release lists")
check(entry(f"https://{deb}/pool/main/z/z/z_2-1+b3_amd64.deb", zbin)["source"].get("url") ==
      f"https://{deb}/pool/main/z/z/z_2-1.dsc",
      "a binNMU whose stanza names no source version finds its source without the +bN")
check(p.returncode == 0 and f"no source https://{el}/x86_64/Packages/l-1.rpm" in p.stdout and
      "missing" in entry(f"https://{el}/x86_64/Packages/l-1.rpm", lone_rpm)["source"],
      "a package whose source isn't there is a reported gap, recorded in the index, not a failure")
put(f"{deb}/pool/main/x/xsrc/xsrc_1.0.orig.tar.xz", b"evil")
p = subprocess.run([sys.executable, "-B", f"{HERE}/archive.py", "ingest", f"{w}/store4", "--offline", "--tree",
                    f"{w}/tree", "--runs", f"{w}/runs3", "--fetched", f"{w}/fetched3"], capture_output=True, text=True)
check(f"no source https://{deb}/pool/main/x/xsrc/x_1.0-1+b1_amd64.deb" in p.stdout and
      not os.path.exists(f"{w}/store4/blob/sha256/{sha(b'evil')[:2]}/{sha(b'evil')}"),
      "a source file that doesn't hash to what Sources lists is refused")
# wheezy: a run pins only a package index, no Release. The suite's live Release is read for its Sources only when it
# lists that pinned index at the pinned digest; and a lenny-era source listed only by md5 is stored by it
md5 = lambda b: hashlib.md5(b).hexdigest()
obin, odsc, ebin = b"!<arch> o", b"Format: 1.0\n", b"!<arch> e"
opk = f"Package: o\nVersion: 1\nFilename: pool/main/o/o/o_1_amd64.deb\nSHA256: {sha(obin)}\n".encode()
epk = f"Package: e\nVersion: 1\nFilename: pool/main/e/e/e_1_amd64.deb\nSHA256: {sha(ebin)}\n".encode()
osrc = gzip.compress(f"Package: o\nVersion: 1\nDirectory: pool/main/o/o\nFiles:\n {md5(odsc)} {len(odsc)} o_1.dsc\n".encode())
put(f"{deb}/dists/old/main/binary-amd64/Packages", opk)
put(f"{deb}/dists/old/main/source/Sources.gz", osrc)
put(f"{deb}/dists/old/Release", (f"SHA256:\n {sha(opk)} {len(opk)} main/binary-amd64/Packages\n"
                                 f" {sha(osrc)} {len(osrc)} main/source/Sources.gz\n").encode())
put(f"{deb}/pool/main/o/o/o_1_amd64.deb", obin)
put(f"{deb}/pool/main/o/o/o_1.dsc", odsc)
put(f"{deb}/dists/else/main/binary-amd64/Packages", epk)
put(f"{deb}/dists/else/main/source/Sources.gz", osrc)
put(f"{deb}/dists/else/Release", (f"SHA256:\n {sha(b'newer')} 5 main/binary-amd64/Packages\n"
                                  f" {sha(osrc)} {len(osrc)} main/source/Sources.gz\n").encode())
put(f"{deb}/pool/main/e/e/e_1_amd64.deb", ebin)
os.makedirs(f"{w}/runsw")
os.makedirs(f"{w}/fetchedw/r.fetched")
open(f"{w}/fetchedw/r.fetched/fetched.txt", "w").write(f"https://{deb}/pool/main/o/o/o_1_amd64.deb sha256:{sha(obin)} 1\n"
                                                       f"https://{deb}/pool/main/e/e/e_1_amd64.deb sha256:{sha(ebin)} 1\n")
json.dump({"result": "pass", "box": "w", "started": "6", "hops": [{"sources": {
    f"https://{deb}/dists/old/main/binary-amd64/Packages": f"sha256:{sha(opk)}",
    f"https://{deb}/dists/else/main/binary-amd64/Packages": f"sha256:{sha(epk)}"}}]}, open(f"{w}/runsw/r.json", "w"))
p = subprocess.run([sys.executable, "-B", f"{HERE}/archive.py", "ingest", f"{w}/storew", "--offline", "--tree",
                    f"{w}/tree", "--runs", f"{w}/runsw", "--fetched", f"{w}/fetchedw"], capture_output=True, text=True)
idx = json.load(open(f"{w}/storew/index/latest.json"))["files"]
check(idx[f"https://{deb}/pool/main/o/o/o_1_amd64.deb"][f"sha256:{sha(obin)}"]["source"] ==
      {"url": f"https://{deb}/pool/main/o/o/o_1.dsc", "digest": f"sha256:{sha(odsc)}"} and
      [(e["live"], e["runs"]) for e in idx[f"https://{deb}/dists/old/Release"].values()] == [(True, ["w@6"])],
      "a suite with only its package index pinned gets its sources through the live Release that lists that index")
check(f"no source https://{deb}/pool/main/e/e/e_1_amd64.deb (its suite's Release lists no Sources)" in p.stdout and
      f"https://{deb}/dists/else/Release" not in idx,
      "a live Release that doesn't list the pinned index at its digest is not used, nor stored")
# a point release: two runs pin the same Release url at two digests, each listing its own Packages and Sources; a
# universe binary built from a main source; a package a run pins directly
def put5(rel, data):
    os.makedirs(os.path.dirname(f"{w}/tree5/{rel}"), exist_ok=True)
    open(f"{w}/tree5/{rel}", "wb").write(data)


m5 = "deb.example.org/ubuntu"
rel5, fetched5 = [], []
for v in ("1", "2"):
    debv, dscv = f"!<arch> x{v}".encode(), f"Format: {v}\n".encode()
    pk = (f"Package: x\nVersion: {v}\nFilename: pool/main/x/x/x_{v}_amd64.deb\nSHA256: {sha(debv)}\n").encode()
    srcs = (f"Package: x\nVersion: {v}\nDirectory: pool/main/x/x\nChecksums-Sha256:\n"
            f" {sha(dscv)} {len(dscv)} x_{v}.dsc\n").encode()
    if v == "2":  # universe's y is built from main's x
        yv = b"!<arch> y"
        upk = f"Package: y\nSource: x\nVersion: 2\nFilename: pool/universe/x/x/y_2_amd64.deb\nSHA256: {sha(yv)}\n"
        uz = gzip.compress(upk.encode())
        put5(f"{m5}/pool/universe/x/x/y_2_amd64.deb", yv)
        fetched5.append(f"https://{m5}/pool/universe/x/x/y_2_amd64.deb sha256:{sha(yv)} 1")
    pz5, px5, sz5 = gzip.compress(pk), lzma.compress(pk), gzip.compress(srcs)
    rel = (f"SHA256:\n {sha(pz5)} {len(pz5)} main/binary-amd64/Packages.gz\n {sha(px5)} {len(px5)} "
           f"main/binary-amd64/Packages.xz\n {sha(sz5)} {len(sz5)} main/source/Sources.gz\n" +
           (f" {sha(uz)} {len(uz)} universe/binary-amd64/Packages.gz\n" if v == "2" else "")).encode()
    rel5.append(rel)
    os.makedirs(f"{w}/b5/{v}/blobs", exist_ok=True)  # each Release's metadata, as each run's capture kept it
    for b in (rel, pz5, px5, sz5) + ((uz,) if v == "2" else ()):
        open(f"{w}/b5/{v}/blobs/{sha(b)}", "wb").write(b)
    put5(f"{m5}/pool/main/x/x/x_{v}_amd64.deb", debv)
    put5(f"{m5}/pool/main/x/x/x_{v}.dsc", dscv)
    fetched5.append(f"https://{m5}/pool/main/x/x/x_{v}_amd64.deb sha256:{sha(debv)} 1")
lone = b"\xed\xab\xee\xdb pinned"
put5("pkgs.example.org/p-1.rpm", lone)
open(f"{w}/b5/1/fetched.txt", "w").write("\n".join(fetched5) + "\n")
os.makedirs(f"{w}/runs5")
for i, rel in enumerate(rel5):
    json.dump({"result": "pass", "box": "u", "started": str(i), "hops": [{"sources": {
        f"https://{m5}/dists/s/Release": f"sha256:{sha(rel)}",
        "https://pkgs.example.org/p-1.rpm": f"sha256:{sha(lone)}"}}]}, open(f"{w}/runs5/{i}.json", "w"))
p = subprocess.run([sys.executable, "-B", f"{HERE}/archive.py", "ingest", f"{w}/store5", "--offline", "--tree",
                    f"{w}/tree5", "--runs", f"{w}/runs5", "--fetched", f"{w}/b5"], capture_output=True, text=True)
idx = json.load(open(f"{w}/store5/index/latest.json"))["files"]
src = lambda u: [e.get("source", {}) for e in idx.get(u, {}).values()]
check([x.get("url") for x in src(f"https://{m5}/pool/main/x/x/x_1_amd64.deb")] == [f"https://{m5}/pool/main/x/x/x_1.dsc"]
      and [x.get("url") for x in src(f"https://{m5}/pool/main/x/x/x_2_amd64.deb")] ==
      [f"https://{m5}/pool/main/x/x/x_2.dsc"],
      "two digests of one Release each get their packages and their sources stored (a point release)")
check(f"https://{m5}/dists/s/main/binary-amd64/Packages.gz" in idx and
      f"https://{m5}/dists/s/main/binary-amd64/Packages.xz" not in idx,
      "of a package index's forms, the .gz the client reads is kept")
check([x.get("url") for x in src(f"https://{m5}/pool/universe/x/x/y_2_amd64.deb")] ==
      [f"https://{m5}/pool/main/x/x/x_2.dsc"], "a binary built from another component's source finds it there")
check("missing" in src("https://pkgs.example.org/p-1.rpm")[0] and "no source https://pkgs.example.org/p-1.rpm" in p.stdout,
      "a package a run pins directly is a listed gap, not silently sourceless")
check(any(e.get("live") for e in json.load(open(f"{w}/store3/index/latest.json"))["files"][
    f"https://{el}/Source/repodata/repomd.xml"].values()), "a source repo's repomd.xml, pinned by no run, is marked live")
# a source repo whose only primary is in a form nothing reads: its packages are gaps, and the ingest still passes
shutil.rmtree(f"{w}/tree/{el}/Source/repodata")
zck = b"(zchunk primary)"
put(f"{el}/Source/repodata/repomd.xml", (f'<repomd><data type="primary"><checksum type="sha256">{sha(zck)}</checksum>'
                                         f'<location href="repodata/primary.xml.zck"/></data></repomd>').encode())
put(f"{el}/Source/repodata/primary.xml.zck", zck)
p = subprocess.run([sys.executable, "-B", f"{HERE}/archive.py", "ingest", f"{w}/store6", "--offline", "--tree",
                    f"{w}/tree", "--runs", f"{w}/runs3", "--fetched", f"{w}/fetched3"], capture_output=True, text=True)
check(p.returncode == 0 and "unreadable" not in p.stdout and
      f"no source https://{el}/x86_64/Packages/t-1.rpm" in p.stdout,
      "unreadable source metadata leaves gaps, never a failed ingest")
import struct  # noqa: E402
sys.path.insert(0, HERE)
import archive  # noqa: E402


def rpm(tags, sig_store=b"\0" * 5, kind=0):
    """A made-up .rpm (kind 0 binary, 1 source): lead, a signature header (whose odd size needs the 8-byte padding),
    the main header."""
    def header(entries, store):
        return (b"\x8e\xad\xe8\x01\0\0\0\0" + struct.pack(">II", len(entries), len(store)) +
                b"".join(struct.pack(">IIII", *e) for e in entries) + store)
    sig = header([(1000, 4, 0, 1)], sig_store)
    store, entries = b"", []
    for tag, v in tags:
        entries.append((tag, 6, len(store), 1))
        store += v + b"\0"
    lead = b"\xed\xab\xee\xdb\3\0" + struct.pack(">H", kind) + b"\0" * 88
    return lead + sig + b"\0" * (-len(sig) % 8) + header(entries, store) + b"payload"


check(archive.rpm_sourcerpm(rpm([(1000, b"p7zip"), (1044, b"p7zip-9.20.1-2.el5.src.rpm")])) ==
      "p7zip-9.20.1-2.el5.src.rpm", "a directly pinned .rpm's own header names its source (EPEL 5's p7zip)")
check(archive.rpm_sourcerpm(rpm([(1044, b"../../etc/passwd")])) is None and
      archive.rpm_sourcerpm(rpm([(1000, b"x")])) is None and archive.rpm_sourcerpm(b"\xed\xab\xee\xdb short") is None,
      "a header with no source, a source name that isn't one, or a truncated rpm names nothing")
# a source its repos lost: Red Hat's SRPMS for a CentOS 7 rebuild, Fedora's koji for what Fedora built, each taken
# only when it is a source rpm named as the binary's header names it; and an earlier ingest's gap is tried again
tags = lambda n, v, r, host, src=None: ([(1000, n), (1001, v), (1002, r), (1007, host)] +
                                       ([(1044, src)] if src else []))
cos, fed = b"x86-01.bsys.centos.org", b"buildvm-x86-01.iad2.fedoraproject.org"
bins = {"g": rpm(tags(b"g", b"1", b"3.el7_9", cos, b"g-1-3.el7_9.src.rpm")),
        "f": rpm(tags(b"f", b"7", b"11", fed, b"f-7-11.src.rpm")),
        "h": rpm(tags(b"h", b"1", b"1.el7", cos, b"h-1-1.el7.src.rpm")),
        "d": rpm(tags(b"d", b"1", b"1.el7", cos, b"d-1-1.el7.src.rpm")),
        "s": rpm(tags(b"s", b"2", b"1.el7", cos, b"s-2-1.el7.src.rpm"))}
srcs = {"g": rpm(tags(b"g", b"1", b"3.el7_9", cos), kind=1), "f": rpm(tags(b"f", b"7", b"11", fed), kind=1),
        "d": rpm(tags(b"evil", b"1", b"1.el7", cos), kind=1), "s": rpm(tags(b"s", b"2", b"1.el7", cos), kind=1)}
rh = "ftp.redhat.com/redhat/linux/enterprise/7Server/en/os/SRPMS"
put(f"{rh}/g-1-3.el7_9.src.rpm", srcs["g"])
put(f"{rh}/d-1-1.el7.src.rpm", srcs["d"])  # names another package
put("kojipkgs.fedoraproject.org/packages/f/7/11/src/f-7-11.src.rpm", srcs["f"])
put("kojipkgs.fedoraproject.org/packages/h/1/1.el7/src/h-1-1.el7.src.rpm", srcs["f"])  # CentOS built h: not koji's
u7 = "vault.centos.org/7.9.2009/updates"
prim7 = gzip.compress(("<metadata>" + "".join(pkg(f"{n}.rpm", b, archive.rpm_sourcerpm(b)) for n, b in bins.items()) +
                       "</metadata>").encode())
put(f"{u7}/x86_64/repodata/repomd.xml", md(prim7))
put(f"{u7}/x86_64/repodata/{sha(prim7)}-primary.xml.gz", prim7)
os.makedirs(f"{w}/runs7")
os.makedirs(f"{w}/fetched7/r")
for n, b in bins.items():
    put(f"{u7}/x86_64/Packages/{n}.rpm", b)
open(f"{w}/fetched7/r/fetched.txt", "w").write("".join(f"https://{u7}/x86_64/Packages/{n}.rpm sha256:{sha(b)} 1\n"
                                                        for n, b in bins.items()))
json.dump({"result": "pass", "box": "c", "started": "7", "hops": [{"sources": {
    f"https://{u7}/x86_64/repodata/repomd.xml": f"sha256:{sha(md(prim7))}"}}]}, open(f"{w}/runs7/r.json", "w"))
p = subprocess.run([sys.executable, "-B", f"{HERE}/archive.py", "ingest", f"{w}/store7", "--offline", "--tree",
                    f"{w}/tree", "--runs", f"{w}/runs7", "--fetched", f"{w}/fetched7"], capture_output=True, text=True)
idx = json.load(open(f"{w}/store7/index/latest.json"))["files"]
src7 = lambda n: idx[f"https://{u7}/x86_64/Packages/{n}.rpm"][f"sha256:{sha(bins[n])}"]["source"]
check(src7("g") == {"url": f"https://{rh}/g-1-3.el7_9.src.rpm", "digest": f"sha256:{sha(srcs['g'])}"} and
      idx[f"https://{rh}/g-1-3.el7_9.src.rpm"][f"sha256:{sha(srcs['g'])}"].get("live"),
      "a CentOS 7 update whose source the vault never got is taken from Red Hat's SRPMS, marked live")
check(src7("f")["url"] == "https://kojipkgs.fedoraproject.org/packages/f/7/11/src/f-7-11.src.rpm",
      "a package Fedora built whose source no repo keeps any more is taken from koji")
check("missing" in src7("h") and "missing" in src7("d") and "missing" in src7("s") and
      f"https://{rh}/d-1-1.el7.src.rpm" not in idx,
      "koji isn't asked for what CentOS built, and a source rpm naming another package is refused")
put(f"{rh}/s-2-1.el7.src.rpm", srcs["s"])
os.makedirs(f"{w}/runs7b")
p = subprocess.run([sys.executable, "-B", f"{HERE}/archive.py", "ingest", f"{w}/store7", "--offline", "--tree",
                    f"{w}/tree", "--runs", f"{w}/runs7b"], capture_output=True, text=True)
idx = json.load(open(f"{w}/store7/index/latest.json"))["files"]
check(src7("s").get("url") == f"https://{rh}/s-2-1.el7.src.rpm" and "missing" in src7("h") and
      f"no source https://{u7}/x86_64/Packages/h.rpm" in p.stdout,
      "an earlier ingest's gap is tried again though this ingest never walked to it, and one still missing is listed")
# CentOS 7's last updates, whose sources survive only in dist-git: the import tag's files at its commit, and each
# tarball its .<name>.metadata lists from the lookaside, checked against that sha1; all of it or nothing
dg = f"{w}/distgit"
tarball = b"k-1.tar.gz bytes"
for n, tarsha in (("k", hashlib.sha1(tarball).hexdigest()), ("m", "0" * 40)):
    g = lambda *a: subprocess.run(["git", "-C", f"{dg}/{n}", "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                  check=True, capture_output=True)
    os.makedirs(f"{dg}/{n}/SPECS")
    os.makedirs(f"{dg}/{n}/SOURCES")
    open(f"{dg}/{n}/SPECS/{n}.spec", "w").write(f"Name: {n}\nVersion: 1\nRelease: 2.el7_9\n")
    open(f"{dg}/{n}/SOURCES/fix.patch", "w").write("--- a\n+++ b\n")
    open(f"{dg}/{n}/.{n}.metadata", "w").write(f"{tarsha} SOURCES/{n}-1.tar.gz\n")
    g("init", "-q")
    g("add", "-A")
    g("commit", "-q", "-m", "import")
    g("tag", f"imports/c7/{n}-1-2.el7_9")
    g("commit", "-q", "--allow-empty", "-m", "a later import")  # the tag, not the branch tip, is what counts
put(f"git.centos.org/sources/k/c7/{hashlib.sha1(tarball).hexdigest()}", tarball)
dbins = {n: rpm(tags(n.encode(), b"1", b"2.el7_9", cos, f"{n}-1-2.el7_9.src.rpm".encode())) for n in ("k", "m")}
u8 = "vault.centos.org/7.9.2009/updates"
os.makedirs(f"{w}/runs8")
os.makedirs(f"{w}/fetched8/r")
prim8 = gzip.compress(("<metadata>" + "".join(pkg(f"{n}.rpm", b, archive.rpm_sourcerpm(b)) for n, b in dbins.items()) +
                       "</metadata>").encode())
shutil.rmtree(f"{w}/tree/{u8}/x86_64")
put(f"{u8}/x86_64/repodata/repomd.xml", md(prim8))
put(f"{u8}/x86_64/repodata/{sha(prim8)}-primary.xml.gz", prim8)
for n, b in dbins.items():
    put(f"{u8}/x86_64/Packages/{n}.rpm", b)
open(f"{w}/fetched8/r/fetched.txt", "w").write("".join(f"https://{u8}/x86_64/Packages/{n}.rpm sha256:{sha(b)} 1\n"
                                                        for n, b in dbins.items()))
json.dump({"result": "pass", "box": "k", "started": "8", "hops": [{"sources": {
    f"https://{u8}/x86_64/repodata/repomd.xml": f"sha256:{sha(md(prim8))}"}}]}, open(f"{w}/runs8/r.json", "w"))
p = subprocess.run([sys.executable, "-B", f"{HERE}/archive.py", "ingest", f"{w}/store8", "--offline", "--tree",
                    f"{w}/tree", "--runs", f"{w}/runs8", "--fetched", f"{w}/fetched8"], capture_output=True, text=True,
                   env={**os.environ, "WEND_DISTGIT": f"file://{dg}/{{name}}"})
idx = json.load(open(f"{w}/store8/index/latest.json"))["files"]
web = "https://gitlab.com/CentOS/archives/git.centos.org/rpms/k/-/raw/"
src8 = lambda n: idx[f"https://{u8}/x86_64/Packages/{n}.rpm"][f"sha256:{sha(dbins[n])}"]["source"]
kept8 = {u: e for u, d in idx.items() for e in d.values() if u.startswith(web) or "/sources/k/" in u}
check(src8("k").get("url", "").startswith(web) and src8("k")["url"].endswith("/SPECS/k.spec") and
      sorted(u.rsplit("/", 1)[1] for u in kept8) == sorted([".k.metadata", "fix.patch", "k.spec",
                                                            hashlib.sha1(tarball).hexdigest()]) and
      all(e.get("live") and e["runs"] == ["k@8"] for e in kept8.values()),
      "a CentOS 7 update with no .src.rpm anywhere stores its dist-git import: the tag's files and its tarball, live")
check("missing" in src8("m") and not [u for u in idx if "/rpms/m/" in u],
      "a dist-git import whose tarball the lookaside lost is no source, and none of it is kept")
import shutil as sh  # noqa: E402
if sh.which("ssh-keygen"):  # the index signature (docs/archive.md, step 8)
    a = lambda *x: subprocess.run([sys.executable, "-B", f"{HERE}/archive.py", *x], capture_output=True, text=True)
    for k in ("key", "other"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", k, "-f", f"{w}/{k}"], check=True)
    signers = f"{w}/allowed_signers"
    open(signers, "w").write(f'wend-archive namespaces="wend-archive" {open(f"{w}/key.pub").read()}')
    s = f"{w}/store"
    os.chmod(f"{w}/key", 0o644)
    p = a("sign", s, "--key", f"{w}/key")
    check(p.returncode == 2 and "chmod 600" in p.stdout, "a signing key others can read is refused")
    os.chmod(f"{w}/key", 0o600)
    p = a("verify", s, "--signers", signers)
    check(p.returncode == 1 and "UNSIGNED index/latest.json" in p.stdout, "verify with signers: an unsigned index is bad")
    p = a("sign", s, "--key", f"{w}/key")
    n = len([f for f in os.listdir(f"{s}/index") if f.endswith(".json")])
    check(p.returncode == 0 and f"signed {n} index files" in p.stdout and
          all(os.path.isfile(f"{s}/index/{f}.sig") for f in os.listdir(f"{s}/index") if f.endswith(".json")),
          "sign writes a .sig beside every index file")
    p = a("verify", s, "--signers", signers)
    check(p.returncode == 0 and "signatures checked" in p.stdout, "verify checks every index's signature")
    p = a("sign", s, "--key", f"{w}/key")
    check("signed 1 index files" in p.stdout, "signing again re-signs only latest.json")
    open(f"{s}/index/latest.json", "a").write(" ")
    p = a("verify", s, "--signers", signers)
    check(p.returncode == 1 and "UNSIGNED index/latest.json" in p.stdout, "an index changed after signing is bad")
    p = a("sign", s, "--key", f"{w}/other")
    p = a("verify", s, "--signers", signers)
    check(p.returncode == 1 and "UNSIGNED index/latest.json" in p.stdout, "a signature by a key not in signers is bad")
# wend's own library tables: only at the digest an adapter in this checkout pins
sys.path.insert(0, HERE)
import sonames  # noqa: E402
tables = sonames.pins()
fam, rel, pin = tables[0]
os.makedirs(f"{w}/tables/{fam}")
open(f"{w}/tables/{fam}/{rel}.txt.xz", "wb").write(b"not the pinned table")
p = subprocess.run([sys.executable, "-B", f"{HERE}/archive.py", "add-tables", f"{w}/tstore", f"{w}/tables"],
                   capture_output=True, text=True)
check(p.returncode == 1 and f"{fam}/{rel}.txt.xz" in p.stdout and "wrong" in p.stdout and
      not os.path.isdir(f"{w}/tstore/blob"),
      "add-tables stores no table that isn't the pinned one")
if sh.which("zstd"):  # EPEL 10 serves its primary only as .zst; CI's runners have zstd, not every laptop does
    sys.path.insert(0, HERE)
    import archive  # noqa: E402
    z = subprocess.run(["zstd", "-c"], input=b"<metadata/>", capture_output=True).stdout
    check(archive.decompress("x/primary.xml.zst", z) == b"<metadata/>", "a .zst index is read")
subprocess.run(["rm", "-rf", w])
sys.exit(0 if all(results) else 1)
