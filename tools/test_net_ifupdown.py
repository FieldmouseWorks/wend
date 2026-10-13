"""The Ubuntu op net.ifupdown-dns-to-resolved (SPEC.md, "Debian ops"; tools/net_ifupdown_dns_to_resolved.py) by
behaviour, with a stand-in ifquery on PATH: which interfaces it reads (those ifupdown brings up at boot), how it names
their options (as ifupdown's hooks receive them), the values it refuses, that every refusal changes nothing, and the
drop-in it writes. The parse of /etc/network/interfaces itself is ifupdown's, proven by the VM replay on wend#13. It
runs the PR's own converter, so the gate's schema job (which judges a PR with the base branch's tools and never runs
the PR's code) doesn't run it: `wend check` and the client job do.
python3 tools/test_net_ifupdown.py"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import tempfile

sys.dont_write_bytecode = True
SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "net_ifupdown_dns_to_resolved.py")
IFQUERY = """#!/usr/bin/env python3
import json, sys
q = json.load(open(%r))
if q.get("fail"):
    sys.stderr.write("ifquery: /etc/network/interfaces:24: misplaced option\\n"); sys.exit(1)
a = sys.argv[1:]
if a[:1] == ["--list"]:
    print("\\n".join(q["lists"].get(a[1].split("=", 1)[1], [])))
else:
    print("\\n".join(q["ifaces"].get(a[0], [])))
"""
ETH0 = ["address: 10.0.2.20", "netmask: 255.255.255.0", "gateway: 10.0.2.2", "dns-nameservers: 10.0.2.3",
        "dns-search: example.org"]
BOOT = {"lists": {"auto": ["lo", "eth0"], "hotplug": []}, "ifaces": {"lo": [], "eth0": ETH0}}
WANT = ("# Written by wend (net.ifupdown-dns-to-resolved) from ifupdown's dns-* options.\n"
        "[Resolve]\nDNS=10.0.2.3\nDomains=example.org\n")
DROP = "etc/systemd/resolved.conf.d/50-wend-ifupdown.conf"
cases = []


def run(query, existing=None, symlink=False):
    """Run the converter on a fresh root with ifquery answering query; return (exit message or None, drop-in text,
    files under the root, the drop-in's mode)."""
    loader = importlib.machinery.SourceFileLoader("net_ifupdown_dns_to_resolved", SRC)
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(m)
    with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as b:
        json.dump(query, open(f"{b}/q.json", "w"))
        open(f"{b}/ifquery", "w").write(IFQUERY % f"{b}/q.json")
        os.chmod(f"{b}/ifquery", 0o755)
        if existing is not None:
            os.makedirs(os.path.dirname(t + m.DROPIN))
            target = t + m.DROPIN + (".target" if symlink else "")
            open(target, "w").write(existing)
            if symlink:
                os.symlink(target, t + m.DROPIN)
        path, mask, msg = os.environ["PATH"], os.umask(0o077), None  # 077: the mode must come from the op
        os.environ["PATH"] = f"{b}:{path}"
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                m.main(t)
        except SystemExit as e:
            msg = str(e.code)
        except Exception as e:  # a traceback is a defect too: it prints WRONG, not a crash of this test
            msg = f"EXCEPTION {e!r}"
        finally:
            os.environ["PATH"], _ = path, os.umask(mask)
        p = t + m.DROPIN
        out = open(p).read() if os.path.isfile(p) else None
        mode = os.stat(p).st_mode & 0o777 if os.path.isfile(p) else None
        left = sorted(os.path.relpath(os.path.join(r, n), t) for r, _, ns in os.walk(t) for n in ns)
        return msg, out, left, mode


def case(name, ok):
    cases.append(ok)
    print(("passed  " if ok else "WRONG   ") + "net.ifupdown-dns-to-resolved: " + name)


def refused(msg, why):
    return msg is not None and msg.endswith(", nothing changed") and why in msg


def with_eth0(lines, lists=None):
    return {"lists": lists or BOOT["lists"], "ifaces": {"lo": [], "eth0": lines}}


msg, out, left, mode = run(BOOT)
case("the installer's interface becomes the exact drop-in, mode 0644 under a 077 umask, nothing else written",
     msg is None and out == WANT and left == [DROP] and mode == 0o644)
msg, out, left, mode = run(BOOT, existing=WANT)
case("a drop-in that already says the same is left alone", msg is None and out == WANT)
msg, out, left, mode = run(BOOT, existing="[Resolve]\nDNS=1.1.1.1\n")
case("a drop-in that says something else is refused, not replaced",
     refused(msg, "exists already") and out == "[Resolve]\nDNS=1.1.1.1\n")
msg, out, left, mode = run(BOOT, existing=WANT, symlink=True)
case("a drop-in that is a symlink is refused, even to the same text", refused(msg, "exists already"))
msg, out, left, mode = run({"lists": {"auto": ["lo"], "hotplug": ["eth0"]}, "ifaces": {"lo": [], "eth0": ETH0}})
case("an allow-hotplug interface counts: ifupdown brings it up at boot", msg is None and out == WANT)
msg, out, left, mode = run({"lists": {"auto": ["lo"], "hotplug": []}, "ifaces": {"lo": [], "eth0": ETH0}})
case("an interface ifupdown doesn't bring up at boot is left out", msg is None and out is None and left == [])
msg, out, left, mode = run(with_eth0([x.replace("dns-nameservers", "dns-nameserver") for x in ETH0]))
case("dns-nameserver (singular), which resolvconf's and ifupdown's hooks read too", msg is None and out == WANT)
msg, out, left, mode = run(with_eth0([x.replace("dns-nameservers", "d.ns_Nameservers") for x in ETH0]))
case("option names as ifupdown hands them to its hooks (- to _, upper case, others dropped)",
     msg is None and out == WANT)
msg, out, left, mode = run(with_eth0(ETH0 + ["dns-nameservers: 10.0.2.3 2001:db8::53", "dns-domain: example.org."]))
case("several servers, IPv6, dns-domain, duplicates once",
     msg is None and out is not None and "DNS=10.0.2.3 2001:db8::53\n" in out and "Domains=example.org\n" in out)
msg, out, left, mode = run({"lists": {"auto": ["lo"], "hotplug": []}, "ifaces": {"lo": []}})
case("no interface with dns options: nothing written", msg is None and out is None and left == [])
for name, q, why in (
        ("ifupdown failing to read its configuration", {"fail": True}, "misplaced option"),
        ("a nameserver that isn't an address", with_eth0(ETH0 + ["dns-nameservers: ns1.example.org"]),
         "isn't an address"),
        ("a scoped global address", with_eth0(ETH0 + ["dns-nameservers: 2001:db8::1%eth0#evil.example"]),
         "isn't a unicast address"),
        ("a link-local address", with_eth0(ETH0 + ["dns-nameservers: fe80::1"]), "isn't a unicast address"),
        ("a multicast address", with_eth0(ETH0 + ["dns-nameservers: 224.0.0.1"]), "isn't a unicast address"),
        ("the unspecified address", with_eth0(ETH0 + ["dns-nameservers: 0.0.0.0"]), "isn't a unicast address"),
        ("an IPv4-mapped address", with_eth0(ETH0 + ["dns-nameservers: ::ffff:10.0.2.3"]),
         "isn't a unicast address"),
        ("a search domain with shell in it", with_eth0(ETH0 + ["dns-search: a;reboot"]), "isn't a plain domain"),
        ("a dns option resolved can't take", with_eth0(ETH0 + ["dns-sortlist: 10.0.0.0/8"]), "isn't carried"),
        ("ifquery printing a line that isn't an option", with_eth0(ETH0 + ["garbage"]), "isn't 'option: value'")):
    msg, out, left, mode = run(q)
    case(f"{name} is refused before anything changes", refused(msg, why) and out is None and left == [])
sys.exit(0 if all(cases) else 1)
