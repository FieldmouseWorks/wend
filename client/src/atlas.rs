//! The atlas (SPEC.md): every adapter, fix, route and run, read from a directory laid out like this repository.
//! Documents are kept as JSON values in file order, because file order means something in a few places (an
//! adapter's first repo is a release's base repo).

use crate::canon::content_id;
use serde_json::Value;
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

pub type Doc = Value;

pub struct Atlas {
    /// family -> adapter
    pub adapters: BTreeMap<String, Doc>,
    /// repo-relative path -> fix, sorted by path
    pub fixes: BTreeMap<String, Doc>,
    pub routes: BTreeMap<String, Doc>,
    pub runs: BTreeMap<String, Doc>,
    /// "family/release" -> that release's driver table (SPEC "Hardware preflight"), as bytes: the adapter pins it by
    /// sha256
    pub drivers: BTreeMap<String, Vec<u8>>,
    /// "family/release" -> that release's library table (SPEC "Runtime preflight"), as bytes: the adapter pins it by
    /// sha256
    pub sonames: BTreeMap<String, Vec<u8>>,
}

pub fn parse_toml(text: &str) -> Result<Doc, String> {
    if text.starts_with('\u{feff}') {
        return Err("starts with a byte order mark, which TOML doesn't allow".into());
    }
    toml::from_str::<Value>(text).map_err(|e| e.to_string())
}

fn files(dir: &Path, depth: usize, ext: &str, out: &mut Vec<PathBuf>) {
    let Ok(rd) = fs::read_dir(dir) else { return };
    for e in rd.flatten() {
        if e.file_name().to_string_lossy().starts_with('.') {
            continue; // as Python's glob: hidden files aren't part of the atlas
        }
        let p = e.path();
        if p.is_dir() {
            if depth > 0 {
                files(&p, depth - 1, ext, out);
            }
        } else if depth == 0 && p.extension().is_some_and(|x| x == ext) {
            out.push(p);
        }
    }
}

fn runs_under(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(rd) = fs::read_dir(dir) else { return };
    for e in rd.flatten() {
        if e.file_name().to_string_lossy().starts_with('.') {
            continue; // as Python's glob: hidden files aren't part of the atlas
        }
        let p = e.path();
        if p.is_dir() {
            runs_under(&p, out);
        } else if p.extension().is_some_and(|x| x == "json") {
            out.push(p);
        }
    }
}

impl Atlas {
    pub fn load(root: &Path) -> Result<Atlas, String> {
        let rel = |p: &Path| p.strip_prefix(root).unwrap_or(p).to_string_lossy().into_owned();
        let read = |p: &Path| fs::read_to_string(p).map_err(|e| format!("{}: {e}", p.display()));
        let mut a = Atlas {
            adapters: BTreeMap::new(),
            fixes: BTreeMap::new(),
            routes: BTreeMap::new(),
            runs: BTreeMap::new(),
            drivers: BTreeMap::new(),
            sonames: BTreeMap::new(),
        };
        let mut ps = vec![];
        files(&root.join("adapters"), 0, "toml", &mut ps);
        if ps.is_empty() {
            return Err(format!("{}: no adapters/ here; is this a wend atlas?", root.display()));
        }
        for p in ps {
            let fam = p.file_stem().unwrap().to_string_lossy().into_owned();
            a.adapters
                .insert(fam, parse_toml(&read(&p)?).map_err(|e| format!("{}: {e}", rel(&p)))?);
        }
        let mut ps = vec![];
        files(&root.join("adapters").join("drivers"), 1, "txt", &mut ps);
        for p in ps {
            let fam = p.parent().and_then(Path::file_name).unwrap().to_string_lossy();
            let rel_ = p.file_stem().unwrap().to_string_lossy();
            let bytes = fs::read(&p).map_err(|e| format!("{}: {e}", p.display()))?;
            a.drivers.insert(format!("{fam}/{rel_}"), bytes);
        }
        // library tables aren't in git: a release tarball carries them in adapters/sonames/, a checkout fetches them
        // into .wend/sonames/ (tools/sonames.py fetch); xz-compressed, checked against their pins where they're read
        let mut ps = vec![];
        files(&root.join("adapters").join("sonames"), 1, "xz", &mut ps);
        files(&root.join(".wend").join("sonames"), 1, "xz", &mut ps);
        for p in ps {
            let fam = p.parent().and_then(Path::file_name).unwrap().to_string_lossy();
            let name = p.file_name().unwrap().to_string_lossy();
            let Some(rel_) = name.strip_suffix(".txt.xz") else {
                continue;
            };
            let bytes = fs::read(&p).map_err(|e| format!("{}: {e}", p.display()))?;
            a.sonames.entry(format!("{fam}/{rel_}")).or_insert(bytes);
        }
        let mut ps = vec![];
        files(&root.join("fixes"), 2, "toml", &mut ps);
        for p in ps {
            a.fixes.insert(
                rel(&p),
                parse_toml(&read(&p)?).map_err(|e| format!("{}: {e}", rel(&p)))?,
            );
        }
        let mut ps = vec![];
        files(&root.join("routes"), 1, "toml", &mut ps);
        for p in ps {
            a.routes.insert(
                rel(&p),
                parse_toml(&read(&p)?).map_err(|e| format!("{}: {e}", rel(&p)))?,
            );
        }
        let mut ps = vec![];
        runs_under(&root.join("runs"), &mut ps);
        for p in ps {
            let v: Value = serde_json::from_str(&read(&p)?).map_err(|e| format!("{}: {e}", rel(&p)))?;
            a.runs.insert(rel(&p), v);
        }
        Ok(a)
    }

    /// Every fix by content id. Two files with the same content are an error for the checker; here the later
    /// path wins, as in tools/check.py.
    pub fn fixes_by_id(&self) -> BTreeMap<String, &Doc> {
        self.fixes.values().map(|d| (content_id(d), d)).collect()
    }

    pub fn route(&self, path_or_name: &str) -> Result<(&String, &Doc), String> {
        let p = path_or_name.trim_start_matches("./");
        self.routes
            .get_key_value(p)
            .or_else(|| self.routes.iter().find(|(k, _)| k.ends_with(&format!("/{p}.toml"))))
            .ok_or_else(|| format!("no route {path_or_name:?} (try routes/<family>/<from>-to-<to>.toml)"))
    }
}

// ---------- reading documents ----------

pub fn get<'a>(v: &'a Value, k: &str) -> Option<&'a Value> {
    v.as_object().and_then(|m| m.get(k))
}

pub fn str_of<'a>(v: &'a Value, k: &str) -> Result<&'a str, String> {
    get(v, k)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("missing string {k:?}"))
}

pub fn list_of(v: &Value, k: &str) -> Result<Vec<String>, String> {
    strings(get(v, k).ok_or_else(|| format!("missing list {k:?}"))?)
        .ok_or_else(|| format!("{k:?} must be a list of strings"))
}

pub fn strings(v: &Value) -> Option<Vec<String>> {
    v.as_array()?.iter().map(|x| x.as_str().map(String::from)).collect()
}

/// A scalar the way Python's str() shows it in a command (adapters hold release majors as integers).
pub fn scalar(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Null => "None".into(),
        v => v.to_string(),
    }
}
