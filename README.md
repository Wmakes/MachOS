<p align="center"><img src=".github/logo.png" alt="MachOS" width="600"></p>

A small Unix-like operating system for x86_64, written in [mach](https://github.com/briar-systems/mach).

## Building

You need the [mach](https://github.com/briar-systems/mach) compiler (6.10) and, on Arch:

```
sudo pacman -S lld gptfdisk e2fsprogs mtools xorriso qemu-system-x86 edk2-ovmf python
```

```
tools/run.sh        # build a disk image and boot it in QEMU
tools/iso.sh        # build the live ISO (out/machos.iso)
tools/test.py       # run the tests
```

Log in as `root` (no password).
