#!/bin/sh
# boot an existing MachOS install as it is (nothing is built or changed):
# a disk image made by `install` (see tools/run.sh --disk) or a real disk
# usage: tools/boot.sh IMAGE|/dev/sdX [extra qemu args...]
# a real disk needs read/write access: run with sudo, or be in the disk group.
# it must not be mounted on the host while MachOS runs from it
set -eu
cd "$(dirname "$0")/.."

[ $# -ge 1 ] || { echo "usage: tools/boot.sh IMAGE|/dev/sdX [qemu args...]" >&2; exit 2; }
DISK="$1"
shift
[ -e "$DISK" ] || { echo "boot.sh: $DISK does not exist" >&2; exit 1; }
if [ ! -r "$DISK" ] || [ ! -w "$DISK" ]; then
    echo "boot.sh: no read/write access to $DISK (try sudo)" >&2
    exit 1
fi
if [ -b "$DISK" ] && grep -q "^$DISK" /proc/mounts; then
    echo "boot.sh: $DISK is mounted on the host; unmount it first" >&2
    exit 1
fi
if ! sgdisk -p "$DISK" 2>/dev/null | grep -q 'MachOS root'; then
    echo "boot.sh: warning: no MachOS root partition on $DISK" >&2
fi

OVMF_CODE="${OVMF_CODE:-/usr/share/edk2/x64/OVMF_CODE.4m.fd}"
OVMF_VARS="${OVMF_VARS:-/usr/share/edk2/x64/OVMF_VARS.4m.fd}"
mkdir -p out
VARS="out/boot-vars.fd"
[ -f "$VARS" ] || cp "$OVMF_VARS" "$VARS"

exec qemu-system-x86_64 -M q35 -cpu max -m 1G -smp 4 \
    -drive if=pflash,format=raw,readonly=on,file="$OVMF_CODE" \
    -drive if=pflash,format=raw,file="$VARS" \
    -drive format=raw,file="$DISK" \
    -serial stdio "$@"
