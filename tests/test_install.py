import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = "https://raw.githubusercontent.com/19Naveen/MockingBird"
ARCHIVE = "https://codeload.github.com/19Naveen/MockingBird/tar.gz/refs"


def install_env(home):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MOCKINGBIRD_", "BRIDGE_"))
           and k != "PYTHONPATH"}
    env.update(HOME=str(home), XDG_STATE_HOME=str(home / "state"))
    return env


def install(env):
    return subprocess.run(["bash", "install.sh"], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=20)


@unittest.skipUnless(shutil.which("bash"), "installer requires bash")
class InstallerTests(unittest.TestCase):
    def mock_downloads(self, home, bundle=True):
        fake_bin = home / "bin"
        fake_bin.mkdir()
        curl = fake_bin / "curl"
        curl.write_text("""#!/usr/bin/env python3
import os
import shutil
import sys
from pathlib import Path

args = sys.argv[1:]
url = next(arg for arg in args if arg.startswith("https://"))
home = Path.home()
with (home / "downloads").open("a") as log:
    print(url, file=log)
if url.startswith("https://codeload.github.com/"):
    source = home / "repo.tar.gz"
else:
    base = os.environ.get("MOCKINGBIRD_BASE", "") or (
        "https://raw.githubusercontent.com/19Naveen/MockingBird/main")
    assert url.startswith(base + "/"), url
    source = Path.cwd() / url[len(base) + 1:]
if not source.is_file():
    sys.exit(22)
shutil.copyfile(source, args[args.index("-o") + 1])
""")
        curl.chmod(0o700)
        if bundle:
            with tarfile.open(home / "repo.tar.gz", "w:gz") as archive:
                directory = tarfile.TarInfo("MockingBird-test")
                directory.type = tarfile.DIRTYPE
                archive.addfile(directory)
                files = ("SKILL.md", "bridge.py", "pyproject.toml", "install.sh",
                         "mockingbird_hook.py", "uninstall.sh", "LICENSE")
                for path in [*(ROOT / name for name in files), *(ROOT / "src").rglob("*.py")]:
                    archive.add(path, "MockingBird-test/" + path.relative_to(ROOT).as_posix())
        env = install_env(home)
        env["PATH"] = str(fake_bin) + os.pathsep + env["PATH"]
        return env

    def test_official_installs_use_one_bundle(self):
        for base, ref in (("", "heads/main"), (RAW + "/v1.2.3", "tags/v1.2.3")):
            with self.subTest(ref=ref), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                env = self.mock_downloads(home)
                if base:
                    env["MOCKINGBIRD_BASE"] = base
                result = install(env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((home / "downloads").read_text().splitlines(), [f"{ARCHIVE}/{ref}"])
                dest = home / ".agents" / "skills" / "mockingbird"
                self.assertTrue((home / ".local" / "bin" / "mockingbird").is_file())
                self.assertFalse((dest / "install.sh").exists())

    def test_bundle_unavailable_falls_back_and_custom_mirrors_skip_bundle(self):
        for base in ("", "https://mirror.example/mock"):
            with self.subTest(base=base), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                env = self.mock_downloads(home, bundle=False)
                if base:
                    env["MOCKINGBIRD_BASE"] = base
                result = install(env)
                self.assertEqual(result.returncode, 0, result.stderr)
                urls = (home / "downloads").read_text().splitlines()
                if not base:
                    self.assertEqual(urls.pop(0), f"{ARCHIVE}/heads/main")
                    self.assertIn("downloading files individually", result.stdout)
                prefix = (base or RAW + "/main") + "/"
                self.assertTrue(urls)
                self.assertTrue(all(url.startswith(prefix) for url in urls))

    def test_invalid_bundle_leaves_existing_install_alone(self):
        for stale in (False, True):
            with self.subTest(stale=stale), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                env = self.mock_downloads(home, bundle=False)
                bundle = home / "repo.tar.gz"
                if stale:
                    with tarfile.open(bundle, "w:gz") as archive:
                        directory = tarfile.TarInfo("MockingBird-test")
                        directory.type = tarfile.DIRTYPE
                        archive.addfile(directory)
                        script = tarfile.TarInfo("MockingBird-test/install.sh")
                        data = b"VERSION=0.0.0\n"
                        script.size = len(data)
                        archive.addfile(script, io.BytesIO(data))
                else:
                    bundle.write_bytes(b"not an archive")
                bridge = home / ".agents" / "skills" / "mockingbird" / "bridge.py"
                bridge.parent.mkdir(parents=True)
                bridge.write_bytes(b"previous install")
                result = install(env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Could not unpack", result.stderr)
                if stale:
                    self.assertIn("archive version does not match", result.stderr)
                self.assertEqual(bridge.read_bytes(), b"previous install")
                self.assertEqual((home / "downloads").read_text().splitlines(), [f"{ARCHIVE}/heads/main"])

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
                env = install_env(home)
                env["MOCKINGBIRD_SRC"] = str(ROOT)
                if not existing:
                    env["MOCKINGBIRD_INSTALL_HOOK"] = "1"  # legacy flag no longer changes startup files
                result = install(env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((home / ".local" / "bin" / "mockingbird").is_file())
                for path in rc:
                    self.assertEqual(path.read_bytes(), shell)
                if existing:
                    self.assertEqual(hook.read_bytes(), original["hook"])
                else:
                    self.assertFalse(hook.parent.exists())
