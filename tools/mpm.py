#!/usr/bin/env python3
"""Build local .mpa packages and seed stock ownership records; no network access."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile
import tomllib

NAME = re.compile(r"[a-z][a-z0-9_-]{0,62}\Z")


def version_ok(value):
    if not isinstance(value, str) or len(value) > 127:
        return False
    main, plus, build = value.partition("+")
    if plus and (not build or any(not re.fullmatch(r"[0-9A-Za-z-]+", part) for part in build.split("."))):
        return False
    core, dash, pre = main.partition("-")
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", core):
        return False
    if any(int(part) > 2**64 - 1 for part in core.split(".")):
        return False
    if dash:
        for part in pre.split("."):
            if not re.fullmatch(r"[0-9A-Za-z-]+", part) or (part.isdecimal() and len(part) > 1 and part[0] == "0"):
                return False
    return True


def quote(value):
    if not value.isascii() or any(ord(c) < 32 or c in '\\"' for c in value):
        raise ValueError("package strings must be printable ASCII without quotes or backslashes")
    return json.dumps(value)


def allowed(path):
    return (path.startswith(("/bin/", "/etc/", "/usr/", "/opt/", "/lib/", "/boot/"))
            and not path.startswith("/etc/mpm/") and not path.endswith(".mpm-new")
            and path not in ("/etc/passwd", "/etc/shadow", "/etc/group", "/etc/machos.conf", "/bin/mpm-bootstrap.tar")
            and all(part not in ("", ".", "..") for part in path[1:].split("/")))


def build_manifest(definition, root):
    name, version = definition["name"], definition["version"]
    if not NAME.fullmatch(name) or not version_ok(version):
        raise ValueError("invalid package name or semantic version")
    if definition.get("arch", "x86_64") != "x86_64":
        raise ValueError("unsupported architecture")
    lines = ['format = 1', 'name = ' + quote(name), 'version = ' + quote(version),
             'arch = "x86_64"', 'protected = ' + str(bool(definition.get("protected", False))).lower()]
    for dep in definition.get("dependencies", []):
        if not NAME.fullmatch(dep["name"]) or not version_ok(dep["version"].removeprefix(">=")):
            raise ValueError("invalid dependency")
        lines += ['', '[[dependencies]]', 'name = ' + quote(dep["name"]), 'version = ' + quote(dep["version"])]
    entries = []
    seen = set()
    for entry in definition["files"]:
        path = entry["path"]
        quote(path)
        if not allowed(path) or len("files/" + path[1:]) > 99 or path in seen:
            raise ValueError("unsupported or duplicate package path: " + path)
        if path.startswith("/boot/"):
            owners = {"/boot/kernel.elf": "machos-kernel", "/boot/initrd.tar": "machos-kernel", "/boot/BOOTX64.EFI": "machos-boot"}
            if owners.get(path) != name or not definition.get("protected", False):
                raise ValueError("reserved boot payload: " + path)
        seen.add(path)
        source = root / path[1:]
        # Reject symlinks at every component, including the final file.
        parent = root
        for component in path[1:].split("/"):
            parent /= component
            if parent.is_symlink():
                raise ValueError("symlink in package source: " + path)
        if not source.is_file():
            raise ValueError("not a regular file: " + path)
        data = source.read_bytes()
        mode = entry.get("mode", source.stat().st_mode & 0o7777)
        config = entry.get("config", path.startswith("/etc/"))
        if not isinstance(mode, int) or not 0 <= mode <= 0o7777 or (config and not path.startswith("/etc/")):
            raise ValueError("invalid mode or configuration file")
        lines += ['', '[[files]]', 'path = ' + quote(path), 'size = ' + str(len(data)),
                  'mode = ' + str(mode), 'sha256 = ' + quote(hashlib.sha256(data).hexdigest()),
                  'config = ' + str(bool(config)).lower()]
        entries.append((path, mode, data))
    if not 1 <= len(entries) <= 512 or len(definition.get("dependencies", [])) > 32:
        raise ValueError("package inventory exceeds limits")
    manifest = ('\n'.join(lines) + '\n').encode()
    if len(manifest) > 65536:
        raise ValueError("manifest exceeds 64 KiB")
    return manifest, entries


def archive(output, manifest, entries):
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.USTAR_FORMAT) as tar:
            for name, mode, data in [("manifest.toml", 0o644, manifest)] + [("files/" + p[1:], m, d) for p, m, d in entries]:
                member = tarfile.TarInfo(name)
                member.size, member.mode, member.mtime = len(data), mode, 0
                member.uid = member.gid = 0
                tar.addfile(member, io.BytesIO(data))


def stock(root, version, output, definitions):
    groups = tomllib.loads(definitions.read_text())["package"]
    tools = [tool for group in groups.values() for tool in group["tools"]]
    if len(tools) != len(set(tools)) or set(tools) != {p.name for p in (root / "bin").iterdir() if p.is_file()}:
        raise ValueError("stock package definitions must own every shipped tool exactly once")
    database = root / "var/lib/mpm/packages"
    database.mkdir(parents=True, exist_ok=True)
    (root / "var/lib/mpm/configs").mkdir(parents=True, exist_ok=True)
    for suffix in ("var", "var/lib", "var/lib/mpm", "var/lib/mpm/packages", "var/lib/mpm/configs"):
        (root / suffix).chmod(0o755)
    for name, group in groups.items():
        deps = group.get("dependencies", []) + ([] if name == "machos-base" else ["machos-base"])
        package_version = group.get("version", version)
        tree_files = []
        for tree in group.get("trees", []):
            relative = Path(tree)
            if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
                raise ValueError("invalid package tree: " + tree)
            source = root / relative
            if not source.is_dir():
                raise ValueError("package tree is not a directory: " + tree)
            tree_files += [dict(path="/" + str(path.relative_to(root)), config=False)
                           for path in sorted(source.rglob("*")) if path.is_file()]
        definition = dict(name=name, version=package_version, protected=group.get("protected", False),
                          dependencies=[dict(name=d, version=">=" + version) for d in deps],
                          files=[dict(path="/bin/" + tool, config=False) for tool in group["tools"]]
                          + [dict(path="/etc/" + cfg, config=True) for cfg in group.get("configs", [])]
                          + [dict(path=path, config=False) for path in group.get("boot", []) if (root / path[1:]).is_file()]
                          + tree_files
                          + ([dict(path="/" + str(p.relative_to(root)), config=False)
                              for p in sorted((root / "usr/share/licenses").rglob("*")) if p.is_file()]
                             if group.get("licenses", False) else []))
        manifest, entries = build_manifest(definition, root)
        record = database / (name + ".toml")
        record.write_bytes(manifest)
        record.chmod(0o644)
        archive(output / (name + "-" + package_version + "-x86_64.mpa"), manifest, entries)
        (output / (name + "-" + version + ".mpa")).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pack = commands.add_parser("pack")
    pack.add_argument("--manifest", type=Path, required=True)
    pack.add_argument("--root", type=Path, required=True)
    pack.add_argument("--output", type=Path, required=True)
    pack.add_argument("--key", type=Path, help="optional signing key; Minisign prompts for its passphrase")
    seed = commands.add_parser("stock")
    seed.add_argument("--root", type=Path, required=True)
    seed.add_argument("--version", required=True)
    seed.add_argument("--output", type=Path, required=True)
    seed.add_argument("--definitions", type=Path, default=Path(__file__).resolve().parents[1] / "packages/stock.toml")
    args = parser.parse_args()
    try:
        if args.command == "stock":
            stock(args.root.resolve(), args.version, args.output, args.definitions)
        else:
            if args.output.suffix != ".mpa":
                raise ValueError("output must end in .mpa")
            manifest, entries = build_manifest(tomllib.loads(args.manifest.read_text()), args.root.resolve())
            archive(args.output, manifest, entries)
            if args.key:
                subprocess.run(["minisign", "-S", "-s", str(args.key), "-m", str(args.output),
                                "-x", str(args.output) + ".sig"], check=True)
    except (ValueError, TypeError, KeyError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, "mpm: " + str(error) + "\n")


if __name__ == "__main__":
    main()
