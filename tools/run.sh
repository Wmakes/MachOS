#!/bin/sh
# build the kernel, pack it with Limine into a FAT image, boot it under OVMF
# Arch: sudo pacman -S limine qemu-system-x86 edk2-ovmf mtools
# usage: tools/run.sh [release] [extra qemu args...]
set -eu
cd "$(dirname "$0")/.."

PROFILE=debug
if [ "${1:-}" = "release" ]; then PROFILE=release; shift; fi

LIMINE="${LIMINE:-/usr/share/limine}"
OVMF_CODE="${OVMF_CODE:-/usr/share/edk2/x64/OVMF_CODE.4m.fd}"
OVMF_VARS="${OVMF_VARS:-/usr/share/edk2/x64/OVMF_VARS.4m.fd}"
OUT="out/kernel/$PROFILE"

for f in "$LIMINE/BOOTX64.EFI" "$OVMF_CODE" "$OVMF_VARS"; do
    [ -f "$f" ] || { echo "run.sh: missing $f" >&2; exit 1; }
done

mach build . -t kernel -p "$PROFILE"

IMG="$OUT/disk.img"
rm -f "$IMG"
dd if=/dev/zero of="$IMG" bs=1M count=64 2>/dev/null
mformat -i "$IMG" -F ::
mmd   -i "$IMG" ::/EFI ::/EFI/BOOT ::/boot ::/boot/limine
mcopy -i "$IMG" "$LIMINE/BOOTX64.EFI" ::/EFI/BOOT/
mcopy -i "$IMG" tools/limine.conf ::/boot/limine/
mcopy -i "$IMG" "$OUT/kernel.elf" ::/boot/kernel.elf

# OVMF keeps its NVRAM (boot entries etc.) in a writable copy of the vars file
[ -f "$OUT/vars.fd" ] || cp "$OVMF_VARS" "$OUT/vars.fd"

exec qemu-system-x86_64 -M q35 -m 256M \
    -drive if=pflash,format=raw,readonly=on,file="$OVMF_CODE" \
    -drive if=pflash,format=raw,file="$OUT/vars.fd" \
    -drive format=raw,file="$IMG" \
    -serial stdio -no-reboot -no-shutdown "$@"
