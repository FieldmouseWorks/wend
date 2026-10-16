# Contributing to wend

You contribute **fixes** and **routes** (SPEC.md). You never contribute **runs**. CI writes them, and a run is the
only thing that makes a fix trusted.

Agents: [AGENTS.md](AGENTS.md) is the step-by-step version of this file, with one command per step.

## The short version

1. Find a hop that breaks: `./wend discover` with your model of choice, or just notice it on a real box.
2. Write the fix as `fixes/<family>/<hop>/<name>.toml`. Use only the op vocabulary. If you can't express the fix
   in it, open an issue proposing a new op instead of working around it.
3. Gate it tightly. An `install` must be gated on `installed` of the package it replaces. Set `meta.fires_on` to the
   boxes where you expect it to fire.
4. Pin it in a route (`routes/...`). If you edited an existing fix, its id changed: run
   `python3 tools/check.py --repin`.
5. `./wend verify --base origin/main` (exactly what CI runs), then open a PR with its evidence block.
6. If you have a box where the problem showed up and CI has no box like it, add the box too. That's the most
   valuable contribution there is.

## The gate

A PR merges only after all six layers pass, in this order.

### 1. Schema check (automatic, seconds)

`tools/check.py`, always the copy **from the base branch**, so a PR can't loosen its own rules. It refuses:

- any op outside the core vocabulary or the family's adapter, any extra argument, and any shell
- `write-file` outside the adapter's `writable` allowlist, or to a harness-owned path
- an `install` not gated on `installed` of something
- a route that pins an id no fix has, or a fix outside the route's hops
- a duplicate fix (same content id as an existing one)

`tools/test_check.py` is the gate's own regression test: 14 broken fixes and one decoy hit that it must refuse.

### 2. Replay in CI (automatic, ~15 min per route per box)

`tools/affected.py` works out what the PR touches. CI then replays each affected route, from scratch, on **every** box
of its family that starts at the route's `from` release, in a chroot on a stock `ubuntu-24.04` runner. No model is
involved. Each hop is rolled back if any health check fails, and a route passes only if every hop is kept and the final
state is fully green, including:

- `pkgdb`: the package database is consistent
- `current`: running services come from packages the release still ships, so nothing passes with 2014's PHP still loaded
- `services`: no new kind of service appeared that the box didn't start with and no fix declared in `adds`

What gets replayed:

| PR changes | replayed |
|---|---|
| a fix | every route that pins its old or new id, on every box of the family that starts where the route starts |
| a route | that route, on every box of the family that starts where it starts |
| a box | every route of the family that starts at the box's release, on that box |
| an adapter | every route of the family, on every box that starts where the route starts |

A box that starts at another release can't walk the route (a CentOS 7 box can't take the CentOS 5 route's hop to
CentOS 6), so it isn't replayed there and isn't a decoy for that route's fixes.

### 3. Decoy boxes (automatic)

Every box a route is replayed on is a decoy for every fix that doesn't list it in `fires_on`. On each fresh run, a fix
that fires on an unclaimed box fails the PR. So does a fix that claims a box and then doesn't fire there. This catches
the loose precondition: in dep-lab, a fix gated on `/etc/mysql/my.cnf` installed a database server on a web box, and
every health check passed. If your fix is honest and the decoy is still wrong, the fix is too broad. Narrow the `when`;
don't widen `fires_on`.

### 4. Proof on remi

The gate judges a PR with the base branch's tools and adapters, so a PR that changes them is never exercised by CI.
remi, the maintainer's lab machine, is where they get tested: VM replays on real installs with real reboots
(`tools/vm.py`, SPEC.md "VM tier"), or whatever else the change needs. remi only runs what a maintainer starts. It is
never a CI runner, and nothing a PR does can reach it.

The `remi-proof` commit status carries the verdict to GitHub (`.github/workflows/remi-proof.yml`). Most fix and route
PRs pass at once: the gate's replays are their proof. A PR stays pending until someone with push access records remi's
verdict on its exact head commit when it:

- touches `tools/`, `adapters/`, `boxes/` or `wend`, or the workflows behind the required checks (`gate.yml`,
  `remi-proof.yml`; Dependabot's own action bumps excepted);
- adds, changes, removes, pins or unpins a fix a chroot can't judge (before or after the change), because it works
  on what a chroot never boots: a `net.*` or `fs.layout` op, or one that names a kernel, bootloader, initramfs,
  storage, init, network or firewall package or unit in any argument (`install`'s packages, `rpm.transit`'s
  reinstall, `service.enable`'s unit: the workflow's `BOOTNET` list);
- or is labeled `needs-remi`, which a maintainer adds to any other PR that needs a real boot.

Statuses belong to a commit, so every push is judged again from scratch.

To record a verdict, check that the run's `run.json` `head` is the PR's current head and not dirty, then:

```
pr=123
sha=$(gh pr view $pr -R FieldmouseWorks/wend --json headRefOid -q .headRefOid)   # must equal run.json head.commit
state=success    # or failure
desc="remi: trusty-to-resolute on ubuntu/trusty-web, BIOS, passed"            # 140 characters at most
gh api -X POST repos/FieldmouseWorks/wend/statuses/$sha -f context=remi-proof -f state=$state \
   -f description="$desc" -f target_url="<link to the write-up: the PR comment or thread>"
```

A Claude session whose `gh` isn't signed in but whose proxy authenticates GitHub API calls sends the same with curl:

```
curl -fsS -X POST -H "Content-Type: application/json" https://api.github.com/repos/FieldmouseWorks/wend/statuses/$sha \
  -d "$(jq -n --arg s "$state" --arg d "$desc" --arg u "<link>" '{context:"remi-proof",state:$s,description:$d,target_url:$u}')"
```

A verdict recorded this way stands: the workflow never overwrites a status a person or a session set. Record it with
a person's own credentials, never from an Actions job's `GITHUB_TOKEN`: the workflow tells its own statuses apart by
that token's account. To re-judge an
open PR that was opened before the workflow existed, run it by hand: Actions, remi-proof, Run workflow, PR number.

### 5. Claude review (automatic, minutes)

Every PR is labeled `needs-claude-review`. A standing Claude reviewer reruns the PR's replays and reads the diff,
hunting specifically for:

- **check gaming:** a fix that makes a check pass without fixing the thing. dep-lab's model forged empty dpkg
  md5sums files to pass `dpkg --audit`, and left PHP 5 serving the site on trixie. Ask: would an admin call this fixed?
- **gates too loose for their effect:** an `installed` gate on a package that's always present, or a `file` gate on a
  file another package also ships
- **drift beyond the fix:** a rewritten config that also changes behavior nobody asked for. In the first discovery
  test, Opus added `AllowOverride All` to a vhost that never had it. The site renders either way, so no check notices.
- **load-bearing cleanup:** a dropped `repair` that looks like noise but wasn't (it broke dep-lab's first stitch)
- **executable content in written files:** piped log targets in Apache config (`CustomLog "|..."`), `!include` of
  writable paths
- **provenance claims** that the runs don't back up

The verdict is one PR comment that explains it. Today the `claude-review` job only applies the label; nothing in
CI waits for the verdict, so branch protection can't enforce it yet. Until a `claude-review` commit status exists and
is required, whoever merges checks for the review comment first. The review is one layer among several, not a
shortcut past them.

### 6. The maintainer or Claude merges

Only after every required check is green **and** the Claude review has signed off on the PR. The maintainer
(CODEOWNERS) can merge, and so can Claude, under a standing permission from the maintainer. Either way the bar is
the same: a red check, an unresolved review finding, or no review at all means no merge. Nobody, human or bot, can
push to `main` directly.

## How runs land

A run's `sources` pin the exact bytes a route installs, and the client trusts them (`wend walk --run`). Nobody can
review a sha256 by eye, so `runs/` takes only what CI wrote. Each gate replay attests its `run.json` (GitHub artifact
attestations, Sigstore). Once a PR's replays pass, whoever drives it downloads that gate run's artifacts and files
them unchanged, in a commit on the same PR:

```
gh run download <gate run id> -R FieldmouseWorks/wend -D new-runs
python3 tools/file_runs.py new-runs && rm -rf new-runs
```

The schema check runs `gh attestation verify` on every run file the PR adds or changes and refuses any that the gate
didn't attest. A run file that only changes runs/ needs no new replay. Runs filed before this rule (their `runner` is
`local`) are left as they are; new CI runs of the same routes replace them as evidence.

## What a good fix looks like

- **Small.** One problem per fix. If you remove packages and then repair, keep both. The repair can be load-bearing.
- **Honest.** It fixes the thing the check is a proxy for.
- **Narrow.** It fires where the problem is and nowhere else, and the decoys agree.
- **Generic when it can be.** The OS-level fixes (libdb, the sysvinit clash, dropping sysvinit) transferred unchanged
  to a second box. The app-level ones didn't need to. A fix that names one site's files is a local fix. Say so in
  `meta`, and expect review to ask whether it belongs in the shared library (SPEC open question 7).

## Authoring with a model

`./wend discover` lets any model propose fixes for a hop that fails: Anthropic, any OpenAI-compatible server
(including local ones), or the calling agent itself through `WEND_MODEL=agent`. Proposals go through the same schema
check as a PR before they run, and they're verified in the sandbox like everything else. The question the model gets
is `prompts/discover.md`, in the repo where anyone can read and improve it. The project
convention is a heavier model for authoring new fixes and a cheap one for per-decision work. Authoring cost is paid
once and amortized over free replays. A model-authored fix goes through exactly the same gate as a human's, with
`meta.author` saying which model.

## License and sign-off

Contributions are dual-licensed MIT OR Apache-2.0, the same as the repo. Every commit needs a
[Developer Certificate of Origin](https://developercertificate.org/) sign-off (`git commit -s`), which adds
`Signed-off-by: Name <email>`. CI rejects PRs with unsigned commits. A model-authored fix is signed off by the
human who submits it, and that human vouches for it like any other contribution.
