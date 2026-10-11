//! How a hop runs (SPEC.md, "How a hop runs"): the commands the harness runs for one hop, around the pre and
//! post steps of the fixes whose `when` held.

use crate::atlas::{get, list_of, str_of, Doc};
use crate::backend::{fill, hop_vars, step};
use serde_json::Value;
use std::collections::BTreeSet;

/// SPEC "restores and adds": one name family per line, appended by the harness, read by the services check
pub const ADDED: &str = "/var/lib/wend/added-families";

pub struct HopPlan {
    /// steps 1 to 7: set the release, refresh, pre steps, the upgrade, post steps, pm-option cleanup, verify
    pub commands: Vec<String>,
    /// step 8: run whatever happened above
    pub after: Vec<String>,
}

fn phase(f: &Doc, ph: &str) -> Vec<Value> {
    get(f, ph).and_then(Value::as_array).cloned().unwrap_or_default()
}

pub fn hop_plan(adapter: &Doc, hop: &str, fixes: &[&Doc]) -> Result<HopPlan, String> {
    let h = get(adapter, "hop").ok_or("the adapter has no [hop]")?;
    let v = hop_vars(adapter, hop)?;
    let comp = |ph: &str| -> Result<Vec<String>, String> {
        let mut out = vec![];
        for f in fixes {
            for s in phase(f, ph) {
                out.push(step(&s, adapter, hop)?);
            }
        }
        Ok(out)
    };
    // pm-options are hop-scoped: their files go after the post steps
    let mut opts = BTreeSet::new();
    for f in fixes {
        for s in phase(f, "pre").iter().chain(phase(f, "post").iter()) {
            if get(s, "op").and_then(Value::as_str) == Some("pm-option") {
                opts.insert(str_of(s, "option")?.to_string());
            }
        }
    }
    let mut cleanup = vec![];
    for o in &opts {
        let p = get(adapter, "pm_options")
            .and_then(|p| get(p, o))
            .ok_or(format!("unknown pm-option {o}"))?;
        cleanup.push(format!("rm -f {}", str_of(p, "path")?));
    }
    let rel = get(adapter, "releases")
        .and_then(|r| get(r, hop))
        .filter(|r| r.is_object());
    let upgrade = match rel.and_then(|r| get(r, "upgrade")) {
        Some(_) => list_of(rel.unwrap(), "upgrade")?,
        None => list_of(h, "upgrade")?,
    };
    let mut commands = vec![fill(str_of(h, "set_release")?, &v), fill(str_of(h, "refresh")?, &v)];
    commands.extend(comp("pre")?);
    commands.extend(upgrade.iter().map(|c| fill(c, &v)));
    commands.extend(comp("post")?);
    // SPEC "restores and adds": the families the fired fixes may introduce, for the services check
    let adds: BTreeSet<String> = fixes
        .iter()
        .flat_map(|f| list_of(f, "adds").unwrap_or_default())
        .collect();
    if let Some(bad) = adds.iter().find(|x| !crate::check::full(crate::check::FAMILY, x)) {
        return Err(format!("adds must be name families, not {bad:?}")); // never shell, on any path
    }
    if !adds.is_empty() {
        let lines: String = adds.iter().map(|x| format!("{x}\\n")).collect();
        commands.push(format!("mkdir -p /var/lib/wend && printf '{lines}' >> {ADDED}"));
    }
    commands.extend(cleanup);
    if let Some(verify) = get(h, "verify").and_then(Value::as_str) {
        commands.push(fill(verify, &v)); // the release file says we arrived
    }
    let after = match get(h, "after") {
        Some(_) => list_of(h, "after")?.iter().map(|c| fill(c, &v)).collect(),
        None => vec![],
    };
    Ok(HopPlan { commands, after })
}

/// The fixes a route pins, in route order, resolved by id.
pub fn pinned<'a>(
    route: &Doc,
    fixes_by_id: &std::collections::BTreeMap<String, &'a Doc>,
) -> Result<Vec<(String, &'a Doc)>, String> {
    get(route, "fixes")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .map(|r| {
            let id = str_of(r, "id")?;
            fixes_by_id
                .get(id)
                .map(|f| (id.to_string(), *f))
                .ok_or(format!("the route pins {id}, which no fix has"))
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::hop_plan;
    use crate::atlas::Doc;

    /// The same string tools/test_check.py asserts for the Python engine: families sorted and deduplicated, one per
    /// line, appended after the post steps.
    #[test]
    fn records_the_fired_fixes_adds_after_their_post_steps() {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("..");
        let a: Doc =
            crate::atlas::parse_toml(&std::fs::read_to_string(root.join("adapters/el.toml")).unwrap()).unwrap();
        let fix = |adds: &str| -> Doc {
            crate::atlas::parse_toml(&format!(
                "hop = \"alma9\"\nadds = [{adds}]\n[[post]]\nop = \"kv.set\"\npath = \"/etc/vsftpd/vsftpd.conf\"\nvalues = {{ tcp_wrappers = \"NO\" }}\n"
            ))
            .unwrap()
        };
        let (f1, f2) = (fix("\"polkit\", \"NetworkManager\""), fix("\"polkit\""));
        let p = hop_plan(&a, "alma9", &[&f1, &f2]).unwrap();
        let want = "mkdir -p /var/lib/wend && printf 'NetworkManager\\npolkit\\n' >> /var/lib/wend/added-families";
        let at = p.commands.iter().position(|c| c == want).expect("no adds step");
        let post = p.commands.iter().rposition(|c| c.contains("vsftpd")).unwrap();
        assert!(at > post);
        // a direct walk doesn't run the checker first: the compile refuses a non-family itself
        let evil = fix("\"x' ; reboot ; '\"");
        assert!(hop_plan(&a, "alma9", &[&evil]).is_err());
    }
}
