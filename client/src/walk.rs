//! Walk a route on a root (SPEC.md, "How a hop runs"; the map walk from dep-lab). On each hop, every pinned fix
//! whose `when` holds on the box right now fires; the hop is kept only if every command succeeds and every check
//! that passed before still passes, otherwise the whole root is rolled back from the pre-hop snapshot and the walk
//! stops there. The run it writes has the same shape as the Python harness's (tools/walk.py).

use crate::atlas::{get, list_of, str_of, Doc};
use crate::backend::{apt_origin, backend_of, bootstrap_pins, fill, hop_vars, pred_sh, step, SOURCES};
use crate::canon::{canonical, content_id, sha256_hex};
use crate::hop::{hop_plan, pinned};
use crate::sandbox::{host, tail, Root};
use serde_json::{json, Map, Value};
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::time::Instant;

/// Health checks: the adapter's shell checks the check file names (`adapter_checks`, default pkgdb), plus its own
/// `[roles]` (a CI box file, or the owner's own checks), then a rehearsal's `[generated.roles]`. Roles win on a
/// name clash.
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
        let mut isolate = match get(doc, "isolate") {
            Some(_) => list_of(doc, "isolate")?,
            None => vec![],
        };
        // a rehearsal's generated checks (SPEC "Checks the machine is held to"), the offline forms
        if let Some(g) = get(doc, "generated") {
            for (k, v) in get(g, "roles").and_then(Value::as_object).into_iter().flatten() {
                checks.entry(k.clone()).or_insert_with(|| v.clone());
            }
            if get(g, "isolate").is_some() {
                isolate.extend(list_of(g, "isolate")?);
            }
        }
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
    /// the adapter as the atlas has it, for recording sources by their origin URLs (`adapter` may point the box's
    /// repos at the verifying proxy)
    pub sources_adapter: &'a Doc,
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
    /// The rung the root sits on partway along the route (SPEC "Route"): the walk takes only the hops after it.
    pub joins_at: Option<String>,
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
            sources_adapter: adapter,
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
            joins_at: None,
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

    /// SPEC "The rehearsal": the adapter's `hop.kernel_fetch`, after a hop, downloads what `kernel_after` will install
    /// on the machine, through the proxy, so the plan's cache has it. Changes nothing a check reads, so it's no step of
    /// its own: its output goes to the log. Whether it got the kernel (null when the adapter has no such step).
    fn fetch_kernel(&mut self) -> Result<Value, String> {
        let cmds = list_of(get(self.adapter, "hop").unwrap_or(&Value::Null), "kernel_fetch").unwrap_or_default();
        if cmds.is_empty() {
            return Ok(Value::Null);
        }
        let t0 = Instant::now();
        let (mut ok, mut outs, mut stage) = (true, vec![], "ok".to_string());
        for c in &cmds {
            let c = fill(c, &self.vars);
            let (rc, out) = self.root.run(&c, 1800, false);
            self.log(&format!("kernel download: rc {rc}\n{}", tail(&out, 1500)));
            outs.push(json!({"cmd": c, "rc": rc, "out": tail(&out, 4000)}));
            if rc != 0 {
                (ok, stage) = (false, format!("kernel download failed: {c}"));
                break;
            }
        }
        // a step like any other, so a failed download is the run's failed stage and its output is the gap report's
        self.steps.push(
            json!({"why": "kernel download", "ok": ok, "stage": stage, "outputs": outs,
                               "seconds": round1(t0.elapsed().as_secs_f64())}),
        );
        Ok(json!(ok))
    }

    /// The pinned fixes whose `when` holds on the root as it is now, by content id (SPEC "The plan": part of the
    /// machine's fingerprint).
    pub fn holding(&self) -> Result<Vec<String>, String> {
        let mut out = vec![];
        for (id, f) in pinned(self.route, &self.fixes_by_id)? {
            if self.holds(f)? {
                out.push(id);
            }
        }
        out.sort();
        Ok(out)
    }

    /// SPEC "The plan": every installed package with its version and architecture (the adapter's
    /// `installed_versions`), sorted.
    pub fn installed_versions(&self) -> Result<Vec<String>, String> {
        let cmd = str_of(get(self.adapter, "hop").unwrap_or(&Value::Null), "installed_versions")?;
        let (rc, out) = self.root.run(cmd, 1800, false);
        if rc != 0 {
            return Err(format!("listing the root's packages failed: {}", tail(&out, 400)));
        }
        let mut v: Vec<String> = out.split_whitespace().map(String::from).collect();
        v.sort();
        Ok(v)
    }

    /// The root's release file as the walk started from it.
    pub fn start_release(&self) -> &str {
        &self.health.release
    }

    fn installed(&self) -> Vec<String> {
        let mut v = self.installed_list();
        v.dedup();
        v
    }

    /// The installed list as tools/walk.py fingerprints it: sorted, duplicates kept (multilib rpm lists a name
    /// once per arch).
    pub fn installed_list(&self) -> Vec<String> {
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
        let from = match &self.joins_at {
            Some(r) => json!(r),
            None => get(self.route, "from").cloned().unwrap_or(Value::Null),
        };
        let fp = json!({"family": family, "release": self.checks.release.clone().map_or_else(|| from.clone(), Value::String),
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
        let mut hops = list_of(self.route, "hops")?;
        if let Some(r) = &self.joins_at {
            let i = hops
                .iter()
                .position(|h| h == r)
                .ok_or(format!("{r} isn't one of the route's hops"))?;
            hops.drain(..=i);
        }
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
            if ok {
                let s = self.sources(hop)?;
                self.hops.last_mut().unwrap()["sources"] = Value::Object(s);
                if crate::sandbox::REHEARSAL.load(std::sync::atomic::Ordering::Relaxed) {
                    let cached = self.fetch_kernel()?;
                    let h = self.hops.last_mut().unwrap();
                    // a hop whose kernel can't be fetched is the gap, as tools/report.py finds it
                    if cached == json!(false) {
                        h["ok"] = json!(false);
                    }
                    h["kernel_cached"] = cached;
                }
            }
            if self.hops.last().unwrap()["ok"] != json!(true) {
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
            "from": from, "to": get(self.route, "to"),
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

    /// SPEC.md "Pinned sources": the repo metadata this hop's refresh read, by origin URL, as tools/backends.py's
    /// `sources` records it.
    fn sources(&self, hop: &str) -> Result<Map<String, Value>, String> {
        let root = &self.root.dir;
        let mut out = BTreeMap::new();
        match backend_of(self.sources_adapter) {
            "apt" => {
                // keys name the origin mirror; apt named its files after the mirror it used (the proxy's, with --run)
                let mirror = |a: &Doc| -> Result<String, String> {
                    Ok(hop_vars(a, hop)?
                        .into_iter()
                        .find(|(k, _)| k == "mirror")
                        .map(|(_, v)| v)
                        .unwrap_or_default())
                };
                let url = format!("{}/dists/{hop}", apt_origin(self.sources_adapter, hop)?);
                let used = format!("{}/dists/{hop}", mirror(self.adapter)?);
                let bare = used
                    .split_once("://")
                    .map_or(used.as_str(), |(_, r)| r)
                    .replace('/', "_");
                // newer apt writes a port's colon as %3a
                let mut stems = vec![bare.clone()];
                if bare.contains(':') {
                    stems.push(bare.replace(':', "%3a"));
                }
                let lists = root.join("var/lib/apt/lists");
                for stem in &stems {
                    for n in ["InRelease", "Release"] {
                        if let Some(h) = file_sha256(root, &lists.join(format!("{stem}_{n}"))) {
                            out.insert(format!("{url}/{n}"), h);
                        }
                    }
                }
                if out.is_empty() && dirs_under(root, &lists).first() == Some(&lists) {
                    // apt 0.9 throws the Release away when its signing key fails: pin the indexes it kept, by the
                    // path the Release lists them under (their bytes are the uncompressed ones the Release hashes)
                    let mut names: Vec<String> = std::fs::read_dir(&lists)
                        .map_err(|e| format!("{}: {e}", lists.display()))?
                        .flatten()
                        .filter_map(|e| e.file_name().into_string().ok())
                        .collect();
                    names.sort();
                    for n in names {
                        let Some(rest) = stems.iter().find_map(|st| n.strip_prefix(&format!("{st}_"))) else {
                            continue;
                        };
                        if n.ends_with(".gpg") {
                            continue;
                        }
                        if let Some(h) = file_sha256(root, &lists.join(&n)) {
                            out.insert(format!("{url}/{}", rest.replace('_', "/")), h);
                        }
                    }
                }
            }
            "rpm" => {
                // the harness's repo ids are <release>-<name> (backend::repo_file), so a cache directory names its repo
                let mut ids = BTreeMap::new();
                for (rel, r) in get(self.sources_adapter, "releases")
                    .and_then(Value::as_object)
                    .into_iter()
                    .flatten()
                {
                    for (n, u) in get(r, "repos").and_then(Value::as_object).into_iter().flatten() {
                        if let Some(u) = u.as_str() {
                            ids.insert(format!("{rel}-{n}"), format!("https://{u}/repodata/repomd.xml"));
                        }
                    }
                }
                // rpm.transit's boot-<name> repos: the release it comes from, then its extra repos
                for r in get(self.sources_adapter, "releases")
                    .and_then(Value::as_object)
                    .into_iter()
                    .flatten()
                    .map(|(_, r)| r)
                {
                    let Some(t) = get(r, "transit") else { continue };
                    let from = get(t, "from")
                        .and_then(Value::as_str)
                        .and_then(|f| get(self.sources_adapter, "releases").and_then(|rs| get(rs, f)));
                    let repos = from
                        .and_then(|f| get(f, "repos"))
                        .and_then(Value::as_object)
                        .into_iter()
                        .flatten();
                    let extra = get(t, "extra_repos").and_then(Value::as_object).into_iter().flatten();
                    for (n, u) in repos.chain(extra) {
                        if let Some(u) = u.as_str() {
                            ids.insert(format!("boot-{n}"), format!("https://{u}/repodata/repomd.xml"));
                        }
                    }
                }
                ids.extend(crate::backend::addon_repo_ids(self.sources_adapter));
                let kept = root.join(SOURCES.trim_start_matches('/')).join(hop);
                // a fetched line counts only if it is one of the adapter's own pins
                let mut pins = BTreeMap::new();
                for rel in get(self.sources_adapter, "releases")
                    .and_then(Value::as_object)
                    .into_iter()
                    .flatten()
                    .map(|(k, _)| k)
                {
                    pins.extend(bootstrap_pins(self.sources_adapter, rel)?);
                }
                let fetched = kept.join("fetched");
                if file_sha256(root, &fetched).is_some() {
                    let bytes = std::fs::read(&fetched).map_err(|e| format!("{}: {e}", fetched.display()))?;
                    let text = String::from_utf8_lossy(&bytes);
                    for line in text.lines() {
                        let (u, h) = line.trim().split_once(' ').unwrap_or((line.trim(), ""));
                        if pins.get(u).is_some_and(|p| p == h) {
                            out.insert(u.to_string(), h.to_string());
                        }
                    }
                }
                if dirs_under(root, &kept).first() == Some(&kept) {
                    for e in std::fs::read_dir(&kept)
                        .map_err(|e| format!("{}: {e}", kept.display()))?
                        .flatten()
                    {
                        let n = e.file_name().to_string_lossy().into_owned();
                        // only the transit's own repos: boot-<name> and the release it passes through
                        let url = n
                            .strip_suffix(".repomd.xml")
                            .filter(|rid| {
                                rid.starts_with("boot-")
                                    || rid.split('-').next().is_some_and(|r| {
                                        get(self.sources_adapter, "releases")
                                            .and_then(|rs| get(rs, r))
                                            .is_some_and(|x| get(x, "transit").is_some())
                                    })
                            })
                            .and_then(|rid| ids.get(rid));
                        if let Some(h) = url.and_then(|_| file_sha256(root, &e.path())) {
                            out.insert(url.unwrap().clone(), h);
                        }
                    }
                }
                let hexsuffix = regex::Regex::new(r"-[0-9a-f]{16}$").unwrap();
                for (top, yum) in [("var/cache/yum", true), ("var/cache/dnf", false)] {
                    for d in dirs_under(root, &root.join(top)) {
                        let name = d
                            .file_name()
                            .map(|n| n.to_string_lossy().into_owned())
                            .unwrap_or_default();
                        let rid = if yum {
                            name.clone()
                        } else {
                            hexsuffix.replace(&name, "").into_owned()
                        };
                        let p = if yum {
                            d.join("repomd.xml")
                        } else {
                            d.join("repodata/repomd.xml")
                        };
                        // only this hop's repos: yum and dnf "clean all" skip disabled repos, so older caches stay
                        if let Some(url) = ids
                            .get(&rid)
                            .filter(|_| rid.starts_with(&format!("{hop}-")) && (yum || rid != name))
                        {
                            if let Some(h) = file_sha256(root, &p) {
                                out.insert(url.clone(), h);
                            }
                        }
                    }
                }
            }
            _ => {}
        }
        Ok(out.into_iter().map(|(k, v)| (k, Value::String(v))).collect())
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

/// sha256 of a regular file inside the root, never through a symlink (anywhere on its path).
fn file_sha256(root: &std::path::Path, p: &std::path::Path) -> Option<String> {
    let md = std::fs::symlink_metadata(p).ok()?;
    let parent = std::fs::canonicalize(p.parent()?).ok()?;
    if !md.is_file() || !parent.starts_with(root) || Some(parent.as_path()) != p.parent() {
        return None;
    }
    Some(format!("sha256:{}", sha256_hex(&std::fs::read(p).ok()?)))
}

/// Every directory under top (top included), without following symlinks; nothing if top itself isn't a plain
/// directory inside the root.
fn dirs_under(root: &std::path::Path, top: &std::path::Path) -> Vec<PathBuf> {
    let plain = |p: &std::path::Path| {
        std::fs::symlink_metadata(p).is_ok_and(|m| m.is_dir())
            && std::fs::canonicalize(p).is_ok_and(|c| c == p && c.starts_with(root))
    };
    if !plain(top) {
        return vec![];
    }
    let mut out = vec![];
    let mut todo = vec![top.to_path_buf()];
    while let Some(d) = todo.pop() {
        if let Ok(rd) = std::fs::read_dir(&d) {
            for e in rd.flatten() {
                if e.file_type().is_ok_and(|t| t.is_dir()) {
                    todo.push(e.path());
                }
            }
        }
        out.push(d);
    }
    out
}
