#!/usr/bin/env python3
"""Offline checks for CA refresh validation and preservation of installed trust."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent


class CABundleRefresh(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        tools = self.project / "tools"
        tools.mkdir()
        shutil.copy2(ROOT / "tools/cabundle.sh", tools / "cabundle.sh")
        self.certs = self.project / "root/etc/ssl/certs"
        self.certs.mkdir(parents=True)
        (self.certs / "ca-certificates.crt").write_bytes(b"previous trust store\n")
        (self.certs / "SOURCE").write_bytes(b"previous source\n")
        self.stub = self.project / "bin"
        self.stub.mkdir()
        curl = self.stub / "curl"
        curl.write_text('''#!/bin/sh
set -eu
while [ "$#" -gt 0 ]; do
    if [ "$1" = -o ]; then shift; destination="$1"; fi
    shift
done
case "$CABUNDLE_TEST_MODE" in
    fail) exit 22 ;;
    invalid) printf '%s\\n' '-----BEGIN CERTIFICATE-----' 'broken' '-----END CERTIFICATE-----' > "$destination" ;;
    valid) cp "$CABUNDLE_TEST_INPUT" "$destination" ;;
esac
''')
        curl.chmod(0o755)

    def refresh(self, mode):
        env = os.environ.copy()
        env.update(PATH=str(self.stub) + os.pathsep + env["PATH"],
                   CABUNDLE_TEST_MODE=mode,
                   CABUNDLE_TEST_INPUT=str(ROOT / "root/etc/ssl/certs/ca-certificates.crt"))
        return subprocess.run(["sh", str(self.project / "tools/cabundle.sh")],
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def assert_preserved(self):
        self.assertEqual((self.certs / "ca-certificates.crt").read_bytes(), b"previous trust store\n")
        self.assertEqual((self.certs / "SOURCE").read_bytes(), b"previous source\n")
        self.assertFalse(list(self.certs.glob(".refresh.*")))

    def test_failed_download_keeps_previous_bundle(self):
        self.assertNotEqual(self.refresh("fail").returncode, 0)
        self.assert_preserved()

    def test_invalid_certificate_keeps_previous_bundle(self):
        self.assertNotEqual(self.refresh("invalid").returncode, 0)
        self.assert_preserved()

    def test_valid_bundle_updates_contents_and_provenance(self):
        result = self.refresh("valid")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual((self.certs / "ca-certificates.crt").read_bytes(),
                         (ROOT / "root/etc/ssl/certs/ca-certificates.crt").read_bytes())
        self.assertIn("https://curl.se/ca/cacert.pem", (self.certs / "SOURCE").read_text())
        self.assertEqual((self.certs / "ca-certificates.crt").stat().st_mode & 0o777, 0o644)
        self.assertFalse(list(self.certs.glob(".refresh.*")))


if __name__ == "__main__":
    unittest.main()
