#!/bin/sh
# build MachOS, update the dev disk out/<profile>/disk.img, boot it under OVMF
# Arch: sudo pacman -S limine qemu-system-x86 edk2-ovmf mtools gptfdisk e2fsprogs
# usage: tools/run.sh [release] [--fresh] [--disk FILE]... [extra qemu args...]
#   --fresh      start from a new, empty dev disk
#   --disk FILE  attach FILE as one more disk (/dev/sdb, ...), making a blank
#                512 MiB one if it does not exist: `install /dev/sdb` inside
#                MachOS, then boot it with tools/boot.sh FILE
set -eu
cd "$(dirname "$0")/.."

PROFILE=debug
FRESH=""
DISKS=""
while [ $# -gt 0 ]; do
    case "$1" in
        release) PROFILE=release; shift ;;
        --fresh) FRESH=--fresh; shift ;;
        --disk)
            [ $# -ge 2 ] || { echo "run.sh: --disk needs a file" >&2; exit 2; }
            [ -e "$2" ] || truncate -s 512M "$2"
            DISKS="$DISKS -drive format=raw,file=$2"
            shift 2 ;;
        *) break ;;
    esac
done

OVMF_CODE="${OVMF_CODE:-/usr/share/edk2/x64/OVMF_CODE.4m.fd}"
OVMF_VARS="${OVMF_VARS:-/usr/share/edk2/x64/OVMF_VARS.4m.fd}"
OUT="out/$PROFILE"
for f in "$OVMF_CODE" "$OVMF_VARS"; do
    [ -f "$f" ] || { echo "run.sh: missing $f" >&2; exit 1; }
done

IMG="$(tools/mkimage.sh "$PROFILE" $FRESH)"

# OVMF keeps its NVRAM (boot entries etc.) in a writable copy of the vars file
[ -f "$OUT/vars.fd" ] || cp "$OVMF_VARS" "$OUT/vars.fd"

# shellcheck disable=SC2086 # DISKS is a list of qemu arguments
exec qemu-system-x86_64 -M q35 -cpu max -m 1G -smp 4 \
    -drive if=pflash,format=raw,readonly=on,file="$OVMF_CODE" \
    -drive if=pflash,format=raw,file="$OUT/vars.fd" \
    -drive format=raw,file="$IMG" $DISKS \
    -serial stdio "$@"
