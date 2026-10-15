//! Op backends: what each typed op compiles to, per package manager (SPEC.md, "Op reference").
//!
//! These are the reference commands, byte for byte what tools/backends.py emits; `client/parity.py` diffs the two
//! over every fix, every route hop and every op. SPEC allows another client to run different commands with the
//! same effect and failure conditions, but the reference is what CI proved, so v0 runs exactly it.
//!
//! Every value substituted here has passed the checker (crate::check) first: package names, paths and adapter
//! op arguments match strict patterns, so none carries a quote, a space or a `$`. Adapter values are reviewed
//! harness code and some (like `{dnf_opts}`) do.

use crate::atlas::{get, list_of, scalar, str_of, strings, Doc};
use regex::Regex;
use serde_json::{Map, Value};
use std::sync::OnceLock;

pub type Vars = Vec<(String, String)>;

fn var<'a>(v: &'a Vars, k: &str) -> &'a str {
    v.iter().find(|(n, _)| n == k).map(|(_, x)| x.as_str()).unwrap_or("")
}

/// `{name}` placeholders only (lowercase and `_`), replaced literally. Unknown names stay as they are, and so does
/// every other brace: templates are shell, with `${...}` and `%{...}` in them.
pub fn fill(template: &str, values: &Vars) -> String {
    static RE: OnceLock<Regex> = OnceLock::new();
    let re = RE.get_or_init(|| Regex::new(r"\{([a-z_]+)\}").unwrap());
    re.replace_all(template, |c: &regex::Captures| {
        match values.iter().find(|(n, _)| n == &c[1]) {
            Some((_, v)) => v.clone(),
            None => c[0].to_string(),
        }
    })
    .into_owned()
}

/// A precondition as a shell test, from the adapter's predicate templates. Harness code, not fix code.
pub fn pred_sh(w: &Value, adapter: &Doc) -> Result<String, String> {
    let m = w
        .as_object()
        .filter(|m| m.len() == 1)
        .ok_or("a predicate is a table with exactly one key")?;
    let (k, v) = m.iter().next().unwrap();
    let p = get(adapter, "predicates").ok_or("the adapter has no [predicates]")?;
    let t = |name: &str| str_of(p, name).map(String::from);
    let s = |x: &Value| x.as_str().map(String::from).ok_or(format!("{k}: expected a string"));
    Ok(match k.as_str() {
        "installed" => t("installed")?.replace("{pkg}", &s(v)?),
        "file" => t("file")?.replace("{path}", &s(v)?),
        "file-contains" => t("file-contains")?
            .replace("{path}", str_of(v, "path")?)
            .replace("{text}", str_of(v, "text")?),
        "mysql-option" => {
            let value = get(v, "value");
            let vars = vec![
                ("path".into(), str_of(v, "path")?.into()),
                ("key".into(), str_of(v, "key")?.into()),
                ("value".into(), value.and_then(Value::as_str).unwrap_or("").into()),
                ("any".into(), if value.is_some() { "0" } else { "1" }.into()),
            ];
            fill(&t("mysql-option")?, &vars)
        }
        "not" => format!("! ( {} )", pred_sh(v, adapter)?),
        "all" | "any" => {
            let j = if k == "all" { " && " } else { " || " };
            let parts = v
                .as_array()
                .ok_or(format!("{k} needs a list"))?
                .iter()
                .map(|x| pred_sh(x, adapter).map(|s| format!("( {s} )")))
                .collect::<Result<Vec<_>, _>>()?;
            format!("( {} )", parts.join(j))
        }
        other => return Err(format!("unknown predicate {other:?}")),
    })
}

/// A package name (already held to a name pattern and SAFE_VALUE) as a literal in an extended regex.
fn ere(name: &str) -> String {
    name.replace('.', "[.]").replace('+', "[+]")
}

/// Shell that writes content to path. The content goes through %b, never as the format itself, so content that
/// starts with "-" (an armoured key) isn't read as an option, and % is literal. Only \\ and \\n are escapes in it.
fn printf(content: &str, path: &str) -> String {
    format!(
        "printf '%b' '{}' > {path}",
        content.replace('\\', "\\\\").replace('\n', "\\n")
    )
}

pub fn backend_of(adapter: &Doc) -> &str {
    get(adapter, "backend").and_then(Value::as_str).unwrap_or("apt")
}

/// Everything a backend implements. The checker holds adapters to these lists: an adapter can narrow what fixes
/// may do, never widen it past what this file implements.
pub struct Caps {
    pub name: &'static str,
    pub ops: &'static [&'static str],
    pub force: &'static [&'static str],
    pub pm_options: &'static [&'static str],
}

pub const APT: Caps = Caps {
    name: "apt",
    ops: &[
        "apache.enable",
        "apache.disable",
        "dpkg.reinstall",
        "net.ifupdown-dns-to-resolved",
        "apt.drop-esm-pins",
    ],
    force: &["depends", "remove-essential"],
    pm_options: &["overwrite-conflicting-files"],
};

pub const RPM: Caps = Caps {
    name: "rpm",
    ops: &[
        "rpm.swap-release",
        "rpm.drop-arch",
        "rpm.drop-unneeded",
        "rpm.bootstrap",
        "rpm.transit",
        "rpm.adopt-rpmnew",
        "rpm.set-aside-unowned",
        "fs.layout",
        "kv.set",
        "kv.unset",
        "mysql.set-password",
        "mysql.clean-shutdown",
        "mysql.upgrade-data",
        "ssh.generate-host-keys",
        "httpd.drop-missing-modules",
        "php.mysqli-report-off",
        "service.record",
        "service.enable",
        "net.ifcfg-to-keyfile",
    ],
    // no --justdb for fixes: dropping a package from the database while its files stay is how a fix would fake
    // the pkgdb check. rpm.transit uses it internally, on the previous release's leftovers only.
    force: &["depends", "scripts"],
    pm_options: &[],
};

pub fn caps(backend: &str) -> Option<&'static Caps> {
    match backend {
        "apt" => Some(&APT),
        "rpm" => Some(&RPM),
        _ => None,
    }
}

/// The values a hop's templates use (SPEC.md, "Adapter format").
pub fn hop_vars(adapter: &Doc, release: &str) -> Result<Vars, String> {
    match backend_of(adapter) {
        "apt" => {
            let lad = list_of(adapter, "ladder")?;
            let r = get(adapter, "releases").ok_or("the adapter has no [releases]")?;
            let idx = |x: &str| {
                lad.iter()
                    .position(|y| y == x)
                    .ok_or(format!("{x} is not on the ladder"))
            };
            let mirror = if idx(release)? >= idx(str_of(r, "current_from")?)? {
                str_of(r, "current")?
            } else {
                str_of(r, "archive")?
            };
            // the room the release's kernel needs in /boot (0: none known)
            let boot_mib = get(adapter, "hardware")
                .and_then(|h| get(h, release))
                .and_then(|t| get(t, "boot_mib"))
                .and_then(Value::as_u64)
                .unwrap_or(0);
            Ok(vec![
                ("mirror".into(), mirror.into()),
                ("release".into(), release.into()),
                ("boot_mib".into(), boot_mib.to_string()),
            ])
        }
        "rpm" => {
            let r = release_of(adapter, release)?;
            let shim = str_of(get(adapter, "mirror").ok_or("the adapter has no [mirror]")?, "shim")?;
            let major = scalar(get(r, "major").ok_or("release without a major")?);
            Ok(vec![
                ("release".into(), release.into()),
                ("major".into(), major.clone()),
                ("shim".into(), shim.into()),
                ("repofile".into(), repo_file(adapter, release, shim)?.replace('\n', "\\n")),
                ("addons".into(), {
                    let a = addon_repos(adapter, release, shim, "/etc/yum.repos.d/ladder.repo");
                    if a.is_empty() { "true".into() } else { a }
                }),
                ("pm".into(), "$(command -v dnf || command -v yum)".into()),
                ("clean_repos".into(), CLEAN_REPOS.into()),
                ("keys".into(), write_keys(adapter)),
                (
                    "dnf_opts".into(),
                    format!(
                        "--releasever={major} --allowerasing --setopt=install_weak_deps=False --setopt=protected_packages="
                    ),
                ),
            ])
        }
        b => Err(format!("no backend {b:?}")),
    }
}

/// The shell one step runs on the hop that arrives at `release`.
pub fn step(s: &Value, adapter: &Doc, release: &str) -> Result<String, String> {
    match backend_of(adapter) {
        "apt" => apt_step(s, adapter),
        "rpm" => rpm_step(s, adapter, release),
        b => Err(format!("no backend {b:?}")),
    }
}

fn packages(s: &Value) -> Result<String, String> {
    Ok(list_of(s, "packages")?.join(" "))
}

fn flag(s: &Value, k: &str) -> Result<bool, String> {
    get(s, k)
        .and_then(Value::as_bool)
        .ok_or(format!("missing boolean {k:?}"))
}

fn apt_step(s: &Value, adapter: &Doc) -> Result<String, String> {
    let op = str_of(s, "op")?;
    Ok(match op {
        "install" => format!("apt-get install {}", packages(s)?),
        "remove" => format!(
            "{}{}",
            if flag(s, "purge")? {
                "apt-get purge "
            } else {
                "apt-get remove "
            },
            packages(s)?
        ),
        "repair" => "apt-get -f install".into(),
        "force-remove" => {
            let f: String = list_of(s, "force")?.iter().map(|x| format!("--force-{x} ")).collect();
            format!(
                "dpkg --{} {f}{}",
                if flag(s, "purge")? { "purge" } else { "remove" },
                packages(s)?
            )
        }
        "write-file" => printf(str_of(s, "content")?, str_of(s, "path")?),
        "pm-option" => {
            let o = get(adapter, "pm_options")
                .and_then(|p| get(p, str_of(s, "option").ok()?))
                .ok_or("unknown pm-option")?;
            printf(str_of(o, "content")?, str_of(o, "path")?)
        }
        "apache.enable" | "apache.disable" => format!(
            "a2{}{} {}",
            if op == "apache.enable" { "en" } else { "dis" },
            str_of(s, "kind")?,
            str_of(s, "name")?
        ),
        "dpkg.reinstall" => format!("apt-get install --reinstall --only-upgrade {}", packages(s)?),
        "net.ifupdown-dns-to-resolved" => {
            // SPEC.md, net.ifupdown-dns-to-resolved: the same source the Python engine sends
            format!(
                "python3 - <<'WEND_PY'\n{}WEND_PY",
                include_str!("../../tools/net_ifupdown_dns_to_resolved.py")
            )
        }
        // SPEC.md, apt.drop-esm-pins: ubuntu-advantage-tools' own file names and lines, nothing else
        "apt.drop-esm-pins" => ESM_PINS.replace("{release}", str_of(s, "release")?),
        _ => return Err(format!("the apt backend has no op {op:?}")),
    })
}

// ---------- rpm ----------

// Release packages drop vendor .repo files that point at mirrors long gone. The harness owns /etc/yum.repos.d
// (like sources.list), so anything that may have installed one cleans up after itself.
pub const CLEAN_REPOS: &str =
    "for f in /etc/yum.repos.d/*.repo; do [ \"$f\" = /etc/yum.repos.d/ladder.repo ] || rm -f \"$f\"; done";
const DB_DAEMON: &str =
    "D=$(ls /usr/libexec/mariadbd /usr/libexec/mysqld /usr/sbin/mariadbd /usr/sbin/mysqld 2>/dev/null | head -n1)";
const DB_DIRS: &str = concat!(
    "mkdir -p /run/mariadb /run/mysqld /var/log/mariadb; touch /var/log/mariadb/mariadb.log; ",
    "chown -R mysql:mysql /run/mariadb /run/mysqld /var/log/mariadb /var/lib/mysql; ",
    "rm -f /var/lib/mysql/mysql.sock /var/lib/mysql/*.pid"
);
const WAIT_SOCK: &str = concat!(
    "[ -n \"$D\" ] || { echo 'no database server installed'; exit 1; }; ",
    "i=0; while [ $i -lt 120 ] && [ ! -S /var/lib/mysql/mysql.sock ]; do sleep 1; i=$((i+1)); done; ",
    "[ -S /var/lib/mysql/mysql.sock ] || { echo 'the database server did not start'; ",
    "tail -n 15 /tmp/wend-db.log; pkill -KILL -x mariadbd; pkill -KILL -x mysqld; exit 1; }"
);
const STOP_DB: &str = concat!(
    "pkill -TERM -x mariadbd; pkill -TERM -x mysqld; i=0; ",
    "while pgrep -x 'mariadbd|mysqld' >/dev/null && [ $i -lt 300 ]; do sleep 1; i=$((i+1)); done; ",
    "! pgrep -x 'mariadbd|mysqld' >/dev/null"
);
/// mysql.upgrade-data: run the server's own upgrade tool and check the data now carries the server's version stamp
/// (SPEC.md, mysql.upgrade-data)
fn upgrade_db() -> String {
    format!(
        concat!(
            "U=$(command -v mariadb-upgrade || command -v mysql_upgrade); ",
            "[ -n \"$U\" ] || {{ echo 'no mariadb-upgrade or mysql_upgrade installed'; {stop}; exit 1; }}; ",
            "V=$($D --version | sed -n 's/.* Ver \\([^ -]*\\).*/\\1/p'); ",
            "timeout 1200 $U -uroot --force >/tmp/wend-upgrade.log 2>&1; u=$?; tail -n 3 /tmp/wend-upgrade.log; ",
            "S=$(cat /var/lib/mysql/mariadb_upgrade_info /var/lib/mysql/mysql_upgrade_info 2>/dev/null | head -n 1); ",
            "if [ -z \"$V\" ] || [ \"${{S%%-*}}\" != \"$V\" ]; then ",
            "echo \"$U (exit $u) left the data stamped ${{S:-nothing}}, the server is ${{V:-unknown}}\"; ",
            "tail -n 30 /tmp/wend-upgrade.log; {stop}; exit 1; fi"
        ),
        stop = STOP_DB
    )
}
/// what adopt-rpmnew replaced and set-aside-unowned moved, kept for the admin
const SAVED: &str = "/var/lib/wend/saved";
/// service.record: the SysV services the box started at boot, one per line
const SYSV_RECORD: &str = "/var/lib/wend/sysv-enabled";
/// <hop>/<repo id>.repomd.xml: metadata an op fetched through repos the hop then drops
pub const SOURCES: &str = "/var/lib/wend/sources";
/// awk: x = everything after the second | (a vendor may contain |; names and NVRAs can't)
const VENDOR_LAST: &str = "{x = $0; sub(/^[^|]*[|][^|]*[|]/, \"\", x)}";
/// package file names (name-version-release.arch.rpm) to package names, one per line
const NAMES: &str = "awk '{sub(/-[^-]*-[^-]*$/, \"\"); print}'";

fn release_of<'a>(adapter: &'a Doc, release: &str) -> Result<&'a Value, String> {
    get(adapter, "releases")
        .and_then(|r| get(r, release))
        .ok_or(format!("the adapter has no release {release:?}"))
}

fn table<'a>(v: &'a Value, k: &str) -> Result<&'a Map<String, Value>, String> {
    get(v, k)
        .and_then(Value::as_object)
        .ok_or(format!("missing table {k:?}"))
}

fn first_repo(rel: &Value) -> Result<String, String> {
    table(rel, "repos")?
        .values()
        .next()
        .map(scalar)
        .ok_or("a release with no repos".into())
}

/// SPEC "Signed packages": the adapter's [keys], written there by every hop's {keys}.
const KEYS: &str = "/etc/wend/keys";

/// A repo section's signature lines: gpgcheck=1 against the named keys, or gpgcheck=0 when it names none.
fn gpg(names: &[String]) -> String {
    if names.is_empty() {
        return "gpgcheck=0\n".into();
    }
    let files: Vec<String> = names.iter().map(|k| format!("file://{KEYS}/{k}")).collect();
    format!("gpgcheck=1\ngpgkey={}\n", files.join(" "))
}

/// Shell that writes every key in the adapter's [keys] under KEYS ("true" if it has none).
fn write_keys(adapter: &Doc) -> String {
    match get(adapter, "keys").and_then(Value::as_object) {
        Some(ks) if !ks.is_empty() => std::iter::once(format!("mkdir -p {KEYS}"))
            .chain(ks.iter().map(|(n, a)| printf(&scalar(a), &format!("{KEYS}/{n}"))))
            .collect::<Vec<_>>()
            .join(" && "),
        _ => "true".into(),
    }
}

fn release_keys(rel: &Value) -> Vec<String> {
    get(rel, "gpgkeys").and_then(strings).unwrap_or_default()
}

fn repo_file(adapter: &Doc, release: &str, shim: &str) -> Result<String, String> {
    let rel = release_of(adapter, release)?;
    let g = gpg(&release_keys(rel));
    Ok(table(rel, "repos")?
        .iter()
        .map(|(n, u)| {
            let u = scalar(u);
            format!("[{release}-{n}]\nname={release}-{n}\nbaseurl={shim}/{u}/\n{g}enabled=1\n\n")
        })
        .collect())
}

/// SPEC "Signed packages": shell that checks files (RPMs fetched outside the package manager, or by one that can't
/// check them) against the release's keys and its add-ons', and refuses any not signed by them. gpgv checks the
/// signatures, not rpm: EL7's rpm 4.11 can't use signing subkeys, which AlmaLinux 8 signs with. A header+payload
/// signature (tag 1002) covers everything; a header-only one (268, newer EPEL builds) covers the header, whose sha256
/// payload digest (5092) then covers the payload. Its variables all start with _w, so it leaves the caller's alone.
/// @K@ is the scratch dir, @KEYFILES@ the key files, @FILES@ the RPMs. tools/backends.py builds the same text.
const CHECKSIG: &str = r#"rm -rf @K@; mkdir -m 700 -p @K@; _wg=$(command -v gpg2 || command -v gpg); _wv=$(command -v gpgv2 || command -v gpgv); [ -n "$_wg" ] && [ -n "$_wv" ] || { echo 'refusing: no gpg and gpgv to check signatures with'; exit 1; }; awk '/BEGIN PGP/ {n++} {print > ("@K@/key." n)}' @KEYFILES@; for _wk in @K@/key.*; do "$_wg" --homedir @K@ --batch --dearmor < $_wk >> @K@/ring || { echo "refusing: can't read key $_wk"; exit 1; }; done; _wbad=; for _wr in @FILES@; do _ws=$(od -An -v -tu1 -j96 -N65536 $_wr 2>/dev/null | awk 'function be(p) { return ((b[p] * 256 + b[p + 1]) * 256 + b[p + 2]) * 256 + b[p + 3] } { for (i = 1; i <= NF; i++) b[n++] = $i } END { if (n < 16 || b[0] != 142 || b[1] != 173 || b[2] != 232) exit; il = be(8); s = 16 + 16 * il; o = c = p = d = 0; for (e = 0; e < il; e++) { t = be(16 + 16 * e); if (t == 1002) { o = be(24 + 16 * e); c = be(28 + 16 * e) } if (t == 268) { p = be(24 + 16 * e); d = be(28 + 16 * e) } } x = 96 + s + be(12); x += (8 - x % 8) % 8; print 96 + s + o, c, 96 + s + p, d, x }'); _wok=; if [ -n "$_ws" ]; then set -- $_ws; if [ $2 -gt 0 ]; then tail -c +$(($1 + 1)) $_wr | head -c $2 > @K@/sig && tail -c +$(($5 + 1)) $_wr > @K@/data && "$_wv" --homedir @K@ --keyring @K@/ring @K@/sig @K@/data >/dev/null 2>&1 && _wok=1; elif [ $4 -gt 0 ]; then _wx=$5; tail -c +$(($3 + 1)) $_wr | head -c $4 > @K@/sig && _wl=$(od -An -v -tu1 -j$((_wx + 8)) -N8 $_wr | awk '{print 16 + 16 * ((($1 * 256 + $2) * 256 + $3) * 256 + $4) + (($5 * 256 + $6) * 256 + $7) * 256 + $8}') && tail -c +$((_wx + 1)) $_wr | head -c $_wl > @K@/hdr && "$_wv" --homedir @K@ --keyring @K@/ring @K@/sig @K@/hdr >/dev/null 2>&1 && _wd=$(od -An -v -tu1 @K@/hdr | awk 'function be(p) { return ((b[p] * 256 + b[p + 1]) * 256 + b[p + 2]) * 256 + b[p + 3] } { for (i = 1; i <= NF; i++) b[n++] = $i } END { if (n < 16 || b[0] != 142 || b[1] != 173 || b[2] != 232) exit; il = be(8); s = 16 + 16 * il; a = h = -1; for (e = 0; e < il; e++) { t = be(16 + 16 * e); if (t == 5093) a = be(s + be(24 + 16 * e)); if (t == 5092) h = s + be(24 + 16 * e) } if (a < 0 || h < 0) exit; for (q = h; q < n && b[q] != 0; q++) { v = b[q]; if (v < 48 || v > 102 || (v > 57 && v < 97)) exit; x = x sprintf("%c", v) } print a, x }') && [ "${_wd%% *}" = 8 ] && [ "$(tail -c +$((_wx + _wl + 1)) $_wr | sha256sum | cut -d' ' -f1)" = "${_wd#* }" ] && _wok=1; fi; fi; [ -n "$_wok" ] || _wbad="$_wbad $_wr"; done; rm -f @K@/data @K@/hdr; [ -z "$_wbad" ] || { echo "refusing, not signed by the release's keys:"; printf '%s\n' $_wbad | head -n 20; exit 1; }"#;

/// CHECKSIG for a release's keys and its add-ons'; None when the release names no keys.
fn checksig(adapter: &Doc, release: &str, k: &str, files: &str) -> Result<Option<String>, String> {
    let mut keys = release_keys(release_of(adapter, release)?);
    if keys.is_empty() {
        return Ok(None);
    }
    for a in get(adapter, "addons")
        .and_then(Value::as_object)
        .into_iter()
        .flatten()
        .map(|(_, a)| a)
    {
        if let Some(k) = get(a, "gpgkeys").and_then(|g| get(g, release)) {
            keys.push(scalar(k));
        }
    }
    let key_files: Vec<String> = keys.iter().map(|n| format!("{KEYS}/{n}")).collect();
    Ok(Some(
        CHECKSIG
            .replace("@KEYFILES@", &key_files.join(" "))
            .replace("@FILES@", files)
            .replace("@K@", k),
    ))
}

/// SPEC "Add-on repositories": shell that appends each add-on's repo for `release` to `file`, as
/// [<release>-<addon>], when the add-on's marker package is installed. Empty when no add-on has one.
fn addon_repos(adapter: &Doc, release: &str, shim: &str, file: &str) -> String {
    let mut out = vec![];
    for (n, a) in get(adapter, "addons").and_then(Value::as_object).into_iter().flatten() {
        if let Some(u) = get(a, "repos").and_then(|r| get(r, release)) {
            let rid = format!("{release}-{n}");
            let marker = get(a, "marker").map(scalar).unwrap_or_default();
            out.push(format!(
                "if rpm -q {marker} >/dev/null 2>&1; then printf '[{rid}]\\nname={rid}\\nbaseurl={shim}/{}/\\n\
                 {}enabled=1\\n\\n' >> {file}; fi",
                scalar(u),
                gpg(&get(a, "gpgkeys")
                    .and_then(|g| get(g, release))
                    .map(scalar)
                    .into_iter()
                    .collect::<Vec<_>>())
                .replace('\n', "\\n")
            ));
        }
    }
    // each `if` is true when its marker is absent
    out.join(" && ")
}

/// Shell that sets A to |vendor|vendor|... for every add-on whose marker package is installed.
fn addon_vendors(adapter: &Doc) -> String {
    let mut out = String::from("A='|'; ");
    for a in get(adapter, "addons")
        .and_then(Value::as_object)
        .into_iter()
        .flatten()
        .map(|(_, a)| a)
    {
        let (m, v) = (
            get(a, "marker").map(scalar).unwrap_or_default(),
            get(a, "vendor").map(scalar).unwrap_or_default(),
        );
        out.push_str(&format!("rpm -q {m} >/dev/null 2>&1 && A=\"${{A}}{v}|\"; "));
    }
    out
}

/// {repo id: repomd.xml URL} for every add-on repo a hop or rpm.transit uses (<release>-<addon>).
pub fn addon_repo_ids(adapter: &Doc) -> Vec<(String, String)> {
    let mut ids = vec![];
    for (n, a) in get(adapter, "addons").and_then(Value::as_object).into_iter().flatten() {
        for (rel, u) in get(a, "repos").and_then(Value::as_object).into_iter().flatten() {
            ids.push((
                format!("{rel}-{n}"),
                format!("https://{}/repodata/repomd.xml", scalar(u)),
            ));
        }
    }
    ids
}

/// Append the name of every package in a repo to out, from its primary metadata (gzip or xz, as EPEL 8 has it).
fn package_names(base: &str, out: &str) -> String {
    format!(
        "R=$(curl -sf {base}/repodata/repomd.xml | grep -o 'href=\"[^\"]*primary.xml[.a-z]*\"' | head -n1 | \
         sed 's/href=\"//;s/\"//') && test -n \"$R\" && curl -sf -o {out}.z {base}/$R && \
         case $R in *.xz) xz -dc < {out}.z > {out}.x;; *.gz) gzip -dc < {out}.z > {out}.x;; *) false;; esac && \
         grep -o '<name>[^<]*</name>' {out}.x | sed 's/<[^>]*>//g' > {out}.one && test -s {out}.one && \
         cat {out}.one >> {out} && rm -f {out}.z {out}.x {out}.one || \
         {{ echo 'no package list from {base}'; exit 1; }}"
    )
}

/// The file names of every package in a repo, from its primary metadata (works with CentOS 5's own tools).
fn primary_list(base: &str, out: &str) -> String {
    format!(
        "R=$(curl -sf {base}/repodata/repomd.xml | grep -o 'href=\"[^\"]*primary.xml.gz\"' | head -n1 | \
         sed 's/href=\"//;s/\"//') && test -n \"$R\" && curl -sf -o {out}.gz {base}/$R && \
         zcat {out}.gz | grep -o '<location href=\"Packages/[^\"]*\"' | sed 's/.*Packages\\///;s/\"//' > {out} && \
         rm -f {out}.gz && test -s {out} || {{ echo 'no package list from {base}'; exit 1; }}"
    )
}

fn pm_install(adapter: &Doc, release: &str, pkgs: &[String]) -> Result<String, String> {
    let v = hop_vars(adapter, release)?;
    if str_of(release_of(adapter, release)?, "pm")? == "yum" {
        return Ok(format!("yum -y install {}", pkgs.join(" ")));
    }
    Ok(format!("dnf -y {} install {}", var(&v, "dnf_opts"), pkgs.join(" ")))
}

fn rpm_step(s: &Value, adapter: &Doc, release: &str) -> Result<String, String> {
    let op = str_of(s, "op")?;
    let v = hop_vars(adapter, release)?;
    let major = var(&v, "major");
    Ok(match op {
        "install" => format!(
            "{CLEAN_REPOS}; {}",
            pm_install(adapter, release, &list_of(s, "packages")?)?
        ),
        // like apt, a package that isn't installed is not an error. rpm -e (not yum/dnf remove) so nothing that
        // depends on it is taken along: if something still needs it, the removal fails and the hop rolls back.
        "remove" => format!(
            "for p in {}; do ! rpm -q $p >/dev/null 2>&1 || rpm -e $p || exit 1; done",
            packages(s)?
        ),
        // rpm has no "-f install". Remove what is left with unmet dependencies from an older release.
        "repair" => [
            "for n in 1 2 3 4 5 6 7 8; do U=$(rpm -Va --nofiles --nodigest --noscripts 2>&1 | ",
            "grep -o 'Unsatisfied dependencies for [^: ]*' | awk '{print $4}' | ",
            &format!("grep -v '[.]el{major}' | sort -u); [ -z \"$U\" ] && break; echo removing $U; "),
            "rpm -e --nodeps --noscripts $U; done; test -z \"$(rpm -Va --nofiles --nodigest --noscripts 2>&1)\"",
        ]
        .concat(),
        "force-remove" => {
            let mut out = String::from("rpm -e ");
            for x in list_of(s, "force")? {
                out += match x.as_str() {
                    "depends" => "--nodeps",
                    "scripts" => "--noscripts",
                    x => return Err(format!("the rpm backend has no force flag {x:?}")),
                };
                out += " ";
            }
            out + &packages(s)?
        }
        "write-file" => printf(str_of(s, "content")?, str_of(s, "path")?),
        "rpm.swap-release" => swap_release(s, adapter, release, &v)?,
        "rpm.drop-arch" => drop_arch(s)?,
        "rpm.drop-unneeded" => format!(
            "P=; for p in {}; do ! rpm -q $p >/dev/null 2>&1 || P=\"$P $p\"; done; \
             [ -n \"$P\" ] || exit 0; if E=$(rpm -e --test $P 2>&1); then rpm -e $P; else \
             echo \"kept$P: still needed\"; echo \"$E\" | grep 'is needed by' | head -n 5; fi",
            list_of(s, "packages")?.join(" ")
        ),
        "rpm.bootstrap" => bootstrap(s, adapter, &v)?,
        "rpm.transit" => transit(s, adapter, &v)?,
        "rpm.adopt-rpmnew" => {
            let p = list_of(s, "paths")?.join(" ");
            [
                &format!("for f in {p}; do [ -f \"$f.rpmnew\" ] || continue; "),
                &format!("mkdir -p \"{SAVED}$(dirname \"$f\")\"; [ -e \"{SAVED}$f\" ] || cp -a \"$f\" \"{SAVED}$f\"; "),
                "mv -f \"$f.rpmnew\" \"$f\"; ",
                "echo adopted $f; done",
            ]
            .concat()
        }
        "rpm.set-aside-unowned" => {
            let p = list_of(s, "paths")?.join(" ");
            format!(
                "for f in {p}; do [ -f \"$f\" ] || continue; rpm -qf \"$f\" >/dev/null 2>&1 && \
                 continue; mkdir -p \"{SAVED}$(dirname \"$f\")\"; if [ -e \"{SAVED}$f\" ]; then rm -f \"$f\"; \
                 else mv -f \"$f\" \"{SAVED}$f\"; fi; echo set aside $f; done"
            )
        }
        "fs.layout" => fs_layout(str_of(s, "layout")?)?,
        "kv.set" => {
            let p = str_of(s, "path")?;
            let vals = table(s, "values")?;
            let mut keys: Vec<&String> = vals.keys().collect();
            keys.sort();
            let dels: Vec<String> = keys
                .iter()
                .map(|k| format!("-e '/^[[:space:]]*#*[[:space:]]*{k}[[:space:]]*=/d'"))
                .collect();
            let adds: String = keys
                .iter()
                .map(|k| format!("{k}={}\\n", scalar(&vals[k.as_str()])))
                .collect();
            format!("test -f {p} && sed -i {} {p} && printf '{adds}' >> {p}", dels.join(" "))
        }
        "kv.unset" => {
            let p = str_of(s, "path")?;
            let dels: Vec<String> = list_of(s, "keys")?
                .iter()
                .map(|k| format!("-e '/^[[:space:]]*{k}[[:space:]]*=/d'"))
                .collect();
            format!("test -f {p} && sed -i {} {p}", dels.join(" "))
        }
        // The plaintext comes from a file the box's owner puts in /etc/wend/secrets, never from the fix.
        "mysql.set-password" => {
            let (user, secret) = (str_of(s, "user")?, str_of(s, "secret")?);
            let sql = format!(
                "SET SESSION old_passwords=0; UPDATE mysql.user SET Password=PASSWORD('$P') WHERE User='{user}';"
            );
            format!(
                "P=$(cat /etc/wend/secrets/{secret} 2>/dev/null); case \"$P\" in ''|*[!A-Za-z0-9_.@%+=-]*) \
                 echo 'no usable /etc/wend/secrets/{secret}'; exit 1;; esac; {STOP_DB}; {DB_DAEMON}; {DB_DIRS}; \
                 ($D --user=mysql --skip-grant-tables --skip-networking \
                 >/tmp/wend-db.log 2>&1 &); {WAIT_SOCK}; C=$(command -v mariadb || command -v mysql); \
                 $C -uroot -e \"{sql}\"; rc=$?; {STOP_DB} && exit $rc"
            )
        }
        "mysql.clean-shutdown" => [
            &format!("{STOP_DB}; {DB_DAEMON}; {DB_DIRS}; ($D --user=mysql --skip-networking --innodb-fast-shutdown=0 "),
            ">/tmp/wend-db.log 2>&1 &); ",
            &format!("{WAIT_SOCK}; sleep 2; {STOP_DB}; rc=$?; tail -n 3 /tmp/wend-db.log; exit $rc"),
        ]
        .concat(),
        "mysql.upgrade-data" => format!(
            "{STOP_DB}; {DB_DAEMON}; {DB_DIRS}; ($D --user=mysql --skip-networking --skip-grant-tables >/tmp/wend-db.log 2>&1 &); \
             {WAIT_SOCK}; {}; {STOP_DB}; rc=$?; [ $rc = 0 ] || tail -n 15 /tmp/wend-db.log; exit $rc",
            upgrade_db()
        ),
        "ssh.generate-host-keys" => "ssh-keygen -A".into(),
        "httpd.drop-missing-modules" => HTTPD_DROP_MISSING_MODULES.into(),
        "php.mysqli-report-off" => php_mysqli_report_off(),
        "service.record" => service_record(),
        "net.ifcfg-to-keyfile" => {
            // SPEC.md, net.ifcfg-to-keyfile: the same converter source the Python engine sends
            format!(
                "python3 - <<'WEND_PY'\n{}WEND_PY",
                include_str!("../../tools/net_ifcfg_to_keyfile.py")
            )
        }
        "service.enable" => {
            let (like, u) = (list_of(s, "like")?.join(" "), str_of(s, "unit")?);
            [
                &format!("W=''; for n in {like}; do if grep -Fqx \"$n\" {SYSV_RECORD} 2>/dev/null || "),
                "[ \"$(systemctl is-enabled \"$n\" 2>/dev/null)\" = enabled ]; then W=$n; break; fi; done; ",
                &format!("[ -n \"$W\" ] || {{ echo 'none of {like} starts at boot: {u} left as is'; exit 0; }}; "),
                &format!("systemctl enable {u} || exit 1; S=$(systemctl is-enabled {u} 2>/dev/null); "),
                &format!(
                    "[ \"$S\" = enabled ] || {{ echo \"{u} is $S after systemctl enable, not enabled\"; exit 1; }}; "
                ),
                &format!("echo \"{u} enabled, like $W\""),
            ]
            .concat()
        }
        _ => return Err(format!("the rpm backend has no op {op:?}")),
    })
}

/// Replace the release package by hand. EL5's centos-release has epoch 10, so yum won't "upgrade" it to EL6's
/// epoch-0 one, and a vendor switch (CentOS -> AlmaLinux) isn't an upgrade at all. The package is fetched with
/// curl, so it is checked against the release's keys (when it names any) before the old one goes.
fn swap_release(s: &Value, adapter: &Doc, release: &str, v: &Vars) -> Result<String, String> {
    let rel = release_of(adapter, release)?;
    let base = format!("{}/{}", var(v, "shim"), first_repo(rel)?);
    let (install, remove) = (ere(str_of(s, "install")?), str_of(s, "remove")?);
    let check = checksig(adapter, release, "/tmp/wend-keydb", "/tmp/$f")?
        .map(|c| c + "; ")
        .unwrap_or_default();
    Ok(format!(
        "set -e; cd /tmp; {}; \
         f=$(grep -E '^{install}-[0-9][^/]*(x86_64|noarch)\\.rpm$' /tmp/wend-pkglist | head -n1); \
         test -n \"$f\"; curl -sf -o /tmp/$f {base}/Packages/$f; \
         {check}rpm -e --nodeps --noscripts {remove}; rpm -ivh --nodeps --noscripts /tmp/$f; \
         rm -rf /tmp/$f /tmp/wend-pkglist /tmp/wend-keydb; {CLEAN_REPOS}",
        primary_list(&base, "/tmp/wend-pkglist")
    ))
}

/// Remove every package of the given architectures (no dependency checks, no scriptlets), then fail if any is
/// left. Only multilib copies are disposable: refuse first if one of them has no twin of another arch.
fn drop_arch(s: &Value) -> Result<String, String> {
    let mut a = list_of(s, "arches")?;
    a.sort();
    let alt = a.join("|");
    Ok([
        &format!(
            "set -e; cd /tmp; for n in $(rpm -qa --qf '%{{NAME}} %{{ARCH}}\\n' | awk '$2 ~ /^({alt})$/ {{print $1}}' "
        ),
        &format!("| sort -u); do rpm -q --qf '%{{ARCH}}\\n' $n | grep -qvE '^({alt})$' || "),
        "{ echo \"refusing: $n is installed only as 32-bit, not a multilib copy\"; exit 1; }; done; ",
        "L=$(rpm -qa --qf '%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}\\n' | ",
        &format!("grep -E '\\.({alt})$' || true); [ -z \"$L\" ] || rpm -e --nodeps --noscripts $L; "),
        &format!("test -z \"$(rpm -qa --qf '%{{ARCH}}\\n' | grep -E '^({alt})$')\""),
    ]
    .concat())
}

/// EL5 -> EL6: rpm 4.4 can't read xz payloads and yum 3.2.22 can't check sha256 repodata. Unpack EL6's rpm/yum/
/// python stack over / with 7-Zip, then let the new rpm rebuild its database.
/// rpm.bootstrap's 7-Zip RPMs for a release, by their origin URL: (url, "sha256:..."), in the adapter's order.
pub fn bootstrap_pins(adapter: &Doc, release: &str) -> Result<Vec<(String, String)>, String> {
    let Some(boot) = get(release_of(adapter, release)?, "bootstrap") else {
        return Ok(vec![]);
    };
    let repo = str_of(boot, "p7zip_repo")?;
    Ok(table(boot, "p7zip")?
        .iter()
        .map(|(f, h)| (format!("https://{repo}/{f}"), scalar(h)))
        .collect())
}

fn bootstrap(s: &Value, adapter: &Doc, v: &Vars) -> Result<String, String> {
    let target = str_of(s, "release")?;
    let trel = release_of(adapter, target)?;
    let boot = get(trel, "bootstrap").ok_or(format!("{target} has no bootstrap table"))?;
    let (shim, b) = (var(v, "shim"), "/var/lib/wend/boot");
    let base = format!("{shim}/{}", first_repo(trel)?);
    let p7 = format!("{shim}/{}", str_of(boot, "p7zip_repo")?);
    let pins = table(boot, "p7zip")?;
    let p7zip = pins.keys().cloned().collect::<Vec<_>>().join(" ");
    let pkgs = list_of(boot, "packages")?.join(" ");
    let sources = format!("{SOURCES}/{}", var(v, "release"));
    Ok([
        format!("set -e; rm -rf {b}; mkdir -p {b}/x; cd {b}"),
        primary_list(&base, &format!("{b}/list")),
        format!(
            "for f in {p7zip}; do curl -sf -O {p7}/$f || curl -sf -O {p7}/Packages/$f || curl -sf -O {p7}/Packages/p/$f; done"
        ),
        // nothing vouches for these but the adapter's pins: refuse other bytes, and record them (Walk::sources)
        pins.iter()
            .map(|(f, h)| {
                let h = scalar(h);
                let h = h.strip_prefix("sha256:").unwrap_or(&h);
                format!("echo '{h}  {f}' | sha256sum -c - >/dev/null || {{ echo 'digest mismatch: {f}'; exit 1; }}")
            })
            .collect::<Vec<_>>()
            .join("; "),
        format!(
            "mkdir -p {sources}; printf '%s %s\\n' {} > {sources}/fetched",
            bootstrap_pins(adapter, target)?
                .iter()
                .map(|(u, h)| format!("{u} {h}"))
                .collect::<Vec<_>>()
                .join(" ")
        ),
        format!("rpm -Uvh --nosignature {p7zip}"),
        format!(
            "for n in {pkgs}; do f=$(grep -E \"^$n-[0-9][^-]*-[^-]*\\.(x86_64|noarch)\\.rpm$\" {b}/list | head -n1); \
             [ -n \"$f\" ] || {{ echo MISSING $n; continue; }}; curl -sf -O {base}/Packages/$f; done"
        ),
        format!(
            "for r in *.rpm; do case $r in p7zip*) continue;; esac; (cd x && 7z x -y ../$r >/dev/null) && \
             (cd / && cpio -idmu --quiet -f './etc/*' './usr/share/doc/*' './usr/share/man/*' < {b}/x/${{r%.rpm}}.cpio) \
             || {{ echo EXTRACTFAIL $r; exit 1; }}; rm -f x/*.cpio; done"
        ),
        "cd /; ldconfig; rpm --version; rm -f /var/lib/rpm/__db.*; rpm --rebuilddb".into(),
        "rpm -e p7zip p7zip-plugins".into(),
        format!("rm -rf {b}"),
    ]
    .join("\n"))
}

/// EL7 -> EL8: rpm 4.11 can't install packages with rich dependencies. Download the EL8 equivalent of every
/// installed package with a temporary EL7 dnf, unpack them into a staging dir, run EL8's own rpm through EL8's
/// ld.so over the whole system, then a real EL8 distro-sync, the packages it erased (`reinstall`), and the EL7
/// leftovers dropped from the database.
fn transit(s: &Value, adapter: &Doc, v: &Vars) -> Result<String, String> {
    let target = str_of(s, "release")?;
    let t = release_of(adapter, target)?;
    let boot = get(t, "transit").ok_or(format!("{target} has no transit table"))?;
    let (shim, b) = (var(v, "shim"), "/var/lib/wend/boot");
    let frm = release_of(adapter, str_of(boot, "from")?)?;
    // {**frm["repos"], **boot["extra_repos"]}: the old release's repos, then the extra ones; a repeated name keeps
    // its first place and takes the later value
    let mut repos: Map<String, Value> = table(frm, "repos")?.clone();
    for (k, x) in table(boot, "extra_repos")? {
        repos.insert(k.clone(), x.clone());
    }
    let old_repos: String = repos
        .iter()
        .map(|(n, u)| {
            format!(
                "[boot-{n}]\nname=boot-{n}\nbaseurl={shim}/{}/\ngpgcheck=0\nenabled=1\n\n",
                scalar(u)
            )
        })
        .collect::<String>()
        .replace('\n', "\\n");
    let new_repos = repo_file(adapter, target, shim)?.replace('\n', "\\n");
    let major = scalar(get(t, "major").ok_or("release without a major")?);
    let o = format!(
        "--setopt=reposdir={b}/new --releasever={major} --allowerasing --setopt=install_weak_deps=False \
         --setopt=protected_packages="
    );
    let ld = format!("{b}/stage/usr/lib64/ld-linux-x86-64.so.2 --library-path {b}/stage/usr/lib64");
    let rpm_s = format!("{b}/stage/usr/bin/rpm");
    let tag = format!("el{major}");
    let base = format!("{shim}/{}", first_repo(t)?);
    let reinstall = strings(get(s, "reinstall").ok_or("missing reinstall")?)
        .ok_or("reinstall must be a list")?
        .join(" ");
    let remove = str_of(s, "remove")?;
    let relpkg = str_of(boot, "release_package")?;
    let mut succ: Vec<String> = table(t, "repos")?
        .values()
        .map(|u| format!("{shim}/{}", scalar(u)))
        .collect();
    for a in get(adapter, "addons")
        .and_then(Value::as_object)
        .into_iter()
        .flatten()
        .map(|(_, a)| a)
    {
        if let Some(u) = get(a, "repos").and_then(|r| get(r, target)) {
            succ.push(format!("{shim}/{}", scalar(u)));
        }
    }
    let mut repo_lines =
        format!("printf '{old_repos}' > {b}/old/boot.repo; printf '{new_repos}' > {b}/new/ladder.repo");
    let x = addon_repos(adapter, target, shim, &format!("{b}/new/ladder.repo"));
    if !x.is_empty() {
        repo_lines.push_str(&format!("; {x}"));
    }
    Ok([
        [
            // Add-on repositories (SPEC "Add-on repositories") are the exception: their vendor's packages pass if
            // the add-on's marker is installed, and the next step requires a successor for each of them.
            &format!(
                "{}V=$(rpm -q --qf '%{{VENDOR}}' {remove}); T=$(rpm -qa --qf \
                 '%{{NAME}}|%{{NAME}}-%{{VERSION}}-%{{RELEASE}}.%{{ARCH}}|%{{VENDOR}}\\n' | awk -F'|' -v v=\"$V\" -v a=\"$A\" ",
                addon_vendors(adapter)
            ),
            &format!("'{VENDOR_LAST} $1 != \"gpg-pubkey\" && !index(a, \"|\" x \"|\") && (x != v || "),
            "$1 ~ /^(rh-|devtoolset-|centos-release-.)/ && $1 != \"centos-release-notes\") {print $2}'); [ -z \"$T\" ] || ",
            "{ printf 'refusing, not plain %s packages: ' \"$V\"; printf '%s ' $T; echo; exit 1; }",
        ]
        .concat(),
        format!("set -e; rm -rf {b}; mkdir -p {b}/old {b}/new {b}/pkgs {b}/stage"),
        // every add-on package needs a package of the same name in the release's repos or its add-on repos, or it
        // would be dropped from the database at the end: refuse before changing anything, naming them
        format!(
            "P=$(rpm -qa --qf '%{{NAME}}|%{{NAME}}|%{{VENDOR}}\\n' | awk -F'|' -v a=\"$A\" '{VENDOR_LAST} index(a, \"|\" x \"|\") \
             {{print $1}}' | \
             sort -u); if [ -n \"$P\" ]; then : > {b}/names; {}M=$(for n in $P; do grep -qxF \"$n\" {b}/names || echo $n; \
             done); [ -z \"$M\" ] || {{ echo \"refusing, no {target} package for these add-on packages (a fix must \
             remove or replace them first):\" $M; exit 1; }}; fi",
            succ.iter()
                .map(|u| format!("{}; ", package_names(u, &format!("{b}/names"))))
                .collect::<String>()
        ),
        repo_lines,
        format!("yum -y --setopt=reposdir={b}/old install dnf"),
        format!(
            "dnf -y {o} --setopt=best=False --setopt=deltarpm=false --nogpgcheck --downloadonly --destdir={b}/pkgs distro-sync \
             > {b}/dl.log 2>&1 || tail -n 20 {b}/dl.log"
        ),
        [
            &format!("cd {b}/pkgs; for n in rpm rpm-libs glibc dnf python3-dnf; do ls | grep -qE \"^$n-[0-9]\" || "),
            "{ echo \"not downloaded: $n\"; exit 1; }; done",
        ]
        .concat(),
        format!(
            "ls | {NAMES} > {b}/got; M=$(for n in $P; do grep -qxF \"$n\" {b}/got || echo $n; done); \
             [ -z \"$M\" ] || {{ echo \"not downloaded:\" $M; exit 1; }}"
        ),
        "rpm -e dnf; rm -f /var/lib/rpm/__db.*; rpm --rebuilddb".into(),
        format!(
            "ls | grep -qE '^{relpkg}-' || {{ {}; \
             f=$(grep -E '^{relpkg}-{major}[^/]*x86_64\\.rpm$' {b}/list | sort -V | tail -n1); \
             test -n \"$f\"; curl -sf -o {b}/pkgs/$f {base}/Packages/$f; }}",
            primary_list(&base, &format!("{b}/list"))
        ),
        format!(
            "{}for r in *.rpm; do rpm2cpio $r | (cd {b}/stage && cpio -idmu --quiet 2>/dev/null) || {{ echo EXTRACTFAIL $r; exit 1; }}; done",
            checksig(adapter, target, &format!("{b}/keydb"), "*.rpm")?.map(|c| c + "\n").unwrap_or_default()
        ),
        format!("{ld} {rpm_s} --version"),
        format!("rpm -e --nodeps --noscripts {remove}"),
        format!(
            "{ld} {rpm_s} -Uvh --nodeps --replacefiles --replacepkgs --oldpackage --nosignature --nodigest {b}/pkgs/*.rpm \
             > {b}/rpm.log 2>&1 || {{ grep -iE 'error|fail' {b}/rpm.log | head -n 20; }}"
        ),
        "/usr/sbin/ldconfig; /bin/sh -c true; rm -f /var/lib/rpm/__db.*; rpm --rebuilddb".into(),
        format!("grep -q 'release {major}[. ]' /etc/redhat-release; {CLEAN_REPOS}"),
        format!("dnf -y {o} --nobest distro-sync > {b}/sync.log 2>&1 || grep -iE 'error|conflict' {b}/sync.log | head -n 20"),
        concat!(
            "rpm -qa --qf '%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}\\n' | grep -E '^yum' | grep -E '\\.el7' | ",
            "xargs -r rpm -e --nodeps --noscripts"
        )
        .into(),
        format!(
            "mkdir -p {SAVED}/etc/yum; for d in pluginconf.d protected.d vars; do \
             if [ -d /etc/yum/$d ] && [ ! -L /etc/yum/$d ]; then mv /etc/yum/$d {SAVED}/etc/yum/; fi; done"
        ),
        format!("dnf -y {o} install yum {reinstall}"),
        [
            &format!(
                "L=$(rpm -qa --qf '%{{NAME}}-%{{VERSION}}-%{{RELEASE}}.%{{ARCH}}\\n' | grep -v '^gpg-pubkey' | grep -v '{tag}' || true); "
            ),
            "[ -z \"$L\" ] || { echo \"dropped from the rpm database (files kept): $L\"; ",
            "rpm -e --nodeps --noscripts --justdb $L; }",
        ]
        .concat(),
        // the hop's refresh cleans every cache: keep the transit repos' metadata so the hop pins it (Walk::sources)
        // (only its own repos: older releases' caches outlive their hops)
        format!(
            "S={SOURCES}/{}; rm -rf $S; mkdir -p $S; for f in /var/cache/dnf/*/repodata/repomd.xml \
             /var/cache/yum/*/*/*/repomd.xml; do [ -f \"$f\" ] || continue; d=${{f%/repomd.xml}}; d=${{d%/repodata}}; \
             d=${{d##*/}}; case $f in /var/cache/dnf/*) d=${{d%-*}};; esac; \
             case $d in boot-*|{target}-*) cp \"$f\" \"$S/$d.repomd.xml\";; esac; done",
            var(v, "release")
        ),
        format!("cd /; rm -rf {b}; {CLEAN_REPOS}"),
    ]
    .join("\n"))
}

fn fs_layout(layout: &str) -> Result<String, String> {
    match layout {
        // EL7's filesystem package needs /bin, /sbin, /lib, /lib64 to be symlinks into /usr. redhat-upgrade-tool
        // does this at boot; here it happens in place: copy what /usr lacks, then swap each dir for a symlink.
        "usrmove" => Ok([
            "set -e",
            concat!(
                "for d in bin sbin lib lib64; do [ -L /$d ] && continue; cd /$d; ",
                "find . -type d | while read x; do mkdir -p \"/usr/$d/$x\"; done; ",
                "find . ! -type d | while read f; do t=\"/usr/$d/$f\"; ",
                "if [ -L \"$t\" ]; then if [ \"$(readlink -f \"$t\")\" = \"$(readlink -f \"/$d/$f\")\" ]; then ",
                "rm -f \"$t\"; cp -a \"/$d/$f\" \"$t\"; fi; elif [ ! -e \"$t\" ]; then cp -a \"/$d/$f\" \"$t\"; fi; ",
                "done; done"
            ),
            concat!(
                "cd /; python -c \"import os; [ (os.rename('/'+d,'/'+d+'.usrmove'), os.symlink('usr/'+d,'/'+d)) ",
                "for d in ('bin','sbin','lib','lib64') if not os.path.islink('/'+d) ]\""
            ),
            "/bin/sh -c true; ldconfig; rpm -q glibc bash",
            "rm -rf /bin.usrmove /sbin.usrmove /lib.usrmove /lib64.usrmove",
        ]
        .join("\n")),
        // /var/run -> /run and /var/lock -> /run/lock, which EL7's filesystem package expects
        "run" => Ok(concat!(
            "mkdir -p /run && if [ -d /var/run ] && [ ! -L /var/run ]; then cp -a /var/run/. /run/ && ",
            "rm -rf /var/run && ln -s ../run /var/run; fi; if [ -d /var/lock ] && [ ! -L /var/lock ]; then ",
            "mkdir -p /run/lock && cp -a /var/lock/. /run/lock/ && rm -rf /var/lock && ln -s ../run/lock /var/lock; ",
            "fi; test -L /var/run"
        )
        .into()),
        l => Err(format!("fs.layout has no layout {l:?}")),
    }
}

/// Comment out every LoadModule whose module this httpd can't load any more: first the ones whose file is gone,
/// then whatever httpd -t still reports.
const HTTPD_DROP_MISSING_MODULES: &str = concat!(
    "cd /etc/httpd && for c in conf/*.conf conf.d/*.conf conf.modules.d/*.conf; do [ -f \"$c\" ] || continue; ",
    "for m in $(grep -E '^[[:space:]]*LoadModule' \"$c\" | awk '{print $3}'); do [ -e \"$m\" ] && continue; ",
    "echo disabling $m; sed -i \"s|^\\([[:space:]]*LoadModule[[:space:]].*[[:space:]]$m[[:space:]]*\\)$|#\\1|\" \"$c\"; ",
    "done; done; for n in $(seq 1 40); do out=$(/usr/sbin/httpd -t 2>&1) && break; ",
    "m=$(echo \"$out\" | grep -o 'Cannot load [^ ]*' | head -n1 | awk '{print $3}'); ",
    "[ -n \"$m\" ] || { echo \"$out\"; break; }; b=$(basename $m); echo disabling $b; ",
    "grep -rl \"LoadModule.*$b\" conf conf.d conf.modules.d 2>/dev/null | ",
    "xargs -r sed -i \"s|^\\([[:space:]]*LoadModule[[:space:]].*$b.*\\)$|#\\1|\"; done; /usr/sbin/httpd -t"
);

/// PHP 8.1 made mysqli throw exceptions where it used to warn and return false. Restore the old default unless
/// the owner already set an auto_prepend_file (read the way PHP reads it), in which case change nothing.
fn php_mysqli_report_off() -> String {
    let php = "<?php if (function_exists(\"mysqli_report\")) { mysqli_report(MYSQLI_REPORT_OFF); }\\n";
    let ini = "/etc/php.d/99-wend-mysqli-report-off.ini";
    [
        "V=$(awk 'FNR == 1 { s = 0 } /^[[:space:]]*\\[/ { s = tolower($0) ~ /^[[:space:]]*\\[(path|host)=/ } ",
        "!s && sub(/^[[:space:]]*auto_prepend_file[[:space:]]*=[[:space:]]*/, \"\") { v = $0 } END { print v }' ",
        "$([ -f /etc/php.ini ] && echo /etc/php.ini) $(LC_ALL=C ls /etc/php.d/*.ini 2>/dev/null | grep -vx ",
        ini,
        ") </dev/null 2>/dev/null | ",
        "sed 's/;.*//' | tr -d \"\\\"' \\t\\r\"); ",
        "case $(echo \"$V\" | tr A-Z a-z) in ''|none|off|no|false|null) ;; *) rm -f ",
        ini,
        "; ",
        "echo \"auto_prepend_file is already $V: left as is, mysqli_report not changed\"; exit 0;; esac; ",
        &format!("mkdir -p /usr/share/wend && printf '{php}' > /usr/share/wend/php-mysqli-report-off.php && "),
        "printf 'auto_prepend_file=/usr/share/wend/php-mysqli-report-off.php\\n' > ",
        ini,
    ]
    .concat()
}

/// Write down which SysV services start at boot, before the hop that brings systemd. The first record is kept.
fn service_record() -> String {
    let r = SYSV_RECORD;
    [
        &format!("if [ -e {r} ]; then echo \"kept the earlier record: $(tr '\\n' ' ' < {r})\"; exit 0; fi; "),
        // a systemd box (EL7 on) records its enabled service units instead, by unit name
        "if command -v systemctl >/dev/null 2>&1; then mkdir -p /var/lib/wend && systemctl list-unit-files ",
        "--type=service --state=enabled --no-legend | awk '{sub(/\\.service$/, \"\", $1); print $1}' | sort -u ",
        &format!("> {r}.new && test -s {r}.new && mv {r}.new {r} && echo \"systemd starts: $(tr '\\n' ' ' < {r})\"; "),
        &format!("R=$?; rm -f {r}.new; exit $R; fi; "),
        "L=$(sed -n 's/^id:\\([0-9]\\):initdefault:.*/\\1/p' /etc/inittab 2>/dev/null | head -n 1); ",
        "[ -n \"$L\" ] || L=3; [ -d /etc/rc.d/rc$L.d ] || { echo \"no /etc/rc.d/rc$L.d: no SysV runlevels to record\"; exit 1; }; ",
        &format!("mkdir -p /var/lib/wend && ls /etc/rc.d/rc$L.d | sed -n 's/^S[0-9][0-9]//p' | sort -u > {r}.new && "),
        &format!("mv {r}.new {r} && echo \"runlevel $L starts: $(tr '\\n' ' ' < {r})\""),
    ]
    .concat()
}

const ESM_PINS: &str = r#"for f in /etc/apt/preferences.d/ubuntu-esm-{release} /etc/apt/preferences.d/ubuntu-esm-*-{release}; do [ -e "$f" ] || [ -L "$f" ] || continue; if [ -L "$f" ] || [ ! -f "$f" ]; then echo "$f: not a plain file, left"; continue; fi; if dpkg -S "$f" >/dev/null 2>&1; then echo "$f: a package owns it, left"; continue; fi; if grep -qvxE '(Package: [*]|Pin: release o=UbuntuESM[A-Za-z]*, n={release}|Pin-Priority: never|#.*|[[:space:]]*)' "$f" || ! grep -qx 'Package: [*]' "$f" || ! grep -qxE 'Pin: release o=UbuntuESM[A-Za-z]*, n={release}' "$f" || ! grep -qx 'Pin-Priority: never' "$f"; then echo "$f: not an ubuntu-advantage-tools ESM pin, left"; continue; fi; rm -f "$f" || exit 1; echo "$f: ubuntu-advantage-tools' ESM pin for {release}, removed"; done"#;
