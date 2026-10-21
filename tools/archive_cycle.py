"""Keep the archive current (docs/archive.md, "Keeping it current"). Runs on remi from a checkout of main, hourly
(archive/remi/wend-archive.timer). Standard library only.

  python3 tools/archive_cycle.py STORE --fetched DIR --push CMD [--token-file FILE] [--tree DIR]... [--blobs DIR]...
                                 [--every HOURS] [--keep-days DAYS]

A pin of a live repo rots within hours (EPEL replaced two of #163's overnight), and the gate's artifacts keep its
bytes only 14 days, so every run that lands on main has to reach the public archive the day it lands. One cycle:

  1. tools/gate_fetched.py DIR --runs --have --lost: download the gate's fetched-N artifacts behind the passing runs
     on main no ingest has walked yet (lost.txt aside), and no others.
  2. If main's runs/ and tools/ are what the last good cycle saw, no artifact is new, and that cycle is under HOURS
     old (default 24), stop: nothing to do. A merged change to the archive's own code (a new source kind) is new.
  3. tools/archive.py ingest STORE --fetched DIR (plus any --tree and --blobs), the origin on.
  4. tools/archive.py verify STORE, signatures aside (the push signs): a bad store is never pushed.
  5. CMD, which signs and pushes (on remi, `sudo -n /data/wend-lab/scripts/r2push-run.sh`). It runs even when the
     ingest missed something: what was stored is good, and the index names only stored blobs.
  6. After a cycle that found everything, delete artifacts unpacked over DAYS ago (default 30): their pinned bytes
     are stored, and only runs already on main had theirs downloaded. To ingest one again, delete its artifact id
     from DIR/.done and the next cycle downloads it again if its run still needs it (GitHub keeps it 14 days).

Exits 1 when any step fails or the ingest missed something archive/lost.txt doesn't excuse, so the timer's unit goes
red; the public check (.github/workflows/archive-check.yml) opens an issue when a pin stays unarchived. Exits 0 when
another ingest holds the store: the next cycle catches up. STORE/.cycle remembers the last good cycle.
"""
import json
import os
import shlex
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def run(argv, env=None):
    print("+ " + " ".join(shlex.quote(a) for a in argv), flush=True)
    return subprocess.run(argv, env=env).returncode


def done_count(fetched):
    try:
        return len(open(f"{fetched}/.done").read().split())
    except FileNotFoundError:
        return 0


def prune(fetched, days):
    """After a good cycle, drop unpacked artifacts older than days: what passing runs pin from them is stored. .done
    keeps their ids, so they aren't downloaded again."""
    old = time.time() - days * 86400
    gone = 0
    for name in os.listdir(fetched):
        p = f"{fetched}/{name}"
        if name.isdigit() and os.path.isdir(p) and os.path.getmtime(p) < old:
            shutil.rmtree(p)
            gone += 1
    if gone:
        print(f"archive_cycle: pruned {gone} artifacts unpacked over {days:g} days ago")


def main(argv):
    if not argv or argv[0].startswith("-"):
        print(__doc__)
        return 2
    store, rest = argv[0], argv[1:]
    opts = {"--fetched": [], "--push": [], "--token-file": [], "--tree": [], "--blobs": [], "--every": ["24"], "--keep-days": ["30"],
            "--repo": [os.path.dirname(HERE)]}  # --repo: the checkout whose runs/ ingest reads, for tests
    while rest:
        a = rest.pop(0)
        if a in opts and rest:
            opts[a].append(rest.pop(0))
        else:
            print(f"archive_cycle: {a!r} isn't an option, or has no value\n{__doc__}")
            return 2
    if not opts["--fetched"] or not opts["--push"]:
        print(f"archive_cycle: --fetched and --push are required\n{__doc__}")
        return 2
    fetched, push, repo = opts["--fetched"][-1], opts["--push"][-1], opts["--repo"][-1]
    py = [sys.executable, "-P", "-u"]

    before = done_count(fetched)
    gate = [*py, f"{HERE}/gate_fetched.py", fetched, "--runs", f"{repo}/runs", "--have", f"{store}/index/latest.json",
            "--lost", f"{repo}/archive/lost.txt"]
    gate += ["--token-file", opts["--token-file"][-1]] if opts["--token-file"] else []
    if run(gate):
        print("archive_cycle: downloading the gate's artifacts failed")
        return 1
    r = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD", "HEAD:runs"], capture_output=True, text=True)
    if r.returncode:
        print(f"archive_cycle: {repo} isn't a checkout with runs/: {r.stderr.strip()}")
        return 1
    head, runs = r.stdout.split()
    tools_tree = subprocess.run(["git", "-C", repo, "ls-tree", "HEAD", "tools"], capture_output=True, text=True).stdout
    try:
        last = json.load(open(f"{store}/.cycle"))
    except (FileNotFoundError, ValueError):
        last = {}
    if ((last.get("runs"), last.get("tools")) == (runs, tools_tree)
            and done_count(fetched) == before == last.get("artifacts")
            and time.time() - last.get("ok", 0) < float(opts["--every"][-1]) * 3600):
        print(f"archive_cycle: nothing new since {time.strftime('%Y-%m-%d %H:%M', time.gmtime(last['ok']))} UTC "
              f"(main {last.get('head', '?')[:7]})")
        return 0

    ingest = [*py, f"{HERE}/archive.py", "ingest", store, "--fetched", fetched, "--runs", f"{repo}/runs",
              "--lost", f"{repo}/archive/lost.txt"]
    for flag in ("--tree", "--blobs"):
        for d in opts[flag]:
            ingest += [flag, d]
    got = run(ingest)
    if got == 1:  # no good cycle on record until one finds everything, so each cycle tries the rest again
        try:
            os.remove(f"{store}/.cycle")
        except FileNotFoundError:
            pass
    if got == 2:
        print("archive_cycle: another ingest, sign or push holds the store; the next cycle catches up")
        return 0
    if got not in (0, 1):
        print(f"archive_cycle: ingest failed (exit {got}); nothing pushed")
        return 1
    if run([*py, f"{HERE}/archive.py", "verify", store], env={**os.environ, "WEND_ARCHIVE_SIGNERS": ""}):
        print("archive_cycle: the store doesn't verify; nothing pushed")
        return 1
    if run(shlex.split(push)):
        print("archive_cycle: the push failed")
        return 1
    if got == 1:
        print("archive_cycle: pushed, but the ingest missed files lost.txt doesn't list (above)")
        return 1
    tmp = f"{store}/.cycle.tmp{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump({"head": head, "runs": runs, "tools": tools_tree, "artifacts": done_count(fetched),
                   "ok": time.time()}, f)
    os.replace(tmp, f"{store}/.cycle")
    prune(fetched, float(opts["--keep-days"][-1]))
    print(f"archive_cycle: main {head[:7]} archived and pushed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
