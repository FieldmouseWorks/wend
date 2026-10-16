"""The wend archive's store (docs/archive.md). Runs on remi, the only writer. Standard library only.

  python3 tools/archive.py ingest STORE [--fetched DIR]... [--blobs DIR]... [--tree DIR]... [--offline] [--runs DIR]
  python3 tools/archive.py verify STORE [--signers FILE]
  python3 tools/archive.py sign STORE --key KEY

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
package index (primary, Packages: the files that vouch for packages), in one form unless a walk used others; each
package a download list names; and each stored package's source (a .src.rpm from its repo's source repo, or a .dsc
and its files from the Sources index its Release lists), checked against that metadata. A package whose source
can't be found is listed as a gap, not a failure. Never the rest of a repo. Indexes compressed with zstd (EPEL 10)
need Python 3.14 or the zstd command. Blobs are kept as upstream served them, under blob/sha256/<2 hex>/<64 hex>. index/latest.json
maps each url to every digest the store has for it, with its size, when it was first stored and the runs that
reference it. A file a chain names only by another hash (CentOS 5's SHA-1) also gets alias/<alg>/<hex>, one line
naming its blob. One exception to "as upstream served them": an index a run pins uncompressed (wheezy, whose Release
is gone) is stored as the uncompressed bytes its pin names, taken from the .gz upstream serves. Nothing is ever
evicted. verify rehashes every blob and alias and checks the index against them.

ingest exits 1 when a referenced file wasn't found or couldn't be read, so a cron job notices. One ingest at a time:
a second one exits 2 rather than wait.

sign signs each index file (index/<stamp>.json, index/latest.json) with ssh-keygen -Y sign, ed25519, namespace
wend-archive, into <file>.sig beside it: what says which URLs the archive saw upstream serve, and when. KEY is the
private key, readable by its owner only. A stamped index whose .sig still verifies is left alone; latest.json is
signed every time. verify checks every index's .sig against FILE (default archive/allowed_signers in this checkout,
when it exists): a missing or bad signature is bad.
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
import struct
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGES = (".rpm", ".deb", ".udeb")
UNREADABLE = (".zck", ".lzma", ".lz4")  # compressions nothing here reads: reported, never skipped
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


def md5(hexdigest):
    """("md5", hex) for a source file a Sources index names only by md5: see sources_entries."""
    return ("md5", hexdigest.lower()) if re.fullmatch("[0-9a-fA-F]{32}", hexdigest) else None


def plain_index(url):
    """An index pinned uncompressed (the client's `plain` rule): upstream may serve only its .gz."""
    return "/dists/" in url and not url.endswith(("Release", ".gz", ".xz", ".bz2", ".zst") + UNREADABLE)


def unzstd(data):
    """EPEL 10 serves its primary only as .zst: Python's own decoder (3.14+), else the zstd command."""
    try:
        from compression import zstd
        return zstd.decompress(data)
    except ImportError:
        pass
    try:
        p = subprocess.run(["zstd", "-dc"], input=data, capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError) as e:
        raise ValueError(f"can't decompress .zst here (no zstd: {e})")
    return p.stdout


def decompress(name, data):
    for ext, f in ((".gz", gzip.decompress), (".xz", lzma.decompress), (".bz2", bz2.decompress), (".zst", unzstd)):
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
        raise ValueError("a compression nothing here reads")
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


# Where each EL repo's source packages live. Data, not logic (docs/archive.md, "Source packages"): a source repo is
# matched by its binary repo's base. Debian and Ubuntu need none: a pinned Release lists <comp>/source/Sources.
SOURCE_REPOS = (
    (r"^(https://vault\.centos\.org/5\.[^/]+/[^/]+/)x86_64/$", r"\1SRPMS/"),
    (r"^(https://vault\.centos\.org/[67]\.[^/]+/[^/]+/)x86_64/$", r"\1Source/"),
    (r"^(https://vault\.centos\.org/8\.[^/]+/[^/]+/)x86_64/os/$", r"\1Source/"),
    (r"^https://repo\.almalinux\.org/almalinux/(\d+)/([^/]+)/x86_64/os/$", r"https://vault.almalinux.org/\1/\2/Source/"),
    (r"^(https://vault\.almalinux\.org/[^/]+/[^/]+/)x86_64/os/$", r"\1Source/"),
    (r"^(https://dl\.fedoraproject\.org/pub/epel/\d+/Everything/)x86_64/$", r"\1source/tree/"),
    (r"^(https://archives\.fedoraproject\.org/pub/archive/epel/\d+/)x86_64/$", r"\1SRPMS/"),
    (r"^(https://archives\.fedoraproject\.org/pub/archive/epel/[0-9.]+/Everything/)x86_64/$", r"\1source/tree/"),
    # Scientific Linux 7: one source repo for os and both update repos
    (r"^(https://linux-mirrors\.fnal\.gov/linux/scientific/obsolete/7\.9/)x86_64/(os|updates/security|updates/fastbugs)/$",
     r"\1SRPMS/"),
)


def source_repo(base):
    for pattern, to in SOURCE_REPOS:
        if re.match(pattern, base):
            return re.sub(pattern, to, base)
    return None


def rpm_sourcerpm(data):
    """The SOURCERPM tag (1044) of a binary .rpm's header, or None: for a package a run pins directly, which no
    primary names. Lead (96 bytes), signature header (padded to 8), then the main header."""
    def header(at):
        if data[at:at + 4] != b"\x8e\xad\xe8\x01":
            return None, None
        n, size = struct.unpack(">II", data[at + 8:at + 16])
        store = at + 16 + 16 * n
        return [struct.unpack(">IIII", data[at + 16 + 16 * i:at + 32 + 16 * i]) for i in range(n)], (store, size)
    try:
        sig, (store, size) = header(96)
        if sig is None:
            return None
        main, (store, _) = header(96 + (16 + 16 * len(sig) + size + 7) // 8 * 8)
        for tag, typ, off, _ in main or []:
            if tag == 1044 and typ == 6:
                v = data[store + off:data.index(b"\0", store + off)].decode()
                return v if re.fullmatch(r"[A-Za-z0-9._+~^-]+\.src\.rpm", v) else None
    except (struct.error, ValueError, TypeError, UnicodeDecodeError):
        pass
    return None


def source_names(url, data):
    """{package url: what it was built from} for a trusted package index: ("rpm", "<name>.src.rpm", repo base) from
    a primary, ("deb", source name, version, mirror, "<.../dists/X>/<comp>") from a Packages file."""
    text = decompress(url, data).decode("utf-8", "replace")
    out = {}
    if "binary-" in url:
        mirror = url[:url.index("/dists/")]
        comp = re.match(r"(.*/dists/[^/]+/.+?)(/debian-installer)?/binary-", url)[1]  # udebs' sources are comp's
        for stanza in text.split("\n\n"):
            f = {}
            for line in stanza.splitlines():
                k, colon, v = line.partition(":")
                if colon and not line.startswith(" "):
                    f.setdefault(k, v.strip())
            if "Filename" in f and "Package" in f and "Version" in f:
                m = re.fullmatch(r"(\S+)(?:\s+\((\S+)\))?", f.get("Source", f["Package"]))
                if m:
                    out[f"{mirror}/{f['Filename']}"] = ("deb", m[1], m[2] or f["Version"], mirror, comp)
        return out
    base = url[:url.index("repodata/")]
    for block in text.split("<package ")[1:]:
        loc = re.search(r'<location (?:xml:base="[^"]*" )?href="([^"]+)"', block)
        src = re.search(r"<rpm:sourcerpm>([^<]+)</rpm:sourcerpm>", block)
        if loc and src:
            out[base + loc[1]] = ("rpm", src[1], base)
    return out


def sources_entries(text):
    """A Sources index: {(package, version): [(directory/name, digest)]}, by Checksums-Sha256, else Checksums-Sha1,
    else Files' md5 (lenny and squeeze still list sources uploaded before dpkg wrote sha sums). md5 is good enough
    here and nowhere else: these are the distro's own 2000s uploads, kept for their licences and never installed."""
    out = {}
    for stanza in text.split("\n\n"):
        f, cur = {}, None
        for line in stanza.splitlines():
            if line.startswith(" "):
                if cur and isinstance(f[cur], list):
                    f[cur].append(line.split())
                continue
            k, colon, v = line.partition(":")
            if colon:
                cur = k
                f[k] = v.strip() if v.strip() else []
        files = []
        for field, alg in (("Checksums-Sha256", "sha256"), ("Checksums-Sha1", "sha1"), ("Files", "md5")):
            if isinstance(f.get(field), list):
                files = [(f"{f.get('Directory', '')}/{x[2]}", md5(x[0]) if alg == "md5" else digest(alg, x[0]))
                         for x in f[field] if len(x) == 3]
                break
        if files and all(d for _, d in files) and isinstance(f.get("Package"), str):
            out[(f["Package"], f.get("Version"))] = files
    return out


def stem(u):
    return re.sub(r"\.(gz|xz|bz2|lzma|zst)$", "", u)


PLAIN_FORMS = (".gz", ".bz2", ".xz")  # jessie..buster's Translation-en: snapshot serves only the .bz2


def bounded_unpack(f, name):
    """A compressed index's contents, refusing past MAX_PLAIN: these bytes aren't checked until they're whole."""
    opener = {".gz": gzip.open, ".bz2": bz2.open, ".xz": lzma.open}[name[name.rindex("."):]]
    data = opener(f).read(MAX_PLAIN + 1)
    if len(data) > MAX_PLAIN:
        raise ValueError("decompresses past the limit")
    return data


class Store:
    def __init__(self, path, fetched=(), blobs=(), trees=(), offline=False):
        self.path, self.offline = path, offline
        self.blob_dirs = list(blobs) + [b for d in fetched for b in glob.glob(f"{d}/**/blobs", recursive=True)]
        self.trees = list(trees)
        self.used = set()  # (url) every download list names
        self.listed = {}  # url -> the sha256 hexes download lists give it: a SHA-1-named file's captured bytes
        for d in fetched:
            for p in glob.glob(f"{d}/**/fetched.txt", recursive=True):
                for line in open(p):
                    f = line.split()
                    if len(f) == 3:
                        self.used.add(f[0])
                        if re.fullmatch(r"sha256:[0-9a-f]{64}", f[1]):
                            self.listed.setdefault(f[0], set()).add(f[1][7:])
        try:
            self.index = json.load(open(f"{path}/index/latest.json"))
        except FileNotFoundError:
            self.index = {"schema": "wend.archive/0", "files": {}}
        self.stamp = now()
        self.added, self.found = [], set()
        self.rotted = set()  # store blobs get() found not hashing to their name: keep() rewrites them
        self.missing = []  # (url, digest, depth, parent (url, digest), why)
        self.unreadable = []  # (url, why)
        self.kept = {}  # (url, digest) -> "sha256:<hex>"
        self.edges = {}  # (url, digest) -> [(child url, child digest)]
        self.found_under = set()  # ((parent url, parent digest), stem): a form of an index that parent lists, found
        self.built_from = {}  # (package url, digest) -> source_names() value, for every package stored
        self.deb_sources = {}  # "<.../dists/X>/<comp>" -> [(Sources url, digest, the Release that lists it)]
        self.no_source = {}  # (package url, digest) -> why no source was stored
        self.deb_indexes = {}  # "<.../dists/X>" -> {(url, digest)} of the package indexes pinned under it
        self.live_releases = {}  # "<.../dists/X>" -> [(url, "sha256:<hex>")]: a live Release read for its Sources

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
            for h in sorted(self.listed.get(url, ())):  # captured under the sha256 the shim logged
                yield self.blob(h), None
                for d in self.blob_dirs:
                    yield f"{d}/{h}", None
        host_path = re.sub(r"^[a-z]+://", "", url)
        forms = PLAIN_FORMS if plain_index(url) else ()  # the pin is the plain index, upstream serves it compressed
        for t in self.trees:
            yield f"{t}/{host_path}", None
            for ext in forms:
                yield f"{t}/{host_path}{ext}", None
        if not self.offline:
            yield None, url
            for ext in forms:
                yield None, url + ext

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
                        data = bounded_unpack(f, path) if plain_index(url) and path.endswith(PLAIN_FORMS) else f.read()
                else:
                    with urllib.request.urlopen(src, timeout=300) as r:
                        data = r.read() if src == url else bounded_unpack(r, src)
            except Exception as e:  # urllib's HTTPError, a timeout, a bad .gz: this source hasn't got it
                if src:
                    why = f"{src}: {e}"
                continue
            if hexof(alg, data) == want:
                return data, ""
            if path and path.startswith(f"{self.path}/blob/") and hexof("sha256", data) != path.rsplit("/", 1)[1]:
                self.rotted.add(path)
            if src:
                why = f"{src}: different bytes now"
        return None, why or "no source has it"

    def keep(self, url, data):
        h = hexof("sha256", data)
        p = self.blob(h)
        if not os.path.exists(p) or p in self.rotted:
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
            if parent and url not in self.used and (parent, stem(url)) in self.found_under:
                continue  # another form of this index, already stored: one vouches as well as three
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
            try:
                names = source_names(url, data) if PACKAGE_INDEX.search(url) else {}
            except Exception:  # its packages still go in; their sources show up as gaps
                names = {}
            if "/dists/" in url and "/binary-" in url and PACKAGE_INDEX.search(url):
                self.deb_indexes.setdefault(url[:url.index("/", url.index("/dists/") + 7)], set()).add((url, d))
            if url.endswith(("/Release", "/InRelease")):
                for child, cd in listed:
                    m = re.match(r"(.*/dists/[^/]+)/(.+)/source/Sources(\.[a-z0-9]+)?$", child)
                    if m:
                        self.deb_sources.setdefault(f"{m[1]}/{m[2]}", []).append((child, cd, (url, d)))
            # popped last-in first: .gz, then plain, then the rest (the client learns packages from .gz or plain only)
            rank = lambda c: 3 if c[0].endswith(".gz") else 0 if c[0].endswith((".xz", ".bz2", ".zst")) else 2
            for child, cd in sorted(listed, key=rank):
                if child in names:
                    self.built_from[(child, cd)] = names[child]
                if child.endswith(PACKAGES):
                    wanted = child in self.used
                else:  # an index: the package indexes always, the rest when a walk used them
                    wanted = bool(PACKAGE_INDEX.search(child)) or child in self.used
                if wanted and depth < 3:
                    self.edges.setdefault((url, d), []).append((child, cd))
                    todo.append((child, cd, depth + 1, (url, d)))
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

    def live(self, url):
        """A file no pin names (a source repo's repomd.xml): from a tree, else the origin. The index records it, and
        everything under it is checked against it; the archive's signature is what vouches for it."""
        host_path = re.sub(r"^[a-z]+://", "", url)
        for t in self.trees:
            if os.path.isfile(f"{t}/{host_path}"):
                return open(f"{t}/{host_path}", "rb").read()
        if self.offline:
            return None
        try:
            with urllib.request.urlopen(url, timeout=300) as r:
                return r.read()
        except Exception:
            return None

    def sources(self):
        """Store the source of every package stored: the .src.rpm its primary names, from its source repo
        (SOURCE_REPOS), or the .dsc and files its Sources stanza lists, from the Sources indexes its pinned Releases
        list. Every source file is checked against that metadata, and gets the runs of the packages built from it.
        A package whose source can't be found gets "source": {"missing": why} in the index and is listed; that's a
        gap to fill before the archive goes public, not a failure."""
        groups = {}  # source repo base, or ("deb", mirror, suite dir) -> {package key: what}
        for key in sorted(self.kept):
            if not key[0].endswith(PACKAGES):
                continue
            what = self.built_from.get(key)
            if what is None and key[0].endswith(".rpm") and not key[0].endswith(".src.rpm"):
                # pinned directly (an old EPEL package no repo serves any more): its own header names its source
                srpm = rpm_sourcerpm(open(self.blob(self.kept[key][7:]), "rb").read())
                if srpm:
                    what = ("rpm", srpm, key[0].rsplit("/", 1)[0] + "/")
            if what is None:
                self.no_source[key] = "no package index names its source (pinned directly, or no sourcerpm)"
            elif what[0] == "rpm":
                base = source_repo(what[2])
                if base:
                    groups.setdefault(base, {})[key] = what
                else:
                    self.no_source[key] = f"no source repo known for {what[2]}"
            elif self.has_sources(what[4].rsplit("/", 1)[0]):
                groups.setdefault(("deb", what[3], what[4].rsplit("/", 1)[0]), {})[key] = what
            else:
                self.no_source[key] = "its suite's Release lists no Sources"
        stored = {}  # (url, digest) -> "sha256:<hex>", or None when it couldn't be had: each source file once
        for group, packages in sorted(groups.items(), key=lambda g: str(g[0])):
            meta = []  # (url, "sha256:<hex>") of the metadata this group's sources came through
            if isinstance(group, str):
                found, why = self.rpm_sources(group, meta)
                lookup = lambda what: found.get(what[1])
            else:
                lookup, why = self.deb_lookup(group[1], group[2], packages.values(), meta), ""
                meta += self.live_releases.get(group[2], [])
            runs_of_group = set()
            for key, what in packages.items():
                files = lookup(what)
                if not files:
                    self.no_source[key] = why or "its source isn't listed (with a sha1 or sha256) in the metadata now"
                    continue
                for u, d in files:
                    if (u, d) not in stored:
                        data, err = self.get(u, d)
                        stored[(u, d)] = None if data is None else f"sha256:{self.keep(u, data)}"
                        if data is not None:
                            self.alias(d, stored[(u, d)][7:])
                        else:
                            self.no_source[key] = f"{u}: {err}"
                if any(stored[f] is None for f in files):
                    self.no_source.setdefault(key, "a source file couldn't be had")
                    continue
                runs = self.index["files"][key[0]][self.kept[key]]["runs"]
                runs_of_group |= set(runs)
                for u, d in files:
                    e = self.index["files"][u][stored[(u, d)]]
                    e["runs"] = sorted(set(e["runs"]) | set(runs))
                main = next((f for f in files if f[0].endswith((".dsc", ".src.rpm"))), files[0])
                self.index["files"][key[0]][self.kept[key]]["source"] = {"url": main[0], "digest": stored[main]}
            for u, hd in meta:
                e = self.index["files"][u][hd]
                e["runs"] = sorted(set(e["runs"]) | runs_of_group)
        for key, why in self.no_source.items():
            self.index["files"][key[0]][self.kept[key]].setdefault("source", {"missing": why})

    def has_sources(self, suite):
        """Whether a Release lists Sources for suite. A suite whose run pinned only a package index (wheezy) gets its
        live Release, used only when it lists that pinned index at the pinned digest: then it is the Release the
        run's index came from, and its Sources entries vouch as a pinned Release's would."""
        if not any(c.rsplit("/", 1)[0] == suite for c in self.deb_sources) and suite not in self.live_releases:
            self.live_releases[suite] = []
            url = f"{suite}/Release"
            data = self.live(url)
            try:
                listed = children(url, data) if data is not None else []
            except ValueError:
                listed = []
            if self.deb_indexes.get(suite, set()) & set(listed):
                h = self.keep(url, data)
                self.index["files"][url][f"sha256:{h}"]["live"] = True
                self.live_releases[suite].append((url, f"sha256:{h}"))
                for child, cd in listed:
                    m = re.match(r"(.*/dists/[^/]+)/(.+)/source/Sources(\.[a-z0-9]+)?$", child)
                    if m:
                        self.deb_sources.setdefault(f"{m[1]}/{m[2]}", []).append((child, cd, (url, ("sha256", h))))
        return any(c.rsplit("/", 1)[0] == suite for c in self.deb_sources)

    def rpm_sources(self, base, meta):
        """({"<name>.src.rpm": [(url, digest)]}, why it's empty) from a source repo's live metadata. Its repomd.xml
        is pinned by no run: its index entry says "live", and the archive's signature is what vouches for it."""
        url = f"{base}repodata/repomd.xml"
        data = self.live(url)
        if data is None:
            return {}, f"its source repo's metadata couldn't be had ({url})"
        h = self.keep(url, data)
        self.index["files"][url][f"sha256:{h}"]["live"] = True
        meta.append((url, f"sha256:{h}"))
        try:
            listed = children(url, data)
        except ValueError as e:  # a gap, not a failure: unreadable fails an ingest only for what runs pin
            return {}, f"{url}: {e}"
        rank = lambda c: next((i for i, ext in enumerate((".gz", ".xz", ".zst")) if c[0].endswith(ext)), 9)
        why = f"no readable primary under {url}"
        for child, cd in sorted((c for c in listed if PACKAGE_INDEX.search(c[0])), key=rank):
            primary, err = self.get(child, cd)
            if primary is None:
                why = f"{child}: {err}"
                continue
            try:
                entries = children(child, primary)
            except ValueError as e:  # a .zst with no decoder here: try the next form
                why = f"{child}: {e}"
                continue
            h = self.keep(child, primary)
            self.alias(cd, h)
            meta.append((child, f"sha256:{h}"))
            return {u.rsplit("/", 1)[1]: [(u, d)] for u, d in entries}, ""
        return {}, why

    def deb_lookup(self, mirror, suite, wanted, meta):
        """A function from a package's what to its source files, reading the Sources indexes the pinned Releases list:
        every Release digest's for the packages' own components first, then the suite's other components (an Ubuntu
        universe binary is often built from a main source)."""
        found, read = {}, set()

        def read_comp(comp):
            read.add(comp)
            rank = lambda f: next((i for i, ext in enumerate((".gz", ".xz", "/Sources", ".bz2")) if f[0].endswith(ext)), 9)
            done = set()  # one form per Release, but every Release digest's (a point release is its own file)
            for u, d, release in sorted(self.deb_sources.get(comp, []), key=rank):
                if release in done:
                    continue
                data, _ = self.get(u, d)
                if data is None:
                    continue
                try:
                    entries = sources_entries(decompress(u, data).decode("utf-8", "replace"))
                except Exception:  # try another form; its packages are gaps if none reads
                    continue
                h = self.keep(u, data)
                self.alias(d, h)
                meta.append((u, f"sha256:{h}"))
                done.add(release)
                for k, files in entries.items():
                    found.setdefault(k, [(f"{mirror}/{name}", fd) for name, fd in files])

        for what in wanted:
            if what[4] not in read and what[4] in self.deb_sources:
                read_comp(what[4])

        def lookup(what):
            key = (what[1], what[2])
            if key not in found and re.search(r"\+b\d+$", what[2]):  # a binNMU whose Source names no version
                key = (what[1], re.sub(r"\+b\d+$", "", what[2]))
            if key not in found:
                for comp in sorted(c for c in self.deb_sources if c.rsplit("/", 1)[0] == suite and c not in read):
                    read_comp(comp)
                    if key in found:
                        break
            return found.get(key)
        return lookup

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


SIG_NS = "wend-archive"


def check_sig(signers, path):
    """Does path.sig verify against the allowed signers file, as wend-archive?"""
    if not os.path.isfile(f"{path}.sig"):
        return False
    with open(path, "rb") as f:
        r = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", signers, "-I", SIG_NS, "-n", SIG_NS,
                            "-s", f"{path}.sig"], stdin=f, capture_output=True)
    return r.returncode == 0


def sign(path, key, signers=None):
    """Sign every index file that needs it: latest.json always, a stamped one when its .sig is missing or (with
    signers) doesn't verify, or the index was rewritten after it. Each .sig is written whole, then renamed into place."""
    st = os.stat(key)
    if st.st_mode & 0o077:
        print(f"sign: {key} is readable by others than its owner; chmod 600 it first")
        return 2
    n = 0
    for p in sorted(glob.glob(f"{path}/index/*.json")):
        latest = p.endswith("/latest.json")
        fresh = os.path.isfile(f"{p}.sig") and os.path.getmtime(f"{p}.sig") >= os.path.getmtime(p)
        if not latest and fresh and (not signers or check_sig(signers, p)):
            continue
        with open(p, "rb") as f:
            r = subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", key, "-n", SIG_NS], stdin=f,
                               capture_output=True)
        if r.returncode != 0 or not r.stdout.startswith(b"-----BEGIN SSH SIGNATURE-----"):
            print(f"sign: ssh-keygen couldn't sign {p} (exit {r.returncode})")
            return 1
        tmp = f"{p}.sig.tmp{os.getpid()}"
        with open(tmp, "wb") as f:
            f.write(r.stdout)
        os.replace(tmp, f"{p}.sig")
        n += 1
    print(f"signed {n} index files")
    return 0


def verify(path, signers=None):
    index = json.load(open(f"{path}/index/latest.json"))
    bad = 0
    if signers:
        for p in sorted(glob.glob(f"{path}/index/*.json")):
            if not check_sig(signers, p):
                print(f"UNSIGNED {os.path.relpath(p, path)} (no .sig, or not by a key in {signers})")
                bad += 1
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
          f"{len(aliases)} aliases, {bad} bad{', signatures checked' if signers else ''}")
    return 1 if bad else 0


def lock_store(path, what):
    os.makedirs(path, exist_ok=True)
    lock = open(f"{path}/.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"{what}: an ingest, sign or push is running on this store")
        return None
    return lock


def main(argv):
    if len(argv) < 2 or argv[0] not in ("ingest", "verify", "sign"):
        print(__doc__)
        return 2
    if argv[0] in ("verify", "sign"):
        flag = "--signers" if argv[0] == "verify" else "--key"
        if len(argv) == 4 and argv[2] == flag:
            arg = argv[3]
        elif len(argv) == 2:
            arg = None
        else:
            print(__doc__)
            return 2
        # $WEND_ARCHIVE_SIGNERS overrides the default ("" for none: the tests' own made-up stores)
        default = os.environ.get("WEND_ARCHIVE_SIGNERS", f"{ROOT}/archive/allowed_signers")
        default = default if default and os.path.isfile(default) else None
        if argv[0] == "verify":
            return verify(argv[1], arg or default)
        if not arg:
            print(__doc__)
            return 2
        lock = lock_store(argv[1], "sign")
        if not lock:
            return 2
        return sign(argv[1], arg, default)
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
    lock = lock_store(argv[1], "ingest")
    if not lock:
        return 2
    s = Store(argv[1], opts["--fetched"], opts["--blobs"], opts["--tree"], offline)
    try:
        s.ingest(pins_on_main(opts["--runs"][-1]))
        s.sources()
    finally:
        s.write_index()  # what was stored before a crash is indexed all the same
    size = sum(n for _, _, n in s.added)
    missing = s.unfound()
    print(f"stored {len(s.added)} new blobs ({size / 1e6:.1f} MB); {len(missing)} referenced files not found:")
    for u, why in missing.items():
        print(f"  missing {u} ({why})")
    for u, why in s.unreadable:
        print(f"  unreadable {u} ({why})")
    if s.no_source:
        print(f"{len(s.no_source)} stored packages have no source stored yet (gaps to fill before going public):")
        for (u, _), why in sorted(s.no_source.items()):
            print(f"  no source {u} ({why})")
    return 1 if missing or s.unreadable else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
