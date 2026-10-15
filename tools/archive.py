"""The wend archive's store (docs/archive.md). Runs on remi, the only writer. Standard library only.

  python3 tools/archive.py ingest STORE [--fetched DIR]... [--blobs DIR]... [--tree DIR]... [--offline] [--runs DIR]
  python3 tools/archive.py verify STORE

ingest trusts nothing but the run files on main (runs/, result "pass"). Each hop's `sources` pins a top metadata file
by sha256; the top file lists its indexes' checksums; an index lists its packages'. A file goes into the store only
when it hashes right against that chain, wherever its bytes came from:

  --fetched DIR   a replay's download list (RUN.fetched/ or the gate's fetched-N artifacts; any depth): its
                  fetched.txt says which indexes and packages the route actually used, its blobs/ holds the
                  metadata's bytes as they were that day
  --blobs DIR     files named by their sha256 hex (the mirror shim's .sha256/)
  --tree DIR      files laid out host/path (the shim's cache, or a frozen copy of it)
  then the origin, by url, unless --offline
  (--runs DIR reads the run files from DIR instead of this checkout's runs/: for tests)

What goes in: every top file a passing run pins; under it, each index some download list names, and always each
package index (primary, Packages: the files that vouch for packages); and each package a download list names. Never
the rest of a repo. Blobs are kept as upstream served them, under blob/sha256/<2 hex>/<64 hex>. index/latest.json
maps each url to every digest the store has for it, with its size, when it was first stored and the runs that
reference it. Nothing is ever evicted. verify rehashes every blob and checks the index against them.
"""
import datetime
import glob
import gzip
import hashlib
import json
import lzma
import bz2
import os
import re
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGES = (".rpm", ".deb", ".udeb")
PACKAGE_INDEX = re.compile(r"(^|/)(primary\.xml(\.[a-z0-9]+)?|[0-9a-f]{40,64}-primary\.xml(\.[a-z0-9]+)?|"
                           r"binary-amd64/Packages(\.[a-z0-9]+)?)$")


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")


def hexof(alg, data):
    return hashlib.new(alg, data).hexdigest()


def decompress(name, data):
    for ext, f in ((".gz", gzip.decompress), (".xz", lzma.decompress), (".bz2", bz2.decompress)):
        if name.endswith(ext):
            return f(data)
    return data


# The same parsers as the client (client/src/fetch.rs), so the archive vouches for exactly what a walk will ask.
def release_entries(text):
    def section(name, alg):
        out, on = [], False
        for line in text.splitlines():
            if not line.startswith(" "):
                on = line.rstrip() == f"{name}:"
                continue
            f = line.split()
            if on and len(f) == 3 and re.fullmatch(r"[0-9a-fA-F]+", f[0]):
                out.append((f[2], (alg, f[0].lower())))
        return out
    return section("SHA256", "sha256") or section("SHA1", "sha1")


def repomd_entries(text):
    out = []
    for block in text.split("<data ")[1:]:
        c = re.search(r'<checksum type="([a-z0-9]+)">\s*([0-9a-fA-F]+)\s*</checksum>', block)
        loc = re.search(r'<location href="([^"]+)"', block)
        if c and loc:
            out.append((loc[1], (alg_name(c[1]), c[2].lower())))
    return out


def packages_entries(text):
    out = []
    for stanza in text.split("\n\n"):
        fields = {}
        for line in stanza.splitlines():
            k, colon, v = line.partition(":")
            if colon and not line.startswith(" "):
                fields.setdefault(k, v.strip())
        if "Filename" in fields and ("SHA256" in fields or "SHA1" in fields):
            out.append((fields["Filename"], ("sha256", fields["SHA256"].lower()) if "SHA256" in fields
                        else ("sha1", fields["SHA1"].lower())))
    return out


def primary_entries(text):
    out = []
    for block in text.split("<package ")[1:]:
        c = re.search(r'<checksum type="([a-z0-9]+)"[^>]*>([0-9a-fA-F]+)</checksum>', block)
        loc = re.search(r'<location (?:xml:base="[^"]*" )?href="([^"]+)"', block)
        if c and loc:
            out.append((loc[1], (alg_name(c[1]), c[2].lower())))
    return out


def alg_name(t):
    return "sha1" if t == "sha" else t


def children(url, data):
    """(url, (alg, hex)) for everything a trusted metadata file vouches for."""
    text = decompress(url, data).decode("utf-8", "replace")
    if url.endswith("/repomd.xml"):
        base = url[:url.index("repodata/")]
        return [(base + href, d) for href, d in repomd_entries(text)]
    if url.endswith(("/Release", "/InRelease")):
        base = url.rsplit("/", 1)[0]
        return [(f"{base}/{name}", d) for name, d in release_entries(text)]
    if PACKAGE_INDEX.search(url) and "binary-" in url:
        mirror = url[:url.index("/dists/")]
        return [(f"{mirror}/{name}", d) for name, d in packages_entries(text)]
    if PACKAGE_INDEX.search(url):
        base = url[:url.index("repodata/")]
        return [(base + href, d) for href, d in primary_entries(text)]
    return []


class Store:
    def __init__(self, path, fetched=(), blobs=(), trees=(), offline=False):
        self.path, self.offline = path, offline
        self.blob_dirs = list(blobs) + [b for d in fetched for b in glob.glob(f"{d}/**/blobs", recursive=True)]
        self.trees = list(trees)
        self.used = set()  # (url) every download list names
        for d in fetched:
            for p in glob.glob(f"{d}/**/fetched.txt", recursive=True):
                for line in open(p):
                    f = line.split()
                    if len(f) == 3:
                        self.used.add(f[0])
        try:
            self.index = json.load(open(f"{path}/index/latest.json"))
        except FileNotFoundError:
            self.index = {"schema": "wend.archive/0", "files": {}}
        self.stamp = now()
        self.added, self.missing = [], []

    def blob(self, hexdigest):
        return f"{self.path}/blob/sha256/{hexdigest[:2]}/{hexdigest}"

    def candidates(self, url, digest):
        """Bytes to try for (url, digest), cheapest first. Each is checked by the caller."""
        alg, want = digest
        if alg == "sha256":
            yield self.blob(want), None
            for d in self.blob_dirs:
                yield f"{d}/{want}", None
        host_path = re.sub(r"^[a-z]+://", "", url)
        for t in self.trees:
            yield f"{t}/{host_path}", None
        if not self.offline:
            yield None, url
            if not url.endswith((".gz", ".xz", ".bz2")):
                yield None, url + ".gz"  # wheezy: the pin is the plain index, the archive serves only its .gz

    def get(self, url, digest):
        """The verified bytes of url at digest (plain bytes for a plain pin), or None."""
        alg, want = digest
        for path, src in self.candidates(url, digest):
            try:
                if path:
                    if not os.path.isfile(path):
                        continue
                    data = open(path, "rb").read()
                else:
                    with urllib.request.urlopen(src, timeout=300) as r:
                        data = r.read()
                    if src != url:
                        data = decompress(src, data)
            except Exception:
                continue
            if hexof(alg, data) == want:
                return data
        return None

    def keep(self, url, data, runs):
        h = hexof("sha256", data)
        p = self.blob(h)
        if not os.path.exists(p):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = f"{p}.tmp{os.getpid()}"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, p)
            self.added.append((url, h, len(data)))
        e = self.index["files"].setdefault(url, {}).setdefault(
            f"sha256:{h}", {"size": len(data), "first": self.stamp, "runs": []})
        e["runs"] = sorted(set(e["runs"]) | set(runs))

    def ingest(self, pins):
        """pins: {(url, (alg, hex)): set of run refs} from runs on main. Walks each chain down."""
        seen = set()
        todo = [(url, d, runs, 0) for (url, d), runs in sorted(pins.items())]
        while todo:
            url, d, runs, depth = todo.pop()
            if (url, d) in seen:
                continue
            seen.add((url, d))
            data = self.get(url, d)
            if data is None:
                self.missing.append(url)
                continue
            self.keep(url, data, runs)
            if url.endswith(PACKAGES):
                continue
            for child, cd in children(url, data):
                if child.endswith(PACKAGES):
                    wanted = child in self.used
                else:  # an index: the package indexes always, the rest when a walk used them
                    wanted = bool(PACKAGE_INDEX.search(child)) or child in self.used
                if wanted and depth < 3:
                    todo.append((child, cd, runs, depth + 1))

    def unfound(self):
        """Referenced files no source had. An index counts as found when any compressed form of it was."""
        stem = lambda u: re.sub(r"\.(gz|xz|bz2|lzma)$", "", u)
        have = {stem(u) for u, _, _ in self.added} | {stem(u) for u in self.index["files"]}
        return sorted({u for u in self.missing if u.endswith(PACKAGES) or stem(u) not in have})

    def write_index(self):
        os.makedirs(f"{self.path}/index", exist_ok=True)
        self.index["generated"] = self.stamp
        body = json.dumps(self.index, indent=1, sort_keys=True) + "\n"
        for name in (f"{self.stamp}.json", "latest.json"):
            tmp = f"{self.path}/index/.{name}.tmp"
            with open(tmp, "w") as f:
                f.write(body)
            os.replace(tmp, f"{self.path}/index/{name}")


def pins_on_main(runs_dir):
    pins = {}
    for p in sorted(glob.glob(f"{runs_dir}/**/*.json", recursive=True)):
        run = json.load(open(p))
        if run.get("result") != "pass":
            continue
        ref = f"{run['box']}@{run['started']}"
        for hop in run.get("hops", []):
            for url, digest in hop.get("sources", {}).items():
                alg, _, h = digest.partition(":")
                pins.setdefault((url, (alg, h)), set()).add(ref)
    return pins


def verify(path):
    index = json.load(open(f"{path}/index/latest.json"))
    bad = 0
    for url, digests in index["files"].items():
        for d, e in digests.items():
            p = f"{path}/blob/sha256/{d[7:9]}/{d[7:]}"
            try:
                data = open(p, "rb").read()
            except FileNotFoundError:
                print(f"MISSING {url} {d}")
                bad += 1
                continue
            if hexof("sha256", data) != d[7:] or len(data) != e["size"]:
                print(f"WRONG   {url} {d}")
                bad += 1
    print(f"{'ok' if not bad else 'BAD'}: {sum(len(v) for v in index['files'].values())} entries, {bad} bad")
    return 1 if bad else 0


def main(argv):
    if len(argv) < 2 or argv[0] not in ("ingest", "verify"):
        print(__doc__)
        return 2
    if argv[0] == "verify":
        return verify(argv[1])
    opts = {"--fetched": [], "--blobs": [], "--tree": [], "--runs": [f"{ROOT}/runs"]}
    rest = argv[2:]
    for i, a in enumerate(rest):
        if a in opts:
            opts[a].append(rest[i + 1])
    s = Store(argv[1], opts["--fetched"], opts["--blobs"], opts["--tree"], "--offline" in rest)
    s.ingest(pins_on_main(opts["--runs"][-1]))
    s.write_index()
    size = sum(n for _, _, n in s.added)
    missing = s.unfound()
    print(f"stored {len(s.added)} new blobs ({size / 1e6:.1f} MB); {len(missing)} referenced files not found:")
    for u in missing:
        print(f"  missing {u}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
