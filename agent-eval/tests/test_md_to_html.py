#!/usr/bin/env python3
"""Tests for md_to_html.py — the Markdown -> self-contained-HTML converter.

Stdlib only (unittest + subprocess), matching the scorer tests' style: each
conversion is exercised as a real subprocess (argv, files, exit codes) since
that is the script's actual contract with the run/analyze skills that shell
out to it.

Run: python3 -m unittest discover -s tests -v   (from the plugin root)
"""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"


def run_script(*argv):
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "md_to_html.py"), *[str(a) for a in argv]],
        capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


class MdToHtmlTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write_md(self, text, name="in.md"):
        p = self.tmp / name
        p.write_text(text, encoding="utf-8")
        return p

    def render(self, text):
        rc, out, err = run_script(self.write_md(text))
        self.assertEqual(rc, 0, err)
        return out


class TestBlockElements(MdToHtmlTest):
    def test_headings_all_levels(self):
        html = self.render("# H1\n\n## H2\n\n### H3\n")
        self.assertIn("<h1>H1</h1>", html)
        self.assertIn("<h2>H2</h2>", html)
        self.assertIn("<h3>H3</h3>", html)

    def test_paragraph(self):
        html = self.render("This is one paragraph\nwrapped over two lines.\n")
        self.assertIn("<p>This is one paragraph wrapped over two lines.</p>",
                      html)

    def test_paragraphs_separated_by_blank_line_are_distinct(self):
        html = self.render("First para.\n\nSecond para.\n")
        self.assertIn("<p>First para.</p>", html)
        self.assertIn("<p>Second para.</p>", html)

    def test_unordered_list(self):
        html = self.render("- alpha\n- beta\n- gamma\n")
        self.assertIn("<ul>", html)
        self.assertIn("<li>alpha</li>", html)
        self.assertIn("<li>beta</li>", html)
        self.assertIn("<li>gamma</li>", html)
        self.assertIn("</ul>", html)

    def test_ordered_list(self):
        html = self.render("1. first\n2. second\n3. third\n")
        self.assertIn("<ol>", html)
        self.assertIn("<li>first</li>", html)
        self.assertIn("<li>third</li>", html)
        self.assertIn("</ol>", html)

    def test_table_with_alignment(self):
        html = self.render(
            "| Name | Score |\n| --- | ---: |\n| Alice | 92 |\n"
            "| Bob | 81 |\n")
        self.assertIn("<table>", html)
        self.assertIn("<th>Name</th>", html)
        self.assertIn('<th style="text-align:right">Score</th>', html)
        self.assertIn("<td>Alice</td>", html)
        self.assertIn('<td style="text-align:right">92</td>', html)

    def test_fenced_code_block_not_interpreted_as_markdown(self):
        html = self.render("```python\ndef f(x):\n    return x * *y\n```\n")
        self.assertIn('<pre><code class="language-python">', html)
        self.assertIn("def f(x):", html)
        # The stray '* *' inside the fence must NOT become <em> — code
        # content is escaped verbatim, never run through inline formatting.
        self.assertNotIn("<em>", html)

    def test_fenced_code_block_without_language(self):
        html = self.render("```\nplain block\n```\n")
        self.assertIn("<pre><code>plain block</code></pre>", html)

    def test_code_block_html_is_escaped(self):
        html = self.render("```\n<script>alert(1)</script>\n```\n")
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>alert(1)</script>", html)


class TestInlineElements(MdToHtmlTest):
    def test_bold_and_italic(self):
        html = self.render("Some **bold** and *italic* text.\n")
        self.assertIn("<strong>bold</strong>", html)
        self.assertIn("<em>italic</em>", html)

    def test_underscore_variants(self):
        html = self.render("Some __bold__ and _italic_ text.\n")
        self.assertIn("<strong>bold</strong>", html)
        self.assertIn("<em>italic</em>", html)

    def test_inline_code(self):
        html = self.render("Run `pytest -k foo` to test.\n")
        self.assertIn("<code>pytest -k foo</code>", html)

    def test_link(self):
        html = self.render("See [the docs](https://example.com/docs) here.\n")
        self.assertIn('<a href="https://example.com/docs">the docs</a>', html)

    def test_inline_code_protected_from_bold_italic(self):
        # Asterisks *inside* a code span must stay literal, not become <em>.
        html = self.render("Use `a * b` for multiplication.\n")
        self.assertIn("<code>a * b</code>", html)
        self.assertNotIn("<em>", html)

    def test_html_is_escaped_outside_code(self):
        html = self.render("Compare `<a>` and <b>bare tags</b> in prose.\n")
        # The bare (non-code) tag must be escaped so a report can't inject
        # markup; the code-quoted one is escaped too, just inside <code>.
        self.assertNotIn("<b>bare tags</b>", html)
        self.assertIn("&lt;b&gt;bare tags&lt;/b&gt;", html)


class TestSelfContained(MdToHtmlTest):
    def test_document_has_inlined_style_and_no_external_refs(self):
        html = self.render("# Title\n\nBody text.\n")
        self.assertIn("<style>", html)
        self.assertNotIn("<link ", html)
        self.assertNotIn("<script src", html)
        self.assertNotIn("http://", html)  # no CDN fetch of any kind
        self.assertIn("<!doctype html>", html.lower())

    def test_title_derived_from_first_h1(self):
        html = self.render("# My Report Title\n\nBody.\n")
        self.assertIn("<title>My Report Title</title>", html)

    def test_fallback_title_from_filename_when_no_h1(self):
        p = self.write_md("Just a paragraph, no heading.\n", name="weekly.md")
        rc, out, err = run_script(p)
        self.assertEqual(rc, 0, err)
        self.assertIn("<title>weekly</title>", out)


class TestOutputFile(MdToHtmlTest):
    def test_writes_to_output_path_when_given(self):
        src = self.write_md("# Written\n\nHello.\n")
        dest = self.tmp / "out.html"
        rc, out, err = run_script(src, dest)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, "")  # silent success when writing to a file
        self.assertTrue(dest.exists())
        self.assertIn("<h1>Written</h1>", dest.read_text(encoding="utf-8"))


class TestR4Regressions(MdToHtmlTest):
    """Bugs found in review of a report renderer fed untrusted model output."""

    def test_link_href_cannot_break_out_of_attribute(self):
        # A quote in the URL must be neutralized, not injected as a live attr.
        out = self.render('[click](x"onerror=alert(1))')
        self.assertNotIn(' onerror=', out)          # no separate live attribute
        self.assertIn("&quot;onerror", out)          # quote escaped inside href

    def test_dangerous_scheme_link_is_not_a_link(self):
        out = self.render('[click](javascript:alert(1))')
        self.assertNotIn('<a href="javascript', out)

    def test_snake_case_is_not_italicized(self):
        # Underscore emphasis must not trigger intra-word — reports are full of
        # snake_case identifiers.
        out = self.render(
            "The tool get_licences_by_id for case_id billing_happy_3.")
        self.assertNotIn("<em>", out)

    def test_title_is_escaped_exactly_once(self):
        out = self.render("# Foo & Bar")
        self.assertIn("<title>Foo &amp; Bar</title>", out)
        self.assertNotIn("&amp;amp;", out)           # not double-escaped

    def test_table_cell_split_respects_code_span_pipe(self):
        out = self.render("| A | B |\n| --- | --- |\n| `a|b` | x |\n")
        self.assertIn("<code>a|b</code>", out)       # pipe stayed inside the code
        self.assertEqual(out.count("<td"), 2)        # 2 cells, not 3

    def test_code_span_sentinel_in_the_input_does_not_crash(self):
        # The code-span stash wraps its index in private-use codepoints because
        # real Markdown does not contain them — but "does not" is an assumption
        # about input, and this converter's input is model output. A document
        # carrying the sentinel itself indexed past the stash list and took the
        # converter down with an IndexError; it must render instead.
        # Written as escapes: the literal codepoints are invisible in an
        # editor, so a pasted-in copy of this test would silently stop
        # testing anything.
        sentinel = "\ue000" + "7" + "\ue001"
        out = self.render(f"Report {sentinel} and `real code` here.")
        self.assertIn("<code>real code</code>", out)
        self.assertIn("Report", out)


class TestErrorContract(MdToHtmlTest):
    """Matches every other scorer's contract: exit 0 on success, or exit 2
    with {"error": ...} JSON on stdout for bad input — never a traceback."""

    def test_missing_input_file_exits_2(self):
        rc, out, err = run_script(self.tmp / "nope.md")
        self.assertEqual(rc, 2)
        payload = json.loads(out)
        self.assertIn("error", payload)
        self.assertNotIn("Traceback", err)

    def test_unwritable_output_path_exits_2(self):
        src = self.write_md("# X\n")
        bad_dest = self.tmp / "no_such_dir" / "out.html"
        rc, out, err = run_script(src, bad_dest)
        self.assertEqual(rc, 2)
        payload = json.loads(out)
        self.assertIn("error", payload)
        self.assertNotIn("Traceback", err)


class TestCleanCli(MdToHtmlTest):
    def test_help_exits_0(self):
        rc, out, err = run_script("--help")
        self.assertEqual(rc, 0, err)
        self.assertIn("usage:", out)
        self.assertNotIn("Traceback", err)

    def test_version_matches_harness_version(self):
        sys.path.insert(0, str(SCRIPTS))
        try:
            from _common import HARNESS_VERSION
        finally:
            sys.path.pop(0)
        rc, out, err = run_script("--version")
        self.assertEqual(rc, 0, err)
        self.assertIn(HARNESS_VERSION, out + err)

    def test_no_args_exits_nonzero_not_a_traceback(self):
        rc, out, err = run_script()
        self.assertNotEqual(rc, 0)
        self.assertNotIn("Traceback", err)


class TestEmphasisFlanking(MdToHtmlTest):
    """CommonMark 0.31.2 §6.2: a single `*` can open emphasis only as part of
    a LEFT-flanking delimiter run ("not followed by Unicode whitespace", rule
    1) and close one only as part of a RIGHT-flanking run ("not preceded by
    Unicode whitespace", rule 3). Without those, any two asterisks on a line
    paired up — and these reports are full of arithmetic and glob patterns, so
    ordinary prose came out mangled in a way that looked deliberate."""

    def test_spaced_asterisks_in_prose_are_literal(self):
        out = self.render("Total: 5 * 3 * 2 items")
        self.assertNotIn("<em>", out)
        self.assertIn("5 * 3 * 2 items", out)

    def test_glob_patterns_survive(self):
        # `*` followed by punctuation and preceded by whitespace IS
        # left-flanking (clause 2b), so it may open — but the closing `*` is
        # preceded by whitespace and so cannot close. Net: literal text.
        out = self.render("pass --glob *.json and *.md to the scanner")
        self.assertNotIn("<em>", out)
        self.assertIn("*.json", out)
        self.assertIn("*.md", out)

    def test_a_later_real_emphasis_does_not_capture_an_earlier_glob(self):
        """The flanking lookarounds alone are not enough. CommonMark pairs a
        closer with the NEAREST preceding opener; a lazy `(.+?)` pairs it with
        the FIRST, so a legal-but-unmatched opener earlier in the line (the
        `*` of `*.json`, left-flanking by clause 2b) swallowed everything up
        to the first real emphasis. cmark emphasizes only "real" here."""
        out = self.render("globs *.json and *.md, plus *real* emphasis.")
        self.assertIn("<em>real</em>", out)
        self.assertEqual(out.count("<em>"), 1)
        self.assertIn("*.json", out)
        self.assertIn("*.md", out)

    def test_adjacent_spans_pair_independently(self):
        out = self.render("see *foo* and *bar*")
        self.assertIn("<em>foo</em>", out)
        self.assertIn("<em>bar</em>", out)

    def test_real_emphasis_still_renders(self):
        self.assertIn("<em>emphasised</em>",
                      self.render("an *emphasised* word"))
        self.assertIn("<em>multi word span</em>",
                      self.render("a *multi word span* here"))

    def test_real_bold_still_renders(self):
        self.assertIn("<strong>bold</strong>", self.render("a **bold** word"))
        self.assertIn("<strong>two words</strong>",
                      self.render("a **two words** phrase"))

    def test_bold_with_spaced_delimiters_is_literal(self):
        out = self.render("a ** b ** c")
        self.assertNotIn("<strong>", out)

    def test_underscore_identifiers_are_untouched(self):
        out = self.render("the snake_case_name field and _leading too")
        self.assertNotIn("<em>", out)


if __name__ == "__main__":
    unittest.main()
