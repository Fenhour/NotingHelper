"""Exercise the Windows launcher without opening the user's app or accessing their data."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe")


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows PowerShell required")
class StartupTests(unittest.TestCase):
    def test_batch_starts_python_without_path_in_unicode_directory(self):
        with tempfile.TemporaryDirectory(prefix="划词助手 启动测试 ") as temporary:
            project = Path(temporary)
            (project / "tools").mkdir()
            shutil.copy2(ROOT / "tools" / "start_word_picker.ps1", project / "tools")
            batch = project / "启动划词助手.bat"
            shutil.copy2(ROOT / batch.name, batch)
            (project / "word_picker.py").write_text(
                "from pathlib import Path\n"
                "Path(__file__).with_name('started.txt').write_text('started', encoding='utf-8')\n",
                encoding="utf-8",
            )
            # Keep Windows shell tools available but remove Python and py from PATH.
            system32 = Path(os.environ["SystemRoot"]) / "System32"
            env = dict(os.environ)
            env["PATH"] = os.pathsep.join((str(system32), str(Path(POWERSHELL).parent)))
            cmd = os.environ.get("COMSPEC", str(system32 / "cmd.exe"))
            result = subprocess.run(
                f'"{cmd}" /d /s /c ""{batch}""',
                cwd=project, env=env, capture_output=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            marker = project / "started.txt"
            deadline = time.monotonic() + 15
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(marker.exists(), "windowless interpreter did not execute the script")
            self.assertEqual(marker.read_text(encoding="utf-8"), "started")

    def test_debug_and_normal_resolve_real_interpreters(self):
        launcher = ROOT / "tools" / "start_word_picker.ps1"
        for flags in ([], ["-DebugMode"]):
            result = subprocess.run(
                [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 str(launcher), "-CheckOnly", *flags],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            executable = Path(result.stdout.strip())
            self.assertTrue(executable.is_file())
            if flags:
                self.assertEqual(executable.name.lower(), "python.exe")


if __name__ == "__main__":
    unittest.main()
