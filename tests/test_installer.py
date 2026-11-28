"""install.sh's pinned hashes match the repo, and its source-dir hash checks pass on the repo's own files.

A module changed without updating install.sh's table made the installer refuse a reviewed source dir
("transcript_reply.py SHA256 mismatch") only at install time. These tests catch that in the suite.
"""
import getpass
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"
ZERO = "0" * 64


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pinned() -> dict[str, str]:
    """MODEL_SHA256 and every EXTRA_SHA256 entry, by file name."""
    text = INSTALLER.read_text()
    table = {"model_control.py": re.search(r'^MODEL_SHA256="([0-9a-f]{64})"', text, re.M).group(1)}
    block = re.search(r"^declare -A EXTRA_SHA256=\((.*?)^\)", text, re.M | re.S).group(1)
    table.update(re.findall(r'\[([a-z_]+\.py)\]="([0-9a-f]{64})"', block))
    modules = re.search(r"^EXTRA_MODULES=\((.*?)\)", text, re.M).group(1).split()
    assert sorted(modules) == sorted(set(table) - {"model_control.py"}), "EXTRA_MODULES and EXTRA_SHA256 differ"
    return table


class PinnedHashes(unittest.TestCase):
    def test_every_pinned_module_hash_is_the_repo_file(self):
        for name, digest in pinned().items():
            with self.subTest(module=name):
                self.assertEqual(digest, sha256(ROOT / name),
                                 f"install.sh pins {name} at {digest[:8]}; update its table with the module")


@unittest.skipUnless(shutil.which("bash") and shutil.which("sha256sum"), "needs bash and sha256sum")
class SourceDirChecks(unittest.TestCase):
    """Run install.sh itself up to and including its hash checks and compile step, in a temp root.

    Only paths and root-only steps are changed in the copy: the EUID check, the managed directories,
    the Herdr binary and ownership. The option parsing and hash checks run as written."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = self.tmp / "src"
        self.src.mkdir()
        for name in ["bridge.py", "request_ledger.py", *pinned()]:
            shutil.copy(ROOT / name, self.src / name)
        self.token = self.tmp / "bridge.token"
        self.token.write_text("x" * 40)
        (self.tmp / "roots").mkdir()
        herdr = self.tmp / "herdr"
        herdr.write_text("#!/bin/sh\n")
        herdr.chmod(0o755)
        text = INSTALLER.read_text()
        swaps = [
            ('[[ $EUID -eq 0 ]] || die "Run with sudo"', ":"),
            ('APP_DIR="/opt/openbot-herdr-bridge"', f'APP_DIR="{self.tmp}/opt"'),
            ('CONF_DIR="/etc/openbot-herdr-bridge"', f'CONF_DIR="{self.tmp}/etc"'),
            ('STATE_DIR="/var/lib/openbot-herdr-gateway"', f'STATE_DIR="{self.tmp}/state"'),
            ('UNIT_FILE="/etc/systemd/system/openbot-herdr-gateway.service"', f'UNIT_FILE="{self.tmp}/unit"'),
            ('HERDR_EXEC="$RUN_HOME/.local/bin/herdr"', f'HERDR_EXEC="{herdr}"'),
            ('-o "$RUN_USER" -g "$(id -gn "$RUN_USER")" ', ""),
            ('  || die "Gateway code did not compile"\n', '  || die "Gateway code did not compile"\necho HASH_CHECKS_PASSED; exit 0\n'),
        ]
        for old, new in swaps:
            self.assertIn(old, text, f"install.sh changed; update this test's swap for: {old[:50]}")
            text = text.replace(old, new)
        self.script = self.tmp / "install.sh"
        self.script.write_text(text)

    def run_installer(self, *extra):
        for name in ("opt", "etc", "state"):
            shutil.rmtree(self.tmp / name, ignore_errors=True)
        args = ["bash", str(self.script), "--user", getpass.getuser(), "--sessions", "auto",
                "--deny-sessions", "careintel,GBlox", "--roots", str(self.tmp / "roots"),
                "--port", "8766", "--bind", "127.0.0.1", "--busy-wait-seconds", "600",
                "--recovery-seconds", "600", "--artifact-mode", "inline",
                "--discovery-token-sha256", "1" * 64, "--verify-token-file", str(self.token),
                "--token-file", str(self.token), "--route-token-sha256", "probe/w1:p1=" + "2" * 64,
                "--source-dir", str(self.src),
                "--expect-bridge-sha256", sha256(self.src / "bridge.py"),
                "--expect-ledger-sha256", sha256(self.src / "request_ledger.py"),
                "--openbot-browser-url", "http://127.0.0.1:3001/api/agent-fabric/browser", *extra]
        env = dict(os.environ, PATH=os.environ.get("PATH", "/usr/bin:/bin"))
        return subprocess.run(args, capture_output=True, text=True, timeout=120, env=env)

    def test_the_repo_files_pass_the_installers_own_checks(self):
        done = self.run_installer()
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        self.assertIn("HASH_CHECKS_PASSED", done.stdout)

    def test_a_changed_module_is_refused(self):
        with (self.src / "transcript_reply.py").open("a") as handle:
            handle.write("\n# changed\n")
        done = self.run_installer()
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("transcript_reply.py SHA256 mismatch", done.stderr + done.stdout)

    def test_expect_extra_overrides_the_table_and_is_checked(self):
        with (self.src / "transcript_reply.py").open("a") as handle:
            handle.write("\n# reviewed variant\n")
        variant = sha256(self.src / "transcript_reply.py")
        done = self.run_installer("--expect-extra-sha256", f"transcript_reply.py={variant}")
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        wrong = self.run_installer("--expect-extra-sha256", f"transcript_reply.py={ZERO}")
        self.assertIn("transcript_reply.py SHA256 mismatch", wrong.stderr + wrong.stdout)
        unknown = self.run_installer("--expect-extra-sha256", f"nope.py={variant}")
        self.assertIn("unknown module", unknown.stderr + unknown.stdout)


if __name__ == "__main__":
    unittest.main()
