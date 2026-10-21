#!/usr/bin/env python3
"""Prepare documented dependency fixes in generated copies, preserving submodules."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / 'deps/mach-crypto'
DESTINATION = ROOT / 'deps/.patched/mach-crypto'

# Recreate derived sources so removals and local edits in the checkout are reflected.
if DESTINATION.exists():
    shutil.rmtree(DESTINATION)
shutil.copytree(SOURCE, DESTINATION,
                ignore=shutil.ignore_patterns('.git', 'dep', 'out'))
subprocess.run(['git', 'apply', '--directory', str(DESTINATION.relative_to(ROOT)),
                'tools/patches/mach-crypto-pem-76.patch'], cwd=ROOT, check=True)
