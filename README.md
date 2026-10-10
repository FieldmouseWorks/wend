# wend

Upgrade a 2007 server to today, one release at a time, along a path someone already proved works.

This repo is **the map**: small, reviewed fixes for the places where a plain release upgrade breaks, and routes that
pin those fixes into release-to-release paths. Each route carries evidence (runs) that CI replayed it from scratch on
real test boxes and every health check stayed green. A client (a static Rust binary, later) fingerprints your
machine, picks a route, and walks it. At each hop it applies the fixes whose preconditions hold on *your* box, takes
a snapshot first, and rolls back if a check fails. The client uses no model and needs no network beyond your
distro's mirrors.

Seed content: the dep-lab Debian results. 12 fixes take Debian 4.0 etch (2007) to Debian 13 trixie. Two very
different boxes, a PHP web server and a MySQL/BIND server, each made it with no model and no rollbacks in 13–15
minutes, and ended with zero obsolete packages.

**Agents and contributors: start with [AGENTS.md](AGENTS.md).** Clone, plug in any model, discover a path, prove it
with the same command CI runs, and open a PR.

> **Early days.** The fix format (`wend.fix/0`) may still change once the RPM (CentOS 5 → AlmaLinux 10) results
> land. See [SPEC.md, open questions](SPEC.md#open-questions). Fixes written now will be migrated by a script, not
> abandoned.

## The loop

The map grows from people using it:

1. **Use.** Someone runs the client on their old box. It walks a route and applies the fixes that match.
2. **Gap.** A hop breaks. The client rolls the box back, so nothing is lost, and stops.
3. **Report.** `wend report <run file>` builds a gap report from the failed run: which hop, which check, the log
   tail. It scrubs hostnames, IPs, emails, local users and anything that looks like a secret, **shows the whole
   thing**, and prints a prefilled GitHub issue link. Nothing is sent until the user opens that link and clicks submit.
   No model, no token, no account linking.
4. **Triage.** The `gaps` workflow merges repeat reports: the same gap key becomes a +1 on one issue. A maintainer
   labels the gap `triaged`, and a bot opens a PR adding it to `wanted.toml`.
5. **Discover.** An agent claims it (AGENTS.md), builds or picks a box like the reporter's, runs `wend discover`,
   and opens a PR that says `Closes #N`.
6. **Merge.** CI replays it on every box and a human merges. The issue closes, and the reporter is pinged to retry.

Every gap that gets closed makes the map work on one more kind of machine.

## Layout

```
SPEC.md                      the format: fix, route, run, box, adapters, open questions
AGENTS.md                    the playbook: claim, box, model, discover, self-review, verify, PR
CLAUDE.md                    points at AGENTS.md
CONTRIBUTING.md              the five-layer PR gate, for humans and agents
wend                         the one command: check, list, replay, discover, verify, report
wanted.toml                  paths people want; claim them via their issue
prompts/discover.md          the exact question a model gets when a hop breaks
NAMES.md                     why "wend"
LICENSE-MIT, LICENSE-APACHE  dual license
adapters/
  debian.toml                ladder, mirrors, how a hop runs, predicates, writable allowlist, adapter ops, checks
  rpm.toml                   (waiting on the CentOS 5 -> AlmaLinux 10 run)
fixes/<family>/<hop>/<name>.toml
  debian/wheezy/purge-stale-libdb4.toml   ...12 seed fixes
routes/<family>/<from>-to-<to>.toml
  debian/etch-to-trixie.toml
boxes/<family>/<name>/box.toml            CI fixtures, trusted test code, never shipped
  debian/etch-web/  debian/etch-db/
runs/<family>/<route>/<box>/<stamp>.json  written by CI only
tools/
  check.py           schema check, content ids, decoy check, --repin, --compile
  test_check.py      broken fixes the gate must refuse
  sandbox.py         chroot boxes: build, snapshot, roll back, private /dev
  walk.py            walk a route on a box (the map walk), write the run
  discover.py        a hop broke: ask the model, check the answer like a PR, try it
  report.py          a hop broke and you won't fix it: scrubbed gap report + prefilled issue link
  models.py          bring your own model: anthropic, any OpenAI-compatible server, or the agent itself
  affected.py        which routes and boxes a change must replay (CI matrix, ./wend verify)
  file_runs.py       files CI's run artifacts
  tomlw.py           writes TOML (tomllib only reads)
  port_oldlinux.py   one-off import of the dep-lab Debian results, with a lossless round-trip proof
docs/validation/     the first end-to-end test: verify on both boxes, and Opus refilling a deleted fix
client/              TODO: Rust. fingerprint, match, snapshot, replay, roll back. No AI, offline.
.github/
  workflows/gate.yml the PR gate: dco, schema, plan, replay, decoys, claude-review
  workflows/gaps.yml the gap loop: dedupe reports, triaged gap -> wanted.toml PR, ping the reporter on close
  pull_request_template.md, ISSUE_TEMPLATE/route-gap.yml, ISSUE_TEMPLATE/wanted.md
  CODEOWNERS         a human on everything; the owner on tools/, adapters/, SPEC.md, .github/
```

## Try it

```
python3 wend check                  # schema check plus the gate's own tests, no root needed
python3 wend list
sudo python3 wend replay routes/debian/etch-to-trixie.toml debian/etch-web   # ~15 min, needs root + debootstrap
```

Standard library only, Python 3.11+.

## License

Everything here, code and content alike (fixes, routes, boxes, runs), is
dual-licensed under **MIT OR Apache-2.0**, at your option. That's the Rust ecosystem norm, and Apache adds an
explicit patent grant. See `LICENSE-MIT` and `LICENSE-APACHE`. Contributions are accepted under the same terms, with
a DCO sign-off (see CONTRIBUTING.md).
