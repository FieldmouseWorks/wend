# wend format spec, draft 0

Status: draft, 2026-10-11. Written from the dep-lab runs: Debian (etch 2007 to trixie, two boxes, 12 fixes) and
Enterprise Linux (CentOS 5.11 to AlmaLinux 10, two boxes, 22 fixes). What the second family settled is under
[Open questions](#open-questions).

This document is the format. It is meant to stand on its own: everything a tool needs to read a fix, a route or an
adapter, check it, and apply it is defined here, including what every op does. wend's own tools (`tools/` in this
repo) are one implementation of it. Where they disagree with this document, they have a bug. Another tool that
reads these files and does what this document says is just as valid a client.

The key words "must", "must not" and "may" are normative. Shell commands given as a backend's **reference** are the
commands wend runs. Another implementation may run different commands as long as the effect and the failure
conditions are the same.

`wend` is the working name (see NAMES.md). Swapping it is a find-and-replace.

## The four things

| thing | what it is | who writes it | format | where it lives |
|---|---|---|---|---|
| **fix** | one small change, plus the rule for when it applies | contributors (usually a model, curated by a human) | TOML | `fixes/<family>/<hop>/<name>.toml` |
| **route** | a release-to-release path and the set of fixes pinned to it | contributors | TOML | `routes/<family>/<from>-to-<to>.toml` |
| **run** | evidence that a route worked (or didn't) on one box | CI only | JSON | `runs/<family>/<route>/<box>/<stamp>.json` |
| **box** | a test machine: a starting release, packages, local config, health checks | maintainers | TOML | `boxes/<family>/<name>/box.toml` |

The **atlas** is all of it together: every adapter, fix, route and run in this repository, the thing a client
fetches and path-finds over (plus the boxes CI proves it on, which clients never see).

Plus one **adapter** per distro family (`adapters/<family>.toml`), which holds everything family-specific.

Fixes and routes are *source*: you review them. Runs are *proof*: you don't edit them, CI writes them. Boxes are
*test code*: they can contain arbitrary shell, because only CI runs them and every change is reviewed. Clients never
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
2. refresh the indexes (`hop.refresh`), then the kernel handover's first half (`hop.kernel_before`, see "Kernels
   and the bootloader"),
3. the `pre` steps of every fix whose `when` held, in route order,
4. the upgrade transaction: the arriving release's own `upgrade` list if the adapter gives one, else `hop.upgrade`
   (Debian: `dist-upgrade`, then `autoremove --purge`; EL: from `yum upgrade` on EL6 to
   `dnf distro-sync --allowerasing` on EL9 and 10), then the kernel handover's second half (`hop.kernel_after`). On a
   real machine, a release whose adapter says `reboot = "before-post"` boots its new kernel here,
5. the `post` steps of those fixes, in route order, then the harness appends their `adds` to
   `/var/lib/wend/added-families` (see "restores and adds"),
6. remove the drop-ins of any `pm-option` used on this hop,
7. `hop.verify` if the adapter has one (EL: the release file names the new major),
8. `hop.after`, whatever happened above (cleanup: package caches, the rpm database),
9. all health checks. Any command that fails in 1 to 7, or any check that passed before the hop and fails now,
   rolls the whole hop back.

Every `when` is evaluated **once, before step 1**, on the box as it is then. A fix can't observe another fix's effect
on the same hop. Ordering is route order. (Open question 6.)

The harness runs everything it does in a root (box build, every `when`, hop steps 1 to 9, health checks, prune) with
**umask 022**, set by the harness itself, never taken from the caller. Op references create files and directories
(`write-file` on a new path, `php.mysqli-report-off`, the work dirs) and rely on that: on a box whose root sessions
default to 077 (AlmaLinux 10's `/etc/login.defs`), php-fpm couldn't read the mysqli shim and every page of an
upgraded site was a 500 (seen replaying the EL route in a real VM, wend#13). A chroot run on CI inherits 022, so CI
alone never sees this. The rule covers only the work on the root: the files written for the person (run files, step logs, gap
reports) are written under the caller's umask, since they can hold check output the person hasn't scrubbed yet.

### Preconditions

A precondition is a table with exactly one key:

| predicate | means | Debian (reference) | EL (reference) |
|---|---|---|---|
| `installed = "P"` | P is installed **now** | dpkg state `ii`; `rc` does not count | `rpm -q P` (any version or arch) |
| `file = "/etc/..."` | that file exists | `test -f` | same |
| `file-contains = { path, text }` | the file contains the literal text | `grep -qF` | same |
| `mysql-option = { path, key, value? }` | the MySQL/MariaDB server's effective option `key`, read from `path` and its includes, is exactly `value` (no `value`: is set at all) | `my_print_defaults --defaults-file` with the groups `mysqld --verbose --help` lists | same |
| `not = {...}` | negation | | |
| `all = [...]`, `any = [...]` | and, or | | |

Paths match `/etc/[A-Za-z0-9_./-]+` and contain no `..`. `text` contains no `'`. `mysql-option` keys are lowercase and
dashed (`-` and `_` match alike); values match `[A-Za-z0-9_./:,@+-]*`. It asks the server's own option reader for
`path`, reading exactly the groups the installed server lists in `mysqld --verbose --help` (`mysqld server mysqld-5.0`
on MySQL 5.0; MariaDB adds `mariadb`, `mariadbd`, `galera` and their versioned forms), so comments, spacing, repeated
keys (the last one wins) and `!include` files resolve as the server reads that file; `loose-` and `skip-` prefixes are
not folded into the key; a substring test can't say "datadir is exactly the default". Where no server tools are
installed it is false, so its `not` is true there: pair a negated one with a positive one. Package names match
`[A-Za-z0-9][A-Za-z0-9._+-]*`. There is no shell predicate. An adapter gives each predicate's command as a template in
`[predicates]` (`{pkg}`, `{path}`, `{text}`, `{key}`, `{value}`, `{any}`); `not`, `all` and `any` are the shell's `!`,
`&&` and `||` over those.

**The install rule.** A fix that has an `install` step must be gated, positively, on `installed` of the package it
replaces. This is the stray-MySQL lesson from dep-lab round 2. A fix gated on `/etc/mysql/my.cnf` installed a MySQL
server on a web box, because PHP had pulled in mysql-common. The checker enforces the shape of the rule (there is a
positive `installed`). Only review and decoy boxes can enforce the meaning (it's the *right* package).

### Op vocabulary

Ops are typed: a name and a fixed set of arguments, never a command. The core ops are family-neutral, and each
adapter names the **backend** that carries them out (`backend = "apt"` or `"rpm"`). What every op does on every
backend is defined in the [Op reference](#op-reference) below. A fix can't change what an op does; only a change to
this document can. In this repo, CI always takes its implementation of the ops **and the adapters** from the base
branch, so a PR that changes either is reviewed as a spec change before it is ever run. A checker must also hold
every adapter to its backend (see [Adapter format](#adapter-format)): an adapter can narrow what fixes may do, never
widen it.

| op | args | apt (Debian) | rpm (EL) |
|---|---|---|---|
| `install` | `packages` | `apt-get install P...` | `yum -y install` / `dnf -y --allowerasing install` (vendor .repo files cleared first) |
| `remove` | `packages`, `purge` | `apt-get purge` / `apt-get remove` | `rpm -e` each one that is installed; fails if anything still needs it |
| `repair` | | `apt-get -f install` | remove packages from older releases that have unmet dependencies |
| `force-remove` | `packages`, `purge`, `force` | `dpkg --purge --force-depends ...`; `force` ⊆ {`depends`, `remove-essential`} | `rpm -e --nodeps ...`; `force` ⊆ {`depends`, `scripts`} |
| `write-file` | `path`, `content` | write the whole file. Only paths on the adapter's `writable` allowlist | same |
| `pm-option` | `option` | `overwrite-conflicting-files`: a hop-scoped `apt.conf.d` drop-in | none yet |

The table is a summary. The [Op reference](#op-reference) has the arguments, effects and failure conditions.

Adapter ops are named `<tool>.<verb>`. The adapter declares which ones its family offers and each one's arguments
(`[ops."<name>"].args`, see [Argument specs](#argument-specs)); the Op reference defines what they do.

- Debian: `apache.enable` and `apache.disable` (`kind` = site/mod/conf, `name`) compile to `a2en*`/`a2dis*`;
  `dpkg.reinstall` (`packages`) to `apt-get install --reinstall --only-upgrade`; Ubuntu also
  `net.ifupdown-dns-to-resolved` and `apt.drop-esm-pins` (`release`).
- EL: `rpm.bootstrap` (unpack EL6's rpm/yum stack over / with 7-Zip, because rpm 4.4 can't read xz payloads),
  `rpm.transit` (pass through EL8: EL8's rpm run from a staging dir, then a real distro-sync), `rpm.swap-release`
  (release packages by hand: epochs and vendor switches), `rpm.drop-arch` (multilib leftovers), `rpm.drop-unneeded`, `rpm.adopt-rpmnew`,
  `rpm.set-aside-unowned`, `fs.layout` (`usrmove`, `run`), `kv.set` / `kv.unset` (key=value lines),
  `mysql.set-password`, `mysql.clean-shutdown`, `mysql.upgrade-data`, `ssh.generate-host-keys`,
  `httpd.drop-missing-modules`, `php.mysqli-report-off`, `service.record`, `service.enable`, `net.ifcfg-to-keyfile`.

`rpm.bootstrap`, `rpm.transit` and `fs.layout` are surgery: dozens of shell lines each, replayed as root on a
stranger's box. They are exactly why ops are typed. The surgery is written once, reviewed as a spec change, and every
fix that needs it says only `{ op = "rpm.transit", release = "alma8", ... }`.

Rules that keep this from being shell with extra steps:

- An op takes exactly its listed args. Extra keys are refused.
- Package names match `[A-Za-z0-9][A-Za-z0-9._+-]*`: rpm names carry capitals and underscores (`NetworkManager`,
  `PolicyKit`), dpkg names never do.
- `write-file` content has no `'` or `%`. The path must match the adapter's `writable` globs, one directory level
  only (`*` never matches `/`). The globs are an allowlist, not "anything under /etc". Cron, systemd units, `/etc/default`, sudoers,
  profile.d, logrotate and apt.conf.d all execute code or grant trust. Growing the list is a spec change.
- EL has a second list, `managed`: config files that `rpm.adopt-rpmnew` and `rpm.set-aside-unowned` may move. Those
  ops never write content. They take the file the package shipped, or move a file no package owns, and keep the old
  one under `/var/lib/wend/saved`.
- Harness-owned paths (sources.list, the harness apt config, yum.repos.d and the yum/dnf config on EL) are never
  writable, and neither is anything under `/etc/wend/` (the box owner's secrets) on any family.
- No op takes a secret as an argument. A value a fix would need but must not publish (a password) is named, and the
  op reads it from `/etc/wend/secrets/<name>` on the box.
- A fix whose steps install packages (`install`, or `rpm.transit` with a non-empty `reinstall`) must pass the install
  rule above.

The Debian seed library uses only these ops, and its reference commands are byte-for-byte the commands dep-lab ran
(wend's port script re-checks this on every port). The EL library is dep-lab's CentOS fix library recut into
typed ops; its proof is the replay, since the cut changed the commands.

### restores and adds

- `restores`: adapter checks (`pkgdb`, `current`, `services`) or `box:<role>` (a check a box defines, such as
  `box:web`). This is documentation for reviewers and a hint for model authors. It isn't enforced, because health is
  always checked in full.
- `adds`: the name families a fix is allowed to bring in as new explicitly installed services. The `services` check
  rejects any other new family. Each value must be the family (`[A-Za-z_+]+`, see below) of a package the same fix
  installs (`install`, or a transit's `reinstall`), so a fix can't declare a family it doesn't itself bring in, and
  review sees the declaration next to the install it covers. The harness records them: after the post steps of a hop
  (step 5 above), it appends the `adds` of every fix that fired to `/var/lib/wend/added-families`, one per line, and a
  box's `services` check accepts those families as well as the ones the box started with. A failed hop's rollback
  takes its lines away with it. Both engines check the values again when they compile a hop, so no path puts anything
  but a family into that command. A human writes `adds`: discover drops whatever a model proposes for it.
  Switching mysql to mariadb isn't new, because the family map treats them as the same; for the same reason a
  family the map renames (`mariadb` to `mysql`) can't be declared new, since the record holds the family before the
  map and the box compares after it (open question 11).

**Name families.** A package's family is its name with every digit and `.` removed and everything from the first
`-` on dropped (`php5-mysql` and `php-mysqlnd` are both `php`), then mapped through the **family map**: renames a
route is known to cause, such as `mariadb` to `mysql`, `ntpsec` and `chrony` to `ntp`, `cronie` to `vixie`. Today
the map lives in each box's `services` check (a list of `sed` rules) and is the same for every box of a family.
Where it should live for clients, which have no box file, is open question 11.

### Identity

`id = "sha256:" + hex(sha256(canonical_json(fix without [meta])))`

Canonical JSON: the TOML document as JSON, object keys sorted by code point, no whitespace at all (separators `,`
and `:`), strings as UTF-8 with only `"`, `\` and control characters escaped (`\n`, `\t`, ... and `\u00XX` for the
rest). Values are only strings, booleans, integers, lists and tables, so a full RFC 8785 isn't needed. Consequences:

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
hops = ["lenny", "squeeze", ..., "trixie"]   # every release in between, in order (see may_skip below)
finish = { prune_obsolete = true }           # after the last hop, purge packages the target no longer ships

[[fixes]]
id = "sha256:0a151144716a..."
name = "purge-stale-libdb4"                  # must agree with the fix's meta.name
```

`hops` lists every release on the ladder after `from`, up to `to`. The one exception is a release the adapter
lists in `may_skip`: a route may leave it out. EL lists `alma8`, because the box only ever passes through EL8
(`rpm.transit`) on the hop to EL9, and `centos8`, where boxes start but no route lands. No health check runs on a
release the route skips. `wend discover` leaves `may_skip` releases out of a new route's hops.

A route starts on the ladder. A release off it can still be where boxes start, if the adapter says which rung it
**joins** (`releases.<id>.joins`): a rebuild of a release on the ladder, same major, same packages from another
vendor. EL's `sl7` (Scientific Linux 7) joins `centos7`, so an SL7 box takes every route from `centos7` and is a
decoy for its fixes; what SL does differently is handled by fixes gated on SL's own packages (`sl-release`). No hop
ever lands on a joined release, so it has no `upgrade`, `bootstrap`, `transit`, `reboot` or keys of its own.

A machine can also join a route **partway**: one sitting on a rung the route passes through, short of `to`, takes
the hops after that rung. A CentOS 6 machine takes `centos5-to-alma10` from `centos6` (centos7, alma9, alma10), a
Debian 10 one `etch-to-trixie` from `buster`. The client judges and walks only what's left: `wend share` offers
the rest of every route passing through when no route starts at the machine's release (and says those hops were
proven on machines that walked up from `from`, not ones installed on the rung), `wend preflight` on the machine
judges from its release, and `wend walk` starts from the release its checks name or its root's release files say,
refusing a route that doesn't pass through it. The fixes are the route's, pinned by the same id, and the run's
`from` is the rung the walk started at. A gate replay still starts every box at `from`.

A route is a **map, not a playlist**. It pins a *set* of fixes by id, and the client applies whichever ones hold on
each hop. In dep-lab, stitching fixes from three different runs into one map beat replaying any single run, and the
same map served two different boxes. Which fixes actually fired on which box is recorded in runs, not in the route.

Pinning by id makes "affected routes" mechanical. Editing a fix changes its id. The route then points at an id that
doesn't exist, so the PR has to update the route too, and CI replays every route the PR touches.

`id(route)` is computed the same way as a fix's id.

**prune_obsolete** is a no-model cleanup. It purges packages that the target release doesn't ship, bisecting
whenever a health check objects. It's defined by the harness, not by fixes.

## Run

Written by CI. JSON, because no human edits it. A run in `runs/` is a gate replay's artifact, byte for byte, with
a GitHub artifact attestation from `gate.yml`; the gate refuses any new or changed run file without one
(CONTRIBUTING.md, "How runs land"). Runs a contributor makes locally are evidence for the PR, never filed.

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

**Pinned sources.** Each hop records the repo metadata it ran against:

```json
"hops": [{"to": "alma9", "...": "...",
          "sources": {"https://repo.almalinux.org/almalinux/9/BaseOS/x86_64/os/repodata/repomd.xml": "sha256:9f2c...",
                      "https://repo.almalinux.org/almalinux/9/AppStream/x86_64/os/repodata/repomd.xml": "sha256:41ab..."}}]
```

A key is the origin URL of the repo's top metadata file (`dists/<suite>/Release` on apt, `repodata/repomd.xml` on
rpm), with any harness shim prefix stripped. A value is the sha256 of the bytes the hop's refresh read. That one hash
pins everything under it: `Release` lists the hash of every `Packages` index and `repomd.xml` the hash of
`primary.xml`, and those list a checksum for every package. So a run pins the exact package set its route proved
without listing thousands of packages, and a client can verify any package from any source against it (open
question 12 says how). One exception: apt 0.9 (wheezy) deletes the `Release` when the archive's signing key fails
and keeps only the indexes. Then the hop pins each index it kept, keyed by its URL
(`<mirror>/dists/wheezy/main/binary-amd64/Packages`), with the sha256 of the uncompressed bytes. The `Release` hashes the
same bytes, so a client fetches whichever compressed form the archive serves and checks it after decompressing.
A hop records only its own release's repos: yum and dnf leave the caches of disabled repos behind, so a cache from
an earlier hop says nothing about this one. An op that fetches through temporary repos the hop then drops
(`rpm.transit`) keeps their metadata under `/var/lib/wend/sources/<hop>/`, so the hop pins those too. A file fetched
outside any repo (`rpm.bootstrap`'s 7-Zip RPMs) is pinned by sha256 in the adapter: the op refuses other bytes, and
the hop records it by its URL. The harness writes `sources` from the files it refreshed. A run without them proves the
route against whatever the mirrors held that day and nothing more.

A run the client writes on a machine (`wend walk`) says how far to trust its `sources`: `"live": false` with
`"pinned_by": {"route", "box", "started"}` naming the run whose pins every download was checked against, or
`"live": true` when it was asked to walk today's mirrors with nothing verified (see "Moved mirrors" under open
question 12).

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
isolate = ["db"]              # roles whose checks start daemons: run them in a network namespace of their own
[roles]                       # box-specific health checks, shell; adapter checks are added automatically
web = "apache2ctl -t && ..."
[shelter.billing]             # optional: an app to shelter on a route that drops its Python 2 ("Sheltering an app")
code = "/opt/billing"
data = ["/var/lib/billing"]
user = "billing"
unit = "billing.service"
```

A box with Python 2 programs of its own may carry `[shelter.<app>]`: the plan its owner would have confirmed
(`wend preflight --shelter`), given to the walk as box input. "Sheltering an app" defines the keys; the checker holds
them to plain shapes, since every value lands in commands the harness runs as root.

### Decoys

Every box that starts at a route's `from` release (or at a release that joins it there, see Route) is a decoy for each of that route's fixes that doesn't claim it (a box
that starts elsewhere can't walk the route, so CI doesn't replay it there). On a passing run, each pinned fix must have
fired on exactly the boxes in its `fires_on`. If it fired somewhere unclaimed, it's a decoy hit and the PR fails. If it
didn't fire where it claims, that fails too: the claim is stale or the precondition broke. dep-lab's round 3 is the
reference case. Box A must not touch the three MySQL fixes, and box B must not touch the six Apache/PHP ones. The
checker enforces this over the runs it has. CI produces fresh runs.

## Adapters

`adapters/<family>.toml` holds the ladder, release mirrors, the per-hop harness commands, how predicates evaluate,
the `writable` allowlist, `pm_options`, the adapter ops, and the universal checks. A fix never mentions apt, dpkg,
yum or rpm directly. If it needs to, that's a new op in an adapter. The keys are defined in
[Adapter format](#adapter-format).

### Debian (done)

See `adapters/debian.toml`. Notes that matter:

- `installed` means state `ii`. Removed-but-configured packages keep their config, so `dpkg -s` alone was a bug.
- The `services` check needs "explicitly installed". apt has it in `/var/lib/apt/extended_states`.
- `current`: every binary a service runs comes from a package that the release in sources.list still ships
  (`apt-cache madison`). This closed the "PHP 5 still serving on trixie" hole, without per-release version tables.
- archive.debian.org has lost files (in 2026, some of every archived release's packages answer 404), so releases
  before bullseye come from one dated snapshot.debian.org tree, whose `Release` files are archive.debian.org's byte
  for byte. It answers with redirects, which apt before 0.7.21 (etch, lenny) can't follow, so it goes through the
  mirror shim (see Enterprise Linux below). Runs pin it by its origin URL, `https://snapshot.debian.org/...`.
- The harness owns `sources.list.d` too. Each hop sets third-party sources aside as `*.wend-disabled`: one built for
  the old release would pull packages across the hop, and one marked `trusted=yes` would get around a signed hop.

### Ubuntu (`ubuntu`, apt backend)

See `adapters/ubuntu.toml`. The Debian adapter's engine on Ubuntu's own ladder. Notes that matter:

- The ladder is LTS to LTS (`trusty xenial bionic focal jammy noble resolute`), the way do-release-upgrade goes.
  Interim releases are never stops.
- `release_file` is `/etc/lsb-release`: Ubuntu's `/etc/debian_version` names Debian testing, not the release. Each hop
  `verify`s `DISTRIB_CODENAME`.
- Only the release pocket, with all four components. It is frozen at release time, so a hop's pinned Release stays
  good; `-updates` and `-security` move and couldn't be replayed. The harness owns `sources.list.d` as well, and each
  hop sets third-party sources (PPAs) aside as `*.wend-disabled`, the way do-release-upgrade disables them.
- Noble needs a merged `/usr` (its libc6 preinst diverts `/lib64`), and noble no longer ships `usrmerge`, so the jammy
  hop installs jammy's `usrmerge` after its upgrade (`releases.jammy.upgrade`). A box is merged once, by the harness.
- xenial's `mysql-server-5.7` postinst runs `mysql_upgrade` against the server it just started, and stops and
  restarts it around an init file. Nothing starts in the sandbox, so when that configure fails, and only then, the
  xenial hop (`releases.xenial.upgrade`) finishes it with a stand-in `policy-rc.d` that starts and stops 5.7's own
  `mysqld` itself (socket only, `--skip-networking`) whenever the postinst asks for `mysql`, and still denies the init
  script. The server is down and the box's `policy-rc.d` is back before the hop goes on. The one place the sandbox
  runs a daemon during a transaction; a real machine never takes that branch.
- debootstrap knows every Ubuntu release as its `gutsy` script, but Ubuntu 24.04's own debootstrap package only
  links lunar and later, so the adapter names the script (`[box] debootstrap_script`).
- The VM tier (tools/vm.py) installs a trusty box with trusty's own debian-installer (`releases.trusty.install`), on
  BIOS or UEFI; all three boxes have passed trusty -> resolute on UEFI (wend#13, #84; Secure Boot isn't proven).

### Enterprise Linux (`el`, rpm backend)

See `adapters/el.toml`. Notes that matter:

- The ladder crosses vendors: `centos4 centos5 centos6 centos7 centos8 alma8 alma9 alma10`. A family isn't a distro; release
  ids carry the vendor. CentOS 8 and AlmaLinux 8 sit side by side: routes from EL7 and earlier skip both (EL8 is
  crossed inside the hop to EL9), and a route from CentOS 8 goes straight to EL9 with its own dnf. Scientific Linux 7
  (`sl7`) is off the ladder and joins it at `centos7` (see Route): its boxes take the CentOS 7 routes.
- The package manager changes mid-ladder (yum through 7, dnf from 8), and so does the upgrade verb: `upgrade` on EL6,
  `distro-sync` from EL7 (EL6's grep is newer than EL7's). Each release can set its own `upgrade` in the adapter.
- EL4 to EL5 runs EL4's yum 2.4 (`upgrade`; it has no distro-sync). Two of EL5's yum's own modules are installed as
  EL4's Python 2.3 builds at a version yum won't replace (python-elementtree, python-sqlite), and EL4's yum imports
  elementtree, so they can't go first: `releases.centos5.upgrade` drops their records only, the transaction installs
  EL5's as yum's dependencies, and the old builds' Python 2.3 files go after. Like the kernel, a harness step: a
  fix can't drop records.
- The harness owns `/etc/yum.repos.d` and clears the vendor .repo files every release package drops (they point at
  dead mirrors). After each hop it also rebuilds the rpm database and, on EL10, moves it to `/usr/lib/sysimage/rpm`.
- CentOS 5's yum can't speak today's TLS, so repos go through a mirror shim the harness runs on the host
  (`[mirror]` in the adapter): plain http on 127.0.0.1, fetching `https://<host>/<path>` for
  `http://127.0.0.1:<port>/<host>/<path>`, for a fixed list of hosts, and caching what it fetched. It also keeps
  every file it fetched under its sha256 (`.sha256/<hex>` in its cache, which no request can reach) and logs every
  file it serves (`.fetched.log`: `<https url> sha256:<hex> <size>`). A live repo's `repomd.xml` is refetched each
  time, so the store is the only place an older one survives. The shim answers `/.wend-shim` with its own source's sha256, and
  the harness reuses a shim already on the port only if that matches its own `tools/mirror.py`: one from another
  checkout would serve with its own allow-list, so the harness stops and says to `shim-stop` it. `wend replay --out RUN.json` turns the lines the
  replay added to the log into a download list beside the run, `RUN.fetched/`: `fetched.txt` (each distinct file,
  sorted) and `blobs/<hex>` (the bytes of every file in it that isn't a package, and of live EPEL's packages, which
  go when they're updated). The gate uploads it for the archive (`docs/archive.md`, "Capture").
- No EL release has an in-place path through a chroot (Leapp and redhat-upgrade-tool reboot), so every hop is a plain
  transaction plus fixes.
- On a live box the hop to EL9 runs its transaction offline, as a chroot does, and out of reach of PID 1. Otherwise
  systemd's `%post` re-executes PID 1 as EL9's systemd on EL7's 3.10 kernel, which never sees a stopped unit's cgroup
  empty, and the first scriptlet that starts or restarts a service (audit's) hangs the transaction (wend#75).
  `SYSTEMD_OFFLINE=1` turns the scriptlets' own systemctl calls into no-ops, but `service` runs legacy actions under
  `env -i`, which drops it (EL8 audit's `service auditd condrestart` hung the first VM proof of the fix), so dnf also
  runs in a private mount namespace with tmpfs over `/run/systemd` and `/run/dbus`: no scriptlet can find PID 1,
  whatever its environment. The old daemons, and the old PID 1, run on until the hop's reboot, except sshd: its
  listener re-executes `/usr/sbin/sshd` for every connection and can't hand its state to a newer binary. A watcher
  outside the namespace restarts it as soon as the transaction swaps the binary, and it is restarted again after
  `rpm.transit` and after the transaction; that step fails unless a new connection gets the ssh greeting. `hop.after`
  restarts it once more, so a hop that fails in between doesn't leave the machine locked.
- `installed` is `rpm -q`. "Explicitly installed" doesn't exist before yum 3 on EL6, so a box's services check uses
  "nothing requires it, by name or by any capability it provides" instead.
- `current`: the service binaries come from a package whose dist tag is the running major (`[._]el$MAJOR`).
- Boxes can name roles that start daemons in `isolate`; those checks run in a network namespace of their own.

#### Add-on repositories

Most real EL machines carry more than the vendor's repos: EPEL above all (EPEL's own mirror statistics are how the
stuck EL7 fleet is counted). The adapter's `[addons.<name>]` tables name the ones the harness knows how to carry:
`marker` (the package whose presence means the box uses it, EPEL: `epel-release`), `vendor` (the rpm `VENDOR` of its
packages, EPEL: `Fedora Project`), `repos` (release id = host/path, through the shim, like `releases.*.repos`) and,
optionally, `libs` (release id = the sha256 of the add-on's library table for that release, "Runtime preflight").
An add-on with no release package to mark it (nginx.org's, which owners set up with a hand-written repo file) leaves
`marker` out: it is installed when any installed package's `VENDOR` is exactly `vendor`. Below, "its marker is
installed" means that test for such an add-on. `module_hotfixes = true` adds `module_hotfixes=true` to each of the
add-on's repo sections: on EL8, dnf hides every package that shares a name with a default module stream's (AppStream's
nginx) in other repos unless their repo says so.

- Every hop's `set_release` appends `{addons}`: for each add-on with a repo for the release being entered, the add-on's
  repo goes into the harness's repo file as `[<release>-<name>]` if its marker is installed at that moment. A box
  without the marker never sees the repo: no hop's repos or `rpm.transit`'s `new` repos ship a marker, so no fix can
  pull add-on packages onto it. On a box with the marker, a fix's `install` can pull any of the add-on's packages,
  checked against the add-on's key for that release when `addons.<name>.gpgkeys` names one (see "Signed packages"),
  else unchecked (`gpgcheck=0` behind the shim).
- `rpm.transit` lets the add-on vendor's packages through its refusal when the marker is installed, and then requires,
  before it changes anything, that each of them has a package of the same name in the release's repos or the
  release's add-on repos (renames don't count). Anything without one is named and the op fails: a fix on the same hop
  has to remove or replace it first, the same way it would for the vendor's own dropped packages. After the download
  it checks again that each of them was actually fetched.
- The add-on repos' metadata is pinned in the hop's `sources` like the vendor's (repo ids `<release>-<name>` and, for
  `rpm.transit`'s `new`, the same `<release>-<name>`). The transit's `old` repos (used only to install EL7's dnf)
  get no add-on repos.
- An adapter with `[addons]` must put `{addons}` in `hop.set_release`, or it would carry an add-on through the
  transit and then drop it from the hops after.
- Software Collections, vendor SIG repos and other third-party repos are not add-ons: `rpm.transit` still refuses them.

#### Signed packages

The adapter's `[keys]` table holds vendors' signing keys, ASCII-armoured, by name (no `'`, `%` or backslash, since
they're written with `printf`). Every hop's `set_release` starts with `{keys}`, which writes each one to
`/etc/wend/keys/<name>` (the files stay on the machine afterwards). A release that lists keys in
`releases.<id>.gpgkeys` gets `gpgcheck=1` and `gpgkey=file:///etc/wend/keys/<name> ...` in each of its repo
sections, so its package manager refuses packages they don't sign (dnf imports them with `-y`). An add-on's
`gpgkeys` table (release id = key name) does the same for its repo for that release. `{drop_keys}` in `hop.after`
removes from the rpm database the keys dnf imported for earlier releases that `releases.<id>.drop_keys` names, by
short key id (8 lowercase hex digits, as in `gpg-pubkey-<id>`), with `rpm -e --allmatches`; a key the box doesn't
have is skipped. It runs in `hop.after` because a step of the hop can import them (`rpm.transit`'s dnf does). A newer rpm can refuse to load an older release's key (EL9's rpm can't load AlmaLinux 8's SHA-1
self-signed 2021 key) and warns about it on every verify, which fails the pkgdb check.

Packages that reach the box any other way are checked by `CHECKSIG` before anything uses them, when the release lists
keys: `rpm.transit`'s download (EL7's dnf and rpm 4.11 can't use signing subkeys, which AlmaLinux 8 signs with, so it
downloads with `--nogpgcheck`) and `rpm.swap-release`'s package (fetched with curl). `CHECKSIG` splits the release's
keys and its add-ons' keys for that release into one block each, reads them into a scratch keyring with the box's
`gpg2` (or `gpg`), and checks each package with `gpgv2` (or `gpgv`): its header+payload signature (signature header
tag 1002, over every byte from the main header to the end) if it has one, else its header-only signature (tag 268,
over the main header; newer EPEL 8 builds carry only this) and then the payload's sha256 against the signed header's
payload digest (tags 5092 and 5093, which must say sha256). It refuses, naming them, every file that isn't an RPM, has
neither signature, or doesn't check out against those keys, and refuses outright if a key won't read or there is no
gpg. Its shell variables all start with `_w`, so it leaves the op's own alone.
Releases that list none keep `gpgcheck=0`:
on EL, CentOS 5 to 8, whose keys can't check what later releases ship or whose rpm can't read the newer signatures.

On apt, a release marked `signed = true` is one whose archive signature still verifies against the keyring the
release before it ships (Debian from buster on, every Ubuntu release from xenial on, checked with `gpgv` against each
predecessor's keyring). Every hop's `set_release` starts with `{apt_conf}`, which writes the harness's apt config
for that hop: `hop.apt_conf` always, plus `hop.apt_unsigned` (unauthenticated installs and insecure repos allowed)
only on a hop into a release that isn't `signed`. So apt refuses packages the archive's signed index doesn't vouch
for from buster and xenial on, and older hops, whose keys have expired or were never shipped to the release before,
keep working unauthenticated. On a signed hop `{apt_conf}` also writes `/etc/apt/apt.conf.d/zzzz-wend-signed`, which
turns `AllowUnauthenticated`, `force-yes`, `AllowInsecureRepositories` and `AllowDowngradeToInsecureRepositories`
off after the box's own fragments, and stops the hop if `apt-config dump` still shows any of them on (set in
`/etc/apt/apt.conf`, which apt reads last); an unsigned hop removes that file. Box builds still use
`debootstrap --no-check-gpg`.

Transport: every Debian and Ubuntu archive is given as host/path, so the box fetches it as plain http from the mirror
shim on 127.0.0.1, and the shim fetches it from the archive over https (apt before 1.5 has no https of its own). That covers the unsigned hops too, where transport is the only
check. The shim refetches a live archive's `dists/` metadata on every request, as it does rpm `repodata/`, except
files named by their hash (`by-hash/`) and the dated snapshot.debian.org tree, which never change.

With `wend walk --run` every byte is checked against the run's pins anyway; signatures are what protects `--live`
walks and CI replays.

## Op reference

Every op below is defined by its arguments, its effect, and when it fails. A failing step fails the hop, and the
hop is rolled back. Each backend gives a **reference**: the commands wend runs. `{x}` in a reference is an argument
or a value from the adapter. Every value that comes from a fix has matched a strict pattern before anything runs, so
none carries a quote, a space or a `$`. (Adapter values are reviewed harness code, and some, like `{dnf_opts}`, do.)

Values every rpm reference uses, for the release `R` the hop arrives at:

- `SHIM`: the adapter's `[mirror].shim`; a repo URL is `SHIM/<repos value>`.
- `BASE`: the first repo of `R` (`releases.R.repos`, in file order).
- `PM_INSTALL`: `yum -y install` if `releases.R.pm` is `yum`, else `dnf -y --releasever=MAJOR --allowerasing
  --setopt=install_weak_deps=False --setopt=protected_packages= install`.
- `CLEAN_REPOS`: delete every `/etc/yum.repos.d/*.repo` except the harness's `ladder.repo`.
- `SAVED`: `/var/lib/wend/saved`. A file an op moves aside goes to `SAVED` + its original path. An existing saved
  file is never overwritten, so `SAVED` always holds the owner's original, however many hops touch the file.
- `PKGLIST(base)`: the file names of every package in a repo: fetch `base/repodata/repomd.xml`, take the
  `primary.xml.gz` it names, list its `<location href="Packages/...">` entries. Fails if the list is empty.

### Core ops

**`install`** `packages` (list of package names, at least one). Installs them from the release being entered, with
whatever dependencies they pull in. Fails if any can't be installed. The fix must pass the install rule.
- apt: `apt-get install {packages}` (the harness's apt config answers yes and keeps local config files).
- rpm: `CLEAN_REPOS; PM_INSTALL {packages}`.

**`remove`** `packages`, `purge` (boolean). Removes the packages that are installed; a package that isn't is not an
error.
- apt: `apt-get purge {packages}` if `purge`, else `apt-get remove {packages}`. As always with apt, packages that
  depend on them go too.
- rpm: for each package, `rpm -q P && rpm -e P`. Nothing else is removed: if another installed package still needs
  one, the step fails. `purge` changes nothing: rpm has no "keep config" removal.

**`repair`** (no arguments). Brings the package database back to consistent: no installed package with unmet
dependencies.
- apt: `apt-get -f install`.
- rpm: up to 8 rounds of: list the packages `rpm -Va --nofiles --nodigest --noscripts` reports as having unsatisfied
  dependencies, drop those whose release tag is the current major (`.elMAJOR`), and `rpm -e --nodeps --noscripts`
  the rest. Fails if the verify output is still not empty after that.

**`force-remove`** `packages`, `purge`, `force` (list, a subset of the adapter's `force`). Removes exactly these
packages, overriding the safety checks named in `force`. (dpkg skips a package that isn't installed; rpm fails.)
- apt: `dpkg --purge` (or `--remove`) with `--force-depends` / `--force-remove-essential` for each flag.
- rpm: `rpm -e` with `depends` = `--nodeps`, `scripts` = `--noscripts`. `purge` changes nothing. There is no
  `--justdb` flag for fixes: dropping a package from the database while its files stay would fake the `pkgdb` check.

**`write-file`** `path` (on the adapter's `writable` list), `content` (no `'` or `%`). Replaces the whole file with
`content`. Reference on both: `printf '{content}' > {path}`, with `\` and newlines escaped for printf.

**`pm-option`** `option` (a key of the adapter's `[pm_options]`). Writes the option's `content` to its `path` for
this hop only; the harness deletes the file after the post steps (step 6 of a hop). Debian's one option,
`overwrite-conflicting-files`, lets dpkg replace a file another package owns. EL has none.

### Debian ops

**`apache.enable`, `apache.disable`** `kind` (`site`, `mod` or `conf`), `name` (`[A-Za-z0-9_.-]+`). Enables or
disables one Apache site, module or config snippet. Reference: `a2en{kind} {name}` / `a2dis{kind} {name}`. Fails
if it doesn't exist.

**`dpkg.reinstall`** `packages` (a list of package names). Installs the current release's build of packages that are
already installed, as they are. A package an older dpkg unpacked without an md5sums file (a transitional package
whose .deb ships none, unpacked by squeeze's dpkg during the wheezy transaction) is flagged by wheezy's
`dpkg --audit` as needing a reinstall, which fails the `pkgdb` check; reinstalling it under the release's own dpkg
writes them. A package apt doesn't know fails the step. One the release no longer ships can't be reinstalled: apt
says so and carries on, so the step passes and the hop's `pkgdb` check is what fails; a fix purges such a package
instead. A package that isn't installed is skipped, never installed (`--only-upgrade`, apt 0.8 and later: squeeze
on). Reference: `apt-get install --reinstall --only-upgrade {packages}`.

**`apt.drop-esm-pins`** `release` (`[a-z]+`; Ubuntu). Removes the apt pins ubuntu-advantage-tools wrote for
`release`, for a box leaving it. u-a-t 19.7ubuntu0.2 (trusty-updates) writes
`/etc/apt/preferences.d/ubuntu-esm-infra-trusty`, a file no package owns, pinning its ESM source with
`Pin-Priority: never`; trusty's apt takes that, xenial's apt 1.2 refuses every command over it ("No priority (or
zero) specified for pin"), so after the xenial transaction autoremove and every later apt call fail (wend#47). The
op looks at `ubuntu-esm-{release}` and `ubuntu-esm-*-{release}` in `/etc/apt/preferences.d` and removes one only
when it is a plain file (not a symlink), `dpkg -S` names no package for it, every line is u-a-t's own (`Package: *`,
`Pin: release o=UbuntuESM..., n={release}`, `Pin-Priority: never`, a comment or a blank line) and each of those
three pin lines is there. Anything else is left, with a line saying why, and the step passes: a pin apt can read does no harm, and one
it can't fails the transaction that follows. The ESM source list beside it is already set aside with the other
third-party sources (`set_release`). Reference: a `for` loop over the two names, in `tools/backends.py`
(`ESM_PINS`); `tools/test_esm_pins.py` runs it against a stand-in directory and `dpkg`.

**`net.ifupdown-dns-to-resolved`** (no arguments; Ubuntu). Gives systemd-resolved the resolvers and search domains
ifupdown's configuration names, for a box that keeps ifupdown into a release where resolved answers its lookups. A
box that came up from trusty keeps ifupdown and resolvconf; on jammy's release pocket ifupdown's own hook that hands
the `dns-*` options to resolved fails ("mystatedir: not found"; jammy-updates fixes it), resolvconf lists only
resolved's stub, and resolved has no upstream server, so nothing resolves after the reboot (found replaying trusty to
resolute in a VM, wend#13). noble ships no resolvconf at all. ifupdown says which interfaces it has up and reads its
own configuration. The op runs on the live box before the hop's reboot: `ifquery --state` lists each interface ifup
brought up as `PHYSICAL=LOGICAL`, the stanza it used (`auto eth0=work` and mapping patterns name the logical stanza),
and `ifquery --no-mappings -- PHYSICAL=LOGICAL` prints that stanza's options as ifupdown parsed them (inet and inet6
stanzas, `inherits`, sourced files); without `--no-mappings` ifquery would run the admin's `mapping` scripts again
from the logical name and could land on another stanza. What the state says is what the op carries: an interface whose ifup failed (even for one address family), that was never brought up, or that was taken
down by hand isn't in it, and one brought up by hand is. A box with no state (a chroot replay, or a live box whose
`/run/network/ifstate` is gone) carries nothing, though its configuration is still parsed (`ifquery --list --all
--exclude='*'`, which skips the per-interface check, so an `auto` name with no stanza isn't a failure) and a main
interfaces file ifupdown can't read fails the step; an error in a sourced file is a warning ifupdown skips, at `ifup`
as here. A value given with `ifup -o` isn't in the configuration, so the
configuration's value is carried, as it is for a stanza edited since boot (the value the hop's reboot gives the
hooks). A stanza deleted since boot fails the step, unless a mapping still matches its name: then ifquery prints
nothing for it and nothing is carried, as the reboot would give its hooks nothing. A configuration ifupdown can't read fails the step, and so does a state line whose interface or stanza name starts with `-` or holds anything
but `[A-Za-z0-9_.:@-]`. ifquery prints `name: value` per option, and ifupdown keeps an option given twice as one value
joined by a newline, so a line without `: ` (or with whitespace in its name) continues the value before it (only a
newline ends a line), and a value is split into words as a hook's shell splits it (space, tab, newline). Each option
name becomes the name ifupdown's hooks receive (up to the first `=`, whose rest leads the value; `-` to `_`, ASCII
upper case, anything else dropped), and two spellings of one `DNS_*` name on an interface fail the step, because a hook sees only the last.
Only one interface may name resolvers: ifupdown's hook gave each interface its own link in resolved, and
one global `DNS=` for two would send every lookup to both, so a second one fails the step (inet and inet6 stanzas of
one interface are one interface). `DNS_NAMESERVERS` and `DNS_NAMESERVER` must be unicast addresses a resolver can be at (no scope, not link-local,
multicast, loopback, unspecified, reserved, broadcast or IPv4-mapped); `DNS_SEARCH` and `DNS_DOMAIN` plain domain
names; any other `DNS_*` option fails the step. The result is `/etc/systemd/resolved.conf.d/50-wend-ifupdown.conf`
with `DNS=` and `Domains=`, written (mode 0644 in a 0755 directory) only when it is new, through the directory reached one name at a time
from `/` without following a symlink: a symlinked `/etc`, `/etc/systemd`, `resolved.conf.d` or drop-in fails the
step, and a directory swapped for a symlink while the op writes doesn't redirect the write;
one that is already there must be a plain file saying the same, and a `.wend` file beside it fails the step. The
drop-in is published with a hard link from the op's open file (`/proc/self/fd`), not a rename, so one created by
something else while the op writes is kept and the step fails, and a temporary name replaced meanwhile fails it too. Nothing
is written when no interface ifupdown has up names a `dns-*` option, and every refusal comes before anything is
written. Addresses and routes stay with ifupdown, and resolved reads the drop-in at the reboot that ends the hop.
Not seen, because ifquery's text doesn't show it, so the options are carried although no hook read them:
`no-scripts`, and a repeated option whose second value itself reads `name: value`, which prints like a separate
option. ifquery prints an interface's inet and inet6 stanzas together, so two spellings across them fail the step
although each hook saw one. Needs python3.6 or later on the box (bionic
on; the route uses it on jammy). Reference: `python3 -` with the source in `tools/net_ifupdown_dns_to_resolved.py`,
sent as a here-document; `tools/test_net_ifupdown.py` runs it with a stand-in `ifquery`, and the VM replay proves the
parse on a real box.

### EL ops

**`rpm.swap-release`** `remove`, `install` (package names ending in `-release`; the EL adapter holds them to lowercase, as every EL release package is). Replaces the release package without the package
manager, because an epoch drop (CentOS 5's `centos-release` has epoch 10, EL6's has none) or a vendor switch isn't an
upgrade to yum. Effect: `remove` is gone from the database (its scriptlets don't run), and the first
`install-<digit>...` package for x86_64 or noarch listed in `BASE`'s metadata is installed (a release's base repo
carries exactly one; CentOS 5's `sort` has no `-V` to pick among several). Fails if there is no such package, or,
when the release lists `gpgkeys`, if `CHECKSIG` (see "Signed packages") refuses it; `remove` is still installed then.
Reference: find it in `PKGLIST(BASE)`, download it, `CHECKSIG` it, `rpm -e --nodeps --noscripts {remove}`,
`rpm -ivh --nodeps --noscripts` the file, `CLEAN_REPOS`.

**`rpm.drop-arch`** `arches` (list of `i[3-6]86`, at least one). Removes every installed package built for one of
these architectures, files and database entries, with no dependency checks and no scriptlets. EL5 x86_64 installs
carry i386 copies of libraries (curl, glibc, ...) that conflict with EL6's x86_64 packages. Refuses, before
removing anything, if a package of these arches has no installed twin of another arch (a 32-bit-only app or compat
library is not a multilib copy). 32-bit software not installed by rpm (a tarball in /opt) still loses its libraries.
Fails if any such package is still installed afterwards. Reference: `rpm -e --nodeps --noscripts` every
`NAME-VERSION-RELEASE.ARCH` that `rpm -qa` lists with one of the arches, then check `rpm -qa --qf '%{ARCH}\n'` has
none of them.

**`rpm.drop-unneeded`** `packages` (list of package names, at least one). Removes the listed packages that are
installed, in one `rpm -e`, if `rpm -e --test` accepts them: nothing outside the list requires anything they provide
(a `Requires(pre)` alone doesn't count, as with `rpm -e`). Otherwise removes none of them, says which packages need
them, and succeeds. For leftovers a release no longer pulls in that some boxes still use: on EL9 nothing requires
polkit, but tuned and pcsc-lite do. `remove` would fail the hop on those boxes instead. Being evaluated when the step
runs, it sees the new release's dependencies; a `when` is evaluated before the hop.

**`rpm.bootstrap`** `release` (the adapter's `releases.<release>.bootstrap` table must exist; EL defines it for
`centos6` only). Installs the next release's package manager when the old one can't: rpm 4.4 can't read EL6's xz
payloads, and yum 3.2.22 can't check EL6's sha256 repodata. Effect: the files of every package in
`bootstrap.packages` (from `release`'s first repo) are on disk, except their `/etc` and documentation, and the new
rpm has rebuilt the database. The packages stay recorded at their old versions; the hop's own transaction then
installs them properly. Reference:
1. In a fresh `/var/lib/wend/boot`, take `PKGLIST` of `release`'s first repo.
2. Download each file in `bootstrap.p7zip` (a table of file name → `sha256:` digest) from `bootstrap.p7zip_repo`.
   Fail unless each one's sha256 is its pin: nothing else vouches for them. Write the pins to
   `/var/lib/wend/sources/<hop>/fetched`, one `https://<p7zip_repo>/<file> sha256:<hex>` per line (the hop's
   `sources` take a line only if it is one of the adapter's pins), then install them with `rpm -Uvh --nosignature`.
3. For each name in `bootstrap.packages`, download the first `name-<digit>...` x86_64 or noarch file in the list.
   A name with no file is reported and skipped.
4. For each downloaded file, unpack its payload with `7z` and extract it over `/` with
   `cpio -idmu -f './etc/*' './usr/share/doc/*' './usr/share/man/*'` (everything except those). Fail on any error.
5. `ldconfig; rpm --version; rm -f /var/lib/rpm/__db.*; rpm --rebuilddb`; then `rpm -e p7zip p7zip-plugins`, and
   delete the work dir.

**`rpm.transit`** `release` (the adapter's `releases.<release>.transit` table must exist; EL: `alma8`), `remove`
(a package name), `reinstall` (list of package names, may be empty). Takes the box through a release its rpm can't
reach: rpm 4.11 (EL7) can't install packages with rich dependencies. Effect: every installed package is replaced by
`release`'s version of it, installed by `release`'s own rpm; `reinstall` is installed again (the release's first
distro-sync erases some packages the box needs); the package manager is `release`'s dnf with a `yum` front end;
nothing from older releases is left in the rpm database (their files stay, unowned). So it refuses, before
touching anything, if any installed package (gpg-pubkey aside) has a vendor other than `remove`'s (or an installed
add-on's: see "Add-on repositories", which also says what it then requires of those packages), or is one of that
vendor's add-on repos (EL: names starting `rh-`, `devtoolset-` or `centos-release-`, except the base
`centos-release-notes`; that is, Software Collections and SIGs). EPEL without its marker, vendor, SIG and locally
built RPMs would otherwise lose their package records. The vendor is read last in each line, so a vendor string
containing the separator can't pass as another package's. Fails if the box doesn't say it is `release` afterwards.
A fix with a non-empty `reinstall` must pass the install rule. Reference, with work dir `/var/lib/wend/boot`, and
repos kept in private `reposdir`s so `/etc/yum.repos.d` stays the harness's:
1. Write a repo file for `transit.from`'s repos plus `transit.extra_repos` (`old`), and one for `release`'s repos
   (`new`). Installed add-ons add their repo for `release` to `new`. Before that, if any add-on vendor's package is
   installed: read the package names in the primary metadata (gzip, xz or zstd) of `release`'s repos and its add-on repos,
   and refuse, naming them, if any of those packages has no package of the same name there. Only the work dir has
   changed at that point.
2. `yum install dnf` from `old` (CentOS 7 extras ships an EL7 dnf).
3. With that dnf and `new`: `--nogpgcheck --downloadonly` distro-sync into the work dir, `--releasever` = the release's major,
   `--allowerasing --setopt=best=False` (EL7's dnf 4.0 has no `--nobest`), no weak deps, no protected packages. Its exit code is ignored; the step fails if
   `rpm`, `rpm-libs`, `glibc`, `dnf` or `python3-dnf` wasn't downloaded, or any of the add-on packages wasn't (as
   with the first five, EL7's dnf is still installed when that fails). Also fetch `transit.release_package` from
   `PKGLIST(BASE)` if the sync didn't.
4. `rpm -e dnf` (the EL7 one) and rebuild the database. When `release` lists `gpgkeys`: `CHECKSIG` (see "Signed
   packages") over every downloaded package. Nothing downloaded is unpacked or run before this check. Then unpack
   every downloaded package into a staging dir.
5. `rpm -e --nodeps --noscripts {remove}` (the old release package), then run the staged rpm through the staged
   `ld-linux-x86-64.so.2 --library-path <stage>/usr/lib64` with `-Uvh --nodeps --replacefiles --replacepkgs
   --oldpackage --nosignature --nodigest` over every downloaded package. Its exit code is ignored and its errors are
   reported: some EL7 scriptlets fail under EL8's rpm, and step 6 checks the result.
6. `ldconfig`, rebuild the database, check `/etc/redhat-release` says `release {major}`, `CLEAN_REPOS`.
7. A real distro-sync with `release`'s dnf and the `new` repos (errors reported, not fatal: the EL7 yum
   directories trip it once).
8. Remove the EL7 `yum*` packages without scriptlets; move `/etc/yum/{pluginconf.d,protected.d,vars}` to `SAVED`.
9. `dnf install yum {reinstall}` (fatal on failure).
   Then, before the cleanup wipes them, copy the `repomd.xml` of the `old` repos (`boot-<name>`) and of `release`'s
   repos (`<release>-<name>`) from the yum and dnf caches to a fresh `/var/lib/wend/sources/<hop>/<repo id>.repomd.xml`,
   and nothing else: the hop's `sources` pin them.
10. `rpm -e --nodeps --noscripts --justdb` every package (but `gpg-pubkey`) without the `el{major}` tag, listing them
    in the output; delete the work dir; `CLEAN_REPOS`. That drops the old release's leftovers from the database and
    keeps their files. It would also drop a third-party package the owner installed, which is why the list is printed
    (and recorded in the run's step output): a client must show it to the owner. Since their `%postun` never runs, first remove (`alternatives
    --remove`) every alternative whose path one of them owns: it would otherwise outrank the release's own at its old
    priority (EL7's Java 8 outranks EL9's, priority 1) and keep the link on their unowned files.

**`rpm.adopt-rpmnew`** `paths` (on the adapter's `managed` list; globs allowed). For each file that has a
`FILE.rpmnew` next to it, keep the old file under `SAVED` and put the `.rpmnew` in its place. Files without one are
left alone. Never writes content of its own.

**`rpm.set-aside-unowned`** `paths` (on `managed`). Moves each existing file that no installed package owns
(`rpm -qf` fails) to `SAVED`. If `SAVED` already holds that path (an earlier hop set aside the owner's original), the
current file, which a package or the harness made since, is deleted instead. Never writes content of its own.

**`fs.layout`** `layout` (`usrmove` or `run`).
- `usrmove`: `/bin`, `/sbin`, `/lib` and `/lib64` become symlinks to `usr/bin`, `usr/sbin`, `usr/lib`, `usr/lib64`
  (EL7's `filesystem` package requires it; redhat-upgrade-tool did it at boot). Reference: for each one not already
  a symlink, copy every entry `/usr/<dir>` lacks into it (a symlink in `/usr` that points at the same file is
  replaced by the file); then, in one process (python), rename each dir to `<dir>.usrmove` and create the symlink;
  then `/bin/sh -c true; ldconfig; rpm -q glibc bash` (fails the step if the system no longer runs), and delete the
  `.usrmove` dirs.
- `run`: `/var/run` becomes a symlink to `../run` and `/var/lock` to `../run/lock`, with their contents copied over
  first. Fails if `/var/run` isn't a symlink afterwards.

**`kv.set`** `path` (on the adapter's `kv` list, with `set = true`), `values` (a table; its keys must be on that
file's `keys` list, values `[A-Za-z0-9_./:-]+`). For a file of `key=value` lines without sections: every line setting one of the keys, commented out or not
(`^\s*#*\s*key\s*=`), is deleted, and `key=value` is appended for each, in key order. Fails if the file doesn't
exist.

**`kv.unset`** `path` (on the adapter's `kv` list), `keys` (on that file's `keys` list). Deletes every uncommented line setting one
of the keys (`^\s*key\s*=`). Works in files with sections too (`my.cnf`). Fails if the file doesn't exist.

The three database ops share a start and a stop. Start: stop any running `mariadbd`/`mysqld`, take the first of
`/usr/libexec/mariadbd`, `/usr/libexec/mysqld`, `/usr/sbin/mariadbd`, `/usr/sbin/mysqld`, create its runtime and log
dirs (`/run/mariadb`, `/run/mysqld`, `/var/log/mariadb`) owned by `mysql` together with `/var/lib/mysql`, remove a
stale socket and pid file, start it as `mysql` in the background with `--skip-networking`, and wait up to 120 s
for `/var/lib/mysql/mysql.sock`. The start fails if no server binary exists or the socket never appears. Stop:
SIGTERM, then wait up to 300 s; the stop fails if a server is still running.

**`mysql.set-password`** `user` (`[A-Za-z0-9_]+`), `secret` (`[a-z0-9][a-z0-9-]*`). Rehashes one account's
password with the server's current scheme (MySQL 4.0's 16-character hashes stop working with MariaDB 10's clients).
The plaintext is read from `/etc/wend/secrets/{secret}` on the box, a file the box's owner puts there; a fix never
carries a password, and no op takes one as an argument. Fails if that file is missing or holds anything but
`[A-Za-z0-9_.@%+=-]+`. Then: start with `--skip-grant-tables --skip-networking`, run `SET SESSION old_passwords=0;
UPDATE mysql.user SET Password=PASSWORD('<plaintext>') WHERE User='{user}'` as root, stop. Fails if the SQL or the
stop fails. A fix using it should also be gated on `file = "/etc/wend/secrets/{secret}"`, so it only fires where
the owner has provided one.

**`mysql.clean-shutdown`** (no arguments). Start with `--innodb-fast-shutdown=0`, stop. The next major version then
finds a clean redo log (MariaDB 10.11 refuses 10.5's otherwise). Fails if the start or the stop fails.

**`mysql.upgrade-data`** (no arguments). Start the installed server on the existing data (socket only, without its
grant tables, so a root password the box set doesn't lock the tool out), run `mariadb-upgrade` (or `mysql_upgrade`)
`-uroot --force` (at most 20 minutes), stop. Fails if the start or the stop fails, if there is no upgrade tool, or if
afterwards the data's version stamp (`mariadb_upgrade_info`, else `mysql_upgrade_info`, in /var/lib/mysql) does not
name the running server's version (`--version`, up to the first `-`). The stamp is the check, not the tool's exit
code: the tool complains about things it then fixes, and a stale tool from an older release can exit 0 without
upgrading anything.

**`ssh.generate-host-keys`** (no arguments). Creates every host key type this sshd expects that the box lacks
(`ssh-keygen -A`; `sshd-keygen` does it at boot, which a chroot never has).

**`httpd.drop-missing-modules`** (no arguments). Comments out each `LoadModule` line, in the `*.conf` files of
`/etc/httpd/conf`, `conf.d` and `conf.modules.d`, whose module httpd can't load: first every line whose module file
doesn't exist (relative to `/etc/httpd`), including lines inside an `<IfModule>` that `httpd -t` never evaluates,
then each one `httpd -t` reports it cannot load, repeating until it stops reporting one (at most 40 rounds). Fails if
`httpd -t` fails afterwards.

**`php.mysqli-report-off`** (no arguments). PHP 8.1 made mysqli throw exceptions where it used to warn and return
false. Restores the old default for every script without touching the app: writes
`/usr/share/wend/php-mysqli-report-off.php` (calls `mysqli_report(MYSQLI_REPORT_OFF)` if the function exists) and
`/etc/php.d/99-wend-mysqli-report-off.ini` with `auto_prepend_file` pointing at it. `auto_prepend_file` holds one
file and this ini sorts last, so it never replaces the owner's: if `/etc/php.ini` or another `/etc/php.d/*.ini`
already sets it, the op removes its own ini if an earlier hop left one, says so, and changes nothing. "Sets it" is
read the way PHP reads it: files in byte order, the last setting outside `[PATH=...]` and `[HOST=...]` sections
wins, and an empty value or `none`, `off`, `no`, `false`, `null` (any case) means not set. That box keeps PHP 8.1's exceptions. A per-pool or per-vhost
`php_value` beats php.d anyway, so the op can't override those either.

**`service.record`** (no arguments). Writes down which SysV services start at boot, before the hop that brings
systemd: the old packages' scriptlets delete their runlevel links when the new ones replace them, and systemd's
presets leave most units disabled (on EL, every one but a few base services). Effect: `/var/lib/wend/sysv-enabled`
lists, one per line, every service with an `S` link in the default runlevel (`initdefault` in `/etc/inittab`, else 3).
On a box that already runs systemd (`systemctl` exists), it lists the box's enabled service units instead, by unit
name (`systemctl list-unit-files --type=service --state=enabled`): EL7 boxes need it before the EL8 pass-through,
whose distro-sync erases module-stream packages (httpd, mariadb, php) along with their enablement links.
Only the first record is kept: if the file exists, it is left as it is, so it always holds the box's own state. It
is only allowed in `pre` (after the upgrade the links are gone), and it stays on the box afterwards: a later route
that uses `service.enable` reads the same record, which is what the box ran at its first record, not what it runs
today. That is why `service.enable` also accepts a `like` service that is enabled now. SysV: fails if
`/etc/rc.d/rc<L>.d` doesn't exist; reference `ls /etc/rc.d/rc$L.d | sed -n 's/^S[0-9][0-9]//p' | sort -u`. systemd:
fails if the list is empty (nothing is recorded then); reference `systemctl list-unit-files --type=service
--state=enabled --no-legend`, first column without `.service`. On EL7, SysV-script services enabled with chkconfig
(`network`) are not in that list.

**`service.enable`** `unit` (`[A-Za-z0-9][A-Za-z0-9_.-]*`, so `NetworkManager`), `like` (a list of the same, at least one). Enables `unit` at
boot if any service in `like` started at boot: it is in the SysV record, or `systemctl is-enabled` says `enabled`
now. `like` names what the unit replaces (`mysqld` for `mariadb`) or follows (`httpd` for `php-fpm`, new on EL9, which
the site needs). Never disables anything, and a unit whose `like` services were all off is left as it is. Fails if
`systemctl enable` fails (the unit doesn't exist) or the unit isn't `enabled` afterwards. Works offline, as in a
chroot. Reference: `systemctl enable {unit}`, then `systemctl is-enabled {unit}` must print `enabled`.

**`net.ifcfg-to-keyfile`** (no arguments). EL10's NetworkManager can't read the initscripts profiles
(`/etc/sysconfig/network-scripts/ifcfg-*`) that EL9's still does. Without this op, a box with a static address comes
up on EL10 with NetworkManager's automatic DHCP profile, or with no address at all. The profiles converted are the
ones EL9's NetworkManager loads: every `ifcfg-*` except `ifcfg-lo` and the names its ifcfg-rh plugin ignores (ending,
in any case, `.bak`, `~`, `.orig`, `.rej`, `.rpmnew`, `.augnew`, `.augtmp`, or an rpm temp suffix `;` and 8 hex
digits). Effect, for each:
- the profile becomes a NetworkManager keyfile in `/etc/NetworkManager/system-connections/`, mode 0600, written by
  NetworkManager's own serializer (`nmcli --offline connection add`, which needs no daemon);
- `/etc/systemd/network/10-wend-<DEVICE>.link` pins the interface name to the profile's `HWADDR`. Once the ifcfg
  file is gone, nothing else keeps an `eth0` from becoming `ens3`, and the keyfile is bound to the old name and the
  MAC. A profile with no `HWADDR` (EL7's installer writes one only when told the NIC by MAC: `network --bindto=mac`
  or `ifname=`) matches its interface by name alone, so the NIC it runs on is the one with that name now: the op pins
  that NIC's MAC, read from `/sys/class/net/<DEVICE>/address`, which keeps the binding the box runs on. Only a
  physical NIC (a `device` link) of type 1 (Ethernet) with its permanent address (`addr_assign_type` 0) is pinned,
  and a static profile's address and prefix must be on it now (`ip -o -4 addr show dev <DEVICE>`), so the op on a
  copy of the box on another machine refuses instead of pinning that machine's NIC. A DHCP profile has nothing to
  compare, so the op must run on the box itself, as every engine runs it. (Renaming several NICs to kernel names like `eth1` can race the kernel; only single-NIC boxes are proven.)
  The pin is written even when an earlier `.link` already pins exactly that MAC to that name (EL8's installer writes
  `10-anaconda-ifname-<DEVICE>.link` when booted with `ifname=`): that one wins and does the same, and ours takes
  over if it goes.
- the ifcfg file moves to `SAVED`.

The files are read as `KEY=VALUE` lines and never executed; a value that isn't one word, or holds `$`, a backquote or
`#`, fails, and an empty value is an unset key. Each key has the meaning the ifcfg-rh plugin gives it:
- `DEVICE` (required) and `HWADDR` (else the running NIC's, above), `TYPE` (`Ethernet` only), `NAME` (the profile's name, else `System <name>`),
  `UUID` (the profile's uuid);
- `BOOTPROTO`: `static`, `none` or empty with `IPADDR` and `PREFIX` or `NETMASK` (a real netmask), plus `GATEWAY` (or
  `/etc/sysconfig/network`'s); `dhcp` or `bootp` (read as `dhcp`) with none of those;
- `BROADCAST` and `NETWORK`, which the installer writes and NetworkManager derives: accepted only when they match the
  address;
- `GATEWAYDEV` in `/etc/sysconfig/network`: every other device's profile gets `ipv4.never-default`;
- `DNS1`, `DNS2` (only after `DNS1`), `DOMAIN`, with any method, and `PEERDNS=no` as `ipv4.ignore-auto-dns`. A static
  profile with no `DNS1`/`DNS2` takes the resolvers, and the search list (or `domain`; `.` is none) unless `DOMAIN` is
  set, from `/etc/resolv.conf`, which is where CentOS 5's installer put them;
- `ONBOOT`, `IPV6INIT` (else `/etc/sysconfig/network`'s), `IPV6_AUTOCONF` (`no` is `link-local`), as NetworkManager's
  booleans (`yes`, `true`, `t`, `y`, `1` and their opposites);
- `NM_CONTROLLED` (`no` fails: EL9's NetworkManager left that device unmanaged, which a keyfile can't say);
- `DHCPCLASS` and `USERCTL` are inert (the plugin doesn't read them); wend still requires `USERCTL` to be a boolean;
- what EL8's installer adds, writing through the plugin: `DEFROUTE=no` as `ipv4.never-default` (a set `GATEWAYDEV`
  overrides it either way), `IPV4_FAILURE_FATAL=yes` as `ipv4.may-fail no`, and, with IPv6 on, `IPV6_DEFROUTE=no` as
  `ipv6.never-default`, `IPV6_FAILURE_FATAL=yes` as `ipv6.may-fail no` and `IPV6_ADDR_GEN_MODE` (`stable-privacy`
  or `eui64`, in that case; absent, `default-or-eui64`, as the plugin reads it) as `ipv6.addr-gen-mode`; all of them are booleans (or that mode) even with IPv6 off. `PROXY_METHOD`
  must be `none` and `BROWSER_ONLY` `no`, the defaults: a proxy fails.

In `/etc/sysconfig/network`, `GATEWAY`, `GATEWAYDEV` and `IPV6INIT` are read as above, and `NETWORKING`,
`NETWORKING_IPV6`, `HOSTNAME`, `NOZEROCONF` and `NISDOMAIN` are left as they are: none of them is part of a profile.

Any other setting in either file (bridges, VLANs, bonds, aliases, IPv6 addresses, `RES_OPTIONS`), a keyfile of the
same name or a `10-wend-<DEVICE>.link` already there, two profiles that pin one name to different MACs or one MAC to
different names, a `route-`, `route6-`, `rule-`, `rule6-` or `keys-` file beside a profile, or a value that isn't an
address, MAC, boolean or plain name fails the step. So does any `.link` in `/etc/systemd/network`,
`/run/systemd/network`, `/usr/local/lib/systemd/network`, `/usr/lib/systemd/network` or `/lib/systemd/network` whose
name sorts before the pin's (systemd applies only the first `.link` that matches, so it could name the NIC something
else), unless it only pins one MAC to one name (`[Match] MACAddress=` and `[Link] Name=`, nothing else, and no
`<name>.link.d` drop-ins in any of those directories) and that is either this profile's MAC and name or another MAC
and another name. Every profile is
checked and serialized by `nmcli` before the first file is written, so a failure, including `nmcli` missing or
refusing a profile, changes nothing. Reference: `python3 -` with the source in `tools/net_ifcfg_to_keyfile.py`, sent
as a here-document; `tools/test_net_ifcfg.py` runs it on fixture roots.

### Argument specs

An adapter op declares each argument as a table with one of these keys. A step must give exactly the declared
arguments, each valid by its spec.

| spec | the value must be |
|---|---|
| `enum = [...]` | one of the listed strings |
| `pattern = "RE"` | a string that matches RE in full |
| `list = "RE"`, optional `min` (default 1) | a list of at least `min` strings, each matching RE in full |
| `path = "LIST"` | a string matching `/etc/[A-Za-z0-9_./-]+`, no `..`, on the adapter's allowlist named LIST |
| `paths = "LIST"` | a non-empty list of such paths; `*` is allowed and must name a subset of an allowed glob |
| `table = "RE"` | a non-empty table, keys `[A-Za-z0-9_]+`, values strings matching RE in full |

A path is on an allowlist when it doesn't start with a `harness_owned` prefix and matches one of the list's globs
with `*` not crossing a `/`. LIST is `writable`, `managed` or `kv` (the paths of the `[kv]` table).

## Adapter format

`adapters/<family>.toml`, schema `wend.adapter/0`. A fix's `family` names one.

| key | meaning |
|---|---|
| `schema`, `family`, `backend` | `wend.adapter/0`; the family's name; `apt` or `rpm` (required) |
| `release_file` | the file whose content names the running release (shown in runs and checks) |
| `ladder` | every release in order; a hop goes one step along it |
| `may_skip` | releases a route may leave out of its hops (see Route) |
| `harness_owned` | path prefixes no fix may write |
| `writable`, `managed` | the allowlists (globs) named by `write-file` (`writable`) and the `rpm.*` file ops (`managed`). Nothing under `/etc/wend/` is ever on either |
| `[kv."<path>"]` | the files `kv.*` may touch: `keys` (the only keys it may set or unset; never one that grants access) and `set` (false for files with sections, which only take `kv.unset`) |
| `force` | required: the `force-remove` flags this family allows, a subset of its backend's (apt: `depends`, `remove-essential`; rpm: `depends`, `scripts`) |
| `[releases]` | where packages come from. apt: `archive`, `current`, `current_from` (releases from `current_from` on use `current`; each an http(s) URL, or host/path through the `[mirror]` shim), and optional `releases.<id>.upgrade` like rpm's. rpm: `releases.<id>` with `major`, `pm` (`yum`/`dnf`), `repos` (name = host/path, through the shim), optional `upgrade`, `bootstrap`, `transit`, `install` (the VM tier's installer), `reboot` (`before-post`, see "Kernels and the bootloader"), `joins` (a start release off the ladder: the ladder release with the same major whose routes its boxes take; see Route). apt: a start release may have `releases.<id>.install` too, and any release `signed` (see "Signed packages") |
| `[addons.<name>]` | rpm: `marker`, `vendor`, `repos` (see "Add-on repositories") |
| `[mirror]` | `shim` (the mirror shim's base URL) and `probe` (a path that must be fetchable before a run starts): every rpm repo, and an apt mirror given as host/path |
| `[hop]` | `set_release`, `refresh`, `upgrade` (list), optional `verify`, `after` (list), `kernel_before` and `kernel_after` (lists, see "Kernels and the bootloader"), `autoremove`, `obsolete`, `installed_list`, `kernel_newest` (the VM tier's newest-kernel query); apt also `apt_conf`, the harness's apt config, and `apt_unsigned`, the lines added on hops into unsigned releases |
| `[predicates]` | `installed`, `file`, `file-contains`, `mysql-option` command templates |
| `[pm_options.<name>]` | `path`, `content`. apt: only `overwrite-conflicting-files`; rpm: none |
| `[ops."<name>"]` | `summary`, `args` (see Argument specs). Only ops the Op reference defines for this backend may appear. Whatever a `pattern`, `list` or `table` says, every value a fix passes through one must also match `[A-Za-z0-9_.:/+=@][A-Za-z0-9_.:/+=@-]*`: no quote, whitespace, backslash, glob character, `%`, any of `` $`;&|<>(){}~ ``, or leading `-` |
| `[checks]` | universal health checks: `pkgdb` is a command; `current` and `services` are described in words and built by the harness from the box |
| `[hardware]` | required: what a machine needs before a route boots it into each release (see "Hardware preflight"): `boot`, `old_kernels`, and a table per release a route can boot into |
| `[relocate]` | optional: relocate's steps (lists of scripts: `preflight`, `refresh`, `place`, `copy`, `fixup`, `flip`, `release`, `cleanup`) and the commands between boots (`booted`, `arm`, `commit_trial`, `rollback_next`, `rollback`, `forward`); see "Relocate" |
| `[box]` | how to build a fresh box of this family (rpm: `seeds`, the packages to bootstrap a root from; apt: optional `debootstrap_script`, a script name under the host's `/usr/share/debootstrap/scripts`) |

These checks narrow what an adapter lets fixes do (ops, force flags, argument values). They don't judge an adapter's
allowlists, hop commands or package lists: those are reviewed as spec changes, and CI never takes them from a PR.

Templates in `[hop]`, `releases.*.upgrade` and `[predicates]` use `{name}` placeholders, replaced literally (no
other brace syntax is touched, so `${...}` and `%{...}` pass through). Values for a hop:

- apt: `{mirror}` (archive or current, see `[releases]`), `{release}`, `{apt_conf}` (the shell that writes
  `hop.apt_conf`, plus `hop.apt_unsigned` unless the release is `signed`, to `/etc/apt/apt.conf.d/00wend`).
- rpm: `{release}`, `{major}`, `{shim}`, `{repofile}` (a yum repo file with one section `[<release>-<name>]` per repo,
  `baseurl=SHIM/<repo>/`, `gpgcheck=1` and `gpgkey=` for a release with `gpgkeys`, else `gpgcheck=0`, newlines as
  `\n` for printf), `{keys}` (the shell that writes the adapter's `[keys]` to `/etc/wend/keys/`; `true` if it has
  none), `{addons}` (the shell that appends each installed
  add-on's repo for the release to `/etc/yum.repos.d/ladder.repo`; `true` if none has one), `{pm}` (`dnf` if installed, else `yum`),
  `{clean_repos}` (`CLEAN_REPOS`), `{dnf_opts}` (`--releasever={major} --allowerasing
  --setopt=install_weak_deps=False --setopt=protected_packages=`).

## Sandbox requirements (for any harness: CI, the verifier, the authoring lab)

- **Never bind-mount the host's `/dev` into a box.** If a run is killed, the mount can leak, and a later
  `rm -rf` of the box deletes the host's device nodes through it. This happened in the CentOS thread. Give each
  chroot its own tmpfs `/dev` containing only `null`, `zero`, `random`, `urandom` and `tty` (created with mknod),
  plus a `devpts` mounted with `newinstance`.
- `/proc` and `/sys` are per-chroot mounts as well, never recursive binds.
- Every command in a box runs in a UTS namespace of its own (`unshare -u`), named by the root's own `/etc/hostname`
  (EL5: `HOSTNAME=` in `/etc/sysconfig/network`), else `wend-box.example.org`, read inside the chroot. A box never
  sees the host's name, so a run doesn't depend on where it ran: etch's postfix postinst built `myhostname` from a
  bare host name and resolv.conf's `search .` and refused the result (wend#26). A Debian box build sets
  `/etc/hostname` to `wend-box.example.org` (debootstrap copies the host's) and writes an `/etc/hosts` for it.
- **Unmount and verify before any cleanup.** Before deleting or rsync-restoring a box root, unmount everything under
  it and check `/proc/self/mountinfo` for leftovers. If anything is still mounted, refuse to delete.
- Daemons never start on their own (`policy-rc.d` on Debian). Health checks start what they need and stop it again.
  The one exception is Ubuntu's xenial hop, which runs MySQL 5.7's server for its own configure (see Ubuntu).
- The harness sets umask 022 for itself before it touches a root (see "How a hop runs"), never taking the caller's.
- A rollback restores the whole root from the pre-hop snapshot. Nothing from a failed hop survives.

## Kernels and the bootloader

A chroot box has no kernel, and these steps do nothing there. The exception is a box whose release's own packages require one (EL4's initscripts require `kernel`, so a CentOS 4 box has one): CI's sandbox sets `WEND_CHROOT_BOX=1`, and `hop.kernel_before` drops its records there as it does on a booted machine, keeping the files; no kernel is installed in its place. Someone's disk walked in a chroot never gets it, so its kernel records stay. A real machine's kernel and boot entries belong to the
harness, not to fixes, because the release's own tools can't carry them up the ladder:

- **The old release's kernel can't stay in the rpm database.** EL5's kernels require `mkinitrd`, which is gone in EL6
  (`pkgdb` fails). EL6's kernel requires `/sbin/new-kernel-pkg`, which nothing provides on EL7 after UsrMove, so the
  transaction fails. And dnf won't remove the running kernel, so `rpm.transit` fails. So `hop.kernel_before` drops the
  installed kernel packages' records with `rpm -e --justdb --nodeps --noscripts` and keeps their files: the running
  kernel and its boot entry stay as the fallback. This is the one place, besides `rpm.transit`'s last step, where wend
  drops records and keeps files. Fixes can't, because a fix doing it could fake `pkgdb`. It then removes the files of
  kernels that no package owns and that aren't running or GRUB's default, so the release's kernel fits in `/boot` (the
  VM tier's CentOS 5 /boot is 200 MiB), its kdump image and any BLS entry that boots its `vmlinuz` with it (EL8 wrote
  one per kernel; left behind, GRUB lists a dead entry). This is how the previous hop's kernel, once a reboot
  has moved off it, finally goes. The rescue image (`vmlinuz-0-rescue-<machine id>`, its initramfs and BLS entry) goes
  too, unless it is or may be GRUB's default (GRUB legacy's `default=saved`; GRUB 2's default, `saved_entry` or
  `GRUB_DEFAULT` resolved against grub.cfg's menu or a BLS entry, not a kernel's entry): it is the old release's kernel
  with a generic initramfs (62 MiB on EL7), and EL9's `51-dracut-rescue.install`, finding no BLS entry for it, copies
  the new `vmlinuz` over the old one and keeps EL7's initramfs. No hop builds another: `hop.kernel_before` sets
  `dracut_rescue_image="no"` in a drop-in (`/etc/dracut.conf.d/99-wend-no-rescue.conf`) that `hop.after` removes, since
  a generic image of 54 MiB (Alma 9) or 65 MiB (Alma 10) doesn't fit the VM tier's 200 MiB CentOS 5 `/boot` beside two
  kernels. The machine lands without a rescue image, and its next kernel update builds one for the release it runs, as
  EL does when none exists. None of the kernel steps acts unless the machine is booted from this root (`/` is PID 1's
  root): in a chroot of someone's disk the records, files and boot entries are left as they are, since `uname -r` there
  is the host's kernel, not the box's. The fallback the running kernel's entry gives is a manual one: GRUB legacy has no
  `fallback=` set, so a hop that fails between the two halves leaves the default on the running kernel's entry, which
  still boots.
- **The release's kernel is installed after the transaction** (`hop.kernel_after`), since the transaction itself only
  syncs what the database lists.
- **GRUB legacy stays.** From EL8 on, kernels only get BLS entries, which GRUB legacy can't read, and GRUB2 can't be
  installed on the sector-63 partition layout of CentOS 5's installer (a 31 KB gap; EL9's smallest `core.img` is
  33.9 KB). When `/boot/grub/grub.conf` exists, `hop.kernel_after` gives the newest installed kernel an entry there,
  using the root and kernel arguments of the current default entry, makes it the default, and drops the `vmlinuz`
  entries whose kernel file is gone (memtest, Xen and chainloader entries stay). It fails if the new entry isn't
  there afterwards. EL6 and EL7 grubby still write their own entries. After every reboot the VM tier checks that the
  release's newest kernel is the one running.
- **Debian keeps GRUB 2 installed** (`hop.kernel_after`). Debian's own packages carry its kernels and their boot
  entries up the ladder; its bootloader they don't. On squeeze `grub` becomes grub-pc, which leaves GRUB legacy in the
  MBR and chains a `core.img` ("Chainload into GRUB 2"). While grub-pc has no install device (an upgraded box has
  none), nothing keeps the image GRUB legacy chains current, and on jessie the box stops after the chainload. After
  every transaction on a booted machine with grub-pc whose install device is empty or names no existing device, the
  harness installs GRUB 2 to the disk `/boot` is on (`grub-probe -t device /boot`, then the disk whose `/sys/block`
  entry holds that partition; squeeze's grub-probe has no `-t disk`, and lenny's kernel, still running then, no
  `/sys/class/block`). Once that worked it records the disk, by its `/dev/disk/by-id` name when it has one, as
  grub-pc's install device, so the release's own upgrades keep it current. A box whose install device is set and
  present is left to grub-pc. It fails if `/boot` isn't on a partition of a disk it can find (md, LVM: nothing is
  installed or recorded) or `grub-install` fails.
- **Ubuntu keeps `/boot` to two kernels** (`hop.kernel_before`). A trusty install with guided LVM puts `/boot` on a
  243 MB partition, and apt's autoremove keeps two older kernels beside the newest, so from focal on the next kernel
  and its initramfs no longer fit and the hop stops half done, as real Ubuntu upgrades do. So before each hop's
  transaction, on a machine booted from this root, the harness purges every kernel older than the running one with
  all of its packages (image, extra modules, signed image, headers): `/boot` then holds the running kernel and the
  one the hop brings. A kernel newer than the running one, installed but not booted yet, stays. So does an old kernel
  that is on hold, or that something still depends on (a metapackage still pinned to it while the machine runs an
  `-updates` or HWE kernel): each is tried with `dpkg --no-act --purge` first and, if dpkg would refuse, kept and
  named. Preflight counts neither those nor a newer kernel waiting for a reboot, so after the purge the hop goes on
  only if `/boot` has the room the release's kernel needs (its `hardware.<release>.boot_mib`, which apt adapters'
  hop steps get as `{boot_mib}`), and otherwise stops there, before its transaction, listing the kernels in `/boot`.
  A machine running a kernel that isn't Ubuntu's (`uname -r` not shaped `X.Y.Z-N-flavour`, a provider's) keeps every
  kernel, and preflight doesn't count that case either. The purge is `dpkg --purge`, never apt, so nothing else goes
  with it.
- **When to boot it.** EL6's binaries can't run on the EL5 kernel (EL6's apr asks for `SOCK_CLOEXEC`, so `httpd -t`
  fails), so on a real machine the `centos6` hop boots its new kernel between the transaction and the post steps
  (`releases.centos6.reboot = "before-post"`). Every other hop runs straight through, then reboots: on the hop to
  EL7, the post steps re-enable the boot services, which must happen before the first boot onto systemd.

The VM tier proves these steps (it reboots where `reboot` says, and after every hop). A client running a route on a
real machine must reboot at the same points; the client doesn't walk a live root yet, so it doesn't split a hop.
The checker holds `reboot` to `"before-post"` and the kernel steps to lists of commands.

## Hardware preflight

A route that reaches its last hop on a CI box can still brick a real machine halfway: the new release's glibc
refuses the CPU, or its kernel no longer drives the disk controller the root is on. So before the first hop touches
anything, a client walking a real machine checks that the machine can land on every release the route boots it into,
and refuses, or names the furthest release it can land on, when it can't. `wend preflight ROUTE` does this.

**Facts.** `wend facts` reads the machine (schema `wend.hardware/4`; its `runtimes`, `python2_modules` and `native` are SPEC "Runtime preflight"): the kernel's architecture (`uname -m`) and
the userland's word size (the ELF class of `/bin/sh`); the CPU's x86-64 level features from CPUID itself, never
`/proc/cpuinfo`, since an old kernel doesn't name features newer than itself (2.6.18 has no `avx2`), and the XSAVE
bit, not OSXSAVE, since the old kernel may not have turned it on and the new one will; CPUID's hypervisor bit;
firmware (`/sys/firmware/efi` means UEFI; `uefi-secureboot` when the firmware's `SecureBoot` variable says on, when
reading it fails, or when no variable store can be read: a Secure Boot chain is its own claim, so an unknown state
fails closed; a store that lists variables but no `SecureBoot`, as OVMF's non-secboot build and older UEFI firmware
leave it, means off, while an empty one is efivarfs not mounted, so unknown: every UEFI firmware keeps boot variables); `MemTotal`; free space on `/`; `/boot`: whether it is a filesystem of its
own, its size and free space as a package install sees them (root's reserved blocks left out of both), and each
kernel in it (one per `vmlinuz-<version>`, rescue images aside) with the MiB its files take (`vmlinuz`, initramfs,
kdump image, `System.map`, `config` and the rest named for that version) and whether it is the running one, and the
MiB the rescue image takes and whether GRUB's default may boot it; the modules the machine blacklists (`blacklist
m`, or `install m /bin/false`, in every file of `/etc/modprobe.d`, since EL5's and lenny's are named `blacklist`
without `.conf`, `/etc/modprobe.conf` and the distro's own `modprobe.d`); every mounted ext2, ext3, ext4, XFS, btrfs,
reiserfs or JFS filesystem (from `/proc/mounts`, once each, bind mounts aside): its mount point, its device (the one
`/proc/mounts` names when that node is the mount point's `st_dev`, else the node `st_dev` names in
`/sys/dev/block`, or `/sys/block` before 2.6.27, since 2.6.18 and 2.6.32 list the root as `/dev/root`; one filesystem
listed twice counts once), its type, and from its superblock whether an XFS is the old v4 format (the low nibble of
`sb_versionnum` is 5 on v5) and an ext2/3/4's inode size (null when the device can't be read); and every PCI device's
slot, class, the modalias its ID files make
(`pci:v…d…sv…sd…bc…sc…i…`), the driver bound to it now, that driver's module and where the module's file is under
`/lib/modules/<release>` (from `modules.dep`), and whether the module is from outside the kernel's own package: the
kernel marks one built out of tree (taint `O`, from Linux 3.2), and kmod packages (ELRepo's `kmod-*`, in `extra/` and
`weak-updates/`) and DKMS (`updates/dkms/`) install theirs outside `kernel/`, which is how EL5's 2.6.18, with no
taint flag, shows one. Facts can be gathered on the machine and judged anywhere (`--facts FILE`).

**Requirements.** The adapter's `[hardware]` table holds them. `boot` lists the firmware modes wend's kernel steps
are proven to carry a machine through (`bios`, `uefi`, `uefi-secureboot`): the VM tier boots its guests by BIOS, and
with `--uefi` by UEFI without Secure Boot, and an adapter names a mode only when a replay of every box on its routes
passed in it. Ubuntu says `["bios", "uefi"]` (trusty -> resolute on OVMF, wend#13); Debian and EL say `["bios"]`
(etch and CentOS 5 have no way to install or boot on x86-64 UEFI, and the CentOS 7 route waits on wend#75). No
adapter says `uefi-secureboot` yet: no replay has walked shim, signed GRUB and signed kernels under enrolled keys. `old_kernels` says which kernels sit in `/boot` beside the one a hop installs: `"running"`
(EL: `hop.kernel_before` removes every kernel but the running one first, and only those of `kernel`,
`kernel-core` and `kernel-modules*`: a kernel another package owns, such as EL5's `kernel-xen`, `kernel-PAE` and
`kernel-debug`, ELRepo's `kernel-lt` and `kernel-ml`, or Oracle's UEK, which preflight knows by its version's
suffix, stays on every hop; Ubuntu: `hop.kernel_before` purges every kernel older than the running one) or
`"autoremove"` (Debian's apt: the transaction installs the new kernel beside every kernel
already there, and `autoremove` only afterwards takes old ones, keeping the running kernel and the newest, so from
the third hop on the new kernel sits beside the two before it). With `"autoremove"`, an optional `autoremove_from`
names the release whose apt first does that: before apt 1.0, `NeverAutoRemove "^linux-image.*"` kept every kernel,
so on Debian every kernel stays through the hop to jessie. Every release a route can boot into (the ladder after its first release, less `may_skip`)
has a `[hardware.<release>]` table:

| key | meaning |
|---|---|
| `cpu` | `x86-64`, `x86-64-v2` or `x86-64-v3`: the release's level. The levels are the psABI's: v2 adds `cx16`, `lahf_lm`, `popcnt`, `sse3`, `sse4_1`, `sse4_2`, `ssse3`; v3 adds `avx`, `avx2`, `bmi1`, `bmi2`, `f16c`, `fma`, `lzcnt`, `movbe`, `xsave`. EL9 is v2 and EL10 v3 (its glibc stops a lower CPU at the first exec: "Fatal glibc error: CPU does not support x86-64-v2") |
| `ram_mib` | optional: the release's documented minimum memory without a desktop. Absent when the vendor publishes none (Ubuntu 16.04 to 22.04) |
| `disk_mib` | an estimate of the free space on `/` the hop needs: its downloads and the release's growth. Walking a copy of the machine first (open question 12) measures the real need |
| `boot_mib` | the space the release's kernel takes in `/boot`: its package's `/boot` files, measured, and its initramfs, built from the release's own packages in a container where that builds what a machine gets (initramfs-tools' `MODULES=most`), estimated where it can't (EL's host-only dracut, releases too old for an https mirror). The VM tier's `/boot` listings are what to check them against |
| `drivers` | the sha256 of `adapters/drivers/<family>/<release>.txt`, the release's driver table |
| `note` | optional: said when the CPU check fails (where a machine that falls short can go instead) |

**Driver tables.** A driver table lists every `alias=pci:` pattern of the storage and network modules (the kernel's
`drivers/` `scsi`, `ata`, `nvme`, `block`, `md`, `message`, `ide`, `net` and `virtio`) in the kernel package the
hop installs, one `<module> <pattern>` per line, plus `builtin <module>` for each driver built into the kernel, with whatever aliases `modules.builtin.modinfo`
records for it (older kernels record no device tables there: EL9's built-in `virtio_pci` has none), plus `fs <type>`
for each filesystem the kernel mounts, read from its own build config (`/boot/config-<version>`, or
`<modules>/config` from EL9 on): built in, or a module the packages carry. ext2 and ext3 count when ext4 mounts them
(`EXT4_USE_FOR_EXT23` to Linux 4.2, `EXT4_USE_FOR_EXT2` after, and ext3 always from 4.3, when its own driver went),
and `fs xfs-v4` when the kernel mounts XFS's old v4 format (crc=0), which every kernel before 5.10 does and later
ones do when built with `XFS_SUPPORT_V4`. A device bound
now to a built-in driver whose table records no `pci:` pattern counts as driven: the one place a match is by name,
not ID. Facts name the device's module (sysfs `driver/module`) as well as its driver, since the two can differ
(lenny's `ide_pci_generic` registers as `PCI_IDE`). `tools/hwdata.py` makes it from the package itself: it finds the kernel in
the release's index, checks the download against the index's sha256, and reads each module's `.modinfo` section.
The table's header names the packages and their hashes. Built from the release's own kernel, not from a vendor's
list of what it deprecated: AlmaLinux 9's kernel still drives the LSI SAS1078 (PERC 6) and SAS2008 HBAs that RHEL
dropped in 8, and RHEL 7 dropped `sym53c8xx` (the LSI 53C895A that QEMU emulates and the VM tier gives etch), which
every Debian kernel keeps. The checker holds each table to its pinned hash.

**The verdict.** Refused outright, before the first hop, when the architecture isn't the route's (`amd64` and
`x86_64` are one) or the userland is 32-bit, or the firmware isn't in `boot`. Otherwise each hop fails when the CPU
is below the release's level, memory below `ram_mib` (with 10% slack: `MemTotal` is the RAM less what the kernel keeps), free space below
`disk_mib`, or a device that holds disks or carries the network (PCI class `0x01` mass storage, `0x0c04` Fibre
Channel, `0x0e` I2O, `0x02` network, `0x0c06` InfiniBand) bound to a driver now matches no pattern in the release's
table and its driver isn't listed as built in: the machine would come up without it. A pattern of a module the
machine blacklists doesn't count, unless that module drives the device now (kmod-r8168 blacklists the kernel's own
r8169). A module from outside the kernel's package never follows to the release's kernel: the device needs one of
that kernel's own drivers, and when it has one the verdict notes which takes over. A hop also fails when its kernel
doesn't fit in a separate `/boot`: `boot_mib` must fit in `/boot`'s size less its other files (what isn't a kernel's;
with `"running"`, the rescue image isn't one either, since `hop.kernel_before` drops it, unless GRUB's default may
boot it) and the kernels the hop keeps (`old_kernels`). Those are the machine's running kernel on the first hop with
`"running"` (with any kernel another package owns, on every hop), and every kernel it has with `"autoremove"`; then the
previous release's kernel with `"running"`, and with `"autoremove"` the two before it (the machine's running kernel and
the first release's, on the second hop), or every kernel so far up to `autoremove_from`. A hop whose fixes pass
through a release with `rpm.transit` also keeps that release's kernel beside them, at its adapter's
`releases.<release>.transit.boot_mib` (EL's alma9 hop: EL8's 4.18 stays until the alma10 hop). A hop also fails
when a mounted filesystem's type has no `fs` line in the release's table, or it is an XFS in v4 format (or one whose
superblock couldn't be read) and the table has no `fs xfs-v4`: from the real configs, EL10's kernel doesn't mount
XFS v4, EL9's has no btrfs (EL10's has it back), EL6 on has no reiserfs or JFS, lenny's
mounts only `ext4dev`, and resolute's 7.0 has no reiserfs (gone from Linux in 6.13; trixie's 6.12 still has it). An
ext2/3/4 with 128-byte inodes mounts on every release (newer kernels warn that its dates end in 2038): a note on the
first hop. A route never reformats `/boot`, and GRUB 2, where every route ends, reads ext2/3/4 and XFS v4 and v5. A release a route
only passes through (`may_skip`) installs no kernel. A `/boot` on `/` is left to `disk_mib`. `ata_generic`'s,
`ide_pci_generic`'s and `pata_acpi`'s class-wide IDE patterns count only for a device that module drives now, since their probes decline
controllers they don't know. Facts read from a file must have every field `wend facts` writes. The same device with no driver bound is a note, not
a failure. The machine can land on the last release before the first failing hop. The client says which, says how to
raise a VM's CPU model when the CPU is the problem and CPUID's hypervisor bit is set (most old fleets are VMs now),
and exits non-zero unless the whole route fits. A client walking a booted machine must run preflight first and stop
before the first hop that fails it. A walk may stop early at the release preflight names; it never starts a hop it
fails.

Not checked yet: firmware files a driver loads; the kernel flavour a machine runs (Ubuntu's tables are
`linux-image-generic` with its extra modules; a machine on `linux-virtual` has fewer); a DKMS module that would
rebuild for the new kernel (it isn't counted, so such a device is refused when no in-kernel driver has it); and in
`/boot`, a GRUB default that isn't the running kernel (EL keeps it too), kdump images the new kernels get once kdump
starts, kernels an admin marked manual, which `autoremove` never takes, and a kmod package's blacklist that goes when
the hop removes the package (it is counted as staying, so such a machine may be refused when it would have landed);
filesystems other than those seven (vfat, ZFS, OCFS2) and ones not mounted now (`noauto`); and LVM1 volume groups, which
lvm2 2.03 (EL8, buster, focal on) no longer reads (every release a route starts from makes LVM2; md's 0.90 metadata is
still read by every kernel and mdadm here).

## Runtime preflight

A route that lands every hop can still kill the one program a machine exists for: its own code runs on a runtime the
new release no longer ships. Python 2 is the first and the biggest. EL7 runs everything on it, and Debian, Ubuntu
and EL all drop it partway up their ladders. wend moves machines and doesn't maintain runtimes: it never ships its own
Python 2, and never carries a dead release's packages forward. What it does is tell the owner, before the first hop
touches anything, which of the machine's own programs run on Python 2 and the last release that still ships it. Then
the owner picks: **stop** there, or **shelter** those programs and go all the way ("Sheltering an app", once built).
Porting them to Python 3 is the third way, and it's the owner's (or their agent's) to do. `wend preflight` does this
as part of the same verdict.

**Facts.** `wend facts` (schema `wend.hardware/4`) lists every Python 2 user under `runtimes`, each with the
interpreter's `version` (`2.7`), `via`, `program`, `interpreter`, `where` and `owner`:

| `via` | found by | `program` |
|---|---|---|
| `process` | a running process whose executable resolves to `python2.N` (or is a hard link to a `python2.N` beside it, as EL5's and EL6's `/usr/bin/python` is; or, failing both, a plain `python2`, or `python` with no `python3*` beside it, whose prefix holds one `lib/python2.N`), or that has `libpython2.N` or a `python2.N/` module directory mapped | the script it runs, made absolute from the process's working directory; `-m module`; `-c` |
| `embedded` | any other running process with `libpython2.N` mapped: mod_wsgi or mod_python in httpd, a uwsgi plugin | the process's executable (`interpreter` is the library) |
| `cron` | a job in `/etc/crontab`, `/etc/cron.d`, the per-user spools (`/var/spool/cron`, `/var/spool/cron/crontabs`) or the `run-parts` directories (`/etc/cron.{hourly,daily,weekly,monthly}`) that runs a Python 2 interpreter (by path, or a bare `python` through the `PATH` the crontab last set, else cron's `/usr/bin:/bin`) or a script whose `#!` names one (`env` resolved the same way; a `cd DIR &&` before it sets the directory a relative `./job` or script argument is under) | the script |

`where` is the process's systemd service (from its cgroup) or the crontab, when there is one. `owner` is the package
that owns `program`: dpkg's file lists, read from `/var/lib/dpkg/info`, or `rpm -qf` on an rpm machine (the one
place facts asks a distro tool, since rpm's database is rpm's own format). Each program is listed once, so httpd's
workers, or a script a service and a cron job both run, count once. A process whose root isn't the machine's (in a
docker or LXC container) isn't listed: its userland isn't the one the route moves. Only root can read every
process's executable and maps and every user's crontab, so `wend facts` and a live `wend preflight` refuse to run as
anyone else rather than miss them. When another package manager the route doesn't drive owns the program (Conary,
on rPath and SAS appliances: `conary q --path`), `other_owner` names it (`conary:mint:python`), and `owner` stays null:
the route doesn't carry that package, so the program is still the machine's own.

Facts also list what those programs stand on that no package owns, so the route won't carry it:

- `python2_modules`: for each Python 2 module directory of the system interpreter (`site-packages` and
  `dist-packages` under `/usr/lib`, `/usr/lib64`, `/usr/local/lib` and `/usr/local/lib64`, in `python2.N`), the files
  in it no package owns (bytecode, `.pyc` and `.pyo`, next to its own `.py` aside: Debian's Python helpers write it
  after dpkg's lists, so it's a cache, not a module), when that leaves a module: `version`, `dir`, `files`,
  `compiled` (how many are ELF extension modules, built for that 2.N alone), and `modules`, the top-level names
  (`django`, `lxml`; `.egg-info` and `.pth` aside). pip, easy_install and a hand `setup.py install` put things here,
  and so does a second package manager.
- `native`: every x86-64 ELF program and shared library no package owns under `/usr`, `/opt`, `/srv`, and `/bin`,
  `/sbin`, `/lib` and `/lib64` when they aren't links into `/usr`, grouped by tree (`/opt/<name>`, `/srv/<name>`,
  `/usr/local`, a Python module directory, else the file's directory): `tree`, `files`, up to three `sample` paths,
  and `needs`, the sonames (`DT_NEEDED`) its files load that no unowned file provides (by `DT_SONAME`, or by file
  name when it has none): one in the same tree, or one in a directory the loader searches (`/lib*`, `/usr/lib*` and
  their `x86_64-linux-gnu`, `/usr/local/lib*`, and what `/etc/ld.so.conf` and its includes list). A vendor's private
  copy elsewhere serves only its own tree. A statically linked program needs nothing. 32-bit files, other
  architectures' (a cross toolchain's sysroot), relocatable
  objects (`.o`, kernel modules) and files under `/usr/src` aren't listed.
- `kernel_owner`: the package that owns the running kernel's `/boot/vmlinuz-<release>`, or null (a hand-built kernel,
  or another package manager's).
- `repo_packages`: the packages that own the machine's repository definitions (`/etc/yum.repos.d/*`,
  `/etc/apt/sources.list` and `sources.list.d/*`), such as `epel-release`: an add-on's marker package (SPEC "Add-on
  repositories") shows up here when the machine has it.

**Whose it is.** A user is the machine's own when its program belongs to no package, or it is `embedded` (whatever
code mod_wsgi runs is the owner's: the distro never configures it). The distro's own Python 2 programs (yum,
firewalld, fail2ban, a package's cron job) don't count, since the route carries their packages across like any
other. A program run with `-m` or `-c` belongs to nobody, so it counts.

**Requirements.** The adapter's `[runtimes.<release>]` tables hold, for every release a route can boot into,
`python2`: the upstream version of each Python 2 interpreter the release ships, `[]` for none. `tools/runtimes.py`
reads them from each release's own index, the components its hop uses (Debian: main; Ubuntu: main and universe,
where Python 2 moved at focal; EL: the release's repos, modular packages included), and never from documentation.
Add-on repositories aren't read. As read in October 2026: Debian ships Python 2 through bullseye (2.7.18), Ubuntu
through jammy (2.7.18, universe), EL through EL8 (2.7.18, the `python27` module, out of upstream support since June
2024). wend's EL routes never land on EL8 (`may_skip`), so an EL7 machine's last release with Python 2 is CentOS 7
itself.

Each release also has a library table, `libs`: the sha256 of `<family>/<release>.txt.xz`, which
`tools/sonames.py` writes from the release's own indexes (EL: every `(64bit)` soname a package in the release's repos
provides; Debian and Ubuntu: every shared library file in the linker's own directories, from the Contents index of the
components the hop uses). A library that only another component ships (Debian's contrib or non-free, such as a vendor
GPU library) isn't in the table, so a program that loads it reads as losing it: check those by hand.

An add-on repository has a table of its own for each release it has a repo for, `<family>/<release>.<add-on>.txt.xz`,
pinned as `[addons.<add-on>].libs.<release>`. A hop keeps an add-on only on a machine with its marker package, so
preflight adds the add-on's sonames to the release's only when `repo_packages` lists the marker: a machine without
`epel-release` isn't credited with EPEL's libraries.

The tables stay out of git: the repository keeps only their pins. Each table is in the wend archive at its pin
(`blob/sha256/<2>/<hex>`, stored by `tools/archive.py add-tables`) and in the release tarball under
`adapters/sonames/`; a checkout fetches them into `.wend/sonames/` with `tools/sonames.py fetch`, which keeps only
bytes matching the pin. A table here whose bytes don't match its pin stops the run. A missing one skips the check:
each hop that would need it gets a note saying the libraries weren't checked, never a silent pass.

**The verdict.** A hop has a runtime problem when the machine has its own Python 2 users and the release ships no
Python 2, and in two more cases:

- **Stranded modules.** The machine's own users run Python 2.N, the release ships Python 2 but not 2.N, and
  `python2_modules` lists unowned modules for 2.N. The release's 2.M replaces the interpreter and never reads 2.N's
  module directories, and compiled modules are built for 2.N alone, so those programs lose their imports. Without
  unowned modules it stays a note (Python 2 code mostly runs on a later 2.x).
- **Missing libraries.** A `native` tree needs a soname the release's library table doesn't list: the route takes
  the library away, and whatever loads it stops starting. One problem per tree, naming the missing sonames. Each hop lists those in `runtime_problems`, apart from `problems`, which stay the hardware's: the machine
would boot fine, and the owner's program wouldn't run. `keeps_runtimes_on` names the last release before the first
such hop, or the route's last release when no hop drops it (`null` when the machine has no Python 2 of its own); a
hop with runtime problems isn't `ok`, `runtimes` lists the machine's own users, and `ok`
needs both the hardware and the runtime to reach the route's end. When a user's own 2.x isn't among a release's
versions (2.4 on squeeze, which ships 2.5 and 2.6), the first such hop notes it. When Python 2 goes before the
hardware stops, the report lists the programs and offers the choice: stop at `keeps_runtimes_on` (on EL7, stay where
it is), or shelter them and land where the hardware allows. When the hardware stops first, its verdict stands
alone. Nothing overrides a runtime problem: the owner shelters the programs, ports them, or retires them and checks
again.

When `kernel_owner` is null and a kernel in `/boot` is the running one (a container boots none of its own), the first
hop notes it: the route installs and boots the release's own kernel and
doesn't carry the running one. On EL, `hop.kernel_after` makes the newest kernel rpm owns GRUB's default, so a
custom kernel with a higher version doesn't stay the default; the custom kernel stays as the fallback entry for that
reboot, and the next hop's `hop.kernel_before` removes its files, modules and boot entry like any kernel no package
owns. A device only that kernel drives is a hardware problem already (its driver isn't in the release's table).

Not checked yet: services that are enabled but not running, and timers; scripts a cron job reaches through a shell
script of its own; a Python 2 an add-on repository ships (EPEL's `python26` on EL5); third-party packages of the
owner's own code (an rpm or deb the owner built counts as the distro's); unowned modules of a Python 2 built from source
(its own `lib/python2.N`; the libraries it loads are `native`'s); ELF files outside the trees `native` reads (home
directories, `/var`); symbol versions (a library of the same soname that is too old); and runtimes other than Python 2 (PHP 5, Java 6).

## Package evolution data (leapp-data)

AlmaLinux's [leapp-data](https://github.com/AlmaLinux/leapp-data) (Apache-2.0) is the data ELevate's Leapp reads.
Its Package Evolution Service events say, for about 18,000 EL packages, which ones are removed, renamed, replaced,
split, merged, moved to another repo or deprecated at each release from CentOS 7 to AlmaLinux 10, plus EPEL's in its
vendor data. That is far more coverage than wend's routes have. It knows nothing about whether a machine's services
still work afterwards, which is what wend's checks are for. So wend reads it as one input to a report and nothing
more: it never picks, changes, gates or refuses a route, and nothing in it becomes an op, a package name in a shell
command or a URL to fetch.

**Pinned, never fetched.** `adapters/leapp/el/` holds an extract of two leapp-data files at one commit
(`files/almalinux/pes-events.json`, `vendors.d/epel_pes.json_template`): the x86_64 events that change something and
name real package names, one per line, behind a header naming the commit, the file and its sha256. `manifest.toml`
pins the commit and, per file, the upstream file's sha256 and the extract's. `wend check` refuses an extract that
isn't the pinned one, a header that disagrees with the manifest, or an extract outside its strict schema (exact keys,
known actions, package names as in "Preconditions", repo ids and module streams by pattern, capped sizes). It lives
under `adapters/`, so the gate judges every PR by the base branch's copy. `tools/leappdata.py COMMIT` is the only
way to refresh it: a maintainer PR, with the changed events (the tool prints them) in its body. No bot bumps it. (The Rust client's `wend check` doesn't read leapp-data and doesn't check it yet.)
`tools/leappdata.py --check` downloads the pinned files again and confirms the extract rebuilds byte for byte. The
attribution is in `adapters/leapp/NOTICE` and the license in `adapters/leapp/LICENSE`.

**The gap check.** `wend gaps ROUTE BOX` (or `--packages FILE` for a real machine's `rpm -qa --qf '%{NAME}\n'`, or
`--all`) walks the route as a model: from the box's packages and enabled module streams it applies, hop by hop, the
events between the two releases, in release order, and the package steps of the route's fixes for that hop whose
`installed` preconditions hold. An event applies when the box has every package it starts from in a matching stream
(`merged`: any of them). A package goes when an event doesn't carry its name on, or carries it to a repo the route
doesn't enable (CRB, PowerTools). Per hop it lists the box's own packages that go (box.toml's `packages`), with what
leapp-data says each becomes and the route's fixes for that hop whose steps name it or its successor; then, in one
line, the dependencies that go; then what is deprecated there. A package with no fix naming it is a lead for a route
author, not a verdict: the replay and the box's checks decide. Hops before EL7 aren't checked, since leapp-data starts
at EL7. On a PR that the gate replays an EL route for, CI runs it on each of those route and box pairs and puts the
report in the job summary, with a warning when an own package goes unnamed (`.github/workflows/route-gaps.yml`); it
never fails and isn't a required check. Whether it should ever stop a preflight is open question 14.

## Sheltering an app (draft: chroot tier built)

This section designs the **shelter** choice that runtime preflight offers. The chroot (CI) tier is built in the
reference harness (`tools/shelter.py`), and the box that needs it (`el/centos7-pyapp`) lands right after it; the client's ops,
the preflight plan and the VM tier come after it, each with its own PR.

### What it is, and what it isn't

Runtime preflight finds a machine's own Python 2 programs and tells the owner where the route leaves Python
2 behind. Sheltering is the option that lets the route go all the way anyway. The host goes to the target release:
kernel, glibc, openssl, sshd, the bootloader, every other service. Only the owner's Python 2 programs keep an old
userland: the last one on the route that still runs them, in a container of their own, with their code and data
mounted in from the host.

It is a tourniquet. The app's Python 2 and every library it loads stay unpatched forever. wend says so in the
verdict, in the run, and in the unit file it writes. It never patches them or rebuilds them, and old packages never
reach the host's own root: they live only inside the shelter's image. Porting the app is the cure, and the shelter buys the time to do it.

Scope of the first version:
- `process` and `cron` users from runtime preflight. `embedded` (mod_wsgi, mod_python) comes next, because it means
  splitting the box's httpd: a sheltered httpd on a loopback port, with the host's httpd proxying the vhost.
- One shelter per app. An app is a code tree (the directory the programs live under, such as `/opt/billing`) with
  its service and its cron jobs. The plan proposes the grouping, and the owner can rename or split it.
- Only the machine's own programs. The distro's own Python 2 tools never get sheltered: the route carries them.

### Where the userland comes from, and how it stays pinned

**Which release.** The shelter's userland is the route's `keeps_runtimes_on` release: the last one the route reaches
that still ships Python 2, so it's the userland the app runs on at the moment the shelter takes over (CentOS 7 for
an EL7 box; bullseye on etch to trixie, whose app has been running on bullseye's 2.7 by then, not on etch's 2.4).
It's built at that release's **final** versions (its last updates), never at a snapshot of what the box has: the
archives keep only each suite's last versions for Debian and Ubuntu, and a release's own updates are what its app
was meant to run on. The plan says when that's newer than what the box runs now.

1. **Package set.** The interpreter's package, plus the package that owns each file the app's processes have mapped
   (`/proc/<pid>/maps`: shared libraries, `.so` modules) or loaded (its imports under `lib/python2.N`), by name,
   resolved with their dependency closure in that release's repos. For a cron-only app, which isn't running when
   facts are gathered, the plan runs the job's interpreter once in a sandbox copy with `-v` to list its imports. It
   never runs the job on the real machine.
2. **Fetch, and what's trusted.** From the vendor's archive of that release (vault.centos.org, archive.debian.org,
   old-releases.ubuntu.com). Today no run pins a release no hop installs, so `shelter.build` pins it itself. The
   build runs while the box is on that release (see "When" below), so its signing keys are the ones the box already
   trusts. An expired key counts: these keys expired with their releases, and the signature is what's checked, not
   the date. The trust anchor differs by family:
   - Debian and Ubuntu sign the index (`InRelease` or `Release.gpg`). wend checks that signature, then every package
     against the sha256 the signed index lists.
   - EL doesn't sign its index: CentOS 7's `repomd.xml` on the vault has no signature. There the anchor is each
     package's own RPM header signature (`rpmkeys --checksig` against `RPM-GPG-KEY-CentOS-7`), and a package that
     isn't signed is refused.

   The lock records the index digests either way. On EL they're trust on first use: they hold later rebuilds to
   what the first build saw, and nothing more. Bytes that don't match, or a signature that doesn't verify, refuse
   the shelter, and the box stays on the release it's on.
3. **Build.** Packages are unpacked and installed into `/var/lib/wend/shelter/<app>/root` by the release's own rpm or
   dpkg, in the sandbox wend already uses to build CI boxes (private tmpfs `/dev`, never the host's).
4. **Carried files.** Files the app needs that no package owns (pip-installed site-packages, `/usr/local/lib/
   python2.7`, a vendored module tree), and packaged files the box has changed (what `rpm -V` or dpkg's md5sums
   flag: a file pip overwrote), are copied in as a separate layer, plus the app's user and group lines for
   `/etc/passwd` and `/etc/group`. Each is listed with its sha256, so the owner sees exactly what came from
   outside the vendor's archive. A module Python loaded from its bytecode goes in as its `.py` when the box has it:
   bytecode is a cache the interpreter rebuilds, and two compiles of one source (two installs of the same wheel)
   needn't match byte for byte. Bytecode with no source goes in as it is.
5. **Lock.** `/var/lib/wend/shelter/<app>/shelter.lock` records the release, the index digests and signing keys,
   every package with its version and sha256, every carried file with its sha256, and the built image's ID. The run
   records the lock too.

Why not snapshot `/usr`? It would be faithful, but it carries whatever the box has, compromised binaries
included. It can't be checked against anything, and a second machine can't reproduce it. Signed packages from the
vendor are verifiable byte for byte, and the carried layer names everything else.

The honest limit: package scriptlets aren't bit-reproducible (timestamps, generated caches). So wend pins the
inputs, records the output's ID, and runs the image by that ID. A rebuild re-verifies every input, not every
output bit.

**When.** The shelter is built from the box as it is on `keeps_runtimes_on`, never from the release the route starts
on. On a route that starts there (an EL7 box; a bullseye or jammy start) that's before the first hop. On a longer
one (etch to trixie) it's the pre phase of the hop that drops Python 2, because only then do `/proc/<pid>/maps`,
the imports, the package names and the changed files describe the userland the shelter replaces; an etch box's are
Python 2.4's and etch's. Before the first hop the plan checks what it can already: that the vault serves that
release's index (reachable only: the box can't check its signature until it trusts that release's keys), the free
space, and the app-level choices (programs, mounts, connections, cron lines), which the
owner confirms then. If the drop hop's facts need anything the confirmed plan didn't list (a new mount, a new
connection), `shelter.build` refuses, and the walk stops there: the box sits on `keeps_runtimes_on` with its app
running natively, which is exactly the "stop" option.

**No unbuilt infrastructure.** The vendor's archive is the only source until the archive (wend#19) exists. If it's
gone, or serves other bytes, the shelter is refused before the box is touched (on a longer route, before the drop
hop touches it, with the box left on `keeps_runtimes_on`), the same rule a route follows (open
question 12), and there's no `--live` escape, since there's nothing live to fall back to. Once a vault dies, a
shelter can't be rebuilt from anywhere else, so #19 matters more with shelters: packages a proven shelter's lock
names belong in the pinned archive, like a route's.

### How it runs on the host

Podman, from the landing release's own repos. Read from the indexes on 2026-10-13: every release where Python 2
drops has podman 4.3 or later: alma9 and alma10 (5.8.2), bookworm (4.3.1), trixie (5.4.2), noble (4.9.3) and
resolute (5.7.0). Their kernels are all 5.14 or later. Idmapped mounts need 5.12 for ext4 and XFS and 5.15 for
btrfs, and don't work on NFS at all, so preflight checks each data directory's filesystem and refuses one that
can't be idmapped. The image is built locally: the unit's `ExecStartPre=` imports the built root once
(`podman import`, tagged `localhost/wend-shelter-<app>:<the lock's image digest>`), and the unit runs that tag. There's no
registry. Its size counts toward the hop's `disk_mib`, and a `/var/lib/containers` on XFS with `ftype=0` (overlay
storage refuses it) fails preflight.

wend writes a plain systemd unit, not a quadlet (bookworm's podman has none):
`/etc/systemd/system/wend-shelter-<app>.service` runs `podman run --rm --name wend-<app> ... localhost/wend-shelter-<app>:<digest>
<the program's own command line>`. It's a harness-owned path, and the unit's header says what it is and that its
userland is unpatched.

Defaults, which the plan shows and the run records:
- `--read-only` root, with a tmpfs `/tmp` and `/run`
- a fixed user namespace, so the container's root isn't the host's root: `shelter.start` reserves a 65536-id range
  for the shelter in `/etc/subuid` and `/etc/subgid` under the name `wend-<app>` (a harness-owned line, so nothing
  else allocates it), records it in the run, and runs with explicit `--uidmap`/`--gidmap` over that range. Not
  `--userns=auto`, which picks a new range at every start. The program runs as its own uid inside, which maps to the
  same unprivileged host uid every time
- data mounts with `:idmap`, so the program writes them as the host uid that owns them now, and nothing on the
  host is chowned
- `--cap-drop=all` (plus `NET_BIND_SERVICE` only if it listens below 1024) and `--security-opt no-new-privileges`
- podman's default seccomp profile, and its default MAC confinement: SELinux `container_t` on EL, the
  `containers-default` AppArmor profile on Debian and Ubuntu. On EL the data directories the app alone uses get
  `container_file_t` (`:Z`), recorded in the run and restored (`restorecon`) when the shelter is removed. A
  directory the app shares with the host (a database's) is never relabeled; see sockets below
- no podman or docker socket, and no host `/dev` beyond podman's default
- **Network** (private by default, and every connection is an explicit opt-in). The shelter gets a
  podman network of its own (`wend-<app>`, a fixed subnet), and wend adds an nftables table of its own
  (`inet wend_shelter`) that drops everything leaving that subnet except the destinations the owner opted in
  to, since podman itself has no per-destination allow-list. It also allows `ct state established,related`
  (replies on published ports) and DNS to the network's gateway (aardvark-dns) when the network has DNS. It has to
  fail closed: netavark's own rules allow all egress, so a shelter started without the table is quietly public.
  The table is loaded by a wend unit (`wend-shelter-net-<app>.service`, a oneshot that stays active, ordered
  `After=nftables.service firewalld.service`, with `PartOf=` and `ReloadPropagatedFrom=` on `nftables.service` and
  an `ExecReload=` that loads the table again), and the shelter's unit has `BindsTo=`, `PartOf=` and `After=` on it (`BindsTo=` stops the shelter when the table goes; `PartOf=` starts it again when the table unit restarts). After a
  reboot the shelter doesn't start unless the table loaded. Restarting `nftables.service` restarts the table unit
  and, through `PartOf=`, the shelter with it, after the table is back. Reloading it (which runs `flush ruleset` on Debian and EL alike)
  reloads the table unit right after, which puts the table back, and that reloads the shelter's unit
  (`ReloadPropagatedFrom=`), whose `ExecReload=` is `podman network reload`, since the flush took netavark's port
  forwarding too; between nftables' flush and that reload the
  shelter's egress is open for a moment, and the spec doesn't pretend it isn't. A hand-typed `nft flush ruleset` still drops the table under a running
  shelter, which systemd can't see; the spec doesn't claim otherwise. Runtime preflight records the TCP connections and
  listening sockets each sheltered program actually has (`/proc/<pid>/net/tcp*` matched to its socket inodes,
  sampled while facts are gathered), and the plan prefills the opt-ins from them, so "private" rarely breaks a
  working app. A connection that only happens at 3 a.m. won't be seen, and the plan says so. Ports the program
  listens on are served on the same host address: `0.0.0.0` is published (`-p`). On `127.0.0.1` the program
  listens on the shelter's own loopback, which podman's forwarding never reaches, so a wend socket unit
  (`wend-shelter-port-<app>-<n>.socket`) listens on the host's `127.0.0.1` and its `systemd-socket-proxyd` waits for the
  shelter's process, then joins its network namespace only (`nsenter --net`, as `nobody`) and connects to the shelter's
  loopback. A program bound to one of the host's other addresses can't start in the shelter, where that address
  doesn't exist, and the plan refuses it.
- **Host services.** Inside the shelter, `127.0.0.1` is the shelter's own, not the host's. A unix-socket client
  (MySQLdb's `host="localhost"` uses the socket at its compiled-in path) is served by a wend-written
  `systemd-socket-proxyd` unit on the host that listens on a socket in `/run/wend/shelter/<app>/` and connects to
  the real one per connection. That directory is mounted where the shelter's library looks, so a MariaDB restart
  doesn't strand it and the database's own directory is never mounted. The proxy runs as the app's host user
  (`User=`, recorded in the run), never as root: MariaDB 10.4 and later give `root@localhost` `unix_socket` auth,
  so a root proxy would hand anything in the shelter database root with no password, and an app that logs in by
  `unix_socket` as its own user keeps working. On EL the proxy's directory is app-only, so it gets
  `container_file_t` (`:Z`) like the app's other directories. The app inside runs as a mapped uid, and that directory has no `:idmap`, so the proxy's
  socket and its directory are owned by that mapped uid (`SocketUser=`, mode 0600; the directory 0700), not by
  the app's host user. The mapping is fixed (above), so that uid is the same at every start. It and the mapped gid
  get a host system user and group, `wend-<app>`: systemd refuses a `SocketUser=` with no passwd entry (EL9's 252
  fails the socket with 217/USER, found on the VM tier), and `ps` and `ls` then name the shelter. `shelter.start`
  refuses when that name exists with other ids, or those ids belong to another name. The shelter's `:Z` relabels
  the directory and the socket `container_file_t`, which PID 1 (`init_t`) may not unlink, so a restart of the socket
  unit would fail with "Address already in use" under enforcing (found on the VM tier). The socket unit therefore
  removes and recreates its directory first (`ExecStartPre=`, run unconfined), and the shelter, which `Requires=`
  it, restarts with it and mounts the new one. The VM tier's enforcing window restarts each socket unit to prove
  it.
- **The proxies under SELinux.** Every proxy above, socket and port alike, starts from a script of wend's in
  `/usr/libexec/wend/` that execs the stock `systemd-socket-proxyd` (the port proxy's being its
  wait-then-`nsenter` script). The script is `bin_t`, so systemd starts it unconfined (`unconfined_service_t`), and
  an unconfined domain execs the proxy without a transition; a copy of the binary would do the same but keep the
  old `libsystemd-shared` past the next hop's upgrade. Started directly, the stock binary runs as the
  confined `systemd_socket_proxyd_t`, which on EL9 may not accept a socket another unit hands it, read `/proc/<pid>`
  to join the shelter's namespace, connect to an arbitrary port, or reach MariaDB's socket (all found on the VM
  tier under enforcing). Writing policy for each app's ports and services would be a module per shelter; the trade
  is that the host side of the bridge is unconfined, like any other root-installed service started from
  `/usr/libexec`. It runs as the app's host user (socket proxies) or `nobody` in the shelter's network namespace
  only (port proxies), and the app itself is still the confined `container_t` process; nothing in the shelter can
  reach the proxy except through the one socket or port it serves. `container_t` may `connectto` an unconfined
  service only by one rule, which `shelter.start` loads as a CIL module (`wend_shelter`, written to
  `/etc/wend/shelter/wend_shelter.cil`, loaded with `semodule -i`, no policy compiler needed) where SELinux is on:
  `(allow container_t unconfined_service_t (unix_stream_socket (connectto)))`, held by a constraint in the same
  module to the same level on both ends. The socket proxy runs at its shelter's level (`SELinuxContext=`, which is
  also the label systemd gives its socket), every other unconfined service at `s0`, and every other container at a
  level of its own, so the rule reaches one shelter's socket proxies from that shelter and from nothing else on the
  host: the owner's other containers gain nothing, and one shelter can't reach another's proxy. The constraint
  would also hold any later rule that lets `container_t` connect to an unconfined service to matching levels.
  A box with SELinux disabled gets no module; turning SELinux on later means running `shelter.start` again.
- **One SELinux level per shelter.** `:Z` relabels a volume with the level of the container that mounts it, and
  podman picks a random level per container, so a cron container's start relabels the data directory away from the
  running shelter (the data-directory write denials on the VM tier, gone with one level). All of a shelter's
  containers, and its socket proxies, run at `s0:c<K>,c1023`, `K` being its id-range slot (above, 0 to 255), so
  they share their volumes and no two shelters share a level.
- **TCP host services.** An app that reaches a host service over
  TCP on `127.0.0.1` can't be served without changing its config, which wend doesn't do: the plan names it, and the
  owner's choices are `network = "host"` (opted in by name, recorded in the run as an exception) or editing the app.
- **Mounts:** the code tree read-only (`:ro,idmap`, so a tree only its owner can read stays readable). Data directories read-write (`:idmap`): the ones the program has open for
  writing, or has as its working directory, under `/var`, `/srv`, `/home` or `/opt`. The plan lists them, and the
  owner confirms them. Data is never copied, so the host and the shelter see the same bytes, and the box's backups
  keep working.

### The ops

Shelters aren't fixes: no route pins them, since what to shelter is one machine's own business. They're steps the
client adds to a walk from the owner's confirmed plan (`wend preflight ROUTE --shelter`), typed like every other
step (no shell), each with preconditions and checks, and recorded in the run's `shelters`:

| op | when | does |
|---|---|---|
| `shelter.build` | on `keeps_runtimes_on`: before the first hop, or in the pre phase of the hop that drops Python 2 | builds the root and the lock (above); needs the free space the plan computed |
| `shelter.prove` | right after `shelter.build` | in a throwaway copy of the machine (so its real data is never written), does `stop-native` and `start`'s units, then runs the app's checks against the shelter root in the sandbox (a chroot); refuses the route if a check that passes natively fails there |
| `shelter.stop-native` | pre phase of the hop that drops Python 2, before the fixes' pre steps | stops (where systemd runs that root) and disables the app's own service unit, and comments out each cron line that runs a sheltered program with a `# wend-shelter:<app>` marker (the original goes under `/var/lib/wend/saved`; the precondition is the line's exact text). Nothing is deleted, so rolling the hop back restores both |
| `shelter.start` | post phase of that hop, after the fixes' post steps | installs podman (an `install` op of the landing release), reserves the shelter's subuid/subgid range, the network, the nftables unit and the socket proxies, loads the image, writes and enables the unit, and writes one systemd timer per moved cron line |

A cron line becomes a timer only when its schedule translates exactly to `OnCalendar=`. Every standard five-field
schedule and the `@daily`-style names do. When both day-of-month and day-of-week are restricted, cron runs on
either, so that becomes two `OnCalendar=` lines; when one of them starts with `*` (`*/2`), cronie wants both, so it's one line. `@reboot` becomes a oneshot at boot (not built yet: the checker
refuses it for now). Anything else is refused at plan time.
The timer runs the same command in the shelter (`podman run --rm ... localhost/wend-shelter-<app>:<digest> <command>`) with the crontab's
`PATH` and environment (built so far: cron's default `PATH=/usr/bin:/bin`). Cron's `MAILTO` doesn't carry over:
output goes to the journal, and the plan says so.

**The plan** is a table per app, `[shelter.<app>]` (on a CI box, in its box file):

| key | type | means |
|---|---|---|
| `code` | absolute path, under `/opt`, `/srv`, `/home`, `/var` or `/usr/local` | the code tree, mounted read-only |
| `data` | list of absolute paths, under `/opt`, `/srv`, `/home` or `/var`, none inside `code` | data directories, mounted read-write (`:idmap`) |
| `user` | user name, not `root` | the app's own user: its passwd and group lines are carried, and it runs as that uid inside |
| `unit` | `<name>.service` | the app's own unit; its `ExecStart` (an absolute interpreter, then plain words) is the command the shelter runs |
| `cron` | list of `{ user, line }` | each crontab line by its exact text: plain words only (no `%`, quotes, `$` or redirects), a schedule that translates exactly, an absolute interpreter; `user` is the app's own `user` (another user's jobs are an app of their own) |
| `sockets` | list of absolute paths, one per directory | host unix sockets the app connects to, each served by its own proxy and mounted at its directory |
| `ports` | list of `"address:port"`, on `127.0.0.1` or `0.0.0.0` | served on the same host address (see Network); one below 1024 adds `NET_BIND_SERVICE` |
| `egress` | list of `"address:port"` | the TCP destinations the owner opted in to; the egress table accepts each and drops every other new connection |

An app's name is lowercase letters and digits. Paths have no `.` or `..` segments. Unknown keys are refused, so a
plan can't ask for host networking or privileges until the spec says how that's recorded.

**What a run records.** Each built shelter goes into the run's `shelters`:
`{app, release, lock_sha256, image, tier, confinement_proved, subuid}`. `tier` is `chroot` on CI and
`confinement_proved` is `false` there (see Proof); `subuid` is the range `shelter.start` reserved (`start:count`),
recorded in the run and not in the lock, since the lock describes the build and the range belongs to the machine.
The lock is `/var/lib/wend/shelter/<app>/shelter.lock`, TOML (`wend.shelter-lock/0`): `release`, `key`, `image`,
the carried `user` and `group` lines, `[indexes]` (each repo's `repomd.xml` by origin URL), and one
`[[packages]]` (`nevra`, `sha256`, `url`) and `[[carried]]` (`path`, `sha256`, `why`: `unowned` or `changed`) per
entry.

`shelter.start`'s checks are the app's own checks against the real container, on the box, under the real
confinement (userns, MAC, read-only root, dropped capabilities, the network table). If they fail, the hop rolls
back like any other, and the app comes back natively on the release before. Rolling back undoes everything
`shelter.start` added, not only the container: the units, timers, proxies and nftables table are removed, the
subuid/subgid line goes, and the `:Z` labels are restored. That's the one place confinement is
tested on the machine itself; `shelter.prove` only proves the userland.

The app is down from `stop-native` until `start`: one hop, including that hop's reboot. That's the same window the
route itself causes.

### Proof

- **The box lands after the chroot tier, never before it**: `el/centos7-pyapp` is a centos7-lamp sibling with a
  Python 2 service (a small BaseHTTPServer JSON API reading MariaDB through MySQLdb over the socket, writing to
  `/var/lib/billing`), its systemd unit, a nightly cron job, and the owner's plan (`[shelter.billing]`). Its `app`
  check starts whatever the box has enabled for billing (its own unit, or the shelter's) from the unit's
  `ExecStart` and fetches the row over HTTP; its `cron` check runs the crontab line, or the shelter's timer once
  that has taken the line over, and reads what it wrote. Neither names Python. The box can't land alone: on the hop
  to alma9 its app and cron checks go red, and a walk rolls back any hop that loses a passing check, so every
  centos7 route would fail on it. Every box that starts at centos7 is a decoy for every centos7 route, so that would
  turn all of them red in CI. And CI judges a PR with the base branch's tools, so the tools land first.
- **CI (chroot tier):** chroots run no podman, so CI proves the userland only. While a box's checks run, the
  harness puts a stand-in at `/usr/bin/podman` (`tools/podman_standin.sh`, never shipped to clients and never left
  in the box) whose `run` chroots into the shelter root whose lock has the tag's digest, with the unit's declared mounts (code
  read-only, data read-write, the socket proxies' directories), and runs the command as `--user`, ignoring every
  confinement flag. It has no image store (`import` and `image exists` do nothing): the tag's digest is the built root's, and the
  root stays at its path. A check that starts a shelter's socket proxies starts them from their units, as the proxy's `User=`, since
  a chroot has no systemd and no user namespace. `app`, `cron` and every other check must stay green at every hop.
  It proves nothing about confinement, and the run says so (`tier = "chroot"`, `confinement_proved = false`).
- **VM tier (remi):** the real thing, and the only proof of confinement. centos7-pyapp to alma10 under podman, with
  SELinux enforcing, real reboots, and checks green after each hop. The same goes for a Debian bullseye-to-trixie
  box (AppArmor) later. `tools/vm.py replay` (as root, since the build takes a chroot) runs `shelter.build` and
  `shelter.prove` exactly as the chroot walk does, on a chroot copy of the box at the route's start (the VM tier
  shelters only from the start release so far), and copies the built shelter into the guest before the drop hop.
  Then it checks that the code and data directories are on a filesystem that can be idmapped (ext4, XFS, btrfs; so
  the box's `[vm].root_fs` is `xfs`), and that the guest's passwd and group lines for the app's user and every carried
  file are the ones the lock was built from. The drop hop then runs `stop-native` and `start` in the guest like any other step, and after
  that hop's reboot and every later one, besides the box's checks, the guest has to pass the confinement probe
  (`shelter.vm_probe`): the table unit, the shelter and its timers enabled and active and the native unit and cron
  line off; one `wend-<app>` range in `/etc/subuid` and `/etc/subgid`; the container running the image tagged with the
  lock's digest; the program's host uids and gids the app's own plus the range's start; no capabilities beyond the
  plan's, `no_new_privs`, and the `container_t` domain; the root and the code tree read-only, and a file it writes in a
  data directory owned on the host by the app's own user (idmap); each port answering on its address and, for a
  loopback one, not on the guest's own; a TCP listener the guest reaches (the mirror shim) refused from inside; and the
  table and the published ports back after `systemctl reload nftables` and after a restart. The VM boxes install with
  SELinux permissive, so then comes an enforcing window: `setenforce 1` (a guard sets permissive again after 15
  minutes whatever happens), the shelter restarted, the box's checks again (none that passed may fail, except those the box's
  `[vm].permissive_only` names: its own stack as the owner left it on a permissive box, which the window doesn't judge),
  the probe again (which has to see enforcing, so it didn't outlast the guard), then permissive, and no AVC denial in the window may touch the shelter (its domain, its files, its units).
  The run's `shelters` say `tier = "vm"` and `confinement_proved` only when every hop from the drop hop on passed all
  of that.

### What preflight says

`wend preflight ROUTE --shelter` turns the runtime verdict into a plan. For each app it gives the programs, the
release and versions the shelter will have, the mounts, the published ports, the observed connections it proposes
as opt-ins, the socket proxies, the cron lines and their timers, the package count and the carried files, the disk
the build needs, and the landing release's podman. It also says, in words: "the shelter keeps Python 2.7.5 and
OpenSSL 1.0.2k, which get no security fixes; anything that reaches this program over the network reaches them too.
Put TLS in front of it on the host if it serves the internet."

The facts it needs (connections, listening sockets, open files, changed package files) are new fields, so they come
with a new facts schema.

The verdict is `ok` only when every own Python 2 user is in a shelter that `shelter.prove` passed.

### Refused

- shipping or patching Python 2, or putting old packages in the host's own root
- sheltering the whole box, or the distro's own programs
- host networking or privileged containers, unless the owner opts in by name (and the run records it)
- relabeling or mounting a directory the app shares with a host service
- a cron schedule that doesn't translate exactly
- a shelter on a release whose own repos have no podman
- building from anything but the vendor's archive, verified as above (or, once it exists, wend's pinned archive);
  a lifeboat's lifted layer is the one exception (see "Lifeboats")

## Lifeboats (draft: design only)

A route has three honest endings for a machine, in this order:

1. **Home.** The machine lands on the target release and everything on it runs natively: the route carried it.
2. **Lifeboat.** The machine lands on the target release, and whatever the target can't run keeps running in a
   sealed lifeboat on it, on the old userland it needs. That buys the owner time and gives them a way out. It isn't
   support: wend never patches what's in a lifeboat, and every report, unit file and login banner says so.
3. **Refusal.** wend stops before touching the machine and says why, and which release the machine can still reach
   with everything working.

The Python 2 shelter (above) is the first lifeboat. This section widens it from one app's interpreter to whole old
app stacks: a PHP 5 web app, an appliance's Python 2.6 + PostgreSQL 9.2 + nginx stack, a PBX. It is the same
mechanism (podman, a fixed user namespace, the platform's MAC, a private network, typed ops, the same proof tiers),
with four changes: what can go in a boat, where its files may come from, how it's sealed, and what it tells the owner
about getting out. "Lifeboat" is what the owner sees in the verdict and the report; the ops and the plan keep the
shelter's names, since it is one mechanism.

Nothing below is built, and it will change as real boxes teach us. It moves into the shelter section, piece by
piece, in the PRs that build it, the way the shelter did.

### What goes in a boat

Preflight decides per app, not per machine. An app is what the shelter already calls one (a code tree, its services,
its cron jobs, its data), widened to every service in its stack: the web server in front of it, its database, its
cache, its queue, its own daemons. An app needs a boat when the route can't carry one of those pieces and the pieces
still work on the release the boat is built from. What says "can't carry" is data, never a list of product names:

- a runtime problem (runtime preflight: Python 2 today; PHP 5 and Java 6 are the same check with another table);
- modules the landing release's interpreter won't load (a `python2.N` site-packages tree under a minor bump, #140);
- unowned native code whose sonames the landing release doesn't ship (#140's `native` trees and `runtimes.<release>.libs`);
- a running service whose package the landing release, with the route's add-on repos, doesn't ship by name or by
  what leapp-data says it becomes (Asterisk on EL10). This one is new: it reads the same pinned indexes.

Everything else on the machine rides the route as usual. The boat holds only what has to stay old, and the host
serves the rest (its database, if the route carries that database fine; its reverse proxy, if the owner opts in, see
"The seal").

Never in a boat: a kernel, a kernel module or the hardware it drives (a telephony card's DAHDI drivers, a vendor
RAID tool); the distro's own programs the host still runs (the route carries those; a boat's httpd or PHP is its own
copy from the vault); the machine's package managers, a second one included (the boat is
immutable, and nothing inside it installs anything); anything that has to run as the host's root or see the host's
`/etc` (the box's own sshd, its firewall); and the whole machine. An app that needs any of those is refused, by name.

### The release a boat is built from

The boat's base is the **last release on the route where every piece of the app still runs**: the release before the
first hop that breaks any of them. That's the shelter's `keeps_runtimes_on` for a Python 2 app, and for an appliance
whose stack dies at the CentOS 7 hop it's CentOS 6 itself, so the boat is built before the first hop. Like the
shelter, it's built at that release's final versions from its vault, never at a snapshot, and it's built on the box
while the box is on that release.

### Where a boat's files come from

In this order, and each file comes from the first source that has it:

1. **Packages from the base release's vault** (or wend's archive, #19), verified exactly as the shelter's are (signed
   index, or per-package signatures on EL), pinned by digest in the lock. The set is the shelter's: the packages that
   own what the app's processes map and load, plus their dependency closure. A piece the vault carries always comes
   from the vault, even when the box's copy came from somewhere else (a second package manager's copy of `zlib`).
2. **Carried files**, as in the shelter: files the app needs that no package owns, inside the app's own trees, and
   packaged files the box has changed. The owner's code and config. Listed with their sha256.
3. **Lifted files.** Files the app needs that no repo wend can reach has at all: not the vault, not the archive, not
   a living repo. They're copied off the machine itself, into a separate read-only layer, and marked **unverified**.
   This is the one place wend builds from the box, and only for lifeboats (decided 2026-10-16; the shelter's rule,
   "never from a copy of the box", still holds for everything a repo can supply). Typical lifted trees: a stack a dead
   repo installed (an appliance vendor's Conary troves, a third-party yum repo that's gone), a hand-built `/opt/pgsql`,
   a vendored library under `/usr/local`.

Each lifted file gets a trust note in the lock, the strongest one that applies:

| `trust` | means |
|---|---|
| `db` | the box's own package database (rpm's, dpkg's md5sums, a second package manager's) records a digest for this path, and the bytes match it. That proves the file hasn't changed since it was installed. It says nothing about where it came from |
| `none` | nothing on the box vouches for it |

What's never lifted, and refuses the boat when the app needs it: setuid or setgid files and files with file
capabilities (the boat strips nothing: it refuses, so a lifted `su` can't quietly lose its bit and work differently);
device nodes; anything under `/boot` or `/lib/modules`; and a file the vault carries but whose vault bytes fail
verification (that's the shelter's refusal, unchanged: a vault that fails its own checks isn't a lifting
opportunity).

Why lifting is allowed at all: on a dead repo there is nowhere else the bytes exist, so the choice is the owner's
files or no boat. Why it's marked: the box may be compromised, and lifted bytes carry that with them. The boat's
confinement doesn't depend on them being clean (they run as an unprivileged mapped uid, `container_t`, no
capabilities), and the report lists every lifted tree with its file count and trust, so the owner sees exactly what
wend couldn't check.

The lock grows one table: `[[lifted]]` with `path`, `sha256`, `trust`, and `claimed_by` (`conary:<trove>`,
`rpm:<name>`, or none), next to `[[packages]]` and `[[carried]]`.

### What runs in a boat

A boat is a podman pod: one network namespace, so the stack's own loopback traffic (the app to its PostgreSQL on
`127.0.0.1:5439`, nginx to gunicorn) stays inside the boat and never touches the host. Each service is a container
in the pod from the boat's one image, with its own command line and its own user. The command is the service's
running main process as facts saw it (argv, working directory, user, environment), confirmed in the plan, because a
boat runs no init system: a SysV script or a supervisord config is evidence for the plan, not something the boat
runs. One wend unit starts the pod and its containers in the plan's order; cron lines become timers exactly as the
shelter does.

The plan grows from one `unit` to a list:

| key | type | means |
|---|---|---|
| `services` | list of `{ name, user, argv, cwd, env, after }` | one container each, in the pod; `after` orders them |
| `ports` | list of `"proto:address:port"` (`tcp` or `udp`), or a range for udp | served on the host, see below |
| `lift` | list of absolute paths | the trees the owner agreed to lift (facts propose them; nothing outside them is lifted) |
| `arch` | `x86_64` or `i686` | the boat's userland; `i686` needs the host's kernel to run 32-bit programs |

The shelter's other keys (`code`, `data`, `user`, `cron`, `sockets`, `egress`) keep their meaning.

**The old userland on a new kernel.** A boat's libc can be fifteen years older than its kernel. Two things are known
to bite, and preflight checks both on the landing release's kernel before the first hop: a glibc older than 2.14
(EL6 and earlier) calls the vsyscall page, which a kernel with `vsyscall=none` (on its command line, or as its build
default) kills it for, while `xonly` still runs those calls (preflight reads the landing kernel's config and command
line; the VM tier proves it); and an `i686`
boat needs the kernel's 32-bit support (`CONFIG_IA32_EMULATION`, and not `ia32_emulation=0` on its command line).

### The seal

Sealed code is old and unpatched, so the seal is the point of the design, not a detail:

- **In:** only the ports the app served before, on the addresses it served them on (facts' listening sockets), now
  with UDP and port ranges (SIP on 5060, RTP's range). Nothing else reaches the pod. An app that served the internet
  still does, because otherwise it isn't working, and the report flags every exposed port in its first lines, not its
  last. Decided (2026-10-16): same ports as before by default, not loopback-only, which would break a working app on
  purpose.
- **Front it (opt-in, recommended for HTTP).** The host's own current web server or proxy terminates TLS and proxies
  to the boat's port on loopback, so the internet talks to a patched TLS stack and HTTP parser, and the boat's old
  OpenSSL never sees a handshake. The plan offers it for every HTTP(S) port and the report recommends it.
- **Out:** nothing by default. The shelter prefills egress opt-ins from observed connections; a boat does the same,
  and every opt-in is listed in the report as a hole, with the destination.
- **Host services** reach the boat the shelter's way (unix-socket proxies), and a database the route carries stays on
  the host.
- Everything else is the shelter's confinement, unchanged: read-only root, fixed user namespace, idmapped data
  mounts, all capabilities dropped (plus the ones the plan names, each shown), `no_new_privs`, SELinux `container_t`
  at the boat's own level or AppArmor's `containers-default`, the nftables table that fails closed.

### The way out

A boat is a waiting room. The report, written to `/var/lib/wend/lifeboat/<app>/README` and printed by
`wend preflight` and `wend status`, answers four questions for each boat:

1. **What's inside:** the base release and its end-of-life date, every package and version, the carried files, and
   the lifted trees with their trust, so nothing in the boat is unnamed.
2. **What's exposed:** each published port and who can reach it, each egress hole, and the plain statement "the
   software in this boat gets no security fixes, from wend or anyone; anything that reaches these ports reaches it".
3. **What replaces it:** for each piece, what the landing release ships instead, read from data (the landing release's
   index by name and provides, and leapp-data's events on EL), never guessed: `postgresql 9.2 (lifted) -> postgresql
   16 (alma10 appstream)`, `nginx 1.x (lifted) -> nginx 1.26`, `Django 1.3 (lifted) -> none: the app needs porting`.
   "None" is a valid answer and the honest one for the owner's own code.
4. **The exit ticket:** for every database in the boat, a logical dump taken by the boat's own binaries at boarding
   and again on a timer (`pg_dumpall`, `mysqldump --all-databases`), written to the host under
   `/var/lib/wend/lifeboat/<app>/exit/`. The boat's old binaries are the only thing that can read its data
   directory; the dump is what a modern server can restore. Without it the boat is a trap, not a lifeboat.

A login banner (`/etc/motd.d/wend-lifeboat`) names each boat, its base release and how long it's been afloat. No
nagging daemon, no expiry date: when to leave is the owner's call.

`shelter.retire APP` (new op) stops and disables the boat's units and removes its network table, proxies, timers and
subuid range, and keeps the image, the lock, the data and the exit dumps until the owner deletes them. The way back to
running natively (port the app, restore the dump into the host's database) is the owner's work; wend's part is the
report that says what that work is.

### Later hops

A boat rides every later hop of the route, and later routes: it's a container, so the host's upgrade doesn't touch
its userland. Each hop's checks include the app's checks against the boat, and the confinement probe after each
reboot, as for the shelter. A later hop that breaks the boat (a kernel that drops 32-bit support, a podman that can't
import the image) fails that hop's checks and rolls back like any other hop.

### Worked examples

**A Python 2 service on CentOS 7** (el/centos7-pyapp, proved on the VM tier): a one-service boat on CentOS 7's vault
packages, nothing lifted. This is the shelter as built.

**A software appliance on CentOS 6 whose stack came from a second package manager.** The base CentOS 6.7 is rpm's
and consistent, so the machine routes like any CentOS 6 box (joining the CentOS 5 route at its el6 rung). The
appliance itself (a Python 2.6 site-packages tree, PostgreSQL 9.2 in `/opt`, nginx, an XMPP server, memcached, a
build service) came from a dead Conary repo, along with a Fedora kernel. #140's preflight says the app dies at the
CentOS 7 hop. Lifeboat: the boat's base is CentOS 6 final from the vault; whatever the vault or a living repo carries
of the stack comes from there (the per-trove diff decides which), and the rest is lifted, mostly with `trust = db`
from Conary's own database. Its services run in one pod, PostgreSQL's data directory is mounted in, nginx's 80 and 443
are published (front recommended), and the first exit dump is a `pg_dumpall` from 9.2 that PostgreSQL 16 on Alma 10
restores. The Fedora kernel and Conary itself stay behind: the host boots the route's kernel, and the route's kernel
cleanup has to learn about a kernel rpm doesn't own (#140 notes it). The host lands on Alma 10, with the appliance
afloat and every lifted tree named.

**A PBX (Elastix-style: Asterisk + FreePBX on PHP 5 + MySQL).** Asterisk isn't packaged for EL10 and FreePBX 2.x
dies on PHP 8, so it's two pieces the route can't carry. A boat on the base the box runs (CentOS 7 for Elastix 4)
carries Asterisk, httpd with PHP 5.4 and FreePBX, with SIP and the RTP range published over UDP and its web UI fronted
and LAN-only by the owner's choice. MySQL can stay on the host if the route carries its data. Refused instead when the
box drives telephony cards (DAHDI is kernel modules, and those never board). Elastix 2.5 is i386 CentOS 5: an i386
machine's in-place route ends at CentOS 6 (EL7 is x86_64-only), so that box can't host a boat on Alma 10 itself.
Its honest outcomes are refusal, or rebuild and carry onto a new x86_64 machine with an `i686` boat, which is later
work. The report's "what replaces it" says Asterisk 18 on EL9's EPEL and a chan_sip to pjsip move, which is a
migration, not an upgrade.

### Order of work

1. This section, reviewed.
2. A public box for a whole-stack boat with nothing lifted: `el/centos7-php5`, a PHP 5.4 app on httpd with MariaDB
   (the most common real case; dies at the alma9 hop on PHP 8). Plan keys `services` and `ports` with `tcp`, pods,
   chroot tier first, then remi.
3. Lifting, the lock's `[[lifted]]`, and the trust note, proved on a public box built for it (a CentOS 6 box with a
   source-built service under `/opt`, and a third-party rpm from a repo that's gone, for a `db` lift), and privately on the appliance on remi, never in CI.
4. UDP ports, the "front it" opt-in, exit dumps, `shelter.retire`, the report and the banner.
5. Preflight's fourth "can't carry" check (a running service the landing release doesn't ship).

### Open questions

1. **How big a lift is too big.** A boat lifting thousands of files with `trust = none` is mostly an unverified copy
   of the box. Should preflight refuse past a share of the boat (and say "rebuild the app instead"), or only report?
2. **Exit dump cadence and space.** Daily by default, keep the last few? A large database doubles its disk.

## VM tier (maintainer-run)

A chroot can't test kernels, bootloaders, init or networking (open question 10). `tools/vm.py` replays a route on a
box installed in a KVM guest by its release's own installer: anaconda with a kickstart for EL, debian-installer with a
preseed for Debian and Ubuntu (appended to the installer's initrd, so it needs no network to be read). Both are made from
`box.toml`: its packages, `customize` and `start_hook` (and the apt boxes' `debconf_selections`), a static address and the
release's kernel and bootloader. The box is then brought to the release's last packages, as the chroot build does
(apt: the harness's `hop.apt_conf` first, then `set_release`, `refresh` and `apt-get dist-upgrade`, which on a
frozen release pocket changes nothing the installer didn't already fetch). A box that asks for `[vm].pockets` gets
those pockets added to `sources.list` right after `set_release` (each of the release's `deb` lines again for
`{release}-{pocket}`), so its dist-upgrade brings it to their last packages. An apt box must then hold only versions
its `sources.list` offers (`apt-cache madison`), or the build fails naming them: a box with packages from another
pocket isn't the box the route starts from. Each hop compiles exactly the commands the chroot harness
runs (the same `hop_cmds`), runs them inside the running guest, and reboots it for real (checked by `boot_id`).
After every hop:

- the guest has to come back reachable over ssh on the box's port (`[vm].ssh_port`);
- its addresses, default route and resolvers have to be the ones it booted with at the start. The resolvers are
  `/etc/resolv.conf`'s, with systemd-resolved's local stub (`127.0.0.53`) standing for the upstream servers resolved
  lists in `/run/systemd/resolve/resolv.conf`, so the stub appearing on a release isn't a change. A shelter's own
  bridge isn't a change either: the address podman reports for the network `wend-<app>` (its gateway on
  `10.90.<K>.0/24`, on the interface podman names) is left out of the comparison, and any other new address counts;
- the box's checks have to keep passing, running on the live box. Roles that start their own daemons are listed in
  `[vm].skip`, and `[vm].live` gives their live forms, which ask the running services instead.

A hop ends the replay when its commands fail, when the guest doesn't come back, when its network differs from the
start, when the newest installed kernel isn't the one running (or the query names none), when the guest no longer
runs on the firmware the replay boots, or when a check that passed before it fails after the reboot. Its disk and serial console are kept.

The VM box differs from the chroot box in ways a replay can show: it has what the installer adds (`@core`, a kernel, a
bootloader, a `/boot` of `releases.<id>.install.boot_part_mib` MiB: 7.9's installer default of 1 GiB on CentOS 7, and
200 MiB on CentOS 5, a chosen size where 5.11's installer makes 100 MiB), SELinux is permissive and the firewall off (kickstart `selinux --permissive`,
`firewall --disabled`), and `customize` runs at the end of the install (after `set_release`, with the mirror shim
where the guest sees the host), before the release's last updates, not after them. The adapter gives the kickstart
lines that differ by release (EL7's `%end`, GRUB 2 and `net.ifnames=0`, so the NIC is `eth0` as the boxes expect). A Debian VM box has what debian-installer's minimal install adds (a kernel metapackage, GRUB legacy on
etch, `iproute`, which the harness's own network check reads); an Ubuntu one what trusty's installer adds (GRUB 2,
`linux-generic`). The adapter gives the preseed lines that differ by installer version (keymap, mirror country, disk
path, partman names). Ubuntu's installer always adds the release's `-updates` pocket and installs from it, so for a
release with `release_pocket_only` the harness gives the installer a mirror (a proxy on the host) that answers 404
for every `-updates`, `-security`, `-backports` and `-proposed` suite, except the ones the box lists in
`[vm].pockets`. Its static address is not the one QEMU's DHCP
hands out and the forwards point at it, so a box that falls back to DHCP is unreachable and fails the hop.

The rules:

- Maintainer-run only. It runs the guest on the host that runs it, needs `/dev/kvm`, and is never wired to a CI
  runner that a pull request can trigger. Its runs go to `.wend/vm/` (schema `wend.vmrun/0`, `runner.kind = "vm"`),
  not to `runs/`.
- QEMU user-mode networking only: nothing on the host changes. The guest reaches the mirror shim at `10.0.2.2`, and its
  ssh is forwarded to a free port on `127.0.0.1` of its own, so a host runs several VM replays side by side. They
  share the mirror shim and the installed boxes: a box is installed once per cache key (the box file, the adapter,
  and the part of `tools/vm.py` the install runs), and a run that needs a box another run is installing waits for it.
  Every run holds a shared lock on `/var/tmp/wend-shim/<port>.users` for each shim port while it lives, whatever its
  `$WEND_WORK` (a directory only root or the running user can write, or it's refused), and `tools/vm.py shim-stop PID`
  stops the shim only when no run holds its port's lock.
  Every run records the commit it ran as `head` (`commit`, and `dirty` when tracked files differ from it).
- Box: an optional `[vm]` table with `ssh_port` (default 22), `skip` (roles that start their own daemons, which a
  running box already has) and `live` (role name to shell: the live forms of those checks; every skipped role needs
  one, so skipping never drops a check), `pockets` (`updates`, and `security` and/or `backports` beside it, once each: the box
  starts from those pockets as well as the release pocket, as a fully updated server does; `backports` because
  trusty's installer also writes an active `-backports` line, which a `customize` that runs `apt-get update` fetches,
  and apt pins it `NotAutomatic`, so it adds an index, not packages; only on a release whose install is
  `release_pocket_only`; wend#47; the route's hops still read the release pocket only, so `pockets` changes the start,
  never the walk) and `root_fs`, the filesystem the installer makes for `/` and `/boot`, one
  its start release's installer offers (`releases.<id>.install.root_fs`; CentOS 7: `ext3`, `xfs`, v5 and EL 7.3's
  default, or `xfs-v4`, `-m crc=0 -n ftype=0`, EL 7.0 to 7.2's on-disk format made by 7.9's installer). Without it the
  kickstart makes ext3. The checker holds it to all that, and the build checks the installed box has its `root_fs`
  (`xfs_info`'s `crc` and `ftype`). `permissive_only` names roles or live checks the box's own stack passes only with
  SELinux permissive, as its owner ran it; the shelter's enforcing window doesn't count them (the run lists them as
  `enforcing_excused`).
- A route that ends with `prune_obsolete` gets the chroot walk's prune after its last hop: the packages the target
  no longer ships are purged and autoremoved, the set split in halves whenever a step fails or a check is lost, and
  each attempt first drops what an earlier autoremove already took. Each attempt runs on its own overlay of the last
  good disk and reboots; a kept attempt is the next base. As in the chroot walk, a removal the prune drops never fails
  the run; a guest that doesn't come up from the last good disk, or a box it can't read, does. It records what was obsolete before and after.
- Adapter: `releases.<id>.install` names a start release's installer. EL: its `tree` through the shim and the
  `pxeboot` dir in it (the shim answers single byte ranges, which anaconda uses to read package headers), `packages`
  (what the install needs besides the box's: the bootloader), `ks_end` (sections end with `%end`), `nobase`
  (default on: `%packages --nobase`, which EL8's installer refuses), `repos` (trees besides `tree`, each a kickstart
  `repo`: EL8's AppStream), `append`
  (kernel arguments for the installer and the installed system) and `ifname` (the installer's boot line names the NIC
  by MAC, `ifname=eth0:<mac>`, so EL7's installer pins the ifcfg to it with `HWADDR`, as a box booted that way has)
  and `boot_part_mib`, the size of the `/boot` partition in MiB (CentOS 7: 1024, 7.9's installer default; 200 when
  unset). Not `[hardware.<release>].boot_mib`, which is the space one kernel takes in `/boot`. apt (Debian,
  Ubuntu): the `netboot` dir under the start release's mirror, `preseed` (the version's own preseed lines),
  `packages` (what the install needs besides the box's), `release_pocket_only` and `late` (shell the installer runs
  at the end, for the bootloader's serial console). `disk` (`virtio`, `lsi53c895a`) and `nic` (`virtio`, `e1000`) name
  the emulated hardware, default virtio, and `uefi` that the installer installs on UEFI firmware: the machine keeps them through every hop, so the start release's kernel has
  to know them (etch's has no virtio). `hop.kernel_newest` prints the newest installed kernel as `uname -r` names it;
  after every reboot it has to be the running one.
- Firmware: BIOS (SeaBIOS) by default; `--uefi` boots every guest on OVMF (UEFI without Secure Boot) instead. The
  box is built and cached apart from its BIOS build, the installer runs in UEFI mode (QEMU hands it the installer's
  kernel as an EFI stub), the installer makes an EFI system partition (kickstart `part /boot/efi`; debian-installer's
  atomic recipe makes one itself), and each disk carries its own copy of the NVRAM, made from the one before it, so a
  hop boots from the boot entries the last one left. After the install and after every reboot the guest has to still
  run on UEFI (`/sys/firmware/efi`), and each hop records the boot chain it left (`efibootmgr -v` and the hash of
  every `.efi` on the system partition). A start release whose installer can't install on UEFI isn't built:
  `releases.<id>.install.uefi` says it can (trusty, CentOS 7; not etch or CentOS 5, whose installers predate UEFI on
  x86-64, so no machine running them boots that way). A run records its `firmware`, and resumes only a run on the same.
- Disk layout: by default anaconda makes `/boot` (the release's default size), swap and `/` as plain partitions on one
  disk. Three install choices, given before the command, change that for an EL7-and-later kickstart (a fourth,
  `--no-hwaddr`, the network profile): `--root-fs`
  overrides the box's `root_fs` (one the installer makes); `--lvm` lays the disk out as EL7's own autopart does
  (`/boot`, then one PV in a volume group
  `centos` holding `swap` and a `root` that fills it, so no extents are free; on CentOS 5, whose kickstart names its
  LVs in `releases.<release>.install.lvm`, `VolGroup00` with `LogVol01` and `LogVol00`, as its installer does); `--extra-disk GB` (repeatable) adds an
  empty disk on the same controller, which anaconda never touches (`ignoredisk --only-use`) and which carries its own
  overlay through every hop like the first. They are install choices, not boxes: a chroot box has no disks, so the box
  and its checks are the same either way. A layout is built and cached apart, a run records its `layout`, and resumes
  only a run with the same. This is how the VM tier makes a CentOS 7 machine installed from 7.0 to 7.2 media (`--root-fs
  xfs-v4 --lvm`) and gives rebuild and carry its room (`--extra-disk`, see `docs/rebuild-and-carry.md`).
  `--no-hwaddr` boots the installer without `ifname=` (for a release whose `install.ifname` is on), so its ifcfg has
  no `HWADDR`, as a plain kickstart or interactive EL7 install writes it (wend#74); the build fails if the installed
  `ifcfg-eth0` has one anyway. It is an install choice too: the chroot box writes its own profile.
- `replay ROUTE BOX --from RUN HOP` starts from an earlier run's disk after the hop before HOP, to iterate on a late
  hop. It only resumes from a hop that passed on the same box, logs in with the ssh key of the box build that disk
  came from, and is recorded as `partial-pass` at best, never `pass`.
- A reboot is proven by a new `boot_id`. The harness retries reading the boot it starts from five times; if it
  still can't, it reboots anyway and proves the new boot by an uptime shorter than the time since the reboot was
  sent, and each failed read (ssh exit code and output) goes in the hop's `reboot_notes` (`mid_reboot_notes` for a
  hop's middle reboot). A guest that answers no poll in the two minutes after the reboot gets up to five more
  minutes to answer, and each silent poll is noted. When the old boot is still the one answering (the first boot
  onto systemd: PID 1 is still the old init, and the new release's `reboot` can't talk to it), the harness stops every
  process (`kill -TERM -1`, then `-KILL`), syncs, remounts read-only and resets with sysrq, and records
  `reboot_forced`. Stopping the processes first matters: a reset with the daemons up leaves every replaced library
  and socket as an orphan inode, and EL7's kernel, cleaning those up at its first mount of the ext3 root, leaves
  extended-attribute reference counts too high. A userspace fsck cleans the same orphans correctly, but EL7 mounts
  the root before any fsck, and EL9's forced fsck then stops the boot. Only PID 1's own files stay orphaned, and
  those come through clean. A client on a real machine must stop the processes the same way.

## Relocate (VM tier for now)

Relocate is the first half of rebuild and carry (`docs/rebuild-and-carry.md`): copy the running root onto a fresh
filesystem beside it, boot the copy once on trial, keep the old root untouched as the rollback. It's for machines a
route's kernels can't read: CentOS 7 installed from 7.0 to 7.2 media has XFS v4 on `/` and `/boot`, which EL10's
kernel can't mount, and XFS has no in-place conversion. The copy is the same machine on XFS v5 (made by the running
release's own `mkfs.xfs`, so every kernel from the running one on mounts it); routes then walk it as they walk any box.
Harness work, like the kernel steps: the adapter's `[relocate]` table holds the commands, fixes can't reach them, and
it's reviewed as a spec change. Built for EL7 on BIOS with GRUB 2, `/` on LVM, `/boot` a plain partition, and the
new root on a whole empty disk the owner names. Everything else is refused.

The steps, in order, each a list of scripts that run inside the booted machine and stop at the first failure (`set
-e`), then `cleanup` whatever happened:

- `preflight` refuses, naming why, before anything is written: UEFI; no GRUB 2 for BIOS or no `rsync`; `/boot` not a
  plain partition of its own; `/` not on LVM; a stub or a `wend-root` LV from an earlier relocate; LUKS anywhere;
  any other filesystem mounted from a disk (carrying more than `/` and `/boot` isn't built, and one left in place
  would be shared by both roots, so the trial would write the rollback's data); an XFS filesystem in fstab that isn't
  mounted; a newer kernel than the running one, or yum running; and a new disk that isn't a whole disk
  with no partitions, no signature, not mounted, not a PV, not the boot disk, and at least 1.2 times what `/` and
  `/boot` use plus `{route_mib}` (what the route's hops need). It writes the facts the later steps and boots read
  to `/var/lib/wend/relocate.env` (the volume group and the old LV, the boot disk, the old `/boot`'s UUID, the new
  disk), which the copy carries.
- `refresh`: the running release's own `grub2-install` onto the boot disk, with the old `/boot/grub2` as its prefix.
  EL 7.0 and 7.1's `core.img` can't read XFS v5, and every module under a prefix has to come from `core.img`'s build.
  It's the only write to the boot disk's MBR, and it runs before the copy so the copy carries the same modules.
- `place`: the new disk becomes a PV in the root's volume group, with one LV `wend-root` filling it, made XFS v5
  (`-m crc=1 -n ftype=1`, checked) and mounted at `/run/wend/new`.
- `copy`: rsync (`-aHAXS --numeric-ids`) from a bind mount of `/` (which shows what mounts hide and nothing mounted
  under it), and the old `/boot` into the copy's `/boot`. Once live, then again with `--delete` after every running
  service is stopped except what keeps the machine reachable and safe (sshd, NetworkManager, firewalld, auditd, dbus,
  polkit, systemd's own). They stay stopped until the trial boot, so downtime is the second pass and one boot.
- `fixup`, in the copy only: fstab names the new root by UUID and drops `/boot` (a directory in the copy now); the
  kernel line's `rd.lvm.lv=` names the new LV instead of the old; every kernel gets an initramfs that isn't host-only
  (an old one activates only the old LV), and one that can't is dropped from the copy unless it's the running kernel;
  `grub2-mkconfig` writes the copy's config, which has to boot the new root; SELinux labels are fixed offline from
  outside (`setfiles -r`), never with `/.autorelabel` (EL7's relabel reboots, ending the trial).
- `flip`: the old `/boot/grub2/grub.cfg` moves to `grub.cfg.wend-old` and a stub takes its place. The stub's entry 0
  finds the new root by UUID and sets `prefix` there before loading its config (so every module and the `grubenv`
  after that are the new root's); entry 1 loads the old config as it was. The stub keeps its own variables in the old
  `/boot`'s `grubenv`, never the configs' `saved_entry` or `next_entry`: `wend_next`, if 0 or 1, is a one-shot boot
  GRUB clears before taking it; otherwise `wend_rollback` from a `grubenv` block of the new root's own
  (`/var/lib/wend/relocate.grubenv`), which means the old root when it says 1; otherwise `wend_default`, and the old
  root unless it says 0. If entry 0 fails inside
  GRUB, it falls back to entry 1 (`fallback=1`), which first puts `root` and `prefix` back to the old `/boot` as GRUB
  found it (a failed entry 0 may have moved them), so a remote machine doesn't sit at a menu. The flip leaves the old
  root the default, arms the trial (`wend_next=0`) as its last write, then freezes and thaws `/boot` so the XFS log
  is written home (GRUB doesn't replay it). Nothing on the old root changes.
- `release` unmounts everything under `/run/wend` and checks nothing is left. `cleanup` does the same after a failure,
  and starts the stopped services again if the flip never happened.

Between boots: `booted` says which root is up (`new`, which must be XFS v5, or `old`); `arm` tries the new root once
more from the old one; `commit_trial` makes the new root the default (it mounts the old `/boot`, which the copy's
fstab doesn't); `rollback_next` boots the old root once. Those three mount the old `/boot`, which a kernel without
XFS v4 (EL10's) can't. So `rollback`, from the new root on any release, writes `wend_rollback=1` into the new root's
own `grubenv` block (by hand, so no release's `grub2-editenv` is needed, then a freeze of `/` from one process, since
GRUB doesn't replay the XFS log). GRUB can't write to LVM, so that rollback sticks until `forward`, from the old root,
mounts the new LV and removes the block.

The copy's `fixup` chroot sees the machine's own `/dev` (bound), `/proc` and `/sys`, all unmounted before the step
ends and checked against `/proc/self/mountinfo` before the labels are fixed. That's an exception to the sandbox rules
above, and it's reachable only from the adapter's `[relocate]` table, inside the machine being relocated, never a box
on a harness host. Nothing under `/run/wend/new` is ever deleted: a failed relocate leaves the LV for a look.

`tools/vm.py --extra-disk GB relocate BOX` proves it on a fresh copy of a box (with `--root-fs xfs-v4 --lvm`, a 7.0
to 7.2 install), the new root on the first extra disk. After the steps, each followed by which root came up, the box's
checks, its network and its firmware: the trial boot (new), a hard reset from outside, as a power cut (old: the trial
was one-shot), the trial again (new), commit to the trial and reboot (new), a one-shot rollback (old), and one more
reboot (new), then `rollback` (old, and old again after another reboot) and `forward` (new). Its run goes to
`.wend/vm/` (schema `wend.vmrelocate/0`), with each stage, how long the steps and the trial boot took, and what the new
root uses.

**Walking a route on a relocated root.** `tools/vm.py --extra-disk GB --relocate replay ROUTE BOX` (with `--root-fs
xfs-v4 --lvm` for a 7.0 to 7.2 install) relocates the box before the route's first hop: the steps, with `{route_mib}`
the route's room (every hop's `hardware.<release>.disk_mib` plus its `boot_mib`, since `/boot` is a directory on the
new root now, plus the `releases.<release>.transit.boot_mib` of each release a pinned fix passes through with
`rpm.transit`), the trial boot, and `commit_trial`. Every hop then walks the new root as it walks any box, and each
hop's reboot must also come up on it (`booted` is `new`, recorded as the hop's `root`). After the last hop, `rollback`
must boot the old root with the box's checks passing as they did before anything moved, and `forward` the walked root
again with them. The run (`wend.vmrun/0`) records `relocate`: the new disk, `route_mib` and every stage. The relocated
root keeps the refreshed EL7 GRUB (`core.img` and the modules on the old `/boot`, the prefix the stub sets into the
new root) for the whole route: the in-place route already boots Alma 10 on EL7's GRUB (BIOS, VM-proven), and nothing
reinstalls GRUB on the way. `tools/test_relocate.py` checks preflight's refusals, the copy's fstab and
kernel line, and the flip (with the real `grub-editenv`, and `grub-script-check` on the stub) without a machine.

Between the flip and commit, the old root must not install a kernel: its grubby and `grub2-mkconfig` write through
`/etc/grub2.cfg` to the stub. The harness never does (the services are stopped and the trial boot follows the flip
at once); a client will have to hold kernel updates while the stub is in charge.

**On GRUB legacy** (CentOS 5 and 6), the adapter's `[relocate_grub_legacy]` table has the same steps and commands,
and `tools/vm.py` takes it when the machine boots from `/boot/grub/grub.conf` with no `/boot/grub2`. Built for BIOS,
`/boot` on a plain ext partition, `/` on one or on a plain linear LV (CentOS 5's default install puts it on
`VolGroup00/LogVol00`, CentOS 6's on `vg_<hostname>/lv_root`), and the new root on a whole empty disk the owner names
(on a CentOS 5 or 6 box the VM tier adds it after the installer). What differs from `[relocate]`:

- `preflight` reads which partitions `/` and `/boot` are by device number (EL5 has no `lsblk` or `findmnt`); a `/`
  on no partition is the device-mapper device of that number (`dmsetup ls`, which prints it `(253, 0)` on EL5 and
  `(253:0)` later), and must be a plain linear LV (no snapshot, RAID or crypt). It refuses a `/` that isn't ext3 or ext4, wants `/boot/grub/device.map` to name the boot
  disk as `(hd0)`, and takes the new disk's GRUB name from there, or the next disk after the ones it lists. The new
  disk's emptiness is its first MiB being zeros (EL5's `blkid` has no `-p`), and `{boot_mib}` (the route's
  `boot_mib` over its hops and transits) sizes the new `/boot` on top of the old one's size.
- `refresh` reinstalls nothing (the boot disk's stage1 and the old `/boot`'s stage2 stay). It writes the device map
  (the copy's `device.map`) and `/var/lib/wend/relocate-grub`, the helper every write to the saved default goes
  through, and records the old `stage2`'s inode and size: GRUB reads `stage2` by the blocks it was installed with, so
  the helper refuses a `stage2` replaced since, which would take writes GRUB never sees.
- `place` splits the disk: a first partition for the new `/boot` (ext3, labelled `wend-boot`), a second for the new
  root (the old root's type, `wend-root`), each made by the running release's `mkfs`, both mounted under
  `/mnt/wend/new` (EL5 has no `/run`).
- `copy` is one `tar --selinux --numeric-owner` pass of `/` and one of `/boot`, with every running service stopped
  but sshd, the network, the firewall, auditd, messagebus, haldaemon, syslog, LVM's and SELinux's own (CentOS 5's
  rsync has no `-X`). CentOS 6's `blk-availability` stays too: its status always says running, and stopping it
  deactivates the machine's LVs.
- `fixup`: the copy's fstab names the new root and `/boot` by label; its `grub.conf` points every entry at the new
  `/boot`'s disk, boots the new root and adds `panic=10` where a kernel line has no `panic=`; its `device.map` names
  the new disk; every kernel's initrd is rebuilt by the release's own `mkinitrd` in the copy (a kernel that can't
  get one stops the relocate, since a dead entry would stay in `grub.conf`); and the labels are checked as "Upgrading
  a running machine" describes.
- `flip`: the old `grub.conf` moves beside the stub as `grub.conf.wend-old`. The stub keeps the old header less any
  `default` or `fallback`, says `default saved`, and has entry 0 (`configfile` on the new `/boot`) and entry 1 (the
  old config). The helper sets the saved default to 1, then arms the trial with grub's own one-shot.
- Between boots, every command is the helper: `booted` compares the booted root's UUID with the two recorded;
  `arm` (`--default=0 --once`) and `rollback_next` (`--default=1 --once`) work from either root; `commit_trial`
  (`--default=0`) and `rollback` (`--default=1`, sticky) only from the new root; `forward` (`--default=0`) only from
  the old one. The helper writes `stage2`'s header bytes in place with `dd` (below), into the old `/boot`, which it
  mounts by UUID when it runs on the new root, then drops the page cache and reads them back. The route loses the
  grub shell at CentOS 7 (grub2 replaces the package), but every release has `dd` and `od`, and every kernel on these
  routes mounts the old ext `/boot`, so a rollback from the last release is the same write as any other.

`tools/vm.py --extra-disk GB --relocate replay routes/el/centos5-to-alma10.toml el/centos5-web` walks the CentOS 5
route on the relocated root, as on CentOS 7. Each hop's kernel steps write the new `/boot`'s `grub.conf`
(`hop.kernel_after` copies the default entry's `root (hd1,0)` and kernel line, EL6 and EL7 grubby read the copy's
`device.map`), and after the last hop `rollback` from Alma 10 must boot the old CentOS 5 system and `forward` the walked
root again.

`tools/vm.py --extra-disk GB relocate el/centos5-web` runs the same stages as on CentOS 7.

An LV root under GRUB legacy relocates the same way, onto a plain partition: the copy's fstab and kernel lines name
`LABEL=wend-root` instead of the LV, and the swap LV stays where it is (the copy's fstab still names it, and both
roots activate the volume group at boot). The helper tells the old root by its UUID, found through device-mapper's
name for the root's device number. `tools/vm.py --lvm --extra-disk GB relocate el/centos5-web` runs it.

On CentOS 6, `el/centos6-lamp` is a native 6.10 install (ext4 `/` and `/boot`, `vg_wendvm/lv_root` with `--lvm`, as
its installer lays out a host called `wend-vm`), and its initrds are dracut's, built by the `mkinitrd` wrapper in the
copy. `tools/vm.py --lvm --extra-disk GB relocate el/centos6-lamp` runs the stages, and `tools/vm.py --lvm
--extra-disk GB --relocate replay routes/el/centos5-to-alma10.toml el/centos6-lamp` joins the CentOS 5 route at
`centos6` ("Route") and walks its last three hops on the relocated root, with the rollback from Alma 10 to the old
CentOS 6 system after them.

Not built yet: free extents in the volume group instead of a disk, a root on a plain partition under GRUB 2, other v4
filesystems,
UEFI (the same stub on the ESP), a GRUB reinstall onto the new root (so the old `/boot` can go; until then commit keeps
it), the client's preflight verdict, `wend commit` and `wend rollback`.

## Upgrading a running machine (draft: design only)

Everything that has upgraded a real machine so far is the VM tier: `tools/vm.py` on remi, driving a guest from
outside over ssh, with a qcow2 overlay per hop to fall back on. The client a machine's owner runs reports (`share`,
`preflight`) and walks copies (`walk --root`), and refuses to walk `/`. This section moves the VM tier's live walk
into the client, so the owner runs it on the machine itself, with an undo the machine carries itself: **the old
system stays bootable until the owner commits.** It ports the harness and relocate; it doesn't invent a second
mechanism of either. Each step below names what it comes from, and the short list of what's new is at the end.

The first target is Enterprise Linux on BIOS: `centos5-to-alma10` and `centos7-to-alma10`, the routes the VM tier has
proved, and CentOS 6 machines joining the first one at its `centos6` rung. EL on UEFI, Debian and Ubuntu come after,
each once its own proof below has run. Nothing here is built. It moves into the sections it changes, piece by piece,
in the PRs that build it.

### Four verbs

1. **`wend share`** (built) on the machine: which route it's on and where it joins, the hops left, the reboots, the
   download size, where the new root would go (or what to add for one), and preflight's verdict.
2. **`wend rehearse`** on a scratch host: walks a copy of the machine's root, writes the checks the machine will be
   held to, fills a cache with every byte the walk needs, and writes a **plan**. A red rehearsal costs nothing and
   gives a gap report.
3. **`wend upgrade`** on the machine: relocates it onto a new root beside the old one, walks the new root from the
   plan and its cache, offline, rebooting where the VM tier reboots, and rolls back to the old root on the first red.
4. **`wend rollback`** and **`wend commit`** on the machine, once it has landed: boot the old system again, or make
   the new one stay.

An owner with several machines rehearses each one. A plan belongs to one machine (see "The plan").

### The undo: relocate first, then walk the copy

A live machine has no snapshot to restore a red hop from. So `upgrade` never walks the machine's own root: it runs
relocate ("Relocate") first, which copies the running root onto a new one beside it and boots the copy through a
stub, and walks the copy. Once the copy is made, nothing on the old root changes, and nothing in the old `/boot`
does but the stub, its one-shot variables and (GRUB 2 only) the GRUB modules beside them, so rolling back boots the
old kernel with the old GRUB config on the old root, exactly as it was. That holds on every hop, every reboot and
every failure, until `wend commit`.

Why relocate, and not an LVM snapshot of `/`: a snapshot has to hold every block a distro upgrade rewrites, so a safe
one is as big as a copy anyway; it doesn't cover `/boot` or the bootloader, which the route rewrites; undoing a walk on
the origin means merging the snapshot back (device mapper's snapshot merge, which CentOS 5's kernel doesn't have), and
walking on the snapshot instead means merging forward to commit. Relocate is built, proved on remi with a route
walked on the copy and a rollback after the last hop (wend#133), and keeps both roots whole. One mechanism, not two.

Where the new root goes, picked by preflight from the machine's facts (the order of `docs/rebuild-and-carry.md`):

1. **Free extents in the root's volume group**: a new LV `wend-root` there.
2. **A whole spare disk the owner names** (`--new-root-disk /dev/disk/by-id/...`, never picked on its own), held to
   relocate's rules for a new disk. With `/` on LVM it joins the root's volume group, as relocate does today; with `/`
   on a plain partition it gets a partition table and the new root goes on a partition.
3. **Nothing else.** No room for either refuses, touching nothing, and says exactly what to add: "free N GiB in volume
   group X, or attach a spare disk of N GiB and name it with --new-root-disk". N is relocate's sum (1.2 times what `/`
   and `/boot` use, plus the route's `disk_mib` and `boot_mib` over its hops and transits) plus the plan's cache. There
   is no way past this: a machine wend can't keep an undo for isn't upgraded live. The places under "Other places for
   the undo" join this list only once remi proves them.

The new root's filesystem is made by the machine's own `mkfs`, of the type its root has now (XFS made v5, as
relocate does; ext3 stays ext3, which every kernel on the EL routes mounts and the in-place routes already keep), so
the old kernel can still read it. Where the new `/boot` goes depends on what the machine's bootloader can read:

- **GRUB 2** (CentOS 7): a directory on the new root, and relocate's stub as built, with one change. Here the stub
  stays in charge for the whole route, not just until a commit made before the walk, so its home (its `grub.cfg`, its
  `grubenv`, and the modules under `core.img`'s prefix) has to be a plain partition that GRUB can save `grubenv` on
  and that every kernel on the route mounts, since `arm_new` writes it from the new root at every hop. The old `/boot`
  is that when it's ext4 or XFS v5 (CentOS 7.3 and later installs). An XFS v4 `/boot` (7.0 to 7.2) isn't, because Alma
  9's kernel and later can't mount it. Then a new step, `stub_home`, runs before `refresh`: it splits the spare disk
  into a first partition for the stub's home, sized like the old `/boot` plus the route's `boot_mib`, made XFS v5 by
  the running release's `mkfs.xfs` and mounted at `/run/wend/stub`, and a second partition that `place` makes the new
  root's PV instead of the whole disk; it copies the old `/boot/grub2` (config, `grubenv`) into the home and records
  its UUID. `refresh` then installs `core.img` onto the boot disk with the home as its prefix (`grub2-install
  --boot-directory=/run/wend/stub`); until the flip the home's copy of the old config boots the old system as before
  (EL7's entries find their kernel by `search --fs-uuid`), and `cleanup` after a failure before the flip runs the
  refresh again with the old `/boot` as the prefix, so the old system's own kernel updates keep reaching its menu.
  From the flip on, every relocate step that writes the stub (`flip`'s stub, its `grub.cfg.wend-old` and its first
  `grubenv` writes) writes the stub's home, `/run/wend/stub/grub2`, where `core.img` now reads, not the old
  `/boot/grub2`. Entry 1 finds the old `/boot` by UUID (GRUB 2 still reads v4). With free extents and no spare disk,
  an XFS v4 `/boot` is refused, naming a disk to add. `relocate.env` records the home's UUID as `STUB_UUID` (the old
  `/boot`'s own when that is the home), and every write to the stub's variables after the flip (`arm_new`,
  `commit_trial`, `rollback_next`) mounts that, never `BOOT_UUID`. The route's GRUB reinstall from EL8 on (the BIOS
  step `docs/rebuild-and-carry.md` describes) installs into the stub's home on a relocated machine, never the new
  root's `/boot`: that is on LVM, where GRUB can't save `grubenv`, so one-shot trials would stop. Each release's
  `core.img` and modules land beside the stub, the stub's entry 0 stops moving `prefix` (the new root's modules come
  from a newer `grub2` package than `core.img` from then on). Before it loads the new root's BLS config, entry 0 reads
  the new root's own `grubenv` (`load_env -f` on the new root's `/boot/grub2/grubenv`, read-only, which GRUB can do on
  LVM), so EL8's `kernelopts` (its root and `rd.lvm.lv=`) come from the root that the kernel and package tools keep
  them on, and the stub's own variables stay in the stub's home; the proof checks `/proc/cmdline`. Entry 1 loads the
  old EL7 config under the newer GRUB, as the doc describes. Dropping the old root, a step of its own after `wend
  commit` ("The upgrade"), moves the prefix to the new root's `/boot` with the landing release's own `grub2-install`;
  commit leaves the stub in place. This home and the reinstall into it have no proof yet either; step 3 proves them
  with the GRUB legacy stub, and refuses what fails.
- **GRUB legacy** (CentOS 5 and 6, and every release after them on those machines, since GRUB legacy stays: "Kernels
  and the bootloader") can't read LVM, so the new `/boot` is a plain ext3 filesystem made by the machine's own
  `mkfs.ext3`: the first partition of the spare disk, or, with free extents and no spare disk, a directory
  `/boot/wend/` on the old `/boot` partition when that partition has room for the route's kernels beside the old ones
  (CentOS 5's installer makes 100 MiB, which doesn't; CentOS 6's 500 MiB does), bind-mounted at the new root's
  `/boot`. The old `grub.conf` becomes a stub with the same two entries as relocate's: entry 0 loads the new `/boot`'s
  `grub.conf` (`configfile`), entry 1 the old one, saved as `grub.conf.wend-old`. Neither entry carries `savedefault`:
  an entry's own `savedefault` does nothing when the entry hands off with `configfile` (remi, CentOS 5). EL5's
  grub-0.97 has no `grub-set-default` and no `/boot/grub/default`; `default saved` reads the saved entry kept in
  `stage2`'s header, which the grub shell's `savedefault --default=N [--once]` writes: three bytes from offset 524
  (the saved entry, the one-shot entry, the one-shot flag; found by `cmp` on remi). The same bytes written in place
  with `dd`, on the same inode and blocks, drive GRUB exactly as the shell's do, sticky and one-shot, across reboots
  and a reset (remi, CentOS 5), so the writes need no grub shell, which the route loses at CentOS 7. That layout is Red
  Hat's patched GRUB, not upstream's, so before any write `refresh` has the release's own grub shell (still there at
  CentOS 5 and 6) write a sticky 1 and a one-shot 0, refuses unless they read 1 0 0 and 1 0 1 at 524, and puts the
  header back with `dd`. So the stub says `default saved`, the saved default stays 1 (the old system), and the trial is
  `savedefault --default=0 --once`, grub's own one-shot: a reset, a panic or a power cut during the trial brings back
  the old system. `arm` writes that from either root (so GRUB legacy needs no `arm_new`), `rollback_next`
  writes `--default=1 --once`, `commit_trial` `--default=0`, and the sticky `rollback` `--default=1`. From the new
  root (whose `/boot` is the new one) every write mounts the stub's home by `STUB_UUID` and writes its `stage2`. (The
  grub shell, by contrast, writes the `stage2` of whatever is mounted at `/boot`, and from the new root changed
  nothing unless the old `/boot` was bound over `/boot` first.) Every new-root kernel line gets `panic=10`,
  so a kernel or init that dies reboots by itself and the spent one-shot lands on the old system. The copy keeps its
  SELinux labels: CentOS 5's rsync (2.6.8) has no `-X`, so the copy is `tar --selinux` on both ends (or rsync where it
  has `-X`), and it's checked, not trusted: if the copy's `/bin/bash`, `/usr/sbin/sshd` or `/etc/shadow` is `file_t`
  or `unlabeled_t`, `setfiles -r` labels the copy from the release's own `file_contexts`, and if they still are,
  relocate stops before the flip (an unlabeled root boots, and on an enforcing machine nobody can log in). The checks
  after every trial boot include the same three labels. The copy's initramfs comes from the release's own tool (CentOS
  5's `mkinitrd`, CentOS 6's dracut), not host-only, so it finds the new root. All of this, on CentOS 5 with a spare
  disk (a plain ext3 `/boot` and root), passed on remi: trial, reset (old), arm from the old system and from the new
  one, commit, two boots on the new root, rollback (old), with the labels kept. Still to prove: the new kernels'
  entries landing in the new `/boot`'s config with the right paths (`hop.kernel_after` takes them from the default
  entry; EL6's grubby from `/boot`'s mount), CentOS 6, the `/boot/wend/` placement, and LVM roots; a layout that can't
  be made to work is refused, not patched over.

#### Other places for the undo

Weighed for owners with no free extents and no spare disk, none in the first build unless its proof passes:

- **The machine's own root filesystem.** Most old machines use a fraction of their disk. When `/` has room for a
  second copy of itself plus what the route needs, the old system is copied into `/.wend-old/` on the same filesystem
  (the old `/boot` beside it), and this time the hops walk the real root in place, as the VM tier walks it; the copy
  is the old system. No initramfs on these routes boots a root from a subdirectory (CentOS 5's `mkinitrd` can't, and
  GRUB legacy only loads the kernel), so rollback swaps instead: a GRUB entry boots the last kernel that booted green,
  with `init=` a static program that, as PID 1, renames the top-level directories of `/` and the old `/boot`'s files
  back into place, keeping a journal so a power cut mid-swap finishes on the next boot, then reboots into the old
  system. It's offered only when every disk-backed filesystem but `/boot` is `/` itself: a separate `/var`, `/usr`,
  `/home` or data mount is refused here as it is for relocate (the hops would change it in place and a swap couldn't
  take it back). The copy skips the virtual mountpoints (`/proc`, `/sys`, `/dev`, `/selinux`) and `/boot`, which it
  carries as files, and the swap skips the same list; any other top-level directory on a different device stops the
  copy, naming it; it keeps and checks SELinux labels as relocate's does. The swap entry's kernel line has `panic=10`
  and is only ever armed one-shot, so a swap that dies reboots by itself onto the system as it was instead of hanging.
  While an upgrade is under way that entry is armed for every boot of the new system, so a power cut rolls back, as
  with relocate. On remi, CentOS 5 with the swap as a busybox script (EL5's busybox 1.2.0 has no `sh` applet; the
  shell applet is picked from busybox's own list and every applet the script uses is checked before it's armed, and
  the script is parsed under that shell first): an init that fails on purpose panicked, rebooted by itself and came
  back on the current system; the real swap brought back the old system with its packages, files and labels. Still to
  prove: the swap done by the static wend binary instead of busybox, after a real hop, and the journal finishing a
  swap cut short; or it's refused. If it works, it covers more machines than any other place.
- **An external disk** (a USB SSD). Turned around: the old system is relocated out onto it and boots from it through
  the stub's entry 1, and the hops walk the internal disk in place, so production never runs from USB. Rollback boots
  the old system from the external disk, then copies it back. Open question 1.
- **An admin box on the same network.** The rehearsal's copy already lives there, pinned to the plan's fingerprint,
  so keeping it as the machine's restore image costs nothing; one admin host could hold one for each machine in a
  closet. It isn't a bootable undo: a rollback would be a boot entry whose initramfs brings up the network and pulls
  the image back (rsync or nbd), which CentOS 5's `mkinitrd` can't do and EL6's and EL7's dracut can only with work.
  Open question 2. Until then, the copy the rehearsal took is the backup the docs tell the owner to keep.
- **A cloud drive.** A backup, not an undo: getting a root back from it means a rescue boot and a long download, so
  it can't bring the old system back by itself after a panic. Not part of this design.

#### Limits

Relocate's own limits shrink to fit: the free-extents placement, a plain-partition root on a spare disk, and the GRUB
legacy stub are built in this work; LUKS, `/boot` on md or LVM, and UEFI stay refused. An XFS v4 CentOS 7 machine
needs a spare disk: the copy is XFS v5 either way, but its old `/boot` can't hold the stub past Alma 9.

### The rehearsal

It runs on a scratch host, never on the machine: the later releases' binaries can't run under the machine's own
kernel (EL6's apr already needs `SOCK_CLOEXEC`, which CentOS 5's kernel doesn't have), and that's every release the
route walks through. The scratch host is any machine the chroot harness runs on ("Sandbox requirements"), root
needed.

- **The copy.** `wend rehearse ROUTE --from root@machine` pulls the machine's root over ssh with rsync, with the
  snapshot's flags (`-aHx --numeric-ids`, and `-AX` where both ends' rsync have them). `-x` is
  `--one-file-system`: each pass copies one filesystem and crosses no mount point, so `/proc`, `/sys`, `/dev` and
  `/run` come over as the empty directories they are underneath, and `/` is copied first, then `/boot` and each other
  mounted disk filesystem into its place in the copy, once each. `--root DIR` takes a copy made another
  way (a mounted image or snapshot). The machine keeps running and nothing on it is written.
- **The walk** is `walk --run` on the copy, unchanged: the pinned run the route was proved by, the verifying proxy,
  the generated checks, rollback per hop. A rehearsal never takes `--live`: `upgrade` installs only bytes a passing run
  pinned. One addition: the kernel steps act only on a machine booted from the root (`[ / -ef /proc/1/root ]`), so in
  the copy they do nothing and fetch nothing. After each hop the rehearsal downloads, without installing, what
  `hop.kernel_after` would install (the release's `kernel` and what it pulls in, through the same proxy), so the
  machine's offline walk finds it in the cache. `net.ifcfg-to-keyfile` reads a profile's missing `HWADDR` from the
  running machine's `/sys/class/net`, which on the copy is the scratch host's NIC (a static profile refuses there, a
  DHCP one pins that NIC). That is harmless only because nothing the copy wrote reaches the machine: `upgrade` runs
  the op again on the machine itself. An engine that ever deploys a copy's output must supply the machine's own NIC
  identity instead.
- **The checks** it holds the copy to are the machine's ("Checks the machine is held to"), run first on the machine
  itself (their live forms, read-only) and then on the copy before the first hop. A check that fails on the machine
  is dropped and listed with its output: it guards nothing that works today. A check that passes on the machine but
  whose offline form can't pass on the copy (it needs another host, or hardware the copy hasn't got) fails the
  rehearsal, naming it, unless the owner has marked it `live_only` in `checks.toml`. A `live_only` check isn't run on
  the copy, but it still judges every boot of the upgrade, so nothing that worked at the start leaves the verdict
  without the owner saying so.
- **The result.** A pass writes the plan directory. A fail writes the run and the scrubbed gap report (`wend report`'s,
  with its issue link) and no plan.

### The plan

A directory, `wend-plan-<machine id>/`, carried to the machine (scp, or a USB stick for a closet with no network):

- `plan.json` (schema `wend.plan/0`): the route's content id, the run it was pinned by, the wend version, and per hop
  the fix ids that fired, the commands' seconds, and whether its kernel steps found the kernel in the cache;
- the machine's **fingerprint** at the moment of the copy: the adapter's `installed_list` (names, versions and
  architectures, and their sha256), the running kernel, the release file, and which pinned fixes' preconditions hold.
  It holds nothing particular to one machine (addresses, disks, hostnames), so another machine can match it ("Fleets");
- the facts preflight read on the rehearsed machine, kept for the report only: every machine is judged on its own
  facts, read when `upgrade` starts, never on these;
- `checks.toml`, the checks, in the format a box's `[roles]` and `[vm].live` use, so `walk` and the VM tier read it
  as they read a box;
- `cache/`, files named by their sha256, with a list of every file the walk and the kernel downloads fetched.

The fingerprint is checked once, when `upgrade` starts with no state file: a plan whose fingerprint isn't the
machine's as it is then (a package installed since, a kernel update, a reboot onto another kernel) is refused, since
the machine isn't the one rehearsed, and the answer is a fresh rehearsal. Every later `upgrade` goes by the state file
instead ("State across reboots"), since the hops change it on purpose.

### Checks the machine is held to

This answers open question 8 for a client. A boot is green on a running machine when all of these hold:

- **What the VM tier checks with no box at all:** the machine is booted from a new `boot_id`, on the root it should
  be on (relocate's `booted`); the release's newest kernel is the one running (`hop.kernel_newest`); it runs on the
  firmware it started on; its addresses, default route and resolvers are the ones it had before the first hop (the VM
  tier's `NET` read, systemd-resolved's stub standing for its upstreams); and sshd answers a new connection on its port.
- **The adapter's checks:** `pkgdb`, `current`, and `services` once the family map is in the adapter (open question 11).
- **Generated checks**, written by the rehearsal from what the machine runs at the moment of the copy, read from
  the machine over the same ssh:
  - every TCP port a packaged service listens on, by program family and port, listens again;
  - every service that was enabled and running is enabled and running, by family (`mysqld` is `mariadb` later on);
  - a web server's config test passes, and each local vhost's `/` answers on localhost with the status it gave before;
  - a MySQL or MariaDB server answers, and every database lists the tables it listed before;
  - `/etc/crontab`, `/etc/cron.d` and the cron spool hold the entries they held.

  Each has a live form (asks the running service, as `[vm].live` does) and an offline form for the copy (starts the
  daemon inside the root and asks it, as a CI box's roles do).
- **The owner's own**, added to `checks.toml` as roles before the rehearsal. `upgrade` refuses a `checks.toml` whose
  sha256 isn't the plan's: a check the rehearsal never ran proves nothing.

The same checks judge the old system after a rollback: they passed on it before the first hop, so they must pass on
it again.

### The upgrade

`sudo wend upgrade PLAN` refuses, touching nothing, unless all of this holds:

- it runs as root on a machine booted from its own root (not a container, not a chroot);
- the plan passed, is pinned, is for a route this atlas has, and its fingerprint is this machine's now (on a first
  start; "The plan");
- every file the plan lists is in its cache and hashes right (the walk's check of every pinned top file, extended
  to every file, since nothing is fetched from a network);
- relocate's preflight passes, with a place for the new root and the new `/boot` ("The undo");
- hardware preflight passes for the whole route, judged with the relocation assumed (as `docs/rebuild-and-carry.md`
  has it): the filesystems relocate carries count as the new ones it makes (XFS v5, or the same type for ext3), and
  `/boot`'s room is the new `/boot`'s, not the old one's. So an XFS v4 CentOS 7 machine, which preflight stops at
  Alma 9 today, is judged on the root it will walk;
- the plan and the cache aren't under `/tmp` or `/run` (they must survive reboots).

It asks for no backup. It says once, before it starts, that an undo on the same disk is no help against that disk
dying, and that a backup is still the owner's to take.

Then:

1. **Detach.** It copies itself, the atlas and the plan's `plan.json` and `checks.toml` to `/var/lib/wend/upgrade/`,
   starts the work as a detached process (`setsid`, the VM tier's `RUNNER`), and follows its log in the foreground.
   The transaction replaces sshd and glibc under the owner's session; when ssh drops, the work goes on, and running
   `wend upgrade` again follows it. The binary is static, so the glibc it was started beside going away changes
   nothing for it. It prints the downtime it expects (relocate's second copy pass, the rehearsal's seconds per hop,
   and a reboot each) before it starts.
2. **Relocate** runs as built (preflight, refresh, place, copy, fixup, flip, release), the cache copied into the new
   root with the rest, and the machine boots the copy on trial. Its first judgment is relocate's own and the checks
   above, on the copy still at the start release. From here the old root's default stays the old system.
3. **Each hop** runs on the new root, booted: exactly the commands the VM tier runs (`hop_parts`, with `hop.after`
   whatever happened), from the cache through the verifying proxy, `--offline`. Before a hop touches anything, its
   fixes' preconditions are evaluated, and the fixes that hold must be exactly the ones that fired in the rehearsal.
   Any difference refuses the hop there, before its first command, and the old system is the next boot.
4. **Every reboot is a trial.** Before each reboot (after a hop, and between the transaction and the post steps on a
   hop whose release says `reboot = "before-post"`, centos6) it arms the stub's one-shot boot of the new root. It
   runs on the new root, whose fstab no longer has the stub's `/boot`, so this is `arm_new`: `arm`'s write, made
   the way `commit_trial` and `rollback_next` make theirs (mount the stub's home by `STUB_UUID`, write the flag
   there, sync, freeze and thaw, unmount); relocate's own `arm` writes the running root's `/boot` and only arms
   from the old system. The stub's default stays the old system until `wend commit`, so a new kernel that hangs or
   panics, a power cut, or anyone's reboot at a bad moment brings back the old system, whole. When `reboot` returns
   and the old boot is still running two minutes later (the first boot onto systemd, and EL8 to EL9), it does what the
   VM tier does from outside, from inside: stops every process but itself (`kill -TERM -1`, then `-KILL`), syncs,
   remounts read-only and resets with sysrq ("VM tier").
5. **After each boot**, the owner runs `wend upgrade` again (decided for the first build: a boot unit that did it
   would have to start under SysV init, upstart and systemd on one route, three places for it to break). It reads the
   state, judges the boot, and goes on with the next hop. Until the end, the new root's `/etc/motd` carries one line
   saying an upgrade is under way and how to go on. If the old system came up instead, `upgrade` says so, and arms
   the trial again only once (a second failure is a red).
6. **At the end**, the prune removes exactly the packages the rehearsal's prune removed (no bisecting on the machine),
   then the checks run once more. The package manager is pointed at the landing release's own repositories (the
   release package's repo files, as a fresh install has them), never left on the proxy. The run (`wend.vmrun/0`'s
   shape, `runner.kind = "live"`, the real downtime) goes to `/var/lib/wend/runs/` on the new root, where `wend share`
   can attach it. The machine now runs the landed system, booted on trial each time until the owner commits.

**The first red rolls back.** A hop's commands failing, a check lost, the network changed, the wrong kernel running,
or a trial boot that comes back on the old system twice: wend stops, makes the old system the stub's default and its
next boot (relocate's `rollback_next`, made sticky as in #133), reboots, and judges the old system against the checks
it passed at the start. It writes the run and the gap report on the old root and leaves the new root as it was for a
look (relocate never deletes it). The machine is back where it started, serving. The next try is a fresh rehearsal
once the gap is closed. A rollback whose old system fails its checks is the one case wend can't fix, and it says so
loudly, with the run.

**`wend rollback`** does the same by hand, any time before commit: the old system, its kernel and its GRUB config
come back, and the new root stays for a look or a later `wend upgrade --resume`. **`wend commit`** makes the new root
the stub's default (relocate's `commit_trial`) and ends the trials, and that is all it does: it deletes nothing, and
the stub, its old entry and the old root stay. After commit `wend rollback` still works, until the old root is
dropped; dropping it (to get its disk back, moving the bootloader's prefix onto the new root, and removing the stub
and the old LVs) is a later step of its own, never part of commit.

### Fleets: one admin box, many identical machines

An owner with a closet of machines built the same way wants to take all of them up from one laptop. wend's part of
that is small on purpose: **a plan one machine's rehearsal made can be reused on another machine that matches it**, and
`upgrade` has exit codes a plain loop can drive. Everything else is the owner's loop. No agent, no daemon on the
machines, no inventory format, no scheduler: fleet orchestration is where a tool like this turns into a platform, and
wend stays out of it.

- **Reuse.** A plan made on machine A may be used on machine B when B matches its fingerprint ("The plan"), which
  is the same check `upgrade` makes on A itself: the same `installed_list`, release and running kernel, every pinned
  fix's precondition holding on B as it did on A, and the same `checks.toml` when B's checks are generated (so B runs
  the same services, ports, vhosts, databases and cron entries). B's own facts must
  pass hardware preflight on their own; they don't have to equal A's (addresses, disks and hostnames differ). On a
  mismatch `upgrade` refuses before it touches anything, naming what differs, and B gets its own rehearsal. What a
  match can't see (a hand-edited config file the rehearsal walked on A but not on B) is what each hop's fix match, its
  checks and B's own undo are for.
- **The cache** goes to each machine with the plan, copied (scp, rsync): nothing on the machines listens, and each
  machine's offline walk is the same as a single machine's. Each machine needs room for its own undo, as always.
- **Exit codes**, so the loop needs no parsing: `upgrade` exits 0 when the machine has landed (or is committed), 10
  when it is about to reboot or has just rebooted and needs running again, 20 when it refused before touching
  anything (with why on stderr, and 21 for a plan that doesn't match: rehearse this one), and 30 when it rolled back.
  `wend upgrade --status` prints the state file's phase and exits the same way without doing anything.
- **The loop** is a shell script in `docs/` (not a wend verb): push the binary and the plan to each machine over ssh,
  run `wend upgrade` until it exits 0, 20 or 30, waiting out each reboot, and collect each machine's run and report.
  One canary first, then batches of a size the owner picks, and the first 20 or 30 anywhere stops the loop before the
  next batch starts. Ansible or anything else that runs a command over ssh and reads its exit code drives the same
  commands.
- **Rollback stays per machine,** local and automatic, as above. The admin box drives the rollout; it is never the
  path back.

Built after a single machine's upgrade and rollback are proved (step 6 of the order of work). The proof: two or three
guests from one box on remi, driven from the host by the loop, with one guest changed on purpose before the start (a
package installed) so it's refused as not matching, and a hop failed on purpose on one guest, which rolls that guest
back and stops the loop before the next batch.

### State across reboots

`/var/lib/wend/upgrade/state.json`, on the new root: the plan's sha256, the hop, the phase (relocated, before the hop,
rebooted mid-hop, after the transaction, booting on trial, landed, committed, rolled back), the `boot_id` before each
reboot, and the network and the checks that passed before the first hop. It's written to a new file, synced and
renamed over the old one before every reboot and every change of phase. The old system reads it by mounting the new
root read-only (relocate's `relocate.env` says where), which is how `upgrade` and `rollback` know where things stand
from either root. A second plan refuses while a state file is there.

Each phase records what the next `upgrade` must find, and that replaces the plan's fingerprint from the first hop on:
the root it should be booted from (`booted`), a `boot_id` other than the one recorded before the reboot, the release
file and newest kernel the hop just finished should have left, and the sha256 of `installed_list` written at the end
of the hop's transaction. A machine that matches none of what the state expects (someone ran the package manager
between boots) is a red.

### Refused

The VM tier's proven layouts, nothing else. Today that refuses:

- no room for the new root and its `/boot` ("The undo"), with what to add, and an XFS v4 `/boot` with no spare disk
  for the stub's home;
- UEFI firmware, until relocate's UEFI stub and EL on UEFI have VM tier passes;
- LUKS anywhere; `/boot` on md or LVM; `/usr` or `/var` on a filesystem of its own (relocate carries `/` and `/boot`
  only, and EL7's UsrMove and the boot need them on `/`);
- a box whose route needs a shelter or a lifeboat (the VM tier builds them in a chroot copy first; a live machine's
  path for that isn't designed);
- everything hardware preflight already refuses (32-bit, a CPU or driver the landing release drops, `/boot` too small).

- a database server's data directory (MySQL, MariaDB, PostgreSQL, from the running server's own settings), or the
  state directory of any other running service a hop migrates, on a filesystem other than `/`: the hops' data
  upgrades (`mysql.upgrade-data`) would rewrite it in place, and a rollback would hand the old server files a newer one
  had already migrated.

Other mounted filesystems (`/home`, `/srv`, a data disk) aren't copied: both roots mount them, and since only one
root runs at a time, the new system's writes to them stay after a rollback. That's fine for files and fatal for a
database, hence the refusal above. `share` and `upgrade` say so, and name them. The proof includes one.

### Proof

A VM tier run of the client, not of the harness: the release workflow's static binary, its atlas, and a plan,
inside a remi guest, upgrading it. `tools/vm.py upgrade ROUTE BOX --client TARBALL` (new) builds the box as `replay`
does, rehearses it from the host with `wend rehearse --from` the running guest, pushes the plan in, and then plays
the owner: runs `wend upgrade` over ssh, waits out each reboot, and runs it again after each boot until it ends. The
harness judges every boot on its own as `replay` does (which root, network, checks, newest kernel, firmware), and the
run passes only if every hop is green by both the client's verdict and the harness's. Its run goes to `.wend/vm/`,
schema `wend.vmrun/0`, `runner.kind = "vm-client"`.

What it has to show, BIOS first, for each layout it allows (free extents, and a spare disk with `/` on LVM and on a
plain partition; GRUB 2 on CentOS 7, GRUB legacy on CentOS 5):

- `el/centos7-lamp` and `el/centos5-web` land on Alma 10, with an extra data disk mounted throughout, then commit;
- **a real rollback:** a hop made to fail on purpose (a harness flag that breaks a check in the guest after the hop)
  rolls back by itself, the old system boots its old kernel with its old GRUB config, and every check it passed at the
  start passes again; and `wend rollback` by hand after landing does the same;
- a reset from outside (QEMU `system_reset`) during a trial boot comes back on the old system, and `wend upgrade` says
  so and goes on;
- the owner's ssh session killed mid-transaction changes nothing;
- on `el/centos7-lamp`, the hop made to fail is the last one, on Alma 10's kernel after the route's GRUB reinstall,
  so the rollback goes through the stub after the reinstall; and one layout has an XFS v4 `/boot` (`--root-fs
  xfs-v4`), its stub's home on the spare disk, with a trial armed from Alma 10;
- the binary runs at all on CentOS 5's 2.6.18 and CentOS 6's 2.6.32 kernels (the cheap remi job already queued).

A native CentOS 6 box (`el/centos6-lamp`, from the 6.10 installer) and its proof come before anyone is told CentOS 6
is a proven start: the only C6 rung proved so far is a C5 box upgraded to C6.

### What's ported and what's new

| piece | from |
|---|---|
| hop commands, fixes, kernel steps, sshd watcher, network carry | the adapters, unchanged; the client already compiles them (`client/parity.py`) |
| the undo: copy, stub, trial boot, commit, rollback | relocate (`[relocate]`, wend#72, wend#133) |
| verified offline fetch | the client's `walk --run --cache --offline` |
| the rehearsal's walk | the client's `walk --root` |
| the detached hop, reboot points, forced reset | `tools/vm.py` (`RUNNER`, `Guest.reboot`, the forced reset) |
| judging a boot | `tools/vm.py` `walk_vm` (`NET`, `kernel_newest`, `FW_NOW`, the checks) |
| gap report | `wend report`, `wend share` |

New: relocate's free-extents placement, a plain-partition root on a spare disk, its GRUB legacy stub and `/boot`, the
new root's `arm_new`, and `stub_home` for an XFS v4 `/boot` (the adapter's `[relocate]` table, reviewed as a spec
change); relocate run by the client; the plan and the state file; the generated checks; the rehearsal's kernel
downloads. Nothing else.

### Order of work

1. This section, reviewed.
2. The mid-route join in `share`, preflight and `walk` (its own PR, in flight), so a CentOS 6 machine hears where it
   joins.
3. Relocate widened, in the VM tier first (`vm.py relocate`, then `vm.py --relocate replay`): free extents, a plain
   root on a spare disk, the GRUB 2 stub's home on a plain partition kept through the route's GRUB reinstall, and
   GRUB legacy's stub and `/boot` on `el/centos5-web` (with `--lvm` made to work on the CentOS 5 kickstart), each with
   its trial, reset, commit and rollback, and a route walked on the copy and rolled back after a red made on purpose.
   Alongside it, the same-filesystem copy and its swap at boot ("Other places for the undo"), proved or refused.
4. `wend rehearse`, the plan and the generated checks, proved by rehearsing VM guests from remi's host and comparing
   the walk with CI's run of the same route.
5. `wend upgrade`, `wend rollback`, `wend commit` and `vm.py upgrade`, on `el/centos7-lamp`, then `el/centos5-web`,
   with the proof above.
6. Fleets: plan reuse, the exit codes and the loop in `docs/`, with the proof in "Fleets".
7. `el/centos6-lamp` and its proof.
8. README and docs/try-it.md say what's real.

Later, each as its own design: EL on UEFI (relocate's stub on the ESP); Debian and Ubuntu (the VM tier has proved
their routes; relocate needs their bootloaders' stubs); dropping the old root after commit; a boot unit that goes on
by itself after each boot.

### Open questions

1. **An external disk as the undo.** BIOS has to boot it through GRUB on the internal disk, under a device order that
   changes when it's unplugged, and a cheap thumb drive is too slow to run a server from even for a day. Refuse below
   a write rate measured during the copy? How long may the old system run from it after a rollback before it's copied
   back?
2. **The admin box's image as the undo.** Worth building as the fallback for machines with no room, instead of
   refusing them, only if remi proves a restore entry that brings back the old system after a hop broken on purpose,
   with no one at the console. Which transport (rsync over ssh, nbd), and what does CentOS 5 do, whose initramfs has
   no network?
3. **How close is a match.** A fleet plan matches on packages, checks and preconditions, not on config files.
   Should it also compare the files the rpm database says differ from their packages (`rpm -Va`'s config changes),
   less the ones every machine has its own of (network, hostname, host keys)? Stricter means more rehearsals; looser
   leans on the canary and the undo.

## Open questions

Questions 1 to 6 were the ones RPM could answer. The CentOS round answered them as follows; 7 to 12 are still open.

1. **Op vocabulary.** Yes, RPM needed ops Debian never did, and none of them is a variant of an apt op: bootstrapping
   the next package manager (twice), the UsrMove and /run layout changes, swapping release packages, adopting
   `.rpmnew` files, and running a database once to migrate its data. Database rebuilds and module-stream resets
   turned out to be harness work on every hop, not fix steps. `force-remove` maps to `rpm -e --nodeps`, with
   `--noscripts` as EL's own force flag (`--justdb` stays harness-only: it would let a fix fake `pkgdb`). So: typed ops with a backend per package manager.
2. **pm-option.** Not needed by any EL fix yet. EL has none until one is.
3. **Config files.** An op (`rpm.adopt-rpmnew`), not `write-file`: the packaged file is the right content, and a
   model rewriting httpd.conf by hand is exactly the drift reviewers have to hunt. Plus `rpm.set-aside-unowned` for
   files no package owns any more.
4. **The `installed` predicate.** Plain `rpm -q` was enough on both boxes. No qualifier until a box needs one.
5. **Release ids across vendors.** One ladder, vendor in the release id. A route may skip a release the adapter lists
   in `may_skip` (EL8, crossed inside one hop by `rpm.transit`; CentOS 8, where boxes start but no route lands).
   Two vendors at one major version are two ladder entries, in the order a route could pass them, when a route
   can land on both (CentOS 8 and AlmaLinux 8). A vendor that's only ever a starting point (Scientific Linux 7) stays
   off the ladder and `joins` the rung its packages match.
6. **Ordering and interaction.** Yes, once: on the hop to EL9, the database password rehash and the slow shutdown
   have to run on EL7, before `rpm.transit` replaces the server. Route order handles it (pre steps run in the order
   the route pins fixes), and the route says so. No fix needs to see another fix's effect in its `when`.
7. **Parameterized fixes.** `apache24-site-conf` is gated on one site's file (`sites-available/mysite`). It's a
   local fix, not a universal one. A useful version would be "for every 2.2 vhost with no `.conf`, write the 2.4
   form". Do we add a `for-each` over files, or keep local fixes out of the shared library?
8. **Client-side checks.** CI boxes have rich roles. A real machine has only the adapter checks plus whatever the
   user supplies. What is the minimum check set a client runs before it calls a hop healthy?
   Proposed for a running machine in "Upgrading a running machine": the VM tier's box-free checks, the adapter's,
   checks generated from what the machine runs, and the owner's own.
9. **Signing.** Sigstore attestation from GitHub Actions on each run file, or a project key held by the verifier
   box? The client trusts a run only if it is attested and refers to the route id it is about to apply.
10. **Booting.** A chroot can't test init. `drop-sysvinit-for-trixie` is fine in a chroot and dangerous on a real
    machine without a check that init is systemd. Do some fixes need a `requires_boot_check` flag that CI can't
    satisfy yet? Part answered for EL: what a chroot *can* see is which units are enabled. `service.record` and
    `service.enable` carry SysV runlevels onto systemd, and boxes check enablement offline (`systemctl is-enabled`,
    `chkconfig --list` before EL7). Kernels, bootloaders and the first real boot are tested by the VM tier (see
    "VM tier"), which the maintainer runs. What it found on the EL route is in wend#13.
11. **The family map.** Move it from box checks into the adapter (a `[families]` table of renames), so a client can
    compute name families for its fingerprint hint and services check without a box? Probably yes; nothing reads it
    outside boxes yet.
12. **Pinning what a route installs.** Answered (2026-10-11): pin repo metadata, not package lists. Each hop's run record
    carries `sources` (see Run), the sha256 of every repo's top metadata file. Packages are verified through the
    chain under it: `Release` (or `repomd.xml`) → index → the package's own checksum. The chain uses the strongest
    hash each level carries. Etch's indexes already carry SHA256. CentOS 5's `primary.xml` carries only SHA-1, which
    still resists a forged package (that takes a second preimage, not a collision), and the top of every chain is
    our own sha256.
    - **Archive.** CI keeps, keyed by sha256, every metadata file a passing run pinned and every package it
      downloaded. That's the pinned tier of the vision doc: never evicted while a proven route references it. The
      download list is a CI artifact, not part of the run file. Design in `docs/archive.md`.
    - **Fallback chain.** A client fetches each file by its expected hash from a list of sources in order: local
      cache, an org cache, the wend archive, the origin, then mirrors. The first bytes that hash right win. A source
      that serves wrong bytes is skipped, not trusted.
    - **Before the first hop.** The client walks a copy of the machine first (what `wend walk --root` does today).
      That walk fetches through the chain into a local cache, verifying everything. The walk on the machine itself
      then installs only from that cache, offline, so a mirror going down mid-upgrade can't strand it.
    - **Moved mirrors.** When the pinned metadata isn't available from any source (a live mirror moved on and no
      archive holds that snapshot), the client stops before touching anything. It walks against today's mirrors only
      when asked to (`--live`), and its run then records `"live": true` and the new `sources`, so the mismatch is
      visible. Decided: refuse by default. `wend walk` takes either `--run` or `--live`, never neither: before the
      first step it fetches and verifies every top file the run pins (indexes and packages are checked as they're
      fetched; walking a copy first is what proves they're all still there). The follow-up is making a vanished
      snapshot rare (the archive and the fallback chain), not making the client looser. Today that's not rare: a run
      pinning live AlmaLinux or EPEL repos goes stale as soon as they publish new metadata, often within a day.
    - **Order of work.** Done: the harness and the client record `sources`, the client walks through a verifying
      cache (`wend walk --run`, with `--source` and `--offline`), and refuses an unpinned walk unless `--live`, and
      the gate uploads an EL replay's download list with its metadata bytes (the mirror shim's log). Next: the same
      for apt, then the archive (wend#19). Until runs carry `sources`, integrity rests on TLS to the
      mirrors and the shim's cache: EL releases from AlmaLinux 8 on, and EPEL for them, check signatures ("Signed
      packages"); older EL releases run with `gpgcheck=0` (CentOS 5's keys can't check what comes later), and the shim
      keeps only complete downloads and checks hash-named metadata against its name.
13. **A machine the route can't carry in place.** Some machines can't take a route on their own disk: CentOS 7
    installed from 7.0 to 7.2 media has XFS v4 everywhere, which AlmaLinux 10's kernel can't mount and nothing
    converts. Proposed: a second root beside the old one (rebuild and carry), done first as **relocate** (copy the
    root onto a fresh filesystem, flip the boot with a one-shot trial, keep the old root as rollback, then walk the
    existing route), with state movers for a fresh target root second. Design in `docs/rebuild-and-carry.md`; each
    piece moves into this document in the PR that builds it.
14. **leapp-data as a gate.** `wend gaps` only reports ("Package evolution data"). Should a client's preflight warn, or
    refuse, when one of a machine's own packages goes at a hop and no fix names it? Route PRs show the report beside
    the replay, as a warning only. Not decided; the data is advisory until then. Its driver deprecation data and
    repo maps aren't used: wend's driver tables come from the kernels themselves ("Hardware preflight"), and the
    route's own repo lists say which repos it enables.
