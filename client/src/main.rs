//! wend, the client: walk a proven upgrade route on a machine. No model, no network beyond the mirrors.
//!
//! It reads and checks the atlas, compiles fixes and hops into the exact commands CI proved, and walks a route on
//! a root filesystem: snapshot, apply, check, roll back on red.

mod atlas;
mod backend;
mod canon;
mod check;
mod fetch;
mod generated;
mod hardware;
mod hop;
mod native;
mod rehearse;
mod report;
mod runtime;
mod sandbox;
mod selftest;
mod share;
mod upgrade;
mod walk;

use atlas::{get, str_of, Atlas, Doc};
use canon::content_id;
use serde_json::{json, Map, Value};
use std::path::PathBuf;
use std::process::ExitCode;

const USAGE: &str = "\
wend: walk a proven upgrade route on a machine. No model, no network beyond your distro's mirrors.

  wend check                        check every adapter, fix, route and run in the atlas (the gate's rules)
  wend ids                          every fix's and route's content id
  wend compile FIX.toml             the commands a fix's steps run, and its precondition as a test
  wend hop ROUTE HOP [--fired N,..] the commands one hop of a route runs, with the named fixes (default: every
                                    fix the route pins on that hop)
  wend walk ROUTE --root DIR --checks FILE [--box NAME] [--out RUN.json] [--work DIR]
            (--run RUN.json [--cache DIR] [--source URL|DIR]... [--archive URL|DIR]... [--no-archive]
             [--offline] | --live)
                                    walk a route on a root filesystem, in place (root needed): each hop's
                                    fixes, the upgrade, the checks; a hop that breaks anything is rolled back
                                    and the walk stops. FILE has [roles] (shell checks), like a CI box file.
                                    --run: the repos only serve what that run's pinned sources vouch for,
                                    from the cache, each --source (laid out host/path), each --archive, then
                                    wend's own archive (by digest, https://archive.fieldmouseworks.com:
                                    docs/archive.md; not with --offline or --no-archive), then the origin. A
                                    pinned file no source has refuses the walk before it touches anything.
                                    --live: no pins, today's mirrors, nothing verified; the run says so
  wend rehearse ROUTE --root DIR --run RUN.json --checks FILE [--from [USER@]HOST [--ssh CMD]]
                [--kernel VERSION] [--plan DIR] [--facts FILE] [--cache DIR] [--source URL|DIR]...
                [--archive URL|DIR]...
                                    the walk `wend upgrade` will make, on a copy of the machine (root needed):
                                    --from copies the machine's root into DIR over ssh (rsync, one filesystem
                                    at a time; CMD defaults to ssh), else DIR is a copy made another way and
                                    --kernel is the machine's uname -r. Walks it as walk --run does and, on a
                                    pass, writes the plan (default wend-plan-<machine id>): plan.json with the
                                    machine's fingerprint, the run, the checks, and every file the walk fetched
                                    (each hop's kernel too); FILE is `wend facts` from the machine, kept in it for
                                    the report. On a fail, no plan: the run and a scrubbed gap report to post
  wend facts                        what preflight reads about this machine: CPU level features, firmware, memory,
                                    free disk, PCI devices and their drivers, its own Python 2 programs (JSON)
  wend preflight ROUTE [--facts FILE] [--json]
                                    can this machine land on every release the route boots it into? CPU level,
                                    firmware, memory, disk, and the disk and network drivers each release's
                                    kernel has, and whether each still ships the Python 2 the machine's own
                                    programs run. Says the furthest release it can land on; exits 1 short of the end
  wend share [ROUTE] [--redact WORD]...
                                    a box report to post: this machine's release, facts and preflight against
                                    the route that starts there (or ROUTE), with its hostname, users, home
                                    directories, IPs and secrets scrubbed. Shows it all, saves it, and prints a
                                    prefilled issue link; sends nothing. Read-only, root needed, like facts
  wend selftest                     what wend needs from this machine's kernel (processes, pipes, files, locks,
                                    loopback, threads), tried one by one; exits 1 naming what failed
  wend --version
  wend parity INPUT.json            machine output for client/parity.py (the Python engine must agree)

Options: --atlas DIR (default: the current directory, laid out like the wend repo).
ROUTE is routes/<family>/<from>-to-<to>.toml or just <from>-to-<to>.";

fn opt(args: &mut Vec<String>, name: &str) -> Option<String> {
    let i = args.iter().position(|a| a == name)?;
    if i + 1 >= args.len() {
        return None;
    }
    let v = args.remove(i + 1);
    args.remove(i);
    Some(v)
}

fn flag(args: &mut Vec<String>, name: &str) -> bool {
    match args.iter().position(|a| a == name) {
        Some(i) => {
            args.remove(i);
            true
        }
        None => false,
    }
}

fn main() -> ExitCode {
    let mut args: Vec<String> = std::env::args().skip(1).collect();
    let root = PathBuf::from(opt(&mut args, "--atlas").unwrap_or_else(|| ".".into()));
    let fired = opt(&mut args, "--fired");
    let facts = opt(&mut args, "--facts");
    let as_json = match args.iter().position(|a| a == "--json") {
        Some(i) => {
            args.remove(i);
            true
        }
        None => false,
    };
    let walk_opts = WalkOpts {
        root: opt(&mut args, "--root"),
        checks: opt(&mut args, "--checks"),
        box_id: opt(&mut args, "--box"),
        out: opt(&mut args, "--out"),
        work: opt(&mut args, "--work").unwrap_or_else(|| "/var/tmp/wend-client".into()),
        steps: opt(&mut args, "--steps"),
        run: opt(&mut args, "--run"),
        cache: opt(&mut args, "--cache"),
        sources: std::iter::from_fn(|| opt(&mut args, "--source")).collect(),
        archives: std::iter::from_fn(|| opt(&mut args, "--archive")).collect(),
        no_archive: flag(&mut args, "--no-archive"),
        offline: flag(&mut args, "--offline"),
        live: flag(&mut args, "--live"),
    };
    let rehearse_opts = rehearse::RehearseOpts {
        from: opt(&mut args, "--from"),
        ssh: opt(&mut args, "--ssh").unwrap_or_else(|| "ssh".into()),
        kernel: opt(&mut args, "--kernel"),
        plan: opt(&mut args, "--plan"),
        facts: facts.clone(),
    };
    let redact: Vec<String> = std::iter::from_fn(|| opt(&mut args, "--redact")).collect();
    let upgrade_opts = upgrade::UpgradeOpts {
        new_root_disk: opt(&mut args, "--new-root-disk"),
        status: flag(&mut args, "--status"),
        work: flag(&mut args, "--work"),
    };
    let Some(cmd) = args.first().cloned() else {
        println!("{USAGE}");
        return ExitCode::from(2);
    };
    if matches!(cmd.as_str(), "-h" | "--help" | "help") {
        println!("{USAGE}");
        return ExitCode::SUCCESS;
    }
    if matches!(cmd.as_str(), "--version" | "version") && args.len() == 1 {
        println!("wend {}", env!("CARGO_PKG_VERSION"));
        return ExitCode::SUCCESS;
    }
    if cmd == "selftest" && args.len() == 1 {
        return if selftest::run() {
            ExitCode::SUCCESS
        } else {
            ExitCode::FAILURE
        };
    }
    if cmd == "facts" && args.len() == 1 {
        // no atlas needed: run it on the machine, judge the file anywhere (`preflight --facts`)
        let f = match hardware::gather() {
            Ok(f) => f,
            Err(e) => {
                eprintln!("wend: {e}");
                return ExitCode::from(2);
            }
        };
        println!("{}", serde_json::to_string_pretty(&f).unwrap());
        return ExitCode::SUCCESS;
    }
    // SPEC "Upgrading a running machine": exit codes a loop can drive, and an atlas of its own once under way
    match cmd.as_str() {
        "upgrade" | "rollback" | "commit" => return ExitCode::from(upgrade::main(&root, &args, &upgrade_opts)),
        _ => {}
    }
    if cmd == "share" && args.len() <= 2 {
        // the atlas is optional: without one the report has the facts and no verdict
        let atlas = Atlas::load(&root).ok();
        if let Some(a) = &atlas {
            if let Err(e) = preflight(a) {
                eprintln!("wend: {e}");
                return ExitCode::FAILURE;
            }
        }
        return match share::cmd_share(atlas.as_ref(), args.get(1).map(String::as_str), &redact) {
            Ok(_) => ExitCode::SUCCESS,
            Err(e) => {
                eprintln!("wend: {e}");
                ExitCode::FAILURE
            }
        };
    }
    let atlas = match Atlas::load(&root) {
        Ok(a) => a,
        Err(e) => {
            eprintln!("wend: {e}");
            return ExitCode::FAILURE;
        }
    };
    let r = match (cmd.as_str(), &args[1..]) {
        ("check", []) => cmd_check(&atlas),
        ("ids", []) => cmd_ids(&atlas),
        ("compile", [fix]) => cmd_compile(&atlas, &root, fix),
        ("hop", [route, hop]) => cmd_hop(&atlas, route, hop, fired.as_deref()),
        ("parity", [input]) => cmd_parity(&atlas, input),
        ("walk", [route]) => cmd_walk(&atlas, route, &walk_opts).map(|w| w.run["result"] == "pass"),
        ("rehearse", [route]) => rehearse::cmd_rehearse(&atlas, route, &walk_opts, &rehearse_opts),
        ("preflight", [route]) => cmd_preflight(&atlas, route, facts.as_deref(), as_json),
        _ => Err(format!(
            "unknown command or wrong arguments: {}\n\n{USAGE}",
            args.join(" ")
        )),
    };
    match r {
        Ok(true) => ExitCode::SUCCESS,
        Ok(false) => ExitCode::FAILURE,
        Err(e) => {
            eprintln!("wend: {e}");
            ExitCode::FAILURE
        }
    }
}

/// Nothing compiles from an atlas that fails the gate's rules: an adapter or fix CI would refuse never becomes a
/// command here, whatever mirror or cache the atlas came from.
fn preflight(atlas: &Atlas) -> Result<(), String> {
    let errs = check::check_atlas(atlas);
    if errs.is_empty() {
        return Ok(());
    }
    Err(format!(
        "the atlas doesn't pass its own check (wend check):\n{}",
        errs.join("\n")
    ))
}

fn cmd_check(atlas: &Atlas) -> Result<bool, String> {
    let errs = check::check_atlas(atlas);
    for e in &errs {
        println!("ERROR {e}");
    }
    println!(
        "{} adapters, {} fixes, {} routes, {} runs: {}",
        atlas.adapters.len(),
        atlas.fixes.len(),
        atlas.routes.len(),
        atlas.runs.len(),
        if errs.is_empty() {
            "ok".to_string()
        } else {
            format!("{} errors", errs.len())
        }
    );
    Ok(errs.is_empty())
}

/// SPEC "Hardware preflight". Reads this machine, or a facts file (`wend facts` on the machine, judged elsewhere).
fn cmd_preflight(atlas: &Atlas, route: &str, facts: Option<&str>, as_json: bool) -> Result<bool, String> {
    preflight(atlas)?;
    let (_, r) = atlas.route(route)?;
    let f = match facts {
        Some(p) => serde_json::from_str(&std::fs::read_to_string(p).map_err(|e| format!("{p}: {e}"))?)
            .map_err(|e| format!("{p}: {e}"))?,
        None => hardware::gather()?,
    };
    // on the machine itself, a release partway along the route is judged from there (a facts file names no release)
    let rest = match (facts, share::system(std::path::Path::new("/")).release) {
        (None, Some((fam, rel))) if str_of(r, "family")? == fam => {
            let rest = check::rest_of_route(adapter_of(atlas, r)?, r, &rel);
            if rest.is_none() {
                eprintln!(
                    "note: this machine runs {rel}, and this route starts at {} and doesn't pass through it on the \
                     way to {}: judged as if it started there",
                    str_of(r, "from")?,
                    str_of(r, "to")?
                );
            }
            rest.filter(|(_, joins)| joins.is_some())
        }
        _ => None,
    };
    let (r, joins) = rest.unwrap_or_else(|| (r.clone(), None));
    let mut v = hardware::evaluate(atlas, &r, &f)?;
    if let Some(rung) = &joins {
        v["joins_at"] = Value::String(rung.clone());
    }
    if as_json {
        println!("{}", serde_json::to_string_pretty(&v).unwrap());
    } else {
        if let Some(rung) = &joins {
            println!("This machine runs {rung}, partway along the route: judged from there.");
        }
        print!("{}", hardware::report(&v));
    }
    Ok(v["ok"] == true)
}

fn cmd_ids(atlas: &Atlas) -> Result<bool, String> {
    for (p, d) in atlas.fixes.iter().chain(atlas.routes.iter()) {
        println!("{} {p}", content_id(d));
    }
    Ok(true)
}

fn adapter_of<'a>(atlas: &'a Atlas, doc: &Doc) -> Result<&'a Doc, String> {
    let fam = str_of(doc, "family")?;
    atlas.adapters.get(fam).ok_or(format!("no adapter for family {fam:?}"))
}

fn compile_fix(atlas: &Atlas, f: &Doc) -> Result<Value, String> {
    let a = adapter_of(atlas, f)?;
    let hop = str_of(f, "hop")?;
    let phase = |ph: &str| -> Result<Vec<String>, String> {
        get(f, ph)
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .map(|s| backend::step(s, a, hop))
            .collect()
    };
    Ok(
        json!({"when": backend::pred_sh(get(f, "when").ok_or("no when")?, a)?, "pre": phase("pre")?, "post": phase("post")?}),
    )
}

fn cmd_compile(atlas: &Atlas, root: &std::path::Path, path: &str) -> Result<bool, String> {
    preflight(atlas)?;
    // a relative path that isn't here is looked up in the atlas, so `--atlas DIR compile fixes/...` works anywhere
    let p = std::path::Path::new(path);
    let file = if p.is_absolute() || p.exists() {
        p.to_path_buf()
    } else {
        root.join(p)
    };
    let text = std::fs::read_to_string(&file).map_err(|e| format!("{}: {e}", file.display()))?;
    let f = atlas::parse_toml(&text)?;
    let errs = check::check_fix(&f, &atlas.adapters, path.trim_start_matches("./"));
    for e in &errs {
        eprintln!("ERROR {e}");
    }
    if !errs.is_empty() {
        return Ok(false);
    }
    println!("{}", serde_json::to_string_pretty(&compile_fix(atlas, &f)?).unwrap());
    Ok(true)
}

fn hop_dump(atlas: &Atlas, route: &Doc, hop: &str, only: Option<&[String]>) -> Result<Value, String> {
    let a = adapter_of(atlas, route)?;
    let by_id = atlas.fixes_by_id();
    let fixes: Vec<&Doc> = hop::pinned(route, &by_id)?
        .into_iter()
        .map(|(_, f)| f)
        .filter(|f| get(f, "hop").and_then(Value::as_str) == Some(hop))
        .filter(|f| {
            only.is_none_or(|o| {
                o.iter()
                    .any(|n| Some(n.as_str()) == get(f, "meta").and_then(|m| get(m, "name")).and_then(Value::as_str))
            })
        })
        .collect();
    let p = hop::hop_plan(a, hop, &fixes)?;
    let names: Vec<Value> = fixes
        .iter()
        .map(|f| {
            get(f, "meta")
                .and_then(|m| get(m, "name"))
                .cloned()
                .unwrap_or(Value::Null)
        })
        .collect();
    Ok(json!({"fixes": names, "commands": p.commands, "after": p.after}))
}

fn cmd_hop(atlas: &Atlas, route: &str, hop: &str, fired: Option<&str>) -> Result<bool, String> {
    preflight(atlas)?;
    let (_, r) = atlas.route(route)?;
    let hops = atlas::list_of(r, "hops")?;
    if !hops.iter().any(|h| h == hop) {
        return Err(format!(
            "{hop} is not a hop of this route (its hops: {})",
            hops.join(", ")
        ));
    }
    let only: Option<Vec<String>> = fired.map(|f| f.split(',').filter(|s| !s.is_empty()).map(String::from).collect());
    let d = hop_dump(atlas, r, hop, only.as_deref())?;
    println!("{}", serde_json::to_string_pretty(&d).unwrap());
    Ok(true)
}

/// Everything client/parity.py compares with the Python engine: ids, every fix compiled, every hop of every route
/// (with all of its pinned fixes and with none), the prune step, extra steps and checker cases from INPUT.
fn cmd_parity(atlas: &Atlas, input: &str) -> Result<bool, String> {
    let inp: Value = serde_json::from_str(&std::fs::read_to_string(input).map_err(|e| format!("{input}: {e}"))?)
        .map_err(|e| e.to_string())?;
    let mut out = Map::new();
    let ids: Map<String, Value> = atlas
        .fixes
        .iter()
        .chain(atlas.routes.iter())
        .map(|(p, d)| (p.clone(), json!(content_id(d))))
        .collect();
    out.insert("ids".into(), Value::Object(ids));
    let mut fixes = Map::new();
    for (p, f) in &atlas.fixes {
        fixes.insert(p.clone(), compile_fix(atlas, f).unwrap_or_else(|e| json!({"error": e})));
    }
    out.insert("fixes".into(), Value::Object(fixes));
    let mut hops = Map::new();
    let mut prune = Map::new();
    for (p, r) in &atlas.routes {
        let mut m = Map::new();
        for h in atlas::list_of(r, "hops")? {
            let all = hop_dump(atlas, r, &h, None)?;
            let none = hop_dump(atlas, r, &h, Some(&[]))?;
            m.insert(h, json!({"all": all, "none": none}));
        }
        hops.insert(p.clone(), Value::Object(m));
        let a = adapter_of(atlas, r)?;
        let rm = json!({"op": "remove", "packages": ["foo", "bar.baz"], "purge": true});
        prune.insert(p.clone(), json!(backend::step(&rm, a, str_of(r, "to")?)?));
    }
    out.insert("hops".into(), Value::Object(hops));
    out.insert("prune".into(), Value::Object(prune));
    let mut steps = vec![];
    for s in get(&inp, "steps").and_then(Value::as_array).into_iter().flatten() {
        let a = atlas.adapters.get(str_of(s, "family")?).ok_or("unknown family")?;
        steps.push(match backend::step(&s["step"], a, str_of(s, "release")?) {
            Ok(c) => json!(c),
            Err(e) => json!({"error": e}),
        });
    }
    out.insert("steps".into(), Value::Array(steps));
    let mut synth = vec![];
    for s in get(&inp, "synth_hops").and_then(Value::as_array).into_iter().flatten() {
        let a = atlas.adapters.get(str_of(s, "family")?).ok_or("unknown family")?;
        let fs: Vec<&Doc> = s["fixes"].as_array().map(|v| v.iter().collect()).unwrap_or_default();
        synth.push(json!(hop::hop_plan(a, str_of(s, "hop")?, &fs)?.commands));
    }
    out.insert("synth_hops".into(), Value::Array(synth));
    let mut preds = vec![];
    for s in get(&inp, "preds").and_then(Value::as_array).into_iter().flatten() {
        let a = atlas.adapters.get(str_of(s, "family")?).ok_or("unknown family")?;
        preds.push(json!(backend::pred_sh(&s["when"], a)?));
    }
    out.insert("preds".into(), Value::Array(preds));
    let mut cases = vec![];
    for c in get(&inp, "cases").and_then(Value::as_array).into_iter().flatten() {
        let adapters: std::collections::BTreeMap<String, Doc> = c["adapters"]
            .as_object()
            .map(|m| m.iter().map(|(k, v)| (k.clone(), v.clone())).collect())
            .unwrap_or_default();
        let wh = str_of(c, "where")?;
        let errs = match str_of(c, "kind")? {
            "fix" => check::check_fix(&c["doc"], &adapters, wh),
            "adapter" => check::check_adapter(&c["doc"], wh),
            "route" => {
                let fx: Vec<Value> = c["fixes"].as_array().cloned().unwrap_or_default();
                let by_id = fx.iter().map(|f| (content_id(f), f)).collect();
                check::check_route(&c["doc"], &by_id, &adapters, wh)
            }
            k => return Err(format!("unknown case kind {k}")),
        };
        cases.push(json!(errs));
    }
    out.insert("cases".into(), Value::Array(cases));
    println!("{}", serde_json::to_string(&Value::Object(out)).unwrap());
    Ok(true)
}

#[derive(Clone)]
struct WalkOpts {
    root: Option<String>,
    checks: Option<String>,
    box_id: Option<String>,
    out: Option<String>,
    work: String,
    steps: Option<String>,
    run: Option<String>,
    cache: Option<String>,
    sources: Vec<String>,
    archives: Vec<String>,
    no_archive: bool,
    offline: bool,
    live: bool,
}

/// The adapter with its repos pointed at the proxy: apt's mirrors become <proxy>/<host>/<path>, and the shim is the
/// proxy itself (it speaks plain http to the box and fetches https, as the shim does), so a host/path mirror, which
/// goes through the shim, stays as it is.
fn proxied_adapter(a: &Doc, base: &str) -> Result<Doc, String> {
    let mut a = a.clone();
    if let Some(m) = a.get_mut("mirror").and_then(Value::as_object_mut) {
        m.insert("shim".into(), Value::String(base.into()));
    }
    if backend::backend_of(&a) == "apt" {
        let r = a
            .get_mut("releases")
            .and_then(Value::as_object_mut)
            .ok_or("the adapter has no [releases]")?;
        for k in ["archive", "current"] {
            if let Some(Value::String(u)) = r.get(k).filter(|u| u.as_str().is_some_and(|u| u.contains("://"))) {
                let rest = u
                    .split_once("://")
                    .map(|x| x.1)
                    .ok_or(format!("releases.{k} isn't a URL"))?;
                r.insert(k.into(), Value::String(format!("{base}/{rest}")));
            }
        }
    }
    Ok(a)
}

/// SPEC "How a hop runs": everything the client runs or writes in a root gets umask 022, whatever the caller's is
/// (AlmaLinux 10 root sessions default to 077, and files a fix creates must be readable by daemons). Dropping it puts
/// the caller's mask back, for the files written for the caller.
struct RootUmask(libc::mode_t);

impl RootUmask {
    fn set() -> RootUmask {
        // SAFETY: umask only sets this process's file mode mask; it can't fail and touches no memory.
        RootUmask(unsafe { libc::umask(0o022) })
    }
}

impl Drop for RootUmask {
    fn drop(&mut self) {
        // SAFETY: as above.
        unsafe { libc::umask(self.0) };
    }
}

/// The rung a root sits on partway along a route, `None` when it starts at the route's start (or nothing says where
/// it starts), refused when the route doesn't pass through its release short of its end.
fn joins_at(a: &Doc, r: &Doc, release: Option<&String>) -> Result<Option<String>, String> {
    let Some(rel) = release else { return Ok(None) };
    match check::rest_of_route(a, r, rel) {
        Some((_, j)) => Ok(j),
        None => Err(format!(
            "the root is a {rel} machine, and this route starts at {} and doesn't pass through {rel} on the way to \
             {}: wrong route or wrong checks",
            str_of(r, "from")?,
            str_of(r, "to")?
        )),
    }
}

/// What a walk leaves for a rehearsal's plan (SPEC "The plan"), besides its run.
pub struct Walked {
    pub run: Value,
    /// the root before the first hop: its packages with their versions, its release file, the pinned fixes that hold
    pub installed: Vec<String>,
    pub release: String,
    pub holding: Vec<String>,
    /// with --run: the proxy's cache and every "host/path" it served from it
    pub cache: Option<PathBuf>,
    pub served: Vec<(String, PathBuf)>,
    /// every step the walk took, with its commands' output (`--steps` writes the same)
    pub steps: Vec<Value>,
    /// each removal its prune kept
    pub pruned: Vec<Vec<String>>,
}

/// `wend walk` (and the walk `wend rehearse` makes): its run, and what a plan needs from the walk.
fn cmd_walk(atlas: &Atlas, route: &str, o: &WalkOpts) -> Result<Walked, String> {
    let mask = RootUmask::set();
    let (rpath, r) = atlas.route(route)?;
    let a = adapter_of(atlas, r)?;
    let root = o
        .root
        .as_deref()
        .ok_or("walk needs --root DIR (a copy of the machine's root filesystem)")?;
    let checks_path = o
        .checks
        .as_deref()
        .ok_or("walk needs --checks FILE: the health checks a hop must keep green")?;
    // SPEC "Moved mirrors": verified by default. Today's mirrors only when asked, and the run records it.
    match (&o.run, o.live) {
        (None, false) => {
            return Err(
                "walk needs --run RUN.json, a passing CI run of this route whose pinned sources every \
                        download is checked against. To walk against today's mirrors with nothing verified, \
                        pass --live instead (the run records it)."
                    .into(),
            )
        }
        (Some(_), true) => {
            return Err("--run and --live don't mix: --run verifies every download, --live verifies none".into())
        }
        _ => {}
    }
    if unsafe { libc::geteuid() } != 0 {
        return Err("walking a root needs root (chroot, mount). Use a throwaway VM or a copy of the machine.".into());
    }
    // the gate's rules, before anything runs as root: a fix CI's gate would refuse never runs here
    let errs = check::check_atlas(atlas);
    if !errs.is_empty() {
        return Err(format!("the atlas doesn't pass its own check:\n{}", errs.join("\n")));
    }
    for t in ["chroot", "rsync", "mount", "unshare"] {
        if sandbox::host(&format!("command -v {t}")).0 != 0 {
            return Err(format!("missing on this host: {t}"));
        }
    }
    let root = sandbox::Root::new(std::path::Path::new(root))?;
    let work = sandbox::work_dir(std::path::Path::new(&o.work), &root.dir)?;
    let cdoc = atlas::parse_toml(&std::fs::read_to_string(checks_path).map_err(|e| format!("{checks_path}: {e}"))?)?;
    let checks = walk::Checks::from_doc(&cdoc, a)?;
    // where the root starts (before the proxy, which verifies only the hops that will run): the release its checks name, else the one its own release files name, else the route's
    // start. A root on a rung partway along the route takes the hops after it (SPEC "Route").
    let fam = str_of(r, "family")?;
    let found = share::system(&root.dir)
        .release
        .filter(|(f, _)| f == fam)
        .map(|(_, rel)| rel);
    if let (Some(said), Some(seen)) = (&checks.release, &found) {
        if check::rung(a, said) != check::rung(a, seen) {
            return Err(format!(
                "{checks_path} is for a {said} machine, and the root runs {seen}: wrong root or wrong checks"
            ));
        }
    }
    let joins_at = joins_at(a, r, checks.release.as_ref().or(found.as_ref()))?;
    if let Some(rung) = &joins_at {
        eprintln!("the root runs {rung}, partway along the route: walking only the hops after it");
    }
    // --run: the box's repos go through the verifying proxy, which serves only what the run's pinned sources vouch
    // for (SPEC "Pinned sources")
    let proxy = match &o.run {
        Some(p) => {
            let run: Value = serde_json::from_str(&std::fs::read_to_string(p).map_err(|e| format!("{p}: {e}"))?)
                .map_err(|e| format!("{p}: {e}"))?;
            if run["route"].as_str() != Some(content_id(r).as_str()) {
                return Err(format!(
                    "{p} is a run of another route (or another version of this one)"
                ));
            }
            if run["result"] != "pass" {
                return Err(format!("{p} didn't pass: its sources prove nothing"));
            }
            // the cache feeds the root: held to the work directory's rules (root's, nobody else can write)
            let cache = PathBuf::from(o.cache.clone().unwrap_or_else(|| format!("{}/cache", work.display())));
            let cache = sandbox::work_dir(&cache, &root.dir)?;
            // wend's own archive last among the sources: every byte is still checked against the run's pins
            let mut archives = o.archives.clone();
            if !o.offline && !o.no_archive {
                archives.push(fetch::ARCHIVE.to_string());
            }
            // a walk joining partway pins only the hops it will run: earlier hops' snapshots aren't needed
            let mut pins = run.clone();
            if let Some(rung) = &joins_at {
                let rest = check::rest_of_route(a, r, rung).map(|(d, _)| d);
                let left: Vec<String> = rest
                    .as_ref()
                    .and_then(|d| get(d, "hops"))
                    .and_then(atlas::strings)
                    .unwrap_or_default();
                if let Some(Value::Array(hs)) = pins.get_mut("hops") {
                    hs.retain(|h| h["to"].as_str().is_some_and(|t| left.iter().any(|l| l == t)));
                }
            }
            let p = fetch::Proxy::start(&pins, &cache, o.sources.clone(), archives, o.offline)?;
            // before anything touches the root: a snapshot gone from every source refuses here
            p.ensure_pinned().map_err(|e| {
                format!(
                    "{e}\nThe walk stopped before its first step. Add a --source or --archive that still has them, or walk against today's \
                     mirrors with --live (unverified; the run records it)."
                )
            })?;
            Some((p, run))
        }
        None if o.offline || o.no_archive || o.cache.is_some() || !o.sources.is_empty() || !o.archives.is_empty() => {
            return Err(
                "--cache, --source, --archive, --no-archive and --offline need --run RUN.json: its pinned sources \
                 are what gets verified"
                    .into(),
            )
        }
        None => None,
    };
    let pinned_by = proxy
        .as_ref()
        .map(|(_, run)| json!({"route": run["route"], "box": run["box"], "started": run["started"]}));
    let proxy = proxy.map(|(p, _)| p);
    let proxied = proxy.as_ref().map(|p| proxied_adapter(a, &p.base())).transpose()?;
    let compile_with = proxied.as_ref().unwrap_or(a);
    if let (Some(m), None) = (get(a, "mirror"), &proxy) {
        let probe = format!("{}/{}", str_of(m, "shim")?, str_of(m, "probe")?);
        if sandbox::host(&format!("curl -sf -o /dev/null --max-time 60 '{probe}'")).0 != 0 {
            return Err(format!(
                "this family's repos go through a mirror shim, and {probe} isn't answering. \
                 Start it from a wend checkout: python3 tools/mirror.py {} /var/tmp/wend/mirror",
                str_of(m, "shim")?.rsplit(':').next().unwrap_or("8085")
            ));
        }
    }
    let box_id = o.box_id.clone().unwrap_or_else(|| "local".into());
    let snap = work.join(format!("snap-{}", std::process::id()));
    std::fs::create_dir(&snap).map_err(|e| format!("{}: {e}", snap.display()))?;
    eprintln!(
        "walking {rpath} on {} ({} checks); snapshots in {}",
        root.dir.display(),
        checks.checks.len(),
        snap.display()
    );
    let root_dir = root.dir.clone();
    if std::fs::symlink_metadata(root_dir.join("etc/resolv.conf")).is_ok_and(|m| m.file_type().is_symlink()) {
        eprintln!(
            "note: {}/etc/resolv.conf is a symlink. The client never writes through one, so name lookups inside \
             the root may fail; replace it with a plain file if they do.",
            root_dir.display()
        );
    }
    // what a CI box's builder leaves in place for good: daemons never start (SPEC "Sandbox requirements") and apt
    // runs with the harness's config. Lent for the walk only, and given back after.
    let mut lent = vec![];
    if let Some(conf) = get(a, "hop").and_then(|h| get(h, "apt_conf")).and_then(Value::as_str) {
        for (rel, bytes, mode) in [
            ("usr/sbin/policy-rc.d", &b"#!/bin/sh\nexit 101\n"[..], 0o755),
            ("etc/apt/apt.conf.d/00wend", conf.as_bytes(), 0o644),
        ] {
            match root.lend(rel, bytes, mode) {
                Ok(l) => lent.push(l),
                Err(e) => {
                    for l in &lent {
                        let _ = root.give_back(l);
                    }
                    let _ = sandbox::safe_rm(&snap, &work);
                    return Err(e);
                }
            }
        }
    }
    let guard = sandbox::Root { dir: root_dir.clone() };
    let mut w = walk::Walk::new(r, compile_with, atlas.fixes_by_id(), checks, root, snap.clone(), box_id);
    w.sources_adapter = a;
    w.joins_at = joins_at;
    if let Some(p) = &proxy {
        eprintln!(
            "repos go through the verifying proxy at {} (cache {}{})",
            p.base(),
            p.cache().display(),
            if o.offline { ", offline" } else { "" }
        );
    }
    let start = w
        .holding()
        .and_then(|h| Ok((w.installed_versions()?, w.start_release().to_string(), h)));
    let run = start.and_then(|s| w.walk().map(|r| (r, s)));
    if let Some(p) = &proxy {
        let refused = p.take_refused();
        if !refused.is_empty() {
            eprintln!("the proxy refused {} request(s):", refused.len());
            for r in refused.iter().take(20) {
                eprintln!("  {r}");
            }
        }
        eprintln!(
            "note: the root's repo config still points at the proxy ({}), which is gone now. Point it back at a \
             mirror before using its package manager.",
            p.base()
        );
    }
    let returned: Vec<String> = lent.iter().filter_map(|l| guard.give_back(l).err()).collect();
    let run = match (run, returned.is_empty()) {
        (Ok(_), false) => Err(format!(
            "restoring the root's own files failed: {}",
            returned.join("; ")
        )),
        (r, _) => r,
    };
    drop(mask); // the steps and the run file are the caller's, written under the caller's mask
    if let Some(p) = &o.steps {
        let _ = std::fs::write(
            p,
            serde_json::to_string_pretty(&Value::Array(w.steps().to_vec())).unwrap(),
        );
    }
    let (run, (installed, release, holding)) = match run {
        Ok(r) => r,
        // the walk stopped mid-hop: the root may be half-changed, and the snapshot is its last good state
        Err(e) if w.mid_step => {
            return Err(format!(
                "{e}\n{} may be mid-step. Its state before the step is in {}; restore it with: rsync -aHAXx \
                 --modify-window=-1 --delete {}/ {}/ (on apt systems the snapshot has wend's \
                 usr/sbin/policy-rc.d and etc/apt/apt.conf.d/00wend; put the root's own back, or delete them)",
                root_dir.display(),
                snap.display(),
                snap.display(),
                root_dir.display()
            ))
        }
        // stopped between steps (or while taking a snapshot): the root is as the last finished step left it
        Err(e) => {
            sandbox::safe_rm(&snap, &work)?;
            return Err(format!(
                "{e}\n{} is as the last finished step left it.",
                root_dir.display()
            ));
        }
    };
    sandbox::safe_rm(&snap, &work)?;
    let mut run = run;
    if let Value::Object(m) = &mut run {
        // how far to trust the run's sources: verified against another run's pins, or today's mirrors as they were
        m.insert("live".into(), Value::Bool(o.live));
        if let Some(p) = &pinned_by {
            m.insert("pinned_by".into(), p.clone());
        }
    }
    let text = serde_json::to_string_pretty(&run).unwrap() + "\n";
    match &o.out {
        Some(p) => std::fs::write(p, &text).map_err(|e| format!("{p}: {e}"))?,
        None => print!("{text}"),
    }
    let fired: usize = run["hops"]
        .as_array()
        .map(|h| h.iter().map(|x| x["fired"].as_array().map_or(0, Vec::len)).sum())
        .unwrap_or(0);
    eprintln!(
        "{}: reached {} ({} hops, {fired} fixes fired, {} min){}",
        run["result"].as_str().unwrap_or("?").to_uppercase(),
        run["final"]["release"].as_str().unwrap_or("?"),
        run["hops"].as_array().map_or(0, Vec::len),
        run["wall_seconds"].as_u64().unwrap_or(0) / 60,
        match run["final"]["failed"].as_array() {
            Some(f) if !f.is_empty() => format!(
                "; failing checks: {}",
                f.iter().filter_map(Value::as_str).collect::<Vec<_>>().join(", ")
            ),
            _ => String::new(),
        }
    );
    Ok(Walked {
        run,
        installed,
        release,
        holding,
        cache: proxy.as_ref().map(|p| p.cache().to_path_buf()),
        served: proxy.as_ref().map(|p| p.served()).unwrap_or_default(),
        steps: w.steps().to_vec(),
        pruned: w.pruned.clone(),
    })
}

#[cfg(test)]
mod tests {
    use super::RootUmask;

    #[test]
    fn a_root_partway_along_a_route_walks_the_rest_of_it() {
        let atlas = super::Atlas::load(&std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("..")).unwrap();
        let el = &atlas.adapters["el"];
        let (_, c5) = atlas.route("centos5-to-alma10").unwrap();
        let (_, c7) = atlas.route("centos7-to-alma10").unwrap();
        let at = |r, rel: &str| super::joins_at(el, r, Some(&rel.to_string()));
        assert_eq!(super::joins_at(el, c5, None), Ok(None));
        assert_eq!(at(c5, "centos5"), Ok(None));
        assert_eq!(at(c5, "centos6"), Ok(Some("centos6".into())));
        assert_eq!(at(c5, "alma9"), Ok(Some("alma9".into())));
        assert_eq!(at(c7, "sl7"), Ok(None));
        for (r, rel) in [(c7, "centos6"), (c5, "alma10"), (c5, "centos8")] {
            let e = at(r, rel).unwrap_err();
            assert!(e.contains("doesn't pass through"), "{e}");
        }
    }

    fn mask_now() -> libc::mode_t {
        // SAFETY: reads the mask by setting it and putting it straight back.
        unsafe {
            let m = libc::umask(0o077);
            libc::umask(m);
            m
        }
    }

    /// The only test that touches the process umask: 022 while the guard lives, the caller's mask after, also when
    /// the walk panics.
    #[test]
    fn root_umask_is_022_inside_and_restores_the_callers_mask() {
        // SAFETY: as above.
        let saved = unsafe { libc::umask(0o077) };
        let g = RootUmask::set();
        assert_eq!(mask_now(), 0o022);
        drop(g);
        assert_eq!(mask_now(), 0o077);
        let r = std::panic::catch_unwind(|| {
            let _g = RootUmask::set();
            assert_eq!(mask_now(), 0o022);
            panic!("walk failed");
        });
        assert!(r.is_err());
        assert_eq!(mask_now(), 0o077);
        // SAFETY: as above.
        unsafe { libc::umask(saved) };
    }
}
