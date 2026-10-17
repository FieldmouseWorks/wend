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
# systemd.link(5): the directories udevd reads .link files from (systemd's CONF_PATHS), and /lib, often /usr/lib
LINK_DIRS = (L, "/run/systemd/network", "/usr/local/lib/systemd/network", "/usr/lib/systemd/network",
             "/lib/systemd/network")
SYS = "/sys/class/net"
SAVED = "/var/lib/wend/saved"
NETWORK = "/etc/sysconfig/network"
RESOLV = "/etc/resolv.conf"
HOSTNAME = "/etc/hostname"
KEYS = {"DEVICE", "HWADDR", "BOOTPROTO", "IPADDR", "NETMASK", "PREFIX", "BROADCAST", "NETWORK", "GATEWAY", "DNS1",
        "DNS2", "DOMAIN", "PEERDNS", "ONBOOT", "IPV6INIT", "IPV6_AUTOCONF", "TYPE", "NAME", "UUID", "NM_CONTROLLED",
        "DHCPCLASS", "USERCTL",
        # what EL8's installer adds, writing the profile through NetworkManager's ifcfg-rh plugin
        "DEFROUTE", "IPV4_FAILURE_FATAL", "IPV6_DEFROUTE", "IPV6_FAILURE_FATAL", "IPV6_ADDR_GEN_MODE",
        "PROXY_METHOD", "BROWSER_ONLY",
        # what EL6's installer adds: the interface's MTU, and the machine's own name (see plan)
        "MTU", "HOSTNAME"}
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
    # the .link pin needs a MAC: without one EL10 may call eth0 ens3, and a keyfile for eth0 never comes up
    hw = v.get("HWADDR", "").lower() or running_mac(path, dev)
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
    pin = f"10-wend-{dev}.link"
    if os.path.lexists(os.path.join(L, pin)):  # someone's pin for this name: never replaced
        fail(f"{path}: {L}/{pin} exists already")
    for d in LINK_DIRS:  # systemd applies only the first .link (by file name, over all its directories) that matches
        for n in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if n.endswith(".link") and n < pin:
                other = mac_pin(d, n)
                if other == (hw, dev):  # EL8's installer, booted with ifname=eth0:<mac>, writes this one: it does what
                    continue  # ours does (which is still written, for when it goes)
                # anything else but a pin of another MAC to another name may match this NIC, or rename another to dev
                if other is None or other[0] == hw or other[1] == dev:
                    fail(f"{path}: {d}/{n} comes before {pin}, and systemd uses the first .link that matches")
    args = ["type", "ethernet", "con-name", name, "ifname", dev, "ethernet.mac-address", hw]
    if "UUID" in v:  # the plugin's connection.uuid
        if not re.fullmatch(r"[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}", v["UUID"]):
            fail(f"{path}: UUID={v['UUID']} isn't a UUID")
        args += ["connection.uuid", v["UUID"].lower()]
    if "MTU" in v:  # the plugin's ethernet.mtu
        if not re.fullmatch(r"[1-9][0-9]{1,4}", v["MTU"]) or not 68 <= int(v["MTU"]) <= 65535:
            fail(f"{path}: MTU={v['MTU']} isn't 68 to 65535")
        args += ["ethernet.mtu", v["MTU"]]
    # EL9's NetworkManager reads no HOSTNAME from a profile (initscripts sent it as a DHCP client's name at most), so
    # it goes when it is the machine's own name (/etc/hostname, or /etc/sysconfig/network's HOSTNAME), as EL6's
    # installer writes it; any other name might mean something to someone, and fails the step
    if "HOSTNAME" in v and v["HOSTNAME"] not in (own_name(), net.get("HOSTNAME")):
        fail(f"{path}: HOSTNAME={v['HOSTNAME']} isn't the machine's own name")
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
        if "HWADDR" not in v:
            carries(path, dev, f"{ip}/{prefix}")
        args += ["ipv4.method", "manual", "ipv4.addresses", f"{ip}/{prefix}"]
    else:
        fail(f"{path}: BOOTPROTO={proto} isn't converted")
    gw = v.get("GATEWAY") or net.get("GATEWAY")
    if gw and proto not in ("dhcp", "bootp"):
        args += ["ipv4.gateway", str(addr(path, "GATEWAY", gw))]
    # the plugin: DEFROUTE=no keeps the default route off this profile, but a GATEWAYDEV overrides it either way
    never = not boolean(path, v, "DEFROUTE", "yes")
    if net.get("GATEWAYDEV"):
        never = net["GATEWAYDEV"] != dev
    if never:
        args += ["ipv4.never-default", "yes"]
    if boolean(path, v, "IPV4_FAILURE_FATAL", "no"):  # the plugin's ipv4.may-fail is its opposite
        args += ["ipv4.may-fail", "no"]
    if v.get("PROXY_METHOD", "none").lower() != "none":  # the plugin's proxy.method; none is the default
        fail(f"{path}: PROXY_METHOD={v['PROXY_METHOD']} isn't converted")
    if boolean(path, v, "BROWSER_ONLY", "no"):
        fail(f"{path}: BROWSER_ONLY=yes isn't converted")
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
    args += ["ipv6.method", six]
    # read (so a value that isn't one fails) whatever the method; they only mean something when IPv6 is on
    v6 = [not boolean(path, v, "IPV6_DEFROUTE", "yes"), boolean(path, v, "IPV6_FAILURE_FATAL", "no")]
    gen = v.get("IPV6_ADDR_GEN_MODE", "")  # the plugin matches it case-sensitively
    if gen and gen not in ("stable-privacy", "eui64"):
        fail(f"{path}: IPV6_ADDR_GEN_MODE={gen} isn't stable-privacy or eui64")
    if six != "ignore":
        args += ["ipv6.never-default", "yes"] if v6[0] else []
        args += ["ipv6.may-fail", "no"] if v6[1] else []
        # absent, the plugin reads default-or-eui64 (eui64 unless NetworkManager.conf says otherwise); a new keyfile
        # would get "default" (stable-privacy), moving the box's SLAAC address
        args += ["ipv6.addr-gen-mode", gen or "default-or-eui64"]
    args += ["connection.autoconnect", "yes" if boolean(path, v, "ONBOOT", "yes") else "no"]
    return c, dev, hw, args


def own_name():
    """/etc/hostname's name, or None"""
    if not os.path.isfile(HOSTNAME):
        return None
    lines = [x.strip() for x in open(HOSTNAME) if x.strip() and not x.lstrip().startswith("#")]
    return lines[0] if lines else None


def running_mac(path, dev):
    """No HWADDR, as EL7's installer writes a profile unless told the NIC by MAC (wend#74): the profile then matches
    the interface by name alone, so the NIC it configures is the one with that name now, on the box this runs on, and
    pinning that NIC's own MAC to the name keeps the binding the box runs on today. Only a physical Ethernet NIC whose
    address is its permanent one (sysfs addr_assign_type 0, not random or set) is pinned; a static profile's address
    must also be on it (carries), so a copy of the box on another machine refuses instead of pinning that one's NIC.
    A DHCP profile has no address to prove that by: the op must run on the box itself, as every engine runs it."""
    # the running machine's sysfs: in a chroot engine, the NIC of whatever machine runs it. Right only because every
    # engine that deploys this op's output runs it on the box itself (SPEC "The rehearsal", "The walk"); an engine that
    # deploys a copy's output must supply the target's NIC identity instead
    s = os.path.join(SYS, dev)
    if not os.path.isdir(os.path.join(s, "device")):
        fail(f"{path}: no HWADDR, and no physical NIC named {dev} on this machine to pin it to")
    attr = {}
    for n in ("type", "addr_assign_type", "address"):
        try:
            attr[n] = open(os.path.join(s, n)).read().strip().lower()
        except OSError:
            attr[n] = ""
    if attr["type"] != "1" or attr["addr_assign_type"] != "0":  # ARPHRD_ETHER; NET_ADDR_PERM
        fail(f"{path}: no HWADDR, and {dev} isn't an Ethernet NIC with its own permanent MAC")
    return attr["address"]


def carries(path, dev, cidr):
    """the NIC named dev has the static profile's address now: it is this box's NIC, running this profile"""
    try:
        p = subprocess.run(["ip", "-o", "-4", "addr", "show", "dev", dev], capture_output=True, text=True)
    except OSError as e:
        fail(f"ip: {e}")
    if p.returncode or not any(f" inet {cidr} " in f" {line} " for line in p.stdout.splitlines()):
        fail(f"{path}: no HWADDR, and {dev} doesn't have {cidr} now, so it may not be the NIC this profile runs on")


def mac_pin(d, n):
    """(mac, name) when the .link only pins one MAC to one name ([Match] MACAddress=, [Link] Name=), else None"""
    if any(os.path.lexists(os.path.join(x, n + ".d")) for x in LINK_DIRS):  # drop-ins (any directory) add to it
        return None
    sections, at = {}, None
    try:
        lines = open(os.path.join(d, n)).read().splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    for line in lines:
        line = line.strip()
        if not line or line[0] in "#;":
            continue
        if re.fullmatch(r"\[[A-Za-z]+\]", line):
            at = sections.setdefault(line[1:-1], {})
            continue
        m = re.fullmatch(r"([A-Za-z]+)\s*=\s*(\S*)", line)
        if at is None or not m or m.group(1) in at:
            return None
        at[m.group(1)] = m.group(2)
    if sections.keys() != {"Match", "Link"} or sections["Match"].keys() != {"MACAddress"} or \
            sections["Link"].keys() != {"Name"}:
        return None
    mac, name = sections["Match"]["MACAddress"].lower(), sections["Link"]["Name"]
    if not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac) or not re.fullmatch(NAME, name):
        return None
    return mac, name


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
