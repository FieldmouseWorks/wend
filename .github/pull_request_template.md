<!-- AGENTS.md step 7. Every section is required; CI and review check them. -->
## What this adds

Route: `routes/<family>/<from>-to-<to>.toml` on box `<family>/<box>`
New fixes: <!-- one line each: name, hop, what it fixes -->
Claims issue: #

## Model

<!-- e.g. anthropic:claude-opus-5-5 (effort medium), 6 calls. Or "none: hand-written". -->

## Evidence

<!-- paste .wend/pr-evidence.md here -->

## Self-review (AGENTS.md step 5)

- [ ] Every precondition is as narrow as I can make it, and `./wend verify` found no decoy hits
- [ ] Every fix fixes the real problem, not the symptom a check inspects
- [ ] One problem per fix; load-bearing steps (like `repair`) kept
- [ ] No edits to tools/, adapters/, SPEC.md, .github/, other boxes' roles, or other people's fixes (except `fires_on`)
- [ ] Commits are signed off (`git commit -s`)
