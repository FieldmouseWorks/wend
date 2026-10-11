"""A small caching http -> https shim for EL boxes, from dep-lab's centos/mirror.py.

CentOS 5's yum (python 2.4, 2007's openssl) can't speak today's TLS, and vault.centos.org redirects http to https.
A chroot shares the host's network, so the box points at http://127.0.0.1:<port>/<host>/<path> and this fetches
https://<host>/<path> once and serves it from disk after that. Only the hosts below are reachable through it.
Started by tools/sandbox.py when an adapter has a [mirror] table. Standard library only.

  python3 tools/mirror.py PORT CACHE_DIR
"""
import http.server
import os
import shutil
import sys
import urllib.error
import urllib.request

ALLOWED = {"vault.centos.org", "repo.almalinux.org", "vault.almalinux.org", "archives.fedoraproject.org"}
CACHE = sys.argv[2] if len(sys.argv) > 2 else "/var/tmp/wend/mirror"


class H(http.server.BaseHTTPRequestHandler):
    def _go(self, body):
        parts = self.path.lstrip("/").split("?")[0].split("/", 1)
        if len(parts) < 2 or parts[0] not in ALLOWED or ".." in parts[1]:
            return self.send_error(403)
        local = os.path.join(CACHE, parts[0], parts[1])
        if local.endswith("/"):
            local += ".index"
        # Live mirrors change their metadata; package files never change, and neither do repodata files named by
        # their own hash (refetching those every time meant 170MB per dnf run, which timed out under load).
        name = os.path.basename(local)
        head = name.split("-", 1)[0]
        hashed = len(head) >= 40 and all(c in "0123456789abcdef" for c in head)
        volatile = ("/repodata/" in parts[1] and "vault" not in parts[0] and not hashed) or local.endswith(".index")
        if not os.path.exists(local) or volatile:
            try:
                with urllib.request.urlopen(f"https://{parts[0]}/{parts[1]}", timeout=300) as r:
                    os.makedirs(os.path.dirname(local), exist_ok=True)
                    tmp = f"{local}.tmp{os.getpid()}-{id(self)}"
                    with open(tmp, "wb") as f:
                        shutil.copyfileobj(r, f)
                    os.replace(tmp, local)
            except Exception as e:
                if not os.path.exists(local):  # a live mirror hiccup: serve the last good copy if there is one
                    return self.send_error(e.code if isinstance(e, urllib.error.HTTPError) else 502, str(e)[:100])
        self.send_response(200)
        self.send_header("Content-Length", str(os.path.getsize(local)))
        self.end_headers()
        if body:
            with open(local, "rb") as f:
                shutil.copyfileobj(f, self.wfile)

    def do_GET(self):
        self._go(True)

    def do_HEAD(self):
        self._go(False)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
