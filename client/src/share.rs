//! `wend share`: a box report someone can post without giving their machine away. It reads what preflight reads
//! (`wend facts`), names the release the machine runs, judges it against the route that starts there (if one does),
//! scrubs everything that identifies the machine or its people, SHOWS the whole report, saves it, and prints a
//! prefilled GitHub issue link. Nothing is sent: the person opens the link and submits it, or doesn't.
//!
//! Read-only, like `facts` and `preflight`: it reads /proc, /sys, /etc and the package database, and writes only the
//! report file in the current directory.

use crate::atlas::{get, str_of, Atlas};
use regex::Regex;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::fs;
use std::path::Path;

pub const REPO_URL: &str = "https://github.com/FieldmouseWorks/wend";
/// GitHub drops very long prefilled URLs; past this the issue gets the summary and the file is attached
pub const URL_BUDGET: usize = 7000;

/// Debian's codenames, by major version.
pub const DEBIAN_CODENAMES: &[&str] = &[
    "", "", "", "", "etch", "lenny", "squeeze", "wheezy", "jessie", "stretch", "buster", "bullseye", "bookworm",
    "trixie",
];

/// The release a machine runs: its own words for it, and the atlas's (family, release id) when wend knows it.
pub struct System {
    pub pretty: String,
    pub release: Option<(String, String)>,
}

fn read(root: &Path, p: &str) -> String {
    fs::read_to_string(root.join(p)).unwrap_or_default()
}

fn kv(text: &str, key: &str) -> Option<String> {
    text.lines().find_map(|l| {
        l.strip_prefix(key)
            .and_then(|r| r.strip_prefix('='))
            .map(|v| v.trim().trim_matches('"').to_string())
    })
}

/// What the machine under `root` runs, from the files each family's adapter reads (`release_file`).
pub fn system(root: &Path) -> System {
    let rh = read(root, "etc/redhat-release");
    let osr = read(root, "etc/os-release");
    let lsb = read(root, "etc/lsb-release");
    let deb = read(root, "etc/debian_version");
    let first = |s: &str| s.lines().next().unwrap_or("").trim().to_string();
    // Oracle Linux's redhat-release says Red Hat, for the programs that look there: oracle-release says what it is
    let oracle = read(root, "etc/oracle-release");
    if !oracle.trim().is_empty() {
        return System {
            pretty: first(&oracle),
            release: None,
        };
    }
    if !rh.trim().is_empty() {
        // CentOS Linux 5 to 8 and AlmaLinux 8 and up are the EL ladder's rungs (a machine on one joins a route
        // partway), and Scientific Linux joins at the rung its packages match; Stream, RHEL and the other rebuilds
        // aren't on it
        let release = Regex::new(r"^(CentOS|AlmaLinux|Scientific) (?:Linux )?release (\d+)")
            .unwrap()
            .captures(rh.trim())
            .map(|c| {
                let vendor = match &c[1] {
                    "CentOS" => "centos",
                    "AlmaLinux" => "alma",
                    _ => "sl",
                };
                ("el".to_string(), format!("{vendor}{}", &c[2]))
            });
        return System {
            pretty: first(&rh),
            release,
        };
    }
    if kv(&lsb, "DISTRIB_ID").as_deref() == Some("Ubuntu") || kv(&osr, "ID").as_deref() == Some("ubuntu") {
        let code = kv(&lsb, "DISTRIB_CODENAME").or_else(|| kv(&osr, "VERSION_CODENAME"));
        let pretty = kv(&lsb, "DISTRIB_DESCRIPTION")
            .or_else(|| kv(&osr, "PRETTY_NAME"))
            .unwrap_or_else(|| "Ubuntu".into());
        return System {
            pretty,
            release: code.map(|c| ("ubuntu".to_string(), c)),
        };
    }
    if !deb.trim().is_empty() {
        let v = first(&deb);
        let major = v.split('.').next().and_then(|m| m.parse::<usize>().ok());
        let code = major.and_then(|m| DEBIAN_CODENAMES.get(m)).filter(|c| !c.is_empty());
        return System {
            pretty: kv(&osr, "PRETTY_NAME").unwrap_or_else(|| format!("Debian GNU/Linux {v}")),
            release: code.map(|c| ("debian".to_string(), c.to_string())),
        };
    }
    System {
        pretty: kv(&osr, "PRETTY_NAME").unwrap_or_else(|| "unknown".into()),
        release: None,
    }
}

/// Words only this machine knows: its hostname, the ways an installer writes that name elsewhere, the domain it is
/// in, and every login account (UID_MIN and up). Anything the person adds with --redact too. Longest first, so a
/// hostname goes before a user named like part of it.
pub fn local_words(root: &Path, nodename: &str, extra: &[String]) -> Vec<String> {
    let mut words: Vec<String> = extra.to_vec();
    for h in [nodename.to_string(), read(root, "etc/hostname").trim().to_string()] {
        let labels: Vec<&str> = h.split('.').collect();
        let short = labels[0];
        words.push(short.to_string());
        // CentOS 6's installer names the volume group after the host with its dashes dropped (vg_web01 for web-01)
        words.push(short.replace('-', ""));
        // the domain alone names the place as well as the host does: each level of it that is more than its last
        // part, and every part of it long enough to be a name and not one of the words all domains are made of
        for from in 1..labels.len().saturating_sub(1) {
            words.push(labels[from..].join("."));
        }
        words.extend(labels[1..].iter().filter(|l| l.len() >= 4).map(|l| l.to_string()));
        words.push(h);
    }
    // EL 5 and 6 start login accounts at 500; login.defs says where this machine does
    let uid_min = read(root, "etc/login.defs")
        .lines()
        .find_map(|l| {
            l.trim()
                .strip_prefix("UID_MIN")
                .and_then(|v| v.trim().parse::<u64>().ok())
        })
        .unwrap_or(500);
    for l in read(root, "etc/passwd").lines() {
        let f: Vec<&str> = l.split(':').collect();
        if let (Some(name), Some(uid)) = (f.first(), f.get(2).and_then(|u| u.parse::<u64>().ok())) {
            // no upper bound: directory services hand out numbers far above 65534. The two "nobody" numbers aren't
            // anybody
            if uid >= uid_min && uid != 65534 && uid != 4294967294 {
                words.push(name.to_string());
            }
        }
    }
    // a cloud image's login is named after its distro (ubuntu, centos, debian): scrubbing those would scrub the
    // release itself, and they say nothing about whose machine it is. Nor do the words every domain is made of
    const COMMON: &[&str] = &[
        "localhost",
        "localdomain",
        "ubuntu",
        "debian",
        "centos",
        "almalinux",
        "rocky",
        "fedora",
        "admin",
        "user",
        "linux",
        "local",
        "internal",
        "intranet",
        "example",
        "corp",
        "home",
        "cloud",
        "prod",
        "test",
        "info",
        "name",
        "arpa",
        "lan",
        "com",
        "net",
        "org",
    ];
    words.retain(|w| w.len() >= 2 && !COMMON.contains(&w.to_ascii_lowercase().as_str()));
    words.sort_by(|a, b| b.len().cmp(&a.len()).then(a.cmp(b)));
    words.dedup();
    words
}

/// Replace secrets, addresses, home and cron names and installer-made volume group names, then each local word. A
/// word matches between any two non-alphanumerics, `_` included, so it's caught inside LVM names
/// (`/dev/mapper/centos_web01-root`), where device mapper also doubles any `-` in it.
pub fn scrub(text: &str, words: &[String]) -> String {
    let rules: &[(&str, &str)] = &[
        (
            r"(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            "<private key>",
        ),
        // a signed token: three runs of base64 with dots between, the first always starting the same way
        (
            r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
            "<token>",
        ),
        // a name with one of these words anywhere in it (DB_PASSWORD, api-key, mytoken), then its value up to a
        // space or a closing quote, so the JSON around it still parses
        (
            r#"(?i)([A-Za-z0-9_.-]*(?:pass(?:word|wd)?|secret|token|api[_-]?key|auth)[A-Za-z0-9_]*)\s*[:=]\s*[^\s"',;)]+"#,
            "$1=<redacted>",
        ),
        (
            r"\b(?:sk|ghp|gho|github_pat|xox[bpa]|AKIA)[-_A-Za-z0-9]{12,}",
            "<token>",
        ),
        (r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "<ip>"),
        (r"(?i)\b[0-9a-f]{2}(?::[0-9a-f]{2}){5}\b", "<mac>"),
        (
            r"(?i)\b(?:[0-9a-f]{1,4}:){1,6}:(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){0,5})?\b",
            "<ip6>",
        ),
        (r"(?i)\b(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4}\b", "<ip6>"),
        (r"/home/[^/\s\x22]+", "/home/<user>"),
        (r"/var/spool/cron/(crontabs/)?[^/\s\x22]+", "/var/spool/cron/$1<user>"),
        // a file in cron.d is named by whoever put it there
        (r"/etc/cron\.d/[^/\s\x22),;]+", "/etc/cron.d/<file>"),
        // an installer names the volume group after the hostname of the day it ran, which may not be today's:
        // centos_web01, vg_web01, web01-vg (device mapper doubles each dash inside a name)
        (
            r"(/dev/mapper/(?:centos|rhel|cl|almalinux|rl|ol|fedora|vg)_)[A-Za-z0-9]+(?:--[A-Za-z0-9]+)*-",
            "${1}<local>-",
        ),
        (
            r"/dev/mapper/[A-Za-z0-9]+(?:--[A-Za-z0-9]+)*--vg-",
            "/dev/mapper/<local>--vg-",
        ),
    ];
    let mut s = text.to_string();
    for (rx, rep) in rules {
        s = Regex::new(rx).unwrap().replace_all(&s, *rep).into_owned();
    }
    // an address, unless it is a systemd unit with an instance (getty@tty1.service, openvpn@server.service)
    const UNITS: &[&str] = &[
        ".service", ".socket", ".timer", ".mount", ".target", ".path", ".slice", ".scope", ".device", ".swap",
    ];
    s = Regex::new(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
        .unwrap()
        .replace_all(&s, |c: &regex::Captures| {
            if UNITS.iter().any(|u| c[0].ends_with(u)) {
                c[0].to_string()
            } else {
                "<email>".to_string()
            }
        })
        .into_owned();
    for w in words {
        for form in [w.clone(), w.replace('-', "--")] {
            let rx = Regex::new(&format!(
                r"(?i)(^|[^A-Za-z0-9]){}($|[^A-Za-z0-9])",
                regex::escape(&form)
            ))
            .unwrap();
            // twice: a match eats the separator after it, so a word repeated with one separator needs a second pass
            for _ in 0..2 {
                s = rx.replace_all(&s, "${1}<local>${2}").into_owned();
            }
        }
    }
    s
}

pub fn pct(s: &str) -> String {
    let mut o = String::new();
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || b"-_.~".contains(&b) {
            o.push(b as char);
        } else {
            o += &format!("%{b:02X}");
        }
    }
    o
}

fn form_url(title: &str, system: &str, route: &str, details: &str) -> String {
    format!(
        "{REPO_URL}/issues/new?template=box-report.yml&labels=box-report&title={}&system={}&route={}&details={}",
        pct(title),
        pct(system),
        pct(route),
        pct(details)
    )
}

/// The prefilled `box-report.yml` issue link: the whole report when it fits, else the summary and a note to attach
/// the file.
pub fn issue_url(title: &str, system: &str, route: &str, summary: &str, full: &str, file: &str) -> String {
    let u = form_url(title, system, route, full);
    if u.len() <= URL_BUDGET {
        return u;
    }
    form_url(
        title,
        system,
        route,
        &format!("{summary}\n\n(the full report didn't fit in a link: drag {file} into this issue to attach it)"),
    )
}

/// A link short enough to copy out of a terminal by hand: the release and route filled in, and the report attached
/// by the person, who has to carry the file to a machine with a browser anyway.
pub fn attach_url(title: &str, system: &str, route: &str, file: &str) -> String {
    form_url(
        title,
        system,
        route,
        &format!("(drag {file} into this box to attach it)"),
    )
}

fn fence(body: &str) -> String {
    format!("```\n{}\n```", body.trim_end())
}

/// One route the machine was judged against.
pub struct Judged {
    pub name: String,
    pub family: String,
    /// The rung it joins at and the release the route starts from, when it joins partway.
    pub joins: Option<(String, String)>,
    /// The hops it would walk.
    pub hops: Vec<String>,
    /// Preflight's verdict (`hardware::evaluate`).
    pub verdict: Value,
}

/// Judge the machine against the route asked for, else every route that starts at its release, else the rest of
/// every route that passes through it (a CentOS 6 machine takes the CentOS 5 route from its centos6 rung).
pub fn judge(atlas: Option<&Atlas>, route: Option<&str>, sys: &System, facts: &Value) -> Result<Vec<Judged>, String> {
    let rest = |d: &Value| -> Option<(Value, Option<String>)> {
        let (a, (fam, rel)) = (atlas?, sys.release.as_ref()?);
        if str_of(d, "family").ok() != Some(fam.as_str()) {
            return None;
        }
        crate::check::rest_of_route(a.adapters.get(fam)?, d, rel)
    };
    let routes: Vec<(String, Value, Option<String>)> = match (atlas, route) {
        (Some(a), Some(r)) => {
            let (k, d) = a.route(r)?;
            // a machine the route doesn't pass through is judged from the route's start, as asked
            let (d, joins) = rest(d).unwrap_or_else(|| (d.clone(), None));
            vec![(k.clone(), d, joins)]
        }
        (Some(a), None) => {
            let all: Vec<(String, Value, Option<String>)> = a
                .routes
                .iter()
                .filter_map(|(k, d)| rest(d).map(|(d, j)| (k.clone(), d, j)))
                .collect();
            if all.iter().any(|r| r.2.is_none()) {
                all.into_iter().filter(|r| r.2.is_none()).collect()
            } else {
                all
            }
        }
        (None, Some(r)) => {
            return Err(format!(
                "no atlas here to find route {r:?} in (run it from the wend folder)"
            ))
        }
        (None, None) => vec![],
    };
    let mut judged = vec![];
    for (path, doc, joins) in routes {
        let start = atlas
            .and_then(|a| a.routes.get(&path))
            .and_then(|d| get(d, "from"))
            .and_then(Value::as_str)
            .unwrap_or("?")
            .to_string();
        judged.push(Judged {
            name: Path::new(&path)
                .file_stem()
                .map_or(path.clone(), |s| s.to_string_lossy().into_owned()),
            family: str_of(&doc, "family").unwrap_or("?").to_string(),
            joins: joins.map(|rung| (rung, start)),
            hops: crate::atlas::strings(&doc["hops"]).unwrap_or_default(),
            // routes is empty without an atlas, so one is here
            verdict: crate::hardware::evaluate(atlas.unwrap(), &doc, facts)?,
        });
    }
    Ok(judged)
}

/// The report for routes already judged: (summary, full report, title, route name). Scrubbed.
pub fn render(
    sys: &System,
    kernel: &str,
    facts: &Value,
    judged: &[Judged],
    words: &[String],
    atlas: bool,
) -> (String, String, String, String) {
    let f = facts;
    let level = crate::hardware::cpu_level(&crate::atlas::strings(&f["cpu"]["features"]).unwrap_or_default());
    let bd = &f["boot_dir"];
    let root_fs = f["mounts"]
        .as_array()
        .into_iter()
        .flatten()
        .find(|m| m["mount"] == "/")
        .map(|m| {
            let mut t = m["fstype"].as_str().unwrap_or("?").to_string();
            if m["xfs_v4"] == true {
                t += " (XFS v4)";
            }
            t
        })
        .unwrap_or_else(|| "?".into());
    let py2 = f["runtimes"].as_array().map_or(0, Vec::len);
    let mut md = vec![
        format!("### Box report: {}", sys.pretty),
        String::new(),
        format!(
            "- release: {} (kernel {kernel}, {})",
            sys.pretty,
            f["arch"].as_str().unwrap_or("?")
        ),
        format!(
            "- CPU: {} (x86-64 level {level}{})",
            f["cpu"]["model"].as_str().unwrap_or("?"),
            if f["cpu"]["hypervisor"] == true { ", a VM" } else { "" }
        ),
        format!(
            "- firmware: {}, RAM {} MiB, free on / {} MiB, root filesystem {root_fs}",
            f["boot"].as_str().unwrap_or("?"),
            f["ram_mib"],
            f["free_mib"]
        ),
        format!(
            "- /boot: {}, {} MiB free of {}, {} kernel(s)",
            if bd["separate"] == true {
                "its own filesystem"
            } else {
                "on /"
            },
            bd["free_mib"],
            bd["total_mib"],
            bd["kernels"].as_array().map_or(0, Vec::len)
        ),
        format!("- Python 2 programs of its own: {py2}"),
        format!(
            "- program trees no package owns: {}; Python 2 module directories with modules no package owns: {}",
            f["native"].as_array().map_or(0, Vec::len),
            f["python2_modules"].as_array().map_or(0, Vec::len)
        ),
        format!("- wend {} (`wend share`)", env!("CARGO_PKG_VERSION")),
        String::new(),
    ];
    let mut route_names = vec![];
    let mut verdicts = vec![];
    let mut headlines = vec![];
    for j in judged {
        let (name, v) = (&j.name, &j.verdict);
        let line = if v["ok"] == true {
            format!(
                "**{name}**: preflight passes, it can land on {}",
                v["to"].as_str().unwrap_or("?")
            )
        } else {
            match v["lands_on"].as_str() {
                Some(l) => format!("**{name}**: preflight stops it at {l}"),
                None => format!("**{name}**: preflight refuses it before the first hop"),
            }
        };
        headlines.push(line.clone());
        verdicts.push(line);
        verdicts.push(String::new());
        if let Some((rung, start)) = &j.joins {
            let joined = format!(
                "It joins this route at its {rung} rung, so it walks only {}. Those hops are proven on machines \
                 that walked up from {start}, not yet on one installed as {rung}.",
                j.hops.join(", ")
            );
            headlines.push(joined.clone());
            verdicts.push(joined);
            verdicts.push(String::new());
        }
        verdicts.push(fence(&crate::hardware::report(v)));
        verdicts.push(String::new());
        route_names.push(name.clone());
    }
    if judged.is_empty() {
        md.push(match &sys.release {
            _ if !atlas => "wend was started without its atlas (the folder of routes it came with), so this report \
                            has the machine's facts and no verdict."
                .into(),
            Some((fam, rel)) => format!(
                "No route starts at or passes through {fam} {rel} yet. This report still says what's out there."
            ),
            None => "wend doesn't know this release yet. This report still says what's out there.".into(),
        });
        md.push(String::new());
    }
    let summary = scrub(&format!("{}\n{}", md.join("\n"), headlines.join("\n")), words);
    let facts_text = serde_json::to_string_pretty(f).unwrap();
    let full = scrub(
        &format!(
            "{}\n{}\n<details><summary>wend facts</summary>\n\n```json\n{facts_text}\n```\n</details>\n",
            md.join("\n"),
            verdicts.join("\n")
        ),
        words,
    );
    let title = scrub(&format!("box: {}", sys.pretty), words);
    (summary, full, title, route_names.join(", "))
}

/// Scrub the sentences in a verdict that name things on the machine: what refuses it, and each hop's problems and
/// notes. The release ids beside them are left alone: they are the atlas's words, not the machine's.
fn scrub_findings(verdict: &mut Value, words: &[String]) {
    let clean = |list: &mut Value| {
        for item in list.as_array_mut().into_iter().flatten() {
            if let Some(text) = item.as_str() {
                *item = Value::String(scrub(text, words));
            }
        }
    };
    clean(&mut verdict["machine"]);
    for hop in verdict["hops"].as_array_mut().into_iter().flatten() {
        for key in ["problems", "runtime_problems", "notes"] {
            clean(&mut hop[key]);
        }
    }
}

/// A report made and saved: what `wend share` prints, and what the guided first run (`guide`) explains.
pub struct Shared {
    pub sys: System,
    pub facts: Value,
    pub judged: Vec<Judged>,
    pub summary: String,
    pub full: String,
    pub title: String,
    /// The routes it was judged against, by name ("none" when there are none).
    pub routes: String,
    /// The release in the machine's own words, scrubbed.
    pub sys_line: String,
    /// The file the report was saved to, in the current directory.
    pub file: String,
}

/// Read the machine, judge it, scrub the report and save it. Read-only but for that one file. Root needed.
pub fn prepare(atlas: Option<&Atlas>, route: Option<&str>, extra: &[String]) -> Result<Shared, String> {
    let facts = crate::hardware::gather()?;
    let root = Path::new("/");
    let sys = system(root);
    let mut uts: libc::utsname = unsafe { std::mem::zeroed() };
    // SAFETY: uname fills the struct it is given and NUL-terminates its fields.
    let (node, kernel) = if unsafe { libc::uname(&mut uts) } == 0 {
        let s = |p: *const libc::c_char| unsafe { std::ffi::CStr::from_ptr(p) }.to_string_lossy().into_owned();
        (s(uts.nodename.as_ptr()), s(uts.release.as_ptr()))
    } else {
        (String::new(), "?".into())
    };
    let words = local_words(root, &node, extra);
    let mut judged = judge(atlas, route, &sys, &facts)?;
    // what preflight found is shown on screen as well as written in the report: both get it scrubbed
    for j in &mut judged {
        scrub_findings(&mut j.verdict, &words);
    }
    let (summary, full, title, routes) = render(&sys, &kernel, &facts, &judged, &words, atlas.is_some());
    let key: String = Sha256::digest(full.as_bytes())[..6]
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    let file = format!("wend-box-{key}.md");
    fs::write(&file, &full).map_err(|e| {
        format!(
            "the report could not be saved in this folder ({file}: {e}). Start wend from a folder you can write to."
        )
    })?;
    Ok(Shared {
        sys_line: scrub(&sys.pretty, &words),
        routes: match (routes.is_empty(), atlas.is_some()) {
            (false, _) => routes,
            (true, true) => "none".into(),
            (true, false) => "not judged (no atlas)".into(),
        },
        sys,
        facts,
        judged,
        summary,
        full,
        title,
        file,
    })
}

/// `wend share [ROUTE] [--redact WORD]...`, on the machine, as root.
pub fn cmd_share(atlas: Option<&Atlas>, route: Option<&str>, extra: &[String]) -> Result<bool, String> {
    let r = prepare(atlas, route, extra)?;
    let (full, file) = (&r.full, &r.file);
    let bar = "=".repeat(72);
    println!("{bar}\n{full}{bar}");
    println!(
        "That is the whole report, also saved to {file}. The hostname, local users, home directories, IPs, MACs, \
         emails and anything that looked like a secret were replaced. Read it; if anything else is private, run \
         again with --redact WORD.\n\nNothing has been sent. To share it, open this link in a browser and submit \
         the issue:\n"
    );
    println!(
        "{}",
        issue_url(&r.title, &r.sys_line, &r.routes, &r.summary, full, file)
    );
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The report from facts already gathered, as `prepare` makes it: (summary, full report, title, route name).
    fn build(
        atlas: Option<&Atlas>,
        route: Option<&str>,
        sys: &System,
        kernel: &str,
        facts: &Value,
        words: &[String],
    ) -> Result<(String, String, String, String), String> {
        Ok(render(
            sys,
            kernel,
            facts,
            &judge(atlas, route, sys, facts)?,
            words,
            atlas.is_some(),
        ))
    }

    fn tree(files: &[(&str, &str)]) -> std::path::PathBuf {
        static N: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
        let n = N.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        let d = std::env::temp_dir().join(format!("wend-share-{}-{n}", std::process::id()));
        let _ = crate::fsx::remove_dir_all(&d);
        for (p, c) in files {
            let f = d.join(p);
            fs::create_dir_all(f.parent().unwrap()).unwrap();
            fs::write(f, c).unwrap();
        }
        d
    }

    #[test]
    fn names_the_release() {
        let c7 = tree(&[("etc/redhat-release", "CentOS Linux release 7.9.2009 (Core)\n")]);
        let s = system(&c7);
        assert_eq!(s.release, Some(("el".into(), "centos7".into())));
        assert_eq!(s.pretty, "CentOS Linux release 7.9.2009 (Core)");
        let c5 = tree(&[("etc/redhat-release", "CentOS release 5.11 (Final)\n")]);
        assert_eq!(system(&c5).release, Some(("el".into(), "centos5".into())));
        let alma9 = tree(&[("etc/redhat-release", "AlmaLinux release 9.4 (Seafoam Ocelot)\n")]);
        assert_eq!(system(&alma9).release, Some(("el".into(), "alma9".into())));
        let stream = tree(&[("etc/redhat-release", "CentOS Stream release 8\n")]);
        assert_eq!(system(&stream).release, None);
        let trusty = tree(&[
            (
                "etc/lsb-release",
                "DISTRIB_ID=Ubuntu\nDISTRIB_CODENAME=trusty\nDISTRIB_DESCRIPTION=\"Ubuntu 14.04.6 LTS\"\n",
            ),
            ("etc/debian_version", "jessie/sid\n"),
        ]);
        let s = system(&trusty);
        assert_eq!(s.release, Some(("ubuntu".into(), "trusty".into())));
        assert_eq!(s.pretty, "Ubuntu 14.04.6 LTS");
        let etch = tree(&[("etc/debian_version", "4.0\n")]);
        assert_eq!(system(&etch).release, Some(("debian".into(), "etch".into())));
    }

    #[test]
    fn scrubs_what_identifies_the_machine() {
        let root = tree(&[
            ("etc/passwd", "root:x:0:0::/root:/bin/bash\nalice:x:1000:1000::/home/alice:/bin/bash\ncarol:x:500:500::/home/carol:/bin/bash\nnobody:x:65534:65534::/:/sbin/nologin\n"),
            ("etc/hostname", "web-01.acme.example\n"),
        ]);
        let words = local_words(&root, "web-01.acme.example", &["billing".into()]);
        assert!(words.contains(&"web-01".to_string()) && words.contains(&"alice".to_string()));
        assert!(
            words.contains(&"carol".to_string()),
            "EL5's first login account is uid 500"
        );
        assert!(!words.contains(&"nobody".to_string()) && !words.contains(&"root".to_string()));
        let s = scrub(
            "/dev/mapper/centos_web--01-root on / ; /srv/billing/app.py ; /home/bob/x.py ; /var/spool/cron/alice ; \
             10.1.2.3 ; fe80::5054:ff:fe12:3456 ; 2001:db8::1 ; ops@acme.example ; 52:54:00:12:34:56 ; password=hunter2 ; web-01.acme.example ; ALICE",
            &words,
        );
        for gone in [
            "web--01", "web-01", "billing", "bob", "alice", "ALICE", "10.1.2.3", "fe80", "db8", "ops@", "52:54",
            "hunter2",
        ] {
            assert!(!s.contains(gone), "{gone} survived: {s}");
        }
        assert!(s.contains("/dev/mapper/centos_<local>-root"), "{s}");
        assert!(s.contains("/var/spool/cron/<user>"), "{s}");
    }

    fn sys(fam: &str, rel: &str) -> System {
        System {
            pretty: format!("{fam} {rel}"),
            release: Some((fam.into(), rel.into())),
        }
    }

    #[test]
    fn a_machine_partway_along_a_route_takes_the_rest_of_it() {
        let a = Atlas::load(&Path::new(env!("CARGO_MANIFEST_DIR")).join("..")).unwrap();
        let f = serde_json::json!({"schema": "wend.hardware/4", "arch": "x86_64", "userland_bits": 64,
            "cpu": {"model": "test", "features": ["cx16", "lahf_lm", "popcnt", "sse4_1", "sse4_2", "ssse3", "sse3"],
                    "hypervisor": true},
            "boot": "bios", "ram_mib": 4096, "free_mib": 40000,
            "boot_dir": {"separate": false, "total_mib": 40000, "free_mib": 40000, "kernels": []},
            "blacklisted": [], "pci": [], "runtimes": [], "python2_modules": [], "native": [], "kernel_owner": "kernel",
            "repo_packages": [], "mounts": []});
        let report =
            |fam: &str, rel: &str, route: Option<&str>| build(Some(&a), route, &sys(fam, rel), "k", &f, &[]).unwrap();
        // CentOS 6: no route starts there, the CentOS 4 and CentOS 5 ones pass through
        let (summary, _, _, names) = report("el", "centos6", None);
        assert_eq!(names, "centos4-to-alma10, centos5-to-alma10");
        assert!(
            summary.contains("joins this route at its centos6 rung, so it walks only centos7, alma9, alma10"),
            "{summary}"
        );
        assert!(summary.contains("walked up from centos5"), "{summary}");
        // the same, asked for by name
        let (summary, _, _, _) = report("el", "centos6", Some("centos5-to-alma10"));
        assert!(summary.contains("only centos7, alma9, alma10"), "{summary}");
        // CentOS 7: its own route only, not the rest of CentOS 5's
        let (summary, _, _, names) = report("el", "centos7", None);
        assert_eq!(names, "centos7-to-alma10");
        assert!(!summary.contains("joins this route"), "{summary}");
        // Scientific Linux 7 joins the ladder at centos7, so the same
        assert_eq!(report("el", "sl7", None).3, "centos7-to-alma10");
        // Debian 10 and Ubuntu 18.04
        let (summary, _, _, names) = report("debian", "buster", None);
        assert_eq!(names, "etch-to-trixie");
        assert!(summary.contains("only bullseye, bookworm, trixie"), "{summary}");
        assert_eq!(report("ubuntu", "bionic", None).3, "trusty-to-resolute");
        // from the machine's own release files: AlmaLinux 9 has only the hop to alma10 left
        let alma9 = system(&tree(&[(
            "etc/redhat-release",
            "AlmaLinux release 9.4 (Seafoam Ocelot)\n",
        )]));
        let (summary, _, _, names) = build(Some(&a), None, &alma9, "k", &f, &[]).unwrap();
        assert_eq!(
            names,
            "centos4-to-alma10, centos5-to-alma10, centos7-to-alma10, centos8-to-alma10"
        );
        assert!(summary.contains("so it walks only alma10"), "{summary}");
        let c6 = system(&tree(&[("etc/redhat-release", "CentOS release 6.10 (Final)\n")]));
        assert_eq!(
            build(Some(&a), None, &c6, "k", &f, &[]).unwrap().3,
            "centos4-to-alma10, centos5-to-alma10"
        );
        // at the end of every route: nothing left
        let (summary, _, _, names) = report("el", "alma10", None);
        assert_eq!(names, "");
        assert!(
            summary.contains("No route starts at or passes through el alma10"),
            "{summary}"
        );
    }

    #[test]
    fn long_reports_attach_instead() {
        let short = issue_url("box: x", "x", "none", "sum", "full", "f.md");
        assert!(short.contains("details=full") && short.contains("template=box-report.yml"));
        let long = issue_url("box: x", "x", "none", "sum", &"y".repeat(8000), "f.md");
        assert!(long.len() <= URL_BUDGET && long.contains("details=sum") && long.contains("f.md"));
        assert_eq!(pct("a b/é"), "a%20b%2F%C3%A9");
    }

    #[test]
    fn the_short_link_carries_the_form_and_none_of_the_report() {
        let u = attach_url(
            "box: CentOS Linux release 7.9.2009 (Core)",
            "CentOS Linux release 7.9.2009 (Core)",
            "centos7-to-alma10",
            "wend-box-0123456789ab.md",
        );
        assert!(
            u.starts_with(
                "https://github.com/FieldmouseWorks/wend/issues/new?template=box-report.yml&labels=box-report&title="
            ),
            "{u}"
        );
        assert!(u.contains("&route=centos7-to-alma10&details="), "{u}");
        assert!(u.contains("wend-box-0123456789ab.md"), "{u}");
        // short enough to copy out of a terminal by hand: a few wrapped lines, not a screenful
        assert!(u.len() < 400, "{} characters", u.len());
    }

    #[test]
    fn scrubs_the_names_a_first_stranger_test_found_surviving() {
        let root = tree(&[
            (
                "etc/passwd",
                "root:x:0:0::/root:/bin/bash\nldapgandalf:x:120001:120001::/data/ldapusers/ldapgandalf:/bin/bash\n\
                 zx:x:1001:1001::/srv/people/zx:/bin/bash\nnobody:x:65534:65534::/:/sbin/nologin\n\
                 nfsnobody:x:4294967294:4294967294::/:/sbin/nologin\n",
            ),
            ("etc/hostname", "strangelove-db01.corp.zylkodomain.example\n"),
        ]);
        let words = local_words(&root, "strangelove-db01.corp.zylkodomain.example", &[]);
        let s = scrub(
            "/data/ldapusers/ldapgandalf/app.py ; /srv/people/zx/tool.py ; /dev/mapper/vg_strangelovedb01-lv_app ; \
             /srv/corp.zylkodomain.example/sync.py ; /srv/zylkodomain.example/x ; /srv/zylkodomain/y ; \
             /dev/mapper/centos_oldhost--mx7-home ; /dev/mapper/oldbox--vg-root ; \
             (cron, /etc/cron.d/zylkocron-billing) ; nobody ; nfsnobody",
            &words,
        );
        for gone in [
            // a login account above 65534 (LDAP ranges start there), and a two-letter one
            "ldapgandalf",
            "/zx/",
            // the hostname as an installer writes it into a volume group: dashes dropped
            "strangelovedb01",
            // the domain alone, at each level
            "zylkodomain",
            // a volume group named after a hostname the machine no longer has
            "oldhost",
            "mx7",
            "oldbox",
            // a cron.d file is named by whoever wrote it
            "zylkocron",
        ] {
            assert!(!s.contains(gone), "{gone} survived: {s}");
        }
        assert!(s.contains("/dev/mapper/vg_<local>-lv_app"), "{s}");
        assert!(s.contains("/dev/mapper/centos_<local>-home"), "{s}");
        assert!(s.contains("/dev/mapper/<local>--vg-root"), "{s}");
        assert!(s.contains("(cron, /etc/cron.d/<file>)"), "{s}");
        // the accounts every machine has are not names of this one
        assert!(s.ends_with("; nobody ; nfsnobody"), "{s}");
        assert!(!words.iter().any(|w| w == "example" || w == "corp"), "{words:?}");
    }

    #[test]
    fn scrubs_the_domain_whatever_its_shape() {
        for (host, text, gone) in [
            // a name with no public suffix at all
            (
                "web01.acmecorp",
                "/srv/acmecorp/app.py /srv/web01/x",
                vec!["acmecorp", "web01"],
            ),
            // a suffix of two parts: the name before it stands alone too
            (
                "web01.acme.co.uk",
                "/srv/acme/app.py /srv/acme.co.uk/x mail.acme.co.uk",
                vec!["acme"],
            ),
            (
                "db.internal.zylko.example",
                "/srv/zylko/a /srv/internal.zylko.example/b",
                vec!["zylko"],
            ),
        ] {
            let root = tree(&[("etc/hostname", &format!("{host}\n")[..])]);
            let words = local_words(&root, host, &[]);
            let s = scrub(text, &words);
            for g in gone {
                assert!(!s.contains(g), "{host}: {g} survived: {s} (words {words:?})");
            }
            // the words every name is made of are nobody's
            for kept in ["co", "uk", "internal", "example", "local", "com"] {
                assert!(!words.iter().any(|w| w == kept), "{host}: {kept} is a word: {words:?}");
            }
        }
        // a suffix word in a path is left alone
        let root = tree(&[("etc/hostname", "web01.example.com\n")]);
        let words = local_words(&root, "web01.example.com", &[]);
        assert_eq!(
            scrub("/usr/local/com/internal/example", &words),
            "/usr/local/com/internal/example"
        );
    }

    #[test]
    fn scrubs_secrets_however_they_are_named_and_leaves_what_follows_them() {
        let s = scrub(
            r#"{"a": "/srv/x/DB_PASSWORD=EnvPathPl4nted/run.py", "b": "password=EqPathPl4nted", "c": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U", "d": "mytoken: abc123"}"#,
            &[],
        );
        for gone in ["EnvPathPl4nted", "EqPathPl4nted", "eyJhbGci", "abc123"] {
            assert!(!s.contains(gone), "{gone} survived: {s}");
        }
        // a value ends at its closing quote: the report's facts are still JSON
        assert!(serde_json::from_str::<Value>(&s).is_ok(), "{s}");
    }

    #[test]
    fn a_unit_with_an_at_sign_is_not_an_email() {
        let s = scrub(
            "(serial-getty@ttyS0.service) (openvpn@server.service) (postgresql@12-main.service) a@b.timer ops@acme.example",
            &[],
        );
        assert!(
            s.starts_with(
                "(serial-getty@ttyS0.service) (openvpn@server.service) (postgresql@12-main.service) a@b.timer "
            ),
            "{s}"
        );
        assert!(s.ends_with(" <email>"), "{s}");
    }

    #[test]
    fn names_rebuilds_that_are_not_centos() {
        let sl7 = tree(&[("etc/redhat-release", "Scientific Linux release 7.9 (Nitrogen)\n")]);
        assert_eq!(system(&sl7).release, Some(("el".into(), "sl7".into())));
        // Oracle Linux's redhat-release says Red Hat, for the programs that look there; oracle-release says what it is
        let ol7 = tree(&[
            (
                "etc/redhat-release",
                "Red Hat Enterprise Linux Server release 7.9 (Maipo)\n",
            ),
            ("etc/oracle-release", "Oracle Linux Server release 7.9\n"),
        ]);
        let s = system(&ol7);
        assert_eq!(
            (s.pretty.as_str(), s.release),
            ("Oracle Linux Server release 7.9", None)
        );
    }

    #[test]
    fn a_report_made_without_the_atlas_says_so() {
        let sys = System {
            pretty: "CentOS Linux release 7.9.2009 (Core)".into(),
            release: Some(("el".into(), "centos7".into())),
        };
        let f = serde_json::json!({"arch": "x86_64", "cpu": {"model": "test", "features": []}, "boot": "bios",
            "ram_mib": 4096, "free_mib": 40000, "boot_dir": {"separate": false, "total_mib": 1, "free_mib": 1, "kernels": []},
            "mounts": [], "runtimes": [], "native": [], "python2_modules": []});
        let (summary, full, _, _) = render(&sys, "k", &f, &[], &[], false);
        assert!(full.contains("wend was started without its atlas"), "{full}");
        assert!(
            !full.contains("No route starts") && !summary.contains("No route starts"),
            "{full}"
        );
        // with the atlas and no route, it still says there is none
        let (_, full, _, _) = render(&sys, "k", &f, &[], &[], true);
        assert!(
            full.contains("No route starts at or passes through el centos7 yet"),
            "{full}"
        );
    }
}
