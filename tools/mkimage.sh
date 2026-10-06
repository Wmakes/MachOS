#!/bin/sh
# build the kernel and the user programs, pack the programs and root/ into
# an initrd tar, and put everything with Limine into a FAT image
# usage: tools/mkimage.sh [debug|release]   prints the image path
set -eu
cd "$(dirname "$0")/.."

PROFILE="${1:-debug}"
LIMINE="${LIMINE:-/usr/share/limine}"
OUT="out/kernel/$PROFILE"

[ -f "$LIMINE/BOOTX64.EFI" ] || { echo "mkimage.sh: missing $LIMINE/BOOTX64.EFI" >&2; exit 1; }

mach build . -t kernel -p "$PROFILE" >&2
mach build . -t user -p "$PROFILE" >&2

# the initrd: root/ as is, plus every program in /bin
ROOTFS="$OUT/rootfs"
rm -rf "$ROOTFS"
mkdir -p "$ROOTFS/bin"
cp -r root/. "$ROOTFS/"
cp "out/user/$PROFILE/bin/"* "$ROOTFS/bin/"
tar --format=ustar --owner=0 --group=0 -cf "$OUT/initrd.tar" -C "$ROOTFS" .

IMG="$OUT/disk.img"
rm -f "$IMG"
dd if=/dev/zero of="$IMG" bs=1M count=64 2>/dev/null
mformat -i "$IMG" -F ::
mmd   -i "$IMG" ::/EFI ::/EFI/BOOT ::/boot ::/boot/limine
mcopy -i "$IMG" "$LIMINE/BOOTX64.EFI" ::/EFI/BOOT/
mcopy -i "$IMG" tools/limine.conf ::/boot/limine/
mcopy -i "$IMG" "$OUT/kernel.elf" ::/boot/kernel.elf
mcopy -i "$IMG" "$OUT/initrd.tar" ::/boot/initrd.tar
echo "$IMG"
