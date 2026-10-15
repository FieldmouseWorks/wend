"""The download list a replay leaves for the archive (docs/archive.md, "Capture").

The mirror shim (tools/mirror.py) logs every file it serves and keeps every file it fetched under its sha256. A walk
notes where the log ends before it starts (mark) and, once its run is saved, writes what came after (collect) next to
the run file, in <run>.fetched/:

  fetched.txt         one "<https url> sha256:<hex> <size>" per distinct file, sorted
  blobs/<hex>         the bytes of every metadata file in it (anything that isn't a package). Live repos replace
                      their metadata within days, so these are taken now; packages last, and the archive fetches
                      them later by url and digest.

The shim is shared, so a walk that runs beside another (`wend verify` runs several, remi a VM run) also lists the
other's files, an apt walk included, and a VM's installer images with them. That's harmless: the archive keeps only
what a landed run references, and checks every byte against its pins. An apt walk alone leaves nothing yet.
An existing <run>.fetched/ is replaced, never added to: a checkout could carry one, symlinks and all.
"""
import hashlib
import os
import shutil

import sandbox

PACKAGES = (".rpm", ".deb", ".udeb")


def cache():
    return f"{sandbox.WORK}/mirror"


def mark():
    """Where the shim's log ends now."""
    try:
        return os.path.getsize(f"{cache()}/.fetched.log")
    except FileNotFoundError:
        return 0


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
        if url.endswith(PACKAGES):
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
    with open(f"{out}/fetched.txt", "w") as f:
        f.writelines(" ".join(t) + "\n" for t in sorted(seen))
    if missing:
        with open(f"{out}/missing.txt", "w") as f:
            f.writelines(u + "\n" for u in sorted(set(missing)))
    return len(seen)
