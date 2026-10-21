#!/bin/sh
# initialize missing dependency submodules without resetting existing checkouts
set -eu
cd "$(dirname "$0")/.."

git config --file .gitmodules --get-regexp '^submodule\..*\.path$' |
while read -r key path; do
    if [ ! -e "$path/.git" ]; then
        # upstream nested test dependencies are not part of the MachOS build
        git submodule update --init -- "$path"
    fi
    if [ ! -f "$path/mach.toml" ]; then
        echo "deps.sh: missing Mach project in $path" >&2
        exit 1
    fi
done
