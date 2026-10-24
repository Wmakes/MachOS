#!/bin/sh
# compile the system once, independently of disk and ISO assembly
set -eu
cd "$(dirname "$0")/.."
PROFILE="${1:-debug}"
case "$PROFILE" in debug|release) ;; *) echo "build.sh: unknown profile $PROFILE" >&2; exit 2 ;; esac
tools/deps.sh
# Drop generated binaries whose source was removed, including the legacy updater.
for binary in userland/out/machos/"$PROFILE"/bin/*; do
    [ -f "$binary" ] || continue
    name=$(basename "$binary")
    [ -f "userland/src/bin/$name.mach" ] || rm -f "$binary"
done
(cd kernel && mach dep pull . >/dev/null && mach build . -t kernel -p "$PROFILE")
(cd userland && mach dep pull . >/dev/null && mach build . -t machos -p "$PROFILE")
tools/machboot.sh release >/dev/null
