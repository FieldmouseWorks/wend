"""The Ubuntu op net.ifupdown-dns-to-resolved (SPEC.md, "Debian ops"; tools/net_ifupdown_dns_to_resolved.py) by
behaviour, with a stand-in ifquery on PATH: which interfaces it reads (those ifupdown has up, from its state), how it names
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
if a == ["--state"]:
    print("\\n".join(q["state"]))
elif a[:2] == ["--no-mappings", "--"] and "raw" in q:
    sys.stdout.buffer.write(bytes.fromhex(q["raw"]))
elif a[:2] == ["--no-mappings", "--"]:
    print("\\n".join(q["ifaces"].get(a[2], [])))
else:
    sys.stderr.write("ifquery: not --no-mappings -- NAME\\n"); sys.exit(1)
"""
ETH0 = ["address: 10.0.2.20", "netmask: 255.255.255.0", "gateway: 10.0.2.2", "dns-nameservers: 10.0.2.3",
        "dns-search: example.org"]
BOOT = {"state": ["lo=lo", "eth0=eth0"], "ifaces": {"lo=lo": [], "eth0=eth0": ETH0}}
WANT = ("# Written by wend (net.ifupdown-dns-to-resolved) from ifupdown's dns-* options.\n"
        "[Resolve]\nDNS=10.0.2.3\nDomains=example.org\n")
DROP = "etc/systemd/resolved.conf.d/50-wend-ifupdown.conf"
cases = []


def run(query, existing=None, symlink=False, prepare=None, no_ifquery=False):
    """Run the converter on a fresh root with ifquery answering query; return (exit message or None, drop-in text,
    files under the root, the drop-in's mode)."""
    loader = importlib.machinery.SourceFileLoader("net_ifupdown_dns_to_resolved", SRC)
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(m)
    with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as b:
        json.dump(query, open(f"{b}/q.json", "w"))
        if not no_ifquery:
            open(f"{b}/ifquery", "w").write(IFQUERY % f"{b}/q.json")
            os.chmod(f"{b}/ifquery", 0o755)
        if prepare:
            prepare(t + m.DROPIN)
        if existing is not None:
            os.makedirs(os.path.dirname(t + m.DROPIN))
            target = t + m.DROPIN + (".target" if symlink else "")
            open(target, "w").write(existing)
            if symlink:
                os.symlink(target, t + m.DROPIN)
        path, mask, msg = os.environ["PATH"], os.umask(0o077), None  # 077: the mode must come from the op
        os.environ["PATH"] = b if no_ifquery else f"{b}:{path}"
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
        mode = (os.stat(p).st_mode & 0o777, os.stat(os.path.dirname(p)).st_mode & 0o777) if os.path.isfile(p) else None
        left = sorted(os.path.relpath(os.path.join(r, n), t) for r, _, ns in os.walk(t) for n in ns)
        return msg, out, left, mode


def case(name, ok):
    cases.append(ok)
    print(("passed  " if ok else "WRONG   ") + "net.ifupdown-dns-to-resolved: " + name)


def refused(msg, why):
    return msg is not None and msg.endswith(", nothing changed") and why in msg


def with_eth0(lines, state=None):
    return {"state": state or BOOT["state"], "ifaces": {"lo=lo": [], "eth0=eth0": lines}}


msg, out, left, mode = run(BOOT)
case("the installer's interface becomes the exact drop-in, mode 0644 in a 0755 directory under a 077 umask, nothing "
     "else written", msg is None and out == WANT and left == [DROP] and mode == (0o644, 0o755))
msg, out, left, mode = run(BOOT, existing=WANT)
case("a drop-in that already says the same is left alone", msg is None and out == WANT)
msg, out, left, mode = run(BOOT, existing="[Resolve]\nDNS=1.1.1.1\n")
case("a drop-in that says something else is refused, not replaced",
     refused(msg, "exists already") and out == "[Resolve]\nDNS=1.1.1.1\n")
msg, out, left, mode = run(BOOT, existing=WANT, symlink=True)
case("a drop-in that is a symlink is refused, even to the same text", refused(msg, "exists already"))
msg, out, left, mode = run({"state": ["lo=lo", "eth0=work"], "ifaces": {"lo=lo": [], "eth0=work": ETH0,
                                                                        "eth0=eth0": ["dns-nameservers: 6.6.6.6"]}})
case("an interface up from a mapped stanza (auto eth0=work) is read from that stanza", msg is None and out == WANT)
msg, out, left, mode = run({"state": ["lo=lo"], "ifaces": {"lo=lo": [], "eth0=eth0": ETH0}})
case("an interface ifupdown doesn't have up is left out", msg is None and out is None and left == [])
msg, out, left, mode = run({"state": ["eth1=eth1", "eth0=eth0", "lo=lo"],
                            "ifaces": {"lo=lo": [], "eth0=eth0": ["dns-nameservers: 10.6.6.6"],
                                       "eth1=eth1": ["dns-nameservers: 10.6.6.7"]}})
case("servers in bring-up order (ifupdown lists its state last first)",
     msg is None and out is not None and "DNS=10.6.6.6 10.6.6.7\n" in out)
msg, out, left, mode = run(with_eth0([x.replace("dns-nameservers", "dns-nameserver") for x in ETH0]))
case("dns-nameserver (singular), which resolvconf's and ifupdown's hooks read too", msg is None and out == WANT)
msg, out, left, mode = run(with_eth0([x.replace("dns-nameservers", "d.ns_Nameservers") for x in ETH0]))
case("option names as ifupdown hands them to its hooks (- to _, upper case, others dropped)",
     msg is None and out == WANT)
msg, out, left, mode = run(with_eth0(ETH0 + ["dns-nameservers: 10.0.2.3 2001:db8::53", "dns-domain: example.org."]))
case("several servers, IPv6, dns-domain, duplicates once",
     msg is None and out is not None and "DNS=10.0.2.3 2001:db8::53\n" in out and "Domains=example.org\n" in out)
msg, out, left, mode = run({"state": [], "ifaces": {}})
case("no interface with dns options: nothing written", msg is None and out is None and left == [])
msg, out, left, mode = run(with_eth0(ETH0 + ["dns-nameservers: 10.0.2.4", "10.0.2.5 2001:db8::53"]))
case("an option given twice is one value over two lines (config.c), as ifupdown hands it to the hooks",
     msg is None and out is not None and "DNS=10.0.2.3 10.0.2.4 10.0.2.5 2001:db8::53\n" in out)
msg, out, left, mode = run(with_eth0(ETH0 + ["dns-nameservers: 10.0.2.3"]))
case("one spelling twice (an inet and an inet6 stanza) is fine", msg is None and out == WANT)
msg, out, left, mode = run(with_eth0(["description: uplink\u2028dns-nameservers: 6.6.6.6",
                                      "description: x\x1cdns-search: evil.example"] + ETH0))
case("only a newline ends a line, as in ifquery's output", msg is None and out == WANT)
msg, out, left, mode = run(with_eth0(ETH0 + ["d=ns-search: evil.example"]))
case("an option name ends at its first = (setlocalenv), so d=ns-search isn't DNS_SEARCH", msg is None and out == WANT)
msg, out, left, mode = run(with_eth0([x.replace("example.org", "example.org.") for x in ETH0]))
case("a search domain's trailing dot is dropped", msg is None and out == WANT)
for name, q, why, kw in (
        ("a nameserver that is loopback", with_eth0(ETH0 + ["dns-nameservers: 127.0.0.1"]), "isn't a unicast", {}),
        ("a reserved address", with_eth0(ETH0 + ["dns-nameservers: 240.0.0.1"]), "isn't a unicast", {}),
        ("the broadcast address", with_eth0(ETH0 + ["dns-nameservers: 255.255.255.255"]), "isn't a unicast", {}),
        ("two spellings of one hook variable", with_eth0(ETH0 + ["dns_nameservers: 10.9.9.9"]), "see only the last",
         {}),
        ("a second line of a nameserver value with ': ' in it", with_eth0(ETH0[:3] + ["dns-nameservers: 10.0.2.3",
                                                                                   "10.0.2.4 x: y"]),
         "isn't an address", {}),
        ("a value a hook sees as one word (\\v inside)", with_eth0(ETH0 + ["dns-nameservers: 10.3.3.3\v10.3.3.4"]),
         "isn't an address", {}),
        ("a search value a hook sees as one word (\\xa0 inside)",
         with_eth0(ETH0 + ["dns-search: a.example\xa0b.example"]), "isn't a plain domain", {}),
        ("a second line a hook sees as a word (\\xa0 alone)", with_eth0(ETH0 + ["\xa0"]), "isn't a plain domain",
         {}),
        ("a key with = whose rest leads the hook's value", with_eth0(ETH0[:3] + ["dns-nameservers=9.9.9.9: 1.1.1.1"]),
         "isn't an address", {}),
        ("a second line with a tab in its key", with_eth0(ETH0[:3] + ["dns-nameservers: 10.0.2.3",
                                                                     "dns-name\tservers: 6.6.6.6"]),
         "isn't an address", {}),
        ("a file where the drop-in's directory goes", BOOT, "isn't a directory",
         {"prepare": lambda p: (os.makedirs(os.path.dirname(os.path.dirname(p))),
                                open(os.path.dirname(p), "w").write("x"))}),
        ("a symlinked resolved.conf.d", BOOT, "is a symlink",
         {"prepare": lambda p: (os.makedirs(os.path.dirname(p) + ".real"),
                                os.symlink(os.path.dirname(p) + ".real", os.path.dirname(p)))}),
        ("a symlinked /etc/systemd", BOOT, "is a symlink",
         {"prepare": lambda p: (os.makedirs(os.path.dirname(os.path.dirname(p)) + ".real"),
                                os.symlink(os.path.dirname(os.path.dirname(p)) + ".real",
                                           os.path.dirname(os.path.dirname(p))))}),
        ("a continuation before any option", with_eth0(["10.0.2.3"] + ETH0), "before any option", {}),
        ("a state interface that reads as an option", with_eth0(ETH0, ["lo=lo", "--help=eth0"]), "isn't 'interface=",
         {}),
        ("a state stanza that reads as an option", with_eth0(ETH0, ["lo=lo", "eth0=--help"]), "isn't 'interface=", {}),
        ("a state name with a space", with_eth0(ETH0, ["lo=lo", "eth 0=eth0"]), "isn't 'interface=", {}),
        ("a state line without a stanza", with_eth0(ETH0, ["lo=lo", "eth0"]), "isn't 'interface=", {}),
        ("ifquery printing something that isn't UTF-8", dict(BOOT, raw=b"dns-search: \xff\n".hex()), "isn't UTF-8",
         {}),
        ("no ifquery on the box", BOOT, "ifquery:", {"no_ifquery": True}),
        ("a .wend file in the way", BOOT, ".wend is in the way",
         {"prepare": lambda p: (os.makedirs(os.path.dirname(p)), open(p + ".wend", "w").write("x"))}),
        ("a directory where the drop-in goes", BOOT, "exists already", {"prepare": lambda p: os.makedirs(p)})):
    msg, out, left, mode = run(q, **kw)
    unchanged = left == ([DROP + ".wend"] if ".wend" in why else [os.path.dirname(DROP)] if "directory" in why else [])
    unchanged = unchanged or ("symlink" in why and not any(n.endswith(".conf") for n in left))
    case(f"{name} is refused before anything changes", refused(msg, why) and out is None and unchanged)
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
        ("a second line of a dns value that isn't a domain", with_eth0(ETH0 + ["a;reboot"]),
         "isn't a plain domain")):
    msg, out, left, mode = run(q)
    case(f"{name} is refused before anything changes", refused(msg, why) and out is None and left == [])
sys.exit(0 if all(cases) else 1)
