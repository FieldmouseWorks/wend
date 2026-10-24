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
  `remi-proof.yml`, `remi-verdict.yml`; Dependabot's own action bumps excepted);
- adds, changes, removes, pins or unpins a fix a chroot can't judge (before or after the change), because it works
  on what a chroot never boots: a `net.*` or `fs.layout` op, or one that names a kernel, bootloader, initramfs,
  storage, init, network or firewall package or unit in any argument (`install`'s packages, `rpm.transit`'s
  reinstall, `service.enable`'s unit: the workflow's `BOOTNET` list);
- or is labeled `needs-remi`, which a maintainer adds to any other PR that needs a real boot.

Statuses belong to a commit, so every push is judged again from scratch.

To record a verdict, check that the run's `run.json` `head` is the PR's current head and not dirty, then run the
`remi-verdict` workflow (Actions, remi-verdict, Run workflow). It needs write access, refuses a commit that isn't the
PR's head, and posts the status as "remi verdict: <summary>":

```
gh workflow run remi-verdict.yml -R FieldmouseWorks/wend -f pr=123 -f sha=<run.json head.commit> -f state=success \
   -f summary="trusty-to-resolute on ubuntu/trusty-web, BIOS, passed" -f link="<the write-up: PR comment or thread>"
```

A Claude session whose `gh` isn't signed in but whose proxy authenticates GitHub API calls sends the same with curl
(the proxy refuses a direct commit status write, but not this):

```
curl -fsS -X POST -H "Content-Type: application/json" \
  https://api.github.com/repos/FieldmouseWorks/wend/actions/workflows/remi-verdict.yml/dispatches \
  -d "$(jq -n --arg pr 123 --arg sha <full sha> --arg s success --arg m "<summary>" --arg l "<link>" \
        '{ref:"main",inputs:{pr:$pr,sha:$sha,state:$s,summary:$m,link:$l}}')"
```

Then check the run in Actions (it fails, posting nothing, on a stale or wrong commit) and the PR's `remi-proof` status.
Someone with a token that can write statuses may also post the status directly (`gh api -X POST
repos/FieldmouseWorks/wend/statuses/<sha> -f context=remi-proof -f state=success -f description=...`). A recorded
verdict stands: remi-proof.yml never overwrites a status a person posted or one whose description starts with
"remi verdict:". Never post one from any other workflow: the classifier treats the rest of github-actions[bot]'s
statuses as its own. To re-judge an open PR that was opened before the workflow existed, run it by hand: Actions,
remi-proof, Run workflow, PR number.

### 5. Review until settled (automatic, minutes to hours)

Codex reviews every PR, and its findings are review threads on the diff. Whoever opened the PR (a Claude session,
usually) fixes each one, or answers it with the reason it's wrong, and resolves it, then asks for another round
(`@codex review`). The loop ends when Codex finds nothing on the PR's exact head. Codex reads the review checklist in
AGENTS.md ("Code Review Rules"), which hunts specifically for:

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

The maintainer's steward on remi turns that into the `review-settled` commit status, which branch protection
requires. It's success on a head only when Codex reviewed that exact commit and found nothing, no review thread is
open (whoever opened it), nobody's review asks for changes, and every other check on the commit is green. Every push
starts over. Dependabot's non-major bumps skip the Codex round: CI is their review.
After four Codex rounds that still find something, the steward stops and asks the maintainer.

A PR from a fork, or from anyone but the maintainer, the maintainer's Claude sessions and Dependabot, gets the same
review, but `review-settled` waits for the maintainer.

### 6. It merges itself

Once every required check is green, `review-settled` included, the steward marks the PR ready and turns on
auto-merge; GitHub merges it and deletes the branch. Nobody, human or bot, can push to `main` directly.

A PR that changes `.github/` (the workflows that are the gate) can loosen its own judge. There's no CODEOWNERS sign-off:
the maintainer's own PRs settle and land like any other, `.github/` included, and the reviewers flag one that loosens
a check (AGENTS.md, "Code Review Rules"). An outside contributor can't change `.github/` and get it merged: their PRs never settle without
the maintainer (layer 5), and the steward lists them for the maintainer.

## What a cloud Claude session can write on GitHub

Claude sessions in the cloud (project threads included) reach GitHub through Anthropic's GitHub proxy. It sends
the maintainer's credentials, so GitHub sees every write as the maintainer's, and it lets only some writes through:

- **Direct.** Pushing a branch; opening, editing and merging PRs; comments and review replies; labels; and
  dispatching a workflow (`POST .../actions/workflows/<file>/dispatches`). From `gh api` or curl, only GitHub's REST
  API works: the proxy refuses GraphQL, so `gh pr` and `gh issue` (including `gh pr merge`) fail; merge with
  `PUT .../pulls/N/merge`. Resolving a review thread exists only in GraphQL, so a session does it with its built-in
  GitHub tools, not `gh` or curl.
- **Through a dispatched workflow.** The proxy refuses `POST /statuses`, so:
  - recording remi's verdict on a PR is `remi-verdict.yml` (layer 4);
  - re-judging a PR's `remi-proof` status is `remi-proof.yml`, input `pr`.
- **The maintainer, by hand.** Tags, deleting branches, and repository settings. The proxy refuses tag pushes and
  branch deletion over git, and refused branch protection writes; tags and branch deletion stay with the maintainer
  as policy too, even though a workflow token could do them.

A new kind of write a session needs goes in a workflow_dispatch workflow with the narrowest `permissions:` that does
it and checks on its inputs, like `remi-verdict.yml`, and gets added to `CHECKS` in `remi-proof.yml` so changing it
needs a remi pass. Its run log names the maintainer as the actor whether a session or the maintainer started it, so
say in the inputs (a link, a summary) where the request came from.

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

File runs from a gate run under two weeks old. The gate keeps a replay's `fetched-N` capture (docs/archive.md) for 14
days, and the archive takes it only once the run lands on main: an older run's capture is gone, and its live-repo pins
with it.

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
