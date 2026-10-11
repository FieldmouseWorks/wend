"""Schema checker for the wend repo: the first layer of the PR gate.

  python3 tools/check.py                 check every fix, route, box and run in the repo
  python3 tools/check.py --ids           also print each fix's and route's content id
  python3 tools/check.py --repin         after editing fixes, point every route at their new ids (matched by name)
  python3 tools/check.py --compile FILE  print the shell commands a fix compiles to on its family's adapter

Standard library only (Python 3.11+ for tomllib). Exit status 1 on any error. The rules live in SPEC.md; this file
is the executable copy of them, and when the two disagree this file is the bug.
"""
import fnmatch
import glob
import hashlib
import json
import os
import re
import sys
import tomllib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backends  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PKG = r"[a-z0-9][a-z0-9.+-]*"
ETC_PATH = r"/etc/[A-Za-z0-9_./-]+"
ETC_GLOB = r"/etc/[A-Za-z0-9_./*-]+"
CORE_OPS = {"install", "remove", "repair", "force-remove", "write-file", "pm-option"}
FORCE = ["depends", "remove-essential"]  # Debian's; an adapter can name its own (`force = [...]`)


def canonical(obj):
    """Canonical JSON: sorted keys, no whitespace, UTF-8. TOML is the authoring format; ids hash this."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def content_id(doc):
    """A fix's or route's id covers what it does, never what it is called: [meta] is left out."""
    return "sha256:" + hashlib.sha256(canonical({k: v for k, v in doc.items() if k != "meta"})).hexdigest()


def load_adapters():
    return {os.path.basename(p)[:-5]: tomllib.load(open(p, "rb")) for p in glob.glob(f"{ROOT}/adapters/*.toml")}


# ---------- validation ----------

def check_when(w, adapter, where, errs):
    if not isinstance(w, dict) or len(w) != 1:
        return errs.append(f"{where}: a predicate is a table with exactly one key, got {w!r}")
    (k, v), = w.items()
    if k in ("all", "any"):
        if not isinstance(v, list) or not v:
            return errs.append(f"{where}: '{k}' needs a non-empty list")
        for i, x in enumerate(v):
            check_when(x, adapter, f"{where}.{k}[{i}]", errs)
    elif k == "not":
        check_when(v, adapter, f"{where}.not", errs)
    elif k == "installed":
        if not re.fullmatch(PKG, str(v)):
            errs.append(f"{where}: bad package name {v!r}")
    elif k == "file":
        if not re.fullmatch(ETC_PATH, str(v)) or ".." in v:
            errs.append(f"{where}: 'file' must be a path under /etc, got {v!r}")
    elif k == "file-contains":
        if set(v) != {"path", "text"} or not re.fullmatch(ETC_PATH, v["path"]) or "'" in v["text"]:
            errs.append(f"{where}: 'file-contains' needs path (under /etc) and text (no single quotes)")
    else:
        errs.append(f"{where}: unknown predicate {k!r}")


def installed_names(w):
    """Packages the precondition requires to be installed, positively (not under a 'not')."""
    k, v = next(iter(w.items()))
    if k == "installed":
        return {v}
    if k == "all":
        return set().union(*map(installed_names, v))
    if k == "any":
        sets = [installed_names(x) for x in v]
        return set.intersection(*sets) if all(sets) else set()
    return set()


def writable(path, adapter, allow="writable"):
    """On one of the adapter's allowlists: matches one of its globs, one directory level only. A glob in `path` (the
    paths of rpm.adopt-rpmnew) must name a subset of an allowed glob, so '*' only matches '*' there."""
    return not any(path.startswith(p) for p in adapter.get("harness_owned", [])) and any(
        fnmatch.fnmatchcase(path, g) and "/" not in path[len(g.rsplit("/", 1)[0]) + 1:]
        for g in adapter.get(allow, []))


def check_arg(name, spec, val, adapter, where, errs):
    """An adapter op's argument, by its spec: enum, pattern, list (of a pattern), path or paths (on one of the
    adapter's allowlists, named by the spec), table (string keys and values, each matching a pattern)."""
    if "enum" in spec and val not in spec["enum"]:
        errs.append(f"{where}: {name} must be one of {spec['enum']}")
    if "pattern" in spec and not (isinstance(val, str) and re.fullmatch(spec["pattern"], val)):
        errs.append(f"{where}: {name}={val!r} does not match {spec['pattern']}")
    if "list" in spec and not (isinstance(val, list) and len(val) >= spec.get("min", 1) and
                               all(isinstance(x, str) and re.fullmatch(spec["list"], x) for x in val)):
        errs.append(f"{where}: {name} must be a list of at least {spec.get('min', 1)} strings matching {spec['list']}")
    if "path" in spec and not (isinstance(val, str) and re.fullmatch(ETC_PATH, val) and ".." not in val and
                               writable(val, adapter, spec["path"])):
        errs.append(f"{where}: {name}={val!r} is not on the {adapter['family']} adapter's {spec['path']} list")
    if "paths" in spec and not (isinstance(val, list) and val and all(
            isinstance(x, str) and re.fullmatch(ETC_GLOB, x) and ".." not in x and writable(x, adapter, spec["paths"])
            for x in val)):
        errs.append(f"{where}: {name} must be a list of paths on the {adapter['family']} adapter's {spec['paths']} list")
    if "table" in spec and not (isinstance(val, dict) and val and all(
            re.fullmatch(r"[A-Za-z0-9_]+", k) and isinstance(v, str) and re.fullmatch(spec["table"], v)
            for k, v in val.items())):
        errs.append(f"{where}: {name} must be a table of key = \"value\" with values matching {spec['table']}")


def check_step(s, adapter, where, errs):
    op = s.get("op")
    adapter_ops = adapter.get("ops", {})
    if op not in CORE_OPS and op not in adapter_ops:
        return errs.append(f"{where}: op {op!r} is not in the core vocabulary or the {adapter['family']} adapter")
    want = {"install": {"packages"}, "remove": {"packages", "purge"}, "repair": set(),
            "force-remove": {"packages", "force", "purge"}, "write-file": {"path", "content"},
            "pm-option": {"option"}}.get(op)
    if want is None:
        want = set(adapter_ops[op]["args"])
    got = set(s) - {"op"}
    if got != want:
        return errs.append(f"{where}: {op} takes exactly {sorted(want)}, got {sorted(got)}")
    if "packages" in want:
        if not s["packages"] or not all(re.fullmatch(PKG, p) for p in s["packages"]):
            errs.append(f"{where}: packages must be a non-empty list of package names")
    force = adapter.get("force", FORCE)
    if op == "force-remove" and not (isinstance(s["force"], list) and set(s["force"]) <= set(force)):
        errs.append(f"{where}: force may only use {sorted(force)}")
    if op == "write-file":
        if not re.fullmatch(ETC_PATH, s["path"]) or ".." in s["path"]:
            errs.append(f"{where}: write-file only writes under /etc")
        if any(s["path"].startswith(p) for p in adapter.get("harness_owned", [])):
            errs.append(f"{where}: {s['path']} is owned by the harness")
        elif not writable(s["path"], adapter):
            errs.append(f"{where}: {s['path']} is not on the {adapter['family']} adapter's writable list")
        if "'" in s["content"] or "%" in s["content"]:
            errs.append(f"{where}: write-file content may not contain ' or %")
    if op == "pm-option" and s["option"] not in adapter.get("pm_options", {}):
        errs.append(f"{where}: unknown pm-option {s['option']!r}")
    for a, spec in adapter_ops.get(op, {}).get("args", {}).items():
        check_arg(a, spec, s[a], adapter, where, errs)


def check_fix(doc, adapters, where):
    errs = []
    if doc.get("schema") != "wend.fix/0":
        return [f"{where}: schema must be 'wend.fix/0'"]
    allowed = {"schema", "family", "hop", "when", "pre", "post", "restores", "adds", "meta"}
    if set(doc) - allowed:
        errs.append(f"{where}: unknown fields {sorted(set(doc) - allowed)}")
    a = adapters.get(doc.get("family"))
    if not a:
        return errs + [f"{where}: unknown family {doc.get('family')!r}"]
    if doc.get("hop") not in a["ladder"][1:]:
        errs.append(f"{where}: hop {doc.get('hop')!r} is not a release on the {a['family']} ladder")
    if "when" not in doc:
        errs.append(f"{where}: every fix needs a 'when'")
    else:
        check_when(doc["when"], a, f"{where}.when", errs)
    steps = doc.get("pre", []) + doc.get("post", [])
    if not steps:
        errs.append(f"{where}: a fix with no steps does nothing")
    for ph in ("pre", "post"):
        for i, s in enumerate(doc.get(ph, [])):
            check_step(s, a, f"{where}.{ph}[{i}]", errs)
    # The stray-MySQL rule: whatever a fix installs, it installs in place of something that is installed now.
    if any(s.get("op") == "install" or s.get("reinstall") for s in steps) and not errs and \
            not installed_names(doc["when"]):
        errs.append(f"{where}: a fix that installs packages must be gated on 'installed' of the package it replaces")
    checks = set(a.get("checks", {}))
    for c in doc.get("restores", []):
        if c not in checks and not re.fullmatch(r"box:[a-z0-9-]+", c):
            errs.append(f"{where}: restores {c!r}, which is neither an adapter check nor box:<role>")
    m = doc.get("meta", {})
    if not m.get("name") or not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", m["name"]):
        errs.append(f"{where}: meta.name must be a lowercase slug")
    if os.path.basename(where)[:-5] != m.get("name") or os.path.basename(os.path.dirname(where)) != doc.get("hop"):
        errs.append(f"{where}: file must live at fixes/<family>/<hop>/<meta.name>.toml")
    return errs


def check_route(doc, fixes_by_id, adapters, where):
    errs = []
    if doc.get("schema") != "wend.route/0":
        return [f"{where}: schema must be 'wend.route/0'"]
    a = adapters.get(doc.get("family"))
    if not a:
        return [f"{where}: unknown family"]
    lad = a["ladder"]
    if doc["from"] not in lad or doc["to"] not in lad or lad.index(doc["from"]) >= lad.index(doc["to"]):
        return [f"{where}: from/to must be releases on the ladder, in order"]
    hops = lad[lad.index(doc["from"]) + 1: lad.index(doc["to"]) + 1]
    # every release in between, in order. The adapter may let a route pass through a release in one hop
    # (`may_skip`): EL8 is only ever a bootstrap step between EL7 and EL9.
    skip, got = a.get("may_skip", []), doc.get("hops")
    if not isinstance(got, list) or not got or got[-1] != doc["to"] or \
            [h for h in hops if h in got] != got or any(h not in got and h not in skip for h in hops):
        errs.append(f"{where}: hops must be every release from {doc['from']} to {doc['to']}: {hops}" +
                    (f" (this adapter lets a route skip {skip})" if skip else ""))
        return errs
    for ref in doc.get("fixes", []):
        f = fixes_by_id.get(ref["id"])
        if not f:
            errs.append(f"{where}: no fix with id {ref['id']} (named {ref.get('name')}); edited fixes get new ids")
        elif f["meta"]["name"] != ref.get("name"):
            errs.append(f"{where}: {ref['id'][:19]} is {f['meta']['name']}, not {ref.get('name')}")
        elif f["hop"] not in got:
            errs.append(f"{where}: {ref['name']} applies on {f['hop']}, outside this route")
    return errs


def check_box(doc, adapters, where):
    errs = []
    if doc.get("schema") != "wend.box/0":
        return [f"{where}: schema must be 'wend.box/0'"]
    a = adapters.get(doc.get("family"))
    if not a or doc.get("release") not in a["ladder"]:
        errs.append(f"{where}: unknown family or release")
    for k in ("packages", "customize", "roles"):
        if k not in doc:
            errs.append(f"{where}: missing {k}")
    if not set(doc.get("isolate", [])) <= set(doc.get("roles", {})):
        errs.append(f"{where}: isolate names roles the box does not have")
    return errs


def check_run(doc, routes_by_id, boxes, where):
    errs = []
    if doc.get("schema") != "wend.run/0":
        return [f"{where}: schema must be 'wend.run/0'"]
    # A run of a route id that no longer exists is history (the route was edited since). It stays, unchecked.
    if doc.get("box") not in boxes:
        errs.append(f"{where}: run refers to unknown box {doc.get('box')}")
    if doc.get("result") not in ("pass", "fail"):
        errs.append(f"{where}: result must be pass or fail")
    return errs


def decoy_errors(runs, routes_by_id, fixes_by_id):
    """On a passing run of a route, each pinned fix fired on exactly the boxes it claims and stayed off the rest."""
    errs = []
    for p, run in runs.items():
        route = routes_by_id.get(run.get("route"))
        if not route or run.get("result") != "pass":
            continue
        fired = {i for h in run["hops"] for i in h["fired"]}
        for ref in route["fixes"]:
            f = fixes_by_id.get(ref["id"])
            if not f:
                continue
            claims = run["box"] in f["meta"].get("fires_on", [])
            if (ref["id"] in fired) != claims:
                errs.append(f"{os.path.relpath(p, ROOT)}: {ref['name']} " +
                            ("fired on a box it does not claim (decoy hit)" if not claims else "claims this box but did not fire"))
    return errs


# ---------- compiling: the backends in backends.py; the lab's ol.py/co.py are the references ----------

def compile_when(w, family="debian"):
    """ol.py's precondition language (for tools/port_oldlinux.py). The harness uses the adapter's [predicates]."""
    k, v = next(iter(w.items()))
    if k == "installed":
        return f"dpkg -s {v}"
    if k == "file":
        return f"test -f {v}"
    if k == "file-contains":
        return f"grep -q '{v['text']}' {v['path']}"
    if k == "not":
        return "! " + compile_when(v)
    raise ValueError(f"'{k}' has no single-command form in ol.py's precondition language")


def compile_step(s, adapter, release=None):
    """The shell one step runs on the hop that arrives at `release` (the apt backend doesn't need it)."""
    return backends.of(adapter).step(s, adapter, release)


def compile_fix(doc, adapter):
    out = {"pre": [compile_step(s, adapter, doc["hop"]) for s in doc.get("pre", [])],
           "post": [compile_step(s, adapter, doc["hop"]) for s in doc.get("post", [])]}
    if adapter.get("backend", "apt") != "apt":
        return {"when": backends.pred_sh(doc["when"], adapter), **out}
    return {"when": compile_when(doc["when"]), **out}


# ---------- main ----------

def load_all():
    adapters = load_adapters()
    fixes = {p: tomllib.load(open(p, "rb")) for p in sorted(glob.glob(f"{ROOT}/fixes/*/*/*.toml"))}
    routes = {p: tomllib.load(open(p, "rb")) for p in sorted(glob.glob(f"{ROOT}/routes/*/*.toml"))}
    boxes = {os.path.relpath(os.path.dirname(p), f"{ROOT}/boxes"): tomllib.load(open(p, "rb"))
             for p in sorted(glob.glob(f"{ROOT}/boxes/*/*/box.toml"))}
    runs = {p: json.load(open(p)) for p in sorted(glob.glob(f"{ROOT}/runs/**/*.json", recursive=True))}
    return adapters, fixes, routes, boxes, runs


def main(argv):
    adapters, fixes, routes, boxes, runs = load_all()
    if "--compile" in argv:
        p = os.path.abspath(argv[argv.index("--compile") + 1])
        print(json.dumps(compile_fix(fixes[p], adapters[fixes[p]["family"]]), indent=1))
        return 0
    if "--repin" in argv:
        now = {d["meta"]["name"]: (d["family"], content_id(d)) for d in fixes.values()}
        for p, d in routes.items():
            text = open(p).read()
            for ref in d["fixes"]:
                fam, new = now.get(ref["name"], (None, None))
                if new and fam == d["family"] and new != ref["id"]:
                    text = text.replace(f'id = "{ref["id"]}"', f'id = "{new}"')
                    print(f"{os.path.relpath(p, ROOT)}: {ref['name']} {ref['id'][:19]} -> {new[:19]}")
            open(p, "w").write(text)
        return 0
    errs = []
    by_id = {}
    for p, d in fixes.items():
        rel = os.path.relpath(p, ROOT)
        e = check_fix(d, adapters, rel)
        errs += e
        i = content_id(d)
        if i in by_id:
            errs.append(f"{rel}: same content as {by_id[i][0]}; one fix, one file")
        by_id[i] = (rel, d)
    fixes_by_id = {i: d for i, (_, d) in by_id.items()}
    routes_by_id = {}
    for p, d in routes.items():
        errs += check_route(d, fixes_by_id, adapters, os.path.relpath(p, ROOT))
        routes_by_id[content_id(d)] = d
    for b, d in boxes.items():
        errs += check_box(d, adapters, f"boxes/{b}/box.toml")
    for p, d in runs.items():
        errs += check_run(d, routes_by_id, boxes, os.path.relpath(p, ROOT))
    errs += decoy_errors(runs, routes_by_id, fixes_by_id)
    if "--ids" in argv:
        for i, (rel, _) in by_id.items():
            print(i[:19], rel)
        for p, d in routes.items():
            print(content_id(d)[:19], os.path.relpath(p, ROOT))
    for e in errs:
        print("ERROR", e)
    print(f"{len(fixes)} fixes, {len(routes)} routes, {len(boxes)} boxes, {len(runs)} runs: "
          + ("ok" if not errs else f"{len(errs)} errors"))
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
