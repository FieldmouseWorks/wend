"""The leapp-data gap check (tools/pesgaps.py, SPEC "Package evolution data") by behaviour, on made-up events: what
counts as a package going, what counts as a fix naming it, which events apply to which box. The pin and the schema
are the gate's (tools/test_check.py). python3 tools/test_pesgaps.py"""
import os
import sys
import tomllib

sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import pesgaps  # noqa: E402

adapter = tomllib.load(open(os.path.join(ROOT, "adapters", "el.toml"), "rb"))


def ev(i, action, to, ins, outs=(), streams=None):
    return {"id": i, "action": action, "from": None, "to": to,
            "in": [{"name": n, "repo": "base"} | ({"streams": streams} if streams else {}) for n in ins],
            "out": [{"name": n, "repo": r} for n, r in outs]}


HEAD = {"commit": "0" * 40}
DATA = {"almalinux": (HEAD, [
    ev(1, "removed", "8.0", ["gone-pkg"]),
    ev(2, "split", "8.0", ["kept-pkg"], [("kept-pkg", "almalinux8-baseos"), ("kept-pkg-libs", "almalinux8-baseos")]),
    ev(3, "moved", "8.0", ["moves-ok"], [("moves-ok", "almalinux8-appstream")]),
    ev(4, "moved", "8.0", ["moves-crb"], [("moves-crb", "almalinux8-powertools")]),
    ev(5, "replaced", "8.0", ["old-name"], [("new-name", "almalinux8-appstream")]),
    ev(6, "merged", "8.0", ["half-a", "half-b"], [("half-b", "almalinux8-baseos")]),
    ev(7, "removed", "9.0", ["modular"], streams=["mod:1"]),
    ev(8, "removed", "9.0", ["gated-only"]),
    ev(9, "deprecated", "8.0", ["old-but-here"]),
    ev(10, "removed", "7.6", ["before-start"]),
    ev(11, "removed", "10.0", ["new-name"]),
    ev(12, "removed", "8.0", ["needs-both", "other-half"]),
    ev(14, "merged", "8.0", ["py2"], [("platform-py", "almalinux8-baseos")]),
    ev(15, "merged", "9.0", ["platform-py"], [("py3", "almalinux9-appstream")]),
    {"id": 13, "action": "replaced", "from": None, "to": "8.0", "in": [{"name": "to-module", "repo": "base"}],
     "out": [{"name": "in-module", "repo": "almalinux8-appstream", "streams": ["mod:2"]}]},
]), "epel": (HEAD, [ev(20, "removed", "8.0", ["epel-thing"])])}


class Repo:
    pass


def fix(name, hop, when, post):
    return {"schema": "wend.fix/0", "family": "el", "hop": hop, "when": when, "post": post, "meta": {"name": name}}


installs_py3 = fix("installs-py3", "alma9", {"installed": "py2"}, [{"op": "install", "packages": ["py3"]}])
installs_new = fix("installs-new-name", "alma9", {"installed": "old-name"}, [{"op": "install", "packages": ["new-name"]}])
gated = fix("only-gated", "alma9", {"installed": "gated-only"}, [{"op": "service.enable", "unit": "x", "like": ["x"]}])
outside = fix("not-in-route", "alma9", {"installed": "gone-pkg"}, [{"op": "remove", "packages": ["gone-pkg"]}])
repo = Repo()
repo.adapters = {"el": adapter}
repo.fixes = {"fixes/el/alma9/d.toml": installs_py3, "fixes/el/alma9/a.toml": installs_new, "fixes/el/alma9/b.toml": gated, "fixes/el/alma9/c.toml": outside}
repo.fixes_by_id = {"d": installs_py3, "a": installs_new, "b": gated, "c": outside}
repo.routes = {"r": {"family": "el", "from": "centos7", "to": "alma10", "hops": ["alma9", "alma10"],
                     "fixes": [{"id": "a"}, {"id": "b"}, {"id": "d"}]},
               "r5": {"family": "el", "from": "centos5", "to": "alma10", "hops": ["centos6", "centos7", "alma9"],
                      "fixes": []}}

box = ["py2", "to-module", "gone-pkg", "kept-pkg", "moves-ok", "moves-crb", "old-name", "half-a", "half-b", "modular", "gated-only",
       "old-but-here", "before-start", "needs-both", "epel-thing"]
r = pesgaps.walk(repo, "r", box, data=DATA, own=box)
h9, h10 = r["hops"]
gone = {g["package"]: g for g in h9["gone"]}
bad = 0


def expect(ok, what):
    global bad
    print(("passed  " if ok else "WRONG   ") + what)
    bad |= not ok


expect("gone-pkg" in gone and not gone["gone-pkg"]["named_by"], "a removed package goes, and no fix names it")
expect(gone.get("gone-pkg", {}).get("elsewhere") == ["not-in-route"], "a fix for the hop outside the route is listed")
expect("kept-pkg" not in gone, "a split that keeps the package's name isn't a loss")
expect("moves-ok" not in gone, "a move between repos the route enables isn't a loss")
expect(gone.get("moves-crb", {}).get("becomes") == [{"name": "moves-crb", "repo": "almalinux8-powertools"}],
       "a move to a repo the route doesn't enable is a loss, naming the repo")
expect(gone.get("old-name", {}).get("named_by") == ["installs-new-name"], "a fix that installs the successor names it")
expect("half-a" in gone and "half-b" not in gone, "a merge takes away the half it doesn't carry on")
expect("modular" not in gone, "an event about a module stream skips a box without that stream")
expect("gated-only" in gone and not gone["gated-only"]["named_by"], "an installed precondition alone doesn't name it")
expect(h9["deprecated"] == ["old-but-here"] and "old-but-here" not in gone, "deprecated is listed apart, not as gone")
expect("before-start" not in gone, "events before the start release don't apply")
expect("needs-both" not in gone, "an event applies only when the box has every package it starts from")
expect("epel-thing" not in gone and r["addons"] == [], "EPEL's events skip a box without epel-release")
expect(gone.get("to-module", {}).get("becomes") == [{"name": "in-module", "streams": ["mod:2"]}],
       "a successor only in a module stream the box hasn't enabled doesn't land")
expect("platform-py" not in gone and [st["package"] for st in gone.get("py2", {}).get("steps", [])] == ["py2", "platform-py"]
       and gone["py2"]["named_by"] == ["installs-py3"],
       "a successor that goes again in the same hop extends the box package it came from, and its fix counts")
expect([g["package"] for g in h10["gone"]] == ["new-name"], "the next hop starts from what the last one left")
r = pesgaps.walk(repo, "r", box + ["epel-release"], data=DATA, own=["gone-pkg"], streams=["mod:1"])
gone = {g["package"]: g for g in r["hops"][0]["gone"]}
expect("epel-thing" in gone and r["addons"] == ["epel"], "EPEL's events apply to a box with epel-release")
expect("modular" in gone, "an event about a module stream applies to a box with that stream")
expect(gone["gone-pkg"]["own"] and not gone["moves-crb"]["own"], "the box's own packages are told from dependencies")
r = pesgaps.walk(repo, "r5", ["gone-pkg"], data=DATA)
expect([h["checked"] for h in r["hops"]] == [False, False, True] and r["hops"][2]["gone"][0]["package"] == "gone-pkg",
       "hops before EL7 aren't checked; the hop out of EL7 is")
txt = pesgaps.text(pesgaps.walk(repo, "r", box, data=DATA, own=box), "box")
expect("gone-pkg: removed at 8.0; no fix names it (outside the route: not-in-route)" in txt, "the text report says so")
sys.exit(bad)
