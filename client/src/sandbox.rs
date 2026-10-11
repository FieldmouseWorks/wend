//! A root the client walks: a whole filesystem tree it enters with chroot, snapshots with rsync and rolls back by
//! copying the snapshot over it. Follows SPEC.md "Sandbox requirements": a private tmpfs /dev per entry (never a
//! bind of the host's), fresh proc and sys, and nothing is copied over or deleted while anything is mounted under it.
//!
//! The same mechanics as tools/sandbox.py, so a run here is comparable with a CI run. A live machine (`/`) can't
//! be snapshotted this way; that needs a snapshot backend (LVM, btrfs, ZFS) and is refused for now.

use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

const ENV: &[(&str, &str)] = &[
    ("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
    ("LANG", "C"),
    ("HOME", "/root"),
    ("DEBIAN_FRONTEND", "noninteractive"),
    ("TERM", "dumb"),
];

pub fn tail(s: &str, n: usize) -> String {
    if s.len() <= n {
        return s.to_string();
    }
    let mut i = s.len() - n;
    while !s.is_char_boundary(i) {
        i += 1;
    }
    format!("...{}", &s[i..])
}

/// Run a command on the host, both output streams into one string.
pub fn host(cmd: &str) -> (i32, String) {
    match Command::new("/bin/sh").arg("-c").arg(cmd).output() {
        Ok(o) => (
            o.status.code().unwrap_or(-1),
            String::from_utf8_lossy(&o.stdout).into_owned() + &String::from_utf8_lossy(&o.stderr),
        ),
        Err(e) => (-1, e.to_string()),
    }
}

fn mounts_under(path: &Path) -> Vec<String> {
    let p = path.to_string_lossy().into_owned();
    fs::read_to_string("/proc/mounts")
        .unwrap_or_default()
        .lines()
        .filter_map(|l| l.split_whitespace().nth(1).map(String::from))
        .filter(|m| *m == p || m.starts_with(&format!("{p}/")))
        .collect()
}

/// Unmount everything under path, deepest first, and refuse to go on if anything is still mounted.
pub fn unmount_all(path: &Path) -> Result<(), String> {
    for _ in 0..3 {
        let mut left = mounts_under(path);
        if left.is_empty() {
            return Ok(());
        }
        left.sort_by_key(|m| std::cmp::Reverse(m.len()));
        for m in left {
            host(&format!("umount '{m}' 2>/dev/null || umount -l '{m}'"));
        }
    }
    let left = mounts_under(path);
    if left.is_empty() {
        Ok(())
    } else {
        Err(format!(
            "still mounted under {}: {left:?}; refusing to touch it",
            path.display()
        ))
    }
}

pub struct Root {
    pub dir: PathBuf,
}

impl Root {
    pub fn new(dir: &Path) -> Result<Root, String> {
        let dir = fs::canonicalize(dir).map_err(|e| format!("{}: {e}", dir.display()))?;
        if dir == Path::new("/") {
            return Err(
                "walking the running system (/) needs a snapshot backend (LVM, btrfs or ZFS), which the \
                        client doesn't have yet. Walk a copy of the machine's root instead (--root DIR)."
                    .into(),
            );
        }
        if !dir.join("bin/sh").exists() {
            return Err(format!("{} has no bin/sh: not a root filesystem", dir.display()));
        }
        Ok(Root { dir })
    }

    fn mount(&self) {
        let r = self.dir.display();
        host(&format!(
            "mount -t proc proc {r}/proc; mount -t sysfs sys {r}/sys; mount -t tmpfs -o mode=755,size=1m dev {r}/dev; \
             cd {r}/dev && mknod -m 666 null c 1 3 && mknod -m 666 zero c 1 5 && mknod -m 666 full c 1 7 && \
             mknod -m 666 random c 1 8 && mknod -m 666 urandom c 1 9 && mknod -m 666 tty c 5 0 && \
             mkdir pts shm && ln -s /proc/self/fd fd && ln -s fd/0 stdin && ln -s fd/1 stdout && ln -s fd/2 stderr; \
             mount -t devpts -o newinstance,ptmxmode=0666 devpts {r}/dev/pts && ln -sf pts/ptmx {r}/dev/ptmx; \
             cp /etc/resolv.conf {r}/etc/resolv.conf 2>/dev/null"
        ));
    }

    fn umount(&self) -> Result<(), String> {
        // stray daemons a maintainer script started anyway
        if let Ok(rd) = fs::read_dir("/proc") {
            for e in rd.flatten() {
                let name = e.file_name().to_string_lossy().into_owned();
                if name.chars().all(|c| c.is_ascii_digit())
                    && fs::read_link(e.path().join("root")).is_ok_and(|r| r == self.dir)
                {
                    if let Ok(pid) = name.parse::<i32>() {
                        unsafe { libc::kill(pid, libc::SIGKILL) };
                    }
                }
            }
        }
        unmount_all(&self.dir)
    }

    /// Run a shell command inside the root. `isolated`: a network namespace of its own with only loopback, for
    /// checks that start daemons. Exit 124 on timeout, as in tools/sandbox.py.
    pub fn run(&self, cmd: &str, timeout: u64, isolated: bool) -> (i32, String) {
        self.mount();
        let r = self.run_mounted(cmd, timeout, isolated);
        if let Err(e) = self.umount() {
            return (125, format!("{}\n{e}", r.1));
        }
        r
    }

    fn run_mounted(&self, cmd: &str, timeout: u64, isolated: bool) -> (i32, String) {
        let root = self.dir.to_string_lossy().into_owned();
        let mut argv: Vec<String> = vec!["chroot".into(), root, "/bin/sh".into(), "-c".into(), cmd.into()];
        if isolated {
            let pre = ["unshare", "-n", "sh", "-c", "ip link set lo up && exec \"$@\"", "x"];
            argv = pre.iter().map(|s| s.to_string()).chain(argv).collect();
        }
        // output to a file, not a pipe: a daemon a command leaves running would hold a pipe open forever
        let out = match tempfile() {
            Ok(f) => f,
            Err(e) => return (-1, e),
        };
        let mut c = Command::new(&argv[0]);
        c.args(&argv[1..])
            .env_clear()
            .envs(ENV.iter().copied())
            .stdin(Stdio::null());
        c.stdout(out.0.try_clone().unwrap()).stderr(out.0.try_clone().unwrap());
        let mut child = match c.spawn() {
            Ok(ch) => ch,
            Err(e) => return (-1, format!("{}: {e}", argv[0])),
        };
        let t0 = Instant::now();
        let rc = loop {
            match child.try_wait() {
                Ok(Some(s)) => break s.code().unwrap_or(-1),
                Ok(None) if t0.elapsed() > Duration::from_secs(timeout) => {
                    let _ = child.kill();
                    let _ = child.wait();
                    let s = read(&out.1);
                    let _ = fs::remove_file(&out.1);
                    return (124, format!("TIMEOUT after {timeout}s\n{}", tail(&s, 3000)));
                }
                Ok(None) => std::thread::sleep(Duration::from_millis(50)),
                Err(e) => break -(e.raw_os_error().unwrap_or(1)),
            }
        };
        let s = read(&out.1);
        let _ = fs::remove_file(&out.1);
        (rc, s)
    }

    /// Copy this root to dst (rsync, exact: --modify-window=-1 compares mtimes to the nanosecond, because EL6's rpm
    /// --rebuilddb rewrites index files with the same size and second). Never while anything is mounted under either.
    pub fn copy_to(&self, dst: &Path) -> Result<Root, String> {
        unmount_all(&self.dir)?;
        unmount_all(dst)?;
        fs::create_dir_all(dst).map_err(|e| e.to_string())?;
        let (rc, log) = host(&format!(
            "rsync -aHx --modify-window=-1 --delete '{}/' '{}/'",
            self.dir.display(),
            dst.display()
        ));
        if rc != 0 {
            return Err(format!(
                "copying {} to {} failed (is rsync installed?):\n{}",
                self.dir.display(),
                dst.display(),
                tail(&log, 800)
            ));
        }
        Ok(Root {
            dir: fs::canonicalize(dst).map_err(|e| e.to_string())?,
        })
    }
}

fn read(p: &Path) -> String {
    let mut b = vec![];
    let _ = fs::File::open(p).and_then(|mut f| f.read_to_end(&mut b));
    String::from_utf8_lossy(&b).into_owned()
}

fn tempfile() -> Result<(fs::File, PathBuf), String> {
    use std::sync::atomic::{AtomicU64, Ordering};
    static N: AtomicU64 = AtomicU64::new(0);
    let p = std::env::temp_dir().join(format!(
        "wend-out-{}-{}",
        std::process::id(),
        N.fetch_add(1, Ordering::Relaxed)
    ));
    fs::File::create(&p)
        .map(|f| (f, p.clone()))
        .map_err(|e| format!("{}: {e}", p.display()))
}

/// Delete a directory tree the client made, only after everything under it is unmounted, and never outside `work`.
pub fn safe_rm(path: &Path, work: &Path) -> Result<(), String> {
    let (p, w) = (
        fs::canonicalize(path).map_err(|e| e.to_string())?,
        fs::canonicalize(work).map_err(|e| e.to_string())?,
    );
    if p == w || !p.starts_with(&w) || p == Path::new("/") {
        return Err(format!(
            "refusing to delete {}: not inside {}",
            p.display(),
            w.display()
        ));
    }
    unmount_all(&p)?;
    host(&format!("rm -rf --one-file-system '{}'", p.display()));
    Ok(())
}
