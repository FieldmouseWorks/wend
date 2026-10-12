"""The VM tier (SPEC.md "VM tier"): replay a route on a real install in a KVM guest, with a real reboot after every hop.

A chroot can't see kernels, bootloaders, init or networking. This can: the box is installed by the release's own
installer (a kickstart made from box.toml), the hop's commands are the ones the chroot harness runs (walk.Walk's
hop_cmds, the same compile), they run inside the running guest over ssh, and after each hop the guest reboots and
has to come back reachable on its own address with its services up.

Maintainer-run only. It needs /dev/kvm, qemu-system-x86_64 and qemu-img, a few GB per box, and it runs the guest's
code on the host that runs it: never wire it to a CI runner a pull request can trigger. Its runs go to .wend/vm/,
not runs/ (runs/ holds what CI proved).

  python3 tools/vm.py build BOX            install the box in a VM (cached by box file + adapter)
  python3 tools/vm.py replay ROUTE BOX     walk ROUTE on a fresh copy of it, rebooting after every hop

Networking is QEMU user mode: nothing on the host changes. The guest reaches the mirror shim at 10.0.2.2. The guest's
ssh and http are forwarded to fixed ports on 127.0.0.1 (10022, 10080), so one host runs one VM replay at a time; a
second one fails at once ("qemu exited at once"). Disks go under $WEND_WORK/vm (default /var/tmp/wend/vm, which the
chroot harness, run as root, may own: point WEND_WORK somewhere the user can write).
"""
import hashlib
import http.server
import json
import os
import signal
import subprocess
import sys
import threading
import time
import tomllib

sys.dont_write_bytecode = True
TOOLS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TOOLS)
sys.path.insert(0, TOOLS)
import backends  # noqa: E402
import check  # noqa: E402
import sandbox  # noqa: E402
import walk  # noqa: E402

WORK = os.path.join(sandbox.WORK, "vm")
GUEST_HOST = "10.0.2.2"  # the host, as QEMU user-mode networking shows it to the guest
MAC = "52:54:00:12:34:56"
# The box's static address. Not 10.0.2.15, which slirp's DHCP hands out: the forwards point here, so a box that falls
# back to DHCP is unreachable and fails the hop, instead of passing because DHCP happened to give the same address.
ADDR = "10.0.2.20"
SSH_FWD, HTTP_FWD = 10022, 10080
ENV = "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin LANG=C HOME=/root TERM=dumb; umask 022; "
# the guest's network as the live checks compare it after every boot: addresses, default route, resolvers
NET = ("(ip -o -4 addr 2>/dev/null || /sbin/ip -o -4 addr) | "
       "awk '$2 != \"lo\" {print $2, $4}' | sort; "
       "(ip route 2>/dev/null || /sbin/ip route) | awk '/^default/ {print $1, $2, $3}'; "
       "grep '^nameserver' /etc/resolv.conf | sort")


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def load(path):
    return tomllib.load(open(os.path.join(ROOT, path), "rb"))


def guest_adapter(family):
    """The adapter with its mirror shim moved to where the guest sees the host."""
    a = load(f"adapters/{family}.toml")
    port = a["mirror"]["shim"].rsplit(":", 1)[1]
    a["mirror"] = {**a["mirror"], "shim": f"http://{GUEST_HOST}:{port}"}
    return a


FORCED_RESET = ("sleep 2; kill -TERM -1; sleep 10; kill -KILL -1; sleep 3; sync; echo 1 > /proc/sys/kernel/sysrq; "
                "echo s > /proc/sysrq-trigger; sleep 2; echo u > /proc/sysrq-trigger; sleep 3; echo b > /proc/sysrq-trigger\n")
GUESTS = []  # every qemu this run started: killed on the way out, whatever happened


class Guest:
    def __init__(self, disk, key, ssh_port):
        self.disk, self.key, self.ssh_port = disk, key, ssh_port
        base = disk[:-len(".qcow2")]
        self.console, self.monitor_path, self.proc = base + ".console.log", base + ".monitor", None

    def start(self, extra=()):
        argv = ["qemu-system-x86_64", "-machine", "pc,accel=kvm", "-cpu", "host", "-smp", "2", "-m", "3072",
                "-drive", f"file={self.disk},if=virtio,format=qcow2", "-netdev",
                f"user,id=n0,hostfwd=tcp:127.0.0.1:{SSH_FWD}-{ADDR}:{self.ssh_port},hostfwd=tcp:127.0.0.1:{HTTP_FWD}-{ADDR}:80",
                "-device", f"virtio-net-pci,netdev=n0,mac={MAC}", "-display", "none",
                "-serial", f"file:{self.console}", "-monitor", f"unix:{self.monitor_path},server,nowait", *extra]
        self.proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=open(self.console + ".qemu", "a"))
        GUESTS.append(self)
        time.sleep(2)
        if self.proc.poll() is not None:
            raise RuntimeError(f"qemu exited at once: {open(self.console + '.qemu').read()[-800:]}")

    def ssh(self, cmd, timeout=600, stdin=None):
        argv = ["ssh", "-q", "-i", self.key, "-p", str(SSH_FWD), "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=15", "-o", "LogLevel=ERROR",
                # CentOS 5's OpenSSH 4.3 offers only these; later releases negotiate the modern ones
                "-o", "KexAlgorithms=+diffie-hellman-group1-sha1,diffie-hellman-group14-sha1",
                "-o", "HostKeyAlgorithms=+ssh-rsa", "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa",
                "root@127.0.0.1", ENV + cmd]
        try:
            p = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout, errors="replace")
            return p.returncode, p.stdout + p.stderr
        except subprocess.TimeoutExpired:
            return 124, "TIMEOUT"

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def wait_ssh(self, timeout=900):
        t = time.time()
        while time.time() - t < timeout and self.running():
            if self.ssh("true", timeout=30)[0] == 0:
                return True
            time.sleep(5)
        return False

    def boot_id(self):
        rc, out = self.ssh("cat /proc/sys/kernel/random/boot_id", timeout=30)
        return out.strip() if rc == 0 else None

    def reboot(self):
        """A real reboot, proven by a new boot_id. Returns (came back, forced). If the release's own `reboot` does
        nothing (the first boot onto systemd: PID 1 is still the old init, the new tools can't talk to it), sync,
        remount read-only and reset with sysrq (SPEC "VM tier")."""
        before = self.boot_id()
        if before is None:  # can't prove a reboot without the boot we started from
            return False, False
        self.ssh("sync; (sleep 2; reboot) >/dev/null 2>&1 &", timeout=30)
        forced, t, last = False, time.time(), before
        while time.time() - t < 120:
            time.sleep(10)
            last = self.boot_id()
            if last not in (None, before):
                break
            if not self.running():
                return False, False
        if last == before:  # the old boot still answers after two minutes: `reboot` did nothing
            forced = True
            # Stop every process first, while the old kernel still runs: a reset with the daemons up leaves every
            # replaced library and socket as an orphan inode, and EL7's kernel, cleaning those up at its first mount
            # of the ext3 root, leaves extended-attribute refcounts too high (EL9's fsck then stops the boot).
            # Only PID 1's own files stay orphaned. A detached script: the kill takes this ssh session too.
            self.ssh("cat > /root/.wend-reset", stdin=FORCED_RESET)
            self.ssh("setsid nohup sh /root/.wend-reset >/dev/null 2>&1 </dev/null &", timeout=30)
            time.sleep(40)
        return self.wait_ssh() and self.boot_id() not in (None, before), forced

    def stop(self):
        if not self.running():
            return
        self.ssh("sync; (sleep 1; poweroff) >/dev/null 2>&1 &", timeout=30)
        try:
            self.proc.wait(300)
        except subprocess.TimeoutExpired:
            self.kill()

    def kill(self):
        if self.running():
            self.proc.kill()
            self.proc.wait(30)


def kickstart(box_doc, adapter, key_pub):
    rel = adapter["releases"][box_doc["release"]]
    inst = rel["install"]
    k = box_doc.get("vm", {})
    return "\n".join([
        "install", f"url --url http://{GUEST_HOST}:{adapter['mirror']['shim'].rsplit(':', 1)[1]}/{inst['tree']}",
        "text", "skipx", "lang en_US.UTF-8", "keyboard us",
        # a static address, as servers have: the live checks compare the guest's addresses with it after every boot
        f"network --device eth0 --bootproto static --ip {ADDR} --netmask 255.255.255.0 --gateway 10.0.2.2 "
        "--nameserver 10.0.2.3 --hostname wend-vm.example.org",
        "rootpw --iscrypted !", "firewall --disabled", "selinux --permissive", "authconfig --enableshadow --enablemd5",
        "timezone --utc UTC", 'bootloader --location=mbr --append="console=tty0 console=ttyS0,115200"', "zerombr",
        "clearpart --all --initlabel", "part /boot --fstype ext3 --size=200", "part swap --size=1024",
        "part / --fstype ext3 --size=1 --grow", "poweroff", "",
        "%packages --nobase", "kernel", "grub", *box_doc["packages"], "",
        "%post --log=/root/ks-post.log", "set -x",
        "mkdir -p /root/.ssh && chmod 700 /root/.ssh", f"echo '{key_pub}' > /root/.ssh/authorized_keys",
        "chmod 600 /root/.ssh/authorized_keys", box_doc["customize"], box_doc.get("start_hook", ""), ""])


class Serve:
    """A one-directory http server on the host for the kickstart, reachable from the installer at 10.0.2.2."""

    def __init__(self, directory):
        h = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(*a, directory=directory, **kw)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), h)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


def fetch(url, out):
    rc, msg = sandbox.host(f"curl -sf -o {out} {url}")
    if rc:
        raise RuntimeError(f"fetching {url} failed: {msg[-300:]}")


def build(box_id):
    """Install BOX in a VM from its release's own installer, then bring it to the release's last packages, as the
    chroot build does (yum update). Cached by box file and adapter, like sandbox.build_box."""
    box_doc = load(f"boxes/{box_id}/box.toml")
    fam = box_doc["family"]
    raw = open(os.path.join(ROOT, f"boxes/{box_id}/box.toml"), "rb").read() + \
        open(os.path.join(ROOT, f"adapters/{fam}.toml"), "rb").read() + open(os.path.abspath(__file__), "rb").read()
    d = os.path.join(WORK, f"{box_id.replace('/', '_')}-{hashlib.sha256(raw).hexdigest()[:12]}")
    base = os.path.join(d, "base.qcow2")
    if os.path.exists(base):
        return d, box_doc
    os.makedirs(d, exist_ok=True)
    a = guest_adapter(fam)
    sandbox.ensure_mirror(load(f"adapters/{fam}.toml"))
    key = os.path.join(d, "key")
    if not os.path.exists(key):
        sandbox.host(f"ssh-keygen -q -t rsa -b 3072 -N '' -C wend-vm -f {key}")
    shim = load(f"adapters/{fam}.toml")["mirror"]["shim"]
    inst = a["releases"][box_doc["release"]]["install"]
    for f in ("vmlinuz", "initrd.img"):
        fetch(f"{shim}/{inst['tree']}/{inst['pxeboot']}/{f}", os.path.join(d, f))
    ks = os.path.join(d, "ks")
    os.makedirs(ks, exist_ok=True)
    open(os.path.join(ks, "ks.cfg"), "w").write(kickstart(box_doc, a, open(key + ".pub").read().strip()))
    srv = Serve(ks)
    disk = os.path.join(d, "install.qcow2")
    sandbox.host(f"qemu-img create -q -f qcow2 {disk} 30G")
    g = Guest(disk, key, box_doc.get("vm", {}).get("ssh_port", 22))
    log(f"installing {box_id} ({box_doc['release']}) in a VM; this takes 10 to 20 minutes")
    g.start(["-kernel", os.path.join(d, "vmlinuz"), "-initrd", os.path.join(d, "initrd.img"), "-no-reboot",
             "-append", f"ks=http://{GUEST_HOST}:{srv.port}/ks.cfg ksdevice=eth0 console=ttyS0,115200 text"])
    g.proc.wait(3600)
    srv.httpd.shutdown()
    g = Guest(disk, key, g.ssh_port)
    g.start()
    if not g.wait_ssh():
        raise RuntimeError(f"the installed box never answered ssh; console: {g.console}")
    v = backends.of(a).hop_vars(a, box_doc["release"])
    rc, out = g.ssh(backends.fill(a["hop"]["set_release"], v) + " && yum -y update && yum clean all", timeout=3600)
    if rc:
        raise RuntimeError(f"updating the installed box failed:\n{out[-2000:]}")
    if not g.reboot()[0]:
        raise RuntimeError(f"the installed box didn't come back from its first reboot; console: {g.console}")
    log("installed and updated; checking the box")
    failed = {n: o for n, (ok, o) in checks(g, box_doc, a).items() if not ok}
    g.stop()
    if failed:
        raise RuntimeError(f"box {box_id} fails its own checks in the VM: {sorted(failed)}\n" +
                           "\n".join(f"{n}: {o[-600:]}" for n, o in failed.items()))
    os.rename(disk, base)
    sandbox.host(f"chmod a-w {base}")
    return d, box_doc


def checks(g, box_doc, adapter):
    """The box's offline checks that make sense on a running box (box.toml [vm].skip lists roles that start their
    own daemons), then its live ones ([vm].live), each as (ok, output)."""
    k = box_doc.get("vm", {})
    named = {n: c for n, c in sandbox.checks_for(box_doc, adapter).items() if n not in k.get("skip", [])}
    named.update({f"live:{n}": c for n, c in k.get("live", {}).items()})
    out = {}
    for n, c in named.items():
        rc, o = g.ssh(f"( {c} ) >/tmp/.wend-check 2>&1; rc=$?; tail -c 800 /tmp/.wend-check; exit $rc", timeout=300)
        out[n] = (rc == 0, o)
    return out


RUNNER = r"""cd /root/wend-hop
for f in $(ls main-*.sh | sort); do sh $f > $f.out 2>&1; rc=$?; echo $rc > $f.rc; [ $rc = 0 ] || break; done
for f in $(ls after-*.sh 2>/dev/null | sort); do sh $f > $f.out 2>&1; echo $? > $f.rc; done
ls main-*.rc | while read r; do cat $r; done | grep -qv '^0$' && echo 1 > DONE || echo 0 > DONE
"""


def run_hop(g, cmds, after):
    """The hop's commands inside the guest, detached: the transaction replaces sshd and glibc under the session.
    Stops at the first failing command, then runs `after` whatever happened (SPEC "How a hop runs")."""
    rc, o = g.ssh("rm -rf /root/wend-hop && mkdir -p /root/wend-hop && test ! -e /root/wend-hop/DONE")
    if rc:  # the previous hop's directory (and its DONE) is on this overlay: never read it as this hop's result
        raise RuntimeError(f"clearing the previous hop's work dir in the guest failed: {o}")
    files = {"runner.sh": RUNNER, **{f"main-{i:02d}.sh": c + "\n" for i, c in enumerate(cmds)},
             **{f"after-{i:02d}.sh": c + "\n" for i, c in enumerate(after)}}
    for name, body in files.items():
        rc, o = g.ssh(f"cat > /root/wend-hop/{name}", stdin=ENV + "\n" + body if name != "runner.sh" else body)
        if rc:
            raise RuntimeError(f"copying {name} into the guest failed: {o}")
    rc, o = g.ssh("cd /root/wend-hop && (setsid nohup sh runner.sh >runner.log 2>&1 </dev/null &)", timeout=30)
    if rc:
        raise RuntimeError(f"starting the hop in the guest failed: {o}")
    t = time.time()
    while time.time() - t < 4 * 3600 and g.running():
        time.sleep(20)
        rc, o = g.ssh("cat /root/wend-hop/DONE 2>/dev/null", timeout=60)
        if rc == 0 and o.strip() in ("0", "1"):
            break
    else:
        return False, "the hop didn't finish in 4 hours, or the guest stopped"
    rc, o = g.ssh("cd /root/wend-hop; for f in main-*.sh after-*.sh; do [ -f $f.rc ] || continue; "
                  "echo \"### $f rc=$(cat $f.rc)\"; tail -c 3000 $f.out; done", timeout=120)
    return g.ssh("cat /root/wend-hop/DONE")[1].strip() == "0", o


def replay(route_path, box_id):
    d, box_doc = build(box_id)
    route = load(route_path)
    if route.get("finish", {}).get("prune_obsolete"):  # the chroot walk's finish step; no VM equivalent yet
        raise SystemExit(f"{route_path} ends with prune_obsolete, which the VM tier doesn't run yet: refusing to "
                         "report a pass that skipped it")
    a = guest_adapter(route["family"])
    sandbox.ensure_mirror(load(f"adapters/{route['family']}.toml"))
    fixes = {}
    for dirpath, _, files in os.walk(os.path.join(ROOT, "fixes", route["family"])):
        for f in files:
            doc = tomllib.load(open(os.path.join(dirpath, f), "rb"))
            fixes[check.content_id(doc)] = doc
    pinned = [fixes[r["id"]] for r in route["fixes"]]
    w = type("W", (), {})()  # the harness's own hop_cmds (tools/walk.py), on a walk with no chroot box
    w.adapter = a
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join(ROOT, ".wend", "vm", f"{stamp}-{box_id.replace('/', '_')}-{os.getpid()}")
    os.makedirs(out_dir)
    key, prev = os.path.join(d, "key"), os.path.join(d, "base.qcow2")
    port = box_doc.get("vm", {}).get("ssh_port", 22)
    g = Guest(prev, key, port)
    hops, start_net = [], None
    for hop in route["hops"]:
        disk = os.path.join(out_dir, f"{hop}.qcow2")
        sandbox.host(f"qemu-img create -q -f qcow2 -b {prev} -F qcow2 {disk}")
        g = Guest(disk, key, port)
        g.start()
        if not g.wait_ssh():
            hops.append({"to": hop, "ok": False, "stage": "the guest didn't come up before the hop"})
            g.kill()  # free the forwarded ports; the overlay and console stay for a look
            break
        start_net = start_net or g.ssh(NET)[1]
        before = checks(g, box_doc, a)
        matched = []
        for f in (f for f in pinned if f["hop"] == hop):
            rc = g.ssh(f"{backends.pred_sh(f['when'], a)} >/dev/null 2>&1")[0]
            if rc in (124, 255):  # ssh itself failed or timed out: not an answer about the precondition
                raise RuntimeError(f"couldn't evaluate {f['meta']['name']}'s precondition: ssh failed")
            if rc == 0:
                matched.append(f)
        log(f"hop to {hop}: {[f['meta']['name'] for f in matched]}")
        first, second = walk.Walk.hop_parts(w, hop, matched)
        after = [backends.fill(c, w.vars) for c in a["hop"].get("after", [])]
        t0 = time.time()
        h = {"to": hop, "fired_names": [f["meta"]["name"] for f in matched]}
        if (a["releases"].get(hop) or {}).get("reboot") == "before-post":
            # boot the new kernel before the post steps run the release's binaries (SPEC "Kernels and the bootloader")
            ok, steps = run_hop(g, first, [])  # hop.after runs once, after everything (SPEC "How a hop runs")
            if not ok:  # ...whatever happened
                steps += run_hop(g, [], after)[1]
            if ok:
                back, forced = g.reboot()
                h["mid_reboot"], h["mid_reboot_forced"] = back, forced
                ok = back
                if back:
                    ok, rest = run_hop(g, second, after)
                    steps += "\n##### after the reboot into the new kernel\n" + rest
        else:
            ok, steps = run_hop(g, first + second, after)
        open(os.path.join(out_dir, f"{hop}.steps.log"), "w").write(steps)
        h["commands_ok"], h["seconds"] = ok, round(time.time() - t0)
        back, forced = g.reboot() if ok else (None, False)  # None: commands failed, no reboot tried
        h["rebooted"], h["reboot_forced"] = back, forced
        if back:
            net = g.ssh(NET)[1]
            after_checks = checks(g, box_doc, a)
            lost = sorted(n for n, (good, _) in before.items() if good and not after_checks.get(n, (False,))[0])
            h["net_kept"] = net == start_net
            # the release's newest kernel must be the one running (SPEC "Kernels and the bootloader"): an old kernel
            # under the new userland is what GRUB legacy did before the harness owned the boot entries
            rc, kv = g.ssh("uname -r; rpm -q --qf '%{INSTALLTIME} %{VERSION}-%{RELEASE}.%{ARCH}\\n' kernel-core kernel "
                           "2>/dev/null | grep -v 'is not installed' | sort -n | tail -n1 | cut -d' ' -f2")
            lines = kv.split()
            h["kernel"] = lines[0] if lines else None
            running, newest = (lines + [None, None])[:2]
            h["kernel_current"] = newest is None or newest in (running, (running or "").rsplit(".", 1)[0])
            h["lost"] = lost
            h["failed_logs"] = {n: after_checks[n][1][-1500:] for n in lost}
            h["ok"] = ok and h["net_kept"] and not lost and h["kernel_current"]
            if not h["net_kept"]:
                h["net"] = {"start": start_net, "now": net}
        else:
            h["ok"] = False
            h["console_tail"] = open(g.console, errors="replace").read()[-4000:]
        log(f"hop to {hop}: {'OK' if h['ok'] else 'FAILED'} (commands {'ok' if ok else 'failed'}, "
            f"rebooted {back}{' (forced)' if forced else ''}, network kept {h.get('net_kept')}, lost {h.get('lost')})")
        hops.append(h)
        g.stop()  # a failed hop's disk and console stay next to the run for a look (boot it with the same qemu line)
        if not h["ok"]:
            break
        prev = disk
    run = {"schema": "wend.vmrun/0", "route": check.content_id(route), "box": box_id, "from": route["from"],
           "to": route["to"], "started": stamp, "result": "pass" if len(hops) == len(route["hops"]) and
           all(h["ok"] for h in hops) else "fail", "hops": hops,
           "runner": {"kind": "vm", "host": os.uname().nodename, "kernel": os.uname().release}}
    json.dump(run, open(os.path.join(out_dir, "run.json"), "w"), indent=1)
    log(f"{run['result'].upper()}: {box_id} on {route_path}; run and disks in {os.path.relpath(out_dir, ROOT)}")
    return 0 if run["result"] == "pass" else 1


def main(args):
    if args[:1] == ["build"] and len(args) == 2:
        build(args[1])
        return 0
    if args[:1] == ["replay"] and len(args) == 3:
        return replay(args[1], args[2])
    return __doc__


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))  # through the finally below, like Ctrl-C
    signal.signal(signal.SIGHUP, lambda *_: sys.exit(129))
    try:
        sys.exit(main(sys.argv[1:]))
    finally:
        for guest in GUESTS:  # an exception or Ctrl-C must not leave a guest holding 3 GB and the forwarded ports
            guest.kill()
