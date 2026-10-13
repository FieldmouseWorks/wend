"""The Ubuntu op net.ifupdown-dns-to-resolved (SPEC.md, "Debian ops"; tools/net_ifupdown_dns_to_resolved.py) by
behaviour, on fixture roots: the drop-in it writes for the interfaces files real Ubuntu boxes have, the sourced
files it follows, the values it refuses, and that a refusal changes nothing. It runs the PR's own converter, so the
gate's schema job (which judges a PR with the base branch's tools and never runs the PR's code) doesn't run it:
`wend check` and the client job do.
python3 tools/test_net_ifupdown.py"""
import contextlib
import importlib.machinery
import io
import importlib.util
import os
import sys
import tempfile

sys.dont_write_bytecode = True
SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "net_ifupdown_dns_to_resolved.py")
INSTALLER = """# This file describes the network interfaces available on your system
auto lo
iface lo inet loopback

# The primary network interface
auto eth0
iface eth0 inet static
\taddress 10.0.2.20
\tnetmask 255.255.255.0
\tgateway 10.0.2.2
\t# dns-* options are implemented by the resolvconf package, if installed
\tdns-nameservers 10.0.2.3
\tdns-search example.org
"""
WANT = ("# Written by wend (net.ifupdown-dns-to-resolved) from /etc/network/interfaces's dns-* options.\n"
        "[Resolve]\nDNS=10.0.2.3\nDomains=example.org\n")
cases = []


def run(files, existing=None, symlink=False):
    """Run the converter on a fresh root holding files ({path: text}); return (exit message or None, drop-in text,
    every file under the root)."""
    loader = importlib.machinery.SourceFileLoader("net_ifupdown_dns_to_resolved", SRC)
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(m)
    with tempfile.TemporaryDirectory() as t:
        for path, text in files.items():
            os.makedirs(os.path.dirname(t + path), exist_ok=True)
            open(t + path, "w").write(text)
        if existing is not None:
            os.makedirs(os.path.dirname(t + m.DROPIN), exist_ok=True)
            target = t + m.DROPIN + (".target" if symlink else "")
            open(target, "w").write(existing)
            if symlink:
                os.symlink(target, t + m.DROPIN)
        msg = None
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                m.main(t)
        except SystemExit as e:
            msg = str(e.code)
        except Exception as e:  # a traceback is a defect too: it prints WRONG, not a crash of this test
            msg = f"EXCEPTION {e!r}"
        out = open(t + m.DROPIN).read() if os.path.exists(t + m.DROPIN) else None
        run.mode = os.stat(t + m.DROPIN).st_mode & 0o777 if os.path.exists(t + m.DROPIN) else None
        left = sorted(os.path.relpath(os.path.join(r, n), t) for r, _, ns in os.walk(t) for n in ns)
        return msg, out, left


def case(name, ok):
    cases.append(ok)
    print(("passed  " if ok else "WRONG   ") + "net.ifupdown-dns-to-resolved: " + name)


def refused(msg, why):
    return msg is not None and msg.endswith(", nothing changed") and why in msg


IN = "etc/network/interfaces"
DROP = "etc/systemd/resolved.conf.d/50-wend-ifupdown.conf"


def unchanged(files, left):
    """a refusal left exactly the input files, nothing else"""
    return left == sorted(p.lstrip("/") for p in files)


msg, out, left = run({"/etc/network/interfaces": INSTALLER})
case("the installer's static stanza becomes the exact drop-in, mode 0644, nothing else written",
     msg is None and out == WANT and left == sorted([IN, DROP]) and run.mode == 0o644)
msg, out, left = run({"/etc/network/interfaces": INSTALLER}, existing=WANT)
case("a drop-in that already says the same is left alone", msg is None and out == WANT)
msg, out, left = run({"/etc/network/interfaces": INSTALLER}, existing="[Resolve]\nDNS=1.1.1.1\n")
case("a drop-in that says something else is refused, not replaced",
     refused(msg, "exists already") and out == "[Resolve]\nDNS=1.1.1.1\n")
msg, out, left = run({"/etc/network/interfaces": INSTALLER}, existing=WANT, symlink=True)
case("a drop-in that is a symlink is refused, even to the same text", refused(msg, "exists already"))
msg, out, left = run({"/etc/network/interfaces": "auto lo eth0\niface lo inet loopback\nsource interfaces.d/*\n",
                      "/etc/network/interfaces.d/eth0": "iface eth0 inet dhcp\n  dns-nameservers 10.0.2.3 \\\n"
                                                        " 2001:db8::53\n  dns-domain example.org.\n"})
case("source globs, continued lines, IPv6 and dns-domain",
     msg is None and out is not None and "DNS=10.0.2.3 2001:db8::53\n" in out and "Domains=example.org\n" in out)
msg, out, left = run({"/etc/network/interfaces": "auto eth0\nsource-directory /etc/network/interfaces.d\n",
                      "/etc/network/interfaces.d/eth0": "iface eth0 inet static\n dns-nameservers 10.0.2.3\n",
                      "/etc/network/interfaces.d/eth0.bak": "iface eth0 inet static\n dns-nameservers 9.9.9.9\n"})
case("source-directory reads only files named as interfaces(5) says",
     msg is None and out is not None and "DNS=10.0.2.3\n" in out and "9.9.9.9" not in out)
msg, out, left = run({"/etc/network/interfaces": INSTALLER.replace("dns-nameservers", "dns-nameserver")})
case("dns-nameserver (singular), which resolvconf's and ifupdown's hooks read too",
     msg is None and out == WANT)
msg, out, left = run({"/etc/network/interfaces": INSTALLER.replace("dns-nameservers", "dns_nameservers")})
case("dns_nameservers, as ifupdown names it to its hooks", msg is None and out == WANT)
msg, out, left = run({"/etc/network/interfaces": INSTALLER.replace(
    "\t# dns-* options", "\t# an old resolver \\\n\tdns-nameservers 10.0.2.3\n\t# dns-* options").replace(
    "\tdns-nameservers 10.0.2.3\n\tdns-search", "\tdns-search")})
case("a # line is skipped before a \\ joins lines, as ifupdown reads them", msg is None and out == WANT)
msg, out, left = run({"/etc/network/interfaces": "source /etc/network/interfaces\nauto eth0\niface eth0 inet static\n"
                                                 " dns-nameservers 10.0.2.3\n"})
case("a file sourced twice is read once, as ifupdown does", msg is None and out is not None and "DNS=10.0.2.3\n" in out)
msg, out, left = run({"/etc/network/interfaces": "auto lo\niface lo inet loopback\n"})
case("no dns-* options: nothing written", msg is None and out is None and left == [IN])
for name, text, why in (
        ("a nameserver that isn't an address", INSTALLER.replace("10.0.2.3", "ns1.example.org"), "isn't an address"),
        ("a scoped address", INSTALLER.replace("10.0.2.3", "fe80::1%eth0#evil.example"), "scoped or link-local"),
        ("a link-local address", INSTALLER.replace("10.0.2.3", "fe80::1"), "scoped or link-local"),
        ("a search domain with shell in it", INSTALLER.replace("example.org", "a;reboot"), "isn't a plain domain"),
        ("a search domain with a #", INSTALLER.replace("example.org", "example.org#x"), "isn't a plain domain"),
        ("a dns option resolved can't take", INSTALLER + "\tdns-sortlist 10.0.0.0/8\n", "isn't carried"),
        ("dns options on an interface that isn't brought up at boot",
         INSTALLER.replace("auto eth0\n", ""), "isn't brought up at boot"),
        ("dns options with a mapping", INSTALLER + "mapping eth1\n\tscript /bin/true\n", "isn't brought up at boot")):
    files = {"/etc/network/interfaces": text}
    msg, out, left = run(files)
    case(f"{name} is refused before anything changes", refused(msg, why) and out is None and unchanged(files, left))
msg, out, left = run({})
case("no /etc/network/interfaces is refused, not a traceback", refused(msg, "No such file"))
sys.exit(0 if all(cases) else 1)
