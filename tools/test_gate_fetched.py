"""Downloading the gate's fetched-N artifacts by behaviour (docs/archive.md, "CI captures, remi archives"): offline,
against a stand-in GitHub API. python3 tools/test_gate_fetched.py"""
import http.server
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import zipfile

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
w = tempfile.mkdtemp(prefix="wend-test-gate-fetched-")
results = []
TOKEN = "tok-never-printed"


def check(ok, what):
    results.append(ok)
    print(("passed  " if ok else "WRONG   ") + what)


def zipped(files):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return b.getvalue()


zips = {1: zipped({"fetched.txt": "https://h/x sha256:00 1\n", "blobs/00": "x"}),
        3: zipped({"fetched.txt": "y"}), 4: zipped({"../escape": "evil"})}
listing = [{"id": 1, "name": "fetched-0", "expired": False}, {"id": 2, "name": "run-0", "expired": False},
           {"id": 3, "name": "fetched-1", "expired": True}]
blob_auth = []


class GitHub(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        base = f"http://127.0.0.1:{self.server.server_address[1]}"
        if self.path.startswith("/repos/FieldmouseWorks/wend/actions/artifacts"):
            ok = self.headers.get("Authorization") == f"Bearer {TOKEN}"
            body = json.dumps({"artifacts": [{**a, "archive_download_url": f"{base}/dl/{a['id']}"}
                                             for a in listing]}).encode()
            self.send_response(200 if ok else 401)
        elif self.path.startswith("/dl/"):
            self.send_response(302)
            self.send_header("Location", f"{base}/blob/{self.path[4:]}")
            body = b""
        else:
            blob_auth.append(self.headers.get("Authorization"))
            body = zips[int(self.path.split("/")[-1])]
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), GitHub)
threading.Thread(target=srv.serve_forever, daemon=True).start()
api = f"http://127.0.0.1:{srv.server_address[1]}"
tok = f"{w}/token"
open(tok, "w").write(TOKEN + "\n")
os.chmod(tok, 0o644)
run = lambda: subprocess.run([sys.executable, "-B", f"{HERE}/gate_fetched.py", f"{w}/out", "--token-file", tok,
                              "--api", api], capture_output=True, text=True)
p = run()
check(p.returncode != 0 and "chmod 600" in p.stdout + p.stderr and TOKEN not in p.stdout + p.stderr,
      "a token file others can read is refused")
os.chmod(tok, 0o600)
p = run()
check(p.returncode == 0 and "unpacked 1 new" in p.stdout and
      open(f"{w}/out/1/fetched.txt").read().startswith("https://h/x") and os.path.isfile(f"{w}/out/1/blobs/00") and
      not os.path.exists(f"{w}/out/2") and not os.path.exists(f"{w}/out/3"),
      "only unexpired fetched-* artifacts are unpacked, each under its id")
check(blob_auth == [None], "the token never follows the redirect to blob storage")
p = run()
check(p.returncode == 0 and "unpacked 0 new" in p.stdout, "an artifact already unpacked isn't fetched again")
listing.append({"id": 4, "name": "fetched-2", "expired": False})
p = run()
check(p.returncode == 1 and "refused member" in p.stdout and not os.path.exists(f"{w}/escape") and
      not [f for f in os.listdir(f"{w}/out") if f.startswith("4")] and "4" not in open(f"{w}/out/.done").read().split(),
      "a zip member that climbs out of its directory is refused, and nothing of it is kept")
check(TOKEN not in p.stdout + p.stderr, "the token is never printed")
# --on-main: only artifacts whose gate run tested a commit main has; the rest wait, unrecorded
repo = f"{w}/repo"
g = lambda *a: subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True,
                              capture_output=True, text=True).stdout.strip()
os.makedirs(repo)
g("init", "-q")
g("commit", "-q", "--allow-empty", "-m", "merged head")
merged = g("rev-parse", "HEAD")
listing[:] = [{"id": 5, "name": "fetched-0", "expired": False, "workflow_run": {"head_sha": merged}},
              {"id": 6, "name": "fetched-0", "expired": False, "workflow_run": {"head_sha": "9" * 40}}]
zips[5] = zips[6] = zipped({"fetched.txt": "z"})
p = subprocess.run([sys.executable, "-B", f"{HERE}/gate_fetched.py", f"{w}/out2", "--token-file", tok, "--api", api,
                    "--on-main", repo], capture_output=True, text=True)
check(p.returncode == 0 and os.path.isdir(f"{w}/out2/5") and not os.path.exists(f"{w}/out2/6") and
      "1 from commits not on main left for later" in p.stdout and open(f"{w}/out2/.done").read().split() == ["5"],
      "--on-main takes only artifacts of commits main has, and leaves the rest unrecorded for a later run")
srv.shutdown()
subprocess.run(["rm", "-rf", w])
sys.exit(0 if all(results) else 1)
