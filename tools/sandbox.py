"""The sandbox: a box is a whole root filesystem in a chroot, snapshotted with rsync, rolled back by copying back.
Generalized from dep-lab's oldlinux/ol.py. Needs root, chroot and rsync on the host, plus debootstrap for Debian boxes.

Follows SPEC.md "Sandbox requirements": a private tmpfs /dev per box (never a bind of the host's), fresh proc/sys,
daemons never start, and nothing is deleted until every mount under it is gone.
"""
import hashlib
import os
import subprocess

WORK = os.environ.get("WEND_WORK", "/var/tmp/wend")
ENV = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "HOME": "/root",
       "DEBIAN_FRONTEND": "noninteractive", "TERM": "dumb"}


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

    def run(self, cmd, timeout=1800):
        self._mount()
        try:
            p = subprocess.run(["chroot", self.root, "/bin/sh", "-c", cmd], env=ENV, capture_output=True, text=True,
                               timeout=timeout, errors="replace")
            return p.returncode, p.stdout + p.stderr
        except subprocess.TimeoutExpired as e:
            return 124, f"TIMEOUT after {timeout}s\n" + (e.stdout or b"").decode(errors="replace")[-3000:]
        finally:
            self._umount()

    def copy_to(self, dst):
        unmount_all(self.root)
        rc, log = host(f"mkdir -p {dst} && rsync -aHx --delete {self.root}/ {dst}/")
        if rc:
            raise RuntimeError(f"copying {self.root} to {dst} failed (is rsync installed?):\n{tail(log, 800)}")
        return Box(dst)

    def health(self, checks, release_file):
        out = {}
        for name, test in checks.items():
            rc, log = self.run(f"( set -x; {test} ) >/tmp/.h 2>&1; rc=$?; tail -c 1200 /tmp/.h; exit $rc", timeout=180)
            out[name] = {"ok": rc == 0, "log": "" if rc == 0 else log}
        v = self.run(f"cat {release_file}")[1].strip()
        return {"release": v, "checks": out, "passed": sorted(c for c in out if out[c]["ok"])}


def checks_for(box_doc, adapter):
    """Adapter checks that are shell commands, plus the box's own roles. Box roles win on a name clash."""
    shell = {k: v for k, v in adapter["checks"].items() if k in box_doc.get("adapter_checks", ["pkgdb"])}
    return {**shell, **box_doc["roles"]}


def mirror(adapter, release):
    lad, r = adapter["ladder"], adapter["releases"]
    return r["current"] if lad.index(release) >= lad.index(r["current_from"]) else r["archive"]


def build_debian(root, box_doc, adapter):
    rel = box_doc["release"]
    rc, log = host(f"debootstrap --no-check-gpg --arch={box_doc.get('arch', 'amd64')} --variant=minbase {rel} {root} "
                   f"{mirror(adapter, rel)}")
    if rc:
        raise RuntimeError("debootstrap failed:\n" + tail(log, 2000))
    with open(f"{root}/usr/sbin/policy-rc.d", "w") as f:
        f.write("#!/bin/sh\nexit 101\n")  # never start daemons inside the sandbox
    os.chmod(f"{root}/usr/sbin/policy-rc.d", 0o755)
    with open(f"{root}/etc/apt/apt.conf.d/00wend", "w") as f:
        f.write(adapter["hop"]["apt_conf"])
    with open(f"{root}/etc/apt/sources.list", "w") as f:
        f.write(f"deb {mirror(adapter, rel)} {rel} main\n")
    b = Box(root)
    rc, log = b.run("".join(f"echo '{d}' | debconf-set-selections && " for d in box_doc.get("debconf_selections", [])) +
                    "apt-get update && apt-get install " + " ".join(box_doc["packages"]))
    if rc:
        raise RuntimeError("package install failed:\n" + tail(log, 2000))
    rc, log = b.run(box_doc["customize"] + "\n" + box_doc.get("start_hook", ""))
    if rc:
        raise RuntimeError("customize failed:\n" + tail(log, 2000))


BUILDERS = {"debian": build_debian}


def build_box(box_id, box_doc, adapter, box_toml_bytes, adapter_bytes, log=print):
    """Build a box once and cache it, keyed by the box file and the adapter. Returns the cached (read-only) Box."""
    key = hashlib.sha256(box_toml_bytes + adapter_bytes).hexdigest()[:12]
    root = f"{WORK}/boxes/{box_id.replace('/', '_')}-{key}"
    if os.path.exists(f"{root}/.wend-ready"):
        return Box(root)
    if os.path.exists(root):
        safe_rm(root)
    os.makedirs(root)
    log(f"building box {box_id} ({box_doc['release']}); this takes a minute or two")
    BUILDERS[box_doc["family"]](root, box_doc, adapter)
    h = Box(root).health(checks_for(box_doc, adapter), adapter["release_file"])
    failed = sorted(set(h["checks"]) - set(h["passed"]))
    if failed:
        raise RuntimeError(f"box {box_id} fails its own checks at the start: {failed}\n" +
                           "\n".join(tail(h["checks"][c]["log"], 800) for c in failed))
    open(f"{root}/.wend-ready", "w").close()
    return Box(root)
