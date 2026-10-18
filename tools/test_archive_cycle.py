"""Keeping the archive current, by behaviour (docs/archive.md, "Keeping it current"): remi's hourly cycle with stand-in
tools, and the public check against a made-up signed index. Offline. python3 tools/test_archive_cycle.py"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
w = tempfile.mkdtemp(prefix="wend-test-archive-cycle-")
results = []


def check(ok, what):
    results.append(ok)
    print(("passed  " if ok else "WRONG   ") + what)


def git(*a):
    subprocess.run(["git", "-C", f"{w}/repo", "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True,
                   capture_output=True)


A, B = "a" * 64, "b" * 64
U = "https://repo.example.org/9/repodata/repomd.xml"
os.makedirs(f"{w}/repo/runs/el/r/box")
os.makedirs(f"{w}/repo/archive")
run = lambda h, result="pass": {"result": result, "hops": [{"sources": {U: f"sha256:{h}"}}]}
json.dump(run(A), open(f"{w}/repo/runs/el/r/box/1.json", "w"))
git("init", "-q", "-b", "main")
git("add", "-A")
git("commit", "-q", "-m", "a run lands", "--date=2000-01-01T00:00:00Z")
subprocess.run(["git", "-C", f"{w}/repo", "commit", "--amend", "-q", "--no-edit"], capture_output=True,
               env={**os.environ, "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z", "GIT_COMMITTER_NAME": "t",
                    "GIT_COMMITTER_EMAIL": "t@t"})

# remi's cycle, with stand-ins for gate_fetched.py and archive.py beside a copy of it: each logs its call and exits
# with the code its file says
tools = f"{w}/tools"
os.makedirs(tools)
shutil.copy(f"{HERE}/archive_cycle.py", tools)
stub = """import os, sys
base = os.path.basename(sys.argv[0]).split(".")[0]
name = base + ("-" + sys.argv[1] if base == "archive" else "")
open(os.environ["LOG"], "a").write(name + " " + " ".join(sys.argv[1:]) + "\\n")
if name == "gate_fetched" and os.path.exists(os.environ["NEW"]):
    open(sys.argv[1] + "/.done", "a").write(open(os.environ["NEW"]).read())
    os.remove(os.environ["NEW"])
try:
    sys.exit(int(open(os.environ["RC"] + "/" + name).read()))
except FileNotFoundError:
    sys.exit(0)
"""
for t in ("gate_fetched.py", "archive.py"):
    open(f"{tools}/{t}", "w").write(stub)
open(f"{tools}/push.py", "w").write(stub)
os.makedirs(f"{w}/rc")
os.makedirs(f"{w}/fetched")
env = {**os.environ, "LOG": f"{w}/log", "RC": f"{w}/rc", "NEW": f"{w}/new"}


def cycle(*extra):
    open(f"{w}/log", "w").close()
    p = subprocess.run([sys.executable, "-B", f"{tools}/archive_cycle.py", f"{w}/store", "--fetched", f"{w}/fetched",
                        "--push", f"{sys.executable} -B {tools}/push.py", "--repo", f"{w}/repo", *extra],
                       capture_output=True, text=True, env=env)
    return p, [line.split()[0] for line in open(f"{w}/log")]


def rc(name, code):
    open(f"{w}/rc/{name}", "w").write(str(code))


os.makedirs(f"{w}/store")
p, steps = cycle("--tree", f"{w}/tree")
check(p.returncode == 0 and steps == ["gate_fetched", "archive-ingest", "archive-verify", "push"],
      "a first cycle downloads, ingests, verifies and pushes")
check(f"--tree {w}/tree" in open(f"{w}/log").read().split("\n")[1] and
      f"--lost {w}/repo/archive/lost.txt" in open(f"{w}/log").read(),
      "the ingest gets the extra sources and the checkout's lost.txt")
check(f"--on-main {w}/repo" in open(f"{w}/log").read().split("\n")[0],
      "the download takes only artifacts of commits the checkout's main has")
p, steps = cycle()
check(p.returncode == 0 and steps == ["gate_fetched"] and "nothing new" in p.stdout,
      "nothing new on main and no new artifact: the cycle stops after the download")
open(f"{w}/new", "w").write("123\n")
p, steps = cycle()
check(p.returncode == 0 and steps[-1] == "push", "a new artifact starts a full cycle")
json.dump(run(B), open(f"{w}/repo/runs/el/r/box/2.json", "w"))
git("add", "-A")
git("commit", "-q", "-m", "another run lands")
p, steps = cycle()
check(p.returncode == 0 and steps[-1] == "push", "a new run on main starts a full cycle")
p, steps = cycle("--every", "0")
check(steps[-1] == "push", "past --every hours a cycle runs even with nothing new")
rc("archive-ingest", 1)
p, steps = cycle("--every", "0")
check(p.returncode == 1 and steps[-1] == "push" and "missed" in p.stdout,
      "an ingest that missed files still pushes what it stored, and fails the cycle")
p, steps = cycle()
check(steps[-1] == "push", "after a cycle that missed files the next one tries again, though nothing is new")
rc("archive-ingest", 2)
p, steps = cycle()
check(p.returncode == 0 and steps == ["gate_fetched", "archive-ingest"] and "holds the store" in p.stdout,
      "another ingest holding the store: this cycle steps aside")
rc("archive-ingest", 0)
rc("archive-verify", 1)
p, steps = cycle()
check(p.returncode == 1 and "push" not in steps, "a store that doesn't verify is never pushed")
rc("archive-verify", 0)
rc("push", 1)
p, steps = cycle()
check(p.returncode == 1 and "push failed" in p.stdout, "a failed push fails the cycle")
p, steps = cycle()
check(steps[-1] == "push", "and the next cycle pushes again")
os.remove(f"{w}/rc/push")
rc("gate_fetched", 1)
p, steps = cycle()
check(p.returncode == 1 and steps == ["gate_fetched"], "a failed download fails the cycle before anything else")
os.remove(f"{w}/rc/gate_fetched")
for d in ("111", "222"):
    os.makedirs(f"{w}/fetched/{d}")
os.utime(f"{w}/fetched/111", (0, 0))
rc("archive-ingest", 1)
p, _ = cycle("--every", "0")
check(os.path.isdir(f"{w}/fetched/111"), "a cycle that missed files prunes nothing")
rc("archive-ingest", 0)
p, _ = cycle("--every", "0")
check(not os.path.exists(f"{w}/fetched/111") and os.path.isdir(f"{w}/fetched/222") and "pruned 1" in p.stdout,
      "a good cycle prunes artifacts unpacked over --keep-days ago, and keeps newer ones")
p, _ = cycle("--fetch", "x")
check(p.returncode == 2 and "isn't an option" in p.stdout, "a mistyped option stops the cycle")

# the public check
if shutil.which("ssh-keygen"):
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "k", "-f", f"{w}/key"], check=True)
    signers = f"{w}/allowed_signers"
    open(signers, "w").write(f'wend-archive namespaces="wend-archive" {open(f"{w}/key.pub").read()}')
    os.makedirs(f"{w}/idx")

    def index(*hexes, sign=True):
        p = f"{w}/idx/latest.json"
        json.dump({"schema": "wend.archive/0", "generated": "x",
                   "files": {U: {f"sha256:{h}": {"size": 1} for h in hexes}}}, open(p, "w"))
        if os.path.exists(f"{p}.sig"):
            os.remove(f"{p}.sig")
        if sign:
            subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", f"{w}/key", "-n", "wend-archive", p], check=True,
                           capture_output=True)

    def ac(*extra):
        return subprocess.run([sys.executable, "-B", f"{HERE}/archive_check.py", "--index", f"{w}/idx/latest.json",
                               "--signers", signers, "--runs", f"{w}/repo/runs", "--lost", f"{w}/repo/archive/lost.txt",
                               *extra], capture_output=True, text=True)

    index(A, B)
    p = ac()
    check(p.returncode == 0 and "0 overdue, 0 pending" in p.stdout, "every pin archived: the check passes")
    index(A, B, sign=False)
    p = ac()
    check(p.returncode == 1 and "can't read" in p.stdout, "an index without its signature fails the check")
    index(A, B)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "o", "-f", f"{w}/other"], check=True)
    os.remove(f"{w}/idx/latest.json.sig")
    subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", f"{w}/other", "-n", "wend-archive", f"{w}/idx/latest.json"],
                   check=True, capture_output=True)
    p = ac()
    check(p.returncode == 1 and "isn't signed" in p.stdout, "an index signed by a key not in signers fails the check")
    index(B)
    p = ac()
    check(p.returncode == 1 and f"OVERDUE {U} sha256:{A}" in p.stdout, "a pin that landed long ago and isn't archived "
          "is overdue")
    index(A)
    p = ac()
    check(p.returncode == 0 and f"pending {U} sha256:{B}" in p.stdout,
          "a pin whose run landed under the grace period ago is pending, not a failure")
    p = ac("--grace", "0")
    check(p.returncode == 1 and f"OVERDUE {U} sha256:{B}" in p.stdout, "past the grace period it is overdue")
    index(B)
    open(f"{w}/repo/archive/lost.txt", "w").write(f"{U} sha256:{A}  # replaced before capture\n")
    p = ac()
    check(p.returncode == 0 and "1 known lost" in p.stdout and "OVERDUE" not in p.stdout,
          "a pin lost.txt lists doesn't fail the check")
    index(A, B)
    p = ac()
    check(p.returncode == 0 and f"found   {U} sha256:{A}" in p.stdout, "a lost pin the archive now holds is pointed out")
    json.dump(run("c" * 64, "fail"), open(f"{w}/repo/runs/el/r/box/3.json", "w"))
    p = ac()
    check(p.returncode == 0 and "2 pins" in p.stdout, "a failed run's pins aren't checked")
shutil.rmtree(w)
sys.exit(0 if all(results) else 1)
