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
import shutil
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

    def raw_block(self, page, block_id="trace-data"):
        """The verbatim text of one embedded JSON <script> block — what the
        HTML tokenizer sees, before json.loads normalizes the escaping away."""
        marker = f'id="{block_id}">'
        start = page.index(marker) + len(marker)
        return page[start:page.index("</script>", start)]

    def data_block(self, page, block_id="trace-data"):
        """The parsed contents of one embedded JSON <script> block."""
        return json.loads(self.raw_block(page, block_id))

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
        # The guard must not corrupt the data it protects: < is a valid
        # JSON escape, so the block still round-trips to the original text.
        payload = 'result: </script> and </div>'
        page = self.page([{"case_id": "c1", "answer": {"actual": payload}}])
        parsed = self.data_block(page)
        self.assertEqual(parsed["records"][0]["answer"]["actual"], payload)

    def test_html_comment_before_script_cannot_swallow_the_block(self):
        # Escaping only "</" is the half-measure. "<!--" moves the tokenizer to
        # script data escaped state and a following "<script" to script data
        # DOUBLE escaped state, where "</script>" stops closing the element —
        # so this record's answer used to swallow the annotation-data block and
        # the viewer's own <script>, leaving a blank inert page. Verified
        # against a spec-compliant parser, which saw no annotation-data element.
        page = self.page([{"case_id": "c1",
                           "answer": {"actual": "<!--<script>foo"}}])
        # Assert on what the TOKENIZER sees, not on the page bytes: with the old
        # "</"-only escaping the annotation-data text was still present in the
        # file — it just stopped being an element, which a substring check
        # cannot tell apart from a working page.
        self.assertNotIn("<!--", self.raw_block(page))
        self.assertEqual(page.count("</script>"), self.legit_closers())
        self.assertEqual(
            self.data_block(page)["records"][0]["answer"]["actual"],
            "<!--<script>foo")

    def test_no_raw_angle_bracket_survives_in_either_json_block(self):
        # The invariant behind the fix: both dangerous tokenizer transitions
        # start with "<", so no "<" may reach either embedded block at all.
        # Asserting the invariant rather than the two known payloads is what
        # stops the next variant of this bug.
        recs = self.write_json("run.json", [
            {"case_id": "c1", "trace_id": "t1",
             "answer": {"actual": "<!--<script> </script> <img> <b>"}}])
        anns = self.write_jsonl("a.jsonl", [
            {"trace_id": "t1", "label": "fail", "critique": "<!--<script>"}])
        rc, page, err = run_viewer(recs, "-a", anns)
        self.assertEqual(rc, 0, err)
        for block_id in ("trace-data", "annotation-data"):
            self.assertNotIn("<", self.raw_block(page, block_id),
                             f"raw '<' reached the {block_id} block")


class TestOffShapeRecords(ViewerTest):
    """A record is evidence to render, not a schema to enforce (module
    docstring). `x or {}` guarded against null but not against the wrong type,
    so a drifted field took the whole page down with an AttributeError — which
    is neither the promised {"error": ...} contract nor a rendered page."""

    OFF_SHAPE = [
        ("answer", "just a string"),
        ("route", "billing"),
        ("result", ["not", "an", "object"]),
        ("layers", ["routing"]),
        ("stage_costs", ["router"]),
        ("tool_calls", ["get_invoice"]),
        ("tool_calls", "get_invoice"),
    ]

    def test_off_shape_fields_still_render_a_page(self):
        for field, value in self.OFF_SHAPE:
            with self.subTest(field=field, value=value):
                out = self.page([{"case_id": "c1", "verdict": "fail",
                                  field: value}])
                self.assertIn('id="trace-data"', out)

    def test_off_shape_fields_are_flagged_not_silently_skipped(self):
        # Same principle as the synthesized-id flag: a reviewer who cannot see
        # that evidence was dropped reads the page as complete.
        html = self.rendered([{"case_id": "c1", "answer": "just a string",
                               "layers": ["routing"]}])
        self.assertIn("Ignored off-shape field(s)", html)
        self.assertIn("answer", html)
        self.assertIn("layers", html)

    def test_the_raw_value_is_still_exported(self):
        # Flagging it must not delete it — the export is the reviewer's evidence.
        rec = self.data_block(self.page(
            [{"case_id": "c1", "answer": "just a string"}]))["records"][0]
        self.assertEqual(rec["answer"], "just a string")

    def test_well_shaped_records_carry_no_flag(self):
        html = self.rendered([{"case_id": "c1", "verdict": "pass",
                               "layers": {"routing": "pass"},
                               "answer": {"expected": "a", "actual": "a"}}])
        self.assertNotIn("off-shape", html)

    def test_a_non_object_entry_inside_a_good_list_is_dropped_and_flagged(self):
        html = self.rendered([{"case_id": "c1", "tool_calls": [
            {"name": "get_invoice"}, "bare string"]}])
        self.assertIn("get_invoice", html)
        self.assertIn("Ignored off-shape field(s)", html)


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


class TestIdBackfillWithExplicitNulls(ViewerTest):
    """Absent and null are different, and the backfill has to treat them the
    same way. `setdefault` cannot: it tests key EXISTENCE, so a record written
    as {"trace_id": null} — what a harness emits in the trace-less mode the
    span tree explicitly supports — kept the null and never got an id."""

    def records_from(self, records):
        return self.data_block(self.page(records))["records"]

    def test_explicit_null_ids_are_backfilled_like_absent_ones(self):
        recs = self.records_from([{"case_id": None, "trace_id": None,
                                   "verdict": "fail"}])
        self.assertEqual(recs[0]["case_id"], "case-0")
        self.assertEqual(recs[0]["trace_id"], "case-0")

    def test_null_ids_do_not_collide_into_one_annotation_key(self):
        """The bug this actually caused. The viewer keys annotations by
        trace_id (latest[r.trace_id]), and JS coerces a null key to the single
        string "null" — so every trace-less record shared ONE entry: annotating
        one marked them all annotated, and the "unannotated only" filter hid
        the rest. Reviewed work silently disappearing is worse than a crash."""
        recs = self.records_from([{"trace_id": None, "verdict": "fail"},
                                  {"trace_id": None, "verdict": "pass"},
                                  {"trace_id": None, "verdict": "fail"}])
        ids = [r["trace_id"] for r in recs]
        self.assertEqual(len(set(ids)), 3, f"annotation keys collide: {ids}")
        self.assertNotIn(None, ids)

    def test_a_null_id_still_renders_the_synthesized_flag(self):
        html = self.data_block(self.page(
            [{"case_id": None, "verdict": "fail"}]))["records"][0]["detail_html"]
        self.assertIn("synthesized id", html)

    def test_one_real_id_still_backfills_the_other(self):
        recs = self.records_from([{"case_id": "c1", "trace_id": None}])
        self.assertEqual(recs[0]["case_id"], "c1")
        self.assertEqual(recs[0]["trace_id"], "c1")


class TestDirectoryScan(ViewerTest):
    """A directory scan matches on filename, which says nothing about content.

    Pointed at a real run root, the scan swept up manifest.json and a canary
    response and rendered them as two synthesized-id "cases" — a page with zero
    real cases, exit 0, and nothing to tell the reviewer the run was not there.
    Meanwhile the actual records sat one level down and the correct path
    reported "no *.json files found — check the path"."""

    def run_dir(self, files):
        d = self.tmp / "run"
        d.mkdir(exist_ok=True)
        for name, doc in files.items():
            path = d / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(doc), encoding="utf-8")
        return d

    MANIFEST = {"run_id": "r1", "dataset_version": "3", "cases": ["a", "b"]}
    CANARY = {"message": "ok"}

    def test_run_artifacts_are_not_rendered_as_cases(self):
        d = self.run_dir({"manifest.json": self.MANIFEST,
                          "canary_response.json": self.CANARY,
                          "c1.json": {"case_id": "c1", "verdict": "fail"}})
        rc, out, err = run_viewer(d)
        self.assertEqual(rc, 0, err)
        ids = [r["case_id"] for r in self.data_block(out)["records"]]
        self.assertEqual(ids, ["c1"])

    def test_skipped_files_are_reported_not_silent(self):
        """Dropped evidence is always reported — the same rule offshape_fields
        follows. On stderr, so a page piped to stdout stays valid HTML."""
        d = self.run_dir({"manifest.json": self.MANIFEST,
                          "c1.json": {"case_id": "c1"}})
        rc, out, err = run_viewer(d)
        self.assertEqual(rc, 0, err)
        self.assertIn("manifest.json", err)
        self.assertIn("skipped", err)
        self.assertTrue(out.lstrip().startswith("<!doctype html"))

    def test_a_directory_of_only_artifacts_errors_and_names_the_fix(self):
        """Not "nothing to review": that page means a run legitimately produced
        no cases, and printing it here would read as a finished, empty run when
        the truth is the path is one level off."""
        d = self.run_dir({"manifest.json": self.MANIFEST,
                          "canary_response.json": self.CANARY})
        rc, out, err = run_viewer(d)
        self.assertEqual(rc, 2)
        error = json.loads(out)["error"]
        self.assertIn("manifest.json", error)
        self.assertIn("--glob", error)

    def test_glob_reaches_a_nested_run_layout(self):
        """The layout `run` actually writes: reports/<id>/cases/<case-id>/*.json.
        Previously unreachable — the run root rendered artifacts and the cases
        directory reported no *.json files, because it holds directories."""
        d = self.run_dir({
            "manifest.json": self.MANIFEST,
            "cases/units-happy/verdict.json": {"case_id": "units-happy",
                                               "verdict": "pass"},
            "cases/units-happy/response.json": {"case_id": "units-happy",
                                                "body": "raw"},
            "cases/oos-refusal/verdict.json": {"case_id": "oos-refusal",
                                               "verdict": "fail"},
        })
        rc, out, err = run_viewer(d, "--glob", "cases/*/verdict.json")
        self.assertEqual(rc, 0, err)
        recs = self.data_block(out)["records"]
        self.assertEqual(sorted(r["case_id"] for r in recs),
                         ["oos-refusal", "units-happy"])
        # response.json carries a case_id too, so only the glob keeps the raw
        # responses from doubling every case.
        self.assertEqual(len(recs), 2)

    def test_an_unmatched_glob_says_so_without_guessing(self):
        d = self.run_dir({"c1.json": {"case_id": "c1"}})
        rc, out, err = run_viewer(d, "--glob", "cases/*/verdict.json")
        self.assertEqual(rc, 2)
        self.assertIn("cases/*/verdict.json", json.loads(out)["error"])

    def test_duplicate_case_ids_get_distinct_trace_ids(self):
        """trace_id is the viewer's primary key — the latest[] map, the
        already-annotated dot, the "unannotated only" filter and every
        exported annotation are keyed by it. Two records sharing one is the
        same defect the explicit-null backfill fixed, one level up:
        annotating either marked both done and hid the other."""
        d = self.run_dir({"a.json": {"case_id": "billing-1", "verdict": "fail"},
                          "b.json": {"case_id": "billing-1", "verdict": "pass"}})
        rc, out, err = run_viewer(d)
        self.assertEqual(rc, 0, err)
        records = self.data_block(out)["records"]
        self.assertEqual(len(records), 2)
        ids = [r["trace_id"] for r in records]
        self.assertEqual(len(set(ids)), 2, ids)
        # The case_id is left alone: only the viewer's own key is suffixed.
        self.assertEqual([r["case_id"] for r in records],
                         ["billing-1", "billing-1"])

    def test_duplicate_id_is_flagged_in_the_rendered_page(self):
        # Same rule as the synthesized-id flag: a reviewer who cannot see that
        # an id was rewritten reads it as the dataset's own.
        d = self.run_dir({"a.json": {"case_id": "dup", "verdict": "fail"},
                          "b.json": {"case_id": "dup", "verdict": "pass"}})
        rc, out, err = run_viewer(d)
        self.assertEqual(rc, 0, err)
        second = self.data_block(out)["records"][1]
        self.assertEqual(second["duplicate_trace_id"], "dup")
        self.assertIn("Duplicate id", second["detail_html"])

    def test_the_export_carries_the_original_id_not_only_the_suffixed_one(self):
        """The suffix is the VIEWER's ("dup#1"), but the export is appended to
        the user's tracked annotations JSONL, so on its own it leaks upstream as
        an id present in no dataset — and it is not stable either, being the
        record's position under whatever --glob built the page.

        A source-level check: there is no JS runtime here, so this asserts the
        export path reads the field the record carries. The record half is
        covered by test_duplicate_id_is_flagged_in_the_rendered_page."""
        d = self.run_dir({"a.json": {"case_id": "dup", "verdict": "fail"},
                          "b.json": {"case_id": "dup", "verdict": "pass"}})
        rc, out, err = run_viewer(d)
        self.assertEqual(rc, 0, err)
        self.assertIn("entry.duplicate_trace_id = r.duplicate_trace_id", out)

    def test_distinct_ids_are_never_rewritten(self):
        d = self.run_dir({"a.json": {"case_id": "a", "verdict": "pass"},
                          "b.json": {"case_id": "b", "verdict": "fail"}})
        rc, out, err = run_viewer(d)
        self.assertEqual(rc, 0, err)
        for r in self.data_block(out)["records"]:
            self.assertNotIn("#", r["trace_id"])
            self.assertNotIn("duplicate_trace_id", r)

    def test_errored_tool_call_is_not_shown_as_uncaptured(self):
        """The convention records a tool result only on success, so an errored
        call has none — and "(no result captured)" told the reviewer that
        tracing was misconfigured on exactly the records where the app had
        failed."""
        rec = {"case_id": "c1", "verdict": "fail", "tool_calls": [
            {"name": "get_invoice", "args": {"id": 1}, "result": None,
             "error": "500"}]}
        rc, out, err = run_viewer(self.write_json("r.json", [rec]))
        self.assertEqual(rc, 0, err)
        detail = self.data_block(out)["records"][0]["detail_html"]
        self.assertIn("errored: 500", detail)
        self.assertNotIn("(no result captured)", detail)

    def test_array_shaped_run_artifacts_are_skipped_too(self):
        """The is_record filter guarded only the dict branch, so a run artifact
        that happens to be a top-level ARRAY walked past it and rendered as
        synthesized-id "cases" — the exact failure RECORD_KEYS exists to stop,
        one shape over."""
        d = self.run_dir({"stages.json": [{"stage": "route", "cost_usd": 0.01},
                                          {"stage": "answer", "cost_usd": 0.2}],
                          "c1.json": {"case_id": "c1", "verdict": "pass"}})
        rc, out, err = run_viewer(d)
        self.assertEqual(rc, 0, err)
        self.assertEqual([r["case_id"] for r in self.data_block(out)["records"]],
                         ["c1"])
        self.assertIn("stages.json", err)

    def test_an_array_of_real_records_is_still_read(self):
        # The filter must not cost the legitimate array-of-cases layout.
        d = self.run_dir({"all.json": [{"case_id": "a", "verdict": "pass"},
                                       {"case_id": "b", "verdict": "fail"}]})
        rc, out, err = run_viewer(d)
        self.assertEqual(rc, 0, err)
        self.assertEqual([r["case_id"] for r in self.data_block(out)["records"]],
                         ["a", "b"])
        self.assertEqual(err, "")

    def test_a_rejected_glob_keeps_the_error_contract(self):
        """Path.glob RAISES on an absolute or empty pattern, which is exit 1 and
        a traceback — the flag added to make this script easier to point at a
        run was the one input that escaped the exit-2 JSON contract."""
        d = self.run_dir({"c1.json": {"case_id": "c1"}})
        for pattern in ("/abs/*.json", ""):
            rc, out, err = run_viewer(d, "--glob", pattern)
            self.assertEqual(rc, 2, f"{pattern!r}: {err}")
            error = json.loads(out)["error"]
            self.assertIn("--glob", error)
            self.assertNotIn("Traceback", err)

    def test_each_rejected_glob_names_the_fix_that_fits_it(self):
        """One shared message described only the absolute-path cause, so
        `--glob ''` was told it was "not an absolute path" — advice that does not
        apply and cannot be acted on. The two causes have different fixes."""
        d = self.run_dir({"c1.json": {"case_id": "c1"}})
        rc, out, _ = run_viewer(d, "--glob", "")
        self.assertNotIn("absolute", json.loads(out)["error"])
        rc, out, _ = run_viewer(d, "--glob", "/abs/*.json")
        self.assertIn("absolute", json.loads(out)["error"])

    def test_flat_directory_default_is_unchanged(self):
        d = self.run_dir({"a.json": {"case_id": "a", "verdict": "pass"},
                          "b.json": {"case_id": "b", "verdict": "fail"}})
        rc, out, err = run_viewer(d)
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.data_block(out)["records"]), 2)
        self.assertEqual(err, "")


NODE = shutil.which("node")

# Boots the page's own JS against a stub DOM and reports which records the
# viewer considers annotated. Asserting on a reimplementation of the join would
# prove nothing — the bug being guarded is in the shipped JS, so the shipped JS
# is what runs. The observable is the one the reviewer sees: the "done" dot on
# each list row, plus the "N annotated" counter.
HARNESS = r"""
const fs = require('fs');
const page = fs.readFileSync(process.argv[1], 'utf8');
function block(id) {
  const m = page.match(new RegExp('id="' + id + '">([\\s\\S]*?)</script>'));
  return m[1];
}
const rows = [];
function el(id) {
  return { id: id, value: '', textContent: '', innerHTML: '',
           style: {}, focus() {}, select() {}, addEventListener() {},
           appendChild(c) { if (id === 'trace-list') rows.push(c.innerHTML); },
           setAttribute() {}, click() {} };
}
const stats = el('stats');
const nodes = { 'trace-data': { textContent: block('trace-data') },
                'annotation-data': { textContent: block('annotation-data') },
                'stats': stats };
global.document = {
  getElementById: (id) => nodes[id] || (nodes[id] = el(id)),
  createElement: (t) => el(t),
  addEventListener() {},
  body: el('body'),
};
global.localStorage = { getItem: () => null, setItem() {} };
global.window = global;
const js = page.match(/<script>([\s\S]*?)<\/script>\s*<\/body>/)[1];
new Function(js)();
const done = rows.filter((h) => h.indexOf('dot done') !== -1)
                 .map((h) => h.match(/<b>([^<]*)<\/b>/)[1]);
console.log(JSON.stringify({ done: done, stats: stats.textContent }));
"""


class BootedPage(ViewerTest):
    """Base for the tests that actually RUN the page's JS under node."""

    def boot(self, records, annotations):
        page = self.tmp / "page.html"
        rc, out, err = run_viewer(self.write_json("run.json", records),
                                  "-a", self.write_jsonl("a.jsonl",
                                                         annotations),
                                  "-o", page)
        self.assertEqual(rc, 0, err)
        proc = subprocess.run([NODE, "-e", HARNESS, str(page)],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def annotation(self, case_id, trace_id, **extra):
        return dict({"trace_id": trace_id, "case_id": case_id, "label": "fail",
                     "category": "hallucinated-count", "reviewer": "priya",
                     "ts": "2026-08-02T14:03:00Z"}, **extra)


@unittest.skipUnless(NODE, "node is required to boot the page's JS")
class TestPrototypeNamedIds(BootedPage):
    """case_id, trace_id and category are all AUTHORED strings, and on a plain
    {} they collide with Object.prototype.

    Same failure the case_id join exists to prevent, reached through the
    lookup instead of the key: byCase['toString'] is the inherited FUNCTION, so
    the guard read as "this annotation matches a record", annotationKey took
    .trace_id off it (undefined), and every such annotation collapsed onto
    latest[undefined]. On the read side latest['constructor'] is truthy on an
    EMPTY map, so an unreviewed trace rendered a done dot and dropped out of
    the "unannotated" filter — work the reviewer never did, reported as done.
    Hence Object.create(null) for byCase/latest/counts, and the
    Object.prototype.hasOwnProperty.call form those maps then require (MDN:
    null-prototype objects have no .hasOwnProperty of their own)."""

    NAMES = ("toString", "constructor", "valueOf", "__proto__",
             "hasOwnProperty")

    def test_a_prototype_named_case_id_is_not_born_annotated(self):
        recs = [{"case_id": n, "trace_id": "t" + str(i), "verdict": "pass"}
                for i, n in enumerate(self.NAMES)]
        out = self.boot(recs, [])
        self.assertEqual(out["done"], [])
        self.assertIn("0 annotated", out["stats"])

    def test_a_prototype_named_trace_id_is_not_born_annotated(self):
        recs = [{"case_id": "c" + str(i), "trace_id": n, "verdict": "pass"}
                for i, n in enumerate(self.NAMES)]
        out = self.boot(recs, [])
        self.assertEqual(out["done"], [])
        self.assertIn("0 annotated", out["stats"])

    def test_a_prototype_named_case_id_still_joins_its_own_annotation(self):
        # The fix must not cost the join it sits inside: these ids are legal
        # case names, not merely hazards to neutralize.
        recs = [{"case_id": "toString", "trace_id": "t1"},
                {"case_id": "plain", "trace_id": "t2"}]
        out = self.boot(recs, [self.annotation("toString", "t1")])
        self.assertEqual(out["done"], ["toString"])
        self.assertIn("1 annotated", out["stats"])

    def test_an_unmatched_prototype_named_annotation_lands_on_nothing(self):
        # No record has this case_id, so the annotation belongs to no row —
        # and must not mark an unrelated one done.
        recs = [{"case_id": "plain", "trace_id": "t1"}]
        out = self.boot(recs, [self.annotation("valueOf", "gone")])
        self.assertEqual(out["done"], [])

    def test_duplicate_hasownproperty_case_ids_do_not_crash_the_page(self):
        # byCase.hasOwnProperty was the collision guard AND a writable key: the
        # first record replaced the method with a record object, so the second
        # threw "is not a function" and the whole viewer script died at load —
        # a blank page, not a degraded one.
        recs = [{"case_id": "hasOwnProperty", "trace_id": "t1"},
                {"case_id": "hasOwnProperty", "trace_id": "t2"}]
        self.assertEqual(self.boot(recs, [])["done"], [])

    def test_a_proto_case_id_is_still_seen_as_a_collision(self):
        # `byCase['__proto__'] = r` never creates an OWN property (the setter
        # swallows it), so the duplicate guard could never fire for that id.
        recs = [{"case_id": "__proto__", "trace_id": "t1"},
                {"case_id": "__proto__", "trace_id": "t2"}]
        out = self.boot(recs, [self.annotation("__proto__", "t2")])
        # Ambiguous case_id -> fall back to trace_id, which annotates t2 only.
        self.assertEqual(out["done"], ["__proto__"])
        self.assertIn("1 annotated", out["stats"])


@unittest.skipUnless(NODE, "node is required to boot the page's JS")
class TestAnnotationJoinOnLoad(BootedPage):
    """Loading an annotations JSONL has to land each annotation on the record
    it belongs to, and trace_id alone cannot do that.

    Two records sharing a trace_id get the later one suffixed to "abc#1", where
    the number is that record's POSITION under whatever --glob built the page.
    Export already carries the original id and case_id for exactly this reason
    (annotation-ux.md: "case_id is never suffixed — join on those, not on the
    #N id"), but the load path keyed straight off trace_id — so one case added
    ahead of the duplicate renumbered it, and a previously-reviewed record came
    back with no dot, back inside the "unannotated only" filter, and missing
    from the taxonomy counts. Silent, and it looks like work you never did."""

    DUPES = [{"case_id": "c1", "trace_id": "abc", "verdict": "fail"},
             {"case_id": "c2", "trace_id": "abc", "verdict": "pass"}]

    def test_a_suffixed_record_keeps_its_annotation_when_the_order_shifts(self):
        # The regression: annotated when c2 was record 1 and got "abc#1", then
        # one case lands ahead of it and it renumbers to "abc#2".
        prior = self.annotation("c2", "abc#1", duplicate_trace_id="abc")
        shifted = [{"case_id": "c0", "trace_id": "zzz"}] + self.DUPES
        self.assertEqual(self.boot(shifted, [prior])["done"], ["c2"])

    def test_the_stable_case_still_matches(self):
        prior = self.annotation("c2", "abc#1", duplicate_trace_id="abc")
        self.assertEqual(self.boot(self.DUPES, [prior])["done"], ["c2"])

    def test_an_annotation_without_case_id_still_matches_on_trace_id(self):
        # Lines written before case_id was exported, and hand-written ones.
        # The join is a fallback chain, not a re-key: dropping trace_id
        # matching would strand every such line as unannotated.
        prior = {"trace_id": "abc", "label": "pass"}
        self.assertEqual(self.boot(self.DUPES, [prior])["done"], ["c1"])

    def test_an_ambiguous_case_id_does_not_move_an_annotation(self):
        # Two records can also share a case_id; then case_id identifies no
        # single record and guessing one would attach a reviewer's verdict to
        # a trace they never read. Fall back to trace_id, which at least is
        # unique on the page.
        recs = [{"case_id": "same", "trace_id": "t1"},
                {"case_id": "same", "trace_id": "t2"}]
        prior = self.annotation("same", "t2")
        self.assertEqual(self.boot(recs, [prior])["done"], ["same"])
        self.assertIn("1 annotated", self.boot(recs, [prior])["stats"])

    def test_taxonomy_counts_one_record_once(self):
        # latest[] is keyed per record; a join that mapped two annotations onto
        # the same key must collapse them, not double the category count.
        out = self.boot(self.DUPES, [
            self.annotation("c2", "abc#1", duplicate_trace_id="abc"),
            self.annotation("c2", "abc#2", duplicate_trace_id="abc")])
        self.assertIn("1 annotated", out["stats"])


if __name__ == "__main__":
    unittest.main()


class TestARealRunDirectoryIsReadable(ViewerTest):
    """verdict.json is a verdict, not a record: pointed at a run_cases.py run,
    every case used to render "No trajectory data / Nothing to diff"
    (2026-09-21 audit). The viewer joins each verdict with its siblings."""

    def run_dir(self, holdout=False, message="night shift headcount?",
                answer="There are 7 nurses."):
        root = self.tmp / "reports" / "smoke-20260901T120000Z"
        case = root / "cases" / "c-0a0a0a0a"
        case.mkdir(parents=True)
        (root / "manifest.yaml").write_text("{}", encoding="utf-8")
        (case / "verdict.json").write_text(json.dumps({
            "case_id": "c-0a0a0a0a", "verdict": "fail", "holdout": holdout,
            "latency_s": 0.25, "layers": {
                "routing": {"layer": "routing", "verdict": "pass", "row": {
                    "expected": "shifts", "observed": "shifts"}},
                "answer": {"layer": "answer", "verdict": "fail", "checks": [
                    {"check": "must_contain", "entry": "$999.99",
                     "status": "fail", "reason": "not found in answer"}]},
                "authz": {"layer": "authz", "verdict": "n/a"}}}),
            encoding="utf-8")
        (case / "request.json").write_text(json.dumps(
            {"body": {"message": message}}), encoding="utf-8")
        (case / "expect.json").write_text(json.dumps(
            {"result": {"scalar": 7}}), encoding="utf-8")
        (case / "actual.json").write_text(json.dumps({"scalar": 3}),
                                          encoding="utf-8")
        (case / "answer.txt").write_text(answer, encoding="utf-8")
        return root

    def test_the_request_the_answer_and_the_reason_are_on_the_page(self):
        rc, page, err = run_viewer(self.run_dir())
        self.assertEqual(rc, 0, err)
        for needle in ("night shift headcount?", "There are 7 nurses.",
                       "not found in answer", "Checks that did not pass",
                       "Router decision", "250 ms"):
            self.assertIn(needle, page)
        self.assertNotIn("Nothing to diff", page)
        self.assertNotIn("No trajectory data", page)

    def test_joined_material_is_escaped_like_everything_else(self):
        rc, page, err = run_viewer(self.run_dir(message=XSS, answer=IMG))
        self.assertEqual(rc, 0, err)
        self.assertNotIn(XSS, page)
        self.assertNotIn(IMG, page)

    def test_a_sealed_holdout_case_is_never_put_on_the_page(self):
        rc, page, err = run_viewer(self.run_dir(
            holdout=True, message="SEALED-QUESTION", answer="SEALED-ANSWER"))
        self.assertEqual(rc, 0, err)
        self.assertNotIn("SEALED-QUESTION", page)
        self.assertNotIn("SEALED-ANSWER", page)
        self.assertIn("holdout", err)
