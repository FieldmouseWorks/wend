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

A PR merges only after all five layers pass, in this order.

### 1. Schema check (automatic, seconds)

`tools/check.py`, always the copy **from the base branch**, so a PR can't loosen its own rules. It refuses:

- any op outside the core vocabulary or the family's adapter, any extra argument, and any shell
- `write-file` outside the adapter's `writable` allowlist, or to a harness-owned path
- an `install` not gated on `installed` of something
- a route that pins an id no fix has, or a fix outside the route's hops
- a duplicate fix (same content id as an existing one)

`tools/test_check.py` is the gate's own regression test: 14 broken fixes and one decoy hit that it must refuse.

### 2. Replay in CI (automatic, ~15 min per route per box)

`tools/affected.py` works out what the PR touches. CI then replays each affected route, from scratch, on **every**
box of its family, in a chroot on a stock `ubuntu-24.04` runner. No model is involved. Each hop is rolled back if any
health check fails, and a route passes only if every hop is kept and the final state is fully green, including:

- `pkgdb`: the package database is consistent
- `current`: running services come from packages the release still ships, so nothing passes with 2014's PHP still loaded
- `services`: no new kind of service appeared that the box didn't start with and no fix declared in `adds`

What gets replayed:

| PR changes | replayed |
|---|---|
| a fix | every route that pins its old or new id, on every box of the family |
| a route | that route, on every box of the family |
| a box | every route of the family, on that box |
| an adapter | everything in the family |

### 3. Decoy boxes (automatic)

Every box is a decoy for every fix that doesn't list it in `fires_on`. On each fresh run, a fix that fires on an
unclaimed box fails the PR. So does a fix that claims a box and then doesn't fire there. This catches the loose
precondition: in dep-lab, a fix gated on `/etc/mysql/my.cnf` installed a database server on a web box, and every
health check passed. If your fix is honest and the decoy is still wrong, the fix is too broad. Narrow the `when`;
don't widen `fires_on`.

### 4. Claude review (automatic, minutes)

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

The verdict is the `claude-review` commit status plus one PR comment that explains it. Claude can't push to `main`,
approve, or merge, and its status is one required check among several, not a shortcut past them.

### 5. A human merges

Always. CODEOWNERS requires a human approval on every PR, and the owner's approval for `tools/`, `adapters/`,
`SPEC.md` and `.github/`. Nobody, human or bot, can push to `main` directly.

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
