//! Foreground adapter from burrow-proof to wend's existing lab queue.
//! Build this from the trusted default branch, never from the head being proved.
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::fs;
use std::os::fd::AsRawFd;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;
static STOP: AtomicBool = AtomicBool::new(false);
extern "C" fn stop(_: libc::c_int) {
    STOP.store(true, Ordering::Relaxed);
}

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
struct Replay {
    route: String,
    box_id: String,
    flags: Vec<String>,
    hops: Vec<String>,
}

fn docs(root: &Path, directory: &str, boxes: bool) -> Result<Vec<(String, toml::Value)>> {
    let mut found = Vec::new();
    for family in fs::read_dir(root.join(directory))? {
        let family = family?;
        if !family.file_type()?.is_dir() {
            return Err("non-directory family in proof atlas".into());
        }
        for entry in fs::read_dir(family.path())? {
            let entry = entry?;
            let path = if boxes {
                entry.path().join("box.toml")
            } else {
                entry.path()
            };
            if !boxes && path.extension().and_then(|x| x.to_str()) != Some("toml") {
                continue;
            }
            if boxes && !entry.file_type()?.is_dir() {
                return Err("non-directory box in proof atlas".into());
            }
            if !fs::symlink_metadata(&path)?.file_type().is_file() {
                return Err("non-file in proof atlas".into());
            }
            let relative = path
                .strip_prefix(root)?
                .to_str()
                .ok_or("non-UTF8 atlas path")?
                .to_owned();
            found.push((relative, toml::from_str(&fs::read_to_string(path)?)?));
        }
    }
    Ok(found)
}

fn word<'a>(v: &'a toml::Value, key: &str) -> Result<&'a str> {
    v.get(key)
        .and_then(toml::Value::as_str)
        .ok_or_else(|| format!("missing {key}").into())
}

/// As tools/shelter.py keeps_runtimes_on: the last release the route boots into
/// that still ships a Python 2.
fn keeps_runtimes_on<'a>(adapter: &toml::Value, hops: &'a [String], from: &'a str) -> &'a str {
    let skip: Vec<&str> = adapter
        .get("may_skip")
        .and_then(toml::Value::as_array)
        .map(|a| a.iter().filter_map(toml::Value::as_str).collect())
        .unwrap_or_default();
    let mut keeps = from;
    for h in hops {
        if skip.contains(&h.as_str()) {
            continue;
        }
        let py2 = adapter
            .get("runtimes")
            .and_then(|r| r.get(h))
            .and_then(|r| r.get("python2"))
            .and_then(toml::Value::as_array)
            .is_some_and(|v| !v.is_empty());
        if py2 {
            keeps = h;
        } else {
            break;
        }
    }
    keeps
}

fn plan(root: &Path) -> Result<Vec<Replay>> {
    let routes = docs(root, "routes", false)?;
    let boxes = docs(root, "boxes", true)?;
    let mut plan = BTreeSet::new();
    for (path, route) in routes {
        let family = word(&route, "family")?;
        if family.is_empty()
            || !family
                .bytes()
                .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || b"_-".contains(&c))
        {
            return Err("invalid adapter family".into());
        }
        let adapter_path = root.join("adapters").join(format!("{family}.toml"));
        if !fs::symlink_metadata(&adapter_path)?.file_type().is_file() {
            return Err("non-file adapter".into());
        }
        let adapter: toml::Value = toml::from_str(&fs::read_to_string(adapter_path)?)?;
        let mut matched = false;
        for (box_path, box_doc) in &boxes {
            if word(box_doc, "family")? != family {
                continue;
            }
            let release = word(box_doc, "release")?;
            let start = adapter
                .get("releases")
                .and_then(|x| x.get(release))
                .and_then(|x| x.get("joins"))
                .and_then(toml::Value::as_str)
                .unwrap_or(release);
            let all_hops = route
                .get("hops")
                .and_then(toml::Value::as_array)
                .ok_or("missing route hops")?
                .iter()
                .map(|v| v.as_str().map(str::to_owned).ok_or("non-string hop"))
                .collect::<std::result::Result<Vec<_>, _>>()?;
            let remaining = if start == word(&route, "from")? {
                Some(all_hops.as_slice())
            } else {
                all_hops.iter().position(|h| h == start).map(|i| &all_hops[i + 1..])
            };
            if let Some(remaining) = remaining.filter(|h| !h.is_empty()) {
                // tools/vm.py builds a box's shelter only on the route's own
                // start release, and refuses any other box that would reach the
                // hop dropping Python 2. Such pairings cannot run, so they are
                // not part of the suite (the box still counts toward the routes
                // it can start).
                if box_doc
                    .get("shelter")
                    .and_then(toml::Value::as_table)
                    .is_some_and(|t| !t.is_empty())
                {
                    let from = word(&route, "from")?;
                    let keeps = keeps_runtimes_on(&adapter, &all_hops, from);
                    let drop = if keeps == word(&route, "to")? {
                        None
                    } else if keeps == from {
                        all_hops.first()
                    } else {
                        all_hops
                            .iter()
                            .position(|h| h == keeps)
                            .and_then(|i| all_hops.get(i + 1))
                    };
                    if drop.is_some_and(|d| remaining.contains(d)) && (keeps != from || start != from) {
                        continue;
                    }
                }
                matched = true;
                let uefi = adapter
                    .get("releases")
                    .and_then(|v| v.get(release))
                    .and_then(|v| v.get("install"))
                    .and_then(|v| v.get("uefi"))
                    .and_then(toml::Value::as_bool)
                    .unwrap_or(false);
                let mut hops = remaining.to_vec();
                if route
                    .get("finish")
                    .and_then(|v| v.get("prune_obsolete"))
                    .and_then(toml::Value::as_bool)
                    == Some(true)
                {
                    hops.push("prune".into());
                }
                for flags in std::iter::once(vec![]).chain(uefi.then(|| vec!["--uefi".into()])) {
                    plan.insert(Replay {
                        route: path.clone(),
                        box_id: box_path
                            .strip_prefix("boxes/")
                            .unwrap()
                            .strip_suffix("/box.toml")
                            .unwrap()
                            .to_owned(),
                        flags,
                        hops: hops.clone(),
                    });
                }
            }
        }
        if !matched {
            return Err(format!("{path} has no starting box; refusing an incomplete proof").into());
        }
    }
    if plan.is_empty() {
        return Err("empty replay suite".into());
    }
    Ok(plan.into_iter().collect())
}

fn evidence(v: &Value, replay: &Replay, head: &str) -> Result<()> {
    if v["sha"] != head
        || v["job"] != "replay"
        || v["args"] != serde_json::json!([replay.route, replay.box_id])
        || v["vm_flags"] != serde_json::json!(replay.flags)
        || v["result"] != "pass"
        || v["reasons"] != serde_json::json!([])
    {
        return Err("lab verdict is not a clean pass for the requested replay and head".into());
    }
    let commands = v["commands"].as_array().ok_or("missing commands")?;
    if commands.is_empty() || commands.iter().any(|c| c["rc"] != 0) {
        return Err("missing or failed command".into());
    }
    let runs = v["runs"].as_array().ok_or("missing runs")?;
    if runs.is_empty() {
        return Err("no VM run evidence".into());
    }
    for run in runs {
        let r = &run["run_json"];
        let hops = r["hops"].as_array().ok_or("missing hop evidence")?;
        if r["head"]["commit"] != head
            || r["head"]["dirty"] != false
            || r["result"] != "pass"
            || r["box"] != replay.box_id
            || r["firmware"] != if replay.flags.is_empty() { "bios" } else { "uefi" }
            || hops.iter().map(|h| h["to"].as_str().unwrap_or("")).collect::<Vec<_>>() != replay.hops
            || r.get("resumed").is_some()
            || hops.is_empty()
            || hops.iter().any(|h| h["ok"] != true)
        {
            return Err("VM evidence is dirty, partial, failed, or belongs to another head/box".into());
        }
    }
    Ok(())
}

struct Lab {
    runner: PathBuf,
    identity: String,
}
impl Lab {
    fn call(&self, args: &[&str]) -> Result<String> {
        let output = Command::new("python3")
            .arg("-P")
            .arg(&self.runner)
            .args(args)
            .output()?;
        if !output.status.success() {
            return Err(format!(
                "lab {}: {}{}",
                args[0],
                String::from_utf8_lossy(&output.stdout),
                String::from_utf8_lossy(&output.stderr)
            )
            .into());
        }
        Ok(String::from_utf8(output.stdout)?)
    }
    /// Lock for one replay identity, in the lab's queue directory.
    fn claim(&self, owner: &str) -> Result<fs::File> {
        let lab = std::env::var("WEND_LAB").unwrap_or_else(|_| "/data/wend-lab".into());
        let dir = Path::new(&lab).join("queue");
        fs::create_dir_all(&dir)?;
        let name: String = Sha256::digest(owner.as_bytes())
            .iter()
            .take(12)
            .map(|b| format!("{b:02x}"))
            .collect();
        let file = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(dir.join(format!(".proof-claim-{name}")))?;
        if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX) } != 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        Ok(file)
    }
    /// Like call, but the child keeps `claim` held until it exits.
    fn call_holding(&self, args: &[&str], claim: &fs::File) -> Result<String> {
        use std::os::unix::process::CommandExt;
        let fd = claim.as_raw_fd();
        let mut command = Command::new("python3");
        command.arg("-P").arg(&self.runner).args(args);
        unsafe {
            command.pre_exec(move || {
                let flags = libc::fcntl(fd, libc::F_GETFD);
                if flags < 0 || libc::fcntl(fd, libc::F_SETFD, flags & !libc::FD_CLOEXEC) < 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        let output = command.output()?;
        if !output.status.success() {
            return Err(format!(
                "lab {}: {}{}",
                args[0],
                String::from_utf8_lossy(&output.stdout),
                String::from_utf8_lossy(&output.stderr)
            )
            .into());
        }
        Ok(String::from_utf8(output.stdout)?)
    }
    /// Ask the queue for a worker. remi.py starts it as an independent user
    /// unit, outside this proof's cgroup, which burrow-proof empties when the
    /// adapter returns; a live worker makes this a no-op.
    fn wake(&self) -> Result<()> {
        self.call(&["wake"]).map(|_| ())
    }
    fn entries(&self) -> Result<Vec<Value>> {
        Ok(serde_json::from_str(&self.call(&["queue", "--json"])?)?)
    }
    fn replay(&self, replay: &Replay, head: &str, number: &str) -> Result<(Value, String)> {
        // Stable identity also lets a replacement proof unit adopt an existing
        // queue entry after an interruption, rather than replaying it twice.
        let owner = format!(
            "autoland:{}:{number}:{head}:{}:{}:{}",
            self.identity,
            replay.route,
            replay.box_id,
            replay.flags.join(" ")
        );
        // One claim per replay identity: the lock is held from the lookup
        // through the enqueue child's exit (the child inherits it, so it
        // outlives this process), so concurrent or replacement adapters see
        // the entry the child persists instead of enqueuing a twin.
        let claim = self.claim(&owner)?;
        let adoptable = |entries: &[Value]| {
            entries
                .iter()
                .find(|e| e["by"] == owner && ["queued", "running", "done"].iter().any(|s| e["state"] == *s))
                .and_then(|e| e["id"].as_str().map(str::to_owned))
        };
        let id = match adoptable(&self.entries()?) {
            Some(id) => id,
            None => {
                if STOP.load(Ordering::Relaxed) {
                    return Err("proof stopped before enqueue".into());
                }
                let known: BTreeSet<String> = self
                    .entries()?
                    .iter()
                    .filter_map(|e| e["id"].as_str().map(str::to_owned))
                    .collect();
                let mut args = vec![
                    "enqueue",
                    "replay",
                    &replay.route,
                    &replay.box_id,
                    "--sha",
                    head,
                    "--pr",
                    number,
                    "--by",
                    &owner,
                ];
                let flags = replay.flags.join(" ");
                if !flags.is_empty() {
                    args.extend(["--vm-flags", &flags]);
                }
                let output = self.call_holding(&args, &claim)?;
                println!("{output}");
                // Follow exactly the job this attempt created, whatever state
                // it already reached; the queue line names it.
                let named = output
                    .split_whitespace()
                    .find_map(|w| w.strip_suffix(':').filter(|id| id.matches('-').count() >= 3))
                    .map(str::to_owned);
                let fresh: Vec<String> = self
                    .entries()?
                    .iter()
                    .filter(|e| e["by"] == owner)
                    .filter_map(|e| e["id"].as_str().map(str::to_owned))
                    .filter(|id| !known.contains(id))
                    .collect();
                named
                    .filter(|id| fresh.contains(id))
                    .or_else(|| fresh.first().cloned())
                    .ok_or("enqueued job is absent")?
            }
        };
        drop(claim);
        let mut woken: Option<Instant> = None;
        loop {
            let entries = self.entries()?;
            let entry = entries.iter().find(|e| e["id"] == id).ok_or("lab job disappeared")?;
            let state = entry["state"].as_str().ok_or("lab job has no state")?;
            if !["queued", "running"].contains(&state) {
                let file = entry["verdict"].as_str().ok_or("job ended without a verdict file")?;
                return Ok((serde_json::from_str(&fs::read_to_string(file)?)?, file.to_owned()));
            }
            // The queue persists a job before it starts a worker, and workers
            // die with their host: have one look at the queue now and then.
            // It takes the queue's worker lock itself, so a live worker makes
            // this a no-op and slot ownership stays with the queue.
            if woken.is_none_or(|t| t.elapsed() >= Duration::from_secs(60)) && !STOP.load(Ordering::Relaxed) {
                self.wake()?;
                woken = Some(Instant::now());
            }
            if STOP.load(Ordering::Relaxed) {
                let _ = self.call(&["cancel", &id]);
                // Keep the foreground owner alive until the queue says its
                // guest is over. Never abandon an active replay on SIGTERM.
            }
            std::thread::sleep(Duration::from_secs(2));
        }
    }
}

fn run(args: &[String]) -> Result<()> {
    if args.len() == 2 && args[0] == "--plan" {
        let suite = plan(Path::new(&args[1]))?;
        for replay in &suite {
            println!("replay {} {} {}", replay.route, replay.box_id, replay.flags.join(" "));
        }
        println!("{} serial replays; queue limit 8 hours each", suite.len());
        return Ok(());
    }
    if args.len() != 2 {
        return Err("usage: wend-lab-proof TRUSTED_TOOLS/remi.py HEAD_WORKTREE".into());
    }
    let head = std::env::var("PROOF_HEAD")?;
    let number = std::env::var("PROOF_NUMBER")?;
    let summary = PathBuf::from(std::env::var("PROOF_SUMMARY")?);
    if head.len() != 40
        || !head.bytes().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
        || number.is_empty()
        || !number.bytes().all(|c| c.is_ascii_digit())
    {
        return Err("invalid proof identity".into());
    }
    let root = PathBuf::from(&args[1]);
    let git = |arguments: &[&str]| -> Result<String> {
        let p = Command::new("git").arg("-C").arg(&root).args(arguments).output()?;
        if !p.status.success() {
            return Err("cannot inspect proof worktree".into());
        }
        Ok(String::from_utf8(p.stdout)?.trim().to_owned())
    };
    if git(&["rev-parse", "HEAD"])? != head || !git(&["status", "--porcelain"])?.is_empty() {
        return Err("proof worktree is not the requested clean head".into());
    }
    let suite = plan(&root)?;
    let runner = PathBuf::from(&args[0]);
    let mut identity = Sha256::new();
    identity.update(include_bytes!("wend-lab-proof.rs"));
    identity.update(include_bytes!("../../Cargo.lock"));
    identity.update(fs::read(&runner)?);
    let lab = Lab {
        runner,
        identity: identity.finalize().iter().map(|b| format!("{b:02x}")).collect(),
    };
    let mut note = format!(
        "VM replay proof on `{head}`: {} serial route/box replays.\n\n",
        suite.len()
    );
    fs::write(&summary, &note)?;
    for replay in &suite {
        if STOP.load(Ordering::Relaxed) {
            return Err("proof stopped".into());
        }
        println!("replay {} {} at {head}", replay.route, replay.box_id);
        let result = lab.replay(replay, &head, &number);
        let checked = match &result {
            Ok((v, _)) => evidence(v, replay, &head),
            Err(e) => Err(e.to_string().into()),
        };
        note += &format!(
            "- `{}` on `{}` {}: {}\n",
            replay.route,
            replay.box_id,
            replay.flags.join(" "),
            if checked.is_ok() { "passed" } else { "FAILED" }
        );
        match &result {
            Ok((v, file)) => {
                println!("Evidence {file}:\n{}", serde_json::to_string_pretty(v)?);
                note += &format!(
                    "  Evidence: `{file}`; commands: `{}`; reasons: `{}`.\n",
                    v["commands"]
                        .as_array()
                        .map(|cs| cs
                            .iter()
                            .map(|c| format!("rc={} log={}", c["rc"], c["log"]))
                            .collect::<Vec<_>>()
                            .join("; "))
                        .unwrap_or_default(),
                    v["reasons"]
                );
                if let Some(runs) = v["runs"].as_array() {
                    for r in runs {
                        note += &format!(
                            "  Run: `{}`; head `{}`; firmware `{}`; result `{}`.\n",
                            r["dir"], r["run_json"]["head"], r["run_json"]["firmware"], r["run_json"]["result"]
                        );
                    }
                }
            }
            Err(e) => note += &format!("\n{e}\n\n"),
        }
        if let Err(e) = &checked {
            note += &format!("Proof refused: {e}\n");
        }
        fs::write(&summary, &note)?;
        checked?;
    }
    Ok(())
}

fn main() -> ExitCode {
    unsafe {
        libc::signal(libc::SIGTERM, stop as *const () as libc::sighandler_t);
        libc::signal(libc::SIGINT, stop as *const () as libc::sighandler_t);
    }
    match run(&std::env::args().skip(1).collect::<Vec<_>>()) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("VM proof: {e}");
            if let Ok(path) = std::env::var("PROOF_SUMMARY") {
                use std::io::Write;
                if let Ok(mut f) = fs::OpenOptions::new().create(true).append(true).open(path) {
                    let _ = writeln!(f, "\nVM proof failed: {e}");
                }
            }
            if STOP.load(Ordering::Relaxed) {
                ExitCode::from(126)
            } else {
                ExitCode::FAILURE
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn refuses_wrong_dirty_partial_and_failed_evidence() {
        let replay = Replay {
            route: "routes/f/a.toml".into(),
            box_id: "f/b".into(),
            flags: vec![],
            hops: vec!["new".into()],
        };
        let valid = json!({"sha":"head", "job":"replay", "args":[replay.route,replay.box_id],
            "vm_flags":[], "result":"pass", "reasons":[], "commands":[{"rc":0}],
            "runs":[{"run_json":{"head":{"commit":"head","dirty":false},"result":"pass",
            "box":"f/b","firmware":"bios","hops":[{"to":"new","ok":true}]}}]});
        assert!(evidence(&valid, &replay, "head").is_ok());
        for (path, bad) in [
            ("/sha", json!("old")),
            ("/result", json!("fail")),
            ("/commands/0/rc", json!(1)),
            ("/commands", json!([])),
            ("/runs", json!([])),
            ("/runs/0/run_json/head/commit", json!("old")),
            ("/runs/0/run_json/head/dirty", json!(true)),
            ("/runs/0/run_json/head/dirty", Value::Null),
            ("/runs/0/run_json/result", json!("partial-pass")),
            ("/runs/0/run_json/box", json!("wrong")),
            ("/runs/0/run_json/firmware", json!("uefi")),
            ("/runs/0/run_json/hops/0/to", json!("other")),
            ("/runs/0/run_json/hops", json!([])),
            ("/runs/0/run_json/hops/0/ok", json!(false)),
        ] {
            let mut v = valid.clone();
            *v.pointer_mut(path).unwrap() = bad;
            assert!(evidence(&v, &replay, "head").is_err(), "{path}");
        }
        let mut v = valid.clone();
        v["runs"][0]["run_json"]["resumed"] = json!({});
        assert!(evidence(&v, &replay, "head").is_err());
    }
}
