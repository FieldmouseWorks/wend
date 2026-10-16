# Try wend on a real old server, read-only

wend upgrades old Linux servers one release at a time along paths that were proven on real installs: CentOS 5, 7
and 8 to AlmaLinux 10, Debian 4.0 etch to Debian 13 trixie, Ubuntu 14.04 trusty to 26.04 resolute. Every one of those
paths was proven on test boxes we built. What we don't know yet is what *your* machines look like.

`wend share` answers that without changing anything. It reads the machine, says whether wend's route from its release
would get it to the end (CPU, firmware, memory, disk, `/boot`, the disk and network drivers each new kernel has, and
any Python 2 programs of its own), and writes a report you can post with the hostname, users and addresses scrubbed.

## 1. Get it

On any machine with a current TLS stack:

```
curl -LO https://github.com/FieldmouseWorks/wend/releases/latest/download/wend-x86_64-linux.tar.gz
curl -LO https://github.com/FieldmouseWorks/wend/releases/latest/download/SHA256SUMS
sha256sum -c SHA256SUMS
```

CentOS 7 and 8, Debian 7 and later and Ubuntu 14.04 can usually do that themselves. CentOS 5 and Debian etch to
squeeze can't speak GitHub's TLS, and CentOS 6 often can't: download it elsewhere and `scp` it over. It's one static
x86_64 binary (no glibc, no Python, nothing to install) and the atlas it reads, about 2 MB packed.

## 2. Run it

```
tar xzf wend-x86_64-linux.tar.gz && cd wend-x86_64-linux
sudo ./wend share
```

It needs root because it reads every process and every crontab, and a normal user can't see other users'. It's
read-only:

- It reads `/proc`, `/sys`, `/boot`, the release files under `/etc`, `/etc/passwd` and `/etc/hostname` (to know what
  to scrub), crontabs, the first bytes of each mounted disk filesystem (to tell XFS v4 from v5), and the package
  database (`rpm -qf` on EL, `/var/lib/dpkg/info` on Debian and Ubuntu), to tell your own Python 2 programs from the
  distro's.
- It writes one file, `wend-box-<id>.md`, in the current directory.
- It opens no network connection. It installs, upgrades, starts and stops nothing.

`sudo ./wend share centos7-to-alma10` judges it against a route you name instead of the one that starts at its
release. A machine partway along a route takes the rest of it: CentOS 6 the CentOS 5 route from its CentOS 6 hop,
Debian 10 and Ubuntu 18.04 likewise, and the report says those hops were proven on machines that walked up from the
route's start. A machine on a release no route reaches still gets a report, and that's useful too: it says what's
out there.

On CentOS 5 and 6 (kernels 2.6.18 and 2.6.32) the binary is untested, since Rust only promises Linux 3.2 and later.
If it doesn't run, that's worth an issue by itself.

## 3. Read it, then share it

It prints the whole report, saves it, and prints a link to a prefilled GitHub issue. Nothing is sent until you open
that link and press submit.

What's replaced: the hostname (also inside LVM names like `/dev/mapper/centos_web01-root`), every login account
(`UID_MIN` and up, 500 on CentOS 5 and 6), home directories, cron spool names, IPv4 and IPv6 addresses, MACs, emails,
and anything shaped like a password, token or private key.

What stays: the release and kernel, CPU model and features, RAM and disk sizes, mount points and filesystem types,
PCI vendor and device IDs with their drivers, and the paths and unit names of any Python 2 programs it found. If a
path or service name says whose machine this is (`/srv/acmecorp/billing.py`), run it again with
`--redact acmecorp`, as many times as you need.

If the report is too long for a link, the issue opens with the summary and asks you to drag the `.md` file in.

## Got a spare VM or a snapshot?

The next step is a real walk: `wend walk` takes a copy of a machine's root filesystem on a scratch host through
every hop, snapshotting before each one and rolling back when a check fails. It never walks the running system.
That's the contributor path for now, from a checkout of the repo: [client/README.md](../client/README.md) has the
commands, and [AGENTS.md](../AGENTS.md) has what to do when a hop breaks. Never point it at production.
