"""Which routes and boxes a PR must replay. Prints a GitHub Actions matrix as JSON.

  python3 tools/affected.py BASE_REF            e.g. origin/main; compares the working tree's commit to it

Rules (CONTRIBUTING.md, "What CI replays"):
  - a changed route: replay it on every box of its family that starts where it starts
  - a changed fix: every route that pins its old or new id (an edited fix has a new id, so its routes changed too)
  - a changed box: every route of that box's family that starts at the box's release, on that box
  - a changed adapter: every route of the family, on every box (expensive on purpose: adapters are spec changes)
Every route is replayed on every box of its family that starts at the route's `from`, not just the boxes its fixes
claim: the others are the decoys. A box that starts elsewhere can't walk the route at all (a centos7 box can't take
the centos5 route's hop to centos6), so it is no evidence either way.
"""
import glob
import json
import os
import subprocess
import sys
import tomllib

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check  # noqa: E402

ROOT = check.ROOT


def changed(base):
    out = subprocess.run(["git", "diff", "--name-only", f"{base}...HEAD"], cwd=ROOT, capture_output=True, text=True,
                         check=True).stdout
    return [l for l in out.splitlines() if l]


def old_id(base, path):
    r = subprocess.run(["git", "show", f"{base}:{path}"], cwd=ROOT, capture_output=True)
    return check.content_id(tomllib.loads(r.stdout.decode())) if r.returncode == 0 else None


def starts(box, route):
    return box["family"] == route["family"] and box["release"] == route["from"]


def main(base):
    files = changed(base)
    routes = {os.path.relpath(p, ROOT): tomllib.load(open(p, "rb")) for p in glob.glob(f"{ROOT}/routes/*/*.toml")}
    boxes = {os.path.relpath(os.path.dirname(p), f"{ROOT}/boxes"): tomllib.load(open(p, "rb"))
             for p in glob.glob(f"{ROOT}/boxes/*/*/box.toml")}
    ids, fams, box_only = set(), set(), {}
    want = set()
    for f in files:
        parts = f.split("/")
        if parts[0] == "fixes" and f.endswith(".toml"):
            if os.path.exists(f"{ROOT}/{f}"):
                ids.add(check.content_id(tomllib.load(open(f"{ROOT}/{f}", "rb"))))
            if o := old_id(base, f):
                ids.add(o)
        elif parts[0] == "routes" and f in routes:
            want |= {(f, b) for b, d in boxes.items() if starts(d, routes[f])}
        elif parts[0] == "boxes" and len(parts) > 2:
            box_only.setdefault(parts[1], set()).add(f"{parts[1]}/{parts[2]}")
        elif parts[0] == "adapters":
            fams.add(parts[1][:-5])
    for r, d in routes.items():
        fam_boxes = {b for b, x in boxes.items() if starts(x, d)}
        if d["family"] in fams or ids & {x["id"] for x in d["fixes"]}:
            want |= {(r, b) for b in fam_boxes}
        want |= {(r, b) for b in box_only.get(d["family"], set()) & fam_boxes}
    print(json.dumps({"include": [{"route": r, "box": b} for r, b in sorted(want)]}))


if __name__ == "__main__":
    main(sys.argv[1])
