#!/bin/sh
# undo tools/mount.sh: unmount a MachOS image's partitions and detach it
# usage: tools/umount.sh [IMAGE]     (default: out/debug/disk.img)
set -eu
cd "$(dirname "$0")/.."

IMG="${1:-out/debug/disk.img}"
LOOPS=$(losetup -j "$IMG" | cut -d: -f1)
[ -n "$LOOPS" ] || { echo "umount.sh: $IMG is not attached" >&2; exit 1; }
sync
for LOOP in $LOOPS; do
    for part in "$LOOP"p*; do
        [ -b "$part" ] || continue
        if grep -q "^$part " /proc/mounts; then
            udisksctl unmount --no-user-interaction -b "$part" >/dev/null && echo "unmounted $part"
        fi
    done
    udisksctl loop-delete --no-user-interaction -b "$LOOP" && echo "detached $LOOP"
done
