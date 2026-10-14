"""The EL op net.ifcfg-to-keyfile (SPEC.md, "EL ops"; tools/net_ifcfg_to_keyfile.py) by behaviour, on fixture roots
with a stand-in nmcli that prints the arguments it was given as the keyfile: the exact nmcli arguments for the profiles
CentOS 5's installer writes, the meaning NetworkManager's ifcfg-rh plugin gives each key, the files it ignores, and
that every refusal changes nothing. It runs the PR's own converter, so the gate's schema job (which judges a PR with
the base branch's tools and never runs the PR's code) doesn't run it: `wend check` and the client job do.
python3 tools/test_net_ifcfg.py"""
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import sys
import tempfile

sys.dont_write_bytecode = True
SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "net_ifcfg_to_keyfile.py")
NMCLI = """#!/bin/sh
for a in "$@"; do [ "$a" = refuse-me ] && { echo "Error: refused" >&2; exit 2; }; done
echo "[connection]"; for a in "$@"; do echo "$a"; done
"""
KS = "DEVICE=eth0\nBOOTPROTO=static\nHWADDR=52:54:00:12:34:56\nIPADDR=10.0.2.20\nNETMASK=255.255.255.0\nONBOOT=yes\n"
GUI = ("# Realtek Semiconductor Co., Ltd. RTL-8139/8139C/8139C+\nDEVICE=eth0\nBOOTPROTO=none\nBROADCAST=10.0.2.255\n"
       "HWADDR=52:54:00:12:34:56\nIPADDR=10.0.2.20\nIPV6INIT=yes\nIPV6_AUTOCONF=yes\nNETMASK=255.255.255.0\n"
       "NETWORK=10.0.2.0\nONBOOT=yes\n")
NET = "NETWORKING=yes\nNETWORKING_IPV6=no\nHOSTNAME=oldbox.example.org\nGATEWAY=10.0.2.2\n"
RESOLV = "search example.org\nnameserver 10.0.2.3\n"
STATIC = ["type", "ethernet", "con-name", "System eth0", "ifname", "eth0", "ethernet.mac-address", "52:54:00:12:34:56",
          "ipv4.method", "manual", "ipv4.addresses", "10.0.2.20/24", "ipv4.gateway", "10.0.2.2", "ipv4.dns", "10.0.2.3",
          "ipv4.dns-search", "example.org"]


def run(profiles, net=NET, resolv=RESOLV, keyfiles=(), links=(), vendor_links=(), link_dirs=None):
    """Run the converter on a fresh root; return (exit message or None, {keyfile: nmcli args}, files left, printed)."""
    loader = importlib.machinery.SourceFileLoader("net_ifcfg_to_keyfile", SRC)
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(m)
    with tempfile.TemporaryDirectory() as t:
        for k in ("D", "K", "L", "SAVED", "NETWORK", "RESOLV"):
            setattr(m, k, t + getattr(m, k))
        m.LINK_DIRS = tuple(t + d for d in m.LINK_DIRS)
        os.makedirs(m.D)
        for d, names in [(m.K, keyfiles), (m.L, links), (t + "/usr/lib/systemd/network", vendor_links)] + [
                (t + d, names) for d, names in (link_dirs or {}).items()]:
            for n in names:
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, n), "w") as out:  # names may map to a .link's text
                    out.write(names[n] if isinstance(names, dict) else "")
        os.makedirs(os.path.dirname(m.NETWORK), exist_ok=True)
        for name, text in profiles.items():
            with open(os.path.join(m.D, name), "w") as out:
                out.write(text)
        if net is not None:
            with open(m.NETWORK, "w") as out:
                out.write(net)
        with open(m.RESOLV, "w") as out:
            out.write(resolv)
        os.makedirs(f"{t}/bin")
        with open(f"{t}/bin/nmcli", "w") as out:
            out.write(NMCLI)
        os.chmod(f"{t}/bin/nmcli", 0o755)
        path, msg, printed = os.environ["PATH"], None, io.StringIO()
        os.environ["PATH"] = f"{t}/bin:{path}"
        try:
            with contextlib.redirect_stdout(printed):
                m.main()
        except SystemExit as e:
            msg = str(e.code)
        except Exception as e:  # a traceback is a defect too: it prints WRONG, not a crash of this test
            msg = f"EXCEPTION {e!r}"
        finally:
            os.environ["PATH"] = path
        keyfiles, links = {}, {}
        if os.path.isdir(m.L):
            for n in sorted(os.listdir(m.L)):
                f = os.path.join(m.L, n)
                links[n] = (oct(os.stat(f).st_mode & 0o777), open(f).read())
        if os.path.isdir(m.K):
            for n in sorted(os.listdir(m.K)):
                f = os.path.join(m.K, n)
                keyfiles[n] = (oct(os.stat(f).st_mode & 0o777), open(f).read().splitlines()[4:])  # after "[connection]", "--offline connection add"
        left = sorted(os.path.relpath(os.path.join(r, n), t).replace(t.lstrip("/") + "/", "")  # SAVED keeps the path
                      for r, _, ns in os.walk(t) for n in ns if not r.startswith(f"{t}/bin"))
        run.links = links
        return msg, keyfiles, left, printed.getvalue()


def arg(kf, name, key):
    """the value nmcli was given for key, or None"""
    a = kf.get(name, (None, []))[1]
    return a[a.index(key) + 1] if key in a[:-1] else None


def refused(msg, why):
    return msg is not None and msg.endswith(", nothing changed") and why in msg


cases = []


def case(name, ok):
    cases.append(ok)
    print(("passed  " if ok else "WRONG   ") + "net.ifcfg-to-keyfile: " + name)


# the positive control: what the VM tier's kickstart writes, the route's own box
msg, kf, left, out = run({"ifcfg-eth0": KS, "ifcfg-lo": "DEVICE=lo\nIPADDR=127.0.0.1\n"})
case("a kickstart static profile becomes the exact keyfile, pinned by MAC, the ifcfg saved",
     msg is None and kf == {"eth0.nmconnection": ("0o600", STATIC + ["ipv6.method", "ignore",
                                                                     "connection.autoconnect", "yes"])}
     and run.links == {"10-wend-eth0.link": ("0o644", "[Match]\nMACAddress=52:54:00:12:34:56\n\n[Link]\nName=eth0\n")}
     and "var/lib/wend/saved/etc/sysconfig/network-scripts/ifcfg-eth0" in left
     and "etc/sysconfig/network-scripts/ifcfg-eth0" not in left and "etc/sysconfig/network-scripts/ifcfg-lo" in left)
msg, kf, left, out = run({"ifcfg-eth0": GUI})
case("the GUI installer's profile (BROADCAST, NETWORK, IPV6_AUTOCONF) converts",
     msg is None and kf["eth0.nmconnection"][1] == STATIC + ["ipv6.method", "auto", "ipv6.addr-gen-mode",
                                                            "default-or-eui64", "connection.autoconnect", "yes"])
msg, kf, left, out = run({"ifcfg-eth0": GUI.replace("BROADCAST=10.0.2.255", "BROADCAST=10.0.9.255")})
case("a BROADCAST that doesn't match the address is refused", refused(msg, "BROADCAST=10.0.9.255") and not kf)
msg, kf, left, out = run({"ifcfg-eth0": KS + "IPV6INIT=yes\nIPV6_AUTOCONF=no\n"})
case("IPV6_AUTOCONF=no is link-local", msg is None and kf["eth0.nmconnection"][1][-6:-4] == ["ipv6.method",
                                                                                             "link-local"])
LEFTOVERS = ("ifcfg-eth0.bak", "ifcfg-eth0.BAK", "ifcfg-eth0~", "ifcfg-eth0.orig", "ifcfg-eth0.Rej", "ifcfg-eth0.rpmnew",
             "ifcfg-eth0.augnew", "ifcfg-eth0.augtmp", "ifcfg-eth0;5f3a9b2c")
msg, kf, left, out = run({"ifcfg-eth0": KS, **{n: KS.replace(".20", ".99") for n in LEFTOVERS}})
case("backups and rpm leftovers NetworkManager ignores (any case) are left alone",
     msg is None and list(kf) == ["eth0.nmconnection"]
     and all(f"etc/sysconfig/network-scripts/{n}" in left for n in LEFTOVERS))
msg, kf, left, out = run({"ifcfg-eth0": "DEVICE=eth0\nHWADDR=52:54:00:12:34:57\nBOOTPROTO=dhcp\nPEERDNS=no\nDNS1=10.0.0.53\nDOMAIN=corp.example\n"})
case("DHCP keeps its pinned resolvers and search domain, and ignores the server's",
     msg is None and kf["eth0.nmconnection"][1] == [
         "type", "ethernet", "con-name", "System eth0", "ifname", "eth0", "ethernet.mac-address", "52:54:00:12:34:57",
         "ipv4.method", "auto", "ipv4.dns",
         "10.0.0.53", "ipv4.dns-search", "corp.example", "ipv4.ignore-auto-dns", "yes", "ipv6.method", "ignore",
         "connection.autoconnect", "yes"])
msg, kf, left, out = run({"ifcfg-eth0": "DEVICE=eth0\nHWADDR=52:54:00:12:34:57\nBOOTPROTO=dhcp\n"})
case("plain DHCP takes the server's resolvers, not resolv.conf's",
     msg is None and "ipv4.dns" not in kf["eth0.nmconnection"][1])
msg, kf, left, out = run({"ifcfg-eth0": KS, "ifcfg-eth1": "DEVICE=eth1\nHWADDR=52:54:00:12:34:57\nIPADDR=192.168.9.5\nPREFIX=24\n"
                          "GATEWAY=192.168.9.1\nDNS1=192.168.9.1\n"}, net=NET + "GATEWAYDEV=eth0\n")
case("only GATEWAYDEV gets the default route",
     msg is None and arg(kf, "eth0.nmconnection", "ipv4.never-default") is None
     and arg(kf, "eth1.nmconnection", "ipv4.never-default") == "yes")
msg, kf, left, out = run({"ifcfg-eth0": KS}, net=NET + "GATEWAYDEV=\n")
case("an empty GATEWAYDEV is unset", msg is None and arg(kf, "eth0.nmconnection", "ipv4.never-default") is None
     and arg(kf, "eth0.nmconnection", "ipv4.gateway") == "10.0.2.2")
msg, kf, left, out = run({"ifcfg-eth0": KS + "UUID=5FB06BD0-0BB0-7FFB-45F1-D6EDD65F3E03\n"})
case("UUID becomes the profile's uuid",
     msg is None and arg(kf, "eth0.nmconnection", "connection.uuid") == "5fb06bd0-0bb0-7ffb-45f1-d6edd65f3e03")
msg, kf, left, out = run({"ifcfg-eth0": "DEVICE=eth0\nHWADDR=52:54:00:12:34:57\nBOOTPROTO=bootp\n"})
case("bootp is dhcp", msg is None and arg(kf, "eth0.nmconnection", "ipv4.method") == "auto")
msg, kf, left, out = run({"ifcfg-eth0": KS}, resolv="nameserver 10.0.2.3\ndomain corp.example\n")
case("resolv.conf's domain line is the search list",
     msg is None and arg(kf, "eth0.nmconnection", "ipv4.dns-search") == "corp.example")
msg, kf, left, out = run({"ifcfg-eth0": KS}, resolv="nameserver 10.0.2.3\nsearch .\n")
case("resolv.conf's search . (systemd-resolved's stub) is no search list",
     msg is None and arg(kf, "eth0.nmconnection", "ipv4.dns-search") is None
     and arg(kf, "eth0.nmconnection", "ipv4.dns") == "10.0.2.3")
msg, kf, left, out = run({"ifcfg-eth0": KS + "IPV6_AUTOCONF=no\n"}, net=NET.replace("NETWORKING_IPV6=no", "IPV6INIT=yes"))
case("IPV6INIT falls back to /etc/sysconfig/network", msg is None and arg(kf, "eth0.nmconnection", "ipv6.method")
     == "link-local")
msg, kf, left, out = run({"ifcfg-eth0": KS.replace("ONBOOT=yes", "ONBOOT=T")})
case("t is a yes", msg is None and arg(kf, "eth0.nmconnection", "connection.autoconnect") == "yes")
msg, kf, left, out = run({"ifcfg-eth0": KS, "ifcfg-eth1": "DEVICE=eth1\nHWADDR=52:54:00:12:34:57\nIPADDR=192.168.9.5\nPREFIX=24\nNAME=refuse-me\n"})
case("nmcli refusing the second profile changes nothing, not even the first",
     refused(msg, "refused") and not kf and "etc/sysconfig/network-scripts/ifcfg-eth0" in left
     and not any(x.startswith(("var/lib/wend", "etc/systemd")) for x in left))
msg, kf, left, out = run({"ifcfg-eth0": KS.replace("ONBOOT=yes", "ONBOOT=false") + "IPV6INIT=true\n"})
case("booleans as NetworkManager reads them (false, true)",
     msg is None and kf["eth0.nmconnection"][1][-6:] == ["ipv6.method", "auto", "ipv6.addr-gen-mode",
                                                         "default-or-eui64", "connection.autoconnect", "no"])
for name, text, why in (
        ("a shell expansion", KS.replace("ONBOOT=yes", "ONBOOT=$(reboot)"), "isn't one plain value"),
        ("a backquote", KS.replace("ONBOOT=yes", "ONBOOT=`reboot`"), "isn't one plain value"),
        ("a NETWORK that doesn't match", KS + "NETWORK=10.0.3.0\n", "NETWORK=10.0.3.0 doesn't match"),
        ("no DEVICE", KS.replace("DEVICE=eth0\n", ""), "no DEVICE"),
        ("a device name past 15 characters", KS.replace("DEVICE=eth0", "DEVICE=eth0123456789abc"), "not a plain"),
        ("NM_CONTROLLED=no", KS + "NM_CONTROLLED=no\n", "NM_CONTROLLED=no isn't converted"),
        ("DNS2 with no DNS1", KS + "DNS2=10.0.0.53\n", "DNS2 with no DNS1"),
        ("a bad UUID", KS + "UUID=not-a-uuid\n", "isn't a UUID"),
        ("a bad MAC", KS.replace("52:54:00:12:34:56", "52:54:00:12:34"), "isn't a MAC address"),
        ("a non-Ethernet TYPE", KS + "TYPE=Wireless\n", "only Ethernet"),
        ("a bad search domain", KS + "DOMAIN=-bad.example\n", "search domain isn't a plain name"),
        ("a # inside a value", KS + "NAME=lan#1\n", "isn't one plain value"),
        ("an unbalanced quote", KS + "NAME=\"lan\n", "No closing quotation"),
        ("a boolean that isn't one", KS.replace("ONBOOT=yes", "ONBOOT=maybe"), "isn't yes or no"),
        ("a bridge", KS + "BRIDGE=br0\n", "doesn't convert: ['BRIDGE']"),
        ("a host mask", KS.replace("255.255.255.0", "0.0.0.255"), "isn't a netmask"),
        ("a prefix past 32", KS.replace("NETMASK=255.255.255.0", "PREFIX=40"), "isn't 0 to 32"),
        ("a static address on DHCP", "DEVICE=eth0\nHWADDR=52:54:00:12:34:57\nBOOTPROTO=dhcp\nIPADDR=10.0.2.20\n", "IPADDR beside"),
        ("no HWADDR", KS.replace("HWADDR=52:54:00:12:34:56\n", ""), "no HWADDR to pin eth0 to"),
        ("a dot as the device", KS.replace("DEVICE=eth0", "DEVICE=.."), "not a plain interface"),
        ("an alias", KS, "not a plain interface")):
    profiles = {"ifcfg-eth0:1" if name == "an alias" else "ifcfg-eth0": text}
    msg, kf, left, out = run(profiles)
    case(f"{name} is refused before anything changes", refused(msg, why) and not kf and len(left) == 3)
msg, kf, left, out = run({"ifcfg-eth0": KS.replace("12:34:56", "AB:CD:EF")})
case("HWADDR is lower-cased", msg is None and arg(kf, "eth0.nmconnection", "ethernet.mac-address") == "52:54:00:ab:cd:ef")
msg, kf, left, out = run({"ifcfg-eth0": KS}, keyfiles=["eth0.nmconnection"])
case("a keyfile already there is refused", refused(msg, "exists already") and "etc/sysconfig/network-scripts/ifcfg-eth0"
     in left and not any(x.startswith(("var/lib/wend", "etc/systemd")) for x in left))
msg, kf, left, out = run({"ifcfg-eth0": KS, "ifcfg-eth1;5f3a9b2c9": KS})
case("a ; and 9 hex digits isn't an rpm leftover, so it's read (and refused as a name)",
     refused(msg, "not a plain") and not kf)
OLD_NIC = KS.replace("12:34:56", "99:99:99").replace(".20", ".21")
msg, kf, left, out = run({"ifcfg-eth0": KS, "ifcfg-eth0-old": OLD_NIC})
case("two profiles pinning eth0 to different MACs are refused", refused(msg, "pin eth0 differently") and not kf
     and not run.links)
msg, kf, left, out = run({"ifcfg-eth0": KS, "ifcfg-eth1": KS.replace("DEVICE=eth0", "DEVICE=eth1").replace(".20", ".21")})
case("two names pinned to one MAC are refused", refused(msg, "pin 52:54:00:12:34:56 differently") and not kf)
msg, kf, left, out = run({"ifcfg-eth0": KS, "ifcfg-eth0-dhcp": "DEVICE=eth0\nHWADDR=52:54:00:12:34:56\nBOOTPROTO=dhcp\n"
                          "ONBOOT=no\n"})
case("two profiles for one NIC share its pin", msg is None and len(kf) == 2 and run.links == {
    "10-wend-eth0.link": ("0o644", "[Match]\nMACAddress=52:54:00:12:34:56\n\n[Link]\nName=eth0\n")})
msg, kf, left, out = run({"ifcfg-a": KS, "ifcfg-b": KS.replace("DEVICE=eth0", "DEVICE=eth1").replace("12:34:56", "12:34:57"),
                          "ifcfg-c": OLD_NIC})
case("a conflict between profiles that aren't next to each other is refused",
     refused(msg, "ifcfg-c and") and "pin eth0 differently" in msg and not kf and not run.links)
msg, kf, left, out = run({"ifcfg-eth0": KS}, links=["10-admin.link"])
case("an earlier .link, which systemd would apply instead, is refused",
     refused(msg, "10-admin.link comes before 10-wend-eth0.link") and not kf)
ANACONDA = "# Generated by Anaconda based on ifname= installer boot option.\n[Match]\nMACAddress=52:54:00:12:34:56\n[Link]\nName=eth0\n"
msg, kf, left, out = run({"ifcfg-eth0": KS}, links={"10-anaconda-ifname-eth0.link": ANACONDA})
case("EL8's installer pin of the same MAC to the same name stands; no second pin is written",
     msg is None and list(kf) == ["eth0.nmconnection"] and list(run.links) == ["10-anaconda-ifname-eth0.link"]
     and "already, by" in out)
msg, kf, left, out = run({"ifcfg-eth0": KS}, links={"10-anaconda-ifname-eth1.link": ANACONDA.replace(
    "34:56", "34:57").replace("eth0", "eth1")})
case("a pin of another MAC to another name doesn't stand in the way", msg is None and list(kf) == ["eth0.nmconnection"]
     and "10-wend-eth0.link" in run.links)
for why, text in (("another name", ANACONDA.replace("Name=eth0", "Name=eth1")),
                  ("another MAC", ANACONDA.replace("34:56", "34:57")),
                  ("more than a pin", ANACONDA + "MTUBytes=9000\n"),
                  ("another match", ANACONDA.replace("[Match]\n", "[Match]\nDriver=e1000\n")),
                  ("two MACs", ANACONDA.replace("34:56", "34:56 52:54:00:12:34:57")),
                  ("a repeated key", ANACONDA + "Name=eth1\n")):
    msg, kf, left, out = run({"ifcfg-eth0": KS}, links={"10-anaconda-ifname-eth0.link": text})
    case(f"an earlier .link pinning {why} is refused",
         refused(msg, "10-anaconda-ifname-eth0.link comes before 10-wend-eth0.link") and not kf)
msg, kf, left, out = run({"ifcfg-eth0": KS}, link_dirs={"/run/systemd/network": {"10-anaconda-ifname-eth0.link": ANACONDA}})
case("the same pin in /run, gone at the next boot, is refused", refused(msg, "10-anaconda-ifname-eth0.link comes before")
     and not kf)
msg, kf, left, out = run({"ifcfg-eth0": KS}, vendor_links=["05-vendor.link"])
case("an earlier .link in /usr/lib/systemd/network is refused too", refused(msg, "05-vendor.link comes before") and not kf)
for d in ("/run/systemd/network", "/usr/local/lib/systemd/network", "/lib/systemd/network"):
    msg, kf, left, out = run({"ifcfg-eth0": KS}, link_dirs={d: ["05-admin.link"]})
    case(f"an earlier .link in {d} is refused", refused(msg, f"{d}/05-admin.link comes before") and not kf)
msg, kf, left, out = run({"ifcfg-eth0": KS}, vendor_links=["10-wend-eth0.link", "05-foo.network"])
case("the pin's own name in /usr/lib (overridden by /etc's) and an earlier .network don't stand in the way",
     msg is None and list(kf) == ["eth0.nmconnection"])
msg, kf, left, out = run({"ifcfg-eth0": KS}, links=["70-later.link"], vendor_links=["99-default.link"])
case("later .link files (99-default) don't stand in the way", msg is None and list(kf) == ["eth0.nmconnection"])
msg, kf, left, out = run({"ifcfg-eth0": KS}, links=["10-wend-eth0.link"])
case("a pin already there is refused, not replaced", refused(msg, "10-wend-eth0.link exists already") and not kf
     and [(n, t) for n, (_, t) in run.links.items()] == [("10-wend-eth0.link", "")])
msg, kf, left, out = run({"ifcfg-eth0": KS}, net=NET + "IPV6INIT=maybe\n")
case("a bad boolean in /etc/sysconfig/network is refused, not a traceback",
     refused(msg, "IPV6INIT=maybe isn't yes or no") and not kf)
msg, kf, left, out = run({"ifcfg-eth0": KS}, net=NET + "RES_OPTIONS=rotate\n")
case("a /etc/sysconfig/network setting NetworkManager reads and this op doesn't convert is refused",
     refused(msg, "doesn't convert: ['RES_OPTIONS']") and not kf)
msg, kf, left, out = run({"ifcfg-eth0": KS}, resolv="nameserver fe80::1\n")
case("an IPv6 resolver in resolv.conf is refused, not a traceback", refused(msg, "isn't an IPv4 address") and not kf)
msg, kf, left, out = run({"ifcfg-eth0": KS, "route-eth0": "10.9.0.0/16 via 10.0.2.1\n"})
case("static routes beside a profile are refused", refused(msg, "route-eth0 beside it") and not kf)
# what EL8's installer writes (through NetworkManager's ifcfg-rh plugin): its defaults spelled out, values unquoted
EL8 = ("TYPE=Ethernet\nPROXY_METHOD=none\nBROWSER_ONLY=no\nBOOTPROTO=none\nDEFROUTE=yes\nIPV4_FAILURE_FATAL=no\n"
       "IPV6INIT=no\nIPV6_DEFROUTE=yes\nIPV6_FAILURE_FATAL=no\nNAME=eth0\nUUID=5fb06bd0-0bb0-7ffb-45f1-d6edd65f3e03\n"
       "DEVICE=eth0\nONBOOT=yes\nIPADDR=10.0.2.20\nPREFIX=24\nGATEWAY=10.0.2.2\nDNS1=10.0.2.3\n"
       "HWADDR=52:54:00:12:34:56\n")
msg, kf, left, out = run({"ifcfg-eth0": EL8})
case("EL8's installer profile, its defaults spelled out, becomes the plain keyfile",
     msg is None and kf["eth0.nmconnection"][1] == [
         "type", "ethernet", "con-name", "eth0", "ifname", "eth0", "ethernet.mac-address", "52:54:00:12:34:56",
         "connection.uuid", "5fb06bd0-0bb0-7ffb-45f1-d6edd65f3e03", "ipv4.method", "manual", "ipv4.addresses",
         "10.0.2.20/24", "ipv4.gateway", "10.0.2.2", "ipv4.dns", "10.0.2.3", "ipv6.method", "ignore",
         "connection.autoconnect", "yes"])
msg, kf, left, out = run({"ifcfg-eth0": EL8.replace("DEFROUTE=yes", "DEFROUTE=no").replace(
    "IPV4_FAILURE_FATAL=no", "IPV4_FAILURE_FATAL=yes").replace("IPV6INIT=no", "IPV6INIT=yes").replace(
    "IPV6_DEFROUTE=yes", "IPV6_DEFROUTE=no").replace("IPV6_FAILURE_FATAL=no", "IPV6_FAILURE_FATAL=yes")
    + "IPV6_ADDR_GEN_MODE=stable-privacy\n"})
case("DEFROUTE, IPV4_FAILURE_FATAL, IPV6_DEFROUTE, IPV6_FAILURE_FATAL and IPV6_ADDR_GEN_MODE keep their meaning",
     msg is None and arg(kf, "eth0.nmconnection", "ipv4.never-default") == "yes"
     and arg(kf, "eth0.nmconnection", "ipv4.may-fail") == "no"
     and kf["eth0.nmconnection"][1][-10:] == ["ipv6.method", "auto", "ipv6.never-default", "yes", "ipv6.may-fail", "no",
                                              "ipv6.addr-gen-mode", "stable-privacy", "connection.autoconnect", "yes"])
msg, kf, left, out = run({"ifcfg-eth0": EL8.replace("DEFROUTE=yes", "DEFROUTE=no")}, net=NET + "GATEWAYDEV=eth0\n")
case("GATEWAYDEV overrides DEFROUTE=no, as the plugin reads it",
     msg is None and arg(kf, "eth0.nmconnection", "ipv4.never-default") is None)
msg, kf, left, out = run({"ifcfg-eth0": EL8.replace("DEFROUTE=yes", "DEFROUTE=no")})
case("DEFROUTE=no with no GATEWAYDEV is never-default",
     msg is None and arg(kf, "eth0.nmconnection", "ipv4.never-default") == "yes")
for name, text, why in (
        ("a proxy", EL8.replace("PROXY_METHOD=none", "PROXY_METHOD=auto"), "PROXY_METHOD=auto isn't converted"),
        ("BROWSER_ONLY=yes", EL8.replace("BROWSER_ONLY=no", "BROWSER_ONLY=yes"), "BROWSER_ONLY=yes isn't converted"),
        ("a DEFROUTE that isn't a boolean", EL8.replace("DEFROUTE=yes", "DEFROUTE=maybe"), "DEFROUTE=maybe isn't yes"),
        ("an IPV6_FAILURE_FATAL that isn't a boolean, IPv6 off",
         EL8.replace("IPV6_FAILURE_FATAL=no", "IPV6_FAILURE_FATAL=2"), "IPV6_FAILURE_FATAL=2 isn't yes"),
        ("an unknown IPV6_ADDR_GEN_MODE", EL8 + "IPV6_ADDR_GEN_MODE=random\n", "isn't stable-privacy or eui64"),
        ("an IPV6_ADDR_GEN_MODE in the wrong case", EL8 + "IPV6_ADDR_GEN_MODE=EUI64\n", "isn't stable-privacy or eui64")):
    msg, kf, left, out = run({"ifcfg-eth0": text})
    case(f"EL8: {name} is refused before anything changes", refused(msg, why) and not kf and len(left) == 3)
msg, kf, left, out = run({})
case("no profiles: nothing to convert", msg is None and not kf and out.startswith("no ifcfg profiles"))
sys.exit(0 if all(cases) else 1)
