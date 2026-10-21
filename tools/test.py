#!/usr/bin/env python3
"""Boot MachOS under QEMU and check it end to end.

usage: tools/test.py [-p release] [NAME...]     (no names: run every test)

Each test boots a fresh disk (or the previous test's disk, for `keep`),
types commands over the serial port, and checks that the output contains
every expected string, in order. Tests marked `fsck` also run e2fsck on the
root partition afterwards.
"""
import argparse, os, re, subprocess, sys, threading, time

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
    return os.path.join(ROOT, "out", PROFILE[0], "disk.img")


def with_posixtest():
    """a fresh dev disk with examples/posixtest on it as /bin/posixtest"""
    p = PROFILE[0]
    sh(f"tools/mkimage.sh {p} --fresh")
    sh(f"cd examples/posixtest && mach dep pull . && mach build . -t machos -p {p}")
    sh(f"tools/put.sh examples/posixtest/out/machos/{p}/posixtest /bin/posixtest {disk()}")


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
        if name == "crypto":
            path = os.path.join(ROOT, "deps", ".patched", repository)
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
    steps = ["wait:" + P, "send:adduser stdtest\\n", "wait:" + P,
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


def dns_fixture():
    """Drop the first A query, then answer through two CNAMEs deterministically."""
    import socket, struct
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 5533))
    DNS_COUNTS.clear()

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


def update_stick():
    """a FAT stick carrying an update package"""
    sh("rm -f out/headless/update.tgz")  # a package that fails to build must not be an old one
    pkg = sh("tools/mkupdate.sh out/headless/update.tgz").strip().splitlines()[-1]
    sh(f"rm -f {USB_STICK}; dd if=/dev/zero of={USB_STICK} bs=1M count=64 status=none")
    sh(f"mkfs.fat -F 32 {USB_STICK}")
    sh(f"mcopy -i {USB_STICK} out/headless/update.tgz ::/update.tgz")


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
      "wait:" + P, "send:dmesg | grep -E 'live system|loaded by'; ls /bin | wc -l\\n", "wait:" + P],
     ["loaded by machboot", "live system: running from memory"]),

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
      "keys:<ret>", "wait:booting Windows", "wait:login: ", "send:root\\n", "wait:" + P],
     ["Windows", "machboot: booting Windows", "machboot 0.9.0"]),

    # machboot hide: no menu, straight into MachOS
    ("hidden-menu", False, True,
     ["wait:" + P, "send:machboot hide; machboot | head -3\\n", "wait:" + P],
     ["the menu is hidden"]),
    ("hidden-menu-boots", True, True,
     ["wait:" + P, "send:machboot show\\n", "wait:" + P],
     ["login"]),

    # an update from a package on a USB stick, then a reboot into it
    ("update", False, True,
     ["--prep", update_stick] + usb_stick() + [
      "wait:" + P, "send:mkdir /mnt; mount /dev/sdb /mnt; rm /etc/ssl/certs/ca-certificates.crt /bin/ca-update; update /mnt/update.tgz; umount /mnt; machboot\\n",
      "wait:" + P],
     ["updating MachOS", "installing the programs", "MachOS is updated to 0.9.0", "MachOS (previous)"]),
    ("update-reboot", True, True,
     ["wait:" + P, "send:ls /boot; uname -a\\n", "wait:" + P],
     ["kernel.old.elf", "MachOS 0.9.0"]),

    # MachOS installed on a USB stick, then booted from the stick alone
    ("usb-install", False, False,
     ["--prep", lambda: sh(f"rm -f {USB_STICK}; dd if=/dev/zero of={USB_STICK} bs=1M count=512 status=none")]
     + usb_stick() + ["wait:" + P, "send:install /dev/sdb\\n", "wait:continue: ", "send:yes\\n", "wait:New password: ", "send:machos\\n", "wait:Again: ", "send:machos\\n",
      "wait:installed on", "wait:" + P],
     ["mass storage", "installed on /dev/sdb"]),
    ("usb-installed-boot", False, False,
     ["--prep", lambda: sh("dd if=/dev/zero of=out/headless/empty.img bs=1M count=8 status=none"),
      "--boot", os.path.join(ROOT, "out/headless/empty.img"),
      "--qemu=-device", "--qemu=qemu-xhci,id=xhci", "--qemu=-drive",
      f"--qemu=if=none,id=stick,format=raw,file={USB_STICK}",
      "--qemu=-device", "--qemu=usb-storage,bus=xhci.0,drive=stick,bootindex=0",
      "wait:Password: ", "send:machos\\n", "wait:" + P, "send:df\\n", "wait:" + P],
     ["root: ext2 on /dev/sdb2", "/dev/sdb2"]),

    ("tools", False, False,
     ["wait:" + P,
      "send:printf 'pear\\\\napple\\\\nfig\\\\napple\\\\n' > /tmp/f; sort -u /tmp/f | tr a-z A-Z; seq 1 20 | sort -n -r | head -2\\n",
      "wait:" + P,
      "send:seq 1 6 > /tmp/a; printf '1\\\\n2\\\\nX\\\\n4\\\\n5\\\\n6\\\\n' > /tmp/b; diff /tmp/a /tmp/b; echo diff=$?\\n",
      "wait:diff=", "wait:" + P,
      "send:find / -name '*.conf'; ls /etc | xargs echo got; seq 1 50 | tail -2 | tee /tmp/t | wc -l; basename /a/b.txt .txt; df\\n",
      "wait:" + P],
     ["APPLE", "FIG", "PEAR", "20", "19", "-3", "+X", "diff=1", "/boot/machboot.conf", "got group hosts machos.conf motd passwd services shadow ssl/",
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
     ["MachOS version 0.9.0", "MemTotal:", "Name:", "cat", "Uid:", "hostname", "cpu0",
      "MachOS 0.9.0 x86_64 on machos", "total", "processes", "PID", "init", "Filesystem", "/dev/sda2",
      "uid=0(root)", "root", "/dev/tty1", "1"]),

    ("events", False, False,
     ["wait:" + P, "send:events -n 2 > /tmp/ev &\\n", "sleep:1", "send:true\\n", "wait:" + P, "sleep:1",
      "send:cat /tmp/ev\\n", "wait:" + P],
     ["start", "true", "exit"]),

    ("clock", False, False,
     ["wait:" + P, "send:date -s '2030-01-02 03:04:05'; date +%s\\n", "wait:" + P,
      "send:echo 'timezone CET-1CEST,M3.5.0,M10.5.0/3' >> /etc/machos.conf; date; date -u; env TZ=JST-9 date\\n",
      "wait:" + P,
      "send:date -s '2030-07-01 12:00'; date -u; echo box > /proc/sys/kernel/hostname; uname -n\\n", "wait:" + P],
     ["2030-01-02 03:04:05 UTC", "18935534", "2030-01-02 04:04", "CET", "2030-01-02 03:04", "UTC",
      "2030-01-02 12:04", "JST", "2030-07-01 12:00:00 CEST", "2030-07-01 10:00", "UTC", "box"]),

    ("config-write", False, False,
     ["wait:" + P, "send:echo 'hostname testbox' > /etc/machos.conf; echo 'ttys 2' >> /etc/machos.conf\\n",
      "wait:" + P,
      "send:echo 'autologin root' >> /etc/machos.conf; echo 'timezone CET-1CEST,M3.5.0,M10.5.0/3' >> /etc/machos.conf\\n",
      "wait:" + P, "send:echo 'rtc local' >> /etc/machos.conf; sync\\n", "wait:" + P, "sleep:1"],
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
      "send:wget http://10.0.2.2:1/\\n", "wait:" + P],
     ["hello.txt saved, 15 bytes", "hello over tcp", "150000",
      "771c3995129ed087c7336651f32a510b009e3c9d2190f13bda69d91dd91a257e",
      "redirected to http://10.0.2.2:8080/sub/", "behind a redirect", "404", "status 1",
      "connection refused"]),

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
     ["--post", lambda: "" if "clean" in root_state(disk()) and "not clean" not in root_state(disk()) else "root not clean after poweroff",
      "wait:" + P, "send:echo hi > /tmp/z; poweroff\\n", "wait:power: off", "sleep:1"],
     ["power: off"]),

    # install onto a blank second disk, then boot that disk on its own
    ("install", False, False,
     ["--disk2", "blank:" + TARGET,
      "wait:" + P, "send:ls /dev; install /dev/sda\\n", "wait:continue: ", "send:no\\n", "wait:" + P,
      "send:install /dev/sdb\\n", "wait:continue: ", "send:yes\\n", "wait:New password: ", "send:machos\\n", "wait:Again: ", "send:machos\\n", "wait:" + P,
      "send:ls /dev; mount /dev/sdb2 /mnt; ls /mnt; umount /dev/sdb2; ls /mnt; echo unmounted\\n", "wait:" + P],
     ["sdb", "/dev/sdb2 MachOS root 446 MiB", "mkfat: FAT32", "mkext2:", "copying the system",
      "is installed on /dev/sdb", "sdb1", "sdb2", "bin/", "unmounted"]),

    ("nvme-boot", False, False,
     ["--nvme", "wait:" + P, "send:df; ls /dev; echo \"[$(partname /dev/nvme0n1 2)]\"\\n", "wait:" + P],
     ["disk: nvme0n1", "root: ext2 on /dev/nvme0n1p2", "/dev/nvme0n1p2", "nvme0n1p1",
      "[/dev/nvme0n1p2]"]),

    ("nvme-install", False, False,
     ["--disk2", "blank:" + NVME_TARGET, "--disk2", "nvme:" + NVME_TARGET,
      "wait:" + P, "send:install /dev/nvme0n1\\n", "wait:continue: ", "send:yes\\n", "wait:New password: ", "send:machos\\n", "wait:Again: ", "send:machos\\n", "wait:installed on",
      "wait:" + P],
     ["/dev/nvme0n1p1 EFI system", "mkfat: FAT32", "copying the system", "installed on /dev/nvme0n1"]),

    ("nvme-installed-boot", False, True,
     ["--boot", NVME_TARGET, "--nvme", "wait:Password: ", "send:machos\\n", "wait:" + P, "send:df\\n", "wait:" + P, "sleep:1"],
     ["root: ext2 on /dev/nvme0n1p2", "/dev/nvme0n1p2"]),

    ("installed-boot", False, True,
     ["--boot", TARGET, "wait:Password: ", "send:machos\\n", "wait:" + P,
      "send:uname; ls /boot; echo on-installed > /home/x; cat /home/x; mkdir /mnt; mount /dev/sda2 /mnt\\n", "wait:" + P,
      "sleep:1"],
     ["root: ext2 on /dev/sda2", "MachOS", "kernel.elf", "on-installed", "busy"]),
]


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
        steps = ["wait:login: ", "send:root\\n"] + steps
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
    if name == "https":
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