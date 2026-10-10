You are writing a fix for wend, a shared library of verified upgrade fixes. A {family} server is being upgraded one
release at a time inside a chroot sandbox (no kernel; daemons never start on their own). The hop from
{release_now} to **{hop}** just failed and the box was rolled back to {release_now}. Write the missing fix or fixes
for this hop.

## How a hop runs

1. set the release: `{set_release}`
2. `{refresh}`
3. the `pre` steps of every fix that applies
4. the upgrade: {upgrade}
5. the `post` steps of every fix that applies
6. every health check; if a check that passed before now fails, the hop is rolled back

A fix applies when its `when` precondition holds on the box *before* the hop. Fixes already applied on this hop:

```json
{matched}
```

## What a fix may say

Precondition (`when`): one JSON object with exactly one key:
- `{{"installed": "pkg"}}`: the package is installed right now
- `{{"file": "/etc/..."}}`: the file exists
- `{{"file-contains": {{"path": "/etc/...", "text": "literal"}}}}`
- `{{"not": {{...}}}}`, `{{"all": [...]}}`, `{{"any": [...]}}`

Steps (`pre` and `post`), each one object with `op` and exactly these keys:
{ops}

Rules. Proposals that break them are refused before they run:
- No shell. Only the ops above.
- A fix with an `install` step MUST be gated, positively, on `{{"installed": "<the package it replaces>"}}`. A fix
  gated on a config file once installed a database server on a web box.
- `write-file` writes the whole file, only to: {writable}. Content may not contain `'` or `%`.
- Fix the real problem. Do not make a check pass by faking what it inspects (forging dpkg md5sums, keeping an
  obsolete module loaded to satisfy "a PHP module is loaded"). Reviewers reject that, and the `current` check exists to
  catch it.
- Keep each fix small: one problem per fix. If removing packages leaves dependencies broken, include `repair`.
- The precondition must be TRUE on this box now, and as narrow as you can make it: the fix will be tested on other
  boxes where it must not fire.

## Health checks (shell tests run in the box)

```json
{checks}
```

## What failed

Stage: {stage}

Output tail of the last command that ran:
```
{output}
```
{lost}
{attempts}
## Answer

Reply with ONLY a JSON object, no prose around it:

```json
{{"reason": "one or two sentences: what broke and why this fixes it",
 "fixes": [{{"name": "short-lowercase-slug", "when": {{"installed": "..."}},
            "pre": [], "post": [{{"op": "install", "packages": ["..."]}}],
            "restores": ["names of the checks this keeps green"]}}]}}
```
