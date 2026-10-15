"""Reading a repo's primary metadata however it's compressed, by behaviour: EPEL 9 lists primary.xml only as .xz and
EPEL 10 only as .zst, while the EL repos still have .gz. Covers backends.primary_href and backends.unpack (which
sandbox's bootstrap depsolver and shelter's repo reader use) and rpm.transit's on-box add-on package list
(backends.package_names), run in sh against file:// repos made from the client's EPEL fixtures (client/testdata). It
runs the PR's own tools, so the gate's schema job (which judges a PR with the base branch's tools and never runs the
PR's code) doesn't run it: `wend check` and the client job do. Needs xz and zstd.
python3 tools/test_primary.py"""
import gzip
import hashlib
import os
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import backends  # noqa: E402

FIX = os.path.join(ROOT, "client", "testdata")
bad = 0


def check(name, ok):
    global bad
    print(("ok   " if ok else "FAIL ") + name)
    bad += not ok


def repomd(href, data):
    # as createrepo_c writes it (EPEL 9's, cut to the primary and a primary_db that isn't XML)
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<repomd xmlns="http://linux.duke.edu/metadata/repo">\n'
            f'  <data type="primary_db">\n    <checksum type="sha256">{"0" * 64}</checksum>\n'
            f'    <location href="repodata/x-primary.sqlite.xz"/>\n  </data>\n'
            f'  <data type="primary">\n    <checksum type="sha256">{hashlib.sha256(data).hexdigest()}</checksum>\n'
            f'    <location href="{href}"/>\n  </data>\n</repomd>\n')


xml9 = backends.unpack("x.xz", open(os.path.join(FIX, "epel9-primary.xml.xz"), "rb").read())
xml10 = backends.unpack("x.zst", open(os.path.join(FIX, "epel10-primary.xml.zst"), "rb").read())
check("EPEL 9's .xz primary unpacks to its two packages",
      xml9.count(b"<package ") == 2 and b"3cpio-0.14.0-1.el9.x86_64.rpm" in xml9)
check("EPEL 10's .zst primary unpacks to its two packages",
      xml10.count(b"<package ") == 2 and b"3proxy-1.0.0-1.el10_3.x86_64.rpm" in xml10)
check("a .gz unpacks as before", backends.unpack("x.gz", gzip.compress(xml9)) == xml9)
check("an uncompressed file is as it is", backends.unpack("primary.xml", xml9) == xml9)
try:
    backends.unpack("x.bz2", b"BZh9")
    check("bz2 is refused, not guessed", False)
except ValueError:
    check("bz2 is refused, not guessed", True)

with tempfile.TemporaryDirectory() as tmp:
    out = os.path.join(tmp, "names")
    for label, name, data in (("gz", "primary.xml.gz", gzip.compress(xml9)),
                              ("xz", "primary.xml.xz", open(os.path.join(FIX, "epel9-primary.xml.xz"), "rb").read()),
                              ("zst", "primary.xml.zst", open(os.path.join(FIX, "epel10-primary.xml.zst"), "rb").read())):
        repo = os.path.join(tmp, label)
        os.makedirs(f"{repo}/repodata")
        href = f"repodata/{hashlib.sha256(data).hexdigest()}-{name}"
        open(f"{repo}/{href}", "wb").write(data)
        open(f"{repo}/repodata/repomd.xml", "w").write(repomd(href, data))
        check(f"primary_href finds the .{label} primary, not the sqlite",
              backends.primary_href(open(f"{repo}/repodata/repomd.xml").read()) == href)
        open(out, "w").close()
        p = subprocess.run(["sh", "-c", backends.package_names(f"file://{repo}", out)], capture_output=True, text=True)
        names = open(out).read().split()
        check(f"rpm.transit's add-on package list reads a .{label} primary", p.returncode == 0 and names == ["3cpio", "3proxy"])
    try:
        backends.primary_href("<repomd><data type=\"filelists\"><location href=\"repodata/f.xml.gz\"/></data></repomd>")
        check("a repomd with no primary is refused", False)
    except ValueError:
        check("a repomd with no primary is refused", True)

sys.exit(1 if bad else 0)
