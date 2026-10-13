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
}.items():
    a = copy.deepcopy(adapters["el"])
    mutate(a)
    errs = check.check_adapter(a, "adapters/el.toml")
    print(("refused " if errs else "MISSED  ") + name + (f"  ({errs[0].split(': ', 1)[1]})" if errs else ""))
    bad += not errs
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
                        True)):
    errs = check.check_box({**web, "vm": vm}, adapters, "boxes/el/centos5-web/box.toml")
    print(("refused " if errs else "passed  ") + name if bool(errs) == want else f"WRONG   {name} {errs}")
    bad += bool(errs) != want
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
sys.exit(1 if bad else 0)
