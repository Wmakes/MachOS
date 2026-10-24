#!/usr/bin/env python3
"""Boot MachOS under QEMU with no window and drive it from a script.

usage: tools/headless.py [-p release] [-t SECONDS] [--keep] [--fsck] [--disk2 [nvme:]IMG]
                         [--boot IMG] [--rescue] [--nvme]
                         [--nic virtio|virtio-modern] [--qemu ARG]... STEP...

The cpu is QEMU's "max" (with SMEP/SMAP) unless QEMU_CPU says otherwise.
The disk is rebuilt fresh unless --keep is given (then files written by an
earlier run are still there). --fsck runs e2fsck on the root partition after
QEMU stops and fails if it finds problems.

steps run in order:
  wait:REGEX     wait until the serial output (since the last wait) matches
  login:USER:PW  log in, setting the required password on a fresh image
  send:TEXT      type TEXT on the serial port (\\n is Enter, \\x03 is ^C)
  keys:TEXT      type TEXT on the PS/2 keyboard (via the QEMU monitor);
                 <up>, <left>, <tab>, ... press named keys,
                 <x 900> holds x down for 900 ms
  mon:COMMAND    a QEMU monitor command (device_add, device_del, ...)
  sleep:SECONDS  pause
  shot:PATH      save a PNG screenshot of the framebuffer

The whole serial log is printed at the end. Exit status is 1 if a wait
timed out, 2 if the kernel panicked or took an unhandled exception, 3 if
e2fsck found problems.
"""
import argparse, os, re, socket, subprocess, sys, threading, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OVMF_CODE = os.environ.get("OVMF_CODE", "/usr/share/edk2/x64/OVMF_CODE.4m.fd")
OVMF_VARS = os.environ.get("OVMF_VARS", "/usr/share/edk2/x64/OVMF_VARS.4m.fd")

KEYNAMES = {" ": "spc", "\n": "ret", "-": "minus", "=": "equal", ".": "dot",
            ",": "comma", "/": "slash", ";": "semicolon", "'": "apostrophe",
            "[": "bracket_left", "]": "bracket_right", "\\": "backslash",
            "`": "grave_accent", "\t": "tab", "\b": "backspace"}
SHIFTED = {"|": "backslash", ">": "dot", "<": "comma", "_": "minus", "+": "equal",
           ":": "semicolon", '"': "apostrophe", "?": "slash", "!": "1", "@": "2",
           "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9",
           ")": "0", "{": "bracket_left", "}": "bracket_right", "~": "grave_accent"}


def keyname(c):
    if ord(c) < 32 and c not in KEYNAMES:
        return "ctrl-" + chr(ord(c) + 96)
    if c.isalpha() and c.isupper():
        return "shift-" + c.lower()
    if c in SHIFTED:
        return "shift-" + SHIFTED[c]
    return KEYNAMES.get(c, c)


started = [time.time()]


def progress(what):
    """what the run is doing, live on stderr (tools/test.py shows it)"""
    print(f"[{time.time() - started[0]:6.1f}s] {what}", file=sys.stderr, flush=True)


def disk_args(path, ident, nvme):
    """QEMU arguments attaching a raw image as a SATA (q35's AHCI) or NVMe
    disk; an "nvme4k:" path is an NVMe disk with 4 KiB sectors"""
    if path.startswith("nvme4k:"):
        path = path[len("nvme4k:"):]
        return ["-drive", f"format=raw,file={path},if=none,id={ident}",
                "-device", f"nvme,serial=machos-{ident},drive={ident},"
                           "logical_block_size=4096,physical_block_size=4096"]
    if not nvme:
        return ["-drive", f"format=raw,file={path}"]
    return ["-drive", f"format=raw,file={path},if=none,id={ident}",
            "-device", f"nvme,serial=machos-{ident},drive={ident}"]


def connect(path, deadline):
    while True:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(path)
            return s
        except OSError:
            if time.time() > deadline:
                raise
            time.sleep(0.05)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-p", "--profile", default="debug")
    ap.add_argument("-t", "--timeout", type=float, default=20.0)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--fsck", action="store_true")
    ap.add_argument("--disk2", help="attach this raw image as a second disk")
    ap.add_argument("--boot", help="boot this image as it is instead of building one")
    ap.add_argument("--rescue", action="store_true", help="boot the rescue entry")
    ap.add_argument("--nvme", action="store_true", help="attach the boot disk as NVMe instead of SATA")
    ap.add_argument("--nic", choices=("virtio", "virtio-modern"),
                    help="replace QEMU's default NIC with a Virtio network device")
    ap.add_argument("--qemu", action="append", default=[], help="one more QEMU argument")
    ap.add_argument("steps", nargs="*")
    a = ap.parse_args()

    os.chdir(ROOT)
    if a.boot:
        img = a.boot
    else:
        progress("building the disk image")
        mk = ["tools/mkimage.sh", a.profile] + ([] if a.keep else ["--fresh"]) + (["--rescue"] if a.rescue else [])
        img = subprocess.run(mk, check=True, stdout=subprocess.PIPE, text=True).stdout.strip().splitlines()[-1]
    out = os.path.join("out", "headless")
    os.makedirs(out, exist_ok=True)
    ser, mon = os.path.join(out, "serial.sock"), os.path.join(out, "monitor.sock")
    for p in (ser, mon):
        if os.path.exists(p):
            os.remove(p)
    # fresh firmware variables every time: old boot entries must not decide
    vars_fd = os.path.join(out, "vars.fd")
    subprocess.run(["cp", OVMF_VARS, vars_fd], check=True)

    progress("starting QEMU")
    started[0] = time.time()
    qemu = subprocess.Popen([
        "qemu-system-x86_64", "-M", "q35", "-cpu", os.environ.get("QEMU_CPU", "max"), "-m", "256M",
        "-display", "none",
        "-drive", f"if=pflash,format=raw,readonly=on,file={OVMF_CODE}",
        "-drive", f"if=pflash,format=raw,file={vars_fd}",
        *disk_args(img, "boot", a.nvme),
        *(disk_args(a.disk2.removeprefix("nvme:"), "second", a.disk2.startswith("nvme:")) if a.disk2 else []),
        *(["-netdev", "user,id=net0", "-device",
           "virtio-net-pci,netdev=net0,id=nic0" +
           (",disable-legacy=on" if a.nic == "virtio-modern" else "")] if a.nic else []),
        *a.qemu,
        "-chardev", f"socket,id=s0,path={ser},server=on,wait=off",
        "-serial", "chardev:s0",
        "-monitor", f"unix:{mon},server,nowait",
        "-no-reboot", "-no-shutdown"])

    log = bytearray()
    lock = threading.Lock()
    status = 0
    try:
        deadline = time.time() + 10
        s = connect(ser, deadline)
        m = connect(mon, deadline)
        m.settimeout(0.2)

        def reader():
            # Retain guest diagnostics even when a login step raises or is interrupted.
            with open(os.path.join(out, "serial-live.log"), "wb", buffering=0) as live:
                while True:
                    try:
                        d = s.recv(4096)
                    except OSError:
                        return
                    if not d:
                        return
                    live.write(d)
                    with lock:
                        log.extend(d)
        threading.Thread(target=reader, daemon=True).start()

        def monitor(cmd):
            m.sendall((cmd + "\n").encode())
            time.sleep(0.05)
            try:
                while m.recv(4096):
                    pass
            except OSError:
                pass

        mark = 0

        def wait_for(pattern):
            nonlocal mark
            rx = re.compile(pattern.encode())
            end = time.time() + a.timeout
            while time.time() <= end:
                with lock:
                    hit = rx.search(log, mark)
                    bad = re.search(rb"panic:|\*\*\* exception", log)
                if hit:
                    mark = hit.end()
                    return hit
                if bad:
                    raise RuntimeError("kernel failure during login")
                time.sleep(0.05)
            raise RuntimeError("timed out waiting for " + pattern)

        def type_line(value):
            for ch in value + "\n":
                s.sendall(b"\r" if ch == "\n" else ch.encode())
                time.sleep(0.01)

        for step in a.steps:
            kind, _, arg = step.partition(":")
            progress(step if len(step) < 100 else step[:97] + "...")
            if kind == "wait":
                rx = re.compile(arg.encode())
                end = time.time() + a.timeout
                while True:
                    with lock:
                        hit = rx.search(log, mark)
                        bad = re.search(rb"panic:|\*\*\* exception", log)
                    if hit:
                        mark = hit.end()
                        break
                    if bad:
                        status = 2
                        break
                    if time.time() > end:
                        print(f"headless: timed out waiting for {arg!r}", file=sys.stderr)
                        status = 1
                        break
                    time.sleep(0.05)
                if status:
                    break
            elif kind == "login":
                user, password = arg.split(":", 1)
                wait_for("login: ")
                type_line(user)
                prompt = wait_for("New password: |Password: ")
                type_line(password)
                if prompt.group() == b"New password: ":
                    wait_for("Again: ")
                    type_line(password)
                result = wait_for(r"Login incorrect|[^\r\n ]+@[^\r\n :]+:[^\r\n]*[$#] ")
                if result.group() == b"Login incorrect":
                    raise RuntimeError("login failed for " + user)
                mark = result.start()  # leave the shell prompt for the next wait step
            elif kind == "send":
                for ch in arg.encode().decode("unicode_escape"):
                    s.sendall(b"\r" if ch == "\n" else ch.encode())
                    time.sleep(0.01)
            elif kind == "keys":
                # <name> presses a named key (<up>, <left>, <tab>, ...);
                # <name MS> holds it down for MS milliseconds
                for tok in re.findall(r"<[a-z_0-9-]+(?: [0-9]+)?>|.", arg.encode().decode("unicode_escape"), re.S):
                    if len(tok) > 2 and tok[0] == "<":
                        monitor("sendkey " + tok[1:-1])
                        if " " in tok:
                            time.sleep(int(tok[1:-1].split()[1]) / 1000 + 0.1)
                    else:
                        monitor("sendkey " + keyname(tok))
            elif kind == "mon":
                monitor(arg)
            elif kind == "sleep":
                time.sleep(float(arg))
            elif kind == "shot":
                ppm = os.path.join(out, "shot.ppm")
                monitor("screendump " + ppm)
                time.sleep(0.5)
                subprocess.run(["python3", "-c",
                                "import sys;from PIL import Image;"
                                "Image.open(sys.argv[1]).save(sys.argv[2])", ppm, arg],
                               check=True)
            else:
                sys.exit(f"headless: unknown step {step!r}")
        time.sleep(0.2)
    finally:
        qemu.kill()
        qemu.wait()
    if a.fsck:
        # A live installer boots an ISO but writes the attached target disk.
        check_img = img
        if img.endswith(".iso") and a.disk2:
            check_img = a.disk2.removeprefix("nvme:")
        part = os.path.join(out, "root.img")
        info = subprocess.run(["sgdisk", "-i", "2", check_img], stdout=subprocess.PIPE, text=True).stdout
        first = int(re.search(r"First sector: (\d+)", info).group(1))
        last = int(re.search(r"Last sector: (\d+)", info).group(1))
        subprocess.run(["dd", f"if={check_img}", f"of={part}", "bs=512", f"skip={first}",
                        f"count={last - first + 1}", "status=none"], check=True)
        fsck = subprocess.run(["e2fsck", "-fn", part], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True)
        os.remove(part)
        print(fsck.stdout, file=sys.stderr, end="")
        if fsck.returncode != 0 and status == 0:
            status = 3
    text = log.decode("utf-8", "replace").replace("\r", "")
    text = re.sub(r"\x1b\[[0-9;=?]*[A-Za-z]", "", text)
    sys.stdout.write(text)
    if not text.endswith("\n"):
        print()
    sys.exit(status)


if __name__ == "__main__":
    main()
