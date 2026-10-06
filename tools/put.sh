#!/bin/sh
# copy a file (a program you built, say) onto a MachOS disk image's root
# filesystem, without booting or mounting anything
# usage: tools/put.sh FILE [DEST] [IMAGE]
#   DEST   path inside MachOS (default /bin/<name of FILE>)
#   IMAGE  default out/debug/disk.img; MachOS must not be running from it
set -eu
cd "$(dirname "$0")/.."

[ $# -ge 1 ] || { echo "usage: tools/put.sh FILE [DEST] [IMAGE]" >&2; exit 2; }
SRC="$1"
DEST="${2:-/bin/$(basename "$1")}"
IMG="${3:-out/debug/disk.img}"
[ -f "$SRC" ] || { echo "put.sh: $SRC does not exist" >&2; exit 1; }
[ -f "$IMG" ] || { echo "put.sh: $IMG does not exist" >&2; exit 1; }

INFO=$(sgdisk -i 2 "$IMG")
FIRST=$(echo "$INFO" | sed -n 's/^First sector: \([0-9]*\).*/\1/p')
LAST=$(echo "$INFO" | sed -n 's/^Last sector: \([0-9]*\).*/\1/p')
[ -n "$FIRST" ] || { echo "put.sh: no partition 2 on $IMG" >&2; exit 1; }
PART=$(mktemp -p out put.XXXXXX)
trap 'rm -f "$PART"' EXIT
dd if="$IMG" of="$PART" bs=512 skip="$FIRST" count="$((LAST - FIRST + 1))" status=none
debugfs -w -R "rm $DEST" "$PART" >/dev/null 2>&1 || true
debugfs -w -R "write $SRC $DEST" "$PART" 2>&1 | grep -vE '^debugfs|^Allocated inode' || true
debugfs -R "stat $DEST" "$PART" 2>&1 | grep -q "Inode:" || { echo "put.sh: writing $DEST failed (does its directory exist?)" >&2; exit 1; }
dd if="$PART" of="$IMG" bs=512 seek="$FIRST" conv=notrunc status=none
echo "$SRC -> $DEST on $IMG"
