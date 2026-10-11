"""Discovery: when a hop fails, ask a model for the missing fix, check it like a PR would, try it in the sandbox."""
import json
import os
import re

import backends
import check
from backends import fill
from sandbox import tail

PROMPT = open(os.path.join(check.ROOT, "prompts", "discover.md")).read()
CORE_OPS_DOC = {
    "install": '{"op": "install", "packages": ["pkg", ...]}',
    "remove": '{"op": "remove", "packages": [...], "purge": true}',
    "repair": '{"op": "repair"}  (fix broken dependencies)',
    "force-remove": '{"op": "force-remove", "packages": [...], "purge": true, "force": [FORCE]}',
    "write-file": '{"op": "write-file", "path": "/etc/...", "content": "whole file"}',
    "pm-option": '{"op": "pm-option", "option": "overwrite-conflicting-files"}  (this hop only)',
}


def arg_doc(s):
    if "enum" in s:
        return json.dumps(s["enum"])
    if "list" in s:
        return f'["pkg", ...]  (each matching {s["list"]}, at least {s.get("min", 1)})'
    if "paths" in s:
        return '["/etc/...", ...]  (on the writable list; a * stays a glob)'
    if "table" in s:
        return f'{{"key": "value", ...}}  (values matching {s["table"]})'
    return json.dumps(s.get("pattern"))


def ops_doc(adapter):
    force = json.dumps(adapter["force"])[1:-1]
    lines = [f"- {v}".replace("FORCE", force) for k, v in CORE_OPS_DOC.items()
             if k != "pm-option" or adapter.get("pm_options")]
    for name, o in adapter.get("ops", {}).items():
        args = "".join(f', "{a}": {arg_doc(s)}' for a, s in o["args"].items())
        lines.append(f'- {{"op": "{name}"{args}}}  ({o["summary"]})')
    return "\n".join(lines)


def parse(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in the answer")
    ans = json.loads(m.group(0))
    if not isinstance(ans.get("fixes"), list) or not ans["fixes"]:
        raise ValueError("answer needs a non-empty 'fixes' list")
    return ans


class Discoverer:
    def __init__(self, provider, max_calls=12):
        self.p, self.max_calls, self.calls, self.added = provider, max_calls, 0, []

    def doc(self, f, walk, hop):
        name = re.sub(r"[^a-z0-9.-]+", "-", str(f.get("name", "fix")).lower()).strip("-") or "fix"
        d = {"schema": "wend.fix/0", "family": walk.family, "hop": hop, "when": f.get("when")}
        for ph in ("pre", "post"):
            if f.get(ph):
                d[ph] = f[ph]
        d["restores"] = [c if c in walk.adapter["checks"] else f"box:{c}" for c in f.get("restores", [])]
        d["adds"] = f.get("adds", [])
        d["meta"] = {"name": name, "author": self.p.label, "found_on": walk.box_id, "fires_on": [walk.box_id]}
        return d

    def prompt(self, walk, hop, matched, attempts):
        s = walk.steps[-1]
        lost = [c for c in walk.checks if c in s["stage"]]
        a = walk.adapter["hop"]
        v = {**backends.of(walk.adapter).hop_vars(walk.adapter, hop), "mirror": "<mirror>"}
        rel = walk.adapter["releases"].get(hop)
        upgrade = rel["upgrade"] if isinstance(rel, dict) and "upgrade" in rel else a["upgrade"]
        return PROMPT.format(
            family=walk.family, release_now=walk.health["release"], hop=hop,
            set_release=fill(a["set_release"], v), refresh=fill(a["refresh"], v),
            upgrade=", then ".join(f"`{fill(c, v)}`" for c in upgrade),
            matched=json.dumps([{k: f[k] for k in ("when", "pre", "post") if k in f} | {"name": f["meta"]["name"]}
                                for f in matched], indent=1) if matched else "none",
            ops=ops_doc(walk.adapter), writable=", ".join(walk.adapter.get("writable", [])),
            checks=json.dumps(walk.checks, indent=1), stage=s["stage"],
            output=tail(s["outputs"][-1]["out"], 3000) if s["outputs"] else "",
            lost="".join(f"\nCheck `{c}` now fails with:\n```\n{tail(s['health_after']['checks'][c]['log'], 1500)}\n```\n"
                         for c in lost),
            attempts=("\n## Your earlier proposals for this hop, all refused or rolled back\n" + "".join(
                f"\n- {json.dumps(x['fixes'])}\n  result: {tail(x['result'], 1500)}\n" for x in attempts)) if attempts else "")

    def fill(self, walk, hop, matched):
        """Returns the new fix docs that made the hop pass, or None when the budget runs out."""
        attempts = []
        while self.calls < self.max_calls:
            self.calls += 1
            text = self.p.ask(self.prompt(walk, hop, matched, attempts))
            try:
                ans = parse(text)
            except (ValueError, json.JSONDecodeError) as e:
                attempts.append({"fixes": text[:400], "result": f"refused: could not read your answer ({e})"})
                continue
            new = [self.doc(f, walk, hop) for f in ans["fixes"]]
            errs = [e for d in new for e in check.check_fix(
                d, walk.repo.adapters, f"fixes/{walk.family}/{hop}/{d['meta']['name']}.toml")]
            errs += [f"precondition of {d['meta']['name']} is false on this box" for d in new
                     if not errs and not walk.holds(d)]
            walk.log(f"model: {str(ans.get('reason', ''))[:160]}" + (f"  REFUSED: {errs[0]}" if errs else ""))
            if errs:
                attempts.append({"fixes": ans["fixes"], "result": "refused before running: " + "; ".join(errs)})
                continue
            if walk.execute(walk.hop_cmds(hop, matched + new), f"hop to {hop} with model fixes"):
                for d in new:
                    d["meta"]["reason"] = str(ans.get("reason", ""))
                self.added += new
                return new
            s = walk.steps[-1]
            attempts.append({"fixes": ans["fixes"],
                             "result": s["stage"] + "\n" + (s["outputs"][-1]["out"][-1200:] if s["outputs"] else "")})
        return None

    def summary(self):
        return {"model": self.p.label, "model_calls": self.calls, "model_usage": self.p.usage,
                "discovered": [d["meta"]["name"] for d in self.added]}
