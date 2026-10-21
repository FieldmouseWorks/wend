//! `wend selftest`: what the client needs from the machine's kernel, tried one by one. The client is static (musl),
//! so the box's libc doesn't matter, but its kernel does: Rust's std targets Linux 3.2 and up, and CentOS 4 to 6 run
//! 2.6.9 to 2.6.32. Each item is something `upgrade`, `rehearse` and the verifying proxy do; a failure names the item
//! and its error, and the rest still run.

use std::io::{Read, Write};
use std::os::unix::fs::symlink;
use std::os::unix::io::AsRawFd;
use std::os::unix::process::CommandExt;
use std::process::{Command, Stdio};

type Item = fn(&std::path::Path) -> Result<String, String>;

fn spawn(_: &std::path::Path) -> Result<String, String> {
    let o = Command::new("/bin/sh")
        .args(["-c", "echo ok"])
        .output()
        .map_err(|e| e.to_string())?;
    match String::from_utf8_lossy(&o.stdout).trim() {
        "ok" => Ok("/bin/sh ran".into()),
        x => Err(format!("/bin/sh printed {x:?}")),
    }
}

fn pipes(_: &std::path::Path) -> Result<String, String> {
    let mut c = Command::new("cat")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .map_err(|e| e.to_string())?;
    c.stdin
        .take()
        .unwrap()
        .write_all(b"through cat")
        .map_err(|e| e.to_string())?;
    let mut out = String::new();
    c.stdout
        .take()
        .unwrap()
        .read_to_string(&mut out)
        .map_err(|e| e.to_string())?;
    c.wait().map_err(|e| e.to_string())?;
    if out == "through cat" {
        Ok("stdin and stdout piped".into())
    } else {
        Err(format!("cat gave back {out:?}"))
    }
}

fn detached(_: &std::path::Path) -> Result<String, String> {
    let mut c = Command::new("/bin/sh");
    c.args(["-c", "cut -d' ' -f6 /proc/$$/stat"])
        .stdout(Stdio::piped())
        .stdin(Stdio::null());
    // SAFETY: setsid only makes the child the leader of a new session; it runs between fork and exec
    unsafe {
        c.pre_exec(|| {
            libc::setsid();
            Ok(())
        });
    }
    let ch = c.spawn().map_err(|e| e.to_string())?;
    let pid = ch.id();
    let o = ch.wait_with_output().map_err(|e| e.to_string())?;
    match String::from_utf8_lossy(&o.stdout).trim().parse::<u32>() {
        Ok(sid) if sid == pid => Ok("a child leads its own session".into()),
        Ok(sid) => Err(format!("the child's session is {sid}, not its own {pid}")),
        Err(_) => Err(format!(
            "couldn't read the child's session: {:?}",
            String::from_utf8_lossy(&o.stdout)
        )),
    }
}

fn files(dir: &std::path::Path) -> Result<String, String> {
    let e = |e: std::io::Error| e.to_string();
    let t = dir.join("tree/a/b");
    std::fs::create_dir_all(&t).map_err(e)?;
    std::fs::write(t.join("f"), b"bytes").map_err(e)?;
    std::fs::copy(t.join("f"), t.join("g")).map_err(e)?;
    std::fs::rename(t.join("g"), t.join("h")).map_err(e)?;
    symlink("h", t.join("l")).map_err(e)?;
    let m = std::fs::symlink_metadata(t.join("l")).map_err(e)?;
    if !m.file_type().is_symlink() || std::fs::metadata(t.join("l")).map_err(e)?.len() != 5 {
        return Err("a symlink or its target read wrong".into());
    }
    let n = std::fs::read_dir(&t).map_err(e)?.count();
    std::fs::remove_dir_all(dir.join("tree")).map_err(e)?;
    if dir.join("tree").exists() {
        Err("remove_dir_all left the tree".into())
    } else {
        Ok(format!("{n} entries made, read and removed"))
    }
}

fn lock(dir: &std::path::Path) -> Result<String, String> {
    let f = std::fs::File::create(dir.join("lock")).map_err(|e| e.to_string())?;
    // SAFETY: flock on a file descriptor this process owns
    if unsafe { libc::flock(f.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
        return Err(std::io::Error::last_os_error().to_string());
    }
    Ok("flock taken".into())
}

fn loopback(_: &std::path::Path) -> Result<String, String> {
    let l = std::net::TcpListener::bind("127.0.0.1:0").map_err(|e| e.to_string())?;
    let at = l.local_addr().map_err(|e| e.to_string())?;
    let t = std::thread::spawn(move || -> Result<(), String> {
        let (mut s, _) = l.accept().map_err(|e| e.to_string())?;
        let mut b = [0u8; 4];
        s.read_exact(&mut b).map_err(|e| e.to_string())?;
        s.write_all(&b).map_err(|e| e.to_string())
    });
    let mut s = std::net::TcpStream::connect(at).map_err(|e| e.to_string())?;
    s.write_all(b"ping").map_err(|e| e.to_string())?;
    let mut b = [0u8; 4];
    s.read_exact(&mut b).map_err(|e| e.to_string())?;
    t.join().map_err(|_| "the thread panicked".to_string())??;
    if &b == b"ping" {
        Ok(format!("a thread echoed over {at}"))
    } else {
        Err(format!("echo came back {b:?}"))
    }
}

fn disk(_: &std::path::Path) -> Result<String, String> {
    let mut s: libc::statvfs = unsafe { std::mem::zeroed() };
    // SAFETY: statvfs fills the struct it is given from a NUL-terminated path
    if unsafe { libc::statvfs(c"/".as_ptr(), &mut s) } != 0 {
        return Err(std::io::Error::last_os_error().to_string());
    }
    Ok(format!(
        "/ has {} MiB free",
        (s.f_bavail as u64 * s.f_frsize as u64) >> 20
    ))
}

fn hashing(_: &std::path::Path) -> Result<String, String> {
    // a HashMap's keys come from the kernel's random source (getrandom, else /dev/urandom)
    let m: std::collections::HashMap<u8, u8> = (0..4).map(|i| (i, i)).collect();
    if m.len() == 4 {
        Ok("random keys read".into())
    } else {
        Err("a HashMap lost entries".into())
    }
}

const ITEMS: &[(&str, Item)] = &[
    ("spawn", spawn),
    ("pipes", pipes),
    ("detached", detached),
    ("files", files),
    ("lock", lock),
    ("loopback", loopback),
    ("disk", disk),
    ("random", hashing),
];

/// Every item, "ok NAME: what" or "FAIL NAME: why" a line; whether all passed.
pub fn run() -> bool {
    let mut u: libc::utsname = unsafe { std::mem::zeroed() };
    // SAFETY: uname fills the struct it is given
    unsafe { libc::uname(&mut u) };
    let field = |f: &[libc::c_char]| {
        unsafe { std::ffi::CStr::from_ptr(f.as_ptr()) }
            .to_string_lossy()
            .into_owned()
    };
    println!(
        "wend {} on {} {}",
        env!("CARGO_PKG_VERSION"),
        field(&u.machine),
        field(&u.release)
    );
    let dir = std::env::temp_dir().join(format!("wend-selftest-{}", std::process::id()));
    if let Err(e) = std::fs::create_dir_all(&dir) {
        println!("FAIL files: {}: {e}", dir.display());
        return false;
    }
    let mut ok = true;
    for (name, item) in ITEMS {
        match std::panic::catch_unwind(|| item(&dir)) {
            Ok(Ok(what)) => println!("ok {name}: {what}"),
            Ok(Err(why)) => {
                ok = false;
                println!("FAIL {name}: {why}");
            }
            Err(_) => {
                ok = false;
                println!("FAIL {name}: panicked");
            }
        }
    }
    let _ = std::fs::remove_dir_all(&dir);
    ok
}

#[cfg(test)]
mod tests {
    /// The build host's kernel is new: every item passes there, so a failure on a box is the box's.
    #[test]
    fn every_item_passes_on_the_build_host() {
        assert!(super::run());
    }
}
