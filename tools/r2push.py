"""Push the archive's store to its public copy (docs/archive.md, step 5). Runs on remi. Standard library only.

  python3 tools/r2push.py STORE --endpoint URL --bucket NAME --credentials FILE

STORE is what tools/archive.py ingest writes. Every blob and alias goes up first, then the stamped index, then
index/latest.json, each index just after its .sig when tools/archive.py sign made one, so the index R2 serves never
names a blob R2 doesn't have. Each PUT carries the body's sha256 in x-amz-content-sha256, and R2 refuses a body that
doesn't hash to it, so what lands is checked on the way in. What's already up is skipped: STORE/.pushed lists every
key pushed (a blob or alias never changes under its key; a stamped index or signature is listed with its sha256, so
one rewritten in place goes up again). It holds the store's lock (STORE/.lock, the one ingest takes), so an ingest
can't add blobs under an index mid-push.

A package whose source the store doesn't hold (its index entry says "source": {"missing": ...}) is held back, and so is
an alias naming it: the archive is public, and a GPL binary goes out only with its source. Its index entry still names
it, so the index says what's held; it goes up with the first push after an ingest stores its source.

The endpoint is the account's S3 API endpoint (https://<account>.r2.cloudflarestorage.com). FILE holds two lines,
access_key_id=... and secret_access_key=... (an R2 API token with Object Read & Write on this bucket). It should be
readable by its owner only; this refuses one that isn't. Neither value is ever printed or logged, and errors name the
key, never a header.
"""
import datetime
import fcntl
import glob
import hashlib
import hmac
import json
import os
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REGION, SERVICE = "auto", "s3"


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def signed_headers(method, url, headers, payload_hash, access_key, secret_key, now=None, region=REGION):
    """AWS Signature Version 4 for one request: the headers to send, Authorization included. `headers` must not
    include host, x-amz-date or x-amz-content-sha256; they're added and signed with the rest."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    amz_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    u = urllib.parse.urlsplit(url)
    h = {k.lower(): str(v).strip() for k, v in headers.items()}
    h.update({"host": u.netloc, "x-amz-date": amz_date, "x-amz-content-sha256": payload_hash})
    names = sorted(h)
    query = "&".join(f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}"
                     for k, v in sorted(urllib.parse.parse_qsl(u.query, keep_blank_values=True)))
    canonical = "\n".join([method, urllib.parse.quote(u.path or "/", safe="/-_.~"), query,
                           "".join(f"{k}:{h[k]}\n" for k in names), ";".join(names), payload_hash])
    scope = f"{day}/{region}/{SERVICE}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = f"AWS4{secret_key}".encode()
    for part in (day, region, SERVICE, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    h["authorization"] = (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={';'.join(names)}, "
                          f"Signature={signature}")
    del h["host"]  # urllib sets it from the url
    return h


def credentials(path):
    st = os.stat(path)
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        sys.exit(f"{path}: readable by others than its owner; chmod 600 it first")
    creds = {}
    for line in open(path):
        k, eq, v = line.strip().partition("=")
        if eq:
            creds[k.strip()] = v.strip()
    if not creds.get("access_key_id") or not creds.get("secret_access_key"):
        sys.exit(f"{path}: needs access_key_id=... and secret_access_key=... lines")
    return creds["access_key_id"], creds["secret_access_key"]


class Bucket:
    def __init__(self, endpoint, bucket, access_key, secret_key):
        self.base = f"{endpoint.rstrip('/')}/{bucket}"
        self.access_key, self.secret_key = access_key, secret_key

    def put(self, key, path, content_type):
        """PUT one file under key, R2 checking its sha256. Tries three times; raises with the key on failure."""
        payload = sha256_file(path)
        url = f"{self.base}/{key}"
        for attempt in range(3):
            size = os.path.getsize(path)
            # a blob or alias never changes under its key; an index or its signature changes on every push
            cache = "no-cache" if key.startswith("index/") else "public, max-age=31536000, immutable"
            h = signed_headers("PUT", url, {"content-type": content_type, "content-length": size,
                                            "cache-control": cache}, payload, self.access_key, self.secret_key)
            try:
                with open(path, "rb") as body:
                    req = urllib.request.Request(url, data=body, method="PUT", headers=h)
                    with urllib.request.urlopen(req, timeout=600) as r:
                        r.read()
                return
            except urllib.error.HTTPError as e:
                if e.code < 500 or attempt == 2:
                    raise RuntimeError(f"PUT {key}: HTTP {e.code}") from None
            except OSError as e:
                if attempt == 2:
                    raise RuntimeError(f"PUT {key}: {type(e).__name__}") from None
            time.sleep(2 ** attempt)


def mark(store, path):
    """What .pushed records for a stamped index: its key and its sha256, so a rewritten one goes up again."""
    return f"{os.path.relpath(path, store)}@{sha256_file(path)}"


def key(entry):
    return entry.partition("@")[0]


PACKAGES = (".rpm", ".deb", ".udeb")


def held(store):
    """sha256 hexes of the blobs to hold back: every index entry for that digest is a package with no source."""
    try:
        files = json.load(open(f"{store}/index/latest.json")).get("files", {})
    except FileNotFoundError:
        return set()
    sourceless, sourced = set(), set()
    for url, digests in files.items():
        for d, e in digests.items():
            if url.endswith(PACKAGES) and not url.endswith(".src.rpm") and "missing" in e.get("source", {}):
                sourceless.add(d[7:])
            else:
                sourced.add(d[7:])
    return sourceless - sourced


def pending(store, pushed):
    """(.pushed entry, path, content type) still to push: blobs and aliases first, then the stamped indexes, then
    latest.json. Sourceless packages (held()) and their aliases aren't among them."""
    out, hold = [], held(store)
    for p in sorted(glob.glob(f"{store}/blob/sha256/*/*")):
        if ".tmp" not in os.path.basename(p) and os.path.basename(p) not in hold:  # archive.py writes <blob>.tmp<pid>
            out.append((os.path.relpath(p, store), p, "application/octet-stream"))
    for p in sorted(glob.glob(f"{store}/alias/*/*")):
        if ".tmp" not in os.path.basename(p) and open(p).read().strip()[7:] not in hold:
            out.append((os.path.relpath(p, store), p, "text/plain"))
    out = [x for x in out if x[0] not in pushed]
    # a stamped index is only seconds-unique (two ingests in one second rewrite it), so it's skipped by key and content
    # each index's signature (tools/archive.py sign) goes up just before it
    stamped = sorted(p for p in glob.glob(f"{store}/index/*.json") if not p.endswith("/latest.json"))
    for p in stamped:
        for f, ctype in ((f"{p}.sig", "text/plain"), (p, "application/json")):
            if os.path.isfile(f) and mark(store, f) not in pushed:
                out.append((mark(store, f), f, ctype))
    for f, ctype in ((f"{store}/index/latest.json.sig", "text/plain"), (f"{store}/index/latest.json", "application/json")):
        if os.path.exists(f):
            out.append((os.path.relpath(f, store), f, ctype))  # always: they change
    return out


def push(store, bucket):
    try:
        pushed = set(open(f"{store}/.pushed").read().split())
    except FileNotFoundError:
        pushed = set()
    todo = pending(store, pushed)
    hold = held(store)
    if hold:
        print(f"holding back {len(hold)} packages whose source the store doesn't have yet")
    done = size = 0
    with open(f"{store}/.pushed", "a") as log:
        for entry, path, ctype in todo:
            bucket.put(key(entry), path, ctype)
            if not entry.startswith("index/latest.json"):
                log.write(entry + "\n")
                log.flush()
            done += 1
            size += os.path.getsize(path)
    print(f"pushed {done} files ({size / 1e6:.1f} MB)")


def main(argv):
    if not argv or argv[0].startswith("-"):
        print(__doc__)
        return 2
    store, opts, rest = argv[0], {}, argv[1:]
    while rest:
        a = rest.pop(0)
        if a in ("--endpoint", "--bucket", "--credentials") and rest:
            opts[a] = rest.pop(0)
        else:
            print(f"r2push: {a!r} isn't an option, or has no value\n{__doc__}")
            return 2
    if set(opts) != {"--endpoint", "--bucket", "--credentials"}:
        print(__doc__)
        return 2
    if not opts["--endpoint"].startswith("https://"):
        print("r2push: the endpoint must be https")
        return 2
    access_key, secret_key = credentials(opts["--credentials"])
    lock = open(f"{store}/.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("r2push: an ingest or another push is running on this store")
        return 2
    try:
        push(store, Bucket(opts["--endpoint"], opts["--bucket"], access_key, secret_key))
    except RuntimeError as e:
        print(f"r2push: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
