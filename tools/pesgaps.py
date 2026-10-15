"""The leapp-data gap check (SPEC "Package evolution data"): which of a box's packages AlmaLinux's leapp-data says
change at a hop that no fix in the route names. Report only: it never picks, changes or refuses a route.

  ./wend gaps ROUTE BOX [--json]               build BOX (cached, as replay does) and check its rpm -qa      (root)
  ./wend gaps ROUTE --packages FILE [--json]   check a package list instead: one name per line, as
                                               `rpm -qa --qf '%{NAME}\\n'` prints it on a real machine (as one
                                               with no module streams enabled: right for EL7, not always for EL8)
  ./wend gaps --all [--json]                   every EL route, on every box that starts where it starts      (root)

The walk is a model, not a replay: it starts from the box's packages and, hop by hop, applies the leapp-data events
between the two releases (in release order, as Leapp does) and the package steps of the route's fixes for that hop
whose `installed` preconditions hold. An event applies when the box has every package it starts from (`merged`: any
of them). A package goes when an event that applies doesn't carry its name on (removed, renamed, replaced, the
merged-away half of a merge), or carries it to a repo the route doesn't enable (CRB, PowerTools). One that keeps its
name in a repo the route enables is left out: dnf follows it. What it reports, per hop:

- the box's own packages that go (the ones its box.toml installs; with --packages, all of them), each with what
  leapp-data says it becomes, and the route's fixes for that hop whose steps name it or what it becomes. None named
  is a gap to look at. A fix for that hop outside the route that names it is listed too. Naming isn't proof a fix
  handles it, and a package going isn't proof anything breaks: the replay and the box's checks decide.
- the dependencies that go, in one line: mostly plumbing the hop replaces as a matter of course.
- deprecated: still shipped there, going later.
"""
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import leappdata  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "adapters", "leapp")
# the step arguments that name packages (`installed` preconditions only gate a fix: they don't make it handle one)
PKG_KEYS = ("packages", "remove", "install", "reinstall")


def load(family="el"):
    """{source: (header, events)} from the vendored extracts, refused unless check.py's pin and schema checks pass:
    the data is untrusted input, read only as data for this report."""
    import check
    errs = check.pes_errors()
    if errs:
        raise SystemExit("the leapp-data extract isn't the pinned one:\n" + "\n".join(errs))
    return {os.path.basename(p)[:-len(".jsonl")]: leappdata.parse(open(p).read(), p)
            for p in sorted(glob.glob(os.path.join(DATA, family, "*.jsonl")))}


def ver(text):
    return tuple(int(x) for x in text.split("."))


def release_version(adapter, rel):
    """(major, minor) of a release: the minor from its pinned repo URL (vault.centos.org/7.9.2009 is 7.9), or 99 for a
    live repo that moves with the release (repo.almalinux.org/almalinux/9 is the newest 9.x)."""
    r = adapter["releases"][rel]
    m = re.search(r"/\d+\.(\d+)", next(iter(r["repos"].values())))
    return (r["major"], int(m.group(1)) if m else 99)


def enabled_repos(adapter, route, addons):
    """{major: {repo name}}: the repos a route's releases and the box's add-ons give dnf, in Leapp's terms (its
    `almalinux9-appstream` and `el9-appstream` are AlmaLinux 9's appstream; `el9-epel` is EPEL 9's)."""
    out = {}
    for rel in [route["from"], *route["hops"], *[r for r, d in adapter["releases"].items() if "transit" in d]]:
        r = adapter["releases"][rel]
        out.setdefault(r["major"], set()).update(k.lower() for k in r["repos"])
        out[r["major"]].update(addons)
    return out


def repo_ok(repo, enabled):
    m = re.fullmatch(r"(?:almalinux|el)(\d+)-(.+)", repo or "")
    return m is None or m.group(2) in enabled.get(int(m.group(1)), set())  # base, epel: the source release's repos


def names(x):
    """Every package name a fix mentions: its `installed` preconditions and its steps' package lists (not meta)."""
    out = set()
    if isinstance(x, dict):
        for k, v in x.items():
            if k == "meta":
                continue
            if k in PKG_KEYS and isinstance(v, str):
                out.add(v)
            elif k in PKG_KEYS and isinstance(v, list):
                out |= {i for i in v if isinstance(i, str)}
            else:
                out |= names(v)
    elif isinstance(x, list):
        for i in x:
            out |= names(i)
    return out


def holds(w, pkgs):
    """A precondition on the model's package set. Anything but `installed` is unknown here, and unknown holds."""
    if "installed" in w:
        return w["installed"] in pkgs
    if "all" in w:
        return all(holds(x, pkgs) for x in w["all"])
    if "any" in w:
        return any(holds(x, pkgs) for x in w["any"])
    if "not" in w:
        return "installed" not in w["not"] or not holds(w["not"], pkgs)
    return True


def steps_of(fix, phase):
    return fix.get(phase, [])


def apply_steps(fix, phase, pkgs):
    for s in steps_of(fix, phase):
        if s.get("op") in ("remove", "force-remove"):
            pkgs -= set(s.get("packages", []))
        elif s.get("op") == "install":
            pkgs |= set(s.get("packages", []))
        elif s.get("op") == "rpm.swap-release":
            pkgs.discard(s.get("remove"))
            pkgs.add(s.get("install"))


def walk(repo, route_path, packages, data=None, own=None, streams=()):
    """The report, as a dict (see the module doc). `own`: the packages the box installs on purpose (None: all).
    `streams`: the box's enabled module streams (`php:7.2`), EL8 only. An event about a package in a module stream
    applies only to a box with that stream; every hop's own steps reset the streams (adapter: modules.d emptied), so
    from the second hop on there are none."""
    mods = set(streams)
    route = repo.routes[route_path]
    adapter = repo.adapters[route["family"]]
    data = load(route["family"]) if data is None else data
    pkgs = set(packages)
    own = set(pkgs if own is None else own)
    addons = {n for n, a in adapter.get("addons", {}).items() if a["marker"] in pkgs}
    sources = [s for s in data if s not in adapter.get("addons", {}) or s in addons]
    enabled = enabled_repos(adapter, route, addons)
    fixes = [repo.fixes_by_id[f["id"]] for f in route["fixes"]]
    report = {"route": route_path, "start_packages": len(pkgs), "addons": sorted(addons),
              "leapp_data": {s: data[s][0]["commit"] for s in sources}, "hops": []}
    prev = release_version(adapter, route["from"])
    for hop in route["hops"]:
        now = release_version(adapter, hop)
        say = lambda v: "%d.x" % v[0] if v[1] == 99 else "%d.%d" % v
        h = {"hop": hop, "from": say(prev), "to": say(now),
             "packages": len(pkgs), "checked": prev[0] >= 7, "gone": [], "deprecated": []}
        report["hops"].append(h)
        mine = [f for f in fixes if f["hop"] == hop]
        others = [d for p, d in sorted(repo.fixes.items()) if d.get("hop") == hop and d not in fixes
                  and p.startswith(f"fixes/{route['family']}/")]
        firing = [f for f in mine if holds(f.get("when", {}), pkgs)]
        for f in firing:
            apply_steps(f, "pre", pkgs)
        events = sorted((e for s in sources for e in data[s][1] if prev < ver(e["to"]) <= now),
                        key=lambda e: (ver(e["to"]), e["id"]))
        gone = {}
        for e in events:
            ins = [p["name"] for p in e["in"]]
            have = [p["name"] for p in e["in"] if p["name"] in pkgs and (mods | {""}) & set(p.get("streams", [""]))]
            if not have or (e["action"] != "merged" and len(have) < len(ins)):
                continue
            if e["action"] == "deprecated":
                h["deprecated"] += [n for n in have if n not in h["deprecated"]]
                continue
            kept = {p["name"] for p in e["out"] if repo_ok(p["repo"], enabled)}
            for n in have:
                if n in kept:
                    continue
                g = gone.setdefault(n, {"package": n, "action": e["action"], "at": e["to"], "own": n in own,
                                        "becomes": [], "events": []})
                g["events"].append(e["id"])
                for p in e["out"]:
                    if p["name"] == n:
                        g["becomes"].append({"name": n, "repo": p["repo"]})  # moved where the route doesn't go
                        continue
                    o = {"name": p["name"]} | ({} if repo_ok(p["repo"], enabled) else {"repo": p["repo"]}) | \
                        ({"streams": [x for x in p["streams"] if x]} if any(p.get("streams", [""])) else {})
                    if o not in g["becomes"]:
                        g["becomes"].append(o)
                pkgs.discard(n)
            pkgs |= kept
        for g in gone.values():
            touched = {g["package"]} | {o["name"] for o in g["becomes"]}
            g["named_by"] = [f["meta"]["name"] for f in mine if names(f) & touched]
            g["elsewhere"] = [d["meta"]["name"] for d in others if names(d) & touched]
            h["gone"].append(g)
        h["deprecated"] = [n for n in h["deprecated"] if n in pkgs]
        for f in firing:
            apply_steps(f, "post", pkgs)
        prev, mods = now, set()
    return report


def text(report, label):
    commits = sorted(set(report["leapp_data"].values()))
    out = [f"{label} on {report['route']}: {report['start_packages']} packages"
           f"{' with ' + ', '.join(report['addons']) if report['addons'] else ''}; leapp-data "
           f"{', '.join(c[:12] for c in commits)} ({', '.join(report['leapp_data'])}). Report only."]

    def becomes(g, most=6):
        b = [o["name"] + (f" (in {o['repo']}, which the route doesn't enable)" if "repo" in o else "") +
             (f" (module {'/'.join(o['streams'])})" if "streams" in o else "") for o in g["becomes"]]
        return ", ".join(b[:most]) + (f" and {len(b) - most} more" if len(b) > most else "")
    total = [0, 0]
    if not all(h["checked"] for h in report["hops"]):
        out.append("  (leapp-data starts at EL7. The hops before it aren't modelled, so the box's packages keep the names "
                   "they had at the start: one that those hops renamed may show as going later when it already went.)")
    for h in report["hops"]:
        out.append(f"  hop {h['hop']} ({h['from']} -> {h['to']}), {h['packages']} packages")
        if not h["checked"]:
            out.append("    not checked: leapp-data has nothing before EL7")
            continue
        own = [g for g in h["gone"] if g["own"]]
        unnamed = [g for g in own if not g["named_by"]]
        total[0] += len(unnamed)
        total[1] += len(own) - len(unnamed)
        out.append(f"    the box's own packages that go: {len(own)}, {len(unnamed)} that no fix in the route names"
                   if own else "    none of the box's own packages go")
        for g in sorted(own, key=lambda g: (bool(g["named_by"]), g["package"].lower())):
            b = becomes(g)
            s = f"      {g['package']}: {g['action']} at {g['at']}{' -> ' + b if b else ''}"
            s += f"; named by {', '.join(g['named_by'])}" if g["named_by"] else "; no fix names it"
            if g["elsewhere"]:
                s += f" (outside the route: {', '.join(g['elsewhere'])})"
            out.append(s)
        deps = sorted((g for g in h["gone"] if not g["own"]), key=lambda g: g["package"].lower())
        if deps:
            out.append(f"    dependencies that go ({len(deps)}): " +
                       ", ".join(g["package"] + (" *" if not g["named_by"] and any("repo" in o for o in g["becomes"])
                                                 else "") for g in deps))
            if any(not g["named_by"] and any("repo" in o for o in g["becomes"]) for g in deps):
                out.append("      * goes to a repo the route doesn't enable")
        if h["deprecated"]:
            out.append(f"    deprecated ({len(h['deprecated'])}): {', '.join(sorted(h['deprecated'], key=str.lower))}")
    out.append(f"  {total[0]} of the box's own packages go with no fix naming them, {total[1]} named by a fix")
    return "\n".join(out)


def box_packages(repo, box_id):
    """Build the box (or reuse the cached build) and read its package names."""
    import sandbox
    fam = box_id.split("/")[0]
    box = sandbox.build_box(box_id, repo.boxes[box_id], repo.adapters[fam],
                            repo.raw[f"boxes/{box_id}/box.toml"], repo.raw[f"adapters/{fam}.toml"])
    rc, log = box.run("rpm -qa --qf '%{NAME}\\n'")
    if rc:
        raise RuntimeError("rpm -qa failed in the box:\n" + log[-2000:])
    # EL8's enabled module streams, as dnf records them (none before EL8)
    _, mods = box.run("cat /etc/dnf/modules.d/*.module 2>/dev/null | awk -F= '/^name=/ {n=$2} /^stream=/ {s=$2} "
                      "/^state=enabled/ {print n \":\" s}'")
    return log.split(), [m for m in mods.split() if leappdata.STREAM.fullmatch(m)]


def main(repo, rest):
    as_json = "--json" in rest
    rest = [x for x in rest if x != "--json"]
    if rest[:1] == ["--all"]:
        jobs = [(r, b) for r, d in sorted(repo.routes.items()) for b, bd in sorted(repo.boxes.items())
                if d["family"] == bd["family"] and bd["release"] == d["from"] and os.path.isdir(f"{DATA}/{d['family']}")]
    elif "--packages" in rest:
        f = rest[rest.index("--packages") + 1]
        given = open(f).read().split()
        odd = [n for n in given if not leappdata.NAME.fullmatch(n)]
        if odd:
            print(f"{f}: not package names, one per line: {odd[:5]}")
            return 2
        report = walk(repo, rest[0], given)
        print(json.dumps(report, indent=1) if as_json else text(report, os.path.basename(f)))
        return 0
    elif len(rest) == 2:
        jobs = [tuple(rest)]
    else:
        print(__doc__)
        return 2
    reports = []
    for r, b in jobs:
        pkgs, streams = box_packages(repo, b)
        report = walk(repo, r, pkgs, own=repo.boxes[b]["packages"], streams=streams) | {"box": b, "streams": streams}
        reports.append(report)
        if not as_json:
            print(text(report, b) + "\n")
    if as_json:
        print(json.dumps(reports if len(reports) > 1 else reports[0], indent=1))
    return 0
