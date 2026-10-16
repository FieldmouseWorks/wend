"""Build each release's library table (SPEC "Runtime preflight"): the 64-bit shared libraries its packages ship.

Maintainer tool. A machine's own native programs (anything in /opt or /usr/local, a hand build, another package
manager's files) keep running after a hop only if every shared library they load is still there. The route carries
packaged libraries to their new versions; a library whose soname no package of the new release ships is gone. This
lists, for every release a route can boot into, the sonames its packages ship, read from the release's own indexes
and never from documentation:

- EL: every `(64bit)` soname a package in the release's repos provides (primary.xml). Each add-on repository the
  adapter names for that release (`[addons.<name>].repos`) gets a table of its own, `<release>.<name>`, pinned as
  `[addons.<name>].libs.<release>`: a hop enables an add-on only where its marker package is installed.
- Debian and Ubuntu: every file under the linker's own directories (`/lib`, `/usr/lib`, `/lib64`, `/usr/lib64` and
  their `x86_64-linux-gnu`) named like a shared library (`libfoo.so.1`, `ld-linux-x86-64.so.2`), from the release's
  Contents index, in the components its hop uses (Debian: main; Ubuntu: main and universe).

The tables aren't kept in git (hundreds of thousands of lines). It writes each one xz-compressed to
`.wend/sonames/<family>/<release>.txt.xz` (or under --out DIR) and prints that file's sha256 for
`[runtimes.<release>].libs`: the pin, and the file's address in the wend archive (blob/sha256/<2 hex>/<hex>), where
remi publishes it (`tools/archive.py add-tables`). The release tarball carries them as adapters/sonames/; a checkout
fetches them into .wend/sonames/ with `fetch`, each checked against its pin.

  python3 tools/sonames.py el
  python3 tools/sonames.py debian squeeze wheezy --out DIR
  python3 tools/sonames.py fetch [--into DIR] [--from URL]
"""
import glob
import gzip
import hashlib
import lzma
import os
import re
import sys
import tomllib
import urllib.request
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backends  # noqa: E402
import hwdata  # noqa: E402

ROOT = hwdata.ROOT
NS = hwdata.RPM_NS
RPM = "http://linux.duke.edu/metadata/rpm"
ARCHIVE = "https://archive.fieldmouseworks.com"
COMPONENTS = {"debian": ["main"], "ubuntu": ["main", "universe"]}
SONAME = re.compile(r"(?:lib[^/\s]*|ld-linux[^/\s]*)\.so(?:\.[0-9][^/\s]*)?")
LIBDIR = re.compile(r"(?:usr/)?lib(?:64)?(?:/x86_64-linux-gnu)?/")


def el_sonames(adapter, release, addon=None):
    """(sonames, [(url, sha256)]) from the release's own repos, or from one add-on's repo for it (`addon`)."""
    if addon:
        repos = [adapter["addons"][addon]["repos"][release]]
    else:
        repos = list(adapter["releases"][release]["repos"].values())
    found, read = set(), []
    for repo in repos:
        base = f"https://{repo}"
        md = ET.fromstring(hwdata.get(f"{base}/repodata/repomd.xml"))
        loc = next(d.find("r:location", NS).get("href") for d in md.findall("r:data", NS)
                   if d.get("type") == "primary")
        raw = hwdata.get(f"{base}/{loc}")
        read.append((f"{base}/{loc}", hashlib.sha256(raw).hexdigest()))
        for p in ET.fromstring(hwdata.unpack(loc, raw)).findall("c:package", NS):
            if p.findtext("c:arch", namespaces=NS) not in ("x86_64", "noarch"):
                continue
            for e in p.findall("c:format/rpm:provides/rpm:entry", {**NS, "rpm": RPM}):
                name = e.get("name", "")
                if name.endswith("(64bit)"):
                    so = name.split("(", 1)[0]
                    if SONAME.fullmatch(so):
                        found.add(so)
    return found, read


def apt_sonames(adapter, release):
    """(sonames, [(url, sha256)]) from the release's Contents index: per component (squeeze on, on Debian) or one for
    the whole pocket (etch, lenny, and Ubuntu, whose lines name the component as a section prefix)."""
    mirror = backends.Apt().origin(adapter, release)
    comps = COMPONENTS[adapter["family"]]
    found, read = set(), []

    def fetch(url):
        raw = hwdata.get(url)
        read.append((url, hashlib.sha256(raw).hexdigest()))
        return gzip.decompress(raw).decode("utf-8", "replace")

    texts = []
    for c in comps:
        try:
            texts.append((fetch(f"{mirror}/dists/{release}/{c}/Contents-amd64.gz"), None))
        except Exception:
            break
    if len(texts) != len(comps):
        texts = [(fetch(f"{mirror}/dists/{release}/Contents-amd64.gz"), comps)]
    for text, want in texts:
        for line in text.splitlines():
            path, _, where = line.rpartition(" ")
            path = path.strip()
            if not LIBDIR.match(path):
                continue
            rest = path[LIBDIR.match(path).end():]
            if "/" in rest or not SONAME.fullmatch(rest):
                continue
            if want is not None:
                # each package is component/section/name ("libs/libc6", "universe/libs/x"); main has no prefix
                comps_here = {w.split("/")[0] if w.count("/") >= 2 else "main" for w in where.split(",")}
                if not comps_here & set(want):
                    continue
            found.add(rest)
    return found, read


def adapter_pins(a):
    """[(table, field, "sha256:<hex>")] for the library tables one adapter pins: each release's own
    (`runtimes.<release>.libs`, table `<release>`) and each add-on's for a release (`addons.<name>.libs.<release>`,
    table `<release>.<name>`)."""
    out = []
    for r, t in (a.get("runtimes") if isinstance(a.get("runtimes"), dict) else {}).items():
        if isinstance(t, dict) and isinstance(t.get("libs"), str):
            out.append((r, f"runtimes.{r}.libs", t["libs"]))
    for n, ad in (a.get("addons") if isinstance(a.get("addons"), dict) else {}).items():
        libs = ad.get("libs") if isinstance(ad, dict) else None
        for r, pin in (libs if isinstance(libs, dict) else {}).items():
            if isinstance(pin, str):
                out.append((f"{r}.{n}", f"addons.{n}.libs.{r}", pin))
    return out


def pins():
    """[(family, table, "sha256:<hex>")] for every library table the adapters pin."""
    out = []
    for p in sorted(glob.glob(f"{ROOT}/adapters/*.toml")):
        a = tomllib.load(open(p, "rb"))
        out += [(a["family"], t, pin) for t, _, pin in adapter_pins(a)]
    return out


def fetch(into, base):
    """Each pinned table from the archive into INTO/<family>/<release>.txt.xz, checked against its pin. A file already
    there at its pin is kept. Exits 1 when one couldn't be had."""
    bad = 0
    for fam, r, pin in pins():
        hexd = pin.split(":", 1)[1]
        out = f"{into}/{fam}/{r}.txt.xz"
        if os.path.isfile(out) and hashlib.sha256(open(out, "rb").read()).hexdigest() == hexd:
            continue
        url = f"{base}/blob/sha256/{hexd[:2]}/{hexd}"
        try:
            with urllib.request.urlopen(url, timeout=120) as resp:
                data = resp.read()
        except Exception as e:
            print(f"{fam} {r}: {url}: {e}", file=sys.stderr)
            bad += 1
            continue
        if hashlib.sha256(data).hexdigest() != hexd:
            print(f"{fam} {r}: {url} isn't the pinned table", file=sys.stderr)
            bad += 1
            continue
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out + ".tmp", "wb") as f:
            f.write(data)
        os.replace(out + ".tmp", out)
        print(f"{fam} {r}: {out}")
    return 1 if bad else 0


def main(argv):
    if not argv:
        raise SystemExit(__doc__)
    if argv[0] == "fetch":
        opts = {"--into": f"{ROOT}/.wend/sonames", "--from": ARCHIVE}
        rest = argv[1:]
        while rest:
            a = rest.pop(0)
            if a not in opts or not rest:
                raise SystemExit(__doc__)
            opts[a] = rest.pop(0)
        return fetch(opts["--into"], opts["--from"].rstrip("/"))
    out_dir = f"{ROOT}/.wend/sonames"
    if "--out" in argv:
        i = argv.index("--out")
        out_dir = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    fam = argv[0]
    adapter = tomllib.load(open(f"{ROOT}/adapters/{fam}.toml", "rb"))
    for release in argv[1:] or hwdata.releases(adapter):
        # the release's own repos, then each add-on's repo for it, each its own table: a hop enables an add-on only on
        # a machine with its marker package, so preflight adds an add-on's sonames only for such a machine
        jobs = [(None, release, f"runtimes.{release}.libs")]
        if adapter["backend"] == "rpm":
            jobs += [(n, f"{release}.{n}", f"addons.{n}.libs.{release}")
                     for n, a in sorted(adapter.get("addons", {}).items()) if release in a.get("repos", {})]
        for addon, table, field in jobs:
            found, read = (el_sonames(adapter, release, addon) if adapter["backend"] == "rpm"
                           else apt_sonames(adapter, release))
            if not found:
                raise SystemExit(f"{fam} {table}: no sonames read; not writing an empty table")
            what = f"{release}'s packages" if addon is None else f"the {addon} add-on's packages for {release}"
            head = [f"# wend library table (SPEC \"Runtime preflight\"): the 64-bit shared libraries {what} ship, by "
                    f"soname.",
                    "# Generated by tools/sonames.py from these indexes; don't edit."]
            head += [f"# {u} sha256:{s}" for u, s in read]
            body = ("\n".join(head + sorted(found)) + "\n").encode()
            # the same bytes every time: no file name or time in an xz stream, one fixed preset and check
            xz = lzma.compress(body, format=lzma.FORMAT_XZ, check=lzma.CHECK_CRC64, preset=9 | lzma.PRESET_EXTREME)
            out = f"{out_dir}/{fam}/{table}.txt.xz"
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as f:
                f.write(xz)
            print(f"{fam} {table}: {len(found)} sonames, {field} = \"sha256:{hashlib.sha256(xz).hexdigest()}\"",
                  flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
