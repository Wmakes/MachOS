#!/bin/sh
# build the kernel and pack it with Limine into a FAT image
# usage: tools/mkimage.sh [debug|release]   prints the image path
set -eu
cd "$(dirname "$0")/.."

PROFILE="${1:-debug}"
LIMINE="${LIMINE:-/usr/share/limine}"
OUT="out/kernel/$PROFILE"

[ -f "$LIMINE/BOOTX64.EFI" ] || { echo "mkimage.sh: missing $LIMINE/BOOTX64.EFI" >&2; exit 1; }

mach build . -t kernel -p "$PROFILE" >&2

IMG="$OUT/disk.img"
rm -f "$IMG"
dd if=/dev/zero of="$IMG" bs=1M count=64 2>/dev/null
mformat -i "$IMG" -F ::
mmd   -i "$IMG" ::/EFI ::/EFI/BOOT ::/boot ::/boot/limine
mcopy -i "$IMG" "$LIMINE/BOOTX64.EFI" ::/EFI/BOOT/
mcopy -i "$IMG" tools/limine.conf ::/boot/limine/
mcopy -i "$IMG" "$OUT/kernel.elf" ::/boot/kernel.elf
echo "$IMG"
