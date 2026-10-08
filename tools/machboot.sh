#!/bin/sh
# build machboot, the boot loader: mach compiles it for a win64 target and
# lld-link links the objects as a UEFI application. prints the .efi path
# usage: tools/machboot.sh [debug|release]
set -eu
cd "$(dirname "$0")/.."
PROFILE="${1:-release}"
(cd machboot && mach dep pull . >/dev/null && mach build . -t uefi -p "$PROFILE") >&2
OUT="machboot/out/uefi/$PROFILE"
lld-link -subsystem:efi_application -entry:mainCRTStartup -nodefaultlib \
    $(find "$OUT/obj" -name '*.o' | sort) -out:"$OUT/BOOTX64.EFI" 2>&1 \
    | grep -v "longer than 8 characters" >&2 || true
echo "$OUT/BOOTX64.EFI"
