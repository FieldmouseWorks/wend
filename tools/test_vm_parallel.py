"""VM replays side by side (tools/vm.py), by behaviour, the parts that don't need KVM: two guests started at once get
ssh forwards on different host ports and both run; a guest whose port another process takes first asks again for
another one; the mirror shim can't be stopped while a run uses it (shim-stop); a second run that needs a box another run is installing waits for it; and the box cache key follows what
the install runs, not the whole file (build_source). QEMU is a stand-in that binds its hostfwd port as QEMU does and
says what QEMU says when it can't. It runs the PR's own vm.py, so the gate's schema job doesn't run it: the client job
does.
python3 tools/test_vm_parallel.py"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
tmp = tempfile.mkdtemp(prefix="wend-test-vm-")
os.environ["WEND_WORK"] = os.path.join(tmp, "work")  # before vm reads it
import vm  # noqa: E402

bad = 0


def say(ok, what, detail=""):
    global bad
    print(("passed  " if ok else "WRONG   ") + what + ("" if ok or not detail else f"\n        {detail}"))
    bad += not ok


bin_dir = os.path.join(tmp, "bin")
os.makedirs(bin_dir)
open(os.path.join(bin_dir, "qemu-system-x86_64"), "w").write(f"""#!{sys.executable}
import re, socket, sys, time
net = sys.argv[sys.argv.index("-netdev") + 1]
port = int(re.search(r"hostfwd=tcp:127\\.0\\.0\\.1:(\\d+)-", net).group(1))
s = socket.socket()
try:
    s.bind(("127.0.0.1", port))
    s.listen()
except OSError:
    sys.stderr.write(f"qemu-system-x86_64: -netdev {{net}}: Could not set up host forwarding rule 'tcp:127.0.0.1:{{port}}'\\n")
    sys.exit(1)
time.sleep(60)
""")
os.chmod(os.path.join(bin_dir, "qemu-system-x86_64"), 0o755)
os.environ["PATH"] = bin_dir + os.pathsep + os.environ["PATH"]

try:
    # two guests at once: each its own port, both up, and ssh asks for that port
    g1, g2 = (vm.Guest(os.path.join(tmp, f"g{i}.qcow2"), "key", 22) for i in (1, 2))
    g1.start()
    g2.start()
    say(g1.running() and g2.running(), "two guests run side by side")
    say(g1.fwd != g2.fwd, "each guest forwards ssh from a host port of its own", f"{g1.fwd} and {g2.fwd}")
    argv = g1.ssh_argv("true")
    say(argv[argv.index("-p") + 1] == str(g1.fwd), "ssh goes to the guest's own forwarded port", str(argv))
    say(not re.search(r"hostfwd=tcp:127\.0\.0\.1:10022", " ".join(g1.argv())), "no fixed port is left in qemu's line")

    # a port taken between the pick and QEMU's bind: the guest asks again
    real, picks = vm.free_port, [g1.fwd]
    vm.free_port = lambda: picks.pop() if picks else real()
    g3 = vm.Guest(os.path.join(tmp, "g3.qcow2"), "key", 22)
    try:
        g3.start()
        say(g3.running() and g3.fwd not in (g1.fwd, g2.fwd), "a guest whose port was taken first starts on another one",
            f"port {g3.fwd}")
    except RuntimeError as e:
        say(False, "a guest whose port was taken first starts on another one", str(e))
    finally:
        vm.free_port = real

    # anything else QEMU refuses is not retried
    g4 = vm.Guest(os.path.join(tmp, "g4.qcow2"), "key", 22)
    open(os.path.join(bin_dir, "qemu-system-x86_64"), "w").write(
        f"#!{sys.executable}\nimport sys\nsys.stderr.write('could not open disk image\\n')\nsys.exit(1)\n")
    t0 = time.time()
    try:
        g4.start()
        say(False, "qemu failing for another reason fails at once, not five times")
    except RuntimeError as e:
        say("could not open disk image" in str(e) and time.time() - t0 < 6,
            "qemu failing for another reason fails at once, not five times", f"{time.time() - t0:.0f}s: {e}")
    for g in vm.GUESTS:
        g.kill()
        shutil.rmtree(os.path.dirname(g.monitor_path), ignore_errors=True)

    # one run installs a box, a second waits for it, then finds it installed
    lock, order = os.path.join(tmp, "work", "box.lock"), []

    def holder(ready):
        with vm.Locked(lock, "installing the box"):
            ready.set()
            time.sleep(1)
            order.append("first done")

    ready = threading.Event()
    t = threading.Thread(target=holder, args=(ready,))
    t.start()
    ready.wait()
    with vm.Locked(lock, "installing the box"):
        order.append("second in")
    t.join()
    say(order == ["first done", "second in"], "a run waits while another installs the same box", str(order))

    # the shim: shim-stop refuses while a run holds it, stops it once none does, and stops nothing else
    fake = os.path.join(tmp, "checkout", "tools", "mirror.py")  # a checkout's shim: vm.py and sandbox.py beside it
    os.makedirs(os.path.dirname(fake))
    open(fake, "w").write("import time\ntime.sleep(60)\n")
    for f in ("vm.py", "sandbox.py"):
        open(os.path.join(os.path.dirname(fake), f), "w").close()
    shim = subprocess.Popen([sys.executable, fake])
    run = subprocess.Popen([sys.executable, "-B", "-c", "import sys, time; sys.path.insert(0, sys.argv[1]); import vm; "
                            "vm.use_shim(); print('holding', flush=True); time.sleep(60)", os.path.join(ROOT, "tools")],
                           stdout=subprocess.PIPE, text=True)
    run.stdout.readline()
    stop = lambda pid: subprocess.run([sys.executable, "-B", os.path.join(ROOT, "tools", "vm.py"), "shim-stop", str(pid)],
                                      capture_output=True, text=True)  # noqa: E731
    p = stop(shim.pid)
    say(p.returncode == 3 and shim.poll() is None, "shim-stop leaves the shim running while a VM run uses it",
        f"rc {p.returncode}: {p.stdout}{p.stderr}")
    run.kill()
    run.wait()
    p = stop(shim.pid)
    time.sleep(0.5)
    say(p.returncode == 0 and shim.poll() is not None, "shim-stop stops the shim once no run uses it",
        f"rc {p.returncode}: {p.stdout}{p.stderr}")
    elsewhere = os.path.join(tmp, "srv", "mirror.py")  # a mirror.py that isn't a wend checkout's
    os.makedirs(os.path.dirname(elsewhere))
    shutil.copyfile(fake, elsewhere)
    for what, argv in [("a process that isn't the shim", ["-c", "import time; time.sleep(60)"]),
                       ("another program's mirror.py", [elsewhere])]:
        other = subprocess.Popen([sys.executable, *argv])
        p = stop(other.pid)
        say(p.returncode == 2 and other.poll() is None, f"shim-stop refuses {what}",
            f"rc {p.returncode}: {p.stdout}{p.stderr}")
        other.kill()
    shim.kill()

    # the cache key: what the install runs, and nothing it doesn't
    src = open(os.path.join(ROOT, "tools", "vm.py")).read()
    key = vm.build_source()

    def key_after(old, new):
        assert src.count(old) == 1, old
        p = os.path.join(tmp, "vm-changed.py")
        open(p, "w").write(src.replace(old, new))
        return vm.build_source(p)

    for what, old in [("the hop walk", "def walk_vm("), ("the shelter's confinement proof", "def shelter_confine("),
                      ("relocate", "def relocate("), ("prune", "def prune("), ("main", "def main(")]:
        say(key_after(f"\n{old}", f"\nUNUSED_BY_BUILD = 1\n\n\n{old}") == key,
            f"a change beside {what} keeps the installed boxes")
    say(key_after("    hops, start_net, efi_start = [], None, None\n",
                  "    hops, start_net, efi_start = [], None, None  # changed\n") == key,
        "a change inside the hop walk keeps the installed boxes")
    for what, old, new in [("the kickstart", 'def kickstart(box_doc, adapter, key_pub):\n',
                            'def kickstart(box_doc, adapter, key_pub):\n    pass\n'),
                           ("the install", "def install(box_id, box_doc, d):\n",
                            "def install(box_id, box_doc, d):\n    pass\n"),
                           ("Guest's reboot", "    def reboot(self):\n", "    def reboot(self):\n        pass\n"),
                           ("a constant the install uses", 'MAC = "52:54:00:12:34:56"', 'MAC = "52:54:00:12:34:57"'),
                           ("the box's checks", "def checks(g, box_doc, adapter):\n",
                            "def checks(g, box_doc, adapter):\n    pass\n")]:
        say(key_after(old, new) != key, f"a change to {what} installs the boxes again")
finally:
    for g in vm.GUESTS:
        g.kill()
    shutil.rmtree(tmp, ignore_errors=True)

print("ok" if not bad else f"{bad} WRONG")
sys.exit(1 if bad else 0)
