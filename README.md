<p align="center"><img src=".github/logo.png" alt="MachOS" width="600"></p>

A small Unix-like operating system for x86_64, written in [mach](https://github.com/briar-systems/mach).

## Build and run

You need mach 6.10, QEMU, OVMF, Limine, mtools, gptfdisk and e2fsprogs.

```
tools/run.sh        # build a disk image and boot it in QEMU
tools/test.py       # run the tests
```

Log in as `root` (no password).
