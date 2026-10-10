# AGENTS.md: how to contribute an upgrade path to wend

This file is for AI agents (and the humans running them). Follow it top to bottom. Every step has one command, and
every command says what to do next. If something here contradicts what a tool prints, the tool is right and this file
has a bug: say so in your PR.

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
4. **Never hand-edit runs.** Runs are written by `./wend`, and CI rewrites them anyway.
5. **Gate installs on what they replace.** A fix with `install` must be gated on
   `{ installed = "<the package it replaces>" }`, not on a config file.
6. **One route per PR.** Keep the diff about one path.

## 0. Requirements

- A throwaway Linux machine or VM where you have root (the sandbox uses chroot and mount). A cloud VM or CI runner is
  ideal. Don't use your laptop.
- Python 3.11+, rsync, plus `debootstrap` for Debian boxes. Nothing else to install.
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
`fixes/`, pinned in the route, and the run is saved to `runs/`.

It ends with `PASS` or `FAIL`. On `FAIL`, read the last run's `final.failed_logs`, then either rerun (a fresh attempt
often finds another way) or stop and open an issue describing the wall you hit. A clear "here's where it breaks" is
useful too.

Expect about 15 minutes for a 9-hop Debian route, plus model time. A strong model needs a handful of calls.

## 5. Review your own fixes before anyone else has to

For each new file under `fixes/`, ask:

- **Is the precondition as narrow as possible?** It will be tested on every other box in the family, and firing
  there fails your PR.
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

This runs the schema check, replays every route your change touches on **every** box in the family (the boxes your
fixes don't claim are the decoys), then the decoy check. It writes `.wend/pr-evidence.md`. Don't open a PR until it
says `VERIFIED`.

## 7. Open the PR

- Branch: `route/<family>-<from>-to-<to>-<box>` or `box/<family>-<name>`
- Title: `route: debian etch -> trixie on etch-mail (3 new fixes)`
- Body: the PR template. Paste `.wend/pr-evidence.md` into it and fill in the model and the self-review answers.
- Commits are signed off (`git commit -s`). That's the DCO, and the human running you is the one signing off.
- Commit the new and changed `fixes/`, `routes/` and `runs/` files. Don't commit `.wend/`.

Then CI replays everything again from scratch. A Claude reviewer reads it for check gaming and loose gates, and a
human merges. If CI fails, read the failing job's run file, fix, and push. Don't open a new PR.
