"""Sheltering an app (SPEC "Sheltering an app"): the reference ops, chroot (CI) tier.

A box's `[shelter.<app>]` table is the owner's confirmed plan. tools/check.py holds it to strict shapes (check_shelter)
before anything here runs, so every value substituted into a command below has already matched a plain pattern: no
quotes, no `$`, no `%`, no backslashes. Like tools/backends.py, the shell lives here, in the harness; the plan only
names paths, units, users and cron lines.

  build        on keeps_runtimes_on: the interpreter and the owners of what the app's programs load, their closure
               from that release's own repos (final versions), each rpm checked against the index's sha256 and its
               own signature (the release's key), installed into /var/lib/wend/shelter/<app>/root by the release's
               rpm, plus the carried layer; writes shelter.lock
  prove        the box's checks against the shelter root, in a copy of the box, with the podman stand-in
  stop-native  the drop hop's pre phase: disable the app's unit, comment out its cron lines
  start        the drop hop's post phase: podman from the landing release, the subordinate id range, the units

The chroot tier has no image store: the image ID is the digest of the built root (recorded in the lock), and the root
stays at its path. tools/podman_standin.sh stands in for `podman run` while checks run, and proves the userland
only, never confinement (runs record confinement_proved: false).
"""
import hashlib
import os
import re
import stat
import urllib.request
import xml.etree.ElementTree as ET

import backends
from sandbox import safe_rm, tail

SHELTER = "/var/lib/wend/shelter"
SAVED = backends.SAVED
ADDED = "/var/lib/wend/added-families"
STANDIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "podman_standin.sh")
STANDIN_REAL = "/usr/bin/.wend-podman-real"  # where the real podman waits while the stand-in runs a check
# The key each release's packages are signed with, as the box on that release has it. A release missing here can't
# be sheltered yet: the build refuses rather than trust unsigned bytes.
KEYS = {"centos7": "/etc/pki/rpm-gpg/RPM-GPG-KEY-CentOS-7"}
PODMAN = ["podman", "nftables"]  # installed from the landing release by shelter.start
ADDS = ["podman", "nftables"]    # their families, for the services check (SPEC "restores and adds")
PROXYD, PORT_PROXY = "/usr/libexec/wend/socket-proxy", "/usr/libexec/wend/shelter-port-proxy"
STOCK_PROXYD = "/usr/lib/systemd/systemd-socket-proxyd"
CIL = "wend_shelter"  # the SELinux module that lets a shelter reach its socket proxies
SUBID_BASE, SUBID_SIZE = 1 << 30, 65536  # above useradd's SUB_UID_MAX (600100000): nothing else allocates there
RUNNER = r'''
import atexit, os, sys, threading
out, seen = os.environ["WEND_SHELTER_OUT"], []
def dump():
    if seen:
        return
    seen.append(1)
    files = set([os.path.realpath(sys.executable)])
    for m in list(sys.modules.values()):
        f = getattr(m, "__file__", None)
        if f:
            files.add(os.path.abspath(f))
    try:
        for line in open("/proc/self/maps"):
            p = line.split(None, 5)
            if len(p) == 6 and p[5].strip().startswith("/"):
                files.add(p[5].strip())
    except IOError:
        pass
    f = open(out, "a")
    f.write("\n".join(sorted(files)) + "\n")
    f.close()
def stop():
    dump()
    os._exit(0)
atexit.register(dump)
t = threading.Timer(5, stop)
t.daemon = True
t.start()
sys.argv = sys.argv[1:]
import runpy
runpy.run_path(sys.argv[0], run_name="__main__")
'''


class Refused(Exception):
    """The shelter can't be built or proved: the walk stops on keeps_runtimes_on (the "stop" option)."""


# ---------- where on a route ----------

def keeps_runtimes_on(adapter, route):
    """SPEC "Runtime preflight": the last release on the route that still ships a Python 2, counting the route's
    start and only the releases a route boots into (may_skip ones are passed through). As client/src/hardware.rs."""
    keeps = route["from"]
    for h in route["hops"]:
        if h in adapter.get("may_skip", []):
            continue
        if adapter["runtimes"][h]["python2"]:
            keeps = h
        else:
            break
    return keeps


def drop_hop(adapter, route):
    """The hop that leaves keeps_runtimes_on (None when the route never drops Python 2)."""
    keeps, hops = keeps_runtimes_on(adapter, route), route["hops"]
    if keeps == route["to"]:
        return None
    i = 0 if keeps == route["from"] else hops.index(keeps) + 1
    return hops[i] if i < len(hops) else None


# ---------- cron lines to timers ----------

DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
NAMES = {"dow": {d.lower(): i for i, d in enumerate(DOW)},
         "month": {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep",
                                                    "oct", "nov", "dec"])}}
FIELDS = [("minute", 0, 59), ("hour", 0, 23), ("dom", 1, 31), ("month", 1, 12), ("dow", 0, 7)]
AT = {"@hourly": "*-*-* *:00:00", "@daily": "*-*-* 00:00:00", "@midnight": "*-*-* 00:00:00",
      "@weekly": "Sun *-*-* 00:00:00", "@monthly": "*-*-01 00:00:00", "@yearly": "*-01-01 00:00:00",
      "@annually": "*-01-01 00:00:00"}


def _field(text, name, lo, hi):
    """One cron field as a systemd calendar component ('*', '1,15', '1..5', '0/15'); ValueError if it doesn't
    translate exactly."""
    if text == "*":
        return "*"
    if text.startswith("*/"):
        step = text[2:]
        if not step.isdigit() or not 0 < int(step) <= hi or name == "dow":
            raise ValueError(f"{name} {text!r}")
        return f"{lo}/{int(step)}"
    out = []
    for part in text.split(","):
        a, dash, b = part.partition("-")
        vals = []
        for x in (a, b) if dash else (a,):
            x = x.lower()
            v = NAMES.get(name, {}).get(x, int(x) if x.isdigit() else None)
            if v is None or not lo <= v <= hi:
                raise ValueError(f"{name} {text!r}")
            vals.append(v)
        if dash and vals[0] > vals[1]:
            raise ValueError(f"{name} {text!r}")
        if name == "dow":  # cron's 7 is Sunday too; systemd takes names
            out += [DOW[v % 7] for v in (range(vals[0], vals[1] + 1) if dash else vals)]
        else:
            out.append(f"{vals[0]:02d}..{vals[1]:02d}" if dash else f"{vals[0]:02d}")
    if name == "dow":
        out = sorted(set(out), key=DOW.index)
    return ",".join(out)


def cron_calendar(line):
    """A crontab line (five fields or an @name, then the command) as (OnCalendar= values, command). Every standard
    five-field schedule translates; day-of-month and day-of-week both restricted means either, so two values, unless
    one of them starts with `*` (`*/2`), where cronie wants both. ValueError
    for anything else (@reboot, a stepped range, a name with a step): SPEC "The ops", refused at plan time."""
    words = line.split()
    if words and words[0].startswith("@"):
        if words[0] not in AT or len(words) < 2:
            raise ValueError(f"{words[0]} doesn't translate to a timer")
        return [AT[words[0]]], " ".join(words[1:])
    if len(words) < 6:
        raise ValueError("a cron line has five schedule fields and a command")
    m, h, dom, mon, dow = (_field(w, *f) for w, f in zip(words[:5], FIELDS))
    when = f"*-{mon}-{dom} {h}:{m}:00"
    if dom != "*" and dow != "*" and not words[2].startswith("*") and not words[4].startswith("*"):
        cal = [when, f"{dow} *-{mon}-* {h}:{m}:00"]  # cron runs on either day field
    elif dow != "*":  # cronie: a day field starting with * means both must match
        cal = [f"{dow} {when}"]
    else:
        cal = [when]
    return cal, " ".join(words[5:])


# ---------- the repos: a release's final versions, and their closure ----------

def _segs(s):
    return re.findall(r"~|[0-9]+|[A-Za-z]+", s)


def vercmp(a, b):
    """rpm's version comparison (rpmvercmp), enough for EL's version and release strings."""
    x, y = _segs(a), _segs(b)
    for p, q in zip(x, y):
        if p == "~" or q == "~":
            if p != q:
                return -1 if p == "~" else 1
            continue
        if p.isdigit() != q.isdigit():
            return 1 if p.isdigit() else -1
        c = (int(p) > int(q)) - (int(p) < int(q)) if p.isdigit() else (p > q) - (p < q)
        if c:
            return c
    if len(x) == len(y):
        return 0
    # the longer one is newer, unless what it has more of starts with a tilde
    more, sign = (x[len(y)], 1) if len(x) > len(y) else (y[len(x)], -1)
    return -sign if more == "~" else sign


def evr_newer(a, b):
    """a is a newer (epoch, version, release) than b."""
    if int(a[0]) != int(b[0]):
        return int(a[0]) > int(b[0])
    return (vercmp(a[1], b[1]) or vercmp(a[2], b[2])) > 0


NS = {"c": "http://linux.duke.edu/metadata/common", "r": "http://linux.duke.edu/metadata/rpm"}


def read_repos(adapter, release):
    """Every x86_64 and noarch package of the release's own repos, newest of each name, and the sha256 of each repo's
    repomd.xml (by origin URL, as runs pin sources)."""
    shim = adapter["mirror"]["shim"]
    pkgs, indexes = {}, {}
    for url in adapter["releases"][release]["repos"].values():
        base = f"{shim}/{url}/"
        repomd = urllib.request.urlopen(base + "repodata/repomd.xml", timeout=300).read()
        indexes[f"https://{url}/repodata/repomd.xml"] = "sha256:" + hashlib.sha256(repomd).hexdigest()
        href = backends.primary_href(repomd.decode())
        root = ET.fromstring(backends.unpack(href, urllib.request.urlopen(base + href, timeout=300).read()))
        for p in root.findall("c:package", NS):
            if p.find("c:arch", NS).text not in ("x86_64", "noarch"):
                continue
            n, f, v = p.find("c:name", NS).text, p.find("c:format", NS), p.find("c:version", NS)
            ck = p.find("c:checksum", NS)
            evr = (v.get("epoch") or "0", v.get("ver"), v.get("rel"))
            if n in pkgs and not evr_newer(evr, pkgs[n]["evr"]):
                continue
            pkgs[n] = {"evr": evr, "arch": p.find("c:arch", NS).text, "url": base + p.find("c:location", NS).get("href"),
                       "origin": f"https://{url}/" + p.find("c:location", NS).get("href"),
                       "sha256": ck.text if ck.get("type") == "sha256" else None,
                       "req": [e.get("name") for e in f.findall("r:requires/r:entry", NS)
                               if not e.get("name").startswith("rpmlib(")],
                       "prov": [n] + [e.get("name") for e in f.findall("r:provides/r:entry", NS)] +
                               [e.text for e in f.findall("c:file", NS)]}
    return pkgs, indexes


def closure(pkgs, seeds):
    """The seeds and everything they require, by provides and file names: the newest package of a name, the
    package named like the requirement first, else the shortest name (sandbox.resolve's rule). Unresolvable
    requirements are left for rpm's own dependency check to refuse."""
    prov = {}
    for n, p in pkgs.items():
        for x in p["prov"]:
            prov.setdefault(x, []).append(n)
    missing = [s for s in seeds if s not in pkgs]
    if missing:
        raise Refused(f"not in the release's repos: {', '.join(missing)}")
    want, todo = set(), list(seeds)
    while todo:
        n = todo.pop()
        if n in want:
            continue
        want.add(n)
        for r in pkgs[n]["req"]:
            c = prov.get(r, [])
            if c and not any(x in want for x in c):
                todo.append(r if r in c else sorted(c, key=lambda x: (len(x), x))[0])
    return sorted(want)


# ---------- the root's digest: the chroot tier's image ID ----------

def tree_digest(root):
    """sha256 over every entry under root (path, type, mode, owner, content or link target), never following a
    symlink. The same tree gives the same ID."""
    h = hashlib.sha256()
    for d, dirs, files in os.walk(root):
        dirs.sort()
        for n in sorted(dirs + files):
            p = os.path.join(d, n)
            st = os.lstat(p)
            rel = os.path.relpath(p, root)
            head = f"{rel}\0{stat.S_IFMT(st.st_mode):o}\0{stat.S_IMODE(st.st_mode):o}\0{st.st_uid}:{st.st_gid}\0"
            if stat.S_ISLNK(st.st_mode):
                h.update((head + os.readlink(p)).encode("utf-8", "surrogateescape") + b"\n")
            elif stat.S_ISREG(st.st_mode):
                with open(p, "rb") as f:
                    h.update(head.encode("utf-8", "surrogateescape") + hashlib.sha256(f.read()).digest() + b"\n")
            else:
                h.update((head + str(st.st_rdev)).encode("utf-8", "surrogateescape") + b"\n")
    return "sha256:" + h.hexdigest()


def _q(s):
    """A TOML basic string."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ---------- shelter.build ----------

def programs(box, plan):
    """The app's own command lines: the unit's ExecStart and each cron line's command. Refused unless each is a plain
    interpreter-and-script line (the plan's cron lines are held to that by check.py; the unit is the box's own)."""
    unit = plan["unit"]
    rc, out = box.run(f"for d in /etc/systemd/system /usr/lib/systemd/system; do [ -f $d/{unit} ] && "
                      f"sed -n 's/^ExecStart=//p' $d/{unit} && exit 0; done; exit 1")
    lines = [x.strip() for x in out.splitlines() if x.strip()]
    if rc or len(lines) != 1 or not re.fullmatch(CMD, lines[0]):
        raise Refused(f"{unit} has no single plain ExecStart (got {tail(out, 300)!r})")
    progs = [lines[0]]
    for c in plan.get("cron", []):
        progs.append(cron_calendar(c["line"])[1])
    return progs


def _host_write(root, path, text):
    """Write a file into a box copy from the host, never through a symlink the box's package scripts planted."""
    real = os.path.realpath(root)
    d, name = os.path.split(f"{real}{path}")
    if not backends.plain_dir(real, d):
        raise Refused(f"{os.path.dirname(path)} in the box isn't a plain directory")
    if os.path.lexists(f"{d}/{name}"):
        os.unlink(f"{d}/{name}")
    fd = os.open(f"{d}/{name}", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w") as f:
        f.write(text)


CMD = r"/[A-Za-z0-9_./+-]+( [A-Za-z0-9_./=:@,+-]+)*"  # an absolute interpreter, then plain words


def build(box, adapter, release, app, plan, scratch, log):
    """shelter.build on the box, which is on `release` (keeps_runtimes_on). Writes shelter.lock and returns the record a run keeps."""
    if release not in KEYS:
        raise Refused(f"no signing key known for {release}")
    d, user = f"{SHELTER}/{app}", plan["user"]
    rel = adapter["releases"][release]
    rc, out = box.run(f"grep -q 'release {rel['major']}[. ]' {adapter['release_file']} && test -f {KEYS[release]} && "
                      f"id -u {user} && test -d {plan['code']}")
    if rc:
        raise Refused(f"the box isn't on {release} with {KEYS[release]}, user {user} and {plan['code']}: {tail(out, 300)}")
    progs = programs(box, plan)
    for c in plan.get("cron", []):
        if box.run(f"grep -Fqx '{c['line']}' /var/spool/cron/{c['user']}")[0]:
            raise Refused(f"{c['user']}'s crontab has no line {c['line']!r}")
    # 1. what the programs load, run once each in a throwaway copy (SPEC: never on the real machine)
    log(f"shelter {app}: finding what its programs load")
    s = box.copy_to(scratch)
    try:
        _host_write(scratch, "/tmp/wend-shelter-runner.py", RUNNER)
        files = set()
        interps = set()
        for p in progs:
            w = p.split()
            interp, script = w[0], w[1:]
            rc, out = s.run(f"{interp} -c 'import sys; print(sys.version_info[0])' 2>&1")
            if rc or out.strip() != "2":
                raise Refused(f"{interp} is not a Python 2 interpreter ({tail(out, 200)!r})")
            if not script or script[0].startswith("-"):
                raise Refused(f"{p!r}: only `interpreter script [args]` command lines can be sheltered so far")
            interps.add(interp)
            rc, out = s.run(f": > /tmp/wend-files && chmod 666 /tmp/wend-files && cd / && runuser -u {user} -- "
                            f"env WEND_SHELTER_OUT=/tmp/wend-files {interp} /tmp/wend-shelter-runner.py "
                            f"{' '.join(script)} >/tmp/wend-run.log 2>&1; "
                            "cat /tmp/wend-files", timeout=120, isolated=True)
            got = [x for x in out.splitlines() if x.startswith("/")]
            if not got:
                raise Refused(f"{p!r} loaded nothing wend could see: {tail(out, 300)}")
            files |= set(got)
        skip = ("/proc/", "/sys/", "/dev/", "/tmp/", "/run/", "/var/run/", plan["code"] + "/") + \
            tuple(x + "/" for x in plan["data"])
        files = sorted(f for f in files if not f.startswith(skip) and f not in ("/tmp/wend-shelter-runner.py",))
        _host_write(scratch, "/tmp/wend-files", "\n".join(files + [
            os.path.realpath(f"{scratch}{i}")[len(os.path.realpath(scratch)):] for i in interps]) + "\n")
        # rpm -qf's own status says whether a package owns the file (for one nobody owns, it still prints a line:
        # "file ... is not owned by any package"); pip's modules are the usual unowned ones
        rc, out = s.run("while read f; do [ -f \"$f\" ] || continue; "
                        "if o=$(rpm -qf --qf '%{NAME}\\n' \"$f\" 2>/dev/null); "
                        "then echo \"OWNED $(echo \"$o\" | head -n1) $f\"; else echo \"UNOWNED - $f\"; fi; done < /tmp/wend-files")
        owners, unowned = set(), []
        for line in out.splitlines():
            k, _, rest = line.partition(" ")
            name, _, path = rest.partition(" ")
            if k == "OWNED":
                owners.add(name)
            elif k == "UNOWNED":
                unowned.append(path)
        if not owners:
            raise Refused(f"no package owns what the programs load: {tail(out, 300)}")
        # packaged files the box has changed (a module pip overwrote): carried with the box's bytes
        rc, out = s.run(f"rpm -V {' '.join(sorted(owners))} 2>/dev/null | awk '$1 ~ /5/ {{print $NF}}'")
        changed = sorted(set(out.split()) & set(files))
    finally:
        safe_rm(scratch)
    # 2. the closure, from the release's own repos, at its final versions
    log(f"shelter {app}: {len(owners)} packages own what it loads; resolving their closure in {release}")
    pkgs, indexes = read_repos(adapter, release)
    names = closure(pkgs, sorted(owners))
    # 3. fetch, check each against the index's sha256, then its signature with the release's key
    work = f"{d}/pkgs"
    rc, out = box.run(f"rm -rf {d}/root {work} {d}/keys && mkdir -p {work} {d}/keys")
    real = os.path.realpath(box.root)
    if rc or not backends.plain_dir(real, f"{real}{work}"):
        raise Refused(f"can't make {work}: {tail(out, 300)}")
    log(f"shelter {app}: fetching {len(names)} packages")
    locked = []
    for n in names:
        p = pkgs[n]
        fn = p["url"].rsplit("/", 1)[1]
        data = urllib.request.urlopen(p["url"], timeout=300).read()
        got = hashlib.sha256(data).hexdigest()
        if not p["sha256"] or got != p["sha256"]:
            raise Refused(f"{fn}: sha256 {got} is not the one {release}'s index lists ({p['sha256']})")
        with open(f"{real}{work}/{fn}", "wb") as f:
            f.write(data)
        e, v, r = p["evr"]
        locked.append({"nevra": f"{n}-{'' if e == '0' else e + ':'}{v}-{r}.{p['arch']}", "sha256": f"sha256:{got}",
                       "url": p["origin"]})
    rc, out = box.run(f"set -e; rpm --dbpath {d}/keys --initdb; rpm --dbpath {d}/keys --import {KEYS[release]}; "
                      f"cd {work}; for f in *.rpm; do o=$(rpm --dbpath {d}/keys -K $f 2>&1) || "
                      "{ echo \"refusing: $o\"; exit 1; }; case \"$o\" in *'NOT OK'*|*NOKEY*) echo \"refusing: $o\"; "
                      "exit 1;; *' pgp '*|*' gpg '*|*' signatures OK'*) ;; *) echo \"refusing, not signed: $o\"; "
                      "exit 1;; esac; done; echo signatures checked")
    if rc:
        raise Refused(f"signature check against {KEYS[release]}: {tail(out, 600)}")
    # 4. install with the release's own rpm, then the carried layer and the mount points
    log(f"shelter {app}: installing into {d}/root")
    rc, out = box.run(f"set -e; mkdir -p {d}/root; rpm --root {d}/root --initdb; "
                      f"rpm --root {d}/root -Uvh --excludedocs {work}/*.rpm > {d}/install.log 2>&1 || "
                      f"{{ tail -n 30 {d}/install.log; exit 1; }}; rpm --root {d}/root -q {' '.join(names)} >/dev/null")
    if rc:
        raise Refused(f"installing the shelter root failed: {tail(out, 1500)}")
    carried = sorted(set(unowned) | set(changed))
    mounts = [plan["code"]] + plan["data"] + sorted({os.path.dirname(x) for x in plan.get("sockets", [])})
    rc, out = box.run(
        f"set -e; R={d}/root; grep '^{user}:' /etc/passwd >> $R/etc/passwd; "
        f"G=$(id -gn {user}); grep \"^$G:\" /etc/group >> $R/etc/group; "
        + "".join(f"mkdir -p $R$(dirname {f}) && cp -a {f} $R{f}; " for f in carried)
        + "".join(f"mkdir -p $R{m}; " for m in mounts)
        + f"rm -rf {work} {d}/keys $R/var/lib/rpm/__db.* $R/var/cache/*; grep '^{user}:' /etc/passwd; "
        f"grep \"^$G:\" /etc/group")
    if rc:
        raise Refused(f"the carried layer: {tail(out, 600)}")
    passwd, group = (["", ""] + out.strip().splitlines())[-2:]
    if not passwd.startswith(f"{user}:"):
        raise Refused(f"no passwd line for {user}: {tail(out, 300)}")
    hashes = {}
    for f in carried:
        h = backends.file_sha256(real, f"{real}{d}/root{f}")
        if not h:
            raise Refused(f"carried file {f} is not a regular file")
        hashes[f] = h
    image = tree_digest(f"{real}{d}/root")
    out = [f"# wend shelter lock for {app} (SPEC \"Sheltering an app\"), written by shelter.build. The userland it",
           "# pins is a dead release's and gets no security fixes: port the app, then remove the shelter.",
           'schema = "wend.shelter-lock/0"', f"app = {_q(app)}", f"release = {_q(release)}",
           f"key = {_q(KEYS[release])}", f"image = {_q(image)}", f"user = {_q(passwd)}", f"group = {_q(group)}",
           "", "[indexes]"] + [f"{_q(u)} = {_q(h)}" for u, h in sorted(indexes.items())]
    for p in locked:
        out += ["", "[[packages]]", f"nevra = {_q(p['nevra'])}", f"sha256 = {_q(p['sha256'])}", f"url = {_q(p['url'])}"]
    for f in carried:
        out += ["", "[[carried]]", f"path = {_q(f)}", f"sha256 = {_q(hashes[f])}",
                f"why = {_q('changed' if f in changed else 'unowned')}"]
    lock = "\n".join(out) + "\n"
    if not backends.plain_dir(real, f"{real}{d}") or os.path.lexists(f"{real}{d}/shelter.lock"):
        raise Refused(f"{d} is not a plain directory, or already has a lock")
    with open(f"{real}{d}/shelter.lock", "w") as f:
        f.write(lock)
    log(f"shelter {app}: {len(locked)} packages, {len(carried)} carried files, image {image[:19]}")
    return {"app": app, "release": release, "lock_sha256": "sha256:" + hashlib.sha256(lock.encode()).hexdigest(),
            "image": image, "packages": len(locked), "carried": carried, "tier": "chroot", "confinement_proved": False}


# ---------- the steps on the box: stop-native (pre) and start (post) ----------

def stop_native(app, plan):
    """shelter.stop-native: stop and disable the app's unit, and comment out each of its cron lines (precondition: the
    line's exact text), keeping the original crontab under /var/lib/wend/saved. Nothing is deleted."""
    u = plan["unit"]
    # stop only where systemd runs this root (a chroot has no manager to ask, as hop.kernel_before reasons)
    out = [f"if [ / -ef /proc/1/root ]; then systemctl stop {u} || exit 1; fi; systemctl disable {u} || exit 1; "
           f"[ \"$(systemctl is-enabled {u} 2>/dev/null)\" != enabled ] || {{ echo '{u} is still enabled'; exit 1; }}"]
    for c in plan.get("cron", []):
        f, line = f"/var/spool/cron/{c['user']}", c["line"]
        out.append(f"F={f}; grep -Fqx '{line}' $F || {{ echo \"refusing: $F has no line '{line}'\"; exit 1; }}; "
                   f"mkdir -p {SAVED}/var/spool/cron && {{ [ -e {SAVED}$F ] || cp -a $F {SAVED}$F; }} && "
                   f"awk -v l='{line}' '$0 == l {{print \"# wend-shelter:{app} \" $0; next}} {{print}}' $F > $F.wend && "
                   f"cat $F.wend > $F && rm -f $F.wend && ! grep -Fqx '{line}' $F")
    return out


def _heredoc(path, text):
    return f"cat > {path} <<WEND_UNIT\n{text}WEND_UNIT"


HEADER = ("# Written by wend (shelter.start) for the shelter \"{app}\": the owner's own program, on {release}'s userland\n"
          "# in a container. That userland is a dead release's and gets no security fixes. Port the app, then remove\n"
          "# the shelter. SPEC \"Sheltering an app\".\n")


def podman_run(app, plan, name, publish, command, env=()):
    """The podman command line a shelter unit runs ($S, $U, $G, $I and $K are the start step's shell variables).
    One SELinux level per shelter, s0:c<K>,c1023, for all its containers: :Z relabels a volume for the container
    that mounts it, and with podman's random level per container a cron container's start relabels the data
    directory away from the running shelter (SPEC "How it runs on the host"). Its socket proxies run at the same level."""
    vols = [f"-v {plan['code']}:{plan['code']}:ro,idmap"] + [f"-v {x}:{x}:Z,idmap" for x in plan["data"]] + \
        [f"-v /run/wend/shelter/{app}/s{i}:{os.path.dirname(x)}:Z" for i, x in enumerate(plan.get("sockets", []))]
    # 0.0.0.0 is published; a program listening on 127.0.0.1 listens on the shelter's own loopback, which podman's
    # forwarding never reaches, so start_units serves that one with a proxy into the shelter's namespace instead
    ports = [f"-p {p}:{p.rsplit(':', 1)[1]}" for p in plan.get("ports", []) if p.startswith("0.0.0.0:")] if publish else []
    low = publish and any(int(p.rsplit(":", 1)[1]) < 1024 for p in plan.get("ports", []))
    return " ".join(["/usr/bin/podman run --rm", f"--name {name}", "--read-only --tmpfs /tmp --tmpfs /run",
                     f"--uidmap 0:$S:{SUBID_SIZE} --gidmap 0:$S:{SUBID_SIZE}", "--user $U:$G", "--cap-drop=all"] +
                    (["--cap-add=NET_BIND_SERVICE"] if low else []) +
                    ["--security-opt no-new-privileges", "--security-opt label=level:s0:c$K,c1023",
                     f"--network wend-{app}"] + ports + vols +
                    [f"--env {e}" for e in env] + [image_ref(app), command])


def image_ref(app):
    """The shelter's image in podman's store: its root, imported under the lock's digest ($I) as the tag."""
    return f"localhost/wend-shelter-{app}:$I"


def import_pre(app):
    """ExecStartPre= that puts the built root into podman's store once, so the unit runs a real image by the lock's
    digest (podman, not the shelter root, then owns the files it maps into the shelter's id range)."""
    return (f"ExecStartPre=/bin/sh -c '/usr/bin/podman image exists {image_ref(app)} || "
            f"tar -C {SHELTER}/{app}/root -c . | /usr/bin/podman import - {image_ref(app)}'\n")


def start_units(app, plan, release):
    """shelter.start's units, without installing podman (prove runs this part in a copy of the box): the subordinate
    id range, the egress table and its unit, the socket proxies, the shelter's unit, a timer per cron line; enabled."""
    d, unit, net = f"{SHELTER}/{app}", f"wend-shelter-{app}", f"wend-shelter-net-{app}"
    hdr = HEADER.format(app=app, release=release)
    socks = plan.get("sockets", [])
    sock_units = [f"{unit}-sock{i}.socket" for i in range(len(socks))]
    after = " ".join([f"{net}.service"] + sock_units)
    # the owner's opt-ins, one TCP destination each (the host's own addresses arrive on input, the rest on forward)
    egress = "".join(f"    ip saddr 10.90.$K.0/24 ip daddr {e.rsplit(':', 1)[0]} tcp dport {e.rsplit(':', 1)[1]} accept\n"
                     for e in plan.get("egress", []))
    parts = [
        "set -e",
        f"I=$(sed -n 's/^image = \"sha256:\\([0-9a-f]\\{{64\\}}\\)\"$/\\1/p' {d}/shelter.lock); "
        f"[ -n \"$I\" ] || {{ echo 'no image in {d}/shelter.lock'; exit 1; }}",
        f"X=$(for d in /etc/systemd/system /usr/lib/systemd/system; do [ -f $d/{plan['unit']} ] && "
        f"sed -n 's/^ExecStart=//p' $d/{plan['unit']} && break; done | head -n1)",
        "case \"$X\" in ''|*[!A-Za-z0-9_./=:@,+\\ -]*) "
        f"echo \"refusing: {plan['unit']}'s ExecStart is not a plain command line: $X\"; exit 1;; esac",
        f"U=$(id -u {plan['user']}); G=$(id -g {plan['user']})",
        # SPEC "How it runs on the host": a fixed 65536-id range under the name wend-<app>, the first free slot
        "touch /etc/subuid /etc/subgid",
        f"S=$(awk -F: '$1 == \"wend-{app}\" {{print $2; exit}}' /etc/subuid)",
        f"if [ -z \"$S\" ]; then S=$(cat /etc/subuid /etc/subgid | awk -F: -v b={SUBID_BASE} -v n={SUBID_SIZE} "
        "'NF >= 3 {s[NR] = $2; e[NR] = $2 + $3} END {for (k = 0; k < 256; k++) {a = b + k * n; ok = 1; "
        "for (i in s) if (a < e[i] && s[i] < a + n) ok = 0; if (ok) {print a; exit}}}'); "
        f"[ -n \"$S\" ] || {{ echo 'no free subordinate id range'; exit 1; }}; "
        f"echo \"wend-{app}:$S:{SUBID_SIZE}\" >> /etc/subuid; echo \"wend-{app}:$S:{SUBID_SIZE}\" >> /etc/subgid; fi",
        f"K=$(( ($S - {SUBID_BASE}) / {SUBID_SIZE} )); MU=$(( $S + $U )); MG=$(( $S + $G ))",
        # the mapped ids get a host user and group, wend-<app>: systemd won't take a SocketUser= with no passwd
        # entry (252 on EL9: status=217/USER), and ps and ls name the shelter instead of a bare number. -l: no
        # lastlog/faillog record, which at a uid past 2^30 makes /var/log/lastlog a ~300 GB sparse file that any
        # copy without --sparse (wend's own snapshots among them) fills the disk with
        f"if getent group wend-{app} >/dev/null; then [ \"$(getent group wend-{app} | cut -d: -f3)\" = $MG ] || "
        f"{{ echo 'group wend-{app} exists with another gid'; exit 1; }}; "
        f"elif getent group $MG >/dev/null; then echo \"gid $MG is taken by $(getent group $MG | cut -d: -f1)\"; exit 1; "
        f"else groupadd -r -g $MG wend-{app}; fi",
        f"if getent passwd wend-{app} >/dev/null; then [ \"$(id -u wend-{app})\" = $MU ] || "
        f"{{ echo 'user wend-{app} exists with another uid'; exit 1; }}; "
        f"elif getent passwd $MU >/dev/null; then echo \"uid $MU is taken by $(getent passwd $MU | cut -d: -f1)\"; exit 1; "
        f"else useradd -r -l -u $MU -g wend-{app} -M -d / -s /sbin/nologin -c 'wend shelter {app}' wend-{app}; fi",
        f"mkdir -p /etc/wend/shelter /etc/tmpfiles.d /run/wend/shelter/{app}",
    ]
    if socks or any(x.startswith("127.0.0.1:") for x in plan.get("ports", [])):
        # the proxies start from a script of wend's that execs the stock systemd-socket-proxyd. Started directly,
        # systemd runs that as systemd_socket_proxyd_t, a domain that may not accept a socket systemd made for
        # another domain, read /proc/<pid> to join the shelter's namespace, or connect to the app's database. The
        # script is bin_t, which systemd starts unconfined (unconfined_service_t), and an unconfined domain execs
        # the proxy without a transition: the host side of the bridge, run by root's own systemd. The app's
        # confinement is the container's process, which this doesn't change (SPEC "How it runs on the host"). A
        # script, not a copy of the binary: a copy would keep the old libsystemd-shared past the next hop's upgrade
        parts += ["mkdir -p /usr/libexec/wend",
                  f"printf '#!/bin/sh\\n# Written by wend (shelter.start). SPEC \"How it runs on the host\".\\n"
                  f"exec {STOCK_PROXYD} \"$@\"\\n' > {PROXYD}",
                  f"chmod 0755 {PROXYD}",
                  "if command -v selinuxenabled >/dev/null && selinuxenabled; then restorecon -R /usr/libexec/wend; fi"]
    if socks:
        # container_t may connect to an unconfined service's socket only by this rule; the socket systemd made for
        # the proxy carries the proxy's context. The constraint holds it to the same level on both ends: the socket
        # proxy runs at its shelter's level (below), every other unconfined service at s0, and every other container
        # at a level of its own, so the rule reaches one shelter's proxies from that shelter and nothing else on the
        # host. Loaded where SELinux is on (the chroot tier has none)
        parts += [_heredoc(f"/etc/wend/shelter/{CIL}.cil",
                           ";; Written by wend (shelter.start): a shelter reaches a host socket through wend's proxy,\n"
                           ";; which runs unconfined at the shelter's own level. SPEC \"How it runs on the host\".\n"
                           "(allow container_t unconfined_service_t (unix_stream_socket (connectto)))\n"
                           "(mlsconstrain (unix_stream_socket (connectto))\n"
                           "    (or (neq t1 container_t) (or (neq t2 unconfined_service_t) (and (eq l1 l2) (eq h1 h2)))))\n"),
                  "if command -v selinuxenabled >/dev/null && selinuxenabled; then "
                  f"semodule -i /etc/wend/shelter/{CIL}.cil; fi"]
    parts += [
        _heredoc(f"/etc/wend/shelter/{app}.nft",
                 hdr + f"# Egress for the shelter's network (10.90.$K.0/24): replies, DNS to its gateway, the plan's egress.\n"
                 f"table inet wend_shelter_{app}\ndelete table inet wend_shelter_{app}\n"
                 f"table inet wend_shelter_{app} {{\n"
                 "  chain forward {\n    type filter hook forward priority -10; policy accept;\n"
                 "    ip saddr 10.90.$K.0/24 ct state established,related accept\n" + egress +
                 "    ip saddr 10.90.$K.0/24 drop\n  }\n"
                 "  chain input {\n    type filter hook input priority -10; policy accept;\n"
                 "    ip saddr 10.90.$K.0/24 ct state established,related accept\n"
                 "    ip saddr 10.90.$K.0/24 ip daddr 10.90.$K.1 udp dport 53 accept\n" + egress +
                 "    ip saddr 10.90.$K.0/24 drop\n  }\n}\n"),
        _heredoc(f"/etc/systemd/system/{net}.service",
                 hdr + f"[Unit]\nDescription=wend shelter {app}: its egress table\n"
                 "After=nftables.service firewalld.service\nPartOf=nftables.service\nReloadPropagatedFrom=nftables.service\n\n"
                 "[Service]\nType=oneshot\nRemainAfterExit=yes\n"
                 f"ExecStart=/usr/sbin/nft -f /etc/wend/shelter/{app}.nft\nExecReload=/usr/sbin/nft -f /etc/wend/shelter/{app}.nft\n"
                 f"ExecStop=/usr/sbin/nft delete table inet wend_shelter_{app}\n\n[Install]\nWantedBy=multi-user.target\n"),
        _heredoc(f"/etc/tmpfiles.d/wend-shelter-{app}.conf",
                 hdr + f"d /run/wend/shelter/{app} 0700 $MU $MG -\n" +
                 "".join(f"d /run/wend/shelter/{app}/s{i} 0700 $MU $MG -\n" for i in range(len(socks)))),
    ]
    for i, s in enumerate(socks):
        parts += [
            # the shelter's :Z relabels this directory and the socket in it container_file_t, which PID 1 (init_t)
            # may not unlink when the socket restarts ("Address already in use", found under enforcing on the VM
            # tier). So it starts from a fresh directory: the old one is removed by a command (a bin_t program, which
            # systemd runs unconfined, not as init_t), and systemd binds in a new one with /run's own label. The
            # shelter Requires= this socket, so it restarts with it and mounts the new directory.
            _heredoc(f"/etc/systemd/system/{unit}-sock{i}.socket",
                     hdr + f"[Unit]\nDescription=wend shelter {app}: {s}, for the shelter\nPartOf={unit}.service\n\n"
                     f"[Socket]\nExecStartPre=/usr/bin/rm -rf /run/wend/shelter/{app}/s{i}\n"
                     f"ExecStartPre=/usr/bin/install -d -m 0700 -o wend-{app} -g wend-{app} /run/wend/shelter/{app}/s{i}\n"
                     f"ListenStream=/run/wend/shelter/{app}/s{i}/{os.path.basename(s)}\nSocketUser=wend-{app}\n"
                     "SocketMode=0600\n\n[Install]\nWantedBy=sockets.target\n"),
            _heredoc(f"/etc/systemd/system/{unit}-sock{i}.service",
                     hdr + f"[Unit]\nDescription=wend shelter {app}: proxy to {s}\n"
                     f"Requires={unit}-sock{i}.socket\nAfter={unit}-sock{i}.socket\n\n"
                     f"[Service]\nUser={plan['user']}\n"
                     # the socket's label too (systemd labels it from SELinuxContext=); ignored without SELinux
                     "SELinuxContext=system_u:system_r:unconfined_service_t:s0:c$K,c1023\n"
                     f"ExecStart={PROXYD} {s}\n")]
    port_units = []
    for i, p in enumerate(x for x in plan.get("ports", []) if x.startswith("127.0.0.1:")):
        # outside the wend-shelter-<app>-* names, which the chroot tier's checks start themselves; there the program
        # runs in the host's own namespace and needs no proxy
        name = f"wend-shelter-port-{app}-{i}"
        port_units.append(f"{name}.socket")
        parts += [
            _heredoc(f"/etc/systemd/system/{name}.socket",
                     hdr + f"[Unit]\nDescription=wend shelter {app}: {p}, into the shelter's own loopback\n"
                     f"PartOf={unit}.service\n\n[Socket]\nListenStream={p}\n\n[Install]\nWantedBy=sockets.target\n"),
            # no start limit: a connection that comes while the container is still being created waits in the
            # script (below) instead of failing the service until the socket gives up
            _heredoc(f"/etc/systemd/system/{name}.service",
                     hdr + f"[Unit]\nDescription=wend shelter {app}: proxy to {p} in the shelter\n"
                     f"Requires={name}.socket {unit}.service\nAfter={name}.socket {unit}.service\n"
                     f"BindsTo={unit}.service\nStartLimitIntervalSec=0\n\n[Service]\n"
                     f"ExecStart={PORT_PROXY}-{app} {p}\n")]
    if port_units:
        # the proxy waits for the shelter's process, then joins its network namespace only (as nobody) and connects
        # to the shelter's own loopback; exec keeps the pid systemd handed the socket to (a quoted heredoc: nothing
        # in it expands when it's written)
        # an executable in /usr/libexec (bin_t), not `sh <script>`: systemd runs it, and the proxy it execs,
        # unconfined, like the socket proxies above
        parts.append(f"rm -f /etc/wend/shelter/{app}.port-proxy; cat > {PORT_PROXY}-{app} <<'WEND_SCRIPT'\n#!/bin/sh\n" +
                     hdr +
                     "i=0\n"
                     "while :; do\n"
                     f"  P=$(/usr/bin/podman inspect -f '{{{{.State.Pid}}}}' wend-{app} 2>/dev/null)\n"
                     "  [ -n \"$P\" ] && [ \"$P\" -gt 0 ] 2>/dev/null && break\n"
                     f"  i=$((i + 1)); [ $i -lt 120 ] || {{ echo 'wend-{app} has no running process'; exit 1; }}\n"
                     "  sleep 1\n"
                     "done\n"
                     f"exec /usr/bin/nsenter --net=/proc/$P/ns/net -S 65534 -G 65534 {STOCK_PROXYD} \"$1\"\n"
                     f"WEND_SCRIPT\nchmod 0755 {PORT_PROXY}-{app}\n"
                     "if command -v selinuxenabled >/dev/null && selinuxenabled; then restorecon /usr/libexec/wend/*; fi")
    parts.append(_heredoc(
        f"/etc/systemd/system/{unit}.service",
        hdr + f"[Unit]\nDescription=wend shelter {app} ({release} userland, unpatched)\n"
        f"Wants=network-online.target\nAfter=network-online.target {after}\n"
        f"BindsTo={net}.service\nPartOf={net}.service\nReloadPropagatedFrom={net}.service\n" +
        "".join(f"Requires={s}\n" for s in sock_units) +
        # the first start imports the image: more than systemd's default 90 seconds on a slow disk
        f"\n[Service]\nTimeoutStartSec=15min\nExecStartPre=-/usr/bin/podman network create --subnet 10.90.$K.0/24 wend-{app}\n" +
        import_pre(app) +
        f"ExecStart={podman_run(app, plan, f'wend-{app}', True, '$X')}\n"
        # nftables' reload flushes the whole ruleset, netavark's port forwarding with it: put it back
        f"ExecReload=/usr/bin/podman network reload wend-{app}\n"
        f"ExecStop=/usr/bin/podman stop -t 10 wend-{app}\nRestart=on-failure\n\n[Install]\nWantedBy=multi-user.target\n"))
    timers = []
    for i, c in enumerate(plan.get("cron", [])):
        cal, cmd = cron_calendar(c["line"])
        name = f"{unit}-cron{i}"
        timers.append(f"{name}.timer")
        parts += [
            _heredoc(f"/etc/systemd/system/{name}.timer",
                     hdr + f"# was {c['user']}'s crontab line: {c['line']}\n[Unit]\nDescription=wend shelter {app}: "
                     f"cron line {i}\n\n[Timer]\n" + "".join(f"OnCalendar={x}\n" for x in cal) +
                     "\n[Install]\nWantedBy=timers.target\n"),
            _heredoc(f"/etc/systemd/system/{name}.service",
                     hdr + f"[Unit]\nDescription=wend shelter {app}: cron line {i}\nAfter={after}\n"
                     f"BindsTo={net}.service\n" + "".join(f"Requires={s}\n" for s in sock_units) +
                     "\n[Service]\nType=oneshot\nTimeoutStartSec=15min\n" + import_pre(app) +
                     f"ExecStart={podman_run(app, plan, f'wend-{app}-cron{i}', False, cmd, ['PATH=/usr/bin:/bin'])}\n")]
    parts.append(f"systemctl enable {net}.service {unit}.service {' '.join(sock_units + port_units + timers)}")
    parts.append(f"mkdir -p /var/lib/wend && printf '{''.join(x + chr(92) + 'n' for x in ADDS)}' >> {ADDED}")
    return "\n".join(parts)


def start(app, plan, adapter, hop, release):
    """shelter.start, on the drop hop's post phase: podman (and nftables) from the landing release by the adapter's
    install op, then the units. `release` is the shelter's own (keeps_runtimes_on)."""
    install = backends.of(adapter).step({"op": "install", "packages": PODMAN}, adapter, hop)
    return [install, start_units(app, plan, release)]


# ---------- the podman stand-in, while checks run ----------

class standin:
    """Put tools/podman_standin.sh at /usr/bin/podman in a box for the length of a `with`, then put back what was
    there (the real podman, or nothing). Never left in the box."""

    def __init__(self, box):
        self.root = os.path.realpath(box.root)

    def __enter__(self):
        b = f"{self.root}/usr/bin"
        if not backends.plain_dir(self.root, b):
            raise RuntimeError(f"{b} is not a plain directory")
        self.real = os.path.lexists(f"{b}/podman")
        if self.real:
            os.rename(f"{b}/podman", f"{self.root}{STANDIN_REAL}")
        with open(STANDIN, "rb") as src, open(f"{b}/podman", "wb") as dst:
            dst.write(src.read())
        os.chmod(f"{b}/podman", 0o755)
        return self

    def __exit__(self, *exc):
        b = f"{self.root}/usr/bin"
        os.remove(f"{b}/podman")
        if self.real:
            os.rename(f"{self.root}{STANDIN_REAL}", f"{b}/podman")


def has_shelter(box, plan):
    """The box has a built shelter for an app of the plan (so its checks need the stand-in)."""
    r = os.path.realpath(box.root)
    return any(os.path.isfile(f"{r}{SHELTER}/{a}/shelter.lock") for a in plan)


# ---------- shelter.prove ----------

def prove(box, app, plan, release, checks, isolate, release_file, passing, scratch, log):
    """shelter.prove: in a copy of the box (so the real data is never written), the native app stopped and the
    shelter's units in its place, run the box's checks with the stand-in. Returns the health; Refused if a check the
    box passes natively (`passing`) fails sheltered."""
    log(f"shelter {app}: proving it in a copy of the box")
    s = box.copy_to(scratch)
    try:
        for c in stop_native(app, plan) + [start_units(app, plan, release)]:
            rc, out = s.run(c)
            if rc:
                raise Refused(f"setting the shelter up in the copy failed: {tail(out, 800)}")
        with standin(s):
            h = s.health(checks, release_file, isolate)
    finally:
        safe_rm(scratch)
    failed = sorted(set(checks) & set(passing) - set(h["passed"]))
    if failed:
        raise Refused(f"the app's checks fail sheltered: {', '.join(failed)}\n" +
                      "\n".join(tail(h["checks"][c]["log"], 800) for c in failed))
    return h


# ---------- the VM tier (tools/vm.py): confinement, on a real machine ----------

def _native_interp(plan):
    """Shell that sets PY to the interpreter of the app's own unit (left on disk, disabled, by stop-native)."""
    return (f"X=$(for d in /etc/systemd/system /usr/lib/systemd/system; do [ -f $d/{plan['unit']} ] && "
            f"sed -n 's/^ExecStart=//p' $d/{plan['unit']} && break; done | head -n1); PY=${{X%% *}}")


def vm_probe(app, plan, outside):
    """The VM tier's confinement probe (SPEC "Sheltering an app", Proof) for a started shelter, run in the guest after
    every reboot from the drop hop on: the units, the native app off, the id range, the image by the lock's digest,
    the program's host ids, capabilities, no_new_privs and SELinux domain, the read-only root and code tree, the data
    directory written as the app's own host uid (idmap), ports on their address only, egress refused, and the egress
    table and the published ports through an nftables reload and restart. `outside` is "address:port": a TCP
    listener the guest itself reaches and the plan has no egress opt-in for. Prints `ok`/`FAIL` lines and `fact`
    lines; exits 1 on any FAIL."""
    oh, op = outside.rsplit(":", 1)
    if outside in plan.get("egress", []) or not re.fullmatch(r"[0-9.]+", oh) or not op.isdigit():
        raise ValueError(f"{outside!r} can't stand for a destination the shelter must not reach")
    c, unit, net, user = f"wend-{app}", f"wend-shelter-{app}", f"wend-shelter-net-{app}", plan["user"]
    timers = [f"{unit}-cron{i}.timer" for i in range(len(plan.get("cron", [])))] + \
        [f"wend-shelter-port-{app}-{i}.socket" for i, x in enumerate(
            x for x in plan.get("ports", []) if x.startswith("127.0.0.1:"))]
    ports = [p.rsplit(":", 1) for p in plan.get("ports", [])]
    low = any(int(p) < 1024 for _, p in ports)
    up = ("up() { i=0; while [ $i -lt 60 ]; do systemctl is-active -q " + f"{unit}.service" + " && " +
          " && ".join([f"t {a} {p}" for a, p in ports] or ["true"]) + " && return 0; sleep 2; i=$((i+1)); done; "
          "return 1; }")
    lines = [
        "f=0; ok() { echo \"ok   $*\"; }; no() { echo \"FAIL $*\"; f=1; }; fact() { echo \"fact $1=$2\"; }",
        "t() { timeout 5 bash -c \"exec 3<>/dev/tcp/$1/$2\" >/dev/null 2>&1; }",
        up,
        "SE=$(getenforce 2>/dev/null || echo none); fact selinux \"$SE\"",
        f"i=0; while [ $i -lt 90 ] && ! systemctl is-active -q {unit}.service; do sleep 2; i=$((i+1)); done",
        f"for u in {net}.service {unit}.service {' '.join(timers)}; do "
        "if systemctl is-active -q $u && [ \"$(systemctl is-enabled $u 2>/dev/null)\" = enabled ]; then ok \"$u is enabled "
        "and active\"; else no \"$u is $(systemctl is-enabled $u 2>&1), $(systemctl is-active $u 2>&1)\"; fi; done",
        f"if [ \"$(systemctl is-enabled {plan['unit']} 2>/dev/null)\" != enabled ] && ! systemctl is-active -q "
        f"{plan['unit']}; then ok 'the native {plan['unit']} is off'; else no 'the native {plan['unit']} still runs'; fi",
    ] + [f"if crontab -l -u {cr['user']} 2>/dev/null | grep -Fqx '{cr['line']}'; then no 'the native cron line "
         f"{i} is still live'; else ok 'the native cron line {i} is commented out'; fi"
         for i, cr in enumerate(plan.get("cron", []))] + [
        f"R=$(awk -F: '$1 == \"wend-{app}\" {{print $2 \":\" $3}}' /etc/subuid); "
        f"RG=$(awk -F: '$1 == \"wend-{app}\" {{print $2 \":\" $3}}' /etc/subgid); fact subuid \"$R\"",
        f"if [ -n \"$R\" ] && [ \"$R\" = \"$RG\" ] && [ \"${{R#*:}}\" = {SUBID_SIZE} ] && "
        f"[ \"$(grep -c '^wend-{app}:' /etc/subuid)\" = 1 ]; then ok \"one id range, $R, the same for groups\"; "
        "else no \"the id range: subuid '$R', subgid '$RG'\"; fi",
        f"S=${{R%%:*}}; S=${{S:-0}}; U=$(id -u {user}); G=$(id -g {user})",
        f"I=$(sed -n 's/^image = \"sha256:\\([0-9a-f]\\{{64\\}}\\)\"$/\\1/p' {SHELTER}/{app}/shelter.lock)",
        f"fact lock \"$(sha256sum {SHELTER}/{app}/shelter.lock | cut -d' ' -f1)\"; fact image \"sha256:$I\"",
        f"if [ -n \"$I\" ] && podman image exists localhost/wend-shelter-{app}:$I && [ \"$(podman inspect -f "
        f"'{{{{.ImageName}}}}' {c} 2>/dev/null)\" = \"localhost/wend-shelter-{app}:$I\" ]; then ok \"{c} runs the image "
        "tagged with the lock's digest\"; else no \"" + c + " isn't running localhost/wend-shelter-" + app + ":$I\"; fi",
        f"P=$(podman inspect -f '{{{{.State.Pid}}}}' {c} 2>/dev/null); fact pid \"$P\"",
        "if [ -z \"$P\" ] || [ \"$P\" = 0 ] || [ ! -r /proc/$P/status ]; then no 'no running process to inspect'; P=1; fi",
        "IDS=$(awk '/^Uid:/ {print $2, $3, $4, $5}' /proc/$P/status); GIDS=$(awk '/^Gid:/ {print $2, $3, $4, $5}' "
        "/proc/$P/status); fact uids \"$IDS\"; fact gids \"$GIDS\"",
        "M=$((S + U)); MG=$((S + G)); if [ \"$IDS\" = \"$M $M $M $M\" ] && [ \"$GIDS\" = \"$MG $MG $MG $MG\" ]; then "
        "ok \"the program runs as host uid $M, gid $MG (the app's ids in the range)\"; else no \"the program's host ids "
        "are '$IDS' / '$GIDS', not $M / $MG\"; fi",
        f"if [ \"$(id -u wend-{app} 2>/dev/null)\" = \"$M\" ] && [ \"$(getent group wend-{app} | cut -d: -f3)\" = \"$MG\" ]; "
        f"then ok 'the host names those ids wend-{app}'; else no \"no host user and group wend-{app} at $M:$MG\"; fi",
        "CE=$(awk '/^CapEff:/ {print $2}' /proc/$P/status); CP=$(awk '/^CapPrm:/ {print $2}' /proc/$P/status); "
        "CB=$(awk '/^CapBnd:/ {print $2}' /proc/$P/status); fact caps \"eff=$CE prm=$CP bnd=$CB\"",
        # NET_BIND_SERVICE alone (bit 10) when a port is below 1024; nothing otherwise
        f"W={'0000000000000400' if low else '0000000000000000'}",
        "if { [ \"$CE\" = 0000000000000000 ] || [ \"$CE\" = $W ]; } && { [ \"$CP\" = 0000000000000000 ] || "
        "[ \"$CP\" = $W ]; } && { [ \"$CB\" = 0000000000000000 ] || [ \"$CB\" = $W ]; }; then ok 'no capabilities "
        "beyond the plan'; else no \"capabilities: eff $CE, prm $CP, bnd $CB\"; fi",
        "if grep -q '^NoNewPrivs:[[:space:]]*1$' /proc/$P/status; then ok no_new_privs; else no 'no_new_privs is off'; fi",
        "LAB=$(tr -d '\\0' < /proc/$P/attr/current 2>/dev/null); fact label \"$LAB\"",
        "case \"$SE\" in Enforcing|Permissive) case \"$LAB\" in *:container_t:*) ok \"SELinux domain $LAB\";; "
        "*) no \"the program's SELinux domain is '$LAB', not container_t\";; esac;; "
        "*) no \"SELinux is $SE: nothing to confine with\";; esac",
        _native_interp(plan),
        f"w() {{ podman exec {c} $PY -c \"open('$1', 'w').close()\" >/dev/null 2>&1; }}",
        "if w /wend-probe; then no 'the root is writable'; else ok 'the root is read-only'; fi",
        f"if w {plan['code']}/wend-probe; then no 'the code tree is writable'; rm -f {plan['code']}/wend-probe; "
        "else ok 'the code tree is read-only'; fi",
    ] + [
        f"if w {data}/wend-probe; then O=$(stat -c %u:%g {data}/wend-probe); fact data_owner \"$O\"; "
        f"if [ \"$O\" = \"$U:$G\" ]; then ok \"{data} is written as the app's own host ids (idmap)\"; else "
        f"no \"a file written in {data} is owned by $O on the host, not $U:$G\"; fi; rm -f {data}/wend-probe; "
        f"else no '{data} is not writable'; fi" for data in plan["data"]] + [
        "H=$(ip -4 -o addr show scope global | awk '{split($4, a, \"/\"); print a[1]; exit}'); fact address \"$H\"",
    ] + [f"if t {a} {p}; then ok 'published on {a}:{p}'; else no 'nothing answers on {a}:{p}'; fi" +
         (f"; if [ -n \"$H\" ] && t $H {p}; then no \"{p} answers on $H too\"; else ok 'only on {a}:{p}'; fi"
          if a.startswith("127.") else "") for a, p in ports] + [
        f"if t {oh} {op}; then ok 'the guest reaches {outside} (the control)'; else no \"the guest can't reach {outside}, "
        "so refusing it proves nothing\"; fi",
        f"eg() {{ podman exec {c} $PY -c \"import socket; socket.create_connection(('{oh}', {op}), 5)\" >/dev/null 2>&1; }}",
        f"if eg; then no 'the shelter reaches {outside}, which the plan never opted in to'; else ok 'egress to "
        f"{outside} refused'; fi",
        f"if nft list table inet wend_shelter_{app} >/dev/null 2>&1; then ok 'the egress table is loaded'; else "
        "no 'no egress table'; fi",
        # a marker table no unit puts back: gone afterwards means the reload (restart) really flushed the ruleset
        "systemctl start nftables.service || no 'nftables.service would not start'; "
        "nft add table inet wend_probe_marker; if systemctl reload nftables.service; then sleep 3; "
        "if nft list table inet wend_probe_marker >/dev/null 2>&1; then no 'the nftables reload flushed nothing (proves "
        "nothing)'; nft delete table inet wend_probe_marker; fi; "
        f"if nft list table inet wend_shelter_{app} >/dev/null 2>&1; then ok 'the egress table is back after an "
        "nftables reload'; else no 'an nftables reload dropped the egress table'; fi; "
        "if up; then ok 'the shelter answers after the reload'; else no 'the shelter stopped answering after an nftables "
        "reload'; fi; if eg; then no 'egress opened after the reload'; else ok 'egress still refused after the reload'; "
        "fi; else no 'nftables.service would not reload'; fi",
        "nft add table inet wend_probe_marker; "
        f"if systemctl restart nftables.service; then sleep 3; if nft list table inet wend_probe_marker >/dev/null 2>&1; "
        "then no 'the nftables restart flushed nothing (proves nothing)'; nft delete table inet wend_probe_marker; fi; "
        f"if nft list table inet wend_shelter_{app} >/dev/null 2>&1 "
        "&& up; then ok 'the table and the shelter are back after an nftables restart'; else no 'after an nftables "
        "restart: table or shelter missing'; fi; else no 'nftables.service would not restart'; "
        "nft delete table inet wend_probe_marker 2>/dev/null; fi",
        "fact selinux_end \"$(getenforce 2>/dev/null || echo none)\"",
        "exit $f"]
    return "\n".join(lines) + "\n"


def vm_enforce_on(app):
    """SELinux enforcing for the length of a confinement window (the VM boxes install permissive, and the route itself
    isn't what's being proved): a guard puts permissive back after 15 minutes whatever happens, then the shelter is
    restarted under enforcing (restarting the table unit restarts it, PartOf=), then each socket proxy, which restarts
    it again (Requires=); on a failure, the units' status. The policy is rebuilt without its dontaudit rules for the
    window (semodule -DB), so the denial list has every denial, not only those the policy chose to log; the guard and
    VM_ENFORCE_OFF rebuild it with them."""
    return ("case \"$(getenforce 2>/dev/null)\" in Enforcing|Permissive) ;; *) echo 'SELinux is disabled'; exit 1;; esac\n"
            "setsid sh -c 'sleep 900; setenforce 0; semodule -B' </dev/null >/dev/null 2>&1 & "
            "echo $! > /run/wend-enforce-guard\n"
            "date +%s > /run/wend-enforce-since\n"
            "if [ -f /var/log/audit/audit.log ]; then wc -c < /var/log/audit/audit.log; else echo 0; fi > /run/wend-avc-from\n"
            "semodule -DB || { echo 'semodule -DB failed'; exit 1; }\n"
            "setenforce 1 && [ \"$(getenforce)\" = Enforcing ] || { echo 'setenforce 1 failed'; exit 1; }\n"
            f"up() {{ i=0; while [ $i -lt 90 ] && ! systemctl is-active -q wend-shelter-{app}.service; do sleep 2; i=$((i+1)); "
            f"done; systemctl is-active -q wend-shelter-{app}.service && return 0; echo \"the shelter isn't up after $1\"; "
            f"systemctl --no-pager -l status 'wend-shelter-{app}*' 'wend-shelter-net-{app}*' 2>&1 | tail -n 40; return 1; }}\n"
            f"systemctl restart wend-shelter-net-{app}.service; up 'restarting its table unit' || exit 1\n"
            # the socket proxies again, now that the shelter has run and relabelled their directories
            f"for u in $(systemctl list-units --plain --no-legend 'wend-shelter-{app}-sock*.socket' | awk '{{print $1}}'); do "
            "systemctl restart $u; up \"restarting $u\" || exit 1; done\n"
            f"echo active\n")


VM_ENFORCE_OFF = (
    "setenforce 0; kill $(cat /run/wend-enforce-guard 2>/dev/null) 2>/dev/null; echo \"selinux now $(getenforce)\"\n"
    "semodule -B || echo 'semodule -B failed: dontaudit rules still off'\n"
    "if [ -f /var/log/audit/audit.log ]; then tail -c +$(( $(cat /run/wend-avc-from) + 1 )) /var/log/audit/audit.log; "
    "else journalctl -k --since @$(cat /run/wend-enforce-since) 2>/dev/null; fi | grep 'avc: .*denied' | tail -n 400\n"
    "rm -f /run/wend-enforce-guard /run/wend-enforce-since /run/wend-avc-from; true\n")


def vm_denials(app, out):
    """The AVC denials of an enforcing window that touch the shelter: its domain, its files, its units, its proxies
    (wend's scripts, the systemd-socket-proxyd they exec, nsenter, the stock proxy's domain). Not every socket's or
    unconfined service's: with dontaudit off, the rest of the host's denials say nothing about the shelter."""
    return [x for x in out.splitlines() if "avc:" in x and
            re.search(rf"container_t|container_file_t|wend-shelter-{app}|wend-{app}|/run/wend/|/usr/libexec/wend/|"
                      r"systemd_socket_proxyd|comm=\"systemd-socket-|comm=\"shelter-port-pr|comm=\"nsenter\"", x)]
