//! The first run: `wend` with nothing after it, at a terminal. It says what it is about to do, waits for the person,
//! makes the read-only check `wend share` makes, and says what it found and what to do with the report, in plain
//! words. Everything it shows comes from `share::prepare`: the same facts, the same verdict, the same saved report.
//!
//! This file is what it says, and the plain way of saying it: prompts and text, 80 columns, ASCII, no colour, for a
//! console that can do no more (or `--plain`). `screens` shows the same words full-screen where the terminal can.

use crate::answer::{machine, paths, verdict};
use crate::atlas::Atlas;
use crate::screens::Ended;
use crate::share::{self, Judged, Shared, System};
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
pub fn item(text: &str) -> String {
    let body = wrap(text, "      ");
    format!("    - {}", &body[6.min(body.len())..])
}

/// One numbered step, wrapped, its later lines under its first word.
fn step(n: usize, text: &str) -> String {
    let body = wrap(text, "     ");
    format!("  {n}. {}", &body[5.min(body.len())..])
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
            "Read it first. If it names anything private, start wend again with --redact and the word after it \
             (wend --redact acme): the new report has that word replaced too. Then delete this one.",
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
pub fn start(
    prog: &str,
    atlas: impl FnOnce() -> Result<Option<Atlas>, String> + Send,
    redact: &[String],
    plain: bool,
) -> u8 {
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
        return match crate::screens::run(raw, caps, || check(atlas(), redact)) {
            Ended::Made(c) => {
                print!("{}", afterwards(&c));
                0
            }
            Ended::Failed(e) => {
                eprintln!("wend: the check could not finish: {e}\nNothing on this machine was changed.");
                1
            }
            Ended::NotAsked => {
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
