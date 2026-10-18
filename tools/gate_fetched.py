"""Download the gate's fetched-N artifacts for the archive (docs/archive.md, "CI captures, remi archives"). Runs on
remi. Standard library only.

  python3 tools/gate_fetched.py DIR [--token-file FILE] [--on-main REPO]

Each gate replay uploads what it downloaded as an artifact named fetched-N: its download list and the metadata's bytes,
which live repos replace within days. GitHub keeps them 90 days. This lists the repo's unexpired fetched-* artifacts
and unpacks each one not yet seen into DIR/<artifact id>/, recording it in DIR/.done. Then:

  python3 tools/archive.py ingest STORE --fetched DIR

With --on-main REPO, an artifact is downloaded only when the commit its gate run tested is in REPO's history (an
ancestor of its HEAD, a checkout of main): a run lands on main only from a PR head that merged, so the rest (pushes that
were superseded, PRs still open) are left for now, unrecorded, and downloaded on a later run once they've merged. A
head that was force-pushed away never lands; if one somehow did, tools/archive_check.py reports its pins.

Nothing here is trusted: ingest stores a file only when it hashes right against a passing run's pins on main, so an
artifact from a run that never landed is read and ignored. Downloading an artifact needs a token even on a public repo:
FILE holds one (a fine-grained token with read-only Actions on this repo), readable by its owner only; without FILE,
$GH_TOKEN, then `gh auth token`. The token is never printed.
"""
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
        raise RuntimeError(f"GET {url.split('?')[0]}: HTTP {e.code}") from None
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


def on_main(repo, sha):
    """Is sha in repo's history? An unknown commit (a PR head that never merged) isn't."""
    if not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
        return False
    return subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", sha, "HEAD"],
                          capture_output=True).returncode == 0


def main(argv):
    if not argv or argv[0].startswith("-"):
        print(__doc__)
        return 2
    out, rest, opts = argv[0], argv[1:], {}
    while rest:
        a = rest.pop(0)
        if a in ("--token-file", "--api", "--on-main") and rest:  # --api: a stand-in GitHub, for tests
            opts[a] = rest.pop(0)
        else:
            print(f"gate_fetched: {a!r} isn't an option, or has no value\n{__doc__}")
            return 2
    tok = token(opts.get("--token-file"))
    os.makedirs(out, exist_ok=True)
    try:
        done = set(open(f"{out}/.done").read().split())
    except FileNotFoundError:
        done = set()
    n = size = later = 0
    try:
        with open(f"{out}/.done", "a") as log:
            for a in artifacts(opts.get("--api", API), tok):
                if str(a["id"]) in done:
                    continue
                if "--on-main" in opts and not on_main(opts["--on-main"], a.get("workflow_run", {}).get("head_sha")):
                    later += 1
                    continue
                data = get(a["archive_download_url"], tok)
                unpack(data, f"{out}/{a['id']}")
                log.write(f"{a['id']}\n")
                log.flush()
                n, size = n + 1, size + len(data)
    except (RuntimeError, zipfile.BadZipFile, OSError) as e:
        print(f"gate_fetched: {e}")
        return 1
    print(f"unpacked {n} new fetched artifacts ({size / 1e6:.1f} MB) into {out}" +
          (f"; {later} from commits not on main left for later" if later else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
