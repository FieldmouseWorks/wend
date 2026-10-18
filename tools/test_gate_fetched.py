"""Downloading the gate's fetched-N artifacts by behaviour (docs/archive.md, "CI captures, remi archives"): offline,
against a stand-in GitHub API. python3 tools/test_gate_fetched.py"""
import base64
import hashlib
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
attest, run_arts, asked = {}, {}, []


class GitHub(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        base = f"http://127.0.0.1:{self.server.server_address[1]}"
        asked.append(self.path)
        if self.path.startswith("/repos/FieldmouseWorks/wend/attestations/sha256:"):
            h = self.path.rsplit(":", 1)[1]
            body = json.dumps({"attestations": attest.get(h, [])}).encode()
            self.send_response(200 if h in attest else 404)
        elif self.path.startswith("/repos/FieldmouseWorks/wend/actions/runs/"):
            arts = run_arts[int(self.path.split("/")[6])]
            body = json.dumps({"artifacts": [{**a, "archive_download_url": f"{base}/dl/{a['id']}"}
                                             for a in arts]}).encode()
            self.send_response(200)
        elif self.path.startswith("/repos/FieldmouseWorks/wend/actions/artifacts"):
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
# --runs: only the artifacts behind main's passing runs, found through each run file's attestation; with --have only
# those of runs pinning a top file the store doesn't hold, lost.txt aside
runs = f"{w}/runs"
os.makedirs(f"{runs}/r")
U = "https://repo.example.org/9/repodata/repomd.xml"
files = {}
for name, h, result in (("held", "1", "pass"), ("new", "2", "pass"), ("lost", "3", "pass"), ("failed", "4", "fail"),
                        ("unattested", "5", "pass")):
    files[name] = json.dumps({"result": result, "box": name, "hops": [{"sources": {U: "sha256:" + h * 64}}]}).encode()
    open(f"{runs}/r/{name}.json", "wb").write(files[name])
sha = {k: hashlib.sha256(v).hexdigest() for k, v in files.items()}
json.dump({"files": {U: {"sha256:" + "1" * 64: {"size": 1, "runs": ["held@None"]},
                         "sha256:" + "2" * 64: {"size": 1, "runs": ["another@run"]}}}}, open(f"{w}/latest.json", "w"))
open(f"{w}/lost.txt", "w").write(f"{U} sha256:{'3' * 64}  # gone\n")


def att(inv):
    st = {"predicate": {"runDetails": {"metadata": {"invocationId": inv}}}}
    return {"bundle": {"dsseEnvelope": {"payload": base64.b64encode(json.dumps(st).encode()).decode()}}}


attest[sha["new"]] = [att("https://github.com/FieldmouseWorks/wend/actions/runs/77/attempts/1"),
                      att("https://github.com/someone/else/actions/runs/88/attempts/1")]
attest[sha["held"]] = [att("https://github.com/FieldmouseWorks/wend/actions/runs/78/attempts/1")]
run_arts[77] = [{"id": 20, "name": "run-0", "expired": False}, {"id": 21, "name": "run-1", "expired": False},
                {"id": 10, "name": "fetched-0", "expired": False, "size_in_bytes": 5},
                {"id": 11, "name": "fetched-1", "expired": False, "size_in_bytes": 7},
                {"id": 12, "name": "fetched-1", "expired": True}]
run_arts[78] = [{"id": 22, "name": "run-0", "expired": False}, {"id": 13, "name": "fetched-0", "expired": False}]
run_arts[88] = []
zips.update({20: zipped({"run.json": b"another job's run"}), 21: zipped({"run.json": files["new"]}),
             22: zipped({"run.json": files["held"]}), 10: zipped({"fetched.txt": "x"}),
             11: zipped({"fetched.txt": "n"}), 13: zipped({"fetched.txt": "h"})})
gf = lambda out, *a: subprocess.run([sys.executable, "-B", f"{HERE}/gate_fetched.py", f"{w}/{out}", "--token-file", tok,
                                     "--api", api, "--runs", runs, *a], capture_output=True, text=True)
have = ("--have", f"{w}/latest.json", "--lost", f"{w}/lost.txt")
asked.clear()
p = gf("out2", *have, "--dry-run")
check(p.returncode == 0 and "would download 1 fetched artifacts" in p.stdout and not os.path.exists(f"{w}/out2/11") and
      not os.path.exists(f"{w}/out2/.sources.json") and not os.path.exists(f"{w}/out2/.done"),
      "--dry-run says what it would download, and downloads and records nothing")
check(not [q for q in asked if sha["held"] in q or sha["lost"] in q or sha["failed"] in q] and
      not [q for q in asked if "/runs/88/" in q],
      "a held pin, a lost one and a failed run need no artifact, and another repo's workflow run is never asked about")
p = gf("out2", *have)
check(p.returncode == 0 and os.path.isdir(f"{w}/out2/11") and open(f"{w}/out2/.done").read().split() == ["11"] and
      "unattested.json" in p.stdout and "1 have a fetched artifact" in p.stdout,
      "--have: only the fetched-N whose run-N is the unwalked run byte for byte (its repomd stored for another run "
      "isn't enough), and a run without one is named")
asked.clear()
p = gf("out2", *have)
check(p.returncode == 0 and "unpacked 0 new" in p.stdout and not [q for q in asked if "attestations" in q],
      "each run's lookup is remembered, so a later cycle asks GitHub nothing about it")
p = gf("out3")
check(p.returncode == 0 and sorted(open(f"{w}/out3/.done").read().split()) == ["11", "13"],
      "without --have, every passing run's artifact is taken")
p = subprocess.run([sys.executable, "-B", f"{HERE}/gate_fetched.py", f"{w}/out4", "--token-file", tok, "--api", api,
                    "--have", f"{w}/latest.json"], capture_output=True, text=True)
check(p.returncode == 2 and "need --runs" in p.stdout, "--have without --runs is refused")
srv.shutdown()
subprocess.run(["rm", "-rf", w])
sys.exit(0 if all(results) else 1)
