//! Walk a route on a root (SPEC.md, "How a hop runs"; the map walk from dep-lab). On each hop, every pinned fix
//! whose `when` holds on the box right now fires; the hop is kept only if every command succeeds and every check
//! that passed before still passes, otherwise the whole root is rolled back from the pre-hop snapshot and the walk
//! stops there. The run it writes has the same shape as the Python harness's (tools/walk.py).

use crate::atlas::{get, list_of, str_of, Doc};
use crate::backend::{fill, pred_sh, step};
use crate::canon::{canonical, content_id, sha256_hex};
use crate::hop::{hop_plan, pinned};
use crate::sandbox::{host, tail, Root};
use serde_json::{json, Map, Value};
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::time::Instant;

/// Health checks: the adapter's shell checks the check file names (`adapter_checks`, default pkgdb), plus its own
/// `[roles]` (a CI box file, or the owner's own checks). Roles win on a name clash.
pub struct Checks {
    pub checks: Map<String, Value>,
    pub isolate: Vec<String>,
    pub arch: Option<String>,
    pub release: Option<String>,
}

impl Checks {
    pub fn from_doc(doc: &Doc, adapter: &Doc) -> Result<Checks, String> {
        let names = match get(doc, "adapter_checks") {
            Some(_) => list_of(doc, "adapter_checks")?,
            None => vec!["pkgdb".into()],
        };
        let mut checks = Map::new();
        for (k, v) in get(adapter, "checks").and_then(Value::as_object).into_iter().flatten() {
            if names.contains(k) {
                checks.insert(k.clone(), v.clone());
            }
        }
        for (k, v) in get(doc, "roles").and_then(Value::as_object).into_iter().flatten() {
            checks.insert(k.clone(), v.clone());
        }
        let isolate = match get(doc, "isolate") {
            Some(_) => list_of(doc, "isolate")?,
            None => vec![],
        };
        let arch = get(doc, "arch").and_then(Value::as_str).map(String::from);
        let release = get(doc, "release").and_then(Value::as_str).map(String::from);
        Ok(Checks {
            checks,
            isolate,
            arch,
            release,
        })
    }
}

pub struct Health {
    pub release: String,
    pub logs: BTreeMap<String, String>,
    pub passed: Vec<String>,
}

pub struct Walk<'a> {
    pub route: &'a Doc,
    pub adapter: &'a Doc,
    pub fixes_by_id: BTreeMap<String, &'a Doc>,
    pub checks: Checks,
    pub root: Root,
    pub snap: PathBuf,
    pub box_id: String,
    vars: Vec<(String, String)>,
    health: Health,
    steps: Vec<Value>,
    hops: Vec<Value>,
    t0: Instant,
    stamp: String,
    /// A complete snapshot exists and the root may have changed since: only then is restoring from it right.
    pub mid_step: bool,
}

impl<'a> Walk<'a> {
    pub fn new(
        route: &'a Doc,
        adapter: &'a Doc,
        fixes_by_id: BTreeMap<String, &'a Doc>,
        checks: Checks,
        root: Root,
        snap: PathBuf,
        box_id: String,
    ) -> Walk<'a> {
        let mut w = Walk {
            route,
            adapter,
            fixes_by_id,
            checks,
            root,
            snap,
            box_id,
            vars: vec![],
            health: Health {
                release: String::new(),
                logs: BTreeMap::new(),
                passed: vec![],
            },
            steps: vec![],
            hops: vec![],
            t0: Instant::now(),
            stamp: host("date +%Y%m%d-%H%M%S").1.trim().to_string(),
            mid_step: false,
        };
        w.health = w.check_health();
        w
    }

    fn log(&self, msg: &str) {
        eprintln!("[{} {:5.0}s] {msg}", self.box_id, self.t0.elapsed().as_secs_f64());
    }

    pub fn check_health(&self) -> Health {
        let mut logs = BTreeMap::new();
        let mut passed = vec![];
        for (name, test) in &self.checks.checks {
            let test = test.as_str().unwrap_or("false");
            let (rc, log) = self.root.run(
                &format!("( set -x; {test} ) >/tmp/.h 2>&1; rc=$?; tail -c 1200 /tmp/.h; exit $rc"),
                180,
                self.checks.isolate.contains(name),
            );
            if rc == 0 {
                passed.push(name.clone());
            }
            logs.insert(name.clone(), if rc == 0 { String::new() } else { log });
        }
        passed.sort();
        let rf = str_of(self.adapter, "release_file").unwrap_or("/etc/os-release");
        let release = self.root.run(&format!("cat {rf}"), 1800, false).1.trim().to_string();
        Health { release, logs, passed }
    }

    fn holds(&self, fix: &Doc) -> Result<bool, String> {
        let p = pred_sh(get(fix, "when").ok_or("a fix without a when")?, self.adapter)?;
        Ok(self.root.run(&format!("{p} >/dev/null 2>&1"), 1800, false).0 == 0)
    }

    /// Run commands in order; keep the step only if all succeed and no passing check is lost, else roll back.
    fn execute(&mut self, commands: &[String], why: &str) -> Result<bool, String> {
        let t0 = Instant::now();
        self.root.copy_to(&self.snap)?;
        self.mid_step = true;
        let mut outs = vec![];
        let mut rc = 0;
        for c in commands {
            let (r, out) = self.root.run(c, 1800, false);
            rc = r;
            outs.push(json!({"cmd": c, "rc": r, "out": tail(&out, 4000)}));
            if r != 0 {
                break;
            }
        }
        for c in list_of(get(self.adapter, "hop").unwrap_or(&Value::Null), "after").unwrap_or_default() {
            self.root.run(&fill(&c, &self.vars), 1800, false);
        }
        let h = self.check_health();
        let lost: Vec<String> = self
            .health
            .passed
            .iter()
            .filter(|c| !h.passed.contains(c))
            .cloned()
            .collect();
        let ok = rc == 0 && lost.is_empty();
        let stage = if ok {
            "ok".to_string()
        } else if rc != 0 {
            format!(
                "command failed: {}",
                outs.last().and_then(|o| o["cmd"].as_str()).unwrap_or("")
            )
        } else {
            format!("checks lost: {}", lost.join(", "))
        };
        let (release, npassed) = (h.release.clone(), h.passed.len());
        let health_after = health_json(&h);
        if ok {
            self.health = h;
        } else {
            Root { dir: self.snap.clone() }.copy_to(&self.root.dir)?;
        }
        self.mid_step = false;
        self.steps.push(
            json!({"why": why, "ok": ok, "stage": stage, "outputs": outs, "health_after": health_after,
                               "seconds": round1(t0.elapsed().as_secs_f64())}),
        );
        self.log(&format!(
            "{} {why} ({stage}) -> {release} {npassed}/{}",
            if ok { "KEPT" } else { "ROLLED BACK" },
            self.checks.checks.len()
        ));
        Ok(ok)
    }

    fn installed(&self) -> Vec<String> {
        let mut v = self.installed_list();
        v.dedup();
        v
    }

    /// The installed list as tools/walk.py fingerprints it: sorted, duplicates kept (multilib rpm lists a name
    /// once per arch).
    fn installed_list(&self) -> Vec<String> {
        let cmd = str_of(get(self.adapter, "hop").unwrap_or(&Value::Null), "installed_list").unwrap_or("true");
        let mut v: Vec<String> = self
            .root
            .run(cmd, 1800, false)
            .1
            .split_whitespace()
            .map(String::from)
            .collect();
        v.sort();
        v
    }

    /// No-model cleanup: purge packages the target no longer ships, bisecting whenever a check objects.
    fn prune(&mut self) -> Result<Value, String> {
        let h = get(self.adapter, "hop").cloned().unwrap_or(Value::Null);
        let obsolete = |w: &Walk| -> Vec<String> {
            let (rc, out) = w.root.run(str_of(&h, "obsolete").unwrap_or("false"), 1800, false);
            if rc == 0 {
                out.split_whitespace().map(String::from).collect()
            } else {
                vec![]
            }
        };
        let before = obsolete(self);
        let mut todo = vec![before.clone()];
        while let Some(pkgs) = todo.pop() {
            let installed = self.installed();
            let pkgs: Vec<String> = pkgs.into_iter().filter(|p| installed.contains(p)).collect();
            if pkgs.is_empty() {
                continue;
            }
            let rm = step(
                &json!({"op": "remove", "packages": pkgs, "purge": true}),
                self.adapter,
                str_of(self.route, "to")?,
            )?;
            let cmds = [rm, str_of(&h, "autoremove")?.to_string()];
            if !self.execute(&cmds, &format!("prune {} obsolete packages", pkgs.len()))? && pkgs.len() > 1 {
                let mid = pkgs.len() / 2;
                todo.push(pkgs[mid..].to_vec()); // popped second: the first half goes first, as in tools/walk.py
                todo.push(pkgs[..mid].to_vec());
            }
        }
        Ok(json!({"obsolete_before": before.len(), "obsolete_after": obsolete(self).len()}))
    }

    pub fn walk(&mut self) -> Result<Value, String> {
        let family = str_of(self.route, "family")?.to_string();
        let start_release = self.health.release.clone();
        let fp = json!({"family": family, "release": self.checks.release.clone().map(Value::String).or_else(|| get(self.route, "from").cloned()),
                        "arch": self.checks.arch.clone().unwrap_or_else(|| "amd64".into()), "installed": self.installed_list()});
        let fingerprint = format!("sha256:{}", sha256_hex(canonical(&fp).as_bytes()));
        let mut services: Vec<String> = self
            .root
            .run("ls /etc/init.d 2>/dev/null", 1800, false)
            .1
            .split_whitespace()
            .map(String::from)
            .collect();
        services.sort();
        let pins = pinned(self.route, &self.fixes_by_id)?;
        let hops = list_of(self.route, "hops")?;
        self.log(&format!(
            "start: {start_release}, {}/{} checks pass",
            self.health.passed.len(),
            self.checks.checks.len()
        ));
        for hop in &hops {
            let mut matched = vec![];
            for (id, f) in &pins {
                if get(f, "hop").and_then(Value::as_str) == Some(hop.as_str()) && self.holds(f)? {
                    matched.push((id.clone(), *f));
                }
            }
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
            let fixes: Vec<&Doc> = matched.iter().map(|(_, f)| *f).collect();
            let plan = hop_plan(self.adapter, hop, &fixes)?;
            self.vars = crate::backend::hop_vars(self.adapter, hop)?;
            let ok = self.execute(&plan.commands, &format!("hop to {hop} with {names:?}"))?;
            let secs = self.steps.last().map(|s| s["seconds"].clone()).unwrap_or(json!(0));
            self.hops.push(
                json!({"to": hop, "fired": matched.iter().map(|(i, _)| i).collect::<Vec<_>>(),
                                  "fired_names": names, "ok": ok, "seconds": secs}),
            );
            if !ok {
                break;
            }
        }
        let complete = self.hops.len() == hops.len() && self.hops.iter().all(|h| h["ok"] == json!(true));
        let prune = get(self.route, "finish").and_then(|f| get(f, "prune_obsolete")) == Some(&json!(true));
        let extra = if prune && complete { Some(self.prune()?) } else { None };
        let fin = self.check_health();
        let failed: Vec<String> = self
            .checks
            .checks
            .keys()
            .filter(|c| !fin.passed.contains(c))
            .cloned()
            .collect();
        let mut failed = failed;
        failed.sort();
        let failed_logs: Map<String, Value> = failed
            .iter()
            .map(|c| (c.clone(), json!(tail(&fin.logs[c], 1500))))
            .collect();
        let failed_stage = if complete {
            String::new()
        } else {
            self.steps
                .iter()
                .rev()
                .find(|s| s["ok"] == json!(false))
                .and_then(|s| s["stage"].as_str())
                .unwrap_or("")
                .into()
        };
        let mut run = json!({
            "schema": "wend.run/0", "route": content_id(self.route), "box": self.box_id,
            "from": get(self.route, "from"), "to": get(self.route, "to"),
            "result": if complete && failed.is_empty() { "pass" } else { "fail" },
            "started": self.stamp, "wall_seconds": self.t0.elapsed().as_secs_f64().round() as u64,
            "start_fingerprint": fingerprint, "runner": runner(), "hops": self.hops, "start_services": services,
            "failed_stage": failed_stage,
            "final": {"release": fin.release, "passed": fin.passed, "failed": failed, "failed_logs": failed_logs},
            "model_calls": 0,
            "log_sha256": format!("sha256:{}", sha256_hex(canonical(&Value::Array(self.steps.clone())).as_bytes())),
        });
        if let (Some(Value::Object(e)), Value::Object(m)) = (extra, &mut run) {
            m.extend(e);
        }
        Ok(run)
    }

    pub fn steps(&self) -> &[Value] {
        &self.steps
    }
}

fn round1(x: f64) -> f64 {
    (x * 10.0).round() / 10.0
}

fn health_json(h: &Health) -> Value {
    let checks: Map<String, Value> = h
        .logs
        .iter()
        .map(|(k, l)| (k.clone(), json!({"ok": h.passed.contains(k), "log": l})))
        .collect();
    json!({"release": h.release, "checks": checks, "passed": h.passed})
}

fn runner() -> Value {
    let rd = |p: &str| std::fs::read_to_string(p).unwrap_or_default().trim().to_string();
    json!({"kind": "local", "host": rd("/proc/sys/kernel/hostname"), "kernel": rd("/proc/sys/kernel/osrelease"),
           "client": format!("wend {} (rust)", env!("CARGO_PKG_VERSION"))})
}
