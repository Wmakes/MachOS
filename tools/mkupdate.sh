#!/bin/sh
# make a MachOS update package: everything `update` (inside MachOS)
# replaces, as a .tgz: VERSION, boot/kernel.elf, boot/initrd.tar,
# boot/BOOTX64.EFI (machboot), bin/*, etc/motd and etc/machos.conf
# usage: tools/mkupdate.sh [OUT.tgz]   (default out/machos-update-VERSION.tgz)
set -eu
cd "$(dirname "$0")/.."
tools/mkimage.sh release >/dev/null
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' kernel/mach.toml | head -1)
OUT="${1:-out/machos-update-$VERSION.tgz}"
S=out/update-stage
rm -rf "$S"
mkdir -p "$S/boot" "$S/bin" "$S/etc"
echo "$VERSION" > "$S/VERSION"
cp kernel/out/kernel/release/kernel.elf "$S/boot/kernel.elf"
cp out/release/initrd.tar "$S/boot/initrd.tar"
cp machboot/out/uefi/release/BOOTX64.EFI "$S/boot/BOOTX64.EFI"
cp userland/out/machos/release/bin/* "$S/bin/"
cp root/etc/motd "$S/etc/motd"
cp root/etc/machos.conf "$S/etc/machos.conf"
chmod 0755 "$S/bin/"*
tar --format=ustar --owner=0 --group=0 -C "$S" -cf - . | gzip -9 > "$OUT"
rm -rf "$S"
echo "$OUT"
