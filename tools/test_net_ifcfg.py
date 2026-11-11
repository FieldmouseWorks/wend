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


IP = """#!/bin/sh
cat "$(dirname "$0")/addr-$6" 2>/dev/null
"""
NIC = {"type": "1", "addr_assign_type": "0", "address": "52:54:00:12:34:56"}
ON_NIC = "2: eth0    inet 10.0.2.20/24 brd 10.0.2.255 scope global noprefixroute eth0\\       valid_lft forever\n"


def run(profiles, net=NET, resolv=RESOLV, keyfiles=(), links=(), vendor_links=(), link_dirs=None, nics=None,
        addrs=None, hostname=None, symlinks=(), udev=None, write_error=False, cmdline=""):
    """Run the converter on a fresh root; return (exit message or None, {keyfile: nmcli args}, files left, printed).
    nics: {name: sysfs attributes} of the running NICs (a "device" link unless physical=False); addrs: {name: what
    `ip -o -4 addr show dev <name>` prints}."""
    loader = importlib.machinery.SourceFileLoader("net_ifcfg_to_keyfile", SRC)
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(m)
    with tempfile.TemporaryDirectory() as t:
        for k in ("D", "K", "L", "SAVED", "NETWORK", "RESOLV", "SYS", "HOSTNAME", "CMDLINE"):
            setattr(m, k, t + getattr(m, k))
        m.LINK_DIRS = tuple(t + d for d in m.LINK_DIRS)
        m.UDEV_DIRS = tuple(t + d for d in m.UDEV_DIRS)
        os.makedirs(os.path.dirname(m.CMDLINE), exist_ok=True)
        with open(m.CMDLINE, "w") as out:
            out.write(cmdline)
        os.makedirs(m.D)
        for d, names in [(m.K, keyfiles), (m.L, links), (t + "/usr/lib/systemd/network", vendor_links)] + [
                (t + d, names) for d, names in (link_dirs or {}).items()]:
            for n in names:
                os.makedirs(os.path.dirname(os.path.join(d, n)), exist_ok=True)  # a name may be in a drop-in dir
                with open(os.path.join(d, n), "w") as out:  # names may map to a .link's text
                    out.write(names[n] if isinstance(names, dict) else "")
                os.chmod(os.path.join(d, n), 0o644)  # as installers write them, whatever the umask (wend#128)
        os.makedirs(os.path.dirname(m.NETWORK), exist_ok=True)
        for name, text in profiles.items():
            with open(os.path.join(m.D, name), "w") as out:
                out.write(text)
        if net is not None:
            with open(m.NETWORK, "w") as out:
                out.write(net)
        with open(m.RESOLV, "w") as out:
            out.write(resolv)
        if hostname is not None:
            with open(m.HOSTNAME, "w") as out:
                out.write(hostname)
        os.makedirs(f"{t}/bin")
        with open(f"{t}/bin/nmcli", "w") as out:
            out.write(NMCLI)
        os.chmod(f"{t}/bin/nmcli", 0o755)
        with open(f"{t}/bin/ip", "w") as out:
            out.write(IP)
        os.chmod(f"{t}/bin/ip", 0o755)
        for n, text in (addrs or {}).items():
            with open(f"{t}/bin/addr-{n}", "w") as out:
                out.write(text)
        for n, attrs in (nics or {}).items():
            os.makedirs(os.path.join(m.SYS, n))
            if attrs.get("physical", True):
                os.makedirs(os.path.join(m.SYS, n, "device"))
            for a, x in attrs.items():
                if a != "physical":
                    with open(os.path.join(m.SYS, n, a), "w") as out:
                        out.write(x + "\n")
        for relative in symlinks:
            target = t + relative
            os.makedirs(os.path.dirname(target), exist_ok=True)
            os.symlink("/missing-wend-fixture", target)
        for name, text in (udev or {}).items():
            target = t + name if name.startswith("/") else os.path.join(m.UDEV_DIRS[0], name)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if text is None:
                os.symlink("/dev/null", target)
                continue
            with open(target, "w") as out:
                out.write(text)
        if write_error:
            real_write = m.write
            def broken_write(path, text, mode):
                if path.endswith(".link"):
                    raise OSError(28, "No space left on device")
                real_write(path, text, mode)
            m.write = broken_write
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
                if os.path.isdir(f) or os.path.islink(f):  # a drop-in dir or dangling link
                    continue
                links[n] = (oct(os.stat(f).st_mode & 0o777), open(f).read())
        if os.path.isdir(m.K):
            for n in sorted(os.listdir(m.K)):
                f = os.path.join(m.K, n)
                if os.path.islink(f):
                    continue
                keyfiles[n] = (oct(os.stat(f).st_mode & 0o777), open(f).read().splitlines()[4:])  # after "[connection]", "--offline connection add"
        left = sorted(os.path.relpath(os.path.join(r, n), t).replace(t.lstrip("/") + "/", "")  # SAVED keeps the path
                      for r, _, ns in os.walk(t) for n in ns if not r.startswith((f"{t}/bin", f"{t}/sys", f"{t}/proc")))
        run.udev = {os.path.relpath(os.path.join(d, n), t):
                    (oct(os.stat(os.path.join(d, n)).st_mode & 0o777), open(os.path.join(d, n)).read())
                    for d in m.UDEV_DIRS if os.path.isdir(d) for n in os.listdir(d)
                    if os.path.isfile(os.path.join(d, n)) and not os.path.islink(os.path.join(d, n))}
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
     and run.links == {"10-wend-eth0.link": ("0o644", "[Match]\nMACAddress=52:54:00:12:34:56\nType=ether\n\n[Link]\nName=eth0\n")}
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
        ("no HWADDR and no NIC by that name", KS.replace("HWADDR=52:54:00:12:34:56\n", ""), "no physical NIC named eth0"),
        ("a dot as the device", KS.replace("DEVICE=eth0", "DEVICE=.."), "not a plain interface"),
        ("an alias", KS, "not a plain interface")):
    profiles = {"ifcfg-eth0:1" if name == "an alias" else "ifcfg-eth0": text}
    msg, kf, left, out = run(profiles)
    case(f"{name} is refused before anything changes", refused(msg, why) and not kf and len(left) == 3)
# wend#74: EL7's installer writes no HWADDR unless told the NIC by MAC; the NIC running the profile is pinned
STOCK = KS.replace("HWADDR=52:54:00:12:34:56\n", "")
msg, kf, left, out = run({"ifcfg-eth0": STOCK}, nics={"eth0": NIC}, addrs={"eth0": ON_NIC})
case("no HWADDR: the NIC named eth0 that has the static address now is pinned by its own MAC",
     msg is None and kf == {"eth0.nmconnection": ("0o600", STATIC + ["ipv6.method", "ignore",
                                                                     "connection.autoconnect", "yes"])}
     and run.links == {"10-wend-eth0.link": ("0o644", "[Match]\nMACAddress=52:54:00:12:34:56\nType=ether\n\n[Link]\nName=eth0\n")})
msg, kf, left, out = run({"ifcfg-eth0": STOCK}, nics={"eth0": {**NIC, "address": "52:54:00:AB:CD:EF"}},
                         addrs={"eth0": ON_NIC})
case("no HWADDR: the running NIC's MAC is lower-cased",
     msg is None and arg(kf, "eth0.nmconnection", "ethernet.mac-address") == "52:54:00:ab:cd:ef")
msg, kf, left, out = run({"ifcfg-eth0": "DEVICE=eth0\nBOOTPROTO=dhcp\n"}, nics={"eth0": NIC})
case("no HWADDR on DHCP: the NIC named eth0 is pinned (no address to compare)",
     msg is None and arg(kf, "eth0.nmconnection", "ethernet.mac-address") == "52:54:00:12:34:56"
     and "10-wend-eth0.link" in run.links)
for why, nics, addrs, text in (
        ("a NIC that isn't physical", {"eth0": {**NIC, "physical": False}}, {"eth0": ON_NIC}, "no physical NIC"),
        ("a random MAC", {"eth0": {**NIC, "addr_assign_type": "1"}}, {"eth0": ON_NIC}, "permanent MAC"),
        ("a MAC someone set", {"eth0": {**NIC, "addr_assign_type": "3"}}, {"eth0": ON_NIC}, "permanent MAC"),
        ("a NIC that isn't Ethernet", {"eth0": {**NIC, "type": "772"}}, {"eth0": ON_NIC}, "permanent MAC"),
        ("a bad MAC in sysfs", {"eth0": {**NIC, "address": "52:54:00:12:34"}}, {"eth0": ON_NIC}, "isn't a MAC"),
        ("another address on the NIC (another machine's eth0)", {"eth0": NIC},
         {"eth0": ON_NIC.replace("10.0.2.20/24", "10.0.2.21/24")}, "doesn't have 10.0.2.20/24 now"),
        ("the address under another prefix", {"eth0": NIC}, {"eth0": ON_NIC.replace("/24", "/16")}, "doesn't have"),
        ("no address on the NIC", {"eth0": NIC}, {}, "doesn't have 10.0.2.20/24 now")):
    msg, kf, left, out = run({"ifcfg-eth0": STOCK}, nics=nics, addrs=addrs)
    case(f"no HWADDR and {why} is refused before anything changes", refused(msg, text) and not kf and not run.links)
msg, kf, left, out = run({"ifcfg-eth0": STOCK, "ifcfg-eth0-old": KS.replace("12:34:56", "99:99:99")
                          .replace(".20", ".21")}, nics={"eth0": NIC}, addrs={"eth0": ON_NIC})
case("no HWADDR beside a profile pinning eth0 to another MAC is refused", refused(msg, "pin eth0 differently") and not kf)
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
    "10-wend-eth0.link": ("0o644", "[Match]\nMACAddress=52:54:00:12:34:56\nType=ether\n\n[Link]\nName=eth0\n")})
msg, kf, left, out = run({"ifcfg-a": KS, "ifcfg-b": KS.replace("DEVICE=eth0", "DEVICE=eth1").replace("12:34:56", "12:34:57"),
                          "ifcfg-c": OLD_NIC})
case("a conflict between profiles that aren't next to each other is refused",
     refused(msg, "ifcfg-c and") and "pin eth0 differently" in msg and not kf and not run.links)
msg, kf, left, out = run({"ifcfg-eth0": KS}, links=["10-admin.link"])
case("an earlier .link, which systemd would apply instead, is refused",
     refused(msg, "10-admin.link comes before 10-wend-eth0.link") and not kf)
ANACONDA = "# Generated by Anaconda based on ifname= installer boot option.\n[Match]\nMACAddress=52:54:00:12:34:56\n[Link]\nName=eth0\n"
msg, kf, left, out = run({"ifcfg-eth0": KS}, links={"10-anaconda-ifname-eth0.link": ANACONDA})
case("EL8's installer pin of the same MAC to the same name stands, and ours is written beside it",
     msg is None and list(kf) == ["eth0.nmconnection"] and run.links == {
         "10-anaconda-ifname-eth0.link": ("0o644", ANACONDA),
         "10-wend-eth0.link": ("0o644", "[Match]\nMACAddress=52:54:00:12:34:56\nType=ether\n\n[Link]\nName=eth0\n")})
for d in ("/etc/systemd/network", "/usr/lib/systemd/network"):
    msg, kf, left, out = run({"ifcfg-eth0": KS}, links={"10-anaconda-ifname-eth0.link": ANACONDA},
                             link_dirs={d: {"10-anaconda-ifname-eth0.link.d/name.conf": "[Link]\nName=lan0\n"}})
    case(f"the installer pin with a drop-in in {d} (which systemd merges into it) is refused",
         refused(msg, "10-anaconda-ifname-eth0.link comes before") and not kf)
msg, kf, left, out = run({"ifcfg-eth0": KS}, links={
    "10-anaconda-ifname-eth1.link": ANACONDA.replace("34:56", "34:57").replace("eth0", "eth1"),
    "10-anaconda-ifname-eth1.link.d/mac.conf": "[Match]\nMACAddress=52:54:00:12:34:56\n"})
case("another NIC's pin with a drop-in, which could add this MAC to its list, is refused",
     refused(msg, "10-anaconda-ifname-eth1.link comes before") and not kf)
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
case("the same pin in /run stands too; ours takes over once it's gone", msg is None and "10-wend-eth0.link" in run.links)
msg, kf, left, out = run({"ifcfg-eth0": KS}, vendor_links=["05-vendor.link"])
case("an earlier .link in /usr/lib/systemd/network is refused too", refused(msg, "05-vendor.link comes before") and not kf)
for d in ("/run/systemd/network", "/usr/local/lib/systemd/network", "/lib/systemd/network"):
    msg, kf, left, out = run({"ifcfg-eth0": KS}, link_dirs={d: ["05-admin.link"]})
    case(f"an earlier .link in {d} is refused", refused(msg, f"{d}/05-admin.link comes before") and not kf)
msg, kf, left, out = run({"ifcfg-eth0": KS}, vendor_links=["10-wend-eth0.link"])
case("the pin's own name in /usr/lib is overridden by /etc's",
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
# what EL6's installer writes for a static install: quoted values, NM_CONTROLLED, its MTU and the machine's name
EL6 = ('DEVICE="eth0"\nBOOTPROTO="static"\nHOSTNAME="oldbox.example.org"\nHWADDR="52:54:00:12:34:56"\n'
       'IPADDR="10.0.2.20"\nIPV6INIT="no"\nMTU="1500"\nNETMASK="255.255.255.0"\nNM_CONTROLLED="yes"\nONBOOT="yes"\n'
       'TYPE="Ethernet"\n')
msg, kf, left, out = run({"ifcfg-eth0": EL6})
case("EL6's installer profile converts: MTU to ethernet.mtu, its HOSTNAME (the machine's) dropped",
     msg is None and arg(kf, "eth0.nmconnection", "ethernet.mtu") == "1500"
     and "oldbox.example.org" not in kf["eth0.nmconnection"][1])
msg, kf, left, out = run({"ifcfg-eth0": EL6}, net=NET.replace("HOSTNAME=oldbox.example.org\n", ""),
                         hostname="oldbox.example.org\n")
case("a profile's HOSTNAME that /etc/hostname names is dropped", msg is None and list(kf) == ["eth0.nmconnection"])
for name, text, why in (("a HOSTNAME that isn't the machine's", EL6.replace('"oldbox.', '"other.'),
                         "HOSTNAME=other.example.org isn't the machine's own name"),
                        ("an MTU that isn't a number", EL6.replace('MTU="1500"', 'MTU="jumbo"'), "MTU=jumbo isn't 68"),
                        ("an MTU below IPv4's minimum", EL6.replace('MTU="1500"', 'MTU="60"'), "MTU=60 isn't 68")):
    msg, kf, left, out = run({"ifcfg-eth0": text}, hostname="oldbox.example.org\n")
    case(f"EL6: {name} is refused before anything changes", refused(msg, why) and not kf)
msg, kf, left, out = run({})
case("no profiles: nothing to convert", msg is None and not kf and out.startswith("no ifcfg profiles"))
msg, kf, left, out = run({"ifcfg-eth0": KS}, vendor_links=["05-foo.network"])
case("an earlier non-link suffix is ignored", msg is None and list(kf) == ["eth0.nmconnection"])
msg, kf, left, out = run({"ifcfg-eth0": KS}, links={"70-owner.link": ANACONDA.replace("Name=eth0", "Name=lan0")})
case("later owner pin conflicts are refused", refused(msg, "move the stale pin") and not kf)
# Follow-ups in #34: each refusal must preserve the input and avoid archiving it.
def untouched(msg, kf, left):
    return msg is not None and not kf and "etc/sysconfig/network-scripts/ifcfg-eth0" in left and not any(
        n.startswith("var/lib/wend/saved/") for n in left)

for relative in ("/etc/NetworkManager/system-connections/eth0.nmconnection",
                 "/etc/systemd/network/10-wend-eth0.link",
                 "/etc/NetworkManager/system-connections/eth0.nmconnection.wend",
                 "/etc/systemd/network/10-wend-eth0.link.wend",
                 "/etc/sysconfig/network-scripts/route-eth0"):
    msg, kf, left, out = run({"ifcfg-eth0": KS}, symlinks=[relative])
    case(f"dangling {relative} is refused and kept", untouched(msg, kf, left) and relative.lstrip("/") in left)
msg, kf, left, out = run({"ifcfg-eth0": KS}, write_error=True)
case("staging ENOSPC leaves every original and no installed files", untouched(msg, kf, left) and not run.links
     and not any(n.endswith(".wend") for n in left) and refused(msg, "cannot stage"))
msg, kf, left, out = run({"ifcfg-eth0": KS}, link_dirs={"/etc/systemd/network": {
    "10-wend-eth0.link.d/x.conf": "[Link]\nName=lan0\n"}})
case("drop-ins for the new pin are refused", untouched(msg, kf, left) and refused(msg, ".link.d drop-ins"))
msg, kf, left, out = run({"ifcfg-eth0": KS}, udev={"70-persistent-net.rules":
    'SUBSYSTEM=="net", ATTR{address}=="52:54:00:12:34:56", NAME="lan0"\n'})
case("udev naming rules cannot silently override the pin", untouched(msg, kf, left) and refused(msg, "naming rule"))
msg, kf, left, out = run({"ifcfg-eth0": KS, "ifcfg-eth1":
    "DEVICE=eth1\nHWADDR=52:54:00:12:34:57\nONBOOT=no\nHOTPLUG=no\n"})
case("Anaconda's unconfigured second NIC is disabled", msg is None and
     arg(kf, "eth1.nmconnection", "ipv4.method") == "disabled" and
     arg(kf, "eth1.nmconnection", "ipv4.gateway") is None and arg(kf, "eth1.nmconnection", "ipv4.dns") is None)
msg, kf, left, out = run({"ifcfg-eth0": KS + "TYPE=eThErNeT\n"})
case("TYPE is case insensitive", msg is None)
msg, kf, left, out = run({"ifcfg-eth0": KS.replace("eth0", "lan0")})
case("non eth device needs an explicit TYPE", refused(msg, "needs TYPE=Ethernet") and not kf)
msg, kf, left, out = run({"ifcfg-eth0": "DEVICE=eth0\nHWADDR=52:54:00:12:34:56\nBOOTPROTO=dhcp\n"
    "DHCP_HOSTNAME=oldbox\nDHCP_CLASSID=ignored\nDHCP_VENDOR_CLASS_IDENTIFIER=kickstart\n"})
case("DHCP kickstart options survive", msg is None and arg(kf, "eth0.nmconnection", "ipv4.dhcp-hostname") == "oldbox"
     and arg(kf, "eth0.nmconnection", "ipv4.dhcp-vendor-class-identifier") == "kickstart")
uid = "UUID=5fb06bd0-0bb0-7ffb-45f1-d6edd65f3e03\n"
msg, kf, left, out = run({"ifcfg-eth0": KS + uid, "ifcfg-old": KS + uid})
case("duplicate UUIDs are refused", untouched(msg, kf, left) and refused(msg, "share UUID="))
for text, want in (("domain .\n", None), ("search example.org\nsearch\ndomain\n", "example.org"),
                   ("domain example.org.\n", "example.org")):
    msg, kf, left, out = run({"ifcfg-eth0": KS}, resolv="nameserver 10.0.2.3\n" + text)
    case(f"resolver search semantics: {text!r}", msg is None and arg(kf, "eth0.nmconnection", "ipv4.dns-search") == want)
msg, kf, left, out = run({"ifcfg-eth0": KS}, resolv="nameserver 127.0.0.53\n")
case("loopback fallback needs an explicit resolver migration", untouched(msg, kf, left) and refused(msg, "loopback DNS"))
for domain, want in ((".", None), ("example.org.", "example.org")):
    msg, kf, left, out = run({"ifcfg-eth0": KS + f"DOMAIN={domain}\n"})
    case(f"DOMAIN={domain} normalizes without taking the fallback search list", msg is None and
         arg(kf, "eth0.nmconnection", "ipv4.dns-search") == want)
for rule, allowed in (('NAME="lan0"\n', False),
                      ('SUBSYSTEM=="net", \\\nNAME="lan0"\n', False),
                      ('SUBSYSTEM=="block", NAME="disk0"\n', True)):
    msg, kf, left, out = run({"ifcfg-eth0": KS}, udev={"70-owner.rules": rule})
    case(f"udev assignment scope: {rule!r}", (msg is None and bool(kf)) if allowed else
         untouched(msg, kf, left) and refused(msg, "naming rule"))
# systemd v252 rules.d/80-net-setup-link.rules, including the NAME== guard and dispatcher assignment.
DISPATCHER = '''# do not edit this file, it will be overwritten on update

SUBSYSTEM!="net", GOTO="net_setup_link_end"
IMPORT{builtin}="path_id"
ACTION=="remove", GOTO="net_setup_link_end"
IMPORT{builtin}="net_setup_link"
NAME=="", ENV{ID_NET_NAME}!="", NAME="$env{ID_NET_NAME}"
LABEL="net_setup_link_end"
'''
msg, kf, left, out = run({"ifcfg-eth0": KS}, udev={"80-net-setup-link.rules": DISPATCHER})
case("systemd's distribution dispatcher leaves the .link name effective", msg is None and bool(kf) and
     "Name=eth0" in run.links["10-wend-eth0.link"][1])
for rule in ('NAME=="eth0", ENV{SEEN}="yes"', 'NAME!="", GOTO="done"'):
    msg, kf, left, out = run({"ifcfg-eth0": KS}, udev={"70-test.rules": rule})
    case(f"NAME comparison is not an assignment: {rule}", msg is None and bool(kf))
for directory in ("/etc", "/run", "/usr/local/lib", "/usr/lib", "/lib"):
    name = directory + "/udev/rules.d/90-owner.rules"
    msg, kf, left, out = run({"ifcfg-eth0": KS}, udev={name: 'SUBSYSTEM=="net", NAME="lan0"'})
    case(f"naming assignment in {directory} refuses before archival", untouched(msg, kf, left) and
         refused(msg, "naming rule"))
for override in ("# no assignments here\n", None):
    msg, kf, left, out = run({"ifcfg-eth0": KS}, udev={
        "/etc/udev/rules.d/90-owner.rules": override,
        "/usr/local/lib/udev/rules.d/90-owner.rules": 'NAME="lan0"'})
    case(f"higher-priority override or mask hides vendor assignment: {override!r}", msg is None and bool(kf))
msg, kf, left, out = run({"ifcfg-eth0": KS}, udev={"80-net-setup-link.rules": DISPATCHER + 'NAME="lan0"\n'})
case("custom assignments added to dispatcher are still refused", untouched(msg, kf, left))
for proto in ("dhcp", "bootp"):
    profile = f"DEVICE=eth0\nHWADDR=52:54:00:12:34:56\nBOOTPROTO={proto}\nDHCP_CLASSID=ignored\n"
    msg, kf, left, out = run({"ifcfg-eth0": profile})
    case(f"{proto}: old kickstart DHCP_CLASSID stays inert", msg is None and
         arg(kf, "eth0.nmconnection", "ipv4.dhcp-vendor-class-identifier") is None)
    msg, kf, left, out = run({"ifcfg-eth0": profile + "DHCP_VENDOR_CLASS_IDENTIFIER=effective\n"})
    case(f"{proto}: NetworkManager vendor class survives", msg is None and
         arg(kf, "eth0.nmconnection", "ipv4.dhcp-vendor-class-identifier") == "effective")
# Stock AlmaLinux 9 initscripts-rename-device-10.11.8-4.el9, observed by the booted remi proof.
RENAMER = ('ACTION=="add", SUBSYSTEM=="net", DRIVERS=="?*", ATTR{type}=="1", '
           'PROGRAM="/lib/udev/rename_device", RESULT=="?*", NAME="$result"\n')
for vendor in ("/usr/lib", "/lib"):
    source = vendor + "/udev/rules.d/60-net.rules"
    msg, kf, left, out = run({"ifcfg-eth0": KS}, udev={source: RENAMER,
        "/usr/lib/udev/rules.d/80-net-setup-link.rules": DISPATCHER})
    case(f"stock {vendor} ifcfg renamer stays enabled while .link takes over", msg is None and bool(kf)
         and "etc/udev/rules.d/60-net.rules" not in run.udev
         and run.udev[source.lstrip("/")][1] == RENAMER
         and "Name=eth0" in run.links["10-wend-eth0.link"][1]
         and "etc/sysconfig/network-scripts/ifcfg-eth0" not in left)
for custom in (RENAMER + 'NAME="lan0"\n', RENAMER.replace('/lib/udev/rename_device', '/opt/rename')):
    msg, kf, left, out = run({"ifcfg-eth0": KS}, udev={"/usr/lib/udev/rules.d/60-net.rules": custom})
    case("modified renamer is refused without a mask", untouched(msg, kf, left) and
         "etc/udev/rules.d/60-net.rules" not in run.udev)
msg, kf, left, out = run({"ifcfg-eth0": KS}, write_error=True,
    udev={"/usr/lib/udev/rules.d/60-net.rules": RENAMER})
case("staging failure installs no renamer mask", untouched(msg, kf, left) and
     "etc/udev/rules.d/60-net.rules" not in run.udev)
for runtime in ("# harmless runtime replacement\n", None, 'NAME=="", ENV{OK}="yes"\n'):
    msg, kf, left, out = run({"ifcfg-eth0": KS}, udev={
        "/run/udev/rules.d/90-owner.rules": runtime,
        "/usr/lib/udev/rules.d/90-owner.rules": 'NAME="lan0"'})
    case(f"persistent conflict behind runtime override {runtime!r} is refused", untouched(msg, kf, left))
    msg, kf, left, out = run({"ifcfg-eth0": KS}, udev={
        "/run/udev/rules.d/60-net.rules": runtime,
        "/usr/lib/udev/rules.d/60-net.rules": RENAMER}, cmdline="ifname=lan0:52:54:00:12:34:56")
    case(f"helper behind runtime override {runtime!r} still validates its bindings", untouched(msg, kf, left)
         and refused(msg, "helper binding"))
for cmdline, allowed in (("ifname=uplink:52:54:00:aa:bb:cc", True),
                         ("ifname=lan0:52:54:00:12:34:56", False),
                         ("ifname=eth0:52:54:00:aa:bb:cc", False),
                         ("ifname=eth0:52:54:00:12:34:56", True)):
    msg, kf, left, out = run({"ifcfg-eth0": KS}, keyfiles=["uplink.nmconnection"], cmdline=cmdline,
        udev={"/usr/lib/udev/rules.d/60-net.rules": RENAMER})
    case(f"kernel helper binding: {cmdline}", (msg is None and "eth0.nmconnection" in kf and
         "etc/udev/rules.d/60-net.rules" not in run.udev) if allowed else
         refused(msg, "helper binding") and "eth0.nmconnection" not in kf and
         "etc/sysconfig/network-scripts/ifcfg-eth0" in left)
# .BAK is ignored by NetworkManager but not by rename_device's case-sensitive suffix test.
for retained, allowed in ((KS.replace("eth0", "uplink").replace("12:34:56", "aa:bb:cc"), True),
                          (KS.replace("eth0", "lan0"), False)):
    msg, kf, left, out = run({"ifcfg-eth0": KS, "ifcfg-old.BAK": retained},
        udev={"/usr/lib/udev/rules.d/60-net.rules": RENAMER})
    case(f"retained helper input allowed={allowed}", (msg is None and bool(kf) and
         "etc/sysconfig/network-scripts/ifcfg-old.BAK" in left) if allowed else untouched(msg, kf, left))
sys.exit(0 if all(cases) else 1)
