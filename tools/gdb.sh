#!/bin/sh
# boot paused with a gdb stub on :1234, attach gdb with kernel symbols
# terminal 1: tools/gdb.sh     terminal 2: gdb -ex 'target remote :1234' out/kernel/debug/kernel.elf
cd "$(dirname "$0")/.."
exec tools/run.sh -s -S "$@"
