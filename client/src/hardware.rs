//! Hardware preflight (SPEC "Hardware preflight"): before a route touches a machine, can this machine land on
//! every release the route boots it into? The CPU's x86-64 level, the firmware, memory, free disk, and whether each
//! release's kernel drives the disk controllers and network cards the machine uses now. A machine that can't is told
//! the furthest release it can land on, before anything is touched.

use crate::atlas::{get, str_of, Atlas, Doc};
use crate::check::{booted_releases, CPU_LEVELS};
use serde_json::{json, Value};
use std::fs;
use std::path::Path;

/// The CPU features each x86-64 level adds (the x86-64 psABI), by the names `facts` records. Level 1 is any x86-64.
const LEVEL_FEATURES: [&[&str]; 2] = [
    &["cx16", "lahf_lm", "popcnt", "sse3", "sse4_1", "sse4_2", "ssse3"],
    &["avx", "avx2", "bmi1", "bmi2", "f16c", "fma", "lzcnt", "movbe", "xsave"],
];

// ---------- gathering facts ----------

/// What preflight knows about this machine (schema `wend.hardware/2`). Read from the hardware and /proc and /sys,
/// never from the distro's tools, so it reads the same on CentOS 5 as on trixie. The one exception is who owns a
/// Python 2 program (SPEC "Runtime preflight"): on rpm machines only `rpm` can read its database.
pub fn gather() -> Result<Value, String> {
    let runtimes = crate::runtime::gather_live()?;
    let mut uts: libc::utsname = unsafe { std::mem::zeroed() };
    // SAFETY: uname fills the struct it is given.
    let arch = if unsafe { libc::uname(&mut uts) } == 0 {
        // SAFETY: uname NUL-terminates machine.
        unsafe { std::ffi::CStr::from_ptr(uts.machine.as_ptr()) }
            .to_string_lossy()
            .into_owned()
    } else {
        String::new()
    };
    // the userland's word size, from the ELF class of the shell every box has
    let userland = match fs::read("/bin/sh").ok().and_then(|b| b.get(4).copied()) {
        Some(1) => json!(32),
        Some(2) => json!(64),
        _ => Value::Null,
    };
    // SAFETY: uname NUL-terminates release.
    let release = unsafe { std::ffi::CStr::from_ptr(uts.release.as_ptr()) }
        .to_string_lossy()
        .into_owned();
    let cpuinfo = fs::read_to_string("/proc/cpuinfo").unwrap_or_default();
    let model = cpuinfo
        .lines()
        .find_map(|l| {
            l.strip_prefix("model name")
                .map(|r| r.trim_start_matches([' ', '\t', ':']).to_string())
        })
        .unwrap_or_default();
    let (features, hypervisor) = cpu_features();
    let ram_mib = fs::read_to_string("/proc/meminfo")
        .ok()
        .and_then(|m| {
            m.lines()
                .find_map(|l| l.strip_prefix("MemTotal:"))
                .and_then(|v| v.trim().trim_end_matches("kB").trim().parse::<u64>().ok())
        })
        .map_or(Value::Null, |kb| json!(kb / 1024));
    Ok(json!({
        "schema": "wend.hardware/2",
        "arch": arch,
        "userland_bits": userland,
        "cpu": {"model": model, "features": features, "hypervisor": hypervisor},
        "boot": if Path::new("/sys/firmware/efi").exists() { "uefi" } else { "bios" },
        "ram_mib": ram_mib,
        "free_mib": free_mib("/").map_or(Value::Null, |m| json!(m)),
        "boot_dir": boot_dir(Path::new("/boot"), &release),
        "blacklisted": blacklisted(),
        "pci": pci_devices(Path::new("/sys/bus/pci/devices"), &module_files(&release)),
        "runtimes": runtimes,
    }))
}

/// What `/boot` holds: whether it is a filesystem of its own, its size and free space as a package install sees them
/// (reserved blocks left out), and each kernel in it with the space its files take (`vmlinuz`, its initramfs and
/// `System.map`, `config` and the rest named for its version; rescue and kdump images count as other files, since
/// they stay when the kernel's other files go).
fn boot_dir(dir: &Path, running: &str) -> Value {
    use std::os::unix::fs::MetadataExt;
    let dev = |p: &Path| fs::metadata(p).ok().map(|m| m.dev());
    let separate = matches!((dev(dir), dev(Path::new("/"))), (Some(a), Some(b)) if a != b);
    let (total, free) = statvfs_mib(&dir.to_string_lossy()).unwrap_or((0, 0));
    let mut files: Vec<(String, u64)> = vec![];
    if let Ok(rd) = fs::read_dir(dir) {
        for e in rd.flatten() {
            if let (Ok(m), Some(n)) = (e.metadata(), e.file_name().to_str()) {
                if m.is_file() {
                    files.push((n.to_string(), m.len()));
                }
            }
        }
    }
    json!({
        "separate": separate,
        "total_mib": total,
        "free_mib": free,
        "kernels": boot_kernels(&files, running),
    })
}

const KERNEL_FILE_PREFIXES: &[&str] = &[
    "vmlinuz-",
    ".vmlinuz-",
    "initrd-",
    "initramfs-",
    "initrd.img-",
    "System.map-",
    "config-",
    "symvers-",
    "abi-",
    "retpoline-",
];

/// The kernels among `/boot`'s files (name, bytes): one per `vmlinuz-<version>`, rescue images aside, each with the
/// MiB its files take, rounded up.
fn boot_kernels(files: &[(String, u64)], running: &str) -> Vec<Value> {
    let mut versions: Vec<&str> = files
        .iter()
        .filter_map(|(n, _)| n.strip_prefix("vmlinuz-"))
        .filter(|v| !v.is_empty() && !v.starts_with("0-rescue-") && !v.ends_with(".efi.signed"))
        .collect();
    versions.sort();
    let mut bytes = vec![0u64; versions.len()];
    for (n, size) in files {
        // kdump images stay when a kernel's other files go (EL's kernel_before leaves them): other files
        if n.contains("kdump") {
            continue;
        }
        let Some(rest) = KERNEL_FILE_PREFIXES.iter().find_map(|p| n.strip_prefix(p)) else {
            continue;
        };
        // the longest version the name is for: `initramfs-<v>.img`, `.vmlinuz-<v>.hmac`, `vmlinuz-<v>.efi.signed`
        let owner = versions
            .iter()
            .enumerate()
            .filter(|(_, v)| {
                rest.strip_prefix(**v)
                    .is_some_and(|t| t.is_empty() || t.starts_with('.'))
            })
            .max_by_key(|(_, v)| v.len());
        if let Some((i, _)) = owner {
            bytes[i] += size;
        }
    }
    versions
        .iter()
        .zip(bytes)
        .map(|(v, b)| json!({"version": v, "mib": b.div_ceil(1024 * 1024), "running": *v == running}))
        .collect()
}

/// Modules the machine keeps from loading by their aliases (`blacklist m`, or `install m /bin/false`): a release's
/// driver for a device doesn't count when it is one of these.
fn blacklisted() -> Vec<String> {
    let mut confs = vec![std::path::PathBuf::from("/etc/modprobe.conf")];
    for d in ["/etc/modprobe.d", "/lib/modprobe.d", "/usr/lib/modprobe.d"] {
        if let Ok(rd) = fs::read_dir(d) {
            let mut v: Vec<_> = rd.flatten().map(|e| e.path()).collect();
            v.sort();
            // every file, not only *.conf: EL5's and lenny's module-init-tools read `blacklist` and
            // `blacklist-compat` too (reading more only refuses more)
            confs.extend(v.into_iter().filter(|p| p.is_file()));
        }
    }
    let mut out = vec![];
    for c in confs {
        out.extend(blacklist_lines(&fs::read_to_string(c).unwrap_or_default()));
    }
    out.sort();
    out.dedup();
    out
}

fn blacklist_lines(text: &str) -> Vec<String> {
    text.lines()
        .filter_map(|l| {
            let w: Vec<&str> = l.split_whitespace().collect();
            match w.as_slice() {
                ["blacklist", m, ..] => Some(m.replace('-', "_")),
                ["install", m, cmd, ..]
                    if matches!(*cmd, "/bin/false" | "/bin/true" | "/usr/bin/false" | "/usr/bin/true") =>
                {
                    Some(m.replace('-', "_"))
                }
                _ => None,
            }
        })
        .collect()
}

/// Where each of the running kernel's modules lives under /lib/modules/<release> (modules.dep; EL5's names it by
/// its full path).
fn module_files(release: &str) -> std::collections::HashMap<String, String> {
    let base = format!("/lib/modules/{release}/");
    let mut out = std::collections::HashMap::new();
    for l in fs::read_to_string(format!("{base}modules.dep"))
        .unwrap_or_default()
        .lines()
    {
        let Some((path, _)) = l.split_once(':') else { continue };
        let rel = path.strip_prefix(&base).unwrap_or(path);
        let name = rel.rsplit('/').next().unwrap_or(rel);
        let Some(stem) = name.split(".ko").next().filter(|s| !s.is_empty()) else {
            continue;
        };
        out.entry(stem.replace('-', "_")).or_insert_with(|| rel.to_string());
    }
    out
}

/// A module from outside the kernel's own package: the kernel marks one built out of tree (taint `O`, since 3.2),
/// and kmod packages (ELRepo's `kmod-*`, under `extra/` or `weak-updates/`) and DKMS (`updates/dkms/`) install
/// theirs outside `kernel/`.
fn out_of_tree(module: &str, file: Option<&str>) -> bool {
    let taint = fs::read_to_string(format!("/sys/module/{module}/taint")).unwrap_or_default();
    taint.contains('O')
        || file.is_some_and(|f| ["extra/", "updates/", "weak-updates/"].iter().any(|p| f.starts_with(p)))
}

/// The level features the CPU itself reports (CPUID), not /proc/cpuinfo: an old kernel's cpuinfo doesn't name
/// features newer than itself (2.6.18 has no avx2), and the kernel the route boots will see them. XSAVE is the CPU's
/// bit, not OSXSAVE: the old kernel may not have turned it on, the new one will.
#[cfg(target_arch = "x86_64")]
fn cpu_features() -> (Vec<&'static str>, bool) {
    use std::arch::x86_64::{__cpuid, __cpuid_count};
    // leaves above the CPU's maximum are never read
    let (max, ext_max) = (__cpuid(0).eax, __cpuid(0x8000_0000).eax);
    let l1 = __cpuid(1);
    let l7 = if max >= 7 { __cpuid_count(7, 0).ebx } else { 0 };
    let e1 = if ext_max >= 0x8000_0001 {
        __cpuid(0x8000_0001).ecx
    } else {
        0
    };
    let bits: [(&str, u32, u32); 16] = [
        ("sse3", l1.ecx, 0),
        ("ssse3", l1.ecx, 9),
        ("fma", l1.ecx, 12),
        ("cx16", l1.ecx, 13),
        ("sse4_1", l1.ecx, 19),
        ("sse4_2", l1.ecx, 20),
        ("movbe", l1.ecx, 22),
        ("popcnt", l1.ecx, 23),
        ("xsave", l1.ecx, 26),
        ("avx", l1.ecx, 28),
        ("f16c", l1.ecx, 29),
        ("bmi1", l7, 3),
        ("avx2", l7, 5),
        ("bmi2", l7, 8),
        ("lahf_lm", e1, 0),
        ("lzcnt", e1, 5),
    ];
    let have = bits
        .iter()
        .filter(|(_, r, b)| r >> b & 1 == 1)
        .map(|(n, _, _)| *n)
        .collect();
    (have, l1.ecx >> 31 & 1 == 1)
}

#[cfg(not(target_arch = "x86_64"))]
fn cpu_features() -> (Vec<&'static str>, bool) {
    (vec![], false)
}

fn free_mib(path: &str) -> Option<u64> {
    statvfs_mib(path).map(|(_, free)| free)
}

/// A filesystem's size and free space as a package install sees them: root's reserved blocks left out of both.
fn statvfs_mib(path: &str) -> Option<(u64, u64)> {
    let c = std::ffi::CString::new(path).ok()?;
    let mut s: libc::statvfs = unsafe { std::mem::zeroed() };
    // SAFETY: statvfs fills the struct it is given.
    if unsafe { libc::statvfs(c.as_ptr(), &mut s) } != 0 {
        return None;
    }
    let (blocks, bfree, bavail, frsize) = (
        s.f_blocks as u64,
        s.f_bfree as u64,
        s.f_bavail as u64,
        s.f_frsize as u64,
    );
    let mib = |b: u64| b * frsize / (1024 * 1024);
    Some((mib(blocks.saturating_sub(bfree) + bavail), mib(bavail)))
}

/// Every PCI device: its slot, class, modalias (built from the ID files, which every kernel since 2.6 has, in the
/// form a kernel's module aliases match) and the driver bound to it now.
fn pci_devices(dir: &Path, files: &std::collections::HashMap<String, String>) -> Vec<Value> {
    let mut out = vec![];
    let Ok(rd) = fs::read_dir(dir) else { return out };
    let mut slots: Vec<_> = rd.flatten().map(|e| e.path()).collect();
    slots.sort();
    for d in slots {
        let hex = |f: &str| {
            fs::read_to_string(d.join(f))
                .ok()
                .and_then(|s| u32::from_str_radix(s.trim().trim_start_matches("0x"), 16).ok())
        };
        let (Some(v), Some(dev), Some(class)) = (hex("vendor"), hex("device"), hex("class")) else {
            continue;
        };
        let (sv, sd) = (
            hex("subsystem_vendor").unwrap_or(0),
            hex("subsystem_device").unwrap_or(0),
        );
        let link = |p: &str| {
            fs::read_link(d.join(p))
                .ok()
                .and_then(|p| p.file_name().map(|n| n.to_string_lossy().into_owned()))
        };
        // a driver's own name isn't always its module's (lenny's ide_pci_generic registers as PCI_IDE)
        let (driver, module) = (link("driver"), link("driver/module"));
        let file = module.as_ref().and_then(|m| files.get(&m.replace('-', "_")).cloned());
        let oot = module.as_ref().is_some_and(|m| out_of_tree(m, file.as_deref()));
        out.push(json!({
            "slot": d.file_name().unwrap().to_string_lossy(),
            "class": format!("0x{class:06x}"),
            "modalias": format!(
                "pci:v{v:08X}d{dev:08X}sv{sv:08X}sd{sd:08X}bc{:02X}sc{:02X}i{:02X}",
                class >> 16,
                class >> 8 & 0xff,
                class & 0xff
            ),
            "driver": driver,
            "module": module,
            "module_file": file,
            "out_of_tree": oot,
        }));
    }
    out
}

// ---------- judging them ----------

/// The x86-64 level these features make (1 to 3).
pub fn cpu_level(features: &[String]) -> usize {
    let has = |l: &[&str]| l.iter().all(|f| features.iter().any(|x| x == f));
    1 + LEVEL_FEATURES.iter().take_while(|l| has(l)).count()
}

/// A kernel module alias pattern (fnmatch: `*`, `?`, `[...]`) against a modalias.
pub fn alias_match(pat: &[u8], s: &[u8]) -> bool {
    match pat.first() {
        None => s.is_empty(),
        Some(b'*') => (0..=s.len()).any(|i| alias_match(&pat[1..], &s[i..])),
        Some(b'?') => !s.is_empty() && alias_match(&pat[1..], &s[1..]),
        Some(b'[') => {
            let Some(end) = pat.iter().skip(2).position(|&c| c == b']').map(|p| p + 2) else {
                return !s.is_empty() && s[0] == b'[' && alias_match(&pat[1..], &s[1..]);
            };
            let Some(&c) = s.first() else { return false };
            let set = &pat[1..end];
            let (neg, set) = match set.first() {
                Some(b'!' | b'^') => (true, &set[1..]),
                _ => (false, set),
            };
            let mut hit = false;
            let mut i = 0;
            while i < set.len() {
                if i + 2 < set.len() && set[i + 1] == b'-' {
                    hit |= set[i] <= c && c <= set[i + 2];
                    i += 3;
                } else {
                    hit |= set[i] == c;
                    i += 1;
                }
            }
            hit != neg && alias_match(&pat[end + 1..], &s[1..])
        }
        Some(&p) => s.first() == Some(&p) && alias_match(&pat[1..], &s[1..]),
    }
}

struct Table {
    kernel: String,
    aliases: Vec<(String, String)>,
    builtin: Vec<String>,
}

fn parse_table(text: &str) -> Table {
    let mut t = Table {
        kernel: String::new(),
        aliases: vec![],
        builtin: vec![],
    };
    for l in text.lines() {
        if let Some(h) = l.strip_prefix("# ") {
            if t.kernel.is_empty() {
                if let Some(k) = h.split("kernel ").nth(1) {
                    t.kernel = k.split_whitespace().next().unwrap_or("").to_string();
                }
            }
            continue;
        }
        match l.split_once(' ') {
            Some(("builtin", m)) => t.builtin.push(m.to_string()),
            Some((m, a)) => t.aliases.push((m.to_string(), a.to_string())),
            None => {}
        }
    }
    t
}

/// The devices a machine needs to boot and stay reachable, by PCI class: mass storage, Fibre Channel HBAs (a
/// SAN-booted root) and I2O controllers hold disks; network cards and InfiniBand carry the network.
fn kind_of(class: &str) -> Option<&'static str> {
    match (class.get(2..4), class.get(4..6)) {
        (Some("01"), _) | (Some("0c"), Some("04")) | (Some("0e"), _) => Some("disk controller"),
        (Some("02"), _) | (Some("0c"), Some("06")) => Some("network card"),
        _ => None,
    }
}

/// Modules whose class-wide alias claims every IDE controller but whose probe declines one it doesn't know
/// (ata_generic and ide_pci_generic without their all-generic options, pata_acpi without ACPI timing methods): they
/// count as a driver only for a device they already drive.
const CLASS_ONLY_MAYBE: &[&str] = &["ata_generic", "ide_pci_generic", "pata_acpi"];

/// Facts from a file are held to the shape `gather` writes: a check whose input is missing never passes by default.
pub fn check_facts(f: &Value) -> Result<(), String> {
    let bad = |what: &str| Err(format!("not a wend.hardware/2 facts file: {what}"));
    if f["schema"] != "wend.hardware/2" {
        return bad("schema (gather the facts again with this client: wend facts)");
    }
    if !crate::runtime::check(&f["runtimes"]) {
        return bad("runtimes");
    }
    let b = &f["boot_dir"];
    if !(b["separate"].is_boolean() && b["total_mib"].is_u64() && b["free_mib"].is_u64())
        || !b["kernels"].as_array().is_some_and(|a| {
            a.iter()
                .all(|k| k["version"].is_string() && k["mib"].is_u64() && k["running"].is_boolean())
        })
    {
        return bad("boot_dir");
    }
    if !f["blacklisted"]
        .as_array()
        .is_some_and(|a| a.iter().all(Value::is_string))
    {
        return bad("blacklisted");
    }
    for k in ["arch", "boot"] {
        if !f[k].is_string() {
            return bad(k);
        }
    }
    for k in ["ram_mib", "free_mib"] {
        if !f[k].is_u64() {
            return bad(k);
        }
    }
    if f["userland_bits"] != json!(32) && f["userland_bits"] != json!(64) {
        return bad("userland_bits");
    }
    if !f["cpu"]["features"]
        .as_array()
        .is_some_and(|a| a.iter().all(Value::is_string))
    {
        return bad("cpu.features");
    }
    let dev_ok = |d: &Value| {
        d["slot"].is_string()
            && d["class"]
                .as_str()
                .is_some_and(|c| c.len() == 8 && c.starts_with("0x") && c[2..].bytes().all(|b| b.is_ascii_hexdigit()))
            && d["modalias"].as_str().is_some_and(|m| {
                regex::Regex::new(
                    "^pci:v[0-9A-F]{8}d[0-9A-F]{8}sv[0-9A-F]{8}sd[0-9A-F]{8}bc[0-9A-F]{2}sc[0-9A-F]{2}i[0-9A-F]{2}$",
                )
                .unwrap()
                .is_match(m)
            })
            && (d["driver"].is_string() || d["driver"].is_null())
            && (d["module"].is_string() || d["module"].is_null())
            && d.get("module_file").is_some_and(|m| m.is_string() || m.is_null())
            && d["out_of_tree"].is_boolean()
    };
    if !f["pci"].as_array().is_some_and(|a| a.iter().all(dev_ok)) {
        return bad("pci");
    }
    Ok(())
}

/// Where an out-of-tree module came from, by where it is installed.
fn source_of(file: Option<&str>) -> String {
    match file {
        Some(f) if f.contains("/dkms/") || f.starts_with("updates/dkms") => "DKMS".into(),
        Some(f) if f.starts_with("extra/") || f.starts_with("weak-updates/") => {
            format!("a kmod package such as ELRepo's ({f})")
        }
        Some(f) => format!("outside the kernel package ({f})"),
        None => "outside the kernel package".into(),
    }
}

fn vm_hint(level: usize) -> String {
    format!(
        "This machine is a VM: give it a CPU model with x86-64-v{level} (CPU passthrough, \"host\"; Proxmox: \
         x86-64-v{level}{}; QEMU/libvirt: {}; VMware: an EVC mode of {} or later), and check again.",
        if level == 2 { "-AES" } else { "" },
        if level == 2 {
            "Nehalem or newer"
        } else {
            "Haswell-noTSX or newer"
        },
        if level == 2 { "Nehalem" } else { "Haswell" },
    )
}

/// The verdict for one route on one machine's facts: every hop's problems (things that would leave the machine
/// unbootable or unreachable), its notes, and the furthest release it can land on.
pub fn evaluate(atlas: &Atlas, route: &Doc, facts: &Value) -> Result<Value, String> {
    check_facts(facts)?;
    let fam = str_of(route, "family")?;
    let a = atlas
        .adapters
        .get(fam)
        .ok_or(format!("no adapter for family {fam:?}"))?;
    let hw = get(a, "hardware").ok_or(format!("adapters/{fam}.toml has no hardware table"))?;
    let from = str_of(route, "from")?;
    let hops: Vec<String> = get(route, "hops")
        .and_then(crate::atlas::strings)
        .ok_or("the route has no hops list")?;
    let booted = booted_releases(a);

    let mut machine = vec![];
    let want_arch = match str_of(route, "arch")? {
        "amd64" | "x86_64" => "x86_64",
        other => other,
    };
    let arch = facts["arch"].as_str().unwrap_or("");
    if arch != want_arch {
        machine.push(format!(
            "this machine runs a {arch} kernel and the route is for {want_arch}. wend has no route that changes \
             a machine's architecture."
        ));
    } else if facts["userland_bits"] != json!(64) {
        machine.push("this machine runs a 32-bit userland on a 64-bit kernel; the route is for a 64-bit one".into());
    }
    let boot = facts["boot"].as_str().unwrap_or("");
    let proven: Vec<String> = get(hw, "boot").and_then(crate::atlas::strings).unwrap_or_default();
    if !proven.iter().any(|b| b == boot) {
        machine.push(format!(
            "this machine boots by {}; the {fam} routes are proven on {} machines only, and wend doesn't walk a \
             machine through boot steps it hasn't proven",
            boot.to_uppercase(),
            proven
                .iter()
                .map(|b| b.to_uppercase())
                .collect::<Vec<_>>()
                .join(" and ")
        ));
    }

    let features: Vec<String> = crate::atlas::strings(&facts["cpu"]["features"]).unwrap_or_default();
    let level = cpu_level(&features);
    let vm = facts["cpu"]["hypervisor"] == json!(true);
    let mut out_hops = vec![];
    let mut lands = if machine.is_empty() {
        Some(from.to_string())
    } else {
        None
    };
    let mut blocked = !machine.is_empty();

    // /boot, which a hop fills with its kernel beside the ones it keeps (`hardware.old_kernels`)
    let keep = get(hw, "old_kernels")
        .and_then(Value::as_str)
        .ok_or(format!("adapters/{fam}.toml has no hardware.old_kernels"))?;
    let bd = &facts["boot_dir"];
    let separate = bd["separate"] == json!(true);
    let (total, free) = (
        bd["total_mib"].as_u64().unwrap_or(0),
        bd["free_mib"].as_u64().unwrap_or(0),
    );
    let box_all: Vec<(String, u64)> = bd["kernels"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|k| {
            (
                format!("its kernel {}", k["version"].as_str().unwrap_or("?")),
                k["mib"].as_u64().unwrap_or(0),
            )
        })
        .collect();
    let mut box_running: Vec<(String, u64)> = bd["kernels"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|k| k["running"] == json!(true))
        .map(|k| {
            (
                format!("the running kernel {}", k["version"].as_str().unwrap_or("?")),
                k["mib"].as_u64().unwrap_or(0),
            )
        })
        .collect();
    let running_found = !box_running.is_empty();
    if !running_found {
        // the running kernel isn't in /boot: every kernel there may be the one GRUB boots
        box_running = box_all.clone();
    }
    let other = (total.saturating_sub(free)).saturating_sub(box_all.iter().map(|k| k.1).sum());
    // EL's kernel_before drops only kernel, kernel-core and kernel-modules* records: a kernel another package owns
    // (EL5's kernel-xen, kernel-PAE, kernel-debug; ELRepo's kernel-lt and kernel-ml; Oracle's UEK) keeps its files
    // on every hop
    // (the running one too, once the machine has moved off it)
    let owned: Vec<(String, u64, bool)> = bd["kernels"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|k| {
            let v = k["version"].as_str().unwrap_or("");
            let flavour = ["xen", "PAE", "debug"].iter().any(|f| v.ends_with(f))
                || ["elrepo", "uek"].iter().any(|f| v.contains(f));
            flavour.then(|| {
                (
                    format!("its kernel {v}"),
                    k["mib"].as_u64().unwrap_or(0),
                    k["running"] == json!(true),
                )
            })
        })
        .collect();
    let other_owned: Vec<(String, u64)> = owned.iter().map(|o| (o.0.clone(), o.1)).collect();
    let idle_owned: Vec<(String, u64)> = owned.iter().filter(|o| !o.2).map(|o| (o.0.clone(), o.1)).collect();
    // apt before 1.0 (Debian to wheezy) never autoremoves a kernel (`APT::NeverAutoRemove "^linux-image.*"`): up to
    // and including the hop to `autoremove_from`, every kernel stays
    let ladder: Vec<String> = get(a, "ladder").and_then(crate::atlas::strings).unwrap_or_default();
    let pos = |r: &str| ladder.iter().position(|x| x == r);
    let keep_all_to = match get(hw, "autoremove_from") {
        None => None,
        Some(r) => Some(r.as_str().and_then(pos).ok_or(format!(
            "adapters/{fam}.toml: hardware.autoremove_from isn't on the ladder"
        ))?),
    };
    let mut route_kernels: Vec<(String, u64)> = vec![];
    let blacklist: Vec<String> = crate::atlas::strings(&facts["blacklisted"]).unwrap_or_default();
    // SPEC "Runtime preflight": the machine's own Python 2 programs, and the last release that still ships Python 2
    let own: Vec<Value> = facts["runtimes"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|u| crate::runtime::is_own(u))
        .cloned()
        .collect();
    let mut keeps = Some(from.to_string());
    let mut runtime_broken = false;
    let mut noted: Vec<String> = vec![];

    for h in &hops {
        let mut problems: Vec<String> = vec![];
        let mut notes: Vec<String> = vec![];
        // a release the route only passes through (may_skip) installs no kernel and is never booted, even
        // when its adapter has a table for it
        if !booted.contains(h) {
            out_hops.push(json!({"to": h, "ok": true, "problems": [], "runtime_problems": [],
                                 "notes": ["not booted on this route"]}));
            continue;
        }
        let Some(t) = get(hw, h) else {
            return Err(format!("adapters/{fam}.toml has no hardware.{h} table"));
        };
        let need = get(t, "cpu").and_then(Value::as_str).unwrap_or("x86-64");
        let need_level = CPU_LEVELS.iter().position(|c| *c == need).unwrap_or(0) + 1;
        if level < need_level {
            let missing: Vec<&str> = LEVEL_FEATURES[..need_level - 1]
                .iter()
                .flat_map(|l| l.iter())
                .filter(|f| !features.iter().any(|x| x == *f))
                .copied()
                .collect();
            let mut p = format!(
                "{h} needs an {need} CPU; this one is x86-64{} (it lacks {})",
                if level > 1 { format!("-v{level}") } else { String::new() },
                missing.join(", ")
            );
            if let Some(n) = get(t, "note").and_then(Value::as_str) {
                p += &format!(". {n}");
            }
            if vm {
                p += &format!(". {}", vm_hint(need_level));
            }
            problems.push(p);
        }
        if let (Some(need), Some(have)) = (get(t, "ram_mib").and_then(Value::as_u64), facts["ram_mib"].as_u64()) {
            // MemTotal is the RAM less what the kernel keeps for itself (a 2 GiB VM shows about 1987 MiB)
            if have * 10 < need * 9 {
                problems.push(format!("{h} needs {need} MiB of memory; this machine has {have}"));
            }
        }
        if let (Some(need), Some(have)) = (get(t, "disk_mib").and_then(Value::as_u64), facts["free_mib"].as_u64()) {
            if have < need {
                problems.push(format!(
                    "the hop to {h} needs about {need} MiB free on /; there are {have}"
                ));
            }
        }
        let need_boot = get(t, "boot_mib")
            .and_then(Value::as_u64)
            .ok_or(format!("adapters/{fam}.toml has no hardware.{h}.boot_mib"))?;
        let k = route_kernels.len();
        let keeps_all = keep == "autoremove" && keep_all_to.is_some_and(|to| pos(h).is_some_and(|i| i <= to));
        let kept: Vec<(String, u64)> = match (keep, k) {
            _ if keeps_all => [box_all.clone(), route_kernels.clone()].concat(),
            // the running kernel is kept on the first hop anyway: count it once
            ("running", 0) if running_found => [box_running.clone(), idle_owned.clone()].concat(),
            ("running", 0) => box_running.clone(),
            ("running", _) => [other_owned.clone(), route_kernels[k - 1..].to_vec()].concat(),
            ("autoremove", 0) => box_all.clone(),
            ("autoremove", 1) => [box_running.clone(), route_kernels.clone()].concat(),
            ("autoremove", _) => route_kernels[k - 2..].to_vec(),
            _ => return Err(format!("adapters/{fam}.toml: unknown hardware.old_kernels {keep:?}")),
        };
        let kept_mib: u64 = kept.iter().map(|k| k.1).sum();
        let room = total.saturating_sub(other + kept_mib);
        if separate && room < need_boot {
            let beside = if kept.is_empty() {
                "nothing".to_string()
            } else {
                kept.iter()
                    .map(|(n, m)| format!("{n} ({m} MiB)"))
                    .collect::<Vec<_>>()
                    .join(" and ")
            };
            let fix = if k == 0 && keep == "autoremove" && box_all.len() > 1 {
                "remove the kernels this machine doesn't run, or grow /boot"
            } else {
                "grow /boot"
            };
            problems.push(format!(
                "the hop to {h} puts its kernel (about {need_boot} MiB) in /boot beside {beside}; /boot holds \
                 {total} MiB with {other} MiB of other files, leaving {room}: {fix}"
            ));
        }
        route_kernels.push((format!("{h}'s kernel"), need_boot));

        let want = get(t, "drivers").and_then(Value::as_str).unwrap_or("");
        let bytes = atlas
            .drivers
            .get(&format!("{fam}/{h}"))
            .ok_or(format!("adapters/drivers/{fam}/{h}.txt is missing"))?;
        if format!("sha256:{}", crate::canon::sha256_hex(bytes)) != want {
            return Err(format!(
                "adapters/drivers/{fam}/{h}.txt isn't the table adapters/{fam}.toml pins"
            ));
        }
        let table = parse_table(&String::from_utf8_lossy(bytes));
        for d in facts["pci"].as_array().into_iter().flatten() {
            let Some(kind) = kind_of(&d["class"].as_str().unwrap_or("").to_ascii_lowercase()) else {
                continue;
            };
            let alias = d["modalias"].as_str().unwrap_or("");
            let driver = d["driver"].as_str();
            let norm = d["module"].as_str().or(driver).map(|d| d.replace('-', "_"));
            let matches = |m: &String, p: &String| {
                alias_match(p.as_bytes(), alias.as_bytes())
                    && (!CLASS_ONLY_MAYBE.contains(&m.as_str()) || norm.as_deref() == Some(m.as_str()))
            };
            // a module the machine blacklists won't load for the device (kmod-r8168 blacklists r8169), unless the
            // machine loads it some other way and it drives the device now
            let barred_m = |m: &String| blacklist.contains(m) && norm.as_deref() != Some(m.as_str());
            let hit = table.aliases.iter().find(|(m, p)| matches(m, p) && !barred_m(m));
            let barred = table
                .aliases
                .iter()
                .find(|(m, p)| matches(m, p) && barred_m(m))
                .map(|(m, _)| m.clone());
            let oot = d["out_of_tree"] == json!(true);
            // by name only when the kernel recorded no IDs for that built-in driver (older kernels record
            // none; EL9's built-in virtio_pci has none)
            let built_in = norm
                .as_ref()
                .is_some_and(|dr| table.builtin.contains(dr) && !table.aliases.iter().any(|(m, _)| m == dr));
            let ids = alias.get(4..23).unwrap_or(alias);
            let slot = d["slot"].as_str().unwrap_or("?");
            let module = norm.clone().unwrap_or_default();
            let origin = || source_of(d["module_file"].as_str());
            if let Some((m, _)) = hit {
                if oot && driver.is_some() {
                    notes.push(format!(
                        "{module}, from {}, drives the {kind} at {slot} now and won't follow to {h}'s kernel; \
                         that kernel's own {m} takes the device over",
                        origin()
                    ));
                }
                continue;
            }
            if built_in {
                continue;
            }
            match driver {
                Some(dr) => problems.push(match (&barred, oot) {
                    (Some(m), _) => format!(
                        "{h}'s kernel drives the {kind} at {slot} ({ids}) with {m}, which this machine blacklists \
                         (/etc/modprobe.d){}: it would come up without it",
                        if oot {
                            format!(", and {module}, from {}, which drives it now, won't follow", origin())
                        } else {
                            String::new()
                        }
                    ),
                    (None, true) => format!(
                        "{h}'s kernel {} has no driver for the {kind} at {slot} ({ids}). {module} drives it now, \
                         but it comes from {}, outside the kernel, and won't follow to {h}'s kernel: it would \
                         come up without it",
                        table.kernel,
                        origin()
                    ),
                    (None, false) => format!(
                        "{h}'s kernel {} has no driver for the {kind} at {slot} ({ids}), which {dr} drives now: \
                         it would come up without it",
                        table.kernel
                    ),
                }),
                None => notes.push(format!(
                    "{h}'s kernel has no driver for the unused {kind} at {slot} ({ids})"
                )),
            }
        }
        let mut app: Vec<String> = vec![];
        if !own.is_empty() {
            let ships: Vec<String> = get(a, "runtimes")
                .and_then(|r| get(r, h))
                .and_then(|t| get(t, "python2"))
                .and_then(crate::atlas::strings)
                .ok_or(format!("adapters/{fam}.toml has no runtimes.{h}.python2"))?;
            if ships.is_empty() {
                app.push(format!(
                    "{h} ships no Python 2, and this machine's own programs run on it: {}",
                    own.iter().map(crate::runtime::describe).collect::<Vec<_>>().join("; ")
                ));
            } else {
                for u in &own {
                    let v = u["version"].as_str().unwrap_or("");
                    let prog = u["program"].as_str().unwrap_or("?").to_string();
                    if !ships.iter().any(|s| s == v || s.starts_with(&format!("{v}."))) && !noted.contains(&prog) {
                        notes.push(format!(
                            "{prog} runs on Python {v}; {h} ships Python {} instead. Python 2 code mostly runs on a \
                             later 2.x, but check it does",
                            ships.join(" and ")
                        ));
                        noted.push(prog);
                    }
                }
            }
        }
        if app.is_empty() && !runtime_broken {
            keeps = Some(h.clone());
        } else {
            runtime_broken = true;
        }
        let ok = problems.is_empty() && !blocked;
        if ok {
            lands = Some(h.clone());
        } else {
            blocked = true;
        }
        out_hops.push(
            json!({"to": h, "ok": problems.is_empty() && app.is_empty(), "problems": problems, "runtime_problems": app,
                             "notes": notes}),
        );
    }
    let to = str_of(route, "to")?;
    Ok(json!({
        "schema": "wend.preflight/0",
        "family": fam,
        "from": from,
        "to": to,
        "cpu_level": level,
        "machine": machine,
        "hops": out_hops,
        "lands_on": lands,
        "runtimes": own,
        "keeps_runtimes_on": if own.is_empty() { Value::Null } else { json!(keeps) },
        "ok": lands.as_deref() == Some(to) && !runtime_broken,
    }))
}

/// How a verdict reads to the machine's owner.
pub fn report(v: &Value) -> String {
    let mut s = String::new();
    for p in v["machine"].as_array().into_iter().flatten() {
        s += &format!("REFUSED {}\n", p.as_str().unwrap_or(""));
    }
    for h in v["hops"].as_array().into_iter().flatten() {
        let to = h["to"].as_str().unwrap_or("?");
        let probs = h["problems"].as_array().cloned().unwrap_or_default();
        let app = h["runtime_problems"].as_array().cloned().unwrap_or_default();
        s += &format!(
            "{} {to}\n",
            if probs.is_empty() && app.is_empty() {
                "ok     "
            } else {
                "NO     "
            }
        );
        for p in probs.iter().chain(&app) {
            s += &format!("        {}\n", p.as_str().unwrap_or(""));
        }
        for n in h["notes"].as_array().into_iter().flatten() {
            s += &format!("        note: {}\n", n.as_str().unwrap_or(""));
        }
    }
    let (from, to) = (v["from"].as_str().unwrap_or(""), v["to"].as_str().unwrap_or("?"));
    let lands = v["lands_on"].as_str();
    let keeps = v["keeps_runtimes_on"].as_str();
    let order: Vec<&str> = std::iter::once(from)
        .chain(
            v["hops"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(|h| h["to"].as_str()),
        )
        .collect();
    let at = |r: &str| order.iter().position(|x| *x == r);
    if let (Some(l), Some(k)) = (lands, keeps) {
        // only when Python 2 goes before the hardware stops does the owner have a choice to make
        if at(k) < at(l) {
            // the runtime goes before the hardware stops: the owner picks
            let stop = if k == from {
                format!("stay on {from}: the route's first hop already leaves Python 2 behind")
            } else {
                format!("walk the route only to {k}, the last release that still ships Python 2")
            };
            let landing = if l == to {
                to.to_string()
            } else {
                format!("{l}, the furthest this hardware goes")
            };
            s += &format!(
                "This machine's own programs run on Python 2, and the route leaves it behind after {k}:\n{}\
                 Pick one:\n  stop:    {stop}\n  shelter: upgrade to {landing}, and run those programs in a sealed \
                 Python 2 userland of their own (SPEC \"Sheltering an app\"; not built yet)\n\
                 Or port them to Python 3 and check again. Nothing was touched.\n",
                v["runtimes"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .map(|u| format!("  {}\n", crate::runtime::describe(u)))
                    .collect::<String>()
            );
            return s;
        }
    }
    s += &match (v["ok"].as_bool(), lands) {
        (Some(true), _) => format!("This machine can land on {to}.\n"),
        (_, Some(l)) if l == from => {
            format!("This machine can't take the first hop. Nothing was touched; it stays on {l}.\n")
        }
        (_, Some(l)) => format!(
            "This machine can't reach {to}. The furthest it can land on is {l}: walk the route only that far.\n"
        ),
        _ => "This machine can't take this route. Nothing was touched.\n".into(),
    };
    s
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn aliases_match_like_the_kernel() {
        let m = |p: &str, s: &str| alias_match(p.as_bytes(), s.as_bytes());
        let ahci_sata = "pci:v00008086d00002922sv00001AF4sd00001100bc01sc06i01";
        assert!(m("pci:v*d*sv*sd*bc01sc06i01*", ahci_sata));
        assert!(m("pci:v00008086d00002922sv*sd*bc*sc*i*", ahci_sata));
        assert!(!m("pci:v00008086d00002923sv*sd*bc*sc*i*", ahci_sata));
        assert!(m("pci:v0000100[0-9]d*", "pci:v00001000d0060"));
        assert!(!m("pci:v0000100[!0-9]d*", "pci:v00001000d0060"));
        assert!(m("a?c", "abc") && !m("a?c", "ac"));
    }

    #[test]
    fn levels_need_every_feature() {
        let f = |l: &[&str]| l.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        assert_eq!(cpu_level(&f(&[])), 1);
        // Core 2 (Penryn): SSE4.1 but no SSE4.2 or POPCNT
        assert_eq!(cpu_level(&f(&["cx16", "lahf_lm", "sse3", "sse4_1", "ssse3"])), 1);
        assert_eq!(cpu_level(&f(LEVEL_FEATURES[0])), 2);
        // Sandy Bridge: AVX but no AVX2
        let mut snb = f(LEVEL_FEATURES[0]);
        snb.extend(f(&["avx", "xsave"]));
        assert_eq!(cpu_level(&snb), 2);
        let mut hsw = f(LEVEL_FEATURES[0]);
        hsw.extend(f(LEVEL_FEATURES[1]));
        assert_eq!(cpu_level(&hsw), 3);
    }

    fn atlas() -> Atlas {
        Atlas::load(&Path::new(env!("CARGO_MANIFEST_DIR")).join("..")).unwrap()
    }

    fn dev(slot: &str, class: &str, alias: &str, driver: Option<&str>) -> Value {
        json!({"slot": slot, "class": class, "modalias": alias, "driver": driver, "module_file": null,
               "out_of_tree": false})
    }

    fn machine(features: &[&str], vm: bool, pci: Vec<Value>) -> Value {
        json!({"schema": "wend.hardware/2", "arch": "x86_64", "userland_bits": 64,
               "cpu": {"model": "test", "features": features, "hypervisor": vm}, "boot": "bios",
               "ram_mib": 4096, "free_mib": 20000,
               "boot_dir": {"separate": false, "total_mib": 20000, "free_mib": 20000, "kernels": []},
               "blacklisted": [], "pci": pci, "runtimes": []})
    }

    /// A separate /boot of `total` MiB holding `kernels` (version, MiB; the last one running) and 4 MiB of other files.
    fn with_boot(mut m: Value, total: u64, kernels: &[(&str, u64)]) -> Value {
        let used: u64 = kernels.iter().map(|k| k.1).sum::<u64>() + 4;
        let ks: Vec<Value> = kernels
            .iter()
            .enumerate()
            .map(|(i, (v, mib))| json!({"version": v, "mib": mib, "running": i + 1 == kernels.len()}))
            .collect();
        m["boot_dir"] = json!({"separate": true, "total_mib": total, "free_mib": total - used, "kernels": ks});
        m
    }

    fn verdict(route: &str, facts: &Value) -> Value {
        let a = atlas();
        let (_, r) = a.route(route).unwrap();
        evaluate(&a, r, facts).unwrap()
    }

    const V1_PENRYN: &[&str] = &["sse3", "ssse3", "cx16", "sse4_1", "lahf_lm"];
    const QEMU64: &[&str] = &["sse3", "cx16", "lahf_lm", "lzcnt"];

    /// A 2008 PowerEdge 2950: Xeon 5400 (SSE4.1, no SSE4.2 or POPCNT), PERC 6/i, Broadcom NetXtreme II. AlmaLinux 9's
    /// kernel still drives the PERC (RHEL 9's dropped it), but its glibc can't run on the CPU: it lands on CentOS 7.
    #[test]
    fn a_core2_server_stops_at_el7() {
        let v = verdict(
            "centos5-to-alma10",
            &machine(
                V1_PENRYN,
                false,
                vec![
                    dev(
                        "0000:01:00.0",
                        "0x010400",
                        "pci:v00001000d00000060sv00001028sd00001F0Cbc01sc04i00",
                        Some("megaraid_sas"),
                    ),
                    dev(
                        "0000:03:00.0",
                        "0x020000",
                        "pci:v000014E4d0000164Csv00001028sd000001B2bc02sc00i00",
                        Some("bnx2"),
                    ),
                ],
            ),
        );
        assert_eq!(v["lands_on"], "centos7", "{v:#}");
        assert_eq!(v["ok"], false);
        let p = v["hops"][2]["problems"].to_string();
        assert!(
            p.contains("x86-64-v2") && p.contains("popcnt") && !p.contains("VM"),
            "{p}"
        );
        assert_eq!(
            v["hops"][2]["problems"].as_array().unwrap().len(),
            1,
            "the PERC is still driven: {v:#}"
        );
    }

    /// The same disk on QEMU's default CPU: the problem is the VM's CPU model, and the report says how to raise it.
    #[test]
    fn a_vm_on_qemu64_is_told_to_raise_its_cpu_model() {
        let virtio = vec![
            dev(
                "0000:00:04.0",
                "0x010000",
                "pci:v00001AF4d00001001sv00001AF4sd00000002bc01sc00i00",
                Some("virtio-pci"),
            ),
            dev(
                "0000:00:03.0",
                "0x020000",
                "pci:v00001AF4d00001000sv00001AF4sd00000001bc02sc00i00",
                Some("virtio-pci"),
            ),
        ];
        let v = verdict("centos5-to-alma10", &machine(QEMU64, true, virtio.clone()));
        assert_eq!(v["lands_on"], "centos7");
        assert!(v["hops"][2]["problems"][0]
            .as_str()
            .unwrap()
            .contains("VM: give it a CPU model with x86-64-v2"));
        let mut snb: Vec<&str> = LEVEL_FEATURES[0].to_vec();
        snb.extend(["avx", "xsave"]);
        let v = verdict("centos5-to-alma10", &machine(&snb, true, virtio));
        assert_eq!(v["lands_on"], "alma9");
        let p = v["hops"][3]["problems"][0].as_str().unwrap();
        assert!(p.contains("x86_64_v2 build") && p.contains("x86-64-v3"), "{p}");
    }

    /// QEMU's LSI 53C895A, the disk tools/vm.py gives etch: every Debian kernel drives it, RHEL 7 dropped sym53c8xx.
    #[test]
    fn a_dropped_disk_driver_stops_the_route_before_it() {
        let lsi = dev(
            "0000:00:05.0",
            "0x010000",
            "pci:v00001000d00000012sv00001AF4sd00001100bc01sc00i00",
            Some("sym53c8xx"),
        );
        let full: Vec<&str> = LEVEL_FEATURES.concat();
        let v = verdict("etch-to-trixie", &machine(&full, true, vec![lsi.clone()]));
        assert_eq!(v["ok"], true, "{v:#}");
        let v = verdict("centos5-to-alma10", &machine(&full, true, vec![lsi.clone()]));
        assert_eq!(v["lands_on"], "centos6", "{v:#}");
        assert!(v["hops"][1]["problems"][0]
            .as_str()
            .unwrap()
            .contains("which sym53c8xx drives now"));
        // the same card unused is a note, not a refusal
        let mut idle = lsi;
        idle["driver"] = Value::Null;
        let v = verdict("centos5-to-alma10", &machine(&full, true, vec![idle]));
        assert_eq!(v["ok"], true);
        assert!(v["hops"][1]["notes"][0]
            .as_str()
            .unwrap()
            .contains("unused disk controller"));
    }

    /// A SAN-booted EL7 server: Emulex LPe16000 (class 0x0c04), dropped from EL9's lpfc. A 2 GiB VM still counts
    /// as 2 GiB, and a facts file missing what a check reads is refused, never judged ok.
    #[test]
    fn fibre_channel_ram_slack_and_bad_facts() {
        let full: Vec<&str> = LEVEL_FEATURES.concat();
        let fc = dev(
            "0000:05:00.0",
            "0x0c0400",
            "pci:v000010DFd0000FE05sv000010DFsd0000FE05bc0Csc04i00",
            Some("lpfc"),
        );
        let v = verdict("centos5-to-alma10", &machine(&full, false, vec![fc]));
        assert_eq!(v["lands_on"], "centos7", "{v:#}");
        let mut m = machine(&full, true, vec![]);
        m["ram_mib"] = json!(1987);
        assert_eq!(verdict("centos5-to-alma10", &m)["ok"], true);
        let a = atlas();
        let (_, r) = a.route("centos5-to-alma10").unwrap();
        let mut bad_class = machine(&full, false, vec![]);
        bad_class["pci"] = json!([dev(
            "0000:00:01.0",
            "0xGGGGGG",
            "pci:v00008086d00001234sv00000000sd00000000bc01sc00i00",
            Some("x")
        )]);
        assert!(evaluate(&a, r, &bad_class).is_err());
        let mut bad_alias = machine(&full, false, vec![]);
        bad_alias["pci"] = json!([dev("0000:00:01.0", "0x010000", "pci:v8086", Some("x"))]);
        assert!(evaluate(&a, r, &bad_alias).is_err());
        let mut odd = machine(&full, false, vec![]);
        odd["userland_bits"] = json!(33);
        assert!(evaluate(&a, r, &odd).is_err());
        for k in [
            "pci",
            "ram_mib",
            "free_mib",
            "schema",
            "userland_bits",
            "boot_dir",
            "blacklisted",
        ] {
            let mut bad = machine(&full, false, vec![]);
            bad.as_object_mut().unwrap().remove(k);
            assert!(evaluate(&a, r, &bad).is_err(), "{k}");
        }
    }

    /// ata_generic's class-wide alias doesn't count for an IDE controller another driver drives now.
    #[test]
    fn a_generic_ide_alias_is_not_a_driver() {
        let full: Vec<&str> = LEVEL_FEATURES.concat();
        let ide = |dr: &str| {
            dev(
                "0000:00:0f.0",
                "0x01018a",
                "pci:v0000FFFFd0000FFFFsv00000000sd00000000bc01sc01i8A",
                Some(dr),
            )
        };
        let v = verdict("etch-to-trixie", &machine(&full, false, vec![ide("pata_madeup")]));
        assert_eq!(v["lands_on"], "etch", "{v:#}");
        let v = verdict("etch-to-trixie", &machine(&full, false, vec![ide("ata_generic")]));
        assert_eq!(v["ok"], true, "{v:#}");
    }

    /// resolute's kernel records its built-in ata_piix's IDs, so an ID it dropped isn't driven just because the
    /// module's name is built in; a hand-written uppercase class is still judged.
    #[test]
    fn a_built_in_driver_with_recorded_ids_is_matched_by_id() {
        let full: Vec<&str> = LEVEL_FEATURES.concat();
        let piix = dev(
            "0000:00:1f.1",
            "0x0C0400",
            "pci:v00008086d0000FFFEsv00000000sd00000000bc01sc01i8A",
            Some("ata_piix"),
        );
        let v = verdict("trusty-to-resolute", &machine(&full, false, vec![piix]));
        assert_eq!(v["lands_on"], "noble", "{v:#}");
    }

    #[test]
    fn uefi_32_bit_and_small_machines_are_refused_before_the_first_hop() {
        let full: Vec<&str> = LEVEL_FEATURES.concat();
        let mut uefi = machine(&full, false, vec![]);
        uefi["boot"] = json!("uefi");
        let v = verdict("trusty-to-resolute", &uefi);
        assert_eq!(v["lands_on"], Value::Null);
        assert!(v["machine"][0].as_str().unwrap().contains("UEFI"));
        let mut i686 = machine(&full, false, vec![]);
        i686["arch"] = json!("i686");
        assert_eq!(verdict("etch-to-trixie", &i686)["lands_on"], Value::Null);
        let mut small = machine(&full, false, vec![]);
        // Debian's documented minimum is 64 MiB to wheezy, then 128 from jessie
        small["ram_mib"] = json!(100);
        let v = verdict("etch-to-trixie", &small);
        assert_eq!(v["lands_on"], "wheezy");
        assert!(report(&v).contains("The furthest it can land on is wheezy"));
        small["ram_mib"] = json!(32);
        let v = verdict("etch-to-trixie", &small);
        assert_eq!(v["lands_on"], "etch");
        assert!(report(&v).contains("can't take the first hop"));
    }

    /// A hop puts its kernel in /boot beside the ones it keeps. EL keeps only the running kernel: CentOS 5's 200 MB
    /// /boot (the VM tier's) takes the whole route, a 100 MB one stops before alma9.
    #[test]
    fn a_small_boot_stops_the_route_before_the_kernel_that_doesnt_fit() {
        let full: Vec<&str> = LEVEL_FEATURES.concat();
        let c5 = [("2.6.18-398.el5", 6)];
        let v = verdict("centos5-to-alma10", &with_boot(machine(&full, true, vec![]), 190, &c5));
        assert_eq!(v["ok"], true, "{v:#}");
        let v = verdict("centos5-to-alma10", &with_boot(machine(&full, true, vec![]), 95, &c5));
        assert_eq!(v["lands_on"], "centos7", "{v:#}");
        let p = v["hops"][2]["problems"][0].as_str().unwrap();
        assert!(
            p.contains("/boot") && p.contains("centos7's kernel") && p.contains("grow /boot"),
            "{p}"
        );
        // /boot on / is the disk check's business
        let mut flat = with_boot(machine(&full, true, vec![]), 95, &c5);
        flat["boot_dir"]["separate"] = json!(false);
        assert_eq!(verdict("centos5-to-alma10", &flat)["ok"], true);
    }

    /// Ubuntu's kernel_before purges every kernel older than the running one, so a hop's kernel only sits beside the
    /// running one: trusty's 243 MB LVM /boot takes the whole route even with five old kernels, a 195 MB one stops
    /// before noble.
    #[test]
    fn ubuntu_keeps_only_the_running_kernel_beside_the_new_one() {
        let full: Vec<&str> = LEVEL_FEATURES.concat();
        let five = [
            ("3.13.0-160-generic", 35),
            ("3.13.0-162-generic", 35),
            ("3.13.0-165-generic", 35),
            ("3.13.0-168-generic", 35),
            ("3.13.0-170-generic", 35),
        ];
        let v = verdict(
            "trusty-to-resolute",
            &with_boot(machine(&full, false, vec![]), 230, &five),
        );
        assert_eq!(v["ok"], true, "{v:#}");
        let one = [("3.13.0-170-generic", 35)];
        let v = verdict(
            "trusty-to-resolute",
            &with_boot(machine(&full, false, vec![]), 195, &one),
        );
        assert_eq!(v["lands_on"], "jammy", "{v:#}");
        let p = v["hops"][4]["problems"][0].as_str().unwrap();
        assert!(
            p.contains("jammy's kernel (88 MiB)") && !p.contains("focal's kernel") && p.contains("grow /boot"),
            "{p}"
        );
    }

    /// A Realtek NIC on ELRepo's kmod-r8168: every release's own r8169 takes it over, unless the machine blacklists
    /// r8169 (kmod-r8168 does). A disk on a DKMS driver no release's kernel has stops the route at the first hop.
    #[test]
    fn out_of_tree_drivers_and_blacklists() {
        let full: Vec<&str> = LEVEL_FEATURES.concat();
        let mut nic = dev(
            "0000:02:00.0",
            "0x020000",
            "pci:v000010ECd00008168sv00001043sd00008432bc02sc00i00",
            Some("r8168"),
        );
        nic["module"] = json!("r8168");
        nic["module_file"] = json!("extra/r8168/r8168.ko");
        nic["out_of_tree"] = json!(true);
        let mut m = machine(&full, false, vec![nic]);
        let v = verdict("centos5-to-alma10", &m);
        assert_eq!(v["ok"], true, "{v:#}");
        let n = v["hops"][0]["notes"][0].as_str().unwrap();
        assert!(
            n.contains("ELRepo") && n.contains("own r8169 takes the device over"),
            "{n}"
        );
        m["blacklisted"] = json!(["r8169"]);
        let v = verdict("centos5-to-alma10", &m);
        assert_eq!(v["lands_on"], "centos5", "{v:#}");
        assert!(v["hops"][0]["problems"][0]
            .as_str()
            .unwrap()
            .contains("which this machine blacklists"));
        let mut disk = dev(
            "0000:03:00.0",
            "0x010400",
            "pci:v0000FFFEd00000001sv00000000sd00000000bc01sc04i00",
            Some("vendor_raid"),
        );
        disk["module_file"] = json!("updates/dkms/vendor_raid.ko");
        disk["out_of_tree"] = json!(true);
        let v = verdict("etch-to-trixie", &machine(&full, false, vec![disk]));
        assert_eq!(v["lands_on"], "etch", "{v:#}");
        let p = v["hops"][0]["problems"][0].as_str().unwrap();
        assert!(p.contains("DKMS, outside the kernel"), "{p}");
    }

    #[test]
    fn boot_files_and_blacklists_are_read_like_the_tools_write_them() {
        let f = |n: &str, mib: u64| (n.to_string(), mib * 1024 * 1024);
        let files = [
            f("vmlinuz-3.10.0-1160.el7.x86_64", 7),
            f("initramfs-3.10.0-1160.el7.x86_64.img", 20),
            f("initramfs-3.10.0-1160.el7.x86_64kdump.img", 15),
            f(".vmlinuz-3.10.0-1160.el7.x86_64.hmac", 0),
            f("System.map-3.10.0-1160.el7.x86_64", 3),
            f("vmlinuz-0-rescue-0123", 7),
            f("initramfs-0-rescue-0123.img", 50),
            f("vmlinuz-2.6.32-754.el6.x86_64", 4),
            f("vmlinuz-3.10.0-1160.el7.x86_64.efi.signed", 7),
            f("initramfs-2.6.32-754.el6.x86_64.img", 16),
        ];
        let k = boot_kernels(&files, "3.10.0-1160.el7.x86_64");
        assert_eq!(
            k,
            vec![
                json!({"version": "2.6.32-754.el6.x86_64", "mib": 20, "running": false}),
                json!({"version": "3.10.0-1160.el7.x86_64", "mib": 37, "running": true}),
            ]
        );
        let b =
            blacklist_lines("# r8168\nblacklist r8169\ninstall pata-acpi /bin/false\ninstall foo /sbin/modprobe bar\n");
        assert_eq!(b, vec!["r8169", "pata_acpi"]);
    }

    /// EL's kernel_before keeps a kernel another package owns (kernel-xen) and every kdump image; a release the route
    /// only passes through installs no kernel, even with a table of its own.
    #[test]
    fn el_keeps_other_packages_kernels_and_skipped_releases_install_none() {
        let full: Vec<&str> = LEVEL_FEATURES.concat();
        let plain = [("2.6.18-398.el5", 6)];
        let with_xen = [("2.6.18-398.el5xen", 9), ("2.6.18-398.el5", 6)];
        let v = verdict(
            "centos5-to-alma10",
            &with_boot(machine(&full, true, vec![]), 85, &plain),
        );
        assert_eq!(v["lands_on"], "centos7", "{v:#}");
        let v = verdict(
            "centos5-to-alma10",
            &with_boot(machine(&full, true, vec![]), 85, &with_xen),
        );
        assert_eq!(v["lands_on"], "centos6", "{v:#}");
        let p = v["hops"][1]["problems"][0].as_str().unwrap();
        assert!(p.contains("its kernel 2.6.18-398.el5xen (9 MiB)"), "{p}");
        // a Xen guest running kernel-xen: it is counted once on the first hop, and stays after
        let xen_guest = [("2.6.18-398.el5", 6), ("2.6.18-398.el5xen", 9)];
        let v = verdict(
            "centos5-to-alma10",
            &with_boot(machine(&full, true, vec![]), 85, &xen_guest),
        );
        assert_eq!(v["lands_on"], "centos6", "{v:#}");
        assert_eq!(v["hops"][0]["ok"], true, "{v:#}");

        let mut a = atlas();
        let (_, r) = a.route("centos5-to-alma10").unwrap();
        let mut r = r.clone();
        r["hops"] = json!(["centos6", "centos7", "alma8", "alma9", "alma10"]);
        let el = a.adapters.get_mut("el").unwrap();
        let mut alma8 = el["hardware"]["alma9"].clone();
        alma8["boot_mib"] = json!(1);
        el["hardware"]["alma8"] = alma8;
        let m = with_boot(machine(&full, true, vec![]), 110, &plain);
        let v = evaluate(&a, &r, &m).unwrap();
        assert_eq!(v["hops"][2]["notes"][0], "not booted on this route");
        let p = v["hops"][3]["problems"][0].as_str().unwrap();
        assert!(p.contains("centos7's kernel (45 MiB)"), "{p}");
    }

    /// Debian's apt autoremoves no kernel before jessie's: on the hop to jessie every kernel is still there.
    #[test]
    fn debian_keeps_every_kernel_through_jessie() {
        let full: Vec<&str> = LEVEL_FEATURES.concat();
        let etch = [("2.6.18-6-amd64", 8)];
        // etch 8 + lenny 12 + squeeze 16 + wheezy 20 + jessie 26 + 4 other = 86; stretch: wheezy 20 + jessie 26 + 32
        let v = verdict("etch-to-trixie", &with_boot(machine(&full, false, vec![]), 85, &etch));
        assert_eq!(v["lands_on"], "wheezy", "{v:#}");
        let v = verdict("etch-to-trixie", &with_boot(machine(&full, false, vec![]), 86, &etch));
        assert_eq!(v["hops"][3]["ok"], true, "{v:#}");
        assert_eq!(v["hops"][4]["ok"], true, "{v:#}");
    }

    fn app(program: &str, version: &str, via: &str, owner: Option<&str>) -> Value {
        json!({"runtime": "python2", "version": version, "via": via, "program": program,
               "interpreter": "/usr/bin/python2.7", "where": "billing.service", "owner": owner})
    }

    fn modern(runtimes: Vec<Value>) -> Value {
        let mut f: Vec<&str> = LEVEL_FEATURES[0].to_vec();
        f.extend(LEVEL_FEATURES[1]);
        let mut m = machine(&f, false, vec![]);
        m["runtimes"] = json!(runtimes);
        m
    }

    /// SPEC "Runtime preflight": a CentOS 7 box whose own billing app runs on Python 2. Its hardware reaches alma10,
    /// but Alma 9 ships no Python 2 and wend never lands on Alma 8, so the choice is stay or shelter.
    #[test]
    fn an_el7_python2_app_is_told_to_stay_or_shelter() {
        let v = verdict(
            "centos7-to-alma10",
            &modern(vec![
                app("/opt/billing/app.py", "2.7", "process", None),
                app("/usr/bin/fail2ban-server", "2.7", "process", Some("fail2ban-server")),
            ]),
        );
        assert_eq!(v["lands_on"], "alma10", "{v:#}");
        assert_eq!(v["keeps_runtimes_on"], "centos7");
        assert_eq!(v["ok"], false);
        assert_eq!(
            v["runtimes"].as_array().unwrap().len(),
            1,
            "fail2ban is the distro's: {v:#}"
        );
        let p = v["hops"][0]["runtime_problems"][0].as_str().unwrap();
        assert!(
            p.starts_with("alma9 ships no Python 2") && p.contains("/opt/billing/app.py"),
            "{p}"
        );
        let r = report(&v);
        assert!(
            r.contains("stay on centos7") && r.contains("shelter: upgrade to alma10,"),
            "{r}"
        );
        // the distro's own Python 2 alone changes nothing
        let v = verdict(
            "centos7-to-alma10",
            &modern(vec![app(
                "/usr/bin/fail2ban-server",
                "2.7",
                "process",
                Some("fail2ban-server"),
            )]),
        );
        assert_eq!(v["ok"], true, "{v:#}");
        assert_eq!(v["keeps_runtimes_on"], Value::Null);
        // mod_wsgi counts whoever owns httpd
        let v = verdict(
            "centos7-to-alma10",
            &modern(vec![app("/usr/sbin/httpd", "2.7", "embedded", Some("httpd"))]),
        );
        assert_eq!(v["keeps_runtimes_on"], "centos7");
    }

    /// Ubuntu keeps Python 2 in universe through jammy; Debian through bullseye, with a note where an app's own
    /// 2.x goes.
    #[test]
    fn apt_routes_stop_where_python2_ends() {
        let v = verdict(
            "trusty-to-resolute",
            &modern(vec![app("/srv/app/run.py", "2.7", "cron", None)]),
        );
        assert_eq!(v["keeps_runtimes_on"], "jammy", "{v:#}");
        assert_eq!(v["lands_on"], "resolute");
        let r = report(&v);
        assert!(
            r.contains("walk the route only to jammy") && r.contains("shelter: upgrade to resolute, and run"),
            "{r}"
        );
        let v = verdict(
            "etch-to-trixie",
            &modern(vec![app("/opt/old/job.py", "2.4", "cron", None)]),
        );
        assert_eq!(v["keeps_runtimes_on"], "bullseye", "{v:#}");
        let notes = v["hops"].to_string();
        assert_eq!(
            notes.matches("runs on Python 2.4;").count(),
            1,
            "noted once, at squeeze: {notes}"
        );
        assert!(
            v["hops"][1]["notes"]
                .to_string()
                .contains("squeeze ships Python 2.5.5 and 2.6.6"),
            "{v:#}"
        );
    }

    /// When the hardware stops first, the runtime isn't the owner's choice to make: the hardware verdict stands.
    #[test]
    fn hardware_that_stops_first_decides() {
        let mut m = machine(V1_PENRYN, false, vec![]);
        m["runtimes"] = json!([app("/opt/billing/app.py", "2.4", "process", None)]);
        let v = verdict("centos5-to-alma10", &m);
        assert_eq!(
            (v["lands_on"].as_str(), v["keeps_runtimes_on"].as_str()),
            (Some("centos7"), Some("centos7"))
        );
        let r = report(&v);
        assert!(
            r.contains("The furthest it can land on is centos7") && !r.contains("shelter"),
            "{r}"
        );
        // facts without the runtimes this client gathers are refused, not read as "none"
        let mut old = m.clone();
        old.as_object_mut().unwrap().remove("runtimes");
        let a = atlas();
        let (_, r) = a.route("centos5-to-alma10").unwrap();
        assert!(evaluate(&a, r, &old).unwrap_err().contains("runtimes"));
    }
}
