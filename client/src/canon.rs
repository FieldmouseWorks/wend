//! Canonical JSON and content ids (SPEC.md, "Identity").
//!
//! `id = "sha256:" + hex(sha256(canonical_json(doc without [meta])))`. Keys sorted by code point, no whitespace,
//! strings as UTF-8 with only `"`, `\` and control characters escaped. This is byte-for-byte what Python's
//! `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)` writes, which is what
//! tools/check.py hashes, so both tools give every fix and route the same id.

use serde_json::Value;
use sha2::{Digest, Sha256};

pub fn canonical(v: &Value) -> String {
    let mut out = String::new();
    write(v, &mut out);
    out
}

fn write(v: &Value, out: &mut String) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => out.push_str(&n.to_string()),
        Value::String(s) => string(s, out),
        Value::Array(a) => {
            out.push('[');
            for (i, x) in a.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write(x, out);
            }
            out.push(']');
        }
        Value::Object(m) => {
            // UTF-8 byte order is code point order, which is what Python's sort_keys uses
            let mut keys: Vec<&String> = m.keys().collect();
            keys.sort();
            out.push('{');
            for (i, k) in keys.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                string(k, out);
                out.push(':');
                write(&m[k.as_str()], out);
            }
            out.push('}');
        }
    }
}

fn string(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

pub fn sha256_hex(bytes: &[u8]) -> String {
    Sha256::digest(bytes).iter().map(|b| format!("{b:02x}")).collect()
}

/// A fix's or route's id covers what it does, never what it is called: the top-level `meta` table is left out.
pub fn content_id(doc: &Value) -> String {
    let stripped = match doc {
        Value::Object(m) => Value::Object(
            m.iter()
                .filter(|(k, _)| k.as_str() != "meta")
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect(),
        ),
        v => v.clone(),
    };
    format!("sha256:{}", sha256_hex(canonical(&stripped).as_bytes()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn matches_python_json_dumps() {
        // python3 -c 'import json; print(json.dumps(x, sort_keys=True, separators=(",", ":"), ensure_ascii=False))'
        let v = json!({"b": [1, true, "x\ny\"\\\t\u{1}"], "a": {"é": "ü", "Z": null}});
        assert_eq!(
            canonical(&v),
            "{\"a\":{\"Z\":null,\"é\":\"ü\"},\"b\":[1,true,\"x\\ny\\\"\\\\\\t\\u0001\"]}"
        );
    }

    #[test]
    fn meta_is_not_part_of_the_id() {
        let a = json!({"schema": "wend.fix/0", "meta": {"name": "a"}});
        let b = json!({"schema": "wend.fix/0", "meta": {"name": "b"}});
        assert_eq!(content_id(&a), content_id(&b));
        assert_ne!(content_id(&a), content_id(&json!({"schema": "wend.fix/1"})));
    }
}
