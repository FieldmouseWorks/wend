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
lists in `may_skip`: a route may leave it out. EL lists `alma8`, because the box only ever passes through EL8
(`rpm.transit`) on the hop to EL9, and `centos8`, where boxes start but no route lands. No health check runs on a
release the route skips. `wend discover` leaves `may_skip` releases out of a new route's hops.

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

Every box that starts at a route's `from` release is a decoy for each of that route's fixes that doesn't claim it (a box
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
- Container tier only for now: the adapter has no `releases.trusty.install` or `kernel_newest`, so the VM tier
  (tools/vm.py) can't build or boot an Ubuntu box yet.

### Enterprise Linux (`el`, rpm backend)

See `adapters/el.toml`. Notes that matter:

- The ladder crosses vendors: `centos5 centos6 centos7 centos8 alma8 alma9 alma10`. A family isn't a distro; release
  ids carry the vendor. CentOS 8 and AlmaLinux 8 sit side by side: routes from EL7 and earlier skip both (EL8 is
  crossed inside the hop to EL9), and a route from CentOS 8 goes straight to EL9 with its own dnf.
- The package manager changes mid-ladder (yum through 7, dnf from 8), and so does the upgrade verb: `upgrade` on EL6,
  `distro-sync` from EL7 (EL6's grep is newer than EL7's). Each release can set its own `upgrade` in the adapter.
- The harness owns `/etc/yum.repos.d` and clears the vendor .repo files every release package drops (they point at
  dead mirrors). After each hop it also rebuilds the rpm database and, on EL10, moves it to `/usr/lib/sysimage/rpm`.
- CentOS 5's yum can't speak today's TLS, so repos go through a mirror shim the harness runs on the host
  (`[mirror]` in the adapter): plain http on 127.0.0.1, fetching `https://<host>/<path>` for
  `http://127.0.0.1:<port>/<host>/<path>`, for a fixed list of hosts, and caching what it fetched.
- No EL release has an in-place path through a chroot (Leapp and redhat-upgrade-tool reboot), so every hop is a plain
  transaction plus fixes.
- On a live box the hop to EL9 runs its transaction offline, as a chroot does (`SYSTEMD_OFFLINE=1`: systemctl skips
  the starts, restarts and `daemon-reexec` scriptlets ask for; `enable --now` and `disable --now` still reach PID 1,
  so an erased EL8 package's service is stopped by the old systemd, which manages that). Otherwise systemd's `%post`
  re-executes PID 1 as EL9's systemd on EL7's 3.10 kernel, which never sees a stopped unit's cgroup empty, and the
  first scriptlet that waits on a stop or start (audit's) hangs the transaction (wend#75). The old daemons run on
  until the hop's reboot, except sshd: its listener re-executes `/usr/sbin/sshd` for every connection and can't hand
  its state to a newer binary, so it is restarted after `rpm.transit` and after the transaction, and the step fails
  unless a new connection gets the ssh greeting. `hop.after` restarts it once more, so a hop that fails in between
  doesn't leave the machine locked.
- `installed` is `rpm -q`. "Explicitly installed" doesn't exist before yum 3 on EL6, so a box's services check uses
  "nothing requires it, by name or by any capability it provides" instead.
- `current`: the service binaries come from a package whose dist tag is the running major (`[._]el$MAJOR`).
- Boxes can name roles that start daemons in `isolate`; those checks run in a network namespace of their own.

#### Add-on repositories

Most real EL machines carry more than the vendor's repos: EPEL above all (EPEL's own mirror statistics are how the
stuck EL7 fleet is counted). The adapter's `[addons.<name>]` tables name the ones the harness knows how to carry:
`marker` (the package whose presence means the box uses it, EPEL: `epel-release`), `vendor` (the rpm `VENDOR` of its
packages, EPEL: `Fedora Project`) and `repos` (release id = host/path, through the shim, like `releases.*.repos`).

- Every hop's `set_release` appends `{addons}`: for each add-on with a repo for the release being entered, the add-on's
  repo goes into the harness's repo file as `[<release>-<name>]` if its marker is installed at that moment. A box
  without the marker never sees the repo: no hop's repos or `rpm.transit`'s `new` repos ship a marker, so no fix can
  pull add-on packages onto it. On a box with the marker, a fix's `install` can pull any of the add-on's packages
  (unsigned, like the vendor's: `gpgcheck=0` behind the shim).
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
   installed: read the package names in the primary metadata (gzip or xz) of `release`'s repos and its add-on repos,
   and refuse, naming them, if any of those packages has no package of the same name there. Only the work dir has
   changed at that point.
2. `yum install dnf` from `old` (CentOS 7 extras ships an EL7 dnf).
3. With that dnf and `new`: `--downloadonly` distro-sync into the work dir, `--releasever` = the release's major,
   `--allowerasing --setopt=best=False` (EL7's dnf 4.0 has no `--nobest`), no weak deps, no protected packages. Its exit code is ignored; the step fails if
   `rpm`, `rpm-libs`, `glibc`, `dnf` or `python3-dnf` wasn't downloaded, or any of the add-on packages wasn't (as
   with the first five, EL7's dnf is still installed when that fails). Also fetch `transit.release_package` from
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
| `may_skip` | releases a route may leave out of its hops (see Route) |
| `harness_owned` | path prefixes no fix may write |
| `writable`, `managed` | the allowlists (globs) named by `write-file` (`writable`) and the `rpm.*` file ops (`managed`). Nothing under `/etc/wend/` is ever on either |
| `[kv."<path>"]` | the files `kv.*` may touch: `keys` (the only keys it may set or unset; never one that grants access) and `set` (false for files with sections, which only take `kv.unset`) |
| `force` | required: the `force-remove` flags this family allows, a subset of its backend's (apt: `depends`, `remove-essential`; rpm: `depends`, `scripts`) |
| `[releases]` | where packages come from. apt: `archive`, `current`, `current_from` (releases from `current_from` on use `current`), and optional `releases.<id>.upgrade` like rpm's. rpm: `releases.<id>` with `major`, `pm` (`yum`/`dnf`), `repos` (name = host/path, through the shim), optional `upgrade`, `bootstrap`, `transit`, `install` (the VM tier's installer), `reboot` (`before-post`, see "Kernels and the bootloader"). apt: a start release may have `releases.<id>.install` too |
| `[addons.<name>]` | rpm: `marker`, `vendor`, `repos` (see "Add-on repositories") |
| `[mirror]` | rpm: `shim` (the mirror shim's base URL) and `probe` (a path that must be fetchable before a run starts) |
| `[hop]` | `set_release`, `refresh`, `upgrade` (list), optional `verify`, `after` (list), `kernel_before` and `kernel_after` (lists, see "Kernels and the bootloader"), `autoremove`, `obsolete`, `installed_list`, `kernel_newest` (the VM tier's newest-kernel query); apt also `apt_conf`, the harness's apt config |
| `[predicates]` | `installed`, `file`, `file-contains`, `mysql-option` command templates |
| `[pm_options.<name>]` | `path`, `content`. apt: only `overwrite-conflicting-files`; rpm: none |
| `[ops."<name>"]` | `summary`, `args` (see Argument specs). Only ops the Op reference defines for this backend may appear. Whatever a `pattern`, `list` or `table` says, every value a fix passes through one must also match `[A-Za-z0-9_.:/+=@][A-Za-z0-9_.:/+=@-]*`: no quote, whitespace, backslash, glob character, `%`, any of `` $`;&|<>(){}~ ``, or leading `-` |
| `[checks]` | universal health checks: `pkgdb` is a command; `current` and `services` are described in words and built by the harness from the box |
| `[hardware]` | required: what a machine needs before a route boots it into each release (see "Hardware preflight"): `boot`, `old_kernels`, and a table per release a route can boot into |
| `[box]` | how to build a fresh box of this family (rpm: `seeds`, the packages to bootstrap a root from; apt: optional `debootstrap_script`, a script name under the host's `/usr/share/debootstrap/scripts`) |

These checks narrow what an adapter lets fixes do (ops, force flags, argument values). They don't judge an adapter's
allowlists, hop commands or package lists: those are reviewed as spec changes, and CI never takes them from a PR.

Templates in `[hop]`, `releases.*.upgrade` and `[predicates]` use `{name}` placeholders, replaced literally (no
other brace syntax is touched, so `${...}` and `%{...}` pass through). Values for a hop:

- apt: `{mirror}` (archive or current, see `[releases]`), `{release}`.
- rpm: `{release}`, `{major}`, `{shim}`, `{repofile}` (a yum repo file with one section `[<release>-<name>]` per repo,
  `baseurl=SHIM/<repo>/`, `gpgcheck=0`, newlines as `\n` for printf), `{addons}` (the shell that appends each installed
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

A chroot box has no kernel, and these steps do nothing there. A real machine's kernel and boot entries belong to the
harness, not to fixes, because the release's own tools can't carry them up the ladder:

- **The old release's kernel can't stay in the rpm database.** EL5's kernels require `mkinitrd`, which is gone in EL6
  (`pkgdb` fails). EL6's kernel requires `/sbin/new-kernel-pkg`, which nothing provides on EL7 after UsrMove, so the
  transaction fails. And dnf won't remove the running kernel, so `rpm.transit` fails. So `hop.kernel_before` drops
  the installed kernel packages' records with `rpm -e --justdb --nodeps --noscripts` and keeps their files: the
  running kernel and its boot entry stay as the fallback. This is the one place, besides `rpm.transit`'s last step,
  where wend drops records and keeps files. Fixes can't, because a fix doing it could fake `pkgdb`. It then removes
  the files of kernels that no package owns and that aren't running or GRUB's default, so the release's kernel fits
  in `/boot` (the VM tier's CentOS 5 /boot is 200 MiB), its kdump image with it. This is how the previous hop's kernel,
  once a reboot has moved off it, finally goes. The rescue image (`vmlinuz-0-rescue-<machine id>`, its initramfs and BLS
  entry) goes too, unless it is or may be GRUB's default (GRUB legacy's `default=saved`, GRUB 2's `saved_entry`): it is
  the old release's kernel with a generic initramfs (62 MiB on EL7), and EL9's `51-dracut-rescue.install`, finding no
  BLS entry for it, copies the new `vmlinuz` over the old one and keeps EL7's initramfs. No hop builds another:
  `hop.kernel_before` sets `dracut_rescue_image="no"` in a drop-in (`/etc/dracut.conf.d/99-wend-no-rescue.conf`) that
  `hop.after` removes, since a generic image of 54 MiB (Alma 9) or 65 MiB (Alma 10) doesn't fit the VM tier's 200 MiB
  CentOS 5 `/boot` beside two kernels. The machine lands without a rescue image, and its next kernel update builds one
  for the release it runs, as EL does when none exists. None of the kernel steps acts unless the machine is booted from
  this root (`/` is PID 1's root): in a chroot of someone's disk the records, files and boot entries are left as they
  are, since `uname -r` there is the host's kernel, not the box's. The fallback the running kernel's entry gives is a
  manual one: GRUB legacy has no `fallback=` set, so a hop that fails between the two halves leaves the default on the
  running kernel's entry, which still boots.
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

**Facts.** `wend facts` reads the machine (schema `wend.hardware/2`; its `runtimes` are SPEC "Runtime preflight"): the kernel's architecture (`uname -m`) and
the userland's word size (the ELF class of `/bin/sh`); the CPU's x86-64 level features from CPUID itself, never
`/proc/cpuinfo`, since an old kernel doesn't name features newer than itself (2.6.18 has no `avx2`), and the XSAVE
bit, not OSXSAVE, since the old kernel may not have turned it on and the new one will; CPUID's hypervisor bit;
firmware (`/sys/firmware/efi` means UEFI); `MemTotal`; free space on `/`; `/boot`: whether it is a filesystem of its
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
are proven to carry a machine through; the VM tier boots its guests by BIOS, so every adapter says `["bios"]` until a
UEFI replay proves more. `old_kernels` says which kernels sit in `/boot` beside the one a hop installs: `"running"`
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
the first release's, on the second hop), or every kernel so far up to `autoremove_from`. A hop also fails
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

**Facts.** `wend facts` (schema `wend.hardware/2`) lists every Python 2 user under `runtimes`, each with the
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
anyone else rather than miss them.

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

**The verdict.** A hop has a runtime problem when the machine has its own Python 2 users and the release ships no
Python 2. Each hop lists those in `runtime_problems`, apart from `problems`, which stay the hardware's: the machine
would boot fine, and the owner's program wouldn't run. `keeps_runtimes_on` names the last release before the first
such hop, or the route's last release when no hop drops it (`null` when the machine has no Python 2 of its own); a
hop with runtime problems isn't `ok`, `runtimes` lists the machine's own users, and `ok`
needs both the hardware and the runtime to reach the route's end. When a user's own 2.x isn't among a release's
versions (2.4 on squeeze, which ships 2.5 and 2.6), the first such hop notes it. When Python 2 goes before the
hardware stops, the report lists the programs and offers the choice: stop at `keeps_runtimes_on` (on EL7, stay where
it is), or shelter them and land where the hardware allows. When the hardware stops first, its verdict stands
alone. Nothing overrides a runtime problem: the owner shelters the programs, ports them, or retires them and checks
again.

Not checked yet: services that are enabled but not running, and timers; scripts a cron job reaches through a shell
script of its own; a Python 2 an add-on repository ships (EPEL's `python26` on EL5); third-party packages of the
owner's own code (an rpm or deb the owner built counts as the distro's); a Python 2 built from source, whose shared
libraries the hop may take away even though its files stay; and runtimes other than Python 2 (PHP 5, Java 6).

## Sheltering an app (draft: not built yet)

This section designs the **shelter** choice that runtime preflight offers. Nothing here is built yet; it lands op by
op, each with its own PR, after a box with a Python 2 app of its own (`el/centos7-pyapp`) is on main.

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
   outside the vendor's archive.
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
can't be idmapped. The image is built locally, loaded with `podman load`, and run by its image ID. There's no
registry. Its size counts toward the hop's `disk_mib`, and a `/var/lib/containers` on XFS with `ftype=0` (overlay
storage refuses it) fails preflight.

wend writes a plain systemd unit, not a quadlet (bookworm's podman has none):
`/etc/systemd/system/wend-shelter-<app>.service` runs `podman run --rm --name wend-<app> ... <image ID>
<the program's own command line>`. It's a harness-owned path, and the unit's header says what it is and that its
userland is unpatched.

Defaults, which the plan shows and the run records:
- `--read-only` root, with a tmpfs `/tmp` and `/run`
- a fixed user namespace, so the container's root isn't the host's root: `shelter.start` reserves a 65536-id range
  for the shelter in `/etc/subuid` and `/etc/subgid` under the name `wend-<app>` (a harness-owned line, so nothing
  else allocates it), records it in the lock, and runs with explicit `--uidmap`/`--gidmap` over that range. Not
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
  reloads the table unit right after, which puts the table back; between nftables' flush and that reload the
  shelter's egress is open for a moment, and the spec doesn't pretend it isn't. A hand-typed `nft flush ruleset` still drops the table under a running
  shelter, which systemd can't see; the spec doesn't claim otherwise. Runtime preflight records the TCP connections and
  listening sockets each sheltered program actually has (`/proc/<pid>/net/tcp*` matched to its socket inodes,
  sampled while facts are gathered), and the plan prefills the opt-ins from them, so "private" rarely breaks a
  working app. A connection that only happens at 3 a.m. won't be seen, and the plan says so. Ports the program
  listens on are published on the same host address.
- **Host services.** Inside the shelter, `127.0.0.1` is the shelter's own, not the host's. A unix-socket client
  (MySQLdb's `host="localhost"` uses the socket at its compiled-in path) is served by a wend-written
  `systemd-socket-proxyd` unit on the host that listens on a socket in `/run/wend/shelter/<app>/` and connects to
  the real one per connection. That directory is mounted where the shelter's library looks, so a MariaDB restart
  doesn't strand it and the database's own directory is never mounted. The proxy runs as the app's host user
  (`User=`, recorded in the run), never as root: MariaDB 10.4 and later give `root@localhost` `unix_socket` auth,
  so a root proxy would hand anything in the shelter database root with no password, and an app that logs in by
  `unix_socket` as its own user keeps working. On EL the proxy's directory is app-only, so it gets
  `container_file_t` (`:Z`) like the app's other directories; whether `container_t` may also `connectto` the
  proxy's domain is something only the VM tier shows, and if SELinux denies it, `shelter.start`'s check fails and
  the hop rolls back. The app inside runs as a mapped uid, and that directory has no `:idmap`, so the proxy's
  socket and its directory are owned by that mapped uid (`SocketUser=`, mode 0600; the directory 0700), not by
  the app's host user. The mapping is fixed (above), so that uid is the same at every start. An app that reaches a host service over
  TCP on `127.0.0.1` can't be served without changing its config, which wend doesn't do: the plan names it, and the
  owner's choices are `network = "host"` (opted in by name, recorded in the run as an exception) or editing the app.
- **Mounts:** the code tree read-only. Data directories read-write (`:idmap`): the ones the program has open for
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
| `shelter.prove` | right after `shelter.build` | runs the app's checks against the shelter root in the sandbox (a chroot), with the real data mounted read-only; refuses the route if the app doesn't run there |
| `shelter.stop-native` | pre phase of the hop that drops Python 2 | stops and disables the app's own service unit, and comments out each cron line that runs a sheltered program with a `# wend-shelter:<app>` marker (the original goes under `/var/lib/wend/saved`; the precondition is the line's exact text). Nothing is deleted, so rolling the hop back restores both |
| `shelter.start` | post phase of that hop | installs podman (an `install` op of the landing release), reserves the shelter's subuid/subgid range, the network, the nftables unit and the socket proxies, loads the image, writes and enables the unit, and writes one systemd timer per moved cron line |

A cron line becomes a timer only when its schedule translates exactly to `OnCalendar=`. Every standard five-field
schedule and the `@daily`-style names do. When both day-of-month and day-of-week are restricted, cron runs on
either, so that becomes two `OnCalendar=` lines. `@reboot` becomes a oneshot at boot. Anything else is refused at
plan time.
The timer runs the same command in the shelter (`podman run --rm ... <image ID> <command>`) with the crontab's
`PATH` and environment. Cron's `MAILTO` doesn't carry over: output goes to the journal, and the plan says so.

`shelter.start`'s checks are the app's own checks against the real container, on the box, under the real
confinement (userns, MAC, read-only root, dropped capabilities, the network table). If they fail, the hop rolls
back like any other, and the app comes back natively on the release before. Rolling back undoes everything
`shelter.start` added, not only the container: the units, timers, proxies and nftables table are removed, the
subuid/subgid line goes, and the `:Z` labels are restored. That's the one place confinement is
tested on the machine itself; `shelter.prove` only proves the userland.

The app is down from `stop-native` until `start`: one hop, including that hop's reboot. That's the same window the
route itself causes.

### Proof

- **Box first** (its own PR): `el/centos7-pyapp` is a centos7-lamp sibling with a Python 2 service (a small
  BaseHTTPServer JSON API reading MariaDB through MySQLdb over the socket, writing to `/var/lib/billing`), its
  systemd unit, and a nightly cron job. Its `app` check fetches the row over HTTP. Its `cron` check runs the job
  and reads what it wrote.
- **CI (chroot tier):** chroots run no podman, so CI proves the userland only: it checks the shelter by chroot
  into the shelter root with the same mounts. `site`, `app` and `cron` must stay green at every hop. It proves
  nothing about confinement, and the run says so.
- **VM tier (remi):** the real thing, and the only proof of confinement. centos7-pyapp to alma10 under podman, with
  SELinux enforcing, real reboots, and checks green after each hop. The same goes for a Debian bullseye-to-trixie
  box (AppArmor) later.

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
- building from anything but the vendor's archive, verified as above (or, once it exists, wend's pinned archive)

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
  lists in `/run/systemd/resolve/resolv.conf`, so the stub appearing on a release isn't a change;
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
  ssh and http are forwarded to fixed ports on `127.0.0.1`, so a host runs one VM replay at a time.
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
  (`xfs_info`'s `crc` and `ftype`).
- A route that ends with `prune_obsolete` gets the chroot walk's prune after its last hop: the packages the target
  no longer ships are purged and autoremoved, the set split in halves whenever a step fails or a check is lost, and
  each attempt first drops what an earlier autoremove already took. Each attempt runs on its own overlay of the last
  good disk and reboots; a kept attempt is the next base. As in the chroot walk, a removal the prune drops never fails
  the run; a guest that doesn't come up from the last good disk, or a box it can't read, does. It records what was obsolete before and after.
- Adapter: `releases.<id>.install` names a start release's installer. EL: its `tree` through the shim and the
  `pxeboot` dir in it (the shim answers single byte ranges, which anaconda uses to read package headers), `packages`
  (what the install needs besides the box's: the bootloader), `ks_end` (sections end with `%end`), `append`
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
  run on UEFI (`/sys/firmware/efi`). A start release whose installer can't install on UEFI isn't built:
  `releases.<id>.install.uefi` says it can (trusty, CentOS 7; not etch or CentOS 5, whose installers predate UEFI on
  x86-64, so no machine running them boots that way). A run records its `firmware`, and resumes only a run on the same.
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
   in `may_skip` (EL8, crossed inside one hop by `rpm.transit`; CentOS 8, where boxes start but no route lands).
   Two vendors at one major version are two ladder entries, in the order a route could pass them.
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
13. **A machine the route can't carry in place.** Some machines can't take a route on their own disk: CentOS 7
    installed from 7.0 to 7.2 media has XFS v4 everywhere, which AlmaLinux 10's kernel can't mount and nothing
    converts. Proposed: a second root beside the old one (rebuild and carry), done first as **relocate** (copy the
    root onto a fresh filesystem, flip the boot with a one-shot trial, keep the old root as rollback, then walk the
    existing route), with state movers for a fresh target root second. Design in `docs/rebuild-and-carry.md`; each
    piece moves into this document in the PR that builds it.
