"""Is every pin on main in the public archive? (docs/archive.md, "Keeping it current"). Runs hourly in CI
(.github/workflows/archive-check.yml), from outside remi, so a cycle that stopped running is noticed. Standard library
plus ssh-keygen.

  python3 tools/archive_check.py [--index URL|FILE] [--grace HOURS] [--signers FILE] [--lost FILE] [--runs DIR]

Reads the archive's index/latest.json (default https://archive.fieldmouseworks.com/index/latest.json) and its .sig,
checks the signature against archive/allowed_signers, then lists every top file a passing run on main pins that the
index doesn't hold at its digest. archive/lost.txt excuses a pin whose bytes are known to be gone everywhere. A pin
whose run landed on this branch (first-parent history) under HOURS ago (default 6) is pending: remi's hourly cycle
hasn't had its turn. Exits 1 when a pin is overdue or the signature is bad, 0 otherwise. A lost.txt entry the archive
now holds is pointed out, so the list only shrinks.
"""
import functools
import glob
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import archive  # noqa: E402

INDEX = "https://archive.fieldmouseworks.com/index/latest.json"


def fetch(src, dst):
    if "://" in src:
        with urllib.request.urlopen(src, timeout=120) as r:
            data = r.read()
    else:
        data = open(src, "rb").read()
    open(dst, "wb").write(data)


@functools.lru_cache(None)
def landed(path, repo):
    """When path first landed on this branch (a PR's merge commit), as a unix time; now if it hasn't."""
    r = subprocess.run(["git", "-C", repo, "log", "--first-parent", "--diff-filter=A", "--format=%ct", "--",
                        os.path.relpath(path, repo)], capture_output=True, text=True)
    stamps = r.stdout.split()
    return int(stamps[-1]) if r.returncode == 0 and stamps else int(time.time())


def main(argv):
    opts = {"--index": [INDEX], "--grace": ["6"], "--signers": [f"{ROOT}/archive/allowed_signers"],
            "--lost": [f"{ROOT}/archive/lost.txt"], "--runs": [f"{ROOT}/runs"]}
    rest = list(argv)
    while rest:
        a = rest.pop(0)
        if a in opts and rest:
            opts[a].append(rest.pop(0))
        else:
            print(f"archive_check: {a!r} isn't an option, or has no value\n{__doc__}")
            return 2
    src, runs_dir = opts["--index"][-1], opts["--runs"][-1]
    grace, signers = float(opts["--grace"][-1]) * 3600, opts["--signers"][-1]
    with tempfile.TemporaryDirectory() as t:
        try:
            fetch(src, f"{t}/latest.json")
            fetch(f"{src}.sig", f"{t}/latest.json.sig")
        except Exception as e:  # urllib's HTTPError, a timeout, a missing file
            print(f"archive_check: can't read {src} and its .sig: {e}")
            return 1
        if not archive.check_sig(signers, f"{t}/latest.json"):
            print(f"archive_check: {src} isn't signed by a key in {signers}")
            return 1
        index = json.load(open(f"{t}/latest.json"))
    files, lost = index["files"], archive.read_lost(opts["--lost"][-1])
    pins = {}  # (url, (alg, hex)) -> run file paths
    for p in sorted(glob.glob(f"{runs_dir}/**/*.json", recursive=True)):
        run = json.load(open(p))
        if run.get("result") != "pass":
            continue
        for hop in run.get("hops", []):
            for url, pin in hop.get("sources", {}).items():
                d = archive.digest(*pin.partition(":")[::2])
                if d and d[0] == "sha256":
                    pins.setdefault((url, d), []).append(p)
    repo = os.path.dirname(os.path.abspath(runs_dir))
    now, overdue, pending = time.time(), [], []
    for (url, d), paths in sorted(pins.items()):
        if f"sha256:{d[1]}" in files.get(url, {}) or (url, d) in lost:
            continue
        first = min(paths, key=lambda p: landed(p, repo))
        age = now - landed(first, repo)
        (overdue if age >= grace else pending).append((url, d[1], os.path.relpath(first, runs_dir), age))
    print(f"archive index generated {index.get('generated', '?')}; {len(pins)} pins in passing runs on main, "
          f"{len(lost)} known lost (archive/lost.txt)")
    for what, rows in (("OVERDUE", overdue), ("pending", pending)):
        for url, h, run, age in rows:
            print(f"  {what:7} {url} sha256:{h} ({run}, landed {age / 3600:.1f}h ago)")
    for url, (_, h) in sorted(lost):
        if f"sha256:{h}" in files.get(url, {}):
            print(f"  found   {url} sha256:{h}: the archive holds it now, so drop it from archive/lost.txt")
    print(f"{len(overdue)} overdue, {len(pending)} pending (under {grace / 3600:g}h since they landed)")
    return 1 if overdue else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
