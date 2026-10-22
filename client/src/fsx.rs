//! Filesystem calls with a fallback for old kernels. The client is static, so it runs on the machine's kernel as it
//! is: CentOS 4's 2.6.9 has no openat or unlinkat (2.6.16), which std's remove_dir_all is built on.

use std::fs::File;
use std::io;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::os::unix::io::AsRawFd;
use std::path::{Path, PathBuf};

/// std::fs::remove_dir_all, or, where the kernel has no openat (ENOSYS), the same anchored to open directories:
/// never following a symlink, removing the link itself.
pub fn remove_dir_all(p: impl AsRef<Path>) -> io::Result<()> {
    let p = p.as_ref();
    match std::fs::remove_dir_all(p) {
        Err(e) if e.raw_os_error() == Some(libc::ENOSYS) => by_fd(p),
        r => r,
    }
}

/// A directory opened without following a link: what the walk below reads and changes, by name.
fn open_dir(p: &Path) -> io::Result<File> {
    std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW)
        .open(p)
}

/// The tree at p, removed the way std does it with openat and unlinkat, but by /proc/self/fd/N/NAME: each name is
/// resolved in the directory this process holds open, never through a path a user could swap a directory in for
/// a symlink, and a directory is only entered after it was opened without following a link and found to be the one
/// lstat saw.
fn by_fd(p: &Path) -> io::Result<()> {
    if std::fs::symlink_metadata(p)?.file_type().is_symlink() {
        return std::fs::remove_file(p);
    }
    empty(open_dir(p)?)?;
    std::fs::remove_dir(p)
}

fn empty(dir: File) -> io::Result<()> {
    let here = PathBuf::from(format!("/proc/self/fd/{}", dir.as_raw_fd()));
    for e in std::fs::read_dir(&here)? {
        let at = here.join(e?.file_name());
        let m = std::fs::symlink_metadata(&at)?;
        if m.is_dir() {
            let sub = open_dir(&at)?;
            let n = sub.metadata()?;
            if (n.dev(), n.ino()) != (m.dev(), m.ino()) {
                return Err(io::Error::other(format!(
                    "{} changed while it was being removed",
                    at.display()
                )));
            }
            empty(sub)?;
            std::fs::remove_dir(&at)?;
        } else {
            std::fs::remove_file(&at)?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    #[test]
    fn by_fd_removes_the_tree_and_never_follows_a_link() {
        let d = std::env::temp_dir().join(format!("wend-fsx-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(d.join("tree/a/b")).unwrap();
        std::fs::create_dir_all(d.join("outside")).unwrap();
        std::fs::write(d.join("outside/keep"), b"x").unwrap();
        std::fs::write(d.join("tree/a/b/f"), b"x").unwrap();
        std::os::unix::fs::symlink(d.join("outside"), d.join("tree/a/link")).unwrap();
        std::os::unix::fs::symlink(d.join("outside"), d.join("dirlink")).unwrap();
        super::by_fd(&d.join("tree")).unwrap();
        assert!(!d.join("tree").exists());
        super::by_fd(&d.join("dirlink")).unwrap();
        assert!(d.join("outside/keep").exists() && std::fs::symlink_metadata(d.join("dirlink")).is_err());
        std::fs::remove_dir_all(&d).unwrap();
    }
}
