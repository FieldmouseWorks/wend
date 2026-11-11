# Lab jobs on remi

remi is the maintainer's lab machine: KVM, real installs, real reboots. It's where `tools/`, `adapters/` and boxes are
proven (CONTRIBUTING.md, "Proof on remi"). This page covers how work gets onto it. `tools/remi.py` holds the
mechanics, and its docstring lists every command.

## The idea

A job is a **name at an exact commit**: `rehearse routes/el/centos7-to-alma10.toml el/centos7-lamp` at
`06e479b…`. What each name runs (`sg kvm` or `sudo -n`, which `WEND_WORK`, cargo first, which log goes where) is
written once in `tools/remi.py` on main, so nobody types it, copies it or splits it into pieces that fit a message.
Every job ends in one `verdict.json` that carries what reviewers have needed, untruncated: every command's rc and log
hash, each run directory's `run.json` in full, the run's files and their hashes, and, for a rehearse, the plan's
`checks.toml` (generated checks included), the walk's hops, every failed step, and `rehearse.log`'s key lines and
tail. Nobody summarizes a run from a grep.

## Setting it up on remi (once)

```
git -C /data/dev/src/wend worktree add --detach /data/dev/src/wend-runner origin/main
```

The runner always runs from `/data/dev/src/wend-runner` (`python3 -P /data/dev/src/wend-runner/tools/remi.py …`),
never from a job's worktree. It refuses to run if that checkout has local changes or sits on a commit no origin branch
holds. That way main's copy decides what a job runs, and the commit under test doesn't. After a change to
`tools/remi.py` merges, `remi.py update` moves the runner to the new origin/main. It refuses while a job runs.

## How a thread asks for a job

A thread sends the remi session one message. Project memory has the session's id.

```
remi job: enqueue rehearse routes/el/centos7-to-alma10.toml el/centos7-lamp --sha <40 hex> --pr 163
          --by "<thread title>" --reply-to <the thread's own session id>
```

Optional extras are `--vm-flags "--uefi --lvm"` (vm.py's flags before the command) and `--after <job id>` (start only
once that job has ended).

The remi session runs exactly that line as `python3 -P /data/dev/src/wend-runner/tools/remi.py enqueue …` and sends
back what it printed: the job id, or the reason for a refusal. Then it runs `remi.py wait <id>` in the background.
When that returns, it sends `remi.py show <id>` to the `--reply-to` session, every part in order (a big verdict comes
in parts of 48000 bytes: drop each part's header line and join the rest, byte for byte, into one JSON), and runs `remi.py delivered <id>`. When it restarts after
being parked, `remi.py queue` lists any verdicts it still owes, and to whom.

The thread reads the verdict, never a summary of it. For anything more, it asks for
`remi.py file <id> <path>`, a log in the job's log directory or a file in one of its run directories, in the same
parts. The verdict's `remi_verdict` holds the `remi-verdict` workflow's inputs, ready to dispatch. Recording the
verdict stays with the thread, under CONTRIBUTING's rules, because remi records nothing on GitHub.

The runner refuses the job, both when it's queued and again when it starts, unless:

- `--sha` is a full commit that is a branch tip on origin or on origin/main (a fork's commit never runs here; push it
  to a branch first), and with `--pr`, that PR's head at that moment. A thread that pushes after queueing gets a
  refusal, not a run on a head nobody froze.
- The job's worktree, `/data/dev/src/wend-<PR or main>-<sha12>` (with `-root` on the end for root's jobs, so a root
  `.wend/` never blocks a dev job), is at that commit with `git status --porcelain` empty. It's created when missing
  and never cleaned, moved or deleted. A dirty one is refused and left as it is.
- The route and box exist at that commit, and the arguments match the job's patterns.

## How ordering holds when several threads ask

The queue on remi (`/data/wend-lab/queue`) is the single source of order, and a worker that `enqueue` starts runs
it. There is only ever one worker (it holds a lock), and it exits when the queue is empty.

- Jobs start in queue order, first come first served. **The coordinator sets the lab order** with
  `remi.py order <id> <id> …`, which puts those queued jobs first, in that order. Threads don't reorder.
- Slots: **one VM job at a time** (build, replay, relocate or rehearse: they all share the one shim on 8085), and two
  `tests` jobs beside it.
  Within a class, nothing jumps a job ranked before it. A job in another class may start while one waits.
- The mirror shim: jobs share it only when their `tools/mirror.py` is byte for byte the same. Otherwise the later
  job waits until the VM jobs using the other one end. A leftover shim from another checkout that nothing uses is
  stopped by its exact pid with `vm.py shim-stop`, as the user that started it (root's with `sudo -n`).
- `remi.py queue` shows what runs, what waits and on what ("a rehearse is running (one at a time)", "behind <id>",
  "after <id>", the shim), and the last verdicts. The coordinator asks for it when it sets the order.
- `remi.py cancel <id>` drops a queued job or stops a running one. vm.py gets SIGTERM and stops its guests, and the
  verdict says `cancelled`.

Nothing VM-bound runs on remi outside the queue. A hand-run replay would hold a slot the queue can't see.

## The jobs

| job | runs | as |
|---|---|---|
| `tests` | every `tools/test_*.py`, then `tools/check.py` | dev |
| `build BOX` | `tools/vm.py build BOX` | dev (`sg kvm`, `WEND_WORK=/data/wend-lab/vmwork`); root for a box with a `[shelter]` plan |
| `replay ROUTE BOX` | `tools/vm.py replay ROUTE BOX` | the same |
| `relocate BOX` | `tools/vm.py relocate BOX` (needs `--vm-flags "--extra-disk GB"`) | the same |
| `rehearse ROUTE BOX` | `cargo build --release --locked` in `client/` as dev, then `tools/vm.py rehearse ROUTE BOX --client client/target/release/wend` | root (`sudo -n`, `WEND_WORK=/data/wend-lab/vmwork-root`) |
| `smoke BOX` | the static build (`--target x86_64-unknown-linux-musl`), then `tools/vm.py smoke BOX --client client/target/x86_64-unknown-linux-musl/release/wend`: the client's `--version`, `selftest`, `facts`, `check`, `share` and a refused `upgrade` inside the box | dev, in the vm slot |
| `upgrade ROUTE BOX` | the same build with `--target x86_64-unknown-linux-musl` (the client runs inside the old box), then `tools/vm.py upgrade ROUTE BOX --client client/target/x86_64-unknown-linux-musl/release/wend` (needs `--vm-flags "--extra-disk GB"`; `--break-after HOP`, `--reset-trial`, `--kill-ssh` and `--rollback-by-hand` go in `--vm-flags` too) | root, as a rehearse, and in the rehearse slot |

A verdict is `pass` only when every command exits 0, every run directory's `run.json` says `pass`, and its `head` is
the job's commit and clean. Otherwise it's `fail`, with every reason listed. A job the runner wouldn't start is
`refused`, one that crashed the runner is `error`, and one stopped by `cancel` is `cancelled`. vm.py's PASS still
isn't the whole review: read the generated checks in the verdict, because a check that can't fail passes too.

Archiving isn't a job. The hourly archive cycle on remi (docs/archive.md, "Keeping it current") owns it.

## Serial command proofs

`client/src/bin/wend-lab-proof.rs` adapts the existing lab queue to a command proof. Build the binary and read
`tools/remi.py` from a clean checkout of the trusted default branch. Pass the runner's path and the clean head
worktree and the stable worker runner as its three arguments. `PROOF_HEAD`, `PROOF_NUMBER` and `PROOF_SUMMARY` are the burrow-proof contract;
the command reads no token and never posts a GitHub verdict itself. For example, with those variables already set:

```
cargo build --release --locked --manifest-path "$trusted/client/Cargo.toml" --bin wend-lab-proof
"$trusted/client/target/release/wend-lab-proof" "$trusted/tools/remi.py" "$head_worktree" "$stable/tools/remi.py"
```

The trusted binary enumerates the atlas at the requested head: every route on every same-family box that starts
at its `from` or joins at an intermediate hop, using the adapter's `joins` mapping. It runs each pair on BIOS and,
when that release's installer has `uefi = true`, on UEFI. Shelter combinations that cross the Python 2 drop
without a shelter build on the route's own start release are excluded, matching vm.py's current precondition
(for example, the centos7 shelter boxes cannot join the centos4/centos5 routes). Those boxes are still proved on
their own starting route. An empty suite or a route with no compatible box refuses
the proof. `wend-lab-proof --plan ATLAS` prints the complete serial suite without starting anything. Other modes
(relocation, alternate layouts, SELinux and client rehearsals) remain additional explicitly requested lab jobs.

Each replay goes through `remi.py enqueue` and the existing slots, shim coordination, origin check and per-replay
eight-hour deadline. `enqueue --claim KEY` atomically adopts or creates the proof identity under the queue lock and returns its ID
as JSON. Adoption also wakes the worker. `queue --json --wake` keeps a polled queue making progress after worker
failure or restart. The adapter stays in the foreground until the exact returned entry ends, including immediate
refused, error and cancelled verdicts. Its stable claim field names
the PR, head, route, box and flags, so a replacement adapter adopts the same queued/running/completed proof rather
than enqueueing it again. SIGTERM asks the queue to cancel its active entry and waits for it to end. A cancelled or
refused queue entry is not adopted by the next invocation. The machine's `proofTimeoutMinutes` for this repo must
cover the serial suite (eight hours per entry at the queue's worst case), plus time waiting for other lab work;
that limit does not extend the steward's repair and judge jobs.

A pass requires all command exits to be zero, every run to say `pass`, its head to equal `PROOF_HEAD` with
`dirty = false`, its box and firmware to match, and all expected hops (and a requested final prune) to be present
and successful. Resumed and partial runs never pass. A failure stops the suite. The summary names every completed
replay and its verdict file, command logs and run directories; full verdict JSON also stays in the command log.
Production workers run in their own transient user units, with the existing queue lock preventing duplicates.
They use `WEND_LAB_WORKER_RUNNER` (the stable third argument), which must remain available after the transient
proof-tools checkout goes away. The worker protocol is unchanged, so the existing stable runner can own jobs;
only the trusted claim API needs the new command. Systemd restarts a failed worker, and explicit queue wakes
recover a missing one. This keeps a proof ending from killing unrelated lab guests in the queue.

Exit 0 is a pass, 1 a failed proof, and 126 an interrupted adapter. The steward posts the summary and exact-head
status through burrow-proof, and a red proof gets the same repair job as any red CI check.

## Disk

VM runs fill remi's disk fast (about 200 GB a day in October 2026): every run directory keeps its hop disks, and every
change to a box, `adapters/el.toml` or vm.py's install code makes a new installed box beside the old ones. A daily
timer, `archive/remi/wend-prune.timer`, runs `tools/lab_prune.py --apply` as root from a root-owned clone of main,
`/var/lib/wend/prune-main`, so main's copy decides and nothing dev can write runs as root. It strips run directories
nothing has written to in 3 days down to their run.json, logs and plan (remi.py's verdict already holds every
run.json), and deletes installed boxes no current head can reuse, by recomputing vm.py's
box key at every branch tip on origin and queued job. Anything held open, a queued or running job's worktree, a box
being installed or one a kept run's disk backs onto stays. `python3 -P tools/lab_prune.py` alone says what it would
delete. Worktrees, logs, the archive, salvage and the mirror caches are never its business.

## Adding a job

Add a job by adding an entry to `JOBS` in `tools/remi.py` (its slot class, argument patterns and time limit), the
code that runs it, and a case in `tools/test_remi.py`. It's a `tools/` change, so it gets its own remi proof: once it
merges, a job runs with the new runner.
