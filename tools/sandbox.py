"""The sandbox: a box is a whole root filesystem in a chroot, snapshotted with rsync, rolled back by copying back.
Generalized from dep-lab's oldlinux/ol.py and centos/co.py. Needs root, chroot and rsync on the host, plus debootstrap
for Debian boxes, or rpm2cpio, cpio and ip for EL boxes. unshare (util-linux) for every box.

Follows SPEC.md "Sandbox requirements": a private tmpfs /dev per box (never a bind of the host's), fresh proc/sys,
a hostname of the box's own (never the host's), daemons never start, and nothing is deleted until every mount under
it is gone.
"""
import gzip
import hashlib
import os
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

import backends

WORK = os.environ.get("WEND_WORK", "/var/tmp/wend")
ENV = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "HOME": "/root",
       "DEBIAN_FRONTEND": "noninteractive", "TERM": "dumb"}
# Every command runs in a UTS namespace of its own, named by the root's own /etc/hostname (or EL's
# /etc/sysconfig/network), else HOSTNAME. Read inside the chroot, so a symlink there can't point at the host. Without
# it a box takes the host's name, and a build depends on where it runs: etch's postfix postinst turns a bare host name
# plus resolv.conf's "search ." into "name..", which newaliases refuses (wend#26). Same string in client/src/sandbox.rs.
HOSTNAME = "wend-box.example.org"
NAME_SELF = ("n=$(head -n 1 /etc/hostname 2>/dev/null | tr -d ' \\t\\r'); [ -n \"$n\" ] || "
             "n=$(sed -n 's/^HOSTNAME=//p' /etc/sysconfig/network 2>/dev/null | tail -n 1 | tr -d \"\\\"' \"); "
             f"case \"$n\" in ''|[!A-Za-z0-9]*|*[!A-Za-z0-9.-]*|*.|*..*) n={HOSTNAME};; esac; "
             f"printf '%s\\n' \"$n\" >/proc/sys/kernel/hostname 2>/dev/null || echo {HOSTNAME} >/proc/sys/kernel/hostname; "
             'exec /bin/sh -c "$1"')


def host(cmd, timeout=3600):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, errors="replace")
    return p.returncode, p.stdout + p.stderr


def tail(s, n):
    return s if len(s) <= n else "..." + s[-n:]


def mounts_under(path):
    path = os.path.realpath(path)
    return [m.split()[1] for m in open("/proc/mounts") if m.split()[1] == path or m.split()[1].startswith(path + "/")]


def unmount_all(path):
    """Unmount everything under path, deepest first, and refuse to go on if anything is still mounted."""
    for _ in range(3):
        left = mounts_under(path)
        if not left:
            return
        for m in sorted(left, key=len, reverse=True):
            host(f"umount {m} 2>/dev/null || umount -l {m}")
    if left := mounts_under(path):
        raise RuntimeError(f"still mounted under {path}: {left}; refusing to touch it")


def safe_rm(path):
    if os.path.realpath(path) in ("/", os.path.realpath(WORK)) or not os.path.realpath(path).startswith(
            os.path.realpath(WORK) + "/"):
        raise RuntimeError(f"refusing to delete {path}: not inside {WORK}")
    unmount_all(path)
    host(f"rm -rf --one-file-system {path}")


class Box:
    def __init__(self, root):
        self.root = root

    def _mount(self):
        r = self.root
        host(f"mount -t proc proc {r}/proc; mount -t sysfs sys {r}/sys; mount -t tmpfs -o mode=755,size=1m dev {r}/dev; "
             f"cd {r}/dev && mknod -m 666 null c 1 3 && mknod -m 666 zero c 1 5 && mknod -m 666 full c 1 7 && "
             f"mknod -m 666 random c 1 8 && mknod -m 666 urandom c 1 9 && mknod -m 666 tty c 5 0 && "
             f"mkdir pts shm && ln -s /proc/self/fd fd && ln -s fd/0 stdin && ln -s fd/1 stdout && ln -s fd/2 stderr; "
             f"mount -t devpts -o newinstance,ptmxmode=0666 devpts {r}/dev/pts && ln -sf pts/ptmx {r}/dev/ptmx; "
             f"cp /etc/resolv.conf {r}/etc/resolv.conf 2>/dev/null")

    def _umount(self):
        r = os.path.realpath(self.root)
        for pid in os.listdir("/proc"):  # stray daemons a maintainer script started anyway
            try:
                if pid.isdigit() and os.readlink(f"/proc/{pid}/root") == r:
                    os.kill(int(pid), 9)
            except OSError:
                pass
        unmount_all(r)

    def run(self, cmd, timeout=1800, isolated=False):
        """isolated: a network namespace of its own with only loopback, for checks that start daemons."""
        self._mount()
        argv = ["chroot", self.root, "/bin/sh", "-c", NAME_SELF, "x", cmd]
        if isolated:
            argv = ["unshare", "-u", "-n", "sh", "-c", 'ip link set lo up && exec "$@"', "x"] + argv
        else:
            argv = ["unshare", "-u"] + argv
        try:
            p = subprocess.run(argv, env=ENV, capture_output=True, text=True, timeout=timeout, errors="replace")
            return p.returncode, p.stdout + p.stderr
        except subprocess.TimeoutExpired as e:
            return 124, f"TIMEOUT after {timeout}s\n" + (e.stdout or b"").decode(errors="replace")[-3000:]
        finally:
            self._umount()

    def copy_to(self, dst):
        # --modify-window=-1: compare mtimes to the nanosecond. EL6's rpm --rebuilddb rewrites some index files with
        # the same size and the old mtime truncated to the second, and the default quick check then skipped them on
        # rollback, leaving EL5's rpm with a database it can't read.
        unmount_all(self.root)
        rc, log = host(f"mkdir -p {dst} && rsync -aHx --modify-window=-1 --delete {self.root}/ {dst}/")
        if rc:
            raise RuntimeError(f"copying {self.root} to {dst} failed (is rsync installed?):\n{tail(log, 800)}")
        return Box(dst)

    def health(self, checks, release_file, isolate=()):
        out = {}
        for name, test in checks.items():
            rc, log = self.run(f"( set -x; {test} ) >/tmp/.h 2>&1; rc=$?; tail -c 1200 /tmp/.h; exit $rc", timeout=180,
                               isolated=name in isolate)
            out[name] = {"ok": rc == 0, "log": "" if rc == 0 else log}
        v = self.run(f"cat {release_file}")[1].strip()
        return {"release": v, "checks": out, "passed": sorted(c for c in out if out[c]["ok"])}


def checks_for(box_doc, adapter):
    """Adapter checks that are shell commands, plus the box's own roles. Box roles win on a name clash."""
    shell = {k: v for k, v in adapter["checks"].items() if k in box_doc.get("adapter_checks", ["pkgdb"])}
    return {**shell, **box_doc["roles"]}


def mirror(adapter, release):
    return backends.Apt().hop_vars(adapter, release)["mirror"]


def ensure_mirror(adapter):
    """Adapters with a [mirror] shim (EL: CentOS 5's yum can't speak today's TLS) get tools/mirror.py on the host."""
    if "mirror" not in adapter:
        return
    shim = adapter["mirror"]["shim"]
    probe = f"{shim}/{adapter['mirror']['probe']}"
    for i in range(30):
        try:
            urllib.request.urlopen(probe, timeout=60).read()
            return
        except Exception:
            if i == 0:
                os.makedirs(WORK, exist_ok=True)
                subprocess.Popen([sys.executable, "-B", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                                     "mirror.py"), shim.rsplit(":", 1)[1],
                                  f"{WORK}/mirror"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
            time.sleep(2)
    raise RuntimeError(f"the mirror shim at {shim} isn't answering {probe}")


def build_debian(root, box_doc, adapter):
    rel = box_doc["release"]
    script = adapter.get("box", {}).get("debootstrap_script", "")  # Ubuntu: see adapters/ubuntu.toml
    if script and not script.isalnum():
        raise ValueError(f"debootstrap_script must be a bare script name, not {script!r}")
    script = f" /usr/share/debootstrap/scripts/{script}" if script else ""
    rc, log = host(f"debootstrap --no-check-gpg --arch={box_doc.get('arch', 'amd64')} --variant=minbase {rel} {root} "
                   f"{mirror(adapter, rel)}{script}")
    if rc:
        raise RuntimeError("debootstrap failed:\n" + tail(log, 2000))
    with open(f"{root}/usr/sbin/policy-rc.d", "w") as f:
        f.write("#!/bin/sh\nexit 101\n")  # never start daemons inside the sandbox
    os.chmod(f"{root}/usr/sbin/policy-rc.d", 0o755)
    with open(f"{root}/etc/hostname", "w") as f:
        f.write(HOSTNAME + "\n")  # debootstrap copied the host's
    if not os.path.exists(f"{root}/etc/hosts"):
        with open(f"{root}/etc/hosts", "w") as f:
            f.write(f"127.0.0.1\tlocalhost\n127.0.1.1\t{HOSTNAME} {HOSTNAME.split('.')[0]}\n")
    with open(f"{root}/etc/apt/apt.conf.d/00wend", "w") as f:
        f.write(adapter["hop"]["apt_conf"])
    b = Box(root)
    rc, log = b.run(backends.fill(adapter["hop"]["set_release"], backends.Apt().hop_vars(adapter, rel)) + " && " +
                    "".join(f"echo '{d}' | debconf-set-selections && " for d in box_doc.get("debconf_selections", [])) +
                    "apt-get update && apt-get install " + " ".join(box_doc["packages"]))
    if rc:
        raise RuntimeError("package install failed:\n" + tail(log, 2000))
    rc, log = b.run(box_doc["customize"] + "\n" + box_doc.get("start_hook", ""))
    if rc:
        raise RuntimeError("customize failed:\n" + tail(log, 2000))


def resolve(base_url, seeds):
    """Close a seed set over Requires using the repo's primary metadata: a tiny depsolver, just for the bootstrap."""
    ns = {"c": "http://linux.duke.edu/metadata/common", "r": "http://linux.duke.edu/metadata/rpm"}
    repomd = urllib.request.urlopen(base_url + "repodata/repomd.xml", timeout=300).read().decode()
    href = next(x.split('"')[1] for x in repomd.split("<location ")[1:] if "primary.xml.gz" in x.split('"')[1])
    root = ET.fromstring(gzip.decompress(urllib.request.urlopen(base_url + href, timeout=300).read()))
    pk, prov = {}, {}
    for p in root.findall("c:package", ns):
        if p.find("c:arch", ns).text not in ("x86_64", "noarch"):
            continue
        n, f = p.find("c:name", ns).text, p.find("c:format", ns)
        pk[n] = {"href": p.find("c:location", ns).get("href"),
                 "req": [e.get("name") for e in f.findall("r:requires/r:entry", ns) if not e.get("name").startswith("rpmlib(")]}
        for name in [n] + [e.get("name") for e in f.findall("r:provides/r:entry", ns)] + [e.text for e in f.findall("c:file", ns)]:
            prov.setdefault(name, []).append(n)
    # a seed may be a name another package provides (centos-release is centos-linux-release on CentOS 8)
    want, todo = set(), [n if n in pk else sorted(prov.get(n, [n]), key=lambda x: (len(x), x))[0] for n in seeds]
    while todo:
        n = todo.pop()
        if n in want:
            continue
        want.add(n)
        for r in pk[n]["req"]:
            c = prov.get(r, [])
            if c and not any(x in want for x in c):
                todo.append(sorted(c, key=lambda x: (len(x), x))[0])
    return [base_url + pk[n]["href"] for n in sorted(want)]


def build_el(root, box_doc, adapter):
    """Nothing builds CentOS 5 roots any more. Resolve a seed set from the release's own metadata, unpack it with the
    host's rpm2cpio, then let the box's own rpm install the same packages properly so its database knows them."""
    rel = box_doc["release"]
    v = backends.Rpm().hop_vars(adapter, rel)
    base = f"{v['shim']}/{next(iter(adapter['releases'][rel]['repos'].values()))}/"
    urls = resolve(base, adapter["box"]["seeds"])
    os.makedirs(f"{root}/tmp/boot", exist_ok=True)
    rc, log = host(f"cd {root}/tmp/boot && printf '%s\\n' {' '.join(urls)} | xargs -P8 -n1 curl -sfO")
    if rc:
        raise RuntimeError("downloading the seed packages failed:\n" + tail(log, 2000))
    # filesystem first: from EL7 on it makes /lib64, /bin, ... symlinks into /usr, which the other packages unpack through
    rc, log = host(f"cd {root} && for f in tmp/boot/filesystem-*.rpm tmp/boot/*.rpm; do rpm2cpio $f | cpio -idm --quiet "
                   "2>/dev/null; done; "
                   f"ln -sf /proc/mounts {root}/etc/mtab")
    if rc:
        raise RuntimeError("unpacking the seed packages failed:\n" + tail(log, 2000))
    b = Box(root)
    steps = [("rpm database", "rpm --initdb && rpm -i --nodeps --force /tmp/boot/*.rpm && rm -rf /tmp/boot"),
             ("package install", backends.fill(adapter["hop"]["set_release"], v) + " && yum -y install " +
              " ".join(box_doc["packages"]) + " && yum -y update && yum clean all"),
             ("customize", box_doc["customize"] + "\n" + box_doc.get("start_hook", ""))]
    for what, cmd in steps:
        rc, log = b.run(cmd)
        if rc:
            raise RuntimeError(f"{what} failed:\n" + tail(log, 3000))


BUILDERS = {"debian": build_debian, "ubuntu": build_debian, "el": build_el}


# Bump when what a build writes changes, so boxes cached before it rebuild. 1: the box's own /etc/hostname (wend#26).
BUILD_VERSION = b"1"


def build_box(box_id, box_doc, adapter, box_toml_bytes, adapter_bytes, log=print):
    """Build a box once and cache it, keyed by the box file, the adapter and BUILD_VERSION. Returns the cached
    (read-only) Box."""
    key = hashlib.sha256(box_toml_bytes + adapter_bytes + BUILD_VERSION).hexdigest()[:12]
    root = f"{WORK}/boxes/{box_id.replace('/', '_')}-{key}"
    if os.path.exists(f"{root}/.wend-ready"):
        return Box(root)
    if os.path.exists(root):
        safe_rm(root)
    os.makedirs(root)
    log(f"building box {box_id} ({box_doc['release']}); this takes a minute or two")
    ensure_mirror(adapter)
    BUILDERS[box_doc["family"]](root, box_doc, adapter)
    h = Box(root).health(checks_for(box_doc, adapter), adapter["release_file"], set(box_doc.get("isolate", [])))
    failed = sorted(set(h["checks"]) - set(h["passed"]))
    if failed:
        raise RuntimeError(f"box {box_id} fails its own checks at the start: {failed}\n" +
                           "\n".join(tail(h["checks"][c]["log"], 800) for c in failed))
    open(f"{root}/.wend-ready", "w").close()
    return Box(root)
