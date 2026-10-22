//! `wend upgrade`, `wend rollback` and `wend commit` (SPEC "Upgrading a running machine"): on the machine itself,
//! from a plan a rehearsal wrote. Relocate first (the adapter's `[relocate]` steps, as tools/vm.py runs them), so the
//! old system stays bootable through a stub until `wend commit`; then each hop on the new root, booted, offline from
//! the plan's cache through the verifying proxy, with a trial boot after it; the first red rolls back.
//!
//! The work runs detached (`setsid`, as the VM tier's RUNNER), so the owner's ssh session dropping changes nothing,
//! and the owner runs `wend upgrade` again after each boot. Where things stand is the state file, which is written
//! to a new file, synced and renamed before every reboot and every change of phase. Exit codes for a loop to drive:
//! 0 landed (or committed), 10 run me again after the reboot, 20 refused before touching anything, 21 the plan isn't
//! this machine's, 30 rolled back.

use crate::atlas::{get, list_of, str_of, Atlas, Doc};
use crate::backend::{fill, hop_vars, pred_sh, step};
use crate::canon::{canonical, content_id, sha256_hex};
use crate::hop::{hop_plan, pinned};
use crate::sandbox::{q, tail};
use serde_json::{json, Map, Value};
use std::io::{Read, Seek, Write};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

pub const LANDED: u8 = 0;
pub const AGAIN: u8 = 10;
pub const REFUSED: u8 = 20;
pub const MISMATCH: u8 = 21;
pub const ROLLED_BACK: u8 = 30;

/// Where the upgrade keeps its copy of itself, the atlas, its state and its log, on whichever root runs it (relocate
/// copies the old root's into the new one)
pub const DIR: &str = "/var/lib/wend/upgrade";
const STATE: &str = "/var/lib/wend/upgrade/state.json";
const LOG: &str = "/var/lib/wend/upgrade/upgrade.log";
const PID: &str = "/var/lib/wend/upgrade/worker.pid";
const RC: &str = "/var/lib/wend/upgrade/worker.rc";
const RUNS: &str = "/var/lib/wend/runs";
const ENV_FILE: &str = "/var/lib/wend/relocate.env";
/// relocate's flip leaves this once it has armed the trial; cleanup leaves the stopped services to the trial boot
const FLIPPED: &str = "/var/lib/wend/relocate-flipped";
/// the relocate steps tools/vm.py runs, in order; cleanup after them whatever happened
const STEPS: &[&str] = &["preflight", "refresh", "place", "copy", "fixup", "flip", "release"];
/// the line the new root's /etc/motd carries until the upgrade ends
const MOTD: &str =
    "wend: an upgrade is under way on this machine. After each boot run: sudo /var/lib/wend/upgrade/wend upgrade";

/// What every command gets, as the VM tier's ENV: a plain PATH, the C locale and umask 022
const ENV: &str = "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin LANG=C HOME=/root \
                   TERM=dumb; umask 022; ";
/// tools/vm.py's NET: addresses, the default route and the resolvers (systemd-resolved's stub standing for its
/// upstreams); after every boot they must be the ones the machine had before the first hop
const NET: &str = "(ip -o -4 addr 2>/dev/null || /sbin/ip -o -4 addr) | awk '$2 != \"lo\" {print $2, $4}' | sort; \
                   (ip route 2>/dev/null || /sbin/ip route) | awk '/^default/ {print $1, $2, $3}'; \
                   { grep '^nameserver' /etc/resolv.conf | grep -vx 'nameserver 127.0.0.53'; \
                   if grep -qx 'nameserver 127.0.0.53' /etc/resolv.conf; then grep '^nameserver' \
                   /run/systemd/resolve/resolv.conf; fi; } 2>/dev/null | sort -u";
const FW_NOW: &str = "{ [ -d /sys/firmware/efi ] && echo uefi || echo bios; } 2>/dev/null";
/// tools/vm.py's FORCED_RESET, from inside: when `reboot` did nothing (the first boot onto systemd, EL8 to EL9),
/// stop every process but this one, sync, remount read-only and reset with sysrq
const FORCED_RESET: &str = "sleep 2; kill -TERM -1; sleep 10; kill -KILL -1; sleep 3; sync; echo 1 > \
                            /proc/sys/kernel/sysrq; echo s > /proc/sysrq-trigger; sleep 2; echo u > \
                            /proc/sysrq-trigger; sleep 3; echo b > /proc/sysrq-trigger";

pub struct UpgradeOpts {
    /// `--new-root-disk`: the whole spare disk the new root goes on (never picked on its own)
    pub new_root_disk: Option<String>,
    /// `--status`: print the phase and exit with its code, doing nothing
    pub status: bool,
    /// `--work`: the detached worker (started by `wend upgrade` itself)
    pub work: bool,
}

fn log(msg: &str) {
    let t = sh("date '+%H:%M:%S'", 10).1;
    eprintln!("[wend upgrade {}] {msg}", t.trim());
}

/// A command on this machine, through sh with ENV, its output (both streams) and its exit status; 124 when it ran
/// past `secs`. Output goes through a file, so a chatty command can't fill a pipe and stall.
fn sh(cmd: &str, secs: u64) -> (i32, String) {
    let out = match tempfile_in("/var/lib/wend") {
        Ok(f) => f,
        Err(e) => return (-1, e),
    };
    let (f, path) = out;
    let child = Command::new("/bin/sh")
        .arg("-c")
        .arg(format!("{ENV}{cmd}"))
        .stdin(Stdio::null())
        .stdout(f.try_clone().map(Stdio::from).unwrap_or(Stdio::null()))
        .stderr(f.try_clone().map(Stdio::from).unwrap_or(Stdio::null()))
        .spawn();
    let mut child = match child {
        Ok(c) => c,
        Err(e) => {
            let _ = std::fs::remove_file(&path);
            return (-1, e.to_string());
        }
    };
    let t0 = Instant::now();
    let rc = loop {
        match child.try_wait() {
            Ok(Some(st)) => break st.code().unwrap_or(-1),
            Ok(None) if t0.elapsed() > Duration::from_secs(secs) => {
                let _ = child.kill();
                let _ = child.wait();
                break 124;
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(200)),
            Err(_) => break -1,
        }
    };
    let text = String::from_utf8_lossy(&std::fs::read(&path).unwrap_or_default()).into_owned();
    let _ = std::fs::remove_file(&path);
    (rc, text)
}

fn tempfile_in(dir: &str) -> Result<(std::fs::File, PathBuf), String> {
    std::fs::create_dir_all(dir).map_err(|e| format!("{dir}: {e}"))?;
    for i in 0..1000 {
        let p = PathBuf::from(format!("{dir}/.wend-out-{}-{i}", std::process::id()));
        if let Ok(f) = std::fs::OpenOptions::new().write(true).create_new(true).open(&p) {
            return Ok((f, p));
        }
    }
    Err(format!("no temporary file in {dir}"))
}

/// Write a file the way the state needs: a new file, synced, renamed over the old one, the directory synced.
fn save(path: &str, v: &Value) -> Result<(), String> {
    let p = Path::new(path);
    let dir = p.parent().unwrap_or(Path::new("/"));
    std::fs::create_dir_all(dir).map_err(|e| format!("{}: {e}", dir.display()))?;
    let tmp = dir.join(format!(".{}.new", p.file_name().unwrap().to_string_lossy()));
    let mut f = std::fs::File::create(&tmp).map_err(|e| format!("{}: {e}", tmp.display()))?;
    f.write_all((serde_json::to_string_pretty(v).unwrap() + "\n").as_bytes())
        .and_then(|_| f.sync_all())
        .map_err(|e| format!("{}: {e}", tmp.display()))?;
    std::fs::rename(&tmp, p).map_err(|e| format!("{path}: {e}"))?;
    if let Ok(d) = std::fs::File::open(dir) {
        let _ = d.sync_all();
    }
    Ok(())
}

fn load(path: &str) -> Option<Value> {
    serde_json::from_str(&std::fs::read_to_string(path).ok()?).ok()
}

fn now() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn boot_id() -> String {
    std::fs::read_to_string("/proc/sys/kernel/random/boot_id")
        .unwrap_or_default()
        .trim()
        .to_string()
}

fn sha_file(p: &Path) -> Result<String, String> {
    let mut f = std::fs::File::open(p).map_err(|e| format!("{}: {e}", p.display()))?;
    let mut h = <sha2::Sha256 as sha2::Digest>::new();
    let mut buf = vec![0u8; 1 << 20];
    loop {
        let n = f.read(&mut buf).map_err(|e| format!("{}: {e}", p.display()))?;
        if n == 0 {
            break;
        }
        sha2::Digest::update(&mut h, &buf[..n]);
    }
    Ok(sha2::Digest::finalize(h).iter().map(|b| format!("{b:02x}")).collect())
}

/// relocate.env's KEY=VALUE lines
fn relocate_env() -> Map<String, Value> {
    let mut m = Map::new();
    for l in std::fs::read_to_string(ENV_FILE).unwrap_or_default().lines() {
        if let Some((k, v)) = l.split_once('=') {
            m.insert(k.to_string(), json!(v.trim_matches('"')));
        }
    }
    m
}

/// tools/vm.py's kernel_is: whether `uname -r` names the release's newest kernel (EL5's has no arch suffix)
pub fn kernel_is(running: &str, newest: &str) -> bool {
    if running.is_empty() || newest.is_empty() {
        return false;
    }
    let bare = |k: &str| -> String {
        regex::Regex::new(r"\.(x86_64|i[3-6]86|noarch|aarch64|ppc64le|s390x)$")
            .unwrap()
            .replace(k, "")
            .into_owned()
    };
    newest == running || running.rsplit_once('.').is_some_and(|(r, _)| newest == r) || bare(newest) == bare(running)
}

/// The plan as a directory: plan.json, run.json (its pins), checks.toml and cache/
struct Plan {
    dir: PathBuf,
    doc: Value,
    run: Value,
    checks: Doc,
    checks_bytes: Vec<u8>,
}

impl Plan {
    fn read(dir: &Path) -> Result<Plan, String> {
        let rd = |n: &str| std::fs::read(dir.join(n)).map_err(|e| format!("{}: {e}", dir.join(n).display()));
        let doc: Value = serde_json::from_slice(&rd("plan.json")?).map_err(|e| format!("plan.json: {e}"))?;
        if doc["schema"] != "wend.plan/0" {
            return Err(format!(
                "{} isn't a wend plan (schema {})",
                dir.display(),
                doc["schema"]
            ));
        }
        let run: Value = serde_json::from_slice(&rd("run.json")?).map_err(|e| format!("run.json: {e}"))?;
        let checks_bytes = rd("checks.toml")?;
        let checks = crate::atlas::parse_toml(&String::from_utf8_lossy(&checks_bytes))?;
        Ok(Plan {
            dir: dir.to_path_buf(),
            doc,
            run,
            checks,
            checks_bytes,
        })
    }

    fn sha(&self) -> Result<String, String> {
        Ok(format!("sha256:{}", sha_file(&self.dir.join("plan.json"))?))
    }

    fn hops(&self) -> Vec<String> {
        self.doc["hops"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|h| h["to"].as_str().map(String::from))
            .collect()
    }
}

/// The checks the booted machine is held to (SPEC "Checks the machine is held to"): the adapter's the file names, its
/// roles less those `[vm].skip` lists (they start their own daemons), its `[vm].live` as `live:NAME`, and the
/// generated checks' live forms. By name.
fn live_checks(doc: &Doc, adapter: &Doc) -> Result<Map<String, Value>, String> {
    let c = crate::walk::Checks::from_doc(doc, adapter)?;
    let vm = get(doc, "vm");
    let skip: Vec<String> = vm
        .and_then(|v| get(v, "skip"))
        .map(|_| list_of(vm.unwrap(), "skip"))
        .transpose()?
        .unwrap_or_default();
    let gen_offline: Vec<String> = get(doc, "generated")
        .and_then(|g| get(g, "roles"))
        .and_then(Value::as_object)
        .map(|r| r.keys().cloned().collect())
        .unwrap_or_default();
    let mut out = Map::new();
    for (k, v) in c.checks {
        if !skip.contains(&k) && !gen_offline.contains(&k) {
            out.insert(k, v);
        }
    }
    for (k, v) in vm
        .and_then(|v| get(v, "live"))
        .and_then(Value::as_object)
        .into_iter()
        .flatten()
    {
        out.insert(format!("live:{k}"), v.clone());
    }
    for (k, v) in get(doc, "generated")
        .and_then(|g| get(g, "live"))
        .and_then(Value::as_object)
        .into_iter()
        .flatten()
    {
        out.insert(k.clone(), v.clone());
    }
    Ok(out)
}

/// Each check run on this machine: name -> (passed, the tail of its output)
fn run_checks(checks: &Map<String, Value>) -> Map<String, Value> {
    let mut out = Map::new();
    for (n, c) in checks {
        let c = c.as_str().unwrap_or("false");
        let (rc, o) = sh(
            &format!(
                "( {c} ) >/var/lib/wend/upgrade/.check 2>&1; rc=$?; tail -c 800 /var/lib/wend/upgrade/.check; exit $rc"
            ),
            300,
        );
        out.insert(
            n.clone(),
            json!({"ok": rc == 0, "out": if rc == 0 { String::new() } else { o }}),
        );
    }
    out
}

fn passed(results: &Map<String, Value>) -> Vec<String> {
    results
        .iter()
        .filter(|(_, r)| r["ok"] == json!(true))
        .map(|(n, _)| n.clone())
        .collect()
}

/// Whether sshd answers a new connection on its port with its greeting (SPEC "Checks the machine is held to")
fn sshd_answers() -> bool {
    let port = sh(
        "P=$(/usr/sbin/sshd -T 2>/dev/null | awk '$1 == \"port\" {print $2; exit}'); [ -n \"$P\" ] || \
         P=$(awk '$1 == \"Port\" {print $2; exit}' /etc/ssh/sshd_config 2>/dev/null); echo ${P:-22}",
        60,
    )
    .1
    .trim()
    .parse::<u16>()
    .unwrap_or(22);
    for _ in 0..15 {
        if let Ok(mut s) = std::net::TcpStream::connect_timeout(&([127, 0, 0, 1], port).into(), Duration::from_secs(5))
        {
            let _ = s.set_read_timeout(Some(Duration::from_secs(5)));
            let mut b = [0u8; 4];
            if s.read_exact(&mut b).is_ok() && &b == b"SSH-" {
                return true;
            }
        }
        std::thread::sleep(Duration::from_secs(2));
    }
    false
}

/// The machine's fingerprint as `wend rehearse` takes it from the copy (SPEC "The plan")
fn fingerprint(route: &Doc, adapter: &Doc, fixes: &std::collections::BTreeMap<String, &Doc>) -> Result<Value, String> {
    let hop = get(adapter, "hop").ok_or("the adapter has no [hop]")?;
    let (rc, out) = sh(str_of(hop, "installed_versions")?, 1800);
    if rc != 0 {
        return Err(format!("listing this machine's packages failed: {}", tail(&out, 400)));
    }
    let mut installed: Vec<String> = out.split_whitespace().map(String::from).collect();
    installed.sort();
    let rf = str_of(adapter, "release_file").unwrap_or("/etc/os-release");
    let release = sh(&format!("cat {rf}"), 60).1.trim().to_string();
    let mut holding = vec![];
    for (id, f) in pinned(route, fixes)? {
        let p = pred_sh(get(f, "when").ok_or("a fix without a when")?, adapter)?;
        if sh(&format!("{p} >/dev/null 2>&1"), 1800).0 == 0 {
            holding.push(id);
        }
    }
    holding.sort();
    Ok(json!({
        "installed_sha256": format!("sha256:{}", sha256_hex(installed.join("\n").as_bytes())),
        "installed": installed, "kernel": sh("uname -r", 30).1.trim(), "release": release, "holding": holding,
    }))
}

/// What differs between two fingerprints, for the refusal
fn differences(want: &Value, got: &Value) -> Vec<String> {
    let mut d = vec![];
    for k in ["kernel", "release"] {
        if want[k] != got[k] {
            d.push(format!("{k}: the plan's is {}, this machine's {}", want[k], got[k]));
        }
    }
    let set = |v: &Value, k: &str| -> std::collections::BTreeSet<String> {
        v[k].as_array()
            .into_iter()
            .flatten()
            .filter_map(|x| x.as_str().map(String::from))
            .collect()
    };
    for (k, what) in [("installed", "packages"), ("holding", "fixes whose preconditions hold")] {
        let (w, g) = (set(want, k), set(got, k));
        let only_plan: Vec<&String> = w.difference(&g).take(10).collect();
        let only_here: Vec<&String> = g.difference(&w).take(10).collect();
        if !only_plan.is_empty() || !only_here.is_empty() {
            d.push(format!(
                "{what}: only in the plan {only_plan:?}, only on this machine {only_here:?}"
            ));
        }
    }
    d
}

fn route_of<'a>(atlas: &'a Atlas, plan: &Plan) -> Result<(&'a String, &'a Doc), String> {
    let id = plan.doc["route"].as_str().unwrap_or("");
    atlas.routes.iter().find(|(_, d)| content_id(d) == id).ok_or(format!(
        "this atlas has no route {id} (the plan's): use the atlas the plan was rehearsed with"
    ))
}

/// The relocate table this machine takes: GRUB legacy's when it boots from /boot/grub/grub.conf with no /boot/grub2
fn table_for(adapter: &Doc) -> &'static str {
    if get(adapter, "relocate_grub_legacy").is_some()
        && sh("[ -f /boot/grub/grub.conf ] && [ ! -e /boot/grub2 ]", 30).0 == 0
    {
        "relocate_grub_legacy"
    } else {
        "relocate"
    }
}

/// tools/vm.py's route_mib and route_boot_mib, over the hops the plan walks, plus the plan's cache (it is copied)
fn room(
    adapter: &Doc,
    route: &Doc,
    fixes: &std::collections::BTreeMap<String, &Doc>,
    hops: &[String],
    cache_mib: u64,
) -> Result<(u64, u64), String> {
    let hw = get(adapter, "hardware");
    let n = |h: &str, k: &str| {
        hw.and_then(|x| get(x, h))
            .and_then(|x| get(x, k))
            .and_then(Value::as_u64)
            .unwrap_or(0)
    };
    let mut transit = std::collections::BTreeSet::new();
    for (_, f) in pinned(route, fixes)? {
        if !get(f, "hop")
            .and_then(Value::as_str)
            .is_some_and(|h| hops.iter().any(|x| x == h))
        {
            continue;
        }
        for ph in ["pre", "post"] {
            for s in get(f, ph).and_then(Value::as_array).into_iter().flatten() {
                if get(s, "op").and_then(Value::as_str) == Some("rpm.transit") {
                    transit.insert(str_of(s, "release")?.to_string());
                }
            }
        }
    }
    let t: u64 = transit
        .iter()
        .map(|r| {
            get(adapter, "releases")
                .and_then(|x| get(x, r))
                .and_then(|x| get(x, "transit"))
                .and_then(|x| get(x, "boot_mib"))
                .and_then(Value::as_u64)
                .unwrap_or(0)
        })
        .sum();
    let boot: u64 = hops.iter().map(|h| n(h, "boot_mib")).sum::<u64>() + t;
    let disk: u64 = hops.iter().map(|h| n(h, "disk_mib")).sum::<u64>() + boot + cache_mib;
    Ok((disk, boot))
}

fn rel_cmd(adapter: &Doc, table: &str, k: &str) -> Result<String, String> {
    Ok(str_of(get(adapter, table).ok_or(format!("the adapter has no [{table}]"))?, k)?.to_string())
}

/// Which root is booted: the adapter's `booted` ("new", "old" or what it is instead); "unrelocated" before relocate
fn booted(adapter: &Doc, table: &str) -> String {
    if !Path::new(ENV_FILE).exists() {
        return "unrelocated".into();
    }
    match rel_cmd(adapter, table, "booted") {
        Ok(c) => sh(&c, 60).1.trim().to_string(),
        Err(e) => e,
    }
}

pub fn main(atlas_dir: &Path, args: &[String], o: &UpgradeOpts) -> u8 {
    let args: Vec<&str> = args.iter().map(String::as_str).collect();
    let r = match args[..] {
        ["upgrade"] | ["upgrade", _] if o.status => Ok(status()),
        ["upgrade"] if o.work => worker(),
        ["rollback"] => by_hand_rollback(),
        ["commit"] => commit(),
        ["upgrade", plan] => start(atlas_dir, Some(&plan.to_string()), o),
        ["upgrade"] => start(atlas_dir, None, o),
        _ => Err("usage: wend upgrade [PLAN --new-root-disk DEV] | --status; wend rollback; wend commit".into()),
    };
    match r {
        Ok(c) => c,
        Err(e) => {
            eprintln!("wend: {e}");
            REFUSED
        }
    }
}

fn code_of(phase: &str) -> u8 {
    match phase {
        "landed" | "committed" => LANDED,
        "rolled-back" | "rollback-failed" | "relocate-failed" => ROLLED_BACK,
        _ => AGAIN,
    }
}

fn status() -> u8 {
    match load(STATE) {
        Some(s) => {
            let p = s["phase"].as_str().unwrap_or("?");
            println!(
                "phase: {p}{}",
                s["hop_to"]
                    .as_str()
                    .map(|h| format!(" (hop to {h})"))
                    .unwrap_or_default()
            );
            if let Some(w) = s["why"].as_str() {
                println!("why: {w}");
            }
            code_of(p)
        }
        None => {
            println!("no upgrade under way on this root ({STATE} isn't there)");
            REFUSED
        }
    }
}

/// `wend upgrade PLAN`: the refusals, then the detached work. With a state file already there, `wend upgrade` goes
/// on with it (or follows the worker that is).
fn start(atlas_dir: &Path, plan_arg: Option<&String>, o: &UpgradeOpts) -> Result<u8, String> {
    if unsafe { libc::geteuid() } != 0 {
        return Err("wend upgrade runs as root".into());
    }
    if let Some(s) = load(STATE) {
        if let Some(p) = plan_arg {
            let given = std::fs::canonicalize(p).map_err(|e| format!("{p}: {e}"))?;
            if s["plan"].as_str() != Some(&given.to_string_lossy()) {
                return Err(format!(
                    "an upgrade from another plan ({}) is under way here: a second plan refuses while {STATE} is there",
                    s["plan"]
                ));
            }
        }
        return follow_or_spawn();
    }
    let p = plan_arg.ok_or("wend upgrade needs PLAN, the directory `wend rehearse` wrote, on a first start")?;
    match refusals(atlas_dir, p, o) {
        Ok(state) => {
            std::fs::create_dir_all(DIR).map_err(|e| format!("{DIR}: {e}"))?;
            // the worker's own copies: the static binary and the atlas, as they were when the upgrade started
            let me = std::env::current_exe().map_err(|e| e.to_string())?;
            let bin = Path::new(DIR).join("wend");
            if me != bin {
                std::fs::copy(&me, &bin).map_err(|e| format!("{}: {e}", bin.display()))?;
            }
            let (rc, out) = sh(
                &format!(
                    "rm -rf {d}/atlas && mkdir -p {d}/atlas && cd {a} && for x in adapters fixes routes runs .wend/sonames; do \
                     [ -e $x ] || continue; mkdir -p {d}/atlas/$(dirname $x) && cp -a $x {d}/atlas/$x || exit 1; done",
                    d = DIR,
                    a = q(&atlas_dir.to_string_lossy())
                ),
                600,
            );
            if rc != 0 {
                return Err(format!("copying the atlas to {DIR}/atlas failed: {out}"));
            }
            save(STATE, &state)?;
            println!(
                "An undo on the same machine is no help against its disk dying: a backup is still yours to take.\n\
                 The rehearsal's hops took {} s; add relocate's second copy pass and {} reboots. Each boot after the \
                 first is a trial: if it doesn't come up, the old system does.",
                state["rehearsed_seconds"],
                state["hops_to"].as_array().map(|h| h.len() + 1).unwrap_or(1)
            );
            follow_or_spawn()
        }
        Err((code, why)) => {
            eprintln!("wend: refused, nothing was touched: {why}");
            Ok(code)
        }
    }
}

/// SPEC "The upgrade": everything checked before anything is touched. The state the worker starts from, or the exit
/// code and why.
fn refusals(atlas_dir: &Path, plan_path: &str, o: &UpgradeOpts) -> Result<Value, (u8, String)> {
    let r = |e: String| (REFUSED, e);
    // booted from its own root: not a container, not a chroot
    let (rc, _) = sh(
        "[ / -ef /proc/1/root ] && [ ! -e /.dockerenv ] && [ ! -e /run/.containerenv ] && ! tr '\\0' '\\n' < \
         /proc/1/environ | grep -q '^container='",
        30,
    );
    if rc != 0 {
        return Err(r(
            "this isn't a machine booted from its own root (a container or a chroot)".into(),
        ));
    }
    let dir = std::fs::canonicalize(plan_path).map_err(|e| r(format!("{plan_path}: {e}")))?;
    let d = dir.to_string_lossy().into_owned();
    if ["/tmp", "/run", "/var/tmp", "/dev/shm"]
        .iter()
        .any(|t| d == *t || d.starts_with(&format!("{t}/")))
    {
        return Err(r(format!(
            "the plan is under {d}, which doesn't survive the reboots: move it (e.g. to /root)"
        )));
    }
    let plan = Plan::read(&dir).map_err(r)?;
    let atlas = Atlas::load(atlas_dir).map_err(r)?;
    crate::preflight(&atlas).map_err(r)?;
    let (rpath, route) = route_of(&atlas, &plan).map_err(r)?;
    let adapter = crate::adapter_of(&atlas, route).map_err(r)?;
    if plan.doc["pinned_by"].is_null() || plan.run["result"] != "pass" {
        return Err(r("the plan isn't pinned by a passing run".into()));
    }
    if plan.doc.get("prune").is_none() {
        return Err(r(
            "the plan predates the prune list `upgrade` needs: rehearse again".into()
        ));
    }
    if format!("sha256:{}", sha256_hex(&plan.checks_bytes)) != plan.doc["checks_sha256"].as_str().unwrap_or("") {
        return Err(r(
            "checks.toml isn't the one the rehearsal ran: a check it never ran proves nothing".into(),
        ));
    }
    // every file the plan lists is in its cache and hashes right: nothing is fetched from a network
    let files = std::fs::read_to_string(dir.join("cache/files.txt")).map_err(|e| r(format!("cache/files.txt: {e}")))?;
    let mut cache_bytes = 0u64;
    let mut bad = vec![];
    for l in files.lines().filter(|l| !l.trim().is_empty()) {
        let mut f = l.split(' ');
        let (Some(want), Some(rel)) = (f.next(), f.next()) else {
            return Err(r(format!(
                "cache/files.txt has a line that isn't `sha256:HEX PATH HOST/PATH`: {l}"
            )));
        };
        let p = dir.join("cache").join(rel);
        cache_bytes += std::fs::metadata(&p).map(|m| m.len()).unwrap_or(0);
        match sha_file(&p) {
            Ok(h) if format!("sha256:{h}") == want => {}
            Ok(_) => bad.push(format!("{rel} doesn't hash to {want}")),
            Err(e) => bad.push(e),
        }
    }
    if !bad.is_empty() {
        return Err(r(format!(
            "the plan's cache isn't whole: {}",
            bad.into_iter().take(10).collect::<Vec<_>>().join("; ")
        )));
    }
    if plan.doc["cache_files"].as_u64() != Some(files.lines().filter(|l| !l.trim().is_empty()).count() as u64) {
        return Err(r("cache/files.txt doesn't list the files the plan says it has".into()));
    }
    // the machine is the one rehearsed (another machine that matches it is too: SPEC "Fleets")
    let fixes = atlas.fixes_by_id();
    let fp = fingerprint(route, adapter, &fixes).map_err(r)?;
    if format!("sha256:{}", sha256_hex(canonical(&fp).as_bytes()))
        != plan.doc["fingerprint_sha256"].as_str().unwrap_or("")
    {
        let d = differences(&plan.doc["fingerprint"], &fp);
        return Err((
            MISMATCH,
            format!(
                "this machine isn't the one the plan was rehearsed on (or it changed since): {}. Rehearse it again.",
                if d.is_empty() {
                    "its fingerprint differs".into()
                } else {
                    d.join("; ")
                }
            ),
        ));
    }
    // hardware preflight over the hops left, on this machine's own facts
    let facts = crate::hardware::gather().map_err(r)?;
    let rest = crate::share::system(Path::new("/"))
        .release
        .and_then(|(_, rel)| crate::check::rest_of_route(adapter, route, &rel))
        .map(|(d, _)| d)
        .unwrap_or_else(|| route.clone());
    let v = crate::hardware::evaluate(&atlas, &rest, &facts).map_err(r)?;
    if v["ok"] != json!(true) {
        return Err(r(format!(
            "hardware preflight stops this machine short of the route's end:\n{}",
            crate::hardware::report(&v)
        )));
    }
    // relocate's preflight, with a place for the new root (only a disk the owner names, so far)
    let disk = o.new_root_disk.clone().ok_or(r(
        "name the spare disk the new root goes on with --new-root-disk /dev/disk/by-id/... (the old system stays \
         untouched on its own disk until `wend commit`; free extents in the root's volume group aren't built yet)"
            .into(),
    ))?;
    let table = table_for(adapter);
    let hops = plan.hops();
    let (route_mib, boot_mib) = room(adapter, route, &fixes, &hops, cache_bytes / 1048576 + 1).map_err(r)?;
    let fills = vec![
        ("new_disk".to_string(), disk.clone()),
        ("route_mib".to_string(), route_mib.to_string()),
        ("boot_mib".to_string(), boot_mib.to_string()),
    ];
    for c in list_of(
        get(adapter, table).ok_or(r(format!("the adapter has no [{table}]")))?,
        "preflight",
    )
    .map_err(r)?
    {
        let (rc, out) = sh(&fill(&c, &fills), 600);
        if rc != 0 {
            return Err(r(format!("relocate's preflight: {}", out.trim())));
        }
        print!("{out}");
    }
    let rehearsed: f64 = plan.doc["hops"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|h| h["seconds"].as_f64())
        .sum();
    Ok(json!({
        "schema": "wend.upgrade/0", "plan": d, "plan_sha256": plan.sha().map_err(r)?, "route": rpath,
        "route_id": plan.doc["route"], "from": plan.doc["from"], "to": plan.doc["to"], "hops_to": hops,
        "table": table, "fills": fills.iter().map(|(k, v)| (k.clone(), json!(v))).collect::<Map<_, _>>(),
        "phase": "start", "hop": 0, "hops": [], "retries": {}, "rehearsed_seconds": rehearsed.round(),
        "started": sh("date -u +%Y%m%dT%H%M%SZ", 10).1.trim(), "started_at": now(), "reboots": [],
    }))
}

/// The worker is running: follow its log. Else start it, detached, and follow that.
fn follow_or_spawn() -> Result<u8, String> {
    let alive = |pid: i32| pid > 0 && unsafe { libc::kill(pid, 0) } == 0;
    // the pid file names the boot it was written in: relocate copies it into the new root, where that pid is nobody
    let running = std::fs::read_to_string(PID)
        .ok()
        .and_then(|p| match p.split_whitespace().collect::<Vec<_>>()[..] {
            [pid, boot] if boot == boot_id() => pid.parse::<i32>().ok(),
            _ => None,
        })
        .filter(|p| alive(*p));
    let start_at = std::fs::metadata(LOG).map(|m| m.len()).unwrap_or(0);
    let mut child = None;
    let pid = match running {
        Some(p) => {
            eprintln!("the upgrade is already running (pid {p}): following its log ({LOG})");
            p
        }
        None => {
            let _ = std::fs::remove_file(RC);
            let logf = std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(LOG)
                .map_err(|e| format!("{LOG}: {e}"))?;
            let mut c = Command::new(Path::new(DIR).join("wend"));
            c.args(["--atlas", &format!("{DIR}/atlas"), "upgrade", "--work"])
                .current_dir("/")
                .stdin(Stdio::null())
                .stdout(logf.try_clone().map_err(|e| e.to_string())?)
                .stderr(logf);
            // SAFETY: setsid only makes the child the leader of a new session; it runs between fork and exec
            unsafe {
                c.pre_exec(|| {
                    libc::setsid();
                    Ok(())
                });
            }
            let ch = c.spawn().map_err(|e| format!("starting the upgrade's worker: {e}"))?;
            let p = ch.id() as i32;
            child = Some(ch);
            p
        }
    };
    let mut f = std::fs::File::open(LOG).map_err(|e| format!("{LOG}: {e}"))?;
    let _ = f.seek(std::io::SeekFrom::Start(if running.is_some() {
        start_at.saturating_sub(2000)
    } else {
        start_at
    }));
    let mut buf = vec![];
    loop {
        buf.clear();
        let _ = f.read_to_end(&mut buf);
        if !buf.is_empty() {
            let _ = std::io::stderr().write_all(&buf);
        }
        // the worker writes its exit code before a reboot, so the owner's session hears 10 before ssh goes down
        let done = std::fs::metadata(RC).is_ok()
            || match child.as_mut() {
                Some(c) => c.try_wait().map(|s| s.is_some()).unwrap_or(true),
                None => !alive(pid),
            };
        if done {
            buf.clear();
            let _ = f.read_to_end(&mut buf);
            let _ = std::io::stderr().write_all(&buf);
            break;
        }
        std::thread::sleep(Duration::from_millis(500));
    }
    Ok(std::fs::read_to_string(RC)
        .ok()
        .and_then(|s| s.trim().parse().ok())
        .unwrap_or_else(|| {
            load(STATE)
                .map(|s| code_of(s["phase"].as_str().unwrap_or("")))
                .unwrap_or(REFUSED)
        }))
}

/// The detached worker: one step of the state machine after another until it reboots, lands or rolls back.
fn worker() -> Result<u8, String> {
    let lock = std::fs::OpenOptions::new()
        .create(true)
        .write(true)
        .open(format!("{DIR}/lock"))
        .map_err(|e| e.to_string())?;
    use std::os::unix::io::AsRawFd;
    // SAFETY: flock on a file descriptor this process owns
    if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
        return Err("another wend upgrade worker holds the lock".into());
    }
    let _ = std::fs::write(PID, format!("{} {}", std::process::id(), boot_id()));
    unsafe { libc::umask(0o022) };
    let code = match Worker::new() {
        Ok(mut w) => w.run().unwrap_or_else(|e| {
            log(&format!("stopped: {e}"));
            w.red(&format!("the upgrade's own error: {e}")).unwrap_or(ROLLED_BACK)
        }),
        Err(e) => {
            log(&format!("can't go on: {e}"));
            REFUSED
        }
    };
    let _ = std::fs::write(RC, code.to_string());
    let _ = std::fs::remove_file(PID);
    Ok(code)
}

struct Worker {
    atlas: Atlas,
    s: Value,
    plan: Plan,
    /// the proxy once started: it serves until the process ends, so a second start on its port would fail
    px: Option<crate::fetch::Proxy>,
}

impl Worker {
    fn new() -> Result<Worker, String> {
        let s = load(STATE).ok_or(format!("{STATE} can't be read"))?;
        let atlas = Atlas::load(&PathBuf::from(format!("{DIR}/atlas")))?;
        let plan = Plan::read(Path::new(s["plan"].as_str().unwrap_or("")))?;
        if plan.sha()? != s["plan_sha256"].as_str().unwrap_or("") {
            return Err("the plan changed since the upgrade started".into());
        }
        Ok(Worker {
            atlas,
            s,
            plan,
            px: None,
        })
    }

    fn route(&self) -> Result<&Doc, String> {
        Ok(route_of(&self.atlas, &self.plan)?.1)
    }

    fn adapter(&self) -> Result<&Doc, String> {
        crate::adapter_of(&self.atlas, self.route()?)
    }

    /// the atlas's fixes by id, owned (so the state can change while they're in use)
    fn fixes(&self) -> std::collections::BTreeMap<String, Doc> {
        self.atlas
            .fixes_by_id()
            .into_iter()
            .map(|(k, v)| (k, v.clone()))
            .collect()
    }

    fn table(&self) -> String {
        self.s["table"].as_str().unwrap_or("relocate").to_string()
    }

    fn phase(&self) -> String {
        self.s["phase"].as_str().unwrap_or("").to_string()
    }

    fn set(&mut self, phase: &str) -> Result<(), String> {
        self.s["phase"] = json!(phase);
        log(&format!("phase: {phase}"));
        save(STATE, &self.s)
    }

    fn checks(&self) -> Result<Map<String, Value>, String> {
        live_checks(&self.plan.checks, self.adapter()?)
    }

    fn run(&mut self) -> Result<u8, String> {
        let adapter = self.adapter()?.clone();
        loop {
            let phase = self.phase();
            match phase.as_str() {
                "rolled-back" | "rollback-failed" | "relocate-failed" => {
                    println!("{phase}: {}", self.s["why"].as_str().unwrap_or(""));
                    return Ok(ROLLED_BACK);
                }
                "relocating" => {
                    // the worker stopped mid-relocate (a crash, a power cut): the old system runs, its default
                    // unchanged; what was copied stays on the new disk. A flip that ran armed the trial, which the
                    // next ordinary reboot would take onto a copy nothing judged: disarm it as a failed relocate does
                    let t = get(&adapter, &self.table()).ok_or("no relocate table")?.clone();
                    let mut stages: Vec<Value> = vec![];
                    let flipped = self.s["flip_started"] == json!(true) || Path::new(FLIPPED).exists();
                    let armed = flipped && self.disarm(&t, &mut stages)?;
                    self.cleanup(&t)?;
                    self.s["relocate_recovery"] = json!(stages);
                    self.s["why"] = json!(if armed {
                        format!(
                            "relocate was interrupted after the flip, and disarming the trial failed: the next \
                             reboot may boot the copy once; run `{}` before rebooting",
                            str_of(&t, "rollback_next")?
                        )
                    } else {
                        "relocate was interrupted".to_string()
                    });
                    self.set("relocate-failed")?;
                    return Ok(ROLLED_BACK);
                }
                _ => {}
            }
            let on = booted(&adapter, &self.table());
            if on == "old" && phase != "start" {
                return self.on_old_root();
            }
            match phase.as_str() {
                "start" => return self.relocate(),
                "relocated" | "after-hop" | "mid-hop" => {
                    if !self.judge(&on)? {
                        return Ok(ROLLED_BACK);
                    }
                    if phase == "mid-hop" {
                        return self.post_part();
                    }
                    let next = if phase == "relocated" {
                        0
                    } else {
                        self.s["hop"].as_u64().unwrap_or(0) + 1
                    };
                    self.s["hop"] = json!(next);
                    self.set("ready")?;
                }
                "ready" => {
                    let i = self.s["hop"].as_u64().unwrap_or(0) as usize;
                    if i >= self.plan.hops().len() {
                        return self.finish();
                    }
                    return self.hop(i);
                }
                "hopping" | "finishing" => {
                    // the worker stopped partway through a transaction (a crash, a kill): what it changed can't be
                    // picked up where it stopped, so the old system comes back
                    let what = if phase == "hopping" { "a hop" } else { "the finish" };
                    return self.red(&format!("the worker stopped partway through {what}"));
                }
                "landed" | "committed" => {
                    println!("the machine has landed ({phase}); `wend commit` makes the new system stay");
                    return Ok(LANDED);
                }
                "rolling-back" => {
                    // a rollback reboot that came up on the new root again: the stub didn't take it
                    self.s["why"] = json!(format!(
                        "{} (and the rollback reboot came back on the new root)",
                        self.s["why"].as_str().unwrap_or("")
                    ));
                    self.set("rollback-failed")?;
                    log("LOUD: the rollback didn't bring back the old system; the run and the state say why");
                    return Ok(ROLLED_BACK);
                }
                p => {
                    log(&format!("nothing to do in phase {p}"));
                    return Ok(code_of(p));
                }
            }
        }
    }

    /// SPEC "The upgrade" 2: the start snapshot the machine is held to, then relocate as built, and the trial boot.
    fn relocate(&mut self) -> Result<u8, String> {
        let adapter = self.adapter()?.clone();
        let table = self.table();
        log("reading the machine as it is: the checks, the network, the firmware");
        let results = run_checks(&self.checks()?);
        self.s["start_checks"] = json!(passed(&results));
        self.s["start_net"] = json!(sh(NET, 60).1);
        self.s["start_firmware"] = json!(sh(FW_NOW, 30).1.trim());
        self.s["start_kernel"] = json!(sh("uname -r", 30).1.trim());
        log(&format!(
            "start: {}/{} checks pass",
            passed(&results).len(),
            results.len()
        ));
        self.set("relocating")?;
        let fills: Vec<(String, String)> = self.s["fills"]
            .as_object()
            .into_iter()
            .flatten()
            .map(|(k, v)| (k.clone(), v.as_str().unwrap_or("").to_string()))
            .collect();
        let t = get(&adapter, &table).ok_or(format!("no [{table}]"))?.clone();
        let mut stages = vec![];
        let mut ok = true;
        for k in STEPS {
            if *k == "flip" {
                // the new root's own state, before the flip arms its trial: what that boot is held to
                let mut s = self.s.clone();
                s["phase"] = json!("relocated");
                s["boot_id"] = json!(boot_id());
                s["relocate"] = json!(stages);
                s["reboots"] = json!([{"for": "the trial boot onto the new root", "at": now()}]);
                s["expect"] = json!({"release": crate::share::system(Path::new("/")).release.map(|r| r.1)});
                save(&format!("/run/wend/new{STATE}"), &s)?;
                let motd = "/run/wend/new/etc/motd";
                let old = std::fs::read_to_string(motd).unwrap_or_default();
                std::fs::write(motd, format!("{old}{MOTD}\n")).map_err(|e| format!("{motd}: {e}"))?;
                // a worker that stops from here on may leave the trial armed, before the flip's marker says so
                self.s["flip_started"] = json!(true);
                save(STATE, &self.s)?;
            }
            let t0 = Instant::now();
            for c in list_of(&t, k)? {
                let (rc, out) = sh(&fill(&c, &fills), 4 * 3600);
                log(&format!("relocate {k}: rc {rc}\n{}", tail(&out, 3000)));
                stages.push(json!({"stage": k, "rc": rc, "out": tail(&out, 3000), "seconds": t0.elapsed().as_secs()}));
                if rc != 0 {
                    ok = false;
                    break;
                }
            }
            if !ok {
                break;
            }
        }
        // a step at or after the flip failed: the flip may have armed the one-shot trial, which the next ordinary
        // reboot would take onto a root nothing judged. Disarm it (the next boot is the old system, as is the default)
        // and drop the flip's marker, so cleanup starts the services the copy stopped
        let armed = !ok && stages.iter().any(|s| s["stage"] == json!("flip")) && self.disarm(&t, &mut stages)?;
        self.cleanup(&t)?;
        self.s["relocate"] = json!(stages);
        if !ok {
            // before the flip nothing on the old root changed; after it, the trial is disarmed and the default is
            // still the old system
            let step = stages
                .iter()
                .rev()
                .find(|s| s["rc"] != json!(0) && s["stage"] != json!("disarm"))
                .and_then(|s| s["stage"].as_str())
                .unwrap_or("?");
            self.s["why"] = json!(if armed {
                format!(
                    "relocate's {step} step failed, and disarming the trial failed too: the next reboot may boot \
                     the copy once; run `{}` before rebooting",
                    str_of(&t, "rollback_next")?
                )
            } else {
                format!("relocate's {step} step failed")
            });
            self.s["phase"] = json!("relocate-failed");
            save(STATE, &self.s)?;
            log(&format!(
                "relocate failed: {}. The machine runs its old system; the new disk holds what was copied",
                self.s["why"].as_str().unwrap_or("")
            ));
            self.write_run("relocate-failed")?;
            return Ok(ROLLED_BACK);
        }
        self.s["phase"] = json!("relocated");
        self.reboot("the trial boot onto the new root", false)
    }

    /// Undo a flip's one-shot trial (the next boot is the old system, as is the default) and drop the flip's marker, so
    /// cleanup starts the services the copy stopped. True: disarming failed and the trial may still be armed.
    fn disarm(&self, t: &Value, stages: &mut Vec<Value>) -> Result<bool, String> {
        let (rc, out) = sh(str_of(t, "rollback_next")?, 600);
        log(&format!(
            "relocate failed after the flip, disarming the trial: rc {rc} {}",
            tail(&out, 500)
        ));
        stages.push(json!({"stage": "disarm", "rc": rc, "out": tail(&out, 3000)}));
        if rc == 0 {
            let _ = std::fs::remove_file(FLIPPED);
        }
        Ok(rc != 0)
    }

    /// relocate's cleanup: unmount what it mounted, start what it stopped unless the flip stands
    fn cleanup(&self, t: &Value) -> Result<(), String> {
        let fills: Vec<(String, String)> = self.s["fills"]
            .as_object()
            .into_iter()
            .flatten()
            .map(|(k, v)| (k.clone(), v.as_str().unwrap_or("").to_string()))
            .collect();
        for c in list_of(t, "cleanup")? {
            let (rc, out) = sh(&fill(&c, &fills), 600);
            log(&format!("relocate cleanup: rc {rc} {}", tail(&out, 500)));
        }
        Ok(())
    }

    /// Judge the boot that just came up on the new root (SPEC "Checks the machine is held to"). False: it was red and
    /// the rollback is under way.
    fn judge(&mut self, on: &str) -> Result<bool, String> {
        let adapter = self.adapter()?.clone();
        let phase = self.phase();
        let mut j = Map::new();
        j.insert("booted".into(), json!(on));
        let new_boot = boot_id() != self.s["boot_id"].as_str().unwrap_or("");
        j.insert("new_boot".into(), json!(new_boot));
        let fw = sh(FW_NOW, 30).1.trim().to_string();
        j.insert("firmware".into(), json!(fw));
        let mut why = vec![];
        if on != "new" {
            why.push(format!("booted from {on:?}, not the new root"));
        }
        if !new_boot {
            why.push("no reboot happened (the same boot_id)".into());
        }
        if Some(fw.as_str()) != self.s["start_firmware"].as_str() {
            why.push(format!("booted on {fw}, not {}", self.s["start_firmware"]));
        }
        let rf = str_of(&adapter, "release_file").unwrap_or("/etc/os-release");
        let release = sh(&format!("cat {rf}"), 30).1.trim().to_string();
        if let Some(want) = self.s["expect"]["release_text"].as_str() {
            if want != release {
                why.push(format!("the release file says {release:?}, not {want:?}"));
            }
        }
        if let Some(want) = self.s["expect"]["installed_sha256"].as_str() {
            let got = self.installed_sha()?;
            if got != want {
                why.push(
                    "the installed packages changed since the hop ended (someone ran the package manager?)".into(),
                );
            }
        }
        if phase != "mid-hop" {
            // the full judgment: network, kernel, sshd, every check that passed at the start
            let net = sh(NET, 60).1;
            j.insert(
                "net_kept".into(),
                json!(Some(net.as_str()) == self.s["start_net"].as_str()),
            );
            if Some(net.as_str()) != self.s["start_net"].as_str() {
                j.insert("net".into(), json!({"start": self.s["start_net"], "now": net}));
                why.push("the network isn't the one the machine started with".into());
            }
            let kn = str_of(get(&adapter, "hop").unwrap_or(&Value::Null), "kernel_newest")?;
            let running = sh("uname -r", 30).1.trim().to_string();
            let newest = sh(kn, 60).1.trim().to_string();
            j.insert("kernel".into(), json!(running));
            j.insert("kernel_newest".into(), json!(newest));
            if !kernel_is(&running, &newest) {
                why.push(format!(
                    "the running kernel {running} isn't the newest installed ({newest})"
                ));
            }
            let ssh = sshd_answers();
            j.insert("sshd_answers".into(), json!(ssh));
            if !ssh {
                why.push("sshd doesn't answer a new connection".into());
            }
            let results = run_checks(&self.checks()?);
            let lost: Vec<String> = self.s["start_checks"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(|c| c.as_str())
                .filter(|c| results.get(*c).is_none_or(|r| r["ok"] != json!(true)))
                .map(String::from)
                .collect();
            j.insert("lost".into(), json!(lost));
            j.insert(
                "failed_logs".into(),
                json!(lost
                    .iter()
                    .map(|c| (
                        c.clone(),
                        results.get(c).map(|r| r["out"].clone()).unwrap_or(Value::Null)
                    ))
                    .collect::<Map<_, _>>()),
            );
            if !lost.is_empty() {
                why.push(format!("checks lost: {}", lost.join(", ")));
            }
        }
        j.insert("ok".into(), json!(why.is_empty()));
        if let Some(b) = self.s["reboots"].as_array_mut().and_then(|r| r.last_mut()) {
            b["up_at"] = json!(now());
        }
        let what = match phase.as_str() {
            "relocated" => "the trial boot after relocate".to_string(),
            "mid-hop" => format!(
                "the boot into {}'s kernel before its post steps",
                self.s["hop_to"].as_str().unwrap_or("?")
            ),
            _ => format!("the boot after the hop to {}", self.s["hop_to"].as_str().unwrap_or("?")),
        };
        log(&format!(
            "{what}: {}",
            if why.is_empty() {
                "green".to_string()
            } else {
                format!("RED ({})", why.join("; "))
            }
        ));
        let rec = Value::Object(j);
        match phase.as_str() {
            "relocated" => self.s["trial"] = rec,
            "mid-hop" => self.last_hop()["mid_boot"] = rec,
            _ => {
                let ok = why.is_empty();
                let h = self.last_hop();
                h["boot"] = rec;
                h["ok"] = json!(ok);
            }
        }
        save(STATE, &self.s)?;
        if !why.is_empty() {
            self.red(&format!("{what}: {}", why.join("; ")))?;
            return Ok(false);
        }
        Ok(true)
    }

    fn last_hop(&mut self) -> &mut Value {
        let hops = self.s["hops"].as_array_mut().unwrap();
        if hops.is_empty() {
            hops.push(json!({}));
        }
        hops.last_mut().unwrap()
    }

    fn installed_sha(&self) -> Result<String, String> {
        let adapter = self.adapter()?;
        let cmd = str_of(get(adapter, "hop").unwrap_or(&Value::Null), "installed_list")?;
        let mut v: Vec<String> = sh(cmd, 1800).1.split_whitespace().map(String::from).collect();
        v.sort();
        Ok(format!("sha256:{}", sha256_hex(v.join("\n").as_bytes())))
    }

    /// The verifying proxy, offline, from the plan's cache and pinned by the rehearsal's run; and the adapter with the
    /// repos pointed at it
    /// The verifying proxy over the plan's cache, on the same port every boot: the repo file the last hop wrote
    /// (and dnf's own cache) name it, and the finish and a split hop's post steps run after a reboot.
    fn proxy(&mut self) -> Result<(crate::fetch::Proxy, Doc), String> {
        let p = match &self.px {
            Some(p) => p.clone(),
            None => {
                let port = self.s["proxy_port"].as_u64().unwrap_or(0) as u16;
                let cache = self.plan.dir.join("cache");
                let p = crate::fetch::Proxy::start_on(&self.plan.run, &cache, vec![], vec![], true, port)?;
                if port == 0 {
                    self.s["proxy_port"] = json!(p.port);
                    save(STATE, &self.s)?;
                }
                self.px = Some(p.clone());
                p
            }
        };
        p.ensure_pinned()?;
        let a = crate::proxied_adapter(self.adapter()?, &p.base())?;
        Ok((p, a))
    }

    /// Commands in order, stopping at the first that fails, then `after` whatever happened. Whether all of the first
    /// list passed, and their records.
    fn commands(&self, cmds: &[String], after: &[String]) -> (bool, Vec<Value>) {
        let mut recs = vec![];
        let mut ok = true;
        for c in cmds {
            let t0 = Instant::now();
            let (rc, out) = sh(c, 4 * 3600);
            log(&format!(
                "rc {rc} in {} s: {}\n{}",
                t0.elapsed().as_secs(),
                tail(c, 300),
                tail(&out, 2000)
            ));
            recs.push(json!({"cmd": c, "rc": rc, "out": tail(&out, 4000), "seconds": t0.elapsed().as_secs()}));
            if rc != 0 {
                ok = false;
                break;
            }
        }
        for c in after {
            let (rc, out) = sh(c, 1800);
            log(&format!("after: rc {rc}: {}\n{}", tail(c, 200), tail(&out, 1000)));
            recs.push(json!({"cmd": c, "rc": rc, "out": tail(&out, 2000), "after": true}));
        }
        (ok, recs)
    }

    /// SPEC "The upgrade" 3: one hop on the new root, booted, offline; then its trial reboot
    fn hop(&mut self, i: usize) -> Result<u8, String> {
        let hop = self.plan.hops()[i].clone();
        let route = self.route()?.clone();
        let adapter = self.adapter()?.clone();
        let owned = self.fixes();
        let fixes: std::collections::BTreeMap<String, &Doc> = owned.iter().map(|(k, v)| (k.clone(), v)).collect();
        // the fixes that hold must be exactly the ones that fired in the rehearsal
        let mut matched = vec![];
        for (id, f) in pinned(&route, &fixes)? {
            if get(f, "hop").and_then(Value::as_str) == Some(hop.as_str()) {
                let p = pred_sh(get(f, "when").ok_or("a fix without a when")?, &adapter)?;
                if sh(&format!("{p} >/dev/null 2>&1"), 1800).0 == 0 {
                    matched.push((id, f));
                }
            }
        }
        let mut got: Vec<String> = matched.iter().map(|(i, _)| i.clone()).collect();
        got.sort();
        let mut want: Vec<String> = self.plan.doc["hops"][i]["fired"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|x| x.as_str().map(String::from))
            .collect();
        want.sort();
        let names: Vec<String> = matched
            .iter()
            .map(|(_, f)| {
                get(f, "meta")
                    .and_then(|m| get(m, "name"))
                    .and_then(Value::as_str)
                    .unwrap_or("?")
                    .to_string()
            })
            .collect();
        self.s["hop_to"] = json!(hop);
        self.s["hops"]
            .as_array_mut()
            .unwrap()
            .push(json!({"to": hop, "fired": got, "fired_names": names, "ok": false}));
        if got != want {
            self.last_hop()["stage"] = json!("the fixes that hold aren't the ones that fired in the rehearsal");
            return self.red(&format!(
                "hop to {hop}: the fixes that hold ({got:?}) aren't the rehearsal's ({want:?})"
            ));
        }
        self.set("hopping")?;
        let (proxy, compile_with) = self.proxy()?;
        log(&format!(
            "hop to {hop} with {names:?}; repos through the verifying proxy at {}, offline",
            proxy.base()
        ));
        let fs: Vec<&Doc> = matched.iter().map(|(_, f)| *f).collect();
        let plan = hop_plan(&compile_with, &hop, &fs)?;
        let before_post = get(&adapter, "releases")
            .and_then(|r| get(r, &hop))
            .and_then(|r| get(r, "reboot"))
            .and_then(Value::as_str)
            == Some("before-post");
        let t0 = Instant::now();
        let (first, after) = if before_post {
            (&plan.commands[..plan.post_at], vec![])
        } else {
            (&plan.commands[..], plan.after.clone())
        };
        let (ok, recs) = self.commands(first, &after);
        let (ok, recs) = if !ok && before_post {
            // hop.after runs whatever happened
            let (_, more) = self.commands(&[], &plan.after);
            (false, recs.into_iter().chain(more).collect())
        } else {
            (ok, recs)
        };
        drop(proxy);
        {
            let h = self.last_hop();
            h["commands"] = json!(recs);
            h["seconds"] = json!(t0.elapsed().as_secs());
        }
        if !ok {
            let c = recs
                .iter()
                .find(|r| r["rc"] != json!(0) && r["after"].is_null())
                .and_then(|r| r["cmd"].as_str())
                .unwrap_or("")
                .to_string();
            self.last_hop()["stage"] = json!(format!("command failed: {c}"));
            return self.red(&format!("hop to {hop}: a command failed"));
        }
        if before_post {
            self.s["expect"] = json!({});
            self.s["phase"] = json!("mid-hop");
            return self.reboot(&format!("{hop}'s new kernel, before its post steps"), true);
        }
        self.end_of_hop()
    }

    /// The second part of a before-post hop, after the reboot into its kernel
    fn post_part(&mut self) -> Result<u8, String> {
        let i = self.s["hop"].as_u64().unwrap_or(0) as usize;
        let hop = self.plan.hops()[i].clone();
        let route = self.route()?.clone();
        let owned = self.fixes();
        let fixes: std::collections::BTreeMap<String, &Doc> = owned.iter().map(|(k, v)| (k.clone(), v)).collect();
        let want: Vec<String> = self.last_hop()["fired"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|x| x.as_str().map(String::from))
            .collect();
        let fs: Vec<&Doc> = pinned(&route, &fixes)?
            .into_iter()
            .filter(|(id, _)| want.contains(id))
            .map(|(_, f)| f)
            .collect();
        let (proxy, compile_with) = self.proxy()?;
        let plan = hop_plan(&compile_with, &hop, &fs)?;
        log(&format!(
            "hop to {hop}: the post steps, after the reboot into its kernel"
        ));
        self.set("hopping")?;
        let (ok, recs) = self.commands(&plan.commands[plan.post_at..], &plan.after);
        drop(proxy);
        if let Some(c) = self.last_hop()["commands"].as_array_mut() {
            c.extend(recs.clone());
        }
        if !ok {
            let c = recs
                .iter()
                .find(|r| r["rc"] != json!(0) && r["after"].is_null())
                .and_then(|r| r["cmd"].as_str())
                .unwrap_or("")
                .to_string();
            self.last_hop()["stage"] = json!(format!("command failed: {c}"));
            return self.red(&format!("hop to {hop}: a post step failed"));
        }
        self.end_of_hop()
    }

    /// What the next boot must find (SPEC "State across reboots"), then the trial reboot
    fn end_of_hop(&mut self) -> Result<u8, String> {
        let adapter = self.adapter()?.clone();
        let rf = str_of(&adapter, "release_file").unwrap_or("/etc/os-release");
        self.s["expect"] = json!({
            "release_text": sh(&format!("cat {rf}"), 30).1.trim(),
            "installed_sha256": self.installed_sha()?,
        });
        self.s["phase"] = json!("after-hop");
        let to = self.s["hop_to"].as_str().unwrap_or("?").to_string();
        self.reboot(&format!("the trial boot after the hop to {to}"), true)
    }

    /// SPEC "The upgrade" 4: arm the stub's one-shot boot of the new root (arm_new; relocate's flip armed the first),
    /// write the state, reboot. A `reboot` that does nothing for two minutes gets the forced reset.
    fn reboot(&mut self, what: &str, arm: bool) -> Result<u8, String> {
        let adapter = self.adapter()?.clone();
        // what the next boot is held to goes to disk before the one-shot is armed: a power cut between the two then
        // boots the old system (the default), which arms the trial again, never a new root whose state is behind it
        self.s["boot_id"] = json!(boot_id());
        self.s["reboots"]
            .as_array_mut()
            .unwrap()
            .push(json!({"for": what, "at": now()}));
        save(STATE, &self.s)?;
        if arm {
            let (rc, out) = sh(&rel_cmd(&adapter, &self.table(), "arm_new")?, 300);
            if rc != 0 {
                self.s["reboots"].as_array_mut().unwrap().pop();
                return self.red(&format!("arming the trial boot failed: {}", tail(&out, 500)));
            }
        }
        let _ = std::fs::write(RC, AGAIN.to_string());
        log(&format!(
            "rebooting: {what}. Run `wend upgrade` again once the machine is back"
        ));
        sh("sync", 120);
        // detached, so this process can end first and the owner hears 10: the reboot, and when it does nothing for
        // two minutes, the forced reset
        let mut c = Command::new("/bin/sh");
        c.arg("-c")
            .arg(format!("{ENV}sleep 5; sync; reboot; sleep 120; {FORCED_RESET}"))
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        // SAFETY: as above, setsid between fork and exec
        unsafe {
            c.pre_exec(|| {
                libc::setsid();
                Ok(())
            });
        }
        c.spawn().map_err(|e| format!("starting the reboot: {e}"))?;
        Ok(AGAIN)
    }

    /// SPEC "The upgrade", the first red: make the old system the stub's default and next boot, and reboot into it.
    /// On the old root already, judge it there.
    fn red(&mut self, why: &str) -> Result<u8, String> {
        log(&format!("RED: {why}. Rolling back to the old system"));
        self.s["why"] = json!(why);
        let adapter = self.adapter()?.clone();
        let on = booted(&adapter, &self.table());
        if on == "old" {
            self.s["phase"] = json!("rolling-back");
            save(STATE, &self.s)?;
            return self.judge_old();
        }
        // the red goes to disk before the rollback is asked for: a power cut between the two then brings back an old
        // system that judges itself, never one that reads a trial and arms the red root again
        self.s["phase"] = json!("rolling-back");
        save(STATE, &self.s)?;
        let (rc, out) = sh(&rel_cmd(&adapter, &self.table(), "rollback")?, 300);
        if rc != 0 {
            log(&format!("the rollback command failed: {}", tail(&out, 500)));
        }
        self.reboot("the rollback onto the old system", false)
    }

    /// Booted on the old system with an upgrade under way: a red's rollback landing, or a trial that didn't come up
    fn on_old_root(&mut self) -> Result<u8, String> {
        let adapter = self.adapter()?.clone();
        let theirs = new_root_state().unwrap_or(Value::Null);
        // the new root has the hops; the old root's own file has the start and its retries
        for k in ["hops", "hop", "hop_to", "trial", "reboots", "expect"] {
            if !theirs[k].is_null() {
                self.s[k] = theirs[k].clone();
            }
        }
        let phase = theirs["phase"]
            .as_str()
            .unwrap_or(self.s["phase"].as_str().unwrap_or(""))
            .to_string();
        if self.phase() == "rolled-back" || self.phase() == "rollback-failed" {
            println!("rolled back: {}", self.s["why"].as_str().unwrap_or(""));
            return Ok(ROLLED_BACK);
        }
        match phase.as_str() {
            "rolling-back" | "rolled-back" => {
                self.s["why"] = theirs["why"].clone();
                self.s["phase"] = json!("rolling-back");
                save(STATE, &self.s)?;
                self.judge_old()
            }
            "relocated" | "after-hop" | "mid-hop" | "landed" => {
                let key = format!("{phase}:{}", theirs["hop"]);
                let n = self.s["retries"][&key].as_u64().unwrap_or(0);
                if phase == "landed" {
                    log("the old system came up: the landed one boots on trial until `wend commit`; arming its trial");
                } else if n >= 1 {
                    return self.red("the trial boot came back on the old system twice");
                } else {
                    log("the old system came up instead of the new root's trial boot: arming the trial once more");
                    self.s["retries"][&key] = json!(n + 1);
                }
                // the retry is counted on disk before the trial is armed, so a power cut can't make it a first again
                save(STATE, &self.s)?;
                let (rc, out) = sh(&rel_cmd(&adapter, &self.table(), "arm")?, 300);
                if rc != 0 {
                    return self.red(&format!(
                        "arming the trial from the old system failed: {}",
                        tail(&out, 500)
                    ));
                }
                self.reboot("the trial boot of the new root, again", false)
            }
            "hopping" | "finishing" => {
                // the machine went down partway through a transaction on the new root (nothing was armed, so the
                // default brought the old system back): that's the first red
                let what = if phase == "hopping" { "a hop" } else { "the finish" };
                self.red(&format!("the machine went down partway through {what}"))
            }
            p => {
                log(&format!(
                    "on the old system, and the new root's state says {p:?}: nothing to do"
                ));
                Ok(code_of(p))
            }
        }
    }

    /// The old system after a rollback, against the checks it passed at the start (SPEC: the one case wend can't fix
    /// is a rollback whose old system fails them, said loudly)
    fn judge_old(&mut self) -> Result<u8, String> {
        let results = run_checks(&self.checks()?);
        let lost: Vec<String> = self.s["start_checks"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|c| c.as_str())
            .filter(|c| results.get(*c).is_none_or(|r| r["ok"] != json!(true)))
            .map(String::from)
            .collect();
        let net_kept = Some(sh(NET, 60).1.as_str()) == self.s["start_net"].as_str();
        self.s["old_system"] = json!({"lost": lost, "net_kept": net_kept, "kernel": sh("uname -r", 30).1.trim(),
                                      "booted": booted(self.adapter()?, &self.table())});
        let good = lost.is_empty() && net_kept;
        self.s["phase"] = json!(if good { "rolled-back" } else { "rollback-failed" });
        save(STATE, &self.s)?;
        let run = self.write_run(if good { "rolled-back" } else { "rollback-failed" })?;
        if good {
            log(&format!(
                "ROLLED BACK: the old system is back and passes every check it passed at the start. Why: {}. The run is \
                 {run}; the new root stays as it was, for a look",
                self.s["why"].as_str().unwrap_or("")
            ));
        } else {
            log(&format!(
                "ROLLBACK FAILED: the old system is back but fails {lost:?} (network kept: {net_kept}). This is the one \
                 case wend can't fix. The run is {run}"
            ));
        }
        Ok(ROLLED_BACK)
    }

    /// SPEC "The upgrade" 6: the rehearsal's prune, the checks once more, the landing release's own repos, the run
    fn finish(&mut self) -> Result<u8, String> {
        let adapter = self.adapter()?.clone();
        let to = self.s["to"].as_str().unwrap_or("").to_string();
        let (proxy, compile_with) = self.proxy()?;
        let h = get(&adapter, "hop").ok_or("no [hop]")?.clone();
        let groups: Vec<Vec<String>> = self.plan.doc["prune"]
            .as_array()
            .into_iter()
            .flatten()
            .map(|g| {
                g.as_array()
                    .into_iter()
                    .flatten()
                    .filter_map(|x| x.as_str().map(String::from))
                    .collect()
            })
            .collect();
        // as the rehearsal's walk ran each prune: [hop].after whatever happened
        let vars = hop_vars(&compile_with, &to)?;
        let after: Vec<String> = list_of(&h, "after")
            .unwrap_or_default()
            .iter()
            .map(|c| fill(c, &vars))
            .collect();
        self.set("finishing")?;
        let mut recs = vec![];
        for g in &groups {
            let rm = step(
                &json!({"op": "remove", "packages": g, "purge": true}),
                &compile_with,
                &to,
            )?;
            let (ok, r) = self.commands(&[rm, str_of(&h, "autoremove")?.to_string()], &after);
            recs.extend(r);
            if !ok {
                self.s["prune"] = json!(recs);
                return self.red("the prune the rehearsal made failed here");
            }
        }
        self.s["prune"] = json!(recs);
        let results = run_checks(&self.checks()?);
        let lost: Vec<String> = self.s["start_checks"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|c| c.as_str())
            .filter(|c| results.get(*c).is_none_or(|r| r["ok"] != json!(true)))
            .map(String::from)
            .collect();
        self.s["final"] = json!({"passed": passed(&results), "lost": lost});
        if !lost.is_empty() {
            return self.red(&format!("after the prune, checks lost: {}", lost.join(", ")));
        }
        if let Some(c) = get(&h, "land_repos").and_then(Value::as_str) {
            let c = fill(c, &vars);
            let (ok, r) = self.commands(&[c], &[]);
            self.s["land_repos"] = json!(r);
            if !ok {
                return self.red("pointing the package manager at the landing release's repos failed");
            }
        }
        drop(proxy);
        let _ = sh(&format!("[ -f /etc/motd ] && grep -vxF {} /etc/motd > /etc/motd.wend && cat /etc/motd.wend > /etc/motd; rm -f /etc/motd.wend", q(MOTD)), 30);
        self.s["phase"] = json!("landed");
        save(STATE, &self.s)?;
        let run = self.write_run("pass")?;
        log(&format!(
            "LANDED on {to}. Every boot is a trial until `wend commit`; `wend rollback` brings back the old system. The \
             run is {run}"
        ));
        Ok(LANDED)
    }

    /// The run (`wend.vmrun/0`'s shape, runner.kind "live") in /var/lib/wend/runs/ on the root that's running
    fn write_run(&self, result: &str) -> Result<String, String> {
        let down: u64 = self.s["reboots"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|b| Some(b["up_at"].as_u64()? - b["at"].as_u64()?))
            .sum();
        let run = json!({
            "schema": "wend.vmrun/0", "route": self.s["route_id"], "box": "local", "from": self.s["from"],
            "to": self.s["to"], "started": self.s["started"], "result": result, "why": self.s["why"],
            "relocate": {"table": self.s["table"], "fills": self.s["fills"], "stages": self.s["relocate"], "trial": self.s["trial"]},
            "hops": self.s["hops"], "prune": self.s["prune"], "final": self.s["final"], "old_system": self.s["old_system"],
            "start": {"checks": self.s["start_checks"], "firmware": self.s["start_firmware"], "kernel": self.s["start_kernel"]},
            "plan_sha256": self.s["plan_sha256"], "reboots": self.s["reboots"], "downtime_seconds": down,
            "wall_seconds": now().saturating_sub(self.s["started_at"].as_u64().unwrap_or(now())),
            "runner": {"kind": "live", "wend": format!("wend {} (rust)", env!("CARGO_PKG_VERSION"))},
        });
        let p = format!(
            "{RUNS}/{}-upgrade-{result}.json",
            self.s["started"].as_str().unwrap_or("run")
        );
        save(&p, &run)?;
        Ok(p)
    }
}

/// The new root's state, read from the old system: the new root mounted read-only (relocate.env says where), never
/// replaying a journal
fn new_root_state() -> Option<Value> {
    let env = relocate_env();
    let dev = match (
        env.get("VG").and_then(Value::as_str),
        env.get("NEW_DEV").and_then(Value::as_str),
    ) {
        (_, Some(d)) => d.to_string(),
        (Some(vg), None) => {
            sh(&format!("lvchange -ay {}", q(&format!("{vg}/wend-root"))), 60);
            format!("/dev/{vg}/wend-root")
        }
        _ => return None,
    };
    let m = "/run/wend/newstate";
    let (rc, _) = sh(
        &format!(
            "mkdir -p {m} && {{ mountpoint -q {m} || mount -o ro,norecovery {d} {m} 2>/dev/null || mount -o ro,noload {d} {m} 2>/dev/null || mount -o ro {d} {m}; }}",
            d = q(&dev)
        ),
        120,
    );
    if rc != 0 {
        return None;
    }
    let s = load(&format!("{m}{STATE}"));
    sh(&format!("umount {m}"), 60);
    s
}

/// `wend rollback`: the old system back, by hand, any time before the old root is dropped (commit included)
fn by_hand_rollback() -> Result<u8, String> {
    if unsafe { libc::geteuid() } != 0 {
        return Err("wend rollback runs as root".into());
    }
    let mut w = Worker::new()?;
    let adapter = w.adapter()?.clone();
    let on = booted(&adapter, &w.table());
    match on.as_str() {
        "new" => {
            w.s["why"] = json!("wend rollback, by hand");
            let (rc, out) = sh(&rel_cmd(&adapter, &w.table(), "rollback")?, 300);
            if rc != 0 {
                return Err(format!("the rollback command failed: {}", tail(&out, 500)));
            }
            w.s["phase"] = json!("rolling-back");
            w.reboot("the rollback onto the old system, by hand", false)
        }
        "old" => {
            w.s["why"] = json!("wend rollback, by hand, on the old system");
            w.judge_old()
        }
        x => Err(format!("this machine is booted from {x:?}: nothing to roll back to")),
    }
}

/// `wend commit`: the new root becomes the stub's default and the trials end. It deletes nothing: the stub, its old
/// entry and the old root stay, so `wend rollback` still works.
fn commit() -> Result<u8, String> {
    if unsafe { libc::geteuid() } != 0 {
        return Err("wend commit runs as root".into());
    }
    let mut w = Worker::new()?;
    let adapter = w.adapter()?.clone();
    if w.phase() != "landed" {
        return Err(format!(
            "the upgrade is in phase {}, not landed: nothing to commit",
            w.phase()
        ));
    }
    let on = booted(&adapter, &w.table());
    if on != "new" {
        return Err(format!(
            "booted from {on:?}: commit runs on the landed system (run `wend upgrade` to boot it)"
        ));
    }
    let (rc, out) = sh(&rel_cmd(&adapter, &w.table(), "commit_trial")?, 300);
    if rc != 0 {
        return Err(format!("commit_trial failed: {}", tail(&out, 500)));
    }
    w.s["phase"] = json!("committed");
    save(STATE, &w.s)?;
    println!("committed: the new system is the default boot; the old root and the stub stay, and `wend rollback` still works");
    Ok(LANDED)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn kernel_names_match_with_or_without_the_arch() {
        assert!(kernel_is("3.10.0-1160.el7.x86_64", "3.10.0-1160.el7.x86_64"));
        assert!(kernel_is("2.6.18-419.el5", "2.6.18-419.el5.x86_64"));
        assert!(!kernel_is("3.10.0-1160.el7.x86_64", "5.14.0-1.el9.x86_64"));
        assert!(!kernel_is("", "x"));
    }

    #[test]
    fn live_checks_drop_skipped_and_offline_generated_roles() {
        let a: Doc = crate::atlas::parse_toml("[checks]\npkgdb = \"rpm -qa\"\n[hop]\nset_release = \"x\"\n").unwrap();
        let doc: Doc = crate::atlas::parse_toml(
            "[roles]\nweb = \"curl a\"\ndb = \"mysqld --bootstrap\"\n[vm]\nskip = [\"db\"]\n[vm.live]\ndb = \"mysql -e 1\"\n\
             [generated.roles]\ngen-web = \"offline\"\n[generated.live]\ngen-web = \"live\"\n",
        )
        .unwrap();
        let c = live_checks(&doc, &a).unwrap();
        let names: Vec<&String> = c.keys().collect();
        assert_eq!(names, ["pkgdb", "web", "live:db", "gen-web"]);
        assert_eq!(c["gen-web"], "live");
    }

    #[test]
    fn fingerprint_differences_name_what_changed() {
        let want = json!({"kernel": "a", "release": "r", "installed": ["x-1", "y-1"], "holding": []});
        let got = json!({"kernel": "b", "release": "r", "installed": ["x-1", "y-2"], "holding": []});
        let d = differences(&want, &got);
        assert_eq!(d.len(), 2);
        assert!(d[0].starts_with("kernel") && d[1].contains("y-1") && d[1].contains("y-2"));
    }
}
