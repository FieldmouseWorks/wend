#!/usr/bin/env python3
"""ELevate head-to-head: walk a wend box with AlmaLinux ELevate (Leapp) the way its official guides say, on the same
VM tier (tools/vm.py: the same cached base install, QEMU, slirp, static 10.0.2.20) and against the same checks a wend
route is held to after every hop. Maintainer-run on the VM host, like tools/vm.py; never from CI.

  python3 bench/elevate/elevate.py [--root-fs xfs-v4 --lvm] BOX

BOX: el/centos7-lamp, el/centos7-pyapp, el/centos8-lamp (CentOS 5 and 6 have no ELevate path to EL8+).

What it does, per stage (CentOS 7 -> Alma 8 -> 9 -> 10; CentOS 8 converts with almalinux-deploy first):
  - the guide's commands, verbatim where the guide gives one
    (wiki.almalinux.org/elevate/ELevating-CentOS7-to-AlmaLinux-10.html, ELevate-quickstart-guide.html)
  - `leapp preupgrade`; for each inhibitor, the guide's listed fix if one matches (tier "guide"), else a command the
    Leapp report itself gives as the remediation (tier "report"); up to 3 rounds. Anything left: the stage is BLOCKED.
  - `leapp upgrade`, reboot, wait (up to 2.5 h) for the box to come back on the target release
  - the box's checks, raw. Then, only if a database check failed, the RHEL docs' MariaDB post-step
    (start it, mysql_upgrade / mariadb-upgrade) and the checks again (tier "docs-post")
  - the guide's pre-cleanup before the next stage (exclude= lines, leftover packages, tmp dir, cache, old keys)
Every step taken beyond plain `leapp upgrade` is an intervention in the run file, with its tier.

Nothing here changes tools/: it imports vm.py as is, so the base install is the same cached one the wend gates use.
Runs go to .wend/elevate/<stamp>-<box>/ (run.json, per-stage logs and Leapp reports, disks).
"""
import json
import os
import shlex
import sys
import time

sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import vm  # noqa: E402

ELEVATE_RELEASE = "http://repo.almalinux.org/elevate/elevate-release-latest-el{n}.noarch.rpm"
DEPLOY = "https://raw.githubusercontent.com/AlmaLinux/almalinux-deploy/master/almalinux-deploy.sh"
EPEL7_ARCHIVE = ("[epel]\nname=Extra Packages for Enterprise Linux 7 - $basearch\n"
                 "baseurl=https://archives.fedoraproject.org/pub/archive/epel/7/$basearch/\nenabled=1\ngpgcheck=1\n"
                 "gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-EPEL-7\n")
EPEL8 = ("[epel]\nname=Extra Packages for Enterprise Linux 8 - $basearch\n"
         "metalink=https://mirrors.fedoraproject.org/metalink?repo=epel-8&arch=$basearch&infra=$infra&content=$contentdir\n"
         "enabled=1\ngpgcheck=1\ngpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-EPEL-8\n")
C8_VAULT = "".join(f"[{r.lower()}]\nname=CentOS Linux 8 - {r}\nbaseurl=https://vault.centos.org/8.5.2111/{r}/$basearch/os/\n"
                   "enabled=1\ngpgcheck=1\ngpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-centosofficial\n\n"
                   for r in ("BaseOS", "AppStream", "extras"))

# The guides' listed inhibitor fixes: (what in the report it answers, the commands). Matched on the report entry's
# title + summary, lowercased. Only these and the report's own command remediations are ever applied.
GUIDE_FIXES = {
    8: [("pata_acpi", ["rmmod pata_acpi"]),
        ("permitrootlogin", ["echo PermitRootLogin yes >> /etc/ssh/sshd_config"]),
        ("openssh", ["grep -q '^PermitRootLogin yes' /etc/ssh/sshd_config || echo PermitRootLogin yes >> /etc/ssh/sshd_config"]),
        ("pam_pkcs11", ["leapp answer --section remove_pam_pkcs11_module_check.confirm=True"])],
    9: [("allowzonedrifting", ["sed -i 's/^AllowZoneDrifting=.*/AllowZoneDrifting=no/' /etc/firewalld/firewalld.conf"]),
        ("vdo", ["leapp answer --section check_vdo.confirm=True"]),
        # Leapp's own hint and the guide both say "remove the file", which drops the box's static address. A competent
        # admin converts it instead, with the command Leapp itself prescribes for the same file on 9 -> 10. Tier admin:
        # not in the guide, counted as an intervention all the same.
        ("unsupported device types", ["nmcli connection migrate"], "admin")],
    10: [("dhclient", ["nmcli conn migrate",
                       "sed -i'.bak' 's/^dhcp=dhclient//g' /usr/lib/NetworkManager/conf.d/10-dhcp-dhclient.conf",
                       "systemctl restart NetworkManager"]),
         ("ifcfg", ["nmcli conn migrate"]),
         ("network-legacy", ["rm -f /etc/dracut.conf.d/50-network-legacy.conf"])],
}
# RHEL docs, "Migrating to a RHEL N version of MariaDB": in place, start the new server and run the upgrade tool
DB_POST = ("systemctl start mariadb; sleep 5; U=$(command -v mariadb-upgrade || command -v mysql_upgrade); "
           "$U -uroot --force; rc=$?; systemctl restart mariadb; exit $rc")
REPORT = "cat /var/log/leapp/leapp-report.json 2>/dev/null"
# Lab hardware, not ELevate: QEMU's i440fx (`-machine pc`, what vm.py boots) has an ISA floppy controller, so the guest
# loads `floppy`, which RHEL 8 dropped and Leapp inhibits on; a real server has no such controller. The initramfs can
# load it early, so the blacklist goes on every kernel's command line too (it then holds through Leapp's own reboots).
# Applied and verified right before every `leapp preupgrade`; recorded under lab_fixes, never as an intervention.
LAB = ("rmmod floppy 2>/dev/null; echo 'blacklist floppy' > /etc/modprobe.d/elevate-lab-nofloppy.conf; "
       "grubby --update-kernel=ALL --args=modprobe.blacklist=floppy; "
       "if grep -qw '^floppy' /proc/modules; then echo 'floppy is still loaded'; exit 1; fi; echo 'floppy not loaded'")
STATE = r"""echo "release: $(cat /etc/redhat-release)"; echo "kernel: $(uname -r)"; echo "selinux: $(getenforce 2>/dev/null)"
df -m /boot | tail -n1 | awk '{print "boot_free_mib:", $4}'
M=$(rpm -E %rhel); echo "leftover_older: $(rpm -qa | grep -vE "\.el${M}([._]|$)|gpg-pubkey" | grep -cE '\.el[0-9]+')"
echo "failed_units: $(systemctl --failed --no-legend 2>/dev/null | awk '{print $1}' | tr '\n' ' ')"
"""


def log(msg):
    print(f"[elevate {time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Run:
    def __init__(self, g, out_dir):
        self.g, self.out, self.n, self.interventions, self.lab_fixes = g, out_dir, 0, [], []

    def sh(self, stage, cmd, timeout=7200):
        """cmd in the guest, detached (a disconnect can't kill it), full output saved next to the run."""
        self.n += 1
        name = f"{self.n:03d}-{stage}"
        g = self.g
        g.ssh("mkdir -p /root/elevate-run")
        g.ssh(f"cat > /root/elevate-run/{name}.sh", stdin=vm.ENV + "\n" + cmd + "\n")
        g.ssh(f"cd /root/elevate-run && rm -f {name}.rc && (setsid nohup sh -c 'sh {name}.sh > {name}.out 2>&1; "
              f"echo $? > {name}.rc' </dev/null >/dev/null 2>&1 &)", timeout=30)
        t, rc = time.time(), None
        while time.time() - t < timeout and g.running():
            time.sleep(10)
            r, o = g.ssh(f"cat /root/elevate-run/{name}.rc 2>/dev/null", timeout=60)
            if r == 0 and o.strip().isdigit():
                rc = int(o.strip())
                break
        out = g.ssh(f"cat /root/elevate-run/{name}.out", timeout=300)[1]
        open(os.path.join(self.out, f"{name}.log"), "w").write(f"$ {cmd}\n{out}\n# rc={rc}\n")
        log(f"{stage}: {cmd[:100]!r} -> rc={rc}")
        return (124 if rc is None else rc), out

    def intervene(self, stage, tier, why, cmd):
        rc, out = self.sh(stage, cmd)
        self.interventions.append({"stage": stage, "tier": tier, "why": why, "cmd": cmd, "rc": rc})
        return rc, out


def entries(report_json):
    try:
        return json.loads(report_json).get("entries", [])
    except ValueError:
        return []


def inhibitors(es):
    return [e for e in es if "inhibitor" in e.get("groups", []) + e.get("flags", [])]


def text(e):
    return (e.get("title", "") + " " + e.get("summary", "")).lower()


def reboot_into(g, major, timeout=9000):
    """Reboot, then wait for the box to answer ssh on the target release (Leapp's upgrade initramfs and the SELinux
    relabel boot sit between). Returns whether it came back on `major` and stayed up."""
    before = g.boot_id()
    g.ssh("sync; (sleep 2; reboot) >/dev/null 2>&1 &", timeout=30)
    t = time.time()
    while time.time() - t < timeout and g.running():
        time.sleep(30)
        rc, o = g.ssh("rpm -E %rhel; cat /proc/sys/kernel/random/boot_id", timeout=30)
        lines = o.split()
        if rc == 0 and len(lines) == 2 and lines[0] == str(major) and lines[1] != before:
            time.sleep(120)  # a relabel or second reboot shows as a new boot id; settle on a stable one
            if g.boot_id() == lines[1]:
                return True
    return False


def score(g, box_doc, a, start_net, before):
    net = g.ssh(vm.NET)[1]
    after = vm.checks(g, box_doc, a)
    rc, kv = g.ssh("uname -r; " + a["hop"]["kernel_newest"])
    k = (kv.split() + [None, None])[:2]
    return {"net_kept": net == start_net, "net": None if net == start_net else {"start": start_net, "now": net},
            "lost": sorted(n for n, (ok, _) in before.items() if ok and not after.get(n, (False,))[0]),
            "failed_logs": {n: o[-1200:] for n, (ok, o) in after.items() if not ok},
            "kernel": k[0], "kernel_current": k[1] is not None and k[1] in (k[0], (k[0] or "").rsplit(".", 1)[0]),
            "state": g.ssh(STATE)[1].strip().splitlines()}, after


def preupgrade(r, stage, major):
    """leapp preupgrade, then the guide's fixes and the report's own commands for what inhibits, up to 3 rounds.
    Returns the inhibitors left (empty: go)."""
    tried = set()
    for rnd in range(4):
        rc, out = r.sh("lab", LAB)
        r.lab_fixes.append({"stage": stage, "round": rnd, "rc": rc, "out": out.strip()[-300:]})
        if rc:  # a lab fault would be scored as an ELevate inhibitor: stop instead
            raise RuntimeError(f"lab fix failed before {stage} preupgrade round {rnd}: {out[-300:]}")
        r.sh("diag", "for f in /etc/sysconfig/network-scripts/ifcfg-*; do echo \"## $f\"; cat \"$f\"; done; nmcli -t connection show")
        r.sh(stage, "leapp preupgrade")
        rep = r.g.ssh(REPORT, timeout=120)[1]
        open(os.path.join(r.out, f"{stage}-preupgrade-{rnd}.json"), "w").write(rep)
        r.g.ssh("cp /var/log/leapp/leapp-report.txt /root/elevate-run/ 2>/dev/null")
        inh = inhibitors(entries(rep))
        if not inh or rnd == 3:
            return inh
        acted = False
        for e in inh:
            for key, cmds, *tier in GUIDE_FIXES.get(major, []):
                if key in text(e) and (key, e.get("title")) not in tried:
                    tried.add((key, e.get("title")))
                    for c in cmds:
                        r.intervene(stage, (tier or ["guide"])[0], e.get("title", ""), c)
                    acted = True
                    break
            else:
                for rem in e.get("detail", {}).get("remediations", []):
                    if rem.get("type") == "command" and (str(rem["context"]), e.get("title")) not in tried:
                        tried.add((str(rem["context"]), e.get("title")))
                        ctx = rem["context"]
                        r.intervene(stage, "report", e.get("title", ""),
                                    shlex.join(ctx) if isinstance(ctx, list) else str(ctx))
                        acted = True
        if not acted:
            return inh
    return inh


def cleanup(r, stage, old):
    """The guide's pre-cleanup before the next stage, each step recorded only if it found something to do."""
    rc, ex = r.sh(stage, "grep -H '^exclude=' /etc/yum.conf /etc/dnf/dnf.conf 2>/dev/null")
    if "leapp" in ex or "elevate" in ex:
        r.intervene(stage, "guide", "exclude= holds elevate/leapp",
                    "sed -i '/^exclude=/{/leapp\\|elevate/d}' /etc/yum.conf /etc/dnf/dnf.conf")
    rc, left = r.sh(stage, f"rpm -qa | grep -E '\\.el{old}([._]|$)|leapp|elevate' | sort")
    pk = [p for p in left.split() if p and not p.startswith("gpg-pubkey")]
    if pk:
        q = " ".join(shlex.quote(p) for p in pk)
        rc, _ = r.intervene(stage, "guide", f"{len(pk)} leftover el{old}/leapp packages", f"dnf -y remove {q}")
        if rc:
            r.intervene(stage, "guide", "dnf couldn't remove them", f"rpm -e --nodeps {q}")
    r.sh(stage, "rm -fr /root/tmp_leapp_py3; dnf clean all")
    rc, keys = r.sh(stage, "rpm -q gpg-pubkey --qf '%{NAME}-%{VERSION}-%{RELEASE}\\t%{SUMMARY}\\n'")
    old_keys = [ln.split("\t")[0] for ln in keys.splitlines()
                if "\t" in ln and (f"EPEL ({old})" in ln or f"CentOS-{old}" in ln or f"CentOS {old}" in ln)]
    if old_keys:
        r.intervene(stage, "guide", "old release keys", "rpm -e " + " ".join(old_keys))


def stage_leapp(r, g, major, box_doc, a, start_net, before, stage_log):
    stage = f"alma{major}"
    for c in (f"yum install -y {ELEVATE_RELEASE.format(n=major - 1)}", "yum install -y leapp-upgrade leapp-data-almalinux"):
        rc, out = r.sh(stage, c)
        if rc:
            stage_log.update(result="install-failed", detail=out[-1500:])
            return False
    inh = preupgrade(r, stage, major)
    if inh:
        stage_log.update(result="blocked", inhibitors=[{"title": e.get("title"), "summary": e.get("summary", "")[:600]}
                                                       for e in inh])
        return False
    rc, out = r.sh(stage, "leapp upgrade")
    r.g.ssh("cp /var/log/leapp/leapp-report.txt /root/elevate-run/ 2>/dev/null")
    open(os.path.join(r.out, f"{stage}-upgrade.json"), "w").write(g.ssh(REPORT, timeout=120)[1])
    if rc:
        stage_log.update(result="upgrade-failed", detail=out[-2000:])
        return False
    return True


def main(args):
    while args[:1] and args[0].startswith("--"):
        if args[0] == "--lvm":
            vm.LAYOUT["lvm"], args = True, args[1:]
        elif args[0] == "--root-fs" and len(args) > 1 and args[1] in vm.ROOT_FS:
            vm.LAYOUT["root_fs"], args = args[1], args[2:]
        else:
            return __doc__
    if len(args) != 1:
        return __doc__
    box_id = args[0]
    d, box_doc = vm.build(box_id)
    rel = box_doc["release"]
    if rel not in ("centos7", "centos8"):
        print(f"{rel}: ELevate has no path from here to EL8 or later (its paths start at CentOS 7; CentOS 8 converts "
              "with almalinux-deploy). Recorded as unsupported, no VM time spent.")
        return 2
    a = vm.guest_adapter("el")
    vm.sandbox.ensure_mirror(vm.load("adapters/el.toml"))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tag = "-".join(filter(None, [vm.FIRMWARE, vm.layout_tag()]))
    out_dir = os.path.join(ROOT, ".wend", "elevate", f"{stamp}-{box_id.replace('/', '_')}-{tag}")
    os.makedirs(out_dir)
    key, port = os.path.join(d, "key"), box_doc.get("vm", {}).get("ssh_port", 22)
    hw = vm.hardware(a, rel)
    disk = os.path.join(out_dir, "elevate.qcow2")  # one disk for the whole walk: ELevate has no per-hop rollback
    vm.overlay(os.path.join(d, "base.qcow2"), disk)
    g = vm.Guest(disk, key, port, hw)
    g.start()
    if not g.wait_ssh():
        raise RuntimeError("the base box didn't come up")
    r = Run(g, out_dir)
    start_net = g.ssh(vm.NET)[1]
    before = vm.checks(g, box_doc, a)
    run = {"schema": "wend.elevate-vs/0", "box": box_id, "from": rel, "started": stamp, "firmware": vm.FIRMWARE,
           "layout": {k: v for k, v in vm.LAYOUT.items() if v},
           "start_checks": {n: ok for n, (ok, _) in before.items()}, "stages": [], "interventions": r.interventions}
    t0 = time.time()
    try:
        r.lab_fixes = run["lab_fixes"] = []  # see LAB: applied and verified before every leapp preupgrade
        # the box as a real one has it: the harness's shim repo file out, the stock repos the guides start from in
        if rel == "centos7":
            r.sh("setup", "rm -f /etc/yum.repos.d/*.repo && curl -fsSo /etc/yum.repos.d/CentOS-Base.repo "
                          "https://el7.repo.almalinux.org/centos/CentOS-Base.repo" +
                 (f" && printf {shlex.quote(EPEL7_ARCHIVE)} > /etc/yum.repos.d/epel.repo"
                  if g.ssh("rpm -q epel-release")[0] == 0 else ""))
            r.sh("setup", "yum clean all && yum upgrade -y")
            ok = reboot_into(g, 7, 900)
            run["stages"].append({"stage": "centos7-update", "ok": ok})
            majors = [8, 9, 10]
        else:
            r.sh("setup", f"rm -f /etc/yum.repos.d/*.repo && printf {shlex.quote(C8_VAULT)} > /etc/yum.repos.d/CentOS-Vault.repo" +
                 (f" && printf {shlex.quote(EPEL8)} > /etc/yum.repos.d/epel.repo"
                  if g.ssh("rpm -q epel-release")[0] == 0 else "") + " && dnf clean all && dnf makecache")
            st = {"stage": "alma8-deploy", "t0": time.time()}
            rc, out = r.sh("alma8", f"curl -fsSO {DEPLOY} && bash almalinux-deploy.sh")
            st["ok"] = rc == 0 and reboot_into(g, 8, 1800)
            if rc:
                st["detail"] = out[-2000:]
            if st["ok"]:
                st["score"], _ = score(g, box_doc, a, start_net, before)
            st["seconds"] = round(time.time() - st.pop("t0"))
            run["stages"].append(st)
            majors = [9, 10] if st["ok"] else []
        for major in majors:
            st = {"stage": f"alma{major}", "t0": time.time()}
            run["stages"].append(st)
            # the guide's pre-cleanup: before stage 2 when the box came from EL7, and before stage 3
            if major >= 9 and not (rel == "centos8" and major == 9):
                cleanup(r, f"alma{major}", major - 2)
            if not stage_leapp(r, g, major, box_doc, a, start_net, before, st):
                st["seconds"] = round(time.time() - st.pop("t0"))
                break
            st["came_back"] = reboot_into(g, major)
            if not st["came_back"]:
                # no ssh on the target release: tell a box that's up without ssh from one that never booted
                http = vm.subprocess.run(["curl", "-s", "-m", "10", "-o", "/dev/null", "-w", "%{http_code}",
                                          f"http://127.0.0.1:{vm.HTTP_FWD}/"], capture_output=True, text=True).stdout
                st.update(result="no-boot", http_code=http, ssh_answers=g.ssh("rpm -E %rhel", timeout=30),
                          console_tail=open(g.console, errors="replace").read()[-5000:])
                st["seconds"] = round(time.time() - st.pop("t0"))
                break
            st["score"], after = score(g, box_doc, a, start_net, before)
            dbs = [n for n in st["score"]["lost"] if "db" in n or "site" in n or "daemons" in n]
            if dbs and g.ssh("rpm -q mariadb-server")[0] == 0:
                r.intervene(f"alma{major}", "docs-post", f"lost {dbs} after the upgrade", DB_POST)
                st["score_after_docs_post"], _ = score(g, box_doc, a, start_net, before)
            s = st.get("score_after_docs_post", st["score"])
            st["result"] = "pass" if s["net_kept"] and not s["lost"] and s["kernel_current"] else "degraded"
            st["seconds"] = round(time.time() - st.pop("t0"))
            log(f"alma{major}: {st['result']} lost={s['lost']} net_kept={s['net_kept']} kernel_current={s['kernel_current']}")
    finally:
        run["seconds"] = round(time.time() - t0)
        run["reached"] = next((s["stage"] for s in reversed(run["stages"]) if s.get("came_back") or
                               (s["stage"].startswith("alma8-deploy") and s.get("ok"))), rel)
        run["runner"] = {"host": os.uname().nodename, "kernel": os.uname().release}
        json.dump(run, open(os.path.join(out_dir, "run.json"), "w"), indent=1, default=str)
        g.stop()
        log(f"done: reached {run['reached']}; {len(r.interventions)} interventions; run in {os.path.relpath(out_dir, ROOT)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    finally:
        for guest in vm.GUESTS:
            guest.kill()
