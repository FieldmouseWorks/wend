//! A root the client walks: a whole filesystem tree it enters with chroot, snapshots with rsync and rolls back by
//! copying the snapshot over it. Follows SPEC.md "Sandbox requirements": a private tmpfs /dev per entry (never a
//! bind of the host's), fresh proc and sys, and nothing is copied over or deleted while anything is mounted under it.
//!
//! The same mechanics as tools/sandbox.py, so a run here is comparable with a CI run. A live machine (`/`) can't
//! be snapshotted this way; that needs a snapshot backend (LVM, btrfs, ZFS) and is refused for now.

use std::fs;
use std::io::{Read, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
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

/// Single-quote a string for sh.
pub fn q(s: &str) -> String {
    format!("'{}'", s.replace('\'', "'\\''"))
}

/// The supplied root itself when it is a mount point (a mounted LVM snapshot or disk image): the client leaves it
/// mounted. Set once, by Root::new.
static KEEP: std::sync::OnceLock<String> = std::sync::OnceLock::new();

fn mounts_under(path: &Path) -> Vec<String> {
    let p = path.to_string_lossy().into_owned();
    fs::read_to_string("/proc/mounts")
        .unwrap_or_default()
        .lines()
        .filter_map(|l| l.split_whitespace().nth(1).map(unescape))
        .filter(|m| *m == p || m.starts_with(&format!("{p}/")))
        .filter(|m| KEEP.get() != Some(m))
        .collect()
}

/// /proc/mounts writes a space, tab, newline or backslash in a path as an octal escape (\\040).
fn unescape(m: &str) -> String {
    let b = m.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        let oct = |c: u8| (b'0'..=b'7').contains(&c);
        if b[i] == b'\\' && i + 3 < b.len() && oct(b[i + 1]) && oct(b[i + 2]) && oct(b[i + 3]) {
            out.push((b[i + 1] - b'0') * 64 + (b[i + 2] - b'0') * 8 + (b[i + 3] - b'0'));
            i += 4;
        } else {
            out.push(b[i]);
            i += 1;
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// Paths the client mounts under or deletes: plain ones only, so they read back from /proc/mounts unchanged.
pub fn plain_path(p: &Path) -> Result<(), String> {
    let Some(s) = p.to_str() else {
        return Err(format!("{}: the root and work paths must be UTF-8", p.display()));
    };
    if s.chars().any(|c| c.is_whitespace() || c == '\\' || c.is_control()) {
        return Err(format!(
            "{s:?}: the root and work paths can't contain whitespace or backslashes"
        ));
    }
    Ok(())
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
            let ok = Command::new("umount")
                .arg(&m)
                .stderr(Stdio::null())
                .status()
                .is_ok_and(|s| s.success());
            if !ok {
                let _ = Command::new("umount").arg("-l").arg(&m).status();
            }
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
        plain_path(&dir)?;
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
        let d = dir.to_string_lossy().into_owned();
        let subs: Vec<String> = mounts_under(&dir).into_iter().filter(|m| *m != d).collect();
        if !subs.is_empty() {
            return Err(format!(
                "{} has filesystems mounted inside it ({}). The client can't snapshot or roll those back: unmount \
                 them, or copy the machine into one plain directory.",
                dir.display(),
                subs.join(", ")
            ));
        }
        if mounts_under(&dir).contains(&d) {
            let _ = KEEP.set(d);
        }
        let r = Root { dir };
        for m in ["proc", "sys", "dev", "etc"] {
            r.inside(m)?;
        }
        Ok(r)
    }

    /// A directory inside the root to mount on: it must be a real directory there, not a symlink that would put
    /// the mount on the host.
    fn inside(&self, name: &str) -> Result<String, String> {
        let p = self.dir.join(name);
        if !p.exists() {
            fs::create_dir(&p).map_err(|e| format!("{}: {e}", p.display()))?;
        }
        let real = fs::canonicalize(&p).map_err(|e| format!("{}: {e}", p.display()))?;
        let md = fs::symlink_metadata(&p).map_err(|e| e.to_string())?;
        if real != p || !md.is_dir() {
            return Err(format!(
                "{} is not a plain directory inside the root (a symlink?); refusing to use it",
                p.display()
            ));
        }
        Ok(real.to_string_lossy().into_owned())
    }

    /// Put a file the harness owns into the root for the length of a walk (on a CI box the box builder puts it
    /// there for good), remembering what was there so `give_back` can restore it.
    pub fn lend(&self, rel: &str, bytes: &[u8], mode: u32) -> Result<Lent, String> {
        let p = self.file_inside(rel)?;
        let before = match fs::symlink_metadata(&p) {
            Ok(m) if m.is_file() => Some((
                fs::read(&p).map_err(|e| format!("{}: {e}", p.display()))?,
                m.mode() & 0o7777,
            )),
            Ok(_) => return Err(format!("{} is not a plain file; refusing to replace it", p.display())),
            Err(_) => None,
        };
        write_mode(&p, bytes, mode).map_err(|e| format!("{}: {e}", p.display()))?;
        Ok(Lent {
            path: p,
            before: before.as_ref().map(|b| b.0.clone()),
            host: bytes.to_vec(),
            mode: before.map(|b| b.1),
        })
    }

    /// Restore what `lend` replaced: the root's own file, or no file.
    pub fn give_back(&self, l: &Lent) -> Result<(), String> {
        let rel = l.path.strip_prefix(&self.dir).map_err(|e| e.to_string())?;
        let p = self.file_inside(&rel.to_string_lossy())?;
        if fs::symlink_metadata(&p).is_ok_and(|m| !m.is_file()) {
            return Err(format!("{} is no longer a plain file; left as it is", p.display()));
        }
        match (&l.before, l.mode) {
            (Some(b), Some(m)) => write_mode(&p, b, m),
            _ => fs::remove_file(&p).or_else(|e| match e.kind() {
                std::io::ErrorKind::NotFound => Ok(()),
                _ => Err(e),
            }),
        }
        .map_err(|e| format!("{}: {e}", p.display()))
    }

    /// A file's path inside the root, its directory checked to be a plain one there.
    fn file_inside(&self, rel: &str) -> Result<PathBuf, String> {
        let (d, name) = rel
            .trim_start_matches('/')
            .rsplit_once('/')
            .ok_or("a path with a directory")?;
        Ok(Path::new(&self.inside(d)?).join(name))
    }

    fn mount(&self) -> Result<(), String> {
        let (proc_, sys, dev) = (self.inside("proc")?, self.inside("sys")?, self.inside("dev")?);
        let (proc_, sys, dev) = (q(&proc_), q(&sys), q(&dev));
        let (rc, log) = host(&format!(
            "mount -t proc proc {proc_} && mount -t sysfs sys {sys} && mount -t tmpfs -o mode=755,size=1m dev {dev} && \
             cd {dev} && mknod -m 666 null c 1 3 && mknod -m 666 zero c 1 5 && mknod -m 666 full c 1 7 && \
             mknod -m 666 random c 1 8 && mknod -m 666 urandom c 1 9 && mknod -m 666 tty c 5 0 && \
             mkdir pts shm && ln -s /proc/self/fd fd && ln -s fd/0 stdin && ln -s fd/1 stdout && ln -s fd/2 stderr && \
             mount -t devpts -o newinstance,ptmxmode=0666 devpts {dev}/pts && ln -sf pts/ptmx {dev}/ptmx 2>&1"
        ));
        if rc != 0 {
            return Err(format!(
                "setting up proc, sys and a private dev in the root failed (the client needs CAP_SYS_ADMIN):\n{}",
                tail(&log, 800)
            ));
        }
        Ok(())
    }

    /// Lend the root the host's resolver for one command (the distro mirrors need name service), remembering what
    /// was there. Never through a symlink: a real machine's resolv.conf often points into /run, which would be the
    /// host's.
    fn lend_resolver(&self) -> Option<Lent> {
        let host = fs::read("/etc/resolv.conf").ok()?;
        let path = Path::new(&self.inside("etc").ok()?).join("resolv.conf");
        let before = match fs::symlink_metadata(&path) {
            Ok(m) if m.is_file() => Some(fs::read(&path).ok()?),
            Ok(_) => return None,
            Err(_) => None,
        };
        write_nofollow(&path, &host).ok()?;
        Some(Lent {
            path,
            before,
            host,
            mode: None,
        })
    }

    /// Put the root's own resolv.conf back, unless the command itself replaced it.
    fn return_resolver(&self, lent: Option<Lent>) {
        let Some(l) = lent else { return };
        // a command may have swapped etc for a symlink out of the root
        if self.inside("etc").is_err() {
            return;
        }
        let ours = fs::symlink_metadata(&l.path).is_ok_and(|m| m.is_file())
            && fs::read(&l.path).is_ok_and(|now| now == l.host);
        if !ours {
            return;
        }
        let _ = match &l.before {
            Some(b) => write_nofollow(&l.path, b),
            None => fs::remove_file(&l.path),
        };
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
        let lent = self.lend_resolver();
        if let Err(e) = self.mount() {
            let _ = self.umount();
            self.return_resolver(lent);
            return (125, e);
        }
        let r = self.run_mounted(cmd, timeout, isolated);
        let u = self.umount();
        self.return_resolver(lent);
        if let Err(e) = u {
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
            "rsync -aHAXx --modify-window=-1 --delete {} {}",
            q(&format!("{}/", self.dir.display())),
            q(&format!("{}/", dst.display()))
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

pub struct Lent {
    path: PathBuf,
    before: Option<Vec<u8>>,
    host: Vec<u8>,
    mode: Option<u32>,
}

fn write_mode(p: &Path, b: &[u8], mode: u32) -> std::io::Result<()> {
    write_nofollow(p, b)?;
    fs::set_permissions(p, std::os::unix::fs::PermissionsExt::from_mode(mode))
}

fn write_nofollow(p: &Path, b: &[u8]) -> std::io::Result<()> {
    fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(p)?
        .write_all(b)
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
    fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&p)
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
    let _ = Command::new("rm").arg("-rf").arg("--one-file-system").arg(&p).status();
    Ok(())
}

/// An access or default ACL: either can let another user into what the client creates there.
fn has_acl(p: &Path) -> bool {
    use std::os::unix::ffi::OsStrExt;
    let Ok(c) = std::ffi::CString::new(p.as_os_str().as_bytes()) else {
        return true;
    };
    ["system.posix_acl_access", "system.posix_acl_default"].iter().any(|n| {
        let name = std::ffi::CString::new(*n).unwrap();
        if unsafe { libc::lgetxattr(c.as_ptr(), name.as_ptr(), std::ptr::null_mut(), 0) } >= 0 {
            return true;
        }
        // no such attribute, or a filesystem without ACLs; any other error means "can't tell", so refuse
        !matches!(
            std::io::Error::last_os_error().raw_os_error(),
            Some(libc::ENODATA) | Some(libc::EOPNOTSUPP)
        )
    })
}

/// The work directory holds snapshots of a root, written as root: it must be root's alone (an existing directory
/// someone else can write to could hold a planted symlink), and it must not be inside the root being walked.
pub fn work_dir(work: &Path, root: &Path) -> Result<PathBuf, String> {
    plain_path(work)?;
    if !work.exists() {
        fs::create_dir_all(work).map_err(|e| format!("{}: {e}", work.display()))?;
        fs::set_permissions(work, std::os::unix::fs::PermissionsExt::from_mode(0o700))
            .map_err(|e| format!("{}: {e}", work.display()))?;
    }
    let md = fs::symlink_metadata(work).map_err(|e| format!("{}: {e}", work.display()))?;
    if !md.is_dir() || md.uid() != 0 || md.mode() & 0o022 != 0 || has_acl(work) {
        return Err(format!(
            "{} must be a directory owned by root that nobody else can write to (and without ACLs)",
            work.display()
        ));
    }
    let w = fs::canonicalize(work).map_err(|e| e.to_string())?;
    plain_path(&w)?;
    if w.starts_with(root) || root.starts_with(&w) {
        return Err(format!(
            "the work directory {} and the root {} must not contain each other (snapshots would copy into \
             themselves)",
            w.display(),
            root.display()
        ));
    }
    Ok(w)
}

#[cfg(test)]
mod tests {
    #[test]
    fn mounts_unescape() {
        assert_eq!(super::unescape("/a\\040b/c\\134d"), "/a b/c\\d");
        assert_eq!(super::unescape("/plain\\04"), "/plain\\04");
    }
}
