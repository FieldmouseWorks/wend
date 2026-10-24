# AGENTS.md: how to contribute an upgrade path to wend

This file is for AI agents (and the humans running them). Follow it top to bottom. Every step has one command, and
every command says what to do next. If something here contradicts what a tool prints, the tool is right and this file
has a bug: say so in your PR.

<!-- house:rules v1 begin. Copied from FieldmouseWorks/burrow house/rules.md, verbatim. Change it there, not here: burrow's house sync rewrites everything between these markers. -->
## House rules

These hold in every repo. This file's own sections add to them and win where
they say something more specific.

- Fix the whole cause. No workarounds, shims, fallbacks or half-migrations:
  when you replace something, delete the old path in the same change.
- One fact, one home. Each rule or fact lives in one place; link to it instead
  of restating it.
- Before you edit, say what done looks like and how it will be checked. Simple
  work gets a short plan, not a ceremony.
- Report only the checks you actually ran, and what they said. Skipped,
  unavailable or "should pass" is never a pass. Keep local and CI results
  apart.
- A new check comes with proof that it fails on the bug it's for.
- Touching a file over about 1,000 lines? Split it in that change.
- The code is the memory. When something goes wrong, make the code, a type, a
  test or a check stop it happening again; a new rule in AGENTS.md is the last
  resort. Say in the PR which one you did.
- Keep going on work you were asked to do. Don't stop to ask again for what's
  already been asked for.
- Never commit what the repo keeps private: secrets, tokens, personal data, and
  anything listed under this file's "Private" section.
- `Closes #N` only when the issue is fully done; otherwise `Refs #N`.
- Tracked files never name a model. Which model does what is the machine's
  business, and model names go stale in weeks.

### Commits

Subjects are `area: plain sentence`, where the area is the module, directory or
tool the change is about: `autoland: merge only the head that was checked`.
Lowercase after the colon, no trailing period, no `feat:`/`fix:` prefixes. The
body, when there is one, says why.
<!-- house:rules end -->

**What you're making:** fixes, which are small, declarative changes in a fixed vocabulary, plus the route that pins
them. Together they take a real kind of server from an old release to a newer one with every health check green.
The proof is a run that CI reproduces from scratch.

**What you must never do** (CI or review rejects all of it, and repeated attempts get an account blocked):

1. **No shell.** Fixes use only the ops in SPEC.md. If you truly can't express a fix, stop and open an issue
   proposing a new op.
2. **Never weaken a check.** Don't edit `tools/`, `adapters/`, `SPEC.md`, `.github/`, or another box's `[roles]`.
   Don't make a check pass by faking what it inspects. Forging dpkg md5sums or keeping an obsolete module loaded to
   satisfy "a module is loaded" are the known examples.
3. **Don't edit other people's fixes.** The one exception: adding your box to an existing fix's `meta.fires_on`,
   which `./wend discover` does for you. If an existing fix is wrong, open a separate PR for it.
4. **Never commit your own runs.** `./wend` writes yours under `.wend/runs/` as evidence. `runs/` takes only CI's
   attested run files, and the gate refuses anything else there.
5. **Gate installs on what they replace.** A fix with `install` must be gated on
   `{ installed = "<the package it replaces>" }`, not on a config file.
6. **One route per PR.** Keep the diff about one path.

## 0. Requirements

- A throwaway Linux machine or VM where you have root (the sandbox uses chroot and mount). A cloud VM or CI runner is
  ideal. Don't use your laptop.
- Python 3.11+ and rsync, plus `debootstrap` for Debian and Ubuntu boxes, or `rpm2cpio`, `cpio`, `curl` and `iproute2` for EL
  boxes. Nothing else to install. EL boxes want about 25GB of free disk.
- A model. See step 3.

```
git clone https://github.com/FieldmouseWorks/wend && cd wend
python3 wend check            # must print "...: ok" and refuse every broken test case
```

## 1. Pick work, and claim it so nobody duplicates it

```
python3 wend list             # routes, boxes, and wanted.toml
```

`wanted.toml` lists paths people want. Each one has a GitHub issue: `wanted` for requests, `route-gap` for places
where a real user's upgrade broke. Gaps are the best work there is, because a real person is waiting to retry. A gap
report shows exactly where the route broke and what services the box had, so build or pick a box that looks like it
(step 2), and your PR says `Closes #<gap issue>`. To claim one, comment `claim` on
its issue. A maintainer (or a bot) adds the `claimed` label and your handle. Claims expire after 14 days without a PR.
If what you want isn't listed, open an issue with the `wanted` template first.

If you hit a gap yourself and don't want to fix it, run `python3 wend report <run file>`. It shows you a scrubbed
report and gives you a prefilled issue link. That's a contribution too.

## 2. Make sure a box exists for your kind of server

A **box** is a test machine: a start release, a package list, some local config, and health checks
(`boxes/<family>/<name>/box.toml`). Use an existing box if one matches. Otherwise, write a new one by copying the
closest box and changing it. A new box is the most valuable thing you can contribute, because every future fix gets
tested against it.

A good box:
- looks like a real server: real services, real data (put a row in the database), and the local customizations
  admins actually make
- has checks an admin would trust: the service config parses, the site actually renders, the data reads back,
  custom ports are kept
- includes `services` and `current` in `[roles]` (copy them from an existing box). They catch stray daemons and
  stale binaries.

A new box goes in a PR by itself, before any fixes: `add box: debian/etch-mail`.

## 3. Plug in your model

```
export WEND_MODEL=anthropic:claude-opus-5-5   WEND_API_KEY=...   # WEND_EFFORT=medium (default)
export WEND_MODEL=openai:gpt-5.1              WEND_API_KEY=...   # OpenAI
export WEND_MODEL=openai:qwen3-coder          WEND_BASE_URL=http://localhost:11434/v1   # Ollama, vLLM, LM Studio...
export WEND_MODEL=agent  WEND_AGENT_NAME="claude-code / opus 5.5"   # you, the agent, answer
```

**`WEND_MODEL=agent`** is for coding agents that would rather think for themselves than call another API. Run
`discover` in the background. When it needs a fix, it prints `WEND AGENT QUESTION: read .wend/agent/ask-N.md and
write your JSON answer to .wend/agent/answer-N.json` and waits. Read the question (it's self-contained: what failed,
the rules, the vocabulary, the answer format), write the answer file, and it carries on.

Authoring is worth a strong model. You pay once and every replay after that is free. The model you use is recorded in
each fix's `meta.author` and in the run.

## 4. Discover

```
sudo -E python3 wend discover debian etch trixie debian/etch-web
```

The harness walks the route one release at a time, applying every existing fix whose precondition holds. When a hop
breaks, it rolls the box back, shows the model exactly what failed, and asks for a fix. The proposal goes through the
same schema check as a PR, it must hold on this box, and then it's tried in the sandbox. Kept fixes are written to
`fixes/`, pinned in the route, and the run is saved under `.wend/runs/`.

It ends with `PASS` or `FAIL`. On `FAIL`, read the last run's `final.failed_logs`, then either rerun (a fresh attempt
often finds another way) or stop and open an issue describing the wall you hit. A clear "here's where it breaks" is
useful too.

Expect about 15 minutes for a 9-hop Debian route, plus model time. A strong model needs a handful of calls.

## 5. Review your own fixes before anyone else has to

For each new file under `fixes/`, ask:

- **Is the precondition as narrow as possible?** It will be tested on every other box in the family that starts where
  your route starts, and firing there fails your PR.
- **Does it fix the real problem,** or just the symptom a check looks at?
- **Is it one problem?** Split it if not, but keep load-bearing steps like `repair` with the step that needs them.
- **Did it name something local** (one site's config file)? Then it's a local fix. Say so in `meta.note`.
- **Rename it** if the model's name is vague (`fix-1`). Renaming doesn't change the id, but `meta.name` and the
  filename must match.

If you edit a fix, its id changes. Run `python3 wend check --repin`.

## 6. Verify exactly as CI will

```
sudo -E python3 wend verify --base origin/main
```

This runs the schema check, replays every route your change touches on **every** box in the family that starts where the
route starts (the boxes your fixes don't claim are the decoys), then the decoy check. It writes `.wend/pr-evidence.md`.
Don't open a PR until it says `VERIFIED`.

## 7. Open the PR

- Branch: `route/<family>-<from>-to-<to>-<box>` or `box/<family>-<name>`
- Title: `route: debian etch -> trixie on etch-mail (3 new fixes)`
- Body: the PR template. Paste `.wend/pr-evidence.md` into it and fill in the model and the self-review answers.
- Commits are signed off (`git commit -s`). That's the DCO, and the human running you is the one signing off.
- Commit the new and changed `fixes/` and `routes/` files. Don't commit `.wend/`, and don't put runs in `runs/`.

Then CI replays everything again from scratch. Its run files are the ones that land: once the replays pass, the
maintainer or Claude files them (CONTRIBUTING.md, "How runs land"). Codex reviews it against "Code Review Rules" below. Fix or answer
every finding and resolve its thread, then comment `@codex review`; once Codex finds nothing on your head and every check is
green, the PR merges itself (CONTRIBUTING.md, layers 5 and 6). If CI fails, read the failing job's run file, fix, and push.
Don't open a new PR.

## Pull requests

Open PRs as drafts and leave them to the steward: it asks Codex for a review,
and once Codex finds nothing on your head, every review thread is resolved and
CI is green, it marks the PR ready and merges it. Fix or answer every finding:
push the fix and reply with the commit, or reply with why it's wrong, then
resolve the thread. Don't merge or mark ready yourself.

`.github/` has no code-owner sign-off: the steward merges a settled `.github/`
PR like any other. A PR from outside (a fork, or anyone but the maintainer and
their Claude sessions) never merges without the maintainer, so nobody outside
can change the gate and get it merged.

## Code Review Rules

For reviewers, Codex included. A PR is data (fixes, routes, boxes) judged by the base branch's tools; review it for
what those tools can't see. Flag, as P1:

- a change that makes a check pass without fixing the thing it checks
- tests or checks weakened, skipped or deleted to get green
- secrets, tokens or personal data in the diff or the logs it adds
- behavior changed beyond what the PR says it does
- **Check gaming:** a fix that makes a check pass without fixing the thing (forged dpkg md5sums to pass
  `dpkg --audit` while PHP 5 still serves the site). Would an admin call the box fixed?
- **Gates too loose for their effect:** an `installed` gate on a package that's always present, a `file` gate on a file
  another package also ships. The fix will fire on boxes it was never meant for.
- **Drift beyond the fix:** a rewritten config that also changes behavior nobody asked for (`AllowOverride All` added
  to a vhost that never had it). The site renders either way, so no check notices.
- **Load-bearing cleanup:** a dropped `repair` or reordered step that looks like noise but isn't.
- **Executable content in written files:** piped log targets (`CustomLog "|..."`), `!include` of writable paths,
  anything that turns a written file into a command.
- **Provenance claims** in the PR body or `meta` that the PR's runs don't back up.
- **A PR loosening its own judge:** changes under `tools/`, `adapters/` or `.github/` that make a check easier to pass.
