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
reference it. A file a chain names only by another hash (CentOS 5's SHA-1) also gets alias/<alg>/<hex>, one line
naming its blob. One exception to "as upstream served them": an index a run pins uncompressed (wheezy, whose Release
is gone) is stored as the uncompressed bytes its pin names, taken from the .gz upstream serves. Nothing is ever
evicted. verify rehashes every blob and alias and checks the index against them.

ingest exits 1 when a referenced file wasn't found or couldn't be read, so a cron job notices. One ingest at a time:
a second one exits 2 rather than wait.
"""
import datetime
import glob
import gzip
import hashlib
import json
import lzma
import bz2
import fcntl
import os
import re
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGES = (".rpm", ".deb", ".udeb")
UNREADABLE = (".zst", ".zck", ".lzma", ".lz4")  # compressions the client can't read either: reported, never skipped
MAX_PLAIN = 1 << 30  # a .gz decompressed before it can be checked stops here
PACKAGE_INDEX = re.compile(r"(^|/)(primary\.xml(\.[a-z0-9]+)?|[0-9a-f]{40,64}-primary\.xml(\.[a-z0-9]+)?|"
                           r"binary-amd64/Packages(\.[a-z0-9]+)?)$")


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")


def hexof(alg, data):
    return hashlib.new(alg, data).hexdigest()


def digest(alg, hexdigest):
    """client/src/fetch.rs Digest::parse: sha256 or sha1 (or "sha"), the right length, else None."""
    alg = {"sha256": "sha256", "sha1": "sha1", "sha": "sha1"}.get(alg.lower())
    hexdigest = hexdigest.lower()
    if alg and re.fullmatch("[0-9a-f]{%d}" % (64 if alg == "sha256" else 40), hexdigest):
        return alg, hexdigest
    return None


def plain_index(url):
    """An index pinned uncompressed (the client's `plain` rule): upstream may serve only its .gz."""
    return "/dists/" in url and not url.endswith(("Release", ".gz", ".xz", ".bz2") + UNREADABLE)


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
            if on and len(f) == 3 and digest(alg, f[0]):
                out.append((f[2], digest(alg, f[0])))
        return out
    return section("SHA256", "sha256") or section("SHA1", "sha1")


def repomd_entries(text):
    out = []
    for block in text.split("<data ")[1:]:
        c = re.search(r'<checksum type="([a-z0-9]+)">\s*([0-9a-fA-F]+)\s*</checksum>', block)
        loc = re.search(r'<location href="([^"]+)"', block)
        if c and loc and digest(c[1], c[2]):
            out.append((loc[1], digest(c[1], c[2])))
    return out


def packages_entries(text):
    out = []
    for stanza in text.split("\n\n"):
        fields = {}
        for line in stanza.splitlines():
            k, colon, v = line.partition(":")
            if colon and not line.startswith(" "):
                fields.setdefault(k, v.strip())
        d = digest("sha256", fields.get("SHA256", "")) or digest("sha1", fields.get("SHA1", ""))
        if "Filename" in fields and d:
            out.append((fields["Filename"], d))
    return out


def primary_entries(text):
    out = []
    for block in text.split("<package ")[1:]:
        c = re.search(r'<checksum type="([a-z0-9]+)"[^>]*>([0-9a-fA-F]+)</checksum>', block)
        loc = re.search(r'<location (?:xml:base="[^"]*" )?href="([^"]+)"', block)
        if c and loc and digest(c[1], c[2]):
            out.append((loc[1], digest(c[1], c[2])))
    return out


def children(url, data):
    """(url, (alg, hex)) for everything a trusted metadata file vouches for. Raises ValueError for a file it can't
    read (a compression the client can't read either, a url the chain can't be joined to)."""
    if url.endswith(UNREADABLE):
        raise ValueError("a compression neither the archive nor the client reads")
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


def stem(u):
    return re.sub(r"\.(gz|xz|bz2|lzma|zst)$", "", u)


def bounded_gunzip(f):
    """A .gz's contents, refusing past MAX_PLAIN: these bytes aren't checked until they're whole."""
    data = gzip.GzipFile(fileobj=f).read(MAX_PLAIN + 1)
    if len(data) > MAX_PLAIN:
        raise ValueError("decompresses past the limit")
    return data


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
        self.added, self.found = [], set()
        self.missing = []  # (url, digest, depth, parent, why)
        self.unreadable = []  # (url, why)
        self.kept = {}  # (url, digest) -> "sha256:<hex>"
        self.edges = {}  # (url, digest) -> [(child url, child digest)]
        self.found_under = set()  # (parent, stem): a form of an index its parent lists that this ingest found

    def blob(self, hexdigest):
        return f"{self.path}/blob/sha256/{hexdigest[:2]}/{hexdigest}"

    def candidates(self, url, digest):
        """Bytes to try for (url, digest), cheapest first. Each is checked by the caller."""
        alg, want = digest
        if alg == "sha256":
            yield self.blob(want), None
            for d in self.blob_dirs:
                yield f"{d}/{want}", None
        else:  # stored through a SHA-1 link before: its alias names the blob, which get() still checks by SHA-1
            try:
                line = open(f"{self.path}/alias/{alg}/{want}").read().strip()
            except OSError:
                line = ""
            if re.fullmatch(r"sha256:[0-9a-f]{64}", line):
                yield self.blob(line[7:]), None
        host_path = re.sub(r"^[a-z]+://", "", url)
        plain = plain_index(url)  # wheezy: the pin is the plain index, the archive serves only its .gz
        for t in self.trees:
            yield f"{t}/{host_path}", None
            if plain:
                yield f"{t}/{host_path}.gz", None
        if not self.offline:
            yield None, url
            if plain:
                yield None, url + ".gz"

    def get(self, url, digest):
        """(the verified bytes of url at digest, or None; why the origin didn't have them)."""
        alg, want = digest
        why = "no source has it" if self.offline else ""
        for path, src in self.candidates(url, digest):
            try:
                if path:
                    if not os.path.isfile(path):
                        continue
                    with open(path, "rb") as f:
                        data = bounded_gunzip(f) if plain_index(url) and path.endswith(".gz") else f.read()
                else:
                    with urllib.request.urlopen(src, timeout=300) as r:
                        data = r.read() if src == url else bounded_gunzip(r)
            except Exception as e:  # urllib's HTTPError, a timeout, a bad .gz: this source hasn't got it
                if src:
                    why = f"{src}: {e}"
                continue
            if hexof(alg, data) == want:
                return data, ""
            if src:
                why = f"{src}: different bytes now"
        return None, why or "no source has it"

    def keep(self, url, data):
        h = hexof("sha256", data)
        p = self.blob(h)
        if not os.path.exists(p):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = f"{p}.tmp{os.getpid()}"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, p)
            self.added.append((url, h, len(data)))
        self.index["files"].setdefault(url, {}).setdefault(
            f"sha256:{h}", {"size": len(data), "first": self.stamp, "runs": []})
        self.found.add(url)
        return h

    def alias(self, digest, h):
        """alias/<alg>/<hex>: for a file a chain names by another hash (CentOS 5's SHA-1), one line naming its blob."""
        alg, other = digest
        p = f"{self.path}/alias/{alg}/{other}"
        if alg != "sha256" and not os.path.exists(p):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(f"{p}.tmp{os.getpid()}", "w") as f:
                f.write(f"sha256:{h}\n")
            os.replace(f"{p}.tmp{os.getpid()}", p)

    def ingest(self, pins):
        """pins: {(url, (alg, hex)): set of run refs} from runs on main. Walks each chain down, then gives every
        stored file the runs of every pin above it (two pins' indexes often list the same package)."""
        seen = set()
        todo = [(url, d, 0, None) for (url, d) in sorted(pins)]
        while todo:
            url, d, depth, parent = todo.pop()
            if (url, d) in seen:
                continue
            seen.add((url, d))
            data, why = self.get(url, d)
            if data is None:
                self.missing.append((url, d, depth, parent, why))
                continue
            h = self.keep(url, data)
            self.alias(d, h)
            self.kept[(url, d)] = f"sha256:{h}"
            if parent:
                self.found_under.add((parent, stem(url)))
            if url.endswith(PACKAGES):
                continue
            try:
                listed = children(url, data)
            except Exception as e:
                self.unreadable.append((url, str(e)))
                continue
            for child, cd in listed:
                if child.endswith(PACKAGES):
                    wanted = child in self.used
                else:  # an index: the package indexes always, the rest when a walk used them
                    wanted = bool(PACKAGE_INDEX.search(child)) or child in self.used
                if wanted and depth < 3:
                    self.edges.setdefault((url, d), []).append((child, cd))
                    todo.append((child, cd, depth + 1, url))
        for key, runs in pins.items():
            stack, done = [key], set()
            while stack:
                k = stack.pop()
                if k in done:
                    continue
                done.add(k)
                if k in self.kept:
                    e = self.index["files"][k[0]][self.kept[k]]
                    e["runs"] = sorted(set(e["runs"]) | set(runs))
                stack.extend(self.edges.get(k, []))

    def unfound(self):
        """{url: why} for referenced files no source had at their digest. The one exception: an index its parent lists
        in several compressed forms counts as found when this ingest found another form of it under the same parent
        (a repo serves one or two). An older digest at the same url never counts: a newer run's pin is its own file."""
        out = {}
        for u, _, depth, parent, why in self.missing:
            if depth == 0 or u.endswith(PACKAGES) or (parent, stem(u)) not in self.found_under or u in self.found:
                out.setdefault(u, why)
        return dict(sorted(out.items()))

    def write_index(self):
        os.makedirs(f"{self.path}/index", exist_ok=True)
        self.index["generated"] = self.stamp
        body = json.dumps(self.index, indent=1, sort_keys=True) + "\n"
        for name in (f"{self.stamp}.json", "latest.json"):
            tmp = f"{self.path}/index/.{name}.tmp{os.getpid()}"
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
            for url, pin in hop.get("sources", {}).items():
                alg, _, h = pin.partition(":")
                d = digest(alg, h)
                if not d or d[0] != "sha256":
                    sys.exit(f"{p}: pins {url} with something that isn't a sha256")
                pins.setdefault((url, d), set()).add(ref)
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
    aliases = glob.glob(f"{path}/alias/*/*")
    for a in aliases:
        alg, other = a.split("/")[-2:]
        line = open(a).read().strip()
        try:
            data = open(f"{path}/blob/sha256/{line[7:9]}/{line[7:]}", "rb").read()
        except OSError:
            data = None
        if not line.startswith("sha256:") or data is None or hexof(alg, data) != other:
            print(f"ALIAS   {alg}:{other} -> {line}")
            bad += 1
    print(f"{'ok' if not bad else 'BAD'}: {sum(len(v) for v in index['files'].values())} entries, "
          f"{len(aliases)} aliases, {bad} bad")
    return 1 if bad else 0


def main(argv):
    if len(argv) < 2 or argv[0] not in ("ingest", "verify"):
        print(__doc__)
        return 2
    if argv[0] == "verify":
        return verify(argv[1])
    opts = {"--fetched": [], "--blobs": [], "--tree": [], "--runs": [f"{ROOT}/runs"]}
    rest, offline = argv[2:], False
    while rest:
        a = rest.pop(0)
        if a == "--offline":
            offline = True
        elif a in opts and rest:
            opts[a].append(rest.pop(0))
        else:
            print(f"ingest: {a!r} isn't an option, or has no value\n{__doc__}")
            return 2
    os.makedirs(argv[1], exist_ok=True)
    lock = open(f"{argv[1]}/.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("ingest: another ingest is running on this store")
        return 2
    s = Store(argv[1], opts["--fetched"], opts["--blobs"], opts["--tree"], offline)
    try:
        s.ingest(pins_on_main(opts["--runs"][-1]))
    finally:
        s.write_index()  # what was stored before a crash is indexed all the same
    size = sum(n for _, _, n in s.added)
    missing = s.unfound()
    print(f"stored {len(s.added)} new blobs ({size / 1e6:.1f} MB); {len(missing)} referenced files not found:")
    for u, why in missing.items():
        print(f"  missing {u} ({why})")
    for u, why in s.unreadable:
        print(f"  unreadable {u} ({why})")
    return 1 if missing or s.unreadable else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
