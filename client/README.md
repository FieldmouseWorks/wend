# wend client

The thing that runs on a real machine: a single static Rust binary that reads the atlas (adapters, fixes, routes,
runs), finds the route for the box, and walks it. No model, no network beyond the distro's mirrors.

It is built in slices, each one proven against the Python engine in `tools/`, which stays CI's judge and SPEC.md's
reference implementation:

1. **Read, check, compile** (this slice). Load the atlas, compute content ids, run the gate's schema rules, and
   compile fixes and hops into the exact commands CI proved.
2. **Walk a route on a root.** Evaluate preconditions on the box, run each hop, run the checks, roll back on red,
   write the run. Proven by replaying the CI boxes (Debian etch to trixie, CentOS 5 to AlmaLinux 10) and matching
   the Python harness's runs.
3. **Fetch and verify the route before touching the box**: packages pinned by digest, a source fallback chain.
   Blocked on SPEC open question 12 (runs don't record package digests yet).

## Build

```
cargo build --release --manifest-path client/Cargo.toml                                    # for this machine
cargo build --release --manifest-path client/Cargo.toml --target x86_64-unknown-linux-musl # static, ~2.5 MB
```

## Use

Run it from a checkout of this repo (or pass `--atlas DIR`):

```
wend check                                       # the gate's schema rules over the whole atlas
wend ids                                         # every fix's and route's content id
wend compile fixes/el/alma9/el8-transit.toml     # what a fix runs, and its precondition as a test
wend hop centos5-to-alma10 alma9                 # every command of one hop, with the fixes the route pins there
wend hop etch-to-trixie wheezy --fired purge-stale-libdb4
```

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

## Known limits

- Rust's standard library supports Linux 3.2 and later. CentOS 5 runs 2.6.18 and CentOS 6 runs 2.6.32, so on those
  kernels the binary is untested (it may still run). The routes already need real reboots into newer kernels on
  the way up (wend#13), which is where this gets settled.
- The checker's error messages follow tools/check.py's but aren't identical (Python prints `repr()` of values);
  parity compares verdicts, not wording.
