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


def run(files, existing=None):
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
            open(t + m.DROPIN, "w").write(existing)
        msg = None
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                m.main(t)
        except SystemExit as e:
            msg = str(e.code)
        except Exception as e:  # a traceback is a defect too: it prints WRONG, not a crash of this test
            msg = f"EXCEPTION {e!r}"
        out = open(t + m.DROPIN).read() if os.path.exists(t + m.DROPIN) else None
        left = sorted(os.path.relpath(os.path.join(r, n), t) for r, _, ns in os.walk(t) for n in ns)
        return msg, out, left


def case(name, ok):
    cases.append(ok)
    print(("passed  " if ok else "WRONG   ") + "net.ifupdown-dns-to-resolved: " + name)


def refused(msg, why):
    return msg is not None and msg.endswith(", nothing changed") and why in msg


msg, out, left = run({"/etc/network/interfaces": INSTALLER})
case("the installer's static stanza becomes the exact drop-in", msg is None and out == WANT)
msg, out, left = run({"/etc/network/interfaces": INSTALLER}, existing=WANT)
case("a drop-in that already says the same is left alone", msg is None and out == WANT)
msg, out, left = run({"/etc/network/interfaces": INSTALLER}, existing="[Resolve]\nDNS=1.1.1.1\n")
case("a drop-in that says something else is refused, not replaced",
     refused(msg, "exists already") and out == "[Resolve]\nDNS=1.1.1.1\n")
msg, out, left = run({"/etc/network/interfaces": "auto lo\niface lo inet loopback\nsource interfaces.d/*\n",
                      "/etc/network/interfaces.d/eth0": "iface eth0 inet dhcp\n  dns-nameservers 10.0.2.3 \\\n"
                                                        "    2001:db8::53\n  dns-domain example.org.\n"})
case("source globs, continued lines, IPv6 and dns-domain",
     msg is None and out is not None and "DNS=10.0.2.3 2001:db8::53\n" in out and "Domains=example.org\n" in out)
msg, out, left = run({"/etc/network/interfaces": "source-directory /etc/network/interfaces.d\n",
                      "/etc/network/interfaces.d/eth0": "iface eth0 inet static\n dns-nameservers 10.0.2.3\n",
                      "/etc/network/interfaces.d/eth0.bak": "iface eth0 inet static\n dns-nameservers 9.9.9.9\n"})
case("source-directory reads only files named as interfaces(5) says",
     msg is None and out is not None and "DNS=10.0.2.3\n" in out and "9.9.9.9" not in out)
msg, out, left = run({"/etc/network/interfaces": "auto lo\niface lo inet loopback\n"})
case("no dns-* options: nothing written", msg is None and out is None)
for name, text, why in (
        ("a nameserver that isn't an address", INSTALLER.replace("10.0.2.3", "ns1.example.org"), "isn't an address"),
        ("a search domain with shell in it", INSTALLER.replace("example.org", "a;reboot"), "isn't a plain domain"),
        ("a file sourced twice", "source /etc/network/interfaces\n", "sourced twice")):
    msg, out, left = run({"/etc/network/interfaces": text})
    case(f"{name} is refused before anything changes", refused(msg, why) and out is None)
sys.exit(0 if all(cases) else 1)
