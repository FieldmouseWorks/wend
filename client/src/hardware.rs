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

/// What preflight knows about this machine (schema `wend.hardware/0`). Read from the hardware and /proc and /sys,
/// never from the distro's tools, so it reads the same on CentOS 5 as on trixie.
pub fn gather() -> Value {
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
    json!({
        "schema": "wend.hardware/0",
        "arch": arch,
        "userland_bits": userland,
        "cpu": {"model": model, "features": features, "hypervisor": hypervisor},
        "boot": if Path::new("/sys/firmware/efi").exists() { "uefi" } else { "bios" },
        "ram_mib": ram_mib,
        "free_mib": free_mib("/").map_or(Value::Null, |m| json!(m)),
        "pci": pci_devices(Path::new("/sys/bus/pci/devices")),
    })
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
    let c = std::ffi::CString::new(path).ok()?;
    let mut s: libc::statvfs = unsafe { std::mem::zeroed() };
    // SAFETY: statvfs fills the struct it is given.
    if unsafe { libc::statvfs(c.as_ptr(), &mut s) } != 0 {
        return None;
    }
    Some(s.f_bavail as u64 * s.f_frsize as u64 / (1024 * 1024))
}

/// Every PCI device: its slot, class, modalias (built from the ID files, which every kernel since 2.6 has, in the
/// form a kernel's module aliases match) and the driver bound to it now.
fn pci_devices(dir: &Path) -> Vec<Value> {
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
        let driver = fs::read_link(d.join("driver"))
            .ok()
            .and_then(|p| p.file_name().map(|n| n.to_string_lossy().into_owned()));
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

fn kind_of(class: &str) -> Option<&'static str> {
    match class.get(2..4) {
        Some("01") => Some("disk controller"),
        Some("02") => Some("network card"),
        _ => None,
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
    } else if facts["userland_bits"] == json!(32) {
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
    for h in &hops {
        let mut problems: Vec<String> = vec![];
        let mut notes: Vec<String> = vec![];
        let Some(t) = get(hw, h) else {
            if booted.contains(h) {
                return Err(format!("adapters/{fam}.toml has no hardware.{h} table"));
            }
            out_hops.push(json!({"to": h, "ok": !blocked, "problems": [], "notes": ["not booted on this route"]}));
            continue;
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
            if have < need {
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
            let Some(kind) = kind_of(d["class"].as_str().unwrap_or("")) else {
                continue;
            };
            let alias = d["modalias"].as_str().unwrap_or("");
            let driver = d["driver"].as_str();
            let hit = table
                .aliases
                .iter()
                .find(|(_, p)| alias_match(p.as_bytes(), alias.as_bytes()));
            let built_in = driver.is_some_and(|dr| table.builtin.iter().any(|b| *b == dr.replace('-', "_")));
            if hit.is_some() || built_in {
                continue;
            }
            let ids = alias.get(4..23).unwrap_or(alias);
            let slot = d["slot"].as_str().unwrap_or("?");
            match driver {
                Some(dr) => problems.push(format!(
                    "{h}'s kernel {} has no driver for the {kind} at {slot} ({ids}), which {dr} drives now: \
                     it would come up without it",
                    table.kernel
                )),
                None => notes.push(format!(
                    "{h}'s kernel has no driver for the unused {kind} at {slot} ({ids})"
                )),
            }
        }
        let ok = problems.is_empty() && !blocked;
        if ok {
            lands = Some(h.clone());
        } else {
            blocked = true;
        }
        out_hops.push(json!({"to": h, "ok": problems.is_empty(), "problems": problems, "notes": notes}));
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
        "ok": lands.as_deref() == Some(to),
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
        s += &format!("{} {to}\n", if probs.is_empty() { "ok     " } else { "NO     " });
        for p in &probs {
            s += &format!("        {}\n", p.as_str().unwrap_or(""));
        }
        for n in h["notes"].as_array().into_iter().flatten() {
            s += &format!("        note: {}\n", n.as_str().unwrap_or(""));
        }
    }
    s += &match (v["ok"].as_bool(), v["lands_on"].as_str()) {
        (Some(true), _) => format!("This machine can land on {}.\n", v["to"].as_str().unwrap_or("?")),
        (_, Some(l)) if l == v["from"].as_str().unwrap_or("") => {
            format!("This machine can't take the first hop. Nothing was touched; it stays on {l}.\n")
        }
        (_, Some(l)) => format!(
            "This machine can't reach {}. The furthest it can land on is {l}: walk the route only that far.\n",
            v["to"].as_str().unwrap_or("?")
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
        json!({"slot": slot, "class": class, "modalias": alias, "driver": driver})
    }

    fn machine(features: &[&str], vm: bool, pci: Vec<Value>) -> Value {
        json!({"schema": "wend.hardware/0", "arch": "x86_64", "userland_bits": 64,
               "cpu": {"model": "test", "features": features, "hypervisor": vm}, "boot": "bios",
               "ram_mib": 4096, "free_mib": 20000, "pci": pci})
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
}
