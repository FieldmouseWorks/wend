//! The first run: `wend` with nothing after it, at a terminal. It says what it is about to do, waits for the person,
//! makes the read-only check `wend share` makes, and says what it found and what to do with the report, in plain
//! words. Everything it shows comes from `share::prepare`: the same facts, the same verdict, the same saved report.
//!
//! This file is what it says, and the plain way of saying it: prompts and text, 80 columns, ASCII, no colour, for a
//! console that can do no more (or `--plain`). `screens` shows the same words full-screen where the terminal can.

use crate::atlas::{str_of, Atlas};
use crate::share::{self, Judged, Shared, System, DEBIAN_CODENAMES};
use serde_json::Value;
use std::io::{BufRead, Write};

/// The widest a line of text gets: what fits inside a frame on an 80-column terminal.
pub const WIDTH: usize = 76;
pub const NAME: &str = concat!("wend ", env!("CARGO_PKG_VERSION"));
pub const TAGLINE: &str = "proven upgrade paths for old Linux servers";
const TITLE: &str = concat!(
    "wend ",
    env!("CARGO_PKG_VERSION"),
    ": proven upgrade paths for old Linux servers"
);

/// A release id as people say it: centos7 is "CentOS 7", trixie is "Debian 13 (trixie)". An id it doesn't know
/// prints as it is.
pub fn release_name(family: &str, id: &str) -> String {
    const UBUNTU: &[(&str, &str)] = &[
        ("trusty", "14.04"),
        ("xenial", "16.04"),
        ("bionic", "18.04"),
        ("focal", "20.04"),
        ("jammy", "22.04"),
        ("noble", "24.04"),
        ("resolute", "26.04"),
    ];
    let numbered = |prefix: &str, name: &str| {
        id.strip_prefix(prefix)
            .filter(|n| !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit()))
            .map(|n| format!("{name} {n}"))
    };
    let known = match family {
        "el" => numbered("centos", "CentOS")
            .or_else(|| numbered("alma", "AlmaLinux"))
            .or_else(|| numbered("sl", "Scientific Linux")),
        "debian" => DEBIAN_CODENAMES
            .iter()
            .position(|c| !c.is_empty() && *c == id)
            .map(|n| format!("Debian {n} ({id})")),
        "ubuntu" => UBUNTU
            .iter()
            .find(|(c, _)| *c == id)
            .map(|(_, v)| format!("Ubuntu {v} ({id})")),
        _ => None,
    };
    known.unwrap_or_else(|| id.to_string())
}

/// `text` wrapped to the screen, every line behind `indent`.
pub fn wrap(text: &str, indent: &str) -> String {
    let mut out = String::new();
    for para in text.split('\n') {
        let mut line = String::new();
        for word in para.split_whitespace() {
            if !line.is_empty() && indent.len() + line.len() + 1 + word.len() > WIDTH {
                out += &format!("{indent}{line}\n");
                line.clear();
            }
            if !line.is_empty() {
                line.push(' ');
            }
            line += word;
        }
        out += &format!("{indent}{line}\n");
    }
    out
}

/// One thing in a list, wrapped, its later lines under its first word.
fn item(text: &str) -> String {
    let body = wrap(text, "      ");
    format!("    - {}", &body[6.min(body.len())..])
}

/// One numbered step, wrapped, its later lines under its first word.
fn step(n: usize, text: &str) -> String {
    let body = wrap(text, "     ");
    format!("  {n}. {}", &body[5.min(body.len())..])
}

/// The releases a path passes, in order, wrapped at the arrows.
fn path_line(family: &str, from: &str, hops: &[String]) -> String {
    let names: Vec<String> = std::iter::once(from)
        .chain(hops.iter().map(String::as_str))
        .map(|r| release_name(family, r))
        .collect();
    let mut out = String::new();
    let mut line = String::new();
    for (i, n) in names.iter().enumerate() {
        let piece = if i == 0 { n.clone() } else { format!(" -> {n}") };
        if !line.is_empty() && 6 + line.len() + piece.len() > WIDTH {
            out += &format!("      {line}\n");
            line = format!("  {}", piece.trim_start());
        } else {
            line += &piece;
        }
    }
    out + &format!("      {line}\n")
}

/// Every path the atlas has, start to end, for the screens that have to say "not this machine, yet".
pub fn paths(atlas: &Atlas) -> Vec<String> {
    let mut all: Vec<String> = atlas
        .routes
        .values()
        .filter_map(|d| {
            let fam = str_of(d, "family").ok()?;
            Some(format!(
                "{} to {}",
                release_name(fam, str_of(d, "from").ok()?),
                release_name(fam, str_of(d, "to").ok()?)
            ))
        })
        .collect();
    all.sort();
    all
}

fn strings(v: &Value) -> Vec<&str> {
    v.as_array().into_iter().flatten().filter_map(Value::as_str).collect()
}

/// How far along its path a machine could go with everything on it still working: the release, and whether what
/// stops it there is its own programs (the route leaves behind what they run on) and not the machine. None when
/// it can't set out at all.
fn reach(v: &Value) -> Option<(&str, bool)> {
    let lands = v["lands_on"].as_str()?;
    if !strings(&v["machine"]).is_empty() {
        return None;
    }
    let order: Vec<&str> = v["from"]
        .as_str()
        .into_iter()
        .chain(
            v["hops"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(|h| h["to"].as_str()),
        )
        .collect();
    let at = |r: &str| order.iter().position(|x| *x == r);
    match v["keeps_runtimes_on"].as_str() {
        Some(keeps) if at(keeps) < at(lands) => Some((keeps, true)),
        _ => Some((lands, false)),
    }
}

/// What preflight found for one path, as an answer to "can wend upgrade it?".
fn answer(j: &Judged) -> String {
    let (v, fam) = (&j.verdict, j.family.as_str());
    let name = |r: &str| release_name(fam, r);
    let (from, to) = (v["from"].as_str().unwrap_or("?"), v["to"].as_str().unwrap_or("?"));
    let hops: Vec<String> = v["hops"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|h| h["to"].as_str().map(String::from))
        .collect();
    let path = format!(
        "  wend has an upgrade path from here to {}:\n\n{}\n",
        name(to),
        path_line(fam, from, &hops)
    );
    let mut s = String::new();
    let mut programs = false;
    if v["ok"] == true {
        s += "  Yes, as far as this check can tell.\n\n";
        s += &path;
        s += &wrap(
            "It was proven on test servers, and nothing found on this machine would stop it: its processor, memory, \
             disk space and drivers all fit.",
            "  ",
        );
    } else {
        let stops = match reach(v) {
            Some((r, own)) if r != from => {
                programs = own;
                s += "  Part of the way.\n\n";
                format!(
                    "This machine could follow it as far as {}{}. What stops it after that:",
                    name(r),
                    if own {
                        " with everything on it still working"
                    } else {
                        ""
                    }
                )
            }
            Some((_, true)) => {
                programs = true;
                s += "  Not as it stands.\n\n";
                "The machine itself could take it, but programs of your own on it would stop working at the first \
                 step:"
                    .to_string()
            }
            _ => {
                s += "  Not as it stands.\n\n";
                "This machine could not take the first step of it. What stands in the way:".to_string()
            }
        };
        s += &path;
        s += &wrap(&stops, "  ");
        s.push('\n');
        for p in strings(&v["machine"]) {
            s += &item(p);
        }
        // the first step that breaks is the answer to "what stops it"; the report has the rest
        let broken: Vec<(&str, Vec<&str>)> = v["hops"]
            .as_array()
            .into_iter()
            .flatten()
            .map(|h| {
                (
                    h["to"].as_str().unwrap_or("?"),
                    strings(&h["problems"])
                        .into_iter()
                        .chain(strings(&h["runtime_problems"]))
                        .collect::<Vec<_>>(),
                )
            })
            .filter(|(_, found)| !found.is_empty())
            .collect();
        if let Some((hop, found)) = broken.first() {
            s += &format!("    At {}:\n", name(hop));
            for p in found {
                s += &item(p);
            }
        }
        s.push('\n');
        if broken.len() > 1 {
            s += "  Later steps have problems of their own. The report lists them all.\n";
        }
        s += &wrap(
            if programs {
                "None of that was changed or attempted. The report says what your choices are."
            } else {
                "None of that was changed or attempted. The report has the detail."
            },
            "  ",
        );
    }
    if let Some((rung, start)) = &j.joins {
        s.push('\n');
        s += &wrap(
            &format!(
                "This machine joins the path partway, at {}. Those steps were proven on machines that started at {}, \
                 not yet on one installed as {}.",
                name(rung),
                name(start),
                name(rung)
            ),
            "  ",
        );
    }
    let notes: Vec<&str> = v["hops"]
        .as_array()
        .into_iter()
        .flatten()
        .flat_map(|h| strings(&h["notes"]))
        .collect();
    if !notes.is_empty() {
        s += "\n  Worth knowing:\n\n";
        for n in notes {
            s += &item(n);
        }
    }
    s
}

fn memory(mib: u64) -> String {
    if mib < 1000 {
        format!("{mib} MB")
    } else {
        format!("{:.1} GB", mib as f64 / 1024.0)
    }
}

/// How the answer to "can wend upgrade it?" turned out, for the one thing a screen sets apart by colour.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Outcome {
    Yes,
    Partway,
    No,
    /// No path for this release, or none could be looked up.
    NoPath,
}

pub fn outcome(judged: &[Judged]) -> Outcome {
    let Some(v) = judged.first().map(|j| &j.verdict) else {
        return Outcome::NoPath;
    };
    match reach(v) {
        _ if v["ok"] == true => Outcome::Yes,
        Some((r, _)) if Some(r) != v["from"].as_str() => Outcome::Partway,
        _ => Outcome::No,
    }
}

/// The machine in two lines: what it runs, and what it is.
pub fn machine(sys: &System, facts: &Value) -> String {
    let f = facts;
    wrap(&sys.pretty, "  ")
        + &wrap(
            &format!(
                "{}, {} of memory, {} firmware{}",
                match f["arch"].as_str() {
                    Some("x86_64") => "64-bit (x86_64)",
                    Some(a) => a,
                    None => "unknown processor type",
                },
                memory(f["ram_mib"].as_u64().unwrap_or(0)),
                match f["boot"].as_str() {
                    Some("uefi") => "UEFI",
                    Some("bios") => "BIOS",
                    _ => "unknown",
                },
                if f["cpu"]["hypervisor"] == true {
                    ", a virtual machine"
                } else {
                    ""
                }
            ),
            "  ",
        )
}

/// The answer to "can wend upgrade it?". Its first line is the answer in a few words.
/// `paths` is every path the atlas has, or None when wend was started without its atlas.
pub fn verdict(sys: &System, judged: &[Judged], paths: Option<&[String]>) -> String {
    let mut s = String::new();
    if judged.is_empty() {
        s += &wrap(
            &match (paths, &sys.release) {
                (None, _) => "wend can't say: it was started without the folder of upgrade paths it came with. Start \
                              it from the folder it was unpacked into. The report still describes the machine."
                    .to_string(),
                (Some(_), Some((fam, rel))) => format!(
                    "Not yet. wend has no upgrade path that starts at {} or passes through it.",
                    release_name(fam, rel)
                ),
                (Some(_), None) => "Not yet. wend doesn't recognize the system this machine runs.".to_string(),
            },
            "  ",
        );
        if let Some(all) = paths {
            s += "\n  The paths it has today:\n\n";
            for p in all {
                s += &item(p);
            }
            s.push('\n');
            s += &wrap(
                "A report from a machine like this one is still worth sending: it is how the next path gets chosen.",
                "  ",
            );
        }
    }
    for (i, j) in judged.iter().enumerate() {
        if i > 0 {
            s.push('\n');
        }
        s += &answer(j);
    }
    s
}

/// What the check did to the machine: nothing, but for the one file at `file`.
pub fn written(file: &str) -> String {
    format!(
        "  Nothing on this machine was changed, and nothing was sent anywhere.\n  One file was written:\n\n      \
         {file}\n\n{}",
        wrap(
            "It is the full report of this check. Before it was saved, the hostname, user names, home directories, \
             network addresses and anything that looked like a password or key were replaced.",
            "  ",
        )
    )
}

/// Why to share the report at `file` and how, down to the link, which the caller shows after it (a link can't be
/// wrapped).
pub fn sharing(file: &str) -> String {
    // a path has no spaces to wrap at: it is cut to the screen instead, so none of it is lost off the edge
    let chars: Vec<char> = file.chars().collect();
    let path: String = chars
        .chunks(WIDTH - 7)
        .map(|piece| format!("       {}\n", piece.iter().collect::<String>()))
        .collect();
    wrap(
        "wend's upgrade paths were proven on test servers the project built. A report from a real machine is how it \
         learns what one looks like, and how a path for a machine like yours gets made or fixed. To share it:",
        "  ",
    ) + "\n"
        + &step(
            1,
            "Read it first. If it names anything private, run \"wend share --redact WORD\" to make a report with that \
             word replaced too.",
        )
        + &step(2, "Copy the file to a computer with a web browser:")
        + &path
        + &step(
            3,
            "Open this link there (it needs a GitHub account), drag the file into the box marked Report, and submit:",
        )
}

/// What the check reads, for someone deciding whether to let it (docs/try-it.md says the same at more length).
pub fn reads() -> String {
    let mut s = String::from("  To judge this machine, wend reads:\n\n");
    for what in [
        "/proc, /sys and /boot: the processor, memory, disks, drivers and kernels",
        "the release files under /etc, to know what the machine runs",
        "/etc/passwd and /etc/hostname, to know which names to take out",
        "the running programs and every crontab, to find programs of your own",
        "the first bytes of each mounted filesystem, to tell which kind it is",
        "the package database, to tell your programs from the distribution's",
    ] {
        s += &item(what);
    }
    s.push('\n');
    s += &wrap(
        "It needs root because a normal user can't see other users' programs and crontabs.",
        "  ",
    );
    s.push('\n');
    s += &wrap(
        "It writes one file, the report, in the folder you started it from. It opens no network connection, and it \
         installs, upgrades, starts and stops nothing.",
        "  ",
    );
    s
}

/// The plain screen after the check: the machine, the answer, what was written, and how to share it.
pub fn result(
    sys: &System,
    facts: &Value,
    judged: &[Judged],
    paths: Option<&[String]>,
    file: &str,
    link: &str,
) -> String {
    format!(
        "THIS MACHINE\n{}\nCAN WEND UPGRADE IT?\n{}\nWHAT HAPPENED\n{}\nSHARING IT (your choice)\n{}\n{link}\n",
        machine(sys, facts),
        verdict(sys, judged, paths),
        written(file),
        sharing(file)
    )
}

/// The three promises the first step makes, said before it reads anything.
pub const PROMISES: [&str; 3] = [
    "It changes nothing on this machine.",
    "It sends nothing anywhere and opens no network connection.",
    "It writes one file in this folder: a report for you to read.",
];
pub const FIRST_STEP: &str = "This first step only looks. It works out which release this machine runs, whether wend \
                              has an upgrade path for it, and whether the machine could take that path.";

/// What `wend` says before it reads anything.
pub fn intro() -> String {
    let promises: String = PROMISES.iter().map(|p| format!("  - {p}\n")).collect();
    format!("{TITLE}\n\n{}\n{promises}\n", wrap(FIRST_STEP, ""))
}

/// What `wend` says to someone who isn't root: the one command to run instead.
pub fn needs_root(prog: &str) -> String {
    format!(
        "{TITLE}\n\n{}\nStart it again as root:\n\n    sudo {prog}\n\nIf this machine has no sudo, become root first \
         (su -) and run it from there.\n",
        wrap(
            "The first step is a read-only check of this machine, and it has to run as root: only root can see \
             every program and scheduled job on it.",
            ""
        )
    )
}

/// What `wend` says when nobody is at a terminal to be guided (a pipe, a script, `ssh host wend`).
pub fn brief(prog: &str) -> String {
    format!(
        "{TITLE}\n\nStart here, at a terminal, as root:\n\n    sudo {prog}\n\n{}\nEvery command: {prog} help\n",
        wrap(
            "It explains each step and waits for you before it does anything. The first step is a read-only check \
             of this machine.",
            ""
        )
    )
}

fn ask(prompt: &str) -> Option<String> {
    print!("{prompt}");
    let _ = std::io::stdout().flush();
    let mut line = String::new();
    match std::io::stdin().lock().read_line(&mut line) {
        Ok(n) if n > 0 => Some(line.trim().to_ascii_lowercase()),
        _ => None,
    }
}

/// Show the saved report a screen at a time, or all at once where no pager is installed.
fn read_report(file: &str, text: &str) {
    for pager in ["less", "more"] {
        if std::process::Command::new(pager)
            .arg(file)
            .status()
            .is_ok_and(|s| s.success())
        {
            return;
        }
    }
    println!("{text}");
}

/// A check made: the report, the paths there are, where the file is, and the link that shares it.
pub struct Checked {
    pub report: Shared,
    /// Every path the atlas has, or None when wend was started without its atlas.
    pub paths: Option<Vec<String>>,
    /// The report file, as a full path.
    pub file: String,
    pub link: String,
}

fn check(atlas: Result<Option<Atlas>, String>, redact: &[String]) -> Result<Checked, String> {
    let atlas = atlas?;
    let report = share::prepare(atlas.as_ref(), None, redact)?;
    Ok(Checked {
        paths: atlas.as_ref().map(paths),
        file: std::env::current_dir()
            .map(|d| d.join(&report.file).display().to_string())
            .unwrap_or_else(|_| report.file.clone()),
        link: share::attach_url(&report.title, &report.sys_line, &report.routes, &report.file),
        report,
    })
}

/// What is left on the terminal once the full screen is gone: where the report is and how to share it, to copy
/// from at leisure.
pub fn afterwards(c: &Checked) -> String {
    format!(
        "{NAME}: nothing on this machine was changed, and nothing was sent.\n\nThe report is here:\n\n    {}\n\n{}\n{}\n",
        c.file,
        wrap(
            "To share it (your choice): copy that file to a computer with a web browser, open this link there (it \
             needs a GitHub account), drag the file into the box marked Report, and submit.",
            ""
        ),
        c.link
    )
}

/// `wend`, with nothing after it. `atlas` loads the atlas a report is judged against (none is not an error);
/// `plain` asks for prompts and text even where the terminal could do more.
/// Exit codes: 0 the check was made (whatever it found), 1 it wasn't, 2 nobody was at a terminal.
pub fn start(prog: &str, atlas: impl FnOnce() -> Result<Option<Atlas>, String>, redact: &[String], plain: bool) -> u8 {
    // SAFETY: isatty and geteuid have no preconditions.
    let (terminal, root) = unsafe { (libc::isatty(0) == 1 && libc::isatty(1) == 1, libc::geteuid() == 0) };
    if !terminal {
        print!("{}", brief(prog));
        return 2;
    }
    if !root {
        print!("{}", needs_root(prog));
        return 1;
    }
    let caps = crate::term::caps(&|k| std::env::var(k).ok(), true).filter(|_| !plain && crate::screens::fits());
    if let Some((caps, raw)) = caps.and_then(|c| Some((c, crate::term::Raw::enter()?))) {
        let made = crate::screens::run(raw, caps, || check(atlas(), redact));
        return match made {
            Some(c) => {
                print!("{}", afterwards(&c));
                0
            }
            None => {
                println!("{NAME}: no check was made. Nothing on this machine was read or changed.");
                1
            }
        };
    }
    print!("{}", intro());
    if ask("Press Enter to check this machine, or Ctrl-C to stop: ").is_none() {
        println!("\nStopped. Nothing was read or changed.");
        return 1;
    }
    println!("\nChecking...");
    let c = match check(atlas(), redact) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("wend: the check could not finish: {e}\nNothing on this machine was changed.");
            return 1;
        }
    };
    let r = &c.report;
    print!(
        "\n{}",
        result(&r.sys, &r.facts, &r.judged, c.paths.as_deref(), &c.file, &c.link)
    );
    while ask("\nType r and press Enter to read the report now, or just press Enter to finish: ").as_deref()
        == Some("r")
    {
        read_report(&r.file, &r.full);
    }
    println!("\nDone. wend does nothing more until you start it again.");
    0
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const LINK: &str = "https://github.com/FieldmouseWorks/wend/issues/new?template=box-report.yml&title=x";

    fn machine(pretty: &str, release: Option<(&str, &str)>) -> System {
        System {
            pretty: pretty.into(),
            release: release.map(|(f, r)| (f.to_string(), r.to_string())),
        }
    }

    fn facts() -> Value {
        json!({"arch": "x86_64", "ram_mib": 3936, "boot": "bios", "cpu": {"hypervisor": true}})
    }

    fn judged(family: &str, joins: Option<(&str, &str)>, verdict: Value) -> Judged {
        Judged {
            name: "a-route".into(),
            family: family.into(),
            joins: joins.map(|(r, s)| (r.to_string(), s.to_string())),
            hops: vec![],
            verdict,
        }
    }

    /// What every screen has to hold to: 80 columns (a link can't be wrapped), plain ASCII, no tabs.
    fn fits_an_old_terminal(screen: &str) {
        for line in screen.lines() {
            assert!(line.is_ascii() && !line.contains('\t'), "not plain ASCII: {line:?}");
            assert!(
                line.len() <= 80 || line.starts_with("https://"),
                "{} columns: {line:?}",
                line.len()
            );
        }
    }

    #[test]
    fn names_releases_the_way_people_do() {
        assert_eq!(release_name("el", "centos7"), "CentOS 7");
        assert_eq!(release_name("el", "alma10"), "AlmaLinux 10");
        assert_eq!(release_name("el", "sl7"), "Scientific Linux 7");
        assert_eq!(release_name("debian", "etch"), "Debian 4 (etch)");
        assert_eq!(release_name("debian", "trixie"), "Debian 13 (trixie)");
        assert_eq!(release_name("ubuntu", "trusty"), "Ubuntu 14.04 (trusty)");
        // an id it has no name for prints as it is, and so does a family it doesn't know
        assert_eq!(release_name("el", "rocky9"), "rocky9");
        assert_eq!(release_name("debian", ""), "");
        assert_eq!(release_name("suse", "leap15"), "leap15");
    }

    #[test]
    fn a_machine_that_can_take_the_whole_path_is_told_yes() {
        let v = json!({"ok": true, "from": "centos7", "to": "alma10", "lands_on": "alma10", "machine": [],
            "hops": [{"to": "alma9", "problems": [], "runtime_problems": [], "notes": []},
                     {"to": "alma10", "problems": [], "runtime_problems": [],
                      "notes": ["the running kernel belongs to no package"]}]});
        let s = result(
            &machine("CentOS Linux release 7.9.2009 (Core)", Some(("el", "centos7"))),
            &facts(),
            &[judged("el", None, v)],
            Some(&[]),
            "/root/wend/wend-box-0123456789ab.md",
            LINK,
        );
        fits_an_old_terminal(&s);
        assert!(
            s.contains("  64-bit (x86_64), 3.8 GB of memory, BIOS firmware, a virtual machine\n"),
            "{s}"
        );
        assert!(s.contains("  Yes, as far as this check can tell.\n"), "{s}");
        assert!(s.contains("      CentOS 7 -> AlmaLinux 9 -> AlmaLinux 10\n"), "{s}");
        assert!(s.contains("Worth knowing:"), "{s}");
        assert!(s.contains("    - the running kernel belongs to no package\n"), "{s}");
        assert!(!s.contains("What stops it") && !s.contains("stands in the way"), "{s}");
        // where the report is, that nothing was sent, and the link, whatever the answer
        assert!(s.contains("      /root/wend/wend-box-0123456789ab.md\n"), "{s}");
        assert!(
            s.contains("Nothing on this machine was changed, and nothing was sent anywhere."),
            "{s}"
        );
        assert!(s.ends_with(&format!("\n{LINK}\n")), "{s}");
    }

    #[test]
    fn a_machine_that_stops_partway_is_told_where_and_why() {
        let v = json!({"ok": false, "from": "centos5", "to": "alma10", "lands_on": "centos7", "machine": [],
            "hops": [{"to": "centos6", "problems": [], "runtime_problems": [], "notes": []},
                     {"to": "centos7", "problems": [], "runtime_problems": [], "notes": []},
                     {"to": "alma9", "problems": ["alma9 needs 1536 MiB of memory; this machine has 1024"],
                      "runtime_problems": ["/opt/app/run.py runs on Python 2, which alma9 doesn't ship"],
                      "notes": []},
                     {"to": "alma10", "problems": ["alma10 needs x86-64 level 3; this processor is level 2"],
                      "runtime_problems": [], "notes": []}]});
        let s = result(
            &machine("CentOS release 6.10 (Final)", Some(("el", "centos6"))),
            &facts(),
            &[judged("el", Some(("centos6", "centos5")), v)],
            Some(&[]),
            "wend-box-0123456789ab.md",
            LINK,
        );
        fits_an_old_terminal(&s);
        assert!(s.contains("  Part of the way.\n"), "{s}");
        assert!(s.contains("as far as CentOS 7. What stops it after that:"), "{s}");
        assert!(
            s.contains("    At AlmaLinux 9:\n    - alma9 needs 1536 MiB of memory; this machine has 1024\n"),
            "{s}"
        );
        assert!(
            s.contains("    - /opt/app/run.py runs on Python 2, which alma9 doesn't ship\n"),
            "{s}"
        );
        // the first step that breaks is the answer; what later ones would hit is left to the report, and said to be
        assert!(!s.contains("At AlmaLinux 10:"), "{s}");
        assert!(
            s.contains("  Later steps have problems of their own. The report lists them all.\n"),
            "{s}"
        );
        assert!(
            s.contains("None of that was changed or attempted. The report has the detail."),
            "{s}"
        );
        assert!(s.contains("joins the path partway, at CentOS 6."), "{s}");
        assert!(s.contains("started at CentOS 5"), "{s}");
        assert!(!s.contains("Yes, as far"), "{s}");
    }

    #[test]
    fn a_machine_whose_own_programs_stop_it_is_not_told_it_can_go_all_the_way() {
        // the hardware could land on alma10, but the machine's own Python 2 program can't follow past centos7:
        // "as far as AlmaLinux 10" would be the wrong answer
        let hop = |to: &str| {
            json!({"to": to, "problems": [], "notes": [], "runtime_problems":
                [format!("{to} ships no Python 2, and this machine's own programs run on it: /srv/shop/billing.py")]})
        };
        let from_start = json!({"ok": false, "from": "centos7", "to": "alma10", "lands_on": "alma10",
            "keeps_runtimes_on": "centos7", "machine": [], "hops": [hop("alma9"), hop("alma10")]});
        let j = [judged("el", None, from_start)];
        assert_eq!(outcome(&j), Outcome::No);
        let s = verdict(&machine("x", None), &j, Some(&[]));
        fits_an_old_terminal(&s);
        assert!(s.starts_with("  Not as it stands.\n"), "{s}");
        let said = s.split_whitespace().collect::<Vec<_>>().join(" ");
        assert!(
            said.contains("programs of your own on it would stop working at the first step:"),
            "{s}"
        );
        assert!(s.contains("    At AlmaLinux 9:\n    - alma9 ships no Python 2"), "{s}");
        assert!(
            !s.contains("as far as AlmaLinux 10") && !s.contains("At AlmaLinux 10:"),
            "{s}"
        );
        assert!(said.contains("The report says what your choices are."), "{s}");
        // partway along, it is told how far everything on it still works
        let later = json!({"ok": false, "from": "centos5", "to": "alma10", "lands_on": "alma10",
            "keeps_runtimes_on": "centos7", "machine": [],
            "hops": [{"to": "centos6", "problems": [], "runtime_problems": [], "notes": []},
                     {"to": "centos7", "problems": [], "runtime_problems": [], "notes": []}, hop("alma9"), hop("alma10")]});
        let j = [judged("el", None, later)];
        assert_eq!(outcome(&j), Outcome::Partway);
        let s = verdict(&machine("x", None), &j, Some(&[]));
        fits_an_old_terminal(&s);
        assert!(s.starts_with("  Part of the way.\n"), "{s}");
        let said = s.split_whitespace().collect::<Vec<_>>().join(" ");
        assert!(
            said.contains("as far as CentOS 7 with everything on it still working. What stops it after that:"),
            "{s}"
        );
        // hardware that stops sooner than the programs do is what limits it
        let hardware_first = json!({"ok": false, "from": "centos5", "to": "alma10", "lands_on": "centos6",
            "keeps_runtimes_on": "centos7", "machine": [],
            "hops": [{"to": "centos6", "problems": [], "runtime_problems": [], "notes": []},
                     {"to": "centos7", "problems": ["centos7 needs 1024 MiB of memory"], "runtime_problems": [], "notes": []}]});
        let s = verdict(&machine("x", None), &[judged("el", None, hardware_first)], Some(&[]));
        assert!(s.contains("as far as CentOS 6. What stops it after that:"), "{s}");
        assert!(s.contains("The report has the detail."), "{s}");
    }

    #[test]
    fn a_machine_that_cannot_start_is_told_what_stands_in_the_way() {
        let refused = json!({"ok": false, "from": "centos7", "to": "alma10", "lands_on": null,
            "machine": ["this is a 32-bit userland, and the route's releases are 64-bit only"], "hops": []});
        let first_hop = json!({"ok": false, "from": "trusty", "to": "resolute", "lands_on": "trusty",
            "machine": [], "hops": [{"to": "xenial", "problems": ["/boot has 12 MiB free; the hop needs 180"],
                                     "runtime_problems": [], "notes": []}]});
        for (v, fam, why) in [
            (
                refused,
                "el",
                "    - this is a 32-bit userland, and the route's releases are 64-bit only\n",
            ),
            (
                first_hop,
                "ubuntu",
                "    At Ubuntu 16.04 (xenial):\n    - /boot has 12 MiB free; the hop needs 180\n",
            ),
        ] {
            let s = result(
                &machine("x", None),
                &facts(),
                &[judged(fam, None, v)],
                Some(&[]),
                "f.md",
                LINK,
            );
            fits_an_old_terminal(&s);
            assert!(s.contains("  Not as it stands.\n"), "{s}");
            assert!(
                s.contains("could not take the first step of it. What stands in the way:"),
                "{s}"
            );
            assert!(s.contains(why), "{s}");
        }
    }

    #[test]
    fn a_machine_with_no_path_is_told_which_paths_there_are() {
        let all = vec![
            "CentOS 7 to AlmaLinux 10".to_string(),
            "Debian 4 (etch) to Debian 13 (trixie)".to_string(),
        ];
        let known = result(
            &machine("AlmaLinux release 10.0 (Purple Lion)", Some(("el", "alma10"))),
            &facts(),
            &[],
            Some(&all),
            "f.md",
            LINK,
        );
        fits_an_old_terminal(&known);
        assert!(
            known.contains("Not yet. wend has no upgrade path that starts at AlmaLinux 10 or passes"),
            "{known}"
        );
        assert!(
            known.contains("    - CentOS 7 to AlmaLinux 10\n    - Debian 4 (etch) to Debian 13 (trixie)\n"),
            "{known}"
        );
        assert!(known.contains("still worth sending"), "{known}");
        let unknown = result(
            &machine("Rocky Linux 9.4 (Blue Onyx)", None),
            &facts(),
            &[],
            Some(&all),
            "f.md",
            LINK,
        );
        fits_an_old_terminal(&unknown);
        assert!(
            unknown.contains("Not yet. wend doesn't recognize the system this machine runs."),
            "{unknown}"
        );
        // started without its atlas, it says so instead of saying there is no path
        let bare = result(
            &machine("CentOS Linux release 7.9.2009 (Core)", Some(("el", "centos7"))),
            &facts(),
            &[],
            None,
            "f.md",
            LINK,
        );
        fits_an_old_terminal(&bare);
        assert!(
            bare.contains("wend can't say: it was started without the folder of upgrade paths"),
            "{bare}"
        );
        assert!(
            !bare.contains("Not yet.") && !bare.contains("The paths it has today"),
            "{bare}"
        );
    }

    #[test]
    fn a_long_file_path_is_cut_to_the_screen_not_lost_off_its_edge() {
        let file = format!("/srv/{}/wend-box-0123456789ab.md", "a-long-folder-name/".repeat(6));
        let s = sharing(&file);
        fits_an_old_terminal(&s);
        let shown: String = s
            .lines()
            .skip_while(|l| !l.contains("2. Copy the file"))
            .skip(1)
            .take_while(|l| !l.contains("3. Open this link"))
            .map(str::trim)
            .collect();
        assert_eq!(shown, file);
        assert!(sharing("/root/w.md")
            .contains("  2. Copy the file to a computer with a web browser:\n       /root/w.md\n  3."));
    }

    #[test]
    fn a_long_path_wraps_at_its_arrows() {
        let hops: Vec<String> = [
            "lenny", "squeeze", "wheezy", "jessie", "stretch", "buster", "bullseye", "bookworm", "trixie",
        ]
        .iter()
        .map(|h| h.to_string())
        .collect();
        let p = path_line("debian", "etch", &hops);
        fits_an_old_terminal(&p);
        assert!(p.lines().count() > 1, "{p}");
        assert!(p.starts_with("      Debian 4 (etch) -> Debian 5 (lenny)"), "{p}");
        assert!(p.lines().skip(1).all(|l| l.starts_with("        -> Debian ")), "{p}");
        assert!(p.trim_end().ends_with("-> Debian 13 (trixie)"), "{p}");
    }

    #[test]
    fn the_screens_before_the_check_name_the_command_to_run() {
        for s in [intro(), needs_root("./wend"), brief("./wend")] {
            fits_an_old_terminal(&s);
            assert!(s.starts_with(TITLE), "{s}");
        }
        assert!(needs_root("./wend").contains("\n    sudo ./wend\n"));
        assert!(brief("/opt/wend/wend").contains("\n    sudo /opt/wend/wend\n"));
        assert!(brief("wend").contains("Every command: wend help\n"));
        assert!(intro().contains("It changes nothing on this machine."));
    }
}
