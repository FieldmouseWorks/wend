"""Build a release's driver table (SPEC "Hardware preflight"): the PCI devices its kernel drives.

Maintainer tool. For each release it finds the kernel a hop installs (EL: `kernel`, from EL8 on `kernel-core` and
`kernel-modules`; Debian: what `linux-image-amd64` (lenny, squeeze: `linux-image-2.6-amd64`) depends on; Ubuntu: what
`linux-image-generic` depends on, extra modules included), downloads it from the release's mirror, checks it against
the sha256 the index lists, unpacks it and reads every storage and network module's `alias=pci:` lines straight from
the module's `.modinfo` section (no depmod, so any kernel's modules read the same). It writes
`adapters/drivers/<family>/<release>.txt` and prints the table's sha256 for `[hardware.<release>].drivers`.

  python3 tools/hwdata.py el alma9 alma10
  python3 tools/hwdata.py debian            # every release the ladder boots into (not the first, not may_skip)

Needs curl, rpm2cpio and cpio (EL), dpkg-deb (apt), xz and zstd. Downloads go to $WEND_WORK/hwdata (default
/var/tmp/wend/hwdata) and are reused when their hash still matches.
"""
import bz2
import gzip
import hashlib
import lzma
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import tomllib
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backends  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(os.environ.get("WEND_WORK", "/var/tmp/wend"), "hwdata")
# Where a kernel keeps the drivers that decide whether a machine boots and stays reachable: disk controllers
# (scsi, ata, nvme, block, md, message: the Fusion MPT drivers, ide) and network (net, and virtio's PCI transport).
DIRS = ("drivers/scsi/", "drivers/ata/", "drivers/nvme/", "drivers/block/", "drivers/md/", "drivers/message/",
        "drivers/ide/", "drivers/net/", "drivers/virtio/")
EL_KERNEL = {6: ["kernel"], 7: ["kernel"], 8: ["kernel-core", "kernel-modules"],
             9: ["kernel-core", "kernel-modules", "kernel-modules-core"],
             10: ["kernel-core", "kernel-modules", "kernel-modules-core"]}
RPM_NS = {"c": "http://linux.duke.edu/metadata/common", "r": "http://linux.duke.edu/metadata/repo"}


def get(url):
    return subprocess.run(["curl", "-fsSL", "--retry", "5", "--retry-all-errors", url], capture_output=True,
                          check=True).stdout


def unpack(name, data):
    if name.endswith(".gz"):
        return gzip.decompress(data)
    if name.endswith(".xz"):
        return lzma.decompress(data)
    if name.endswith(".bz2"):
        return bz2.decompress(data)
    if name.endswith(".zst"):
        return subprocess.run(["zstd", "-dc"], input=data, capture_output=True, check=True).stdout
    return data


def download(url, sha256):
    os.makedirs(WORK, exist_ok=True)
    path = os.path.join(WORK, sha256)
    if os.path.exists(path) and hashlib.sha256(open(path, "rb").read()).hexdigest() == sha256:
        return path
    subprocess.run(["curl", "-fsSL", "--retry", "5", "--retry-all-errors", "-o", f"{path}.part", url], check=True)
    got = hashlib.sha256(open(f"{path}.part", "rb").read()).hexdigest()
    if got != sha256:
        os.unlink(f"{path}.part")
        raise SystemExit(f"{url}: sha256 {got}, the index says {sha256}")
    os.rename(f"{path}.part", path)
    return path


def evr_key(v):
    return [(1, int(x), "") if x.isdigit() else (0, 0, x) for x in re.findall(r"\d+|[A-Za-z]+", v)]  # rpm: digits win


# ---------- finding the kernel ----------

def el_kernel(adapter, release):
    """[(name, url, sha256)] for the newest kernel across the release's repos, at its origin (not through the shim)."""
    rel = adapter["releases"][release]
    found = {}  # name -> (evr key, evr, url, sha256)
    for repo in rel["repos"].values():
        base = f"https://{repo}"
        md = ET.fromstring(get(f"{base}/repodata/repomd.xml"))
        loc = next(d.find("r:location", RPM_NS).get("href") for d in md.findall("r:data", RPM_NS)
                   if d.get("type") == "primary")
        primary = ET.fromstring(unpack(loc, get(f"{base}/{loc}")))
        for p in primary.findall("c:package", RPM_NS):
            name, arch = p.findtext("c:name", namespaces=RPM_NS), p.findtext("c:arch", namespaces=RPM_NS)
            if name not in EL_KERNEL[rel["major"]] or arch != "x86_64":
                continue
            v = p.find("c:version", RPM_NS)
            evr = f"{v.get('ver')}-{v.get('rel')}"
            cs = p.find("c:checksum", RPM_NS)
            if cs.get("type") != "sha256":
                continue
            url = f"{base}/{p.find('c:location', RPM_NS).get('href')}"
            if name not in found or evr_key(evr) > found[name][0]:
                found[name] = (evr_key(evr), evr, url, cs.text)
    evrs = {f[1] for f in found.values()}
    if set(found) != set(EL_KERNEL[rel["major"]]) or len(evrs) != 1:
        raise SystemExit(f"{release}: kernel packages {sorted(found)} at {sorted(evrs)}; want "
                         f"{EL_KERNEL[rel['major']]} at one version")
    return [(f"{n}-{f[1]}", f[2], f[3]) for n, f in sorted(found.items())]


def apt_index(mirror, release):
    """The release pocket's main/binary-amd64 Packages, as {name: fields}; the newest version wins."""
    base = f"{mirror}/dists/{release}/main/binary-amd64/Packages"
    for ext in (".xz", ".gz", ".bz2"):
        try:
            text = unpack(ext, get(base + ext)).decode("utf-8", "replace")
            break
        except Exception:
            continue
    else:
        raise SystemExit(f"no Packages index under {base}")
    pkgs = {}
    for stanza in text.split("\n\n"):
        f = dict(re.findall(r"^([A-Za-z0-9-]+): ?(.*)$", stanza, re.M))
        if "Package" in f:
            pkgs[f["Package"]] = f  # Packages lists one version per name in a release pocket
    return pkgs


def apt_kernel(adapter, release):
    """[(package, url, sha256)]: the flavour metapackage's kernel image and its module packages."""
    mirror = backends.Apt().hop_vars(adapter, release)["mirror"]
    pkgs = apt_index(mirror, release)
    if adapter["family"] == "ubuntu":
        meta = "linux-image-generic"
    else:
        meta = "linux-image-amd64" if "linux-image-amd64" in pkgs else "linux-image-2.6-amd64"
    want, seen, out = [meta], set(), []
    while want:
        n = want.pop()
        if n in seen or n not in pkgs:
            continue
        seen.add(n)
        f = pkgs[n]
        if re.match(r"linux-(image|modules)(-extra)?-[0-9]", n):
            out.append((f"{n}_{f['Version']}", f"{mirror}/{f['Filename']}", f["SHA256"]))
        for dep in re.split(r"[,|]", f.get("Depends", "")):
            d = dep.strip().split(" ")[0]
            if re.match(r"linux-(image|modules)(-extra)?-", d) and not d.startswith("linux-image-unsigned"):
                want.append(d)
    if not any(n.startswith("linux-image-") for n, _, _ in out):
        raise SystemExit(f"{release}: {meta} leads to no kernel image in {mirror}")
    return sorted(out)


# ---------- reading the modules ----------

def modinfo(ko):
    """The NUL-separated key=value strings in an ELF64 module's .modinfo section."""
    if ko[:4] != b"\x7fELF" or ko[4] != 2:
        return []
    shoff, = struct.unpack_from("<Q", ko, 0x28)
    shentsize, shnum, shstrndx = struct.unpack_from("<HHH", ko, 0x3A)
    sections = [struct.unpack_from("<IIQQQQ", ko, shoff + i * shentsize) for i in range(shnum)]
    strtab = sections[shstrndx]
    for name, _, _, _, off, size in sections:
        end = ko.index(b"\0", strtab[4] + name)
        if ko[strtab[4] + name:end] == b".modinfo":
            return [s.decode("ascii", "replace") for s in ko[off:off + size].split(b"\0") if s]
    return []


def extract(path, into):
    if path.endswith(".rpm") or open(path, "rb").read(4) == b"\xed\xab\xee\xdb":
        subprocess.run(f"rpm2cpio '{path}' | cpio -idm --quiet", shell=True, cwd=into, check=True)
    else:
        subprocess.run(["dpkg-deb", "-x", path, into], check=True)


def table(pkgs):
    """(kernel version, sorted lines) from the unpacked packages."""
    os.makedirs(WORK, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=WORK) as tmp:
        for _, url, sha in pkgs:
            extract(download(url, sha), tmp)
        mods = [d for base in ("lib/modules", "usr/lib/modules") if os.path.isdir(f"{tmp}/{base}")
                for d in [os.path.join(tmp, base, v) for v in os.listdir(f"{tmp}/{base}")] if os.path.isdir(f"{d}/kernel")]
        if len(mods) != 1:
            raise SystemExit(f"expected one kernel's modules, found {mods}")
        top = mods[0]
        lines = set()
        for dirpath, _, files in os.walk(f"{top}/kernel"):
            for fn in files:
                m = re.fullmatch(r"(.+)\.ko(\.xz|\.zst|\.gz)?", fn)
                rel = os.path.relpath(os.path.join(dirpath, fn), f"{top}/kernel") + "/"
                if not m or not rel.startswith(DIRS):
                    continue
                name = m.group(1).replace("-", "_")
                for kv in modinfo(unpack(fn, open(os.path.join(dirpath, fn), "rb").read())):
                    if kv.startswith("alias=pci:"):
                        lines.add(f"{name} {kv[6:]}")
        # Built-in drivers have no .ko. Their aliases are in modules.builtin.modinfo (Linux 5.2 on); before that only
        # their names are known, and preflight takes a device whose driver is built in as driven.
        if os.path.exists(f"{top}/modules.builtin.modinfo"):
            for kv in open(f"{top}/modules.builtin.modinfo", "rb").read().split(b"\0"):
                s = kv.decode("ascii", "replace")
                mod, _, rest = s.partition(".")
                if rest.startswith("alias=pci:"):
                    lines.add(f"{mod.replace('-', '_')} {rest[6:]}")
        if os.path.exists(f"{top}/modules.builtin"):
            for p in open(f"{top}/modules.builtin").read().split():
                p = p.removeprefix("kernel/")
                if p.startswith(DIRS):
                    lines.add(f"builtin {os.path.basename(p).split('.ko')[0].replace('-', '_')}")
        return os.path.basename(top), sorted(lines)


def releases(adapter):
    """Every release a route can boot into: the ladder after its first release, less may_skip."""
    return [r for r in adapter["ladder"][1:] if r not in adapter.get("may_skip", [])]


def main(argv):
    if not argv:
        raise SystemExit(__doc__)
    fam = argv[0]
    adapter = tomllib.load(open(f"{ROOT}/adapters/{fam}.toml", "rb"))
    for release in argv[1:] or releases(adapter):
        pkgs = el_kernel(adapter, release) if adapter["backend"] == "rpm" else apt_kernel(adapter, release)
        version, lines = table(pkgs)
        out = f"{ROOT}/adapters/drivers/{fam}/{release}.txt"
        os.makedirs(os.path.dirname(out), exist_ok=True)
        head = [f"# wend driver table (SPEC \"Hardware preflight\"): the PCI storage and network devices {release}'s "
                f"kernel {version} drives.",
                "# Generated by tools/hwdata.py from these packages, checked against the release index; don't edit."]
        head += [f"# {n} sha256:{s} {u}" for n, u, s in pkgs]
        body = ("\n".join(head + lines) + "\n").encode()
        with open(out, "wb") as f:
            f.write(body)
        print(f"{fam} {release}: {version}, {len(lines)} lines, drivers = \"sha256:{hashlib.sha256(body).hexdigest()}\"",
              flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
