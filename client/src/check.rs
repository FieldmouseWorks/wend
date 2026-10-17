//! The schema check (SPEC.md): what makes a fix, a route or an adapter well formed, and what makes a value safe to
//! put in a root shell command. A port of tools/check.py's rules; `client/parity.py` runs the gate's own broken
//! cases (tools/test_check.py) through both and requires the same verdicts.
//!
//! The client runs this over the atlas before it touches a box, so a fix that would never pass CI's gate never
//! runs here either, whatever mirror or cache the atlas came from.

use crate::atlas::{get, strings, Atlas, Doc};
use crate::backend::caps;
use crate::canon::{content_id, sha256_hex};
use regex::Regex;
use serde_json::{Map, Value};
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::sync::Mutex;

/// a name family: no digits or dots, nothing from the first "-" on (SPEC "restores and adds")
pub const FAMILY: &str = r"[A-Za-z_+]+";

/// The families of the packages a fix's own steps install (install, or a transit's reinstall): what its adds may
/// name. The checker and the hop compiler both use it, so a direct walk holds a fix to the same rule.
pub fn installed_families(doc: &Value) -> BTreeSet<String> {
    ["pre", "post"]
        .iter()
        .flat_map(|ph| get(doc, ph).and_then(Value::as_array).cloned().unwrap_or_default())
        .flat_map(|s| {
            let key = if get(&s, "op").and_then(Value::as_str) == Some("install") {
                "packages"
            } else {
                "reinstall"
            };
            str_list(get(&s, key))
        })
        .map(|p| name_family(&p))
        .collect()
}

/// A package's name family (SPEC "restores and adds"): digits and dots removed, nothing from the first "-" on.
pub fn name_family(pkg: &str) -> String {
    let s: String = pkg.chars().filter(|c| !c.is_ascii_digit() && *c != '.').collect();
    s.split('-').next().unwrap_or_default().to_string()
}
/// rpm names have capitals (NetworkManager) and underscores
pub const PKG: &str = r"[A-Za-z0-9][A-Za-z0-9._+-]*";
const ETC_PATH: &str = r"/etc/[A-Za-z0-9_./-]+";
const MYSQL_KEY: &str = r"[a-z][a-z0-9-]*";
const MYSQL_VALUE: &str = r"[A-Za-z0-9_./:,@+-]*";
const ETC_GLOB: &str = r"/etc/[A-Za-z0-9_./*-]+";
pub const CORE_OPS: &[&str] = &["install", "remove", "repair", "force-remove", "write-file", "pm-option"];
/// Characters no adapter argument pattern may accept: values land in root shell commands.
const UNSAFE: &[&str] = &[
    "%", "'", "\"", " ", "\t", "\n", "$", "`", ";", "&", "|", "<", ">", "(", ")", "{", "}", "\\", "*", "?", "[", "~",
];
/// Whatever an adapter's patterns say, a value a fix passes to an adapter op is held to this too: no shell
/// metacharacters, no printf %, and no leading - (it would read as an option).
const SAFE_VALUE: &str = r"[A-Za-z0-9_.:/+=@][A-Za-z0-9_.:/+=@-]*";
const REPO_URL: &str = r"[A-Za-z0-9.-]+(/[A-Za-z0-9._-]+)+";
const APT_URL: &str = r"https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._-]+)*";
const ADAPTER_KEYS: &[&str] = &[
    "schema",
    "family",
    "backend",
    "release_file",
    "ladder",
    "may_skip",
    "harness_owned",
    "writable",
    "managed",
    "force",
    "releases",
    "mirror",
    "hop",
    "predicates",
    "pm_options",
    "box",
    "ops",
    "checks",
    "kv",
    "hardware",
    "addons",
    "runtimes",
    "relocate",
    "relocate_grub_legacy",
    "keys",
];
/// SPEC "Hardware preflight"
pub const CPU_LEVELS: &[&str] = &["x86-64", "x86-64-v2", "x86-64-v3"];
const HARDWARE_KEYS: &[&str] = &["cpu", "ram_mib", "disk_mib", "boot_mib", "drivers", "note"];
const OLD_KERNELS: &[&str] = &["running", "autoremove"];
/// SPEC "Runtime preflight"
pub const RUNTIMES: &[&str] = &["python2"];

/// Python's re.fullmatch. A pattern Rust's regex can't compile matches nothing (and an adapter carrying one is
/// reported by check_adapter).
pub fn full(pattern: &str, s: &str) -> bool {
    static CACHE: Mutex<Option<HashMap<String, Option<Regex>>>> = Mutex::new(None);
    let mut g = CACHE.lock().unwrap();
    let re = g
        .get_or_insert_with(HashMap::new)
        .entry(pattern.to_string())
        // the pattern must compile on its own first: `a)|(?:.*` would otherwise break out of the anchors
        .or_insert_with(|| {
            Regex::new(pattern)
                .ok()
                .and(Regex::new(&format!(r"\A(?:{pattern})\z")).ok())
        });
    re.as_ref().is_some_and(|r| r.is_match(s))
}

fn full_v(pattern: &str, v: &Value) -> bool {
    v.as_str().is_some_and(|s| full(pattern, s))
}

/// What Python's set() makes of a list of names or a table's keys. Never use it to decide which keys a table may
/// have: a list there is a type error, and `want`/`got` comparisons use table_keys.
fn keys(v: Option<&Value>) -> BTreeSet<String> {
    match v {
        Some(Value::Object(m)) => m.keys().cloned().collect(),
        Some(Value::Array(a)) => a.iter().filter_map(|x| x.as_str().map(String::from)).collect(),
        _ => BTreeSet::new(),
    }
}

fn table_keys(v: Option<&Value>) -> Option<BTreeSet<String>> {
    v.and_then(Value::as_object).map(|m| m.keys().cloned().collect())
}

/// Values in fixes and routes are only strings, booleans, integers, lists and tables (SPEC.md, "Identity"): no
/// floats (their canonical JSON isn't settled between implementations) and no TOML dates.
fn plain_values(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Number(n) => n.is_i64() || n.is_u64(),
        Value::Array(a) => a.iter().all(plain_values),
        Value::Object(m) => !m.contains_key("$__toml_private_datetime") && m.values().all(plain_values),
        _ => true,
    }
}

/// A field that is there must have its type: a list of tables, a list of strings, a table.
fn want_shape(doc: &Value, k: &str, shape: &str, wh: &str, errs: &mut Vec<String>) {
    let Some(v) = get(doc, k) else { return };
    let ok = match shape {
        "tables" => v.as_array().is_some_and(|a| a.iter().all(Value::is_object)),
        "strings" => v.as_array().is_some_and(|a| a.iter().all(Value::is_string)),
        "table" => v.is_object(),
        "string" => v.is_string(),
        _ => unreachable!(),
    };
    if !ok {
        let what = match shape {
            "tables" => "a list of tables",
            "strings" => "a list of strings",
            "table" => "a table",
            _ => "a string",
        };
        errs.push(format!("{wh}: {k} must be {what}"));
    }
}

/// Python's truthiness, for the few flags tools/check.py reads that way.
fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(m) => !m.is_empty(),
    }
}

fn str_list(v: Option<&Value>) -> Vec<String> {
    v.and_then(strings).unwrap_or_default()
}

fn sorted(s: &BTreeSet<String>) -> String {
    format!("{:?}", s.iter().collect::<Vec<_>>())
}

// ---------- adapters ----------

/// An adapter can narrow what fixes may do, never widen it past what its backend implements.
pub fn check_adapter(a: &Doc, wh: &str) -> Vec<String> {
    let mut errs = vec![];
    if get(a, "schema").and_then(Value::as_str) != Some("wend.adapter/0") {
        return vec![format!("{wh}: schema must be 'wend.adapter/0'")];
    }
    let extra: BTreeSet<String> = keys(Some(a))
        .into_iter()
        .filter(|k| !ADAPTER_KEYS.contains(&k.as_str()))
        .collect();
    if !extra.is_empty() {
        errs.push(format!("{wh}: unknown keys {}", sorted(&extra)));
    }
    for k in [
        "family",
        "backend",
        "release_file",
        "ladder",
        "force",
        "hop",
        "predicates",
        "checks",
    ] {
        if get(a, k).is_none() {
            errs.push(format!("{wh}: missing {k}"));
        }
    }
    for k in ["ladder", "may_skip", "harness_owned", "writable", "managed", "force"] {
        want_shape(a, k, "strings", wh, &mut errs);
    }
    for k in [
        "releases",
        "mirror",
        "hop",
        "predicates",
        "pm_options",
        "box",
        "ops",
        "checks",
        "kv",
        "addons",
    ] {
        want_shape(a, k, "table", wh, &mut errs);
    }
    let Some(b) = get(a, "backend").and_then(Value::as_str).and_then(caps) else {
        errs.push(format!("{wh}: backend must be one of ['apt', 'rpm']"));
        return errs;
    };
    let not_in = |k: &str, allowed: &[&str]| -> BTreeSet<String> {
        keys(get(a, k))
            .into_iter()
            .filter(|x| !allowed.contains(&x.as_str()))
            .collect()
    };
    if !not_in("force", b.force).is_empty() {
        errs.push(format!(
            "{wh}: force may only name {:?} on the {} backend",
            b.force, b.name
        ));
    }
    if !not_in("pm_options", b.pm_options).is_empty() {
        errs.push(format!(
            "{wh}: pm_options may only name {:?} on the {} backend",
            b.pm_options, b.name
        ));
    }
    let bad_ops = not_in("ops", b.ops);
    if !bad_ops.is_empty() {
        errs.push(format!("{wh}: the {} backend has no ops {}", b.name, sorted(&bad_ops)));
    }
    let ladder = str_list(get(a, "ladder"));
    let inner: Vec<&String> = if ladder.len() >= 2 {
        ladder[1..ladder.len() - 1].iter().collect()
    } else {
        vec![]
    };
    if !str_list(get(a, "may_skip")).iter().all(|x| inner.contains(&x)) {
        errs.push(format!("{wh}: may_skip names releases that aren't inside the ladder"));
    }
    if let Some(Value::Object(ops)) = get(a, "ops") {
        for (op, d) in ops {
            let Some(Value::Object(args)) = get(d, "args") else {
                errs.push(format!("{wh}: ops.{op} needs an args table (args = {{}} for none)"));
                continue;
            };
            for (name, spec) in args {
                if !spec.is_object() {
                    errs.push(format!("{wh}: ops.{op}.args.{name} must be a table"));
                    continue;
                }
                for kind in ["pattern", "list", "table", "path", "paths"] {
                    if get(spec, kind).is_some_and(|p| !p.is_string()) {
                        errs.push(format!("{wh}: ops.{op}.args.{name}.{kind} must be a string"));
                    }
                }
                if get(spec, "enum").is_some_and(|e| !e.as_array().is_some_and(|e| e.iter().all(Value::is_string))) {
                    errs.push(format!("{wh}: ops.{op}.args.{name}.enum must be a list of strings"));
                }
                if get(spec, "min").is_some_and(|m| !m.is_u64()) {
                    errs.push(format!("{wh}: ops.{op}.args.{name}.min must be a whole number"));
                }
                for kind in ["pattern", "list", "table"] {
                    if let Some(p) = get(spec, kind).and_then(Value::as_str) {
                        if Regex::new(p).is_err() {
                            errs.push(format!("{wh}: ops.{op}.args.{name}: {p:?} is not a regular expression"));
                        } else if UNSAFE.iter().any(|c| full(p, &format!("a{c}b")) || full(p, c)) {
                            errs.push(format!(
                                "{wh}: ops.{op}.args.{name}: {p:?} accepts shell metacharacters"
                            ));
                        }
                    }
                }
                if !["enum", "pattern", "list", "path", "paths", "table"]
                    .iter()
                    .any(|k| get(spec, k).is_some())
                {
                    errs.push(format!("{wh}: ops.{op}.args.{name} has no spec"));
                }
                for k in ["path", "paths"] {
                    if let Some(l) = get(spec, k) {
                        if !matches!(l.as_str(), Some("writable" | "managed" | "kv")) {
                            errs.push(format!(
                                "{wh}: ops.{op}.args.{name}: {k} must name writable, managed or kv"
                            ));
                        }
                    }
                }
            }
        }
    }
    // SPEC "Add-on repositories": their values land in root shell commands
    if get(a, "addons")
        .and_then(Value::as_object)
        .is_some_and(|x| !x.is_empty())
        && !get(a, "hop")
            .and_then(|h| get(h, "set_release"))
            .and_then(Value::as_str)
            .is_some_and(|s| s.contains("{addons}"))
    {
        errs.push(format!(
            "{wh}: an adapter with addons must put {{addons}} in hop.set_release"
        ));
    }
    if let Some(Value::Object(addons)) = get(a, "addons") {
        let rels = get(a, "releases");
        for (n, ad) in addons {
            let fields = ad
                .as_object()
                .map(|o| o.keys().map(String::as_str).collect::<BTreeSet<_>>());
            let repos_ok = get(ad, "repos").and_then(Value::as_object).is_some_and(|rs| {
                !rs.is_empty()
                    && rs.iter().all(|(r, u)| {
                        let rel = rels.and_then(|x| get(x, r));
                        rel.is_some()
                            && full_v(REPO_URL, u)
                            && !rel.and_then(|x| get(x, "repos")).is_some_and(|x| get(x, n).is_some())
                    })
            });
            let ok = b.name == "rpm"
                && full("[a-z0-9]+", n)
                && fields.as_ref().is_some_and(|f| {
                    BTreeSet::from(["repos", "vendor"]).is_subset(f)
                        && f.is_subset(&BTreeSet::from([
                            "gpgkeys",
                            "libs",
                            "marker",
                            "module_hotfixes",
                            "repos",
                            "vendor",
                        ]))
                })
                && get(ad, "marker").is_none_or(|m| full_v(PKG, m))
                && get(ad, "module_hotfixes").is_none_or(Value::is_boolean)
                && get(ad, "vendor").is_some_and(|v| full_v("[A-Za-z0-9][A-Za-z0-9 .,@<>-]*", v))
                && repos_ok;
            let libs_ok = match get(ad, "libs") {
                None => true,
                Some(Value::Object(l)) => l.iter().all(|(r, p)| {
                    get(ad, "repos").is_some_and(|rs| get(rs, r).is_some()) && full_v("sha256:[0-9a-f]{64}", p)
                }),
                Some(_) => false,
            };
            if !ok {
                errs.push(format!(
                    "{wh}: addons.{n} needs a plain name, a plain vendor, and repos (host/path) for releases \
                     the adapter has, on the rpm backend; optionally a marker package and module_hotfixes = true"
                ));
            } else if !libs_ok {
                errs.push(format!(
                    "{wh}: addons.{n}.libs must pin (sha256:<hex>) library tables for releases the add-on has a repo \
                     for"
                ));
            }
        }
    }
    // SPEC "Relocate": the harness's steps (lists of scripts) and the commands it runs between boots, in [relocate]
    // and in [relocate_grub_legacy] (GRUB legacy machines)
    for t in ["relocate", "relocate_grub_legacy"] {
        let Some(r) = get(a, t) else { continue };
        const STEPS: &[&str] = &[
            "preflight",
            "refresh",
            "place",
            "copy",
            "fixup",
            "flip",
            "release",
            "cleanup",
        ];
        const COMMANDS: &[&str] = &["booted", "arm", "commit_trial", "rollback_next", "rollback", "forward"];
        let want: BTreeSet<&str> = STEPS.iter().chain(COMMANDS).copied().collect();
        match r.as_object() {
            Some(o) if o.keys().map(String::as_str).collect::<BTreeSet<_>>() == want => {
                for k in STEPS {
                    if !o[*k]
                        .as_array()
                        .is_some_and(|l| !l.is_empty() && l.iter().all(Value::is_string))
                    {
                        errs.push(format!("{wh}: {t}.{k} must be a list of commands"));
                    }
                }
                for k in COMMANDS {
                    if !o[*k].is_string() {
                        errs.push(format!("{wh}: {t}.{k} must be a command"));
                    }
                }
            }
            _ => errs.push(format!("{wh}: {t} needs exactly {want:?}")),
        }
    }
    if let Some(Value::Object(kv)) = get(a, "kv") {
        for (p, d) in kv {
            if get(d, "set").is_some_and(|s| !s.is_boolean()) {
                errs.push(format!("{wh}: kv.{p:?}.set must be true or false"));
            }
            let ks = get(d, "keys").and_then(Value::as_array);
            let ok = full(ETC_PATH, p)
                && ks.is_some_and(|ks| !ks.is_empty() && ks.iter().all(|k| full_v("[A-Za-z0-9_]+", k)));
            if !ok {
                errs.push(format!("{wh}: kv.{p:?} needs a path under /etc and plain keys"));
            }
        }
    }
    // SPEC "Hardware preflight": the kernel a transit leaves in /boot
    for (r, rel) in get(a, "releases").and_then(Value::as_object).into_iter().flatten() {
        if let Some(t) = get(rel, "transit") {
            if !get(t, "boot_mib").and_then(Value::as_u64).is_some_and(|m| m > 0) {
                errs.push(format!(
                    "{wh}: releases.{r}.transit.boot_mib must be the MiB its kernel takes in /boot"
                ));
            }
        }
        // a release off the ladder that boxes start at, on the rung it joins: never a hop's target
        if let Some(j) = get(rel, "joins") {
            let on = j.as_str().and_then(|j| get(a, "releases").and_then(|rs| get(rs, j)));
            let ok = !ladder.contains(r)
                && j.as_str().is_some_and(|j| ladder.iter().any(|x| x == j))
                && on.is_some_and(|on| {
                    on.is_object() && get(on, "joins").is_none() && get(rel, "major") == get(on, "major")
                })
                && ["upgrade", "bootstrap", "transit", "reboot", "gpgkeys", "drop_keys"]
                    .iter()
                    .all(|k| get(rel, k).is_none());
            if !ok {
                errs.push(format!(
                    "{wh}: releases.{r}.joins must name a ladder release with the same major, from a release off the \
                     ladder with no upgrade, bootstrap, transit, reboot, gpgkeys or drop_keys"
                ));
            }
        }
    }
    // SPEC "Adapter format": the mirrors land in root shell commands as {mirror}
    if get(a, "backend").and_then(Value::as_str) == Some("apt") {
        let shim = get(a, "mirror")
            .and_then(|m| get(m, "shim"))
            .is_some_and(Value::is_string);
        for k in ["archive", "current"] {
            let u = get(a, "releases").and_then(|r| get(r, k));
            if !u.is_some_and(|u| full_v(APT_URL, u) || full_v(REPO_URL, u) && shim) {
                errs.push(format!(
                    "{wh}: releases.{k} must be an http(s) URL, or host/path through [mirror].shim"
                ));
            }
        }
    }
    errs.extend(check_hardware(a, wh));
    errs.extend(check_runtimes(a, wh));
    errs.extend(check_keys(a, wh));
    errs.extend(check_signed(a, wh));
    errs
}

/// An ASCII-armoured public key that is safe inside the harness's printf '...': printable ASCII and newlines, no
/// quote, % or backslash.
fn armor_ok(t: &Value) -> bool {
    t.as_str().is_some_and(|t| {
        t.starts_with("-----BEGIN PGP PUBLIC KEY BLOCK-----\n")
            && t.trim_end_matches('\n')
                .ends_with("\n-----END PGP PUBLIC KEY BLOCK-----")
            && t.chars().all(|c| c == '\n' || (' '..='~').contains(&c))
            && !t.contains(['\'', '%', '\\'])
    })
}

/// SPEC "Signed packages": [keys], releases.*.gpgkeys and addons.*.gpgkeys.
fn check_keys(a: &Doc, wh: &str) -> Vec<String> {
    let empty = Map::new();
    let keys = match get(a, "keys") {
        None => &empty,
        Some(Value::Object(k)) => k,
        Some(_) => {
            return vec![format!(
                "{wh}: keys must map plain names to ASCII-armoured public keys (no ', % or \\), on the rpm backend"
            )]
        }
    };
    if !keys.is_empty() && get(a, "backend").and_then(Value::as_str) != Some("rpm")
        || !keys
            .iter()
            .all(|(n, t)| full("[A-Za-z0-9][A-Za-z0-9.-]*", n) && armor_ok(t))
    {
        return vec![format!(
            "{wh}: keys must map plain names to ASCII-armoured public keys (no ', % or \\), on the rpm backend"
        )];
    }
    let mut errs = vec![];
    if !keys.is_empty()
        && !get(a, "hop")
            .and_then(|h| get(h, "set_release"))
            .and_then(Value::as_str)
            .is_some_and(|s| s.contains("{keys}"))
    {
        errs.push(format!(
            "{wh}: an adapter with keys must put {{keys}} in hop.set_release"
        ));
    }
    for (r, rel) in get(a, "releases").and_then(Value::as_object).into_iter().flatten() {
        let ok = match get(rel, "gpgkeys") {
            None => true,
            Some(Value::Array(g)) => g.iter().all(|k| k.as_str().is_some_and(|k| keys.contains_key(k))),
            Some(_) => false,
        };
        if !ok {
            errs.push(format!("{wh}: releases.{r}.gpgkeys must list names from [keys]"));
        }
        let ok = match get(rel, "drop_keys") {
            None => true,
            Some(Value::Array(d)) => d.iter().all(|k| k.as_str().is_some_and(|k| full("[0-9a-f]{8}", k))),
            Some(_) => false,
        };
        if !ok {
            errs.push(format!(
                "{wh}: releases.{r}.drop_keys must list short key ids (8 lowercase hex digits)"
            ));
        }
    }
    let drops = get(a, "releases")
        .and_then(Value::as_object)
        .into_iter()
        .flatten()
        .any(|(_, rel)| {
            get(rel, "drop_keys")
                .and_then(Value::as_array)
                .is_some_and(|d| !d.is_empty())
        });
    if drops
        && !get(a, "hop")
            .and_then(|h| get(h, "after"))
            .is_some_and(|x| x.to_string().contains("{drop_keys}"))
    {
        errs.push(format!(
            "{wh}: an adapter whose releases drop keys must put {{drop_keys}} in hop.after"
        ));
    }
    for (n, ad) in get(a, "addons").and_then(Value::as_object).into_iter().flatten() {
        let ok = match get(ad, "gpgkeys") {
            None => true,
            Some(Value::Object(g)) => g.iter().all(|(r, k)| {
                get(ad, "repos").and_then(|x| get(x, r)).is_some() && k.as_str().is_some_and(|k| keys.contains_key(k))
            }),
            Some(_) => false,
        };
        if !ok {
            errs.push(format!(
                "{wh}: addons.{n}.gpgkeys must map releases the add-on has a repo for to names from [keys]"
            ));
        }
    }
    errs
}

/// SPEC "Signed packages", apt: releases.*.signed is true or false, and an adapter with a signed release writes its
/// apt config per hop ({apt_conf} in hop.set_release) with the unsigned lines it drops (hop.apt_unsigned).
fn check_signed(a: &Doc, wh: &str) -> Vec<String> {
    let mut errs = vec![];
    let signed: Vec<(&String, &Value)> = get(a, "releases")
        .and_then(Value::as_object)
        .into_iter()
        .flatten()
        .filter_map(|(r, rel)| get(rel, "signed").map(|s| (r, s)))
        .collect();
    for (r, s) in &signed {
        if !s.is_boolean() || get(a, "backend").and_then(Value::as_str) != Some("apt") {
            errs.push(format!(
                "{wh}: releases.{r}.signed must be true or false, on the apt backend"
            ));
        }
    }
    let h = get(a, "hop");
    let text = |k: &str| h.and_then(|h| get(h, k));
    if !signed.is_empty()
        && !(text("set_release")
            .and_then(Value::as_str)
            .is_some_and(|s| s.contains("{apt_conf}"))
            && text("apt_unsigned").is_some_and(Value::is_string))
    {
        errs.push(format!(
            "{wh}: an adapter with signed releases must put {{apt_conf}} in hop.set_release and the lines unsigned \
             releases add in hop.apt_unsigned"
        ));
    }
    for k in ["apt_conf", "apt_unsigned"] {
        if let Some(v) = text(k) {
            if !v.as_str().is_some_and(|t| !t.contains(['\'', '%', '\\'])) {
                errs.push(format!(
                    "{wh}: hop.{k} must be text without ', % or \\ (it is written with printf)"
                ));
            }
        }
    }
    errs
}

/// The ladder release a machine that starts at `release` stands on: the release itself, or the one it `joins`
/// (SPEC "Adapter format"): Scientific Linux 7 is off the EL ladder and takes the routes from centos7.
pub fn rung<'a>(a: &'a Doc, release: &'a str) -> &'a str {
    get(a, "releases")
        .and_then(|rs| get(rs, release))
        .and_then(|r| get(r, "joins"))
        .and_then(Value::as_str)
        .unwrap_or(release)
}

/// What's left of `route` for a machine on `release` (SPEC "Route", joining partway): the route itself and `None` when
/// the machine starts at its `from` (or joins it there), the hops after that rung and `Some(rung)` when the machine
/// sits on one short of `to`, else nothing.
pub fn rest_of_route(a: &Doc, route: &Doc, release: &str) -> Option<(Doc, Option<String>)> {
    let rel = rung(a, release);
    if get(route, "from").and_then(Value::as_str) == Some(rel) {
        return Some((route.clone(), None));
    }
    let hops = get(route, "hops").and_then(strings)?;
    let i = hops.iter().position(|h| h == rel)?;
    if i + 1 == hops.len() {
        return None;
    }
    let mut rest = route.clone();
    rest["from"] = Value::String(rel.to_string());
    rest["hops"] = Value::from(hops[i + 1..].to_vec());
    Some((rest, Some(rel.to_string())))
}

/// Every release a route can boot a machine into: the ladder after its first release, less may_skip.
pub fn booted_releases(a: &Doc) -> Vec<String> {
    let skip = str_list(get(a, "may_skip"));
    str_list(get(a, "ladder"))
        .into_iter()
        .skip(1)
        .filter(|r| !skip.contains(r))
        .collect()
}

/// SPEC "Hardware preflight": what a machine needs before a route boots it into each release.
fn check_hardware(a: &Doc, wh: &str) -> Vec<String> {
    let Some(Value::Object(h)) = get(a, "hardware") else {
        return vec![format!("{wh}: missing the hardware table")];
    };
    let mut errs = vec![];
    let boot_ok = h.get("boot").and_then(Value::as_array).is_some_and(|b| {
        !b.is_empty()
            && b.iter()
                .all(|x| matches!(x.as_str(), Some("bios" | "uefi" | "uefi-secureboot")))
    });
    if !boot_ok {
        errs.push(format!(
            "{wh}: hardware.boot must list the firmware modes proven, from bios, uefi and uefi-secureboot"
        ));
    }
    if !h
        .get("old_kernels")
        .and_then(Value::as_str)
        .is_some_and(|k| OLD_KERNELS.contains(&k))
    {
        errs.push(format!("{wh}: hardware.old_kernels must be one of {OLD_KERNELS:?}"));
    }
    let ladder = str_list(get(a, "ladder"));
    if let Some(f) = h.get("autoremove_from") {
        let ok = h.get("old_kernels").and_then(Value::as_str) == Some("autoremove")
            && f.as_str().is_some_and(|f| ladder.iter().any(|r| r == f));
        if !ok {
            errs.push(format!(
                "{wh}: hardware.autoremove_from must name a release on the ladder, with old_kernels = \"autoremove\""
            ));
        }
    }
    for (r, t) in h {
        if r == "boot" || r == "old_kernels" || r == "autoremove_from" {
            continue;
        }
        let Some(tm) = t.as_object().filter(|_| ladder.contains(r)) else {
            errs.push(format!(
                "{wh}: hardware.{r} must be a table named for a release on the ladder"
            ));
            continue;
        };
        let extra: BTreeSet<String> = tm
            .keys()
            .filter(|k| !HARDWARE_KEYS.contains(&k.as_str()))
            .cloned()
            .collect();
        if !extra.is_empty() {
            errs.push(format!("{wh}: hardware.{r} has unknown keys {}", sorted(&extra)));
        }
        if !tm
            .get("cpu")
            .and_then(Value::as_str)
            .is_some_and(|c| CPU_LEVELS.contains(&c))
        {
            errs.push(format!("{wh}: hardware.{r}.cpu must be one of {CPU_LEVELS:?}"));
        }
        for k in ["ram_mib", "disk_mib", "boot_mib"] {
            // ram_mib is optional: some releases publish no minimum
            if (k != "ram_mib" || tm.contains_key(k)) && !tm.get(k).and_then(Value::as_i64).is_some_and(|n| n >= 1) {
                errs.push(format!("{wh}: hardware.{r}.{k} must be a whole number of MiB"));
            }
        }
        if !tm.get("drivers").is_some_and(|d| full_v("sha256:[0-9a-f]{64}", d)) {
            errs.push(format!(
                "{wh}: hardware.{r}.drivers must be the sha256 of its driver table"
            ));
        }
        if tm.get("note").is_some_and(|n| !n.is_string()) {
            errs.push(format!("{wh}: hardware.{r}.note must be a string"));
        }
    }
    let missing: Vec<String> = booted_releases(a).into_iter().filter(|r| !h.contains_key(r)).collect();
    if !missing.is_empty() {
        errs.push(format!(
            "{wh}: hardware has no table for {missing:?}, which a route can boot into"
        ));
    }
    errs
}

/// SPEC "Runtime preflight": the versions of each legacy runtime every booted release ships ([] for none).
fn check_runtimes(a: &Doc, wh: &str) -> Vec<String> {
    let Some(Value::Object(rt)) = get(a, "runtimes") else {
        return vec![format!("{wh}: missing the runtimes table")];
    };
    let mut errs = vec![];
    let ladder = str_list(get(a, "ladder"));
    for (r, t) in rt {
        let Some(tm) = t.as_object().filter(|_| ladder.contains(r)) else {
            errs.push(format!(
                "{wh}: runtimes.{r} must be a table named for a release on the ladder"
            ));
            continue;
        };
        for k in RUNTIMES {
            let ok = tm
                .get(*k)
                .and_then(Value::as_array)
                .is_some_and(|v| v.iter().all(|x| full_v(r"\d+\.\d+(\.\d+)?", x)));
            if !ok {
                errs.push(format!(
                    "{wh}: runtimes.{r}.{k} must list the versions the release ships ([] for none)"
                ));
            }
        }
        if !tm.get("libs").is_some_and(|d| full_v("sha256:[0-9a-f]{64}", d)) {
            errs.push(format!(
                "{wh}: runtimes.{r}.libs must be the sha256 of its library table"
            ));
        }
        let extra: BTreeSet<String> = tm
            .keys()
            .filter(|k| !RUNTIMES.contains(&k.as_str()) && k.as_str() != "libs")
            .cloned()
            .collect();
        if !extra.is_empty() {
            errs.push(format!("{wh}: runtimes.{r} has unknown keys {}", sorted(&extra)));
        }
    }
    let missing: Vec<String> = booted_releases(a).into_iter().filter(|r| !rt.contains_key(r)).collect();
    if !missing.is_empty() {
        errs.push(format!(
            "{wh}: runtimes has no table for {missing:?}, which a route can boot into"
        ));
    }
    errs
}

/// The library tables an adapter pins: `(table, field, pin)`, a release's own (`runtimes.<r>.libs`, table `<r>`) and
/// an add-on's for a release (`addons.<name>.libs.<r>`, table `<r>.<name>`).
pub fn library_pins(a: &Doc) -> Vec<(String, String, String)> {
    let mut out = vec![];
    for (r, t) in get(a, "runtimes").and_then(Value::as_object).into_iter().flatten() {
        if let Some(p) = get(t, "libs").and_then(Value::as_str) {
            out.push((r.clone(), format!("runtimes.{r}.libs"), p.to_string()));
        }
    }
    for (n, ad) in get(a, "addons").and_then(Value::as_object).into_iter().flatten() {
        for (r, p) in get(ad, "libs").and_then(Value::as_object).into_iter().flatten() {
            if let Some(p) = p.as_str() {
                out.push((format!("{r}.{n}"), format!("addons.{n}.libs.{r}"), p.to_string()));
            }
        }
    }
    out
}

/// Each library table that is in the atlas (adapters/sonames/ or .wend/sonames/<family>/<table>.txt.xz) is at the
/// pinned hash.
pub fn library_table_errors(atlas: &Atlas, fam: &str, a: &Doc, wh: &str) -> Vec<String> {
    let mut errs = vec![];
    for (table, field, want) in library_pins(a) {
        // not in git: a table that isn't here is fine (preflight says its check was skipped); one that is must match
        let Some(got) = atlas.sonames.get(&format!("{fam}/{table}")).map(|b| sha256_hex(b)) else {
            continue;
        };
        if format!("sha256:{got}") != want {
            errs.push(format!(
                "{wh}: {field}: the library table {fam}/{table}.txt.xz here has sha256 {}..., not the pinned one",
                &got[..12]
            ));
        }
    }
    errs
}

/// Each release's driver table is in the atlas (adapters/drivers/<family>/<release>.txt) at the pinned hash.
pub fn driver_table_errors(atlas: &Atlas, fam: &str, a: &Doc, wh: &str) -> Vec<String> {
    let mut errs = vec![];
    let Some(Value::Object(h)) = get(a, "hardware") else {
        return errs;
    };
    for (r, t) in h {
        let Some(want) = get(t, "drivers").and_then(Value::as_str) else {
            continue;
        };
        if r == "boot" || r == "old_kernels" || r == "autoremove_from" {
            continue;
        }
        let got = atlas.drivers.get(&format!("{fam}/{r}")).map(|b| sha256_hex(b));
        if got.as_deref().map(|g| format!("sha256:{g}")).as_deref() != Some(want) {
            errs.push(format!(
                "{wh}: hardware.{r}.drivers: adapters/drivers/{fam}/{r}.txt {}, not the pinned one",
                match got {
                    None => "is missing".to_string(),
                    Some(g) => format!("has sha256 {}...", &g[..12]),
                }
            ));
        }
    }
    errs
}

// ---------- fixes ----------

fn check_when(w: &Value, wh: &str, errs: &mut Vec<String>) {
    let Some(m) = w.as_object().filter(|m| m.len() == 1) else {
        return errs.push(format!("{wh}: a predicate is a table with exactly one key, got {w}"));
    };
    let (k, v) = m.iter().next().unwrap();
    match k.as_str() {
        "all" | "any" => match v.as_array() {
            Some(l) if !l.is_empty() => {
                for (i, x) in l.iter().enumerate() {
                    check_when(x, &format!("{wh}.{k}[{i}]"), errs);
                }
            }
            _ => errs.push(format!("{wh}: '{k}' needs a non-empty list")),
        },
        "not" => check_when(v, &format!("{wh}.not"), errs),
        "installed" => {
            if !full_v(PKG, v) {
                errs.push(format!("{wh}: bad package name {v}"));
            }
        }
        "file" => {
            if !(full_v(ETC_PATH, v) && !v.as_str().unwrap().contains("..")) {
                errs.push(format!("{wh}: 'file' must be a path under /etc, got {v}"));
            }
        }
        "file-contains" => {
            let ok = v.as_object().is_some_and(|m| {
                keys(Some(v)) == ["path", "text"].map(String::from).into()
                    && m.values().all(Value::is_string)
                    && full_v(ETC_PATH, &m["path"])
                    && !m["path"].as_str().unwrap().contains("..")
                    && !m["text"].as_str().unwrap().contains('\'')
            });
            if !ok {
                errs.push(format!(
                    "{wh}: 'file-contains' needs path (under /etc) and text (no single quotes)"
                ));
            }
        }
        "mysql-option" => {
            let ok = v.as_object().is_some_and(|m| {
                let ks = keys(Some(v));
                ks.contains("path")
                    && ks.contains("key")
                    && ks.iter().all(|k| ["path", "key", "value"].contains(&k.as_str()))
                    && m.values().all(Value::is_string)
                    && full_v(ETC_PATH, &m["path"])
                    && !m["path"].as_str().unwrap().contains("..")
                    && full_v(MYSQL_KEY, &m["key"])
                    && full(MYSQL_VALUE, m.get("value").and_then(Value::as_str).unwrap_or(""))
            });
            if !ok {
                errs.push(format!(
                    "{wh}: 'mysql-option' needs path (under /etc), key (lowercase, dashed) and optionally \
                     value (no quotes, backslashes or spaces)"
                ));
            }
        }
        _ => errs.push(format!("{wh}: unknown predicate {k:?}")),
    }
}

/// Packages the precondition requires to be installed, positively (not under a `not`).
pub fn installed_names(w: &Value) -> BTreeSet<String> {
    let Some((k, v)) = w.as_object().and_then(|m| m.iter().next()) else {
        return BTreeSet::new();
    };
    match k.as_str() {
        "installed" => v.as_str().map(|s| BTreeSet::from([s.to_string()])).unwrap_or_default(),
        "all" => v
            .as_array()
            .map(|l| l.iter().flat_map(installed_names).collect())
            .unwrap_or_default(),
        "any" => {
            let sets: Vec<BTreeSet<String>> = v
                .as_array()
                .map(|l| l.iter().map(installed_names).collect())
                .unwrap_or_default();
            if sets.is_empty() || sets.iter().any(BTreeSet::is_empty) {
                return BTreeSet::new();
            }
            sets.iter()
                .skip(1)
                .fold(sets[0].clone(), |acc, s| acc.intersection(s).cloned().collect())
        }
        _ => BTreeSet::new(),
    }
}

/// Python's fnmatch.fnmatchcase for the globs adapters use: `*` matches any run of characters (including `/`;
/// the caller limits that), `?` any one character, everything else itself.
fn fnmatch(name: &[char], pat: &[char]) -> bool {
    match pat.first() {
        None => name.is_empty(),
        Some('*') => (0..=name.len()).any(|i| fnmatch(&name[i..], &pat[1..])),
        Some('?') => !name.is_empty() && fnmatch(&name[1..], &pat[1..]),
        Some(c) => name.first() == Some(c) && fnmatch(&name[1..], &pat[1..]),
    }
}

/// On one of the adapter's allowlists: matches one of its globs, one directory level only. A glob in `path` (the
/// paths of rpm.adopt-rpmnew) must name a subset of an allowed glob, so `*` only matches `*` there.
pub fn writable(path: &str, adapter: &Doc, allow: &str) -> bool {
    let mut owned = str_list(get(adapter, "harness_owned"));
    owned.push("/etc/wend/".into()); // the box owner's secrets, on every family
    let pc: Vec<char> = path.chars().collect();
    !owned.iter().any(|p| path.starts_with(p.as_str()))
        // an allowlist is a list of globs, or a table keyed by them ([kv."<path>"])
        && keys(get(adapter, allow)).iter().any(|g| {
            let gc: Vec<char> = g.chars().collect();
            let dir_len = g.rsplit_once('/').map_or(g.as_str(), |(d, _)| d).chars().count();
            let rest: String = pc.iter().skip(dir_len + 1).collect();
            fnmatch(&pc, &gc) && !rest.contains('/')
        })
}

fn check_arg(name: &str, spec: &Value, val: &Value, adapter: &Doc, wh: &str, errs: &mut Vec<String>) {
    let family = get(adapter, "family").and_then(Value::as_str).unwrap_or("?");
    let vals: Vec<&Value> = match val {
        Value::Object(m) => m.values().collect(),
        Value::Array(a) => a.iter().collect(),
        v => vec![v],
    };
    let has = |k: &str| get(spec, k).is_some();
    let sp = |k: &str| get(spec, k).and_then(Value::as_str).unwrap_or("");
    if (has("pattern") || has("list") || has("table")) && !vals.iter().all(|x| full_v(SAFE_VALUE, x)) {
        errs.push(format!("{wh}: {name} may only hold plain values ({SAFE_VALUE})"));
    }
    if let Some(e) = get(spec, "enum") {
        if !e.as_array().is_some_and(|e| e.contains(val)) {
            errs.push(format!("{wh}: {name} must be one of {e}"));
        }
    }
    if has("pattern") && !full_v(sp("pattern"), val) {
        errs.push(format!("{wh}: {name}={val} does not match {}", sp("pattern")));
    }
    if has("list") {
        let min = get(spec, "min").and_then(Value::as_u64).unwrap_or(1) as usize;
        if !val
            .as_array()
            .is_some_and(|l| l.len() >= min && l.iter().all(|x| full_v(sp("list"), x)))
        {
            errs.push(format!(
                "{wh}: {name} must be a list of at least {min} strings matching {}",
                sp("list")
            ));
        }
    }
    if has("path") {
        let ok = val
            .as_str()
            .is_some_and(|p| full(ETC_PATH, p) && !p.contains("..") && writable(p, adapter, sp("path")));
        if !ok {
            errs.push(format!(
                "{wh}: {name}={val} is not on the {family} adapter's {} list",
                sp("path")
            ));
        }
    }
    if has("paths") {
        let ok = val.as_array().is_some_and(|l| {
            !l.is_empty()
                && l.iter().all(|x| {
                    x.as_str()
                        .is_some_and(|p| full(ETC_GLOB, p) && !p.contains("..") && writable(p, adapter, sp("paths")))
                })
        });
        if !ok {
            errs.push(format!(
                "{wh}: {name} must be a list of paths on the {family} adapter's {} list",
                sp("paths")
            ));
        }
    }
    if has("table") {
        let ok = val.as_object().is_some_and(|m| {
            !m.is_empty()
                && m.iter()
                    .all(|(k, v)| full("[A-Za-z0-9_]+", k) && full_v(sp("table"), v))
        });
        if !ok {
            errs.push(format!(
                "{wh}: {name} must be a table of key = \"value\" with values matching {}",
                sp("table")
            ));
        }
    }
}

fn check_step(s: &Value, adapter: &Doc, wh: &str, errs: &mut Vec<String>) {
    if !s.is_object() {
        return errs.push(format!("{wh}: a step is a table with an op"));
    }
    let family = get(adapter, "family").and_then(Value::as_str).unwrap_or("?");
    let adapter_ops = get(adapter, "ops").and_then(Value::as_object);
    let op = get(s, "op").and_then(Value::as_str).unwrap_or("");
    let aop = adapter_ops.and_then(|o| o.get(op));
    if !CORE_OPS.contains(&op) && aop.is_none() {
        return errs.push(format!(
            "{wh}: op {:?} is not in the core vocabulary or the {family} adapter",
            get(s, "op")
        ));
    }
    let want: BTreeSet<String> = match op {
        "install" => ["packages"].map(String::from).into(),
        "remove" => ["packages", "purge"].map(String::from).into(),
        "repair" => BTreeSet::new(),
        "force-remove" => ["packages", "force", "purge"].map(String::from).into(),
        "write-file" => ["path", "content"].map(String::from).into(),
        "pm-option" => ["option"].map(String::from).into(),
        _ => match table_keys(aop.and_then(|d| get(d, "args"))) {
            Some(k) => k,
            None => return errs.push(format!("{wh}: the {family} adapter's {op} has no args table")),
        },
    };
    let got: BTreeSet<String> = keys(Some(s)).into_iter().filter(|k| k != "op").collect();
    if got != want {
        return errs.push(format!(
            "{wh}: {op} takes exactly {}, got {}",
            sorted(&want),
            sorted(&got)
        ));
    }
    if want.contains("packages") {
        let ok = get(s, "packages")
            .and_then(Value::as_array)
            .is_some_and(|l| !l.is_empty() && l.iter().all(|p| full_v(PKG, p)));
        if !ok {
            errs.push(format!("{wh}: packages must be a non-empty list of package names"));
        }
    }
    let force = str_list(get(adapter, "force"));
    if op == "force-remove" {
        let ok = get(s, "force")
            .and_then(strings)
            .is_some_and(|l| l.iter().all(|x| force.contains(x)))
            && get(s, "force").is_some_and(|f| f.as_array().is_some_and(|a| a.iter().all(Value::is_string)));
        if !ok {
            errs.push(format!("{wh}: force may only use {force:?}"));
        }
    }
    if op == "write-file" {
        let path = get(s, "path").and_then(Value::as_str).unwrap_or("");
        let content = get(s, "content").and_then(Value::as_str);
        if !full(ETC_PATH, path) || path.contains("..") {
            errs.push(format!("{wh}: write-file only writes under /etc"));
        }
        if str_list(get(adapter, "harness_owned"))
            .iter()
            .any(|p| path.starts_with(p.as_str()))
        {
            errs.push(format!("{wh}: {path} is owned by the harness"));
        } else if !writable(path, adapter, "writable") {
            errs.push(format!("{wh}: {path} is not on the {family} adapter's writable list"));
        }
        if content.is_none_or(|c| c.contains('\'') || c.contains('%')) {
            errs.push(format!("{wh}: write-file content may not contain ' or %"));
        }
    }
    if op == "kv.set" || op == "kv.unset" {
        if let Some(path) = get(s, "path").and_then(Value::as_str) {
            let kv = get(adapter, "kv").and_then(|k| get(k, path));
            let ks: BTreeSet<String> = match (op, get(s, "values")) {
                ("kv.set", Some(v @ Value::Object(_))) => keys(Some(v)),
                _ => keys(get(s, "keys").filter(|k| k.is_array())),
            };
            match kv {
                None => errs.push(format!("{wh}: {path} is not on the {family} adapter's kv list")),
                Some(kv) => {
                    if op == "kv.set" && !get(kv, "set").and_then(Value::as_bool).unwrap_or(false) {
                        errs.push(format!("{wh}: {path} has sections; kv.set can't write it"));
                    }
                    let allowed = str_list(get(kv, "keys"));
                    let extra: BTreeSet<String> = ks.into_iter().filter(|k| !allowed.contains(k)).collect();
                    if !extra.is_empty() {
                        errs.push(format!(
                            "{wh}: {path} only allows keys {allowed:?}, not {}",
                            sorted(&extra)
                        ));
                    }
                }
            }
        }
    }
    if op == "pm-option" {
        let o = get(s, "option").and_then(Value::as_str).unwrap_or("");
        if get(adapter, "pm_options").and_then(|p| get(p, o)).is_none() {
            errs.push(format!("{wh}: unknown pm-option {o:?}"));
        }
    }
    if let Some(Value::Object(args)) = aop.and_then(|d| get(d, "args")) {
        for (a, spec) in args {
            if !spec.is_object() {
                errs.push(format!("{wh}: the {family} adapter's {op}.{a} has no spec"));
                continue;
            }
            check_arg(a, spec, &s[a.as_str()], adapter, wh, errs);
        }
    }
}

pub fn check_fix(doc: &Doc, adapters: &BTreeMap<String, Doc>, wh: &str) -> Vec<String> {
    let mut errs = vec![];
    if get(doc, "schema").and_then(Value::as_str) != Some("wend.fix/0") {
        return vec![format!("{wh}: schema must be 'wend.fix/0'")];
    }
    let allowed = [
        "schema", "family", "hop", "when", "pre", "post", "restores", "adds", "meta",
    ];
    let extra: BTreeSet<String> = keys(Some(doc))
        .into_iter()
        .filter(|k| !allowed.contains(&k.as_str()))
        .collect();
    if !extra.is_empty() {
        errs.push(format!("{wh}: unknown fields {}", sorted(&extra)));
    }
    if !plain_values(doc) {
        errs.push(format!(
            "{wh}: values are only strings, booleans, integers, lists and tables"
        ));
    }
    want_shape(doc, "pre", "tables", wh, &mut errs);
    want_shape(doc, "post", "tables", wh, &mut errs);
    want_shape(doc, "restores", "strings", wh, &mut errs);
    want_shape(doc, "adds", "strings", wh, &mut errs);
    want_shape(doc, "meta", "table", wh, &mut errs);
    let family = get(doc, "family").and_then(Value::as_str);
    let Some(a) = family.and_then(|f| adapters.get(f)) else {
        errs.push(format!("{wh}: unknown family {:?}", family));
        return errs;
    };
    let hop = get(doc, "hop").and_then(Value::as_str);
    let ladder = str_list(get(a, "ladder"));
    if !hop.is_some_and(|h| ladder.iter().skip(1).any(|x| x == h)) {
        errs.push(format!(
            "{wh}: hop {hop:?} is not a release on the {} ladder",
            family.unwrap()
        ));
    }
    match get(doc, "when") {
        None => errs.push(format!("{wh}: every fix needs a 'when'")),
        Some(w) => check_when(w, &format!("{wh}.when"), &mut errs),
    }
    let phase = |ph: &str| get(doc, ph).and_then(Value::as_array).cloned().unwrap_or_default();
    let steps: Vec<Value> = phase("pre").into_iter().chain(phase("post")).collect();
    if steps.is_empty() {
        errs.push(format!("{wh}: a fix with no steps does nothing"));
    }
    for ph in ["pre", "post"] {
        for (i, s) in phase(ph).iter().enumerate() {
            check_step(s, a, &format!("{wh}.{ph}[{i}]"), &mut errs);
            if ph == "post" && get(s, "op").and_then(Value::as_str) == Some("service.record") {
                // after the upgrade the SysV links are already gone, and the first record is the one that is kept
                errs.push(format!(
                    "{wh}.post[{i}]: service.record must run before the upgrade (pre)"
                ));
            }
        }
    }
    // The stray-MySQL rule: whatever a fix installs, it installs in place of something that is installed now.
    let installs = steps
        .iter()
        .any(|s| get(s, "op").and_then(Value::as_str) == Some("install") || get(s, "reinstall").is_some_and(truthy));
    if installs && errs.is_empty() && installed_names(&doc["when"]).is_empty() {
        errs.push(format!(
            "{wh}: a fix that installs packages must be gated on 'installed' of the package it replaces"
        ));
    }
    let checks = keys(get(a, "checks"));
    for c in str_list(get(doc, "restores")) {
        if !checks.contains(&c) && !full(r"box:[a-z0-9-]+", &c) {
            errs.push(format!(
                "{wh}: restores {c:?}, which is neither an adapter check nor box:<role>"
            ));
        }
    }
    // a fix may only declare what it installs itself, so review sees both together
    let installs = installed_families(doc);
    for f in get(doc, "adds").and_then(Value::as_array).into_iter().flatten() {
        match f.as_str() {
            Some(s) if full(FAMILY, s) => {
                if !installs.contains(s) {
                    errs.push(format!(
                        "{wh}: adds {f}, but none of the fix's own install steps installs a package of that family"
                    ));
                }
            }
            _ => errs.push(format!("{wh}: adds {f}, which is not a name family ({FAMILY})")),
        }
    }
    let name = get(doc, "meta").and_then(|m| get(m, "name")).and_then(Value::as_str);
    if !name.is_some_and(|n| full(r"[a-z0-9][a-z0-9.-]*", n)) {
        errs.push(format!("{wh}: meta.name must be a lowercase slug"));
    }
    let mut parts = wh.rsplit('/');
    let file = parts.next().unwrap_or("");
    let dir = parts.next().unwrap_or("");
    let stem: String = file.chars().take(file.chars().count().saturating_sub(5)).collect();
    if Some(stem.as_str()) != name || Some(dir) != hop {
        errs.push(format!("{wh}: file must live at fixes/<family>/<hop>/<meta.name>.toml"));
    }
    errs
}

// ---------- routes and runs ----------

pub fn route_hops(ladder: &[String], from: &str, to: &str) -> Option<Vec<String>> {
    let (f, t) = (
        ladder.iter().position(|x| x == from)?,
        ladder.iter().position(|x| x == to)?,
    );
    (f < t).then(|| ladder[f + 1..=t].to_vec())
}

pub fn check_route(
    doc: &Doc,
    fixes_by_id: &BTreeMap<String, &Doc>,
    adapters: &BTreeMap<String, Doc>,
    wh: &str,
) -> Vec<String> {
    let mut errs = vec![];
    if get(doc, "schema").and_then(Value::as_str) != Some("wend.route/0") {
        return vec![format!("{wh}: schema must be 'wend.route/0'")];
    }
    let Some(a) = get(doc, "family").and_then(Value::as_str).and_then(|f| adapters.get(f)) else {
        return vec![format!("{wh}: unknown family")];
    };
    if !plain_values(doc) {
        errs.push(format!(
            "{wh}: values are only strings, booleans, integers, lists and tables"
        ));
    }
    want_shape(doc, "finish", "table", wh, &mut errs);
    want_shape(doc, "fixes", "tables", wh, &mut errs);
    want_shape(doc, "meta", "table", wh, &mut errs);
    for (i, r) in get(doc, "fixes")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .enumerate()
    {
        if !(get(r, "id").is_some_and(Value::is_string) && get(r, "name").is_some_and(Value::is_string)) {
            errs.push(format!("{wh}: fixes[{i}] needs an id and a name"));
        }
    }
    let ladder = str_list(get(a, "ladder"));
    let (from, to) = (
        get(doc, "from").and_then(Value::as_str),
        get(doc, "to").and_then(Value::as_str),
    );
    let Some(hops) = from.zip(to).and_then(|(f, t)| route_hops(&ladder, f, t)) else {
        errs.push(format!("{wh}: from/to must be releases on the ladder, in order"));
        return errs;
    };
    // every release in between, in order. The adapter may let a route pass through a release in one hop.
    let skip = str_list(get(a, "may_skip"));
    let got = get(doc, "hops").and_then(strings);
    let ok = got.as_ref().is_some_and(|got| {
        !got.is_empty()
            && got.last().map(String::as_str) == to
            && hops.iter().filter(|h| got.contains(h)).cloned().collect::<Vec<_>>() == *got
            && !hops.iter().any(|h| !got.contains(h) && !skip.contains(h))
    });
    if !ok {
        let mut e = format!(
            "{wh}: hops must be every release from {} to {}: {hops:?}",
            from.unwrap(),
            to.unwrap()
        );
        if !skip.is_empty() {
            e += &format!(" (this adapter lets a route skip {skip:?})");
        }
        errs.push(e);
        return errs;
    }
    let got = got.unwrap();
    let prune = get(doc, "finish")
        .and_then(|f| get(f, "prune_obsolete"))
        .is_some_and(truthy);
    let hop_keys = keys(get(a, "hop"));
    if prune && !(hop_keys.contains("obsolete") && hop_keys.contains("autoremove")) {
        errs.push(format!(
            "{wh}: prune_obsolete needs hop.obsolete and hop.autoremove, which the {} adapter doesn't have",
            get(a, "family").and_then(Value::as_str).unwrap_or("?")
        ));
    }
    for r in get(doc, "fixes").and_then(Value::as_array).cloned().unwrap_or_default() {
        let id = get(&r, "id").and_then(Value::as_str).unwrap_or("");
        let name = get(&r, "name").and_then(Value::as_str);
        match fixes_by_id.get(id) {
            None => errs.push(format!(
                "{wh}: no fix with id {id} (named {name:?}); edited fixes get new ids"
            )),
            Some(f) => {
                let fname = get(f, "meta").and_then(|m| get(m, "name")).and_then(Value::as_str);
                let fhop = get(f, "hop").and_then(Value::as_str).unwrap_or("");
                if fname != name {
                    errs.push(format!("{wh}: {} is {fname:?}, not {name:?}", &id[..id.len().min(19)]));
                } else if !got.iter().any(|h| h == fhop) {
                    errs.push(format!(
                        "{wh}: {} applies on {fhop}, outside this route",
                        name.unwrap_or("")
                    ));
                }
            }
        }
    }
    errs
}

pub fn check_run(doc: &Doc, wh: &str) -> Vec<String> {
    let mut errs = vec![];
    if get(doc, "schema").and_then(Value::as_str) != Some("wend.run/0") {
        return vec![format!("{wh}: schema must be 'wend.run/0'")];
    }
    if !matches!(get(doc, "result").and_then(Value::as_str), Some("pass" | "fail")) {
        errs.push(format!("{wh}: result must be pass or fail"));
    }
    errs
}

/// On a passing run of a route, each pinned fix fired on exactly the boxes it claims and stayed off the rest.
pub fn decoy_errors(
    runs: &BTreeMap<String, Doc>,
    routes_by_id: &BTreeMap<String, &Doc>,
    fixes_by_id: &BTreeMap<String, &Doc>,
) -> Vec<String> {
    let mut errs = vec![];
    for (p, run) in runs {
        let Some(route) = get(run, "route")
            .and_then(Value::as_str)
            .and_then(|r| routes_by_id.get(r))
        else {
            continue;
        };
        if get(run, "result").and_then(Value::as_str) != Some("pass") {
            continue;
        }
        let fired: BTreeSet<String> = get(run, "hops")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .flat_map(|h| str_list(get(h, "fired")))
            .collect();
        let bx = get(run, "box").and_then(Value::as_str).unwrap_or("");
        for r in get(route, "fixes").and_then(Value::as_array).into_iter().flatten() {
            let id = get(r, "id").and_then(Value::as_str).unwrap_or("");
            let Some(f) = fixes_by_id.get(id) else {
                continue;
            };
            let claims = str_list(get(f, "meta").and_then(|m| get(m, "fires_on")))
                .iter()
                .any(|b| b == bx);
            if fired.contains(id) != claims {
                let name = get(r, "name").and_then(Value::as_str).unwrap_or("");
                errs.push(format!(
                    "{p}: {name} {}",
                    if claims {
                        "claims this box but did not fire"
                    } else {
                        "fired on a box it does not claim (decoy hit)"
                    }
                ));
            }
        }
    }
    errs
}

/// Everything in the atlas, as `wend check` and the client's own preflight run it.
pub fn check_atlas(atlas: &Atlas) -> Vec<String> {
    let mut errs = vec![];
    for (fam, a) in &atlas.adapters {
        errs.extend(check_adapter(a, &format!("adapters/{fam}.toml")));
        errs.extend(driver_table_errors(atlas, fam, a, &format!("adapters/{fam}.toml")));
        errs.extend(library_table_errors(atlas, fam, a, &format!("adapters/{fam}.toml")));
    }
    let mut seen: BTreeMap<String, &String> = BTreeMap::new();
    for (p, d) in &atlas.fixes {
        errs.extend(check_fix(d, &atlas.adapters, p));
        let id = content_id(d);
        if let Some(q) = seen.get(&id) {
            errs.push(format!("{p}: same content as {q}; one fix, one file"));
        }
        seen.insert(id, p);
    }
    let fixes_by_id = atlas.fixes_by_id();
    let mut routes_by_id = BTreeMap::new();
    for (p, d) in &atlas.routes {
        errs.extend(check_route(d, &fixes_by_id, &atlas.adapters, p));
        routes_by_id.insert(content_id(d), d);
    }
    for (p, d) in &atlas.runs {
        errs.extend(check_run(d, p));
    }
    errs.extend(decoy_errors(&atlas.runs, &routes_by_id, &fixes_by_id));
    errs
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::atlas::parse_toml;

    #[test]
    fn a_pattern_cannot_break_out_of_its_anchors() {
        assert!(full("[a-z]+", "abc"));
        assert!(!full("[a-z]+", "abc;"));
        assert!(!full("[a-z]+)|(?:.*", "x; rm -rf /"));
    }

    #[test]
    fn dates_and_floats_are_not_values() {
        let d = parse_toml("schema = \"wend.fix/0\"\nadds = [1979-05-27T07:32:00Z]\n").unwrap();
        assert!(!plain_values(&d));
        let f = parse_toml("adds = [1.5]\n").unwrap();
        assert!(!plain_values(&f));
        assert!(plain_values(&parse_toml("adds = [\"a\", 1, true]\n").unwrap()));
    }

    #[test]
    fn a_byte_order_mark_is_refused_like_tomllib() {
        assert!(parse_toml("\u{feff}a = 1\n").is_err());
    }
}
