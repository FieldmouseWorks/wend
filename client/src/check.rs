//! The schema check (SPEC.md): what makes a fix, a route or an adapter well formed, and what makes a value safe to
//! put in a root shell command. A port of tools/check.py's rules; `client/parity.py` runs the gate's own broken
//! cases (tools/test_check.py) through both and requires the same verdicts.
//!
//! The client runs this over the atlas before it touches a box, so a fix that would never pass CI's gate never
//! runs here either, whatever mirror or cache the atlas came from.

use crate::atlas::{get, strings, Atlas, Doc};
use crate::backend::caps;
use crate::canon::content_id;
use regex::Regex;
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::sync::Mutex;

/// a name family: no digits or dots, nothing from the first "-" on (SPEC "restores and adds")
pub const FAMILY: &str = r"[A-Za-z_+]+";

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
];

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
    let installs: BTreeSet<String> = steps
        .iter()
        .flat_map(|s| {
            let key = if get(s, "op").and_then(Value::as_str) == Some("install") {
                "packages"
            } else {
                "reinstall"
            };
            str_list(get(s, key))
        })
        .map(|p| name_family(&p))
        .collect();
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
