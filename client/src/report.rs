//! SPEC "Gap report": what a failed run tells upstream, scrubbed, and the prefilled `route-gap.yml` issue link. The
//! same report and key as `./wend report` (tools/report.py), so repeat reports of one gap from either merge into one
//! issue. Built here for a failed rehearsal: nothing is sent, the owner reads the file and opens the link.

use crate::share::{pct, scrub, REPO_URL, URL_BUDGET};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

/// The gap: its key, the release before the hop that broke, and that hop. A walk that finished every hop and still
/// failed its final checks broke on its last hop.
pub fn gap_key(run: &Value, family: &str) -> (String, String, String) {
    let hops = run["hops"].as_array().cloned().unwrap_or_default();
    let i = hops
        .iter()
        .position(|h| h["ok"] != json!(true))
        .unwrap_or(hops.len().saturating_sub(1));
    let before = match i {
        0 => run["from"].as_str().unwrap_or("start").to_string(),
        _ => hops[i - 1]["to"].as_str().unwrap_or("?").to_string(),
    };
    let hop = hops
        .get(i)
        .and_then(|h| h["to"].as_str())
        .or(run["to"].as_str())
        .unwrap_or("?")
        .to_string();
    let stage = regex::Regex::new(r"[0-9][0-9.:+~-]*")
        .unwrap()
        .replace_all(run["failed_stage"].as_str().unwrap_or(""), "N")
        .into_owned();
    let mut failed: Vec<String> = run["final"]["failed"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|v| v.as_str().map(String::from))
        .collect();
    failed.sort();
    // json.dumps's own layout, as tools/report.py hashes it: non-ASCII as \u escapes (UTF-16, as Python writes them)
    let j = |s: &str| {
        serde_json::to_string(s)
            .unwrap()
            .chars()
            .map(|c| match c as u32 {
                0..=0x7f => c.to_string(),
                _ => c
                    .encode_utf16(&mut [0; 2])
                    .iter()
                    .map(|u| format!("\\u{u:04x}"))
                    .collect(),
            })
            .collect::<String>()
    };
    let raw = format!(
        "[{}, {}, {}, {}, [{}]]",
        j(family),
        j(&before),
        j(&hop),
        j(&stage),
        failed.iter().map(|c| j(c)).collect::<Vec<_>>().join(", ")
    );
    let key: String = Sha256::digest(raw.as_bytes())[..6]
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect();
    (key, before, hop)
}

/// The report (markdown, then its fields as JSON) and the issue link. `output` is the failing command's output, when
/// a command (not a check) is what broke.
pub fn build(run: &Value, family: &str, output: Option<&str>, words: &[String]) -> (String, String, String) {
    let (key, before, hop) = gap_key(run, family);
    let hops = run["hops"].as_array().cloned().unwrap_or_default();
    let fired: Vec<String> = hops
        .iter()
        .find(|h| h["to"].as_str() == Some(hop.as_str()))
        .and_then(|h| h["fired_names"].as_array())
        .into_iter()
        .flatten()
        .filter_map(|v| v.as_str().map(String::from))
        .collect();
    let failed: Vec<String> = run["final"]["failed"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|v| v.as_str().map(|c| scrub(c, words)))
        .collect();
    let services: Vec<String> = run["start_services"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|v| v.as_str().map(|c| scrub(c, words)))
        .collect();
    let stage = scrub(run["failed_stage"].as_str().unwrap_or(""), words);
    let stage = if stage.is_empty() {
        "the final checks".to_string()
    } else {
        stage
    };
    let body = json!({
        "gap_key": key, "family": family, "from_release": before, "hop": hop,
        "route": run["route"], "route_target": run["to"], "fixes_fired_on_this_hop": fired,
        "failed_stage": stage, "failed_checks": failed, "start_fingerprint": run["start_fingerprint"],
        "services": services, "client": run["runner"]["kind"],
    });
    let route = run["route"].as_str().unwrap_or("?");
    let mut md = vec![
        format!("### Gap `{key}`: {family} {before} → {hop}"),
        String::new(),
        format!(
            "- route: `{}` toward {}",
            &route[..route.len().min(19)],
            run["to"].as_str().unwrap_or("?")
        ),
        format!(
            "- fixes that fired on this hop: {}",
            if fired.is_empty() {
                "none".to_string()
            } else {
                fired.join(", ")
            }
        ),
        format!("- failed at: `{stage}`"),
        format!(
            "- checks failing after rollback: {}",
            if failed.is_empty() {
                "none".to_string()
            } else {
                failed.join(", ")
            }
        ),
        format!(
            "- box fingerprint: `{}`",
            run["start_fingerprint"].as_str().unwrap_or("?")
        ),
        String::new(),
    ];
    if !services.is_empty() {
        md.push(format!("Services on the box at the start: {}", services.join(", ")));
        md.push(String::new());
    }
    let fence = |name: &str, log: &str| {
        let log = scrub(log, words);
        let tail: String = log
            .chars()
            .rev()
            .take(1500)
            .collect::<Vec<_>>()
            .into_iter()
            .rev()
            .collect();
        vec![
            format!("<details><summary>{name}</summary>"),
            String::new(),
            "```".into(),
            tail,
            "```".into(),
            "</details>".into(),
            String::new(),
        ]
    };
    if let Some(out) = output.filter(|o| !o.trim().is_empty()) {
        md.extend(fence("the failing command's output", out));
    }
    if let Some(logs) = run["final"]["failed_logs"].as_object() {
        for (c, l) in logs {
            md.extend(fence(&format!("{} log", scrub(c, words)), l.as_str().unwrap_or("")));
        }
    }
    let md = md.join("\n");
    let title = format!("gap: {family} {before} -> {hop} [{key}]");
    let url = |details: &str| {
        format!(
            "{REPO_URL}/issues/new?template=route-gap.yml&title={}&labels=route-gap&gap_key={key}&family={}&from_release={}&hop={}&details={}",
            pct(&title),
            pct(family),
            pct(&before),
            pct(&hop),
            pct(details)
        )
    };
    let mut link = url(&md);
    // the budget is the encoded URL's: a non-ASCII character takes up to 12 bytes there, so the summary shrinks
    // until the link fits
    let mut keep = 1500;
    while link.len() > URL_BUDGET && keep > 0 {
        let short: String = md.chars().take(keep).collect();
        link = url(&format!(
            "{short}\n\n(full report attached: drag wend-gap-{key}.md into this issue)"
        ));
        keep = keep * 3 / 4;
    }
    let file = format!(
        "{md}\n\n```json\n{}\n```\n",
        serde_json::to_string_pretty(&body).unwrap()
    );
    (key, file, link)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn run() -> Value {
        json!({"route": "sha256:0123456789abcdef0123", "from": "centos7", "to": "alma10", "box": "el/x",
               "hops": [{"to": "alma8", "ok": true, "fired_names": ["a"]},
                        {"to": "alma9", "ok": false, "fired_names": ["b", "c"]}],
               "failed_stage": "command failed: dnf -y install foo-1.2.3",
               "final": {"failed": ["web"], "failed_logs": {"web": "curl http://10.0.0.5/ from web01: 500"}},
               "start_fingerprint": "sha256:ab", "start_services": ["httpd"], "runner": {"kind": "client"}})
    }

    /// The key is tools/report.py's: sha256 of json.dumps([family, before, hop, stage, failed])[:12].
    #[test]
    fn keys_the_gap_as_the_python_report_does() {
        let (key, before, hop) = gap_key(&run(), "el");
        assert_eq!((before.as_str(), hop.as_str()), ("alma8", "alma9"));
        let raw = r#"["el", "alma8", "alma9", "command failed: dnf -y install foo-N", ["web"]]"#;
        let want: String = Sha256::digest(raw.as_bytes())[..6]
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        assert_eq!(key, want);
        // json.dumps escapes what isn't ASCII: python3 -c 'import json; print(json.dumps(["café", "🙂"]))'
        let mut r = run();
        r["final"]["failed"] = json!(["café", "🙂"]);
        let raw = r#"["el", "alma8", "alma9", "command failed: dnf -y install foo-N", ["caf\u00e9", "\ud83d\ude42"]]"#;
        let want: String = Sha256::digest(raw.as_bytes())[..6]
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        assert_eq!(gap_key(&r, "el").0, want);
    }

    #[test]
    fn scrubs_and_links_the_route_gap_template() {
        let words = vec!["web01".to_string()];
        let (key, file, link) = build(&run(), "el", Some("Error: nothing provides libfoo for web01"), &words);
        assert!(file.contains(&format!("### Gap `{key}`: el alma8 → alma9")));
        assert!(file.contains("b, c"));
        assert!(!file.contains("web01") && !file.contains("10.0.0.5"), "{file}");
        assert!(file.contains("the failing command's output"));
        assert!(link.starts_with(&format!("{REPO_URL}/issues/new?template=route-gap.yml")));
        assert!(link.contains(&format!("gap_key={key}")));
        // every hop passed and the final checks failed: the gap is the last hop's
        let mut r = run();
        r["hops"][1]["ok"] = json!(true);
        r["failed_stage"] = json!("");
        assert_eq!(gap_key(&r, "el").2, "alma9");
        assert!(build(&r, "el", None, &[]).1.contains("failed at: `the final checks`"));
        // an owner's check named after the machine is scrubbed too
        r["final"]["failed"] = json!(["web01-backup"]);
        r["start_services"] = json!(["httpd", "web01-sync"]);
        r["final"]["failed_logs"] = json!({"web01-backup": "no"});
        let (_, file, link) = build(&r, "el", None, &words);
        assert!(!file.contains("web01") && !link.contains("web01"), "{file}");
    }
}
