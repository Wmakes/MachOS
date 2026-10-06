#!/usr/bin/env python3
"""Boot MachOS under QEMU with no window and drive it from a script.

usage: tools/headless.py [-p release] [-t SECONDS] [--keep] [--fsck] STEP...

The disk is rebuilt fresh unless --keep is given (then files written by an
earlier run are still there). --fsck runs e2fsck on the root partition after
QEMU stops and fails if it finds problems.

steps run in order:
  wait:REGEX     wait until the serial output (since the last wait) matches
  send:TEXT      type TEXT on the serial port (\\n is Enter, \\x03 is ^C)
  keys:TEXT      type TEXT on the PS/2 keyboard (via the QEMU monitor)
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
    ap.add_argument("steps", nargs="*")
    a = ap.parse_args()

    os.chdir(ROOT)
    mk = ["tools/mkimage.sh", a.profile] + ([] if a.keep else ["--fresh"])
    img = subprocess.run(mk, check=True, stdout=subprocess.PIPE, text=True).stdout.strip().splitlines()[-1]
    out = os.path.join("out", "headless")
    os.makedirs(out, exist_ok=True)
    ser, mon = os.path.join(out, "serial.sock"), os.path.join(out, "monitor.sock")
    for p in (ser, mon):
        if os.path.exists(p):
            os.remove(p)
    vars_fd = os.path.join(out, "vars.fd")
    if not os.path.exists(vars_fd):
        subprocess.run(["cp", OVMF_VARS, vars_fd], check=True)

    qemu = subprocess.Popen([
        "qemu-system-x86_64", "-M", "q35", "-m", "256M", "-display", "none",
        "-drive", f"if=pflash,format=raw,readonly=on,file={OVMF_CODE}",
        "-drive", f"if=pflash,format=raw,file={vars_fd}",
        "-drive", f"format=raw,file={img}",
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
            while True:
                try:
                    d = s.recv(4096)
                except OSError:
                    return
                if not d:
                    return
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
        for step in a.steps:
            kind, _, arg = step.partition(":")
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
            elif kind == "send":
                for ch in arg.encode().decode("unicode_escape"):
                    s.sendall(b"\r" if ch == "\n" else ch.encode())
                    time.sleep(0.01)
            elif kind == "keys":
                for ch in arg.encode().decode("unicode_escape"):
                    monitor("sendkey " + keyname(ch))
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
        part = os.path.join(out, "root.img")
        subprocess.run(["dd", f"if={img}", f"of={part}", "bs=512", "skip=67584",
                        "count=188416", "status=none"], check=True)
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
