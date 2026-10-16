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
    out = {os.path.basename(p)[:-len(".jsonl")]: leappdata.parse(open(p).read(), p)
           for p in sorted(glob.glob(os.path.join(DATA, family, "*.jsonl")))}
    if set(out) != set(leappdata.SOURCES):
        raise SystemExit(f"adapters/leapp/{family}: expected the extracts {sorted(leappdata.SOURCES)}, found {sorted(out)}")
    return out


def ver(text):
    return tuple(int(x) for x in text.split("."))


def release_version(adapter, rel):
    """(major, minor) of a release: the minor from its pinned repo URL (vault.centos.org/7.9.2009 is 7.9), or 99 for a
    live repo that moves with the release (repo.almalinux.org/almalinux/9 is the newest 9.x)."""
    r = adapter["releases"][rel]
    m = re.search(r"/\d+\.(\d+)", next(iter(r.get("repos", {}).values()), ""))
    return (r["major"], int(m.group(1)) if m else 99)


def enabled_repos(adapter, route, addons):
    """{major: {repo name}}: the repos a route's releases and the box's add-ons give dnf, in Leapp's terms (its
    `almalinux9-appstream` and `el9-appstream` are AlmaLinux 9's appstream; `el9-epel` is EPEL 9's)."""
    out = {}
    for rel in [route["from"], *route["hops"], *[r for r, d in adapter["releases"].items() if "transit" in d]]:
        r = adapter["releases"][rel]
        out.setdefault(r["major"], set()).update(k.lower() for k in r.get("repos", {}))
        out[r["major"]].update(addons)
    return out


def repo_ok(repo, enabled):
    m = re.fullmatch(r"(?:almalinux|el)(\d+)-(.+)", repo or "")
    return m is None or m.group(2) in enabled.get(int(m.group(1)), set())  # base, epel: the source release's repos


def names(x):
    """Every package name a fix's steps install, remove or reinstall (not its `installed` gates, not meta)."""
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
    return truth(w, pkgs) is not False


def truth(w, pkgs):
    """True, False, or None for unknown (any predicate but `installed`), through not, all and any."""
    if "installed" in w:
        return w["installed"] in pkgs
    if "not" in w:
        t = truth(w["not"], pkgs)
        return None if t is None else not t
    if "all" in w:
        ts = [truth(x, pkgs) for x in w["all"]]
        return False if False in ts else None if None in ts else True
    if "any" in w:
        ts = [truth(x, pkgs) for x in w["any"]]
        return True if True in ts else None if None in ts else False
    return None


def in_stream(p, mods):
    """A package of an event is outside module streams ("" in its streams) or in one the box has enabled."""
    return bool((mods | {""}) & set(p.get("streams", [""])))


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
    # names alone can't show an add-on found by vendor (nginx): only marker add-ons count here
    addons = {n for n, a in adapter.get("addons", {}).items() if a.get("marker") in pkgs}
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
        # Within one release, leapp-data can map a package more than once (one event written from 8.0, another from
        # 8.3). The one written from the latest release applies; the others are shown beside it, not applied.
        events = sorted((e for s in sources for e in data[s][1] if prev < ver(e["to"]) <= now),
                        key=lambda e: (ver(e["to"]), tuple(-x for x in ver(e["from"] or "0.0")), e["id"]))
        rivals = {}
        for e in events:
            for p in e["in"]:
                rivals.setdefault((e["to"], p["name"]), []).append(e)
        gone, origin, done = {}, {}, set()  # origin: a successor this hop installed -> the box package it stands for
        for e in events:
            ins = set(p["name"] for p in e["in"])
            have = list(dict.fromkeys(p["name"] for p in e["in"] if p["name"] in pkgs and in_stream(p, mods)))
            if not have or (e["action"] != "merged" and len(have) < len(ins)):
                continue
            # a package an earlier mapping at this release already took isn't taken again; the rest of a merge still is
            have = [n for n in have if (e["to"], n) not in done]
            if not have:
                continue
            if e["action"] == "deprecated":
                h["deprecated"] += [n for n in have if n not in h["deprecated"]]
                continue
            # an output only in a module stream lands only where that stream is enabled: rpm.transit enables none,
            # not even EL8's defaults (el9-fail2ban-through-el8), and every hop resets them
            kept = {p["name"] for p in e["out"] if repo_ok(p["repo"], enabled) and in_stream(p, mods)}
            done |= {(e["to"], n) for n in have}
            for n in have:
                if n in kept:
                    continue
                # a successor going again (python -> platform-python at 8.0 -> python3 at 9.0, both in the hop
                # through EL8) extends the box package it came from, not a new entry
                root = origin.get(n, n)
                g = gone.setdefault(root, {"package": root, "action": e["action"], "at": e["to"], "own": root in own,
                                           "becomes": [], "steps": [], "events": []})
                g["events"].append(e["id"])
                step = {"package": n, "action": e["action"], "at": e["to"], "becomes": []}
                same = (e["action"], sorted(p["name"] for p in e["out"]))
                alt = []
                for r in rivals[(e["to"], n)]:
                    a = {"action": r["action"], "becomes": sorted(p["name"] for p in r["out"])}
                    if r is e or r["action"] == "deprecated" or (a["action"], a["becomes"]) == same or a in alt or \
                            not any(p["name"] == n and in_stream(p, mods) for p in r["in"]):
                        continue
                    alt.append(a)
                if alt:
                    step["also"] = alt  # leapp-data maps it another way too
                g["steps"].append(step)
                for p in e["out"]:
                    o = {"name": p["name"]} | ({} if repo_ok(p["repo"], enabled) else {"repo": p["repo"]}) | \
                        ({} if in_stream(p, mods) else {"streams": [x for x in p["streams"] if x]})
                    if p["name"] == n and "repo" not in o and "streams" not in o:
                        continue
                    step["becomes"].append(o)
                    if p["name"] in kept and p["name"] not in pkgs:  # one the box already had stands for itself
                        origin[p["name"]] = root
                g["becomes"] = [o for o in g["becomes"] if o["name"] != n] + \
                    [o for o in step["becomes"] if o not in g["becomes"]]
                pkgs.discard(n)
            pkgs |= kept
        for g in gone.values():
            touched = {g["package"]} | {o["name"] for st in g["steps"] for o in st["becomes"]} | \
                {st["package"] for st in g["steps"]} | {n for st in g["steps"] for a in st.get("also", []) for n in a["becomes"]}
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
    stranded = lambda g: not g["named_by"] and any("repo" in o or "streams" in o for o in g["becomes"])
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
            s = f"      {g['package']}: " + ", then ".join(
                (f"{st['package']} " if i else "") + f"{st['action']} at {st['at']}" +
                (f" -> {becomes(st)}" if st["becomes"] else "") +
                "".join(f" (leapp-data also says {a['action']}{' -> ' + ', '.join(a['becomes']) if a['becomes'] else ''})"
                        for a in st.get("also", [])) for i, st in enumerate(g["steps"]))
            s += f"; named by {', '.join(g['named_by'])}" if g["named_by"] else "; no fix names it"
            if g["elsewhere"]:
                s += f" (outside the route: {', '.join(g['elsewhere'])})"
            out.append(s)
        deps = sorted((g for g in h["gone"] if not g["own"]), key=lambda g: g["package"].lower())
        if deps:
            out.append(f"    dependencies that go ({len(deps)}): " +
                       ", ".join(g["package"] + (" *" if stranded(g) else "") for g in deps))
            if any(stranded(g) for g in deps):
                out.append("      * goes to a repo or module stream the route doesn't enable")
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


def route_path(repo, name):
    """A route by its path or its short name (centos7-to-alma10), as the client takes it; None if there's none, or the
    family has no leapp-data."""
    hits = [p for p, d in repo.routes.items() if name in (p, f"{d['family']}/{os.path.basename(p)[:-5]}",
                                                           os.path.basename(p)[:-5])]
    if len(hits) != 1 or not os.path.isdir(f"{DATA}/{repo.routes[hits[0]]['family']}"):
        return None
    return hits[0]


def main(repo, rest):
    as_json = "--json" in rest
    rest = [x for x in rest if x != "--json"]
    if rest == ["--all"]:
        jobs = [(r, b) for r, d in sorted(repo.routes.items()) for b, bd in sorted(repo.boxes.items())
                if d["family"] == bd["family"] and bd["release"] == d["from"] and os.path.isdir(f"{DATA}/{d['family']}")]
    elif len(rest) == 3 and rest[1] == "--packages" and route_path(repo, rest[0]):
        f = rest[2]
        try:
            given = open(f).read().split()
        except OSError as e:
            print(f"{f}: {e.strerror}")
            return 2
        odd = [n for n in given if not leappdata.NAME.fullmatch(n)]
        if odd:
            print(f"{f}: not package names, one per line: {odd[:5]}")
            return 2
        report = walk(repo, route_path(repo, rest[0]), given)
        print(json.dumps(report, indent=1) if as_json else text(report, os.path.basename(f)))
        return 0
    elif len(rest) == 2 and route_path(repo, rest[0]) and rest[1] in repo.boxes:
        r, b = route_path(repo, rest[0]), rest[1]
        if (repo.boxes[b]["family"], repo.boxes[b]["release"]) != (repo.routes[r]["family"], repo.routes[r]["from"]):
            print(f"{b} doesn't start where {r} starts ({repo.routes[r]['family']} {repo.routes[r]['from']})")
            return 2
        jobs = [(r, b)]
    else:
        print(__doc__.split("\n\n")[1])
        print("ROUTE: an EL route, by path or short name (centos7-to-alma10); BOX: a box id (el/centos7-lamp)")
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
