"""Build a release's driver table (SPEC "Hardware preflight"): the PCI devices its kernel drives and the filesystems
it mounts.

Maintainer tool. For each release it finds the kernel a hop installs (EL: `kernel`, from EL8 on `kernel-core` and
`kernel-modules`; Debian: what `linux-image-amd64` (lenny, squeeze: `linux-image-2.6-amd64`) depends on; Ubuntu: what
`linux-image-generic` depends on, extra modules included), downloads it from the release's mirror, checks it against
the sha256 the index lists, unpacks it and reads every storage and network module's `alias=pci:` lines straight from
the module's `.modinfo` section (no depmod, so any kernel's modules read the same), and which filesystems it mounts
from the kernel's own build config (`fs` lines). It writes `adapters/drivers/<family>/<release>.txt` and prints the
table's sha256 for `[hardware.<release>].drivers`.

  python3 tools/hwdata.py el alma9 alma10
  python3 tools/hwdata.py debian            # every release the ladder boots into (not the first, not may_skip)
  python3 tools/hwdata.py --pinned ubuntu   # rebuild from the packages the tables already name, not the newest kernel

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
EL_KERNEL = {5: ["kernel"], 6: ["kernel"], 7: ["kernel"], 8: ["kernel-core", "kernel-modules"],
             9: ["kernel-core", "kernel-modules", "kernel-modules-core"],
             10: ["kernel-core", "kernel-modules", "kernel-modules-core"]}
# The filesystems a machine's disks may carry, with the config symbol and module that mount each (SPEC "Hardware
# preflight"). ext2 and ext3 can also be mounted by ext4 (below); `xfs-v4` is XFS's old on-disk format (crc=0), which
# CONFIG_XFS_SUPPORT_V4 (Linux 5.10 on) can leave out.
FILESYSTEMS = {"ext2": ("EXT2_FS", "fs/ext2/ext2"), "ext3": ("EXT3_FS", "fs/ext3/ext3"),
               "ext4": ("EXT4_FS", "fs/ext4/ext4"), "xfs": ("XFS_FS", "fs/xfs/xfs"),
               "btrfs": ("BTRFS_FS", "fs/btrfs/btrfs"), "reiserfs": ("REISERFS_FS", "fs/reiserfs/reiserfs"),
               "jfs": ("JFS_FS", "fs/jfs/jfs")}
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
    found = {}  # name -> (evr key, evr, url, digest, digest type)
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
            if cs.get("type") not in ("sha256", "sha", "sha1"):
                continue
            url = f"{base}/{p.find('c:location', RPM_NS).get('href')}"
            if name not in found or evr_key(evr) > found[name][0]:
                found[name] = (evr_key(evr), evr, url, cs.text, cs.get("type"))
    evrs = {f[1] for f in found.values()}
    if set(found) != set(EL_KERNEL[rel["major"]]) or len(evrs) != 1:
        raise SystemExit(f"{release}: kernel packages {sorted(found)} at {sorted(evrs)}; want "
                         f"{EL_KERNEL[rel['major']]} at one version")
    return [(f"{n}-{f[1]}", f[2], f[3] if f[4] == "sha256" else via_sha1(f[2], f[3])) for n, f in sorted(found.items())]


def via_sha1(url, sha1):
    """EL5's index lists only SHA-1: download, check that, and name the file by its sha256 from then on."""
    os.makedirs(WORK, exist_ok=True)
    part = os.path.join(WORK, f"sha1-{sha1}.part")
    subprocess.run(["curl", "-fsSL", "--retry", "5", "--retry-all-errors", "-o", part, url], check=True)
    data = open(part, "rb").read()
    if hashlib.sha1(data).hexdigest() != sha1:
        os.unlink(part)
        raise SystemExit(f"{url}: sha1 {hashlib.sha1(data).hexdigest()}, the index says {sha1}")
    sha256 = hashlib.sha256(data).hexdigest()
    os.rename(part, os.path.join(WORK, sha256))
    return sha256


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
    mirror = backends.Apt().origin(adapter, release)
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
        # Built-in drivers have no .ko. modules.builtin.modinfo (Linux 5.2 on) carries their aliases, device tables
        # included only on newer kernels (EL9's built-in virtio_pci has none there). So a built-in driver is listed by
        # name (`builtin <module>`) with whatever aliases the kernel recorded, and preflight matches it by name only
        # when it has none.
        builtin = set()
        if os.path.exists(f"{top}/modules.builtin"):
            for p in open(f"{top}/modules.builtin").read().split():
                p = p.removeprefix("kernel/")
                if p.startswith(DIRS):
                    builtin.add(os.path.basename(p).split(".ko")[0].replace("-", "_"))
        if os.path.exists(f"{top}/modules.builtin.modinfo"):
            for kv in open(f"{top}/modules.builtin.modinfo", "rb").read().split(b"\0"):
                mod, _, rest = kv.decode("ascii", "replace").partition(".")
                if mod.replace("-", "_") in builtin and rest.startswith("alias=pci:"):
                    lines.add(f"{mod.replace('-', '_')} {rest[6:]}")
        lines.update(f"builtin {m}" for m in builtin)
        lines.update(f"fs {f}" for f in filesystems(tmp, top))
        return os.path.basename(top), sorted(lines)


def filesystems(tmp, top):
    """The filesystems the kernel mounts, from its build config (/boot/config-<version>, or <modules>/config from EL9
    on): built in, or a module the packages carry."""
    version = os.path.basename(top)
    path = next((p for p in (f"{tmp}/boot/config-{version}", f"{top}/config") if os.path.exists(p)), None)
    if path is None:
        raise SystemExit(f"{version}: no kernel config in the packages")
    cfg = {}
    for line in open(path):
        if m := re.fullmatch(r"CONFIG_(\w+)=(.*)\n?", line):
            cfg[m.group(1)] = m.group(2)
        elif m := re.fullmatch(r"# CONFIG_(\w+) is not set\n?", line):
            cfg[m.group(1)] = "n"
    have = {}
    for fs, (sym, mod) in FILESYSTEMS.items():
        ko = any(os.path.exists(f"{top}/kernel/{mod}.ko{z}") for z in ("", ".xz", ".zst", ".gz"))
        have[fs] = cfg.get(sym) == "y" or (cfg.get(sym) == "m" and ko)
    # ext4 mounts ext2 and ext3 too when built to (EXT4_USE_FOR_EXT23 to Linux 4.2, EXT4_USE_FOR_EXT2 after); from
    # 4.3 on, ext3's own driver is gone and ext4 always mounts ext3
    major, minor = (int(x) for x in re.match(r"(\d+)\.(\d+)", version).groups())
    for_23 = cfg.get("EXT4_USE_FOR_EXT23") == "y"
    have["ext2"] |= have["ext4"] and (for_23 or cfg.get("EXT4_USE_FOR_EXT2") == "y")
    have["ext3"] |= have["ext4"] and (for_23 or (major, minor) >= (4, 3))
    # before 5.10 every XFS mounted v4; from 5.10 the kernel's config says
    have["xfs-v4"] = have["xfs"] and cfg.get("XFS_SUPPORT_V4", "y") == "y"
    return sorted(f for f, ok in have.items() if ok)


def pinned(fam, release):
    """[(package, url, sha256)] the release's current table was built from."""
    out = []
    for line in open(f"{ROOT}/adapters/drivers/{fam}/{release}.txt"):
        if m := re.fullmatch(r"# (\S+) sha256:([0-9a-f]{64}) (\S+)\n", line):
            out.append((m.group(1), m.group(3), m.group(2)))
    if not out:
        raise SystemExit(f"adapters/drivers/{fam}/{release}.txt names no packages")
    return out


def releases(adapter):
    """Every release a route can boot into: the ladder after its first release, less may_skip."""
    return [r for r in adapter["ladder"][1:] if r not in adapter.get("may_skip", [])]


def main(argv):
    use_pinned = "--pinned" in argv
    argv = [a for a in argv if a != "--pinned"]
    if not argv:
        raise SystemExit(__doc__)
    fam = argv[0]
    adapter = tomllib.load(open(f"{ROOT}/adapters/{fam}.toml", "rb"))
    for release in argv[1:] or releases(adapter):
        if use_pinned:
            pkgs = pinned(fam, release)
        else:
            pkgs = el_kernel(adapter, release) if adapter["backend"] == "rpm" else apt_kernel(adapter, release)
        version, lines = table(pkgs)
        out = f"{ROOT}/adapters/drivers/{fam}/{release}.txt"
        os.makedirs(os.path.dirname(out), exist_ok=True)
        head = [f"# wend driver table (SPEC \"Hardware preflight\"): the PCI storage and network devices {release}'s "
                f"kernel {version} drives, and the filesystems it mounts.",
                "# Generated by tools/hwdata.py from these packages, checked against the release index; don't edit."]
        head += [f"# {n} sha256:{s} {u}" for n, u, s in pkgs]
        body = ("\n".join(head + lines) + "\n").encode()
        with open(out, "wb") as f:
            f.write(body)
        print(f"{fam} {release}: {version}, {len(lines)} lines, drivers = \"sha256:{hashlib.sha256(body).hexdigest()}\"",
              flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
