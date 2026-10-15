# The wend archive: design

Status: design, 2026-10-15. Not normative yet. Each piece moves into SPEC.md in the PR that builds it, as AGENTS.md
asks (spec before ops). Issue: wend#19. Background: SPEC open question 12 ("Archive", "Fallback chain", "Moved
mirrors").

## Why it can't wait

A run pins each hop's top metadata file by sha256 (`hops[].sources`), and `wend walk --run` refuses a walk if any
pinned file is gone from every source. On 2026-10-15 the 82 run files on main pin 31 URLs (54 distinct digests). Fetched
that day:

| Where | Pinned URLs | Still served |
|---|---|---|
| archive.debian.org, deb.debian.org, archive.ubuntu.com | 15 | all 15 (wheezy's plain index through its `.gz`) |
| vault.centos.org, archives.fedoraproject.org (p7zip) | 7 | all 7 |
| repo.almalinux.org (8, 9, 10) | 6 | **none** |
| dl.fedoraproject.org (EPEL 8, 9, 10) | 3 | **none** |

Every pin of a live EL repo has already rotted, so no route past CentOS 7's vault replays pinned today. That needs no
mirror outage: these repos republish metadata every few days and keep no old copies. Fetching a pin later doesn't
work, because by then the bytes are gone. **The archive's first job is to keep metadata at the moment a run reads it.**
Packages last longer (AlmaLinux moves an old minor's packages to vault.almalinux.org, and the bytes stay the same), so
they can be fetched afterwards by digest. Vaults rot too, more slowly: archive.debian.org now 404s 10 of etch's
251 installer udebs.

## What it is

A content-addressed blob store plus a signed index. Nothing fancier.

- **Blobs.** Each file is stored once, under `blob/sha256/<first 2 hex>/<64 hex>`, byte-identical to what upstream
  served. An RPM, deb, udeb, `repomd.xml`, `primary.xml.gz`, `InRelease` or `Packages.xz` is just a blob. Dedup across
  routes comes free, because routes share most packages and the digest is already the key. No chunking: packages are
  already compressed, so chunk dedup would save little, and it would add a reassembler (one more place to get integrity
  wrong) and lose the property that any blob checks with stock `rpm -K`, `gpgv` or `sha256sum`.
- **Index.** `index/<stamp>.json`, plus `index/latest.json` naming the newest one. It maps each origin URL to every
  digest the archive holds for it (one URL, many snapshots: a `repomd.xml` changes, its pins don't), with size, when it
  was first archived, and the runs that reference it. It is signed with `ssh-keygen -Y sign` (ed25519, namespace
  `wend-archive`), and its public key is committed as `archive/allowed_signers`. ssh-keygen is everywhere, which isn't
  true of signify or minisign, and it needs no gpg keyring.
- **What the signature is for.** Clients don't need it for integrity: a run file on main names every digest, git
  carries that, and the client checks every byte against it whatever the source. The signature says which URLs the
  archive vouches it saw upstream serve, and when. That matters for mirrors of the archive and for anyone auditing it.
  It also matters for the source packages below.

## What goes in

Only files that proven routes and boxes reference. Never a mirror of a whole distro.

1. **Every pinned top file**, at the digest the run pins.
2. **Every index under it that a walk read** (`primary.xml.gz`, `modules.yaml`, `Packages.xz`...), at the digest its
   top file lists. These are what make an old `repomd.xml` useful: without its `primary` it vouches for nothing.
3. **Every package a passing run downloaded**, at the digest its index lists.
4. **What a box build downloaded**: debootstrap's and the installer's files (etch's udebs), and the start release's
   packages, so a box can still be built once its vault rots.
5. **The matching source packages.** The archive will be public, and redistributing GPL binaries means offering their
   source. For each binary, the archive keeps the source it names: the `.src.rpm` (vault.centos.org `Source/`,
   AlmaLinux `Source/`, EPEL `source/tree/`) or the `.dsc` and the files it lists. They are found through the same
   repos' source metadata and checked against it. When upstream no longer has a source (a rotted vault), the archive
   records that in the index rather than inventing one, and the gap is a reason to keep the archive private for that
   release until snapshot.debian.org or another source fills it.

"Referenced" means: by a run file on main whose `result` is `pass`, or by a box file on main. The archive never
evicts a blob that such a file references. No eviction at all in the first version: storage is cheap and the set is
small. A blob a later run no longer references can be swept once there are numbers saying it's worth the code.

## Who writes it, who reads it

```
  CI gate replay ──(artifact: download list + the metadata bytes it read)──┐
  remi VM runs ───(same files, from its own runs)──────────────────────────┤
                                                                           v
                               remi /data/wend-archive  (the only writer: fetch, verify, store)
                                                                           │ push (S3 API, Object R/W token)
                                                                           v
                               Cloudflare R2 bucket wend-archive  (public read, once signing is in)
                                                                           │
                               client: cache -> --source ... -> archive -> origin -> mirrors
```

- **remi is the only writer.** It never listens: nothing on the internet talks to it. It pulls work in (CI artifacts,
  its own runs), fetches what's missing from upstream, checks each file against the chain (pinned top file → index
  → package), stores the blob, then pushes blob and index to R2. It runs at idle priority, beside VM runs, and never
  touches remi's network, kernel or bootloader. The R2 credentials are a root-only file; nothing logs or prints them.
- **CI captures, remi archives.** A gate replay runs on a throwaway runner, which is the only place the metadata it
  pinned exists at that moment. So the harness writes a download list (every file the hop fetched: origin URL, sha256,
  size; for rpm the mirror shim logs what it served, for apt the box's `/var/lib/apt/lists` and `/var/cache/apt/archives`
  before they're cleaned), and the replay job uploads it as an artifact together with the metadata bytes it read
  (tens of MB; packages stay out, since they don't rot that fast). remi downloads the artifacts of every gate run whose
  run files land on main, well inside GitHub's 90-day retention.
- **The mirror shim stops forgetting.** Today it overwrites a live repo's `repomd.xml` in place. It keeps every
  version under its digest instead, which is what lets remi's own runs feed the archive the same way.
- **R2 is the public copy.** No egress fees, so CI and users can read as much as they like. If remi dies, the archive
  stays up. remi's `/data` copy is the second copy, and the one that can rebuild R2.
- **Reading.** The client already fetches each file by its expected digest from an ordered list of sources and keeps
  the first bytes that hash right (`wend walk --run`, `--source`). The archive is one more source, of a new kind: it
  is addressed by digest, not by `host/path`, so the client asks for `blob/sha256/<hex>` directly. The default chain
  becomes local cache → `--source`s (an org cache) → the wend archive → origin → mirrors and vaults. A pinned file gone
  from all of them still refuses, as decided on 2026-10-11. `--live` stays the explicit escape.

## Size

Measured on remi, 2026-10-15. Its mirror shim cache holds everything every EL run there has fetched, old package
versions and all: 7.9 GB in 4614 files (repo.almalinux.org 4.4 GB, vault.centos.org 3.3 GB, dl.fedoraproject.org
148 MB, archives.fedoraproject.org 25 MB). That's an upper bound for the EL routes' binaries, since the archive keeps
only what a passing run referenced. apt fetches straight from the mirrors, not through the shim, so Debian and Ubuntu
have no such number yet (remi's Debian installer cache is 99 MB). A cold `wend walk` of centos5-to-alma10 filled
1.5 GB. Source packages tend to be as big as the binaries again. Call it 20-40 GB to start. remi has 1.9 TB free on
`/data`, and R2 charges about $0.015 per GB-month past a free 10 GB, so storage is not the constraint for a long time.

Salvage: of the 27 EL8+ digests runs pin, remi's caches still hold 4 (Alma 9 and 10 BaseOS and AppStream, all from
one centos5-web run, whose Alma 8 and EPEL pins are gone). No run already on main can be made to replay pinned
again. Fresh CI replays, once capture is in, replace them. That's cheaper than digging older bytes out of VM disks.

## Order of work

Each one a small PR.

1. This document.
2. **Capture, EL** (with this document). The shim keeps every file it fetched by digest and logs what it served;
   `wend replay --out` writes the download list and the metadata bytes beside the run; the gate uploads them. This
   stops the rot for every EL run from then on. Runs already on main whose EL pins are gone get replaced by fresh CI
   replays. remi's shim is a long-running process, so it has to be restarted once this lands.
   **Capture, apt.** The same from the box's own `/var/lib/apt/lists` and `/var/cache/apt/archives`, since apt doesn't
   go through the shim. Less urgent: of apt's pins only the current Debian releases' change (at point releases).
3. **The store.** `tools/archive.py` on remi: ingest a download list (or a run file), fetch what's missing, verify
   the chain, store blobs, write the index. Measures as it goes.
4. **Sources.** Source packages for everything ingested.
5. **R2 push.** Upload new blobs and the index (S3 API, standard library only), checking each upload's digest.
6. **Client.** The archive as a by-digest source, in the default chain.
7. **Signing, then public.** The index signature and `archive/allowed_signers`; then the bucket gets public read.
   It stays private until this step.

## Refused

- Mirroring whole distros or whole repos, "just in case". That's how this turns into the thing it's meant to save us
  from.
- Chunk-level dedup, delta packages, or any format that isn't upstream's bytes.
- Making remi reachable from outside, or letting CI write to the archive directly. CI has no credentials, and PR code
  runs there.
- Trusting the archive. It is one more source, checked like the others.
