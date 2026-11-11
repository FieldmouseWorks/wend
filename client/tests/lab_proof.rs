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
import json, pathlib, sys
root = pathlib.Path(__file__).parent
entries = json.loads((root/'queue.json').read_text())
control = json.loads((root/'control.json').read_text())
a = sys.argv[1:]
if a[0] == 'queue':
    print(json.dumps(entries))
elif a[0] == 'enqueue':
    def opt(k): return a[a.index(k)+1]
    key = opt('--claim')
    existing = next((e for e in entries if e.get('claim') == key and e['state'] in ('queued','running','done')), None)
    if existing:
        print(json.dumps({'id':existing['id'],'adopted':True})); raise SystemExit(0)
    i = str(len(entries)+1)
    sha, route, box = opt('--sha'), a[2], a[3]
    bad = control.get('bad')
    flags = opt('--vm-flags').split() if '--vm-flags' in a else []
    run = {'head':{'commit':sha,'dirty':False},'box':box,'result':'pass','firmware':'uefi' if flags else 'bios','hops':[{'to':'new','ok':True}]}
    if bad == 'head': run['head']['commit'] = '0'*40
    if bad == 'dirty': run['head']['dirty'] = True
    if bad == 'partial': run['result'] = 'partial-pass'
    if bad == 'empty': run['hops'] = []
    verdict = {'sha':sha,'job':'replay','args':[route,box],'vm_flags':flags, 'result':'pass',
               'reasons':[], 'commands':[{'rc':0}], 'runs':[{'run_json':run}]}
    if bad == 'fail': verdict['result'] = 'fail'; verdict['commands'][0]['rc'] = 1
    path = root/('verdict-'+i+'.json')
    path.write_text(json.dumps(verdict))
    entries.append({'id':i,'by':opt('--by'),'claim':key,'state':'queued' if control.get('hold') else 'done','verdict':str(path)})
    (root/'queue.json').write_text(json.dumps(entries))
    if control.get('terminal'):
        entries[-1]['state'] = control['terminal']; verdict['result'] = control['terminal']
        verdict['reasons'] = ['immediate '+control['terminal']]
        path.write_text(json.dumps(verdict)); (root/'queue.json').write_text(json.dumps(entries))
    print(json.dumps({'id':i,'adopted':False}))
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
            .arg(self.dir.join("remi.py"))
            .env("PROOF_HEAD", &self.head)
            .env("PROOF_NUMBER", "5")
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
        .arg(f.dir.join("remi.py"))
        .env("PROOF_HEAD", &f.head)
        .env("PROOF_NUMBER", "5")
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

#[test]
fn immediate_terminal_verdicts_preserve_their_actual_reasons() {
    for terminal in ["refused", "error", "cancelled"] {
        let f = Fixture::new();
        fs::write(f.dir.join("control.json"), json!({"terminal":terminal}).to_string()).unwrap();
        assert_eq!(f.invoke().status.code(), Some(1));
        let note = fs::read_to_string(f.dir.join("summary.md")).unwrap();
        assert!(note.contains(&format!("immediate {terminal}")), "{note}");
        assert!(!note.contains("enqueued job is absent"));
    }
}

#[test]
fn joined_shelters_are_excluded_but_regular_and_mapped_joins_remain() {
    let f = Fixture::new();
    let root = f.dir.join("head");
    for folder in [
        "routes/el",
        "boxes/el/junk",
        "boxes/el/pyapp",
        "boxes/el/plain6",
        "boxes/el/sl7",
    ] {
        fs::create_dir_all(root.join(folder)).unwrap();
    }
    fs::write(
        root.join("adapters/el.toml"),
        "[releases.centos7.install]\nuefi=true\n[releases.sl7]\njoins='centos7'\n[runtimes.centos6]\npython2=['2.7']\n[runtimes.centos7]\npython2=['2.7']\n[runtimes.alma10]\npython2=[]\n",
    )
    .unwrap();
    for start in ["centos4", "centos5", "centos7"] {
        let hops = if start == "centos7" {
            "['alma10']"
        } else {
            "['centos6','centos7','alma10']"
        };
        fs::write(
            root.join(format!("routes/el/{start}.toml")),
            format!("family='el'\nfrom='{start}'\nto='alma10'\nhops={hops}\n"),
        )
        .unwrap();
    }
    for name in ["junk", "pyapp"] {
        fs::write(
            root.join(format!("boxes/el/{name}/box.toml")),
            "family='el'\nrelease='centos7'\n[shelter.app]\nruntime='python2'\n",
        )
        .unwrap();
    }
    for (name, release) in [("plain6", "centos6"), ("sl7", "sl7")] {
        fs::write(
            root.join(format!("boxes/el/{name}/box.toml")),
            format!("family='el'\nrelease='{release}'\n"),
        )
        .unwrap();
    }
    let output = Command::new(env!("CARGO_BIN_EXE_wend-lab-proof"))
        .arg("--plan")
        .arg(&root)
        .output()
        .unwrap();
    assert!(output.status.success(), "{}", String::from_utf8_lossy(&output.stderr));
    let text = String::from_utf8(output.stdout).unwrap();
    for start in ["centos4", "centos5"] {
        for shelter in ["junk", "pyapp"] {
            assert!(!text.contains(&format!("routes/el/{start}.toml el/{shelter}")));
        }
        for plain in ["plain6", "sl7"] {
            assert!(text.contains(&format!("routes/el/{start}.toml el/{plain}")));
        }
    }
    for shelter in ["junk", "pyapp"] {
        assert!(text.contains(&format!("routes/el/centos7.toml el/{shelter} --uefi")));
    }
}
