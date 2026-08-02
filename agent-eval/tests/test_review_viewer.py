#!/usr/bin/env python3
"""Tests for build_review_viewer.py — the static trace/annotation viewer.

Weighted toward OUTPUT ESCAPING on purpose. Two XSS bugs were found and fixed
in this file during review (a raw verdict interpolated into a class attribute,
and a `</script>` in record data terminating the embedded JSON block early),
and neither had a test — so nothing stopped them coming back. OWASP SAMM's rule
is that every fixed vulnerability earns a permanent regression test.

Escaping is tested per CONTEXT rather than once generically, because each
context has its own escape rules and its own bypass: HTML body, quoted
attribute value, and the embedded JSON <script> block. A payload that is inert
in one is live in another.

The reviewer's input is NOT the threat model here. The payloads are what an
evaluated app can put in a trace — a tool result, a model answer, a route name
— which lands in this page verbatim. The app under evaluation is exactly the
thing you do not trust yet; that is why you are evaluating it.

One structural note the assertions depend on: the page ships NO server-rendered
markup in its body. Every detail view is built in Python, stored as a
`detail_html` string on the record, delivered inside the JSON <script> block,
and injected by the viewer's JS. So a raw `<img src=x onerror=...>` DOES appear
in the page bytes, inside that block, and is inert there — script-element
content is raw text to the HTML parser, and only a literal `</script>` ends it
(context 3 below). Asserting "payload not in page" would therefore be both
wrong and falsely reassuring. Body/attribute assertions run against the
rendered `detail_html` fragment, which is what actually reaches the DOM.
"""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"

# A payload that is live in an HTML body, and its telltale in each context.
XSS = '<script>alert(1)</script>'
IMG = '<img src=x onerror=alert(1)>'


def run_viewer(*argv):
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "build_review_viewer.py"),
         *[str(a) for a in argv]],
        capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


class ViewerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write_json(self, name, doc):
        p = self.tmp / name
        p.write_text(json.dumps(doc), encoding="utf-8")
        return p

    def write_jsonl(self, name, rows):
        p = self.tmp / name
        p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        return p

    def page(self, records, *extra):
        """Build a page from records and return its HTML, asserting exit 0."""
        rc, out, err = run_viewer(self.write_json("run.json", records), *extra)
        self.assertEqual(rc, 0, f"exit {rc}: {err}")
        return out

    def data_block(self, page, block_id="trace-data"):
        """The parsed contents of one embedded JSON <script> block."""
        marker = f'id="{block_id}">'
        start = page.index(marker) + len(marker)
        return json.loads(page[start:page.index("</script>", start)])

    def rendered(self, records, idx=0):
        """The detail_html fragment for one record — the markup that actually
        reaches the DOM, and therefore the thing to assert escaping against."""
        return self.data_block(self.page(records))["records"][idx]["detail_html"]


class TestBodyContextEscaping(ViewerTest):
    """Context 1: HTML body. Markup must arrive as text, not as elements."""

    def assert_inert(self, html, *payloads):
        for payload in payloads:
            self.assertNotIn(payload, html,
                             "payload reached the DOM markup unescaped")
        self.assertIn("&lt;", html, "nothing was escaped at all")

    def test_case_id_is_escaped(self):
        self.assert_inert(self.rendered([{"case_id": XSS, "verdict": "fail"}]),
                          XSS)

    def test_answer_text_is_escaped(self):
        # The most likely real vector: the evaluated app's own answer text.
        self.assert_inert(self.rendered([
            {"case_id": "c1", "answer": {"actual": XSS, "expected": "fine"}}]),
            XSS)

    def test_tool_call_name_args_and_result_are_escaped(self):
        self.assert_inert(self.rendered([{"case_id": "c1", "tool_calls": [
            {"name": IMG, "args": {"q": XSS}, "result": {"row": IMG}}]}]),
            IMG, XSS)

    def test_layer_names_are_escaped_not_just_values(self):
        # The dict KEY is rendered too; escaping only the value leaves a hole.
        self.assert_inert(
            self.rendered([{"case_id": "c1", "layers": {IMG: "fail"}}]), IMG)

    def test_prompt_surface_is_escaped(self):
        self.assert_inert(
            self.rendered([{"case_id": "c1", "prompt_surface": XSS}]), XSS)

    def test_route_names_are_escaped(self):
        self.assert_inert(self.rendered([
            {"case_id": "c1",
             "route": {"expected": "billing", "actual": XSS}}]), XSS)

    def test_word_diff_escapes_both_sides(self):
        # difflib output is assembled with f-strings; both the del and the ins
        # branch must escape, and a token only present on one side exercises
        # exactly one branch.
        self.assert_inert(self.rendered([{"case_id": "c1", "result": {
            "expected": f"alpha {XSS} beta",
            "actual": f"alpha {IMG} gamma"}}]), XSS, IMG)

    def test_stage_cost_cells_are_escaped(self):
        self.assert_inert(self.rendered([{"case_id": "c1", "stage_costs": [
            {"stage": XSS, "latency_ms": IMG, "cost_usd": IMG}]}]), XSS, IMG)


class TestAttributeContextEscaping(ViewerTest):
    """Context 2: a quoted attribute value. This is the bug that shipped —
    the verdict string was concatenated straight into class="badge {verdict}",
    so a payload closing the quote could add its own event handler."""

    def test_verdict_cannot_break_out_of_the_class_attribute(self):
        html = self.rendered([{"case_id": "c1",
                               "verdict": '" onmouseover="alert(1)'}])
        self.assertNotIn('onmouseover="alert(1)"', html)
        self.assertNotIn('badge " onmouseover', html)

    def test_unknown_verdict_falls_back_to_a_whitelisted_class(self):
        # The fix is a whitelist, not escaping: an unrecognized verdict must
        # resolve to a known class rather than being carried through at all.
        html = self.rendered([{"case_id": "c1", "verdict": "totally-made-up"}])
        self.assertIn('class="badge v-unscored"', html)

    def test_layer_verdicts_also_use_the_whitelist(self):
        html = self.rendered([
            {"case_id": "c1",
             "layers": {"routing": '"><script>alert(1)</script>'}}])
        self.assertNotIn("<script>alert(1)", html)
        self.assertIn('class="badge v-unscored"', html)


class TestJsonScriptBlockEscaping(ViewerTest):
    """Context 3: the embedded <script type="application/json"> blocks. JSON
    escaping alone does NOT protect these — the HTML parser looks for the
    literal "</script>" inside the block before any JSON parsing happens."""

    def legit_closers(self):
        return 3  # trace-data, annotation-data, and the inline JS block

    def test_closing_script_tag_in_record_data_cannot_end_the_block(self):
        payload = '</script><img src=x onerror=alert(1)>'
        page = self.page([{"case_id": "c1", "answer": {"actual": payload}}])
        self.assertEqual(page.count("</script>"), self.legit_closers(),
                         "record data introduced an extra </script>")

    def test_closing_script_tag_in_an_annotation_cannot_end_the_block(self):
        # Annotations are a second, independently-serialized block; the same
        # guard has to be applied there and it is easy to protect only one.
        recs = self.write_json("run.json", [{"case_id": "c1",
                                             "trace_id": "t1"}])
        anns = self.write_jsonl("a.jsonl", [
            {"trace_id": "t1", "label": "fail",
             "critique": '</script><script>alert(1)</script>'}])
        rc, page, err = run_viewer(recs, "-a", anns)
        self.assertEqual(rc, 0, err)
        self.assertEqual(page.count("</script>"), self.legit_closers())

    def test_embedded_json_still_parses_after_escaping(self):
        # The guard must not corrupt the data it protects: <\/ is a valid JSON
        # escape, so the block still round-trips to the original text.
        payload = 'result: </script> and </div>'
        page = self.page([{"case_id": "c1", "answer": {"actual": payload}}])
        parsed = self.data_block(page)
        self.assertEqual(parsed["records"][0]["answer"]["actual"], payload)


class TestSelfContained(ViewerTest):
    """§22 calls for one self-contained file — a page that fetches anything
    external also leaks the trace contents it was given to the network."""

    def test_no_external_resource_references(self):
        page = self.page([{"case_id": "c1", "verdict": "pass"}])
        for marker in ("<link", 'src="http', 'src="//', "@import",
                       "fetch(", "XMLHttpRequest"):
            self.assertNotIn(marker, page, f"page references {marker}")


class TestLoadingContract(ViewerTest):
    def test_empty_run_is_not_an_error(self):
        rc, out, err = run_viewer(self.write_json("run.json", []))
        self.assertEqual(rc, 0)
        self.assertIn("Nothing to review", out)

    def test_directory_with_no_json_is_an_error(self):
        empty = self.tmp / "emptydir"
        empty.mkdir()
        rc, out, err = run_viewer(empty)
        self.assertEqual(rc, 2)
        self.assertIn("error", json.loads(out))

    def test_missing_path_is_an_error(self):
        rc, out, err = run_viewer(self.tmp / "nope.json")
        self.assertEqual(rc, 2)
        self.assertIn("error", json.loads(out))

    def test_synthesized_id_is_flagged_not_hidden(self):
        # Regression: build_page backfilled case_id from the position BEFORE
        # rendering, so render_record's "neither id present" check was always
        # false and every synthesized id rendered as if it were a real one.
        # A positional id does not correlate back to the dataset — showing it
        # unflagged sends the reviewer hunting for a case that never existed.
        html = self.rendered([{"verdict": "fail"}])
        self.assertIn("synthesized id", html)

    def test_a_real_id_is_not_flagged_as_synthesized(self):
        self.assertNotIn("synthesized id",
                         self.rendered([{"trace_id": "t1", "verdict": "fail"}]))

    def test_backfilled_ids_still_reach_the_data_block(self):
        # The flag fix must not cost the JS its annotation key: every record
        # still needs both ids populated, since annotations are keyed by
        # trace_id.
        rec = self.data_block(self.page([{"verdict": "fail"}]))["records"][0]
        self.assertEqual(rec["case_id"], "case-0")
        self.assertEqual(rec["trace_id"], "case-0")

    def test_output_file_is_written_when_requested(self):
        out_path = self.tmp / "viewer.html"
        rc, out, err = run_viewer(self.write_json("run.json",
                                                  [{"case_id": "c1"}]),
                                  "-o", out_path)
        self.assertEqual(rc, 0, err)
        self.assertIn("c1", out_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
