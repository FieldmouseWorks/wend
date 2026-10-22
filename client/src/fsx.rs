//! Filesystem calls with a fallback for old kernels. The client is static, so it runs on the machine's kernel as it
//! is: CentOS 4's 2.6.9 has no openat or unlinkat (2.6.16), which std's remove_dir_all is built on.

use std::io;
use std::path::Path;

/// std::fs::remove_dir_all, or, where the kernel has no openat (ENOSYS), the same by path: never following a
/// symlink, removing the link itself.
pub fn remove_dir_all(p: impl AsRef<Path>) -> io::Result<()> {
    let p = p.as_ref();
    match std::fs::remove_dir_all(p) {
        Err(e) if e.raw_os_error() == Some(libc::ENOSYS) => by_path(p),
        r => r,
    }
}

fn by_path(p: &Path) -> io::Result<()> {
    if std::fs::symlink_metadata(p)?.file_type().is_symlink() {
        return std::fs::remove_file(p);
    }
    for e in std::fs::read_dir(p)? {
        let e = e?.path();
        // lstat by path: a DirEntry's own file type may go through fstatat, which is just as new
        if std::fs::symlink_metadata(&e)?.is_dir() {
            by_path(&e)?;
        } else {
            std::fs::remove_file(&e)?;
        }
    }
    std::fs::remove_dir(p)
}

#[cfg(test)]
mod tests {
    #[test]
    fn by_path_removes_the_tree_and_never_follows_a_link() {
        let d = std::env::temp_dir().join(format!("wend-fsx-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(d.join("tree/a/b")).unwrap();
        std::fs::create_dir_all(d.join("outside")).unwrap();
        std::fs::write(d.join("outside/keep"), b"x").unwrap();
        std::fs::write(d.join("tree/a/b/f"), b"x").unwrap();
        std::os::unix::fs::symlink(d.join("outside"), d.join("tree/a/link")).unwrap();
        std::os::unix::fs::symlink(d.join("outside"), d.join("dirlink")).unwrap();
        super::by_path(&d.join("tree")).unwrap();
        assert!(!d.join("tree").exists());
        super::by_path(&d.join("dirlink")).unwrap();
        assert!(d.join("outside/keep").exists() && std::fs::symlink_metadata(d.join("dirlink")).is_err());
        std::fs::remove_dir_all(&d).unwrap();
    }
}
