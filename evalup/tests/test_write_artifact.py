"""write_artifact.py (#106): findings.md and report.md without the Write tool.

A skill run in a Claude Code subagent is refused Write on those two names, so
it writes a draft and hands it to this script. The script's one promise is
that it lands a Markdown file inside the state location and nowhere else:
these tests pin the containment, both input paths and the replace semantics.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "write_artifact.py"
BIN = ROOT / "bin" / "evalup"


def run(argv, stdin=None, exe=None):
    cmd = [sys.executable, str(exe or SCRIPT), *argv]
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                          check=False, encoding="utf-8")


class WriteArtifactTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(os.path.realpath(tmp.name))
        self.state = self.base / ".evalup"
        self.state.mkdir()
        self.drafts = self.state / "drafts"
        self.drafts.mkdir()

    def draft(self, text, name="findings.txt"):
        path = self.drafts / name
        path.write_text(text, encoding="utf-8")
        return path

    def ok(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def refused(self, proc, phrase):
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn(phrase, json.loads(proc.stdout)["error"])


class TestInput(WriteArtifactTest):
    def test_from_a_draft_writes_it_verbatim(self):
        text = "# Findings\n\n- F1 no OOS route — café\n"
        src = self.draft(text)
        out = self.ok(run(["--state-dir", str(self.state), "findings.md",
                           "--from", str(src)]))
        dest = self.state / "findings.md"
        self.assertEqual(dest.read_text(encoding="utf-8"), text)
        self.assertEqual(out, {"wrote": str(dest),
                               "bytes": len(text.encode("utf-8")),
                               "replaced": False, "moved": False})
        self.assertTrue(src.exists(), "without --move the draft stays")

    def test_stdin_writes_it_verbatim(self):
        text = "# Report\r\n\r\nkeeps CRLF\r\n"
        self.ok(run(["--state-dir", str(self.state), "findings.md"],
                    stdin=text))
        self.assertEqual((self.state / "findings.md").read_bytes(),
                         text.encode("utf-8"))

    def test_move_deletes_the_draft_after_writing(self):
        src = self.draft("# R\n", "report.txt")
        out = self.ok(run(["--state-dir", str(self.state),
                           "reports/r1/report.md", "--from", str(src),
                           "--move"]))
        self.assertTrue(out["moved"])
        self.assertFalse(src.exists())
        self.assertEqual(
            (self.state / "reports" / "r1" / "report.md").read_text(
                encoding="utf-8"), "# R\n")

    def test_move_without_from_is_refused(self):
        self.refused(run(["--state-dir", str(self.state), "findings.md",
                          "--move"], stdin="x\n"), "--move needs --from")

    def test_empty_text_is_refused_and_nothing_written(self):
        for text in ("", "  \n\n"):
            with self.subTest(text=text):
                self.refused(run(["--state-dir", str(self.state),
                                  "findings.md"], stdin=text), "empty")
                self.assertFalse((self.state / "findings.md").exists())

    def test_missing_draft_is_exit_2(self):
        self.refused(run(["--state-dir", str(self.state), "findings.md",
                          "--from", str(self.drafts / "nope.txt")]),
                     "bad input")

    def test_draft_that_is_the_destination_is_refused(self):
        dest = self.state / "findings.md"
        dest.write_text("old\n", encoding="utf-8")
        self.refused(run(["--state-dir", str(self.state), "findings.md",
                          "--from", str(dest), "--move"]), "same file")
        self.assertEqual(dest.read_text(encoding="utf-8"), "old\n")

    def test_through_the_dispatcher(self):
        src = self.draft("# F\n")
        self.ok(run(["write-artifact", "--state-dir", str(self.state),
                     "findings.md", "--from", str(src)], exe=BIN))
        self.assertTrue((self.state / "findings.md").is_file())


class TestOverwrite(WriteArtifactTest):
    def test_an_existing_file_is_replaced_whole_and_says_so(self):
        dest = self.state / "findings.md"
        dest.write_text("old text that is longer than the new\n",
                        encoding="utf-8")
        os.chmod(dest, 0o640)
        out = self.ok(run(["--state-dir", str(self.state), "findings.md"],
                          stdin="new\n"))
        self.assertTrue(out["replaced"])
        self.assertEqual(dest.read_text(encoding="utf-8"), "new\n")
        self.assertEqual(dest.stat().st_mode & 0o777, 0o640,
                         "a replaced file keeps its mode")

    def test_a_new_file_gets_the_umask_mode_not_mkstemps_0600(self):
        self.ok(run(["--state-dir", str(self.state), "findings.md"],
                    stdin="x\n"))
        umask = os.umask(0)
        os.umask(umask)
        self.assertEqual((self.state / "findings.md").stat().st_mode & 0o777,
                         0o666 & ~umask)

    def test_no_temp_file_is_left_behind(self):
        self.ok(run(["--state-dir", str(self.state), "findings.md"],
                    stdin="x\n"))
        self.assertEqual(sorted(p.name for p in self.state.iterdir()),
                         ["drafts", "findings.md"])


class TestContainment(WriteArtifactTest):
    def assert_nothing_outside(self):
        self.assertEqual(sorted(p.name for p in self.base.iterdir()),
                         [".evalup"])

    def test_parent_traversal_is_refused(self):
        self.refused(run(["--state-dir", str(self.state), "../findings.md"],
                         stdin="x\n"), "outside the state location")
        self.assert_nothing_outside()

    def test_absolute_path_outside_is_refused(self):
        self.refused(run(["--state-dir", str(self.state),
                          str(self.base / "findings.md")], stdin="x\n"),
                     "outside the state location")
        self.assert_nothing_outside()

    def test_absolute_path_inside_is_allowed(self):
        self.ok(run(["--state-dir", str(self.state),
                     str(self.state / "findings.md")], stdin="x\n"))

    def test_the_state_dir_itself_is_refused(self):
        self.refused(run(["--state-dir", str(self.state), "."],
                         stdin="x\n"), "outside the state location")

    def test_a_sibling_with_the_same_prefix_is_outside(self):
        (self.base / ".evalup-old").mkdir()
        self.refused(run(["--state-dir", str(self.state),
                          "../.evalup-old/findings.md"], stdin="x\n"),
                     "outside the state location")

    @unittest.skipUnless(hasattr(os, "symlink"), "needs symlinks")
    def test_a_symlinked_directory_leading_out_is_refused(self):
        outside = self.base / "elsewhere"
        outside.mkdir()
        os.symlink(outside, self.state / "reports")
        self.refused(run(["--state-dir", str(self.state),
                          "reports/report.md"], stdin="x\n"),
                     "outside the state location")
        self.assertEqual(list(outside.iterdir()), [])

    @unittest.skipUnless(hasattr(os, "symlink"), "needs symlinks")
    def test_a_symlinked_file_leading_out_is_refused(self):
        target = self.base / "victim.md"
        target.write_text("keep\n", encoding="utf-8")
        os.symlink(target, self.state / "findings.md")
        self.refused(run(["--state-dir", str(self.state), "findings.md"],
                         stdin="x\n"), "outside the state location")
        self.assertEqual(target.read_text(encoding="utf-8"), "keep\n")

    @unittest.skipUnless(hasattr(os, "symlink"), "needs symlinks")
    def test_a_md_symlink_to_a_record_inside_is_refused(self):
        record = self.state / "results.json"
        record.write_text("{}\n", encoding="utf-8")
        os.symlink(record, self.state / "findings.md")
        self.refused(run(["--state-dir", str(self.state), "findings.md"],
                         stdin="x\n"), "not a .md file")
        self.assertEqual(record.read_text(encoding="utf-8"), "{}\n")

    @unittest.skipUnless(hasattr(os, "symlink"), "needs symlinks")
    def test_a_symlinked_state_dir_is_followed(self):
        link = self.base / "state-link"
        os.symlink(self.state, link)
        self.ok(run(["--state-dir", str(link), "findings.md"], stdin="x\n"))
        self.assertTrue((self.state / "findings.md").is_file())

    def test_only_markdown_is_written(self):
        for dest in ("reports/r1/results.json", "datasets/cases.yaml",
                     "findings.txt", "findings.md.bak"):
            with self.subTest(dest=dest):
                self.refused(run(["--state-dir", str(self.state), dest],
                                 stdin="x\n"), "not a .md file")

    def test_a_directory_named_like_markdown_is_refused(self):
        (self.state / "odd.md").mkdir()
        self.refused(run(["--state-dir", str(self.state), "odd.md"],
                         stdin="x\n"), "not a regular file")

    def test_a_missing_state_dir_is_refused(self):
        self.refused(run(["--state-dir", str(self.base / "nope"),
                          "findings.md"], stdin="x\n"), "not a directory")
        self.assert_nothing_outside()


if __name__ == "__main__":
    unittest.main()
