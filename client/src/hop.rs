//! How a hop runs (SPEC.md, "How a hop runs"): the commands the harness runs for one hop, around the pre and
//! post steps of the fixes whose `when` held.

use crate::atlas::{get, list_of, str_of, Doc};
use crate::backend::{fill, hop_vars, step};
use serde_json::Value;
use std::collections::BTreeSet;

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
