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

PKG = r"[A-Za-z0-9][A-Za-z0-9._+-]*"  # rpm names have capitals (NetworkManager) and underscores
ETC_PATH = r"/etc/[A-Za-z0-9_./-]+"
MYSQL_KEY = r"[a-z][a-z0-9-]*"
FAMILY = r"[A-Za-z_+]+"  # a name family: no digits or dots, nothing from the first "-" on (SPEC "restores and adds")
MYSQL_VALUE = r"[A-Za-z0-9_./:,@+-]*"
ETC_GLOB = r"/etc/[A-Za-z0-9_./*-]+"
CORE_OPS = {"install", "remove", "repair", "force-remove", "write-file", "pm-option"}
# Characters no adapter argument pattern may accept: values land in root shell commands.
UNSAFE = ["%", "'", '"', " ", "\t", "\n", "$", "`", ";", "&", "|", "<", ">", "(", ")", "{", "}", "\\", "*", "?", "[", "~"]
# Whatever an adapter's patterns say, a value a fix passes to an adapter op is held to this too: no shell
# metacharacters, no printf %, and no leading - (it would read as an option).
SAFE_VALUE = r"[A-Za-z0-9_.:/+=@][A-Za-z0-9_.:/+=@-]*"
REPO_URL = r"[A-Za-z0-9.-]+(/[A-Za-z0-9._-]+)+"
ADAPTER_KEYS = {"schema", "family", "backend", "release_file", "ladder", "may_skip", "harness_owned", "writable",
                "managed", "force", "releases", "mirror", "hop", "predicates", "pm_options", "box", "ops", "checks",
                "kv", "hardware", "addons", "runtimes", "relocate"}
CPU_LEVELS = ("x86-64", "x86-64-v2", "x86-64-v3")  # SPEC "Hardware preflight"
HARDWARE_KEYS = {"cpu", "ram_mib", "disk_mib", "boot_mib", "drivers", "note"}
OLD_KERNELS = ("running", "autoremove")
RUNTIMES = ("python2",)  # SPEC "Runtime preflight"


def family(pkg):
    """A package's name family (SPEC "restores and adds"): digits and dots removed, nothing from the first "-" on."""
    return re.sub(r"[0-9.]", "", pkg).split("-")[0]


def installed_families(doc):
    """The families of the packages a fix's own steps install (install, or a transit's reinstall): what its adds may
    name. The checker and both hop compilers use this, so a direct `wend replay` holds a fix to the same rule."""
    steps = [s for ph in ("pre", "post") for s in (doc.get(ph) or []) if isinstance(s, dict)]
    return {family(p) for s in steps for p in (s.get("packages", []) if s.get("op") == "install" else
                                               s.get("reinstall") or []) if isinstance(p, str)}


def canonical(obj):
    """Canonical JSON: sorted keys, no whitespace, UTF-8. TOML is the authoring format; ids hash this."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def content_id(doc):
    """A fix's or route's id covers what it does, never what it is called: [meta] is left out."""
    return "sha256:" + hashlib.sha256(canonical({k: v for k, v in doc.items() if k != "meta"})).hexdigest()


def load_adapters():
    return {os.path.basename(p)[:-5]: tomllib.load(open(p, "rb")) for p in glob.glob(f"{ROOT}/adapters/*.toml")}


# ---------- validation ----------

def check_adapter(a, where):
    """An adapter can narrow what fixes may do, never widen it past what its backend implements (tools/backends.py
    holds the lists). CI also takes adapters from the base branch; this is the second lock."""
    errs = []
    if a.get("schema") != "wend.adapter/0":
        return [f"{where}: schema must be 'wend.adapter/0'"]
    if set(a) - ADAPTER_KEYS:
        errs.append(f"{where}: unknown keys {sorted(set(a) - ADAPTER_KEYS)}")
    for k in ("family", "backend", "release_file", "ladder", "force", "hop", "predicates", "checks"):
        if k not in a:
            errs.append(f"{where}: missing {k}")
    b = backends.BACKENDS.get(a.get("backend"))
    if not b:
        return errs + [f"{where}: backend must be one of {sorted(backends.BACKENDS)}"]
    if not set(a.get("force", [])) <= b.FORCE:
        errs.append(f"{where}: force may only name {sorted(b.FORCE)} on the {b.name} backend")
    if not set(a.get("pm_options", {})) <= b.PM_OPTIONS:
        errs.append(f"{where}: pm_options may only name {sorted(b.PM_OPTIONS)} on the {b.name} backend")
    if not set(a.get("ops", {})) <= b.OPS:
        errs.append(f"{where}: the {b.name} backend has no ops {sorted(set(a.get('ops', {})) - b.OPS)}")
    if not set(a.get("may_skip", [])) <= set(a.get("ladder", [])[1:-1]):
        errs.append(f"{where}: may_skip names releases that aren't inside the ladder")
    for op, d in a.get("ops", {}).items():
        for name, spec in d.get("args", {}).items():
            for kind in ("pattern", "list", "table"):
                if kind in spec and any(re.fullmatch(spec[kind], f"a{c}b") or re.fullmatch(spec[kind], c)
                                        for c in UNSAFE):
                    errs.append(f"{where}: ops.{op}.args.{name}: {spec[kind]!r} accepts shell metacharacters")
            if not any(k in spec for k in ("enum", "pattern", "list", "path", "paths", "table")):
                errs.append(f"{where}: ops.{op}.args.{name} has no spec")
            for k in ("path", "paths"):
                if k in spec and spec[k] not in ("writable", "managed", "kv"):
                    errs.append(f"{where}: ops.{op}.args.{name}: {k} must name writable, managed or kv")
    for p, d in a.get("kv", {}).items():
        if not re.fullmatch(ETC_PATH, p) or not d.get("keys") or \
                not all(re.fullmatch(r"[A-Za-z0-9_]+", k) for k in d["keys"]):
            errs.append(f"{where}: kv.{p!r} needs a path under /etc and plain keys")
    for r, rel in a.get("releases", {}).items():  # SPEC "Kernels and the bootloader"
        if isinstance(rel, dict) and rel.get("reboot", "before-post") != "before-post":
            errs.append(f"{where}: releases.{r}.reboot can only be \"before-post\"")
    errs += check_hardware(a, where)
    errs += check_runtimes(a, where)
    if not isinstance(a.get("addons", {}), dict):
        return errs + [f"{where}: addons must be a table"]
    for n, ad in a.get("addons", {}).items():  # SPEC "Add-on repositories": their values land in root shell commands
        rels = a.get("releases", {})
        if a.get("backend") != "rpm" or not re.fullmatch(r"[a-z0-9]+", n) or not isinstance(ad, dict) or \
                set(ad) != {"marker", "vendor", "repos"} or not isinstance(ad["marker"], str) or \
                not re.fullmatch(PKG, ad["marker"]) or not isinstance(ad["vendor"], str) or \
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .,-]*", ad["vendor"]) or not isinstance(ad["repos"], dict) or \
                not ad["repos"] or not all(r in rels and isinstance(u, str) and re.fullmatch(REPO_URL, u) and
                                           n not in rels[r].get("repos", {}) for r, u in ad["repos"].items()):
            errs.append(f"{where}: addons.{n} needs a plain name, a marker package, a plain vendor, and repos "
                        "(host/path) for releases the adapter has, on the rpm backend")
    if a.get("addons") and "{addons}" not in str(a.get("hop", {}).get("set_release", "")):
        errs.append(f"{where}: an adapter with addons must put {{addons}} in hop.set_release")
    for k in ("kernel_before", "kernel_after"):
        if k in a.get("hop", {}) and not (isinstance(a["hop"][k], list) and all(isinstance(c, str) for c in a["hop"][k])):
            errs.append(f"{where}: hop.{k} must be a list of commands")
    errs += check_relocate(a, where)
    return errs


# SPEC "Relocate": the harness's steps, in the order tools/vm.py runs them, then the commands it runs between boots
RELOCATE_STEPS = ("preflight", "refresh", "place", "copy", "fixup", "flip", "release", "cleanup")
RELOCATE_COMMANDS = ("booted", "arm", "commit_trial", "rollback_next")


def check_relocate(a, where):
    r = a.get("relocate")
    if r is None:
        return []
    if not isinstance(r, dict) or set(r) != set(RELOCATE_STEPS + RELOCATE_COMMANDS):
        return [f"{where}: relocate needs exactly {sorted(RELOCATE_STEPS + RELOCATE_COMMANDS)}"]
    errs = [f"{where}: relocate.{k} must be a list of commands" for k in RELOCATE_STEPS
            if not (isinstance(r[k], list) and r[k] and all(isinstance(c, str) for c in r[k]))]
    return errs + [f"{where}: relocate.{k} must be a command" for k in RELOCATE_COMMANDS if not isinstance(r[k], str)]


def booted_releases(a):
    """Every release a route can boot a machine into: the ladder after its first release, less may_skip."""
    ladder = a.get("ladder") if isinstance(a.get("ladder"), list) else []
    skip = a.get("may_skip") if isinstance(a.get("may_skip"), list) else []
    return [r for r in ladder[1:] if r not in skip]


def check_hardware(a, where):
    """SPEC "Hardware preflight": what a machine needs before a route boots it into each release."""
    h = a.get("hardware")
    if not isinstance(h, dict):
        return [f"{where}: missing the hardware table"]
    errs = []
    boot = h.get("boot")
    if not (isinstance(boot, list) and boot and all(b in ("bios", "uefi", "uefi-secureboot") for b in boot)):
        errs.append(f"{where}: hardware.boot must list the firmware modes proven, from bios, uefi and uefi-secureboot")
    if h.get("old_kernels") not in OLD_KERNELS:
        errs.append(f"{where}: hardware.old_kernels must be one of {list(OLD_KERNELS)}")
    if "autoremove_from" in h and not (h.get("old_kernels") == "autoremove" and isinstance(a.get("ladder"), list)
                                       and h["autoremove_from"] in a["ladder"]
                                       and isinstance(h["autoremove_from"], str)):
        errs.append(f"{where}: hardware.autoremove_from must name a release on the ladder, with old_kernels = "
                    f"\"autoremove\"")
    for r, t in h.items():
        if r in ("boot", "old_kernels", "autoremove_from"):
            continue
        if not isinstance(a.get("ladder"), list) or r not in a["ladder"] or not isinstance(t, dict):
            errs.append(f"{where}: hardware.{r} must be a table named for a release on the ladder")
            continue
        if set(t) - HARDWARE_KEYS:
            errs.append(f"{where}: hardware.{r} has unknown keys {sorted(set(t) - HARDWARE_KEYS)}")
        if t.get("cpu") not in CPU_LEVELS:
            errs.append(f"{where}: hardware.{r}.cpu must be one of {list(CPU_LEVELS)}")
        for k in ("ram_mib", "disk_mib", "boot_mib"):  # ram_mib is optional: some releases publish no minimum
            if (k != "ram_mib" or k in t) and (type(t.get(k)) is not int or t[k] < 1):
                errs.append(f"{where}: hardware.{r}.{k} must be a whole number of MiB")
        if not isinstance(t.get("drivers"), str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", t["drivers"]):
            errs.append(f"{where}: hardware.{r}.drivers must be the sha256 of its driver table")
        if "note" in t and not isinstance(t["note"], str):
            errs.append(f"{where}: hardware.{r}.note must be a string")
    missing = [r for r in booted_releases(a) if r not in h]
    if missing:
        errs.append(f"{where}: hardware has no table for {missing}, which a route can boot into")
    return errs


def check_runtimes(a, where):
    """SPEC "Runtime preflight": the versions of each legacy runtime every booted release ships ([] for none)."""
    rt = a.get("runtimes")
    if not isinstance(rt, dict):
        return [f"{where}: missing the runtimes table"]
    errs = []
    for r, t in rt.items():
        if not isinstance(a.get("ladder"), list) or r not in a["ladder"] or not isinstance(t, dict):
            errs.append(f"{where}: runtimes.{r} must be a table named for a release on the ladder")
            continue
        for k in RUNTIMES:
            v = t.get(k)
            if not (isinstance(v, list) and all(isinstance(x, str) and re.fullmatch(r"\d+\.\d+(\.\d+)?", x) for x in v)):
                errs.append(f"{where}: runtimes.{r}.{k} must list the versions the release ships ([] for none)")
        if set(t) - set(RUNTIMES):
            errs.append(f"{where}: runtimes.{r} has unknown keys {sorted(set(t) - set(RUNTIMES))}")
    missing = [r for r in booted_releases(a) if r not in rt]
    if missing:
        errs.append(f"{where}: runtimes has no table for {missing}, which a route can boot into")
    return errs


def driver_table_errors(fam, a, where):
    """Each release's driver table is in the atlas (adapters/drivers/<family>/<release>.txt) at the pinned hash."""
    errs = []
    h = a.get("hardware")
    for r, t in (h if isinstance(h, dict) else {}).items():
        if r in ("boot", "old_kernels", "autoremove_from") or not isinstance(t, dict) or not isinstance(t.get("drivers"), str):
            continue
        p = f"{ROOT}/adapters/drivers/{fam}/{r}.txt"
        got = "sha256:" + hashlib.sha256(open(p, "rb").read()).hexdigest() if os.path.isfile(p) else None
        if got != t["drivers"]:
            errs.append(f"{where}: hardware.{r}.drivers: adapters/drivers/{fam}/{r}.txt "
                        f"{'is missing' if got is None else 'has sha256 ' + got[7:19] + '...'}, not the pinned one")
    return errs


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
        if not isinstance(v, str) or not re.fullmatch(PKG, v):
            errs.append(f"{where}: bad package name {v!r}")
    elif k == "file":
        if not isinstance(v, str) or not re.fullmatch(ETC_PATH, v) or ".." in v:
            errs.append(f"{where}: 'file' must be a path under /etc, got {v!r}")
    elif k == "file-contains":
        if not isinstance(v, dict) or set(v) != {"path", "text"} or not all(isinstance(x, str) for x in v.values()) \
                or not re.fullmatch(ETC_PATH, v["path"]) or ".." in v["path"] or "'" in v["text"]:
            errs.append(f"{where}: 'file-contains' needs path (under /etc) and text (no single quotes)")
    elif k == "mysql-option":
        if not isinstance(v, dict) or not {"path", "key"} <= set(v) <= {"path", "key", "value"} \
                or not all(isinstance(x, str) for x in v.values()) \
                or not re.fullmatch(ETC_PATH, v["path"]) or ".." in v["path"] \
                or not re.fullmatch(MYSQL_KEY, v["key"]) or not re.fullmatch(MYSQL_VALUE, v.get("value", "")):
            errs.append(f"{where}: 'mysql-option' needs path (under /etc), key (lowercase, dashed) and optionally "
                        "value (no quotes, backslashes or spaces)")
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
    owned = list(adapter.get("harness_owned", [])) + ["/etc/wend/"]  # the box owner's secrets, on every family
    return not any(path.startswith(p) for p in owned) and any(
        fnmatch.fnmatchcase(path, g) and "/" not in path[len(g.rsplit("/", 1)[0]) + 1:]
        for g in adapter.get(allow, []))


def check_arg(name, spec, val, adapter, where, errs):
    """An adapter op's argument, by its spec: enum, pattern, list (of a pattern), path or paths (on one of the
    adapter's allowlists, named by the spec), table (string keys and values, each matching a pattern)."""
    vals = val.values() if isinstance(val, dict) else val if isinstance(val, list) else [val]
    if any(k in spec for k in ("pattern", "list", "table")) and not all(
            isinstance(x, str) and re.fullmatch(SAFE_VALUE, x) for x in vals):
        errs.append(f"{where}: {name} may only hold plain values ({SAFE_VALUE})")
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
    force = adapter["force"]
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
    if op in ("kv.set", "kv.unset") and isinstance(s.get("path"), str):
        kv = adapter.get("kv", {}).get(s["path"])
        keys = set(s["values"]) if op == "kv.set" and isinstance(s["values"], dict) else set(s.get("keys") or [])
        if not kv:
            errs.append(f"{where}: {s['path']} is not on the {adapter['family']} adapter's kv list")
        else:
            if op == "kv.set" and not kv.get("set"):
                errs.append(f"{where}: {s['path']} has sections; kv.set can't write it")
            if not keys <= set(kv["keys"]):
                errs.append(f"{where}: {s['path']} only allows keys {kv['keys']}, not {sorted(keys - set(kv['keys']))}")
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
            if ph == "post" and s.get("op") == "service.record":
                # after the upgrade the SysV links are already gone, and the first record is the one that is kept
                errs.append(f"{where}.post[{i}]: service.record must run before the upgrade (pre)")
    # The stray-MySQL rule: whatever a fix installs, it installs in place of something that is installed now.
    if any(s.get("op") == "install" or s.get("reinstall") for s in steps) and not errs and \
            not installed_names(doc["when"]):
        errs.append(f"{where}: a fix that installs packages must be gated on 'installed' of the package it replaces")
    checks = set(a.get("checks", {}))
    for c in doc.get("restores", []):
        if c not in checks and not re.fullmatch(r"box:[a-z0-9-]+", c):
            errs.append(f"{where}: restores {c!r}, which is neither an adapter check nor box:<role>")
    if not isinstance(doc.get("adds", []), list):
        errs.append(f"{where}: adds must be a list of name families")
    installs = installed_families(doc)
    for f in doc.get("adds", []) if isinstance(doc.get("adds", []), list) else []:
        if not isinstance(f, str) or not re.fullmatch(FAMILY, f):
            errs.append(f"{where}: adds {f!r}, which is not a name family ({FAMILY})")
        elif f not in installs:  # a fix may only declare what it installs itself, so review sees both together
            errs.append(f"{where}: adds {f!r}, but none of the fix's own install steps installs a package of that family")
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
    if doc.get("finish", {}).get("prune_obsolete") and not {"obsolete", "autoremove"} <= set(a.get("hop", {})):
        errs.append(f"{where}: prune_obsolete needs hop.obsolete and hop.autoremove, which the {a['family']} "
                    "adapter doesn't have")
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
    vm = doc.get("vm", {})  # tools/vm.py (SPEC "VM tier")
    if not isinstance(vm, dict) or set(vm) - {"ssh_port", "skip", "live", "pockets", "root_fs"}:
        errs.append(f"{where}: [vm] takes only ssh_port, skip, live, pockets and root_fs")
    elif not isinstance(vm.get("pockets", []), list) or not all(isinstance(p, str) for p in vm.get("pockets", [])) or \
            not set(vm.get("pockets", [])) <= {"updates", "security", "backports"} or \
            len(set(vm.get("pockets", []))) != len(vm.get("pockets", [])) or \
            (vm.get("pockets") and "updates" not in vm["pockets"]) or \
            (vm.get("pockets") and not adapters.get(doc.get("family"), {}).get("releases", {}).get(
                doc.get("release"), {}).get("install", {}).get("release_pocket_only")):
        errs.append(f"{where}: [vm] pockets lists updates, and security and/or backports beside it, once each, and only on a "
                    "release whose VM install is release_pocket_only")
    elif "root_fs" in vm and not (isinstance(fs := (adapters.get(doc.get("family"), {}).get("releases", {})
                                                    .get(doc.get("release")) or {}).get("install", {}).get("root_fs", []),
                                             list) and vm["root_fs"] in fs):
        errs.append(f"{where}: [vm] root_fs must be one its release's installer makes (releases.<release>.install.root_fs)")
    elif type(vm.get("ssh_port", 22)) is not int or not 0 < vm.get("ssh_port", 22) < 65536 or \
            not isinstance(vm.get("skip", []), list) or not all(isinstance(r, str) for r in vm.get("skip", [])) or \
            not set(vm.get("skip", [])) <= set(doc.get("roles", {})) or not isinstance(vm.get("live", {}), dict) or \
            not all(isinstance(c, str) and c.strip() for c in vm.get("live", {}).values()) or \
            not set(vm.get("skip", [])) <= set(vm.get("live", {})):
        errs.append(f"{where}: [vm] needs an integer ssh_port, skip naming roles the box has, and a live shell string "
                    "for every skipped role")
    if "shelter" in doc:
        errs += check_shelter(doc["shelter"], f"{where}: shelter")
    return errs


# SPEC "Sheltering an app": a box's [shelter.<app>] tables, the owner's confirmed plan. Every value lands in commands
# tools/shelter.py writes and runs as root, and in unit files, so each is held to a plain shape here.
SHELTER_APP = r"[a-z][a-z0-9]{0,30}"
SHELTER_KEYS = {"code", "data", "user", "unit", "cron", "sockets", "ports", "egress"}
ABS_PATH = r"(/[A-Za-z0-9_+-][A-Za-z0-9_.+-]*)+"  # no segment starts with a dot: no "." or "..", nothing hidden
USER = r"[a-z_][a-z0-9_-]{0,31}"
SERVICE_UNIT = r"[A-Za-z0-9][A-Za-z0-9_.@-]*\.service"
CRON_LINE = r"[A-Za-z0-9_./=:@,*+-]+( [A-Za-z0-9_./=:@,*+-]+)+"  # single spaces; no %, quotes, $ or redirects
CODE_UNDER = ("/opt/", "/srv/", "/home/", "/var/", "/usr/local/")
DATA_UNDER = ("/opt/", "/srv/", "/home/", "/var/")
PUBLISH = r"(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3}):(\d{1,5})"


def check_shelter(sh, where):
    import shelter  # tools/shelter.py: the same cron translation start runs
    if not isinstance(sh, dict) or not sh:
        return [f"{where} must be a table of apps, [shelter.<app>]"]
    errs = []
    for app, p in sh.items():
        w = f"{where}.{app}"
        if not re.fullmatch(SHELTER_APP, app):
            errs.append(f"{w}: an app's name is lowercase letters and digits ({SHELTER_APP})")
        if not isinstance(p, dict):
            errs.append(f"{w} must be a table")
            continue
        if set(p) - SHELTER_KEYS or not {"code", "data", "user", "unit"} <= set(p):
            errs.append(f"{w} takes code, data, user and unit, and optionally cron, sockets, ports and egress; "
                        f"got {sorted(p)}")
            continue
        path = lambda x, under: isinstance(x, str) and re.fullmatch(ABS_PATH, x) and x.startswith(under)  # noqa: E731
        if not path(p["code"], CODE_UNDER):
            errs.append(f"{w}.code must be a plain absolute path under {', '.join(CODE_UNDER)}")
        if not isinstance(p["data"], list) or not all(path(x, DATA_UNDER) for x in p["data"]):
            errs.append(f"{w}.data must list plain absolute paths under {', '.join(DATA_UNDER)}")
        elif isinstance(p["code"], str) and any(x == p["code"] or x.startswith(p["code"] + "/") or
                                                p["code"].startswith(x + "/") for x in p["data"]):
            errs.append(f"{w}.data overlaps the code tree, which is mounted read-only")
        if not isinstance(p["user"], str) or not re.fullmatch(USER, p["user"]) or p["user"] == "root":
            errs.append(f"{w}.user must be the app's own user ({USER}), not root")
        if not isinstance(p["unit"], str) or not re.fullmatch(SERVICE_UNIT, p["unit"]) or \
                p["unit"].startswith("wend-"):
            errs.append(f"{w}.unit must name the app's own service unit ({SERVICE_UNIT})")
        cron = p.get("cron", [])
        if not isinstance(cron, list) or not all(isinstance(c, dict) and set(c) == {"user", "line"} for c in cron):
            errs.append(f"{w}.cron must be a list of {{ user, line }} tables")
        else:
            for i, c in enumerate(cron):
                if not isinstance(c["user"], str) or not re.fullmatch(USER, c["user"]):
                    errs.append(f"{w}.cron[{i}].user must be a user name ({USER})")
                elif c["user"] != p.get("user"):  # one identity per shelter: discovery and the timer run as it
                    errs.append(f"{w}.cron[{i}].user must be the app's user ({p.get('user')!r}); "
                                "shelter another user's jobs as an app of their own")
                if not isinstance(c["line"], str) or not re.fullmatch(CRON_LINE, c["line"]):
                    errs.append(f"{w}.cron[{i}].line must be the crontab line exactly, in plain words ({CRON_LINE})")
                    continue
                try:
                    _, cmd = shelter.cron_calendar(c["line"])
                except ValueError as e:
                    errs.append(f"{w}.cron[{i}].line: its schedule doesn't translate exactly to a timer ({e})")
                    continue
                if not re.fullmatch(shelter.CMD, cmd):
                    errs.append(f"{w}.cron[{i}].line: the command must start with an absolute interpreter path")
        socks = p.get("sockets", [])
        if not isinstance(socks, list) or not all(isinstance(x, str) and re.fullmatch(ABS_PATH, x) for x in socks):
            errs.append(f"{w}.sockets must list plain absolute paths")
        elif len({os.path.dirname(x) for x in socks}) != len(socks):
            errs.append(f"{w}.sockets: one socket per directory (each directory is mounted from its own proxy)")
        ports = p.get("ports", [])
        m = [re.fullmatch(PUBLISH, x) if isinstance(x, str) else None for x in ports] if isinstance(ports, list) else [None]
        if not all(x and all(int(o) < 256 for o in x.groups()[:4]) and 0 < int(x.group(5)) < 65536 for x in m):
            errs.append(f"{w}.ports must list address:port strings (127.0.0.1:8080)")
        egress = p.get("egress", [])  # the TCP destinations the owner opted in to (SPEC: private by default)
        m = [re.fullmatch(PUBLISH, x) if isinstance(x, str) else None for x in egress] if isinstance(egress, list) else [None]
        if not all(x and all(int(o) < 256 for o in x.groups()[:4]) and 0 < int(x.group(5)) < 65536 for x in m):
            errs.append(f"{w}.egress must list address:port strings (192.0.2.10:443)")
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


def starts(box, route):
    """A route is replayed on the boxes of its family that start at its `from` (tools/affected.py, CONTRIBUTING.md).
    A box that starts elsewhere can't walk it: a centos7 box can't take a centos5 route's hop to centos6."""
    return box.get("family") == route.get("family") and box.get("release") == route.get("from")


def pairing_errors(routes, boxes, fixes):
    """CI replays a route only where it starts, so a route with no box at its `from` would be replayed nowhere, and a
    fix claiming a box that no route pinning it is replayed on would have that claim never checked."""
    errs = []
    pinned_by = {}
    for p, r in routes.items():
        if not any(starts(b, r) for b in boxes.values()):
            errs.append(f"{os.path.relpath(p, ROOT)}: no {r.get('family')} box starts at {r.get('from')}, so CI would "
                        "replay this route nowhere; add the box first (AGENTS.md step 2)")
        for ref in r.get("fixes", []):
            pinned_by.setdefault(ref.get("id"), []).append(r)
    for p, f in fixes.items():
        rs = pinned_by.get(content_id(f), [])
        for b in f.get("meta", {}).get("fires_on", []):
            if b not in boxes:
                errs.append(f"{os.path.relpath(p, ROOT)}: fires_on names {b}, which is not a box")
            elif rs and not any(starts(boxes[b], r) for r in rs):
                errs.append(f"{os.path.relpath(p, ROOT)}: fires_on names {b}, which starts where no route pinning "
                            "this fix starts, so CI never replays it there")
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
    try:
        return {"when": compile_when(doc["when"]), **out}  # ol.py's form, for the port's round-trip proof
    except ValueError:  # all/any, mysql-option: no ol.py form, so the adapter's template
        return {"when": backends.pred_sh(doc["when"], adapter), **out}


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
    for fam, a in adapters.items():
        errs += check_adapter(a, f"adapters/{fam}.toml")
        errs += driver_table_errors(fam, a, f"adapters/{fam}.toml")
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
    errs += pairing_errors(routes, boxes, fixes)
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
