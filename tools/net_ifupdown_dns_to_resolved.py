"""The reference of the apt op net.ifupdown-dns-to-resolved (SPEC.md, "Debian ops"): give systemd-resolved the
resolvers and search domains that /etc/network/interfaces names, for a box that keeps ifupdown into a release where
resolved answers the box's lookups.

An upgraded Ubuntu box that came up from trusty keeps ifupdown and resolvconf. On jammy's release pocket,
ifupdown's own hook that hands an interface's dns-* options to resolved (/etc/network/if-up.d/resolved) fails
("mystatedir: not found"), resolvconf lists only resolved's stub, and resolved has no upstream server: nothing
resolves after the reboot. Noble no longer ships resolvconf at all.

Typed, not a guess: the files are read as interfaces(5) says (a "#" only at the start of a line, "\\" continuing a
line, "source" globs and "source-directory" directories followed), only the dns-nameservers, dns-search and
dns-domain options are read, every value must be an address or a plain domain name, and anything else about the
file is left to ifupdown. The result is one resolved drop-in, /etc/systemd/resolved.conf.d/50-wend-ifupdown.conf,
with DNS= and Domains=, written only when it is new or the same. Addresses and routes stay with ifupdown.

Run on the box by both engines as `python3 -` with this file as a here-document (tools/backends.py); this file is
the source, and the client embeds the same text (client/parity.py compares them). tools/test_net_ifupdown.py runs it
against fixture roots.
"""
import glob
import ipaddress
import os
import re
import sys

INTERFACES = "/etc/network/interfaces"
DROPIN = "/etc/systemd/resolved.conf.d/50-wend-ifupdown.conf"
DOMAIN = r"[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*\.?"
SOURCE_DIR_FILE = r"[A-Za-z0-9_-]+"  # interfaces(5): source-directory reads files named like this, others are skipped


def fail(msg):
    sys.exit(f"{msg}, nothing changed")


def lines(path, seen, root=""):
    """interfaces(5) logical lines of path and the files it sources, in order. root prefixes absolute names (tests)."""
    real = os.path.realpath(path)
    if real in seen:
        fail(f"{path} is sourced twice")
    seen.add(real)
    try:
        text = open(path).read()
    except OSError as e:
        fail(f"{path}: {e.strerror}")
    logical = []
    for raw in re.sub(r"\\\n", " ", text).split("\n"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        logical.append(line)
    out = []
    base = os.path.dirname(path)
    for line in logical:
        words = line.split()
        if words[0] == "source":
            for pattern in words[1:]:
                where = os.path.join(base, pattern) if not pattern.startswith("/") else root + pattern
                for f in sorted(glob.glob(where)):
                    out += lines(f, seen, root)
        elif words[0] in ("source-directory", "source-dir"):
            for d in words[1:]:
                d = os.path.join(base, d) if not d.startswith("/") else root + d
                if os.path.isdir(d):
                    for n in sorted(os.listdir(d)):
                        if re.fullmatch(SOURCE_DIR_FILE, n) and os.path.isfile(os.path.join(d, n)):
                            out += lines(os.path.join(d, n), seen, root)
        else:
            out.append((path, words))
    return out


def plan(root=""):
    dns, search = [], []
    for path, words in lines(root + INTERFACES, set(), root):
        key, values = words[0], words[1:]
        if key == "dns-nameservers":
            for v in values:
                try:
                    a = str(ipaddress.ip_address(v))
                except ValueError:
                    fail(f"{path}: dns-nameservers {v} isn't an address")
                if a not in dns:
                    dns.append(a)
        elif key in ("dns-search", "dns-domain"):
            for v in values:
                if not re.fullmatch(DOMAIN, v):
                    fail(f"{path}: {key} {v} isn't a plain domain name")
                if v.rstrip(".") not in search:
                    search.append(v.rstrip("."))
    return dns, search


def main(root=""):
    dns, search = plan(root)
    if not dns and not search:
        print(f"{INTERFACES} names no dns-* options: nothing to carry to resolved")
        return
    text = "".join(["# Written by wend (net.ifupdown-dns-to-resolved) from " + INTERFACES + "'s dns-* options.\n",
                    "[Resolve]\n", f"DNS={' '.join(dns)}\n" if dns else "",
                    f"Domains={' '.join(search)}\n" if search else ""])
    path = root + DROPIN
    if os.path.lexists(path):
        if os.path.islink(path) or open(path).read() != text:
            fail(f"{DROPIN} exists already with other contents")
        print(f"{DROPIN} already says so")
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path + ".wend", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w") as out:
        out.write(text)
    os.chmod(path + ".wend", 0o644)
    os.replace(path + ".wend", path)
    print(f"{DROPIN}: DNS={' '.join(dns) or '-'} Domains={' '.join(search) or '-'}")


if __name__ == "__main__":
    main()
