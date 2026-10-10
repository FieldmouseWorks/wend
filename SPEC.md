# wend format spec, draft 0

Status: draft, 2026-10-10. Written from the dep-lab Debian runs (etch 2007 to trixie, two boxes, 12 fixes). The
CentOS 5 to AlmaLinux 10 run is still in progress; everything it might change is listed under
[Open questions](#open-questions). `tools/check.py` is the executable copy of this document.

`wend` is the working name (see NAMES.md). Swapping it is a find-and-replace.

## The four things

| thing | what it is | who writes it | format | where it lives |
|---|---|---|---|---|
| **fix** | one small change, plus the rule for when it applies | contributors (usually a model, curated by a human) | TOML | `fixes/<family>/<hop>/<name>.toml` |
| **route** | a release-to-release path and the set of fixes pinned to it | contributors | TOML | `routes/<family>/<from>-to-<to>.toml` |
| **run** | evidence that a route worked (or didn't) on one box | CI only | JSON | `runs/<family>/<route>/<box>/<stamp>.json` |
| **box** | a test machine: a starting release, packages, local config, health checks | maintainers | TOML | `boxes/<family>/<name>/box.toml` |

Plus one **adapter** per distro family (`adapters/<family>.toml`), which holds everything family-specific.

Fixes and routes are *source*: you review them. Runs are *proof*: you don't edit them, CI writes them. Boxes are
*test code*: they can contain arbitrary shell, because only CI runs them and humans review every change. Clients never
see a box.

## Fix

```toml
schema = "wend.fix/0"
family = "debian"
hop = "wheezy"                       # the release this fix helps you arrive at
when = { installed = "libdb4.2" }    # precondition, checked on the box just before the hop
restores = ["pkgdb"]                 # checks this fix exists to keep green
adds = []                            # service families it may introduce (for the services check)

[[post]]                             # steps after the upgrade transaction ("pre" = before it)
op = "force-remove"
packages = ["libdb4.2", "libdb4.3", "libdb4.4", "libdb4.5"]
purge = true
force = ["depends"]

[[post]]
op = "repair"

[meta]                               # not part of the id
name = "purge-stale-libdb4"
author = "claude-haiku-5-5 (guided runs)"
curated_by = "claude (dep-lab)"
fires_on = ["debian/etch-db", "debian/etch-web"]
```

### How a hop runs

For each hop on a route, the harness (not the fix) does:

1. set the release (adapter `hop.set_release`; harness-owned files are rewritten),
2. refresh the indexes,
3. the `pre` steps of every fix whose `when` held, in route order,
4. the upgrade transaction (Debian: `dist-upgrade`, then `autoremove --purge`),
5. the `post` steps of those fixes, in route order,
6. all health checks. Any failure rolls the hop back.

Every `when` is evaluated **once, before step 1**, on the box as it is then. A fix can't observe another fix's effect
on the same hop. Ordering is route order. (Open question 6.)

### Preconditions

A precondition is a table with exactly one key:

| predicate | means | Debian | RPM (proposed) |
|---|---|---|---|
| `installed = "P"` | P is installed **now** | dpkg state `ii`; `rc` does not count | `rpm -q P` |
| `file = "/etc/..."` | that file exists | `test -f` | same |
| `file-contains = { path, text }` | the file contains the literal text | `grep -qF` | same |
| `not = {...}` | negation | | |
| `all = [...]`, `any = [...]` | and, or | | |

Paths are under `/etc`. There is no shell predicate.

**The install rule.** A fix that has an `install` step must be gated, positively, on `installed` of the package it
replaces. This is the stray-MySQL lesson from dep-lab round 2. A fix gated on `/etc/mysql/my.cnf` installed a MySQL
server on a web box, because PHP had pulled in mysql-common. The checker enforces the shape of the rule (there is a
positive `installed`). Only review and decoy boxes can enforce the meaning (it's the *right* package).

### Op vocabulary

The core ops are family-neutral. The adapter compiles them.

| op | args | Debian compiles to |
|---|---|---|
| `install` | `packages` | `apt-get install P...` |
| `remove` | `packages`, `purge` | `apt-get purge` / `apt-get remove` |
| `repair` | | `apt-get -f install` |
| `force-remove` | `packages`, `purge`, `force` ⊆ {`depends`, `remove-essential`} | `dpkg --purge --force-depends ...` |
| `write-file` | `path`, `content` | write the whole file. Only paths on the adapter's `writable` allowlist |
| `pm-option` | `option` ∈ {`overwrite-conflicting-files`} | a hop-scoped `apt.conf.d` drop-in |

Adapter ops are named `<tool>.<verb>`. Debian has `apache.enable` and `apache.disable` (`kind` = site/mod/conf,
`name`). They compile to `a2en*`/`a2dis*`.

Rules that keep this from being shell with extra steps:

- An op takes exactly its listed args. Extra keys are refused.
- Package names match `[a-z0-9][a-z0-9.+-]*`.
- `write-file` content has no `'` or `%`. The path must match the adapter's `writable` globs, one directory level
  only. The globs are an allowlist, not "anything under /etc". Cron, systemd units, `/etc/default`, sudoers,
  profile.d, logrotate and apt.conf.d all execute code or grant trust. Growing the list is a spec change.
- Harness-owned paths (sources.list, the harness apt config, yum.repos.d on RPM) are never writable.

The Debian seed library uses only these ops, and it compiles back byte-for-byte to the commands dep-lab ran
(`tools/port_oldlinux.py` proves this on every port).

### restores and adds

- `restores`: adapter checks (`pkgdb`, `current`, `services`) or `box:<role>` (a check a box defines, such as
  `box:web`). This is documentation for reviewers and a hint for model authors. It isn't enforced, because health is
  always checked in full.
- `adds`: the name families a fix is allowed to bring in as new explicitly installed services. The `services` check
  rejects any other new family. Empty for every seed fix. Switching mysql to mariadb isn't new, because the family map
  treats them as the same.

### Identity

`id = "sha256:" + hex(sha256(canonical_json(fix without [meta])))`

Canonical JSON means keys sorted, no whitespace, UTF-8, the way `tools/check.py` does it. (A full RFC 8785 is
pointless while values are only strings, booleans and lists.) Consequences:

- Renaming a fix or editing its notes does not change its id. Changing what it does always does.
- Two people who find the same fix produce the same id, and the checker refuses the duplicate.
- Display uses the first 12 hex digits (`sha256:0a151144716a`).

### meta

Free-form, except:

- `name`: a lowercase slug that matches the filename.
- `fires_on`: the boxes this fix is expected to fire on. CI holds fixes to it in both directions (see Decoys).
- `superseded_when`: an older precondition kept for history. Ported fixes keep v2's loose `when` here.

Provenance lives in `meta` (`author`, `curated_by`, `source`, and run references). The signed proof is the run, not
the author's say-so.

## Route

```toml
schema = "wend.route/0"
family = "debian"
arch = "amd64"
from = "etch"
to = "trixie"
hops = ["lenny", "squeeze", ..., "trixie"]   # every release in between, in order: no skipping (yet)
finish = { prune_obsolete = true }           # after the last hop, purge packages the target no longer ships

[[fixes]]
id = "sha256:0a151144716a..."
name = "purge-stale-libdb4"                  # must agree with the fix's meta.name
```

A route is a **map, not a playlist**. It pins a *set* of fixes by id, and the client applies whichever ones hold on
each hop. In dep-lab, stitching fixes from three different runs into one map beat replaying any single run, and the
same map served two different boxes. Which fixes actually fired on which box is recorded in runs, not in the route.

Pinning by id makes "affected routes" mechanical. Editing a fix changes its id. The route then points at an id that
doesn't exist, so the PR has to update the route too, and CI replays every route the PR touches.

`id(route)` is computed the same way as a fix's id.

**prune_obsolete** is a no-model cleanup. It purges packages that the target release doesn't ship, bisecting
whenever a health check objects. It's defined by the harness, not by fixes.

## Run

Written by CI. JSON, because no human edits it.

```json
{
 "schema": "wend.run/0",
 "route": "sha256:2b355e1d1143...",
 "box": "debian/etch-db",
 "result": "pass",
 "started": "20261010-005243",
 "wall_seconds": 843,
 "runner": {"kind": "github-actions", "image": "ubuntu-24.04", "run_url": "...", "harness": "<commit>"},
 "hops": [{"to": "squeeze", "fired": ["sha256:720c..."], "fired_names": ["reinstall-mysql-server-5.1-after-hop"],
           "ok": true, "seconds": 146.1}],
 "final": {"release": "13.7", "passed": ["cron", "current", "db", "..."], "failed": []},
 "obsolete_after": 0,
 "start_fingerprint": "sha256:...",
 "model": "anthropic:claude-opus-5-5 (effort medium)", "model_calls": 2, "discovered": ["..."],
 "log_sha256": "sha256:..."
}
```

A discovery run records which model was used and how many calls it made. Replays record `model_calls: 0`. CI only
ever replays.

Later (open question 9): a Sigstore/GitHub artifact attestation over the run file. That's what proves CI wrote it.

**Fingerprint.** `sha256(canonical_json({family, release, arch, installed: sorted names of installed packages}))`
is an exact match. For "has this route been proven on a box like mine", the client compares name families (versions
stripped, renames mapped) by Jaccard similarity against the start states of the boxes that have passing runs. It's a
hint, never a gate: the preconditions do the real matching.

## Gap report

What `wend report` builds from a failed run, and the only thing a user ever sends upstream:
`gap_key`, `family`, `from_release`, `hop`, `route`, `route_target`, `fixes_fired_on_this_hop`, `failed_stage`,
`failed_checks`, `start_fingerprint`, `services` (init scripts present at the start), the failing checks' log tails,
and `client`. The logs are scrubbed of hostnames, IPs, MACs, emails, `/home/<user>` paths, local user names, private
keys, and anything shaped like `password=`/`token=` or a known token prefix. The user sees all of it before anything
leaves.

`gap_key = sha256(json([family, from_release, hop, failed_stage with numbers stripped, sorted failed_checks]))[:12]`
goes in the issue title. Same key, same gap: repeat reports become +1s.

## Box

```toml
schema = "wend.box/0"
family = "debian"
release = "etch"
arch = "amd64"
packages = ["openssh-server", "apache2", ...]
customize = '''shell run once in the fresh chroot'''
start_hook = '''records the starting service families, outside /etc'''
[roles]                       # box-specific health checks, shell; adapter checks are added automatically
web = "apache2ctl -t && ..."
```

### Decoys

Every box is a decoy for every fix that doesn't claim it. On a passing run, each pinned fix must have fired on
exactly the boxes in its `fires_on`. If it fired somewhere unclaimed, it's a decoy hit and the PR fails. If it didn't
fire where it claims, that fails too: the claim is stale or the precondition broke. dep-lab's round 3 is the
reference case. Box A must not touch the three MySQL fixes, and box B must not touch the six Apache/PHP ones. The
checker enforces this over the runs it has. CI produces fresh runs.

## Adapters

`adapters/<family>.toml` holds the ladder, release mirrors, the per-hop harness commands, how predicates evaluate,
the `writable` allowlist, `pm_options`, the adapter ops, and the universal checks. A fix never mentions apt, dpkg,
yum or rpm directly. If it needs to, that's a new op in an adapter.

### Debian (done)

See `adapters/debian.toml`. Notes that matter:

- `installed` means state `ii`. Removed-but-configured packages keep their config, so `dpkg -s` alone was a bug.
- The `services` check needs "explicitly installed". apt has it in `/var/lib/apt/extended_states`.
- `current`: every binary a service runs comes from a package that the release in sources.list still ships
  (`apt-cache madison`). This closed the "PHP 5 still serving on trixie" hole, without per-release version tables.

### RPM (placeholder, waiting on the CentOS thread)

What the CentOS harness (`dep-lab/centos/co.py`) already shows:

- The ladder crosses vendors: `c5 c6 c7 a8 a9 a10` (CentOS 5.11 to AlmaLinux 10). Release ids need a vendor, so a
  family isn't the same thing as a distro.
- The harness owns `/etc/yum.repos.d`, and clears vendor .repo files that point at dead mirrors. That's equivalent
  to sources.list.
- The package manager itself changes mid-ladder (yum through 7, dnf from 8).
- No EL release has an in-place path through a chroot (Leapp and redhat-upgrade-tool reboot), so every hop is a plain
  transaction, likely `distro-sync --allowerasing`.
- "Explicitly installed" doesn't exist before yum 3 on EL6. The draft services check uses "nothing requires it"
  instead.

## Sandbox requirements (for any harness: CI, the verifier, the authoring lab)

- **Never bind-mount the host's `/dev` into a box.** If a run is killed, the mount can leak, and a later
  `rm -rf` of the box deletes the host's device nodes through it. This happened in the CentOS thread. Give each
  chroot its own tmpfs `/dev` containing only `null`, `zero`, `random`, `urandom` and `tty` (created with mknod),
  plus a `devpts` mounted with `newinstance`.
- `/proc` and `/sys` are per-chroot mounts as well, never recursive binds.
- **Unmount and verify before any cleanup.** Before deleting or rsync-restoring a box root, unmount everything under
  it and check `/proc/self/mountinfo` for leftovers. If anything is still mounted, refuse to delete.
- Daemons never start on their own (`policy-rc.d` on Debian). Health checks start what they need and stop it again.
- A rollback restores the whole root from the pre-hop snapshot. Nothing from a failed hop survives.

## Open questions

These are the ones RPM may answer. Each one is reconciled when the CentOS thread posts results.

1. **Op vocabulary.** Does RPM need ops that Debian didn't? Likely candidates are `rpm --rebuilddb` (the rpmdb
   moved from BDB to sqlite in EL9), dnf module streams (`module enable php:8.2` on EL8), and `swap` (dnf's atomic
   replace, versus install + remove). Does `force-remove` map cleanly to `rpm -e --nodeps`?
2. **pm-option.** `overwrite-conflicting-files` on RPM is `--replacefiles`. Is that a hop-scoped option there too,
   or per transaction?
3. **Config files.** Debian keeps local config (`--force-confold`). RPM writes `.rpmnew`/`.rpmsave` next to it.
   Do fixes need an op to adopt a `.rpmnew`, or is `write-file` enough?
4. **The `installed` predicate.** `rpm -q` has no `rc` state, but it does have multiple installed versions
   (kernels, multilib). Does `installed` need an arch or version qualifier?
5. **Release ids across vendors.** Is `rpm:c7` → `rpm:a8` one ladder, or two families joined by a bridge hop
   (like AlmaLinux's ELevate)? Does a route ever skip releases?
6. **Ordering and interaction.** All `when`s are evaluated before the hop, and steps run in route order. Is there
   ever a fix that needs another fix's effect on the same hop? Debian never did.
7. **Parameterized fixes.** `apache24-site-conf` is gated on one site's file (`sites-available/mysite`). It's a
   local fix, not a universal one. A useful version would be "for every 2.2 vhost with no `.conf`, write the 2.4
   form". Do we add a `for-each` over files, or keep local fixes out of the shared library?
8. **Client-side checks.** CI boxes have rich roles. A real machine has only the adapter checks plus whatever the
   user supplies. What is the minimum check set a client runs before it calls a hop healthy?
9. **Signing.** Sigstore attestation from GitHub Actions on each run file, or a project key held by the verifier
   box? The client trusts a run only if it is attested and refers to the route id it is about to apply.
10. **Booting.** A chroot can't test init. `drop-sysvinit-for-trixie` is fine in a chroot and dangerous on a real
    machine without a check that init is systemd. Do some fixes need a `requires_boot_check` flag that CI can't
    satisfy yet?
