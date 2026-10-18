"""Lab jobs on remi, the maintainer's lab machine (docs/remi.md): a named job at an exact commit, run from a queue one
slot at a time, ending in a full JSON verdict. Nobody types a VM command by hand, and nobody greps a log for a summary.

  python3 -P tools/remi.py enqueue JOB ARG... --sha SHA [--pr N] --by WHO [--reply-to SESSION] [--after ID]
                                               [--vm-flags "FLAGS"]
  python3 -P tools/remi.py queue [--json]      the queue: running, waiting (and on what), queued, then the last done
  python3 -P tools/remi.py order ID...         put these queued jobs first, in this order (the lab order)
  python3 -P tools/remi.py cancel ID           drop a queued job, or stop a running one (its verdict says cancelled)
  python3 -P tools/remi.py wait ID             block until the job ends; prints its result and verdict path
  python3 -P tools/remi.py show ID [--part N]  the verdict JSON, in parts of at most 48000 bytes ("part i/n" first)
  python3 -P tools/remi.py file ID PATH [--part N]
                                               a file from the job's log directory or one of its run directories
  python3 -P tools/remi.py delivered ID        mark the verdict as sent to whoever asked; `queue` lists undelivered
  python3 -P tools/remi.py update              move this runner checkout to origin/main (refused while jobs run)

Jobs (ROUTE is routes/<family>/<name>.toml, BOX is <family>/<name>, both as they are at SHA):

  tests                  every tools/test_*.py and tools/check.py, as dev. The proof for a comment- or test-only change
  build BOX              tools/vm.py build BOX
  replay ROUTE BOX       tools/vm.py replay ROUTE BOX
  relocate BOX           tools/vm.py relocate BOX (give it --vm-flags "--extra-disk GB")
  rehearse ROUTE BOX     the client built with cargo build --release --locked, then tools/vm.py rehearse, as root

--vm-flags takes vm.py's flags before the command: --uefi --no-hwaddr --lvm --relocate --root-fs FS --extra-disk GB.
VM jobs run as dev under `sg kvm` with WEND_WORK=<lab>/vmwork, except a rehearse and any job on a box with a [shelter]
plan, which run as root (sudo -n) with WEND_WORK=<lab>/vmwork-root, as they always have on remi.

What it enforces, so nobody has to remember it:
- SHA is the tip of a branch on origin (pushed by someone with write access) or on origin/main, checked at enqueue and
  again when the job starts; with --pr N it must also be PR N's head then. A fork's commit never runs here.
- Each job runs in its own worktree, <src>/wend-<PR or main>-<sha7>, created from <src>/wend at SHA; a worktree that
  exists must be at SHA with `git status --porcelain` empty, or the job is refused. Worktrees are left in place.
- Slots: one rehearse at a time; at most 3 VM jobs (rehearse included) and 2 test jobs at once. A queued job starts
  when its slot is free, everything ranked before it in its class has started, and its --after job has ended.
- The mirror shim (tools/mirror.py on 8085) is shared only by jobs whose tools/mirror.py is byte for byte the same. A
  job whose mirror.py differs waits for the VM jobs using the other one; a shim from another checkout that nothing
  uses is stopped by its exact pid with `vm.py shim-stop`, as the user who started it. Never pkill.
- Logs go to <lab>/logs/<PR or main>/<sha>/<id>/, one file per command (`> log 2>&1`), never into a worktree; the
  verdict (verdict.json there) carries every command's rc and log hash, each run directory's run.json in full, its
  files' hashes, and for a rehearse the plan's checks.toml in full, the walk's hops and every failed step.
- This runner runs only from a clean checkout whose HEAD is on origin (docs/remi.md: <src>/wend-runner, kept at
  origin/main by `update`), so what a job runs is decided by main's copy of this file, not by the commit under test.

Standard library only. It never prints, reads or copies anything under /root/.config/wend or the token files.
"""
import fcntl
import hashlib
import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import time
import tomllib
import traceback
import urllib.error
import urllib.request

sys.dont_write_bytecode = True
TOOLS = os.path.dirname(os.path.abspath(__file__))
RUNNER = os.path.dirname(TOOLS)
LAB = os.environ.get("WEND_LAB", "/data/wend-lab")
SRC = os.environ.get("WEND_LAB_SRC", "/data/dev/src")
BASE = os.path.join(SRC, "wend")  # the clone every job's worktree is added from
CARGO = os.environ.get("WEND_LAB_CARGO", "/data/dev/cache/cargo/bin/cargo")
QUEUE, LOGS = os.path.join(LAB, "queue"), os.path.join(LAB, "logs")
WORK = {"dev": os.path.join(LAB, "vmwork"), "root": os.path.join(LAB, "vmwork-root")}
# tests only (tools/test_remi.py): run root's and kvm's commands as the caller, and skip the runner's own origin check
TEST = os.environ.get("WEND_LAB_TEST") == "1"
POLL = float(os.environ.get("WEND_LAB_POLL", "10"))
PART = 48000  # under a cross-session message's 64 KiB, with room for the header
SHA = re.compile(r"[0-9a-f]{40}")
ROUTE = re.compile(r"routes/[a-z0-9][a-z0-9_-]*/[a-z0-9][a-z0-9._-]*\.toml")
BOX = re.compile(r"[a-z0-9][a-z0-9_-]*/[a-z0-9][a-z0-9._-]*")
JOBS = {  # name: (slot class, argument patterns, hours before it is stopped)
    "tests": ("tests", [], 1),
    "build": ("vm", [BOX], 3),
    "replay": ("vm", [ROUTE, BOX], 8),
    "relocate": ("vm", [BOX], 3),
    "rehearse": ("rehearse", [ROUTE, BOX], 4),
}
SLOTS = {"rehearse": 1, "vm": 3, "tests": 2}  # a rehearse also takes a vm slot
VM_FLAGS = {"--uefi": None, "--no-hwaddr": None, "--lvm": None, "--relocate": None,
            # what tools/vm.py's main takes: ROOT_FS's keys, and 1 to 500 GB
            "--root-fs": re.compile(r"ext3|ext4|xfs|xfs-v4"), "--extra-disk": re.compile(r"[1-9][0-9]?|[1-4][0-9]{2}|500")}
DONE = ("done", "refused", "error", "cancelled")


class Refused(Exception):
    pass


def git(*args, cwd=BASE, check=True):
    p = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", cwd, *args], capture_output=True, text=True,
                       timeout=600)
    if check and p.returncode:
        raise Refused(f"git {' '.join(args)}: {p.stderr.strip()[-500:]}")
    return p.stdout.strip()


def vm_flags(text):
    """vm.py's flags before the command, checked against what it takes; anything else is refused."""
    args, out = shlex.split(text or ""), []
    while args:
        f = args.pop(0)
        if f not in VM_FLAGS:
            raise Refused(f"--vm-flags: {f!r} isn't one of {', '.join(VM_FLAGS)}")
        out.append(f)
        if VM_FLAGS[f]:
            if not args or not VM_FLAGS[f].fullmatch(args[0]):
                raise Refused(f"--vm-flags: {f} needs a value matching {VM_FLAGS[f].pattern}")
            out.append(args.pop(0))
    return out


class GitLock:
    """One fetch or worktree add at a time in the base clone: concurrent ones fail on git's own ref locks."""
    def __enter__(self):
        os.makedirs(QUEUE, exist_ok=True)
        self.f = open(os.path.join(QUEUE, ".git-lock"), "a")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        self.f.close()


def trusted(sha, pr):
    """Where SHA sits on origin, after a fresh fetch: refused unless it is a branch tip there or on origin/main, and
    (with pr) PR pr's head right now. A pull request's head from a fork is never a branch tip on origin."""
    with GitLock():
        return where_on_origin(sha, pr)


def where_on_origin(sha, pr):
    git("fetch", "--quiet", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*")
    out = {}
    if pr:
        git("fetch", "--quiet", "origin", f"+refs/pull/{pr}/head:refs/wend-lab/pr/{pr}")
        out["pr_head"] = git("rev-parse", f"refs/wend-lab/pr/{pr}")
        if out["pr_head"] != sha:
            raise Refused(f"{sha} isn't PR #{pr}'s head (it is {out['pr_head']} now): send the job again at that head")
    tips = [l.split() for l in git("for-each-ref", "--format=%(objectname) %(refname:short)",
                                   "refs/remotes/origin/").splitlines()]
    hit = [ref for obj, ref in tips if obj == sha and ref != "origin/HEAD"]
    if hit:
        out["origin"] = f"tip of {hit[0]}"
    elif subprocess.run(["git", "-C", BASE, "merge-base", "--is-ancestor", sha, "origin/main"]).returncode == 0:
        out["origin"] = "on origin/main"
    else:
        raise Refused(f"{sha} is neither a branch tip on origin nor on origin/main; push it to a branch there first")
    return out


def runner_check():
    """This file's own checkout must be clean and on origin, or nothing it decides can be trusted."""
    if TEST:
        return {"commit": "test", "path": RUNNER}
    head = git("rev-parse", "HEAD", cwd=RUNNER)
    if git("status", "--porcelain", cwd=RUNNER):
        raise Refused(f"the runner checkout {RUNNER} has local changes; it must be clean")
    on = subprocess.run(["git", "-C", RUNNER, "branch", "-r", "--contains", head], capture_output=True, text=True)
    if not on.stdout.strip():
        raise Refused(f"the runner checkout {RUNNER} is at {head}, which is on no origin branch")
    return {"commit": head, "path": RUNNER, "on_main": "origin/main" in on.stdout.split()}


# ---- the queue: one JSON file per job under <lab>/queue, changed only under <lab>/queue/.lock -------------------

class QueueLock:
    def __enter__(self):
        os.makedirs(QUEUE, exist_ok=True)
        self.f = open(os.path.join(QUEUE, ".lock"), "a")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        self.f.close()


def entry_path(jid):
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[a-z]+-[0-9a-f]{4}", jid):
        raise Refused(f"no such job id: {jid!r}")
    return os.path.join(QUEUE, jid + ".json")


def load(jid):
    try:
        return json.load(open(entry_path(jid)))
    except FileNotFoundError:
        raise Refused(f"no job {jid} in {QUEUE}")


def save(e):
    p = entry_path(e["id"])
    with open(p + ".tmp", "w") as f:
        json.dump(e, f, indent=1)
    os.replace(p + ".tmp", p)


def entries():
    out = []
    for f in sorted(os.listdir(QUEUE)) if os.path.isdir(QUEUE) else []:
        if f.endswith(".json"):
            try:
                out.append(json.load(open(os.path.join(QUEUE, f))))
            except (OSError, ValueError):
                pass
    return out


def runner_fd(e):
    """A pidfd for job e's runner process (`remi.py run ID`), or None once it has ended. The pidfd names that process,
    not its number, and its command line is read while the pidfd says it lives, so a reused pid is never taken for it."""
    try:
        fd = os.pidfd_open(e.get("pid") or 0)
    except (ProcessLookupError, ValueError, OSError):
        return None
    try:
        cmd = open(f"/proc/{e['pid']}/cmdline", "rb").read().split(b"\0")
        signal.pidfd_send_signal(fd, 0)
        if b"run" in cmd and e["id"].encode() in cmd:
            return fd
    except (OSError, ProcessLookupError):
        pass
    os.close(fd)
    return None


def alive(e):
    fd = runner_fd(e)
    if fd is None:
        return False
    os.close(fd)
    return True


def update(jid, **kw):
    with QueueLock():
        e = load(jid)
        e.update(kw)
        save(e)
        return e


def enqueue(args):
    job, rest = args[0] if args else None, args[1:]
    if job not in JOBS:
        raise Refused(f"no job {job!r}; jobs: {', '.join(JOBS)}")
    opts, pos = {}, []
    while rest:
        a = rest.pop(0)
        if a in ("--sha", "--pr", "--by", "--reply-to", "--after", "--vm-flags"):
            if not rest:
                raise Refused(f"{a} needs a value")
            opts[a[2:]] = rest.pop(0)
        else:
            pos.append(a)
    pats = JOBS[job][1]
    if len(pos) != len(pats) or not all(p.fullmatch(a) for p, a in zip(pats, pos)):
        raise Refused(f"{job} takes {' '.join(p.pattern for p in pats) or 'no arguments'}; got {pos}")
    sha, pr = opts.get("sha", ""), opts.get("pr")
    if not SHA.fullmatch(sha):
        raise Refused("--sha must be a full 40-hex commit")
    if pr is not None and not re.fullmatch(r"[1-9][0-9]{0,6}", pr):
        raise Refused("--pr must be a PR number")
    if not opts.get("by"):
        raise Refused("--by must say who asked (the thread's title)")
    flags = vm_flags(opts.get("vm-flags"))
    if flags and JOBS[job][0] == "tests":
        raise Refused("--vm-flags is for VM jobs")
    runner_check()
    where = trusted(sha, pr)
    with QueueLock():
        if opts.get("after"):
            load(opts["after"])
        jid = f"{time.strftime('%Y%m%d-%H%M%S')}-{job}-{secrets.token_hex(2)}"
        e = {"id": jid, "job": job, "args": pos, "vm_flags": flags, "sha": sha, "pr": int(pr) if pr else None,
             "by": opts["by"], "reply_to": opts.get("reply-to"), "after": opts.get("after"), "rank": time.time(),
             "state": "queued", "enqueued": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "origin": where,
             "delivered": False}
        save(e)
        ahead = [x for x in entries() if x["state"] in ("queued", "running") and x["id"] != jid]
    if os.environ.get("WEND_LAB_NO_WORKER") != "1":
        start_worker()
    print(f"queued {jid}: {job} {' '.join(pos)} at {sha[:12]} ({where['origin']}); {len(ahead)} job(s) ahead")
    return 0


def start_worker():
    os.makedirs(QUEUE, exist_ok=True)
    with open(os.path.join(QUEUE, "worker.log"), "a") as log:
        subprocess.Popen([sys.executable, "-P", "-u", os.path.abspath(__file__), "worker"], stdin=subprocess.DEVNULL,
                         stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


def mirror_hash(sha):
    """sha256 of tools/mirror.py at sha: jobs share the shim only when it is the same file."""
    p = subprocess.run(["git", "-C", BASE, "show", f"{sha}:tools/mirror.py"], capture_output=True)
    return hashlib.sha256(p.stdout).hexdigest() if p.returncode == 0 else None


def shim_urls():
    out = set()
    d = os.path.join(RUNNER, "adapters")
    for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if f.endswith(".toml"):
            m = tomllib.load(open(os.path.join(d, f), "rb")).get("mirror")
            if m:
                out.add(m["shim"])
    return sorted(out)


def shim_have(url):
    """What the shim listening at url says it is: its mirror.py's sha256, 'older' for one from before it said, or None
    when nothing listens."""
    try:
        with urllib.request.urlopen(f"{url}/.wend-shim", timeout=5) as r:
            return r.read().decode(errors="replace").strip()
    except urllib.error.HTTPError:
        return "older"
    except Exception:
        return None


def shim_pid(port):
    """The pid and uid of the tools/mirror.py process serving port, from /proc (exact pid; never a pattern kill)."""
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read().decode(errors="replace").split("\0")
            uid = os.stat(f"/proc/{pid}").st_uid
        except OSError:
            continue
        at = next((i for i in (1, 2) if i < len(cmd) and os.path.basename(cmd[i]) == "mirror.py"), None)
        if at and at + 1 < len(cmd) and cmd[at + 1] == str(port):
            return int(pid), uid
    return None, None


def as_user(user, argv, cwd):
    """argv as user (dev under `sg kvm`, or root with sudo -n), with that user's WEND_WORK and dev's umask 0002."""
    env = f"WEND_WORK={WORK[user]}"
    if user == "root":
        return (["env", env] if TEST else ["sudo", "-n", "env", env]) + argv
    line = f"umask 0002; cd {shlex.quote(cwd)} && exec env {env} " + " ".join(shlex.quote(a) for a in argv)
    return ["sh", "-c", line] if TEST else ["sg", "kvm", "-c", line]


def shim_stop(url):
    """Stop the shim at url if nothing uses it; None when stopped, else why not."""
    port = url.rsplit(":", 1)[1]
    pid, uid = shim_pid(port)
    if pid is None:
        return f"a shim answers on {port} but no tools/mirror.py process serves it"
    user = "root" if uid == 0 else "dev"
    argv = as_user(user, ["python3", "-P", os.path.join(TOOLS, "vm.py"), "shim-stop", str(pid)], RUNNER)
    p = subprocess.run(argv, cwd=RUNNER, capture_output=True, text=True)
    print(f"{time.strftime('%H:%M:%S')} shim-stop {pid} ({user}): rc={p.returncode} {p.stdout.strip()[-300:]}",
          flush=True)
    return None if p.returncode == 0 else f"the shim on {port} (pid {pid}) is in use outside the queue: rc {p.returncode}"


def blocker(e, running, ranked):
    """Why queued job e can't start now, or None."""
    cls = JOBS[e["job"]][0]
    if e.get("after"):
        try:
            if load(e["after"])["state"] not in DONE:
                return f"after {e['after']}"
        except Refused:
            pass
    for x in ranked:  # the lab order holds within a class: nothing jumps a job ranked before it
        if x is e:
            break
        if JOBS[x["job"]][0] == cls:
            return f"behind {x['id']}"
    use = {c: sum(1 for x in running if JOBS[x["job"]][0] == c) for c in SLOTS}
    vm = use["vm"] + use["rehearse"]
    if cls == "tests" and use["tests"] >= SLOTS["tests"]:
        return "test slots full"
    if cls == "rehearse" and use["rehearse"] >= SLOTS["rehearse"]:
        return "a rehearse is running (one at a time)"
    if cls != "tests":
        if vm >= SLOTS["vm"]:
            return f"{vm} VM jobs running"
        want = mirror_hash(e["sha"])
        others = [x["id"] for x in running if JOBS[x["job"]][0] != "tests" and x.get("mirror") != want]
        if others:
            return f"the mirror shim: {', '.join(others)} use another tools/mirror.py"
        e["mirror"] = want
        if not any(JOBS[x["job"]][0] != "tests" for x in running):
            for url in shim_urls():
                have = shim_have(url)
                if have is not None and have != want:
                    why = shim_stop(url)
                    if why:
                        return why
    return None


def worker():
    os.makedirs(QUEUE, exist_ok=True)
    wl = open(os.path.join(QUEUE, "worker.lock"), "a")
    try:
        fcntl.flock(wl, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0  # one worker already runs the queue
    print(f"{time.strftime('%H:%M:%S')} worker {os.getpid()} up", flush=True)
    kids = []
    while True:
        kids = [k for k in kids if k.poll() is None]
        with QueueLock():
            all_ = entries()
            for x in all_:  # a job whose runner died without a verdict
                if x["state"] == "running" and not alive(x):
                    x.update(state="error", ended=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                             reason="its runner process died without writing a verdict")
                    save(x)
            running = [x for x in all_ if x["state"] == "running"]
            ranked = sorted((x for x in all_ if x["state"] == "queued"), key=lambda x: x["rank"])
            if not ranked and not running:
                fcntl.flock(wl, fcntl.LOCK_UN)  # before the queue lock goes: an enqueue after this starts a worker
                print(f"{time.strftime('%H:%M:%S')} worker {os.getpid()} done: queue empty", flush=True)
                return 0
            for e in ranked:
                why = blocker(e, running, ranked)
                if why:
                    if e.get("waiting") != why:
                        e["waiting"] = why
                        save(e)
                    continue
                with open(os.path.join(QUEUE, "worker.log"), "a") as log:
                    k = subprocess.Popen([sys.executable, "-P", "-u", os.path.abspath(__file__), "run", e["id"]],
                                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
                kids.append(k)
                e.update(state="running", pid=k.pid, waiting=None, started=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
                save(e)
                running.append(e)
                ranked = [x for x in ranked if x is not e]
                print(f"{time.strftime('%H:%M:%S')} started {e['id']} (pid {k.pid})", flush=True)
        time.sleep(POLL)


# ---- one job ----------------------------------------------------------------------------------------------------

def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def tail_text(p, n):
    with open(p, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - n))
        return f.read().decode(errors="replace")


CURRENT = {}  # the command running now, for a cancel or a timeout


def command(v, name, argv, cwd, deadline):
    """Run argv with its output in its own log (`> log 2>&1`), and record it in the verdict."""
    n = len(v["commands"]) + 1
    path = os.path.join(v["log_dir"], f"{n:02d}-{name}.log")
    t0 = time.time()
    with open(path, "w") as out:
        p = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                             start_new_session=True)
        CURRENT["p"] = p
        stopped = None
        try:
            rc = p.wait(timeout=max(1, deadline - time.time()))
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as x:
            stop_current()
            rc, stopped = p.wait(), x
        CURRENT.pop("p", None)
    if isinstance(stopped, subprocess.TimeoutExpired):
        v["reasons"].append(f"{name} ran past the job's time limit and was stopped")
    c = {"name": name, "argv": argv, "cwd": cwd, "rc": rc, "seconds": round(time.time() - t0),
         "log": path, "bytes": os.path.getsize(path), "sha256": sha256_file(path), "tail": tail_text(path, 4000)}
    v["commands"].append(c)
    if isinstance(stopped, KeyboardInterrupt):
        raise stopped
    return c


def stop_current():
    p = CURRENT.get("p")
    if p and p.poll() is None:
        for sig, wait in ((signal.SIGTERM, 60), (signal.SIGKILL, 10)):
            try:
                os.killpg(p.pid, sig)  # sudo relays it to vm.py, whose SIGTERM handler stops its guests
            except ProcessLookupError:
                return
            try:
                p.wait(timeout=wait)
                return
            except subprocess.TimeoutExpired:
                pass


def worktree(e):
    """The job's own worktree at its sha: created if missing, refused unless at sha and clean if not."""
    wt = os.path.join(SRC, f"wend-{e['pr'] or 'main'}-{e['sha'][:7]}")
    if not os.path.exists(wt):
        with GitLock():
            git("worktree", "add", "--detach", wt, e["sha"])
    head = git("rev-parse", "HEAD", cwd=wt)
    dirty = git("status", "--porcelain", cwd=wt)
    if head != e["sha"]:
        raise Refused(f"{wt} is at {head}, not {e['sha']}; it is left as it is")
    if dirty:
        raise Refused(f"{wt} has local changes (git status --porcelain: {dirty[:300]!r}); it is left as it is")
    return wt, {"head": head, "porcelain": dirty}


RUN_LINE = re.compile(r"^\S+ (PASS|FAIL|PARTIAL\S*)\S*: .* in (\.wend/vm/\S+)$", re.M)
KEY = re.compile(r"(?i)(error|fail|refus|warn|denied|pass|fired|generated|hop |plan|walk|fingerprint|checks?)")
LISTED = ("plan",)  # run directory subdirectories whose files are listed; copy/ and work/ are a whole box root


def run_dir(d, v):
    """Everything a reviewer reads from one vm.py run directory."""
    r = {"dir": d, "files": []}
    names = [n for n in sorted(os.listdir(d)) if os.path.isfile(os.path.join(d, n))]
    names += [f"{s}/{n}" for s in LISTED if os.path.isdir(os.path.join(d, s))
              for n in sorted(os.listdir(os.path.join(d, s)))]
    for n in names:
        p = os.path.join(d, n)
        size = os.path.getsize(p)
        r["files"].append({"path": n, "bytes": size, "sha256": sha256_file(p) if size < (16 << 20) else None})
    try:
        r["run_json"] = json.load(open(os.path.join(d, "run.json")))
    except (OSError, ValueError) as x:
        r["run_json"] = None
        v["reasons"].append(f"{d}: no readable run.json ({x})")
        return r
    rj = r["run_json"]
    head = rj.get("head") or {}
    if head.get("commit") != v["sha"] or head.get("dirty"):
        v["reasons"].append(f"{d}: run.json head is {head}, not {v['sha']} clean")
    if rj.get("result") != "pass":
        v["reasons"].append(f"{d}: run.json result is {rj.get('result')!r}")
    log = os.path.join(d, "rehearse.log")
    if os.path.exists(log):
        lines = open(log, errors="replace").read().splitlines()
        r["rehearse_log"] = {"lines": len(lines), "key_lines": [l for l in lines if KEY.search(l)][-400:],
                             "tail": lines[-200:]}
    checks = os.path.join(d, "plan", "checks.toml")
    if os.path.exists(checks):
        text = open(checks, errors="replace").read()
        r["checks_toml"] = text
        try:
            gen = tomllib.loads(text).get("generated")
            r["generated"] = sorted(gen) if isinstance(gen, dict) else gen
        except tomllib.TOMLDecodeError as x:
            r["generated"] = f"checks.toml doesn't parse: {x}"
    plan = os.path.join(d, "plan", "plan.json")
    if os.path.exists(plan):
        r["plan_json"] = json.load(open(plan)) if os.path.getsize(plan) < (256 << 10) else "over 256 KiB: see files"
    walk = os.path.join(d, "walk.json")
    if os.path.exists(walk):
        w = json.load(open(walk))
        r["walk"] = {"hops": [{k: h.get(k) for k in ("to", "ok", "fired_names", "seconds")} for h in w.get("hops", [])],
                     "final": w.get("final")}
    steps = os.path.join(d, "steps.json")
    if os.path.exists(steps):
        s = json.load(open(steps))
        r["steps"] = {"count": len(s), "failed": [x for x in s if x.get("ok") is False],
                      "note": "each step's output keeps only its last bytes (the client's own limit)"}
    return r


def verdict_path(e):
    return os.path.join(LOGS, str(e["pr"] or "main"), e["sha"], e["id"], "verdict.json")


def interrupted(*_):
    raise KeyboardInterrupt  # `cancel` sends SIGTERM: stop the command, then write a cancelled verdict


def run(jid):
    os.umask(0o002)
    e = load(jid)
    v = {"schema": "wend.remijob/0", **{k: e[k] for k in ("id", "job", "args", "vm_flags", "sha", "pr", "by",
                                                         "reply_to", "enqueued")},
         "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "host": os.uname().nodename, "log_dir": None,
         "commands": [], "runs": [], "reasons": [], "result": None}
    v["log_dir"] = os.path.dirname(verdict_path(e))
    os.makedirs(v["log_dir"], exist_ok=True)
    signal.signal(signal.SIGTERM, interrupted)
    t0 = time.time()
    deadline = t0 + JOBS[e["job"]][2] * 3600
    try:
        v["runner"] = runner_check()
        v["origin"] = trusted(e["sha"], e["pr"])
        wt, v["preflight"] = worktree(e)
        v["worktree"] = wt
        if e["job"] == "tests":
            tests(v, wt, deadline)
        else:
            vm_job(e, v, wt, deadline)
        v["result"] = "pass" if not v["reasons"] else "fail"
    except Refused as x:
        v["result"], v["reasons"] = "refused", v["reasons"] + [str(x)]
    except KeyboardInterrupt:
        stop_current()
        v["result"], v["reasons"] = "cancelled", v["reasons"] + ["cancelled"]
    except Exception:
        v["result"], v["reasons"] = "error", v["reasons"] + [traceback.format_exc()[-3000:]]
    v["ended"], v["seconds"] = time.strftime("%Y-%m-%dT%H:%M:%S%z"), round(time.time() - t0)
    v["remi_verdict"] = remi_verdict(v)
    with open(verdict_path(e) + ".tmp", "w") as f:
        json.dump(v, f, indent=1)
    os.replace(verdict_path(e) + ".tmp", verdict_path(e))
    state = {"pass": "done", "fail": "done"}.get(v["result"], v["result"])
    update(jid, state=state, result=v["result"], ended=v["ended"], verdict=verdict_path(e))
    print(f"{time.strftime('%H:%M:%S')} {jid}: {v['result'].upper()} ({verdict_path(e)})", flush=True)
    return 0


def remi_verdict(v):
    """The remi-verdict workflow's inputs, ready to send (CONTRIBUTING "Proof on remi"): only a pass or a fail."""
    if v["result"] not in ("pass", "fail") or not v["pr"]:
        return None
    what = " ".join(a.replace("routes/", "").removesuffix(".toml") for a in v["args"]) or "tools tests"
    flags = f" {' '.join(v['vm_flags'])}" if v["vm_flags"] else ""
    return {"pr": str(v["pr"]), "sha": v["sha"], "state": "success" if v["result"] == "pass" else "failure",
            "summary": f"{v['job']} {what}{flags}: {'passed' if v['result'] == 'pass' else 'failed'} (job {v['id']})"}


def tests(v, wt, deadline):
    names = sorted(n for n in os.listdir(os.path.join(wt, "tools")) if re.fullmatch(r"test_\w+\.py", n))
    for n in names + ["check.py"]:
        c = command(v, n[:-3], ["python3", "-B", "-P", f"tools/{n}"], wt, deadline)
        if c["rc"]:
            v["reasons"].append(f"tools/{n} exited {c['rc']}")


def vm_job(e, v, wt, deadline):
    box = e["args"][-1]
    for a in e["args"]:
        if not os.path.exists(os.path.join(wt, a if a.startswith("routes/") else f"boxes/{a}/box.toml")):
            raise Refused(f"{a} doesn't exist at {e['sha']}")
    sheltered = "shelter" in tomllib.load(open(os.path.join(wt, "boxes", box, "box.toml"), "rb"))
    user = "root" if e["job"] == "rehearse" or sheltered else "dev"
    v["user"] = user
    tail = list(e["args"])
    if e["job"] == "rehearse":
        c = command(v, "cargo-build", as_user("dev", [CARGO, "build", "--release", "--locked"], os.path.join(wt, "client")),
                    os.path.join(wt, "client"), deadline)
        if c["rc"]:
            v["reasons"].append(f"cargo build exited {c['rc']}")
            return
        tail += ["--client", "client/target/release/wend"]
    argv = ["python3", "-P", "-u", "tools/vm.py", *e["vm_flags"], e["job"], *tail]
    c = command(v, e["job"], as_user(user, argv, wt), wt, deadline)
    found = RUN_LINE.findall(open(c["log"], errors="replace").read())
    for _, d in found:
        try:
            v["runs"].append(run_dir(os.path.join(wt, d), v))
        except (OSError, ValueError) as x:  # the run stands; the verdict says what it couldn't read
            v["runs"].append({"dir": os.path.join(wt, d), "unreadable": str(x)})
            v["reasons"].append(f"{d}: couldn't read the run directory ({x})")
    if c["rc"]:
        v["reasons"].append(f"tools/vm.py {e['job']} exited {c['rc']}")
    if e["job"] != "build" and not found:
        v["reasons"].append("tools/vm.py named no run directory (its last lines are in commands[-1].tail)")


# ---- reading results ----------------------------------------------------------------------------------------------

def parts(text, part):
    data = text.encode()
    n = max(1, -(-len(data) // PART))
    i = int(part or 1)
    if not 1 <= i <= n:
        raise Refused(f"there are {n} part(s)")
    print(f"part {i}/{n} ({len(data)} bytes in all; concatenate the parts in order)")
    sys.stdout.write(data[(i - 1) * PART:i * PART].decode(errors="replace") + "\n")
    return 0


def opt(args, name):
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else None


def show(args):
    e = load(args[0])
    if not e.get("verdict"):
        raise Refused(f"{e['id']} is {e['state']}: no verdict yet")
    return parts(open(e["verdict"]).read(), opt(args, "--part"))


def file_(args):
    e = load(args[0])
    if not e.get("verdict"):
        raise Refused(f"{e['id']} is {e['state']}: no verdict yet")
    v = json.load(open(e["verdict"]))
    roots = [v["log_dir"]] + [r["dir"] for r in v["runs"]]
    for root in roots:
        p = os.path.realpath(os.path.join(root, args[1]))
        if p.startswith(os.path.realpath(root) + os.sep) and os.path.isfile(p):
            return parts(open(p, errors="replace").read(), opt(args, "--part"))
    raise Refused(f"{args[1]} isn't a file under the job's log directory or its run directories")


def queue(args):
    es = entries()
    if "--json" in args:
        print(json.dumps(es, indent=1))
        return 0
    order = {"running": 0, "queued": 1}
    es.sort(key=lambda x: (order.get(x["state"], 2), x["rank"] if x["state"] == "queued" else 0,
                           -time.mktime(time.strptime(x.get("ended") or x["enqueued"], "%Y-%m-%dT%H:%M:%S%z"))))
    shown = [x for x in es if x["state"] in order or not x["delivered"]]
    shown += [x for x in es if x not in shown][:5]
    for x in shown:
        what = f"{x['job']} {' '.join(x['vm_flags'] + x['args'])} @{x['sha'][:7]}{' #' + str(x['pr']) if x['pr'] else ''}"
        state = x["state"] + (f" ({x.get('result')})" if x.get("result") else "")
        extra = x.get("waiting") or x.get("started") or ""
        if x["state"] in DONE and not x["delivered"]:
            extra = f"UNDELIVERED to {x['reply_to'] or x['by']}"
        print(f"{x['id']}  {state:<16} {what}  by {x['by']}  {extra}")
    return 0


def order(ids):
    with QueueLock():
        qs = [x for x in entries() if x["state"] == "queued"]
        low = min((x["rank"] for x in qs), default=time.time())
        for i, jid in enumerate(ids):
            e = load(jid)
            if e["state"] != "queued":
                raise Refused(f"{jid} is {e['state']}, not queued")
            e["rank"] = low - len(ids) + i
            save(e)
    return queue([])


def cancel(jid):
    with QueueLock():
        e = load(jid)
        if e["state"] == "queued":
            e.update(state="cancelled", ended=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
            save(e)
            print(f"{jid}: cancelled before it started")
            return 0
    fd = runner_fd(e) if e["state"] == "running" else None
    if fd is not None:
        try:
            signal.pidfd_send_signal(fd, signal.SIGTERM)  # its runner stops the command, writes a cancelled verdict
            print(f"{jid}: stopping (runner pid {e['pid']})")
            return 0
        except ProcessLookupError:
            raise Refused(f"{jid} ended just now; see its verdict")
        finally:
            os.close(fd)
    raise Refused(f"{jid} is {e['state']}")


def wait(jid):
    while load(jid)["state"] not in DONE:
        time.sleep(POLL)
    e = load(jid)
    print(f"{jid}: {(e.get('result') or e['state']).upper()} {e.get('verdict') or e.get('reason', '')}")
    return 0


def update_runner():
    if any(x["state"] == "running" for x in entries()):
        raise Refused("a job is running; update the runner when the queue is idle")
    git("fetch", "--quiet", "origin", "+refs/heads/*:refs/remotes/origin/*", cwd=RUNNER)
    if git("status", "--porcelain", cwd=RUNNER):
        raise Refused(f"{RUNNER} has local changes")
    git("checkout", "--quiet", "--detach", "origin/main", cwd=RUNNER)
    print(f"runner at {git('rev-parse', 'HEAD', cwd=RUNNER)}")
    return 0


def main(args):
    cmd, rest = (args[0], args[1:]) if args else (None, [])
    if cmd == "enqueue":
        return enqueue(rest)
    if cmd == "worker" and not rest:
        return worker()
    if cmd == "run" and len(rest) == 1:
        return run(rest[0])
    if cmd == "queue":
        return queue(rest)
    if cmd == "order" and rest:
        return order(rest)
    if cmd == "cancel" and len(rest) == 1:
        return cancel(rest[0])
    if cmd == "wait" and len(rest) == 1:
        return wait(rest[0])
    if cmd == "show" and rest:
        return show(rest)
    if cmd == "file" and len(rest) >= 2:
        return file_(rest)
    if cmd == "delivered" and len(rest) == 1:
        update(rest[0], delivered=True)
        return 0
    if cmd == "update" and not rest:
        return update_runner()
    return __doc__


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Refused as x:
        sys.exit(f"remi.py: refused: {x}")
