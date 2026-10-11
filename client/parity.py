"""The Rust client and the Python engine must agree, byte for byte.

  cargo build --release --manifest-path client/Cargo.toml
  python3 -B client/parity.py client/target/release/wend

Compares, over the whole atlas: every fix's and route's content id; every fix compiled (its precondition as a test,
its pre and post steps); every hop of every route, with all its pinned fixes and with none (the commands and the
`after` cleanup); the prune step; one example of every op on every backend; every predicate shape; and the gate's
own broken cases (tools/test_check.py), which the Rust checker must refuse exactly where the Python one does.

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
import walk  # noqa: E402

adapters, fixes, routes, boxes, runs = check.load_all()
rel = lambda p: os.path.relpath(p, ROOT)  # noqa: E731

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
            cases.append({"kind": kind, "doc": copy.deepcopy(doc), "adapters": copy.deepcopy(ad), "where": wh,
                          "refused": bool(errs)})
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
        cases.append({"kind": "fix", "doc": d, "adapters": adapters, "where": rel(p), "refused": False})
    for fam, a in adapters.items():
        cases.append({"kind": "adapter", "doc": a, "adapters": {}, "where": f"adapters/{fam}.toml", "refused": False})
    return cases


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
    py = python_side(cases)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"steps": [{"family": fam, "release": r, "step": s} for fam, r, s in STEPS],
                   "preds": [{"family": fam, "when": w} for fam, w in PREDS],
                   "cases": [{k: c[k] for k in ("kind", "doc", "adapters", "where")} for c in cases]}, f)
    try:
        p = subprocess.run([os.path.abspath(binary), "parity", f.name], cwd=ROOT, capture_output=True, text=True)
    finally:
        os.unlink(f.name)
    if p.returncode:
        sys.exit(f"the client failed:\n{p.stderr}")
    rs = json.loads(p.stdout)
    rs["cases"] = [bool(e) for e in rs["cases"]]
    errs = diff(py, rs)
    for i, (c, a, b) in enumerate(zip(cases, py["cases"], rs["cases"])):
        if a != b:
            errs.append(f"case {i} ({c['kind']} {c['where']}): python {'refuses' if a else 'accepts'}, "
                        f"rust {'refuses' if b else 'accepts'}: {json.dumps(c['doc'])[:400]}")
    n_hops = sum(len(r["hops"]) for r in routes.values())
    print(f"compared {len(py['ids'])} ids, {len(fixes)} fixes, {n_hops} route hops (x2), {len(STEPS)} op examples, "
          f"{len(PREDS)} predicates, {len(cases)} checker cases "
          f"({sum(py['cases'])} refused): " + ("identical" if not errs else f"{len(errs)} differences"))
    for e in errs[:40]:
        print("DIFF", e)
    return 1 if errs else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1]))
