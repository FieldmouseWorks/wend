"""Download the gate's fetched-N artifacts for the archive (docs/archive.md, "CI captures, remi archives"). Runs on
remi. Standard library only.

  python3 tools/gate_fetched.py DIR [--token-file FILE] [--runs DIR [--have INDEX] [--lost FILE]] [--dry-run]

Each gate replay uploads what it downloaded as an artifact named fetched-N: its download list and the metadata's bytes,
which live repos replace within days. GitHub keeps them 14 days (gate.yml). This unpacks each artifact not yet seen into
DIR/<artifact id>/, recording it in DIR/.done. Then:

  python3 tools/archive.py ingest STORE --fetched DIR

Without --runs it takes every unexpired fetched-* artifact: hundreds of GB, mostly superseded pushes. With --runs
(main's runs/), it takes only the artifacts behind the passing run files there, and with --have (the store's
index/latest.json) only those of runs no ingest has walked yet: a top file they pin isn't stored, or its index entry
doesn't name the run (lost.txt aside). A run file lands byte for byte as a gate job wrote it (the gate's "runs land
only from CI"), so its sha256 names its attestation, the attestation names the workflow run, and the run-N artifact
there whose run.json is that file names N: fetched-N is its capture. Each lookup is remembered in DIR/.sources.json.
--dry-run says what it would download, and downloads none.

Nothing here is trusted: ingest stores a file only when it hashes right against a passing run's pins on main, so an
artifact from a run that never landed is read and ignored. Downloading an artifact needs a token even on a public
repo: FILE holds one (a fine-grained token with read-only Actions and Attestations on this repo; --runs reads
attestations), readable by its owner only; without FILE, $GH_TOKEN, then `gh auth token`. The token is never printed.
"""
import base64
import functools
import glob
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile

REPO = "FieldmouseWorks/wend"
API = "https://api.github.com"
MAX_ZIP = 2 << 30  # an artifact is metadata, tens of MB: anything near this is wrong
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import archive  # noqa: E402


class Gone(RuntimeError):
    """404 or 410: not there, or expired."""


def token(path):
    if path:
        if os.stat(path).st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            sys.exit(f"{path}: readable by others than its owner; chmod 600 it first")
        return open(path).read().strip()
    if os.environ.get("GH_TOKEN"):
        return os.environ["GH_TOKEN"]
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
    except FileNotFoundError:
        r = None
    if r and r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    sys.exit("gate_fetched: no token (--token-file FILE, $GH_TOKEN, or a logged-in gh)")


class NoAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """The download redirects to blob storage, which must not see the token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            new.remove_header("Authorization")
        return new


def get(url, tok):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
                                               "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.build_opener(NoAuthOnRedirect).open(req, timeout=300) as r:
            data = r.read(MAX_ZIP + 1)
    except urllib.error.HTTPError as e:
        raise (Gone if e.code in (404, 410) else RuntimeError)(f"GET {url.split('?')[0]}: HTTP {e.code}") from None
    if len(data) > MAX_ZIP:
        raise RuntimeError(f"GET {url.split('?')[0]}: larger than {MAX_ZIP} bytes")
    return data


def artifacts(api, tok):
    page = 1
    while True:
        got = json.loads(get(f"{api}/repos/{REPO}/actions/artifacts?per_page=100&page={page}", tok))["artifacts"]
        yield from (a for a in got if a["name"].startswith("fetched-") and not a["expired"])
        if len(got) < 100:
            return
        page += 1


def unpack(data, dst):
    """Every regular file in the zip, under dst only."""
    tmp = f"{dst}.tmp{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        extract(data, tmp)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    os.makedirs(tmp, exist_ok=True)
    shutil.rmtree(dst, ignore_errors=True)  # a half-recorded earlier try
    os.replace(tmp, dst)


def extract(data, tmp):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for m in z.infolist():
            if m.is_dir():
                continue
            parts = m.filename.split("/")
            if m.filename.startswith("/") or any(p in ("", ".", "..") for p in parts):
                raise RuntimeError(f"refused member {m.filename!r}")
            os.makedirs(os.path.join(tmp, *parts[:-1]), exist_ok=True)
            with z.open(m) as src, open(os.path.join(tmp, *parts), "wb") as out:
                out.write(src.read())


def lacking(runs, have, lost):
    """Passing run files under runs; with have, only those no ingest has walked yet: some top file they pin isn't in
    that index with the run among its "runs" (lost.txt aside). Two runs often pin the same repomd.xml, and each still
    needs its own capture: it fetched its own packages."""
    try:
        files = json.load(open(have))["files"] if have else {}
    except FileNotFoundError:
        files = {}

    def walked(run, url, pin):
        d = archive.digest(*pin.partition(":")[::2])
        return not d or (url, d) in lost or \
            f"{run.get('box')}@{run.get('started')}" in files.get(url, {}).get(f"{d[0]}:{d[1]}", {}).get("runs", [])
    out = []
    for p in sorted(glob.glob(f"{runs}/**/*.json", recursive=True)):
        run = json.load(open(p))
        if run.get("result") == "pass" and not (have and all(walked(run, url, pin) for hop in run.get("hops", [])
                                                             for url, pin in hop.get("sources", {}).items())):
            out.append(p)
    return out


@functools.lru_cache(None)
def run_artifacts(api, tok, run_id):
    """A workflow run's unexpired artifacts."""
    got, page = [], 1
    while True:
        part = json.loads(get(f"{api}/repos/{REPO}/actions/runs/{run_id}/artifacts?per_page=100&page={page}",
                              tok))["artifacts"]
        got += part
        if len(part) < 100:
            break
        page += 1
    return [a for a in got if not a["expired"]]


def behind(api, tok, path):
    """The fetched-N artifacts of the gate job that wrote the run file at path, as [{id, url, size}]."""
    h = hashlib.sha256(open(path, "rb").read()).hexdigest()
    try:
        atts = json.loads(get(f"{api}/repos/{REPO}/attestations/sha256:{h}", tok))["attestations"]
    except Gone:
        atts = []  # a run from before the repo was public, when nothing was attested
    runs = set()
    for att in atts:
        try:
            st = json.loads(base64.b64decode(att["bundle"]["dsseEnvelope"]["payload"]))
            inv = st["predicate"]["runDetails"]["metadata"]["invocationId"]
        except (KeyError, TypeError, ValueError):
            continue
        m = re.match(rf"https://github\.com/{REPO}/actions/runs/(\d+)/", inv)
        if m:
            runs.add(int(m.group(1)))
    out = []
    for run_id in sorted(runs):
        arts = run_artifacts(api, tok, run_id)
        for a in arts:
            if not a["name"].startswith("run-"):
                continue
            try:
                with zipfile.ZipFile(io.BytesIO(get(a["archive_download_url"], tok))) as z:
                    mine = z.getinfo("run.json").file_size < 1 << 24 and \
                        hashlib.sha256(z.read("run.json")).hexdigest() == h
            except (Gone, KeyError, zipfile.BadZipFile):
                continue
            if mine:
                out += [{"id": b["id"], "url": b["archive_download_url"], "size": b.get("size_in_bytes", 0)}
                        for b in arts if b["name"] == "fetched-" + a["name"][4:]]
    return out


def wanted(api, tok, out, opts):
    """[{id, url, size}] to consider: every unexpired fetched-* artifact, or with --runs those behind the runs."""
    if "--runs" not in opts:
        return [{"id": a["id"], "url": a["archive_download_url"], "size": a.get("size_in_bytes", 0)}
                for a in artifacts(api, tok)]
    try:
        cache = json.load(open(f"{out}/.sources.json"))
    except FileNotFoundError:
        cache = {}
    lost = archive.read_lost(opts["--lost"]) if "--lost" in opts else set()
    runs = lacking(opts["--runs"], opts.get("--have"), lost)
    got, seen, none = [], set(), []
    for p in runs:
        h = hashlib.sha256(open(p, "rb").read()).hexdigest()
        if h not in cache:
            cache[h] = behind(api, tok, p)
        if not cache[h]:
            none.append(os.path.relpath(p, opts["--runs"]))
        for a in cache[h]:
            if a["id"] not in seen:
                seen.add(a["id"])
                got.append(a)
    if "--dry-run" not in opts:
        tmp = f"{out}/.sources.json.tmp{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump(cache, f, indent=1, sort_keys=True)
        os.replace(tmp, f"{out}/.sources.json")
    print(f"gate_fetched: {len(runs)} passing runs in {opts['--runs']}" +
          (" not yet in the store" if "--have" in opts else "") +
          f"; {len(runs) - len(none)} have a fetched artifact on GitHub")
    for r in none:
        print(f"  none    {r} (attested before capture, its artifact expired, or not attested)")
    return got


def main(argv):
    if not argv or argv[0].startswith("-"):
        print(__doc__)
        return 2
    out, rest, opts = argv[0], argv[1:], {}
    while rest:
        a = rest.pop(0)
        if a == "--dry-run":
            opts[a] = True
        elif a in ("--token-file", "--api", "--runs", "--have", "--lost") and rest:  # --api: a stand-in, for tests
            opts[a] = rest.pop(0)
        else:
            print(f"gate_fetched: {a!r} isn't an option, or has no value\n{__doc__}")
            return 2
    if ("--have" in opts or "--lost" in opts) and "--runs" not in opts:
        print(f"gate_fetched: --have and --lost need --runs\n{__doc__}")
        return 2
    tok = token(opts.get("--token-file"))
    api = opts.get("--api", API)
    os.makedirs(out, exist_ok=True)
    try:
        done = set(open(f"{out}/.done").read().split())
    except FileNotFoundError:
        done = set()
    n = size = 0
    try:
        todo = [a for a in wanted(api, tok, out, opts) if str(a["id"]) not in done]
        if "--dry-run" in opts:
            print(f"would download {len(todo)} fetched artifacts "
                  f"({sum(a['size'] for a in todo) / 1e9:.1f} GB zipped) into {out}")
            return 0
        with open(f"{out}/.done", "a") as log:
            for a in todo:
                try:
                    data = get(a["url"], tok)
                except Gone:
                    print(f"gate_fetched: artifact {a['id']} expired before it was downloaded")
                    continue
                unpack(data, f"{out}/{a['id']}")
                log.write(f"{a['id']}\n")
                log.flush()
                n, size = n + 1, size + len(data)
    except (RuntimeError, zipfile.BadZipFile, OSError) as e:
        print(f"gate_fetched: {e}")
        return 1
    print(f"unpacked {n} new fetched artifacts ({size / 1e6:.1f} MB) into {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
