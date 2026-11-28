#!/bin/sh
# try.sh: put wend on a machine, checked and unpacked, and say the one command that starts it.
#
#   curl -fsSL https://github.com/FieldmouseWorks/wend/releases/latest/download/try.sh | sh
#   sh try.sh ./wend-x86_64-linux.tar.gz    a release copied here from a machine that can reach GitHub
#
# It downloads the latest release and its SHA256SUMS (or takes the tarball it is given, and the SHA256SUMS beside
# it), refuses a copy whose checksum is wrong, and unpacks it into ./wend-x86_64-linux. It starts nothing: not wend,
# not sudo. It needs no root, and writes only that folder and a temporary one it removes.
#
# POSIX sh, for what old servers have as /bin/sh (bash 3, dash, busybox ash). The checksum tool is sha256sum alone:
# coreutils has shipped it on every release a route starts from but CentOS 4, which has nothing else that computes
# SHA-256 either. WEND_TRY_URL names another place to download from (client/tests/try_sh.rs points it at a folder).
# Everything runs from main, called on the last line, so a copy of this script cut short in a pipe runs nothing.

set -u

name=wend-x86_64-linux
url=${WEND_TRY_URL:-https://github.com/FieldmouseWorks/wend/releases/latest/download}
tmp=
unpacking=

say() {
    printf '%s\n' "$@"
}

# Stop, with every line it is given on stderr.
fail() {
    say "$@" >&2
    exit 1
}

# What this run made and didn't finish: the downloads, and a folder half unpacked.
cleanup() {
    [ -z "$tmp" ] || rm -rf "$tmp"
    [ -z "$unpacking" ] || rm -rf "./$name"
}

have() {
    command -v "$1" >/dev/null 2>&1
}

# For a machine that can't download: where the files are, and how to bring them here.
elsewhere() {
    say "On a machine that can reach GitHub, download these three files:" \
        "" \
        "    $url/try.sh" \
        "    $url/$name.tar.gz" \
        "    $url/SHA256SUMS" \
        "" \
        "Copy them into one folder on this machine, and from that folder run:" \
        "" \
        "    sh try.sh $name.tar.gz"
}

# One of the release's files, into the temporary folder. The latest release's address redirects to the file (-L;
# wget follows by itself).
fetch() {
    if have curl; then
        curl -fsSL -o "$tmp/$1" "$url/$1"
    else
        wget -q -O "$tmp/$1" "$url/$1"
    fi
}

# The checksum a SHA256SUMS lists for the release tarball, or nothing.
listed() {
    while read -r sum file || [ -n "$sum" ]; do
        case $file in
        "$name.tar.gz" | "*$name.tar.gz")
            say "$sum"
            return
            ;;
        esac
    done <"$1"
}

# Refuse a tarball that isn't the one its SHA256SUMS lists. $tarball and $sums are the files, $shown and
# $sums_shown what to call them.
verify() {
    want=$(listed "$sums")
    [ -n "$want" ] || fail "try.sh: $sums_shown has no checksum for" \
        "$name.tar.gz, so $shown" \
        "was not unpacked. Download both files again."
    got=$(sha256sum <"$tarball") || fail "try.sh: could not read $shown"
    got=${got%% *}
    [ "$got" = "$want" ] || fail "try.sh: $shown" \
        "does not match its checksum, so it was not unpacked." \
        "" \
        "  listed  $want" \
        "  found   $got" \
        "" \
        "It was cut short or changed on the way here. Download it again."
}

main() {
    # nothing, or one tarball: it has no options
    case $#:${1-} in
    0: | 1:[!-]*) ;;
    *) fail "usage: sh try.sh [$name.tar.gz]" ;;
    esac

    system=$(uname -s)
    machine=$(uname -m)
    if [ "$system" != Linux ] || [ "$machine" != x86_64 ]; then
        fail "try.sh: wend is built for 64-bit x86 Linux (x86_64) only." \
            "This machine is $machine $system, so there is nothing here it can run."
    fi
    # root runs wend as it is; anyone else is pointed at sudo, and told what to do without one
    if [ "$(id -u)" = 0 ]; then
        start="./wend"
    else
        start="sudo ./wend"
    fi

    if [ -e "$name" ] || [ -L "$name" ]; then
        fail "try.sh: ./$name is already here, and it was left alone." \
            "" \
            "To use that copy:    cd $name && $start" \
            "To get a fresh one:  move it away first (reports wend wrote are inside it)," \
            "                     or run this from another folder."
    fi

    trap cleanup EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM

    if [ $# -eq 1 ]; then
        tarball=$1
        shown=$tarball
        [ -f "$tarball" ] || fail "try.sh: there is no file $tarball"
        case $tarball in
        */*) sums=${tarball%/*}/SHA256SUMS ;;
        *) sums=SHA256SUMS ;;
        esac
        sums_shown=$sums
        if [ ! -f "$sums" ]; then
            checked="It was NOT checked: there is no SHA256SUMS beside $tarball."
        elif have sha256sum; then
            verify
            checked="Its checksum matches the one in $sums."
        else
            fail "try.sh: this machine has no sha256sum, so it can't check" \
                "$tarball here." \
                "" \
                "Check it on the machine you downloaded it with:" \
                "" \
                "    sha256sum -c SHA256SUMS" \
                "" \
                "When that says OK, remove $sums here and run this again:" \
                "it unpacks a tarball that has no SHA256SUMS beside it without checking it."
        fi
    else
        if ! have sha256sum; then
            say "try.sh: this machine has no sha256sum to check a download with, so nothing" \
                "was downloaded. Download and check wend on another machine instead." "" >&2
            elsewhere >&2
            exit 1
        fi
        if ! have curl && ! have wget; then
            say "try.sh: this machine has neither curl nor wget to download with." "" >&2
            elsewhere >&2
            exit 1
        fi
        tmp=$(mktemp -d "${TMPDIR:-/tmp}/wend-try.XXXXXX") || fail "try.sh: could not make a temporary folder"
        for file in "$name.tar.gz" SHA256SUMS; do
            if ! fetch "$file"; then
                say "try.sh: could not download $url/$file" \
                    "" \
                    "If the network was only slow, run this again. An old release often can't make" \
                    "the secure connection GitHub asks for (CentOS 5 and 6, Debian 6 and older)." "" >&2
                elsewhere >&2
                exit 1
            fi
        done
        tarball=$tmp/$name.tar.gz
        shown="the download of $name.tar.gz"
        sums=$tmp/SHA256SUMS
        sums_shown="the release's SHA256SUMS"
        verify
        checked="Its checksum matches the release's SHA256SUMS."
    fi

    # mkdir takes the name or fails: nothing that turns up there meanwhile is unpacked into
    mkdir "$name" || fail "try.sh: could not make ./$name here. Run this from a folder you can write to."
    unpacking=1
    # only the release's own folder comes out of the tarball, and root keeps the files its own (o): the tarball
    # records the account that packed it, and root would otherwise hand that account's number the binary
    tar xzof - "$name" <"$tarball" && [ -f "$name/wend" ] ||
        fail "try.sh: $shown" \
            "did not unpack into a wend: it is not a wend release, or it is damaged."
    # asked of the file, never by starting it
    [ -x "$name/wend" ] || fail "try.sh: wend unpacked, but it can't be run from this folder, so it was removed." \
        "A folder on a filesystem mounted noexec does that, and /tmp often is one." \
        "Run this again from another folder, such as your home."
    unpacking=

    say "wend is unpacked in ./$name, and nothing has been started." \
        "$checked" \
        "" \
        "Next, at a terminal on this machine:" \
        "" \
        "    cd $name && $start" \
        "" \
        "It walks you through a read-only check of this machine, and changes nothing."
    [ "$start" = ./wend ] || say "No sudo here? Become root (su -), come back to this folder, and run ./wend"
}

main "$@"
