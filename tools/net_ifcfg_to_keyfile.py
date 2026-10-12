"""The reference of the EL op net.ifcfg-to-keyfile (SPEC.md, "EL ops"): turn the initscripts network profiles
(/etc/sysconfig/network-scripts/ifcfg-*) into NetworkManager keyfiles before the hop to EL10, whose NetworkManager has
no ifcfg support, and pin each interface's name to its MAC so it doesn't become ens3 once the ifcfg file (whose HWADDR
kept it eth0) is gone.

Typed, not a guess: the profiles are the ones EL9's NetworkManager loads (its ifcfg-rh plugin's own list of ignored
names), each is read as KEY=VALUE lines (never executed), only the keys below are understood, each with the meaning
that plugin gives it, and anything else fails the step before anything changes. Every keyfile is serialized by
NetworkManager itself (`nmcli --offline connection add`, no daemon) before the first file is written, so a profile
nmcli refuses changes nothing either. Keyfiles are mode 0600, as NetworkManager requires. The ifcfg files move to
/var/lib/wend/saved.

Run on the box by both engines as `python3 -` with this file as a here-document (tools/backends.py); this file is
the source, and the client embeds the same text (client/parity.py compares them). tools/test_net_ifcfg.py runs it
against fixture roots.
"""
import ipaddress
import os
import re
import shlex
import subprocess
import sys

D = "/etc/sysconfig/network-scripts"
K = "/etc/NetworkManager/system-connections"
L = "/etc/systemd/network"
SAVED = "/var/lib/wend/saved"
NETWORK = "/etc/sysconfig/network"
RESOLV = "/etc/resolv.conf"
KEYS = {"DEVICE", "HWADDR", "BOOTPROTO", "IPADDR", "NETMASK", "PREFIX", "BROADCAST", "NETWORK", "GATEWAY", "DNS1",
        "DNS2", "DOMAIN", "PEERDNS", "ONBOOT", "IPV6INIT", "IPV6_AUTOCONF", "TYPE", "NAME", "UUID", "NM_CONTROLLED",
        "DHCPCLASS", "USERCTL"}
# /etc/sysconfig/network: what the plugin reads there and this op converts (GATEWAY, GATEWAYDEV, IPV6INIT), and what
# NetworkManager doesn't read at all; anything else it reads there (RES_OPTIONS, IPV6_DEFAULTGW...) fails the step
NET_KEYS = {"GATEWAY", "GATEWAYDEV", "IPV6INIT", "NETWORKING", "NETWORKING_IPV6", "HOSTNAME", "NOZEROCONF", "NISDOMAIN"}
# names NetworkManager's ifcfg-rh plugin never loads (nms-ifcfg-rh-utils.c, utils_should_ignore_file, which compares
# the suffixes ignoring case): backups, rpm and augeas leftovers. EL9 ran the box with exactly the other ifcfg-* files, so those are the ones carried over.
IGNORED = (".bak", "~", ".orig", ".rej", ".rpmnew", ".augnew", ".augtmp")
# files the plugin reads beside a profile; this op converts none of them, so their presence fails the step
SIDE = ("route-", "route6-", "rule-", "rule6-", "keys-")
NAME = r"[A-Za-z0-9_][A-Za-z0-9_.-]*"
YES, NO = {"yes", "true", "t", "y", "1"}, {"no", "false", "f", "n", "0"}  # shvar.c, svParseBoolean


def fail(msg):
    sys.exit(f"{msg}, nothing changed")


def read(path, allowed):
    out = {}
    for n, line in enumerate(open(path), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r"([A-Z][A-Z0-9_]*)=(.*)", line)
        if not m:
            fail(f"{path}:{n}: not a KEY=VALUE line")
        try:
            words = shlex.split(m.group(2))
        except ValueError as e:
            fail(f"{path}:{n}: {m.group(1)}: {e}")
        if len(words) > 1 or "#" in m.group(2) or "$" in m.group(2) or "`" in m.group(2):
            fail(f"{path}:{n}: {m.group(1)} isn't one plain value")
        out[m.group(1)] = words[0] if words else ""
    out = {k: x for k, x in out.items() if x}  # an empty value is an unset key, as svGetValueStr reads it
    if set(out) - allowed:
        fail(f"{path}: settings this op doesn't convert: {sorted(set(out) - allowed)}")
    return out


def boolean(path, v, key, default):
    s = v.get(key, default).lower()
    if s in YES or s in NO:
        return s in YES
    fail(f"{path}: {key}={s} isn't yes or no")


def addr(path, key, s):
    try:
        return ipaddress.IPv4Address(s)
    except ValueError:
        fail(f"{path}: {key}={s} isn't an IPv4 address")


def plan(path, net, resolv):
    c = path[len(D) + len("/ifcfg-"):]
    v = read(path, KEYS)
    dev = v.get("DEVICE")
    if not dev:  # the plugin then binds the profile to no interface, and GATEWAYDEV can't name it
        fail(f"{path}: no DEVICE")
    if not re.fullmatch(NAME, c) or not re.fullmatch(NAME, dev) or len(dev) > 15:
        fail(f"{path}: not a plain interface or connection name")
    for side in SIDE:
        if os.path.exists(os.path.join(D, side + c)):
            fail(f"{path}: {side}{c} beside it isn't converted")
    if v.get("TYPE", "Ethernet") != "Ethernet":
        fail(f"{path}: TYPE={v['TYPE']}: only Ethernet profiles are converted")
    hw = v.get("HWADDR", "").lower()
    if not hw:  # the .link pin needs it: without one EL10 may call eth0 ens3, and a keyfile for eth0 never comes up
        fail(f"{path}: no HWADDR to pin {dev} to")
    if not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", hw):
        fail(f"{path}: HWADDR={hw} isn't a MAC address")
    name = v.get("NAME") or f"System {c}"
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_. -]*", name):
        fail(f"{path}: NAME={name} isn't a plain name")
    if not boolean(path, v, "NM_CONTROLLED", "yes"):  # EL9's NetworkManager left it unmanaged; a keyfile can't say so
        fail(f"{path}: NM_CONTROLLED=no isn't converted")
    boolean(path, v, "USERCTL", "no")
    if os.path.lexists(os.path.join(K, f"{c}.nmconnection")):
        fail(f"{path}: {K}/{c}.nmconnection exists already")
    if os.path.lexists(os.path.join(L, f"10-wend-{dev}.link")):  # someone's pin for this name: never replaced
        fail(f"{path}: {L}/10-wend-{dev}.link exists already")
    args = ["type", "ethernet", "con-name", name, "ifname", dev, "ethernet.mac-address", hw]
    if "UUID" in v:  # the plugin's connection.uuid
        if not re.fullmatch(r"[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}", v["UUID"]):
            fail(f"{path}: UUID={v['UUID']} isn't a UUID")
        args += ["connection.uuid", v["UUID"].lower()]
    proto = v.get("BOOTPROTO", "none").lower()
    if proto in ("dhcp", "bootp"):  # the plugin reads bootp as dhcp
        for key in ("IPADDR", "PREFIX", "NETMASK", "GATEWAY"):
            if key in v:
                fail(f"{path}: {key} beside BOOTPROTO={proto} isn't converted")
        args += ["ipv4.method", "auto"]
    elif proto in ("static", "none", ""):
        if "IPADDR" not in v:
            fail(f"{path}: BOOTPROTO={proto} with no IPADDR")
        ip = addr(path, "IPADDR", v["IPADDR"])
        if "PREFIX" in v:
            if not re.fullmatch(r"[0-9]|[12][0-9]|3[0-2]", v["PREFIX"]):
                fail(f"{path}: PREFIX={v['PREFIX']} isn't 0 to 32")
            prefix = int(v["PREFIX"])
        elif "NETMASK" in v:
            m = int(addr(path, "NETMASK", v["NETMASK"]))
            prefix = bin(m).count("1")
            if m != (0xffffffff << (32 - prefix)) & 0xffffffff:
                fail(f"{path}: NETMASK={v['NETMASK']} isn't a netmask")
        else:
            fail(f"{path}: no PREFIX or NETMASK")
        lan = ipaddress.IPv4Network(f"{ip}/{prefix}", strict=False)
        for key, want in (("NETWORK", lan.network_address), ("BROADCAST", lan.broadcast_address)):
            if key in v and addr(path, key, v[key]) != want:  # the installer writes both; NetworkManager derives them
                fail(f"{path}: {key}={v[key]} doesn't match {ip}/{prefix}")
        args += ["ipv4.method", "manual", "ipv4.addresses", f"{ip}/{prefix}"]
    else:
        fail(f"{path}: BOOTPROTO={proto} isn't converted")
    gw = v.get("GATEWAY") or net.get("GATEWAY")
    if gw and proto not in ("dhcp", "bootp"):
        args += ["ipv4.gateway", str(addr(path, "GATEWAY", gw))]
    if net.get("GATEWAYDEV", dev) != dev:  # the plugin: only GATEWAYDEV gets the default route
        args += ["ipv4.never-default", "yes"]
    if "DNS2" in v and "DNS1" not in v:  # the plugin stops at the first missing DNSn
        fail(f"{path}: DNS2 with no DNS1")
    dns = [v[k] for k in ("DNS1", "DNS2") if k in v]
    search = v.get("DOMAIN", "").split()
    if proto not in ("dhcp", "bootp") and not dns:  # CentOS 5's installer put a static box's resolvers only here
        dns, search = resolv["nameserver"], search or resolv["search"]
    if dns:
        args += ["ipv4.dns", ",".join(str(addr(path, "DNS", d)) for d in dns)]
    if search:
        if not all(re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*",
                                s) for s in search):
            fail(f"{path}: a search domain isn't a plain name")
        args += ["ipv4.dns-search", ",".join(search)]
    if not boolean(path, v, "PEERDNS", "yes"):
        args += ["ipv4.ignore-auto-dns", "yes"]
    if not boolean(path, v, "IPV6INIT", net.get("IPV6INIT", "no")):
        six = "ignore"
    else:
        six = "auto" if boolean(path, v, "IPV6_AUTOCONF", "yes") else "link-local"
    args += ["ipv6.method", six, "connection.autoconnect", "yes" if boolean(path, v, "ONBOOT", "yes") else "no"]
    return c, dev, hw, args


def ignored(n):
    return n.lower().endswith(IGNORED) or re.search(r";[0-9a-fA-F]{8}\Z", n) is not None


def main():
    files = sorted(os.path.join(D, n) for n in os.listdir(D) if n.startswith("ifcfg-") and n != "ifcfg-lo" and
                   not ignored(n) and os.path.isfile(os.path.join(D, n))) if os.path.isdir(D) else []
    if not files:
        print("no ifcfg profiles: nothing to convert")
        return
    net = read(NETWORK, NET_KEYS) if os.path.exists(NETWORK) else {}
    resolv = {"nameserver": [], "search": []}
    if os.path.exists(RESOLV):
        for line in open(RESOLV):
            w = line.split()
            if len(w) >= 2 and w[0] == "nameserver" and len(resolv["nameserver"]) < 3:
                resolv["nameserver"].append(w[1])
            elif w and w[0] in ("search", "domain"):  # resolv.conf(5): the last of the two wins; "." is the root,
                resolv["search"] = [d for d in (w[1:2] if w[0] == "domain" else w[1:]) if d != "."]  # no domain
    plans = [plan(f, net, resolv) for f in files]
    pins = {}
    for (c, dev, hw, args), f in zip(plans, files):  # one pin per name and per MAC (two profiles of one NIC agree)
        for key, other in ((("dev", dev), hw), (("hw", hw), dev)):
            if pins.setdefault(key, (other, f))[0] != other:
                fail(f"{f} and {pins[key][1]} pin {key[1]} differently")
    keyfiles = []
    for (c, dev, hw, args), f in zip(plans, files):  # every keyfile serialized before anything is written
        try:
            p = subprocess.run(["nmcli", "--offline", "connection", "add"] + args, capture_output=True, text=True)
        except OSError as e:
            fail(f"nmcli: {e}")
        if p.returncode or "[connection]" not in p.stdout:
            fail(f"nmcli --offline refused {f}: {p.stderr.strip()}")
        keyfiles.append(p.stdout)
    os.makedirs(K, exist_ok=True)
    os.makedirs(L, exist_ok=True)
    os.makedirs(SAVED + D, exist_ok=True)
    for (c, dev, hw, args), f, text in zip(plans, files, keyfiles):
        kf = os.path.join(K, f"{c}.nmconnection")
        write(kf, text, 0o600)
        write(os.path.join(L, f"10-wend-{dev}.link"), f"[Match]\nMACAddress={hw}\n\n[Link]\nName={dev}\n", 0o644)
        if os.path.exists(SAVED + f):  # SAVED keeps the owner's original, whatever later hops do (SPEC)
            os.remove(f)
        else:
            os.replace(f, SAVED + f)
        print(f"{f} -> {kf}, {dev} pinned to {hw}")


def write(path, text, mode):
    fd = os.open(path + ".wend", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "w") as out:
        out.write(text)
    os.chmod(path + ".wend", mode)
    os.replace(path + ".wend", path)


if __name__ == "__main__":
    main()
