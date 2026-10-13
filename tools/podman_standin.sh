#!/bin/sh
# wend's podman stand-in for the chroot tier (SPEC "Sheltering an app", Proof). CI only: never shipped to a client,
# never left in a box. The harness (tools/shelter.py) puts it at /usr/bin/podman inside a box only while that box's
# health checks run, and puts the real podman (if any) back afterwards.
#
# A chroot has no podman to run, so `podman run ... localhost/wend-shelter-<app>:<digest> <command>` becomes: find
# the shelter root whose shelter.lock names that digest, bind it read-only onto itself, mount the declared volumes on it (:ro read-only,
# everything else read-write), a tmpfs on /tmp and /run, and the box's own /dev and a fresh /proc, then chroot into it
# and run the command as --user. It runs in a mount namespace of its own, so nothing it mounts outlives the command.
# Everything else (the user namespace and its uid map, capabilities, no-new-privileges, the network and its egress
# table, published ports, SELinux labels and idmapped mounts) is ignored: this proves the userland runs the app,
# nothing about confinement, and runs say so (confinement_proved: false).
#
# Only the flags wend's own units use are accepted; anything else is refused, so a unit this doesn't understand
# fails its check instead of passing on a guess.
set -e
SHELTERS=/var/lib/wend/shelter
if [ "$1" = __wend_inner ]; then
  # inside the private mount namespace: $2 root, $3 user, $4 volumes (space-separated), $5 env (same), then -- and
  # the command
  root=$2 user=$3 vols=$4 envs=$5
  shift 5
  [ "$1" = -- ] && shift
  # a chroot's / is no mount point, so it can't be made private: the root's own bind mount is, before anything else
  # is mounted on it (that first bind can reach the box's mount table; the harness unmounts it after the check)
  mount --bind "$root" "$root"
  mount --make-private "$root"
  mount -o remount,bind,ro "$root"
  for v in $vols; do
    src=${v%%:*} rest=${v#*:}
    dst=${rest%%:*} opts=
    [ "$rest" = "$dst" ] || opts=${rest#*:}
    [ -e "$root$dst" ] || { echo "wend podman stand-in: no mount point $dst in the shelter root"; exit 125; }
    mount --bind "$src" "$root$dst"
    case ",$opts," in *,ro,*) mount -o remount,bind,ro "$root$dst";; esac
  done
  mount -t tmpfs -o mode=1777 tmpfs "$root/tmp"
  mount -t tmpfs -o mode=755 tmpfs "$root/run"
  mount --rbind /dev "$root/dev"
  mount -t proc proc "$root/proc"
  uid=${user%%:*} gid=${user#*:}
  # podman's default environment plus --env, set out here: the shelter's userland needn't have env or a shell
  exec /usr/bin/env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin HOME=/ TERM=xterm \
    container=podman $envs /usr/sbin/chroot --userspec="$uid:$gid" --groups="$gid" "$root" "$@"
fi
case "$1 $2" in
  "network create"|"network exists"|"stop "*|"image exists") exit 0;;  # the units' ExecStartPre and ExecStop:
  "import -") cat >/dev/null; exit 0;;                                     # nothing to do here
esac
[ "$1" = run ] || { echo "wend podman stand-in: only run is supported, not: $*" >&2; exit 125; }
shift
user=0:0 vols= envs=
while [ $# -gt 0 ]; do
  case "$1" in
    --*=*) a=${1%%=*} b=${1#*=}; shift; set -- "$a" "$b" "$@"; continue;;
  esac
  case "$1" in
    --rm|--read-only) shift;;
    --name|--uidmap|--gidmap|--cap-drop|--cap-add|--security-opt|--network|--publish|-p|--tmpfs|--hostname) shift 2;;
    --user|-u) user=$2; shift 2;;
    --volume|-v) vols="$vols $2"; shift 2;;
    --env|-e) envs="$envs $2"; shift 2;;
    -*) echo "wend podman stand-in: unknown flag $1" >&2; exit 125;;
    *) break;;
  esac
done
[ $# -ge 2 ] || { echo "wend podman stand-in: no image and command" >&2; exit 125; }
image=${1#localhost/wend-shelter-*:}  # shelter.start imports the root under the lock's digest as the tag
case "$1" in localhost/wend-shelter-*:*) ;; *) image=;; esac
shift
case "$image" in *[!0-9a-f]*|'') echo "wend podman stand-in: run a shelter's image (localhost/wend-shelter-<app>:<digest>), not $image" >&2; exit 125;; esac
case "$user" in *[!0-9:]*|*:*:*|:*|*:) echo "wend podman stand-in: --user must be uid:gid, not $user" >&2; exit 125;; esac
root=
for l in $SHELTERS/*/shelter.lock; do
  [ -f "$l" ] && grep -qx "image = \"sha256:$image\"" "$l" && root=${l%/shelter.lock}/root && break
done
[ -n "$root" ] && [ -d "$root" ] || { echo "wend podman stand-in: no shelter root has image $image" >&2; exit 125; }
P=
unshare --help 2>&1 | grep -q -- --propagation && P="--propagation unchanged"  # util-linux 2.27 on: default private
exec unshare -m $P "$0" __wend_inner "$root" "$user" "$vols" "$envs" -- "$@"
