"""The download list a replay leaves for the archive (docs/archive.md, "Capture").

The mirror shim (tools/mirror.py) logs every file it serves and keeps every file it fetched under its sha256. A walk
notes where the log ends before it starts (mark) and, once its run is saved, writes what came after (collect) next to
the run file, in <run>.fetched/:

  fetched.txt         one "<https url> sha256:<hex> <size>" per distinct file, sorted
  blobs/<hex>         the bytes of every metadata file in it (anything that isn't a package), and of every package
                      from a repo that keeps only each package's latest build (live EPEL). Live repos replace their
                      metadata daily, so these are taken now; other packages last (AlmaLinux keeps every version of
                      a minor release, then moves it to its vault whole), and the archive fetches them later by url
                      and digest.

A replay asks only for the indexes its own package manager needed, but a live walk of the same run on a real machine
(`wend rehearse`, `wend walk --run`) can ask for more: filelists for a file dependency, updateinfo. A live repo
deletes those with the repomd.xml that lists them, so for every live repo's repomd.xml in the list, collect also
fetches, from the origin and checked against that repomd, every index it lists but `other` (changelogs) and deltas,
and lists and keeps them like the rest. One the origin no longer serves goes in missing.txt.

The shim is shared, so a walk that runs beside another (`wend verify` runs several, remi a VM run) also lists the
other's files, an apt walk included, and a VM's installer images with them. That's harmless: the archive keeps only
what a landed run references, and checks every byte against its pins. An apt walk alone leaves nothing yet.
An existing <run>.fetched/ is replaced, never added to: a checkout could carry one, symlinks and all.
"""
import hashlib
import os
import shutil
import urllib.request

import archive

import sandbox

PACKAGES = (".rpm", ".deb", ".udeb")
LATEST_ONLY = ("https://dl.fedoraproject.org/pub/epel/",)  # live repos whose packages vanish when updated
# live repos that delete a repomd.xml's indexes with it (vaults and archives keep theirs)
LIVE = ("https://repo.almalinux.org/", "https://dl.fedoraproject.org/pub/epel/", "https://nginx.org/packages/")


def cache():
    return f"{sandbox.WORK}/mirror"


def mark():
    """Where the shim's log ends now."""
    try:
        return os.path.getsize(f"{cache()}/.fetched.log")
    except FileNotFoundError:
        return 0


def origin(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "wend-capture"}), timeout=300) as r:
        return r.read()


def indexes(seen, out, missing):
    """Every index a live repo's captured repomd.xml lists, but changelogs and deltas, fetched from the origin and
    checked against the repomd: (url, "sha256:<hex>", size) for each one kept in out/blobs."""
    have, added = {u for u, _, _ in seen}, set()
    for url, digest, _ in sorted(seen):
        if not (url.endswith("/repodata/repomd.xml") and url.startswith(LIVE)):
            continue
        try:
            text = open(f"{out}/blobs/{digest[7:]}", "rb").read().decode("utf-8", "replace")
        except FileNotFoundError:
            continue
        base = url[:url.index("repodata/")]
        for href, (alg, want) in archive.repomd_entries(text):
            u = base + href
            if u in have or archive.NOT_WALKED.search(u.rsplit("/", 1)[1]):
                continue
            have.add(u)
            try:
                data = origin(u)
            except Exception:  # urllib's HTTPError, a timeout: gone already
                missing.append(u)
                continue
            if archive.hexof(alg, data) != want:
                missing.append(u)
                continue
            h = hashlib.sha256(data).hexdigest()
            with open(f"{out}/blobs/{h}", "wb") as f:
                f.write(data)
            added.add((u, f"sha256:{h}", str(len(data))))
    return added


def collect(since, out):
    """Write the files the shim served after `since` to the directory `out`. Returns how many, or 0 (writing
    nothing) when there were none."""
    try:
        with open(f"{cache()}/.fetched.log", "rb") as f:
            f.seek(since)
            lines = f.read().decode("utf-8", "replace").splitlines()
    except FileNotFoundError:
        return 0
    seen = set()
    for line in lines:
        parts = line.split(" ")
        if len(parts) == 3 and parts[0].startswith("https://") and parts[1].startswith("sha256:") \
                and len(parts[1]) == 71 and parts[2].isdigit():
            seen.add(tuple(parts))
    if os.path.islink(out) or os.path.isfile(out):
        os.remove(out)
    elif os.path.isdir(out):
        shutil.rmtree(out)
    if not seen:
        return 0
    os.makedirs(f"{out}/blobs")
    missing = []
    for url, digest, _ in sorted(seen):
        if url.endswith(PACKAGES) and not url.startswith(LATEST_ONLY):
            continue
        hexdigest = digest[7:]
        src, dst = f"{cache()}/.sha256/{hexdigest}", f"{out}/blobs/{hexdigest}"
        if os.path.exists(dst):
            continue
        h = hashlib.sha256()
        try:
            with open(src, "rb") as f:
                while chunk := f.read(1 << 20):
                    h.update(chunk)
        except FileNotFoundError:
            missing.append(url)
            continue
        if h.hexdigest() != hexdigest:  # a store file is only ever a hard link to complete, hashed bytes
            missing.append(url)
            continue
        shutil.copyfile(src, dst)
    seen |= indexes(seen, out, missing)
    with open(f"{out}/fetched.txt", "w") as f:
        f.writelines(" ".join(t) + "\n" for t in sorted(seen))
    if missing:
        with open(f"{out}/missing.txt", "w") as f:
            f.writelines(u + "\n" for u in sorted(set(missing)))
    return len(seen)
