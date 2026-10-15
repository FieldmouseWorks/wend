"""A small caching http -> https shim for old boxes, from dep-lab's centos/mirror.py.

CentOS 5's yum (python 2.4, 2007's openssl) can't speak today's TLS, and vault.centos.org redirects http to https.
Debian's archived releases come from a dated snapshot.debian.org tree, which answers with redirects that etch's and
lenny's apt (before 0.7.21) can't follow; this follows them.
A chroot shares the host's network, so the box points at http://127.0.0.1:<port>/<host>/<path> and this fetches
https://<host>/<path> once and serves it from disk after that. Only the hosts below are reachable through it.
Started by tools/sandbox.py when an adapter has a [mirror] table. Standard library only.

  python3 tools/mirror.py PORT CACHE_DIR

For the archive (docs/archive.md, "Capture"), every file it fetches is also hard-linked under CACHE_DIR/.sha256/<hex>,
so a live repo's repomd.xml that the next fetch replaces survives under its digest, and every file it serves is logged
to CACHE_DIR/.fetched.log as "<https url> sha256:<hex> <size>". tools/fetched.py reads that log.
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
           "dl.fedoraproject.org", "snapshot.debian.org"}
CACHE = sys.argv[2] if len(sys.argv) > 2 else "/var/tmp/wend/mirror"
STORE = os.path.join(CACHE, ".sha256")  # not reachable through the shim: a request's first segment must be a host
LOG = os.path.join(CACHE, ".fetched.log")
DIGESTS = {}  # (inode, mtime_ns, size) -> sha256 hex of a cached file, so a cache hit isn't rehashed every time
LOG_LOCK = threading.Lock()


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
        keep(tmp, h.hexdigest(), os.stat(tmp))
        os.replace(tmp, local)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def keep(path, hexdigest, st):
    """Remember a complete file's digest, and hard-link it under STORE by its sha256 (once). st is the stat of the
    bytes that were hashed: when path names another file by now (a refetch replaced it), nothing is linked.
    Capture never stops a file being served: a store or log that can't be written is skipped."""
    DIGESTS[(st.st_ino, st.st_mtime_ns, st.st_size)] = hexdigest
    try:
        if os.stat(path).st_ino != st.st_ino:
            return
        os.makedirs(STORE, exist_ok=True)
        os.link(path, os.path.join(STORE, hexdigest))
    except OSError:  # FileExistsError: already kept
        pass


def digest_of(f, local):
    """The sha256 of an open cached file, kept under STORE if it isn't yet (files cached before the store existed)."""
    st = os.fstat(f.fileno())
    hexdigest = DIGESTS.get((st.st_ino, st.st_mtime_ns, st.st_size))
    if hexdigest is None:
        h = hashlib.sha256()
        while chunk := f.read(1 << 20):
            h.update(chunk)
        f.seek(0)
        hexdigest = h.hexdigest()
        keep(local, hexdigest, st)
    return hexdigest, st.st_size


def log_served(url, hexdigest, size):
    line = f"{url} sha256:{hexdigest} {size}\n".encode()
    with LOG_LOCK:
        try:
            fd = os.open(LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        except OSError:
            return
        try:
            os.write(fd, line)
        except OSError:
            pass
        finally:
            os.close(fd)


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
    # keep-alive: apt pipelines its requests, and etch's apt turns an HTTP/1.0 close after each response into
    # retries that end in a corrupt file (MD5Sum mismatch). Every response below says its Content-Length.
    protocol_version = "HTTP/1.1"

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
        with open(local, "rb") as f:  # one open file: the bytes served are the bytes hashed and logged
            size = os.fstat(f.fileno()).st_size
            if body and not local.endswith(".index"):  # directory listings aren't archived
                log_served(f"https://{parts[0]}/{parts[1]}", *digest_of(f, local))
            r = byte_range(self.headers.get("Range", ""), size)  # anaconda (tools/vm.py) reads rpm headers this way
            if r == "unsatisfiable":
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if r:
                start, end = r
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(end - start + 1))
                self.end_headers()
                if body:
                    f.seek(start)
                    self.wfile.write(f.read(end - start + 1))
                return
            self.send_response(200)
            self.send_header("Content-Length", str(size))
            self.end_headers()
            if body:
                shutil.copyfileobj(f, self.wfile)

    def do_GET(self):
        self._go(True)

    def do_HEAD(self):
        self._go(False)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
