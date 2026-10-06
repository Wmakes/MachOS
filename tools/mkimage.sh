#!/bin/sh
# build the kernel and the user programs, then make or update the disk:
#   GPT, partition 1 = EFI system partition (FAT: Limine, kernel, initrd)
#        partition 2 = MachOS root (ext2, 4 KiB blocks: root/ plus /bin)
# an existing disk is updated in place (EFI files replaced, /bin refreshed)
# so files created inside MachOS survive rebuilds; --fresh starts over.
# the initrd stays on the EFI partition as the root used when no disk is found
# usage: tools/mkimage.sh [debug|release] [--fresh]   prints the image path
set -eu
cd "$(dirname "$0")/.."

PROFILE=debug
FRESH=0
for a in "$@"; do
    case "$a" in
        debug|release) PROFILE="$a" ;;
        --fresh) FRESH=1 ;;
        *) echo "mkimage.sh: unknown argument $a" >&2; exit 2 ;;
    esac
done
LIMINE="${LIMINE:-/usr/share/limine}"
OUT="out/kernel/$PROFILE"

# MachOS root partition type; src/fs/gpt.mach looks for the same GUID
ROOT_TYPE=A9056588-C0B9-470F-AC01-A8528DCA9461
DISK_MB=128
ESP_START=2048      # sectors
ESP_SECTORS=65536   # 32 MiB
ROOT_START=67584
ROOT_MB=92

[ -f "$LIMINE/BOOTX64.EFI" ] || { echo "mkimage.sh: missing $LIMINE/BOOTX64.EFI" >&2; exit 1; }

mach build . -t kernel -p "$PROFILE" >&2
mach build . -t user -p "$PROFILE" >&2

# the root tree: root/ as is, plus every program in /bin
ROOTFS="$OUT/rootfs"
rm -rf "$ROOTFS"
mkdir -p "$ROOTFS/bin"
cp -r root/. "$ROOTFS/"
cp "out/user/$PROFILE/bin/"* "$ROOTFS/bin/"
# what `install` puts on a new disk's EFI partition
cp "$OUT/kernel.elf" "$ROOTFS/boot/kernel.elf"
cp "$LIMINE/BOOTX64.EFI" "$ROOTFS/boot/BOOTX64.EFI"
tar --format=ustar --owner=0 --group=0 -cf "$OUT/initrd.tar" -C "$ROOTFS" .

IMG="$OUT/disk.img"
ROOTIMG="$OUT/root.img"
if [ "$FRESH" = 1 ] || [ ! -f "$IMG" ] || ! sgdisk -p "$IMG" 2>/dev/null | grep -q 'MachOS root'; then
    rm -f "$IMG" "$ROOTIMG"
    dd if=/dev/zero of="$IMG" bs=1M count="$DISK_MB" 2>/dev/null
    sgdisk -n "1:$ESP_START:+$((ESP_SECTORS / 2048))M" -t 1:ef00 -c 1:"EFI system" \
              -n "2:$ROOT_START:+${ROOT_MB}M" -t "2:$ROOT_TYPE" -c 2:"MachOS root" "$IMG" >/dev/null
    rm -f "$ROOTIMG"
    mke2fs -q -t ext2 -b 4096 -I 128 -O ^dir_index,^resize_inode,^ext_attr -L machos -d "$ROOTFS" "$ROOTIMG" "${ROOT_MB}M" >&2
    dd if="$ROOTIMG" of="$IMG" bs=512 seek="$ROOT_START" conv=notrunc 2>/dev/null
    rm -f "$ROOTIMG"
else
    # refresh /bin and /boot inside the existing root partition
    dd if="$IMG" of="$ROOTIMG" bs=512 skip="$ROOT_START" count="$((ROOT_MB * 2048))" 2>/dev/null
    CMDS="$OUT/debugfs.cmds"
    : > "$CMDS"
    # (only when missing: debugfs's mkdir of an existing directory leaves the
    # bitmaps inconsistent)
    if debugfs -R "stat /boot" "$ROOTIMG" 2>&1 | grep -q "File not found"; then
        echo "mkdir /boot" >> "$CMDS"
    fi
    for d in bin boot; do
        for f in "$ROOTFS/$d"/*; do
            n=$(basename "$f")
            echo "rm /$d/$n" >> "$CMDS"
            echo "write $f /$d/$n" >> "$CMDS"
        done
    done
    debugfs -w -f "$CMDS" "$ROOTIMG" >/dev/null 2>&1
    dd if="$ROOTIMG" of="$IMG" bs=512 seek="$ROOT_START" conv=notrunc 2>/dev/null
    rm -f "$ROOTIMG" "$CMDS"
fi

# the EFI partition is always rebuilt
ESP="$OUT/esp.img"
rm -f "$ESP"
dd if=/dev/zero of="$ESP" bs=512 count="$ESP_SECTORS" 2>/dev/null
mformat -i "$ESP" ::
mmd   -i "$ESP" ::/EFI ::/EFI/BOOT ::/boot ::/boot/limine
mcopy -i "$ESP" "$LIMINE/BOOTX64.EFI" ::/EFI/BOOT/
mcopy -i "$ESP" tools/limine.conf ::/boot/limine/
mcopy -i "$ESP" "$OUT/kernel.elf" ::/boot/kernel.elf
mcopy -i "$ESP" "$OUT/initrd.tar" ::/boot/initrd.tar
dd if="$ESP" of="$IMG" bs=512 seek="$ESP_START" conv=notrunc 2>/dev/null
rm -f "$ESP"
echo "$IMG"
