"""Vendor AlmaLinux's leapp-data (SPEC "Package evolution data"): what each EL package becomes at each release.

Maintainer tool. leapp-data (https://github.com/AlmaLinux/leapp-data, Apache-2.0) is the data ELevate's Leapp reads:
its Package Evolution Service events say, for ~18k packages, which ones are removed, renamed, split, merged, replaced,
moved to another repo or deprecated between CentOS 7 and AlmaLinux 10. wend never fetches it at run time. This tool
downloads the two files wend reads, from one pinned commit, checks each against the sha256 the pin names, and writes a
compact extract to adapters/leapp/el/<source>.jsonl: the x86_64 events that say something changed (`Present` and
`Reinstalled` say nothing did), one per line, with a header line naming the commit, the file and its sha256. The
pin itself is adapters/leapp/el/manifest.toml: the commit, and per source the upstream file's sha256 and the
extract's. `./wend check` refuses an extract that doesn't match it or doesn't parse (parse() below).

  python3 tools/leappdata.py COMMIT     pin a new commit: download, write the extracts and the manifest, and print
                                        what changed against the old extract (for the PR body: refresh only by PR)
  python3 tools/leappdata.py --check    download the pinned files again; fail unless they hash as pinned and the
                                        extracts come out byte for byte as committed

The data is untrusted input. It is only ever read as data for a report: never an op, never a package name handed to
a shell, never a URL to fetch. parse() holds every line to a strict schema and caps its sizes.

Needs curl. Standard library only.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIR = os.path.join(ROOT, "adapters", "leapp", "el")
UPSTREAM = "https://github.com/AlmaLinux/leapp-data"
RAW = "https://raw.githubusercontent.com/AlmaLinux/leapp-data"
# <source>: the file in leapp-data. `almalinux` covers CentOS 7 -> AlmaLinux 8 -> 9 -> 10; `epel` is ELevate's EPEL
# vendor data (its os_name is a placeholder Leapp fills in, which wend doesn't read).
SOURCES = {"almalinux": "files/almalinux/pes-events.json", "epel": "vendors.d/epel_pes.json_template"}
# Leapp's action numbers (leapp-repository, pes_events_scanner)
ACTIONS = {0: "present", 1: "removed", 2: "deprecated", 3: "replaced", 4: "split", 5: "merged", 6: "moved",
           7: "renamed", 8: "reinstalled"}
SAY_NOTHING = ("present", "reinstalled")
# parse(): what a line may hold, and how much of it
MAX_BYTES, MAX_EVENTS, MAX_SET, MAX_STREAMS = 16 << 20, 100_000, 256, 16
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}")  # SPEC "Preconditions": package names
REPO = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
STREAM = re.compile(r"(?:[A-Za-z0-9][A-Za-z0-9._+-]{0,63}:[A-Za-z0-9][A-Za-z0-9._+-]{0,63})?")
VERSION = re.compile(r"\d{1,2}\.\d{1,2}")
HEAD_KEYS = {"schema", "source", "upstream", "commit", "path", "sha256", "license", "note"}
EVENT_KEYS = {"id", "action", "from", "to", "in", "out"}


def get(url):
    return subprocess.run(["curl", "-fsSL", "--retry", "5", "--retry-all-errors", url], capture_output=True,
                          check=True).stdout


def version(r):
    return None if r is None else f"{r['major_version']}.{r['minor_version']}"


def package(p):
    out = {"name": p["name"], "repo": p.get("repository") or ""}
    streams = [f"{m['name']}:{m['stream']}" if m else "" for m in (p.get("modulestreams") or [None])]
    if streams != [""]:
        out["streams"] = streams  # "" is "not in a module", which Leapp lists beside streams
    return out


def extract(source, commit, data):
    """The extract's lines: the header, then one event per line, sorted by release and id."""
    doc = json.loads(data)
    events = []
    for e in doc["packageinfo"]:
        action = ACTIONS[e["action"]]
        if action in SAY_NOTHING or "x86_64" not in e["architectures"]:
            continue
        # the EPEL template's own placeholder replaced part of a few names (`python-simple{os_name}`): no package is
        # called that, so those events can't match anything and are left out
        if any(not NAME.fullmatch(p["name"]) for k in ("in_packageset", "out_packageset")
               for p in (e[k] or {"package": []})["package"]):
            continue
        events.append({"id": e["id"], "action": action, "from": version(e["initial_release"]),
                       "to": version(e["release"]),
                       "in": [package(p) for p in e["in_packageset"]["package"]],
                       "out": [package(p) for p in (e["out_packageset"] or {"package": []})["package"]]})
    events.sort(key=lambda e: (tuple(int(x) for x in e["to"].split(".")), e["id"]))
    head = {"schema": "wend.pes/0", "source": source, "upstream": UPSTREAM, "commit": commit, "path": SOURCES[source],
            "sha256": hashlib.sha256(data).hexdigest(), "license": "Apache-2.0",
            "note": "Extracted from leapp-data by tools/leappdata.py: x86_64 events that change something and name only "
                    "real package names, reformatted, one per line. Not the original file; see adapters/leapp/NOTICE. Don't edit."}
    return "".join(json.dumps(x, sort_keys=True, separators=(",", ":")) + "\n" for x in [head] + events)


def parse(text, where):
    """(header, events) from an extract, or ValueError naming the first thing wrong. Strict: unknown keys, wrong
    types, names outside the package-name pattern and anything past the size caps are refused, not skipped."""
    def fail(why):
        raise ValueError(f"{where}: {why}")
    if len(text) > MAX_BYTES:
        fail(f"over {MAX_BYTES >> 20} MiB")
    lines = text.split("\n")
    if lines[-1] != "" or len(lines) < 2:
        fail("not one JSON object per line, ending in a newline")
    lines.pop()
    if len(lines) - 1 > MAX_EVENTS:
        fail(f"over {MAX_EVENTS} events")
    try:
        rows = [json.loads(x) for x in lines]
    except ValueError as e:
        fail(f"not JSON: {e}")
    head, events = rows[0], rows[1:]
    if not (isinstance(head, dict) and set(head) == HEAD_KEYS and all(isinstance(v, str) for v in head.values())
            and head["schema"] == "wend.pes/0" and head["upstream"] == UPSTREAM and head["source"] in SOURCES
            and head["path"] == SOURCES[head["source"]] and re.fullmatch(r"[0-9a-f]{40}", head["commit"])
            and re.fullmatch(r"[0-9a-f]{64}", head["sha256"]) and head["license"] == "Apache-2.0"):
        fail("the header line isn't a wend.pes/0 header for a known leapp-data source")

    def pkgs(v, n):
        if not (isinstance(v, list) and len(v) <= MAX_SET):
            fail(f"line {n}: a package set must be a list of at most {MAX_SET}")
        for p in v:
            if not (isinstance(p, dict) and set(p) - {"streams"} == {"name", "repo"} and isinstance(p["name"], str)
                    and NAME.fullmatch(p["name"]) and isinstance(p["repo"], str)
                    and (p["repo"] == "" or REPO.fullmatch(p["repo"]))):
                fail(f"line {n}: bad package {str(p)[:80]!r}")
            st = p.get("streams", [""])
            if not (isinstance(st, list) and 0 < len(st) <= MAX_STREAMS
                    and all(isinstance(x, str) and STREAM.fullmatch(x) for x in st)):
                fail(f"line {n}: bad module streams for {p['name']}")
    last = (0, 0)
    for n, e in enumerate(events, 2):
        if not (isinstance(e, dict) and set(e) == EVENT_KEYS):
            fail(f"line {n}: an event has exactly the keys {sorted(EVENT_KEYS)}")
        if not (type(e["id"]) is int and 0 <= e["id"] < 1 << 31):
            fail(f"line {n}: bad id")
        if e["action"] not in ACTIONS.values() or e["action"] in SAY_NOTHING:
            fail(f"line {n}: unknown action {str(e['action'])[:20]!r}")
        if not (isinstance(e["to"], str) and VERSION.fullmatch(e["to"])
                and (e["from"] is None or isinstance(e["from"], str) and VERSION.fullmatch(e["from"]))):
            fail(f"line {n}: releases are MAJOR.MINOR")
        to = tuple(int(x) for x in e["to"].split("."))
        if to < last:
            fail(f"line {n}: events are sorted by release")
        last = to
        pkgs(e["in"], n)
        pkgs(e["out"], n)
        if not e["in"]:
            fail(f"line {n}: an event names at least one package it starts from")
    return head, events


def manifest(fam="el"):
    p = os.path.join(ROOT, "adapters", "leapp", fam, "manifest.toml")
    return tomllib.load(open(p, "rb")) if os.path.isfile(p) else None


def pinned():
    """{source: header} from the committed extracts."""
    return {s: json.loads(open(os.path.join(DIR, f"{s}.jsonl")).readline()) for s in SOURCES}


def changes(old, new):
    """What a refresh changes, for the PR body: events added and dropped, by action."""
    key = lambda e: json.dumps(e, sort_keys=True)
    o, n = {key(e): e for e in old}, {key(e): e for e in new}
    out = []
    for word, keys, src in (("added", n.keys() - o.keys(), n), ("dropped", o.keys() - n.keys(), o)):
        by = {}
        for k in keys:
            by.setdefault(src[k]["action"], []).append(src[k])
        for a, es in sorted(by.items()):
            some = ", ".join(sorted({p["name"] for e in es for p in e["in"]})[:8])
            out.append(f"  {word} {len(es)} {a}: {some}{', ...' if len(es) > 8 else ''}")
    return out or ["  no event changed"]


def write_manifest(commit, rows):
    lines = ["# The leapp-data pin (SPEC \"Package evolution data\"). Written by tools/leappdata.py; refresh only by PR,",
             "# with what changed in the PR body. ./wend check refuses an extract that doesn't match it.",
             f'upstream = "{UPSTREAM}"', f'commit = "{commit}"', ""]
    for s, (path, up, ex) in rows.items():
        lines += [f"[files.{s}]", f'path = "{path}"', f'upstream_sha256 = "sha256:{up}"', f'sha256 = "sha256:{ex}"', ""]
    open(os.path.join(DIR, "manifest.toml"), "w").write("\n".join(lines))


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 2
    check = argv[0] == "--check"
    heads = pinned() if check else {s: {"commit": argv[0]} for s in SOURCES}
    if not check and not re.fullmatch(r"[0-9a-f]{40}", argv[0]):
        print("COMMIT is a full 40-character commit id, never a branch or tag: the pin must not move")
        return 2
    bad, rows = 0, {}
    os.makedirs(DIR, exist_ok=True)
    for s, path in SOURCES.items():
        commit = heads[s]["commit"]
        data = get(f"{RAW}/{commit}/{path}")
        text = extract(s, commit, data)
        parse(text, s)
        out = os.path.join(DIR, f"{s}.jsonl")
        if check:
            ok = hashlib.sha256(data).hexdigest() == heads[s]["sha256"] and open(out).read() == text
            print(f"{s}: {'ok' if ok else 'MISMATCH'} ({path} at {commit[:12]})")
            bad |= not ok
            continue
        try:
            old = parse(open(out).read(), out)[1] if os.path.isfile(out) else []
        except ValueError as e:
            print(f"  the old extract doesn't parse ({e}): everything counts as added")
            old = []
        open(out, "w").write(text)
        rows[s] = (path, hashlib.sha256(data).hexdigest(), hashlib.sha256(text.encode()).hexdigest())
        print(f"{s}: {path} sha256:{rows[s][1]} -> {os.path.relpath(out, ROOT)} ({text.count(chr(10)) - 1} events)")
        print("\n".join(changes(old, parse(text, s)[1])))
    if not check:
        write_manifest(argv[0], rows)
    return bad


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
