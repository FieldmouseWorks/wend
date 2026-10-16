//! What the machine's own programs stand on that no package owns (SPEC "Runtime preflight"): Python 2 modules in the
//! system interpreter's module directories (`python2_modules`), and native programs and libraries with the sonames
//! they load (`native`). The route carries packages; these it doesn't, so preflight holds them to each release.

use crate::runtime::Owners;
use regex::Regex;
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet, HashSet};
use std::fs;
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;

/// Where `native` looks: the trees a package or a person installs programs into. `/bin`, `/sbin`, `/lib` and `/lib64`
/// only when they aren't links into `/usr` (they're read there).
const NATIVE_ROOTS: &[&str] = &["usr", "opt", "srv", "bin", "sbin", "lib", "lib64"];
/// Never read: kernel sources and build trees hold objects, not programs
const SKIP: &[&str] = &["usr/src"];
/// The system interpreter's module directories, `<base>/python2.N/<kind>`
const PY_BASES: &[&str] = &["usr/lib", "usr/lib64", "usr/local/lib", "usr/local/lib64"];
const PY_KINDS: &[&str] = &["site-packages", "dist-packages"];

/// A 64-bit ELF program or shared library: its soname, if it names one, and the sonames it loads.
#[derive(Debug, PartialEq)]
pub struct Dyn {
    pub soname: Option<String>,
    pub needed: Vec<String>,
}

/// Read a file's ELF header. None: not ELF. Some(None): ELF, but not a 64-bit little-endian program or shared
/// library (32-bit, a relocatable object). Some(Some(dyn)): one; a static program has no `needed`.
pub fn elf(path: &Path) -> Option<Option<Dyn>> {
    let mut f = fs::File::open(path).ok()?;
    let mut h = [0u8; 64];
    let n = f.read(&mut h).ok()?;
    if n < 4 || &h[..4] != b"\x7fELF" {
        return None;
    }
    if n < 64 || h[4] != 2 || h[5] != 1 {
        return Some(None);
    }
    let u16_ = |b: &[u8], o: usize| u16::from_le_bytes([b[o], b[o + 1]]) as u64;
    let u32_ = |b: &[u8], o: usize| u32::from_le_bytes(b[o..o + 4].try_into().unwrap()) as u64;
    let u64_ = |b: &[u8], o: usize| u64::from_le_bytes(b[o..o + 8].try_into().unwrap());
    let etype = u16_(&h, 16);
    // x86-64 only: an aarch64 sysroot or cross toolchain in /opt loads its own ld.so, which no x86-64 release ships
    if (etype != 2 && etype != 3) || u16_(&h, 18) != 62 {
        return Some(None);
    }
    let (phoff, phentsize, phnum) = (u64_(&h, 32), u16_(&h, 54), u16_(&h, 56));
    if phentsize < 56 || phnum > 512 {
        return Some(None);
    }
    let mut ph = vec![0u8; (phentsize * phnum) as usize];
    if f.seek(SeekFrom::Start(phoff)).is_err() || f.read_exact(&mut ph).is_err() {
        return Some(None);
    }
    let mut loads = vec![]; // (vaddr, filesz, offset)
    let mut dynamic = None; // (offset, filesz)
    for i in 0..phnum as usize {
        let p = &ph[i * phentsize as usize..];
        match u32_(p, 0) {
            1 => loads.push((u64_(p, 16), u64_(p, 32), u64_(p, 8))),
            2 => dynamic = Some((u64_(p, 8), u64_(p, 32))),
            _ => {}
        }
    }
    let Some((doff, dsz)) = dynamic else {
        return Some(Some(Dyn {
            soname: None,
            needed: vec![],
        })); // statically linked
    };
    let mut d = vec![0u8; dsz.min(1 << 20) as usize];
    if f.seek(SeekFrom::Start(doff)).is_err() || f.read_exact(&mut d).is_err() {
        return Some(None);
    }
    let (mut strtab, mut strsz, mut needed, mut soname) = (None, 0u64, vec![], None);
    for e in d.as_chunks::<16>().0 {
        let (tag, val) = (u64_(e, 0), u64_(e, 8));
        match tag {
            0 => break,
            1 => needed.push(val),
            5 => strtab = Some(val),
            10 => strsz = val,
            14 => soname = Some(val),
            _ => {}
        }
    }
    // the string table's address, mapped to where it is in the file through the loaded segment that holds it
    let Some(off) = strtab.and_then(|v| {
        loads
            .iter()
            .find(|(va, sz, _)| v.checked_sub(*va).is_some_and(|d| d < *sz))
            .and_then(|(va, _, o)| o.checked_add(v - va))
    }) else {
        return Some(None);
    };
    let mut s = vec![0u8; strsz.min(16 << 20) as usize];
    if f.seek(SeekFrom::Start(off)).is_err() || f.read_exact(&mut s).is_err() {
        return Some(None);
    }
    let at = |i: u64| -> Option<String> {
        let rest = s.get(i as usize..)?;
        let end = rest.iter().position(|b| *b == 0)?;
        let name = String::from_utf8_lossy(&rest[..end]).into_owned();
        // a path in DT_NEEDED is loaded by that path; its file name is what a release ships
        Some(name.rsplit('/').next().unwrap_or(&name).to_string())
    };
    Some(Some(Dyn {
        soname: soname.and_then(at),
        needed: needed.into_iter().filter_map(at).collect(),
    }))
}

fn is_elf(path: &Path) -> bool {
    let mut b = [0u8; 4];
    fs::File::open(path).and_then(|mut f| f.read_exact(&mut b)).is_ok() && &b == b"\x7fELF"
}

/// Every regular file under `dir` (inside `root`), as a path from the machine's `/`, links not followed.
fn walk(root: &Path, dir: &str, out: &mut Vec<String>) {
    if SKIP.contains(&dir.trim_start_matches('/')) {
        return;
    }
    for e in fs::read_dir(root.join(dir.trim_start_matches('/')))
        .into_iter()
        .flatten()
        .flatten()
    {
        let Ok(t) = e.file_type() else { continue };
        let p = format!("{}/{}", dir.trim_end_matches('/'), e.file_name().to_string_lossy());
        if t.is_dir() {
            walk(root, &p, out);
        } else if t.is_file() {
            out.push(p);
        }
    }
}

/// The tree a file belongs to in `native`: `/opt/<name>`, `/srv/<name>`, `/usr/local`, a Python module directory, or
/// its own directory.
fn tree_of(p: &str) -> String {
    let parts: Vec<&str> = p.split('/').collect(); // ["", "opt", "x", ...]
    if parts.len() > 3 && (parts[1] == "opt" || parts[1] == "srv") {
        return parts[..3].join("/");
    }
    if parts.len() > 3 && parts[1] == "usr" && parts[2] == "local" {
        return "/usr/local".into();
    }
    if let Some(i) = parts.iter().position(|c| PY_KINDS.contains(c)) {
        return parts[..=i].join("/");
    }
    p.rsplit_once('/').map_or("/".into(), |(d, _)| d.to_string())
}

/// The trees `native` reads, as paths from `/`: a top-level link (`/lib` -> `usr/lib`) is read where it points.
fn native_roots(root: &Path) -> Vec<String> {
    NATIVE_ROOTS
        .iter()
        .filter(|r| fs::symlink_metadata(root.join(r)).map(|m| m.is_dir()).unwrap_or(false))
        .map(|r| format!("/{r}"))
        .collect()
}

/// `native`: every unowned 64-bit program and shared library, grouped by tree, with what each tree loads that no
/// unowned file provides.
pub fn native(root: &Path, owned: &HashSet<String>) -> Vec<Value> {
    let mut files = vec![];
    for r in native_roots(root) {
        walk(root, &r, &mut files);
    }
    files.sort();
    let linker = linker_dirs(root);
    // a library in a directory the loader searches serves every tree; one anywhere else, only its own tree (a
    // vendor's private libssl in /opt/a/lib doesn't keep /opt/b's programs running when the distro's goes)
    let mut everyone = BTreeSet::new();
    // per tree: its files, what they load, what they provide
    type Tree = (Vec<String>, BTreeSet<String>, BTreeSet<String>);
    let mut trees: BTreeMap<String, Tree> = BTreeMap::new();
    for p in files.iter().filter(|p| !owned.contains(p.as_str())) {
        let Some(Some(d)) = elf(&root.join(p.trim_start_matches('/'))) else {
            continue;
        };
        let (dir, name) = p.rsplit_once('/').unwrap_or(("", p));
        let soname = d.soname.clone().unwrap_or_else(|| name.to_string());
        let t = trees.entry(tree_of(p)).or_default();
        if linker.contains(if dir.is_empty() { "/" } else { dir }) {
            everyone.insert(soname.clone());
        }
        t.0.push(p.clone());
        t.1.extend(d.needed);
        t.2.insert(soname);
    }
    trees
        .into_iter()
        .map(|(tree, (fs_, needs, own))| {
            let needs: Vec<&String> = needs
                .iter()
                .filter(|n| !own.contains(*n) && !everyone.contains(*n))
                .collect();
            json!({"tree": tree, "files": fs_.len(), "sample": fs_.iter().take(3).collect::<Vec<_>>(), "needs": needs})
        })
        .collect()
}

/// The directories the loader searches: the distro's own, `/usr/local/lib*`, and whatever `/etc/ld.so.conf` (and the
/// files it includes) adds, as paths from `/`.
fn linker_dirs(root: &Path) -> HashSet<String> {
    let mut dirs: HashSet<String> = [
        "/lib",
        "/lib64",
        "/usr/lib",
        "/usr/lib64",
        "/usr/local/lib",
        "/usr/local/lib64",
    ]
    .iter()
    .flat_map(|d| [d.to_string(), format!("{d}/x86_64-linux-gnu")])
    .collect();
    let mut todo = vec!["/etc/ld.so.conf".to_string()];
    let mut seen = HashSet::new();
    while let Some(f) = todo.pop() {
        if !seen.insert(f.clone()) || seen.len() > 256 {
            continue;
        }
        let Ok(text) = fs::read_to_string(root.join(f.trim_start_matches('/'))) else {
            continue;
        };
        for line in text.lines().map(|l| l.split('#').next().unwrap_or("").trim()) {
            if let Some(pat) = line.strip_prefix("include").filter(|r| r.starts_with([' ', '\t'])) {
                for pat in pat.split_whitespace() {
                    let pat = if pat.starts_with('/') {
                        pat.to_string()
                    } else {
                        format!("/etc/{pat}")
                    };
                    todo.extend(expand(root, &pat));
                }
            } else if line.starts_with('/') {
                // `dir:type` and `dir=type` are libc5 forms; the directory is what's before them
                let d = line.split([':', '=', ' ', '\t']).next().unwrap_or(line);
                dirs.insert(d.trim_end_matches('/').to_string());
            }
        }
    }
    dirs
}

/// `/etc/ld.so.conf.d/*.conf`, the one wildcard form ld.so.conf uses: a `*` in the last part of the path.
fn expand(root: &Path, pat: &str) -> Vec<String> {
    let Some((dir, last)) = pat.rsplit_once('/') else {
        return vec![];
    };
    let Some((pre, post)) = last.split_once('*') else {
        return vec![pat.to_string()];
    };
    let mut out: Vec<String> = fs::read_dir(root.join(dir.trim_start_matches('/')))
        .into_iter()
        .flatten()
        .flatten()
        .map(|e| e.file_name().to_string_lossy().into_owned())
        .filter(|n| n.len() >= pre.len() + post.len() && n.starts_with(pre) && n.ends_with(post))
        .map(|n| format!("{dir}/{n}"))
        .collect();
    out.sort();
    out
}

/// `python2_modules`: per module directory of the system Python 2, what no package owns in it.
pub fn python2_modules(root: &Path, owned: &HashSet<String>) -> Vec<Value> {
    let ver = Regex::new(r"^python(2\.\d+)$").unwrap();
    let mut seen = HashSet::new(); // /usr/local/lib64 may be a link to /usr/local/lib
    let mut out = vec![];
    for base in PY_BASES {
        let mut dirs: Vec<(String, String)> = fs::read_dir(root.join(base))
            .into_iter()
            .flatten()
            .flatten()
            .filter_map(|e| {
                let n = e.file_name().to_string_lossy().into_owned();
                ver.captures(&n).map(|c| (c[1].to_string(), n.clone()))
            })
            .collect();
        dirs.sort();
        for (v, n) in dirs {
            for kind in PY_KINDS {
                let dir = format!("/{base}/{n}/{kind}");
                let Ok(real) = fs::canonicalize(root.join(dir.trim_start_matches('/'))) else {
                    continue;
                };
                if !seen.insert(real) {
                    continue;
                }
                let mut fs_ = vec![];
                walk(root, &dir, &mut fs_);
                // bytecode next to its source is a cache (dh_python2 and pycentral write it after dpkg's lists); the
                // source may be a link into /usr/share/pyshared or pycentral, so any file type counts
                let source =
                    |p: &str| fs::symlink_metadata(root.join(p[..p.len() - 1].trim_start_matches('/'))).is_ok();
                let mine: Vec<&String> = fs_
                    .iter()
                    .filter(|p| !owned.contains(p.as_str()))
                    .filter(|p| !(p.ends_with(".pyc") || p.ends_with(".pyo")) || !source(p))
                    .collect();
                let compiled = mine
                    .iter()
                    .filter(|p| is_elf(&root.join(p.trim_start_matches('/'))))
                    .count();
                let modules: BTreeSet<String> = mine
                    .iter()
                    .filter_map(|p| module_name(p[dir.len() + 1..].split('/').next()?))
                    .collect();
                if modules.is_empty() {
                    continue;
                }
                out.push(
                    json!({"version": v, "dir": dir, "files": mine.len(), "compiled": compiled,
                                "modules": modules}),
                );
            }
        }
    }
    out
}

/// The importable name a top-level entry of a module directory stands for: `django/` and `six.py` are `django` and
/// `six`, `lxml-2.3-py2.6-linux-x86_64.egg/` is `lxml`, `_sqlite.so` is `_sqlite`; metadata (`.egg-info`, `.pth`)
/// isn't a module.
fn module_name(entry: &str) -> Option<String> {
    for meta in [".egg-info", ".dist-info", ".pth", ".txt", ".egg-link"] {
        if entry.ends_with(meta) {
            return None;
        }
    }
    let e = entry
        .strip_suffix(".egg")
        .map_or(entry, |e| e.split('-').next().unwrap_or(e));
    let e = ["module.so", ".so", ".pyc", ".pyo", ".py"]
        .iter()
        .find_map(|s| e.strip_suffix(s))
        .unwrap_or(e);
    (!e.is_empty()).then(|| e.to_string())
}

/// Facts read from a file are held to the shape these write.
pub fn check(modules: &Value, native: &Value) -> bool {
    let strs = |v: &Value| v.as_array().is_some_and(|a| a.iter().all(Value::is_string));
    modules.as_array().is_some_and(|a| {
        a.iter().all(|m| {
            m["version"].is_string()
                && m["dir"].is_string()
                && m["files"].is_u64()
                && m["compiled"].is_u64()
                && strs(&m["modules"])
        })
    }) && native.as_array().is_some_and(|a| {
        a.iter()
            .all(|t| t["tree"].is_string() && t["files"].is_u64() && strs(&t["sample"]) && strs(&t["needs"]))
    })
}

/// Gather both on the live machine.
pub fn gather_live(owners: &Owners) -> (Vec<Value>, Vec<Value>) {
    let root = Path::new("/");
    let owned = owners.owned();
    (python2_modules(root, &owned), native(root, &owned))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn tempdir(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("wend-native-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(&d).unwrap();
        d
    }

    /// A minimal 64-bit ELF shared object or program: one PT_LOAD over the whole file, one PT_DYNAMIC, a string
    /// table, DT_NEEDED for each of `needed` and DT_SONAME when given.
    fn fake_elf(etype: u16, soname: Option<&str>, needed: &[&str]) -> Vec<u8> {
        let mut strs = vec![0u8];
        let mut add = |s: &str| {
            let at = strs.len() as u64;
            strs.extend_from_slice(s.as_bytes());
            strs.push(0);
            at
        };
        let so = soname.map(&mut add);
        let ns: Vec<u64> = needed.iter().map(|n| add(n)).collect();
        let (ph_off, ph_n) = (64u64, 2u64);
        let str_off = ph_off + 56 * ph_n;
        let dyn_off = str_off + strs.len() as u64;
        let mut dyns: Vec<(u64, u64)> = ns.iter().map(|n| (1, *n)).collect();
        if let Some(s) = so {
            dyns.push((14, s));
        }
        let base = 0x400000u64;
        dyns.push((5, base + str_off));
        dyns.push((10, strs.len() as u64));
        dyns.push((0, 0));
        let total = dyn_off + 16 * dyns.len() as u64;
        let mut b = vec![0u8; 64];
        b[..4].copy_from_slice(b"\x7fELF");
        b[4] = 2;
        b[5] = 1;
        b[16..18].copy_from_slice(&etype.to_le_bytes());
        b[18..20].copy_from_slice(&62u16.to_le_bytes());
        b[32..40].copy_from_slice(&ph_off.to_le_bytes());
        b[54..56].copy_from_slice(&56u16.to_le_bytes());
        b[56..58].copy_from_slice(&(ph_n as u16).to_le_bytes());
        for (t, off, va, sz) in [
            (1u32, 0, base, total),
            (2, dyn_off, base + dyn_off, 16 * dyns.len() as u64),
        ] {
            let mut p = vec![0u8; 56];
            p[..4].copy_from_slice(&t.to_le_bytes());
            p[8..16].copy_from_slice(&off.to_le_bytes());
            p[16..24].copy_from_slice(&va.to_le_bytes());
            p[32..40].copy_from_slice(&sz.to_le_bytes());
            b.extend(p);
        }
        b.extend(&strs);
        for (t, v) in dyns {
            b.extend(t.to_le_bytes());
            b.extend(v.to_le_bytes());
        }
        b
    }

    fn put(root: &Path, p: &str, bytes: &[u8]) {
        let f = root.join(p.trim_start_matches('/'));
        fs::create_dir_all(f.parent().unwrap()).unwrap();
        fs::write(f, bytes).unwrap();
    }

    #[test]
    fn elf_headers_are_read_like_the_loader_reads_them() {
        let t = tempdir("elf");
        put(
            &t,
            "a.so",
            &fake_elf(3, Some("libpq.so.5"), &["libssl.so.10", "libc.so.6"]),
        );
        put(&t, "b", &fake_elf(2, None, &[]));
        put(&t, "c.o", &fake_elf(1, None, &[]));
        put(&t, "d.txt", b"hello");
        let mut e32 = fake_elf(3, None, &[]);
        e32[4] = 1;
        put(&t, "e", &e32);
        let mut arm = fake_elf(3, None, &["ld-linux-aarch64.so.1"]);
        arm[18..20].copy_from_slice(&183u16.to_le_bytes());
        put(&t, "f", &arm);
        assert_eq!(elf(&t.join("f")), Some(None));
        assert_eq!(
            elf(&t.join("a.so")),
            Some(Some(Dyn {
                soname: Some("libpq.so.5".into()),
                needed: vec!["libssl.so.10".into(), "libc.so.6".into()]
            }))
        );
        assert_eq!(
            elf(&t.join("b")),
            Some(Some(Dyn {
                soname: None,
                needed: vec![]
            }))
        );
        assert_eq!(elf(&t.join("c.o")), Some(None));
        assert_eq!(elf(&t.join("e")), Some(None));
        assert_eq!(elf(&t.join("d.txt")), None);
    }

    /// The rBuilder box in small: a Postgres in /opt that loads EL6's libssl and its own libpq, a hand build in
    /// /usr/local, pip's Django and a compiled lxml in python2.6's site-packages, and a packaged library, which
    /// doesn't count.
    #[test]
    fn unowned_trees_and_modules_are_listed_with_what_they_load() {
        let t = tempdir("tree");
        put(
            &t,
            "/opt/postgresql-9.2/bin/postgres",
            &fake_elf(3, None, &["libpq.so.5", "libssl.so.10", "libc.so.6"]),
        );
        put(
            &t,
            "/opt/postgresql-9.2/lib/libpq.so.5",
            &fake_elf(3, Some("libpq.so.5"), &["libc.so.6"]),
        );
        put(&t, "/opt/postgresql-9.2/share/README", b"text");
        put(
            &t,
            "/usr/local/bin/redis-server",
            &fake_elf(3, None, &["libjemalloc.so.1", "libc.so.6"]),
        );
        put(&t, "/usr/local/bin/static-tool", &fake_elf(2, None, &[]));
        // a vendor's private libssl serves only its own tree; /opt/shared/lib is on the loader's path
        put(
            &t,
            "/opt/vendor/lib/libssl.so.10",
            &fake_elf(3, Some("libssl.so.10"), &[]),
        );
        put(
            &t,
            "/opt/shared/lib/libjemalloc.so.1",
            &fake_elf(3, Some("libjemalloc.so.1"), &[]),
        );
        put(&t, "/etc/ld.so.conf", b"include ld.so.conf.d/*.conf\n");
        put(&t, "/etc/ld.so.conf.d/shared.conf", b"# vendor\n/opt/shared/lib/\n");
        let sp = "/usr/lib64/python2.6/site-packages";
        put(&t, &format!("{sp}/django/__init__.py"), b"");
        put(&t, &format!("{sp}/Django-1.3-py2.6.egg-info/PKG-INFO"), b"");
        put(
            &t,
            &format!("{sp}/lxml/etree.so"),
            &fake_elf(3, None, &["libpython2.6.so.1.0", "libxml2.so.2"]),
        );
        put(&t, &format!("{sp}/six.py"), b"");
        put(&t, &format!("{sp}/yum/__init__.py"), b"");
        // bytecode written after the package manager's lists, next to an owned source, is only a cache
        put(&t, &format!("{sp}/yum/__init__.pyc"), b"");
        let dp = "/usr/lib/python2.7/dist-packages";
        put(&t, &format!("{dp}/apt/__init__.py"), b"");
        put(&t, &format!("{dp}/apt/__init__.pyc"), b"");
        // pycentral: the source is a link into /usr/share/pycentral, only the bytecode is a file
        put(&t, "/usr/share/pycentral/x/foo.py", b"");
        std::os::unix::fs::symlink("/usr/share/pycentral/x/foo.py", t.join(&format!("{dp}/foo.py")[1..])).unwrap();
        put(&t, &format!("{dp}/foo.pyc"), b"");
        put(
            &t,
            "/usr/lib64/libxml2.so.2",
            &fake_elf(3, Some("libxml2.so.2"), &["libc.so.6"]),
        );
        put(
            &t,
            "/usr/src/kernels/x/scripts/foo",
            &fake_elf(3, None, &["libgone.so.1"]),
        );
        let owned: HashSet<String> = [
            format!("{sp}/yum/__init__.py"),
            format!("{dp}/apt/__init__.py"),
            "/usr/lib64/libxml2.so.2".to_string(),
        ]
        .into_iter()
        .collect();
        let n = native(&t, &owned);
        let m = python2_modules(&t, &owned);
        assert!(check(&json!(m), &json!(n)), "{m:#?} {n:#?}");
        let by: BTreeMap<&str, &Value> = n.iter().map(|x| (x["tree"].as_str().unwrap(), x)).collect();
        assert_eq!(
            by.keys().copied().collect::<Vec<_>>(),
            vec!["/opt/postgresql-9.2", "/opt/shared", "/opt/vendor", sp, "/usr/local"]
        );
        // its own libpq is provided by an unowned file; libssl.so.10 and libc.so.6 aren't
        assert_eq!(by["/opt/postgresql-9.2"]["needs"], json!(["libc.so.6", "libssl.so.10"]));
        assert_eq!(by["/opt/postgresql-9.2"]["files"], 2);
        assert_eq!(by[sp]["needs"], json!(["libpython2.6.so.1.0", "libxml2.so.2"]));
        assert_eq!(by["/usr/local"]["needs"], json!(["libc.so.6"]));
        assert_eq!(m.len(), 1, "{m:#?}");
        assert_eq!(m[0]["version"], "2.6");
        assert_eq!(m[0]["compiled"], 1);
        assert_eq!(m[0]["files"], 4);
        assert_eq!(m[0]["modules"], json!(["django", "lxml", "six"]));
    }

    #[test]
    fn module_names() {
        assert_eq!(module_name("lxml-2.3-py2.6-linux-x86_64.egg"), Some("lxml".into()));
        assert_eq!(module_name("_sqlitemodule.so"), Some("_sqlite".into()));
        assert_eq!(module_name("easy-install.pth"), None);
        assert_eq!(module_name("six.pyc"), Some("six".into()));
        assert_eq!(tree_of("/opt/app/bin/x"), "/opt/app");
        assert_eq!(tree_of("/usr/local/lib/libx.so.1"), "/usr/local");
        assert_eq!(tree_of("/usr/sbin/nginx"), "/usr/sbin");
        assert_eq!(
            tree_of("/usr/lib64/python2.6/site-packages/lxml/etree.so"),
            "/usr/lib64/python2.6/site-packages"
        );
    }
}
