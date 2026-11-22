"""bin/evalup (#41): one command on PATH for every script, no behaviour change.

Claude Code puts a plugin's bin/ on the Bash tool's PATH while the plugin is
enabled, so `Bash(evalup *)` can cover every script. That only holds while
the dispatcher is a pass-through: these tests pin that each subcommand is
exactly one script, and that arguments, stdin, stdout, stderr and the exit
code reach the caller unchanged.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin" / "evalup"
SCRIPTS = ROOT / "scripts"
CLIS = sorted(p for p in SCRIPTS.glob("*.py") if not p.name.startswith("_"))


def run(argv, exe=BIN, **kwargs):
    return subprocess.run([sys.executable, str(exe), *argv],
                          capture_output=True, text=True, check=False,
                          **kwargs)


def direct(script, argv, **kwargs):
    return subprocess.run([sys.executable, str(script), *argv],
                          capture_output=True, text=True, check=False,
                          **kwargs)


class TestDispatcher(unittest.TestCase):
    def test_is_an_executable_file_with_a_python3_shebang(self):
        self.assertTrue(BIN.is_file())
        if os.name == "posix":
            self.assertTrue(os.access(BIN, os.X_OK), "chmod +x bin/evalup")
        first = BIN.read_text(encoding="utf-8").split("\n", 1)[0]
        self.assertEqual(first, "#!/usr/bin/env python3")

    def test_help_lists_every_cli_and_nothing_else(self):
        proc = run(["--help"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        listed = [line.strip() for line in
                  proc.stdout.split("commands:\n", 1)[1].splitlines()
                  if line.strip()]
        self.assertEqual(listed,
                         [p.stem.replace("_", "-") for p in CLIS])
        self.assertEqual(len(listed), 22)

    def test_no_arguments_is_usage_on_stderr_exit_2(self):
        proc = run([])
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertIn("usage: evalup", proc.stderr)

    def test_unknown_command_is_exit_2_naming_it(self):
        proc = run(["nope"])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("unknown command 'nope'", proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_every_subcommand_help_is_its_scripts_help(self):
        for script in CLIS:
            name = script.stem.replace("_", "-")
            with self.subTest(command=name):
                got = run([name, "--help"])
                want = direct(script, ["--help"])
                self.assertEqual(got.returncode, 0, got.stderr)
                self.assertTrue(got.stdout.strip())
                self.assertEqual(
                    (got.returncode, got.stdout, got.stderr),
                    (want.returncode, want.stdout, want.stderr))

    def test_underscore_and_py_spellings_are_the_same_command(self):
        want = run(["run-cases", "--help"]).stdout
        for spelling in ("run_cases", "run_cases.py", "run-cases.py"):
            with self.subTest(spelling=spelling):
                self.assertEqual(run([spelling, "--help"]).stdout, want)

    def test_a_scripts_error_and_exit_code_pass_through(self):
        got = run(["gate"])
        want = direct(SCRIPTS / "gate.py", [])
        self.assertEqual(got.returncode, 2)
        self.assertEqual((got.returncode, got.stdout, got.stderr),
                         (want.returncode, want.stdout, want.stderr))

    def test_arguments_and_stdin_reach_the_script_untouched(self):
        # One argument with spaces, a `$` and quotes arrives as one literal
        # argument: the script names it back in its error.
        odd = "no such file $HOME 'x'.json"
        argv = ["--no-capabilities", "--cases", odd]
        got = run(["validate-cases", *argv])
        self.assertEqual(got.returncode, 2)
        self.assertIn(odd, got.stdout)
        want = direct(SCRIPTS / "validate_cases.py", argv)
        self.assertEqual((got.returncode, got.stdout, got.stderr),
                         (want.returncode, want.stdout, want.stderr))
        # stdin reaches the script: `--cases -` lints what is piped in.
        suite = '{"cases": [{"category": "happy"}]}'
        got = run(["validate-cases", "--no-capabilities", "--cases", "-"],
                  input=suite)
        want = direct(SCRIPTS / "validate_cases.py",
                      ["--no-capabilities", "--cases", "-"], input=suite)
        self.assertEqual(got.returncode, 1)
        self.assertIn('"missing_id"', got.stdout)
        self.assertEqual((got.returncode, got.stdout, got.stderr),
                         (want.returncode, want.stdout, want.stderr))

    def test_finds_the_scripts_through_a_symlink(self):
        # A plugin may be installed by copy or linked; the dispatcher
        # resolves its own path, because CLAUDE_PLUGIN_ROOT is not set for
        # commands the Bash tool runs.
        if not hasattr(os, "symlink"):
            self.skipTest("no symlinks")
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        link = tmp / "evalup"
        try:
            os.symlink(BIN, link)
        except OSError:
            self.skipTest("cannot create a symlink here")
        env = {k: v for k, v in os.environ.items()
               if k != "CLAUDE_PLUGIN_ROOT"}
        proc = run(["stats", "--help"], exe=link, env=env, cwd=str(tmp))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("usage: stats.py", proc.stdout)


if __name__ == "__main__":
    unittest.main()
