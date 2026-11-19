import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("bash"), "installer requires bash")
class InstallerTests(unittest.TestCase):
    def test_fresh_install_and_upgrade_do_not_touch_shell_hooks(self):
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                hook = home / ".mockingbird" / "hook" / "sitecustomize.py"
                original = {"lines": b"# local shell settings\n", "hook": b"# existing hook payload\n"}
                rc = (home / ".zshrc", home / ".bashrc")
                shell = original["lines"]
                if existing:
                    hook.parent.mkdir(parents=True)
                    hook.write_bytes(original["hook"])
                    shell += b"\n# >>> mockingbird >>>\n# old hook block\n# <<< mockingbird <<<\n"
                for path in rc:
                    path.write_bytes(shell)
                env = {k: v for k, v in os.environ.items() if not k.startswith(("MOCKINGBIRD_", "BRIDGE_"))
                       and k != "PYTHONPATH"}
                env.update(HOME=tmp, XDG_STATE_HOME=str(home / "state"), MOCKINGBIRD_SRC=str(ROOT))
                if not existing:
                    env["MOCKINGBIRD_INSTALL_HOOK"] = "1"  # legacy flag no longer changes startup files
                result = subprocess.run(["bash", "install.sh"], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                        capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((home / ".local" / "bin" / "mockingbird").is_file())
                for path in rc:
                    self.assertEqual(path.read_bytes(), shell)
                if existing:
                    self.assertEqual(hook.read_bytes(), original["hook"])
                else:
                    self.assertFalse(hook.parent.exists())
