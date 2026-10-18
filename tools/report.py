"""Gap reports: a route broke on someone's machine and they have no model (or no interest) to fix it themselves.

  ./wend report RUN.json [--redact WORD ...]

Reads the run the client (or ./wend replay) wrote, strips anything identifying, SHOWS the user the whole report, and
only then prints a prefilled GitHub issue link. Nothing is sent anywhere by this tool: the user opens the link
themselves (no token, no account linking), and attaches the full report file if the log is too long for a URL.

The gap key (sha256 of family, release before the hop, the hop, the failed stage and the failed checks, with
version noise removed) is in the issue title, so a triage workflow can merge repeat reports into one issue with +1s.
"""
import hashlib
import json
import os
import re
import urllib.parse

REPO_URL = os.environ.get("WEND_REPO_URL", "https://github.com/FieldmouseWorks/wend")
URL_BUDGET = 7000  # GitHub truncates very long prefilled URLs; the rest goes in the attachment

SECRETS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "<private key>"),
    (re.compile(r"(?i)\b(pass(word|wd)?|secret|token|api[_-]?key|auth)\b\s*[:=]\s*\S+"), r"\1=<redacted>"),
    (re.compile(r"\b(sk|ghp|gho|github_pat|xox[bpa]|AKIA)[-_A-Za-z0-9]{12,}"), "<token>"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "<email>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<ip>"),
    (re.compile(r"\b(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4}\b", re.I), "<ip6>"),
    (re.compile(r"\b[0-9a-f]{2}(?::[0-9a-f]{2}){5}\b", re.I), "<mac>"),
    (re.compile(r"/home/[^/\s]+"), "/home/<user>"),
]


def local_words(extra):
    """Words to scrub that only this machine knows: hostname, FQDN pieces, local users, anything the user adds."""
    words = set(extra)
    u = os.uname().nodename
    words |= {u, u.split(".")[0]}
    try:
        for line in open("/etc/passwd"):
            f = line.split(":")
            if len(f) > 2 and f[2].isdigit() and int(f[2]) >= 1000 and f[0] != "nobody":
                words.add(f[0])
    except OSError:
        pass
    return sorted((w for w in words if w and len(w) > 2), key=len, reverse=True)


def scrub(text, words):
    for rx, rep in SECRETS:
        text = rx.sub(rep, text)
    for w in words:
        text = re.sub(rf"(?i)\b{re.escape(w)}\b", "<local>", text)
    return text


def failed_hop(run):
    """The hop that broke: the first not ok, else (every hop ok, the final checks failed) the last; None with no hops."""
    hops = run["hops"]
    return next((h for h in hops if not h["ok"]), hops[-1] if hops else None)


def gap_key(run):
    bad = failed_hop(run) or {"to": run.get("to", "?")}
    i = run["hops"].index(bad) if bad in run["hops"] else 0
    before = run["hops"][i - 1]["to"] if i else run.get("from", "start")
    stage = re.sub(r"[0-9][0-9.:+~-]*", "N", run.get("failed_stage", ""))
    raw = json.dumps([run["box"].split("/")[0], before, bad["to"], stage, sorted(run["final"]["failed"])])
    return hashlib.sha256(raw.encode()).hexdigest()[:12], before, bad["to"]


def build(run, extra_words=()):
    if run["result"] == "pass":
        raise SystemExit("this run passed; there is no gap to report")
    words = local_words(extra_words)
    key, before, hop = gap_key(run)
    fam = run["box"].split("/")[0]
    logs = {c: scrub(l, words) for c, l in run["final"].get("failed_logs", {}).items()}
    body = {
        "gap_key": key, "family": fam, "from_release": before, "hop": hop,
        "route": run["route"], "route_target": run.get("to", "?"),
        "fixes_fired_on_this_hop": (failed_hop(run) or {}).get("fired_names", []),
        "failed_stage": scrub(run.get("failed_stage", ""), words),
        "failed_checks": run["final"]["failed"],
        "start_fingerprint": run.get("start_fingerprint"),
        "services": run.get("start_services", []),
        "client": run.get("runner", {}).get("kind"),
    }
    md = [f"### Gap `{key}`: {fam} {before} → {hop}", "",
          f"- route: `{body['route'][:19]}` toward {body['route_target']}",
          f"- fixes that fired on this hop: {', '.join(body['fixes_fired_on_this_hop']) or 'none'}",
          f"- failed at: `{body['failed_stage']}`",
          f"- checks failing after rollback: {', '.join(body['failed_checks']) or 'none'}",
          f"- box fingerprint: `{body['start_fingerprint']}`", ""]
    if body["services"]:
        md += ["Services on the box at the start: " + ", ".join(body["services"]), ""]
    for c, l in logs.items():
        md += [f"<details><summary>{c} log</summary>", "", "```", l[-1500:], "```", "</details>", ""]
    return key, body, "\n".join(md)


def issue_url(key, body, md):
    title = f"gap: {body['family']} {body['from_release']} -> {body['hop']} [{key}]"
    q = {"template": "route-gap.yml", "title": title, "labels": "route-gap", "gap_key": key,
         "family": body["family"], "from_release": body["from_release"], "hop": body["hop"],
         "details": md}
    url = f"{REPO_URL}/issues/new?" + urllib.parse.urlencode(q)
    if len(url) > URL_BUDGET:
        q["details"] = md[:1500] + "\n\n(full report attached: drag wend-gap-" + key + ".md into this issue)"
        url = f"{REPO_URL}/issues/new?" + urllib.parse.urlencode(q)
    return url


def main(run_path, extra_words=()):
    run = json.load(open(run_path))
    key, body, md = build(run, extra_words)
    out = f"wend-gap-{key}.md"
    open(out, "w").write(md + "\n\n```json\n" + json.dumps(body, indent=1) + "\n```\n")
    print("=" * 72)
    print(open(out).read())
    print("=" * 72)
    print(f"That is everything in the report, also saved to {out}. Hostnames, IPs, emails, local users and "
          "anything that looked like a secret were replaced. Read it; if anything else is private, rerun with "
          "--redact WORD.\n\nNothing has been sent. To report the gap, open this link in a browser and submit:\n")
    print(issue_url(key, body, md))
    return 0
