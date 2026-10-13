# wend client

The thing that runs on a real machine: a single static Rust binary that reads the atlas (adapters, fixes, routes,
runs), finds the route for the box, and walks it. No model, no network beyond the distro's mirrors.

It is built in slices, each one proven against the Python engine in `tools/`, which stays CI's judge and SPEC.md's
reference implementation:

1. **Read, check, compile.** Load the atlas, compute content ids, run the gate's schema rules, and
   compile fixes and hops into the exact commands CI proved.
2. **Walk a route on a root.** Evaluate preconditions on the box, run each hop, run the checks, roll
   back on red, prune obsolete packages, write the run. Proven by replaying the CI boxes and matching the Python
   harness's runs (see below). It walks a root filesystem (a chroot), not the running system: walking `/` needs a
   snapshot backend first, and `wend walk --root /` refuses.
3. **Fetch and verify by digest** (this slice). With a run that pins its sources (SPEC "Pinned sources"), the
   walk's package traffic goes through a verifying proxy: only bytes the run's pins vouch for, from a cache, a list of
   sources, then the origin. Walk a copy once to fill the cache; walk the machine `--offline` from it.

## Build

```
cargo build --release --manifest-path client/Cargo.toml                                    # for this machine
cargo build --release --manifest-path client/Cargo.toml --target x86_64-unknown-linux-musl # static, ~3 MB
```

## Use

Run it from a checkout of this repo (or pass `--atlas DIR`):

```
wend check                                       # the gate's schema rules over the whole atlas
wend ids                                         # every fix's and route's content id
wend compile fixes/el/alma9/el8-transit.toml     # what a fix runs, and its precondition as a test
wend hop centos5-to-alma10 alma9                 # every command of one hop, with the fixes the route pins there
wend hop etch-to-trixie wheezy --fired purge-stale-libdb4
sudo wend walk etch-to-trixie --root /srv/etch --checks boxes/debian/etch-web/box.toml --out run.json
```

`walk` runs as root, copies the root to a snapshot before each hop (rsync, under `--work`, default
`/var/tmp/wend-client`), and restores it when a hop fails or loses a check. It mounts a private tmpfs `/dev` and fresh
`/proc` and `/sys` inside the root, never the host's. EL routes fetch through `tools/mirror.py` on port 8085 (old yum
can't speak the vault's TLS), as the Python harness does; `walk` checks it is up before it starts.

### Hardware preflight

```
sudo wend preflight centos5-to-alma10            # on the machine, as root: can it land on every release the route boots?
sudo wend facts > box.json                       # or read the machine here...
wend preflight centos5-to-alma10 --facts box.json --json    # ...and judge it anywhere
```

SPEC "Hardware preflight". It reads the CPU's x86-64 level features from CPUID (not `/proc/cpuinfo`, which an old
kernel under-reports), the firmware, memory, free space on `/`, what `/boot` holds, the modules the machine
blacklists, and every PCI device's IDs, bound driver and whether that driver comes from outside the kernel (ELRepo's
kmods, DKMS), and holds them to the adapter's `[hardware]` table: each release's CPU level, memory, disk and `/boot`
footprint, and its driver table (`adapters/drivers/<family>/<release>.txt`, made by `tools/hwdata.py` from the
release's own kernel package). It prints each hop, what would break there (a CPU the release's glibc refuses, a disk
controller or network card in use that the release's kernel doesn't drive, a kernel that won't fit in `/boot` beside
the ones the hop keeps), and the furthest release the machine can land on, and exits 1 unless the whole route fits. On a VM whose CPU model is too low it says how to raise it. It touches nothing.

It also finds the machine's own Python 2 programs (SPEC "Runtime preflight"): running processes, cron jobs, and
anything with `libpython2` loaded (mod_wsgi), less what the distro's own packages own. When the route leaves Python 2
behind before the hardware stops, it names the last release that ships it (adapters' `[runtimes]`, from
`tools/runtimes.py`) and offers the choice: stop there, or shelter those programs and go on.

### Verified fetching (`--run`)

```
sudo wend walk centos5-to-alma10 --root /srv/copy --checks boxes/el/centos5-web/box.toml \
     --run runs/<the CI run>.json --cache /var/cache/wend          # fills the cache, checking every byte
sudo wend walk centos5-to-alma10 --root /srv/real --checks ... --run ... --cache /var/cache/wend --offline
```

`--run` names a passing run of this route. Its `hops[].sources` pin each hop's top metadata file (apt's
`Release`/`InRelease`, rpm's `repomd.xml`) by sha256. The client starts an HTTP proxy on 127.0.0.1 and points the box's
repos at it (apt's mirror, or the rpm shim, which it replaces: no `tools/mirror.py` needed). The proxy trusts a file
only through that chain: a pinned top file, then each index it lists at the hash it lists, then each package an index
lists at the hash the index gives. Anything else is refused (403, logged at the end of the walk), and unpinned
metadata (`InRelease` when the run pinned `Release`, signatures) gets a 404 so the package manager falls back to what
was pinned. One exception: a suite whose hop pinned its indexes directly (wheezy, whose apt deletes the `Release`)
gets its `Release` served unverified, kept by name for offline walks. It vouches for nothing there, since each index
is checked against its own pin, and its `.gz` is a gzip the proxy makes from the verified bytes, never the archive's
own (a `.gz` can carry a second member that apt would read too). When a package turns up that no index has taught the proxy yet (the package manager
read its indexes from its own cache), the proxy learns every index the run pins and looks again; a request that comes
in meanwhile waits for it.

Bytes come from `--cache DIR` (default `<work>/cache`, files named by their hash; held to the work directory's rules:
owned by root, nobody else can write it, outside the root), then each `--source` in order (a
directory or URL laid out `host/path`, like the shim's cache or an org mirror), then the origin unless `--offline`.
The first bytes that hash right win and go into the cache. When no source has them, the request is refused and the
hop rolls back: SPEC's default for a vanished snapshot (`--live` is wend#19). Fetching shells out to `curl`, so
proxies and CA bundles are the host's.

Proven on centos5-to-alma10 (el/centos5-web): a cold walk through the proxy passed with nothing refused and filled
1.5 GB of cache. A second walk on a fresh copy `--offline` passed in 9 minutes. With one cached RPM corrupted, the
offline walk refused it and rolled the hop back. On etch-to-trixie (debian/etch-web), a cold walk recorded the same
sources as the harness run, and an `--offline` walk from its cache passed in 12 minutes.

After a walk the root's repo config still points at the proxy, which is gone: point it back at a mirror before using
its package manager. Not done yet: downloads are held in memory whole and unbounded (a source or origin is trusted not
to send endless bytes, only to send right ones), nothing reaches the package manager until a file is in and hashed, and
two requests for one file download it twice.

The repo's `./wend` is the Python tool for contributors (discover, verify, report). This binary is the client a
machine's owner runs. They share a name because the client is the product; the Python tool is how the map gets made.

## Parity with the Python engine

The ops are typed, and what each one runs is the backend's **reference** in SPEC.md. The client runs exactly those
commands, so a route CI proved is the route the client walks. `client/parity.py` holds it to that:

```
python3 -B client/parity.py client/target/release/wend
```

It compares every content id, every fix compiled, every hop of every route (with all its pinned fixes and with
none), one example of every op on every backend, every predicate shape, and the gate's own broken cases from
`tools/test_check.py`, which the Rust checker must refuse exactly where the Python one does. The `client` workflow
runs it on every PR that touches the client, the tools, the adapters or the atlas, so a spec change (a new op, a
changed reference command) that the client hasn't caught up with shows up as a red `client` check, not as a client
that silently runs something CI never proved.

## Replays

The same CI box, built by `./wend replay`, walked by both engines on one machine:

| Box | Route | Python | Rust |
| --- | --- | --- | --- |
| debian/etch-web | etch-to-trixie | 13.7, 9 hops, 9 fixes, 10/10 checks, 58 obsolete pruned | identical |
| el/centos5-web | centos5-to-alma10 | AlmaLinux 10.2, 4 hops, 26 fixes, 12/12 checks | identical |

"Identical" means the run JSON matches field for field (every hop's fired fix ids, the final checks, the obsolete
package counts) except timing, the runner line and `log_sha256`, which hashes command output.

## Known limits

- Rust's standard library supports Linux 3.2 and later. CentOS 5 runs 2.6.18 and CentOS 6 runs 2.6.32, so on those
  kernels the binary is untested (it may still run). The routes already need real reboots into newer kernels on
  the way up (wend#13), which is where this gets settled.
- The checker is stricter than tools/check.py in one direction only: it refuses wrong-type fields (a `pre` that
  isn't a list of tables, a `packages` that is a string, an adapter op without an `args` table, floats and dates
  anywhere in a fix or route), several of which Python accepts or crashes on. Parity requires Rust to refuse
  everything Python refuses, and lists what only Rust refuses so tools/check.py can be tightened to match.
- `walk` refuses a root with filesystems mounted inside it (it can't snapshot them), a work directory that isn't
  root's alone, and a work directory and root that contain each other. A root that is itself a mount point (a mounted
  LVM snapshot or disk image) is fine. It never mounts on or writes through a symlink in the root, so a root whose
  `etc/resolv.conf` is a symlink gets no name service until that's a plain file. A plain one is lent the host's
  resolver for each command and gets its own contents back afterwards, unless the command replaced it.
- On apt systems `walk` puts the harness's `usr/sbin/policy-rc.d` (daemons never start) and
  `etc/apt/apt.conf.d/00wend` into the root for the length of the walk, as a CI box has them, and restores the
  root's own (or none) at the end.
- Snapshots keep hard links, ACLs and extended attributes (SELinux labels, file capabilities): `rsync -aHAXx`.
  The work directory needs a filesystem with xattrs (ext4, xfs, btrfs, tmpfs). On an SELinux-enforcing host, labels
  the host policy doesn't know (an old EL root on a new RHEL host) can make the snapshot fail; the walk then stops
  before touching the root.
  Root and work paths can't contain whitespace or backslashes (the kernel escapes those in its mount table, and
  the client must read its own mounts back exactly). A root missing `proc`, `sys`, `dev` or `etc` gets them created
  as empty directories.
- Interrupting `walk` mid-hop leaves the root mid-hop. The snapshot of its state before the hop stays in the work
  directory, and the error says how to restore it. The same goes for any walk that stops on an error.
- The verifying proxy parses gzip and plain indexes only. apt's `.xz`/`.bz2` indexes are served verified, and their
  packages are learned from the `.gz` form the same `Release` lists. yum's `primary.sqlite` is served verified, and
  packages come from `primary.xml.gz`, which the proxy fetches itself.
- Boxes are CI fixtures the client never sees, so it doesn't check them (or whether a run's box exists).
- The checker's error messages follow tools/check.py's but aren't identical (Python prints `repr()` of values);
  parity compares verdicts, not wording.
