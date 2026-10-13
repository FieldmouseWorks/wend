"""Build each release's runtime table (SPEC "Runtime preflight"): the Python 2 interpreters the release ships.

Maintainer tool. For every release a route can boot into it reads the release's own package index, the pockets and
components the hop itself uses (Debian: main; Ubuntu: main and universe, where Python 2 moved at focal; EL: the
release's repos, modular packages included), and lists the upstream version of every Python 2 interpreter in it:
Debian and Ubuntu's `python2.N`, EL's `python` (EL5 to EL7) and `python2` (EL8's python27 module). It prints the
`[runtimes.<release>]` tables for the adapter; paste them in. No release index, no table: it stops rather than
writing "none" for a release it couldn't read.

  python3 tools/runtimes.py el
  python3 tools/runtimes.py ubuntu jammy noble

Add-on repositories (EPEL) aren't read: a Python 2 from an add-on is a package the route carries or refuses on its
own terms (SPEC "Add-on repositories").
"""
import os
import re
import sys
import tomllib
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backends  # noqa: E402
import hwdata  # noqa: E402

ROOT = hwdata.ROOT
NS = hwdata.RPM_NS
# the components each apt family's hop puts in sources.list (adapters' hop.set_release)
COMPONENTS = {"debian": ["main"], "ubuntu": ["main", "universe"]}


def upstream(version):
    """The upstream part of a package version: 2.7.18-8+deb11u1 -> 2.7.18, 2.7.15~rc1-1 -> 2.7.15."""
    return re.match(r"(?:\d+:)?(\d+(?:\.\d+)*)", version).group(1)


def apt_python2(adapter, release):
    mirror = backends.Apt().hop_vars(adapter, release)["mirror"]
    found = set()
    for comp in COMPONENTS[adapter["family"]]:
        base = f"{mirror}/dists/{release}/{comp}/binary-amd64/Packages"
        for ext in (".xz", ".gz", ".bz2"):
            try:
                text = hwdata.unpack(ext, hwdata.get(base + ext)).decode("utf-8", "replace")
                break
            except Exception:
                continue
        else:
            raise SystemExit(f"no Packages index under {base}")
        for stanza in text.split("\n\n"):
            f = dict(re.findall(r"^([A-Za-z0-9-]+): ?(.*)$", stanza, re.M))
            if re.fullmatch(r"python2\.\d+", f.get("Package", "")):
                found.add(upstream(f["Version"]))
    return found


def el_python2(adapter, release):
    found = set()
    for repo in adapter["releases"][release]["repos"].values():
        base = f"https://{repo}"
        md = ET.fromstring(hwdata.get(f"{base}/repodata/repomd.xml"))
        loc = next(d.find("r:location", NS).get("href") for d in md.findall("r:data", NS)
                   if d.get("type") == "primary")
        for p in ET.fromstring(hwdata.unpack(loc, hwdata.get(f"{base}/{loc}"))).findall("c:package", NS):
            name, arch = p.findtext("c:name", namespaces=NS), p.findtext("c:arch", namespaces=NS)
            ver = p.find("c:version", NS).get("ver")
            if name in ("python", "python2") and arch == "x86_64" and ver.startswith("2."):
                found.add(upstream(ver))
    return found


def key(v):
    return [int(x) for x in v.split(".")]


def main(argv):
    if not argv:
        raise SystemExit(__doc__)
    fam = argv[0]
    adapter = tomllib.load(open(f"{ROOT}/adapters/{fam}.toml", "rb"))
    for release in argv[1:] or hwdata.releases(adapter):
        got = el_python2(adapter, release) if adapter["backend"] == "rpm" else apt_python2(adapter, release)
        # the newest of each minor: a pocket lists one version per package, but EL's updates repo lists every build
        newest = {}
        for v in sorted(got, key=key):
            newest[".".join(v.split(".")[:2])] = v
        vals = ", ".join(f'"{v}"' for v in sorted(newest.values(), key=key))
        print(f"[runtimes.{release}]\npython2 = [{vals}]\n", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
