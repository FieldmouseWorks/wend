"""The reference of the apt op net.ifupdown-dns-to-resolved (SPEC.md, "Debian ops"): give systemd-resolved the
resolvers and search domains that /etc/network/interfaces names, for a box that keeps ifupdown into a release where
resolved answers the box's lookups.

An upgraded Ubuntu box that came up from trusty keeps ifupdown and resolvconf. On jammy's release pocket,
ifupdown's own hook that hands an interface's dns-* options to resolved (/etc/network/if-up.d/resolved) fails
("mystatedir: not found"), resolvconf lists only resolved's stub, and resolved has no upstream server: nothing
resolves after the reboot. Noble no longer ships resolvconf at all.

Typed, not a guess: the files are read the way ifupdown reads them (a "#" line skipped before "\\" joins lines,
"source" globs and "source-directory" directories followed), the dns options are named the way ifupdown hands them
to its hooks (dns-nameservers, dns-nameserver, dns-search, dns-domain; "-" or "_"), every value must be a global
address or a plain domain name, any other dns option, or one on an interface that isn't brought up at boot by name,
fails the step, and anything else about the file is left to ifupdown. The result is one resolved drop-in,
/etc/systemd/resolved.conf.d/50-wend-ifupdown.conf, with DNS= and Domains=, written only when it is new or the same.
Addresses and routes stay with ifupdown.

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


def physical_to_logical(path):
    """ifupdown's own reading (config.c): a line whose first non-blank is "#" is skipped as one physical line, then a
    line ending in "\" takes the next physical line on, the "\" removed and nothing added."""
    try:
        raw = open(path, "rb").read().decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        fail(f"{path}: {getattr(e, 'strerror', None) or e}")
    phys, out, k = raw.split("\n"), [], 0
    while k < len(phys):
        line = phys[k]
        k += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        while line.endswith("\\") and k < len(phys):
            line = line[:-1] + phys[k]
            k += 1
        out.append(line.strip())
    return out


def lines(path, seen, root=""):
    """The logical lines of path and the files it sources, in order, as ifupdown reads them: a file already read is
    skipped, a source pattern that matches nothing or a directory is skipped. root prefixes absolute names (tests)."""
    real = os.path.realpath(path)
    if real in seen:
        return []
    seen.add(real)
    out = []
    base = os.path.dirname(path)
    for line in physical_to_logical(path):
        words = line.split()
        if words[0] == "source":
            for pattern in words[1:]:
                where = os.path.join(base, pattern) if not pattern.startswith("/") else root + pattern
                for f in sorted(glob.glob(where)):
                    if os.path.isfile(f):
                        out += lines(f, seen, root)
        elif words[0].startswith("source-dir"):  # ifupdown takes any word starting so (header.h)
            for d in words[1:]:
                d = os.path.join(base, d) if not d.startswith("/") else root + d
                if os.path.isdir(d):
                    for n in sorted(os.listdir(d)):
                        if re.fullmatch(SOURCE_DIR_FILE, n) and os.path.isfile(os.path.join(d, n)):
                            out += lines(os.path.join(d, n), seen, root)
        else:
            out.append((path, words))
    return out


def option(key):
    """An option name as ifupdown hands it to the hooks: "-" becomes "_", upper case (execute.c setlocalenv)."""
    return key.replace("-", "_").upper()


def plan(root=""):
    dns, search, up, stanza, mapping = [], [], set(), None, False
    found = []
    for path, words in lines(root + INTERFACES, set(), root):
        key, values = words[0], words[1:]
        if key == "auto" or key.startswith("allow-"):
            up.update(values)
        elif key == "iface":
            stanza = values[0] if values else None
        elif key == "mapping":
            mapping, stanza = True, None
        elif option(key).startswith("DNS_"):
            found.append((path, stanza, option(key), key, values))
    for path, stanza, opt, key, values in found:
        if mapping or stanza not in up:  # resolvconf only took the dns-* options of interfaces that came up
            fail(f"{path}: {key} on {stanza or 'a mapping'}, which isn't brought up at boot by name")
        if opt in ("DNS_NAMESERVERS", "DNS_NAMESERVER"):  # what resolvconf's and ifupdown's hooks read
            for v in values:
                try:
                    a = ipaddress.ip_address(v)
                except ValueError:
                    fail(f"{path}: {key} {v} isn't an address")
                if "%" in v or a.is_link_local:
                    fail(f"{path}: {key} {v} is scoped or link-local, which resolved can't use as a global server")
                if str(a) not in dns:
                    dns.append(str(a))
        elif opt in ("DNS_SEARCH", "DNS_DOMAIN"):
            for v in values:
                if not re.fullmatch(DOMAIN, v):
                    fail(f"{path}: {key} {v} isn't a plain domain name")
                if v.rstrip(".") not in search:
                    search.append(v.rstrip("."))
        else:
            fail(f"{path}: {key} isn't carried to resolved")
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
