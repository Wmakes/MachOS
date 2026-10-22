#!/usr/bin/env python3
"""Check submodule bootstrap and cross-build an isolated checkout offline."""
import configparser
import os
import runpy
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parent.parent


def run(args, cwd, env):
    result = subprocess.run(args, cwd=cwd, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode:
        raise AssertionError(f"{args}:\n{result.stdout}")
    return result.stdout


class DependencyCheckout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="machos-deps-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.checkout = Path(cls.temp.name) / "MachOS"
        modules = configparser.ConfigParser()
        modules.read(ROOT / ".gitmodules")
        cls.modules = [dict(modules[section]) for section in modules.sections()]
        cls.env = os.environ.copy()
        # Local transports stand in for GitHub; any hidden Git network fetch fails.
        cls.env["GIT_ALLOW_PROTOCOL"] = "file"
        count = int(cls.env.get("GIT_CONFIG_COUNT", "0"))
        for module in cls.modules:
            cls.env[f"GIT_CONFIG_KEY_{count}"] = f"url.{ROOT / module['path']}.insteadOf"
            cls.env[f"GIT_CONFIG_VALUE_{count}"] = module["url"]
            count += 1
        cls.env["GIT_CONFIG_COUNT"] = str(count)
        run(["git", "clone", "--quiet", "--no-local", str(ROOT), str(cls.checkout)],
            ROOT, cls.env)
        # Validate tracked working changes too, before they are committed.
        changes = subprocess.run(["git", "diff", "HEAD", "--binary"], cwd=ROOT,
                                 check=True, stdout=subprocess.PIPE).stdout
        if changes:
            subprocess.run(["git", "apply", "--index"], cwd=cls.checkout,
                           input=changes, check=True, env=cls.env)
        for module in cls.modules:
            if (cls.checkout / module["path"] / ".git").exists():
                raise AssertionError("the fresh clone already has initialized submodules")
        run(["tools/deps.sh"], cls.checkout, cls.env)

    def test_bootstrap_uses_recorded_commits_and_preserves_edits(self):
        for module in self.modules:
            path = module["path"]
            with self.subTest(path=path):
                expected = run(["git", "rev-parse", f":{path}"],
                               self.checkout, self.env).strip()
                directory = self.checkout / path
                actual = run(["git", "rev-parse", "HEAD"], directory, self.env).strip()
                self.assertEqual(actual, expected)
                tracked = run(["git", "ls-files", "*.mach"], directory, self.env).splitlines()
                source = directory / tracked[0]
                previous = source.read_bytes()
                edited = previous + b"\n# local development edit\n"
                try:
                    source.write_bytes(edited)
                    run(["tools/deps.sh"], self.checkout, self.env)
                    self.assertEqual(source.read_bytes(), edited)
                finally:
                    source.write_bytes(previous)

    def test_manifests_stay_inside_the_checkout(self):
        manifests = run(["git", "ls-files", "*/mach.toml"],
                        self.checkout, self.env).splitlines()
        for name in manifests:
            manifest = self.checkout / name
            for dependency, spec in tomllib.loads(manifest.read_text()).get("dep", {}).items():
                with self.subTest(manifest=name, dependency=dependency):
                    self.assertIn("path", spec)
                    self.assertFalse(Path(spec["path"]).is_absolute())
                    target = (manifest.parent / spec["path"]).resolve()
                    self.assertTrue(target.is_relative_to(self.checkout))
                    self.assertTrue((target / "mach.toml").is_file())

    def test_projects_resolve_and_build_without_network_fetches(self):
        selections = (
            ("kernel", "kernel", ["kernel"]),
            ("machboot", "uefi", ["machboot"]),
            ("userland", "machos", ["wget", "ca-update"]),
            ("examples/hello", "machos", ["hello"]),
            ("examples/posixtest", "machos", ["posixtest"]),
            ("examples/stdtest", "machos", ["stdtest", "runtests"]),
            ("examples/tlstest", "machos", ["tlstest", "dns-proxy"]),
        )
        for project, target, artifacts in selections:
            with self.subTest(project=project):
                run(["mach", "dep", "pull", project], self.checkout, self.env)
                run(["mach", "dep", "verify", project], self.checkout, self.env)
                args = ["mach", "build", project, "-t", target, "-p", "release"]
                for artifact in artifacts:
                    args += ["-a", artifact]
                run(args, self.checkout, self.env)

    def test_tls_test_copy_uses_local_sources_and_leaves_submodules_clean(self):
        functions = runpy.run_path(str(self.checkout / "tools/test.py"))
        project = Path(functions["tls_test_project"]())
        manifest = tomllib.loads((project / "mach.toml").read_text())
        for name, spec in manifest["dep"].items():
            with self.subTest(dependency=name):
                repository = "machos-std" if name == "std" else "mach-" + name
                expected = project / "sources/tls" if name == "tls" else self.checkout / "deps" / repository
                self.assertEqual(Path(spec["path"]), expected)
        run(["git", "apply", "--reverse", "--check", "--directory",
             str(project.relative_to(self.checkout) / "sources/tls"),
             "tools/patches/mach-tls-pki-0.4-tests.patch"], self.checkout, self.env)
        for module in self.modules:
            self.assertEqual(run(["git", "status", "--porcelain"],
                                 self.checkout / module["path"], self.env), "")
        run(["mach", "dep", "pull", str(project)], self.checkout, self.env)
        dispatcher = project / "out/machos/release/tlstest-dispatcher"
        args = ["mach", "test", str(project), "-a", "tlstest", "-t", "machos",
                "-p", "release", "--include-deps", "-o", "out/machos/release/tlstest-dispatcher"]
        result = subprocess.run(args, cwd=self.checkout, env=self.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertIn(result.returncode, (0, 1), result.stdout)
        self.assertTrue(dispatcher.is_file(), result.stdout)
        if result.returncode == 1:
            self.assertIn("target.not_runnable", result.stdout)


if __name__ == "__main__":
    unittest.main()
