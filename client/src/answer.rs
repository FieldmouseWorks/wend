//! The answer to "can wend upgrade this machine?": preflight's verdict (`hardware::evaluate`) put the way a person
//! would say it. How far the machine could go, what stops it there, and the releases by the names people use.

use crate::atlas::{str_of, Atlas};
use crate::guide::{item, width, wrap};
use crate::share::{Judged, System, DEBIAN_CODENAMES};
use serde_json::Value;

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
        if !line.is_empty() && 6 + line.len() + piece.len() > width() {
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

/// What preflight found for one path, as an answer to "can wend upgrade it?". `starts` are the releases the
/// routes through this machine's release begin at, when it joins them partway.
fn answer(j: &Judged, starts: &[&str]) -> String {
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
                // only a machine that could itself reach the end is told so: its hardware may stop short too
                if v["lands_on"].as_str() == Some(to) {
                    "The machine itself could take it, but programs of your own on it would stop working at the \
                     first step:"
                } else {
                    "Programs of your own on this machine would stop working at the first step:"
                }
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
    if let Some((rung, _)) = &j.joins {
        let from: Vec<String> = starts.iter().map(|r| name(r)).collect();
        s.push('\n');
        s += &wrap(
            &format!(
                "This machine joins the path partway, at {}. Those steps were proven on machines that started at {}, \
                 not yet on one installed as {}.",
                name(rung),
                from.join(" or "),
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
    if v["ok"] == true {
        // a yes is not an invitation to upgrade
        s.push('\n');
        s += &wrap("This was only a check. What can come next is in wend's README.", "  ");
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
    // several routes can pass through one release, and the way on from it is the same on each: one answer, with
    // every start those steps were proven from
    let mut answers: Vec<(&Judged, Vec<&str>)> = vec![];
    for j in judged {
        let start = j.joins.as_ref().map(|(_, start)| start.as_str());
        match answers
            .iter_mut()
            .find(|(k, _)| k.family == j.family && k.verdict == j.verdict)
        {
            Some((_, starts)) => starts.extend(start),
            None => answers.push((j, start.into_iter().collect())),
        }
    }
    for (i, (j, starts)) in answers.iter().enumerate() {
        if i > 0 {
            s.push('\n');
        }
        s += &answer(j, starts);
    }
    s
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::guide::result;
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
        // a yes is not an invitation to upgrade: it says this was a check, and where what comes next is written
        assert!(
            s.contains("  This was only a check. What can come next is in wend's README.\n"),
            "{s}"
        );
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
        assert!(
            said.contains("The machine itself could take it, but programs of your own"),
            "{s}"
        );
        // hardware that stops short as well: it isn't told the machine could take the path
        let both = json!({"ok": false, "from": "centos7", "to": "alma10", "lands_on": "alma9",
            "keeps_runtimes_on": "centos7", "machine": [], "hops": [hop("alma9"), hop("alma10")]});
        let s = verdict(&machine("x", None), &[judged("el", None, both)], Some(&[]));
        let said = s.split_whitespace().collect::<Vec<_>>().join(" ");
        assert!(said.contains("Not as it stands."), "{s}");
        assert!(
            said.contains("Programs of your own on this machine would stop working at the first step:"),
            "{s}"
        );
        assert!(!said.contains("The machine itself could take it"), "{s}");
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
    fn a_machine_on_several_routes_gets_one_answer_from_where_it_is() {
        // the real atlas: CentOS 6 is on the CentOS 4 and CentOS 5 routes, AlmaLinux 9 on four, and the way on is the
        // same on each
        let a = Atlas::load(&std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("..")).unwrap();
        let f = json!({"schema": "wend.hardware/4", "arch": "x86_64", "userland_bits": 64,
            "cpu": {"model": "test", "features": ["cx16", "lahf_lm", "popcnt", "sse4_1", "sse4_2", "ssse3", "sse3"],
                    "hypervisor": true},
            "boot": "bios", "ram_mib": 4096, "free_mib": 40000,
            "boot_dir": {"separate": false, "total_mib": 40000, "free_mib": 40000, "kernels": []},
            "blacklisted": [], "pci": [], "runtimes": [], "python2_modules": [], "native": [], "kernel_owner": "kernel",
            "repo_packages": [], "mounts": []});
        let said = |fam: &str, rel: &str| {
            let sys = System {
                pretty: rel.into(),
                release: Some((fam.into(), rel.into())),
            };
            let judged = crate::share::judge(Some(&a), None, &sys, &f).unwrap();
            let s = verdict(&sys, &judged, Some(&[]));
            fits_an_old_terminal(&s);
            (
                judged.len(),
                s.clone(),
                s.split_whitespace().collect::<Vec<_>>().join(" "),
            )
        };
        let (routes, s, flat) = said("el", "centos6");
        assert_eq!(routes, 2);
        assert_eq!(s.matches("wend has an upgrade path from here").count(), 1, "{s}");
        // the path starts where the machine is, not where the route does
        assert!(
            s.contains("      CentOS 6 -> CentOS 7 -> AlmaLinux 9 -> AlmaLinux 10\n"),
            "{s}"
        );
        assert!(
            flat.contains(
                "joins the path partway, at CentOS 6. Those steps were proven on machines that started at CentOS 4 \
                 or CentOS 5, not yet on one installed as CentOS 6."
            ),
            "{s}"
        );
        let (routes, s, flat) = said("el", "alma9");
        assert_eq!(routes, 4);
        assert_eq!(s.matches("wend has an upgrade path from here").count(), 1, "{s}");
        assert!(s.contains("      AlmaLinux 9 -> AlmaLinux 10\n"), "{s}");
        assert!(
            flat.contains("started at CentOS 4 or CentOS 5 or CentOS 7 or CentOS 8, not yet"),
            "{s}"
        );
        let (_, s, flat) = said("debian", "buster");
        assert!(
            s.contains(
                "      Debian 10 (buster) -> Debian 11 (bullseye) -> Debian 12 (bookworm)\n        -> Debian 13"
            ),
            "{s}"
        );
        assert!(
            flat.contains("started at Debian 4 (etch), not yet on one installed as Debian 10 (buster)."),
            "{s}"
        );
        let (_, s, _) = said("ubuntu", "bionic");
        assert!(s.contains("      Ubuntu 18.04 (bionic) -> Ubuntu 20.04 (focal)"), "{s}");
        // a machine at a route's start joins nothing
        let (routes, s, _) = said("el", "centos7");
        assert_eq!(routes, 1);
        assert!(
            s.contains("      CentOS 7 -> AlmaLinux 9 -> AlmaLinux 10\n") && !s.contains("partway"),
            "{s}"
        );
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
}
