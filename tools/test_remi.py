"""remi's job runner by behaviour (tools/remi.py, docs/remi.md): offline, against a stand-in origin, a stand-in vm.py
and a stand-in cargo, with no sudo, sg or KVM. python3 tools/test_remi.py"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
w = tempfile.mkdtemp(prefix="wend-test-remi-")
results = []


def check(ok, what):
    results.append(ok)
    print(("passed  " if ok else "WRONG   ") + what)


def sh(*argv, cwd=None):
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


ENV = dict(os.environ, WEND_LAB_TEST="1", WEND_LAB=f"{w}/lab", WEND_LAB_SRC=f"{w}/src", WEND_LAB_POLL="0.2",
           WEND_LAB_CARGO=f"{w}/cargo", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
           GIT_COMMITTER_EMAIL="t@t")


def remi(*args, env=None):
    p = subprocess.run([sys.executable, "-P", os.path.join(HERE, "remi.py"), *args], capture_output=True, text=True,
                       env=env or ENV, timeout=120)
    return p.returncode, p.stdout + p.stderr


# a stand-in vm.py: writes a run directory the way tools/vm.py does and names it in its last line. FAKE_RESULT=fail
# fails it; FAKE_SLEEP holds it; every start and end goes to $WEND_LAB/trace, with the WEND_WORK it saw
VM = r'''import json, os, subprocess, sys, time
a = sys.argv[1:]
while a[0].startswith("--"):
    a = a[2:] if a[0] in ("--root-fs", "--extra-disk", "--selinux") else a[1:]
cmd, box = a[0], (a[2] if a[0] in ("replay", "rehearse") else a[1])
trace = open(os.environ["WEND_LAB"] + "/trace", "a")
trace.write(f"start {cmd} {box} {os.environ.get('WEND_WORK')} {' '.join(sys.argv[1:])}\n"); trace.flush()
time.sleep(float(os.environ.get("FAKE_SLEEP", "0.5")))
if cmd == "build":
    print("12:00:00 built"); trace.write(f"end {cmd} {box}\n"); sys.exit(0)
head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
d = f".wend/vm/20261018-000000-{box.replace('/', '_')}-bios-{cmd}-{os.getpid()}"
os.makedirs(d + "/plan")
res = os.environ.get("FAKE_RESULT", "pass")
json.dump({"box": box, "result": res, "head": {"commit": head, "dirty": False}, "hops": [{"to": "7", "ok": True}]},
          open(d + "/run.json", "w"))
if cmd == "rehearse":
    open(d + "/rehearse.log", "w").write("".join(f"line {i}\n" for i in range(300)) + "hop to 8: fired 3\nERROR x\n")
    open(d + "/plan/checks.toml", "w").write('[web]\nok = "true"\n[generated]\nsvc-httpd = "systemctl is-enabled httpd"\n')
    json.dump({"hops": [{"to": "8", "fired": ["a"]}]}, open(d + "/plan/plan.json", "w"))
    json.dump({"hops": [{"to": "8", "ok": True, "fired_names": ["a"], "seconds": 3}], "final": {"passed": ["web"]}},
              open(d + "/walk.json", "w"))
    json.dump([{"why": "fix a", "ok": True, "outputs": []}, {"why": "fix b", "ok": False, "outputs": [{"rc": 1}]}],
              open(d + "/steps.json", "w"))
print(f"12:00:01 {res.upper()}: {box} on route; run and disks in {d}")
trace.write(f"end {cmd} {box}\n")
sys.exit(0 if res == "pass" else 1)
'''

# a stand-in shim the stand-in repo's adapter names: it notes each probe and hangs up, so a probe finds nothing listening
# and the worker never tries to stop it. The worker must probe this one, from the job's own adapters, and never the
# lab's real shim from the runner's.
shim, probes = socket.socket(), []
shim.bind(("127.0.0.1", 0))
shim.listen(8)


def hang_up():
    while True:
        c, _ = shim.accept()
        probes.append(c.recv(4096).split(b"\r\n", 1)[0].decode())
        c.close()


threading.Thread(target=hang_up, daemon=True).start()

# the stand-in origin: main with two commits, a PR branch, and a commit only a fork has (a pull head, no branch)
origin, seed = f"{w}/origin.git", f"{w}/seed"
sh("git", "init", "-q", "--bare", origin)
sh("git", "init", "-q", "-b", "main", seed)
for d in ("tools", "routes/el", "boxes/el/c7", "boxes/el/junk", "adapters"):
    os.makedirs(f"{seed}/{d}")
open(f"{seed}/tools/vm.py", "w").write(VM)
open(f"{seed}/tools/mirror.py", "w").write("# shim\n")
open(f"{seed}/tools/check.py", "w").write("print('ok')\n")
open(f"{seed}/tools/test_a.py", "w").write("print('fine')\n")
open(f"{seed}/adapters/el.toml", "w").write(f'[mirror]\nshim = "http://127.0.0.1:{shim.getsockname()[1]}"\n')
open(f"{seed}/routes/el/c7-to-a10.toml", "w").write('family = "el"\n')
open(f"{seed}/boxes/el/c7/box.toml", "w").write('release = "7"\n')
open(f"{seed}/boxes/el/junk/box.toml", "w").write('release = "7"\n[shelter.app]\nx = 1\n')
open(f"{seed}/.gitignore", "w").write(".wend/\nclient/target/\n")
os.makedirs(f"{seed}/client")
open(f"{seed}/client/Cargo.toml", "w").write("")
sh("git", "-C", seed, "add", "-A")
subprocess.run(["git", "-C", seed, "commit", "-qm", "one"], env=ENV, check=True)
old = sh("git", "-C", seed, "rev-parse", "HEAD")
open(f"{seed}/tools/test_b.py", "w").write("import sys; sys.exit(3)\n")
sh("git", "-C", seed, "add", "-A")
subprocess.run(["git", "-C", seed, "commit", "-qm", "two"], env=ENV, check=True)
main = sh("git", "-C", seed, "rev-parse", "HEAD")
sh("git", "-C", seed, "push", "-q", origin, "main")
sh("git", "-C", seed, "checkout", "-qb", "claude/x")
open(f"{seed}/tools/mirror.py", "w").write("# another shim\n")
sh("git", "-C", seed, "add", "-A")
subprocess.run(["git", "-C", seed, "commit", "-qm", "pr"], env=ENV, check=True)
pr = sh("git", "-C", seed, "rev-parse", "HEAD")
sh("git", "-C", seed, "push", "-q", origin, "claude/x")
sh("git", "-C", origin, "update-ref", "refs/pull/7/head", pr)
sh("git", "-C", seed, "checkout", "-qb", "fork", main)
open(f"{seed}/tools/evil.py", "w").write("")
sh("git", "-C", seed, "add", "-A")
subprocess.run(["git", "-C", seed, "commit", "-qm", "fork"], env=ENV, check=True)
fork = sh("git", "-C", seed, "rev-parse", "HEAD")
sh("git", "-C", seed, "push", "-q", origin, f"{fork}:refs/pull/8/head")
sh("git", "clone", "-q", origin, f"{w}/src/wend")
open(f"{w}/cargo", "w").write("#!/bin/sh\nmkdir -p target/release && touch target/release/wend && echo built\n")
os.chmod(f"{w}/cargo", 0o755)
os.makedirs(f"{w}/lab")
NOW = dict(ENV, WEND_LAB_NO_WORKER="1")


def entry(out):
    return out.split()[1].rstrip(":")


def verdict(jid):
    rc, out = remi("show", jid)
    parts = int(out.split()[1].split("/")[1])
    text = out.split("\n", 1)[1]
    for i in range(2, parts + 1):
        text += remi("show", jid, "--part", str(i))[1].split("\n", 1)[1]
    return json.loads(text)


# refused at the door: bad arguments, a fork's commit, a stale PR head, unknown flags
for args, what in (
        (["replay", "routes/el/../x.toml", "el/c7", "--sha", main, "--by", "t"], "a route with .. in it"),
        (["replay", "routes/el/c7-to-a10.toml", "el/c7;rm", "--sha", main, "--by", "t"], "a box with shell in it"),
        (["build", "el/c7", "--sha", main[:12], "--by", "t"], "a short sha"),
        (["build", "el/c7", "--sha", main], "no --by"),
        (["build", "el/c7", "--sha", fork, "--by", "t"], "a fork's commit (a pull head, no branch on origin)"),
        (["build", "el/c7", "--sha", fork, "--pr", "8", "--by", "t"], "a fork's PR head"),
        (["build", "el/c7", "--sha", main, "--pr", "7", "--by", "t"], "a sha that isn't PR 7's head"),
        (["build", "el/c7", "--sha", main, "--by", "t", "--vm-flags", "--uefi; reboot"], "an unknown vm flag"),
        (["build", "el/c7", "--sha", main, "--by", "t", "--vm-flags", "--extra-disk 9999"], "a bad flag value"),
        (["build", "el/c7", "--sha", main, "--by", "t", "--vm-flags", "--extra-disk 501"], "an extra disk vm.py won't take"),
        (["build", "el/c7", "--sha", main, "--by", "t", "--vm-flags", "--root-fs btrfs"], "a root fs vm.py won't take"),
        (["tests", "--sha", main, "--by", "t", "--vm-flags", "--uefi"], "vm flags on a tests job"),
        (["build", "el/c7", "--sha", main, "--by", "t", "--vm-flags", "--selinux permissive"], "a selinux mode vm.py won't take"),
        (["build", "el/c7", "--sha", main, "--by", "t", "--vm-flags", "--selinux enforcing"],
         "--selinux on a job that isn't a replay"),
        (["relocate", "el/c7", "--sha", main, "--by", "t"], "a relocate with no extra disk"),
        (["replay", "routes/el/c7-to-a10.toml", "el/c7", "--sha", main, "--by", "t", "--vm-flags", "--relocate"],
         "a replay --relocate with no extra disk"),
        (["build", "el/c7", "--sha", main, "--by", "t", "--vm-flags", "--relocate --extra-disk 20"],
         "--relocate on a job that isn't a replay"),
        (["shell", "--sha", main, "--by", "t"], "a job that doesn't exist")):
    rc, out = remi("enqueue", *args, env=NOW)
    check(rc != 0 and "refused" in out, f"enqueue refuses {what}")
check(not os.path.exists(f"{w}/lab/queue") or not [f for f in os.listdir(f"{w}/lab/queue") if f.endswith(".json")],
      "nothing refused was queued")

# old main is on origin/main (not a tip); the PR head with --pr; then the lab order and the slots
rc, out = remi("enqueue", "rehearse", "routes/el/c7-to-a10.toml", "el/c7", "--sha", old, "--by", "thread A",
               "--reply-to", "cse_a", env=NOW)
check(rc == 0 and "on origin/main" in out, "a commit on origin/main queues")
j1 = entry(out)
rc, out = remi("enqueue", "rehearse", "routes/el/c7-to-a10.toml", "el/c7", "--sha", main, "--by", "thread B", env=NOW)
j2 = entry(out)
rc, out = remi("enqueue", "replay", "routes/el/c7-to-a10.toml", "el/c7", "--sha", pr, "--pr", "7", "--by", "thread C",
               "--vm-flags", "--uefi --extra-disk 20 --selinux enforcing", env=NOW)
check(rc == 0 and "tip of origin/claude/x" in out, "a PR head that is a branch tip on origin queues")
j3 = entry(out)
rc, out = remi("enqueue", "tests", "--sha", main, "--by", "thread D", "--after", j1, env=NOW)
j4 = entry(out)
rc, out = remi("order", j2, j1)
lines = out.splitlines()
check(lines[0].startswith(j2) and lines[1].startswith(j1), "order puts the named jobs first, in that order")
rc, out = remi("worker")
check(rc == 0, "the worker runs the queue to empty and exits")
check(probes and set(probes) == {"GET /.wend-shim HTTP/1.1"} and "shim-stop" not in out,
      "it probes the shim the job's own adapters name, and stops nothing it doesn't own")
trace = open(f"{w}/lab/trace").read().splitlines()
at = [i for i, l in enumerate(trace) if l.startswith("start rehearse")]
check(len(at) == 2 and trace.index("end rehearse el/c7") < at[1], "two rehearses never overlap")
check(trace[at[0]].split()[3] == f"{w}/lab/vmwork-root", "a rehearse runs with root's WEND_WORK")
reps = [l for l in trace if l.startswith("start replay")]
check(len(reps) == 1 and reps[0].split()[3] == f"{w}/lab/vmwork" and "--uefi --extra-disk 20 --selinux enforcing replay" in reps[0],
      "a replay runs as dev with dev's WEND_WORK and its vm flags before the command")
check(trace.index(reps[0]) > trace.index("end rehearse el/c7"),
      "a job with another tools/mirror.py waits for the VM jobs using the first one")
v1, v2, v3, v4 = (verdict(j) for j in (j1, j2, j3, j4))
check(v2["started"] <= v1["started"], "the lab order holds: the job ordered first started first")
check(v4["started"] >= v1["ended"], "--after waits for that job to end")
check(v1["result"] == "pass" and v1["reasons"] == [], "a passing rehearse is a pass")
r = v1["runs"][0]
check(r["run_json"]["head"]["commit"] == old and v1["preflight"] == {"head": old, "porcelain": ""},
      "the verdict carries run.json in full and the preflight head and status")
check(r["generated"] == ["svc-httpd"] and "[generated]" in r["checks_toml"], "it carries the generated checks in full")
check(r["rehearse_log"]["lines"] == 302 and "ERROR x" in r["rehearse_log"]["key_lines"] and
      len(r["rehearse_log"]["tail"]) == 200, "it carries rehearse.log's key lines and tail, with its length")
check([s["why"] for s in r["steps"]["failed"]] == ["fix b"] and r["walk"]["hops"][0]["fired_names"] == ["a"],
      "it carries every failed step and the walk's hops")
check({f["path"] for f in r["files"]} >= {"run.json", "plan/checks.toml"} and all(f["sha256"] for f in r["files"]),
      "it lists the run directory's files with hashes")
check([c["name"] for c in v1["commands"]] == ["cargo-build", "rehearse"] and v1["commands"][0]["rc"] == 0,
      "a rehearse builds the client first, each command with its own log, rc and hash")
check(all(c["log"].startswith(f"{w}/lab/logs/main/{old}/{j1}/") for c in v1["commands"]),
      "logs go under the lab's logs/<PR or main>/<sha>/<id>, not into the worktree")
check(os.path.isdir(f"{w}/src/wend-main-{old[:12]}-root") and v1["worktree"].endswith("-root") and os.path.isdir(f"{w}/src/wend-7-{pr[:12]}"),
      "each job gets its own worktree, wend-<PR or main>-<sha12>, root's apart (-root)")
check(v3["remi_verdict"] == {"pr": "7", "sha": pr, "state": "success",
                              "summary": f"replay el/c7-to-a10 el/c7 --uefi --extra-disk 20 --selinux enforcing: passed (job {j3})"},
      "a PR job's verdict carries the remi-verdict inputs")
check(v4["result"] == "fail" and v4["reasons"] == ["tools/test_b.py exited 3"] and
      [c["name"] for c in v4["commands"]] == ["test_a", "test_b", "check"], "tests runs each test and check.py")

# failures, refusals and a dirty worktree
os.makedirs(f"{w}/src/wend-main-{main[:12]}/.wend", exist_ok=True)
open(f"{w}/src/wend-main-{main[:12]}/tools/check.py", "a").write("# local edit\n")
rc, out = remi("enqueue", "replay", "routes/el/c7-to-a10.toml", "el/c7", "--sha", main, "--by", "t", env=NOW)
j5 = entry(out)
remi("worker")
v5 = verdict(j5)
check(v5["result"] == "refused" and "local changes" in v5["reasons"][0] and
      open(f"{w}/src/wend-main-{main[:12]}/tools/check.py").read().endswith("# local edit\n"),
      "a dirty worktree is refused and left as it is")
sh("git", "-C", f"{w}/src/wend-main-{main[:12]}", "checkout", "tools/check.py")
rc, out = remi("enqueue", "replay", "routes/el/c7-to-a10.toml", "el/junk", "--sha", main, "--by", "t", env=NOW)
j6 = entry(out)
remi("worker", env=dict(ENV, FAKE_RESULT="fail"))
v6 = verdict(j6)
check(v6["result"] == "fail" and v6["user"] == "root" and
      any("result is 'fail'" in x for x in v6["reasons"]) and any("exited 1" in x for x in v6["reasons"]),
      "a failed replay is a fail with its reasons, and a shelter box runs as root")
check(v6["remi_verdict"] is None, "a job with no PR has no remi-verdict inputs")
rc, out = remi("enqueue", "replay", "routes/el/c7-to-a10.toml", "el/none", "--sha", main, "--by", "t", env=NOW)
j7 = entry(out)
remi("worker")
check(verdict(j7)["result"] == "refused", "a box that doesn't exist at the sha is refused")

# the PR moves after the job is queued: refused at start, not run on the new head
rc, out = remi("enqueue", "build", "el/c7", "--sha", pr, "--pr", "7", "--by", "t", env=NOW)
j8 = entry(out)
sh("git", "-C", origin, "update-ref", "refs/pull/7/head", main)
remi("worker")
check(verdict(j8)["result"] == "refused" and "isn't PR #7's head" in verdict(j8)["reasons"][0],
      "a PR that moved after the job was queued is refused when it starts")

# cancel, the queue listing, delivered, file, show in parts
rc, out = remi("enqueue", "build", "el/c7", "--sha", main, "--by", "t", env=NOW)
j9 = entry(out)
rc, out = remi("cancel", j9)
check(rc == 0 and json.load(open(f"{w}/lab/queue/{j9}.json"))["state"] == "cancelled", "cancel drops a queued job")
check(verdict(j9)["result"] == "cancelled", "a job cancelled while queued still has a verdict to deliver")
rc, out = remi("enqueue", "build", "el/c7", "--sha", main, "--by", "t", env=NOW)
j10 = entry(out)
subprocess.Popen([sys.executable, "-P", os.path.join(HERE, "remi.py"), "worker"], env=dict(ENV, FAKE_SLEEP="30"),
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(100):
    if json.load(open(f"{w}/lab/queue/{j10}.json"))["state"] == "running" and \
            "start build" in open(f"{w}/lab/trace").read().splitlines()[-1]:
        break
    time.sleep(0.2)
remi("cancel", j10)
rc, out = remi("wait", j10)
check("CANCELLED" in out and verdict(j10)["reasons"] == ["cancelled"], "cancel stops a running job, verdict cancelled")

# every VM job shares the one shim on remi, so two VM jobs on the same mirror.py still run one at a time
seen = len(open(f"{w}/lab/trace").read().splitlines())
for job in (["build", "el/c7"], ["replay", "routes/el/c7-to-a10.toml", "el/c7"]):
    remi("enqueue", *job, "--sha", main, "--by", "t", env=NOW)
remi("worker")
depth, most = 0, 0
for line in open(f"{w}/lab/trace").read().splitlines()[seen:]:
    depth += 1 if line.startswith("start ") else -1 if line.startswith("end ") else 0
    most = max(most, depth)
check(most == 1, "VM jobs run one at a time, even on the same shim")
rc, out = remi("queue")
check(f"{j1}  done (pass)" in out and "UNDELIVERED to cse_a" in out, "queue lists undelivered verdicts and to whom")
remi("delivered", j1)
rc, out = remi("queue")
check("UNDELIVERED to cse_a" not in out, "delivered marks a verdict as sent")
rc, out = remi("file", j1, "01-cargo-build.log")
check(rc == 0 and "built" in out, "file reads a log from the job's log directory")
rc, out = remi("file", j1, "plan/checks.toml")
check(rc == 0 and "[generated]" in out, "file reads a file from the job's run directory")
rc, out = remi("file", j1, "../../../../../../etc/passwd")
check(rc != 0, "file refuses a path outside them")
rc, out = remi("show", j1)
check(out.startswith("part 1/1") and json.loads(out.split("\n", 1)[1])["id"] == j1, "show prints the verdict whole")
big = json.load(open(f"{w}/lab/queue/{j1}.json"))["verdict"]
v = json.load(open(big))
v["pad"] = "x" * 120000
json.dump(v, open(big, "w"))
check(verdict(j1)["pad"] == "x" * 120000 and remi("show", j1)[1].startswith("part 1/3"),
      "show splits a big verdict into parts that join back exactly")

# a cancel that lands before the runner is ready leaves a flag the runner honours; a split character joins back
rc, out = remi("enqueue", "build", "el/c7", "--sha", main, "--by", "t", env=NOW)
j11 = entry(out)
import fcntl  # noqa: E402  (as the worker does: hold the queue lock while starting the runner and recording it)
lk = open(f"{w}/lab/queue/.lock", "a")
fcntl.flock(lk, fcntl.LOCK_EX)
kid = subprocess.Popen([sys.executable, "-P", os.path.join(HERE, "remi.py"), "run", j11], env=ENV,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
e11 = json.load(open(f"{w}/lab/queue/{j11}.json"))
e11.update(state="running", pid=kid.pid, cancel=True)  # what cancel leaves for a runner not yet ready
json.dump(e11, open(f"{w}/lab/queue/{j11}.json", "w"))
lk.close()
kid.wait(timeout=60)
check(verdict(j11)["result"] == "cancelled" and not verdict(j11)["commands"],
      "a runner that finds the cancel flag at its handshake runs nothing and ends cancelled")
rc, out = remi("enqueue", "build", "el/c7", "--sha", main, "--by", "t", env=NOW)
j12 = entry(out)
rc, out = remi("run", j12)
check(rc == 0 and json.load(open(f"{w}/lab/queue/{j12}.json"))["state"] == "queued" and "leaving it" in out,
      "a runner whose entry isn't recorded as its own runs nothing")
remi("cancel", j12)
logdir = verdict(j11)["log_dir"]
open(f"{logdir}/utf8.log", "wb").write(b"a" * 47999 + "é".encode() + b"z" * 10)
raw = b""
for i in (1, 2):
    p = subprocess.run([sys.executable, "-P", os.path.join(HERE, "remi.py"), "file", j11, "utf8.log", "--part", str(i)],
                       capture_output=True, env=ENV)
    raw += p.stdout.split(b"\n", 1)[1]
check(raw == b"a" * 47999 + "é".encode() + b"z" * 10, "file parts join back byte for byte across a split character")

print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
