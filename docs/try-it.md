# Try wend on a real old server, read-only

wend upgrades old Linux servers one release at a time along paths that were proven on real installs: CentOS 5, 7
and 8 to AlmaLinux 10, Debian 4.0 etch to Debian 13 trixie, Ubuntu 14.04 trusty to 26.04 resolute. Every one of those
paths was proven on test boxes we built. What we don't know yet is what *your* machines look like.

wend's first step answers that without changing anything. It reads the machine, says whether wend's route from its
release would get it to the end (CPU, firmware, memory, disk, `/boot`, the disk and network drivers each new kernel
has, and any Python 2 programs of its own), and writes a report you can post with the hostname, users and addresses
scrubbed. That check is all this page asks you to run.

## 1. Get it

One line, on the server, as any user, from a folder you can write to:

```
curl -fsSL https://github.com/FieldmouseWorks/wend/releases/latest/download/try.sh | sh
```

No curl there? Debian and Ubuntu often have wget instead:

```
wget -qO- https://github.com/FieldmouseWorks/wend/releases/latest/download/try.sh | sh
```

That script ([client/try.sh](../client/try.sh), short enough to read first) downloads the latest release and its
`SHA256SUMS`, refuses a download whose checksum doesn't match, and unpacks it into `./wend-x86_64-linux`. It needs no
root, starts nothing (not wend, not sudo), and leaves only that folder behind. It ends by telling you the command in
step 2.

If you're reading this as the `README.md` inside that folder, you have it already: go to step 2.

wend is x86_64 only: one static binary (no glibc, no Python, nothing to install) and the atlas it reads, a few
megabytes packed. On a 32-bit or ARM machine the script names the machine's architecture and stops, because there is
nothing it could run there.

### A server that can't reach GitHub

CentOS 7 and 8, Debian 7 and later and Ubuntu 14.04 can usually run that line themselves. CentOS 5 and Debian etch to
squeeze can't speak GitHub's TLS, and CentOS 6 often can't: there the curl line stops with an SSL error, the wget
line prints nothing at all, and nothing has been downloaded. For those, on a machine that can reach GitHub, download
three files from the [latest release](https://github.com/FieldmouseWorks/wend/releases/latest): `try.sh`,
`wend-x86_64-linux.tar.gz` and `SHA256SUMS`. Copy them into one folder on the server (`scp`), and from that folder
run:

```
sh try.sh wend-x86_64-linux.tar.gz
```

It checks the tarball against the `SHA256SUMS` beside it and unpacks it, as the one line does, without touching the
network.

## 2. Run it

```
cd wend-x86_64-linux && sudo ./wend
```

Run like that, with nothing after it, wend walks you through its first step. It says what it is about to do and
waits for you before it reads anything. Then it makes the check and tells you, in plain words:

- what the machine is: its release, its processor type, memory and firmware
- whether wend has an upgrade path for it, how far along that path this machine could go, and what stands in the
  way if something does
- the one file it wrote, and that nothing else was changed or sent
- how to share that file, if you choose to

and it offers to show you the report there and then.

On a terminal of 80 columns by 24 rows or more it does this full-screen: a frame, the answer set apart, and buttons
you choose with the arrow keys, Tab and Enter, or by clicking, where your terminal program passes the mouse through
(nearly all do). On a smaller terminal or a bare console it asks and tells in plain text instead, and
`sudo ./wend --plain` asks for that anywhere. Both say the same things. Leaving the full-screen run prints where the
report is and the link to share it, on the ordinary screen, to copy from.

It needs root because it reads every process and every crontab, and a normal user can't see other users'. Started
without root, it only tells you to start it with `sudo`. No sudo on the machine? Become root first (`su -`) and run
`./wend` from that folder. It also needs a terminal, since it waits for you: over ssh that means logging in, or
`ssh -t`. (CentOS 5's sudoers has `requiretty`, so `ssh host 'sudo ./wend'` gets "sorry, you must have a tty to run
sudo" before wend even starts.)

It's read-only:

- It reads `/proc`, `/sys`, `/boot`, the release files under `/etc`, `/etc/passwd` and `/etc/hostname` (to know what
  to scrub), crontabs, the first bytes of each mounted disk filesystem (to tell XFS v4 from v5), and the package
  database (`rpm -qf` on EL, `/var/lib/dpkg/info` on Debian and Ubuntu), to tell your own Python 2 programs from the
  distro's.
- It writes one file, `wend-box-<id>.md`, in the current directory.
- It opens no network connection. It installs, upgrades, starts and stops nothing.

`sudo ./wend share` makes the same check without the walk-through: it prints the whole report, saves it, and prints
a link to a prefilled GitHub issue. `sudo ./wend share centos7-to-alma10` judges the machine against a route you name
instead of the one that starts at its release. A machine partway along a route takes the rest of it: CentOS 6 the
CentOS 5 route from its CentOS 6 hop, Debian 10 and Ubuntu 18.04 likewise, and the report says those hops were proven
on machines that walked up from the route's start. A machine on a release no route reaches still gets a report, and
that's useful too: it says what's out there.

Rust only promises Linux 3.2 and later. The binary runs on CentOS 5.11's 2.6.18 and CentOS 6.10's 2.6.32
kernels (tested 2026-10-16: `share`, `preflight` and `facts` on real installs). If it doesn't run on yours, that's
worth an issue by itself.

## 3. Read it, then share it

The report is that one file, `wend-box-<id>.md`. wend sends nothing, ever: sharing it is you opening a link wend
gives you, to an issue form on this repository with the release and the route filled in, and pressing submit. That
takes a GitHub account and a web browser, so the usual way is to copy the file to your own computer, open the link
there, and drag the file into the form. (`wend share`'s link carries the whole report in it. If the report is too
long for a link, the issue opens with the summary and asks you to drag the `.md` file in.)

Read it first.

What's replaced: the hostname and the domain it is in (also inside a volume group an installer named after a
hostname, like `/dev/mapper/centos_web01-root` or `vg_web01`, even one the machine no longer has), every login
account in `/etc/passwd` (`UID_MIN` and up, 500 on CentOS 5 and 6), directories under `/home`, cron spool names and
the names of files in `/etc/cron.d`, IPv4 and IPv6 addresses, MACs, emails, and anything shaped like a password,
token or private key.

What stays: the release and kernel, CPU model and features, RAM and disk sizes, mount points and filesystem types,
PCI vendor and device IDs with their drivers, and, for the programs of your own it found (Python 2 programs, and
programs and libraries no package owns, under places like `/opt`, `/srv` and `/usr/local`), their paths, module
names and unit names.

Those last are where a name that says whose machine this is can be, and wend can't tell which ones do: a company or
a customer in a folder name (`/srv/acmecorp/billing.py`), an account that isn't in `/etc/passwd` (LDAP, NIS) or sits
below `UID_MIN`, a name that is part of a longer word. So read the report before you share it. To take a word out,
make the report again with that word replaced too: `sudo ./wend --redact acmecorp` walks you through it again, and
`sudo ./wend share --redact acmecorp` does it without the walk-through. `--redact` can be given as many times as you
need. Each run writes a new file, so delete the earlier one.

## Got a spare VM or a snapshot?

The check above is the whole of what this page asks for. The step after it is a real walk: `wend walk` takes a copy
of a machine's root filesystem on a scratch host through every hop, snapshotting before each one and rolling back
when a check fails. `wend walk` never walks the running system. That's the contributor path for now, from a checkout
of the repo: [client/README.md](../client/README.md) has the commands, and [AGENTS.md](../AGENTS.md) has what to do
when a hop breaks. Never point it at production.
