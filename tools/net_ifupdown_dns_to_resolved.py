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
`ifquery --no-mappings -- PHYSICAL=LOGICAL` prints that stanza's options as ifupdown parsed them (sourced files,
comments, continuations, misplaced options, inherits); without --no-mappings ifquery would run the admin's mapping
scripts again from the logical name and could land on another stanza. A configuration ifupdown can't read fails the
step. What the state says is what the op carries: an interface whose ifup failed (even one family
of it), that was never brought up, or that was taken down by hand isn't in it; one brought up by hand is; a box with
no state (a chroot, or a live box whose /run/network/ifstate is gone) carries nothing, though its configuration is
still parsed (`ifquery --list --all --exclude='*'`) and a main interfaces file ifupdown can't read fails the step; an
error in a sourced file is a warning ifupdown skips, at ifup as here. Options given with `ifup -o`
aren't in the configuration, so the configuration's value is carried, as it is for a stanza edited since boot (the
value the hop's reboot gives the hooks). A stanza deleted since boot fails the step, unless a mapping still matches
its name: then ifquery prints nothing for it and nothing is carried, as the reboot would give its hooks nothing.
ifquery prints `name: value` per option, and an option given twice is one value joined by a newline (config.c), so a
line without ": " (or with whitespace in its name) continues the value before it, and a value is split into words
as a hook's shell splits it (space, tab, newline; nothing else). Each option name is turned into the name ifupdown's
hooks receive (setlocalenv: up to the first "=", whose rest leads the value; "-" to "_", ASCII upper case, anything
else dropped), and two spellings of one DNS_* name on an interface fail the step, since a hook sees only the last.
Only one interface may name resolvers: ifupdown's hook gave each interface its own link in resolved, and one global
DNS= for two would send every lookup to both, so a second one fails the step.
DNS_NAMESERVERS and DNS_NAMESERVER (what resolvconf's and ifupdown's hooks read) must be unicast addresses a resolver
can be at; DNS_SEARCH and DNS_DOMAIN plain domain names; any other DNS_* option fails the step. The result is one
resolved drop-in, /etc/systemd/resolved.conf.d/50-wend-ifupdown.conf, with DNS= and Domains=, written only when it
is new or already the same, written through its directory opened one name at a time from / without following a
symlink, so a symlinked or swapped-in path component can't redirect it. Addresses and routes stay with ifupdown. Not seen, from ifquery's text: `no-scripts`
(kept apart from the options), and a repeated option whose second value itself reads `name: value`, which prints
like a separate option; in each, the options are carried although no hook read them. ifquery prints an interface's
inet and inet6 stanzas together, so two spellings across them fail the step although each hook saw one. A file where
the drop-in's directory goes fails the step too.

Run on the box by both engines as `python3 -` with this file as a here-document (tools/backends.py); this file is
the source, and the client embeds the same text (client/parity.py compares them). tools/test_net_ifupdown.py runs it
with a stand-in ifquery; the parse itself is ifupdown's, proven by the VM replay on #13.
"""
import errno
import ipaddress
import os
import re
import stat
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
    # ifupdown parses its configuration, state or no state: a main file it can't read fails the step. --exclude='*'
    # skips the per-interface check, so an auto name with no stanza (which ifup -a passes over) isn't a failure.
    ifquery("--list", "--all", "--exclude=*")
    names = []
    for n in ifquery("--state"):
        phys, eq, logical = n.partition("=")
        if not eq or not re.fullmatch(NAME, phys) or not re.fullmatch(NAME, logical):
            fail(f"ifquery's state has {n!r}, which isn't 'interface=stanza'")
        if n not in names:
            names.append(n)
    dns, search, carriers = [], [], []
    for iface in names:
        options, spelled = [], {}
        for line in ifquery("--no-mappings", "--", iface):
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
            if iface not in carriers:
                carriers.append(iface)
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
    if len(carriers) > 1:  # ifupdown's hook gave each its own link; global DNS= would send every lookup to all of them
        fail(f"{' and '.join(carriers)} each name resolvers, which resolved would need per link")
    return dns, search


def dropin_dir(root):
    """The drop-in's directory as an open descriptor, reached one name at a time from root without following a symlink,
    so nothing swapped in after a check can redirect the write; a missing directory is made, mode 0755."""
    d = os.open(root or "/", os.O_RDONLY | os.O_DIRECTORY)
    path = ""
    for part in os.path.dirname(DROPIN).strip("/").split("/"):
        path += "/" + part
        try:
            try:
                n = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=d)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o755, dir_fd=d)
                except OSError as e:
                    os.close(d)
                    fail(f"{path} couldn't be made ({e.strerror})")
                n = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=d)
                try:
                    os.fchmod(n, 0o755)
                except OSError:
                    os.close(n)
                    raise
        except OSError as e:  # O_NOFOLLOW with O_DIRECTORY refuses a symlink as ENOTDIR (or ELOOP)
            try:
                link = stat.S_ISLNK(os.stat(part, dir_fd=d, follow_symlinks=False).st_mode)
            except OSError:
                link = False
            os.close(d)
            fail(f"{path} is a symlink" if link or e.errno == errno.ELOOP else f"{path} isn't a directory ({e.strerror})")
        os.close(d)
        d = n
    return d


def main(root=""):
    dns, search = plan()
    if not dns and not search:
        print("ifupdown has no interface up with a dns-* option: nothing to carry to resolved")
        return
    text = "".join(["# Written by wend (net.ifupdown-dns-to-resolved) from ifupdown's dns-* options.\n",
                    "[Resolve]\n", f"DNS={' '.join(dns)}\n" if dns else "",
                    f"Domains={' '.join(search)}\n" if search else ""])
    d = dropin_dir(root)
    name = os.path.basename(DROPIN)
    try:
        try:
            old = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=d)
        except FileNotFoundError:
            old = None
        except OSError:  # a symlink (ELOOP) or something open() refuses
            fail(f"{DROPIN} exists already as something else")
        if old is not None:
            if not stat.S_ISREG(os.fstat(old).st_mode):
                os.close(old)
                fail(f"{DROPIN} exists already as something else")
            with os.fdopen(old, "rb") as f:
                same = f.read() == text.encode()
            if not same:
                fail(f"{DROPIN} exists already as something else")
            print(f"{DROPIN} already says so")
            return
        try:
            fd = os.open(name + ".wend", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=d)
        except FileExistsError:
            fail(f"{DROPIN}.wend is in the way")
        try:
            with os.fdopen(fd, "w") as out:
                out.write(text)
                out.flush()
                os.fchmod(out.fileno(), 0o644)
                # link, not rename: a drop-in created since the check above is kept, never replaced. The link is made
                # from the open file itself (/proc/self/fd), so whatever is put at the .wend name meanwhile isn't it
                os.link(f"/proc/self/fd/{out.fileno()}", name, dst_dir_fd=d, follow_symlinks=True)
        except FileExistsError:
            fail(f"{DROPIN} appeared while it was being written")
        except FileNotFoundError:  # the .wend name was removed or replaced, so the open file has no link left to share
            fail(f"{DROPIN}.wend was replaced while it was being written")
        finally:
            try:
                os.unlink(name + ".wend", dir_fd=d)
            except FileNotFoundError:
                pass
    finally:
        os.close(d)
    print(f"{DROPIN}: DNS={' '.join(dns) or '-'} Domains={' '.join(search) or '-'}")


if __name__ == "__main__":
    main()
