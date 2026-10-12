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

- Debian: `apache.enable` and `apache.disable` (`kind` = site/mod/conf, `name`) compile to `a2en*`/`a2dis*`.
- EL: `rpm.bootstrap` (unpack EL6's rpm/yum stack over / with 7-Zip, because rpm 4.4 can't read xz payloads),
  `rpm.transit` (pass through EL8: EL8's rpm run from a staging dir, then a real distro-sync), `rpm.swap-release`
  (release packages by hand: epochs and vendor switches), `rpm.drop-arch` (multilib leftovers), `rpm.adopt-rpmnew`,
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
lists in `may_skip`: a route may cross it inside one hop. EL lists `alma8`, because the box only ever passes through
EL8 (`rpm.transit`) on the hop to EL9. No health check runs on a release the route skips.

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
yum or rpm directly. If it needs to, that's a new op in an adapter. The keys are defined in
[Adapter format](#adapter-format).

### Debian (done)

See `adapters/debian.toml`. Notes that matter:

- `installed` means state `ii`. Removed-but-configured packages keep their config, so `dpkg -s` alone was a bug.
- The `services` check needs "explicitly installed". apt has it in `/var/lib/apt/extended_states`.
- `current`: every binary a service runs comes from a package that the release in sources.list still ships
  (`apt-cache madison`). This closed the "PHP 5 still serving on trixie" hole, without per-release version tables.

### Enterprise Linux (`el`, rpm backend)

See `adapters/el.toml`. Notes that matter:

- The ladder crosses vendors: `centos5 centos6 centos7 alma8 alma9 alma10`. A family isn't a distro; release ids
  carry the vendor.
- The package manager changes mid-ladder (yum through 7, dnf from 8), and so does the upgrade verb: `upgrade` on EL6,
  `distro-sync` from EL7 (EL6's grep is newer than EL7's). Each release can set its own `upgrade` in the adapter.
- The harness owns `/etc/yum.repos.d` and clears the vendor .repo files every release package drops (they point at
  dead mirrors). After each hop it also rebuilds the rpm database and, on EL10, moves it to `/usr/lib/sysimage/rpm`.
- CentOS 5's yum can't speak today's TLS, so repos go through a mirror shim the harness runs on the host
  (`[mirror]` in the adapter): plain http on 127.0.0.1, fetching `https://<host>/<path>` for
  `http://127.0.0.1:<port>/<host>/<path>`, for a fixed list of hosts, and caching what it fetched.
- No EL release has an in-place path through a chroot (Leapp and redhat-upgrade-tool reboot), so every hop is a plain
  transaction plus fixes.
- `installed` is `rpm -q`. "Explicitly installed" doesn't exist before yum 3 on EL6, so a box's services check uses
  "nothing requires it, by name or by any capability it provides" instead.
- `current`: the service binaries come from a package whose dist tag is the running major (`[._]el$MAJOR`).
- Boxes can name roles that start daemons in `isolate`; those checks run in a network namespace of their own.

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

### EL ops

**`rpm.swap-release`** `remove`, `install` (package names ending in `-release`; the EL adapter holds them to lowercase, as every EL release package is). Replaces the release package without the package
manager, because an epoch drop (CentOS 5's `centos-release` has epoch 10, EL6's has none) or a vendor switch isn't an
upgrade to yum. Effect: `remove` is gone from the database (its scriptlets don't run), and the first
`install-<digit>...` package for x86_64 or noarch listed in `BASE`'s metadata is installed (a release's base repo
carries exactly one; CentOS 5's `sort` has no `-V` to pick among several). Fails if there is no such package.
Reference: find it in `PKGLIST(BASE)`, download it, `rpm -e --nodeps --noscripts {remove}`,
`rpm -ivh --nodeps --noscripts` the file, `CLEAN_REPOS`.

**`rpm.drop-arch`** `arches` (list of `i[3-6]86`, at least one). Removes every installed package built for one of
these architectures, files and database entries, with no dependency checks and no scriptlets. EL5 x86_64 installs
carry i386 copies of libraries (curl, glibc, ...) that conflict with EL6's x86_64 packages. Refuses, before
removing anything, if a package of these arches has no installed twin of another arch (a 32-bit-only app or compat
library is not a multilib copy). 32-bit software not installed by rpm (a tarball in /opt) still loses its libraries.
Fails if any such package is still installed afterwards. Reference: `rpm -e --nodeps --noscripts` every
`NAME-VERSION-RELEASE.ARCH` that `rpm -qa` lists with one of the arches, then check `rpm -qa --qf '%{ARCH}\n'` has
none of them.

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
touching anything, if any installed package (gpg-pubkey aside) has a vendor other than `remove`'s, or is one of that
vendor's add-on repos (EL: names starting `rh-`, `devtoolset-` or `centos-release-`, except the base
`centos-release-notes`; that is, Software Collections and SIGs). EPEL, vendor, SIG and locally built RPMs would
otherwise lose their package records. Fails if the box doesn't say it is `release` afterwards.
A fix with a non-empty `reinstall` must pass the install rule. Reference, with work dir `/var/lib/wend/boot`, and
repos kept in private `reposdir`s so `/etc/yum.repos.d` stays the harness's:
1. Write a repo file for `transit.from`'s repos plus `transit.extra_repos` (`old`), and one for `release`'s repos
   (`new`).
2. `yum install dnf` from `old` (CentOS 7 extras ships an EL7 dnf).
3. With that dnf and `new`: `--downloadonly` distro-sync into the work dir, `--releasever` = the release's major,
   `--allowerasing --setopt=best=False` (EL7's dnf 4.0 has no `--nobest`), no weak deps, no protected packages. Its exit code is ignored; the step fails if
   `rpm`, `rpm-libs`, `glibc`, `dnf` or `python3-dnf` wasn't downloaded. Also fetch `transit.release_package` from
   `PKGLIST(BASE)` if the sync didn't.
4. `rpm -e dnf` (the EL7 one), rebuild the database, and unpack every downloaded package into a staging dir.
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
    (and recorded in the run's step output): a client must show it to the owner.

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

**`mysql.upgrade-data`** (no arguments). Start the installed server on the existing data, run `mariadb-upgrade` (or
`mysql_upgrade`) `-uroot --force` if there is one, stop. Fails if the start or the stop fails. The tool's own exit
code is not: the server starting and stopping cleanly on the data is the point, and the tool complains about
things it then fixes.

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
Only the first record is kept: if the file exists, it is left as it is, so it always holds the box's own state. It
is only allowed in `pre` (after the upgrade the links are gone), and it stays on the box afterwards: a later route
that uses `service.enable` reads the same record, which is what the box ran under SysV, not what it runs today. That
is why `service.enable` also accepts a `like` service that is enabled now. Fails if `/etc/rc.d/rc<L>.d` doesn't exist. Reference: `ls /etc/rc.d/rc$L.d | sed -n 's/^S[0-9][0-9]//p' | sort -u`.

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
  MAC. (Renaming several NICs to kernel names like `eth1` can race the kernel; only single-NIC boxes are proven.)
- the ifcfg file moves to `SAVED`.

The files are read as `KEY=VALUE` lines and never executed; a value that isn't one word, or holds `$`, a backquote or
`#`, fails, and an empty value is an unset key. Each key has the meaning the ifcfg-rh plugin gives it:
- `DEVICE` and `HWADDR` (both required), `TYPE` (`Ethernet` only), `NAME` (the profile's name, else `System <name>`),
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
- `DHCPCLASS` and `USERCTL` are inert (the plugin doesn't read them); wend still requires `USERCTL` to be a boolean.

In `/etc/sysconfig/network`, `GATEWAY`, `GATEWAYDEV` and `IPV6INIT` are read as above, and `NETWORKING`,
`NETWORKING_IPV6`, `HOSTNAME`, `NOZEROCONF` and `NISDOMAIN` are left as they are: none of them is part of a profile.

Any other setting in either file (bridges, VLANs, bonds, aliases, IPv6 addresses, `RES_OPTIONS`), a keyfile of the
same name or a `10-wend-<DEVICE>.link` already there, any `.link` in `/etc/systemd/network`, `/run/systemd/network`,
`/usr/local/lib/systemd/network`, `/usr/lib/systemd/network` or `/lib/systemd/network` whose name sorts before the
pin's (systemd applies only the first `.link` that matches, so it could name the NIC something else), two profiles
that pin one name to different MACs or one MAC to different names, a `route-`, `route6-`, `rule-`, `rule6-` or `keys-`
file beside a profile, or a value that isn't an address, MAC, boolean or plain name fails the step. Every profile is
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
| `may_skip` | releases a route may cross inside one hop (see Route) |
| `harness_owned` | path prefixes no fix may write |
| `writable`, `managed` | the allowlists (globs) named by `write-file` (`writable`) and the `rpm.*` file ops (`managed`). Nothing under `/etc/wend/` is ever on either |
| `[kv."<path>"]` | the files `kv.*` may touch: `keys` (the only keys it may set or unset; never one that grants access) and `set` (false for files with sections, which only take `kv.unset`) |
| `force` | required: the `force-remove` flags this family allows, a subset of its backend's (apt: `depends`, `remove-essential`; rpm: `depends`, `scripts`) |
| `[releases]` | where packages come from. apt: `archive`, `current`, `current_from` (releases from `current_from` on use `current`). rpm: `releases.<id>` with `major`, `pm` (`yum`/`dnf`), `repos` (name = host/path, through the shim), optional `upgrade`, `bootstrap`, `transit`, `install` (the VM tier's installer), `reboot` (`before-post`, see "Kernels and the bootloader"). apt: a start release may have `releases.<id>.install` too |
| `[mirror]` | rpm: `shim` (the mirror shim's base URL) and `probe` (a path that must be fetchable before a run starts) |
| `[hop]` | `set_release`, `refresh`, `upgrade` (list), optional `verify`, `after` (list), `kernel_before` and `kernel_after` (lists, see "Kernels and the bootloader"), `autoremove`, `obsolete`, `installed_list`, `kernel_newest` (the VM tier's newest-kernel query); apt also `apt_conf`, the harness's apt config |
| `[predicates]` | `installed`, `file`, `file-contains`, `mysql-option` command templates |
| `[pm_options.<name>]` | `path`, `content`. apt: only `overwrite-conflicting-files`; rpm: none |
| `[ops."<name>"]` | `summary`, `args` (see Argument specs). Only ops the Op reference defines for this backend may appear. Whatever a `pattern`, `list` or `table` says, every value a fix passes through one must also match `[A-Za-z0-9_.:/+=@][A-Za-z0-9_.:/+=@-]*`: no quote, whitespace, backslash, glob character, `%`, any of `` $`;&|<>(){}~ ``, or leading `-` |
| `[checks]` | universal health checks: `pkgdb` is a command; `current` and `services` are described in words and built by the harness from the box |
| `[box]` | how to build a fresh box of this family (rpm: `seeds`, the packages to bootstrap a root from) |

These checks narrow what an adapter lets fixes do (ops, force flags, argument values). They don't judge an adapter's
allowlists, hop commands or package lists: those are reviewed as spec changes, and CI never takes them from a PR.

Templates in `[hop]`, `releases.*.upgrade` and `[predicates]` use `{name}` placeholders, replaced literally (no
other brace syntax is touched, so `${...}` and `%{...}` pass through). Values for a hop:

- apt: `{mirror}` (archive or current, see `[releases]`), `{release}`.
- rpm: `{release}`, `{major}`, `{shim}`, `{repofile}` (a yum repo file with one section `[<release>-<name>]` per repo,
  `baseurl=SHIM/<repo>/`, `gpgcheck=0`, newlines as `\n` for printf), `{pm}` (`dnf` if installed, else `yum`),
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
- The harness sets umask 022 for itself before it touches a root (see "How a hop runs"), never taking the caller's.
- A rollback restores the whole root from the pre-hop snapshot. Nothing from a failed hop survives.

## Kernels and the bootloader

A chroot box has no kernel, and these steps do nothing there. A real machine's kernel and boot entries belong to the
harness, not to fixes, because the release's own tools can't carry them up the ladder:

- **The old release's kernel can't stay in the rpm database.** EL5's kernels require `mkinitrd`, which is gone in EL6
  (`pkgdb` fails). EL6's kernel requires `/sbin/new-kernel-pkg`, which nothing provides on EL7 after UsrMove, so the
  transaction fails. And dnf won't remove the running kernel, so `rpm.transit` fails. So `hop.kernel_before` drops
  the installed kernel packages' records with `rpm -e --justdb --nodeps --noscripts` and keeps their files: the
  running kernel and its boot entry stay as the fallback. This is the one place, besides `rpm.transit`'s last step,
  where wend drops records and keeps files. Fixes can't, because a fix doing it could fake `pkgdb`. It then removes
  the files of kernels that no package owns and that aren't running or GRUB's default, so the release's kernel fits
  in `/boot` (CentOS 5's default is 200 MB). This is how the previous hop's kernel, once a reboot has moved off it,
  finally goes. None of the kernel steps acts unless the machine is booted from this root (`/` is PID 1's root): in
  a chroot of someone's disk the records, files and boot entries are left as they are, since `uname -r` there is the
  host's kernel, not the box's. The fallback the running kernel's entry gives is a manual one:
  GRUB legacy has no `fallback=` set, so a hop that fails between the two halves leaves the default on the running
  kernel's entry, which still boots.
- **The release's kernel is installed after the transaction** (`hop.kernel_after`), since the transaction itself only
  syncs what the database lists.
- **GRUB legacy stays.** From EL8 on, kernels only get BLS entries, which GRUB legacy can't read, and GRUB2 can't be
  installed on the sector-63 partition layout of CentOS 5's installer (a 31 KB gap; EL9's smallest `core.img` is
  33.9 KB). When `/boot/grub/grub.conf` exists, `hop.kernel_after` gives the newest installed kernel an entry there,
  using the root and kernel arguments of the current default entry, makes it the default, and drops the `vmlinuz`
  entries whose kernel file is gone (memtest, Xen and chainloader entries stay). It fails if the new entry isn't
  there afterwards. EL6 and EL7 grubby still write their own entries. After every reboot the VM tier checks that the
  release's newest kernel is the one running.
- **When to boot it.** EL6's binaries can't run on the EL5 kernel (EL6's apr asks for `SOCK_CLOEXEC`, so `httpd -t`
  fails), so on a real machine the `centos6` hop boots its new kernel between the transaction and the post steps
  (`releases.centos6.reboot = "before-post"`). Every other hop runs straight through, then reboots: on the hop to
  EL7, the post steps re-enable the boot services, which must happen before the first boot onto systemd.

The VM tier proves these steps (it reboots where `reboot` says, and after every hop). A client running a route on a
real machine must reboot at the same points; the client doesn't walk a live root yet, so it doesn't split a hop.
The checker holds `reboot` to `"before-post"` and the kernel steps to lists of commands.

## VM tier (maintainer-run)

A chroot can't test kernels, bootloaders, init or networking (open question 10). `tools/vm.py` replays a route on a
box installed in a KVM guest by its release's own installer: anaconda with a kickstart for EL, debian-installer with a
preseed for Debian (appended to the installer's initrd, so it needs no network to be read). Both are made from
`box.toml`: its packages, `customize` and `start_hook` (and Debian's `debconf_selections`), a static address and the
release's kernel and bootloader. The box is then brought to the release's last packages, as the chroot build does
(Debian: the harness's `hop.apt_conf` first, then `set_release`, `refresh` and `apt-get dist-upgrade`, which on a
frozen archive changes nothing the installer didn't already fetch). Each hop compiles exactly the commands the chroot harness
runs (the same `hop_cmds`), runs them inside the running guest, and reboots it for real (checked by `boot_id`).
After every hop:

- the guest has to come back reachable over ssh on the box's port (`[vm].ssh_port`);
- its addresses, default route and resolvers have to be the ones it booted with at the start;
- the box's checks have to keep passing, running on the live box. Roles that start their own daemons are listed in
  `[vm].skip`, and `[vm].live` gives their live forms, which ask the running services instead.

A hop ends the replay when its commands fail, when the guest doesn't come back, when its network differs from the
start, when the newest installed kernel isn't the one running (or the query names none), or when a check that passed
before it fails after the reboot. Its disk and serial console are kept.

The VM box differs from the chroot box in ways a replay can show: it has what the installer adds (`@core`, a kernel, a
bootloader, a 200 MB `/boot`), SELinux is permissive and the firewall off (kickstart `selinux --permissive`,
`firewall --disabled`), and `customize` runs before the release's last updates, not after. The kickstart is EL5's
format for now. A Debian VM box has what debian-installer's minimal install adds (a kernel metapackage, GRUB legacy on
etch, `iproute`, which the harness's own network check reads). The adapter gives the preseed lines that differ by
installer version (disk path, partman names); the rest of the preseed is etch's spelling, the only Debian start
release with an installer table so far. Its static address is not the one QEMU's DHCP hands out and the forwards point at it, so a box that
falls back to DHCP is unreachable and fails the hop.

The rules:

- Maintainer-run only. It runs the guest on the host that runs it, needs `/dev/kvm`, and is never wired to a CI
  runner that a pull request can trigger. Its runs go to `.wend/vm/` (schema `wend.vmrun/0`, `runner.kind = "vm"`),
  not to `runs/`.
- QEMU user-mode networking only: nothing on the host changes. The guest reaches the mirror shim at `10.0.2.2`, and its
  ssh and http are forwarded to fixed ports on `127.0.0.1`, so a host runs one VM replay at a time.
- Box: an optional `[vm]` table with `ssh_port` (default 22), `skip` (roles that start their own daemons, which a
  running box already has) and `live` (role name to shell: the live forms of those checks; every skipped role needs
  one, so skipping never drops a check). The checker holds it to that.
- A route that ends with `prune_obsolete` gets the chroot walk's prune after its last hop: the packages the target
  no longer ships are purged and autoremoved, the set split in halves whenever a step fails or a check is lost, and
  each attempt first drops what an earlier autoremove already took. Each attempt runs on its own overlay of the last
  good disk and reboots; a kept attempt is the next base. Like the chroot walk's, the prune never fails the run; it
  records what was obsolete before and after.
- Adapter: `releases.<id>.install` names a start release's installer. EL: its `tree` through the shim and the
  `pxeboot` dir in it (the shim answers single byte ranges, which anaconda uses to read package headers). Debian: the
  `netboot` dir under the archive, `preseed` (the version's own preseed lines) and `late` (shell the installer runs at
  the end, for the bootloader's serial console). `disk` (`virtio`, `lsi53c895a`) and `nic` (`virtio`, `e1000`) name
  the emulated hardware, default virtio: the machine keeps them through every hop, so the start release's kernel has
  to know them (etch's has no virtio). `hop.kernel_newest` prints the newest installed kernel as `uname -r` names it;
  after every reboot it has to be the running one.
- `replay ROUTE BOX --from RUN HOP` starts from an earlier run's disk after the hop before HOP, to iterate on a late
  hop. It only resumes from a hop that passed on the same box, logs in with the ssh key of the box build that disk
  came from, and is recorded as `partial-pass` at best, never `pass`.
- A reboot is proven by a new `boot_id`. When `reboot` doesn't change it within two minutes (the first boot onto
  systemd: PID 1 is still the old init, and the new release's `reboot` can't talk to it), the harness stops every
  process (`kill -TERM -1`, then `-KILL`), syncs, remounts read-only and resets with sysrq, and records
  `reboot_forced`. Stopping the processes first matters: a reset with the daemons up leaves every replaced library
  and socket as an orphan inode, and EL7's kernel, cleaning those up at its first mount of the ext3 root, leaves
  extended-attribute reference counts too high. A userspace fsck cleans the same orphans correctly, but EL7 mounts
  the root before any fsck, and EL9's forced fsck then stops the boot. Only PID 1's own files stay orphaned, and
  those come through clean. A client on a real machine must stop the processes the same way.

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
   in `may_skip` (EL8, crossed inside one hop by `rpm.transit`).
6. **Ordering and interaction.** Yes, once: on the hop to EL9, the database password rehash and the slow shutdown
   have to run on EL7, before `rpm.transit` replaces the server. Route order handles it (pre steps run in the order
   the route pins fixes), and the route says so. No fix needs to see another fix's effect in its `when`.
7. **Parameterized fixes.** `apache24-site-conf` is gated on one site's file (`sites-available/mysite`). It's a
   local fix, not a universal one. A useful version would be "for every 2.2 vhost with no `.conf`, write the 2.4
   form". Do we add a `for-each` over files, or keep local fixes out of the shared library?
8. **Client-side checks.** CI boxes have rich roles. A real machine has only the adapter checks plus whatever the
   user supplies. What is the minimum check set a client runs before it calls a hop healthy?
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
      download list is a CI artifact, not part of the run file.
    - **Fallback chain.** A client fetches each file by its expected hash from a list of sources in order: local
      cache, an org cache, the wend archive, the origin, then mirrors. The first bytes that hash right win. A source
      that serves wrong bytes is skipped, not trusted.
    - **Before the first hop.** The client walks a copy of the machine first (what `wend walk --root` does today).
      That walk fetches through the chain into a local cache, verifying everything. The walk on the machine itself
      then installs only from that cache, offline, so a mirror going down mid-upgrade can't strand it.
    - **Moved mirrors.** When the pinned metadata isn't available from any source (a live mirror moved on and no
      archive holds that snapshot), the client stops before touching anything. It walks against today's mirrors only
      when asked to (`--live`), and its run then records the new `sources`, so the mismatch is visible. Decided:
      refuse by default. The follow-up is making a vanished snapshot rare (the archive and the fallback chain), not
      making the client looser.
    - **Order of work.** Done: the harness and the client record `sources`, and the client walks through a verifying
      cache (`wend walk --run`, with `--source` and `--offline`). Next: CI uploads the download list, then the archive
      (wend#19). Until runs carry `sources`, integrity rests on TLS to the
      mirrors and the shim's cache: EL repos run with `gpgcheck=0` (CentOS 5's keys can't check what comes later), and the shim
      keeps only complete downloads and checks hash-named metadata against its name.
