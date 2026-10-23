# wend

Upgrade a 2007 server to today, one release at a time, along a path someone already proved works.

wend is for people running fleets of old servers on Linux releases that stopped getting updates years ago. A
distro's own upgrader moves the OS; wend moves the server: every hop carries the box's services and data along (web
server, PHP, database, DNS) and checks they still answer before the next one. Debian etch to trixie, Ubuntu trusty to
resolute and CentOS 5, 7 and 8 to AlmaLinux 10 have each passed on real installs in a VM, with a real reboot after
every hop.

This repo is **the map**: small, reviewed fixes for the places where a plain release upgrade breaks, and routes that
pin those fixes into release-to-release paths. Each route carries evidence (runs) that it was replayed from scratch on
real test boxes and every health check stayed green. A client (a static Rust binary, being built in `client/`)
fingerprints your machine, picks a route, and walks it. At each hop it applies the fixes whose preconditions hold on
*your* box, takes a snapshot first, and rolls back if a check fails. The client uses no model. A walk pinned to a
proven run (`--run`) fetches the exact bytes that run used, by sha256, from your own cache, wend's public package
archive (https://archive.fieldmouseworks.com, [docs/archive.md](docs/archive.md)) or your distro's mirrors, and
refuses anything that doesn't match. A `--live` walk takes today's mirrors unverified, and its run says so.

Five routes so far. Every box below passes its route's current version from scratch, with no model:

| Route | Fixes | Boxes | Runs |
|---|---|---|---|
| Debian 4.0 etch (2007) to Debian 13 trixie | 14 | `etch-web` (PHP web server), `etch-db` (MySQL/BIND) | CI |
| CentOS 5.11 (2014) to AlmaLinux 10 | 36 | `centos5-web` (web/MySQL), `centos5-dns` (DNS/FTP) | CI for `centos5-web`, local for `centos5-dns` |
| CentOS 7.9 to AlmaLinux 10 | 24 | `centos7-lamp`, `centos7-pyapp` (a Python 2.7 app on MariaDB) | local |
| CentOS 8.5 to AlmaLinux 10 | 20 | `centos8-lamp` | local |
| Ubuntu 14.04 trusty to 26.04 resolute | 8 | `trusty-web`, `trusty-web-updated`, `trusty-db` | local |

CI runs are attested by the gate. Local runs were filed before that rule (CONTRIBUTING.md, "How runs land"), and CI
replays of the same routes will replace them. The Debian route began as the dep-lab results. The CentOS 5 route
crosses yum to dnf, the /usr merge, two package-manager bootstraps and a vendor switch; Opus 5.5 found the path in all
5 of its dep-lab runs, and replays are free.

CI replays a route in a chroot whenever a change touches it, which proves the userland only. Kernels, bootloaders and
the first real boot are tested by the VM tier (`tools/vm.py`, SPEC.md "VM tier"), which the maintainer runs: each box
is installed by its release's own installer in a KVM guest, and every hop ends with a real reboot and the box's
checks against the live services. As of 2026-10-14, at the route versions recorded there, these boxes have passed it
from scratch on BIOS: `etch-web`, `etch-db`, `trusty-db`, `trusty-web-updated`, `centos5-web`, `centos7-lamp` and
`centos8-lamp`. All three Ubuntu boxes have also passed on UEFI. The results are on
[wend#13](https://github.com/FieldmouseWorks/wend/issues/13), [#84](https://github.com/FieldmouseWorks/wend/pull/84)
(UEFI) and [#92](https://github.com/FieldmouseWorks/wend/pull/92) (CentOS 8).

**Agents and contributors: start with [AGENTS.md](AGENTS.md).** Clone, plug in any model, discover a path, prove it
with the same command CI runs, and open a PR.

> **Early days.** The fix format (`wend.fix/0`) now covers two package managers: ops are typed, and each family's
> adapter names a backend (apt or rpm) that compiles them. Open questions remain (see
> [SPEC.md](SPEC.md#open-questions)); fixes written now will be migrated by a script, not abandoned.

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
6. **Merge.** CI replays the routes it touches, remi proves it on real installs when a chroot can't judge it
   (CONTRIBUTING.md, `remi-proof`), Codex's review is settled, and it merges itself. The issue
   closes, and the reporter is pinged to retry.

Every gap that gets closed makes the map work on one more kind of machine.

## Layout

```
SPEC.md                      the format: fix, route, run, box, adapters, tiers, open questions
AGENTS.md                    the playbook: claim, box, model, discover, self-review, verify, PR
CLAUDE.md                    points at AGENTS.md
CONTRIBUTING.md              the five-layer PR gate, for humans and agents
wend                         the one command: check, list, replay, discover, report, gaps, verify
wanted.toml                  paths people want; claim them via their issue
prompts/discover.md          the exact question a model gets when a hop breaks
NAMES.md                     why "wend"
LICENSE-MIT, LICENSE-APACHE  dual license
adapters/
  debian.toml, ubuntu.toml   apt families: ladder, mirrors, how a hop runs, predicates, writable allowlist, ops, checks
  el.toml                    Enterprise Linux: CentOS 5, 7 and 8 to AlmaLinux 10, the rpm backend, the mirror shim
  drivers/<family>/          each release's driver table (SPEC "Hardware preflight")
  leapp/el/                  AlmaLinux's leapp-data, vendored (SPEC "Package evolution data")
fixes/<family>/<hop>/<name>.toml          14 debian, 46 el, 8 ubuntu
routes/<family>/<from>-to-<to>.toml       the five routes above
boxes/<family>/<name>/box.toml            CI fixtures, trusted test code, never shipped
runs/<family>/<route>/<box>/<stamp>.json  new runs land from CI only: the gate refuses a run it didn't attest
archive/allowed_signers                   the archive's public signing key (docs/archive.md)
tools/
  check.py           schema check, content ids, decoy check, --repin, --compile
  backends.py        what each typed op compiles to: apt (Debian, Ubuntu) and rpm (EL)
  mirror.py          http -> https cache for old boxes (CentOS 5's yum can't do today's TLS; trusty's apt lacks https)
  sandbox.py         chroot boxes: build, snapshot, roll back, private /dev
  walk.py            walk a route on a box (the map walk), write the run
  vm.py              the VM tier: replay a route on a real install in a KVM guest, a real reboot after every hop
  discover.py        a hop broke: ask the model, check the answer like a PR, try it
  report.py          a hop broke and you won't fix it: scrubbed gap report + prefilled issue link
  models.py          bring your own model: anthropic, any OpenAI-compatible server, or the agent itself
  affected.py        which routes and boxes a change must replay (CI matrix, ./wend verify)
  file_runs.py       files CI's run artifacts
  pesgaps.py, leappdata.py   what leapp-data says changes at each EL hop, and its vendoring
  hwdata.py, runtimes.py     build the driver and runtime tables for hardware and runtime preflight
  shelter.py, podman_standin.sh   sheltering an app (SPEC "Sheltering an app"), chroot tier
  net_*.py           reference implementations of the network ops
  archive.py, fetched.py, gate_fetched.py, r2push.py   the archive: capture, store, publish
  tomlw.py           writes TOML (tomllib only reads)
  port_oldlinux.py   one-off import of the dep-lab Debian results, with a lossless round-trip proof
  test_*.py          the tools' own tests; test_check.py holds the broken fixes the gate must refuse
docs/validation/     the first end-to-end test: verify on both boxes, and Opus refilling a deleted fix
docs/try-it.md       for a machine's owner: get the client, run `wend share` (read-only), post the report
docs/archive.md      design: the wend archive (wend#19), every byte proven routes reference, by sha256
docs/rebuild-and-carry.md  design: moving a machine whose filesystem a later kernel can't mount (old-XFS CentOS 7)
client/              the Rust client (client/README.md): reads, checks and compiles the atlas exactly as tools/
                     does (client/parity.py proves it), walks a route on a root filesystem, and runs hardware and
                     runtime preflight. Fetching by digest is the current slice. It doesn't walk the running
                     system yet: that needs a snapshot backend. No AI.
.github/
  workflows/gate.yml the PR gate: dco, schema, plan, replay, decoys
  workflows/client.yml the Rust client: build, test, parity with tools/
  workflows/gaps.yml the gap loop: dedupe reports, triaged gap -> wanted.toml PR, ping the reporter on close
  workflows/release.yml on a version tag: the static client and the atlas it reads, as a release download
  workflows/remi-proof.yml the remi-proof status: holds PRs a chroot can't judge until remi passes their head
  pull_request_template.md, ISSUE_TEMPLATE/route-gap.yml, ISSUE_TEMPLATE/box-report.yml,
  ISSUE_TEMPLATE/wanted.md, dependabot.yml
  CODEOWNERS         the owner signs off on .github/ (the gate itself); the rest merges once settled
```

## Try it on your server

Got an old CentOS, Debian or Ubuntu machine? `sudo ./wend share`, from the
[latest release](https://github.com/FieldmouseWorks/wend/releases/latest), reads it without changing anything, says
whether its release's route would get it to the end, and writes a report with the hostname, users and addresses
scrubbed, plus a prefilled issue link. [docs/try-it.md](docs/try-it.md) says what it reads and what it scrubs.

## Try the map

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
