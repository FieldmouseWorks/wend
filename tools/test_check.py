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
# decoys: the MySQL fix firing on the web box must fail; the honest run must pass
import json  # noqa: E402
_, fixes_, routes_, _, runs_ = check.load_all()
route = next(iter(routes_.values()))
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
