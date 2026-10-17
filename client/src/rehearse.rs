//! `wend rehearse` (SPEC "The rehearsal", "The plan"): copy a running machine's root over ssh, walk the route on the
//! copy exactly as `walk --run` does, and on a pass write the plan `wend upgrade` will go by: the route and the run
//! that pinned it, the machine's fingerprint, the checks, and every byte the walk fetched. The machine keeps running
//! and nothing on it is written.

use crate::atlas::{get, str_of, Atlas};
use crate::canon::{canonical, content_id, sha256_hex};
use crate::sandbox::{plain_path, q};
use crate::{cmd_walk, WalkOpts};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::process::Command;

pub struct RehearseOpts {
    /// `[user@]host` the root is copied from (`--from`), through `ssh` (`--ssh`, an ssh command line)
    pub from: Option<String>,
    pub ssh: String,
    /// the machine's running kernel when the copy was made another way (`--root` without `--from`)
    pub kernel: Option<String>,
    /// where the plan goes (default `wend-plan-<machine id>` in the current directory)
    pub plan: Option<String>,
}

/// The filesystems rsync copies one by one: disk filesystems (`-x` crosses none of them), `/` first, then each in
/// its place in the copy. Everything else mounted (proc, sysfs, tmpfs, devpts, nfs) comes over as the empty directory
/// it is underneath.
const DISK_FS: &[&str] = &["ext2", "ext3", "ext4", "xfs", "btrfs", "jfs", "reiserfs", "vfat"];

/// The mount points to copy, from the machine's /proc/mounts: `/` first, then the others shortest first, each once.
pub fn disk_mounts(proc_mounts: &str) -> Vec<String> {
    let mut v: Vec<String> = proc_mounts
        .lines()
        .filter_map(|l| {
            let f: Vec<&str> = l.split_whitespace().collect();
            (f.len() >= 3 && f[0].starts_with('/') && DISK_FS.contains(&f[2])).then(|| unescape_mount(f[1]))
        })
        .collect();
    v.sort_by_key(|m| (m != "/", m.matches('/').count(), m.clone()));
    v.dedup();
    v
}

/// /proc/mounts writes a space, tab, newline and backslash in a path as octal escapes.
fn unescape_mount(s: &str) -> String {
    s.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
}

/// Whether an `rsync --version` says it can copy ACLs and extended attributes (rsync 3; CentOS 5's 2.6.8 can't).
fn acl_xattr(version: &str) -> bool {
    version.contains("ACLs")
        && version.contains("xattrs")
        && !version.contains("no ACLs")
        && !version.contains("no xattrs")
}

/// The plan directory's name: the machine's own id (systemd's, else D-Bus's, both 32 hex digits), else the first 16
/// hex digits of its fingerprint.
pub fn machine_id(root: &Path, fingerprint: &str) -> String {
    for p in ["etc/machine-id", "var/lib/dbus/machine-id"] {
        let id = std::fs::read_to_string(root.join(p))
            .unwrap_or_default()
            .trim()
            .to_string();
        if id.len() == 32 && id.bytes().all(|c| c.is_ascii_hexdigit()) {
            return id.to_ascii_lowercase();
        }
    }
    fingerprint.trim_start_matches("sha256:").chars().take(16).collect()
}

/// SPEC "The rehearsal": what a boot clears on the machine (EL's rc.sysinit) goes from the copy before the walk: every
/// non-directory under var/run and var/lock where those are directories on disk (EL7 and later link them to the
/// /run tmpfs, which the copy doesn't carry), never through a symlink, and rpm's var/lib/rpm/__db.* environment.
/// A daemon's pid file left there says it is running, and its init script won't start it. How many went.
pub fn boot_clean(root: &Path) -> Result<usize, String> {
    let mut n = 0;
    for top in ["var/run", "var/lock"] {
        let mut todo = vec![root.join(top)];
        while let Some(d) = todo.pop() {
            if !std::fs::symlink_metadata(&d).is_ok_and(|m| m.is_dir()) {
                continue;
            }
            for e in std::fs::read_dir(&d)
                .map_err(|e| format!("{}: {e}", d.display()))?
                .flatten()
            {
                if e.file_type().is_ok_and(|t| t.is_dir()) {
                    todo.push(e.path());
                } else {
                    std::fs::remove_file(e.path()).map_err(|x| format!("{}: {x}", e.path().display()))?;
                    n += 1;
                }
            }
        }
    }
    let rpm = root.join("var/lib/rpm");
    if std::fs::symlink_metadata(&rpm).is_ok_and(|m| m.is_dir()) {
        for e in std::fs::read_dir(&rpm)
            .map_err(|e| format!("{}: {e}", rpm.display()))?
            .flatten()
        {
            if e.file_name().to_string_lossy().starts_with("__db.") && e.file_type().is_ok_and(|t| t.is_file()) {
                std::fs::remove_file(e.path()).map_err(|x| format!("{}: {x}", e.path().display()))?;
                n += 1;
            }
        }
    }
    Ok(n)
}

struct Machine<'a> {
    from: &'a str,
    ssh: &'a str,
}

impl Machine<'_> {
    /// A command on the machine, its stdout (stderr goes to ours).
    fn run(&self, cmd: &str) -> Result<String, String> {
        let o = Command::new("/bin/sh")
            .arg("-c")
            .arg(format!("{} {} {}", self.ssh, q(self.from), q(cmd)))
            .stderr(std::process::Stdio::inherit())
            .output()
            .map_err(|e| e.to_string())?;
        if !o.status.success() {
            return Err(format!(
                "on {}: {cmd} exited {}",
                self.from,
                o.status.code().unwrap_or(-1)
            ));
        }
        Ok(String::from_utf8_lossy(&o.stdout).into_owned())
    }

    /// SPEC "The rehearsal", the copy: rsync with the snapshot's flags, one filesystem per pass.
    fn copy(&self, into: &Path) -> Result<(), String> {
        self.run("command -v rsync >/dev/null")
            .map_err(|_| format!("{} has no rsync: install it there to rehearse from it", self.from))?;
        let here = crate::sandbox::host("rsync --version").1;
        let there = self.run("rsync --version")?;
        let ax = if acl_xattr(&here) && acl_xattr(&there) {
            " -AX"
        } else {
            ""
        };
        let mounts = disk_mounts(&self.run("cat /proc/mounts")?);
        if mounts.first().map(String::as_str) != Some("/") {
            return Err(format!("{}'s / isn't on a disk filesystem ({DISK_FS:?})", self.from));
        }
        for m in &mounts {
            let dst = into.join(m.trim_start_matches('/'));
            std::fs::create_dir_all(&dst).map_err(|e| format!("{}: {e}", dst.display()))?;
            let src = if m == "/" { "/".to_string() } else { format!("{m}/") };
            eprintln!(
                "copying {}:{m} ({})",
                self.from,
                if ax.is_empty() {
                    "no ACLs or xattrs"
                } else {
                    "with ACLs and xattrs"
                }
            );
            let cmd = format!(
                "rsync -aHx --numeric-ids{ax} -e {} {} {}/",
                q(self.ssh),
                q(&format!("{}:{src}", self.from)),
                q(&dst.to_string_lossy())
            );
            let st = Command::new("/bin/sh")
                .arg("-c")
                .arg(&cmd)
                .status()
                .map_err(|e| e.to_string())?;
            // 24: files vanished during the copy, which a running machine does
            if !matches!(st.code(), Some(0) | Some(24)) {
                return Err(format!(
                    "copying {}:{m} failed (rsync exited {})",
                    self.from,
                    st.code().unwrap_or(-1)
                ));
            }
        }
        Ok(())
    }
}

pub fn cmd_rehearse(atlas: &Atlas, route: &str, o: &WalkOpts, r: &RehearseOpts) -> Result<bool, String> {
    if o.live || o.run.is_none() {
        return Err(
            "rehearse needs --run RUN.json, the passing run the plan will be pinned by (never --live: \
                    `upgrade` installs only bytes a passing run pinned)"
                .into(),
        );
    }
    let checks_path = o
        .checks
        .as_deref()
        .ok_or("rehearse needs --checks FILE: the checks the machine is held to")?;
    let root = o
        .root
        .as_deref()
        .ok_or("rehearse needs --root DIR: where the copy is (or goes, with --from)")?;
    if unsafe { libc::geteuid() } != 0 {
        return Err("rehearsing needs root (the copy keeps the machine's owners; the walk chroots)".into());
    }
    let kernel = match (&r.from, &r.kernel) {
        (Some(from), k) => {
            let m = Machine { from, ssh: &r.ssh };
            let dir = Path::new(root);
            plain_path(dir)?;
            if dir.exists()
                && std::fs::read_dir(dir)
                    .map_err(|e| format!("{root}: {e}"))?
                    .next()
                    .is_some()
            {
                return Err(format!(
                    "{root} isn't empty: --from copies into an empty or new directory"
                ));
            }
            std::fs::create_dir_all(dir).map_err(|e| format!("{root}: {e}"))?;
            std::fs::set_permissions(dir, std::os::unix::fs::PermissionsExt::from_mode(0o700))
                .map_err(|e| format!("{root}: {e}"))?;
            let running = m.run("uname -r")?.trim().to_string();
            if k.as_ref().is_some_and(|k| *k != running) {
                return Err(format!(
                    "--kernel says {}, and {from} runs {running}",
                    k.as_ref().unwrap()
                ));
            }
            m.copy(dir)?;
            running
        }
        (None, Some(k)) => k.clone(),
        (None, None) => {
            return Err(
                "rehearsing a copy made another way (--root without --from) needs --kernel VERSION, the \
                        machine's `uname -r`: it is part of the fingerprint `upgrade` checks"
                    .into(),
            )
        }
    };
    let n = boot_clean(Path::new(root))?;
    eprintln!("cleaned the copy as a boot would: {n} runtime files from var/run, var/lock and rpm's __db.*");
    crate::sandbox::REHEARSAL.store(true, std::sync::atomic::Ordering::Relaxed);
    let checks_bytes = std::fs::read(checks_path).map_err(|e| format!("{checks_path}: {e}"))?;
    let w = cmd_walk(atlas, route, o)?;
    if w.run["result"] != "pass" {
        eprintln!("the rehearsal failed: no plan written. The run says where it stopped; `wend share` makes a report.");
        return Ok(false);
    }
    let (_, rdoc) = atlas.route(route)?;
    let fp = json!({
        "installed": w.installed, "installed_sha256": format!("sha256:{}", sha256_hex(w.installed.join("\n").as_bytes())),
        "kernel": kernel, "release": w.release, "holding": w.holding,
    });
    let fingerprint = format!("sha256:{}", sha256_hex(canonical(&fp).as_bytes()));
    let id = machine_id(Path::new(root), &fingerprint);
    let dir = PathBuf::from(r.plan.clone().unwrap_or_else(|| format!("wend-plan-{id}")));
    plain_path(&dir)?;
    std::fs::create_dir(&dir)
        .map_err(|e| format!("{}: {e} (a plan is written once, to a new directory)", dir.display()))?;
    let cache_dir = dir.join("cache");
    std::fs::create_dir(&cache_dir).map_err(|e| format!("{}: {e}", cache_dir.display()))?;
    // the proxy's cache layout (sha256/, sha1/, named/), so `upgrade` serves from it offline as `walk --offline` does
    let mut listed = vec![];
    if let Some(src) = &w.cache {
        for (key, file) in &w.served {
            let rel = file
                .strip_prefix(src)
                .map_err(|_| format!("{} isn't in the cache", file.display()))?;
            let bytes = std::fs::read(file).map_err(|e| format!("{}: {e}", file.display()))?;
            let dst = cache_dir.join(rel);
            std::fs::create_dir_all(dst.parent().unwrap()).map_err(|e| format!("{}: {e}", dst.display()))?;
            std::fs::write(&dst, &bytes).map_err(|e| format!("{}: {e}", dst.display()))?;
            listed.push(format!("sha256:{} {} {key}", sha256_hex(&bytes), rel.display()));
        }
    }
    listed.sort();
    let hops: Vec<Value> = w.run["hops"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|h| json!({"to": h["to"], "fired": h["fired"], "seconds": h["seconds"], "kernel_cached": false}))
        .collect();
    let plan = json!({
        "schema": "wend.plan/0", "route": content_id(rdoc), "pinned_by": w.run["pinned_by"],
        "wend": format!("wend {} (rust)", env!("CARGO_PKG_VERSION")), "machine": id, "rehearsed": w.run["started"],
        "from": w.run["from"], "to": get(rdoc, "to"), "hops": hops,
        "fingerprint": fp, "fingerprint_sha256": fingerprint,
        "checks_sha256": format!("sha256:{}", sha256_hex(&checks_bytes)), "cache_files": listed.len(),
    });
    let write = |name: &str, text: &[u8]| -> Result<(), String> {
        std::fs::write(dir.join(name), text).map_err(|e| format!("{}: {e}", dir.join(name).display()))
    };
    write(
        "plan.json",
        (serde_json::to_string_pretty(&plan).unwrap() + "\n").as_bytes(),
    )?;
    write(
        "run.json",
        (serde_json::to_string_pretty(&w.run).unwrap() + "\n").as_bytes(),
    )?;
    write("checks.toml", &checks_bytes)?;
    std::fs::write(cache_dir.join("files.txt"), listed.join("\n") + "\n")
        .map_err(|e| format!("{}: {e}", cache_dir.display()))?;
    eprintln!(
        "plan written: {} ({} to {}, {} hops, {} cached files)",
        dir.display(),
        str_of(&w.run, "from").unwrap_or("?"),
        str_of(rdoc, "to")?,
        hops.len(),
        listed.len()
    );
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn copies_root_first_then_each_disk_filesystem_once() {
        let m = "rootfs / rootfs rw 0 0\n/dev/mapper/vg_wendvm-lv_root / ext4 rw 0 0\nproc /proc proc rw 0 0\n\
                 /dev/vda1 /boot ext4 rw 0 0\ntmpfs /dev/shm tmpfs rw 0 0\n/dev/vdb1 /srv/my\\040data xfs rw 0 0\n\
                 /dev/vdc1 /var xfs rw 0 0\nnfs:/x /mnt/x nfs rw 0 0\n";
        assert_eq!(disk_mounts(m), ["/", "/boot", "/var", "/srv/my data"]);
    }

    #[test]
    fn acls_and_xattrs_only_where_rsync_says_so() {
        assert!(acl_xattr("    64-bit files, 64-bit inums, 64-bit timestamps, 64-bit long ints,\n    socketpairs, hardlinks, symlinks, IPv6, batchfiles, inplace,\n    append, ACLs, xattrs, iconv, symtimes, prealloc"));
        assert!(!acl_xattr("    64-bit files, socketpairs, hard links, symlinks, batchfiles,\n    inplace, IPv6, 64-bit system inums, 64-bit internal inums"));
        assert!(!acl_xattr("append, no ACLs, xattrs"));
    }

    #[test]
    fn boot_clean_clears_runtime_files_but_not_dirs_or_symlinked_trees() {
        let d = std::env::temp_dir().join(format!("wend-clean-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        for p in [
            "var/run/mysqld",
            "var/lock/subsys",
            "var/lib/rpm",
            "run/keep",
            "outside",
        ] {
            std::fs::create_dir_all(d.join(p)).unwrap();
        }
        for p in [
            "var/run/httpd.pid",
            "var/run/mysqld/mysqld.pid",
            "var/lock/subsys/httpd",
            "var/lib/rpm/__db.001",
            "var/lib/rpm/Packages",
            "run/keep/x.pid",
            "outside/f",
        ] {
            std::fs::write(d.join(p), "x").unwrap();
        }
        std::os::unix::fs::symlink(d.join("outside"), d.join("var/run/link")).unwrap();
        assert_eq!(boot_clean(&d), Ok(5));
        assert!(d.join("var/run/mysqld").is_dir() && d.join("var/lock/subsys").is_dir());
        assert!(d.join("var/lib/rpm/Packages").exists() && d.join("run/keep/x.pid").exists());
        assert!(d.join("outside/f").exists() && !d.join("var/run/link").exists());
        std::fs::remove_dir_all(&d).unwrap();
        // EL7: var/run is a symlink to ../run, which is left alone
        let e = std::env::temp_dir().join(format!("wend-clean7-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&e);
        std::fs::create_dir_all(e.join("run")).unwrap();
        std::fs::create_dir_all(e.join("var")).unwrap();
        std::fs::write(e.join("run/a.pid"), "x").unwrap();
        std::os::unix::fs::symlink("../run", e.join("var/run")).unwrap();
        assert_eq!(boot_clean(&e), Ok(0));
        assert!(e.join("run/a.pid").exists());
        std::fs::remove_dir_all(&e).unwrap();
    }

    #[test]
    fn machine_id_from_the_root_else_the_fingerprint() {
        let d = std::env::temp_dir().join(format!("wend-mid-{}", std::process::id()));
        std::fs::create_dir_all(d.join("var/lib/dbus")).unwrap();
        assert_eq!(machine_id(&d, "sha256:0123456789abcdef0123"), "0123456789abcdef");
        std::fs::write(d.join("var/lib/dbus/machine-id"), "ABCDEF0123456789abcdef0123456789\n").unwrap();
        assert_eq!(machine_id(&d, "sha256:00"), "abcdef0123456789abcdef0123456789");
        std::fs::remove_dir_all(&d).unwrap();
    }
}
