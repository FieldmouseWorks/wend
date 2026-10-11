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
2. refresh the indexes (`hop.refresh`),
3. the `pre` steps of every fix whose `when` held, in route order,
4. the upgrade transaction: the arriving release's own `upgrade` list if the adapter gives one, else `hop.upgrade`
   (Debian: `dist-upgrade`, then `autoremove --purge`; EL: from `yum upgrade` on EL6 to
   `dnf distro-sync --allowerasing` on EL9 and 10),
5. the `post` steps of those fixes, in route order,
6. remove the drop-ins of any `pm-option` used on this hop,
7. `hop.verify` if the adapter has one (EL: the release file names the new major),
8. `hop.after`, whatever happened above (cleanup: package caches, the rpm database),
9. all health checks. Any command that fails in 1 to 7, or any check that passed before the hop and fails now,
   rolls the whole hop back.

Every `when` is evaluated **once, before step 1**, on the box as it is then. A fix can't observe another fix's effect
on the same hop. Ordering is route order. (Open question 6.)

### Preconditions

A precondition is a table with exactly one key:

| predicate | means | Debian (reference) | EL (reference) |
|---|---|---|---|
| `installed = "P"` | P is installed **now** | dpkg state `ii`; `rc` does not count | `rpm -q P` (any version or arch) |
| `file = "/etc/..."` | that file exists | `test -f` | same |
| `file-contains = { path, text }` | the file contains the literal text | `grep -qF` | same |
| `not = {...}` | negation | | |
| `all = [...]`, `any = [...]` | and, or | | |

Paths match `/etc/[A-Za-z0-9_./-]+` and contain no `..`. `text` contains no `'`. Package names match
`[a-z0-9][a-z0-9.+-]*`. There is no shell predicate. An adapter gives each predicate's command as a template in
`[predicates]` (`{pkg}`, `{path}`, `{text}`); `not`, `all` and `any` are the shell's `!`, `&&` and `||` over those.

**The install rule.** A fix that has an `install` step must be gated, positively, on `installed` of the package it
replaces. This is the stray-MySQL lesson from dep-lab round 2. A fix gated on `/etc/mysql/my.cnf` installed a MySQL
server on a web box, because PHP had pulled in mysql-common. The checker enforces the shape of the rule (there is a
positive `installed`). Only review and decoy boxes can enforce the meaning (it's the *right* package).

### Op vocabulary

Ops are typed: a name and a fixed set of arguments, never a command. The core ops are family-neutral, and each
adapter names the **backend** that carries them out (`backend = "apt"` or `"rpm"`). What every op does on every
backend is defined in the [Op reference](#op-reference) below. A fix can't change what an op does; only a change to
this document can. (In this repo, CI always takes its implementation of the ops from the base branch, so a PR that
changes one is reviewed as a spec change before it is ever run.)

| op | args | apt (Debian) | rpm (EL) |
|---|---|---|---|
| `install` | `packages` | `apt-get install P...` | `yum -y install` / `dnf -y --allowerasing install` (vendor .repo files cleared first) |
| `remove` | `packages`, `purge` | `apt-get purge` / `apt-get remove` | `rpm -e` each one that is installed; fails if anything still needs it |
| `repair` | | `apt-get -f install` | remove packages from older releases that have unmet dependencies |
| `force-remove` | `packages`, `purge`, `force` | `dpkg --purge --force-depends ...`; `force` ⊆ {`depends`, `remove-essential`} | `rpm -e --nodeps ...`; `force` ⊆ {`depends`, `scripts`, `db-only`} |
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
  `httpd.drop-missing-modules`, `php.mysqli-report-off`.

`rpm.bootstrap`, `rpm.transit` and `fs.layout` are surgery: dozens of shell lines each, replayed as root on a
stranger's box. They are exactly why ops are typed. The surgery is written once, reviewed as a spec change, and every
fix that needs it says only `{ op = "rpm.transit", release = "alma8", ... }`.

Rules that keep this from being shell with extra steps:

- An op takes exactly its listed args. Extra keys are refused.
- Package names match `[a-z0-9][a-z0-9.+-]*`.
- `write-file` content has no `'` or `%`. The path must match the adapter's `writable` globs, one directory level
  only (`*` never matches `/`). The globs are an allowlist, not "anything under /etc". Cron, systemd units, `/etc/default`, sudoers,
  profile.d, logrotate and apt.conf.d all execute code or grant trust. Growing the list is a spec change.
- EL has a second list, `managed`: config files that `rpm.adopt-rpmnew` and `rpm.set-aside-unowned` may move. Those
  ops never write content. They take the file the package shipped, or move a file no package owns, and keep the old
  one under `/var/lib/wend/saved`.
- Harness-owned paths (sources.list, the harness apt config, yum.repos.d and the yum/dnf config on EL) are never
  writable.
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
  rejects any other new family. Empty for every seed fix. Switching mysql to mariadb isn't new, because the family map
  treats them as the same.

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

`sources` is reserved: a table of repo URL to the sha256 of the repo metadata the run used (`Release` on Debian,
`repomd.xml` on EL), so a later replay can tell whether the mirror it sees is the one the run proved. Nothing writes
it yet. Live EL mirrors change under a route, so this is likely the next field a run gains.

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
or a value from the adapter; all of them have matched strict patterns before anything runs, so none carries a quote,
a space or a `$`.

Values every rpm reference uses, for the release `R` the hop arrives at:

- `SHIM`: the adapter's `[mirror].shim`; a repo URL is `SHIM/<repos value>`.
- `BASE`: the first repo of `R` (`releases.R.repos`, in file order).
- `PM_INSTALL`: `yum -y install` if `releases.R.pm` is `yum`, else `dnf -y --releasever=MAJOR --allowerasing
  --setopt=install_weak_deps=False --setopt=protected_packages= install`.
- `CLEAN_REPOS`: delete every `/etc/yum.repos.d/*.repo` except the harness's `ladder.repo`.
- `SAVED`: `/var/lib/wend/saved`. A file an op moves aside goes to `SAVED` + its original path.
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
- rpm: `rpm -e` with `depends` = `--nodeps`, `scripts` = `--noscripts`, `db-only` = `--justdb` (drop the database
  entry, leave the files). `purge` changes nothing.

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

**`rpm.swap-release`** `remove`, `install` (package names). Replaces the release package without the package
manager, because an epoch drop (CentOS 5's `centos-release` has epoch 10, EL6's has none) or a vendor switch isn't an
upgrade to yum. Effect: `remove` is gone from the database (its scriptlets don't run), and the newest
`install-<digit>...` package for x86_64 or noarch in `BASE` is installed. Fails if there is no such package.
Reference: find it in `PKGLIST(BASE)`, download it, `rpm -e --nodeps --noscripts {remove}`,
`rpm -ivh --nodeps --noscripts` the file, `CLEAN_REPOS`.

**`rpm.drop-arch`** `arches` (list of `i[3-6]86`, at least one). Removes every installed package built for one of
these architectures, files and database entries, with no dependency checks and no scriptlets. EL5 x86_64 installs
carry i386 copies of libraries (curl, glibc, ...) that conflict with EL6's x86_64 packages. Fails if any such
package is still installed afterwards. Reference: `rpm -e --nodeps --noscripts` every
`NAME-VERSION-RELEASE.ARCH` that `rpm -qa` lists with one of the arches, then check `rpm -qa --qf '%{ARCH}\n'` has
none of them.

**`rpm.bootstrap`** `release` (the adapter's `releases.<release>.bootstrap` table must exist; EL defines it for
`centos6` only). Installs the next release's package manager when the old one can't: rpm 4.4 can't read EL6's xz
payloads, and yum 3.2.22 can't check EL6's sha256 repodata. Effect: the files of every package in
`bootstrap.packages` (from `release`'s first repo) are on disk, except their `/etc` and documentation, and the new
rpm has rebuilt the database. The packages stay recorded at their old versions; the hop's own transaction then
installs them properly. Reference:
1. In a fresh `/var/lib/wend/boot`, take `PKGLIST` of `release`'s first repo.
2. Download `bootstrap.p7zip` from `bootstrap.p7zip_repo` and install it with `rpm -Uvh --nosignature`.
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
nothing from older releases is left in the rpm database. Fails if the box doesn't say it is `release` afterwards.
A fix with a non-empty `reinstall` must pass the install rule. Reference, with work dir `/var/lib/wend/boot`, and
repos kept in private `reposdir`s so `/etc/yum.repos.d` stays the harness's:
1. Write a repo file for `transit.from`'s repos plus `transit.extra_repos` (`old`), and one for `release`'s repos
   (`new`).
2. `yum install dnf` from `old` (CentOS 7 extras ships an EL7 dnf).
3. With that dnf and `new`: `--downloadonly` distro-sync into the work dir, `--releasever` = the release's major,
   `--allowerasing --nobest`, no weak deps, no protected packages. Its exit code is ignored; the step fails if
   `rpm`, `rpm-libs`, `glibc`, `dnf` or `python3-dnf` wasn't downloaded. Also fetch `transit.release_package` from
   `PKGLIST(BASE)` if the sync didn't.
4. `rpm -e dnf` (the EL7 one), rebuild the database, and unpack every downloaded package into a staging dir.
5. `rpm -e --nodeps --noscripts {remove}` (the old release package), then run the staged rpm through the staged
   `ld-linux-x86-64.so.2 --library-path <stage>/usr/lib64` with `-Uvh --nodeps --replacefiles --replacepkgs
   --oldpackage --nosignature --nodigest` over every downloaded package.
6. `ldconfig`, rebuild the database, check `/etc/redhat-release` says `release {major}`, `CLEAN_REPOS`.
7. A real distro-sync with `release`'s dnf and the `new` repos (errors reported, not fatal: the EL7 yum
   directories trip it once).
8. Remove the EL7 `yum*` packages without scriptlets; move `/etc/yum/{pluginconf.d,protected.d,vars}` to `SAVED`.
9. `dnf install yum {reinstall}` (fatal on failure).
10. `rpm -e --nodeps --noscripts --justdb` every package (but `gpg-pubkey`) without the `el{major}` tag; delete the
    work dir; `CLEAN_REPOS`.

**`rpm.adopt-rpmnew`** `paths` (on the adapter's `managed` list; globs allowed). For each file that has a
`FILE.rpmnew` next to it, keep the old file under `SAVED` and put the `.rpmnew` in its place. Files without one are
left alone. Never writes content of its own.

**`rpm.set-aside-unowned`** `paths` (on `managed`). Moves each existing file that no installed package owns
(`rpm -qf` fails) to `SAVED`. Never writes content of its own.

**`fs.layout`** `layout` (`usrmove` or `run`).
- `usrmove`: `/bin`, `/sbin`, `/lib` and `/lib64` become symlinks to `usr/bin`, `usr/sbin`, `usr/lib`, `usr/lib64`
  (EL7's `filesystem` package requires it; redhat-upgrade-tool did it at boot). Reference: for each one not already
  a symlink, copy every entry `/usr/<dir>` lacks into it (a symlink in `/usr` that points at the same file is
  replaced by the file); then, in one process (python), rename each dir to `<dir>.usrmove` and create the symlink;
  then `/bin/sh -c true; ldconfig; rpm -q glibc bash` (fails the step if the system no longer runs), and delete the
  `.usrmove` dirs.
- `run`: `/var/run` becomes a symlink to `../run` and `/var/lock` to `../run/lock`, with their contents copied over
  first. Fails if `/var/run` isn't a symlink afterwards.

**`kv.set`** `path` (on `writable`), `values` (a table; keys `[A-Za-z0-9_]+`, values `[A-Za-z0-9_./:-]+`). For a file
of `key=value` lines without sections: every line setting one of the keys, commented out or not
(`^\s*#*\s*key\s*=`), is deleted, and `key=value` is appended for each, in key order. Fails if the file doesn't
exist.

**`kv.unset`** `path` (on `writable`), `keys` (list of `[A-Za-z0-9_]+`). Deletes every uncommented line setting one
of the keys (`^\s*key\s*=`). Works in files with sections too (`my.cnf`). Fails if the file doesn't exist.

The three database ops share a start and a stop. Start: stop any running `mariadbd`/`mysqld`, take the first of
`/usr/libexec/mariadbd`, `/usr/libexec/mysqld`, `/usr/sbin/mariadbd`, `/usr/sbin/mysqld`, create its runtime and log
dirs (`/run/mariadb`, `/run/mysqld`, `/var/log/mariadb`) owned by `mysql` together with `/var/lib/mysql`, remove a
stale socket and pid file, start it as `mysql` in the background, and wait up to 120 s for
`/var/lib/mysql/mysql.sock`. Stop: SIGTERM, then wait up to 300 s; the stop fails if a server is still running.

**`mysql.set-password`** `user` (`[A-Za-z0-9_]+`), `password` (`[A-Za-z0-9_.-]+`). Rehashes one account's password
with the server's current scheme (MySQL 4.0's 16-character hashes stop working with MariaDB 10's clients). Start
with `--skip-grant-tables --skip-networking`, run `SET SESSION old_passwords=0; UPDATE mysql.user SET
Password=PASSWORD('{password}') WHERE User='{user}'` as root, stop. Fails if the SQL or the stop fails. It needs the
plaintext, so a fix using it is always local to one box and never belongs in a shared route.

**`mysql.clean-shutdown`** (no arguments). Start with `--innodb-fast-shutdown=0`, stop. The next major version then
finds a clean redo log (MariaDB 10.11 refuses 10.5's otherwise). Fails if the stop fails.

**`mysql.upgrade-data`** (no arguments). Start the installed server on the existing data, run `mariadb-upgrade` (or
`mysql_upgrade`) `-uroot --force` if there is one, stop. Fails only if the stop fails: the server starting and
stopping cleanly on the data is the point, and the tool's own complaints are informational.

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
`/etc/php.d/99-wend-mysqli-report-off.ini` with `auto_prepend_file` pointing at it.

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
with `*` not crossing a `/`. LIST is `writable` or `managed`.

## Adapter format

`adapters/<family>.toml`, schema `wend.adapter/0`. A fix's `family` names one.

| key | meaning |
|---|---|
| `family`, `backend` | the family's name; `apt` or `rpm` |
| `release_file` | the file whose content names the running release (shown in runs and checks) |
| `ladder` | every release in order; a hop goes one step along it |
| `may_skip` | releases a route may cross inside one hop (see Route) |
| `harness_owned` | path prefixes no fix may write |
| `writable`, `managed` | the allowlists (globs) named by `write-file`, `kv.*` (`writable`) and the `rpm.*` file ops (`managed`) |
| `force` | the `force-remove` flags this family allows (Debian: `depends`, `remove-essential`) |
| `[releases]` | where packages come from. apt: `archive`, `current`, `current_from` (releases from `current_from` on use `current`). rpm: `releases.<id>` with `major`, `pm` (`yum`/`dnf`), `repos` (name = host/path, through the shim), optional `upgrade`, `bootstrap`, `transit` |
| `[mirror]` | rpm: `shim` (the mirror shim's base URL) and `probe` (a path that must be fetchable before a run starts) |
| `[hop]` | `set_release`, `refresh`, `upgrade` (list), optional `verify`, `after` (list), `autoremove`, `obsolete`, `installed_list`; apt also `apt_conf`, the harness's apt config |
| `[predicates]` | `installed`, `file`, `file-contains` command templates |
| `[pm_options.<name>]` | `path`, `content` |
| `[ops."<name>"]` | `summary`, `args` (see Argument specs). Only ops defined in the Op reference may appear |
| `[checks]` | universal health checks: `pkgdb` is a command; `current` and `services` are described in words and built by the harness from the box |
| `[box]` | how to build a fresh box of this family (rpm: `seeds`, the packages to bootstrap a root from) |

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
- **Unmount and verify before any cleanup.** Before deleting or rsync-restoring a box root, unmount everything under
  it and check `/proc/self/mountinfo` for leftovers. If anything is still mounted, refuse to delete.
- Daemons never start on their own (`policy-rc.d` on Debian). Health checks start what they need and stop it again.
- A rollback restores the whole root from the pre-hop snapshot. Nothing from a failed hop survives.

## Open questions

Questions 1 to 6 were the ones RPM could answer. The CentOS round answered them as follows; 7 to 10 are still open.

1. **Op vocabulary.** Yes, RPM needed ops Debian never did, and none of them is a variant of an apt op: bootstrapping
   the next package manager (twice), the UsrMove and /run layout changes, swapping release packages, adopting
   `.rpmnew` files, and running a database once to migrate its data. Database rebuilds and module-stream resets
   turned out to be harness work on every hop, not fix steps. `force-remove` maps to `rpm -e --nodeps`, with
   `--noscripts` and `--justdb` as EL's own force flags. So: typed ops with a backend per package manager.
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
    satisfy yet?
