"""A small caching http -> https shim for EL boxes, from dep-lab's centos/mirror.py.

CentOS 5's yum (python 2.4, 2007's openssl) can't speak today's TLS, and vault.centos.org redirects http to https.
A chroot shares the host's network, so the box points at http://127.0.0.1:<port>/<host>/<path> and this fetches
https://<host>/<path> once and serves it from disk after that. Only the hosts below are reachable through it.
Started by tools/sandbox.py when an adapter has a [mirror] table. Standard library only.

  python3 tools/mirror.py PORT CACHE_DIR
"""
import hashlib
import http.server
import os
import re
import shutil
import sys
import threading
import urllib.error
import urllib.request

ALLOWED = {"vault.centos.org", "repo.almalinux.org", "vault.almalinux.org", "archives.fedoraproject.org",
           "dl.fedoraproject.org"}
CACHE = sys.argv[2] if len(sys.argv) > 2 else "/var/tmp/wend/mirror"


def fetch(url, local, sha256=None):
    """Download url to local, atomically. Never keep a cut-off download: a hash-named file is served from disk
    forever after, so a short read (or, for those, a wrong sha256) raises and leaves nothing behind."""
    os.makedirs(os.path.dirname(local), exist_ok=True)
    tmp = f"{local}.tmp{os.getpid()}-{threading.get_ident()}"
    try:
        with urllib.request.urlopen(url, timeout=300) as r:
            size, h = 0, hashlib.sha256()
            with open(tmp, "wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
                    h.update(chunk)
                    size += len(chunk)
            want = r.headers.get("Content-Length")
        if want is not None and int(want) != size:
            raise OSError(f"short read: {size} of {want} bytes")
        if sha256 and h.hexdigest() != sha256:
            raise OSError("sha256 does not match the file name")
        os.replace(tmp, local)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def byte_range(header, size):
    """A single bytes range (RFC 9110) as (start, end), "unsatisfiable", or None to serve the whole file (no header,
    several ranges, another unit, or a header that isn't a valid range)."""
    if not header.startswith("bytes=") or "," in header:
        return None
    a, dash, b = header[6:].strip().partition("-")
    if not dash or not (a or b) or not re.fullmatch(r"[0-9]+", a + b):
        return None
    if a:
        start, end = int(a), min(int(b), size - 1) if b else size - 1
        if b and int(b) < start:
            return None
    elif int(b) == 0:
        return "unsatisfiable"
    else:
        start, end = max(size - int(b), 0), size - 1
    return "unsatisfiable" if start >= size else (start, end)


class H(http.server.BaseHTTPRequestHandler):
    def _go(self, body):
        parts = self.path.lstrip("/").split("?")[0].split("/", 1)
        # Only <allowed host>/<relative path>: no empty or dot segments, so the path can't leave the cache
        # (os.path.join would throw CACHE away for "/etc/shadow"; the shim runs as root).
        if len(parts) < 2 or parts[0] not in ALLOWED or \
                any(seg in ("", ".", "..") for seg in parts[1].rstrip("/").split("/")):
            return self.send_error(403)
        local = os.path.join(CACHE, parts[0], parts[1])
        if os.path.commonpath([os.path.realpath(CACHE), os.path.realpath(local)]) != os.path.realpath(CACHE):
            return self.send_error(403)
        if local.endswith("/"):
            local += ".index"
        # Live mirrors change their metadata; package files never change, and neither do repodata files named by
        # their own hash (refetching those every time meant 170MB per dnf run, which timed out under load).
        name = os.path.basename(local)
        head = name.split("-", 1)[0]
        hashed = len(head) >= 40 and all(c in "0123456789abcdef" for c in head)
        volatile = ("/repodata/" in parts[1] and "vault" not in parts[0] and not hashed) or local.endswith(".index")
        if not os.path.exists(local) or volatile:
            err = None
            for attempt in range(3):  # transfers through a busy proxy get cut off now and then
                try:
                    fetch(f"https://{parts[0]}/{parts[1]}", local, head if hashed and len(head) == 64 else None)
                    err = None
                    break
                except Exception as e:
                    err = e
                    if isinstance(e, urllib.error.HTTPError) and e.code < 500:
                        break
            if err and not os.path.exists(local):  # a live mirror hiccup: serve the last good copy if there is one
                return self.send_error(err.code if isinstance(err, urllib.error.HTTPError) else 502, str(err)[:100])
        size = os.path.getsize(local)
        r = byte_range(self.headers.get("Range", ""), size)  # anaconda (tools/vm.py) reads rpm headers this way
        if r == "unsatisfiable":
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return
        if r:
            start, end = r
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            if body:
                with open(local, "rb") as f:
                    f.seek(start)
                    self.wfile.write(f.read(end - start + 1))
            return
        self.send_response(200)
        self.send_header("Content-Length", str(size))
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
