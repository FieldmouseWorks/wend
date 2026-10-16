//! Runtime preflight (SPEC "Runtime preflight"): does this machine's own code run on a runtime the route's releases
//! stop shipping? Python 2 first. What counts is what the machine's programs run, not what is installed: a running
//! process or a cron job whose program no package owns, and any process with the interpreter loaded into it
//! (mod_wsgi in httpd). The distro's own Python 2 programs (yum, firewalld, fail2ban) belong to packages the route
//! carries across, so they don't count.

use regex::Regex;
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};

/// Where cron reads its jobs: the system crontab and cron.d (a user field after the time), the per-user spools (EL's
/// and Debian's), and the run-parts directories, whose files are the jobs themselves.
const CRONTABS: &[&str] = &["etc/crontab"];
const CRON_DIRS: &[&str] = &["etc/cron.d"];
const SPOOLS: &[&str] = &["var/spool/cron", "var/spool/cron/crontabs"];
const RUN_PARTS: &[&str] = &[
    "etc/cron.hourly",
    "etc/cron.daily",
    "etc/cron.weekly",
    "etc/cron.monthly",
];
/// cron's PATH (Debian's default, and what EL's /etc/crontab sets less the sbins), which `#!/usr/bin/env python`
/// and a bare `python` in a job resolve through
const PATH: &[&str] = &["usr/bin", "bin"];

/// A path inside `root`, the way the machine itself would resolve it: symlinks followed, absolute targets kept
/// inside `root`.
fn resolve(root: &Path, path: &str) -> String {
    let mut p = path.to_string();
    for _ in 0..16 {
        let Ok(t) = fs::read_link(root.join(p.trim_start_matches('/'))) else {
            break;
        };
        let t = t.to_string_lossy().into_owned();
        p = if t.starts_with('/') {
            t
        } else {
            let parent = Path::new(&p).parent().unwrap_or(Path::new("/"));
            normalize(&format!("{}/{t}", parent.to_string_lossy()))
        };
    }
    p
}

fn normalize(p: &str) -> String {
    let mut out: Vec<&str> = vec![];
    for c in p.split('/') {
        match c {
            "" | "." => {}
            ".." => {
                out.pop();
            }
            c => out.push(c),
        }
    }
    format!("/{}", out.join("/"))
}

/// The Python 2 version (`2.7`) an interpreter path runs, or None when it isn't Python 2. By its resolved name
/// (`python2.7`), else, for a plain `python` or `python2`, by the one `lib/python2.N` beside it.
fn python2_of(root: &Path, path: &str) -> Option<String> {
    let real = resolve(root, path);
    let base = Path::new(&real).file_name()?.to_string_lossy().into_owned();
    let re = Regex::new(r"^python(2(?:\.(\d+))?|3.*)?$").unwrap();
    let c = re.captures(&base)?;
    if c.get(1).is_some_and(|m| m.as_str().starts_with('3')) {
        return None;
    }
    if let Some(minor) = c.get(2) {
        return Some(format!("2.{}", minor.as_str()));
    }
    // `python` or `python2` that is a binary of its own. EL5 and EL6 ship /usr/bin/python as a hard link to
    // python2.N: a sibling python2.N with the same inode names it, even beside EPEL's python26 or python34
    {
        use std::os::unix::fs::MetadataExt;
        let here = root.join(real.trim_start_matches('/'));
        if let (Ok(m), Some(dir)) = (fs::metadata(&here), here.parent()) {
            let two = Regex::new(r"^python(2\.\d+)$").unwrap();
            for e in fs::read_dir(dir).into_iter().flatten().flatten() {
                let n = e.file_name().to_string_lossy().into_owned();
                if let (Some(c2), Ok(m2)) = (two.captures(&n), fs::metadata(e.path())) {
                    if m2.dev() == m.dev() && m2.ino() == m.ino() {
                        return Some(c2[1].to_string());
                    }
                }
            }
        }
    }
    // otherwise what its prefix's lib holds says which
    let prefix = Path::new(&real).parent()?.parent()?.to_string_lossy().into_owned();
    let mut found = BTreeSet::new();
    let mut three = false;
    let two = Regex::new(r"^python(2\.\d+)$").unwrap();
    for lib in ["lib", "lib64"] {
        let dir = root.join(format!("{prefix}/{lib}").trim_start_matches('/'));
        for e in fs::read_dir(dir).into_iter().flatten().flatten() {
            let n = e.file_name().to_string_lossy().into_owned();
            if let Some(m) = two.captures(&n) {
                found.insert(m[1].to_string());
            } else if n.starts_with("python3") {
                three = true;
            }
        }
    }
    match (found.len(), three, c.get(1).is_some()) {
        (1, _, true) | (1, false, false) => found.into_iter().next(),
        _ => None,
    }
}

/// The interpreter a script's `#!` line names, `env` looked up through cron's PATH.
fn shebang(root: &Path, script: &str, path: &[String]) -> Option<String> {
    let mut head = vec![0u8; 256];
    let n = {
        use std::io::Read;
        let mut f = fs::File::open(root.join(script.trim_start_matches('/'))).ok()?;
        f.read(&mut head).ok()?
    };
    let line = String::from_utf8_lossy(&head[..n]);
    let line = line.strip_prefix("#!")?.lines().next()?.trim().to_string();
    let mut words = line.split_whitespace();
    let interp = words.next()?;
    if interp.ends_with("/env") {
        let name = words.find(|w| !w.starts_with('-') && !w.contains('='))?;
        return lookup(root, name, path);
    }
    Some(interp.to_string())
}

fn lookup(root: &Path, name: &str, path: &[String]) -> Option<String> {
    if name.starts_with('/') {
        return Some(name.to_string());
    }
    path.iter().map(|d| normalize(&format!("{d}/{name}"))).find(|p| {
        root.join(p.trim_start_matches('/')).exists() || fs::read_link(root.join(p.trim_start_matches('/'))).is_ok()
    })
}

/// What a Python command line runs: the script (made absolute from `cwd`), `-m module`, or `-c`.
fn program_of(args: &[String], cwd: &str) -> String {
    let mut i = 0;
    while i < args.len() {
        let a = &args[i];
        match a.as_str() {
            "-m" => return format!("-m {}", args.get(i + 1).map_or("", |s| s)),
            "-c" => return "-c".into(),
            // options that take a value
            "-W" | "-X" | "-Q" => i += 2,
            a if a.starts_with('-') => i += 1,
            a if a.starts_with('/') => return normalize(a),
            a => return normalize(&format!("{cwd}/{a}")),
        }
    }
    "(interactive)".into()
}

/// Which package owns a file: dpkg's lists, read straight from /var/lib/dpkg/info; rpm's database through `rpm`
/// (only on the live machine: its format is rpm's own).
pub struct Owners {
    root: PathBuf,
    dpkg: Option<HashMap<String, String>>,
}

impl Owners {
    pub fn new(root: &Path) -> Owners {
        let info = root.join("var/lib/dpkg/info");
        let dpkg = info.is_dir().then(|| {
            let mut m = HashMap::new();
            for e in fs::read_dir(&info).into_iter().flatten().flatten() {
                let n = e.file_name().to_string_lossy().into_owned();
                let Some(pkg) = n.strip_suffix(".list") else {
                    continue;
                };
                let pkg = pkg.split(':').next().unwrap_or(pkg).to_string();
                for l in fs::read_to_string(e.path()).unwrap_or_default().lines() {
                    m.insert(l.to_string(), pkg.clone());
                }
            }
            m
        });
        // dpkg installed on an rpm machine (EPEL ships it) leaves an empty info dir: rpm's database is the real one
        let dpkg = dpkg.filter(|m| !m.is_empty());
        Owners {
            root: root.to_path_buf(),
            dpkg,
        }
    }

    pub fn of(&self, path: &str) -> Option<String> {
        let real = resolve(&self.root, path);
        if let Some(m) = &self.dpkg {
            return m.get(path).or_else(|| m.get(&real)).cloned();
        }
        if self.root != Path::new("/") {
            return None;
        }
        let out = std::process::Command::new("rpm")
            .args(["-qf", "--qf", "%{NAME}\\n", &real])
            .output()
            .ok()?;
        let s = String::from_utf8_lossy(&out.stdout);
        let name = s.lines().next()?.trim();
        (out.status.success() && !name.is_empty() && !name.contains(' ')).then(|| name.to_string())
    }

    /// The Conary trove that owns a file, on a live machine Conary manages too (rPath and SAS appliances): a package
    /// manager the route doesn't drive, so what it owns is still the machine's own (`conary:<trove>`).
    pub fn other(&self, path: &str) -> Option<String> {
        if self.root != Path::new("/") || !Path::new("/usr/bin/conary").exists() {
            return None;
        }
        let real = resolve(&self.root, path);
        let out = std::process::Command::new("conary")
            .args(["q", "--path", &real])
            .output()
            .ok()?;
        let s = String::from_utf8_lossy(&out.stdout);
        // `mint:python=/pdt.cny.sas.com@sas:rba-8-devel/...[...]`, one line per trove that owns it
        let trove = s.lines().next()?.split('=').next()?.trim();
        (out.status.success() && !trove.is_empty() && !trove.contains(' ')).then(|| format!("conary:{trove}"))
    }

    /// Every file a package owns, as the machine resolves it (a package that lists `/lib/x86_64-linux-gnu/libc.so.6`
    /// owns `/usr/lib/x86_64-linux-gnu/libc.so.6` on a merged-/usr machine): dpkg's lists, or `rpm -qal` on the live
    /// machine.
    pub fn owned(&self) -> HashSet<String> {
        let listed: Vec<String> = if let Some(m) = &self.dpkg {
            m.keys().cloned().collect()
        } else if self.root == Path::new("/") {
            std::process::Command::new("rpm")
                .arg("-qal")
                .output()
                .map(|o| {
                    String::from_utf8_lossy(&o.stdout)
                        .lines()
                        .filter(|l| l.starts_with('/'))
                        .map(str::to_string)
                        .collect()
                })
                .unwrap_or_default()
        } else {
            vec![]
        };
        let mut dirs: HashMap<String, String> = HashMap::new();
        let mut out = HashSet::new();
        for p in listed {
            let Some((dir, name)) = p.rsplit_once('/') else {
                continue;
            };
            let real = dirs
                .entry(dir.to_string())
                .or_insert_with(|| {
                    fs::canonicalize(self.root.join(dir.trim_start_matches('/')))
                        .ok()
                        .and_then(|r| {
                            r.strip_prefix(&self.root)
                                .ok()
                                .map(|r| format!("/{}", r.to_string_lossy()))
                        })
                        .unwrap_or_else(|| dir.to_string())
                })
                .clone();
            out.insert(format!("{}/{name}", real.trim_end_matches('/')));
            out.insert(p);
        }
        out
    }
}

/// The systemd unit a process runs in, from its cgroup (`0::/system.slice/billing.service`).
fn unit_of(cgroup: &str) -> Option<String> {
    cgroup.lines().find_map(|l| {
        let path = l.rsplit(':').next()?;
        path.split('/').rfind(|c| c.ends_with(".service")).map(str::to_string)
    })
}

/// Every Python 2 user on the live machine. Only root can read every process's executable and maps and every
/// user's crontab, and a list a normal user gathered would quietly miss them, so anyone else is refused.
pub fn gather_live() -> Result<(Vec<Value>, Owners), String> {
    // SAFETY: geteuid has no preconditions.
    if unsafe { libc::geteuid() } != 0 {
        return Err(
            "wend facts and preflight must run as root: they read every process and every crontab, and a \
                    normal user can't see another user's"
                .into(),
        );
    }
    let root = Path::new("/");
    let owners = Owners::new(root);
    Ok((gather(Path::new("/proc"), root, &owners), owners))
}

/// Every Python 2 user on the machine: running processes (`proc`) and cron jobs (under `root`).
pub fn gather(proc: &Path, root: &Path, owners: &Owners) -> Vec<Value> {
    use std::os::unix::fs::MetadataExt;
    let root_id = fs::metadata(root).ok().map(|m| (m.dev(), m.ino()));
    // one entry per program: httpd's workers, or a script both a service and a cron job run, count once
    let mut found: BTreeMap<String, Value> = BTreeMap::new();
    let lib = Regex::new(r"(?:libpython|/python)(2\.\d+)(?:[./]|$)").unwrap();
    let me = std::process::id().to_string();
    for e in fs::read_dir(proc).into_iter().flatten().flatten() {
        let pid = e.file_name().to_string_lossy().into_owned();
        if !pid.bytes().all(|b| b.is_ascii_digit()) || pid == me {
            continue;
        }
        let d = e.path();
        // a process in a container (docker, LXC) has a root of its own: its programs aren't this machine's
        if let (Some(r), Ok(m)) = (root_id, fs::metadata(d.join("root"))) {
            if (m.dev(), m.ino()) != r {
                continue;
            }
        }
        let Ok(exe) = fs::read_link(d.join("exe")) else {
            continue; // a kernel thread, or gone
        };
        let exe = exe.to_string_lossy().trim_end_matches(" (deleted)").to_string();
        let maps = fs::read_to_string(d.join("maps")).unwrap_or_default();
        let mapped = maps
            .lines()
            .filter_map(|l| l.split_whitespace().nth(5))
            .find_map(|p| lib.captures(p).map(|c| (c[1].to_string(), p.to_string())));
        let base = Path::new(&exe)
            .file_name()
            .map(|b| b.to_string_lossy().into_owned())
            .unwrap_or_default();
        let by_name = python2_of(root, &exe);
        let Some(version) = by_name.or(mapped.as_ref().map(|m| m.0.clone())) else {
            continue;
        };
        let cmd: Vec<String> = fs::read(d.join("cmdline"))
            .unwrap_or_default()
            .split(|b| *b == 0)
            .filter(|a| !a.is_empty())
            .map(|a| String::from_utf8_lossy(a).into_owned())
            .collect();
        let cwd = fs::read_link(d.join("cwd")).map_or("/".into(), |c| c.to_string_lossy().into_owned());
        let unit = unit_of(&fs::read_to_string(d.join("cgroup")).unwrap_or_default());
        // embedded: the program is the process, the interpreter the Python library it has loaded
        let (via, program, interp) = if base.starts_with("python") {
            ("process", program_of(cmd.get(1..).unwrap_or(&[]), &cwd), exe.clone())
        } else {
            ("embedded", exe.clone(), mapped.map_or(exe.clone(), |m| m.1))
        };
        let owner = if program.starts_with('/') {
            owners.of(&program)
        } else {
            None
        };
        let other = if owner.is_none() && program.starts_with('/') {
            owners.other(&program)
        } else {
            None
        };
        found.entry(program.clone()).or_insert(json!({
            "runtime": "python2", "version": version, "via": via, "program": program, "interpreter": interp,
            "where": unit, "owner": owner, "other_owner": other,
        }));
    }
    let bare = Regex::new(r"^python2?(\.\d+)?$").unwrap();
    for (src, cmd, path) in cron_jobs(root) {
        let words: Vec<String> = cmd
            .split(|c: char| c.is_whitespace() || ";&|()`".contains(c))
            .filter(|w| !w.is_empty())
            .map(str::to_string)
            .collect();
        let mut cwd = "/".to_string();
        for (i, w) in words.iter().enumerate() {
            if i > 0 && words[i - 1] == "cd" && w.starts_with('/') {
                cwd = normalize(w);
                continue;
            }
            // a path (`/opt/x/job`, or `./job` after a `cd`), or a bare interpreter name through the job's PATH
            let found_at = if w.starts_with('/') {
                Some(w.clone())
            } else if w.contains('/') {
                Some(normalize(&format!("{cwd}/{w}")))
            } else if bare.is_match(w) {
                lookup(root, w, &path)
            } else {
                None
            };
            let Some(at) = found_at else { continue };
            let hit = if let Some(v) = python2_of(root, &at) {
                Some((v, at.clone(), program_of(&words[i + 1..], &cwd)))
            } else {
                shebang(root, &at, &path).and_then(|interp| python2_of(root, &interp).map(|v| (v, interp, at.clone())))
            };
            let Some((version, interp, program)) = hit else {
                continue;
            };
            let owner = if program.starts_with('/') {
                owners.of(&program)
            } else {
                None
            };
            let other = if owner.is_none() && program.starts_with('/') {
                owners.other(&program)
            } else {
                None
            };
            found.entry(program.clone()).or_insert(json!({
                "runtime": "python2", "version": version, "via": "cron", "program": program,
                "interpreter": interp, "where": src, "owner": owner, "other_owner": other,
            }));
            break;
        }
    }
    found.into_values().collect()
}

/// (where, command, PATH) for every cron job, PATH as the crontab last set it before the job; a run-parts file is
/// its own command.
fn cron_jobs(root: &Path) -> Vec<(String, String, Vec<String>)> {
    let default: Vec<String> = PATH.iter().map(|d| format!("/{d}")).collect();
    let mut out = vec![];
    let mut tables: Vec<(PathBuf, bool)> = CRONTABS.iter().map(|t| (root.join(t), true)).collect();
    for (dirs, system) in [(CRON_DIRS, true), (SPOOLS, false)] {
        for d in dirs {
            let mut fs_: Vec<PathBuf> = fs::read_dir(root.join(d))
                .into_iter()
                .flatten()
                .flatten()
                .map(|e| e.path())
                .filter(|p| p.is_file())
                .collect();
            fs_.sort();
            tables.extend(fs_.into_iter().map(|p| (p, system)));
        }
    }
    let env = Regex::new(r"^[A-Za-z_][A-Za-z0-9_]*\s*=").unwrap();
    for (t, system) in tables {
        let Ok(text) = fs::read_to_string(&t) else { continue };
        let src = format!("/{}", t.strip_prefix(root).unwrap_or(&t).to_string_lossy());
        let mut path = default.clone();
        for l in text.lines() {
            let l = l.trim();
            if let Some(p) = l
                .strip_prefix("PATH")
                .map(str::trim_start)
                .and_then(|r| r.strip_prefix('='))
            {
                path = p
                    .trim()
                    .trim_matches(|c| c == '"' || c == '\'')
                    .split(':')
                    .filter(|d| d.starts_with('/'))
                    .map(str::to_string)
                    .collect();
                continue;
            }
            if l.is_empty() || l.starts_with('#') || env.is_match(l) {
                continue;
            }
            let skip = if l.starts_with('@') { 1 } else { 5 } + usize::from(system);
            let mut rest = l;
            for _ in 0..skip {
                rest = rest.trim_start();
                rest = rest.find(char::is_whitespace).map_or("", |i| &rest[i..]);
            }
            let rest = rest.trim();
            if !rest.is_empty() {
                out.push((src.clone(), rest.to_string(), path.clone()));
            }
        }
    }
    for d in RUN_PARTS {
        let mut fs_: Vec<PathBuf> = fs::read_dir(root.join(d))
            .into_iter()
            .flatten()
            .flatten()
            .map(|e| e.path())
            .filter(|p| p.is_file())
            .collect();
        fs_.sort();
        for p in fs_ {
            let abs = format!("/{}", p.strip_prefix(root).unwrap_or(&p).to_string_lossy());
            out.push((format!("/{d}"), abs, default.clone()));
        }
    }
    out
}

/// A user the machine's owner has to decide about: its program belongs to no package, or the interpreter runs
/// inside another program (mod_wsgi, mod_python: the code it runs is configured on this machine, and a package that
/// ships such an app, like FreeIPA, is one no route carries across anyway).
pub fn is_own(u: &Value) -> bool {
    u["via"] == "embedded" || u["owner"].is_null()
}

/// How one user reads in a verdict.
pub fn describe(u: &Value) -> String {
    let what = match u["via"].as_str() {
        Some("embedded") => format!(
            "{} has Python {} loaded",
            u["program"].as_str().unwrap_or("?"),
            u["version"].as_str().unwrap_or("2")
        ),
        _ => format!(
            "{} runs on Python {}",
            u["program"].as_str().unwrap_or("?"),
            u["version"].as_str().unwrap_or("2")
        ),
    };
    let what = match u["other_owner"].as_str() {
        Some(o) => match o.split_once(':') {
            Some((pm, pkg)) => format!("{what}, installed by {pm} ({pkg})"),
            None => format!("{what}, installed by {o}"),
        },
        None => what,
    };
    match u["where"].as_str() {
        Some(w) if u["via"] == "cron" => format!("{what} (cron, {w})"),
        Some(w) => format!("{what} ({w})"),
        None => what,
    }
}

/// Facts from a file are held to the shape `gather` writes.
pub fn check(users: &Value) -> bool {
    users.as_array().is_some_and(|a| {
        a.iter().all(|u| {
            u["runtime"] == "python2"
                && (u["version"].is_string())
                && matches!(u["via"].as_str(), Some("process" | "embedded" | "cron"))
                && u["program"].is_string()
                && u["interpreter"].is_string()
                && (u["where"].is_string() || u["where"].is_null())
                && (u["owner"].is_string() || u["owner"].is_null())
                && (u["other_owner"].is_string() || u["other_owner"].is_null())
        })
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::symlink;

    fn tempdir(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("wend-runtime-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(&d).unwrap();
        d
    }

    fn write(root: &Path, p: &str, text: &str) {
        let f = root.join(p);
        fs::create_dir_all(f.parent().unwrap()).unwrap();
        fs::write(f, text).unwrap();
    }

    fn link(root: &Path, p: &str, to: &str) {
        let f = root.join(p);
        fs::create_dir_all(f.parent().unwrap()).unwrap();
        symlink(to, f).unwrap();
    }

    fn process(proc: &Path, pid: u32, exe: &str, cmd: &[&str], maps: &[&str], cgroup: &str) {
        let d = proc.join(pid.to_string());
        fs::create_dir_all(&d).unwrap();
        symlink(exe, d.join("exe")).unwrap();
        symlink("/opt/billing", d.join("cwd")).unwrap();
        fs::write(d.join("cmdline"), cmd.join("\0") + "\0").unwrap();
        let m: Vec<String> = maps
            .iter()
            .map(|p| format!("7f0000000000-7f0000001000 r-xp 00000000 fd:00 1234 {p}"))
            .collect();
        fs::write(d.join("maps"), m.join("\n")).unwrap();
        fs::write(d.join("cgroup"), cgroup).unwrap();
    }

    /// A CentOS 7 LAMP box with a Python 2 billing app: its service, its cron job, mod_wsgi in httpd, and the
    /// distro's own Python 2 daemons, which don't count.
    #[test]
    fn the_machines_own_python2_counts_and_the_distros_doesnt() {
        let t = tempdir("lamp");
        let (root, proc) = (t.join("root"), t.join("proc"));
        link(&root, "usr/bin/python", "python2");
        link(&root, "usr/bin/python2", "python2.7");
        write(&root, "usr/bin/python2.7", "ELF");
        write(&root, "usr/bin/python3.6", "ELF");
        write(&root, "usr/bin/fail2ban-server", "#!/usr/bin/python -s\n");
        write(&root, "opt/billing/app.py", "print 'hi'\n");
        write(&root, "opt/billing/nightly.py", "#!/usr/bin/env python\nprint 'x'\n");
        write(&root, "opt/billing/tool3.py", "#!/usr/bin/python3.6\n");
        write(&root, "etc/crontab", "SHELL=/bin/bash\nMAILTO=root\n# comment\n");
        write(
            &root,
            "etc/cron.d/billing",
            "PATH=/usr/bin\n30 2 * * * billing /opt/billing/nightly.py --all\n*/5 * * * * root /opt/billing/tool3.py\n",
        );
        write(
            &root,
            "var/spool/cron/alice",
            "@daily cd /opt/billing && python app.py report\n",
        );
        write(&root, "etc/cron.daily/logrotate", "#!/bin/sh\n");
        // a crontab's own PATH, and a relative script after a cd
        write(&root, "opt/legacy/bin/python", "ELF");
        fs::create_dir_all(root.join("opt/legacy/lib/python2.6")).unwrap();
        write(&root, "srv/app/nightly", "#!/usr/bin/python2.7\n");
        write(&root, "etc/cron.d/legacy", "PATH=/opt/legacy/bin:/usr/bin:/bin\n0 4 * * * root python /opt/legacy/report.py\n15 4 * * * root cd /srv/app && ./nightly\n");
        let py = "/usr/bin/python2.7";
        process(
            &proc,
            100,
            py,
            &["/usr/bin/python2", "-u", "app.py", "--serve"],
            &[],
            "0::/system.slice/billing.service\n",
        );
        process(
            &proc,
            101,
            py,
            &["/usr/bin/python", "-s", "/usr/bin/fail2ban-server", "-xf"],
            &[],
            "0::/system.slice/fail2ban.service\n",
        );
        let wsgi = ["/usr/lib64/httpd/modules/mod_wsgi.so", "/usr/lib64/libpython2.7.so.1.0"];
        process(
            &proc,
            102,
            "/usr/sbin/httpd",
            &["/usr/sbin/httpd", "-DFOREGROUND"],
            &wsgi,
            "0::/system.slice/httpd.service\n",
        );
        process(
            &proc,
            103,
            "/usr/sbin/httpd",
            &["/usr/sbin/httpd", "-DFOREGROUND"],
            &wsgi,
            "0::/system.slice/httpd.service\n",
        );
        process(
            &proc,
            104,
            "/usr/sbin/sshd",
            &["/usr/sbin/sshd", "-D"],
            &["/usr/lib64/libcrypto.so.10"],
            "",
        );
        // packages own the distro's files, nothing owns /opt
        let mut m = HashMap::new();
        m.insert("/usr/bin/fail2ban-server".to_string(), "fail2ban-server".to_string());
        m.insert("/usr/sbin/httpd".to_string(), "httpd".to_string());
        let owners = Owners {
            root: root.clone(),
            dpkg: Some(m),
        };
        let users = gather(&proc, &root, &owners);
        assert!(check(&json!(users)), "{users:#?}");
        let own: Vec<String> = users.iter().filter(|u| is_own(u)).map(describe).collect();
        assert_eq!(
            own,
            vec![
                "/opt/billing/app.py runs on Python 2.7 (billing.service)",
                "/opt/billing/nightly.py runs on Python 2.7 (cron, /etc/cron.d/billing)",
                "/opt/legacy/report.py runs on Python 2.6 (cron, /etc/cron.d/legacy)",
                "/srv/app/nightly runs on Python 2.7 (cron, /etc/cron.d/legacy)",
                "/usr/sbin/httpd has Python 2.7 loaded (httpd.service)",
            ],
            "{users:#?}"
        );
        // fail2ban is the distro's: recorded, not the owner's to decide
        assert!(users.iter().any(|u| u["owner"] == "fail2ban-server" && !is_own(u)));
        // alice's `cd /opt/billing && python app.py` is the same program the service runs: counted once
        assert_eq!(
            users.iter().filter(|u| u["program"] == "/opt/billing/app.py").count(),
            1
        );
    }

    #[test]
    fn interpreters_are_named_by_what_they_resolve_to() {
        let t = tempdir("names");
        let root = t.as_path();
        // Ubuntu focal with python-is-python3: `python` is Python 3
        link(root, "usr/bin/python", "python3");
        link(root, "usr/bin/python3", "python3.8");
        write(root, "usr/bin/python2.7", "ELF");
        assert_eq!(python2_of(root, "/usr/bin/python"), None);
        assert_eq!(python2_of(root, "/usr/bin/python2.7"), Some("2.7".into()));
        // a Python 2 built from source under /opt, named plain `python`
        write(root, "opt/py/bin/python", "ELF");
        fs::create_dir_all(root.join("opt/py/lib/python2.6")).unwrap();
        assert_eq!(python2_of(root, "/opt/py/bin/python"), Some("2.6".into()));
        // CentOS 5 with EPEL's python26: /usr/bin/python is a hard link to python2.4, beside python2.6
        let c5 = t.join("c5");
        write(&c5, "usr/bin/python2.4", "ELF 2.4");
        fs::hard_link(c5.join("usr/bin/python2.4"), c5.join("usr/bin/python")).unwrap();
        write(&c5, "usr/bin/python2.6", "ELF 2.6");
        fs::create_dir_all(c5.join("usr/lib64/python2.4")).unwrap();
        fs::create_dir_all(c5.join("usr/lib64/python2.6")).unwrap();
        assert_eq!(python2_of(&c5, "/usr/bin/python"), Some("2.4".into()));
        assert_eq!(
            program_of(&["-W".into(), "ignore".into(), "-m".into(), "http.server".into()], "/"),
            "-m http.server"
        );
        assert_eq!(program_of(&["../run.py".into()], "/opt/app/bin"), "/opt/app/run.py");
        assert_eq!(
            unit_of("1:name=systemd:/system.slice/x.service\n"),
            Some("x.service".into())
        );
        assert_eq!(unit_of("0::/user.slice/user-0.slice/session-1.scope\n"), None);
    }
}
