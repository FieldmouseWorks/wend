"""Op backends: what each typed op compiles to, per package manager. SPEC.md "Op vocabulary" is the contract.

A fix only ever names an op and its arguments. tools/check.py validates the arguments (against the core vocabulary
and the adapter's [ops] table) before anything here runs, so every value substituted below has already matched a
strict pattern: no quotes, no spaces, no `$`. The shell lives here, in the harness, which CI always takes from the
base branch. A PR can't change what an op does; it can only propose a new op, which is a spec change.

  apt  (adapters/debian.toml)  compiles byte-for-byte to the commands dep-lab's ol.py ran (tools/port_oldlinux.py)
  rpm  (adapters/el.toml)      the CentOS 5 -> AlmaLinux 10 work from dep-lab's co.py, recut into typed ops
"""
import hashlib
import os
import re


def fill(template, values):
    """{name} placeholders only. Not str.format: templates are shell and contain ${...} and %{...}."""
    return re.sub(r"\{([a-z_]+)\}", lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0),
                  template)


def plain_dir(root, d):
    """d is a directory inside root (a resolved path) and nothing on the way to it is a symlink."""
    return os.path.isdir(d) and os.path.realpath(d) == d and d.startswith(f"{root}/")


def file_sha256(root, path):
    """sha256 of a regular file inside root (a resolved path), never through a symlink; else None. A box's
    package scripts can plant symlinks, and this runs on the host."""
    if not plain_dir(root, os.path.dirname(path)) or os.path.islink(path) or not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        return "sha256:" + hashlib.sha256(f.read()).hexdigest()


def pred_sh(w, adapter):
    """A precondition as a shell test, from the adapter's predicate templates. Harness code, not fix code."""
    k, v = next(iter(w.items()))
    p = adapter["predicates"]
    if k == "installed":
        return p["installed"].replace("{pkg}", v)  # not str.format: templates contain ${Status}
    if k == "file":
        return p["file"].replace("{path}", v)
    if k == "file-contains":
        return p["file-contains"].replace("{path}", v["path"]).replace("{text}", v["text"])
    if k == "mysql-option":
        return fill(p["mysql-option"], {"path": v["path"], "key": v["key"], "value": v.get("value", ""),
                                        "any": 0 if "value" in v else 1})
    if k == "not":
        return f"! ( {pred_sh(v, adapter)} )"
    j = " && " if k == "all" else " || "
    return "( " + j.join(f"( {pred_sh(x, adapter)} )" for x in v) + " )"


def ere(name):
    """A package name (already held to a name pattern and SAFE_VALUE) as a literal in an extended regex."""
    return name.replace(".", "[.]").replace("+", "[+]")


def printf(content, path):
    """Shell that writes content to path. The content goes through %b, never as the format itself, so content that
    starts with "-" (an armoured key) isn't read as an option, and % is literal. Only \\ and \\n are escapes in it."""
    return "printf '%b' '" + content.replace("\\", "\\\\").replace("\n", "\\n") + f"' > {path}"


class Apt:
    name = "apt"
    # Everything an adapter may declare for this backend. check.py holds adapters to these lists, so an adapter
    # can narrow what fixes may do but never widen it past what this file implements.
    OPS = {"apache.enable", "apache.disable", "dpkg.reinstall", "net.ifupdown-dns-to-resolved", "apt.drop-esm-pins"}
    FORCE = {"depends", "remove-essential"}
    PM_OPTIONS = {"overwrite-conflicting-files"}

    def sources(self, root, adapter, release):
        """SPEC.md "Pinned sources": the Release/InRelease file apt read for this hop, by its origin URL."""
        root = os.path.realpath(root)
        url = f"{self.hop_vars(adapter, release)['mirror']}/dists/{release}"
        stem = re.sub(r"^[a-z]+://", "", url).replace("/", "_")
        lists = f"{root}/var/lib/apt/lists"
        found = {f"{url}/{n}": file_sha256(root, f"{lists}/{stem}_{n}") for n in ("InRelease", "Release")}
        found = {u: h for u, h in found.items() if h}
        if not found and plain_dir(root, lists):
            # apt 0.9 throws the Release away when its signing key fails: pin the indexes it kept, by the path the
            # Release lists them under (their bytes are the uncompressed ones the Release hashes)
            for n in sorted(os.listdir(lists)):
                h = file_sha256(root, f"{lists}/{n}") if n.startswith(f"{stem}_") and not n.endswith(".gpg") else None
                if h:
                    found[f"{url}/{n[len(stem) + 1:].replace('_', '/')}"] = h
        return dict(sorted(found.items()))

    def hop_vars(self, adapter, release):
        lad, r = adapter["ladder"], adapter["releases"]
        mirror = r["current"] if lad.index(release) >= lad.index(r["current_from"]) else r["archive"]
        hw = adapter.get("hardware", {}).get(release)  # the room the release's kernel needs in /boot (0: none known)
        return {"mirror": mirror, "release": release,
                "boot_mib": str(hw.get("boot_mib", 0) if isinstance(hw, dict) else 0)}

    def step(self, s, adapter, release=None):
        op = s["op"]
        if op == "install":
            return "apt-get install " + " ".join(s["packages"])
        if op == "remove":
            return ("apt-get purge " if s["purge"] else "apt-get remove ") + " ".join(s["packages"])
        if op == "repair":
            return "apt-get -f install"
        if op == "force-remove":
            return (f"dpkg --{'purge' if s['purge'] else 'remove'} " + "".join(f"--force-{x} " for x in s["force"]) +
                    " ".join(s["packages"]))
        if op == "write-file":
            return printf(s["content"], s["path"])
        if op == "pm-option":
            o = adapter["pm_options"][s["option"]]
            return printf(o["content"], o["path"])
        if op in ("apache.enable", "apache.disable"):
            return f"a2{'en' if op == 'apache.enable' else 'dis'}{s['kind']} {s['name']}"
        if op == "dpkg.reinstall":
            return "apt-get install --reinstall --only-upgrade " + " ".join(s["packages"])
        if op == "net.ifupdown-dns-to-resolved":
            return here_doc(NET_IFUPDOWN_DNS_TO_RESOLVED)
        if op == "apt.drop-esm-pins":
            return ESM_PINS.replace("{release}", s["release"])
        raise ValueError(f"the apt backend has no op {op!r}")


# ---------- rpm ----------

# Release packages drop vendor .repo files that point at mirrors long gone. The harness owns /etc/yum.repos.d
# (like sources.list), so anything that may have installed one cleans up after itself.
CLEAN_REPOS = "for f in /etc/yum.repos.d/*.repo; do [ \"$f\" = /etc/yum.repos.d/ladder.repo ] || rm -f \"$f\"; done"
DB_DAEMON = "D=$(ls /usr/libexec/mariadbd /usr/libexec/mysqld /usr/sbin/mariadbd /usr/sbin/mysqld 2>/dev/null | head -n1)"
DB_DIRS = ("mkdir -p /run/mariadb /run/mysqld /var/log/mariadb; touch /var/log/mariadb/mariadb.log; "
           "chown -R mysql:mysql /run/mariadb /run/mysqld /var/log/mariadb /var/lib/mysql; "
           "rm -f /var/lib/mysql/mysql.sock /var/lib/mysql/*.pid")
WAIT_SOCK = ("[ -n \"$D\" ] || { echo 'no database server installed'; exit 1; }; "
             "i=0; while [ $i -lt 120 ] && [ ! -S /var/lib/mysql/mysql.sock ]; do sleep 1; i=$((i+1)); done; "
             "[ -S /var/lib/mysql/mysql.sock ] || { echo 'the database server did not start'; "
             "tail -n 15 /tmp/wend-db.log; pkill -KILL -x mariadbd; pkill -KILL -x mysqld; exit 1; }")
STOP_DB = ("pkill -TERM -x mariadbd; pkill -TERM -x mysqld; i=0; "
           "while pgrep -x 'mariadbd|mysqld' >/dev/null && [ $i -lt 300 ]; do sleep 1; i=$((i+1)); done; "
           "! pgrep -x 'mariadbd|mysqld' >/dev/null")
# mysql.upgrade-data: run the server's own upgrade tool and check the data now carries the server's version stamp.
UPGRADE_DB = ("U=$(command -v mariadb-upgrade || command -v mysql_upgrade); "
              f"[ -n \"$U\" ] || {{ echo 'no mariadb-upgrade or mysql_upgrade installed'; {STOP_DB}; exit 1; }}; "
              "V=$($D --version | sed -n 's/.* Ver \\([^ -]*\\).*/\\1/p'); "
              "timeout 1200 $U -uroot --force >/tmp/wend-upgrade.log 2>&1; u=$?; tail -n 3 /tmp/wend-upgrade.log; "
              "S=$(cat /var/lib/mysql/mariadb_upgrade_info /var/lib/mysql/mysql_upgrade_info 2>/dev/null | head -n 1); "
              "if [ -z \"$V\" ] || [ \"${S%%-*}\" != \"$V\" ]; then "
              "echo \"$U (exit $u) left the data stamped ${S:-nothing}, the server is ${V:-unknown}\"; "
              f"tail -n 30 /tmp/wend-upgrade.log; {STOP_DB}; exit 1; fi")
SAVED = "/var/lib/wend/saved"  # what adopt-rpmnew replaced and set-aside-unowned moved, kept for the admin
SYSV_RECORD = "/var/lib/wend/sysv-enabled"  # service.record: the SysV services the box started at boot, one per line
# package file names (name-version-release.arch.rpm) to package names, one per line
# awk: x = everything after the second | (a vendor may contain |; names and NVRAs can't)
VENDOR_LAST = "{x = $0; sub(/^[^|]*[|][^|]*[|]/, \"\", x)}"
NAMES = "awk '{sub(/-[^-]*-[^-]*$/, \"\"); print}'"
SOURCES = "/var/lib/wend/sources"  # <hop>/<repo id>.repomd.xml: metadata an op fetched through repos the hop then drops


KEYS = "/etc/wend/keys"  # SPEC "Signed packages": the adapter's [keys], written there by every hop's {keys}


def gpg(names):
    """A repo section's signature lines: gpgcheck=1 against the named keys, or gpgcheck=0 when it names none."""
    return f"gpgcheck=1\ngpgkey={' '.join(f'file://{KEYS}/{k}' for k in names)}\n" if names else "gpgcheck=0\n"


def write_keys(adapter):
    """Shell that writes every key in the adapter's [keys] under KEYS ("true" if it has none)."""
    ks = adapter.get("keys", {})
    return " && ".join([f"mkdir -p {KEYS}"] + [printf(a, f"{KEYS}/{n}") for n, a in ks.items()]) if ks else "true"


def repo_file(adapter, release, shim):
    r = adapter["releases"][release]
    return "".join(f"[{release}-{n}]\nname={release}-{n}\nbaseurl={shim}/{u}/\n{gpg(r.get('gpgkeys', []))}enabled=1\n\n"
                   for n, u in r["repos"].items())


def addon_repos(adapter, release, shim, file):
    """SPEC "Add-on repositories": shell that appends each add-on's repo for `release` to `file`, as
    [<release>-<addon>], when the add-on's marker package is installed. Empty when no add-on has one."""
    out = []
    for n, a in adapter.get("addons", {}).items():
        if release in a["repos"]:
            rid = f"{release}-{n}"
            k = [a["gpgkeys"][release]] if release in a.get("gpgkeys", {}) else []
            body = f"[{rid}]\\nname={rid}\\nbaseurl={shim}/{a['repos'][release]}/\\n" + \
                gpg(k).replace("\n", "\\n") + "enabled=1\\n\\n"
            out.append(f"if rpm -q {a['marker']} >/dev/null 2>&1; then printf '{body}' >> {file}; fi")
    return " && ".join(out)  # each `if` is true when its marker is absent


def addon_vendors(adapter):
    """Shell that sets A to |vendor|vendor|... for every add-on whose marker package is installed."""
    return "A='|'; " + "".join(f"rpm -q {a['marker']} >/dev/null 2>&1 && A=\"${{A}}{a['vendor']}|\"; "
                               for a in adapter.get("addons", {}).values())


def addon_repo_ids(adapter):
    """{repo id: repomd.xml URL} for every add-on repo a hop or rpm.transit uses (<release>-<addon>)."""
    return {f"{rel}-{n}": f"https://{u}/repodata/repomd.xml"
            for n, a in adapter.get("addons", {}).items() for rel, u in a["repos"].items()}


def primary_list(base, out):
    """The file names of every package in a repo, from its primary metadata (works with CentOS 5's own tools)."""
    return (f"R=$(curl -sf {base}/repodata/repomd.xml | grep -o 'href=\"[^\"]*primary.xml.gz\"' | head -n1 | "
            f"sed 's/href=\"//;s/\"//') && test -n \"$R\" && curl -sf -o {out}.gz {base}/$R && "
            f"zcat {out}.gz | grep -o '<location href=\"Packages/[^\"]*\"' | sed 's/.*Packages\\///;s/\"//' > {out} && "
            f"rm -f {out}.gz && test -s {out} || {{ echo 'no package list from {base}'; exit 1; }}")


def package_names(base, out):
    """Append the name of every package in a repo to out, from its primary metadata (gzip or xz, as EPEL 8 has it)."""
    return (f"R=$(curl -sf {base}/repodata/repomd.xml | grep -o 'href=\"[^\"]*primary.xml[.a-z]*\"' | head -n1 | "
            f"sed 's/href=\"//;s/\"//') && test -n \"$R\" && curl -sf -o {out}.z {base}/$R && "
            f"case $R in *.xz) xz -dc < {out}.z > {out}.x;; *.gz) gzip -dc < {out}.z > {out}.x;; *) false;; esac && "
            f"grep -o '<name>[^<]*</name>' {out}.x | sed 's/<[^>]*>//g' > {out}.one && test -s {out}.one && "
            f"cat {out}.one >> {out} && rm -f {out}.z {out}.x {out}.one || "
            f"{{ echo 'no package list from {base}'; exit 1; }}")


# From od's bytes after an RPM's 96-byte lead: "<tag 1002 offset> <length> <tag 268 offset> <length> <main header
# offset>", a length 0 for a signature that isn't there; nothing if it isn't an RPM. 1002 signs the main header and
# the payload, 268 the main header only.
RPM_SIG = ("function be(p) { return ((b[p] * 256 + b[p + 1]) * 256 + b[p + 2]) * 256 + b[p + 3] } "
           "{ for (i = 1; i <= NF; i++) b[n++] = $i } "
           "END { if (n < 16 || b[0] != 142 || b[1] != 173 || b[2] != 232) exit; il = be(8); s = 16 + 16 * il; "
           "o = c = p = d = 0; for (e = 0; e < il; e++) { t = be(16 + 16 * e); "
           "if (t == 1002) { o = be(24 + 16 * e); c = be(28 + 16 * e) } if (t == 268) { p = be(24 + 16 * e); d = be(28 + 16 * e) } } "
           "x = 96 + s + be(12); x += (8 - x % 8) % 8; print 96 + s + o, c, 96 + s + p, d, x }")
# From od's bytes of an RPM's main header: "<payload digest algorithm> <hex digest>" (tags 5093 and 5092), or nothing
RPM_PAYLOAD = ("function be(p) { return ((b[p] * 256 + b[p + 1]) * 256 + b[p + 2]) * 256 + b[p + 3] } "
               "{ for (i = 1; i <= NF; i++) b[n++] = $i } "
               "END { if (n < 16 || b[0] != 142 || b[1] != 173 || b[2] != 232) exit; il = be(8); s = 16 + 16 * il; "
               "a = h = -1; for (e = 0; e < il; e++) { t = be(16 + 16 * e); if (t == 5093) a = be(s + be(24 + 16 * e)); "
               "if (t == 5092) h = s + be(24 + 16 * e) } if (a < 0 || h < 0) exit; "
               "for (q = h; q < n && b[q] != 0; q++) { v = b[q]; if (v < 48 || v > 102 || (v > 57 && v < 97)) exit; "
               "x = x sprintf(\"%c\", v) } print a, x }")


def checksig(adapter, release, k, files):
    """SPEC "Signed packages": shell that checks files (RPMs fetched outside the package manager, or by one that can't
    check them) against the release's keys and its add-ons', and refuses any not signed by them. "" when the release
    names no keys. gpgv checks the signatures, not rpm: EL7's rpm 4.11 can't use signing subkeys, which AlmaLinux 8
    signs with, and gpgv works the same on every release. A header+payload signature (1002) covers everything; a
    header-only one (268, newer EPEL builds) covers the header, whose sha256 payload digest (5092) then covers the
    payload. Its variables all start with _w, so it leaves the caller's ($f, $r, ...) alone."""
    keys = adapter["releases"][release].get("gpgkeys", [])
    if not keys:
        return ""
    keys = keys + [a["gpgkeys"][release] for a in adapter.get("addons", {}).values() if release in a.get("gpgkeys", {})]
    v = f"\"$_wv\" --homedir {k} --keyring {k}/ring {k}/sig"
    return (f"rm -rf {k}; mkdir -m 700 -p {k}; _wg=$(command -v gpg2 || command -v gpg); "
            "_wv=$(command -v gpgv2 || command -v gpgv); "
            "[ -n \"$_wg\" ] && [ -n \"$_wv\" ] || { echo 'refusing: no gpg and gpgv to check signatures with'; exit 1; }; "
            # one key block per file (AlmaLinux 8's holds two), each into a binary keyring; one that won't read refuses
            f"awk '/BEGIN PGP/ {{n++}} {{print > (\"{k}/key.\" n)}}' " + " ".join(f"{KEYS}/{n}" for n in keys) + "; "
            f"for _wk in {k}/key.*; do \"$_wg\" --homedir {k} --batch --dearmor < $_wk >> {k}/ring || "
            "{ echo \"refusing: can't read key $_wk\"; exit 1; }; done; "
            f"_wbad=; for _wr in {files}; do _ws=$(od -An -v -tu1 -j96 -N65536 $_wr 2>/dev/null | awk '{RPM_SIG}'); "
            "_wok=; if [ -n \"$_ws\" ]; then set -- $_ws; "
            f"if [ $2 -gt 0 ]; then tail -c +$(($1 + 1)) $_wr | head -c $2 > {k}/sig && "
            f"tail -c +$(($5 + 1)) $_wr > {k}/data && {v} {k}/data >/dev/null 2>&1 && _wok=1; "
            f"elif [ $4 -gt 0 ]; then _wx=$5; tail -c +$(($3 + 1)) $_wr | head -c $4 > {k}/sig && "
            f"_wl=$(od -An -v -tu1 -j$((_wx + 8)) -N8 $_wr | awk '{{print 16 + 16 * ((($1 * 256 + $2) * 256 + $3) * 256 + $4) "
            "+ (($5 * 256 + $6) * 256 + $7) * 256 + $8}') && "
            f"tail -c +$((_wx + 1)) $_wr | head -c $_wl > {k}/hdr && {v} {k}/hdr >/dev/null 2>&1 && "
            f"_wd=$(od -An -v -tu1 {k}/hdr | awk '{RPM_PAYLOAD}') && [ \"${{_wd%% *}}\" = 8 ] && "
            f"[ \"$(tail -c +$((_wx + _wl + 1)) $_wr | sha256sum | cut -d' ' -f1)\" = \"${{_wd#* }}\" ] && _wok=1; fi; fi; "
            "[ -n \"$_wok\" ] || _wbad=\"$_wbad $_wr\"; done; "
            f"rm -f {k}/data {k}/hdr; [ -z \"$_wbad\" ] || {{ echo \"refusing, not signed by the release's keys:\"; "
            "printf '%s\\n' $_wbad | head -n 20; exit 1; }")


def transit_checksig(adapter, release, b):
    """rpm.transit downloads with EL7's dnf, which can't check these signatures (--nogpgcheck): check every
    downloaded package before any is unpacked or run. No step when the release names no keys."""
    c = checksig(adapter, release, f"{b}/keydb", "*.rpm")
    return [c] if c else []


def bootstrap_pins(adapter, release):
    """rpm.bootstrap's 7-Zip RPMs for a release, by their origin URL: {url: "sha256:..."}."""
    boot = adapter["releases"][release].get("bootstrap")
    return {f"https://{boot['p7zip_repo']}/{f}": h for f, h in boot["p7zip"].items()} if boot else {}


class Rpm:
    name = "rpm"
    OPS = {"rpm.swap-release", "rpm.drop-arch", "rpm.drop-unneeded", "rpm.bootstrap", "rpm.transit", "rpm.adopt-rpmnew",
           "rpm.set-aside-unowned", "fs.layout", "kv.set", "kv.unset", "mysql.set-password", "mysql.clean-shutdown",
           "mysql.upgrade-data", "ssh.generate-host-keys", "httpd.drop-missing-modules", "php.mysqli-report-off",
           "service.record", "service.enable", "net.ifcfg-to-keyfile"}
    # no --justdb for fixes: dropping a package from the database while its files stay is how a fix would fake the
    # pkgdb check. rpm.transit uses it internally, on the previous release's leftovers only.
    FORCE = {"depends", "scripts"}
    PM_OPTIONS = set()

    def sources(self, root, adapter, release):
        """SPEC.md "Pinned sources": every repomd.xml yum or dnf cached this hop, and those rpm.transit kept under
        SOURCES, by its origin URL. The harness's repo ids are <release>-<name> (repo_file) and transit's boot-<name>,
        so a cache directory names the repo it came from."""
        root = os.path.realpath(root)
        ids = {f"{rel}-{n}": f"https://{u}/repodata/repomd.xml"
               for rel, r in adapter["releases"].items() for n, u in r.get("repos", {}).items()}
        transits = {rel for rel, r in adapter["releases"].items() if "transit" in r}
        for r in adapter["releases"].values():  # rpm.transit's boot-<name> repos
            if "transit" in r:
                t = r["transit"]
                for n, u in {**adapter["releases"][t["from"]]["repos"], **t.get("extra_repos", {})}.items():
                    ids[f"boot-{n}"] = f"https://{u}/repodata/repomd.xml"
        ids.update(addon_repo_ids(adapter))
        out = {}
        kept = f"{root}{SOURCES}/{release}"
        pins = {u: h for r in adapter["releases"] for u, h in bootstrap_pins(adapter, r).items()}
        f = f"{kept}/fetched"
        if file_sha256(root, f):  # a line counts only if it is one of the adapter's own pins
            with open(f, "rb") as fh:
                for u, _, h in (line.decode("utf-8", "replace").strip().partition(" ") for line in fh):
                    if pins.get(u) == h:
                        out[u] = h
        if plain_dir(root, kept):
            for n in os.listdir(kept):
                rid = n[:-len(".repomd.xml")] if n.endswith(".repomd.xml") else None
                ok = rid in ids and (rid.startswith("boot-") or rid.split("-", 1)[0] in transits)
                h = file_sha256(root, f"{kept}/{n}") if ok else None
                if h:
                    out[ids[n[:-len(".repomd.xml")]]] = h
        for top, layout in ((f"{root}/var/cache/yum", "yum"), (f"{root}/var/cache/dnf", "dnf")):
            for d, dirs, files in os.walk(top) if plain_dir(root, top) else ():  # never follows symlinks below top
                name = os.path.basename(d)
                rid = name if layout == "yum" else re.sub(r"-[0-9a-f]{16}$", "", name)
                p = f"{d}/repomd.xml" if layout == "yum" else f"{d}/repodata/repomd.xml"
                # only this hop's repos: yum and dnf "clean all" skip disabled repos, so older caches stay behind
                if rid in ids and rid.startswith(f"{release}-") and (layout == "yum" or rid != name):
                    h = file_sha256(root, p)
                    if h:
                        out[ids[rid]] = h
        return dict(sorted(out.items()))

    def hop_vars(self, adapter, release):
        r = adapter["releases"][release]
        shim = adapter["mirror"]["shim"]
        return {"release": release, "major": r["major"], "shim": shim,
                "repofile": repo_file(adapter, release, shim).replace("\n", "\\n"),
                "addons": addon_repos(adapter, release, shim, "/etc/yum.repos.d/ladder.repo") or "true",
                "pm": "$(command -v dnf || command -v yum)", "clean_repos": CLEAN_REPOS, "keys": write_keys(adapter),
                "dnf_opts": f"--releasever={r['major']} --allowerasing --setopt=install_weak_deps=False "
                            "--setopt=protected_packages="}

    def pm_install(self, adapter, release, packages, reposdir=None):
        v = self.hop_vars(adapter, release)
        if adapter["releases"][release]["pm"] == "yum":
            return "yum -y install " + " ".join(packages)
        rd = f"--setopt=reposdir={reposdir} " if reposdir else ""
        return f"dnf -y {rd}{v['dnf_opts']} install " + " ".join(packages)

    def step(self, s, adapter, release):
        if release is None:
            raise ValueError("rpm ops compile per hop: pass the release the hop arrives at")
        op, v, rel = s["op"], self.hop_vars(adapter, release), adapter["releases"][release]
        if op == "install":
            return f"{CLEAN_REPOS}; {self.pm_install(adapter, release, s['packages'])}"
        if op == "remove":
            # like apt, a package that isn't installed is not an error. rpm -e (not yum/dnf remove) so nothing that
            # depends on it is taken along: if something still needs it, the removal fails and the hop rolls back.
            return f"for p in {' '.join(s['packages'])}; do ! rpm -q $p >/dev/null 2>&1 || rpm -e $p || exit 1; done"
        if op == "repair":
            # rpm has no "-f install". Remove what is left with unmet dependencies from an older release.
            return ("for n in 1 2 3 4 5 6 7 8; do U=$(rpm -Va --nofiles --nodigest --noscripts 2>&1 | "
                    "grep -o 'Unsatisfied dependencies for [^: ]*' | awk '{print $4}' | "
                    f"grep -v '[.]el{v['major']}' | sort -u); [ -z \"$U\" ] && break; echo removing $U; "
                    "rpm -e --nodeps --noscripts $U; done; test -z \"$(rpm -Va --nofiles --nodigest --noscripts 2>&1)\"")
        if op == "force-remove":
            flags = {"depends": "--nodeps", "scripts": "--noscripts"}
            return "rpm -e " + "".join(flags[x] + " " for x in s["force"]) + " ".join(s["packages"])
        if op == "write-file":
            return printf(s["content"], s["path"])
        body = getattr(self, "op_" + op.replace(".", "_").replace("-", "_"), None)
        if not body:
            raise ValueError(f"the rpm backend has no op {op!r}")
        return body(s, adapter, release, v, rel)

    # ----- rpm.* -----

    def op_rpm_swap_release(self, s, adapter, release, v, rel):
        """Replace the release package by hand. EL5's centos-release has epoch 10, so yum won't "upgrade" it to EL6's
        epoch-0 one, and a vendor switch (CentOS -> AlmaLinux) isn't an upgrade at all. The package is fetched with
        curl, so it is checked against the release's keys (when it names any) before the old one goes."""
        base = f"{v['shim']}/{next(iter(rel['repos'].values()))}"
        return (f"set -e; cd /tmp; {primary_list(base, '/tmp/wend-pkglist')}; "
                f"f=$(grep -E '^{ere(s['install'])}-[0-9][^/]*(x86_64|noarch)\\.rpm$' /tmp/wend-pkglist | head -n1); "
                f"test -n \"$f\"; curl -sf -o /tmp/$f {base}/Packages/$f; "
                + "".join(f"{c}; " for c in [checksig(adapter, release, "/tmp/wend-keydb", "/tmp/$f")] if c) +
                f"rpm -e --nodeps --noscripts {s['remove']}; rpm -ivh --nodeps --noscripts /tmp/$f; "
                f"rm -rf /tmp/$f /tmp/wend-pkglist /tmp/wend-keydb; {CLEAN_REPOS}")

    def op_rpm_drop_unneeded(self, s, adapter, release, v, rel):
        """Remove these packages, together, only if nothing else installed still requires them; otherwise keep them
        all and say what needs them. For leftovers a release stops pulling in that some boxes still use (polkit:
        nothing on EL9 requires it, but tuned and pcsc-lite do)."""
        return (f"P=; for p in {' '.join(s['packages'])}; do ! rpm -q $p >/dev/null 2>&1 || P=\"$P $p\"; done; "
                "[ -n \"$P\" ] || exit 0; if E=$(rpm -e --test $P 2>&1); then rpm -e $P; else "
                "echo \"kept$P: still needed\"; echo \"$E\" | grep 'is needed by' | head -n 5; fi")

    def op_rpm_drop_arch(self, s, adapter, release, v, rel):
        """Remove every package of the given architectures from the rpm database and disk (no dependency checks, no
        scriptlets), then fail if any is left. EL5 x86_64 installs carried i386 multilib copies; EL6's transaction
        can't upgrade them alongside the x86_64 ones (file conflicts), and nothing on the box needs them."""
        alt = "|".join(sorted(s["arches"]))
        # Only multilib copies are disposable: refuse if any package of these arches has no twin of another arch
        # (a 32-bit-only app or compat library), so such a box stops here instead of losing it.
        return (f"set -e; cd /tmp; for n in $(rpm -qa --qf '%{{NAME}} %{{ARCH}}\\n' | awk '$2 ~ /^({alt})$/ {{print $1}}' "
                f"| sort -u); do rpm -q --qf '%{{ARCH}}\\n' $n | grep -qvE '^({alt})$' || "
                "{ echo \"refusing: $n is installed only as 32-bit, not a multilib copy\"; exit 1; }; done; "
                f"L=$(rpm -qa --qf '%{{NAME}}-%{{VERSION}}-%{{RELEASE}}.%{{ARCH}}\\n' | "
                f"grep -E '\\.({alt})$' || true); [ -z \"$L\" ] || rpm -e --nodeps --noscripts $L; "
                f"test -z \"$(rpm -qa --qf '%{{ARCH}}\\n' | grep -E '^({alt})$')\"")

    def op_rpm_bootstrap(self, s, adapter, release, v, rel):
        """EL5 -> EL6: rpm 4.4 can't read xz payloads and yum 3.2.22 can't check sha256 repodata, so neither can
        install their own successors. Unpack EL6's rpm/yum/python stack over / with 7-Zip (from archived EPEL 5,
        removed again afterwards), then let the new rpm rebuild its database. The EL6 transaction then owns them."""
        target = s["release"]
        boot = adapter["releases"][target]["bootstrap"]
        shim, b = v["shim"], "/var/lib/wend/boot"
        base = f"{shim}/{next(iter(adapter['releases'][target]['repos'].values()))}"
        p7 = f"{shim}/{boot['p7zip_repo']}"
        pkgs = " ".join(boot["packages"])
        return "\n".join([
            f"set -e; rm -rf {b}; mkdir -p {b}/x; cd {b}",
            primary_list(base, f"{b}/list"),
            f"for f in {' '.join(boot['p7zip'])}; do curl -sf -O {p7}/$f || curl -sf -O {p7}/Packages/$f || "
            f"curl -sf -O {p7}/Packages/p/$f; done",
            # nothing vouches for these but the adapter's pins: refuse other bytes, and record them (Rpm.sources)
            "; ".join(f"echo '{h.removeprefix('sha256:')}  {f}' | sha256sum -c - >/dev/null || "
                      f"{{ echo 'digest mismatch: {f}'; exit 1; }}" for f, h in boot["p7zip"].items()),
            f"mkdir -p {SOURCES}/{v['release']}; printf '%s %s\\n' "
            + " ".join(f"{u} {h}" for u, h in bootstrap_pins(adapter, target).items())
            + f" > {SOURCES}/{v['release']}/fetched",
            f"rpm -Uvh --nosignature {' '.join(boot['p7zip'])}",
            f"for n in {pkgs}; do f=$(grep -E \"^$n-[0-9][^-]*-[^-]*\\.(x86_64|noarch)\\.rpm$\" {b}/list | head -n1); "
            f"[ -n \"$f\" ] || {{ echo MISSING $n; continue; }}; curl -sf -O {base}/Packages/$f; done",
            f"for r in *.rpm; do case $r in p7zip*) continue;; esac; (cd x && 7z x -y ../$r >/dev/null) && "
            f"(cd / && cpio -idmu --quiet -f './etc/*' './usr/share/doc/*' './usr/share/man/*' < {b}/x/${{r%.rpm}}.cpio) "
            f"|| {{ echo EXTRACTFAIL $r; exit 1; }}; rm -f x/*.cpio; done",
            "cd /; ldconfig; rpm --version; rm -f /var/lib/rpm/__db.*; rpm --rebuilddb",
            "rpm -e p7zip p7zip-plugins",
            f"rm -rf {b}",
        ])

    def op_rpm_transit(self, s, adapter, release, v, rel):
        """EL7 -> EL8: rpm 4.11 can't install packages with rich dependencies. Download the EL8 equivalent of every
        installed package with a temporary EL7 dnf, unpack them into a staging dir, and run EL8's own rpm through
        EL8's ld.so over the whole system. Then a real EL8 dnf distro-sync, the packages it erased (`reinstall`), and
        the EL7 leftovers dropped from the database. The box only passes through EL8: the hop's own transaction
        takes it on to the next release."""
        target = s["release"]
        boot = adapter["releases"][target]["transit"]
        shim, b = v["shim"], "/var/lib/wend/boot"
        frm = adapter["releases"][boot["from"]]
        t = adapter["releases"][target]
        old_repos = "".join(f"[boot-{n}]\nname=boot-{n}\nbaseurl={shim}/{u}/\ngpgcheck=0\nenabled=1\n\n"
                            for n, u in {**frm["repos"], **boot["extra_repos"]}.items()).replace("\n", "\\n")
        new_repos = repo_file(adapter, target, shim).replace("\n", "\\n")
        o = (f"--setopt=reposdir={b}/new --releasever={t['major']} --allowerasing --setopt=install_weak_deps=False "
             "--setopt=protected_packages=")
        ld, rpmS = f"{b}/stage/usr/lib64/ld-linux-x86-64.so.2 --library-path {b}/stage/usr/lib64", f"{b}/stage/usr/bin/rpm"
        tag = f"el{t['major']}"
        base = f"{shim}/{next(iter(t['repos'].values()))}"
        reinstall = " ".join(s["reinstall"])
        succ = [f"{shim}/{u}" for u in t["repos"].values()] + \
            [f"{shim}/{a['repos'][target]}" for a in adapter.get("addons", {}).values() if target in a["repos"]]
        return "\n".join([
            # The last step drops every package EL8 doesn't ship from the rpm database (files kept). That is only
            # acceptable for the old release's own packages: refuse up front if anything else is installed (EPEL,
            # vendor or locally built RPMs), so such a box stops here instead of ending up with unowned software.
            # CentOS's own SIG and Software Collections packages carry its vendor too but EL8 doesn't ship them, so
            # those are refused by name.
            # Add-on repositories (SPEC "Add-on repositories") are the exception: their vendor's packages pass if the
            # add-on's marker is installed, and the next step requires a successor for each of them.
            f"{addon_vendors(adapter)}V=$(rpm -q --qf '%{{VENDOR}}' {s['remove']}); T=$(rpm -qa --qf "
            "'%{NAME}|%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}|%{VENDOR}\\n' | awk -F'|' -v v=\"$V\" -v a=\"$A\" "
            f"'{VENDOR_LAST} $1 != \"gpg-pubkey\" && !index(a, \"|\" x \"|\") && (x != v || "
            "$1 ~ /^(rh-|devtoolset-|centos-release-.)/ && $1 != \"centos-release-notes\") {print $2}'); [ -z \"$T\" ] || "
            "{ printf 'refusing, not plain %s packages: ' \"$V\"; printf '%s ' $T; echo; exit 1; }",
            f"set -e; rm -rf {b}; mkdir -p {b}/old {b}/new {b}/pkgs {b}/stage",
            # every add-on package needs a package of the same name in the release's repos or its add-on repos, or
            # it would be dropped from the database at the end: refuse before changing anything, naming them
            f"P=$(rpm -qa --qf '%{{NAME}}|%{{NAME}}|%{{VENDOR}}\\n' | awk -F'|' -v a=\"$A\" '{VENDOR_LAST} index(a, \"|\" x \"|\") "
            "{print $1}' | "
            f"sort -u); if [ -n \"$P\" ]; then : > {b}/names; "
            + "".join(f"{package_names(u, f'{b}/names')}; " for u in succ) +
            f"M=$(for n in $P; do grep -qxF \"$n\" {b}/names || echo $n; done); [ -z \"$M\" ] || {{ echo "
            f"\"refusing, no {target} package for these add-on packages (a fix must remove or replace them first):\" "
            "$M; exit 1; }; fi",
            f"printf '{old_repos}' > {b}/old/boot.repo; printf '{new_repos}' > {b}/new/ladder.repo"
            + "".join(f"; {x}" for x in [addon_repos(adapter, target, shim, f"{b}/new/ladder.repo")] if x),
            f"yum -y --setopt=reposdir={b}/old install dnf",
            # EL7's dnf 4.0 has no --nobest. rc ignored, as in the lab: what matters is that the new rpm, glibc and
            # dnf were fetched (checked next)
            f"dnf -y {o} --setopt=best=False --setopt=deltarpm=false --nogpgcheck --downloadonly --destdir={b}/pkgs distro-sync "
            f"> {b}/dl.log 2>&1 || tail -n 20 {b}/dl.log",
            f"cd {b}/pkgs; for n in rpm rpm-libs glibc dnf python3-dnf; do ls | grep -qE \"^$n-[0-9]\" || "
            "{ echo \"not downloaded: $n\"; exit 1; }; done",
            f"ls | {NAMES} > {b}/got; M=$(for n in $P; do grep -qxF \"$n\" {b}/got || echo $n; done); "
            "[ -z \"$M\" ] || { echo \"not downloaded:\" $M; exit 1; }",
            "rpm -e dnf; rm -f /var/lib/rpm/__db.*; rpm --rebuilddb",
            f"ls | grep -qE '^{boot['release_package']}-' || {{ {primary_list(base, f'{b}/list')}; "
            f"f=$(grep -E '^{boot['release_package']}-{t['major']}[^/]*x86_64\\.rpm$' {b}/list | sort -V | tail -n1); "
            f"test -n \"$f\"; curl -sf -o {b}/pkgs/$f {base}/Packages/$f; }}",
            *transit_checksig(adapter, target, b),
            f"for r in *.rpm; do rpm2cpio $r | (cd {b}/stage && cpio -idmu --quiet 2>/dev/null) || {{ echo EXTRACTFAIL $r; exit 1; }}; done",
            f"{ld} {rpmS} --version",
            f"rpm -e --nodeps --noscripts {s['remove']}",
            f"{ld} {rpmS} -Uvh --nodeps --replacefiles --replacepkgs --oldpackage --nosignature --nodigest {b}/pkgs/*.rpm "
            f"> {b}/rpm.log 2>&1 || {{ grep -iE 'error|fail' {b}/rpm.log | head -n 20; }}",
            "/usr/sbin/ldconfig; /bin/sh -c true; rm -f /var/lib/rpm/__db.*; rpm --rebuilddb",
            f"grep -q 'release {t['major']}[. ]' /etc/redhat-release; {CLEAN_REPOS}",
            # the first EL8 transaction trips over EL7 yum's directories; tolerated as in the lab, then cleared below
            f"dnf -y {o} --nobest distro-sync > {b}/sync.log 2>&1 || grep -iE 'error|conflict' {b}/sync.log | head -n 20",
            "rpm -qa --qf '%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}\\n' | grep -E '^yum' | grep -E '\\.el7' | "
            "xargs -r rpm -e --nodeps --noscripts",
            f"mkdir -p {SAVED}/etc/yum; for d in pluginconf.d protected.d vars; do "
            f"if [ -d /etc/yum/$d ] && [ ! -L /etc/yum/$d ]; then mv /etc/yum/$d {SAVED}/etc/yum/; fi; done",
            f"dnf -y {o} install yum {reinstall}",
            f"L=$(rpm -qa --qf '%{{NAME}}-%{{VERSION}}-%{{RELEASE}}.%{{ARCH}}\\n' | grep -v '^gpg-pubkey' | grep -v '{tag}' || true); "
            "[ -z \"$L\" ] || { echo \"dropped from the rpm database (files kept): $L\"; "
            "rpm -e --nodeps --noscripts --justdb $L; }",
            # the hop's refresh cleans every cache: keep the transit repos' metadata so the hop pins it (Rpm.sources)
            # (only its own repos: older releases' caches outlive their hops)
            f"S={SOURCES}/{v['release']}; rm -rf $S; mkdir -p $S; for f in /var/cache/dnf/*/repodata/repomd.xml "
            "/var/cache/yum/*/*/*/repomd.xml; do [ -f \"$f\" ] || continue; d=${f%/repomd.xml}; d=${d%/repodata}; "
            "d=${d##*/}; case $f in /var/cache/dnf/*) d=${d%-*};; esac; "
            f"case $d in boot-*|{target}-*) cp \"$f\" \"$S/$d.repomd.xml\";; esac; done",
            f"cd /; rm -rf {b}; {CLEAN_REPOS}",
        ])

    def op_rpm_adopt_rpmnew(self, s, adapter, release, v, rel):
        """Take the packaged config where rpm left it as .rpmnew; the old file is kept under /var/lib/wend/saved."""
        return (f"for f in {' '.join(s['paths'])}; do [ -f \"$f.rpmnew\" ] || continue; "
                f"mkdir -p \"{SAVED}$(dirname \"$f\")\"; [ -e \"{SAVED}$f\" ] || cp -a \"$f\" \"{SAVED}$f\"; "
                "mv -f \"$f.rpmnew\" \"$f\"; "
                "echo adopted $f; done")

    def op_rpm_set_aside_unowned(self, s, adapter, release, v, rel):
        """Move config files that no installed package owns (left by packages an older release had) out of the way."""
        return (f"for f in {' '.join(s['paths'])}; do [ -f \"$f\" ] || continue; rpm -qf \"$f\" >/dev/null 2>&1 && "
                f"continue; mkdir -p \"{SAVED}$(dirname \"$f\")\"; if [ -e \"{SAVED}$f\" ]; then rm -f \"$f\"; "
                f"else mv -f \"$f\" \"{SAVED}$f\"; fi; echo set aside $f; done")

    # ----- fs.* -----

    def op_fs_layout(self, s, adapter, release, v, rel):
        if s["layout"] == "usrmove":
            # EL7's filesystem package needs /bin, /sbin, /lib, /lib64 to be symlinks into /usr. redhat-upgrade-tool
            # does this at boot; here it happens in place: copy what /usr lacks, then swap each dir for a symlink.
            return "\n".join([
                "set -e",
                "for d in bin sbin lib lib64; do [ -L /$d ] && continue; cd /$d; "
                "find . -type d | while read x; do mkdir -p \"/usr/$d/$x\"; done; "
                "find . ! -type d | while read f; do t=\"/usr/$d/$f\"; "
                "if [ -L \"$t\" ]; then if [ \"$(readlink -f \"$t\")\" = \"$(readlink -f \"/$d/$f\")\" ]; then "
                "rm -f \"$t\"; cp -a \"/$d/$f\" \"$t\"; fi; elif [ ! -e \"$t\" ]; then cp -a \"/$d/$f\" \"$t\"; fi; "
                "done; done",
                "cd /; python -c \"import os; [ (os.rename('/'+d,'/'+d+'.usrmove'), os.symlink('usr/'+d,'/'+d)) "
                "for d in ('bin','sbin','lib','lib64') if not os.path.islink('/'+d) ]\"",
                "/bin/sh -c true; ldconfig; rpm -q glibc bash",
                "rm -rf /bin.usrmove /sbin.usrmove /lib.usrmove /lib64.usrmove",
            ])
        if s["layout"] == "run":
            # /var/run -> /run and /var/lock -> /run/lock, which EL7's filesystem package expects
            return ("mkdir -p /run && if [ -d /var/run ] && [ ! -L /var/run ]; then cp -a /var/run/. /run/ && "
                    "rm -rf /var/run && ln -s ../run /var/run; fi; if [ -d /var/lock ] && [ ! -L /var/lock ]; then "
                    "mkdir -p /run/lock && cp -a /var/lock/. /run/lock/ && rm -rf /var/lock && ln -s ../run/lock /var/lock; "
                    "fi; test -L /var/run")
        raise ValueError(s["layout"])

    # ----- kv.* : key=value files without sections (vsftpd.conf), and deleting keys anywhere -----

    def op_kv_set(self, s, adapter, release, v, rel):
        p = s["path"]
        dels = " ".join(f"-e '/^[[:space:]]*#*[[:space:]]*{k}[[:space:]]*=/d'" for k in sorted(s["values"]))
        adds = "".join(f"{k}={s['values'][k]}\\n" for k in sorted(s["values"]))
        return f"test -f {p} && sed -i {dels} {p} && printf '{adds}' >> {p}"

    def op_kv_unset(self, s, adapter, release, v, rel):
        p = s["path"]
        dels = " ".join(f"-e '/^[[:space:]]*{k}[[:space:]]*=/d'" for k in s["keys"])
        return f"test -f {p} && sed -i {dels} {p}"

    # ----- daemons that have to run once to migrate their own data -----

    def op_mysql_set_password(self, s, adapter, release, v, rel):
        """Rehash one account's password with the current scheme (MySQL 4.0's 16-character hashes stop working with
        MariaDB 10's clients). The plaintext comes from a file the box's owner puts in /etc/wend/secrets, never from
        the fix, so no password ever lands in a public repo."""
        sql = (f"SET SESSION old_passwords=0; UPDATE mysql.user SET Password=PASSWORD('$P') "
               f"WHERE User='{s['user']}';")
        return (f"P=$(cat /etc/wend/secrets/{s['secret']} 2>/dev/null); case \"$P\" in ''|*[!A-Za-z0-9_.@%+=-]*) "
                f"echo 'no usable /etc/wend/secrets/{s['secret']}'; exit 1;; esac; {STOP_DB}; {DB_DAEMON}; {DB_DIRS}; ($D --user=mysql --skip-grant-tables --skip-networking "
                f">/tmp/wend-db.log 2>&1 &); {WAIT_SOCK}; C=$(command -v mariadb || command -v mysql); "
                f"$C -uroot -e \"{sql}\"; rc=$?; {STOP_DB} && exit $rc")

    def op_mysql_clean_shutdown(self, s, adapter, release, v, rel):
        """Start the server once and stop it with a slow shutdown (innodb_fast_shutdown=0), so the next major
        version finds a clean redo log. MariaDB 10.11 refuses 10.5's redo log otherwise."""
        return (f"{STOP_DB}; {DB_DAEMON}; {DB_DIRS}; ($D --user=mysql --skip-networking --innodb-fast-shutdown=0 "
                ">/tmp/wend-db.log 2>&1 &); "
                f"{WAIT_SOCK}; sleep 2; {STOP_DB}; rc=$?; tail -n 3 /tmp/wend-db.log; exit $rc")

    def op_mysql_upgrade_data(self, s, adapter, release, v, rel):
        """Start the new server on the old data directory once, run its upgrade tool, stop it cleanly. The data must
        come out stamped with the running server's version: that is what proves the upgrade ran, not the tool's exit
        code (it complains about things it then fixes)."""
        # no grant tables: a root password the box set can't lock the upgrade out (the socket is the only way in)
        return (f"{STOP_DB}; {DB_DAEMON}; {DB_DIRS}; "
                "($D --user=mysql --skip-networking --skip-grant-tables >/tmp/wend-db.log 2>&1 &); "
                f"{WAIT_SOCK}; {UPGRADE_DB}; {STOP_DB}; rc=$?; [ $rc = 0 ] || tail -n 15 /tmp/wend-db.log; exit $rc")

    # ----- one-off service and app plumbing -----

    def op_ssh_generate_host_keys(self, s, adapter, release, v, rel):
        """Create the host key types this sshd expects and the box lacks. sshd-keygen does this at boot."""
        return "ssh-keygen -A"

    def op_httpd_drop_missing_modules(self, s, adapter, release, v, rel):
        """Comment out every LoadModule whose module this httpd can't load any more (2.2 -> 2.4 dropped some): first
        the ones whose file is gone (including those inside an <IfModule> httpd -t never reaches, like EL6 php.conf's
        libphp5-zts.so line), then whatever httpd -t still reports."""
        return ("cd /etc/httpd && for c in conf/*.conf conf.d/*.conf conf.modules.d/*.conf; do [ -f \"$c\" ] || continue; "
                "for m in $(grep -E '^[[:space:]]*LoadModule' \"$c\" | awk '{print $3}'); do [ -e \"$m\" ] && continue; "
                "echo disabling $m; sed -i \"s|^\\([[:space:]]*LoadModule[[:space:]].*[[:space:]]$m[[:space:]]*\\)$|#\\1|\" \"$c\"; "
                "done; done; for n in $(seq 1 40); do out=$(/usr/sbin/httpd -t 2>&1) && break; "
                "m=$(echo \"$out\" | grep -o 'Cannot load [^ ]*' | head -n1 | awk '{print $3}'); "
                "[ -n \"$m\" ] || { echo \"$out\"; break; }; b=$(basename $m); echo disabling $b; "
                "grep -rl \"LoadModule.*$b\" conf conf.d conf.modules.d 2>/dev/null | "
                "xargs -r sed -i \"s|^\\([[:space:]]*LoadModule[[:space:]].*$b.*\\)$|#\\1|\"; done; /usr/sbin/httpd -t")

    def op_php_mysqli_report_off(self, s, adapter, release, v, rel):
        """PHP 8.1 made mysqli throw exceptions where it used to warn and return false. Restore the old default for
        code written against it (mysqli_report(MYSQLI_REPORT_OFF), run before every script). The app is untouched.
        auto_prepend_file holds one file, so if the owner already set one in php.ini or php.d, theirs stays and this
        does nothing (and says so): ours would sort last and silently replace it."""
        php = "<?php if (function_exists(\"mysqli_report\")) { mysqli_report(MYSQLI_REPORT_OFF); }\\n"
        ini = "/etc/php.d/99-wend-mysqli-report-off.ini"
        # PHP's own reading: files in byte order (php.ini, then php.d), each line on its own even without a final
        # newline, the last global setting wins, [PATH=...] and [HOST=...] sections only apply there, and the
        # keywords none/off/no/false/null (any case) mean empty
        return ("V=$(awk 'FNR == 1 { s = 0 } /^[[:space:]]*\\[/ { s = tolower($0) ~ /^[[:space:]]*\\[(path|host)=/ } "
                "!s && sub(/^[[:space:]]*auto_prepend_file[[:space:]]*=[[:space:]]*/, \"\") { v = $0 } END { print v }' "
                "$([ -f /etc/php.ini ] && echo /etc/php.ini) $(LC_ALL=C ls /etc/php.d/*.ini 2>/dev/null | grep -vx " + ini + ") </dev/null 2>/dev/null | "
                "sed 's/;.*//' | tr -d \"\\\"' \\t\\r\"); "
                "case $(echo \"$V\" | tr A-Z a-z) in ''|none|off|no|false|null) ;; *) rm -f " + ini + "; "
                "echo \"auto_prepend_file is already $V: left as is, mysqli_report not changed\"; exit 0;; esac; "
                f"mkdir -p /usr/share/wend && printf '{php}' > /usr/share/wend/php-mysqli-report-off.php && "
                "printf 'auto_prepend_file=/usr/share/wend/php-mysqli-report-off.php\\n' > " + ini)

    # ----- boot: SysV runlevels to systemd -----

    def op_service_record(self, s, adapter, release, v, rel):
        """Write down which SysV services start at boot (S links in the default runlevel), before the hop that
        replaces SysV init with systemd: the old packages' scriptlets remove their links, and systemd's presets
        leave most new units disabled. On a box that already runs systemd, its enabled service units, before a hop
        that erases and reinstalls packages (EL8's module streams). Only the first record is kept, so it is always
        the box's own state."""
        r = SYSV_RECORD
        # a systemd box (EL7 on) records its enabled service units instead, by unit name
        return (f"if [ -e {r} ]; then echo \"kept the earlier record: $(tr '\\n' ' ' < {r})\"; exit 0; fi; "
                "if command -v systemctl >/dev/null 2>&1; then mkdir -p /var/lib/wend && systemctl list-unit-files "
                "--type=service --state=enabled --no-legend | awk '{sub(/\\.service$/, \"\", $1); print $1}' | sort -u "
                f"> {r}.new && test -s {r}.new && mv {r}.new {r} && echo \"systemd starts: $(tr '\\n' ' ' < {r})\"; "
                f"R=$?; rm -f {r}.new; exit $R; fi; "
                "L=$(sed -n 's/^id:\\([0-9]\\):initdefault:.*/\\1/p' /etc/inittab 2>/dev/null | head -n 1); "
                "[ -n \"$L\" ] || L=3; [ -d /etc/rc.d/rc$L.d ] || { echo \"no /etc/rc.d/rc$L.d: no SysV runlevels to record\"; exit 1; }; "
                f"mkdir -p /var/lib/wend && ls /etc/rc.d/rc$L.d | sed -n 's/^S[0-9][0-9]//p' | sort -u > {r}.new && "
                f"mv {r}.new {r} && echo \"runlevel $L starts: $(tr '\\n' ' ' < {r})\"")

    def op_net_ifcfg_to_keyfile(self, s, adapter, release, v, rel):
        return net_ifcfg_to_keyfile()

    def op_service_enable(self, s, adapter, release, v, rel):
        """Enable a systemd unit at boot if the service it replaces or follows was: one of `like` is in the SysV
        record, or is an enabled unit now. Never disables anything; a unit nothing it follows started is left as is."""
        like, u = " ".join(s["like"]), s["unit"]
        return (f"W=''; for n in {like}; do if grep -Fqx \"$n\" {SYSV_RECORD} 2>/dev/null || "
                "[ \"$(systemctl is-enabled \"$n\" 2>/dev/null)\" = enabled ]; then W=$n; break; fi; done; "
                f"[ -n \"$W\" ] || {{ echo 'none of {like} starts at boot: {u} left as is'; exit 0; }}; "
                f"systemctl enable {u} || exit 1; S=$(systemctl is-enabled {u} 2>/dev/null); "
                f"[ \"$S\" = enabled ] || {{ echo \"{u} is $S after systemctl enable, not enabled\"; exit 1; }}; "
                f"echo \"{u} enabled, like $W\"")

NET_IFCFG_TO_KEYFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "net_ifcfg_to_keyfile.py")
# SPEC.md, apt.drop-esm-pins: ubuntu-advantage-tools' own file names and lines, nothing else
ESM_PINS = ("for f in /etc/apt/preferences.d/ubuntu-esm-{release} /etc/apt/preferences.d/ubuntu-esm-*-{release}; do "
            "[ -e \"$f\" ] || [ -L \"$f\" ] || continue; "
            "if [ -L \"$f\" ] || [ ! -f \"$f\" ]; then echo \"$f: not a plain file, left\"; continue; fi; "
            "if dpkg -S \"$f\" >/dev/null 2>&1; then echo \"$f: a package owns it, left\"; continue; fi; "
            "if grep -qvxE '(Package: [*]|Pin: release o=UbuntuESM[A-Za-z]*, n={release}|Pin-Priority: never|#.*|"
            "[[:space:]]*)' \"$f\" || ! grep -qx 'Package: [*]' \"$f\" || "
            "! grep -qxE 'Pin: release o=UbuntuESM[A-Za-z]*, n={release}' \"$f\" || ! grep -qx 'Pin-Priority: never' \"$f\"; "
            "then "
            "echo \"$f: not an ubuntu-advantage-tools ESM pin, left\"; continue; fi; "
            "rm -f \"$f\" || exit 1; echo \"$f: ubuntu-advantage-tools' ESM pin for {release}, removed\"; done")
NET_IFUPDOWN_DNS_TO_RESOLVED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "net_ifupdown_dns_to_resolved.py")


def here_doc(path):
    """An op whose reference is a Python file runs on the box as a here-document, so no file of wend's is needed
    there. The client embeds the same source (include_str!), and parity compares the two."""
    src = open(path).read()
    if "WEND_PY" in src:  # the here-document would end early (an assert goes away under python -O)
        raise ValueError(f"{path} holds the here-document marker")
    return f"python3 - <<'WEND_PY'\n{src}WEND_PY"


def net_ifcfg_to_keyfile():
    """SPEC.md, net.ifcfg-to-keyfile."""
    return here_doc(NET_IFCFG_TO_KEYFILE)


BACKENDS = {"apt": Apt(), "rpm": Rpm()}


def of(adapter):
    return BACKENDS[adapter.get("backend", "apt")]
