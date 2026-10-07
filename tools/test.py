#!/usr/bin/env python3
"""Boot MachOS under QEMU and check it end to end.

usage: tools/test.py [-p release] [NAME...]     (no names: run every test)

Each test boots a fresh disk (or the previous test's disk, for `keep`),
types commands over the serial port, and checks that the output contains
every expected string, in order. Tests marked `fsck` also run e2fsck on the
root partition afterwards.
"""
import argparse, os, re, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = r"[$#] "  # the shell prompt (# for root)
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
    return os.path.join(ROOT, "out", PROFILE[0], "disk.img")


def with_posixtest():
    """a fresh dev disk with examples/posixtest on it as /bin/posixtest"""
    p = PROFILE[0]
    sh(f"tools/mkimage.sh {p} --fresh")
    sh(f"cd examples/posixtest && mach dep pull . && mach build . -t machos -p {p}")
    sh(f"tools/put.sh examples/posixtest/out/machos/{p}/posixtest /bin/posixtest {disk()}")


def make_broken():
    """a copy of a fresh dev disk with three faults put in by debugfs"""
    sh("tools/mkimage.sh --fresh")
    sh(f"cp out/debug/disk.img {BROKEN}")
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

USB_HUB = ["--qemu=-machine", "--qemu=i8042=off", "--qemu=-device", "--qemu=qemu-xhci,id=xhci",
           "--qemu=-device", "--qemu=usb-hub,bus=xhci.0,port=2",
           "--qemu=-device", "--qemu=usb-hub,bus=xhci.0,port=2.1",
           "--qemu=-device", "--qemu=usb-mouse,bus=xhci.0,port=2.2",
           "--qemu=-device", "--qemu=usb-kbd,bus=xhci.0,port=2.1.4"]

TESTS = [
    # name, keep previous disk, fsck, steps, expected output in order
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

    ("tools", False, False,
     ["wait:" + P,
      "send:printf 'pear\\\\napple\\\\nfig\\\\napple\\\\n' > /tmp/f; sort -u /tmp/f | tr a-z A-Z; seq 1 20 | sort -n -r | head -2\\n",
      "wait:" + P,
      "send:seq 1 6 > /tmp/a; printf '1\\\\n2\\\\nX\\\\n4\\\\n5\\\\n6\\\\n' > /tmp/b; diff /tmp/a /tmp/b; echo diff=$?\\n",
      "wait:diff=", "wait:" + P,
      "send:find / -name '*.conf'; ls /etc | xargs echo got; seq 1 50 | tail -2 | tee /tmp/t | wc -l; basename /a/b.txt .txt; df\\n",
      "wait:" + P],
     ["APPLE", "FIG", "PEAR", "20", "19", "-3", "+X", "diff=1", "/boot/limine.conf", "got group motd passwd shadow",
      "      2", "b", "/dev/sda2", "%"]),

    ("jobs-and-ctrl-c", False, False,
     ["wait:" + P, "send:sleep 1 &\\n", "wait:" + P, "send:ps\\n", "wait:" + P,
      "sleep:1.5", "send:cat\\n", "sleep:0.5", "send:\\x03", "wait:" + P,
      "send:seq 1 100000000 | wc\\n", "sleep:1", "send:\\x03", "wait:" + P,
      "send:echo alive\\n", "wait:alive\\r\\n.*" + P],
     ["sleep", "running", "^C", "Done", "^C", "alive"]),

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

    ("posix-fds", False, False,
     ["--prep", with_posixtest, "--boot", disk,
      "wait:" + P, "send:posixtest\\n", "wait:failures",
      "send:cat /nope 2>/tmp/e; cat /tmp/e; cat /nope 2>&1 | wc -l; ls /etc /nope > /tmp/o 2>&1; cat /tmp/o\\n",
      "wait:" + P,
      "send:echo to-stderr >&2 2>/dev/null; echo kept 3>/tmp/three; cat < /etc/motd 0<&- | wc -c\\n", "wait:" + P],
     ["exec-works", "posixtest: 0 failures", "cat: /nope: no such file", "      1", "no such file", "motd",
      "kept"]),

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
     ["wait:" + P, "send:id; adduser alice; ls -l /home\\n", "wait:" + P,
      "send:passwd alice\\n", "wait:New password: ", "send:secret\\n", "wait:Again: ", "send:secret\\n",
      "wait:" + P,
      "send:su alice\\n", "wait:" + P,
      "send:id; cat /etc/shadow; touch /x; echo hi > ~/f; cat ~/f; kill 1; exit\\n", "wait:# ",
      "send:exit\\n", "wait:login: ", "send:alice\\n", "wait:Password: ", "send:wrong\\n",
      "wait:incorrect", "wait:login: ", "send:alice\\n", "wait:Password: ", "send:secret\\n",
      "wait:" + P, "send:pwd; ls -l /bin/su\\n", "wait:" + P],
     ["uid=0(root)", "added alice (uid 1000", "alice    alice", "password changed",
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
     ["--post", lambda: "" if "clean" in root_state("out/debug/disk.img") and "not clean" not in root_state("out/debug/disk.img") else "root not clean after poweroff",
      "wait:" + P, "send:echo hi > /tmp/z; poweroff\\n", "wait:power: off", "sleep:1"],
     ["power: off"]),

    # install onto a blank second disk, then boot that disk on its own
    ("install", False, False,
     ["--disk2", "blank:" + TARGET,
      "wait:" + P, "send:ls /dev; install /dev/sda\\n", "wait:continue: ", "send:no\\n", "wait:" + P,
      "send:install /dev/sdb\\n", "wait:continue: ", "send:yes\\n", "wait:" + P,
      "send:ls /dev; mount /dev/sdb2 /mnt; ls /mnt; umount /dev/sdb2; ls /mnt; echo unmounted\\n", "wait:" + P],
     ["sdb", "/dev/sdb2 MachOS root 446 MiB", "mkfat: FAT32", "mkext2:", "copying the system",
      "is installed on /dev/sdb", "sdb1", "sdb2", "bin/", "unmounted"]),

    ("nvme-boot", False, False,
     ["--nvme", "wait:" + P, "send:df; ls /dev; echo \"[$(partname /dev/nvme0n1 2)]\"\\n", "wait:" + P],
     ["disk: nvme0n1", "root: ext2 on /dev/nvme0n1p2", "/dev/nvme0n1p2", "nvme0n1p1",
      "[/dev/nvme0n1p2]"]),

    ("nvme-install", False, False,
     ["--disk2", "blank:" + NVME_TARGET, "--disk2", "nvme:" + NVME_TARGET,
      "wait:" + P, "send:install /dev/nvme0n1\\n", "wait:continue: ", "send:yes\\n", "wait:installed on",
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


def run(name, keep, fsck, steps, expect, profile):
    cmd = [os.path.join(ROOT, "tools/headless.py"), "-p", profile, "-t", "240"]
    if keep:
        cmd.append("--keep")
    if fsck:
        cmd.append("--fsck")
    # leading "--opt value" pairs go to headless.py; "blank:PATH" makes an
    # empty 512 MiB disk there first. --prep FN runs FN first, --post FN runs
    # it after (it returns "" or a problem)
    steps = list(steps)
    post = None
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
    # every boot but rescue mode starts at a login prompt: log in as root
    if "--rescue" not in cmd:
        steps = ["wait:login: ", "send:root\\n"] + steps
    r = subprocess.run(cmd + steps, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out = r.stdout
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
    at = 0
    for want in expect:
        i = out.find(want, at)
        if i < 0:
            problems.append(f"missing {want!r}")
        else:
            at = i + len(want)
    return problems, out, r.stderr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-p", "--profile", default="debug")
    ap.add_argument("names", nargs="*")
    a = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # progress shows even when logged to a file
    PROFILE[0] = a.profile
    chosen = [t for t in TESTS if not a.names or t[0] in a.names]
    failed = 0
    for name, keep, fsck, steps, expect in chosen:
        problems, out, err = run(name, keep, fsck, steps, expect, a.profile)
        if problems:
            failed += 1
            print(f"FAIL {name}: " + "; ".join(problems))
            tail = "\n".join(out.splitlines()[-15:])
            print("     " + tail.replace("\n", "\n     "))
            if "e2fsck" in "".join(problems):
                print("     " + "\n     ".join(err.splitlines()[-12:]))
        else:
            print(f"ok   {name}")
    print(f"{len(chosen) - failed}/{len(chosen)} passed ({a.profile})")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
