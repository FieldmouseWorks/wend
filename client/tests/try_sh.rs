//! Run the newcomer's one line, client/try.sh, as a machine's owner would: against a made-up release, from a folder
//! of their own, on a PATH holding exactly the programs that machine is said to have. Then run the release
//! workflow's publish step, which has to put the script beside the files it fetches.
//!
//! WEND_TRY_SH names another copy of the script (a broken one, to see a test fail), WEND_TRY_SHELL the shell to run
//! it with (`dash`, `busybox sh`; `sh` when unset), WEND_RELEASE_WORKFLOW another copy of the workflow.
use sha2::{Digest, Sha256};
use std::fs;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};
use std::sync::atomic::{AtomicUsize, Ordering};

const NAME: &str = "wend-x86_64-linux";
const TARBALL: &str = "wend-x86_64-linux.tar.gz";
/// What the script runs on every machine, beside sha256sum and a downloader.
const BASE: &[&str] = &["mkdir", "rm", "mktemp", "tar", "gzip", "chmod"];
/// A made-up release's files. Its wend leaves a file behind if anything starts it.
const RELEASE: &[(&str, &str)] = &[
    ("wend-x86_64-linux/wend", "#!/bin/sh\n: > \"${0%/*}/started\"\n"),
    ("wend-x86_64-linux/README.md", "Try wend\n"),
    ("wend-x86_64-linux/routes/el/a.toml", "family = \"el\"\n"),
];
/// A download address the script must never get as far as using.
const NOWHERE: &str = "file:///nowhere";
static NEXT: AtomicUsize = AtomicUsize::new(0);

/// A program of the machine running this test.
fn host(tool: &str) -> PathBuf {
    std::env::split_paths(&std::env::var_os("PATH").unwrap())
        .map(|dir| dir.join(tool))
        .find(|path| path.is_file())
        .unwrap_or_else(|| panic!("this test needs {tool} on PATH"))
}

fn script() -> Vec<u8> {
    let path = std::env::var_os("WEND_TRY_SH").map_or_else(
        || PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("try.sh"),
        PathBuf::from,
    );
    fs::read(path).unwrap()
}

/// The names in a folder, sorted.
fn names(dir: &Path) -> Vec<String> {
    let mut all: Vec<String> = fs::read_dir(dir)
        .unwrap()
        .map(|entry| entry.unwrap().file_name().into_string().unwrap())
        .collect();
    all.sort();
    all
}

/// The line SHA256SUMS has for a tarball of these bytes, as sha256sum writes it.
fn sums(tarball: &[u8]) -> String {
    let digest: String = Sha256::digest(tarball).iter().map(|b| format!("{b:02x}")).collect();
    format!("{digest}  {TARBALL}\n")
}

/// A made-up machine: the folder its owner is in (`here`), its temporary folder (`tmp`), a made-up release
/// (`release`), and a PATH (`bin`) of the programs it has.
struct Machine(PathBuf);
impl Machine {
    /// An x86_64 Linux machine with `tools` beside what every machine has, and its owner not root.
    fn new(tools: &[&str]) -> Self {
        let root = std::env::temp_dir().join(format!(
            "wend-try-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        let _ = fs::remove_dir_all(&root);
        for dir in ["bin", "here", "tmp", "release", "pack"] {
            fs::create_dir_all(root.join(dir)).unwrap();
        }
        let machine = Self(root);
        for tool in BASE.iter().chain(tools) {
            std::os::unix::fs::symlink(host(tool), machine.0.join("bin").join(tool)).unwrap();
        }
        machine.stand_in("uname", "case $1 in -m) echo x86_64 ;; *) echo Linux ;; esac");
        machine.stand_in("id", "echo 1000");
        machine.stand_in("sudo", ": > \"${0%/*}/sudo-ran\"");
        let tarball = machine.pack(RELEASE);
        fs::write(machine.0.join("release").join(TARBALL), &tarball).unwrap();
        fs::write(machine.0.join("release/SHA256SUMS"), sums(&tarball)).unwrap();
        machine
    }

    /// A program of this machine that only says what the test needs it to, in place of the real one if it had it.
    fn stand_in(&self, name: &str, body: &str) {
        let path = self.0.join("bin").join(name);
        // the link to this test's own machine's program goes, not the program
        let _ = fs::remove_file(&path);
        fs::write(&path, format!("#!/bin/sh\n{body}\n")).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
    }

    /// A gzipped tarball of `files`, as the release workflow packs one.
    fn pack(&self, files: &[(&str, &str)]) -> Vec<u8> {
        self.pack_with(files, 0o755)
    }

    /// A gzipped tarball of `files`, any wend in it with this mode.
    fn pack_with(&self, files: &[(&str, &str)], wend: u32) -> Vec<u8> {
        let dir = self
            .0
            .join("pack")
            .join(NEXT.fetch_add(1, Ordering::Relaxed).to_string());
        let mut top: Vec<&str> = Vec::new();
        for (name, content) in files {
            let path = dir.join(name);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(&path, content).unwrap();
            if name.ends_with("/wend") {
                fs::set_permissions(path, fs::Permissions::from_mode(wend)).unwrap();
            }
            let first = name.split('/').next().unwrap();
            if !top.contains(&first) {
                top.push(first);
            }
        }
        let made = Command::new(host("tar"))
            .arg("czf")
            .arg(dir.join("packed.tar.gz"))
            .arg("-C")
            .arg(&dir)
            .args(&top)
            .output()
            .unwrap();
        assert!(made.status.success(), "{}", String::from_utf8_lossy(&made.stderr));
        fs::read(dir.join("packed.tar.gz")).unwrap()
    }

    /// The made-up release, as a download address.
    fn release(&self) -> String {
        format!("file://{}", self.0.join("release").display())
    }

    /// Put the release's files where the owner is, as someone who copied them over from another machine has.
    fn copy_here(&self, files: &[&str]) {
        for file in files {
            fs::copy(self.0.join("release").join(file), self.0.join("here").join(file)).unwrap();
        }
    }

    fn here(&self) -> Vec<String> {
        names(&self.0.join("here"))
    }

    fn command(&self, url: &str) -> Command {
        let shell = std::env::var("WEND_TRY_SHELL").unwrap_or_else(|_| "sh".into());
        let mut words = shell.split_whitespace();
        let mut command = Command::new(host(words.next().unwrap()));
        command
            .args(words)
            .current_dir(self.0.join("here"))
            .env_clear()
            .env("PATH", self.0.join("bin"))
            .env("HOME", &self.0)
            .env("TMPDIR", self.0.join("tmp"))
            .env("WEND_TRY_URL", url);
        command
    }

    /// `curl .../try.sh | sh`: this copy of the script down a pipe, downloading from `url`.
    fn piped(&self, script: &[u8], url: &str) -> Output {
        let mut child = self
            .command(url)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        // a shell that gives up on a copy cut short may close the pipe before all of it is written
        let _ = child.stdin.take().unwrap().write_all(script);
        child.wait_with_output().unwrap()
    }

    /// The one line: the script down a pipe, downloading from `url`.
    fn fetch(&self, url: &str) -> Output {
        self.piped(&script(), url)
    }

    /// `sh try.sh TARBALL`, for a tarball already on this machine.
    fn unpack(&self, tarball: &str) -> Output {
        let copy = self.0.join("try.sh");
        fs::write(&copy, script()).unwrap();
        self.command(NOWHERE).arg(copy).arg(tarball).output().unwrap()
    }

    /// What every screen has to hold to (as client/src/guide.rs's do): plain ASCII and 80 columns. A line that
    /// carries an address or one of this test's own long paths can't be held to the width.
    fn fits_an_old_terminal(&self, screen: &str) {
        let root = self.0.display().to_string();
        for line in screen.lines() {
            assert!(line.is_ascii() && !line.contains('\t'), "not plain ASCII: {line:?}");
            assert!(
                line.len() <= 80 || line.contains("://") || line.contains(&root),
                "{} columns: {line:?}",
                line.len()
            );
        }
    }

    /// The run unpacked the release beside `kept` and said `next`, started nothing, and left nothing else behind.
    fn unpacked(&self, run: &Output, kept: &[&str], next: &str) -> String {
        let (said, errors) = (
            String::from_utf8_lossy(&run.stdout).into_owned(),
            String::from_utf8_lossy(&run.stderr),
        );
        assert!(run.status.success(), "{said}{errors}");
        assert_eq!(errors, "");
        let mut here: Vec<&str> = kept.to_vec();
        here.push(NAME);
        here.sort_unstable();
        assert_eq!(self.here(), here);
        let folder = self.0.join("here").join(NAME);
        // no `started`: nothing ran the wend it unpacked
        assert_eq!(names(&folder), ["README.md", "routes", "wend"]);
        assert_eq!(fs::read_to_string(folder.join("wend")).unwrap(), RELEASE[0].1);
        assert_eq!(
            fs::read_to_string(folder.join("routes/el/a.toml")).unwrap(),
            RELEASE[2].1
        );
        assert_ne!(
            fs::metadata(folder.join("wend")).unwrap().permissions().mode() & 0o111,
            0
        );
        assert!(!self.0.join("bin/sudo-ran").exists(), "it ran sudo");
        assert_eq!(names(&self.0.join("tmp")), [""; 0]);
        assert!(said.contains(&format!("\n    {next}\n")), "{said}");
        self.fits_an_old_terminal(&said);
        said
    }

    /// The run stopped, saying `why`, with only `kept` where the owner is and nothing in the temporary folder.
    fn refused(&self, run: &Output, why: &str, kept: &[&str]) -> String {
        let (said, errors) = (
            String::from_utf8_lossy(&run.stdout),
            String::from_utf8_lossy(&run.stderr).into_owned(),
        );
        assert_eq!(run.status.code(), Some(1), "{said}{errors}");
        assert!(errors.contains(why), "{errors}");
        assert_eq!(said, "");
        assert_eq!(self.here(), kept);
        assert_eq!(names(&self.0.join("tmp")), [""; 0]);
        assert!(!self.0.join("bin/sudo-ran").exists(), "it ran sudo");
        self.fits_an_old_terminal(&errors);
        errors
    }
}
impl Drop for Machine {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}

/// A stand-in for GitHub's releases: `/latest/download/FILE` redirects to `/assets/FILE`, as a release's latest
/// link does, and only that second address has the file. Returns the address to download from.
fn serve(dir: PathBuf) -> String {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(mut stream) = stream else { continue };
            let (mut request, mut some) = (Vec::new(), [0u8; 1024]);
            while !request.windows(4).any(|w| w == b"\r\n\r\n") {
                match stream.read(&mut some) {
                    Ok(n) if n > 0 => request.extend_from_slice(&some[..n]),
                    _ => break,
                }
            }
            let request = String::from_utf8_lossy(&request).into_owned();
            let path = request.split_whitespace().nth(1).unwrap_or("");
            let head = |status: &str, more: &str, length: usize| {
                format!("HTTP/1.1 {status}\r\n{more}Content-Length: {length}\r\nConnection: close\r\n\r\n").into_bytes()
            };
            let reply = if let Some(file) = path.strip_prefix("/latest/download/") {
                head("302 Found", &format!("Location: /assets/{file}\r\n"), 0)
            } else if let Some(Ok(body)) = path.strip_prefix("/assets/").map(|file| fs::read(dir.join(file))) {
                [head("200 OK", "", body.len()), body].concat()
            } else {
                head("404 Not Found", "", 0)
            };
            let _ = stream.write_all(&reply);
        }
    });
    format!("http://127.0.0.1:{port}/latest/download")
}

#[test]
fn the_one_line_checks_and_unpacks_a_release() {
    let machine = Machine::new(&["sha256sum", "curl"]);
    let said = machine.unpacked(
        &machine.fetch(&machine.release()),
        &[],
        "cd wend-x86_64-linux && sudo ./wend",
    );
    assert!(said.contains("Its checksum matches"), "{said}");
    assert!(said.contains("No sudo here?"), "{said}");
}

#[test]
fn root_is_not_sent_to_sudo() {
    let machine = Machine::new(&["sha256sum", "curl"]);
    machine.stand_in("id", "echo 0");
    let said = machine.unpacked(
        &machine.fetch(&machine.release()),
        &[],
        "cd wend-x86_64-linux && ./wend",
    );
    assert!(!said.contains("sudo"), "{said}");
}

#[test]
fn a_download_that_does_not_match_is_refused() {
    // cut short on the way, and something else altogether
    for cut_short in [true, false] {
        let machine = Machine::new(&["sha256sum", "curl"]);
        let tarball = machine.0.join("release").join(TARBALL);
        let whole = fs::read(&tarball).unwrap();
        let served = if cut_short {
            &whole[..whole.len() / 2]
        } else {
            &b"<html>Not found</html>"[..]
        };
        fs::write(&tarball, served).unwrap();
        let errors = machine.refused(&machine.fetch(&machine.release()), "does not match its checksum", &[]);
        // it shows both checksums, for whoever has to work out which file is wrong
        for digest in [sums(&whole), sums(served)] {
            assert!(errors.contains(digest.split_whitespace().next().unwrap()), "{errors}");
        }
    }
}

#[test]
fn a_checksum_file_that_does_not_list_the_tarball_is_refused() {
    for listing in [
        "<html><body>Sign in to continue</body></html>\n".to_string(),
        sums(b"x").replace(TARBALL, "wend-aarch64-linux.tar.gz"),
        String::new(),
    ] {
        let machine = Machine::new(&["sha256sum", "curl"]);
        fs::write(machine.0.join("release/SHA256SUMS"), listing).unwrap();
        machine.refused(&machine.fetch(&machine.release()), "has no checksum for", &[]);
    }
}

#[test]
fn a_checksum_file_may_end_without_a_newline_or_mark_the_file_binary() {
    for mark in ["  ", " *"] {
        let machine = Machine::new(&["sha256sum", "curl"]);
        let tarball = fs::read(machine.0.join("release").join(TARBALL)).unwrap();
        let listing = sums(&tarball).trim_end().replace("  ", mark);
        fs::write(machine.0.join("release/SHA256SUMS"), listing).unwrap();
        machine.unpacked(
            &machine.fetch(&machine.release()),
            &[],
            "cd wend-x86_64-linux && sudo ./wend",
        );
    }
}

#[test]
fn a_machine_that_cannot_run_wend_is_refused_by_name() {
    for (uname, named) in [
        (
            "case $1 in -m) echo i686 ;; *) echo Linux ;; esac",
            "This machine is i686 Linux",
        ),
        (
            "case $1 in -m) echo aarch64 ;; *) echo Linux ;; esac",
            "This machine is aarch64 Linux",
        ),
        (
            "case $1 in -m) echo x86_64 ;; *) echo Darwin ;; esac",
            "This machine is x86_64 Darwin",
        ),
    ] {
        let machine = Machine::new(&["sha256sum", "curl"]);
        machine.stand_in("uname", uname);
        let errors = machine.refused(&machine.fetch(&machine.release()), named, &[]);
        assert!(errors.contains("x86_64) only"), "{errors}");
        machine.copy_here(&[TARBALL, "SHA256SUMS"]);
        machine.refused(&machine.unpack(TARBALL), named, &["SHA256SUMS", TARBALL]);
    }
}

#[test]
fn a_tarball_copied_here_is_checked_and_unpacked() {
    // no curl and no wget: it has nothing to download
    let machine = Machine::new(&["sha256sum"]);
    machine.copy_here(&[TARBALL, "SHA256SUMS"]);
    let said = machine.unpacked(
        &machine.unpack(&format!("./{TARBALL}")),
        &[TARBALL, "SHA256SUMS"],
        "cd wend-x86_64-linux && sudo ./wend",
    );
    assert!(said.contains("Its checksum matches the one in ./SHA256SUMS."), "{said}");
}

#[test]
fn a_copied_tarball_that_does_not_match_is_refused_and_kept() {
    let machine = Machine::new(&["sha256sum"]);
    machine.copy_here(&[TARBALL, "SHA256SUMS"]);
    let (tarball, listing) = (machine.0.join("here").join(TARBALL), machine.0.join("here/SHA256SUMS"));
    let whole = fs::read(&tarball).unwrap();
    fs::write(&tarball, &whole[..whole.len() - 1]).unwrap();
    machine.refused(
        &machine.unpack(TARBALL),
        "does not match its checksum",
        &["SHA256SUMS", TARBALL],
    );
    // the owner's own files are theirs to deal with
    assert_eq!(fs::read(&tarball).unwrap(), &whole[..whole.len() - 1]);
    assert_eq!(fs::read_to_string(listing).unwrap(), sums(&whole));
}

#[test]
fn a_copied_tarball_with_no_checksum_file_is_unpacked_and_said_to_be_unchecked() {
    let machine = Machine::new(&["sha256sum"]);
    machine.copy_here(&[TARBALL]);
    let said = machine.unpacked(
        &machine.unpack(TARBALL),
        &[TARBALL],
        "cd wend-x86_64-linux && sudo ./wend",
    );
    assert!(said.contains("It was NOT checked"), "{said}");
    assert!(!said.contains("matches"), "{said}");
}

#[test]
fn a_machine_with_no_sha256sum_is_told_what_to_do() {
    // CentOS 4's coreutils has md5sum and sha1sum only
    let machine = Machine::new(&["curl"]);
    let errors = machine.refused(&machine.fetch(&machine.release()), "has no sha256sum", &[]);
    assert!(errors.contains("sh try.sh wend-x86_64-linux.tar.gz"), "{errors}");
    machine.copy_here(&[TARBALL, "SHA256SUMS"]);
    let errors = machine.refused(&machine.unpack(TARBALL), "has no sha256sum", &["SHA256SUMS", TARBALL]);
    assert!(errors.contains("sha256sum -c SHA256SUMS"), "{errors}");
    // the way forward it names: checked elsewhere, and unpacked here with no SHA256SUMS beside it
    assert!(errors.contains("remove SHA256SUMS here and run this again"), "{errors}");
    fs::remove_file(machine.0.join("here/SHA256SUMS")).unwrap();
    let said = machine.unpacked(
        &machine.unpack(TARBALL),
        &[TARBALL],
        "cd wend-x86_64-linux && sudo ./wend",
    );
    assert!(said.contains("It was NOT checked"), "{said}");
}

#[test]
fn a_folder_already_there_is_left_alone() {
    let machine = Machine::new(&["sha256sum", "curl"]);
    let report = machine.0.join("here").join(NAME).join("wend-box-1a2b.md");
    fs::create_dir(report.parent().unwrap()).unwrap();
    fs::write(&report, "a report").unwrap();
    let errors = machine.refused(&machine.fetch(&machine.release()), "is already here", &[NAME]);
    assert!(errors.contains("cd wend-x86_64-linux && sudo ./wend"), "{errors}");
    machine.copy_here(&[TARBALL, "SHA256SUMS"]);
    machine.refused(
        &machine.unpack(TARBALL),
        "is already here",
        &["SHA256SUMS", NAME, TARBALL],
    );
    assert_eq!(names(report.parent().unwrap()), ["wend-box-1a2b.md"]);
    assert_eq!(fs::read_to_string(report).unwrap(), "a report");
}

#[test]
fn it_follows_the_latest_release_link_with_curl_or_wget() {
    for downloader in ["curl", "wget"] {
        let machine = Machine::new(&["sha256sum", downloader]);
        let url = serve(machine.0.join("release"));
        machine.unpacked(&machine.fetch(&url), &[], "cd wend-x86_64-linux && sudo ./wend");
    }
}

#[test]
fn a_machine_that_cannot_download_is_told_how_to_copy_it_over() {
    let copy_it = "sh try.sh wend-x86_64-linux.tar.gz";
    let machine = Machine::new(&["sha256sum"]);
    let errors = machine.refused(&machine.fetch(&machine.release()), "neither curl nor wget", &[]);
    assert!(errors.contains(copy_it), "{errors}");
    // a release it can't reach, over each downloader
    for downloader in ["curl", "wget"] {
        let machine = Machine::new(&["sha256sum", downloader]);
        let url = serve(machine.0.join("nothing-here"));
        let errors = machine.refused(&machine.fetch(&url), "could not download", &[]);
        assert!(errors.contains(copy_it), "{errors}");
        for file in ["try.sh", TARBALL, "SHA256SUMS"] {
            assert!(errors.contains(&format!("    {url}/{file}\n")), "{errors}");
        }
    }
}

#[test]
fn only_the_release_folder_comes_out_of_a_tarball() {
    let machine = Machine::new(&["sha256sum"]);
    let mut files = RELEASE.to_vec();
    files.push(("stray.txt", "not wend's"));
    files.push(("another/wend", "not wend's either"));
    fs::write(machine.0.join("here").join(TARBALL), machine.pack(&files)).unwrap();
    machine.unpacked(
        &machine.unpack(TARBALL),
        &[TARBALL],
        "cd wend-x86_64-linux && sudo ./wend",
    );
}

#[test]
fn a_tarball_that_is_not_a_wend_release_is_refused() {
    for files in [
        // somebody's backup
        &[("etc/hostname", "web01\n"), ("wend", "#!/bin/sh\n")][..],
        // the folder, with no wend in it
        &[("wend-x86_64-linux/README.md", "Try wend\n")][..],
    ] {
        let machine = Machine::new(&["sha256sum"]);
        fs::write(machine.0.join("here/backup.tar.gz"), machine.pack(files)).unwrap();
        machine.refused(
            &machine.unpack("backup.tar.gz"),
            "it is not a wend release",
            &["backup.tar.gz"],
        );
    }
    let machine = Machine::new(&["sha256sum"]);
    fs::write(machine.0.join("here/notes.tar.gz"), "not a tarball at all").unwrap();
    machine.refused(
        &machine.unpack("notes.tar.gz"),
        "it is not a wend release",
        &["notes.tar.gz"],
    );
    machine.refused(
        &machine.unpack("missing.tar.gz"),
        "there is no file missing.tar.gz",
        &["notes.tar.gz"],
    );
    machine.refused(&machine.unpack("--help"), "usage: sh try.sh [", &["notes.tar.gz"]);
}

#[test]
fn a_wend_that_cannot_be_run_from_here_is_not_left_behind() {
    // a wend without its executable bit
    let machine = Machine::new(&["sha256sum"]);
    fs::write(machine.0.join("here").join(TARBALL), machine.pack_with(RELEASE, 0o644)).unwrap();
    let errors = machine.refused(&machine.unpack(TARBALL), "can't be run from this folder", &[TARBALL]);
    assert!(!errors.contains("not a wend release"), "{errors}");
    // a folder nothing can be started from, as one mounted noexec is (/tmp, often): the kernel refuses to start
    // the script's own probe there, as it does here when chmod has left the probe without its executable bit
    let machine = Machine::new(&["sha256sum"]);
    machine.stand_in("chmod", ":");
    machine.copy_here(&[TARBALL, "SHA256SUMS"]);
    let errors = machine.refused(
        &machine.unpack(TARBALL),
        "can't be run from this folder",
        &["SHA256SUMS", TARBALL],
    );
    assert!(errors.contains("noexec"), "{errors}");
}

#[test]
fn a_copy_of_the_script_cut_short_does_nothing() {
    let machine = Machine::new(&["sha256sum", "curl"]);
    let whole = script();
    // everything runs from the last line, so a copy that stops anywhere before it must run nothing
    let last_line = whole[..whole.len() - 1].iter().rposition(|b| *b == b'\n').unwrap() + 1;
    assert_eq!(&whole[last_line..], b"main \"$@\"\n");
    let cuts = (0..last_line).filter(|at| at % 41 == 0 || whole[*at] == b'\n');
    for at in cuts.chain([last_line]) {
        let run = machine.piped(&whole[..at], &machine.release());
        assert_eq!(
            (machine.here(), names(&machine.0.join("tmp")), run.stdout.as_slice()),
            (vec![], vec![], &b""[..]),
            "cut at byte {at}: {}",
            String::from_utf8_lossy(&run.stderr)
        );
    }
    // and the whole of it, by the same road, unpacks
    machine.unpacked(
        &machine.piped(&whole, &machine.release()),
        &[],
        "cd wend-x86_64-linux && sudo ./wend",
    );
}

/// What the release workflow's publish step hands to gh, when the tag's release is already there and when it isn't.
fn published(release_exists: bool) -> Vec<String> {
    let workflow = std::env::var_os("WEND_RELEASE_WORKFLOW").map_or_else(
        || PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.github/workflows/release.yml"),
        PathBuf::from,
    );
    let source = fs::read_to_string(workflow).unwrap();
    let step = source.split_once("      - name: publish\n").unwrap().1;
    let step = step.split_once("\n      - name: ").unwrap().0;
    let shell = step.split_once("        run: |\n").unwrap().1;
    let script = shell
        .lines()
        .map(|line| line.strip_prefix("          ").unwrap_or(line))
        .collect::<Vec<_>>()
        .join("\n");
    let machine = Machine::new(&[]);
    machine.stand_in(
        "gh",
        "if [ \"$1 $2\" = \"release view\" ]; then exit \"$RELEASE_MISSING\"; fi\nprintf '%s\\n' \"$@\" > \"$GH_SAW\"",
    );
    let saw = machine.0.join("gh-saw");
    let mut paths = vec![machine.0.join("bin")];
    paths.extend(std::env::split_paths(&std::env::var_os("PATH").unwrap()));
    let run = Command::new("bash")
        .args(["-e", "-c", &script])
        .env("PATH", std::env::join_paths(paths).unwrap())
        .env("TAG", "v9.9.9")
        .env("GITHUB_EVENT_NAME", "push")
        .env("GITHUB_SHA", "0000000")
        .env("RELEASE_MISSING", if release_exists { "0" } else { "1" })
        .env("GH_SAW", &saw)
        .output()
        .unwrap();
    assert!(run.status.success(), "{}", String::from_utf8_lossy(&run.stderr));
    fs::read_to_string(saw).unwrap().lines().map(String::from).collect()
}

#[test]
fn the_release_publishes_the_script_beside_what_it_fetches() {
    for (release_exists, verb) in [(true, "upload"), (false, "create")] {
        let saw = published(release_exists);
        assert_eq!(saw[..3], ["release", verb, "v9.9.9"], "{saw:?}");
        // the names the script and the one line ask for, whatever folder the workflow publishes them from
        for asset in ["try.sh", TARBALL, "SHA256SUMS"] {
            assert!(
                saw.iter()
                    .any(|arg| Path::new(arg).file_name().is_some_and(|name| name == asset)),
                "{verb} doesn't publish {asset}: {saw:?}"
            );
        }
    }
}
