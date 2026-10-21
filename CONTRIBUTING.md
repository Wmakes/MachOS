# Contributing to MachOS

Bug reports, focused fixes, hardware testing and documentation improvements are
welcome. For substantial changes, open an issue first so the scope and approach
can be discussed before implementation.

## Getting started

Fork the repository and create a branch from `main`. Clone your fork, then run
`tools/deps.sh` to initialize the pinned dependencies under `deps/` and prepare
the generated compatibility copies. No sibling checkouts are required.

The `machos-std` submodule currently uses a GitHub SSH URL, so fetching it requires
GitHub SSH access. Other upstream dependencies use HTTPS.

Use Mach 6.10.x. On Arch Linux, the host tools can be installed with:

```sh
sudo pacman -S lld gptfdisk e2fsprogs mtools xorriso qemu-system-x86 edk2-ovmf python
```

Build and boot with:

```sh
tools/build.sh debug
tools/run.sh
```

Build products and disk images live under `out/` and component output directories.
Use disposable images for installation, filesystem and recovery work.

## Making changes

- Keep each change focused and follow the surrounding code and comment style.
- Add tests for new behavior and bug fixes, using the existing test facilities.
- Kernel tests belong under `kernel/src/tests/`; serial integration scenarios
  belong in `tools/test.py`.
- Keep dependency revisions pinned. Describe dependency patches explicitly;
  generated files under `deps/.patched/` are not the source of a patch.
- `userland/mach.toml` contains generated program entries. Use
  `tools/programs.sh` when changing that list.
- Keep README edits separate and discuss them with the maintainer first.
- Do not commit build output, disk images, credentials or local editor settings.

## Validation

Run tests for the behavior and paths your change affects. An unrelated change
does not require rerunning every suite. For example, an HTTP client change can
be checked with:

```sh
tools/test.py wget tcp
```

Select release builds when the change requires optimized behavior:

```sh
tools/test.py --profile release wget tcp
```

The runner builds the selected profile and drives QEMU through the serial
console. Logs and JSON results are written under `out/headless/`. Some scenarios
require host network access or additional tools; inspect the selected scenario
when reporting a preparation failure. Running `tools/test.py` without names
selects the full suite and is unnecessary for most contributions.

For documentation-only changes, check formatting, links and command accuracy;
OS runtime tests are unnecessary.

## Pull requests

Target `main`. Explain the problem, the resulting behavior and the scope of the
change. Link its issue when one exists. Include the exact validation commands
and results, plus any known limitations or checks you could not run.

Use short, descriptive commit messages. Do not add assistant credits or generated
co-author trailers. Keep unrelated changes and formatting out of the patch.

## Reporting problems

Use the issue templates. Include the release or commit, reproduction steps,
expected and actual behavior, and the relevant environment. Serial logs are
especially useful for boot failures and panics. State whether you used QEMU or
real hardware and whether the problem reproduces on a fresh image.
