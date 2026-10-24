#!/usr/bin/env python3
"""Boot MachOS under QEMU and check it end to end.

usage: tools/test.py [-p release] [NAME...]     (no names: run every test)

Each test boots a fresh disk (or the previous test's disk, for `keep`),
types commands over the serial port, and checks that the output contains
every expected string, in order. Tests marked `fsck` also run e2fsck on the
root partition afterwards.
"""
import argparse, os, re, subprocess, sys, threading, time
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = r"[^\r\n ]+@[^\r\n :]+:[^\r\n]*[$#] "  # shell prompt, not a printed comment
TARGET = os.path.join(ROOT, "out", "headless", "target.img")
BROKEN = os.path.join(ROOT, "out", "headless", "broken.img")
NVME_TARGET = os.path.join(ROOT, "out", "headless", "nvme-target.img")


def sh(cmd):
    return subprocess.run(cmd, shell=True, cwd=ROOT, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True).stdout


def root_state(img):
    """the ext2 'Filesystem state' of partition 2 of img"""
    out = sh(f"sgdisk -i 2 {img}")
    first = int(re.search(r"First sector: (\d+)", out).group(1))
    last = int(re.search(r"Last sector: (\d+)", out).group(1))
    sh(f"dd if={img} of=out/headless/st.img bs=512 skip={first} count={last - first + 1} status=none")
    st = sh("dumpe2fs -h out/headless/st.img 2>/dev/null | grep -i 'state'")
    os.remove(os.path.join(ROOT, "out/headless/st.img"))
    return st


PROFILE = ["debug"]


def disk():
    return os.path.join(ROOT, "out", "headless", "images", PROFILE[0], "disk.img")


def with_posixtest():
    """a fresh dev disk with examples/posixtest on it as /bin/posixtest"""
    p = PROFILE[0]
    sh(f"tools/mkimage.sh {p} --fresh")
    sh(f"cd examples/posixtest && mach dep pull . && mach build . -t machos -p {p}")
    sh(f"tools/put.sh examples/posixtest/out/machos/{p}/posixtest /bin/posixtest {disk()}")


def with_posix_artifact(name):
    """Install one resource-regression program on a fresh test disk."""
    p = PROFILE[0]
    for command in (["tools/mkimage.sh", p, "--fresh"],
                    ["mach", "dep", "pull", "examples/posixtest"],
                    ["mach", "build", "examples/posixtest", "-a", name, "-t", "machos", "-p", p],
                    ["tools/put.sh", f"examples/posixtest/out/machos/{p}/{name}", f"/bin/{name}", disk()]):
        subprocess.run(command, cwd=ROOT, check=True, stdout=subprocess.PIPE)


def with_ramtest():
    with_posix_artifact("ramtest")


def with_smptest():
    with_posix_artifact("smptest")


def with_threadtest():
    with_posix_artifact("threadtest")


def with_iommtest():
    with_posix_artifact("iommtest")


def with_securitytest():
    """Install the boundary-safety regression program and its archive fixtures."""
    import io
    import tarfile
    with_posix_artifact("securitytest")
    fixtures = os.path.join(ROOT, "out", "headless")
    os.makedirs(fixtures, exist_ok=True)
    archive = os.path.join(fixtures, "security-link.tar")
    with tarfile.open(archive, "w", format=tarfile.USTAR_FORMAT) as output:
        entry = tarfile.TarInfo("link/payload")
        entry.size = 7
        output.addfile(entry, io.BytesIO(b"changed"))
    short = os.path.join(fixtures, "security-short.tar")
    with open(short, "wb") as output:
        output.write(bytes(100))
    for path in (archive, short):
        subprocess.run(["tools/put.sh", path, "/bin/" + os.path.basename(path), disk()],
                       cwd=ROOT, check=True, stdout=subprocess.PIPE)


def with_mpm():
    """Signed local archives, dependency graphs and hostile inputs for MPM."""
    import gzip
    import hashlib
    import io
    import shutil
    import tarfile
    import mpm as builder
    subprocess.run(["tools/mkimage.sh", PROFILE[0], "--fresh"], cwd=ROOT, check=True,
                   stdout=subprocess.PIPE)
    target = Path(ROOT) / "out/headless/mpm-fixtures"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    payload = target / "root"
    (payload / "opt/mpm/mounted").mkdir(parents=True)
    (payload / "etc").mkdir()
    key, public = target / "test.key", target / "test.pub"
    subprocess.run(["minisign", "-G", "-W", "-s", str(key), "-p", str(public)],
                   check=True, stdout=subprocess.PIPE)

    def sign(path):
        subprocess.run(["minisign", "-S", "-W", "-s", str(key), "-m", str(path),
                        "-x", str(path) + ".sig", "-t", "mpm test archive"],
                       check=True, stdout=subprocess.PIPE)

    def make(filename, name, version="1.0.0", deps=(), files=None, signed=True):
        if files is None:
            files = {"/opt/mpm/" + name: (name + "\n").encode()}
        specs = []
        for namepath, contents in files.items():
            source = payload / namepath[1:]
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(contents)
            specs.append(dict(path=namepath, mode=0o644, config=namepath.startswith("/etc/")))
        definition = dict(name=name, version=version,
                          dependencies=[dict(name=d, version=v) for d, v in deps], files=specs)
        manifest, entries = builder.build_manifest(definition, payload)
        path = target / filename
        builder.archive(path, manifest, entries)
        if signed:
            sign(path)
        return path

    make("helper.mpa", "mpm-helper")
    make("demo.mpa", "mpm-demo", deps=[("mpm-helper", ">=1.0.0")],
         files={"/opt/mpm/demo": bytes(range(256)) * 4096,
                "/etc/mpm-demo.conf": b"setting default\n"})
    make("upgrade.mpa", "mpm-demo", "1.1.0", deps=[("mpm-helper", ">=1.0.0")],
         files={"/opt/mpm/demo": b"upgraded\n", "/etc/mpm-demo.conf": b"setting upgraded\n"})
    make("unsigned.mpa", "mpm-unsigned", signed=False)
    make("conflict.mpa", "mpm-conflict", files={"/opt/mpm/conflict": b"replacement\n"})
    make("ownership.mpa", "mpm-ownership", files={"/opt/mpm/demo": b"takeover\n"})
    make("cycle-a.mpa", "mpm-cycle-a", deps=[("mpm-cycle-b", ">=1.0.0")])
    make("cycle-b.mpa", "mpm-cycle-b", deps=[("mpm-cycle-a", ">=1.0.0")])
    make("missing.mpa", "mpm-missing", deps=[("mpm-does-not-exist", ">=1.0.0")])
    make("rollback.mpa", "mpm-rollback", files={"/opt/mpm/rollback": b"after\n",
         "/opt/mpm/mounted/second": b"cannot cross filesystems\n"})
    good = (target / "helper.mpa").read_bytes()
    bad = target / "truncated.mpa"
    bad.write_bytes(good[:-8]); sign(bad)
    tampered = target / "tampered.mpa"
    changed = bytearray(good); changed[len(changed) // 2] ^= 1
    tampered.write_bytes(changed)
    (target / "tampered.mpa.sig").write_bytes((target / "helper.mpa.sig").read_bytes())
    comment = target / "comment.mpa"
    comment.write_bytes(good)
    (target / "comment.mpa.sig").write_bytes((target / "helper.mpa.sig").read_bytes().replace(b"mpm test archive", b"mpm fake archive"))
    # The signer is trusted, but these tar inventories must still be rejected.
    plain = gzip.decompress(good)
    source_entries = []
    with tarfile.open(fileobj=io.BytesIO(plain), mode="r:") as tar:
        for member in tar:
            source_entries.append((member, tar.extractfile(member).read()))
    for filename, kind in (("bootstrap.mpa", "bootstrap"), ("traversal.mpa", "traversal"), ("duplicate.mpa", "duplicate"), ("link.mpa", "link")):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as tar:
            for member, contents in source_entries:
                cloned = tarfile.TarInfo(member.name)
                cloned.mode = member.mode
                if kind == "bootstrap":
                    if member.name == "manifest.toml":
                        contents = contents.replace(b"/opt/mpm/mpm-helper", b"/bin/mpm-bootstrap.tar")
                    else:
                        cloned.name = "files/bin/mpm-bootstrap.tar"
                if kind == "traversal" and member.name != "manifest.toml":
                    cloned.name = "files/../../etc/evil"
                if kind == "link" and member.name != "manifest.toml":
                    cloned.type, cloned.linkname = tarfile.SYMTYPE, "/etc/shadow"
                    cloned.size = 0
                    tar.addfile(cloned)
                else:
                    cloned.size = len(contents)
                    tar.addfile(cloned, io.BytesIO(contents))
                    if kind == "duplicate" and member.name != "manifest.toml":
                        tar.addfile(cloned, io.BytesIO(contents))
        bad = target / filename
        bad.write_bytes(gzip.compress(raw.getvalue(), mtime=0)); sign(bad)
    # A valid signature from a key that is not in the guest's trust store.
    other = target / "other.key"
    subprocess.run(["minisign", "-G", "-W", "-s", str(other), "-p", str(target / "other.pub")],
                   check=True, stdout=subprocess.PIPE)
    unknown = target / "unknown.mpa"; unknown.write_bytes(good)
    subprocess.run(["minisign", "-S", "-W", "-s", str(other), "-m", str(unknown), "-x", str(unknown) + ".sig"],
                   check=True, stdout=subprocess.PIPE)
    key.unlink(); other.unlink()
    (target / "recover.journal").write_bytes(b"10\t/opt/mpm/rollback\n")
    bundle = target / "mpm-fixtures.tar"
    with tarfile.open(bundle, "w", format=tarfile.USTAR_FORMAT) as tar:
        for path in sorted(target.iterdir()):
            if path.suffix in (".mpa", ".sig") or path.name in ("test.pub", "recover.journal"):
                tar.add(path, arcname=path.name, recursive=False)
    subprocess.run(["tools/put.sh", str(bundle), "/bin/mpm-fixtures.tar", disk()],
                   cwd=ROOT, check=True, stdout=subprocess.PIPE)


def with_mpm_repository(system=False):
    """A private HTTPS repository with signed snapshots and real .mpa archives."""
    import datetime, hashlib, json, shutil
    import mpm as builder
    with_mpm()
    https_server()
    base = Path(ROOT) / "out/headless/https/mpm"
    shutil.rmtree(base, ignore_errors=True)
    assets = base / "assets"
    assets.mkdir(parents=True)
    payload = base / "payload"
    payload.mkdir()
    key, public = base / "test.key", base / "test.pub"
    subprocess.run(["minisign", "-G", "-W", "-s", str(key), "-p", str(public)], check=True, stdout=subprocess.DEVNULL)
    def sign(path):
        subprocess.run(["minisign", "-S", "-W", "-s", str(key), "-m", str(path), "-x", str(path) + ".sig", "-t", "temporary repository fixture"], check=True, stdout=subprocess.DEVNULL)
    records = []
    def package(name, version, files, deps=(), protected=False):
        specs = []
        for path, data in files.items():
            source = payload / path[1:]
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(data)
            specs.append(dict(path=path, mode=0o644, config=path.startswith("/etc/")))
        definition = dict(name=name, version=version, protected=protected, files=specs, dependencies=[dict(name=n, version=v) for n,v in deps])
        manifest, entries = builder.build_manifest(definition, payload)
        filename = name + "-" + version + "-x86_64.mpa"
        path = assets / filename
        builder.archive(path, manifest, entries)
        sign(path)
        record = dict(name=name, version=version, arch="x86_64", description="Repository fixture " + name, filename=filename,
                      size=path.stat().st_size, installed_size=sum(len(d) for d in files.values()), sha256=hashlib.sha256(path.read_bytes()).hexdigest(), protected=protected,
                      url="https://tls.test:9443/mpm/redirect/" + filename, signature_url="https://tls.test:9443/mpm/assets/" + filename + ".sig", dependencies=[n + " " + v for n,v in deps])
        records.append(record)
        return path
    import tarfile, tomllib
    def stock(path, target_version=None, kernel=None):
        with tarfile.open(path, "r:gz") as tar:
            meta = tomllib.loads(tar.extractfile("manifest.toml").read().decode())
            entries = [(item["path"], item["mode"], tar.extractfile("files/" + item["path"][1:]).read(), item.get("config", False)) for item in meta["files"]]
        if target_version:
            meta["version"] = target_version
            for dep in meta.get("dependencies", []):
                dep["version"] = ">=" + target_version
            for dest, mode, data, config_file in entries:
                source = payload / dest[1:]
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(kernel if dest == "/boot/kernel.elf" else data)
            definition = dict(name=meta["name"], version=target_version, protected=meta["protected"], dependencies=meta.get("dependencies", []),
                              files=[dict(path=dest, mode=mode, config=configuration) for dest,mode,_,configuration in entries])
            manifest, members = builder.build_manifest(definition, payload)
            filename = meta["name"] + "-" + target_version + "-x86_64.mpa"
            target = assets / filename
            builder.archive(target, manifest, members)
        else:
            filename = path.name
            target = assets / filename
            shutil.copyfile(path, target)
        sign(target)
        records.append(dict(name=meta["name"], version=meta["version"], arch="x86_64", description="Stock package " + meta["name"], filename=filename,
                            size=target.stat().st_size, installed_size=sum(len(data) if dest != "/boot/kernel.elf" or kernel is None else len(kernel) for dest,_,data,_ in entries),
                            sha256=hashlib.sha256(target.read_bytes()).hexdigest(), protected=meta["protected"], url="https://tls.test:9443/mpm/redirect/" + filename,
                            signature_url="https://tls.test:9443/mpm/assets/" + filename + ".sig", dependencies=[dep["name"] + " " + dep["version"] for dep in meta.get("dependencies", [])]))
    stock_paths = sorted((Path(ROOT) / "out" / PROFILE[0] / "packages").glob("*-x86_64.mpa"))
    for path in stock_paths:
        stock(path)
    package("mpm-helper", "1.0.0", {"/opt/mpm/mpm-helper": b"dependency fetched\n"})
    package("mpm-demo", "1.0.0", {"/opt/mpm/demo": b"network install\n", "/etc/mpm-demo.conf": b"setting first\n"}, [("mpm-helper", ">=1.0.0")])
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    def encoded(lines):
        return ("\n".join(lines) + "\n").encode()
    def fields(values):
        return [k + " = " + (str(v).lower() if isinstance(v, bool) else str(v) if isinstance(v, int) else json.dumps(v)) for k,v in values.items()]
    previous = "0" * 64
    def snapshot(name, revision, expired=False, bad_index=False):
        nonlocal previous
        start = now - datetime.timedelta(days=32) if expired else now
        end = now - datetime.timedelta(days=2) if expired else now + datetime.timedelta(days=30)
        header = dict(format=1, channel="stable", published=start.strftime("%Y-%m-%dT%H:%M:%SZ"))
        index = fields(dict(**header, os_version="0.9.3", arch="x86_64", revision=revision, system=["machos-base", "machos-kernel", "machos-boot"]))
        for record in records:
            index += ["", "[[packages]]"] + fields(record)
        index_path = assets / ("index-r" + str(revision) + ".toml")
        index_path.write_bytes(encoded(index));sign(index_path)
        if bad_index:
            sig = Path(str(index_path) + ".sig")
            sig.write_text(sig.read_text().replace("temporary repository fixture", "tampered trusted comment"))
        stable = fields(dict(**header, expires=end.strftime("%Y-%m-%dT%H:%M:%SZ"), revision=revision, previous_sha256=previous, latest_os="0.9.3"))
        stable += ["", "[[releases]]"] + fields(dict(os_version="0.9.3", revision=revision, size=index_path.stat().st_size, sha256=hashlib.sha256(index_path.read_bytes()).hexdigest(),
                                                    url="https://tls.test:9443/mpm/assets/" + index_path.name, signature_url="https://tls.test:9443/mpm/assets/" + index_path.name + ".sig"))
        dest = base / "snapshots" / name
        dest.mkdir(parents=True)
        (dest / "stable.toml").write_bytes(encoded(stable));sign(dest / "stable.toml")
        if name in ("initial", "upgrade"):
            previous = hashlib.sha256((dest / "stable.toml").read_bytes()).hexdigest()
    snapshot("initial", 1)
    package("mpm-demo", "1.1.0", {"/opt/mpm/demo": b"network upgrade\n", "/etc/mpm-demo.conf": b"setting upgraded\n"}, [("mpm-helper", ">=1.0.0")])
    snapshot("upgrade", 2)
    snapshot("bad-index", 3, bad_index=True)
    snapshot("expired", 4, expired=True)
    bad = package("mpm-demo", "2.0.0", {"/opt/mpm/demo": b"signed before tampering\n", "/etc/mpm-demo.conf": b"setting malicious\n"}, [("mpm-helper", ">=1.0.0")])
    snapshot("bad-package", 5)
    bad.write_bytes(bad.read_bytes() + b"tampered")
    if system:
        project = Path(ROOT) / "out/headless/system-kernel"
        project.mkdir(parents=True, exist_ok=True)
        manifest = (Path(ROOT) / "kernel/mach.toml").read_text().replace('version = "0.9.3"', 'version = "0.9.4"', 1)
        shutil.copytree(Path(ROOT) / "kernel/src", project / "src", dirs_exist_ok=True)
        manifest = manifest.replace('path = "../sdk"', 'path = ' + json.dumps(str(Path(ROOT) / "sdk")))
        (project / "mach.toml").write_text(manifest)
        subprocess.run(["mach", "dep", "pull", str(project)], cwd=ROOT, check=True, stdout=subprocess.PIPE)
        subprocess.run(["mach", "build", str(project), "-a", "kernel", "-t", "kernel", "-p", PROFILE[0]], cwd=ROOT, check=True)
        kernel = (project / "out/kernel" / PROFILE[0] / "kernel.elf").read_bytes()
        # A different kernel version is confined to this ignored test project.
        old_records = records[:]
        records.clear()
        for path in stock_paths:
            stock(path, "0.9.4", kernel)
        header = dict(format=1, channel="stable", published=now.strftime("%Y-%m-%dT%H:%M:%SZ"))
        index = fields(dict(**header, os_version="0.9.4", arch="x86_64", revision=1, system=["machos-base", "machos-kernel", "machos-boot"]))
        for record in records:
            index += ["", "[[packages]]"] + fields(record)
        path = assets / "index-system-r1.toml"
        path.write_bytes(encoded(index));sign(path)
        current = __import__("tomllib").loads((base / "snapshots/upgrade/stable.toml").read_text())["releases"][0]
        latest = dict(os_version="0.9.4", revision=1, size=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                      url="https://tls.test:9443/mpm/assets/" + path.name, signature_url="https://tls.test:9443/mpm/assets/" + path.name + ".sig")
        stable = fields(dict(**header, expires=(now + datetime.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"), revision=3, previous_sha256=previous, latest_os="0.9.4"))
        for record in (current, latest):
            stable += ["", "[[releases]]"] + fields(record)
        target = base / "snapshots/system"
        target.mkdir()
        (target / "stable.toml").write_bytes(encoded(stable));sign(target / "stable.toml")
        records[:] = old_records
    key.unlink()
    (base / "active").write_text("initial")
    subprocess.run(["tools/put.sh", str(public), "/root/repository.pub", disk()], cwd=ROOT, check=True, stdout=subprocess.PIPE)


def check_truecolor():
    from PIL import Image
    with Image.open(os.path.join(ROOT, "out/headless/truecolor.png")) as image:
        image = image.convert("RGB")
        ink = set(image.crop((0, 0, 24, 16)).getdata())
        if ink != {(18, 52, 86), (101, 67, 33)}:
            return "RGB glyph foreground/background differ from requested colours"
        for x, colour in ((0, (17, 34, 51)), (32, (255, 0, 0)), (64, (128, 128, 128))):
            if set(image.crop((x, 32, x + 32, 48)).getdata()) != {colour}:
                return "RGB/indexed background pixels differ from requested colours"
    return ""


def with_usability():
    """Install the command/configuration assertions on a fresh test disk."""
    subprocess.run(["tools/mkimage.sh", PROFILE[0], "--fresh"], cwd=ROOT, check=True,
                   stdout=subprocess.PIPE)
    subprocess.run(["tools/put.sh", "tools/fixtures/usability.sh", "/bin/usability-test", disk()],
                   cwd=ROOT, check=True, stdout=subprocess.PIPE)


def with_stdtest():
    """a fresh dev disk with examples/stdtest (mach's std on MachOS) on it
    as /bin/stdtest"""
    p = PROFILE[0]
    sh(f"tools/mkimage.sh {p} --fresh")
    sh(f"cd examples/stdtest && mach dep pull . && mach build . -t machos -p {p}")
    sh(f"tools/put.sh examples/stdtest/out/machos/{p}/stdtest /bin/stdtest {disk()}")


STD_SUITE = []


def with_stdsuite():
    """Build std's supported module tests and keep their index/name mapping."""
    p = PROFILE[0]
    subprocess.run(["tools/mkimage.sh", p, "--fresh"], cwd=ROOT, check=True,
                   stdout=subprocess.PIPE)
    project = os.path.join(ROOT, "examples", "stdtest")
    subprocess.run(["mach", "dep", "pull", "."], cwd=project, check=True)
    subprocess.run(["mach", "build", ".", "-a", "runtests", "-t", "machos", "-p", p],
                   cwd=project, check=True)
    base = ["mach", "test", ".", "-a", "stdsuite", "-t", "machos", "-p", p, "--include-deps"]
    listing = subprocess.run(base + ["--list"], cwd=project, check=True,
                             stdout=subprocess.PIPE, text=True).stdout
    STD_SUITE[:] = [line.split()[0] for line in listing.splitlines() if "#" in line]
    if not STD_SUITE:
        raise RuntimeError("std test dispatcher contains no tests")
    output = os.path.join(project, "out", "machos", p, "stdsuite-dispatcher")
    if os.path.isfile(output):
        os.remove(output)
    built = subprocess.run(base + ["-o", os.path.join("out", "machos", p, "stdsuite-dispatcher")], cwd=project, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    # a cross-built dispatcher is usable even though mach test cannot run it on the host.
    if built.returncode not in (0, 1) or not os.path.isfile(output):
        raise RuntimeError(built.stdout)
    for src, dest in ((output, "/bin/stdsuite"),
                      (os.path.join(project, "out", "machos", p, "runtests"), "/bin/runtests")):
        subprocess.run(["tools/put.sh", src, dest, disk()], cwd=ROOT, check=True)
    os.makedirs(os.path.join(ROOT, "out", "headless"), exist_ok=True)
    import shutil
    fixtures = os.path.join(ROOT, "out", "headless", "std-fixtures")
    shutil.rmtree(fixtures, ignore_errors=True)
    for platform in ("darwin", "windows"):
        shutil.copytree(os.path.join(ROOT, "deps", "machos-std", "src", "system", "os", platform),
                        os.path.join(fixtures, "src", "system", "os", platform))
    put_test_tree(fixtures, "/home/stdtest")
    with open(os.path.join(ROOT, "out", "headless", "stdtest-tests.txt"), "w") as f:
        f.write("".join(f"{i} {name}\n" for i, name in enumerate(STD_SUITE)))


TLS_SUITE = []

def tls_test_project():
    """Prepare the patched test copy while leaving dependency submodules pristine."""
    # Apply the documented upstream test correction in an isolated source copy.
    # The submodule checkouts retain the exact shipped upstream revisions.
    import shutil
    original = os.path.join(ROOT, "examples", "tlstest")
    project = os.path.join(ROOT, "out", "headless", "tlstest-project")
    os.makedirs(project, exist_ok=True)
    tls_source = os.path.join(project, "sources", "tls")
    shutil.rmtree(tls_source, ignore_errors=True)
    shutil.copytree(os.path.join(ROOT, "deps", "mach-tls"), tls_source,
                    ignore=shutil.ignore_patterns(".git", "dep", "out"))
    subprocess.run(["git", "apply", "--directory", os.path.relpath(tls_source, ROOT),
                    "tools/patches/mach-tls-pki-0.4-tests.patch"], cwd=ROOT, check=True)
    with open(os.path.join(original, "mach.toml")) as source:
        manifest = source.read()
    for name in ("std", "crypto", "pki", "tls", "http"):
        repository = "machos-std" if name == "std" else "mach-" + name
        path = tls_source if name == "tls" else os.path.join(ROOT, "deps", repository)
        manifest, changed = re.subn(r'\[dep\.' + name + r'\]\n(?:(?!\[)[^\n]*\n)*',
                                   '[dep.' + name + ']\npath = "' + path + '"\n\n',
                                   manifest, count=1)
        if changed != 1:
            raise RuntimeError("TLS test project has no " + name + " dependency")
    with open(os.path.join(project, "mach.toml"), "w") as output:
        output.write(manifest)
    shutil.copytree(os.path.join(original, "src"), os.path.join(project, "src"), dirs_exist_ok=True)
    return project


def with_tlssuite():
    """Build upstream crypto, PKI, TLS and HTTP tests and keep their index/name mapping."""
    p = PROFILE[0]
    subprocess.run(["tools/mkimage.sh", p, "--fresh"], cwd=ROOT, check=True,
                   stdout=subprocess.PIPE)
    project = os.path.join(ROOT, "examples", "tlstest")
    subprocess.run(["mach", "dep", "pull", "."], cwd=project, check=True)
    project = tls_test_project()
    subprocess.run(["mach", "dep", "pull", "."], cwd=project, check=True)
    subprocess.run(["mach", "build", "examples/stdtest", "-a", "runtests", "-t", "machos", "-p", p],
                   cwd=ROOT, check=True)
    base = ["mach", "test", ".", "-a", "tlstest", "-t", "machos", "-p", p, "--include-deps"]
    listing = subprocess.run(base + ["--list"], cwd=project, check=True,
                             stdout=subprocess.PIPE, text=True).stdout
    TLS_SUITE[:] = [line.split()[0] for line in listing.splitlines() if "#" in line]
    if not TLS_SUITE:
        raise RuntimeError("std test dispatcher contains no tests")
    output = os.path.join(project, "out", "machos", p, "tlstest-dispatcher")
    if os.path.isfile(output):
        os.remove(output)
    built = subprocess.run(base + ["-o", os.path.join("out", "machos", p, "tlstest-dispatcher")], cwd=project, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    # a cross-built dispatcher is usable even though mach test cannot run it on the host.
    if built.returncode not in (0, 1) or not os.path.isfile(output):
        raise RuntimeError(built.stdout)
    for src, dest in ((output, "/bin/tlstest"),
                      (os.path.join(ROOT, "examples", "stdtest", "out", "machos", p, "runtests"), "/bin/runtests")):
        subprocess.run(["tools/put.sh", src, dest, disk()], cwd=ROOT, check=True)
    os.makedirs(os.path.join(ROOT, "out", "headless"), exist_ok=True)
    put_test_tree(os.path.join(project, "dep", "tls", "test", "fixtures", "bundles"),
                  "/root/test/fixtures/bundles")
    with open(os.path.join(ROOT, "out", "headless", "tlstest-tests.txt"), "w") as f:
        f.write("".join(f"{i} {name}\n" for i, name in enumerate(TLS_SUITE)))


def put_test_tree(source, destination):
    """Copy a fixture tree into the offline guest root filesystem."""
    import tempfile
    info = subprocess.check_output(["sgdisk", "-i", "2", disk()], text=True)
    first = int(re.search(r"First sector: (\d+)", info)[1])
    last = int(re.search(r"Last sector: (\d+)", info)[1])
    with tempfile.TemporaryDirectory(dir=os.path.join(ROOT, "out")) as temp:
        part = os.path.join(temp, "root.img")
        subprocess.run(["dd", "if=" + disk(), "of=" + part, "bs=512",
                        f"skip={first}", f"count={last - first + 1}", "status=none"], check=True)
        commands = []
        planned = set()

        def ensure_dir(path):
            if path in planned:
                return
            stat = subprocess.run(["debugfs", "-R", "stat " + path, part],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True).stdout
            if "Inode:" not in stat:
                commands.append('mkdir "' + path + '"')
            planned.add(path)

        components = destination.strip("/").split("/")
        for i in range(len(components)):
            ensure_dir('/' + '/'.join(components[:i + 1]))
        for directory, dirs, files in os.walk(source):
            relative = os.path.relpath(directory, source)
            target = destination if relative == "." else destination + "/" + relative
            for child in dirs:
                ensure_dir(target + "/" + child)
            for child in files:
                source_file = os.path.join(directory, child)
                if os.path.islink(source_file):
                    commands.append(f'symlink "{target}/{child}" "{os.readlink(source_file)}"')
                else:
                    commands.append(f'write "{source_file}" "{target}/{child}"')
        script = os.path.join(temp, "fixtures.cmds")
        with open(script, "w") as output:
            output.write("\n".join(commands) + "\n")
        subprocess.run(["debugfs", "-w", "-f", script, part], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["dd", "if=" + part, "of=" + disk(), "bs=512",
                        f"seek={first}", "conv=notrunc", "status=none"], check=True)


def tlssuite_steps():
    steps = ["wait:" + P]
    selected = [i for i, name in enumerate(TLS_SUITE)
                if name.startswith(("crypto.", "pki.", "tls.", "http."))]
    groups = []
    for i in selected:
        if groups and i == groups[-1][-1] + 1:
            groups[-1].append(i)
        else:
            groups.append([i])
    for group in groups:
        steps.append(f"send:runtests /bin/tlstest {group[0]} {group[-1]}\\n")
        for i in group:
            steps.append(rf"wait:(?:ok {i}|HANG {i}|FAIL {i} [^\n]*)\r?\n")
        steps += ["wait:runtests:", "wait:" + P]
    return steps


def stdsuite_steps():
    # Permission regressions must run as an ordinary owner, not uid 0.
    steps = ["wait:" + P, "send:adduser stdtest\\n", "wait:New password: ", "send:stdtest\\n", "wait:Again: ", "send:stdtest\\n", "wait:" + P,
             "send:su stdtest\\n", r"wait:stdtest@[^\r\n :]+:[^\r\n]*[$] ",
             f"send:cd /home/stdtest; runtests /bin/stdsuite 0 {len(STD_SUITE) - 1}\\n"]
    for i in range(len(STD_SUITE)):
        steps.append(rf"wait:(?:ok {i}|HANG {i}|FAIL {i} [^\n]*)\r?\n")
    return steps + ["wait:runtests:", "wait:" + P]



def with_kernel_sockets():
    """Run the kernel's socket syscall regressions using the standard dispatcher runner."""
    p = PROFILE[0]
    subprocess.run(["tools/mkimage.sh", p, "--fresh"], cwd=ROOT, check=True,
                   stdout=subprocess.PIPE)
    subprocess.run(["mach", "dep", "pull", "kernel"], cwd=ROOT, check=True)
    output = os.path.join(ROOT, "kernel", "out", "machos", p, "socket-tests-dispatcher")
    if os.path.isfile(output):
        os.remove(output)
    subprocess.run(["mach", "test", "kernel", "-a", "socket-tests", "-t", "machos", "-p", p,
                    "-o", os.path.join("out", "machos", p, "socket-tests-dispatcher")], cwd=ROOT)
    if not os.path.isfile(output):
        raise RuntimeError("socket test dispatcher was not built")
    project = os.path.join(ROOT, "examples", "stdtest")
    subprocess.run(["mach", "dep", "pull", "."], cwd=project, check=True)
    subprocess.run(["mach", "build", ".", "-a", "runtests", "-t", "machos", "-p", p], cwd=project, check=True)
    for src, dest in ((output, "/bin/socket-tests"),
                      (os.path.join(project, "out", "machos", p, "runtests"), "/bin/runtests")):
        subprocess.run(["tools/put.sh", src, dest, disk()], cwd=ROOT, check=True)
    # debugfs creates a standard ext2 fast symlink, unlike the kernel's block-backed links.
    import tempfile
    with tempfile.TemporaryDirectory(prefix="machos-symlinks-") as fixture:
        os.symlink("/etc/motd", os.path.join(fixture, "fast-link"))
        put_test_tree(fixture, "/root")


UDP_ECHO = []


def udp_echo():
    """a fresh dev disk, and a UDP echo server on the host's port 7777 (QEMU
    passes 10.0.2.2 to the host): it answers "echo: " and what it got"""
    import socket as pysocket
    sh(f"tools/mkimage.sh {PROFILE[0]} --fresh")
    if UDP_ECHO:
        return
    srv = pysocket.socket(pysocket.AF_INET, pysocket.SOCK_DGRAM)
    srv.setsockopt(pysocket.SOL_SOCKET, pysocket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 7777))
    UDP_ECHO.append(srv)

    def serve():
        while True:
            data, peer = srv.recvfrom(2048)
            srv.sendto(b"echo: " + data, peer)
    threading.Thread(target=serve, daemon=True).start()


HTTP_SERVER = []


def http_server():
    """a fresh dev disk, and a web server on the host's port 8080 (10.0.2.2
    for MachOS) serving out/headless/www: hello.txt and big.txt (the
    numbers 1 to 150000, a line each)"""
    import http.server, functools
    sh(f"tools/mkimage.sh {PROFILE[0]} --fresh")
    www = os.path.join(ROOT, "out", "headless", "www")
    os.makedirs(www, exist_ok=True)
    with open(os.path.join(www, "hello.txt"), "w") as f:
        f.write("hello over tcp\n")
    os.makedirs(os.path.join(www, "sub"), exist_ok=True)
    with open(os.path.join(www, "sub", "index.html"), "w") as f:
        f.write("behind a redirect\n")
    with open(os.path.join(www, "big.txt"), "w") as f:
        f.write("".join(f"{i}\n" for i in range(1, 150001)))
    if any(server.server_address[1] == 8080 for server in HTTP_SERVER):
        return
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=www)
    handler.log_message = lambda *a: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 8080), handler)
    HTTP_SERVER.append(srv)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


DNS_COUNTS = {}
DNS_SERVER = []


def dns_fixture():
    """Drop the first A query, then answer through two CNAMEs deterministically."""
    import socket, struct
    DNS_COUNTS.clear()
    if DNS_SERVER:
        return
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 5533))
    DNS_SERVER.append(server)

    def name(text):
        return b"".join(bytes([len(part)]) + part.encode("ascii") for part in text.split(".")) + b"\x00"

    def serve():
        while True:
            packet, peer = server.recvfrom(4096)
            if len(packet) < 12:
                continue
            at, labels = 12, []
            while at < len(packet) and packet[at]:
                size = packet[at]
                labels.append(packet[at + 1:at + 1 + size].decode("ascii"))
                at += size + 1
            at += 1
            if at + 4 > len(packet):
                continue
            query = ".".join(labels).lower()
            kind = struct.unpack("!H", packet[at:at + 2])[0]
            key = (query, kind)
            DNS_COUNTS[key] = DNS_COUNTS.get(key, 0) + 1
            if kind == 1 and DNS_COUNTS[key] == 1:
                continue
            answers = b""
            count = 0
            if query == "alias.test" and kind == 1:
                for owner, target in (("alias.test", "middle.test"), ("middle.test", "tls.test")):
                    data = name(target)
                    answers += name(owner) + struct.pack("!HHIH", 5, 1, 60, len(data)) + data
                answers += name("tls.test") + struct.pack("!HHIH", 1, 1, 60, 4) + bytes([10, 0, 2, 2])
                count = 3
            response = packet[:2] + struct.pack("!HHHHH", 0x8180, 1, count, 0, 0)
            server.sendto(response + packet[12:at + 4] + answers, peer)

    threading.Thread(target=serve, daemon=True).start()


def check_dns_retry():
    if DNS_COUNTS.get(("alias.test", 1), 0) < 2:
        return "wget did not retry the dropped DNS query"
    return ""


def https_server():
    """A fresh private CA and TLS server; no trust bypass is used in the guest."""
    import datetime, ssl, http.server
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, rsa
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
    # Each adapter scenario gets its own CA; release the previous TLS fixtures.
    for server in list(HTTP_SERVER):
        if server.server_address[1] in (9080, 9443, 9444, 9445):
            server.shutdown()
            server.server_close()
            HTTP_SERVER.remove(server)
    subprocess.run(["tools/mkimage.sh", PROFILE[0], "--fresh"], cwd=ROOT, check=True,
                   stdout=subprocess.PIPE)
    directory = os.path.join(ROOT, "out", "headless", "https")
    os.makedirs(directory, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MachOS test CA")])
    ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
          .public_key(key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - datetime.timedelta(days=1))
          .not_valid_after(now + datetime.timedelta(days=30))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
          .sign(key, hashes.SHA256()))
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf_builder = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "tls.test")]))
            .issuer_name(name).public_key(leaf_key.public_key())
            .serial_number(x509.random_serial_number())
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("tls.test"), x509.DNSName("alias.test"),
                                                       x509.IPAddress(__import__("ipaddress").ip_address("10.0.2.2"))]), critical=False)
            .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            )
    leaf = (leaf_builder.not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=7)).sign(key, hashes.SHA256()))
    expired = (leaf_builder.not_valid_before(now - datetime.timedelta(days=10))
               .not_valid_after(now - datetime.timedelta(days=1)).sign(key, hashes.SHA256()))
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rsa_leaf = (x509.CertificateBuilder().subject_name(leaf.subject).issuer_name(name)
                .public_key(rsa_key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=7))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.SubjectAlternativeName([x509.DNSName("tls.test")]), critical=False)
                .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .sign(key, hashes.SHA256()))
    for filename, data in (("ca.pem", ca.public_bytes(serialization.Encoding.PEM)),
                           ("leaf.pem", leaf.public_bytes(serialization.Encoding.PEM)),
                           ("expired.pem", expired.public_bytes(serialization.Encoding.PEM)),
                           ("rsa.pem", rsa_leaf.public_bytes(serialization.Encoding.PEM)),
                           ("rsa-key.pem", rsa_key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8, serialization.NoEncryption())),
                           ("key.pem", leaf_key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))):
        with open(os.path.join(directory, filename), "wb") as output:
            output.write(data)
    subprocess.run(["tools/put.sh", os.path.join(directory, "ca.pem"), "/root/test-ca.pem", disk()],
                   cwd=ROOT, check=True)

    # Exercise the real Mozilla bundle and its 76-column PEM in guest updates.
    with open(os.path.join(ROOT, "root/etc/ssl/certs/ca-certificates.crt"), "rb") as source:
        provider_bundle = source.read()
    raw_ca = ca.public_bytes(serialization.Encoding.PEM).splitlines()
    encoded_ca = b"".join(raw_ca[1:-1])
    provider_bundle += (raw_ca[0] + b"\n" +
                        b"\n".join(encoded_ca[i:i + 76] for i in range(0, len(encoded_ca), 76)) +
                        b"\n" + raw_ca[-1] + b"\n")

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            body = b"hello over verified TLS\n"
            if self.path.startswith("/mpm/"):
                self.serve_mpm()
                return
            if self.path == "/bundle":
                body = provider_bundle
            if self.path == "/big":
                body = b"0123456789abcdef" * 65536
            if self.path in ("/truncated", "/no-notify"):
                self.send_response(200)
                self.send_header("Content-Length", "1048576" if self.path == "/truncated" else str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(b"cut short" if self.path == "/truncated" else body)
                self.wfile.flush()
                os.close(self.connection.detach())
                self.close_connection = True
                return
            if self.path == "/chunked":
                self.send_response(200)
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(b"6;part=one\r\nhello \r\n8\r\nchunked!\r\n0\r\nX-Test: yes\r\n\r\n")
            else:
                if self.path in ("/redirect-http", "/redirect-tls"):
                    self.send_response(302)
                    self.send_header("Location", "http://tls.test:9080/plain" if self.path == "/redirect-http" else "/")
                    body = b""
                else:
                    self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
            self.wfile.flush()
            self.close_connection = True
            try:
                self.connection.settimeout(5)
                self.connection.unwrap()
            except (OSError, ssl.SSLError):
                pass

        def serve_mpm(self):
            base = Path(directory) / "mpm"
            if self.path.startswith("/mpm/redirect/"):
                filename = self.path.removeprefix("/mpm/redirect/")
                self.send_response(302)
                self.send_header("Location", "https://tls.test:9443/mpm/assets/" + filename + "?token=" + "x" * 2048)
                # Release redirects carry large security-policy headers.
                self.send_header("Content-Security-Policy", "x" * 9216)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.flush()
                self.close_connection = True
                try:
                    self.connection.settimeout(5)
                    self.connection.unwrap()
                except (OSError, ssl.SSLError):
                    pass
                return
            control = self.path.removeprefix("/mpm/control/")
            if self.path.startswith("/mpm/control/") and control in ("initial", "upgrade", "bad-index", "expired", "bad-package", "system"):
                (base / "active").write_text(control)
                data = b"repository snapshot selected\n"
            else:
                relative = self.path.removeprefix("/mpm/").split("?", 1)[0]
                if relative in ("stable.toml", "stable.toml.sig"):
                    relative = "snapshots/" + (base / "active").read_text() + "/" + relative
                path = base / relative
                if ".." in relative.split("/") or not path.is_file():
                    self.send_error(404)
                    return
                data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()
            self.close_connection = True
            try:
                self.connection.settimeout(5)
                self.connection.unwrap()
            except (OSError, ssl.SSLError):
                pass

        def log_message(self, *args):
            pass

    def start(port, certificate, handler, private_key="key.pem"):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.set_alpn_protocols(["http/1.1"])
        context.load_cert_chain(os.path.join(directory, certificate), os.path.join(directory, private_key))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        HTTP_SERVER.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()

    subprocess.run(["mach", "build", "examples/tlstest", "-a", "dns-proxy", "-t", "machos",
                    "-p", PROFILE[0]], cwd=ROOT, check=True)
    proxy = os.path.join(ROOT, "examples", "tlstest", "out", "machos", PROFILE[0], "dns-proxy")
    subprocess.run(["tools/put.sh", proxy, "/bin/dns-proxy", disk()], cwd=ROOT, check=True)
    dns_fixture()
    start(9443, "leaf.pem", Handler)
    start(9444, "expired.pem", Handler)
    start(9445, "rsa.pem", Handler, "rsa-key.pem")

    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/plain":
                body = b"plain HTTP after TLS redirect\n"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(302)
            self.send_header("Location", "https://tls.test:9443/")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    redirect = http.server.ThreadingHTTPServer(("127.0.0.1", 9080), Redirect)
    HTTP_SERVER.append(redirect)
    threading.Thread(target=redirect.serve_forever, daemon=True).start()


def make_broken():
    """a copy of a fresh dev disk with three faults put in by debugfs"""
    sh(f"tools/mkimage.sh {PROFILE[0]} --fresh")
    sh(f"cp {disk()} {BROKEN}")
    sh(f"dd if={BROKEN} of=out/headless/p.img bs=512 skip=133120 count=389120 status=none")
    blk = sh("debugfs -R 'blocks /bin/ls' out/headless/p.img 2>/dev/null").split()[0]
    with open(os.path.join(ROOT, "out/headless/cmds"), "w") as f:
        f.write(f"set_inode_field /etc/motd links_count 5\nfreeb {blk}\nunlink /bin/true\n")
    sh("debugfs -w -f out/headless/cmds out/headless/p.img")
    sh(f"dd if=out/headless/p.img of={BROKEN} bs=512 seek=133120 conv=notrunc status=none")
    sh("rm -f out/headless/p.img out/headless/cmds")


def broken_clean_on_host():
    sh(f"dd if={BROKEN} of=out/headless/p.img bs=512 skip=133120 count=389120 status=none")
    out = sh("e2fsck -fn out/headless/p.img")
    sh("rm -f out/headless/p.img")
    return "" if re.search(r"Fix|Popraw", out) is None else "e2fsck still finds problems"

USB = ["--qemu=-machine", "--qemu=i8042=off", "--qemu=-device", "--qemu=qemu-xhci,id=xhci",
       "--qemu=-device", "--qemu=usb-kbd,bus=xhci.0"]

USB_STICK = os.path.join(ROOT, "out", "headless", "stick.img")


def usb_stick(path=None):
    """QEMU arguments for a USB stick (on xHCI) backed by `path`"""
    return ["--qemu=-device", "--qemu=qemu-xhci,id=xhci", "--qemu=-drive",
            f"--qemu=if=none,id=stick,format=raw,file={path or USB_STICK}",
            "--qemu=-device", "--qemu=usb-storage,bus=xhci.0,drive=stick"]


def fat_stick(kind):
    """a 64 MiB stick image formatted FAT32 or exFAT (whole disk, no partition
    table) holding hello.txt and docs/a long name.txt"""
    sh(f"rm -f {USB_STICK}; dd if=/dev/zero of={USB_STICK} bs=1M count=64 status=none")
    if kind == "fat":
        sh(f"mkfs.fat -F 32 -n STICK {USB_STICK}")
        sh(f"printf 'from the host\\n' > out/headless/h.txt; mcopy -i {USB_STICK} out/headless/h.txt ::/hello.txt")
        sh(f"mmd -i {USB_STICK} ::/docs; mcopy -i {USB_STICK} out/headless/h.txt '::/docs/a long name.txt'")
    else:
        sh(f"mkfs.exfat -L STICK {USB_STICK}")
        sh(f"mkdir -p out/headless/mnt; printf 'from the host\\n' > out/headless/h.txt")
        # exFAT has no mtools: put the files on with the host's own driver via a loop mount is
        # not possible here (no root), so the test writes them inside MachOS instead


def fsck_stick(kind):
    tool = "fsck.fat -n" if kind == "fat" else "fsck.exfat -n"
    out = sh(f"{tool} {USB_STICK} 2>&1; echo status=$?")
    return "" if "status=0" in out else "host fsck found problems: " + out.strip()[-300:]


def legacy_update(output):
    """Generate only the final 0.9.3 migration artifact; not a future update format."""
    import shutil, tarfile, tempfile
    version = re.search(r'^version = "([^"]+)"', (Path(ROOT) / "kernel/mach.toml").read_text(), re.M)[1]
    if version != "0.9.3":
        raise RuntimeError("legacy migration packages end at 0.9.3")
    subprocess.run(["tools/mkimage.sh", "release", "--fresh"], cwd=ROOT, check=True, stdout=subprocess.PIPE)
    root = Path(ROOT) / "out/release/rootfs"
    def owner(info):
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mtime = 0
        return info
    with tempfile.TemporaryDirectory(dir=Path(ROOT) / "out") as temporary:
        stage = Path(temporary)
        for name in ("bin", "boot", "etc", "usr/share/licenses"):
            shutil.copytree(root / name, stage / name, dirs_exist_ok=True)
        (stage / "VERSION").write_text("0.9.3\n")
        data = stage / "bin/mpm-bootstrap.tar"
        with tarfile.open(data, "w", format=tarfile.USTAR_FORMAT) as tar:
            defaults = [*sorted((root / "etc").glob("*.conf")), root / "etc/hosts", root / "etc/services", root / "etc/mpm/keys/machos.pub", root / "etc/mpm/os-version"]
            for path in defaults + sorted((root / "var/lib/mpm/packages").glob("*.toml")):
                tar.add(path, arcname=str(path.relative_to(root)), recursive=False, filter=owner)
        with tarfile.open(output, "w:gz", format=tarfile.USTAR_FORMAT) as tar:
            for path in sorted(stage.rglob("*")):
                tar.add(path, arcname=str(path.relative_to(stage)), recursive=False, filter=owner)


def update_stick():
    """Exercise the real 0.9.2 updater against the last migration archive."""
    import shutil
    archive = Path(ROOT) / "out/headless/update.tgz"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.unlink(missing_ok=True)
    legacy_update(archive)
    project = Path(ROOT) / "out/headless/legacy-update"
    (project / "src").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(ROOT) / "tools/fixtures/legacy-update-0.9.2.mach", project / "src/update.mach")
    (project / "mach.toml").write_text('''[project]
id = "legacy"
version = "0.9.2"
mach = "^6.10"
src = "src"
out = "out/{target.name}/{profile.name}"
[profile.release]
opt = 2
debug = false
simd = "scalarize"
vectorize = false
float_reassoc = false
[target.machos]
isa = "x86_64"
os = "freestanding"
abi = "sysv64"
of = "elf"
base = 0x400000
platform = "machos"
[artifact.update]
kind = "bin"
entry = "update.mach"
out = "update"
targets = ["machos"]
link = []
need = []
[dep.sdk]
path = "''' + str(Path(ROOT) / "sdk") + '''"
''')
    subprocess.run(["mach", "dep", "pull", str(project)], cwd=ROOT, check=True, stdout=subprocess.PIPE)
    subprocess.run(["mach", "build", str(project), "-a", "update", "-t", "machos", "-p", "release"], cwd=ROOT, check=True)
    subprocess.run(["tools/put.sh", str(project / "out/machos/release/update"), "/bin/update", disk()], cwd=ROOT, check=True, stdout=subprocess.PIPE)
    sh(f"rm -f {USB_STICK}; dd if=/dev/zero of={USB_STICK} bs=1M count=64 status=none")
    sh(f"mkfs.fat -F 32 {USB_STICK}")
    subprocess.run(["mcopy", "-i", USB_STICK, str(archive), "::/update.tgz"], cwd=ROOT, check=True)


DUAL = os.path.join(ROOT, "out", "headless", "dual.img")


def fake_windows():
    """a copy of the dev disk with machboot standing in for Windows' boot
    manager on its EFI partition"""
    sh(f"tools/mkimage.sh {PROFILE[0]} >/dev/null")
    sh(f"cp {disk()} {DUAL}")
    sh(f"mmd -i {DUAL}@@1M ::/EFI/Microsoft ::/EFI/Microsoft/Boot")
    sh(f"mcopy -i {DUAL}@@1M machboot/out/uefi/release/BOOTX64.EFI ::/EFI/Microsoft/Boot/bootmgfw.efi")


USB_BARE = ["--qemu=-machine", "--qemu=i8042=off", "--qemu=-device", "--qemu=qemu-xhci,id=xhci"]

USB_HUB = ["--qemu=-machine", "--qemu=i8042=off", "--qemu=-device", "--qemu=qemu-xhci,id=xhci",
           "--qemu=-device", "--qemu=usb-hub,bus=xhci.0,port=2",
           "--qemu=-device", "--qemu=usb-hub,bus=xhci.0,port=2.1",
           "--qemu=-device", "--qemu=usb-mouse,bus=xhci.0,port=2.2",
           "--qemu=-device", "--qemu=usb-kbd,bus=xhci.0,port=2.1.4"]

TESTS = [
    # name, keep previous disk, fsck, steps, expected output in order
    ("usability", False, True,
     ["--prep", with_usability, "--boot", disk,
      "wait:" + P, "send:sh /bin/usability-test; echo usability-status $?\\n", "wait:" + P,
      "send:config set hostname configured; config set ttys 1; uname -n\\n", "wait:" + P],
     ["usability: all assertions passed", "usability-status 0", "config: saved", "machos"]),

    ("usability-reboot", True, True,
     ["wait:" + P,
      "send:uname -n; config get ttys; test \"$(uname -n)\" = configured; echo boot-config-status $?\\n", "wait:" + P],
     ["MachOS 0.9.3", "configured", "1", "boot-config-status 0"]),

    ("boot", False, False,
     ["wait:" + P, "send:uname -a; ls /\\n", "wait:" + P],
     ["root: ext2 on /dev/sda2", "MachOS", "bin/", "etc/", "dev/", "tmp/"]),

    ("pipes", False, False,
     ["wait:" + P, "send:seq 1 2000 | grep 7 | wc; seq 1 3 | cat | cat | wc\\n", "wait:" + P],
     ["542     542    2419", "3       3       6"]),

    ("shell", False, False,
     ["wait:" + P,
      "send:nosuch; echo \"status $?\"; false || echo or-ok; true && echo and-ok; false && echo WRONG\\n",
      "wait:" + P,
      "send:echo 'single $? quotes' \"a   b\"; cd /etc; pwd; cd /; echo \"echo from-script\" > /tmp/s.sh; sh /tmp/s.sh\\n",
      "wait:" + P],
     ["command not found", "status 127", "or-ok", "and-ok",
      "single $? quotes a   b", "/etc", "from-script"]),

    ("hostname-prompt", False, False,
     ["wait:" + P, "send:echo prompt-check > /proc/sys/kernel/hostname\\n",
      "wait:root@prompt-check", "send:uname -n; echo machos > /proc/sys/kernel/hostname\\n",
      "wait:root@machos"],
     ["root@prompt-check", "\nprompt-check\n", "root@machos"]),

    ("line-editing", False, False,
     ["wait:" + P, "send:echo one\\n", "wait:" + P, "send:echo two\\n", "wait:" + P,
      "send:\\x1b[A\\x1b[A", "sleep:0.3", "send:\\n", "wait:" + P,
      "send:ech\\t", "sleep:0.3", "send:tab-ok\\n", "wait:" + P,
      "send:cat /etc/mo\\t", "sleep:0.3", "send:\\n", "wait:" + P,
      "send:echo wrld\\x1b[D\\x1b[D\\x1b[Do\\n", "wait:" + P,
      "send:junk\\x03", "wait:" + P,
      "send:cat\\n", "sleep:0.3", "send:canon\\n", "sleep:0.3", "send:\\x03", "wait:" + P,
      "keys:echo ps2 hist<ret>", "wait:" + P, "keys:<up><left><left><left><left>X<ret>", "wait:" + P],
     ["one", "two", "echo one\none", "tab-ok", "Welcome", "world", "junk^C", "canon\ncanon",
      "ps2 Xhist"]),

    # a USB keyboard on xHCI with the PS/2 controller switched off; a key
    # held for 900 ms repeats
    ("usb-keyboard", False, False,
     USB + ["wait:" + P, "keys:echo USB_kbd ok!<ret>", "wait:" + P,
      "keys:<up><left><left><left>X<ret>", "wait:" + P,
      "keys:junk<ctrl-c>", "wait:" + P,
      "keys:echo [<x 900>]<ret>", "wait:" + P],
     ["usb: 1 xhci controller(s), 1 keyboard(s)", "USB_kbd ok!", "USB_kbd Xok!", "junk^C",
      "[xxxxxxxxxx"]),

    # the keyboard two hubs deep, next to a mouse
    ("usb-hub", False, False,
     USB_HUB + ["wait:" + P, "keys:echo Behind_hubs<ret>", "wait:" + P],
     ["usb: 1 xhci controller(s), 1 keyboard(s)", "Behind_hubs"]),

    # nano: type, undo, cut and paste, replace all, save on exit
    ("nano", False, False,
     ["wait:" + P, "send:nano /tmp/t.txt\\n", "sleep:1", "send:hello world\\nsecond line\\nthird\\n", "sleep:0.5",
      "send:\\x1bu", "sleep:0.3", "send:\\x1b[A\\x1b[A\\x0b\\x1b[B\\x15", "sleep:0.3",
      "send:\\x1cline\\r", "sleep:0.5", "send:LINE\\r", "sleep:0.5", "send:a", "sleep:0.5",
      "send:\\x18", "sleep:0.5", "send:y", "sleep:0.5", "send:\\r", "sleep:0.8",
      "send:cat /tmp/t.txt; tty\\n", "wait:" + P],
     ["Replaced 1 occurrence", "second LINE\nhello world", "/dev/tty1"]),

    # six terminals: log in on tty2 with the keyboard, then back to tty1
    ("terminals", False, False,
     ["wait:" + P, "keys:<alt-f2>", "sleep:0.5", "keys:root<ret>", "sleep:1.5",
      "keys:tty > /tmp/which<ret>", "sleep:0.5", "keys:<alt-f1>", "sleep:0.3",
      "send:cat /tmp/which; ps | grep -c login\\n", "wait:" + P],
     ["/dev/tty2", "\n4"]),

    # plugging in while running: a keyboard, then a hub and a keyboard on
    # it, unplugged and plugged into another of its ports
    ("usb-hotplug", False, False,
     USB_BARE + ["wait:" + P,
      "mon:device_add usb-kbd,bus=xhci.0,port=1,id=k1", "sleep:1.5", "keys:echo plugged_in<ret>", "wait:" + P,
      "mon:device_del k1", "sleep:1", "mon:device_add usb-hub,bus=xhci.0,port=2,id=h1", "sleep:1.5",
      "mon:device_add usb-kbd,bus=xhci.0,port=2.3,id=k2", "sleep:1.5", "keys:echo via_hub<ret>", "wait:" + P,
      "mon:device_del k2", "sleep:1", "mon:device_add usb-kbd,bus=xhci.0,port=2.1,id=k3", "sleep:1.5",
      "keys:echo again<ret>", "wait:" + P],
     ["usb: 1 xhci controller(s), 0 keyboard(s)", "plugged_in\nroot", "via_hub\nroot", "again\nroot"]),

    # 0.7.1's programs on four cores: tar and gzip round trip, calc, dmesg,
    # top, time, history
    ("new-tools", False, True,
     ["--qemu=-smp", "--qemu=4", "wait:" + P,
      "send:calc 2+3*4; calc '(1<<40) / 3'; calc 7 % -3\\n", "wait:" + P,
      "send:cp -r /etc /tmp/e; tar -cf /tmp/e.tar /tmp/e; gzip /tmp/e.tar; ls /tmp\\n", "wait:" + P,
      "send:mkdir /root/x; cd /root/x; gunzip -c /tmp/e.tar.gz | tar -xvf -; diff /etc/motd tmp/e/motd && echo same\\n", "wait:" + P,
      "send:gzip -c /bin/ls > /root/ls.gz; gunzip /root/ls.gz; test \"$(sha256sum < /bin/ls)\" = \"$(sha256sum < /root/ls)\" && echo binsame\\n", "wait:" + P,
      "send:dmesg | grep smp; top -n 1 | grep cpu3; time true; history | grep -c calc\\n", "wait:" + P],
     ["14", "366503875925", "1\n", "e.tar.gz", "tmp/e/motd", "same", "binsame", "smp: 4 cpu(s)", "cpu3", "real", "\n2\n"]),

    # machboot's settings, changed inside MachOS, take effect on the next boot
    ("machboot-config", False, True,
     ["wait:" + P, "send:machboot cmdline 1 loglevel=4 hello; machboot timeout 1; machboot\\n", "wait:" + P],
     ["menu waits 1 s", "loglevel=4 hello"]),
    ("machboot-config-boots", True, True,
     ["wait:" + P, "send:dmesg | grep 'command line'; dmesg | grep 'loaded by'\\n", "wait:" + P],
     ["command line: 'loglevel=4 hello'", "loaded by machboot"]),

    # the live ISO, as a USB stick would boot it: runs from memory
    ("live-iso", False, False,
     ["--prep", lambda: sh("tools/iso.sh out/headless/live.iso"), "--boot", os.path.join(ROOT, "out/headless/live.iso"),
      "wait:" + P, "send:dmesg | grep -E 'live system|loaded by'; ls /bin | wc -l; mpm list; mpm info coreutils\\n", "wait:" + P],
     ["loaded by machboot", "live system: running from memory", "machos-base 0.9.4", "mpm: installed files verified"]),

    # FAT32 on a USB stick: read the host's files, write, rename, remove;
    # the host's fsck.fat checks it after
    ("fat-usb", False, False,
     ["--prep", lambda: fat_stick("fat"), "--post", lambda: fsck_stick("fat")] + usb_stick() + [
      "wait:" + P, "send:mkdir /mnt; mount /dev/sdb /mnt; mount; cat /mnt/hello.txt '/mnt/docs/a long name.txt'\\n",
      "wait:" + P,
      "send:echo written by MachOS > /mnt/Notes.TXT; mkdir '/mnt/New Dir'; seq 1 3000 > '/mnt/New Dir/numbers file.txt'; "
      "mv /mnt/hello.txt /mnt/docs/moved.txt; rm '/mnt/docs/a long name.txt'; ls /mnt /mnt/docs; wc -l '/mnt/New Dir/numbers file.txt'; umount /mnt\\n",
      "wait:" + P],
     ["usb 1-", "mass storage", "/dev/sdb on /mnt type vfat", "from the host\nfrom the host",
      "Notes.TXT", "moved.txt", "3000"]),

    # exFAT on a USB stick, written and read back; the host's fsck.exfat checks it
    ("exfat-usb", False, False,
     ["--prep", lambda: fat_stick("exfat"), "--post", lambda: fsck_stick("exfat")] + usb_stick() + [
      "wait:" + P, "send:mkdir /mnt; mount /dev/sdb /mnt; mount | grep sdb\\n", "wait:" + P,
      "send:echo exfat works > '/mnt/a rather long file name.txt'; mkdir /mnt/folder; seq 1 5000 > /mnt/folder/n; "
      "mv /mnt/folder/n /mnt/folder/numbers; cat '/mnt/a rather long file name.txt'; wc -l /mnt/folder/numbers; umount /mnt; "
      "mount /dev/sdb /mnt; ls /mnt /mnt/folder; umount /mnt\\n",
      "wait:" + P],
     ["type exfat", "exfat works", "5000", "a rather long file name.txt", "numbers"]),

    # the EFI partition is mounted next to the root; machboot can be updated there
    ("esp-mounted", False, False,
     ["wait:" + P, "send:mount; ls /boot/efi/EFI/BOOT; machboot update\\n", "wait:" + P],
     ["/dev/sda1 on /boot/efi type vfat", "BOOTX64.EFI", "machboot on the EFI partition is now"]),

    # machboot finds another system's loader and starts it (here: a copy of
    # itself standing in for Windows' boot manager)
    ("dualboot", False, False,
     ["--prep", fake_windows, "--boot", DUAL, "--nologin", "sleep:2.5", "keys:<down><down><down>", "sleep:0.5",
      "keys:<ret>", "wait:booting Windows", "login:root:machos", "wait:" + P],
     ["Windows", "machboot: booting Windows", "machboot 0.9.3"]),

    # machboot hide: no menu, straight into MachOS
    ("hidden-menu", False, True,
     ["wait:" + P, "send:machboot hide; machboot | head -3\\n", "wait:" + P],
     ["the menu is hidden"]),
    ("hidden-menu-boots", True, True,
     ["wait:" + P, "send:machboot show\\n", "wait:" + P],
     ["login"]),

    # an update from a package on a USB stick, then a reboot into it
    ("update", False, True,
     ["--prep", update_stick, "--boot", disk] + usb_stick() + [
      "wait:" + P, "send:cat /etc/system.conf /etc/time.conf /etc/login.conf /etc/network.conf > /etc/machos.conf; rm /etc/system.conf /etc/time.conf /etc/login.conf /etc/network.conf /etc/se.conf /etc/sh.conf /etc/mpm.conf /etc/wget.conf; rm -r /etc/mpm /var/lib/mpm; mkdir /mnt; mount /dev/sdb /mnt; rm -r /usr/share/licenses; rm /etc/ssl/certs/ca-certificates.crt /bin/ca-update /bin/install; mv /mnt/update.tgz '/mnt/update name;literal.tgz'; mount /dev/sda1 /boot/efi; cd /mnt; update 'update name;literal.tgz'; cd /root; umount /boot/efi; umount /mnt; config get hostname; machboot\\n",
      "wait:" + P],
     ["updating MachOS", "installing the programs", "MachOS is updated to 0.9.3", "machos", "MachOS (previous)"]),
    ("update-reboot", True, True,
     ["--boot", disk, "wait:" + P, "send:ls /boot; uname -a; mpm info coreutils; test -e /bin/update; echo legacy-updater-status $?; test -e /bin/mpm-bootstrap.tar; echo migration-data-status $?; cat /etc/mpm/os-version\\n", "wait:" + P],
     ["kernel.old.elf", "MachOS 0.9.3", "mpm: installed files verified", "legacy-updater-status 1", "migration-data-status 1", "0.9.3"]),

    # MachOS installed on a USB stick, then booted from the stick alone
    ("usb-install", False, False,
     ["--prep", lambda: sh(f"rm -f {USB_STICK}; dd if=/dev/zero of={USB_STICK} bs=1M count=512 status=none")]
     + usb_stick() + ["wait:" + P, "send:install /dev/sdb\\n", "wait:continue: ", "send:yes\\n", "wait:New password: ", "send:machos\\n", "wait:Again: ", "send:machos\\n", "wait:Create an administrator", "send:no\\n",
      "wait:installed on", "wait:" + P],
     ["mass storage", "installed on /dev/sdb"]),
    ("usb-installed-boot", False, False,
     ["--prep", lambda: sh("dd if=/dev/zero of=out/headless/empty.img bs=1M count=8 status=none"),
      "--boot", os.path.join(ROOT, "out/headless/empty.img"),
      "--qemu=-device", "--qemu=qemu-xhci,id=xhci", "--qemu=-drive",
      f"--qemu=if=none,id=stick,format=raw,file={USB_STICK}",
      "--qemu=-device", "--qemu=usb-storage,bus=xhci.0,drive=stick,bootindex=0",
      "wait:" + P, "send:df\\n", "wait:" + P],
     ["root: ext2 on /dev/sdb2", "/dev/sdb2"]),

    ("tools", False, False,
     ["wait:" + P,
      "send:printf 'pear\\\\napple\\\\nfig\\\\napple\\\\n' > /tmp/f; sort -u /tmp/f | tr a-z A-Z; seq 1 20 | sort -n -r | head -2\\n",
      "wait:" + P,
      "send:seq 1 6 > /tmp/a; printf '1\\\\n2\\\\nX\\\\n4\\\\n5\\\\n6\\\\n' > /tmp/b; diff /tmp/a /tmp/b; echo diff=$?\\n",
      "wait:diff=", "wait:" + P,
      "send:find / -name '*.conf'; ls /etc | xargs echo got; seq 1 50 | tail -2 | tee /tmp/t | wc -l; basename /a/b.txt .txt; df\\n",
      "wait:" + P],
     ["APPLE", "FIG", "PEAR", "20", "19", "-3", "+X", "diff=1", "/boot/machboot.conf", "group hosts login.conf motd mpm/ mpm.conf network.conf passwd se.conf services sh.conf shadow ssl/ system.conf time.conf wget.conf",
      "      2", "b", "/dev/sda2", "%"]),

    ("jobs-and-ctrl-c", False, False,
     ["wait:" + P, "send:sleep 1 &\\n", "wait:" + P, "send:ps\\n", "wait:" + P,
      "sleep:1.5", "send:cat\\n", "sleep:0.5", "send:\\x03", "wait:" + P,
      "send:seq 1 100000000 | wc\\n", "sleep:1", "send:\\x03", "wait:" + P,
      "send:echo alive\\n", "wait:alive\\r\\n.*" + P],
     ["sleep", "running", "^C", "Done", "^C", "alive"]),

    ("shell-groups", False, False,
     ["wait:" + P, "send:sh -c 'sleep 1 & echo child=$!; wait; echo waited'\\n",
      r"wait:\r\nchild=[0-9]+\r\n", "wait:waited", "wait:" + P],
     ["waited"]),

    ("files", False, True,
     ["wait:" + P,
      "send:mkdir /home; cd /home; echo hello > a.txt; echo more >> a.txt; cat a.txt; wc < a.txt\\n",
      "wait:" + P,
      "send:mkdir d1 d1/d2; cp a.txt d1/d2/b; mv d1/d2/b d1/c; mv d1 d3; ls d3; rm d3; rm -r d3; ls\\n",
      "wait:" + P,
      "send:seq 1 300000 > n.txt; wc n.txt; rm n.txt; mv a.txt /tmp/x; mkdir /tmp/q; ls /tmp\\n",
      "wait:" + P],
     ["hello", "more", "2       2      11", "d2/", "c", "directory not empty", "a.txt",
      "300000  300000 1988895", "cannot move across filesystems", "q/"]),

    ("persist-write", False, True,
     ["wait:" + P, "send:mkdir /home; echo kept across reboots > /home/note; seq 1 1000000 > /home/big\\n",
      "wait:" + P, "sleep:1"],
     []),

    ("persist-read", True, True,
     ["wait:" + P, "send:cat /home/note; wc /home/big; grep -c 77777 /home/big; rm /home/big\\n",
      "wait:" + P, "sleep:1"],
     ["kept across reboots", "1000000 1000000 6888896", "19"]),

    ("posix-fds", False, True,
     ["--prep", with_posixtest, "--boot", disk,
      "wait:" + P, "send:posixtest\\n", "wait:failures",
      "send:cat /nope 2>/tmp/e; cat /tmp/e; cat /nope 2>&1 | wc -l; ls /etc /nope > /tmp/o 2>&1; cat /tmp/o\\n",
      "wait:" + P,
      "send:echo to-stderr >&2 2>/dev/null; echo kept 3>/tmp/three; cat < /etc/motd 0<&- | wc -c\\n", "wait:" + P],
     ["exec-works", "posixtest: 0 failures", "cat: /nope: no such file", "      1", "no such file", "motd",
      "kept"]),

    ("std", False, False,
     ["--prep", with_stdtest, "--boot", disk,
      "wait:" + P, "send:stdtest one two\\n", "wait:failures", "wait:" + P],
     ["hello from mach std on MachOS", "ok args", "ok env", "ok write_bytes", "ok metadata", "ok read",
      "ok remove_file", "ok exists", "ok not_found", "ok create_dir", "ok is_dir", "ok remove_dir",
      "ok clock", "ok monotonic", "ok random", "ok thread_spawn", "ok thread_join", "ok mutex", "ok timed_wait",
      "stdtest: 0 failures"]),

    ("socket-kernel", False, True,
     ["--prep", with_kernel_sockets, "--boot", disk,
      "wait:" + P, "send:runtests /bin/socket-tests 0 19\\n", "wait:runtests:", "wait:" + P],
     ["runtests: 20 passed, 0 failed, 0 hung"]),

    ("socket-kernel-smp", False, True,
     ["--prep", with_kernel_sockets, "--boot", disk, "--qemu=-smp", "--qemu=4",
      "wait:" + P, "send:runtests /bin/socket-tests 0 19\\n", "wait:runtests:", "wait:" + P],
     ["runtests: 20 passed, 0 failed, 0 hung"]),

    ("socket-kernel-no-entropy", False, True,
     ["--prep", with_kernel_sockets, "--boot", disk, "--qemu=-cpu", "--qemu=max,-rdrand,-rdseed",
      "wait:" + P, "send:runtests /bin/socket-tests 0 19\\n", "wait:runtests:", "wait:" + P],
     ["runtests: 20 passed, 0 failed, 0 hung"]),

    ("tlstest", False, False,
     ["--prep", with_tlssuite, "--boot", disk, tlssuite_steps],
     []),

    # The 100,000-result resolver stress test retains at least 400 MiB of
    # page-allocator storage. Match tools/run.sh's normal 1 GiB VM.
    ("stdtest", False, False,
     ["--prep", with_stdsuite, "--boot", disk, "--qemu=-m", "--qemu=1G", stdsuite_steps],
     []),

    ("proc", False, False,
     ["wait:" + P, "send:cat /proc/version; head -2 /proc/meminfo; cat /proc/self/status; ls /proc/sys/kernel; cat /proc/stat\\n",
      "wait:" + P,
      "send:uname -a; free; uptime; ps; df; id; whoami; tty; neofetch | grep -c Memory\\n", "wait:" + P],
     ["MachOS version 0.9.3", "MemTotal:", "Name:", "cat", "Uid:", "hostname", "cpu0",
      "MachOS 0.9.3 x86_64 on machos", "total", "processes", "PID", "init", "Filesystem", "/dev/sda2",
      "uid=0(root)", "root", "/dev/tty1", "1"]),

    ("events", False, False,
     ["wait:" + P, "send:events -n 2 > /tmp/ev &\\n", "sleep:1", "send:true\\n", "wait:" + P, "sleep:1",
      "send:cat /tmp/ev\\n", "wait:" + P],
     ["start", "true", "exit"]),

    ("clock", False, False,
     ["wait:" + P, "send:date -s '2030-01-02 03:04:05'; date +%s\\n", "wait:" + P,
      "send:echo 'timezone CET-1CEST,M3.5.0,M10.5.0/3' >> /etc/time.conf; date; date -u; env TZ=JST-9 date\\n",
      "wait:" + P,
      "send:date -s '2030-07-01 12:00'; date -u; echo box > /proc/sys/kernel/hostname; uname -n\\n", "wait:" + P],
     ["2030-01-02 03:04:05 UTC", "18935534", "2030-01-02 04:04", "CET", "2030-01-02 03:04", "UTC",
      "2030-01-02 12:04", "JST", "2030-07-01 12:00:00 CEST", "2030-07-01 10:00", "UTC", "box"]),

    ("config-write", False, False,
     ["wait:" + P, "send:echo 'hostname testbox' > /etc/system.conf; echo 'ttys 2' >> /etc/login.conf\\n",
      "wait:" + P,
      "send:echo 'autologin root' >> /etc/login.conf; echo 'timezone CET-1CEST,M3.5.0,M10.5.0/3' >> /etc/time.conf\\n",
      "wait:" + P, "send:echo 'rtc local' >> /etc/time.conf; sync\\n", "wait:" + P, "sleep:1"],
     []),

    ("config-boot", True, False,
     ["--nologin", "wait:" + P, "send:uname -n; date; cat /proc/sys/kernel/rtc_offset; ps | grep -c login\\n",
      "wait:" + P],
     ["testbox", "CE", "00", "1"]),

    # driver discovery must tolerate missing and unsupported Ethernet cards.
    ("ethernet-none", False, False,
     ["--qemu=-nic", "--qemu=none", "wait:" + P,
      "send:ip; ping -c 1 127.0.0.1\\n", "wait:packet loss", "wait:" + P],
     ["ethernet: 0 device(s) ready", "64 bytes from 127.0.0.1", "0% packet loss"]),

    ("ethernet-unsupported", False, False,
     ["--qemu=-nic", "--qemu=none", "--qemu=-netdev", "--qemu=user,id=lan",
      "--qemu=-device", "--qemu=virtio-net-pci,netdev=lan", "wait:" + P,
      "send:ping -c 1 127.0.0.1\\n", "wait:packet loss", "wait:" + P],
     ["ethernet: 0 device(s) ready", "64 bytes from 127.0.0.1", "0% packet loss"]),

    # skip an unsupported function, bind both supported cards, and use the
    # first registered card for DHCP and traffic through the shared contract.
    ("ethernet-discovery", False, False,
     ["--qemu=-nic", "--qemu=none", "--qemu=-netdev", "--qemu=user,id=unknown",
      "--qemu=-device", "--qemu=virtio-net-pci,netdev=unknown",
      "--qemu=-netdev", "--qemu=user,id=lan", "--qemu=-device", "--qemu=e1000,netdev=lan",
      "--qemu=-netdev", "--qemu=user,id=lan2", "--qemu=-device", "--qemu=e1000e,netdev=lan2",
      "wait:" + P, "send:sleep 2; ip; ping -c 2 10.0.2.2\\n", "wait:" + P],
     ["ethernet: 2 device(s) ready", "net: eth0: 82540EM", "address 10.0.2.15/24",
      "method dhcp", "0% packet loss"]),

    ("network", False, False,
     ["--prep", udp_echo, "--boot", disk,
      "wait:" + P, "send:ip; cat /proc/net/resolv.conf; ping -c 2 10.0.2.2\\n", "wait:packet loss", "wait:" + P,
      "send:host localhost; echo hello | nc -u -w 1 10.0.2.2 7777; ip stats; ip neigh\\n", "wait:" + P],
     ["address 10.0.2.15/24", "gateway 10.0.2.2", "dns 10.0.2.3", "method dhcp", "nameserver 10.0.2.3",
      "64 bytes from 10.0.2.2: icmp_seq=0", "64 bytes from 10.0.2.2: icmp_seq=1", "0% packet loss",
      "localhost has address 127.0.0.1", "echo: hello", "eth0", "10.0.2.2 52:55:0a:00:02:02"]),

    ("tcp", False, False,
     ["--prep", http_server, "--boot", disk,
      "wait:" + P, "send:printf 'GET /hello.txt HTTP/1.0\\\\n\\\\n' | nc 10.0.2.2 8080\\n", "wait:" + P,
      "send:printf 'GET /big.txt HTTP/1.0\\\\n\\\\n' | nc 10.0.2.2 8080 > /tmp/big; tail -150000 /tmp/big > /tmp/body; wc -l /tmp/body; sha256sum /tmp/body\\n",
      "wait:" + P, "send:nc 10.0.2.2 1 < /dev/null; cat /proc/net/tcp\\n", "wait:" + P],
     ["HTTP/1.0 200 OK", "hello over tcp", "150000",
      "771c3995129ed087c7336651f32a510b009e3c9d2190f13bda69d91dd91a257e", "connection refused"]),

    ("https", False, True,
     ["--prep", https_server, "--post", check_dns_retry, "--boot", disk,
      "wait:" + P, "send:echo '10.0.2.2 tls.test wrong.test' >> /etc/hosts; export SSL_CERT_FILE=/root/test-ca.pem; time wget -O - https://tls.test:9443/; echo https-status $?\\n", "wait:" + P,
      "send:wget -q -O /tmp/tls-big https://tls.test:9443/big; echo big-status $?; sha256sum /tmp/tls-big\\n", "wait:" + P,
      "send:wget -q -O - https://tls.test:9443/chunked; echo chunk-status $?; wget -q -O - http://tls.test:9080/; echo redirect-status $?\\n", "wait:" + P,
      "send:wget -q -O /tmp/bad https://wrong.test:9443/; echo wrong-host-status $?; wget -q -O /tmp/bad https://tls.test:9444/; echo expired-status $?\\n", "wait:" + P,
      "send:wget -q -O /tmp/bad https://tls.test:9443/truncated; echo truncated-status $?; unset SSL_CERT_FILE; wget -q -O /tmp/bad https://tls.test:9443/; echo untrusted-status $?\\n", "wait:" + P,
      "send:export SSL_CERT_FILE=/root/test-ca.pem; wget -q -O /tmp/bad https://tls.test:9443/no-notify; echo no-notify-status $?; wget -q -O - https://tls.test:9443/redirect-http; echo downgrade-status $?; time wget -q -O /tmp/rsa https://tls.test:9445/; echo rsa-status $?; wget -q -O - https://tls.test:9443/redirect-tls; echo tls-redirect-status $?\\n", "wait:" + P,
      "send:dns-proxy &\\n", "wait:" + P,
      "send:sleep 1; export SSL_CERT_FILE=/root/test-ca.pem; ip set 10.0.2.15/24 10.0.2.2 127.0.0.1; cat /proc/net/resolv.conf; wget -q -O - https://alias.test:9443/; echo dns-chain-retry-status $?\\n", "wait:" + P,
      "send:export SSL_CERT_FILE=/root/test-ca.pem; wget -q -O /tmp/ip-san https://10.0.2.2:9443/; echo ip-san-status $?; ca-update --url https://tls.test:9443/bundle; echo ca-update-status $?; cat /etc/ssl/certs/SOURCE; cp /etc/ssl/certs/ca-certificates.crt /tmp/before-ca; ca-update --url https://tls.test:9443/; echo invalid-bundle-status $?; test \"$(sha256sum < /tmp/before-ca)\" = \"$(sha256sum < /etc/ssl/certs/ca-certificates.crt)\"; echo bundle-kept-status $?; ca-update --url http://tls.test:9080/; echo insecure-url-status $?; ca-update --url https://tls.test:9443/redirect-http; echo insecure-redirect-status $?; test \"$(sha256sum < /tmp/before-ca)\" = \"$(sha256sum < /etc/ssl/certs/ca-certificates.crt)\"; echo redirect-kept-status $?; unset SSL_CERT_FILE; wget -q -O - https://tls.test:9443/; echo updated-roots-status $?\\n", "wait:" + P],
     ["hello over verified TLS", "https-status 0", "big-status 0",
      __import__("hashlib").sha256(b"0123456789abcdef" * 65536).hexdigest(),
      "hello chunked!", "chunk-status 0", "hello over verified TLS", "redirect-status 0",
      "certificate verification failed", "wrong-host-status 1",
      "certificate verification failed", "expired-status 1",
      "truncated, invalid, or unauthenticated", "truncated-status 1",
      "certificate verification failed", "untrusted-status 1", "truncated, invalid, or unauthenticated", "no-notify-status 1",
      "plain HTTP after TLS redirect", "downgrade-status 0", "rsa-status 0", "tls-redirect-status 0", "dns-proxy ready", "nameserver 127.0.0.1", "hello over verified TLS", "dns-chain-retry-status 0", "ip-san-status 0", "ca-update: CA bundle updated", "ca-update-status 0", "# Source: https://tls.test:9443/bundle",
      "invalid CA bundle; existing CA bundle kept", "invalid-bundle-status 1", "bundle-kept-status 0",
      "HTTPS URL is required", "insecure-url-status 1", "ca-update: download failed; existing CA bundle kept",
      "insecure-redirect-status 1", "redirect-kept-status 0", "hello over verified TLS", "updated-roots-status 0"]),

    ("wget", False, False,
     ["--prep", http_server, "--boot", disk,
      "wait:" + P, "send:cd /tmp; wget http://10.0.2.2:8080/hello.txt; cat hello.txt\\n", "wait:" + P,
      "send:wget -q -O big http://10.0.2.2:8080/big.txt; wc -l big; sha256sum big\\n", "wait:" + P,
      "send:wget -O - http://10.0.2.2:8080/sub; wget http://10.0.2.2:8080/nope; echo status $?\\n", "wait:" + P,
      "send:wget http://10.0.2.2:1/\\n", "wait:" + P,
      "send:config set wget.quiet yes; wget -O /tmp/quiet http://10.0.2.2:8080/hello.txt > /tmp/wget-log 2>&1; grep saved /tmp/wget-log; echo quiet-progress $?; config set wget.redirects 0; wget -q -O /tmp/limit http://10.0.2.2:8080/sub; echo redirect-limit $?; config set wget.timeout 1; echo timeout-range $?\\n", "wait:" + P],
     ["hello.txt saved, 15 bytes", "hello over tcp", "150000",
      "771c3995129ed087c7336651f32a510b009e3c9d2190f13bda69d91dd91a257e",
      "redirected to http://10.0.2.2:8080/sub/", "behind a redirect", "404", "status 1",
      "connection refused", "quiet-progress 1", "too many redirects", "redirect-limit 1", "timeout-range 2"]),

    ("net-config", False, False,
     ["wait:" + P, "send:ip set 10.0.2.50/24 10.0.2.2 10.0.2.3; ip; ping -c 1 10.0.2.2\\n", "wait:packet loss",
      "wait:" + P, "send:ip off; ip; ip dhcp; sleep 2; ip\\n", "wait:" + P],
     ["address 10.0.2.50/24", "method static", "64 bytes from 10.0.2.2", "0% packet loss",
      "address 0.0.0.0/0", "method off", "address 10.0.2.15/24", "method dhcp"]),

    # needs the internet (QEMU's DNS asks the host's resolver)
    ("dns", False, False,
     ["wait:" + P, "send:host example.com\\n", "wait:" + P],
     ["example.com has address"]),

    ("environment", False, False,
     ["wait:" + P,
      "send:echo \"$HOME $PATH\"; X=1; echo x=$X ${X}y; env | grep -c X=; export X; env | grep X=\\n", "wait:" + P,
      "send:Y=only-here env | grep Y=; echo \"[$Y]\"; unset X; echo \"[$X]\"; echo ~ $$\\n", "wait:" + P,
      "send:mkdir /tmp/b; printf '#!/bin/sh\\\\necho via-path $1\\\\n' > /tmp/b/tool; chmod +x /tmp/b/tool; PATH=/tmp/b:/bin; tool z; which tool\\n",
      "wait:" + P,
      "send:env -i K=v env; echo \"a\\\\nb \\\\$\"\\n", "wait:" + P,
      "send:set | grep -c PATH=; pwd | cat; export | grep -c HOME; cd /etc | cat; pwd\\n", "wait:" + P],
     ["/root /bin", "x=1 1y", "0", "X=1", "Y=only-here", "[]", "[]", "/root ", "via-path z", "/tmp/b/tool",
      "K=v", "a\\nb $", "1", "/", "1", "/"]),

    ("job-control", False, False,
     ["wait:" + P, "send:sleep 3\\n", "sleep:0.7", "send:\\x1a", "wait:Stopped.*\\n.*" + P,
      "send:ps | grep sleep; bg; jobs\\n", "wait:" + P,
      "send:fg\\n", "wait:sleep 3.*\\n.*" + P, "send:echo fg-status=$?\\n", "wait:" + P,
      "send:sleep 30\\n", "sleep:0.5", "send:\\x1a", "wait:Stopped.*\\n.*" + P,
      "send:fg\\n", "sleep:0.5", "send:\\x03", "wait:" + P,
      "send:echo status=$?; kill -l | grep -c TERM; kill -TERM 999\\n", "wait:" + P],
     ["^Z", "Stopped  sleep 3", "stopped", "Running  sleep 3", "fg-status=0", "Stopped  sleep 30",
      "^C", "status=130", "1", "no such process"]),

    ("users", False, True,
     ["wait:" + P, "send:id; adduser alice\\n", "wait:New password: ", "send:first\\n", "wait:Again: ", "send:first\\n", "wait:" + P, "send:ls -l /home\\n", "wait:" + P,
      "send:passwd alice\\n", "wait:New password: ", "send:secret\\n", "wait:Again: ", "send:secret\\n",
      "wait:" + P,
      "send:su alice\\n", "wait:" + P,
      "send:id; cat /etc/shadow; touch /x; echo hi > ~/f; cat ~/f; kill 1; exit\\n", "wait:# ",
      "send:exit\\n", "wait:login: ", "send:alice\\n", "wait:Password: ", "send:wrong\\n",
      "wait:incorrect", "wait:login: ", "send:alice\\n", "wait:Password: ", "send:secret\\n",
      "wait:" + P, "send:pwd; ls -l /bin/su\\n", "wait:" + P],
     ["uid=0(root)", "added alice", "alice    alice", "password changed",
      "uid=1000(alice)", "shadow: permission denied", "/x: permission denied", "hi",
      "operation not permitted", "Login incorrect", "alice@machos", "/home/alice", "-rwsr-xr-x root"]),

    # repair a disk that debugfs damaged on the host
    ("fsck", False, False,
     ["--disk2", BROKEN, "--prep", make_broken, "--post", broken_clean_on_host,
      "wait:" + P, "send:fsck /dev/sda2; echo rc=$?.\\n", "wait:rc=[0-9]+\\.",
      "send:fsck /dev/sdb2; echo rc=$?.\\n", "wait:rc=[0-9]+\\.",
      "send:fsck -y /dev/sdb2; echo rc=$?.\\n", "wait:rc=[0-9]+\\.",
      "send:fsck /dev/sdb2; echo rc=$?.\\n", "wait:rc=[0-9]+\\.",
      "send:fsck -y /dev/sda2; echo rc=$?.\\n", "wait:rc=[0-9]+\\."],
     ["clean", "rc=0.", "moved to /lost+found", "wrong link count", "block bitmap", "rc=4.",
      "(fixed)", "3 problems fixed", "rc=1.", "clean", "rc=0.", "is mounted", "rc=8."]),

    # a hard kill leaves the root unclean; rescue mode can check it; a clean
    # shutdown leaves it clean
    ("unclean-then-rescue", False, False,
     ["wait:" + P, "send:echo x > /tmp/y\\n", "wait:" + P],
     []),
    ("unclean-warning", True, False,
     ["wait:" + P, "send:true\\n", "wait:" + P],
     ["not shut down cleanly", "rescue"]),
    ("rescue", True, False,
     ["--rescue", "--rescue",
      "wait:" + P, "send:ls /; fsck -y /dev/sda2; echo rc=$?.\\n", "wait:rc=[0-9]+\\."],
     ["rescue mode", "root: initrd in memory", "bin/", "/dev/sda2:", "rc="]),
    ("shutdown-clean", True, False,
     ["--post", lambda: "" if "clean" in root_state(disk()) and "not clean" not in root_state(disk()) else "root not clean after poweroff",
      "wait:" + P, "send:echo hi > /tmp/z; poweroff\\n", "wait:power: off", "sleep:1"],
     ["power: off"]),

    ("install-guards", False, True,
     ["--disk2", "blank:" + os.path.join(ROOT, "out/headless/install-guard.img"),
      "wait:" + P,
      "send:head -c512 /dev/sdb | sha256sum > /tmp/before; gpt --check /dev/sdb; echo disk-check $?; head -c512 /dev/sdb | sha256sum > /tmp/after; diff /tmp/before /tmp/after; echo check-unchanged $?\\n",
      "wait:" + P,
      "send:install /dev/sda2; echo partition-rejected $?; install /dev/zero; echo character-rejected $?; gpt /dev/sda2; echo gpt-partition $?; gpt /dev/null; echo gpt-character $?\\n",
      "wait:" + P,
      "send:touch /tmp/mode; chmod 640 /tmp/mode; chmod 10000 /tmp/mode; echo mode-range $?; ls -l /tmp/mode; chmod '' /tmp/mode; echo mode-empty $?; chmod u+r, /tmp/mode; echo mode-comma $?; chmod 7777777777777777777777777777 /tmp/mode; echo mode-overflow $?; ls -l /tmp/mode\\n",
      "wait:" + P,
      "send:chmod u+x,go-r /tmp/mode; ls -l /tmp/mode; chmod 7777 /tmp/mode; ls -l /tmp/mode; chmod 0 /tmp/mode; ls -l /tmp/mode\\n",
      "wait:" + P],
     ["disk-check 0", "check-unchanged 0", "partition-rejected 2", "character-rejected 2",
      "gpt-partition 1", "gpt-character 1", "mode-range 2", "-rw-r-----", "mode-empty 2",
      "mode-comma 2", "mode-overflow 2", "-rw-r-----", "-rwx------", "-rwsrwsrwt", "----------"]),

    # install onto a blank second disk, then boot that disk on its own
    ("install", False, False,
     ["--disk2", "blank:" + TARGET,
      "wait:" + P, "send:ls /dev; install /dev/sda\\n", "wait:continue: ", "send:no\\n", "wait:" + P,
      "send:install /dev/sdb\\n", "wait:continue: ", "send:yes\\n", "wait:New password: ", "send:\\n", "wait:It cannot be empty.", "wait:New password: ", "send:machos\\n", "wait:Again: ", "send:machos\\n", "wait:Create an administrator", "send:yes\\n", "wait:User name: ", "send:installer\\n", "wait:New password: ", "send:testing\\n", "wait:Again: ", "send:testing\\n", "wait:" + P,
      "send:ls /dev; mount /dev/sdb2 /mnt; ls /mnt; cat /mnt/etc/group; ls /mnt/var/lib/mpm/packages; umount /dev/sdb2; ls /mnt; echo unmounted\\n", "wait:" + P],
     ["sdb", "/dev/sdb2 MachOS root 446 MiB", "mkfat: FAT32", "mkext2:", "copying the system",
      "It cannot be empty.", "adduser: added installer", "is installed on /dev/sdb", "sdb1", "sdb2", "bin/", "admin:x:101:installer", "machos-base.toml", "unmounted"]),

    ("nvme-boot", False, False,
     ["--nvme", "wait:" + P, "send:df; ls /dev; echo \"[$(partname /dev/nvme0n1 2)]\"\\n", "wait:" + P],
     ["disk: nvme0n1", "root: ext2 on /dev/nvme0n1p2", "/dev/nvme0n1p2", "nvme0n1p1",
      "[/dev/nvme0n1p2]"]),

    ("nvme-install", False, False,
     ["--disk2", "blank:" + NVME_TARGET, "--disk2", "nvme:" + NVME_TARGET,
      "wait:" + P, "send:install /dev/nvme0n1\\n", "wait:continue: ", "send:yes\\n", "wait:New password: ", "send:machos\\n", "wait:Again: ", "send:machos\\n", "wait:Create an administrator", "send:no\\n", "wait:installed on",
      "wait:" + P],
     ["/dev/nvme0n1p1 EFI system", "mkfat: FAT32", "copying the system", "installed on /dev/nvme0n1"]),

    ("nvme-installed-boot", False, True,
     ["--boot", NVME_TARGET, "--nvme", "wait:" + P, "send:df\\n", "wait:" + P, "sleep:1"],
     ["root: ext2 on /dev/nvme0n1p2", "/dev/nvme0n1p2"]),

    ("installed-boot", False, True,
     ["--boot", TARGET, "wait:" + P,
      "send:uname; ls /boot; echo on-installed > /home/x; cat /home/x; mkdir /mnt; mount /dev/sda2 /mnt\\n", "wait:" + P,
      "sleep:1"],
     ["root: ext2 on /dev/sda2", "MachOS", "kernel.elf", "on-installed", "busy"]),
]


# Large-memory tests run separately so only one large guest occupies host RAM.
for ram, allocation in ((2048, 256), (4096, 512), (8192, 4352),
                        (16384, 1024), (32768, 1024), (65536, 1024)):
    # Sparse backing permits address-range tests beyond the host's RAM/swap
    # reservation limit. The workload above 8 GiB touches only 1 GiB.
    backing = (["--qemu=-object",
                f"--qemu=memory-backend-ram,id=ram,size={ram}M,reserve=off",
                "--qemu=-machine", "--qemu=memory-backend=ram"] if ram >= 16384 else [])
    TESTS.append((f"ram-{ram // 1024}g", False, False,
                  ["--prep", with_ramtest, "--boot", disk,
                   "--qemu=-m", f"--qemu={ram}M", "--qemu=-smp", "--qemu=4"] + backing + [
                   "wait:" + P,
                   f"send:free -m; ramtest {ram} {allocation}; echo ram-status $?\\n",
                   "wait:" + P],
                  [f"ok resident RAM MiB: {allocation}", "ram-status 0"]))


for cores in (4, 8, 16):
    TESTS.append((f"smp-{cores}", False, False,
                  ["--prep", with_smptest, "--boot", disk,
                   "--qemu=-smp", f"--qemu={cores}", "wait:" + P,
                   "send:smptest; echo smp-status $?\\n", "wait:" + P],
                  [f"smp: {cores} cpu(s) running", f"ok parallel CPUs: {cores}", "smp-status 0"]))


for cores in (2, 4, 8, 16):
    TESTS.append((f"threads-{cores}", False, False,
                  ["--prep", with_threadtest, "--boot", disk,
                   "--qemu=-smp", f"--qemu={cores}", "wait:" + P,
                   "send:threadtest; echo thread-status $?\\n", "wait:" + P],
                  ["ok concurrent process and thread identifiers", "ok concurrent shared-address-space faults",
                   "ok parallel threads", "ok remote protection",
                   "ok exit exec and failed exec with running threads",
                   "ok concurrent fork and copy-on-write",
                   "ok process-wide stop and continue",
                   "ok lifecycle with identifier-only threads", "thread-status 0"]))

_, keep, fsck, steps, expect = next(test for test in TESTS if test[0] == "std")
TESTS.append(("std-smp", keep, fsck,
              ["--qemu=-smp", "--qemu=4"] + steps, expect))


TESTS.append(("security", False, True,
              ["--prep", with_securitytest, "--boot", disk,
               "--qemu=-smp", "--qemu=4", "wait:" + P,
               "send:securitytest; echo security-status $?\\n", "wait:" + P],
              ["ok recursive rm preserves link target", "ok rm removes dangling link",
               "ok tar rejects directory-link destinations", "ok tar rejects truncated header",
               "ok malformed shadow fails closed", "ok new accounts start locked",
               "ok failed account update preserves database",
               "ok long PATH entries do not overflow command buffer",
               "ok reused handles discard old queue registrations",
               "ok writes and chmod restrict set-id privileges",
               "ok exec releases abandoned blocking I/O resources",
               "ok blocked pipe survives close and unmap", "ok blocked queue survives close and unmap",
               "ok invalid syscall stack ends only its process",
               "ok invalid signal return ends only its process",
               "ok signal return masks unsupported MXCSR bits", "0 security failures", "security-status 0"]))


TESTS.append(("password-migration", False, True,
              ["wait:" + P, "send:adduser legacy\\n", "wait:New password: ", "send:initial\\n", "wait:Again: ", "send:initial\\n", "wait:" + P,
               "send:grep 'root:' /etc/shadow > /tmp/root-shadow; cp /tmp/root-shadow /etc/shadow; echo 'legacy::' >> /etc/shadow; exit\\n",
               "login:legacy:changed", "wait:legacy@.*[$] ", "send:id; exit\\n", "login:root:machos", "wait:" + P,
               "send:echo 'legacy:!:' > /tmp/locked; grep 'root:' /etc/shadow >> /tmp/locked; cp /tmp/locked /etc/shadow; exit\\n",
               "wait:login: ", "send:legacy\\n", "wait:Password: ", "send:changed\\n", "wait:Login incorrect",
               "login:root:machos", "wait:" + P, "send:sync\\n", "wait:" + P],
              ["adduser: added legacy", "A password is required", "uid=1000(legacy)", "Login incorrect"]))


TESTS.append(("mpm", False, True,
              ["--prep", with_mpm, "--boot", disk, "wait:" + P,
               "send:cd /bin; tar -xf /bin/mpm-fixtures.tar; cp /bin/test.pub /etc/mpm/keys/test.pub; cd /root\\n", "wait:" + P,
               "send:mpm list; mpm info machos-base; mpm info /bin/demo.mpa; mpm install /bin/demo.mpa; echo install-status $?\\n", "wait:" + P,
               "send:sha256sum /opt/mpm/demo; mpm info mpm-demo; mpm remove mpm-helper; echo dependency-status $?\\n", "wait:" + P,
               "send:echo edited > /etc/mpm-demo.conf; mpm install /bin/upgrade.mpa; cat /etc/mpm-demo.conf; cat /etc/mpm-demo.conf.mpm-new; mpm install /bin/demo.mpa; echo downgrade-status $?\\n", "wait:" + P,
               "send:mpm install --force /bin/ownership.mpa; echo ownership-status $?; mpm remove machos-base; echo base-status $?\\n", "wait:" + P,
               "send:mpm remove mpm-demo; cat /etc/mpm-demo.conf; test -e /opt/mpm/demo; echo removed-file $?; mpm remove --purge mpm-demo; test -e /etc/mpm-demo.conf; echo purged-config $?; test -e /etc/mpm-demo.conf.mpm-new; echo purged-pending $?; mpm remove mpm-helper; sync\\n", "wait:" + P],
              ["machos-base 0.9.4", "mpm: installed files verified", "mpm: archive verified",
               "mpm: installed mpm-helper 1.0.0", "mpm: installed mpm-demo 1.0.0", "install-status 0",
               "fbbab289f7f94b25736c58be46a994c441fd02552cc6022352e3d86d2fab7c83", "mpm: installed files verified", "dependency-status 1",
               "mpm: keeping edited configuration", "mpm: installed mpm-demo 1.1.0", "edited", "setting upgraded", "downgrade-status 1",
               "ownership-status 1", "base-status 1", "configuration preserved", "edited", "removed-file 1", "purged-config 1", "purged-pending 1"]))

TESTS.append(("mpm-reject", False, True,
              ["--prep", with_mpm, "--boot", disk, "wait:" + P,
               "send:cd /bin; tar -xf /bin/mpm-fixtures.tar; cp /bin/test.pub /etc/mpm/keys/test.pub; cd /root\\n", "wait:" + P,
               "send:mpm install /bin/unsigned.mpa; echo unsigned-status $?; mpm install /bin/tampered.mpa; echo tampered-status $?; mpm install --allow-unsigned /bin/comment.mpa; echo comment-status $?; mpm install /bin/unknown.mpa; echo unknown-status $?\\n", "wait:" + P,
               "send:mpm install --force /bin/bootstrap.mpa; echo bootstrap-status $?; test -e /bin/mpm-bootstrap.tar; echo bootstrap-file-status $?; mpm install /bin/truncated.mpa; echo truncated-status $?; mpm install /bin/traversal.mpa; echo traversal-status $?; mpm install /bin/duplicate.mpa; echo duplicate-status $?; mpm install /bin/link.mpa; echo link-status $?\\n", "wait:" + P,
               "send:mpm install /bin/cycle-a.mpa; echo cycle-status $?; mpm install /bin/missing.mpa; echo missing-status $?\\n", "wait:" + P,
               "send:mpm install --allow-unsigned /bin/unsigned.mpa\\n", "wait:Password: ", "send:wrong\\n", "wait:" + P,
               "send:mpm install --allow-unsigned /bin/unsigned.mpa\\n", "wait:Password: ", "send:machos\\n", "wait:" + P,
               "send:echo old > /opt/mpm/conflict; mpm install /bin/conflict.mpa; echo conflict-status $?; cat /opt/mpm/conflict; mpm install --force /bin/conflict.mpa; cat /opt/mpm/conflict; test -e /var/lib/mpm/transaction; echo clean-status $?; sync\\n", "wait:" + P],
              ["unsigned-status 1", "tampered-status 1", "comment-status 1", "unknown-status 1", "bootstrap-status 1", "bootstrap-file-status 1", "truncated-status 1", "traversal-status 1", "duplicate-status 1", "link-status 1", "cycle-status 1", "missing-status 1", "authentication failed", "mpm: installed mpm-unsigned 1.0.0", "conflict-status 1", "old", "mpm: installed mpm-conflict 1.0.0", "replacement", "clean-status 1"]))

TESTS.append(("mpm-rollback", False, True,
              ["--prep", with_mpm, "--boot", disk, "--disk2", "blank:" + TARGET,
               "wait:" + P, "send:cd /bin; tar -xf /bin/mpm-fixtures.tar; cp /bin/test.pub /etc/mpm/keys/test.pub; cd /root\\n", "wait:" + P,
               "send:mkdir -p /opt/mpm/mounted; echo before > /opt/mpm/rollback; mkext2 /dev/sdb; mount /dev/sdb /opt/mpm/mounted; mpm install --force /bin/rollback.mpa; echo rollback-status $?; cat /opt/mpm/rollback; test -e /var/lib/mpm/packages/mpm-rollback.toml; echo record-status $?; umount /opt/mpm/mounted\\n", "wait:" + P,
               "send:mkdir /var/lib/mpm/transaction; chmod 700 /var/lib/mpm/transaction; cp /bin/recover.journal /var/lib/mpm/transaction/journal; echo original > /var/lib/mpm/transaction/old-0; echo interrupted > /opt/mpm/rollback; mpm recover; echo recovery-status $?; cat /opt/mpm/rollback; sync\\n", "wait:" + P],
              ["transaction rolled back", "rollback-status 1", "before", "record-status 1", "recovery-status 0", "original"]))


TESTS.append(("fat-replace", False, True,
              ["--prep", lambda: with_posix_artifact("fatreplace"), "--boot", disk, "wait:" + P,
               "send:fatreplace; echo fat-replace-status $?; sync\\n", "wait:" + P],
              ["FAT replacement and detached handles passed", "fat-replace-status 0"]))


TESTS.append(("live-install", False, True,
              ["--prep", lambda: sh("tools/iso.sh out/headless/live-install.iso"), "--boot", os.path.join(ROOT, "out/headless/live-install.iso"), "--disk2", "blank:" + TARGET,
               "wait:" + P, "send:install /dev/sdb\\n", "wait:Type yes to continue:", "send:yes\\n", "wait:New password:", "send:machos\\n", "wait:Again:", "send:machos\\n", "wait:Create an administrator account?", "send:no\\n", "wait:" + P,
               "send:mkdir -p /mnt; mount /dev/sdb2 /mnt; test -f /mnt/boot/initrd.tar; echo live-rescue-status $?; test -e /mnt/bin/update; echo live-update-status $?; umount /mnt; sync\\n", "wait:" + P],
              ["MachOS is installed", "live-rescue-status 0", "live-update-status 1"]))

TESTS.append(("live-installed-boot", True, True,
              ["--boot", TARGET, "wait:" + P, "send:uname -r; mpm info coreutils; test -f /boot/initrd.tar; echo installed-rescue-status $?; sync\\n", "wait:" + P],
              ["0.9.3", "installed files verified", "installed-rescue-status 0"]))


TESTS.append(("mpm-network", False, True,
              ["--prep", with_mpm_repository, "--boot", disk, "wait:" + P,
               "send:echo '10.0.2.2 tls.test' >> /etc/hosts; export SSL_CERT_FILE=/root/test-ca.pem; cp /root/repository.pub /etc/mpm/keys/machos.pub; config set mpm.repository https://tls.test:9443/mpm/stable.toml; mpm refresh; echo refresh-status $?\\n", "wait:" + P,
               "send:mpm list --available; echo available-status $?; mpm list --invalid; echo invalid-list-status $?; mpm search demo; mpm info mpm-demo; mpm install mpm-demo; echo network-install-status $?; cat /opt/mpm/demo /opt/mpm/mpm-helper; mpm info mpm-demo\\n", "wait:" + P,
               "send:echo edited > /etc/mpm-demo.conf; wget -q -O - https://tls.test:9443/mpm/control/upgrade; mpm refresh; mpm upgrade; echo network-upgrade-status $?; cat /opt/mpm/demo /etc/mpm-demo.conf /etc/mpm-demo.conf.mpm-new; sync\\n", "wait:" + P],
              ["refreshed verified stable repository", "refresh-status 0", "mpm-helper 1.0.0 -", "mpm-demo 1.0.0 -", "available-status 0", "invalid-list-status 2", "mpm-demo 1.0.0 -", "installed bytes:", "mpm: installed mpm-demo 1.0.0", "network-install-status 0", "network install", "dependency fetched", "installed files verified", "refreshed verified stable repository", "mpm: installed mpm-demo 1.1.0", "network-upgrade-status 0", "network upgrade", "edited", "setting upgraded"]))


TESTS.append(("mpm-network-reject", False, True,
              ["--prep", with_mpm_repository, "--boot", disk, "wait:" + P,
               "send:echo '10.0.2.2 tls.test' >> /etc/hosts; export SSL_CERT_FILE=/root/test-ca.pem; cp /etc/mpm/keys/machos.pub /root/original.pub; cp /root/repository.pub /etc/mpm/keys/machos.pub; cp /root/repository.pub /etc/mpm/keys/test.pub; config set mpm.repository https://tls.test:9443/mpm/stable.toml; mpm refresh; mpm install mpm-demo; wget -q -O - https://tls.test:9443/mpm/control/upgrade; mpm refresh; cp /var/lib/mpm/repository/stable.toml /tmp/catalogue-before\\n", "wait:" + P,
               "send:wget -q -O - https://tls.test:9443/mpm/control/bad-index; mpm refresh; echo bad-index-status $?; test \"$(sha256sum < /tmp/catalogue-before)\" = \"$(sha256sum < /var/lib/mpm/repository/stable.toml)\"; echo index-cache-kept $?\\n", "wait:" + P,
               "send:wget -q -O - https://tls.test:9443/mpm/control/expired; mpm refresh; echo expired-index-status $?; wget -q -O - https://tls.test:9443/mpm/control/initial; mpm refresh; echo replay-status $?; test \"$(sha256sum < /tmp/catalogue-before)\" = \"$(sha256sum < /var/lib/mpm/repository/stable.toml)\"; echo replay-cache-kept $?\\n", "wait:" + P,
               "send:wget -q -O - https://tls.test:9443/mpm/control/upgrade; cp /root/original.pub /etc/mpm/keys/machos.pub; mpm refresh; echo wrong-official-key-status $?; cp /root/repository.pub /etc/mpm/keys/machos.pub; wget -q --max-size 10 -O /tmp/limited https://tls.test:9443/big; echo download-limit-status $?\\n", "wait:" + P,
               "send:wget -q -O - https://tls.test:9443/mpm/control/bad-package; mpm refresh; mpm install --allow-unsigned mpm-demo; echo bad-network-package-status $?; cat /opt/mpm/demo; mpm info mpm-demo; test -e /var/lib/mpm/transaction; echo network-lock-status $?; sync\\n", "wait:" + P],
              ["bad-index-status 1", "index-cache-kept 0", "expired-index-status 1", "replay-status 1", "replay-cache-kept 0", "wrong-official-key-status 1", "download-limit-status 1", "bad-network-package-status 1", "network install", "mpm-demo 1.0.0", "installed files verified", "network-lock-status 1"]))


TESTS.append(("mpm-system-rollback", False, True,
              ["--prep", lambda: with_mpm_repository(True), "--boot", disk, "wait:" + P,
               "send:echo '10.0.2.2 tls.test' >> /etc/hosts; export SSL_CERT_FILE=/root/test-ca.pem; cp /root/repository.pub /etc/mpm/keys/machos.pub; config set mpm.repository https://tls.test:9443/mpm/stable.toml; wget -q -O - https://tls.test:9443/mpm/control/system; mpm refresh; cp /boot/kernel.elf /tmp/kernel-before; cp /boot/efi/EFI/BOOT/BOOTX64.EFI /tmp/efi-before; mkdir /boot/efi/EFI/BOOT/MPMNEW.EFI; echo block > /boot/efi/EFI/BOOT/MPMNEW.EFI/block; mpm upgrade; echo failed-system-status $?; cat /etc/mpm/os-version\\n", "wait:" + P,
               "send:rm -r /boot/efi/EFI/BOOT/MPMNEW.EFI; mpm recover; echo system-recover-status $?; test \"$(sha256sum < /boot/kernel.elf)\" = \"$(sha256sum < /tmp/kernel-before)\"; echo kernel-restored $?; test \"$(sha256sum < /boot/efi/EFI/BOOT/BOOTX64.EFI)\" = \"$(sha256sum < /tmp/efi-before)\"; echo efi-restored $?; mpm info machos-kernel; test -e /var/lib/mpm/transaction; echo recovered-lock-status $?; sync\\n", "wait:" + P],
              ["rollback incomplete", "failed-system-status 1", "0.9.3", "system-recover-status 0", "kernel-restored 0", "efi-restored 0", "installed files verified", "recovered-lock-status 1"]))


TESTS.append(("mpm-system", False, True,
              ["--prep", lambda: with_mpm_repository(True), "--boot", disk, "wait:" + P,
               "send:echo '10.0.2.2 tls.test' >> /etc/hosts; export SSL_CERT_FILE=/root/test-ca.pem; cp /root/repository.pub /etc/mpm/keys/machos.pub; config set mpm.repository https://tls.test:9443/mpm/stable.toml; mpm refresh; wget -q -O - https://tls.test:9443/mpm/control/system; mpm refresh; echo system-refresh-status $?\\n", "wait:" + P,
               "send:sha256sum /boot/kernel.elf; mpm upgrade; echo system-upgrade-status $?; cat /etc/mpm/os-version; mpm info machos-kernel; grep 'MachOS (previous)' /boot/machboot.conf; sync\\n", "wait:" + P],
              ["system-refresh-status 0", "system upgrade committed; reboot to start 0.9.4", "system-upgrade-status 0", "0.9.4", "installed files verified", "entry MachOS (previous)"]))

TESTS.append(("mpm-system-boot", True, True,
              ["--boot", disk, "wait:" + P, "send:uname -r; cat /etc/mpm/os-version; mpm info machos-kernel; mpm search coreutils; sync\\n", "wait:" + P],
              ["0.9.4", "0.9.4", "installed files verified", "coreutils 0.9.4 -"]))


TESTS.append(("truecolor", False, True,
              ["--post", check_truecolor, "wait:" + P,
               "send:printf '\\\\e[2J\\\\e[H\\\\e[38;2;18;52;86;48;2;101;67;33mRGB\\\\e[0m\\\\e[3;1H\\\\e[48;2;17;34;51m    \\\\e[48;5;196m    \\\\e[48;5;244m    \\\\e[0m\\\\n'\\n",
               "wait:" + P, "shot:out/headless/truecolor.png"], ["RGB"]))


TESTS.append(("admin", False, True,
              ["--prep", with_mpm, "--boot", disk, "wait:" + P,
               "send:cd /bin; tar -xf /bin/mpm-fixtures.tar; cp /bin/test.pub /etc/mpm/keys/test.pub; cd /root\\n", "wait:" + P,
               "send:adduser --admin tester\\n", "wait:New password: ",
               "send:\\n", "wait:Password cannot be empty.", "wait:New password: ",
               "send:testing\\n", "wait:Again: ", "send:testing\\n", "wait:" + P,
               "send:su tester\\n", "wait:tester@.*[$] ",
               "send:se id\\n", "wait:Password: ", "send:wrong\\n", "wait:authentication failed", "wait:" + P,
               "send:se id\\n", "wait:Password: ", "send:testing\\n", "wait:uid=0", "wait:" + P,
               "send:se id; se -k\\n", "wait:uid=0", "wait:" + P,
               "send:se id\\n", "wait:Password: ", "send:testing\\n", "wait:uid=0", "wait:" + P,
               "send:se mpm install --allow-unsigned /bin/unsigned.mpa\\n", "wait:Password: ", "send:testing\\n", "wait:mpm: installed mpm-unsigned", "wait:" + P,
               "send:export MPM_UNSAFE=yes; se env | grep MPM_UNSAFE; echo leaked-env $?\\n", "wait:" + P,
               "send:se usermod --no-admin tester; se id; echo revoked-status $?; mpm remove mpm-unsigned; echo direct-status $?\\n", "wait:" + P,
               "send:exit\\n", "wait:root@.*# ",
               "send:usermod --admin tester; usermod --admin tester; config set se.timeout 0; config get se.timeout; config set wget.redirects 2; config get wget.redirects; config set se.timeout 1; su tester\\n", "wait:tester@.*[$] ",
               "send:se id\\n", "wait:Password: ", "send:testing\\n", "wait:uid=0", "wait:" + P,
               "sleep:2", "send:se id\\n", "wait:Password: ", "send:testing\\n", "wait:uid=0", "wait:" + P,
               "send:exit\\n", "wait:root@.*# ", "send:sync\\n", "wait:" + P],
              ["Password cannot be empty.", "adduser: added tester", "se: authentication failed", "uid=0(root)", "uid=0(root)", "uid=0(root)", "mpm: installed mpm-unsigned", "leaked-env 1", "user is not an administrator", "revoked-status 1", "direct-status 1", "config: saved", "0", "config: saved", "2", "uid=0(root)", "uid=0(root)"]))


for cores in (2, 4):
    TESTS.append((f"io-memory-{cores}", False, True,
                  ["--prep", with_iommtest, "--boot", disk,
                   "--qemu=-m", "--qemu=512M", "--qemu=-smp", f"--qemu={cores}",
                   "wait:" + P, "send:iommtest; echo io-memory-status $?\\n", "wait:" + P,
                   "send:cat /proc/kernel-work\\n", "wait:" + P],
                  ["ok parallel memory initialization", "ok overlapping file and memory work",
                   "io-memory-status 0", "io_releases", "memory_releases", "max_parallel"]))


# Reuse protocol scenarios with an explicit Virtio NIC, never QEMU's default NIC.
for source, name, adapter in (("tcp", "virtio-net", "virtio"),
                              ("https", "virtio-https", "virtio"),
                              ("https", "virtio-modern-https", "virtio-modern")):
    _, keep, fsck, steps, expect = next(test for test in TESTS if test[0] == source)
    extra = []
    if source == "tcp":
        extra = ["mon:set_link nic0 off", "sleep:0.5",
                 "send:ping -c 1 10.0.2.2; echo virtio-link-down $?\\n", "wait:" + P,
                 "mon:set_link nic0 on", "sleep:0.5",
                 "send:ping -c 1 10.0.2.2; echo virtio-link-up $?; ip; ip stats\\n", "wait:" + P]
    TESTS.append((name, keep, fsck, ["--nic", adapter] + steps + extra,
                  ["[1af4:1041]" if adapter == "virtio-modern" else "[1af4:1000]",
                   "virtio-net: modern PCI, polled RX/TX ready", "net: eth0: Virtio-net"] + expect +
                  (["virtio-link-down 1", "virtio-link-up 0", "address 10.0.2.15/24"] if extra else [])))


# FAT/exFAT retain their supported name limit without creating names that truncate on remount.
for index, (name, keep, fsck, steps, expect) in enumerate(TESTS):
    if name in ("fat-usb", "exfat-usb"):
        long_name = "x" * 60
        TESTS[index] = (name, keep, fsck,
                        steps + ["send:mount /dev/sdb /mnt; touch /mnt/" + long_name +
                                 "; echo long-create-status $?; echo kept > /mnt/boundary; mv /mnt/boundary /mnt/" +
                                 long_name + "; echo long-rename-status $?; cat /mnt/boundary; rm /mnt/boundary; umount /mnt\\n",
                                 "wait:" + P],
                        expect + ["long-create-status 1", "long-rename-status 1", "kept"])


# Verify the shipped trust store and updater on every system-copy path.
import hashlib
with open(os.path.join(ROOT, "root", "etc", "ssl", "certs", "ca-certificates.crt"), "rb") as source:
    CA_BUNDLE_SHA256 = hashlib.sha256(source.read()).hexdigest()
for index, (name, keep, fsck, steps, expect) in enumerate(TESTS):
    if name in ("live-iso", "update", "update-reboot", "usb-installed-boot",
                "nvme-installed-boot", "installed-boot"):
        TESTS[index] = (name, keep, fsck,
                        steps + ["send:sha256sum /etc/ssl/certs/ca-certificates.crt; ca-update --provider unsupported; test \"$?\" = 2; echo ca-tool-status $?\\n", "wait:" + P],
                        expect + [CA_BUNDLE_SHA256, "usage: ca-update", "ca-tool-status 0"])


# System-copy paths must retain licence notices, the MOTD and the installer.
NOTICE_FILES = sorted(os.path.relpath(os.path.join(directory, name), os.path.join(ROOT, "root"))
                      for directory, _, names in os.walk(os.path.join(ROOT, "root", "usr", "share", "licenses"))
                      for name in names)
NOTICE_FILES.extend(("etc/motd", "bin/install"))
NOTICE_HASHES = []
for path in NOTICE_FILES:
    with open(os.path.join(ROOT, "root", path), "rb") as source:
        NOTICE_HASHES.append(hashlib.sha256(source.read()).hexdigest() + "  /" + path)
for index, (name, keep, fsck, steps, expect) in enumerate(TESTS):
    if name in ("live-iso", "update", "update-reboot", "usb-installed-boot",
                "nvme-installed-boot", "installed-boot"):
        TESTS[index] = (name, keep, fsck,
                        steps + ["send:sha256sum " + " ".join("/" + path for path in NOTICE_FILES) + "\\n",
                                 "wait:" + P, "send:ls -l /bin/su /bin/passwd\\n", "wait:" + P],
                        expect + NOTICE_HASHES + ["-rwsr-xr-x", "-rwsr-xr-x"])


def run(name, keep, fsck, steps, expect, profile):
    cmd = [os.path.join(ROOT, "tools/headless.py"), "-p", profile, "-t", "60" if name.startswith("mpm") or name in ("admin", "password-migration", "install") else "240"]
    if keep:
        cmd.append("--keep")
    if fsck:
        cmd.append("--fsck")
    # leading "--opt value" pairs go to headless.py; "blank:PATH" makes an
    # empty 512 MiB disk there first. --prep FN runs FN first, --post FN runs
    # it after (it returns "" or a problem)
    steps = list(steps)
    post = None
    nologin = False
    while steps and isinstance(steps[0], str) and steps[0].startswith("--"):
        opt, val = steps[0], steps[1]
        if opt == "--prep":
            val()
            steps = steps[2:]
            continue
        if opt == "--post":
            post = val
            steps = steps[2:]
            continue
        if opt == "--nologin":  # the test logs in itself (or never)
            nologin = True
            steps = steps[1:]
            continue
        if opt == "--nvme" or "=" in opt:  # takes no value (or has it inside)
            cmd.append(opt)
            steps = steps[1:]
            continue
        if callable(val):
            val = val()
        if opt == "--rescue":
            cmd.append("--rescue")
            steps = steps[2:]
            continue
        if val.startswith("blank:"):
            val = val[len("blank:"):]
            with open(val, "wb") as f:
                f.truncate(512 << 20)
        if opt == "--disk2" and "--disk2" in cmd:  # a later --disk2 replaces an earlier one
            i = cmd.index("--disk2")
            del cmd[i:i + 2]
        cmd += [opt, val]
        steps = steps[2:]
    # preparations can discover a dispatcher's test count before its steps are expanded.
    steps = [part for step in steps for part in (step() if callable(step) else [step])]
    # every boot but rescue mode starts at a login prompt: log in as root
    if "--rescue" not in cmd and not nologin:
        steps = ["login:root:machos"] + steps
    # headless.py's progress (stderr) is shown live, so a stuck step is seen
    # as it happens; its stdout (the serial log) is kept for the checks
    proc = subprocess.Popen(cmd + steps, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    err_lines = []

    def pump():
        for line in proc.stderr:
            err_lines.append(line)
            if not line.startswith("building "):  # the compiler's per-program lines
                print(f"     | {line.rstrip()}", flush=True)
    t = threading.Thread(target=pump, daemon=True)
    t.start()
    out = proc.stdout.read()
    proc.wait()
    t.join()

    class R:
        pass
    r = R()
    r.returncode = proc.returncode
    r.stderr = "".join(err_lines)
    r.stdout = out
    problems = []
    if r.returncode == 1:
        problems.append("timed out")
    elif r.returncode == 2:
        problems.append("kernel panic or exception")
    elif r.returncode == 3:
        problems.append("e2fsck found problems")
    elif r.returncode != 0:
        problems.append(f"headless.py failed ({r.returncode}): {r.stderr.strip()[-300:]}")
    if post is not None:
        why = post()
        if why:
            problems.append(why)
    os.makedirs(os.path.join(ROOT, "out", "headless"), exist_ok=True)
    with open(os.path.join(ROOT, "out", "headless", name + ".log"), "w") as f:
        f.write(out)
    if name == "https" or name.endswith("-https"):
        timings = [float(value) for value in re.findall(r"\breal\s+(\d+\.\d+)s", out)]
        if len(timings) < 2:
            problems.append("TLS handshake timing results are missing")
        else:
            print(f"     | TLS small-file times: ECDSA {timings[0]:.3f}s, RSA {timings[1]:.3f}s", flush=True)
            if profile == "release" and max(timings[:2]) > 5:
                problems.append("a release TLS handshake exceeded five seconds under QEMU")
    if name == "tlstest":
        selected = [i for i, label in enumerate(TLS_SUITE)
                    if label.startswith(("crypto.", "pki.", "tls.", "http."))]
        results = list(re.finditer(r"^(ok|FAIL|HANG) (\d+)(.*)$", out, re.MULTILINE))
        failures = [match for match in results if match[1] != "ok"]
        for match in failures:
            print(f"     | {match[1]} {match[2]} {TLS_SUITE[int(match[2])]}{match[3]}", flush=True)
        summary = f"upstream libraries: {len(results) - len(failures)} passed, " + \
                  f"{sum(match[1] == 'FAIL' for match in failures)} failed, " + \
                  f"{sum(match[1] == 'HANG' for match in failures)} hung"
        print("     | " + summary, flush=True)
        if failures or len(results) != len(selected):
            problems.append(summary + f" ({len(results)}/{len(selected)} completed)")
    if name == "stdtest":
        with open(os.path.join(ROOT, "out", "headless", "stdtest.log"), "w") as f:
            f.write(out)
        results = list(re.finditer(r"^(ok|FAIL|HANG) (\d+)(.*)$", out, re.MULTILINE))
        failures = [match for match in results if match[1] != "ok"]
        for match in failures:
            index = int(match[2])
            label = STD_SUITE[index] if index < len(STD_SUITE) else "unknown test"
            print(f"     | {match[1]} {index} {label}{match[3]}", flush=True)
        summary = re.search(r"runtests: (\d+) passed, (\d+) failed, (\d+) hung", out)
        if summary:
            print("     | " + summary[0], flush=True)
            completed = sum(int(summary[i]) for i in (1, 2, 3))
            indices = [int(match[2]) for match in results]
            if (completed != len(STD_SUITE) or indices != list(range(len(STD_SUITE)))
                    or failures or int(summary[2]) or int(summary[3])):
                problems.append(summary[0] + f" ({len(results)}/{len(STD_SUITE)} completed)")
        else:
            problems.append("std test dispatcher did not finish")
    at = 0
    for want in expect:
        i = out.find(want, at)
        if i < 0:
            problems.append(f"missing {want!r}")
        else:
            at = i + len(want)
    return problems, out, r.stderr


def main():
    import json
    ap = argparse.ArgumentParser()
    ap.add_argument("-p", "--profile", choices=("debug", "release"), default="debug")
    ap.add_argument("names", nargs="*")
    a = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # progress shows even when logged to a file
    unknown = set(a.names) - {test[0] for test in TESTS}
    if unknown:
        ap.error("unknown tests: " + ", ".join(sorted(unknown)))
    chosen = [test for test in TESTS if not a.names or test[0] in a.names]
    PROFILE[0] = a.profile
    # Fixture keys and settings must never leak into the interactive dev disk.
    os.environ["MACHOS_IMAGE_ROOT"] = os.path.join(ROOT, "out", "headless", "images")
    subprocess.run(["tools/build.sh", a.profile], cwd=ROOT, check=True)
    os.environ["MACHOS_PREBUILT_PROFILE"] = a.profile
    failed = 0
    results = []
    for name, keep, fsck, steps, expect in chosen:
        print(f"run  {name}", flush=True)
        t0 = time.time()
        try:
            problems, out, err = run(name, keep, fsck, steps, expect, a.profile)
        except Exception as error:
            problems, out, err = ["preparation failed: " + str(error)], "", ""
        results.append({"name": name, "passed": not problems, "problems": problems,
                        "seconds": round(time.time() - t0, 2)})
        os.makedirs(os.path.join(ROOT, "out", "headless"), exist_ok=True)
        with open(os.path.join(ROOT, "out", "headless", f"results-{a.profile}.json"), "w") as output:
            json.dump(results, output, indent=2)
        took = f"({time.time() - t0:.0f} s)"
        if problems:
            failed += 1
            print(f"FAIL {name} {took}: " + "; ".join(problems))
            tail = "\n".join(out.splitlines()[-15:])
            print("     " + tail.replace("\n", "\n     "))
            if "e2fsck" in "".join(problems):
                print("     " + "\n     ".join(err.splitlines()[-12:]))
        else:
            print(f"ok   {name} {took}")
    import json
    os.makedirs(os.path.join(ROOT, "out", "headless"), exist_ok=True)
    with open(os.path.join(ROOT, "out", "headless", f"results-{a.profile}.json"), "w") as output:
        json.dump(results, output, indent=2)
    print(f"{len(chosen) - failed}/{len(chosen)} passed ({a.profile})")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
