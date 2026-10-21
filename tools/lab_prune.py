"""Give remi's disk back (docs/remi.md, "Disk"). Runs on remi as root, daily (archive/remi/wend-prune.timer), from
wend-runner, so what it deletes is decided by main's copy of this file. Standard library only.

  python3 -P tools/lab_prune.py [--apply] [--days N] [--only FILE] [--src DIR] [--lab DIR]

Without --apply it only says what it would do. Two things grow, and nothing else is touched:

- A VM run's disks. vm.py writes each run to <src>/<worktree>/.wend/vm/<run>/: its hop disks (*.qcow2, a UEFI
  disk's *.vars.fd), and for a rehearse or an upgrade the box root it copied (copy/), the walk's work/ and the plan's
  cache (plan/cache/). Those are only for resuming or poking at a run: remi.py's verdict already keeps every run.json
  and file hash under <lab>/logs. A run directory nothing has written to in N days (default 3) loses them; its
  run.json, logs, steps and plan stay.
- An installed box, <lab>/vmwork*/vm/<box>-<firmware>[-<layout>]-<key>/. vm.py reuses one only while its key (the box
  file, the adapter, vm.py's build_source, the firmware and the layout: vm.build) is what a checkout would make, so
  a box is deleted when no branch tip on origin, no PR head remi.py has fetched and no queued or running job's sha
  makes its key, nothing has written to it in N days, and no disk left in any run directory backs onto it.

Never: a run directory or box any process has open (its cwd, an open file or a mapped one), a worktree a queued or
running remi.py job uses, a box whose install lock is held (held through the delete, so an install waits for it),
anything a symlink points to, anything outside those two patterns. --only FILE limits it to the paths FILE lists
(tab-separated, path in the second column, as a reviewed delete list has them). Each action and its bytes go to
stdout; the timer's journal keeps them.
"""
import fcntl
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib

ROOT_FS = ("ext3", "ext4", "xfs", "xfs-v4")  # vm.ROOT_FS's names, longest match first below
DISK_SIZES = range(1, 501)  # vm.py's --extra-disk GB, 1 to 500: the name says how many disks, not how big
RUN_BULK = ("copy", "work", os.path.join("plan", "cache"))


HERE = os.path.dirname(os.path.abspath(__file__))


class Unsure(Exception):
    """The base clone couldn't be read in full: no box is judged stale on a partial inventory."""


def git(base, *a, text=True):
    """git in the base clone, read-only; any failure is Unsure."""
    r = subprocess.run(["git", "-c", "safe.directory=*", "-C", base, *a], capture_output=True, text=text,
                       env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    if r.returncode:
        raise Unsure(f"git {' '.join(a)} in {base}: rc {r.returncode}: {(r.stderr or '')[:300]!r}")
    return r


def size(path):
    total = 0
    for top, dirs, files in os.walk(path):
        for n in files:
            try:
                total += os.lstat(os.path.join(top, n)).st_blocks * 512
            except FileNotFoundError:
                pass
    return total


def newest(path):
    t = os.lstat(path).st_mtime
    for top, dirs, files in os.walk(path):
        for n in dirs + files:
            try:
                t = max(t, os.lstat(os.path.join(top, n)).st_mtime)
            except FileNotFoundError:
                pass
    return t


def held():
    """Every path a process has as its cwd, an open file or a mapped file."""
    out = set()
    for p in glob.glob("/proc/[0-9]*"):
        links = [f"{p}/cwd"] + glob.glob(f"{p}/fd/*")
        for link in links:
            try:
                out.add(os.readlink(link))
            except OSError:
                pass
        try:
            for line in open(f"{p}/maps"):
                f = line.split(None, 5)
                if len(f) == 6 and f[5].startswith("/"):
                    out.add(f[5].strip())
        except OSError:
            pass
    return out


def plain(root, d):
    """Whether d is root/<relative> with no symlink anywhere below root, so a delete there stays there."""
    return os.path.realpath(d) == os.path.join(os.path.realpath(root), os.path.relpath(d, root))


def under(paths, d):
    d = d.rstrip("/") + "/"
    return any(p == d[:-1] or p.startswith(d) for p in paths)


def backing(qcow2):
    """The file a qcow2 disk is an overlay on (its header's backing file name), or None."""
    try:
        with open(qcow2, "rb") as f:
            h = f.read(20)
            if len(h) < 20 or h[:4] != b"QFI\xfb":
                return None
            off, n = int.from_bytes(h[8:16], "big"), int.from_bytes(h[16:20], "big")
            if not off or not n or n > 4096:
                return None
            f.seek(off)
            name = f.read(n).decode(errors="replace")
    except OSError:
        return None
    return os.path.normpath(os.path.join(os.path.dirname(qcow2), name))


def chain(qcow2):
    seen = []
    while qcow2 and qcow2 not in seen and len(seen) < 64:
        seen.append(qcow2)
        qcow2 = backing(qcow2)
    return seen


def jobs(lab):
    """The queued and running remi.py jobs."""
    out = []
    for f in glob.glob(os.path.join(lab, "queue", "*.json")):
        try:
            e = json.load(open(f))
        except (OSError, ValueError):
            continue
        if e.get("state") in ("queued", "running"):
            out.append(e)
    return out


def busy_worktrees(src, js):
    """Worktrees remi.py jobs use (remi.worktree's names, either user's)."""
    out = set()
    for e in js:
        wt = os.path.join(src, f"wend-{e.get('pr') or 'main'}-{e.get('sha', '')[:12]}")
        out |= {wt, wt + "-root"}
        if e.get("worktree"):
            out.add(e["worktree"])
    return out


def heads(base, js):
    """Every commit a VM run could be at next: origin's branch tips, the PR heads remi.py fetched, queued jobs."""
    r = git(base, "for-each-ref", "--format=%(objectname)", "refs/remotes/origin", "refs/wend-lab/pr")
    out = set(r.stdout.split()) | {e["sha"] for e in js if re.fullmatch(r"[0-9a-f]{40}", e.get("sha", ""))}
    if not out:
        raise Unsure(f"no branch tips or jobs in {base}")
    return sorted(out)


def build_source(src):
    """vm.build_source's bytes for another commit's vm.py source. The function run is this checkout's (main's, on
    remi), never the candidate's: a branch is only parsed here, as vm.build_source parses, so no branch code runs as
    root. A branch that changes build_source itself gets main's reading of its file, so its boxes may look stale."""
    import ast
    mine = open(os.path.join(HERE, "vm.py")).read()
    fn = [n for n in ast.parse(mine).body if isinstance(n, ast.FunctionDef) and n.name == "build_source"][0]
    ns = {}
    mod = ast.Module(body=[ast.Import(names=[ast.alias(name="ast")]), ast.Import(names=[ast.alias(name="os")]), fn],
                     type_ignores=[])
    exec(compile(ast.fix_missing_locations(mod), os.path.join(HERE, "vm.py"), "exec"), ns)
    with tempfile.TemporaryDirectory(prefix="wend-lab-prune-") as tmp:  # 0700, made fresh: nothing to race
        path = os.path.join(tmp, "vm.py")
        with open(path, "x") as f:
            f.write(src)
        try:
            return ns["build_source"](path)
        except SyntaxError:
            return None


def parse_tag(tag):
    """layout_tag()'s name back to (root_fs, lvm, number of extra disks, no_hwaddr), or None."""
    rest, root_fs = tag or "", None
    for fs in sorted(ROOT_FS, key=len, reverse=True):
        if rest == fs or rest.startswith(fs + "-"):
            root_fs, rest = fs, rest[len(fs) + 1:]
            break
    parts = rest.split("-") if rest else []
    lvm = bool(parts) and parts[0] == "lvm"
    parts = parts[1:] if lvm else parts
    n = 0
    if parts and re.fullmatch(r"x[0-9]+", parts[0]):
        n, parts = int(parts[0][1:]), parts[1:]
    nohw = parts == ["nohwaddr"]
    if parts and not nohw:
        return None
    return root_fs, lvm, n, nohw


def disk_lists(n):
    """Every extra_disks list of n (at most 2) disks vm.py could have hashed."""
    if not n:
        return [[]]
    if n == 1:
        return [[s] for s in DISK_SIZES]
    return [[a, b] for a in DISK_SIZES for b in DISK_SIZES]


def current(name, sources):
    """Whether some commit's box file, adapter and build_source make box dir `name` (vm.build's key), or it can't be
    ruled out. The name is vm.build's: <box id, / as _>-<firmware>[-<layout_tag>]-<12 hex>, read from each candidate
    box id's own prefix, since a box id or a layout can hold a "-"."""
    for box_id, srcs in sources.items():
        for fw in ("bios", "uefi"):
            pre = f"{box_id.replace('/', '_')}-{fw}-"
            rest = name[len(pre):] if name.startswith(pre) else ""
            if not re.fullmatch(r"(.+-)?[0-9a-f]{12}", rest):
                continue
            tag, key = rest[:-13], rest[-12:]
            lay = parse_tag(tag)
            if lay is None:
                continue
            root_fs, lvm, n, nohw = lay
            if n > 2:
                return True  # 500^n size lists can't all be tried: keep it
            lays = [json.dumps({"root_fs": root_fs, "lvm": lvm, "extra_disks": ex, "no_hwaddr": nohw},
                               sort_keys=True).encode() for ex in disk_lists(n)] if tag else []
            for raw_box, raw_ad, bs in srcs:
                h = hashlib.sha256(raw_box + raw_ad + bs + fw.encode())
                if not tag:
                    if h.hexdigest()[:12] == key:
                        return True
                    continue
                for lay in lays:
                    k = h.copy()
                    k.update(lay)
                    if k.hexdigest()[:12] == key:
                        return True
    return False


def box_sources(base, commits):
    """{box id: {(box.toml, adapter, build_source) bytes}} over commits, deduplicated by content. Unsure on any failed
    read: a file a commit lacks is fine, a git that fails is not."""
    out, bs_by_blob = {}, {}
    for c in commits:
        if not git(base, "ls-tree", "--name-only", c, "tools/vm.py").stdout.strip():
            continue
        vm_blob = git(base, "rev-parse", f"{c}:tools/vm.py").stdout.strip()
        if vm_blob not in bs_by_blob:
            bs_by_blob[vm_blob] = build_source(git(base, "cat-file", "blob", vm_blob).stdout)
        bs = bs_by_blob[vm_blob]
        if bs is None:
            continue
        files = set(git(base, "ls-tree", "-r", "--name-only", c, "boxes", "adapters").stdout.split())
        for path in sorted(files):
            if not (path.startswith("boxes/") and path.endswith("/box.toml")):
                continue
            raw_box = git(base, "show", f"{c}:{path}", text=False).stdout
            try:
                fam = tomllib.loads(raw_box.decode())["family"]
            except (ValueError, KeyError, UnicodeDecodeError):
                continue
            if f"adapters/{fam}.toml" in files:
                raw_ad = git(base, "show", f"{c}:adapters/{fam}.toml", text=False).stdout
                out.setdefault(path[len("boxes/"):-len("/box.toml")], set()).add((raw_box, raw_ad, bs))
    return out


def remove(path, apply):
    if os.path.islink(path):
        return 0
    n = size(path) if os.path.isdir(path) else os.lstat(path).st_blocks * 512
    if apply:
        shutil.rmtree(path) if os.path.isdir(path) else os.unlink(path)
    return n


def strip_run(d, apply):
    """A run directory without its disks and box copies: what vm.py writes to resume or inspect, not to judge."""
    n = 0
    for b in RUN_BULK:
        p = os.path.join(d, b)
        if os.path.lexists(p):
            n += remove(p, apply)
    for p in glob.glob(os.path.join(d, "*.qcow2")) + glob.glob(os.path.join(d, "*.vars.fd")):
        if os.path.isfile(p) and not os.path.islink(p):
            n += remove(p, apply)
    return n


def main(argv):
    opts = {"--days": "3", "--src": "/data/dev/src", "--lab": "/data/wend-lab", "--only": None}
    apply = False
    args = list(argv)
    while args:
        a = args.pop(0)
        if a == "--apply":
            apply = True
        elif a in opts and args:
            opts[a] = args.pop(0)
        else:
            print(__doc__)
            return 2
    src, lab, cutoff = opts["--src"], opts["--lab"], time.time() - float(opts["--days"]) * 86400
    only = None
    if opts["--only"]:
        only = {line.split("\t")[1].rstrip("/") for line in open(opts["--only"]) if line.count("\t") >= 1}
    verb = "removed" if apply else "would remove"
    js, open_paths = jobs(lab), held()
    busy = busy_worktrees(src, js)
    freed, kept = 0, []

    stripped = set()
    for d in sorted(glob.glob(os.path.join(src, "*", ".wend", "vm", "*"))):
        wt = d.split("/.wend/vm/")[0]
        if not os.path.isdir(d) or not plain(src, d):
            continue
        why = ("not listed" if only is not None and d.rstrip("/") not in only else
               "a queued or running job's worktree" if wt in busy else
               "held open" if under(open_paths, d) else
               "written to recently" if newest(d) > cutoff else None)
        if why:
            kept.append((d, why))
            continue
        n = strip_run(d, apply)
        stripped.add(d)
        if n:
            print(f"{verb} {n / 1e9:.1f} GB of disks from run {d}")
        freed += n

    # what the run disks that stay are overlays on: a box one of them backs onto stays with it
    backed = set()
    for q in glob.glob(os.path.join(src, "*", ".wend", "vm", "*", "*.qcow2")):
        if apply or os.path.dirname(q) not in stripped:
            backed |= set(chain(q)[1:])
    base = os.path.join(src, "wend")
    try:
        sources = box_sources(base, heads(base, js))
    except Unsure as e:
        print(f"lab_prune: no box is deleted this time: {e}")
        sources = None
    for d in sorted(glob.glob(os.path.join(lab, "vmwork*", "vm", "*")) if sources is not None else []):
        if not os.path.isdir(d) or not plain(lab, d):
            continue
        name = os.path.basename(d)
        if only is not None and d.rstrip("/") not in only:
            kept.append((d, "not listed"))
            continue
        # vm.Locked's lock, opened read-only and never created: a root-owned new one would lock dev's installs out
        try:
            lock = open(d + ".lock", "rb") if not os.path.islink(d + ".lock") else open(os.devnull, "rb")
        except FileNotFoundError:
            lock = open(os.devnull, "rb")  # nothing ever installed under this lock, so nothing is installing
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                kept.append((d, "installing (lock held)"))
                continue
            why = ("held open" if under(open_paths, d) else
                   "a kept run's disk backs onto it" if under(backed, d) else
                   "written to recently" if newest(d) > cutoff else
                   "a current head makes its key" if current(name, sources) else None)
            if why:
                kept.append((d, why))
                continue
            n = remove(d, apply)
            print(f"{verb} {n / 1e9:.1f} GB box {d}")
            freed += n
        finally:
            lock.close()
    for d, why in kept:
        if why != "not listed":
            print(f"kept {d}: {why}")
    print(f"lab_prune: {verb} {freed / 1e9:.1f} GB" + ("" if apply else " (dry run: --apply to delete)"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
