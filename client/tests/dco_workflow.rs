//! Run the DCO workflow's actual shell against commit fixtures without credentials.
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::PathBuf;
use std::process::Command;
use std::sync::atomic::{AtomicUsize, Ordering};

const WORKERS: &str = "349565860+fieldmouseworks-workers[bot]@users.noreply.github.com";
const DEPENDABOT: &str = "49699333+dependabot[bot]@users.noreply.github.com";
static NEXT: AtomicUsize = AtomicUsize::new(0);

struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "wend-dco-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&root).unwrap();
        Self(root)
    }

    fn executable(&self, name: &str, content: &str) {
        let path = self.0.join(name);
        fs::write(&path, content).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}

fn check(opener: &str, author: &str, committer: &str, verified: bool, log: &str, expected: bool) {
    let workflow = std::env::var_os("DCO_WORKFLOW").map_or_else(
        || PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.github/workflows/gate.yml"),
        PathBuf::from,
    );
    let source = fs::read_to_string(workflow).unwrap();
    let job = source.split_once("  dco:\n").unwrap().1;
    let job = job.split_once("\n  schema:").unwrap().0;
    let shell = job.rsplit_once("        run: |\n").unwrap().1;
    let script = shell
        .lines()
        .map(|line| line.strip_prefix("          ").unwrap_or(line))
        .collect::<Vec<_>>()
        .join("\n")
        .replace("${{ github.base_ref }}", "fixture")
        .replace("${{ github.event.pull_request.head.sha }}", "fixture");
    let fixture = Fixture::new();
    let commit = fixture.0.join("commit.json");
    fs::write(
        &commit,
        format!(
            r#"{{"commit":{{"author":{{"email":"{author}"}},"committer":{{"email":"{committer}"}},"verification":{{"verified":{verified}}}}}}}"#
        ),
    )
    .unwrap();
    fixture.executable("git", "#!/bin/sh\nprintf '%s\\n' \"$TEST_LOG\"\n");
    fixture.executable(
        "gh",
        "#!/bin/sh\nif [ \"$2\" = repos/fixture/commits/other ]; then echo false; exit; fi\njq -r \"$4\" \"$TEST_COMMIT\"\n",
    );
    let mut paths = vec![fixture.0.clone()];
    paths.extend(std::env::split_paths(&std::env::var_os("PATH").unwrap()));
    let result = Command::new("bash")
        .args(["-e", "-c", &script])
        .env("PATH", std::env::join_paths(paths).unwrap())
        .env("PR_AUTHOR", opener)
        .env("DEPENDABOT_PR", (opener == "dependabot[bot]").to_string())
        .env("REPO", "fixture")
        .env("TEST_LOG", log)
        .env("TEST_COMMIT", commit)
        .output()
        .unwrap();
    assert_eq!(
        result.status.success(),
        expected,
        "{opener}, {author}, {committer}, verified={verified}: {}{}",
        String::from_utf8_lossy(&result.stdout),
        String::from_utf8_lossy(&result.stderr)
    );
}

#[test]
fn verified_workers() {
    check("fieldmouseworks-workers[bot]", WORKERS, WORKERS, true, "bot", true);
}

#[test]
fn signed_workers_webflow() {
    for verified in [true, false] {
        check(
            "fieldmouseworks-workers[bot]",
            WORKERS,
            "noreply@github.com",
            verified,
            "bot Signed-off-by: Fixture",
            true,
        );
    }
}

#[test]
fn workers_rejections() {
    for (opener, author, committer, verified) in [
        ("someone", WORKERS, WORKERS, true),
        ("fieldmouseworks-workers[bot]", "someone", WORKERS, true),
        ("fieldmouseworks-workers[bot]", WORKERS, "someone", true),
        ("fieldmouseworks-workers[bot]", WORKERS, "noreply@github.com", true),
        ("fieldmouseworks-workers[bot]", WORKERS, "noreply@github.com", false),
        ("someone", WORKERS, "noreply@github.com", true),
        ("fieldmouseworks-workers[bot]", WORKERS, WORKERS, false),
    ] {
        check(opener, author, committer, verified, "bot", false);
    }
}

#[test]
fn mixed_workers_pr() {
    check(
        "fieldmouseworks-workers[bot]",
        WORKERS,
        WORKERS,
        true,
        "bot\nother",
        false,
    );
}

#[test]
fn dependabot_preserved() {
    for committer in [DEPENDABOT, "noreply@github.com"] {
        check("dependabot[bot]", DEPENDABOT, committer, true, "bot", true);
    }
    for (author, committer, verified) in [
        (DEPENDABOT, DEPENDABOT, false),
        (DEPENDABOT, "noreply@github.com", false),
        ("someone", DEPENDABOT, true),
        (DEPENDABOT, "someone", true),
    ] {
        check("dependabot[bot]", author, committer, verified, "bot", false);
    }
    check("someone", DEPENDABOT, DEPENDABOT, true, "bot", false);
}

#[test]
fn signed_human() {
    check(
        "someone",
        "someone",
        "someone",
        false,
        "human Signed-off-by: Fixture",
        true,
    );
}

#[test]
fn unsigned_human() {
    check("someone", "someone", "someone", false, "human", false);
}
