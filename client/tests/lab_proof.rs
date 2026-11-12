//! Exercise the command boundary with a queue stand-in, without KVM or GitHub.
use serde_json::{json, Value};
use std::fs;
use std::path::PathBuf;
use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};

struct Fixture {
    dir: PathBuf,
    head: String,
}
impl Fixture {
    fn new() -> Self {
        let dir = std::env::temp_dir().join(format!(
            "wend-proof-{}-{}",
            std::process::id(),
            SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos()
        ));
        let root = dir.join("head");
        for folder in ["routes/f", "boxes/f/a", "boxes/f/b", "adapters"] {
            fs::create_dir_all(root.join(folder)).unwrap();
        }
        fs::write(
            root.join("routes/f/from.toml"),
            "family='f'\nfrom='old'\nhops=['new']\n",
        )
        .unwrap();
        fs::write(root.join("boxes/f/a/box.toml"), "family='f'\nrelease='old'\n").unwrap();
        fs::write(root.join("boxes/f/b/box.toml"), "family='f'\nrelease='joined'\n").unwrap();
        fs::write(
            root.join("adapters/f.toml"),
            "[releases.old.install]\nuefi=true\n[releases.joined]\njoins='old'\n",
        )
        .unwrap();
        let git = |args: &[&str]| {
            let output = Command::new("git")
                .arg("-C")
                .arg(&root)
                .args(args)
                .env("GIT_AUTHOR_NAME", "Test")
                .env("GIT_AUTHOR_EMAIL", "test@example.test")
                .env("GIT_COMMITTER_NAME", "Test")
                .env("GIT_COMMITTER_EMAIL", "test@example.test")
                .output()
                .unwrap();
            assert!(output.status.success(), "{}", String::from_utf8_lossy(&output.stderr));
            String::from_utf8(output.stdout).unwrap().trim().to_owned()
        };
        git(&["init", "-q"]);
        git(&["add", "."]);
        git(&["commit", "-qm", "fixture"]);
        let head = git(&["rev-parse", "HEAD"]);
        fs::write(dir.join("queue.json"), "[]").unwrap();
        fs::write(dir.join("control.json"), "{}").unwrap();
        // The actual lab protocol: enqueue, queue --json and a verdict path.
        fs::write(
            dir.join("remi.py"),
            r#"
import json, pathlib, sys, time, hashlib
SELF = hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest()
root = pathlib.Path(__file__).parent
entries = json.loads((root/'queue.json').read_text())
control = json.loads((root/'control.json').read_text())
a = sys.argv[1:]
if a[0] == 'queue' and control.get('blind_after_enqueue') and (root/'enqueues').exists(): raise SystemExit(4)
if a[0] == 'queue' and control.get('flaky'):
    with open(root/'queue-calls', 'a') as w: w.write('x')
    if len((root/'queue-calls').read_text()) % 2: raise SystemExit(3)
if a[0] == 'queue':
    print(json.dumps(entries))
elif a[0] == 'wake':
    if control.get('wake_fails'): raise SystemExit('no bus')
    with open(root/'wakes', 'a') as w: w.write('x')
    if control.get('worker_runs'):
        for e in entries:
            if e['state'] == 'queued': e['state'] = 'done'
        (root/'queue.json').write_text(json.dumps(entries))
elif a[0] == 'enqueue':
    def opt(k): return a[a.index(k)+1]
    with open(root/'enqueues', 'a') as w: w.write('x')
    if control.get('slow'):
        time.sleep(1.5)
    entries = json.loads((root/'queue.json').read_text())
    i = str(len(entries)+1)
    sha, route, box = opt('--sha'), a[2], a[3]
    bad = control.get('bad')
    flags = opt('--vm-flags').split() if '--vm-flags' in a else []
    run = {'head':{'commit':sha,'dirty':False},'box':box,'result':'pass','firmware':'uefi' if flags else 'bios','hops':[{'to':'new','ok':True}]}
    if bad == 'head': run['head']['commit'] = '0'*40
    if bad == 'dirty': run['head']['dirty'] = True
    if bad == 'partial': run['result'] = 'partial-pass'
    if bad == 'empty': run['hops'] = []
    verdict = {'runner_sha':SELF,'worker_sha':'old' if control.get('old_worker') else SELF,'sha':sha,'job':'replay','args':[route,box],'vm_flags':flags, 'result':'pass',
               'reasons':[], 'commands':[{'rc':0}], 'runs':[{'run_json':run}]}
    if bad == 'fail': verdict['result'] = 'fail'; verdict['commands'][0]['rc'] = 1
    path = root/('verdict-'+i+'.json')
    path.write_text(json.dumps(verdict))
    jid = '20260101-000000-replay-%04x' % int(i)
    state = 'queued' if control.get('hold') else control.get('end', 'done')
    entries.append({'id':jid,'by':opt('--by'),'state':state,'verdict':str(path)})
    (root/'queue.json').write_text(json.dumps(entries))
    print('queued '+jid+': replay x')
    if control.get('enqueue_dies'): raise SystemExit(9)
elif a[0] == 'cancel':
    entry = next(e for e in entries if e['id'] == a[1])
    entry['state'] = 'cancelled'
    path = pathlib.Path(entry['verdict'])
    verdict = json.loads(path.read_text()); verdict['result'] = 'cancelled'
    path.write_text(json.dumps(verdict))
    (root/'queue.json').write_text(json.dumps(entries))
else: raise SystemExit(2)
"#,
        )
        .unwrap();
        Self { dir, head }
    }
    fn invoke(&self) -> std::process::Output {
        Command::new(env!("CARGO_BIN_EXE_wend-lab-proof"))
            .arg(self.dir.join("remi.py"))
            .arg(self.dir.join("head"))
            .env("PROOF_HEAD", &self.head)
            .env("PROOF_NUMBER", "5")
            .env("WEND_LAB", self.dir.join("lab"))
            .env("WEND_LAB_WORKER_RUNNER", self.dir.join("remi.py"))
            .env("PROOF_SUMMARY", self.dir.join("summary.md"))
            .output()
            .unwrap()
    }
    fn queue(&self) -> Vec<Value> {
        serde_json::from_str(&fs::read_to_string(self.dir.join("queue.json")).unwrap()).unwrap()
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.dir);
    }
}

#[test]
fn serial_suite_includes_joined_box_and_adopts_existing_evidence() {
    let f = Fixture::new();
    let first = f.invoke();
    assert!(first.status.success(), "{}", String::from_utf8_lossy(&first.stderr));
    assert_eq!(f.queue().len(), 3);
    let summary = fs::read_to_string(f.dir.join("summary.md")).unwrap();
    assert!(
        summary.contains(&f.head)
            && summary.contains("f/a")
            && summary.contains("f/b")
            && summary.contains("verdict-3.json")
    );
    assert!(f.invoke().status.success());
    assert_eq!(
        f.queue().len(),
        3,
        "a repeated proof adopts existing same-head queue entries"
    );
}

#[test]
fn nonpasses_stop_the_suite_and_write_failed_evidence() {
    for bad in ["head", "dirty", "partial", "empty", "fail"] {
        let f = Fixture::new();
        fs::write(f.dir.join("control.json"), json!({"bad":bad}).to_string()).unwrap();
        let result = f.invoke();
        assert_eq!(result.status.code(), Some(1), "{bad}");
        assert_eq!(f.queue().len(), 1, "the second replay must not start after {bad}");
        assert!(fs::read_to_string(f.dir.join("summary.md")).unwrap().contains("FAILED"));
    }
}

#[test]
fn dirty_worktree_never_enqueues_a_proof() {
    let f = Fixture::new();
    fs::write(f.dir.join("head/routes/f/from.toml"), "family='f'\nfrom='other'\n").unwrap();
    assert!(!f.invoke().status.success());
    assert!(f.queue().is_empty());
}

#[test]
fn stopping_cancels_the_queue_entry_and_a_retry_does_not_adopt_it() {
    use std::process::Stdio;
    use std::time::Duration;
    let f = Fixture::new();
    fs::write(f.dir.join("control.json"), json!({"hold":true}).to_string()).unwrap();
    let mut child = Command::new(env!("CARGO_BIN_EXE_wend-lab-proof"))
        .arg(f.dir.join("remi.py"))
        .arg(f.dir.join("head"))
        .env("PROOF_HEAD", &f.head)
        .env("PROOF_NUMBER", "5")
        .env("WEND_LAB", f.dir.join("lab"))
        .env("WEND_LAB_WORKER_RUNNER", f.dir.join("remi.py"))
        .env("PROOF_SUMMARY", f.dir.join("summary.md"))
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let mut queued = false;
    for _ in 0..100 {
        let entries = fs::read_to_string(f.dir.join("queue.json"))
            .ok()
            .and_then(|s| serde_json::from_str::<Vec<Value>>(&s).ok());
        if entries.is_some_and(|v| !v.is_empty()) {
            queued = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    if !queued {
        child.kill().unwrap();
        panic!("proof did not enqueue");
    }
    unsafe {
        libc::kill(child.id() as libc::pid_t, libc::SIGTERM);
    }
    let mut status = None;
    for _ in 0..100 {
        if let Some(s) = child.try_wait().unwrap() {
            status = Some(s);
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    if status.is_none() {
        child.kill().unwrap();
        panic!("proof abandoned a queued replay on stop");
    }
    assert_eq!(status.unwrap().code(), Some(126));
    assert_eq!(f.queue()[0]["state"], "cancelled");
    fs::write(f.dir.join("control.json"), "{}").unwrap();
    assert!(f.invoke().status.success());
    assert_eq!(
        f.queue().len(),
        4,
        "retry enqueues a replacement and the remaining two suite entries"
    );
}

fn spawn(f: &Fixture) -> std::process::Child {
    use std::process::Stdio;
    Command::new(env!("CARGO_BIN_EXE_wend-lab-proof"))
        .arg(f.dir.join("remi.py"))
        .arg(f.dir.join("head"))
        .env("PROOF_HEAD", &f.head)
        .env("PROOF_NUMBER", "5")
        .env("WEND_LAB", f.dir.join("lab"))
        .env("WEND_LAB_WORKER_RUNNER", f.dir.join("remi.py"))
        .env("PROOF_SUMMARY", f.dir.join("summary.md"))
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap()
}

fn wait_for(path: PathBuf) {
    for _ in 0..200 {
        if path.exists() {
            return;
        }
        std::thread::sleep(std::time::Duration::from_millis(20));
    }
    panic!("{} never appeared", path.display());
}

#[test]
fn concurrent_invocations_share_one_job_per_replay() {
    let f = Fixture::new();
    fs::write(f.dir.join("control.json"), json!({"slow":true}).to_string()).unwrap();
    let mut a = spawn(&f);
    let mut b = spawn(&f);
    assert!(a.wait().unwrap().success());
    assert!(b.wait().unwrap().success());
    assert_eq!(f.queue().len(), 3, "each of the three replays is enqueued once");
    assert_eq!(fs::read_to_string(f.dir.join("enqueues")).unwrap().len(), 3);
}

#[test]
fn a_replacement_waits_for_the_enqueue_child_of_a_dead_adapter() {
    let f = Fixture::new();
    fs::write(f.dir.join("control.json"), json!({"slow":true}).to_string()).unwrap();
    let mut a = spawn(&f);
    wait_for(f.dir.join("enqueues"));
    a.kill().unwrap();
    a.wait().unwrap();
    assert!(f.invoke().status.success());
    assert_eq!(f.queue().len(), 3, "the child's entry was adopted, not duplicated");
    assert_eq!(fs::read_to_string(f.dir.join("enqueues")).unwrap().len(), 3);
}

#[test]
fn adopting_an_unstarted_entry_wakes_the_queue_worker() {
    let f = Fixture::new();
    fs::write(f.dir.join("control.json"), json!({"hold":true}).to_string()).unwrap();
    let mut a = spawn(&f);
    wait_for(f.dir.join("queue.json").with_file_name("wakes"));
    a.kill().unwrap();
    a.wait().unwrap();
    assert_eq!(f.queue().len(), 1);
    assert_eq!(f.queue()[0]["state"], "queued");
    fs::remove_file(f.dir.join("wakes")).unwrap();
    // The first worker never ran it (interrupted enqueue, crashed worker):
    // the replacement adopts the entry and must get a worker started.
    fs::write(f.dir.join("control.json"), json!({"worker_runs":true}).to_string()).unwrap();
    let out = f.invoke();
    assert!(out.status.success(), "{}", String::from_utf8_lossy(&out.stderr));
    assert!(f.dir.join("wakes").exists(), "adoption woke the worker");
    assert_eq!(f.queue().iter().filter(|e| e["id"] == f.queue()[0]["id"]).count(), 1);
    assert_eq!(f.queue().len(), 3);
}

#[test]
fn a_job_that_ends_at_once_keeps_its_verdict_in_the_summary() {
    for end in ["refused", "error", "cancelled"] {
        let f = Fixture::new();
        fs::write(f.dir.join("control.json"), json!({"end":end,"bad":"fail"}).to_string()).unwrap();
        let out = f.invoke();
        assert_eq!(out.status.code(), Some(1), "{end}");
        let summary = fs::read_to_string(f.dir.join("summary.md")).unwrap();
        assert!(!summary.contains("absent"), "{end}: {summary}");
        assert!(
            summary.contains("verdict-1.json") && summary.contains("FAILED"),
            "{end}: {summary}"
        );
        assert_eq!(f.queue().len(), 1);
    }
}

#[test]
fn a_fresh_attempt_follows_its_own_job_not_an_older_terminal_one() {
    let f = Fixture::new();
    fs::write(
        f.dir.join("control.json"),
        json!({"end":"error","bad":"fail"}).to_string(),
    )
    .unwrap();
    assert!(!f.invoke().status.success());
    fs::write(f.dir.join("control.json"), "{}").unwrap();
    assert!(f.invoke().status.success());
    let q = f.queue();
    assert_eq!(q.len(), 4, "the errored entry is not adopted; a new one is made");
    assert_eq!(q[0]["state"], "error");
}

#[test]
fn shelter_boxes_only_start_where_the_runner_can_build_them() {
    let f = Fixture::new();
    let root = f.dir.join("head");
    fs::create_dir_all(root.join("boxes/f/s")).unwrap();
    fs::write(
        root.join("boxes/f/s/box.toml"),
        "family='f'\nrelease='joined'\n[shelter.app]\nx=1\n",
    )
    .unwrap();
    fs::write(
        root.join("routes/f/from.toml"),
        "family='f'\nfrom='old'\nto='new'\nhops=['mid','new']\n",
    )
    .unwrap();
    fs::write(
        root.join("adapters/f.toml"),
        "[runtimes.mid]\npython2=[\"2.7\"]\n[runtimes.new]\npython2=[]\n[releases.old.install]\nuefi=true\n[releases.joined]\njoins='mid'\n",
    )
    .unwrap();
    let plan = |dir: &PathBuf| {
        let o = Command::new(env!("CARGO_BIN_EXE_wend-lab-proof"))
            .arg("--plan")
            .arg(dir)
            .output()
            .unwrap();
        String::from_utf8(o.stdout).unwrap()
    };
    let listed = plan(&root);
    assert!(listed.contains("f/a") && listed.contains("f/b"), "{listed}");
    assert!(
        !listed.contains("f/s"),
        "a shelter box joined mid-route cannot run: {listed}"
    );
    // A shelter box on the route's own start is runnable.
    fs::write(
        root.join("boxes/f/s/box.toml"),
        "family='f'\nrelease='old'\n[shelter.app]\nx=1\n",
    )
    .unwrap();
    // ... unless the route's own start already keeps a Python 2 the shelter replaces.
    assert!(!plan(&root).contains("f/s"), "the route keeps Python 2 past its start");
    fs::write(
        root.join("adapters/f.toml"),
        "[runtimes.mid]\npython2=[]\n[runtimes.new]\npython2=[]\n[releases.old.install]\nuefi=true\n[releases.joined]\njoins='mid'\n",
    )
    .unwrap();
    assert!(plan(&root).contains("f/s"));
}

#[test]
fn a_worker_that_is_not_the_trusted_runner_is_refused() {
    let f = Fixture::new();
    fs::write(f.dir.join("other.py"), "print('older')").unwrap();
    let out = Command::new(env!("CARGO_BIN_EXE_wend-lab-proof"))
        .arg(f.dir.join("remi.py"))
        .arg(f.dir.join("head"))
        .env("PROOF_HEAD", &f.head)
        .env("PROOF_NUMBER", "5")
        .env("WEND_LAB", f.dir.join("lab"))
        .env("WEND_LAB_WORKER_RUNNER", f.dir.join("other.py"))
        .env("PROOF_SUMMARY", f.dir.join("summary.md"))
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&out.stderr).contains("not the trusted runner"));
    assert!(f.queue().is_empty());
    let missing = Command::new(env!("CARGO_BIN_EXE_wend-lab-proof"))
        .arg(f.dir.join("remi.py"))
        .arg(f.dir.join("head"))
        .env("PROOF_HEAD", &f.head)
        .env("PROOF_NUMBER", "5")
        .env("WEND_LAB", f.dir.join("lab"))
        .env("WEND_LAB_WORKER_RUNNER", f.dir.join("missing.py"))
        .env("PROOF_SUMMARY", f.dir.join("summary.md"))
        .output()
        .unwrap();
    assert_eq!(missing.status.code(), Some(1));
    assert!(f.queue().is_empty());
}

#[test]
fn a_helper_dying_after_it_persisted_the_job_still_drains_it() {
    let f = Fixture::new();
    fs::write(
        f.dir.join("control.json"),
        json!({"hold":true,"enqueue_dies":true}).to_string(),
    )
    .unwrap();
    let out = f.invoke();
    assert_eq!(out.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&out.stderr).contains("enqueue failed after creating"));
    assert_eq!(f.queue().len(), 1);
    assert_eq!(
        f.queue()[0]["state"],
        "cancelled",
        "the persisted job was cancelled, not abandoned"
    );
}

#[test]
fn a_failing_worker_wake_cancels_its_job_before_reporting() {
    let f = Fixture::new();
    fs::write(
        f.dir.join("control.json"),
        json!({"hold":true,"wake_fails":true}).to_string(),
    )
    .unwrap();
    let out = f.invoke();
    assert_eq!(out.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&out.stderr).contains("no bus"));
    assert_eq!(f.queue()[0]["state"], "cancelled");
}

#[test]
fn transient_queue_helper_failures_do_not_abandon_a_job() {
    let f = Fixture::new();
    fs::write(f.dir.join("control.json"), json!({"flaky":true}).to_string()).unwrap();
    let out = f.invoke();
    assert!(out.status.success(), "{}", String::from_utf8_lossy(&out.stderr));
    assert_eq!(f.queue().len(), 3);
}

#[test]
fn a_verdict_from_an_older_worker_implementation_is_refused() {
    let f = Fixture::new();
    fs::write(f.dir.join("control.json"), json!({"old_worker":true}).to_string()).unwrap();
    let out = f.invoke();
    assert_eq!(out.status.code(), Some(1));
    assert!(fs::read_to_string(f.dir.join("summary.md"))
        .unwrap()
        .contains("not produced by the trusted queue implementation"));
}

#[test]
fn persistent_queue_read_failures_after_enqueue_never_orphan_the_job() {
    // The queue becomes unreadable right after the enqueue persists a held
    // job; the adapter keeps ownership, and when SIGTERM arrives and the
    // queue recovers it cancels the job and exits only once it is over.
    let f = Fixture::new();
    fs::write(
        f.dir.join("control.json"),
        json!({"hold":true,"blind_after_enqueue":true}).to_string(),
    )
    .unwrap();
    let mut child = spawn(&f);
    wait_for(f.dir.join("enqueues"));
    std::thread::sleep(std::time::Duration::from_millis(1500));
    assert!(
        child.try_wait().unwrap().is_none(),
        "the adapter must not give up while the queue is unreadable"
    );
    unsafe {
        libc::kill(child.id() as libc::pid_t, libc::SIGTERM);
    }
    std::thread::sleep(std::time::Duration::from_millis(800));
    assert!(
        child.try_wait().unwrap().is_none(),
        "still owned: the queue cannot confirm it is over"
    );
    fs::write(f.dir.join("control.json"), json!({"hold":true}).to_string()).unwrap();
    let status = child.wait().unwrap();
    assert_eq!(status.code(), Some(126));
    assert_eq!(f.queue()[0]["state"], "cancelled");
}
