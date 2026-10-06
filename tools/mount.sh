#!/bin/sh
# mount a MachOS disk's filesystems on the host, without sudo (udisks):
#   the root (ext2) and the EFI system partition (FAT32)
# usage: tools/mount.sh [IMAGE]      (default: out/debug/disk.img)
# prints where they are mounted; tools/umount.sh IMAGE undoes it.
# MachOS must not be running from the image at the same time. files MachOS
# created belong to root on the host, so changing those needs sudo
set -eu
cd "$(dirname "$0")/.."

IMG="${1:-out/debug/disk.img}"
[ -f "$IMG" ] || { echo "mount.sh: $IMG does not exist (tools/mkimage.sh makes it)" >&2; exit 1; }
if losetup -j "$IMG" | grep -q .; then
    echo "mount.sh: $IMG is already attached:" >&2
    losetup -j "$IMG" >&2
    exit 1
fi

LOOP=$(udisksctl loop-setup --no-user-interaction -f "$IMG" | sed -n 's/.* as \(\/dev\/loop[0-9]*\)\.*$/\1/p')
[ -n "$LOOP" ] || { echo "mount.sh: udisksctl could not attach $IMG" >&2; exit 1; }
# give the kernel a moment to read the partition table
for _ in 1 2 3 4 5 6 7 8 9 10; do
    [ -b "${LOOP}p2" ] && break
    sleep 0.2
done
for p in 2 1; do
    [ -b "${LOOP}p$p" ] || continue
    udisksctl mount --no-user-interaction -b "${LOOP}p$p" 2>/dev/null \
        | sed "s|^Mounted ${LOOP}p$p at |partition $p: |" || true
done
echo "attached as $LOOP"
