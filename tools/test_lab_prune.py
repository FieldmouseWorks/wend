"""remi's disk prune, by behaviour (tools/lab_prune.py): run disks and stale boxes go, everything a run, a job or a
current head needs stays, and the box key it recomputes is vm.build's own. Offline, no root. python3 tools/test_lab_prune.py"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stdout

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
w = tempfile.mkdtemp(prefix="wend-test-lab-prune-")
os.environ["WEND_WORK"] = f"{w}/lab/vmwork"
import lab_prune  # noqa: E402
import vm  # noqa: E402

results = []


def check(ok, what):
    results.append(ok)
    print(("passed  " if ok else "WRONG   ") + what)


def git(*a):
    subprocess.run(["git", "-C", f"{w}/src/wend", "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True,
                   capture_output=True)


BOX = "el/centos7-lamp"
src, lab = f"{w}/src", f"{w}/lab"
# the base clone: this checkout's vm.py, box and adapter, at a commit origin/main names
os.makedirs(f"{src}/wend/tools")
os.makedirs(f"{src}/wend/boxes/{BOX}")
os.makedirs(f"{src}/wend/adapters")
shutil.copy(f"{HERE}/vm.py", f"{src}/wend/tools/vm.py")
shutil.copy(f"{HERE}/../boxes/{BOX}/box.toml", f"{src}/wend/boxes/{BOX}/box.toml")
shutil.copy(f"{HERE}/../adapters/el.toml", f"{src}/wend/adapters/el.toml")
# a box id with a "-bios" of its own: its cache name has to be read from its own prefix
TRICKY = "el/lamp-bios-x"
os.makedirs(f"{src}/wend/boxes/{TRICKY}")
shutil.copy(f"{HERE}/../boxes/{BOX}/box.toml", f"{src}/wend/boxes/{TRICKY}/box.toml")
git("init", "-q")
git("add", "-A")
git("commit", "-q", "-m", "base")
git("update-ref", "refs/remotes/origin/main", "HEAD")
HEAD = subprocess.run(["git", "-C", f"{src}/wend", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

# the boxes vm.build makes at that commit, by vm.build itself (install stubbed): their names are the keys to match
vm.WORK = f"{lab}/vmwork/vm"
vm.ROOT = f"{src}/wend"  # the base clone: the same files, and the tricky box beside them
vm.install = lambda box_id, box_doc, d: (os.makedirs(d, exist_ok=True), open(f"{d}/base.qcow2", "wb").close())
made = {}
for box, fw, layout in ((BOX, "bios", {}), (BOX, "uefi", {}), (BOX, "bios", {"lvm": True, "extra_disks": [30]}),
                        (BOX, "bios", {"root_fs": "xfs-v4", "lvm": True, "extra_disks": [30]}),
                        (BOX, "bios", {"no_hwaddr": True}), (BOX, "bios", {"lvm": True, "extra_disks": [300, 40]}),
                        (TRICKY, "uefi", {}), (TRICKY, "bios", {"extra_disks": [500]})):
    vm.FIRMWARE = fw
    vm.LAYOUT.update({"root_fs": None, "lvm": False, "extra_disks": [], "no_hwaddr": False, **layout})
    d, _ = vm.build(box)
    made[(box, fw, json.dumps(layout, sort_keys=True))] = d
vm.LAYOUT.update({"root_fs": None, "lvm": False, "extra_disks": [], "no_hwaddr": False})
vm.FIRMWARE = "bios"

old = time.time() - 10 * 86400


def age(path, t=old):
    for top, dirs, files in os.walk(path):
        for n in dirs + files:
            os.utime(os.path.join(top, n), (t, t), follow_symlinks=False)
    os.utime(path, (t, t))


def qcow2(path, back=None):
    """A qcow2 header, overlay on back (absolute) if given, and some bulk."""
    name = (back or "").encode()
    h = b"QFI\xfb" + (3).to_bytes(4, "big") + ((512 if name else 0).to_bytes(8, "big")) + len(name).to_bytes(4, "big")
    with open(path, "wb") as f:
        f.write(h.ljust(512, b"\0") + name + b"\0" * 65536)


stale = f"{lab}/vmwork/vm/el_centos7-lamp-bios-0123456789ab"
stale_backed = f"{lab}/vmwork/vm/el_centos5-web-bios-ba5eba5eba5e"
stale_root = f"{lab}/vmwork-root/vm/el_centos7-lamp-bios-lvm-x1-fedcba987654"
for d in (stale, stale_backed, stale_root):
    os.makedirs(d)
    qcow2(f"{d}/base.qcow2")


def run(wt, name, back, extra=()):
    d = f"{src}/{wt}/.wend/vm/{name}"
    os.makedirs(f"{d}/plan/cache/sha256", exist_ok=True)
    os.makedirs(f"{d}/copy/etc", exist_ok=True)
    json.dump({"result": "pass"}, open(f"{d}/run.json", "w"))
    open(f"{d}/centos7.steps.log", "w").write("ok\n")
    open(f"{d}/plan/plan.json", "w").write("{}")
    open(f"{d}/plan/cache/sha256/aa", "wb").write(b"x" * 4096)
    open(f"{d}/copy/etc/passwd", "w").write("root:x:0:0::/root:/bin/sh\n")
    qcow2(f"{d}/centos7.qcow2", f"{back}/base.qcow2")
    qcow2(f"{d}/alma8.qcow2", f"{d}/centos7.qcow2")
    open(f"{d}/alma8.vars.fd", "wb").write(b"v" * 4096)
    for e in extra:
        open(f"{d}/{e}", "w").write("x")
    return d


old_run = run("wend-92", "20261014-el_centos7-lamp-bios-1", stale)
new_run = run("wend-160", "20261021-el_centos5-web-bios-2", stale_backed)
busy_run = run(f"wend-174-{HEAD[:12]}", "20261015-el_centos4-dns-bios-3", stale)
held_run = run("wend-103", "20261015-el_centos7-lamp-bios-4", stale)
# an old run a recent resumed replay goes on from: the replay's disk is an overlay on its hop disk
resumed_from = run("wend-120", "20261013-el_centos7-lamp-bios-5", stale)
resumed = f"{src}/wend-121/.wend/vm/20261021-el_centos7-lamp-bios-6"
os.makedirs(resumed)
qcow2(f"{resumed}/alma8.qcow2", f"{resumed_from}/centos7.qcow2")
for d in (old_run, busy_run, held_run, resumed_from, *made.values(), stale, stale_backed, stale_root):
    age(d)
os.makedirs(f"{lab}/queue")
json.dump({"id": "20261021-133308-replay-f707", "state": "running", "pr": 174, "sha": HEAD},
          open(f"{lab}/queue/20261021-133308-replay-f707.json", "w"))
# a run directory through a symlink: never followed
os.makedirs(f"{w}/elsewhere/r")
qcow2(f"{w}/elsewhere/r/x.qcow2")
os.makedirs(f"{src}/wend-link/.wend/vm")
os.symlink(f"{w}/elsewhere/r", f"{src}/wend-link/.wend/vm/r")

# a vmwork* through a symlink, onto something old: never followed
os.makedirs(f"{w}/precious/vm/el_centos7-lamp-bios-0000aaaa0000")
os.symlink(f"{w}/precious", f"{lab}/vmwork-link")
age(f"{w}/precious")

installing = f"{lab}/vmwork/vm/el_centos4-dns-bios-1111aaaa1111"
os.makedirs(installing)
age(installing)
import fcntl  # noqa: E402
inst_lock = open(installing + ".lock", "a")
fcntl.flock(inst_lock, fcntl.LOCK_EX)
fifo_box = f"{lab}/vmwork/vm/el_centos4-dns-bios-2222bbbb2222"
os.makedirs(fifo_box)
age(fifo_box)
os.mkfifo(fifo_box + ".lock")  # opened blocking, this would hang the prune
link_box = f"{lab}/vmwork/vm/el_centos4-dns-bios-3333cccc3333"
os.makedirs(link_box)
age(link_box)
os.symlink(f"{w}/precious", link_box + ".lock")

hold = open(f"{held_run}/centos7.steps.log")


def prune(*a):
    out = io.StringIO()
    with redirect_stdout(out):
        rc = lab_prune.main(["--src", src, "--lab", lab, *a])
    return rc, out.getvalue()


# the key, both ways
names = {os.path.basename(d) for d in made.values()}
sources = lab_prune.box_sources(f"{src}/wend", lab_prune.heads(f"{src}/wend", []))
check(all(lab_prune.current(n, sources) for n in names),
      f"every box vm.build makes at a branch tip is current ({', '.join(sorted(names))})")
check(not any(lab_prune.current(os.path.basename(d), sources) for d in (stale, stale_backed, stale_root)),
      "a box dir whose key no head makes isn't current")
check(lab_prune.current("el_centos7-lamp-bios-x3-0123456789ab", sources),
      "a box with more than two extra disks is kept: its sizes can't all be tried")
check(lab_prune.parse_tag("xfs-v4-lvm-x2-nohwaddr") == ("xfs-v4", True, 2, True) and lab_prune.parse_tag("") ==
      (None, False, 0, False) and lab_prune.parse_tag("lvm-bogus") is None, "layout tags read back as layout_tag wrote them")

# a branch's own build_source is parsed, never run (the prune runs as root)
evil = open(f"{HERE}/vm.py").read().replace('def build_source(path=None):\n',
                                             f'def build_source(path=None):\n    open("{w}/ran", "w").close()\n', 1)
open(f"{src}/wend/tools/vm.py", "w").write(evil)
git("commit", "-qam", "evil")
git("update-ref", "refs/remotes/origin/evil", "HEAD")
git("checkout", "-q", "HEAD~1", "--", "tools/vm.py")
evil_sources = lab_prune.box_sources(f"{src}/wend", lab_prune.heads(f"{src}/wend", []))
check(not os.path.exists(f"{w}/ran"), "a branch's vm.py code never runs: its build_source is only parsed")
check(lab_prune.current(os.path.basename(stale), evil_sources),
      "a box a head with its own build_source might key is kept, not guessed at")
git("update-ref", "-d", "refs/remotes/origin/evil")

# a base clone that can't be read in full: no box is judged on it
os.rename(f"{src}/wend", f"{src}/wend-away")
rc, out = prune("--apply", "--only", "/dev/null")
os.rename(f"{src}/wend-away", f"{src}/wend")
check(rc == 0 and "no box is deleted this time" in out, "an unreadable base clone deletes no box")

rc, out = prune()
check(rc == 0 and "dry run" in out and os.path.exists(f"{old_run}/centos7.qcow2") and os.path.isdir(stale),
      "without --apply nothing is deleted")
check(f"would remove" in out and old_run in out and stale in out, "the dry run names what it would delete")

rc, out = prune("--only", "/dev/null")
check(rc == 0 and "0.0 GB" in out.splitlines()[-1], "--only with an empty list touches nothing")

listed = f"{w}/list.tsv"
open(listed, "w").write(f"kind\tpath\tGB\n" f"rundir-disks\t{old_run}/\t1\n")
rc, out = prune("--only", listed, "--apply")
check(not os.path.exists(f"{old_run}/centos7.qcow2") and os.path.isdir(stale) and os.path.isdir(stale_root),
      "--only limits --apply to the listed paths")

rc, out = prune("--apply")
check(rc == 0, f"--apply runs (rc {rc})")
check(not any(os.path.exists(f"{old_run}/{p}") for p in ("centos7.qcow2", "alma8.qcow2", "alma8.vars.fd", "copy",
                                                          "plan/cache")),
      "an old run loses its disks, NVRAM, box copy and plan cache")
check(all(os.path.exists(f"{old_run}/{p}") for p in ("run.json", "centos7.steps.log", "plan/plan.json")),
      "and keeps its run.json, logs and plan")
check(os.path.exists(f"{new_run}/alma8.qcow2"), "a run written to recently keeps its disks")
check(os.path.exists(f"{resumed_from}/centos7.qcow2") and os.path.exists(f"{resumed}/alma8.qcow2"),
      "an old run a kept resumed replay backs onto keeps its disks")
check(os.path.exists(f"{busy_run}/alma8.qcow2"), "a queued or running job's worktree keeps its runs' disks")
check(os.path.exists(f"{held_run}/alma8.qcow2"), "a run with a file held open keeps its disks")
check(os.path.isdir(stale) and "a kept run's disk backs onto it" in out,
      "a stale box a kept run's disk backs onto stays")
check(os.path.isdir(stale_backed), "a stale box backing a recent run stays")
check(not os.path.exists(stale_root), "a stale box nothing uses goes (vmwork-root too)")
check(all(os.path.isdir(d) for d in made.values()), "every box a current head makes stays, however old")
check(os.path.exists(f"{w}/elsewhere/r/x.qcow2"), "a symlinked run directory is never followed")
check(os.path.isdir(installing), "a box whose install lock is held stays")
check(os.path.isdir(fifo_box) and os.path.isdir(link_box), "a box whose lock is a FIFO or a symlink stays, and the prune doesn't hang")
check(not os.path.exists(f"{stale_root}.lock") and not os.path.exists(f"{stale}.lock"),
      "the prune never creates a box's install lock (as root it would lock dev out)")
check(os.path.isdir(f"{w}/precious/vm/el_centos7-lamp-bios-0000aaaa0000"), "nor a box dir under a symlinked vmwork*")

# the delete itself goes through no-follow fds: a directory above swapped for a link deletes nothing
os.makedirs(f"{w}/target/vm/box")
os.symlink(f"{w}/target", f"{lab}/swapped")
check(lab_prune.remove(lab, f"{lab}/swapped/vm/box", True) == 0 and os.path.isdir(f"{w}/target/vm/box"),
      "remove() never follows a symlinked ancestor")

hold.close()
inst_lock.close()
shutil.rmtree(w)
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
