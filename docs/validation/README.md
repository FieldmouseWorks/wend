# Validation, 2026-10-10 (dep-lab, Ubuntu 24.04 host, root, kernel 6.18)

## `./wend verify --all` on the seed content

The new harness (`tools/sandbox.py`, `walk.py`) replaying `routes/debian/etch-to-trixie.toml` with no model:

| box | result | reached | fixes fired | minutes |
|---|---|---|---|---|
| debian/etch-web | pass, 10/10 checks, 0 obsolete | trixie 13.7 | 9 | 13 |
| debian/etch-db | pass, 10/10 checks, 0 obsolete | trixie 13.7 | 6 | 14 |

The decoy check passed on the fresh runs: every fix fired on exactly the boxes in its `fires_on`. Those runs are in
`runs/debian/etch-to-trixie/*/20261010-191951.json`. The pm-option is now hop-scoped (deleted after buster) and
nothing broke.

## `./wend discover`: can a model refill a hole?

I deleted `apache24-site-conf` from the library and the route, then ran
`WEND_MODEL=anthropic:claude-opus-5-5 ./wend discover debian etch trixie debian/etch-web`.

- jessie broke (`checks lost: web`), as expected
- Opus 5.5 (medium) diagnosed it in **1 call** (4.0k tokens in, 1.2k out): "Apache 2.4 only loads
  sites-enabled/*.conf". It wrote [apache24-mysite-conf-suffix.toml](apache24-mysite-conf-suffix.toml)
- it passed the schema check and the sandbox on the first try, and the walk went on to trixie, 10/10, 0 obsolete,
  14 min ([run](discover-run-etch-web.json))

Its precondition is *tighter* than the hand-curated one: apache2 installed AND the old file names this site AND no
`.conf` exists yet. This is also exactly what the review layer is for. The model added `AllowOverride All`
and `Options FollowSymLinks`, which the old vhost didn't have. That's a behavior change no check notices (the site
renders either way), and a reviewer should send it back.
