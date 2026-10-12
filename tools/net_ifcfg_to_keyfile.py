"""The reference of the EL op net.ifcfg-to-keyfile (SPEC.md, "EL ops"): turn the initscripts network profiles
(/etc/sysconfig/network-scripts/ifcfg-*) into NetworkManager keyfiles before the hop to EL10, whose NetworkManager has
no ifcfg support, and pin each interface's name to its MAC so it doesn't become ens3 once the ifcfg file (whose HWADDR
kept it eth0) is gone.

Typed, not a guess: each file is read as KEY=VALUE lines (never executed), only the keys below are understood, and a
file with any other setting (bridges, VLANs, bonds, aliases, IPv6 addresses...) fails the step before anything
changes. The keyfile is written by NetworkManager's own serializer (`nmcli --offline connection add`, no daemon), mode
0600 as NetworkManager requires. The ifcfg files move to /var/lib/wend/saved.

Run on the box by both engines as `python3 - <<'EOF' ... EOF`; this file is the source, and the client embeds the
same text (client/parity.py compares them).
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
KEYS = {"DEVICE", "HWADDR", "BOOTPROTO", "IPADDR", "NETMASK", "PREFIX", "GATEWAY", "DNS1", "DNS2", "DOMAIN", "ONBOOT",
        "TYPE", "NAME", "UUID", "NM_CONTROLLED", "DHCPCLASS", "USERCTL", "IPV6INIT", "PEERDNS"}


def read(path, allowed=None):
    out = {}
    for n, line in enumerate(open(path), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r"([A-Z][A-Z0-9_]*)=(.*)", line)
        if not m:
            sys.exit(f"{path}:{n}: not a KEY=VALUE line, nothing changed")
        words = shlex.split(m.group(2), comments=True)
        if len(words) > 1:
            sys.exit(f"{path}:{n}: {m.group(1)} holds more than one value, nothing changed")
        out[m.group(1)] = words[0] if words else ""
    if allowed is not None and set(out) - allowed:
        sys.exit(f"{path}: settings this op doesn't convert: {sorted(set(out) - allowed)}, nothing changed")
    return out


def plan(path, net, resolv):
    c = path[len(D) + len("/ifcfg-"):]
    v = read(path, KEYS)
    dev = v.get("DEVICE", c)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", dev) or not re.fullmatch(r"[A-Za-z0-9_.-]+", c):
        sys.exit(f"{path}: not a plain interface or connection name, nothing changed")
    if v.get("TYPE", "Ethernet") != "Ethernet":
        sys.exit(f"{path}: TYPE={v['TYPE']}: only Ethernet profiles are converted, nothing changed")
    hw = v.get("HWADDR", "").lower()
    if hw and not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", hw):
        sys.exit(f"{path}: HWADDR={hw} isn't a MAC address, nothing changed")
    args = ["type", "ethernet", "con-name", v.get("NAME") or f"System {c}", "ifname", dev]
    if hw:
        args += ["ethernet.mac-address", hw]
    proto = v.get("BOOTPROTO", "none").lower()
    if proto == "dhcp":
        args += ["ipv4.method", "auto"]
    elif proto in ("static", "none", ""):
        if "IPADDR" not in v:
            sys.exit(f"{path}: BOOTPROTO={proto} with no IPADDR, nothing changed")
        ip = ipaddress.IPv4Address(v["IPADDR"])
        if "PREFIX" in v:
            prefix = int(v["PREFIX"])
        elif "NETMASK" in v:
            prefix = ipaddress.IPv4Network(f"0.0.0.0/{v['NETMASK']}").prefixlen
        else:
            sys.exit(f"{path}: no PREFIX or NETMASK, nothing changed")
        args += ["ipv4.method", "manual", "ipv4.addresses", f"{ip}/{prefix}"]
        gw = v.get("GATEWAY") or (net.get("GATEWAY") if net.get("GATEWAYDEV", dev) == dev else None)
        if gw:
            args += ["ipv4.gateway", str(ipaddress.IPv4Address(gw))]
        dns = [v[k] for k in ("DNS1", "DNS2") if v.get(k)] or resolv["nameserver"]
        if dns:
            args += ["ipv4.dns", ",".join(str(ipaddress.IPv4Address(d)) for d in dns)]
        search = v.get("DOMAIN", "").split() or resolv["search"]
        if search:
            if not all(re.fullmatch(r"[A-Za-z0-9.-]+", s) for s in search):
                sys.exit(f"{path}: a search domain isn't a plain name, nothing changed")
            args += ["ipv4.dns-search", ",".join(search)]
    else:
        sys.exit(f"{path}: BOOTPROTO={proto} isn't converted, nothing changed")
    args += ["ipv6.method", "auto" if v.get("IPV6INIT", "no").lower() == "yes" else "ignore",
             "connection.autoconnect", "no" if v.get("ONBOOT", "yes").lower() == "no" else "yes"]
    return c, dev, hw, args


def main():
    files = sorted(f for f in (os.path.join(D, n) for n in os.listdir(D) if n.startswith("ifcfg-")) if
                   os.path.isfile(f) and not f.endswith("ifcfg-lo")) if os.path.isdir(D) else []
    if not files:
        print("no ifcfg profiles: nothing to convert")
        return
    net = read("/etc/sysconfig/network") if os.path.exists("/etc/sysconfig/network") else {}
    resolv = {"nameserver": [], "search": []}
    if os.path.exists("/etc/resolv.conf"):
        for line in open("/etc/resolv.conf"):
            w = line.split()
            if len(w) >= 2 and w[0] == "nameserver" and len(resolv["nameserver"]) < 3:
                resolv["nameserver"].append(w[1])
            elif w and w[0] == "search":
                resolv["search"] = w[1:]
    plans = [plan(f, net, resolv) for f in files]  # every profile checked before anything changes
    os.makedirs(K, exist_ok=True)
    os.makedirs(L, exist_ok=True)
    os.makedirs(SAVED + D, exist_ok=True)
    for (c, dev, hw, args), f in zip(plans, files):
        p = subprocess.run(["nmcli", "--offline", "connection", "add"] + args, capture_output=True, text=True)
        if p.returncode or "[connection]" not in p.stdout:
            sys.exit(f"nmcli --offline couldn't serialize {f}: {p.stderr.strip()}")
        kf = os.path.join(K, f"{c}.nmconnection")
        fd = os.open(kf + ".wend", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as out:
            out.write(p.stdout)
        os.chmod(kf + ".wend", 0o600)
        os.replace(kf + ".wend", kf)
        if hw:
            with open(os.path.join(L, f"70-wend-{dev}.link"), "w") as out:
                out.write(f"[Match]\nMACAddress={hw}\n\n[Link]\nName={dev}\n")
        if os.path.exists(SAVED + f):  # SAVED keeps the owner's original, whatever later hops do (SPEC)
            os.remove(f)
        else:
            os.replace(f, SAVED + f)
        print(f"{f} -> {kf}" + (f", {dev} pinned to {hw}" if hw else ""))


main()
