#!/bin/sh
# build the kernel and the userland, then make or update the dev disk
# out/<profile>/disk.img. it has the same layout `install` writes:
#   GPT, partition 1 = EFI system partition (FAT32, 64 MiB: only machboot; it
#                      reads its config, the kernel and the initrd from /boot)
#        partition 2 = MachOS root (ext2, 4 KiB blocks: root/ plus /bin)
# an existing disk is updated in place (EFI files replaced, /bin and /boot
# refreshed) so files created inside MachOS survive rebuilds; --fresh starts
# over. the initrd on the EFI partition is the root used when no disk is found
# usage: tools/mkimage.sh [debug|release] [--fresh] [--rescue]   prints the image path
#   --rescue  make "MachOS (rescue)" the entry machboot boots
set -eu
cd "$(dirname "$0")/.."

PROFILE=debug
FRESH=0
RESCUE=0
for a in "$@"; do
    case "$a" in
        debug|release) PROFILE="$a" ;;
        --fresh) FRESH=1 ;;
        --rescue) RESCUE=1 ;;
        *) echo "mkimage.sh: unknown argument $a" >&2; exit 2 ;;
    esac
done
OUT="out/$PROFILE"
KERNEL="kernel/out/kernel/$PROFILE/kernel.elf"
BIN="userland/out/machos/$PROFILE/bin"

# MachOS root partition type; kernel/src/fs/gpt.mach and the gpt tool agree
ROOT_TYPE=A9056588-C0B9-470F-AC01-A8528DCA9461
DISK_MB=256
ESP_START=2048      # sectors
ESP_SECTORS=131072  # 64 MiB
ROOT_START=133120
ROOT_MB=190

mkdir -p "$OUT"

# kernel and userland are separate mach projects; each pulls a fresh copy of the SDK
(cd kernel && mach dep pull . >/dev/null && mach build . -t kernel -p "$PROFILE") >&2
(cd userland && mach dep pull . >/dev/null && mach build . -t machos -p "$PROFILE") >&2
MACHBOOT=$(tools/machboot.sh release)

# the root tree: root/ as is, every program in /bin, and what `install`
# puts on a new disk's EFI partition in /boot
ROOTFS="$OUT/rootfs"
rm -rf "$ROOTFS"
mkdir -p "$ROOTFS/bin" "$ROOTFS/boot" "$ROOTFS/boot/efi"
cp -r root/. "$ROOTFS/"
cp "$BIN/"* "$ROOTFS/bin/"
cp "$KERNEL" "$ROOTFS/boot/kernel.elf"
cp "$MACHBOOT" "$ROOTFS/boot/BOOTX64.EFI"
if [ "$RESCUE" = 1 ]; then
    sed 's/^default 1/default 2/' root/boot/machboot.conf > "$ROOTFS/boot/machboot.conf"
fi
# modes as MachOS should see them (tar and mke2fs -d keep them)
mkdir -p "$ROOTFS/root" "$ROOTFS/home"
chmod 0755 "$ROOTFS" "$ROOTFS/bin" "$ROOTFS/boot" "$ROOTFS/etc" "$ROOTFS/home"
chmod 0700 "$ROOTFS/root"
find "$ROOTFS/etc" -type d -exec chmod 0755 {} +
find "$ROOTFS/etc" -type f -exec chmod 0644 {} +
chmod 0644 "$ROOTFS/boot/"*.*
chmod 0755 "$ROOTFS/boot/efi"
chmod 0600 "$ROOTFS/etc/shadow"
chmod 0755 "$ROOTFS/bin/"*
chmod 4755 "$ROOTFS/bin/su" "$ROOTFS/bin/passwd"
tar --format=ustar --owner=0 --group=0 -cf "$OUT/initrd.tar" -C "$ROOTFS" .
# the disk root also carries the initrd, for `install` to give new disks a
# rescue entry (the initrd itself cannot contain itself)
cp "$OUT/initrd.tar" "$ROOTFS/boot/initrd.tar"

# mke2fs -d and debugfs write keep the host's user ids: give these paths
# (newline-separated, absolute inside the filesystem) to root
owned_by_root() {
    CMDS="$OUT/owner.cmds"
    echo "$2" | while read -r p; do
        [ -n "$p" ] || continue
        echo "sif \"$p\" uid 0"
        echo "sif \"$p\" gid 0"
    done > "$CMDS"
    echo 'sif / uid 0' >> "$CMDS"
    echo 'sif / gid 0' >> "$CMDS"
    debugfs -w -f "$CMDS" "$1" >/dev/null 2>&1
    rm -f "$CMDS"
}

IMG="$OUT/disk.img"
ROOTIMG="$OUT/root.img"
if [ "$FRESH" = 1 ] || [ ! -f "$IMG" ] || ! sgdisk -p "$IMG" 2>/dev/null | grep -q 'MachOS root' \
   || ! sgdisk -i 2 "$IMG" 2>/dev/null | grep -q "First sector: $ROOT_START "; then
    rm -f "$IMG" "$ROOTIMG"
    if [ "$RESCUE" = 1 ]; then touch "$OUT/rescue-conf"; else rm -f "$OUT/rescue-conf"; fi
    dd if=/dev/zero of="$IMG" bs=1M count="$DISK_MB" 2>/dev/null
    sgdisk -n "1:$ESP_START:+$((ESP_SECTORS / 2048))M" -t 1:ef00 -c 1:"EFI system" \
           -n "2:$ROOT_START:+${ROOT_MB}M" -t "2:$ROOT_TYPE" -c 2:"MachOS root" "$IMG" >/dev/null
    mke2fs -q -t ext2 -b 4096 -I 128 -O ^dir_index,^resize_inode,^ext_attr -L machos \
           -d "$ROOTFS" "$ROOTIMG" "${ROOT_MB}M" >&2
    owned_by_root "$ROOTIMG" "$(cd "$ROOTFS" && find . -mindepth 1 | sed 's|^\.||')"
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
    # machboot.conf is the system's own setting (`machboot` changes it): only
    # written when the disk has none
    KEEP_CONF=0
    if ! debugfs -R "stat /boot/machboot.conf" "$ROOTIMG" 2>&1 | grep -q "File not found"; then
        KEEP_CONF=1
    fi
    # Add resolver defaults to older dev images without replacing local policy.
    for n in hosts services; do
        if debugfs -R "stat /etc/$n" "$ROOTIMG" 2>&1 | grep -q "File not found"; then
            echo "write $ROOTFS/etc/$n /etc/$n" >> "$CMDS"
        fi
    done
    for d in /etc/ssl /etc/ssl/certs; do
        if debugfs -R "stat $d" "$ROOTIMG" 2>&1 | grep -q "File not found"; then
            echo "mkdir $d" >> "$CMDS"
        fi
    done
    for d in bin boot etc/ssl/certs; do
        for f in "$ROOTFS/$d"/*; do
            [ -f "$f" ] || continue
            n=$(basename "$f")
            if [ "$d/$n" = boot/machboot.conf ] && [ "$KEEP_CONF" = 1 ] && [ "$RESCUE" = 0 ] \
               && [ ! -f "$OUT/rescue-conf" ]; then continue; fi
            echo "rm /$d/$n" >> "$CMDS"
            echo "write $f /$d/$n" >> "$CMDS"
        done
    done
    debugfs -w -f "$CMDS" "$ROOTIMG" >/dev/null 2>&1
    owned_by_root "$ROOTIMG" "$(cd "$ROOTFS" && find bin boot etc/ssl -mindepth 1 | sed 's|^|/|')"
    # a rescue build's config is undone by the next normal one
    if [ "$RESCUE" = 1 ]; then touch "$OUT/rescue-conf"; else rm -f "$OUT/rescue-conf"; fi
    dd if="$ROOTIMG" of="$IMG" bs=512 seek="$ROOT_START" conv=notrunc 2>/dev/null
    rm -f "$ROOTIMG" "$CMDS"
fi

# the EFI partition is always rebuilt
ESP="$OUT/esp.img"
rm -f "$ESP"
dd if=/dev/zero of="$ESP" bs=512 count="$ESP_SECTORS" 2>/dev/null
mformat -i "$ESP" -F -c 1 ::
mmd   -i "$ESP" ::/EFI ::/EFI/BOOT
mcopy -i "$ESP" "$MACHBOOT" ::/EFI/BOOT/BOOTX64.EFI
dd if="$ESP" of="$IMG" bs=512 seek="$ESP_START" conv=notrunc 2>/dev/null
rm -f "$ESP"
echo "$IMG"
