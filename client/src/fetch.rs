//! The verifying proxy (SPEC "Pinned sources", open question 12): with `wend walk --run RUN.json` the box's repos
//! point here, and it serves only bytes that hash right. A run pins each hop's top metadata file (`Release`,
//! `repomd.xml`) by sha256. Every file those list (indexes) is trusted at the hash they list, and every package an
//! index lists at the hash the index gives. Anything else is refused. Bytes come from the cache, then each
//! `--source` in order, then the origin (unless offline): the first bytes that hash right win, and stay in the cache
//! under their hash, so a second walk needs no network at all.
//!
//! Fetching shells out to curl (proxies, CA bundles and TLS are the host's). Indexes are parsed in memory.

use crate::canon::sha256_hex;
use flate2::read::MultiGzDecoder;
use serde_json::Value;
use sha1::{Digest as _, Sha1};
use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

#[derive(Clone, Debug, PartialEq)]
pub struct Digest {
    /// "sha256" or "sha1": every Debian Release has SHA256; EL5's repomd and primary.xml only SHA-1
    alg: &'static str,
    hex: String,
}

impl Digest {
    fn of(alg: &'static str, bytes: &[u8]) -> Digest {
        let hex = match alg {
            "sha1" => Sha1::digest(bytes).iter().map(|b| format!("{b:02x}")).collect(),
            _ => sha256_hex(bytes),
        };
        Digest { alg, hex }
    }
    fn parse(alg: &str, hex: &str) -> Option<Digest> {
        let alg = match alg.to_ascii_lowercase().as_str() {
            "sha256" => "sha256",
            "sha1" | "sha" => "sha1",
            _ => return None,
        };
        let n = if alg == "sha256" { 64 } else { 40 };
        let hex = hex.to_ascii_lowercase();
        (hex.len() == n && hex.bytes().all(|c| c.is_ascii_hexdigit())).then_some(Digest { alg, hex })
    }
}

#[derive(Clone, Debug)]
struct Pin {
    digest: Digest,
    /// the origin URL, with its scheme
    origin: String,
    /// the digest is of the uncompressed bytes (an index pinned by the path a Release lists it under, wheezy)
    plain: bool,
}

pub struct Proxy {
    pub port: u16,
    state: Arc<State>,
}

struct State {
    /// keyed by "host/path": the proxy's own URL space, http://127.0.0.1:PORT/host/path
    pins: Mutex<HashMap<String, Pin>>,
    cache: PathBuf,
    sources: Vec<String>,
    offline: bool,
    refused: Mutex<Vec<String>>,
    /// the run's own pins, for learning every index once when a package turns up unknown
    tops: Vec<(String, Pin)>,
    learned_all: Mutex<bool>,
}

/// A lock that survives a panicked holder (one bad connection mustn't take the proxy down).
fn lock<T>(m: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

fn key_of(url: &str) -> Option<&str> {
    url.split_once("://").map(|(_, r)| r)
}

impl Proxy {
    /// Pins from every hop's `sources` in a run file; listens on a free port on 127.0.0.1.
    pub fn start(run: &Value, cache: &Path, sources: Vec<String>, offline: bool) -> Result<Proxy, String> {
        let mut pins = HashMap::new();
        for h in run.get("hops").and_then(Value::as_array).into_iter().flatten() {
            for (url, d) in h.get("sources").and_then(Value::as_object).into_iter().flatten() {
                let d = d
                    .as_str()
                    .and_then(|d| d.split_once(':'))
                    .and_then(|(a, x)| Digest::parse(a, x))
                    .filter(|d| d.alg == "sha256");
                let (Some(k), Some(digest)) = (key_of(url), d) else {
                    return Err(format!("the run pins {url} with something that isn't a sha256"));
                };
                // an index pinned directly (no Release survived, wheezy) is hashed uncompressed
                let plain = url.contains("/dists/") && !url.ends_with("Release");
                pins.insert(
                    k.to_string(),
                    Pin {
                        digest,
                        origin: url.clone(),
                        plain,
                    },
                );
            }
        }
        if pins.is_empty() {
            return Err(
                "the run pins no sources (it predates SPEC's pinned sources): replay the route to get one".into(),
            );
        }
        std::fs::create_dir_all(cache).map_err(|e| format!("{}: {e}", cache.display()))?;
        let listener = TcpListener::bind("127.0.0.1:0").map_err(|e| format!("proxy: {e}"))?;
        let port = listener.local_addr().map_err(|e| e.to_string())?.port();
        let tops = pins.iter().map(|(k, p)| (k.clone(), p.clone())).collect();
        let state = Arc::new(State {
            tops,
            learned_all: Mutex::new(false),
            pins: Mutex::new(pins),
            cache: cache.to_path_buf(),
            sources,
            offline,
            refused: Mutex::new(vec![]),
        });
        let s = state.clone();
        std::thread::spawn(move || {
            for c in listener.incoming().flatten() {
                let s = s.clone();
                std::thread::spawn(move || {
                    let _ = serve(&s, c);
                });
            }
        });
        Ok(Proxy { port, state })
    }

    pub fn cache(&self) -> &Path {
        &self.state.cache
    }

    pub fn base(&self) -> String {
        format!("http://127.0.0.1:{}", self.port)
    }

    /// SPEC "Moved mirrors": every top file the run pins, from the cache, a --source or the origin, verified before
    /// the walk touches anything. A snapshot no source has any more refuses the walk here, not halfway through a hop.
    pub fn ensure_pinned(&self) -> Result<(), String> {
        let missing = learn_all(&self.state);
        if missing.is_empty() {
            return Ok(());
        }
        Err(format!(
            "{} of the run's pinned sources can't be had from the cache, any --source or the origin:\n  {}",
            missing.len(),
            missing.join("\n  ")
        ))
    }

    /// What the proxy refused since the last call (each once), for the walk's log.
    pub fn take_refused(&self) -> Vec<String> {
        std::mem::take(&mut *lock(&self.state.refused))
    }
}

/// One connection: requests in order (keep-alive, as apt pipelines them), one response each.
fn serve(s: &State, c: TcpStream) -> std::io::Result<()> {
    let mut out = c.try_clone()?;
    let mut rd = BufReader::new(c);
    loop {
        let mut line = String::new();
        if rd.read_line(&mut line)? == 0 {
            return Ok(());
        }
        let mut close = !line.contains("HTTP/1.1");
        loop {
            let mut h = String::new();
            if rd.read_line(&mut h)? == 0 || h == "\r\n" || h == "\n" {
                break;
            }
            let l = h.to_ascii_lowercase();
            if l.starts_with("connection:") {
                close = l.contains("close");
            }
        }
        let mut parts = line.split_whitespace();
        let (method, path) = (parts.next().unwrap_or(""), parts.next().unwrap_or(""));
        let conn = if close { "close" } else { "keep-alive" };
        let r = if method == "GET" || method == "HEAD" {
            answer(s, path)
        } else {
            Err((405, format!("{method} not allowed")))
        };
        match r {
            Ok(file) => {
                let len = std::fs::metadata(&file)?.len();
                write!(
                    out,
                    "HTTP/1.1 200 OK\r\nContent-Length: {len}\r\nConnection: {conn}\r\n\r\n"
                )?;
                if method == "GET" {
                    std::io::copy(&mut std::fs::File::open(&file)?, &mut out)?;
                }
            }
            Err((code, why)) => {
                if code == 403 || code == 502 {
                    lock(&s.refused).push(why.clone());
                }
                let body = format!("{why}\n");
                write!(
                    out,
                    "HTTP/1.1 {code} {}\r\nContent-Length: {}\r\nConnection: {conn}\r\n\r\n{}",
                    match code {
                        404 => "Not Found",
                        405 => "Method Not Allowed",
                        _ => "Refused",
                    },
                    if method == "HEAD" { 0 } else { body.len() },
                    if method == "HEAD" { "" } else { body.as_str() }
                )?;
            }
        }
        out.flush()?;
        if close {
            return Ok(());
        }
    }
}

/// The cached file to serve for a request path, or (status, why).
fn answer(s: &State, path: &str) -> Result<PathBuf, (u16, String)> {
    let key = unescape(path.split('?').next().unwrap_or("").trim_start_matches('/'));
    let key = key.as_str();
    let (host, rest) = key.split_once('/').unwrap_or((key, ""));
    if matches!(host, "" | "." | "..") || rest.split('/').any(|seg| seg.is_empty() || seg == "." || seg == "..") {
        return Err((403, format!("refused {key}: not a host/path")));
    }
    let pin = lock(&s.pins).get(key).cloned();
    if let Some(p) = pin {
        if p.plain {
            return fetch_plain(s, key, &p, false);
        }
        let f = fetch(s, key, &p)?;
        learn(s, key, &p, &f);
        return Ok(f);
    }
    // a compressed form of an index pinned uncompressed (wheezy: the Release is gone, only Packages is pinned)
    if let Some(plain) = key.strip_suffix(".gz") {
        let p = lock(&s.pins).get(plain).cloned();
        if let Some(p) = p.filter(|p| p.plain) {
            return fetch_plain(s, plain, &p, true);
        }
    }
    // by-hash names its own digest: trusted when a trusted index lists it
    if let Some((dir, hex)) = key.rsplit_once("/by-hash/SHA256/") {
        // only beside the index that lists it, so the host and path stay the pinned ones
        let found = lock(&s.pins)
            .iter()
            .find(|(k, p)| p.digest.hex == hex && !p.plain && k.rsplit_once('/').is_some_and(|(d, _)| d == dir))
            .map(|(k, p)| (k.clone(), p.clone()));
        if let Some((name, p)) = found {
            let origin = format!("{}://{key}", p.origin.split_once("://").map_or("http", |x| x.0));
            let f = fetch(s, key, &Pin { origin, ..p.clone() })?;
            learn(s, &name, &p, &f);
            return Ok(f);
        }
    }
    // a suite whose hop pinned an index directly, not its Release: serve the Release unverified so apt finds the
    // indexes (it vouches for nothing here; the indexes are checked against their own pins)
    if let Some((dir, name)) = key.rsplit_once('/') {
        if matches!(name, "Release" | "Release.gpg") {
            let p = s
                .pins
                .lock()
                .unwrap()
                .iter()
                .find(|(k, p)| p.plain && suite_dir(k).as_deref() == Some(dir))
                .map(|(_, p)| p.clone());
            if let Some(p) = p {
                let origin = format!("{}://{key}", p.origin.split_once("://").map_or("http", |x| x.0));
                return passthrough(s, &origin);
            }
        }
    }
    let last = key.rsplit('/').next().unwrap_or("");
    let meta = matches!(
        last,
        "InRelease" | "Release" | "Release.gpg" | "repomd.xml" | "repomd.xml.asc" | "repomd.xml.key"
    ) || key.contains("/dists/") && !key.ends_with(".deb")
        || key.contains("/repodata/");
    if meta {
        // metadata the run didn't pin: the package manager falls back to what it did pin
        return Err((404, format!("not pinned: {key}")));
    }
    // the package manager may have read its indexes from its own cache: learn every pinned index once, then look again
    // (held for the whole pass, so a parallel request waits for it instead of being refused; tried again until every
    // pinned index was had)
    learn_all(s);
    let pin = lock(&s.pins).get(key).cloned();
    if let Some(p) = pin {
        return fetch(s, key, &p);
    }
    Err((403, format!("refused {key}: no pinned index lists it")))
}

/// Fetch and learn every top file the run pins, once (tried again until every one was had). What couldn't be had,
/// each with why, sorted.
fn learn_all(s: &State) -> Vec<String> {
    let mut done = lock(&s.learned_all);
    if *done {
        return vec![];
    }
    let mut missing = vec![];
    for (k, p) in &s.tops {
        let r = if p.plain {
            fetch_plain(s, k, p, false).map(|_| ())
        } else {
            fetch(s, k, p).map(|f| learn(s, k, p, &f))
        };
        if let Err((_, why)) = r {
            missing.push(why);
        }
    }
    *done = missing.is_empty();
    missing.sort();
    missing
}

/// %XX decoded (yum asks for libstdc%2B%2B); anything malformed stays as it is.
fn unescape(p: &str) -> String {
    let b = p.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        let hex = |c: u8| (c as char).to_digit(16);
        if b[i] == b'%' && i + 2 < b.len() {
            if let (Some(h), Some(l)) = (hex(b[i + 1]), hex(b[i + 2])) {
                out.push((h * 16 + l) as u8);
                i += 3;
                continue;
            }
        }
        out.push(b[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// ".../dists/<suite>" of an index key.
fn suite_dir(k: &str) -> Option<String> {
    let (pre, post) = k.split_once("/dists/")?;
    Some(format!("{pre}/dists/{}", post.split('/').next()?))
}

/// Where bytes nothing can verify by hash are kept, by the name they were asked for: an unverified Release
/// (passthrough) and an index's original .gz (fetch_plain checks it again on every use).
fn named(s: &State, key: &str) -> PathBuf {
    s.cache.join("named").join(sha256_hex(key.as_bytes()))
}

fn keep_named(s: &State, key: &str, bytes: &[u8]) -> Result<PathBuf, (u16, String)> {
    let dst = named(s, key);
    let io = |e: std::io::Error| (500, format!("cache: {e}"));
    std::fs::create_dir_all(dst.parent().unwrap()).map_err(io)?;
    let tmp = tmp_in(s);
    write_new(&tmp, bytes).map_err(io)?;
    std::fs::rename(&tmp, &dst).map_err(io)?;
    Ok(dst)
}

/// Cache path for a digest.
fn cached(s: &State, d: &Digest) -> PathBuf {
    s.cache.join(d.alg).join(&d.hex)
}

/// Every place to try for a "host/path", in order: each --source (a directory or URL laid out host/path), then the
/// origin.
fn candidates(s: &State, key: &str, origin: &str) -> Vec<String> {
    let mut v: Vec<String> = s
        .sources
        .iter()
        .map(|b| format!("{}/{key}", b.trim_end_matches('/')))
        .collect();
    if !s.offline {
        v.push(origin.to_string());
    }
    v
}

fn download(src: &str) -> Result<Vec<u8>, String> {
    if !src.contains("://") {
        // a plain file only: not a FIFO that never ends, nor a device that never stops
        let md = std::fs::metadata(src).map_err(|e| format!("{src}: {e}"))?;
        if !md.is_file() {
            return Err(format!("{src}: not a plain file"));
        }
        return std::fs::read(src).map_err(|e| format!("{src}: {e}"));
    }
    // straight into memory: nothing on disk until the bytes hash right
    let out = std::process::Command::new("curl")
        .args([
            "-sfL",
            "--globoff",
            "--proto",
            "=http,https",
            "--proto-redir",
            "=http,https",
        ])
        .args([
            "--retry",
            "2",
            "--connect-timeout",
            "60",
            "--speed-limit",
            "1024",
            "--speed-time",
            "120",
            "--",
        ])
        .arg(src)
        .stdin(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .output()
        .map_err(|e| format!("curl: {e}"))?;
    if !out.status.success() {
        return Err(format!("{src}: curl exit {}", out.status.code().unwrap_or(-1)));
    }
    Ok(out.stdout)
}

/// A fresh file, never through a link or over something already there.
fn write_new(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    use std::io::Write;
    use std::os::unix::fs::OpenOptionsExt;
    let mut f = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?;
    f.write_all(bytes)
}

fn tmp_in(s: &State) -> PathBuf {
    use std::sync::atomic::{AtomicU64, Ordering};
    static N: AtomicU64 = AtomicU64::new(0);
    s.cache.join(format!(
        ".tmp-{}-{}",
        std::process::id(),
        N.fetch_add(1, Ordering::Relaxed)
    ))
}

/// Keep verified bytes under their digest (atomically) and return the path.
fn keep(s: &State, d: &Digest, bytes: &[u8]) -> Result<PathBuf, (u16, String)> {
    let dst = cached(s, d);
    let io = |e: std::io::Error| (500, format!("cache: {e}"));
    std::fs::create_dir_all(dst.parent().unwrap()).map_err(io)?;
    let tmp = tmp_in(s);
    write_new(&tmp, bytes).map_err(io)?;
    std::fs::rename(&tmp, &dst).map_err(io)?;
    Ok(dst)
}

/// The bytes for a pin: the cache, else the first source whose bytes hash right.
fn fetch(s: &State, key: &str, p: &Pin) -> Result<PathBuf, (u16, String)> {
    let dst = cached(s, &p.digest);
    if std::fs::read(&dst).is_ok_and(|b| Digest::of(p.digest.alg, &b) == p.digest) {
        return Ok(dst);
    }
    let mut tried = vec![];
    for src in candidates(s, key, &p.origin) {
        match download(&src) {
            Ok(b) if Digest::of(p.digest.alg, &b) == p.digest => return keep(s, &p.digest, &b),
            Ok(_) => tried.push(format!("{src}: wrong bytes")),
            Err(e) => tried.push(e),
        }
    }
    Err((
        502,
        format!(
            "no source has {key} at {}:{} ({})",
            p.digest.alg,
            p.digest.hex,
            why(&tried)
        ),
    ))
}

/// An index pinned by its uncompressed bytes (the archive may only serve it gzipped). The plain bytes are cached
/// under the pinned digest; `gz` serves a gzip made here from those verified bytes, never the archive's own .gz
/// (a .gz can carry members after the one that hashes right, and apt would read them too).
fn fetch_plain(s: &State, key: &str, p: &Pin, gz: bool) -> Result<PathBuf, (u16, String)> {
    let gz_key = format!("{key}.gz");
    let ok = |x: &[u8]| Digest::of(p.digest.alg, x) == p.digest;
    let mut plain = std::fs::read(cached(s, &p.digest)).ok().filter(|b| ok(b));
    let mut tried = vec![];
    let forms = [
        (gz_key.clone(), format!("{}.gz", p.origin), true),
        (key.to_string(), p.origin.clone(), false),
    ];
    'found: for (k, origin, zipped) in forms {
        if plain.is_some() {
            break;
        }
        for src in candidates(s, &k, &origin) {
            match download(&src) {
                Ok(b) => match if zipped { gunzip(&b) } else { Some(b) } {
                    Some(x) if ok(&x) => {
                        plain = Some(x);
                        break 'found;
                    }
                    _ => tried.push(format!("{src}: wrong bytes")),
                },
                Err(e) => tried.push(e),
            }
        }
    }
    let Some(x) = plain else {
        return Err((
            502,
            format!(
                "no source has {key} at {}:{} ({})",
                p.digest.alg,
                p.digest.hex,
                why(&tried)
            ),
        ));
    };
    learn_index(s, key, &x);
    let f = keep(s, &p.digest, &x)?;
    if !gz {
        return Ok(f);
    }
    // apt of this age checks the uncompressed hash, so any gzip of the verified bytes will do
    let kept = named(s, &gz_key);
    if std::fs::read(&kept)
        .ok()
        .and_then(|b| gunzip(&b))
        .is_some_and(|y| y == x)
    {
        return Ok(kept);
    }
    let mut z = flate2::write::GzEncoder::new(vec![], flate2::Compression::default());
    z.write_all(&x).map_err(|e| (500, e.to_string()))?;
    let z = z.finish().map_err(|e| (500, e.to_string()))?;
    keep_named(s, &gz_key, &z)
}

/// Unverified bytes: only a Release that vouches for nothing (see answer()). Kept by name, so an offline walk gets
/// the same file the online one did.
fn passthrough(s: &State, origin: &str) -> Result<PathBuf, (u16, String)> {
    let key = key_of(origin).unwrap_or(origin);
    let mut tried = vec![];
    for src in candidates(s, key, origin) {
        match download(&src) {
            Ok(b) => return keep_named(s, key, &b),
            Err(e) => tried.push(e),
        }
    }
    let kept = named(s, key);
    if kept.is_file() {
        return Ok(kept);
    }
    Err((404, format!("{key}: {}", why(&tried))))
}

/// What every source said, or why there were none to ask.
fn why(tried: &[String]) -> String {
    if tried.is_empty() {
        "offline, no --source, and no cached copy hashes right".into()
    } else {
        tried.join("; ")
    }
}

fn gunzip(b: &[u8]) -> Option<Vec<u8>> {
    let mut out = vec![];
    // every member, as zlib's gzread (apt) and zcat read it
    MultiGzDecoder::new(b).read_to_end(&mut out).ok()?;
    Some(out)
}

/// An index as its name says it's compressed: gzip, xz (EPEL 9's primary.xml) or zstd (EPEL 10's), or as it is. None
/// for a compression there's no decoder here for, or bytes that don't decode.
fn decompress(key: &str, b: &[u8]) -> Option<Vec<u8>> {
    let mut out = vec![];
    if key.ends_with(".gz") {
        return gunzip(b);
    } else if key.ends_with(".xz") {
        lzma_rs::xz_decompress(&mut &b[..], &mut out).ok()?;
    } else if key.ends_with(".zst") {
        // every frame, as zstd -dc reads them
        let mut rest = b;
        while !rest.is_empty() {
            ruzstd::decoding::StreamingDecoder::new(&mut rest)
                .ok()?
                .read_to_end(&mut out)
                .ok()?;
        }
    } else if [".bz2", ".lzma"].iter().any(|z| key.ends_with(z)) {
        return None;
    } else {
        out = b.to_vec();
    }
    Some(out)
}

fn pin_more(s: &State, key: String, digest: Digest, scheme: &str) {
    let origin = format!("{scheme}://{key}");
    lock(&s.pins).entry(key).or_insert(Pin {
        digest,
        origin,
        plain: false,
    });
}

/// A verified file may vouch for more: a Release for its indexes, a repomd.xml for its data files, an index for its
/// packages.
fn learn(s: &State, key: &str, p: &Pin, file: &Path) {
    let Ok(bytes) = std::fs::read(file) else { return };
    let scheme = p.origin.split_once("://").map_or("http", |x| x.0).to_string();
    let last = key.rsplit('/').next().unwrap_or("");
    if last == "Release" || last == "InRelease" {
        let dir = key.rsplit_once('/').map_or("", |x| x.0);
        for (rel, d) in release_entries(&String::from_utf8_lossy(&bytes)) {
            pin_more(s, format!("{dir}/{rel}"), d, &scheme);
        }
    } else if last == "repomd.xml" {
        let base = key.strip_suffix("repodata/repomd.xml").unwrap_or(key);
        let entries = repomd_entries(&String::from_utf8_lossy(&bytes));
        for (href, d) in &entries {
            pin_more(s, format!("{base}{href}"), d.clone(), &scheme);
        }
        // yum and dnf may read primary.sqlite instead, which this can't parse: learn the packages from the XML, however
        // it's compressed (EPEL 9 lists it only as .xz, EPEL 10 only as .zst)
        let primary = |h: &str| {
            [".gz", ".xz", ".zst"]
                .iter()
                .any(|z| h.ends_with(&format!("primary.xml{z}")))
        };
        for (href, _) in entries.iter().filter(|(h, _)| primary(h)) {
            let k = format!("{base}{href}");
            let pin = lock(&s.pins).get(&k).cloned();
            if let Some(pp) = pin {
                if let Ok(f) = fetch(s, &k, &pp) {
                    learn(s, &k, &pp, &f);
                }
            }
        }
    } else if let Some(stem) = [".bz2", ".lzma"].iter().find_map(|z| key.strip_suffix(z)) {
        // no decoder here for these: learn the packages from the gzip (or plain) form the same Release lists
        for k in [format!("{stem}.gz"), stem.to_string()] {
            let sib = lock(&s.pins).get(&k).cloned();
            if let Some(sp) = sib {
                if let Ok(f) = fetch(s, &k, &sp) {
                    learn(s, &k, &sp, &f);
                    return;
                }
            }
        }
    } else if let Some(x) = decompress(key, &bytes) {
        learn_index(s, key, &x);
    }
}

/// Packages and primary.xml: every package they list, at the hash they give.
fn learn_index(s: &State, key: &str, plain: &[u8]) {
    let scheme = {
        let pins = lock(&s.pins);
        pins.get(key)
            .or_else(|| pins.get(key.trim_end_matches(".gz")))
            .map(|p| p.origin.split_once("://").map_or("http", |x| x.0).to_string())
            .unwrap_or_else(|| "http".into())
    };
    let name = key.rsplit('/').next().unwrap_or("");
    let text = String::from_utf8_lossy(plain);
    if name.starts_with("Packages") && key.contains("/dists/") {
        let root = key.split("/dists/").next().unwrap_or("");
        for (file, d) in packages_entries(&text) {
            pin_more(s, format!("{root}/{file}"), d, &scheme);
        }
    } else if name.contains("primary.xml") {
        let base = key.split("repodata/").next().unwrap_or("");
        for (href, d) in primary_entries(&text) {
            pin_more(s, format!("{base}{href}"), d, &scheme);
        }
    }
}

/// A Release's file list: SHA256 when it has one, else SHA1.
pub fn release_entries(text: &str) -> Vec<(String, Digest)> {
    let section = |name: &str, alg: &str| -> Vec<(String, Digest)> {
        let mut out = vec![];
        let mut on = false;
        for l in text.lines() {
            if !l.starts_with(' ') {
                on = l.trim_end() == format!("{name}:");
                continue;
            }
            let f: Vec<&str> = l.split_whitespace().collect();
            if on && f.len() == 3 {
                if let Some(d) = Digest::parse(alg, f[0]) {
                    out.push((f[2].to_string(), d));
                }
            }
        }
        out
    };
    let v = section("SHA256", "sha256");
    if v.is_empty() {
        section("SHA1", "sha1")
    } else {
        v
    }
}

/// repomd.xml's data files: (location href, checksum).
pub fn repomd_entries(text: &str) -> Vec<(String, Digest)> {
    let ck = regex::Regex::new(r#"<checksum type="([a-z0-9]+)">\s*([0-9a-fA-F]+)\s*</checksum>"#).unwrap();
    let loc = regex::Regex::new(r#"<location href="([^"]+)""#).unwrap();
    text.split("<data ")
        .skip(1)
        .filter_map(|block| {
            let c = ck.captures(block)?;
            let l = loc.captures(block)?;
            Some((l[1].to_string(), Digest::parse(&c[1], &c[2])?))
        })
        .collect()
}

/// A Packages index: (Filename, SHA256 or else SHA1).
pub fn packages_entries(text: &str) -> Vec<(String, Digest)> {
    text.split("\n\n")
        .filter_map(|st| {
            let field = |k: &str| {
                st.lines().find_map(|l| {
                    l.strip_prefix(k)
                        .and_then(|r| r.strip_prefix(':'))
                        .map(|v| v.trim().to_string())
                })
            };
            let file = field("Filename")?;
            let d = field("SHA256")
                .and_then(|h| Digest::parse("sha256", &h))
                .or_else(|| field("SHA1").and_then(|h| Digest::parse("sha1", &h)))?;
            Some((file, d))
        })
        .collect()
}

/// primary.xml: every package's (location href, checksum).
pub fn primary_entries(text: &str) -> Vec<(String, Digest)> {
    let ck = regex::Regex::new(r#"<checksum type="([a-z0-9]+)"[^>]*>([0-9a-fA-F]+)</checksum>"#).unwrap();
    let loc = regex::Regex::new(r#"<location (?:xml:base="[^"]*" )?href="([^"]+)""#).unwrap();
    text.split("<package ")
        .skip(1)
        .filter_map(|block| {
            let c = ck.captures(block)?;
            let l = loc.captures(block)?;
            Some((l[1].to_string(), Digest::parse(&c[1], &c[2])?))
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_release_repomd_packages_primary() {
        let rel = "Origin: Debian\nMD5Sum:\n 0123456789abcdef0123456789abcdef 10 main/binary-amd64/Packages\n\
                   SHA256:\n 9f2c3e2a1b0d9c8e7f6a5b4c3d2e1f0a9b8c7d6e5f4a3b2c1d0e9f8a7b6c5d4e 10 main/binary-amd64/Packages\n";
        let e = release_entries(rel);
        assert_eq!(e.len(), 1);
        assert_eq!(e[0].0, "main/binary-amd64/Packages");
        assert_eq!(e[0].1.alg, "sha256");
        let sha1_only = "SHA1:\n 0123456789abcdef0123456789abcdef01234567 10 main/Packages.gz\n";
        assert_eq!(release_entries(sha1_only)[0].1.alg, "sha1");

        let repomd = r#"<repomd><data type="primary"><checksum type="sha">0123456789abcdef0123456789abcdef01234567</checksum>
            <open-checksum type="sha">ffffffffffffffffffffffffffffffffffffffff</open-checksum>
            <location href="repodata/primary.xml.gz"/></data></repomd>"#;
        let e = repomd_entries(repomd);
        assert_eq!(
            e,
            vec![(
                "repodata/primary.xml.gz".into(),
                Digest::parse("sha", "0123456789abcdef0123456789abcdef01234567").unwrap()
            )]
        );

        let pk = "Package: a\nFilename: pool/main/a/a.deb\nMD5sum: x\nSHA256: 9f2c3e2a1b0d9c8e7f6a5b4c3d2e1f0a9b8c7d6e5f4a3b2c1d0e9f8a7b6c5d4e\n\nPackage: b\nFilename: pool/b.deb\nSHA1: 0123456789abcdef0123456789abcdef01234567\n";
        let e = packages_entries(pk);
        assert_eq!(e.len(), 2);
        assert_eq!((e[0].0.as_str(), e[1].1.alg), ("pool/main/a/a.deb", "sha1"));

        let prim = r#"<package type="rpm"><name>p7zip</name><checksum type="sha" pkgid="YES">b3fd14ec513523cc8410171ad236c689536f23c8</checksum><location href="p7zip-9.20.1-2.el5.x86_64.rpm"/></package>"#;
        assert_eq!(primary_entries(prim)[0].0, "p7zip-9.20.1-2.el5.x86_64.rpm");
    }

    fn get(port: u16, method: &str, path: &str) -> (u16, Vec<u8>) {
        let mut c = TcpStream::connect(("127.0.0.1", port)).unwrap();
        write!(c, "{method} {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n").unwrap();
        let mut b = vec![];
        c.read_to_end(&mut b).unwrap();
        let head_end = b.windows(4).position(|w| w == b"\r\n\r\n").unwrap() + 4;
        let code = String::from_utf8_lossy(&b[9..12]).parse().unwrap();
        (code, b[head_end..].to_vec())
    }

    #[test]
    fn proxy_serves_only_the_chain() {
        let dir = std::env::temp_dir().join(format!("wend-proxy-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let src = dir.join("src/h.test/el");
        std::fs::create_dir_all(src.join("repodata")).unwrap();
        std::fs::create_dir_all(src.join("Packages")).unwrap();
        let rpm = b"an rpm".to_vec();
        std::fs::write(src.join("Packages/a.rpm"), &rpm).unwrap();
        std::fs::write(src.join("Packages/b.rpm"), b"tampered").unwrap();
        let primary = format!(
            r#"<package type="rpm"><checksum type="sha256" pkgid="YES">{}</checksum><location href="Packages/a.rpm"/></package>
<package type="rpm"><checksum type="sha256" pkgid="YES">{}</checksum><location href="Packages/b.rpm"/></package>"#,
            sha256_hex(&rpm),
            sha256_hex(b"the real b")
        );
        let mut z = flate2::write::GzEncoder::new(vec![], flate2::Compression::default());
        z.write_all(primary.as_bytes()).unwrap();
        let pz = z.finish().unwrap();
        std::fs::write(src.join("repodata/primary.xml.gz"), &pz).unwrap();
        let repomd = format!(
            r#"<repomd><data type="primary"><checksum type="sha256">{}</checksum><location href="repodata/primary.xml.gz"/></data></repomd>"#,
            sha256_hex(&pz)
        );
        std::fs::write(src.join("repodata/repomd.xml"), &repomd).unwrap();
        let run = serde_json::json!({"hops": [{"sources": {
            "https://h.test/el/repodata/repomd.xml": format!("sha256:{}", sha256_hex(repomd.as_bytes()))}}]});
        let px = Proxy::start(
            &run,
            &dir.join("cache"),
            vec![dir.join("src").display().to_string()],
            true,
        )
        .unwrap();
        // cold: nothing learned yet, the miss learns every pinned index once
        assert_eq!(get(px.port, "GET", "/h.test/el/Packages/a.rpm"), (200, rpm));
        assert_eq!(get(px.port, "GET", "/h.test/el/Packages/b.rpm").0, 502);
        assert_eq!(get(px.port, "GET", "/h.test/el/Packages/c.rpm").0, 403);
        assert_eq!(get(px.port, "GET", "/h.test/el/repodata/repomd.xml.asc").0, 404);
        assert_eq!(get(px.port, "GET", "/../etc/passwd").0, 403);
        assert_eq!(get(px.port, "POST", "/h.test/el/Packages/a.rpm").0, 405);
        assert_eq!(get(px.port, "HEAD", "/h.test/el/repodata/repomd.xml").0, 200);
        assert_eq!(px.take_refused().len(), 3);
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// Two packages cut from EPEL's own primary.xml (2026-10-15), compressed by xz and zstd themselves: EPEL 9 lists its
    /// primary only as .xz, EPEL 10 only as .zst.
    type Fixture = (&'static str, &'static [u8], [(&'static str, &'static str); 2]);
    const EPEL: [Fixture; 2] = [
        (
            "primary.xml.xz",
            include_bytes!("../testdata/epel9-primary.xml.xz"),
            [
                (
                    "Packages/3/3cpio-0.14.0-1.el9.x86_64.rpm",
                    "0022bf15d74e01ea04f94c853445e2bfa839633447905c1329bb8442251f9333",
                ),
                (
                    "Packages/3/3proxy-1.0.0-1.el9.x86_64.rpm",
                    "3905164ef1b309ddb097bb731de946ec3a62413455f55b9a61e607bab5d4d3e7",
                ),
            ],
        ),
        (
            "primary.xml.zst",
            include_bytes!("../testdata/epel10-primary.xml.zst"),
            [
                (
                    "Packages/3/3cpio-0.14.0-1.el10_2.x86_64.rpm",
                    "f910ffeb1d39dedb050dbb2449d544912bbd410f46292f24cf1e1b25b407577e",
                ),
                (
                    "Packages/3/3proxy-1.0.0-1.el10_3.x86_64.rpm",
                    "17b64343997888c9c35e49cac6e13f3aae7c23a4dbb1c43036bb7b00cae21b83",
                ),
            ],
        ),
    ];

    #[test]
    fn decompresses_epel_primary_xz_and_zst() {
        for (name, z, want) in EPEL {
            let xml = decompress(name, z).unwrap();
            let got = primary_entries(&String::from_utf8(xml).unwrap());
            let want: Vec<_> = want
                .iter()
                .map(|(h, d)| (h.to_string(), Digest::parse("sha256", d).unwrap()))
                .collect();
            assert_eq!(got, want, "{name}");
        }
        // zstd -dc reads every frame of a file, and so does this
        let (_, z, _) = EPEL[1];
        let two = decompress("x.zst", &[z, z].concat()).unwrap();
        let one = decompress("x.zst", z).unwrap();
        assert_eq!(two, [one.clone(), one].concat());
        assert_eq!(decompress("Packages.bz2", b"BZh9"), None);
        assert_eq!(decompress("Packages", b"as is").unwrap(), b"as is");
    }

    #[test]
    fn learns_packages_from_a_primary_listed_only_as_xz_or_zst() {
        for (i, (name, z, want)) in EPEL.into_iter().enumerate() {
            let dir = std::env::temp_dir().join(format!("wend-epel-test-{}-{i}", std::process::id()));
            let _ = std::fs::remove_dir_all(&dir);
            let src = dir.join("src/h.test/epel");
            std::fs::create_dir_all(src.join("repodata")).unwrap();
            std::fs::create_dir_all(src.join("Packages/3")).unwrap();
            for (href, _) in want {
                std::fs::write(src.join(href), b"not the rpm the index names").unwrap();
            }
            let href = format!("repodata/{}-{name}", sha256_hex(z));
            std::fs::write(src.join(&href), z).unwrap();
            let repomd = format!(
                r#"<repomd><data type="primary"><checksum type="sha256">{}</checksum><location href="{href}"/></data></repomd>"#,
                sha256_hex(z)
            );
            std::fs::write(src.join("repodata/repomd.xml"), &repomd).unwrap();
            let run = serde_json::json!({"hops": [{"sources": {
                "https://h.test/epel/repodata/repomd.xml": format!("sha256:{}", sha256_hex(repomd.as_bytes()))}}]});
            let px = Proxy::start(
                &run,
                &dir.join("cache"),
                vec![dir.join("src").display().to_string()],
                true,
            )
            .unwrap();
            // listed: pinned at the index's hash, so wrong bytes are a bad source (502), not an unknown package (403)
            for (href, _) in want {
                assert_eq!(
                    get(px.port, "GET", &format!("/h.test/epel/{href}")).0,
                    502,
                    "{name}: {href}"
                );
            }
            assert_eq!(
                get(px.port, "GET", "/h.test/epel/Packages/3/unlisted.rpm").0,
                403,
                "{name}"
            );
            let _ = std::fs::remove_dir_all(&dir);
        }
    }

    #[test]
    fn plain_index_serves_only_the_verified_member() {
        let dir = std::env::temp_dir().join(format!("wend-plain-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let base = dir.join("src/h.test/debian");
        let idx = base.join("dists/wheezy/main/binary-amd64");
        std::fs::create_dir_all(&idx).unwrap();
        std::fs::create_dir_all(base.join("pool")).unwrap();
        let (a, b) = (b"deb a".to_vec(), b"deb b".to_vec());
        std::fs::write(base.join("pool/a.deb"), &a).unwrap();
        std::fs::write(base.join("pool/b.deb"), &b).unwrap();
        let stanza = |n: &str, d: &[u8]| format!("Package: {n}\nFilename: pool/{n}.deb\nSHA256: {}\n\n", sha256_hex(d));
        let pinned = stanza("a", &a);
        let gz = |t: &str| {
            let mut z = flate2::write::GzEncoder::new(vec![], flate2::Compression::default());
            z.write_all(t.as_bytes()).unwrap();
            z.finish().unwrap()
        };
        // a second gzip member smuggling in a stanza the run never pinned
        let mut two = gz(&pinned);
        two.extend(gz(&stanza("b", &b)));
        std::fs::write(idx.join("Packages.gz"), &two).unwrap();
        let run = serde_json::json!({"hops": [{"sources": {
            "http://h.test/debian/dists/wheezy/main/binary-amd64/Packages":
                format!("sha256:{}", sha256_hex(pinned.as_bytes()))}}]});
        let px = Proxy::start(
            &run,
            &dir.join("cache"),
            vec![dir.join("src").display().to_string()],
            true,
        )
        .unwrap();
        let path = "/h.test/debian/dists/wheezy/main/binary-amd64/Packages.gz";
        // read whole (as apt reads it) the .gz doesn't hash right: refused
        assert_eq!(get(px.port, "GET", path).0, 502);
        // the plain index from another source: served as a gzip of exactly the pinned bytes
        std::fs::write(idx.join("Packages"), &pinned).unwrap();
        let (code, body) = get(px.port, "GET", path);
        assert_eq!(code, 200);
        assert_eq!(gunzip(&body).unwrap(), pinned.as_bytes());
        assert_eq!(get(px.port, "GET", "/h.test/debian/pool/a.deb"), (200, a));
        assert_eq!(get(px.port, "GET", "/h.test/debian/pool/b.deb").0, 403);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_vanished_snapshot_refuses_before_the_walk() {
        let dir = std::env::temp_dir().join(format!("wend-pinned-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let src = dir.join("src/h.test/el/repodata");
        std::fs::create_dir_all(&src).unwrap();
        let repomd = "<repomd></repomd>";
        let run = serde_json::json!({"hops": [{"sources": {
            "https://h.test/el/repodata/repomd.xml": format!("sha256:{}", sha256_hex(repomd.as_bytes()))}}]});
        let px = Proxy::start(
            &run,
            &dir.join("cache"),
            vec![dir.join("src").display().to_string()],
            true,
        )
        .unwrap();
        // the mirror moved on: other bytes at the pinned path
        std::fs::write(src.join("repomd.xml"), "<repomd>newer</repomd>").unwrap();
        let e = px.ensure_pinned().unwrap_err();
        assert!(e.contains("h.test/el/repodata/repomd.xml"), "{e}");
        // a source that still has the snapshot
        std::fs::write(src.join("repomd.xml"), repomd).unwrap();
        px.ensure_pinned().unwrap();
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn temp_files_never_follow_links() {
        let d = std::env::temp_dir().join(format!("wend-wn-{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        let target = d.join("target");
        std::fs::write(&target, b"keep").unwrap();
        std::os::unix::fs::symlink(&target, d.join("planted")).unwrap();
        assert!(write_new(&d.join("planted"), b"evil").is_err());
        assert!(write_new(&target, b"evil").is_err());
        assert_eq!(std::fs::read(&target).unwrap(), b"keep");
        write_new(&d.join("fresh"), b"ok").unwrap();
        std::fs::remove_dir_all(&d).unwrap();
    }

    #[test]
    fn unescapes() {
        assert_eq!(unescape("a/libstdc%2B%2B-4.rpm"), "a/libstdc++-4.rpm");
        assert_eq!(unescape("a%2"), "a%2");
        assert_eq!(unescape("a%zz"), "a%zz");
    }

    #[test]
    fn digests() {
        assert_eq!(
            Digest::of("sha1", b"abc").hex,
            "a9993e364706816aba3e25717850c26c9cd0d89d"
        );
        assert!(Digest::parse("md5", "00").is_none());
        assert!(Digest::parse("sha256", "zz").is_none());
    }
}
