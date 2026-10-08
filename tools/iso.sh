#!/bin/sh
# make the live ISO (UEFI): machboot and a FAT image with the kernel and the
# initrd as the ISO's EFI boot image. it boots on its own from a CD, a USB
# stick (dd it there) or Ventoy, runs MachOS from memory, and can `install`
# it onto a disk
# usage: tools/iso.sh [OUT.iso]   (default out/machos.iso)
set -eu
cd "$(dirname "$0")/.."
ISO="${1:-out/machos.iso}"
tools/mkimage.sh release >/dev/null
MACHBOOT=machboot/out/uefi/release/BOOTX64.EFI
T=out/iso
rm -rf "$T"
mkdir -p "$T"
cat > out/live.conf <<CONF
# the live ISO's menu
timeout 5
default 1

entry MachOS live
    help    runs from memory: nothing on the disks is touched
    kernel  /boot/kernel.elf
    initrd  /boot/initrd.tar
    cmdline live

entry MachOS live (verbose)
    help    every kernel message on the screen
    kernel  /boot/kernel.elf
    initrd  /boot/initrd.tar
    cmdline live debug

entry MachOS (rescue)
    help    a root shell, the disks unmounted, to repair them
    kernel  /boot/kernel.elf
    initrd  /boot/initrd.tar
    cmdline rescue
CONF
# the EFI boot image: big enough for everything, rounded up
SIZE=$(( ( $(stat -c %s kernel/out/kernel/release/kernel.elf) + $(stat -c %s out/release/initrd.tar) + $(stat -c %s "$MACHBOOT") ) / 1048576 + 4 ))
dd if=/dev/zero of="$T/efiboot.img" bs=1M count="$SIZE" status=none
mformat -i "$T/efiboot.img" ::
mmd   -i "$T/efiboot.img" ::/EFI ::/EFI/BOOT ::/boot
mcopy -i "$T/efiboot.img" "$MACHBOOT" ::/EFI/BOOT/BOOTX64.EFI
mcopy -i "$T/efiboot.img" out/live.conf ::/boot/machboot.conf
mcopy -i "$T/efiboot.img" kernel/out/kernel/release/kernel.elf ::/boot/kernel.elf
mcopy -i "$T/efiboot.img" out/release/initrd.tar ::/boot/initrd.tar
xorriso -as mkisofs -R -J -V MACHOS -e efiboot.img -no-emul-boot -isohybrid-gpt-basdat \
    -o "$ISO" "$T" >/dev/null 2>&1
rm -rf "$T" out/live.conf
echo "$ISO"
