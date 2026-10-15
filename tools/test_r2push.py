"""The archive's push to R2 by behaviour (docs/archive.md, step 5): offline, against a stand-in S3 endpoint that, like
R2, refuses a body that doesn't hash to its x-amz-content-sha256. The signer is checked against AWS's own SigV4
example. python3 tools/test_r2push.py"""
import datetime
import hashlib
import http.server
import os
import socket
import subprocess
import sys
import tempfile
import threading

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import r2push  # noqa: E402

w = tempfile.mkdtemp(prefix="wend-test-r2push-")
results = []
SECRET = "s3cr3t-never-printed"


def check(ok, what):
    results.append(ok)
    print(("passed  " if ok else "WRONG   ") + what)


h = r2push.signed_headers("GET", "https://examplebucket.s3.amazonaws.com/test.txt", {"Range": "bytes=0-9"},
                          hashlib.sha256(b"").hexdigest(), "AKIAIOSFODNN7EXAMPLE",
                          "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                          datetime.datetime(2013, 5, 24, tzinfo=datetime.timezone.utc), "us-east-1")
check(h["authorization"].endswith("Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"),
      "the signer reproduces AWS's SigV4 example (GET Object)")

got, fail = [], set()


class S3(http.server.BaseHTTPRequestHandler):
    def do_PUT(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        key = self.path.split("/", 2)[2]
        ok = (hashlib.sha256(body).hexdigest() == self.headers["x-amz-content-sha256"] and
              self.headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKID/") and key not in fail)
        self.send_response(200 if ok else 400)
        self.send_header("Content-Length", "0")
        self.end_headers()
        if ok:
            got.append((key, body))

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), S3)
threading.Thread(target=srv.serve_forever, daemon=True).start()
store = f"{w}/store"
files = {"blob/sha256/ab/" + "ab" * 32: b"blob one", "blob/sha256/cd/" + "cd" * 32: b"blob two",
         "blob/sha256/cd/" + "cd" * 32 + ".tmp123": b"half written", "alias/sha1/" + "ef" * 20: b"sha256:" + b"ab" * 32,
         "index/20261015-000000.json": b"{}", "index/latest.json": b"{}"}
for rel, data in files.items():
    os.makedirs(os.path.dirname(f"{store}/{rel}"), exist_ok=True)
    open(f"{store}/{rel}", "wb").write(data)
creds = f"{w}/creds"
open(creds, "w").write(f"access_key_id=AKID\nsecret_access_key={SECRET}\n")
os.chmod(creds, 0o644)
# the stand-in speaks http; r2push insists on https, so drive Bucket directly for the transfer checks
run = lambda *extra: subprocess.run([sys.executable, "-B", f"{HERE}/r2push.py", store, "--endpoint",
                                     "https://example.invalid", "--bucket", "b", "--credentials", creds, *extra],
                                    capture_output=True, text=True)
p = run()
check(p.returncode != 0 and "chmod 600" in p.stdout + p.stderr and SECRET not in p.stdout + p.stderr,
      "a credentials file others can read is refused")
os.chmod(creds, 0o600)
p = subprocess.run([sys.executable, "-B", f"{HERE}/r2push.py", store, "--endpoint", "http://127.0.0.1:1", "--bucket",
                    "b", "--credentials", creds], capture_output=True, text=True)
check(p.returncode == 2 and "https" in p.stdout, "an endpoint that isn't https is refused")
bucket = r2push.Bucket(f"http://127.0.0.1:{srv.server_address[1]}", "wend-archive", *r2push.credentials(creds))
r2push.push(store, bucket)
keys = [k for k, _ in got]
check(sorted(keys[:3]) == sorted(k for k in files if k.startswith(("blob", "alias")) and ".tmp" not in k) and
      keys[3:] == ["index/20261015-000000.json", "index/latest.json"],
      "blobs and aliases go up first, then the stamped index, latest.json last; half-written files never")
check(all(body == files[k] for k, body in got), "each file lands byte for byte")
got.clear()
r2push.push(store, bucket)
check([k for k, _ in got] == ["index/latest.json"], "a second push sends only latest.json")
os.makedirs(f"{store}/blob/sha256/12")
open(f"{store}/blob/sha256/12/" + "12" * 32, "wb").write(b"new")
fail.add("blob/sha256/12/" + "12" * 32)
got.clear()
try:
    r2push.push(store, bucket)
    check(False, "a refused upload stops the push before the index")
except RuntimeError as e:
    check("blob/sha256/12/" in str(e) and SECRET not in str(e) and got == [],
          "a refused upload stops the push before the index, naming the key and never the secret")
srv.shutdown()
subprocess.run(["rm", "-rf", w])
sys.exit(0 if all(results) else 1)
