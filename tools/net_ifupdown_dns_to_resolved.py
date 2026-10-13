"""The reference of the Ubuntu op net.ifupdown-dns-to-resolved (SPEC.md, "Debian ops"): give systemd-resolved the
resolvers and search domains that ifupdown's configuration names, for a box that keeps ifupdown into a release where
resolved answers the box's lookups.

An upgraded Ubuntu box that came up from trusty keeps ifupdown and resolvconf. On jammy's release pocket,
ifupdown's own hook that hands an interface's dns-* options to resolved (/etc/network/if-up.d/resolved) fails
("mystatedir: not found"), resolvconf lists only resolved's stub, and resolved has no upstream server: nothing
resolves after the reboot. Noble no longer ships resolvconf at all.

Typed, not a guess: ifupdown says which interfaces it has up and reads its own configuration. The op runs on the
live box before the hop's reboot, so `ifquery --state` lists each interface ifup brought up, as `PHYSICAL=LOGICAL`
(the stanza it used: `auto eth0=work` and mapping patterns name the logical stanza there, not the physical one), and
`ifquery -- PHYSICAL=LOGICAL` prints that stanza's options as ifupdown parsed them (sourced files, comments,
continuations, misplaced options, inherits) without running mapping scripts; a configuration ifupdown can't read
fails the step. An interface whose ifup failed, or that was never brought up, isn't in the state; a box with no
state (a chroot) carries nothing.
ifquery prints `name: value` per option, and an option given twice is one value joined by a newline (config.c), so a
line without ": " (or with whitespace in its name) continues the value before it, and a value is split into words
as a hook's shell splits it (space, tab, newline; nothing else). Each option name is turned into the name ifupdown's
hooks receive (setlocalenv: up to the first "=", whose rest leads the value; "-" to "_", ASCII upper case, anything
else dropped), and two spellings of one DNS_* name on an interface fail the step, since a hook sees only the last.
DNS_NAMESERVERS and DNS_NAMESERVER (what resolvconf's and ifupdown's hooks read) must be unicast addresses a resolver
can be at; DNS_SEARCH and DNS_DOMAIN plain domain names; any other DNS_* option fails the step. The result is one
resolved drop-in, /etc/systemd/resolved.conf.d/50-wend-ifupdown.conf, with DNS= and Domains=, written only when it
is new or already the same. Addresses and routes stay with ifupdown. Not seen, from ifquery's text: `no-scripts`
(kept apart from the options), and a repeated option whose second value itself reads `name: value`, which prints
like a separate option; in each, the options are carried although no hook read them. ifquery prints an interface's
inet and inet6 stanzas together, so two spellings across them fail the step although each hook saw one. A file where
the drop-in's directory goes fails the step too.

Run on the box by both engines as `python3 -` with this file as a here-document (tools/backends.py); this file is
the source, and the client embeds the same text (client/parity.py compares them). tools/test_net_ifupdown.py runs it
with a stand-in ifquery; the parse itself is ifupdown's, proven by the VM replay on #13.
"""
import ipaddress
import os
import re
import subprocess
import sys

DROPIN = "/etc/systemd/resolved.conf.d/50-wend-ifupdown.conf"
DOMAIN = r"[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*\.?"
IFS = r"[ \t\n]+"  # the shell's default IFS: how a hook word-splits $IF_DNS_*
SPACE = r"[ \t\n\v\f\r]"  # C isspace(): what can't be in an ifupdown option name
NAME = r"[A-Za-z0-9_.:@][A-Za-z0-9_.:@-]*"


def fail(msg):
    sys.exit(f"{msg}, nothing changed")


def ifquery(*args):
    try:
        p = subprocess.run(["ifquery", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as e:
        fail(f"ifquery: {e.strerror}")
    if p.returncode:
        fail(f"ifquery {' '.join(args)} failed: {p.stderr.decode('utf-8', 'replace').strip()}")
    try:
        return [x for x in p.stdout.decode("utf-8").split("\n") if x.strip(" \t")]
    except UnicodeDecodeError:
        fail(f"ifquery {' '.join(args)} printed something that isn't UTF-8")


def option(key):
    """An option name as ifupdown hands it to its hooks (execute.c, setlocalenv)."""
    return re.sub(r"[^A-Za-z0-9_]", "", key.split("=", 1)[0].replace("-", "_")).upper()


def server(iface, key, v):
    try:
        a = ipaddress.ip_address(v)
    except ValueError:
        fail(f"{iface}: {key} {v} isn't an address")
    mapped = a.version == 6 and a.ipv4_mapped is not None
    if "%" in v or mapped or a.is_link_local or a.is_multicast or a.is_loopback or a.is_unspecified or \
            a.is_reserved:
        fail(f"{iface}: {key} {v} isn't a unicast address resolved can use as a server")
    return str(a)


def plan():
    names = []
    for n in ifquery("--state"):
        phys, eq, logical = n.partition("=")
        if not eq or not re.fullmatch(NAME, phys) or not re.fullmatch(NAME, logical):
            fail(f"ifquery's state has {n!r}, which isn't 'interface=stanza'")
        if n not in names:
            names.append(n)
    dns, search = [], []
    for iface in names:
        options, spelled = [], {}
        for line in ifquery("--", iface):
            key, sep, value = line.partition(": ")
            if sep and key and not re.search(SPACE, key):
                options.append([key, value])
            elif options:
                options[-1][1] += "\n" + line
            else:
                fail(f"{iface}: ifquery printed {line!r} before any option")
        for key, value in options:
            opt, eq, rest = option(key), *key.partition("=")[1:]
            value = rest + eq + value if eq else value  # setlocalenv: the name ends at "=", the rest leads the value
            if not opt.startswith("DNS_"):
                continue
            if spelled.setdefault(opt, key) != key:
                fail(f"{iface}: {spelled[opt]} and {key} are both {opt} to ifupdown's hooks, which see only the last")
            if opt in ("DNS_NAMESERVERS", "DNS_NAMESERVER"):
                for v in re.split(IFS, value.strip(" \t\n")):
                    a = server(iface, key, v)
                    if a not in dns:
                        dns.append(a)
            elif opt in ("DNS_SEARCH", "DNS_DOMAIN"):
                for v in re.split(IFS, value.strip(" \t\n")):
                    if not re.fullmatch(DOMAIN, v):
                        fail(f"{iface}: {key} {v} isn't a plain domain name")
                    if v.rstrip(".") not in search:
                        search.append(v.rstrip("."))
            else:
                fail(f"{iface}: {key} isn't carried to resolved")
    return dns, search


def main(root=""):
    dns, search = plan()
    if not dns and not search:
        print("ifupdown has no interface up with a dns-* option: nothing to carry to resolved")
        return
    text = "".join(["# Written by wend (net.ifupdown-dns-to-resolved) from ifupdown's dns-* options.\n",
                    "[Resolve]\n", f"DNS={' '.join(dns)}\n" if dns else "",
                    f"Domains={' '.join(search)}\n" if search else ""])
    path = root + DROPIN
    if os.path.lexists(path):
        if os.path.islink(path) or not os.path.isfile(path) or open(path, "rb").read() != text.encode():
            fail(f"{DROPIN} exists already as something else")
        print(f"{DROPIN} already says so")
        return
    if os.path.lexists(path + ".wend"):
        fail(f"{DROPIN}.wend is in the way")
    if os.path.lexists(os.path.dirname(path)) and not os.path.isdir(os.path.dirname(path)):
        fail(f"{os.path.dirname(DROPIN)} isn't a directory")
    if not os.path.isdir(os.path.dirname(path)):
        os.makedirs(os.path.dirname(path))
        os.chmod(os.path.dirname(path), 0o755)
    fd = os.open(path + ".wend", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w") as out:
        out.write(text)
    os.chmod(path + ".wend", 0o644)
    os.replace(path + ".wend", path)
    print(f"{DROPIN}: DNS={' '.join(dns) or '-'} Domains={' '.join(search) or '-'}")


if __name__ == "__main__":
    main()
