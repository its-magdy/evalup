#!/usr/bin/env python3
"""Generate a self-contained static HTML trace/annotation viewer.

Implements the annotation UX: everything on one screen (span tree,
expected-vs-actual diff, per-stage latency/cost, the implicated
prompt/surface), one-keystroke binary pass/fail with a free-text critique box
beside it, hotkey navigation, and a live taxonomy (category -> count) sidebar
for axial coding. See skills/analyze/references/annotation-ux.md for the full
workflow this viewer supports and the write-path decision (documented
manual-edit flow, not a localhost sidecar — see that doc's "Why manual-edit,
not a sidecar" section).

INPUT (run traces): a JSON file containing a list of case records, a JSON
file shaped {"cases": [...]} / {"results": [...]}, or a directory of `*.json`
files (one record per file). A directory scan keeps only files carrying at
least one case-record field (see RECORD_KEYS) — a run root also holds
results.json and canary artifacts, which are not cases (its manifest.yaml the
`*.json` scan never matches at all) — and `--glob` selects
a nested layout without this script guessing at the depth, e.g. `--glob
'cases/*/verdict.json'`. Every field on
a record is optional except that each record should carry a `case_id` and/or
`trace_id` (one is synthesized from position if both are absent, and that is
reported, not hidden). Recognized shape (extra keys are ignored, not an
error — a record is evidence to render, not a strict schema to enforce):

    {
      "case_id": "billing-happy-3f9a2c1d",
      "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
      "verdict": "fail",
      "layers": {"routing": "pass", "trajectory": "fail", "answer": "pass"},
      "prompt_surface": "router system prompt (agents/router.md)",
      "route": {"expected": "billing", "actual": "billing"},
      "tool_calls": [{"name": "get_invoice", "args": {...}, "result": {...},
                      "duration_ms": 120}],
      "answer": {"expected": "...", "actual": "..."},
      "result": {"expected": {"scalar": 3}, "actual": {"scalar": 0}},
      "latency_ms": 4210, "cost_usd": 0.014,
      "stage_costs": [{"stage": "router", "latency_ms": 300,
                       "cost_usd": 0.001}, ...]
    }

NOTE on the three cost fields above: `run_cases.py` writes NONE of them, so on
a run this harness produced the cost panel renders "No latency/cost recorded on
this record" and always will. They are kept because a record is evidence to
render (see the docstring below) and a harness that does capture per-stage cost
should be able to show it -- not because anything here computes one. There is
no `stages.json` artifact anywhere and never was: `stage_costs` is read off the
CASE RECORD. Cost for a runner-produced run is computed after the fact by
`scripts/score_cost.py`, which is run-level and writes no per-case field
(runner-contract.md SS5.7).

INPUT (annotations, optional): a JSONL file, one line per annotation event —
`{"trace_id": ..., "label": "pass"|"fail", "category": "...", "critique":
"...", "reviewer": "...", "ts": "..."}` (see case-format's sibling doc,
annotation-ux.md, for the full field contract). Loaded read-only to seed the
viewer's taxonomy counts and per-trace status; the viewer never writes this
file itself (see the docstring above) — it renders an export box the
reviewer copies/downloads and appends to this same file by hand.

Usage: build_review_viewer.py <run-path> [-a annotations.jsonl] [-o out.html]
                              [--glob 'cases/*/verdict.json']
Error contract: on unreadable/malformed input print {"error": "..."} to
stdout and exit 2; otherwise exit 0. A *file* that parses fine but contains
zero case records is NOT an error (a fresh run can legitimately have nothing
to review yet) — the viewer renders a "nothing to review" page instead of
guessing at content that was never there. A *directory* is held to a stricter
rule: matching no files, or matching only run artifacts (manifest, canary),
means the path or --glob depth is wrong, not that the run is empty, and is
reported as an error naming the likely fix. See load_records.
"""
import argparse
import difflib
import html
import json
import math
import pathlib
import sys

from _common import add_version_flag, die, load_json, load_jsonl, write_output

# Tool-result/answer text beyond this is elided in the span tree — the raw
# record is still exported in full via the embedded JSON, only the rendered
# HTML is capped, so one giant blob can't make the page unusable.
TRUNCATE_AT = 4000


# --- loading --------------------------------------------------------------

# A directory scan matches files by name, which says nothing about what is in
# them: pointed at a run root, the scan swept up results.json and
# canary_response.json and rendered them as two synthesized-id "cases" — a page
# with zero real cases and exit 0. A record is recognized by carrying at least
# one field this viewer actually renders. Deliberately permissive (any one key
# is enough, since every field is optional), and deliberately not a schema:
# it separates a case record from a run artifact, nothing more.
#
# Only the identity fields are listed here; the rendered fields come from
# RECORD_SHAPE below, so adding a field to the renderer cannot leave
# recognition behind (which would silently drop the file from a directory
# scan).
RECORD_KEYS = ("case_id", "trace_id", "verdict", "prompt_surface")


def is_record(doc):
    return isinstance(doc, dict) and any(
        k in doc for k in (*RECORD_KEYS, *RECORD_SHAPE))


RUN_DIR_GLOB = "cases/*/verdict.json"


def load_records(run_path, pattern=None):
    """Return (records, source_note). Distinguishes a structurally bad input
    (wrong path, unreadable JSON, a directory whose matched files hold no case
    records at all — likely the wrong depth) from a valid input that simply
    contains zero records (a legitimately empty run) — only the former is an
    error.

    `pattern` is a glob relative to the directory, so a run layout that keeps
    records one level down (reports/<id>/cases/<case-id>/verdict.json) is
    addressable without this script guessing at the depth: --glob
    'cases/*/verdict.json'. The default stays a flat *.json scan -- except for
    the one layout this package writes itself: a directory holding
    manifest.yaml and cases/ IS a run_cases.py run directory, so its records
    are at RUN_DIR_GLOB and no flag is needed. That is recognition of our own
    output, not a guess at someone else's depth; the viewer used to fail by
    default on the very thing `run` produces (2026-09 audit)."""
    p = pathlib.Path(run_path)
    if not p.exists():
        die(f"bad input: {run_path}: no such file or directory")
    if pattern is None:
        is_run_dir = (p / "manifest.yaml").is_file() and (p / "cases").is_dir()
        pattern = RUN_DIR_GLOB if is_run_dir else "*.json"
    if p.is_dir():
        return _load_dir(p, run_path, pattern)
    return _load_file(p, run_path)


def _sibling(case_dir, name):
    """A per-case artifact next to verdict.json, or None. Lenient like every
    load here: a missing or unreadable sibling is evidence not shown, never a
    reason to withhold the rest of the record."""
    path = case_dir / name
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not name.endswith(".json"):
        return text
    try:
        return json.loads(text)
    except ValueError:
        return None


def from_case_dir(verdict, case_dir):
    """A run_cases.py verdict.json, joined with its siblings into the record
    shape this page renders.

    verdict.json is a VERDICT: it carries layers and nothing a reviewer can
    read -- no request, no answer, no expected/actual pair. Pointed at a real
    run, every case rendered "No trajectory data / Nothing to diff / No
    latency" (2026-09-21 audit): the error-analysis screen worked only on
    hand-written records, never on the thing `run` produces. The material was
    always one directory away (runner-contract.md SS6), so it is joined here
    rather than in a staging step a model would have to perform.

    The verdict's own keys win; nothing here overwrites what the runner wrote.
    """
    rec = dict(verdict)
    layers = as_dict(verdict.get("layers"))
    # Badges read a verdict word; the runner's per-layer objects (checks,
    # reasons) stay available to render_failed_checks under layer_detail.
    rec["layer_detail"] = layers
    rec["layers"] = {name: as_dict(body).get("verdict", body)
                     if isinstance(body, dict) else body
                     for name, body in layers.items()}
    request = as_dict(_sibling(case_dir, "request.json"))
    if request.get("body") is not None:
        rec.setdefault("request", request["body"])
    expect = as_dict(_sibling(case_dir, "expect.json"))
    row = as_dict(as_dict(layers.get("routing")).get("row"))
    if row:
        rec.setdefault("route", {"expected": row.get("expected"),
                                 "actual": row.get("observed")})
    answer = _sibling(case_dir, "answer.txt")
    if answer is not None:
        rec.setdefault("answer", {"actual": answer})
    actual = _sibling(case_dir, "actual.json")
    if expect.get("result") is not None or actual is not None:
        rec.setdefault("result", {"expected": expect.get("result"),
                                  "actual": actual})
    trajectory = as_dict(_sibling(case_dir, "trajectory.json"))
    trajectory = as_dict(trajectory.get("trajectory")) or trajectory
    if isinstance(trajectory.get("tool_calls"), list):
        rec.setdefault("tool_calls", trajectory["tool_calls"])
    latency = verdict.get("latency_s")
    if isinstance(latency, (int, float)) and not isinstance(latency, bool):
        rec.setdefault("latency_ms", round(latency * 1000))
    return rec


def _load_dir(p, run_path, pattern):
    # Path.glob rejects some patterns outright — an absolute one
    # ("Non-relative patterns are unsupported") and an empty one
    # ("Unacceptable pattern") — by RAISING, which is exit 1 and a
    # traceback: the flag added to make this script easier to point at a
    # run was the one input that escaped the exit-2 JSON contract the rest
    # of it keeps. Name the fix rather than only the rejection — but the
    # fix differs by cause, and one shared arm described only the absolute
    # case, so `--glob ''` was told it was "not an absolute path".
    try:
        files = sorted(p.glob(pattern))
    except (ValueError, NotImplementedError) as e:
        fix = ("omit --glob for the default flat scan, or name a pattern"
               if not pattern else
               f"the pattern is relative to {run_path} "
               f"(e.g. 'cases/*/verdict.json'), not an absolute path")
        die(f"bad input: --glob {pattern!r}: {e} — {fix}")
    if not files:
        die(f"bad input: {run_path}: no files match {pattern!r} in this "
            f"directory — check the path, or pass --glob if the run keeps "
            f"records one level down (e.g. --glob 'cases/*/verdict.json')")
    records, skipped = [], []
    holdout_dropped = 0
    for f in files:
        doc = load_json(f, strict=False)
        # One is_record filter for both shapes. It used to guard only the
        # dict branch, so a run artifact that happens to be a top-level
        # JSON array — a manifest holding a list of stage entries — walked
        # straight past the check and rendered as synthesized-id "cases",
        # which is the exact failure RECORD_KEYS was added to stop, one
        # shape over. Normalizing to a list first leaves the policy in one
        # place, so the next tweak to it cannot be made in only one branch.
        if isinstance(doc, dict):
            doc = [doc]
        elif not isinstance(doc, list):
            die(f"bad input: {f}: expected a JSON object or array of "
                f"objects, got {type(doc).__name__}")
        found = [d for d in doc if is_record(d)]
        if f.name == "verdict.json" and (f.parent / "request.json").is_file():
            # THE HOLDOUT SEAL (annotation-ux.md): a sealed case's material is
            # never put on a review page. The runner marks it on the verdict,
            # so it is dropped here instead of by a hand-staged copy.
            sealed = [d for d in found if d.get("holdout") is True]
            holdout_dropped += len(sealed)
            found = [from_case_dir(d, f.parent) for d in found
                     if d.get("holdout") is not True]
            if sealed and not found:
                continue
        records.extend(found)
        if not found:
            skipped.append(f.name)
    skipped_names = ", ".join(sorted(skipped)[:4])
    if skipped and not records:
        # Every matched file was a run artifact, so the path is one level
        # off rather than empty. Naming the files and the likely fix beats
        # rendering them as cases (what this used to do) and beats a bare
        # "nothing to review" page (which would read as a finished run).
        die(f"bad input: {run_path}: {len(files)} file(s) matched "
            f"{pattern!r} but none is a case record "
            f"({skipped_names}) — if this is a run root, "
            f"the records are usually one level down: "
            f"--glob 'cases/*/verdict.json'")
    note = f"{len(files)} file(s) under {run_path}"
    if holdout_dropped:
        note += f" ({holdout_dropped} sealed holdout case(s) not shown)"
        print(f"note: {holdout_dropped} holdout case(s) left off the page -- "
              "the holdout is aggregate-only", file=sys.stderr)
    if skipped:
        # Dropped evidence is reported, never silent — same rule as
        # offshape_fields. stderr, not stdout: stdout may be the page.
        note += f" ({len(skipped)} non-record file(s) skipped)"
        print(f"note: skipped {len(skipped)} file(s) matching {pattern!r} "
              f"that carry no case-record field: {skipped_names}",
              file=sys.stderr)
    return records, note


def _load_file(p, run_path):
    doc = load_json(p, strict=False)
    if isinstance(doc, list):
        return as_dicts(doc), str(run_path)
    if isinstance(doc, dict):
        for key in ("cases", "results", "traces", "records"):
            if isinstance(doc.get(key), list):
                return as_dicts(doc[key]), str(run_path)
        # A single bare record is also accepted (a one-case run).
        return [doc], str(run_path)
    die(f"bad input: {run_path}: expected a JSON array or object, got "
        f"{type(doc).__name__}")


# --- rendering helpers ------------------------------------------------------

def esc(x):
    return html.escape(str(x), quote=True)


def as_dict(value):
    """A record field that should be an object, or {} when it isn't.

    Records are written by a run harness and are explicitly NOT schema-validated
    here (see the module docstring: "a record is evidence to render, not a strict
    schema to enforce"). A drifted field must therefore degrade to "nothing to
    render" — `x or {}` guards against null but not against a string, and
    `"answer": "some text"` used to take the whole page down with an
    AttributeError, which is neither the promised {"error": ...} contract nor a
    rendered page. Off-shape fields are surfaced by offshape_note, not hidden."""
    return value if isinstance(value, dict) else {}


def as_dicts(value):
    """The list-of-objects sibling of as_dict; non-object entries are dropped."""
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, dict)]


# The shape every rendered field is coerced to, and the only place that mapping
# is written down: the renderers coerce through as_dict/as_dicts and the
# off-shape flag is derived from the same coercers, so a field cannot be
# silently dropped by one and reported clean by the other.
RECORD_SHAPE = {"route": as_dict, "answer": as_dict, "result": as_dict,
                "layers": as_dict, "tool_calls": as_dicts,
                "stage_costs": as_dicts}


def offshape_fields(rec):
    """Fields present on the record but not in the shape this viewer renders —
    i.e. fields the coercion above had to change. Reported rather than silently
    skipped, on the same principle as the synthesized-id flag: a reviewer who
    cannot see that evidence was dropped reads the page as complete."""
    return sorted(f for f, coerce in RECORD_SHAPE.items()
                  if rec.get(f) is not None and coerce(rec[f]) != rec[f])


def as_text(value):
    """Render any JSON value as comparable/displayable text: strings as-is,
    everything else as canonical indented JSON so a diff is meaningful."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, indent=2, sort_keys=True)


def truncate(text, limit=TRUNCATE_AT):
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit} more chars]"


def word_diff_html(expected_text, actual_text):
    """Inline word-level diff (stdlib difflib), rendered as <del>/<ins> spans
    — the "expected-vs-actual diff at the leaf that matters" from §22,
    computed once here so the viewer's JS stays free of diff logic."""
    exp_words = expected_text.split()
    act_words = actual_text.split()
    sm = difflib.SequenceMatcher(a=exp_words, b=act_words, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out.append(esc(" ".join(exp_words[i1:i2])))
        elif tag == "delete":
            out.append(f'<del>{esc(" ".join(exp_words[i1:i2]))}</del>')
        elif tag == "insert":
            out.append(f'<ins>{esc(" ".join(act_words[j1:j2]))}</ins>')
        elif tag == "replace":
            out.append(f'<del>{esc(" ".join(exp_words[i1:i2]))}</del>')
            out.append(f'<ins>{esc(" ".join(act_words[j1:j2]))}</ins>')
    return " ".join(out)


VERDICT_CLASS = {"pass": "v-pass", "fail": "v-fail", "unscored": "v-unscored",
                 "unjudged": "v-unscored", "infra_error": "v-infra",
                 "infra_incomplete": "v-infra"}


def badge(text, cls=None):
    cls = cls or VERDICT_CLASS.get(str(text).lower(), "v-unscored")
    return f'<span class="badge {cls}">{esc(text)}</span>'


def render_span_tree(rec):
    parts = ['<div class="span-tree">']
    route = as_dict(rec.get("route"))
    tool_calls = as_dicts(rec.get("tool_calls"))
    if route:
        exp, act = route.get("expected"), route.get("actual")
        # Three states: a missing observation is NOT a match — showing it green
        # would read as a confirmed correct route when nothing was observed.
        if act is None:
            cls, act_text = "unknown", "n/a"
        elif exp is not None and exp != act:
            cls, act_text = "mismatch", esc(act)
        else:
            cls, act_text = "match", esc(act)
        parts.append(
            f'<div class="span span-route">'
            f'<div class="span-label">Router decision</div>'
            f'expected <b>{esc(exp)}</b> &rarr; actual '
            f'<b class="{cls}">{act_text}</b></div>')

    for tc in tool_calls:
        name = tc.get("name", "?")
        dur = tc.get("duration_ms")
        dur_txt = f'{dur} ms' if dur is not None else "duration n/a"
        args_txt = esc(truncate(as_text(tc.get("args")) or "(none)"))
        # An errored call has no result BY SPEC (the convention records one
        # only on success), so "(no result captured)" was the viewer telling
        # the reviewer that tracing was misconfigured on precisely the records
        # where the app had in fact failed.
        err = tc.get("error")
        result_txt = esc(truncate(
            as_text(tc.get("result"))
            or (f"(call errored: {err}; no result recorded)" if err
                else "(no result captured)")))
        err_html = (f'<span class="tool-error">errored: {esc(err)}</span>'
                    if err else "")
        parts.append(
            f'<div class="span span-tool{" span-error" if err else ""}">'
            f'<div class="span-label">Tool call: <code>{esc(name)}</code> '
            f'<span class="dur">{esc(dur_txt)}</span> {err_html}</div>'
            f'<div class="tool-block"><span class="tool-block-label">args'
            f'</span><pre>{args_txt}</pre></div>'
            f'<div class="tool-block"><span class="tool-block-label">result'
            f'</span><pre>{result_txt}</pre></div></div>')

    answer = as_dict(rec.get("answer"))
    actual_answer = answer.get("actual")
    if actual_answer is not None:
        parts.append(
            f'<div class="span span-answer">'
            f'<div class="span-label">Final answer</div>'
            f'<pre>{esc(truncate(as_text(actual_answer)))}</pre></div>')
    elif not route and not tool_calls:
        parts.append('<p class="empty">No trajectory data on this record — '
                     'answer-only case, or traces unavailable '
                     '(trace-less mode).</p>')
    parts.append("</div>")
    return "\n".join(parts)


def render_diff_section(rec):
    """The expected-vs-actual leaf: prefer `result` (execution accuracy —
    the check that catches a stub tool confidently returning the wrong
    number) over `answer`, since result is the more load-bearing ground
    truth when both are present."""
    for key, label in (("result", "Result (execution accuracy)"),
                       ("answer", "Answer")):
        block = as_dict(rec.get(key))
        exp_text, act_text = as_text(block.get("expected")), as_text(
            block.get("actual"))
        if exp_text is None and act_text is None:
            continue
        if exp_text is None:
            return (f'<p class="note">No expected {label.lower()} recorded '
                    f'— nothing to diff. Actual:</p>'
                    f'<pre>{esc(truncate(act_text or ""))}</pre>')
        if act_text is None:
            return (f'<p class="note">No actual {label.lower()} extracted '
                    f'(unscorable, not a mismatch). Expected:</p>'
                    f'<pre>{esc(truncate(exp_text))}</pre>')
        diff = word_diff_html(truncate(exp_text), truncate(act_text))
        return f'<div class="worddiff"><b>{esc(label)}:</b><br>{diff}</div>'
    return '<p class="empty">Nothing to diff on this record (no ' \
          '<code>result</code> or <code>answer</code> expected/actual ' \
          'pair present).</p>'


def render_request(rec):
    request = rec.get("request")
    if request is None:
        return ""
    return (f'<section><h3>Request sent</h3>'
            f'<pre>{esc(truncate(as_text(request) or ""))}</pre></section>')


def render_failed_checks(rec):
    """Why a layer failed, in the scorer's own words. "answer: fail" is a
    badge; `must_contain "$999.99": not found in answer` is what the reviewer
    came for, and it was only ever in verdict.json."""
    rows = []
    for name, body in as_dict(rec.get("layer_detail")).items():
        body = as_dict(body)
        if body.get("verdict") in (None, "pass", "n/a"):
            continue
        checks = [c for c in as_dicts(body.get("checks"))
                  if c.get("status") not in ("pass", None)]
        if not checks and body.get("reason"):
            checks = [{"status": body.get("verdict"),
                       "reason": body.get("reason")}]
        for check in checks:
            what = " ".join(as_text(check[k]) for k in ("check", "entry", "tool")
                            if check.get(k) is not None)
            rows.append(f'<tr><td>{esc(name)}</td>'
                        f'<td>{badge(check.get("status"))}</td>'
                        f'<td>{esc(what)}</td>'
                        f'<td>{esc(check.get("reason") or "")}</td></tr>')
    if not rows:
        return ""
    return ('<section><h3>Checks that did not pass</h3>'
            '<table class="cost-table"><thead><tr><th>Layer</th><th>Status'
            '</th><th>Check</th><th>Reason</th></tr></thead><tbody>'
            + "".join(rows) + '</tbody></table></section>')


def render_cost_table(rec):
    stages = as_dicts(rec.get("stage_costs"))
    if stages:
        rows = "".join(
            f'<tr><td>{esc(s.get("stage", "?"))}</td>'
            f'<td>{esc(s.get("latency_ms", "?"))} ms</td>'
            f'<td>${esc(s.get("cost_usd", "?"))}</td></tr>'
            for s in stages)
        return (f'<table class="cost-table"><thead><tr><th>Stage</th>'
                f'<th>Latency</th><th>Cost</th></tr></thead>'
                f'<tbody>{rows}</tbody></table>')
    lat, cost = rec.get("latency_ms"), rec.get("cost_usd")
    if lat is None and cost is None:
        return '<p class="empty">No latency/cost recorded on this record.' \
              '</p>'
    return (f'<p>Total latency: <b>{esc(lat) if lat is not None else "n/a"} '
            f'ms</b> &middot; cost: <b>$'
            f'{esc(cost) if cost is not None else "n/a"}</b></p>')


def render_record(rec, idx):
    case_id = rec.get("case_id") or rec.get("trace_id") or f"case-{idx}"
    # A record with neither id gets a positional one — flag it so the reviewer
    # knows the id is synthesized (the docstring promises this is reported, not
    # hidden), since a synthesized id won't correlate back to the dataset.
    synth = " <span class=\"synth\" title=\"id synthesized from position; " \
            "this record carried no case_id or trace_id\">(synthesized id)</span>" \
        if not (rec.get("case_id") or rec.get("trace_id")) else ""
    verdict = rec.get("verdict", "unscored")
    layers = as_dict(rec.get("layers"))
    layer_badges = " ".join(
        f'{esc(name)}: {badge(v)}' for name, v in layers.items())
    surface = rec.get("prompt_surface") or rec.get("surface")
    surface_html = (f'<div class="callout">Implicated surface: '
                    f'<b>{esc(surface)}</b></div>' if surface else "")
    offshape = offshape_fields(rec)
    offshape_html = (f'<div class="callout">Ignored off-shape field(s): '
                     f'<b>{esc(", ".join(offshape))}</b> — not the object/array '
                     f'shape this viewer renders. The raw value is still in the '
                     f'exported record below.</div>' if offshape else "")

    return f"""
<article class="detail">
  <header>
    <h2>{esc(case_id)}{synth}</h2>
    <div class="badges">{badge(verdict)} {layer_badges}</div>
  </header>
  {surface_html}
  {offshape_html}
  {render_request(rec)}
  {render_failed_checks(rec)}
  <section>
    <h3>Span tree</h3>
    {render_span_tree(rec)}
  </section>
  <section>
    <h3>Expected vs actual</h3>
    {render_diff_section(rec)}
  </section>
  <section>
    <h3>Per-stage latency / cost</h3>
    {render_cost_table(rec)}
  </section>
</article>"""


# --- page assembly ----------------------------------------------------------

CSS = """
:root { color-scheme: light dark; --pass:#1a7f37; --fail:#c22; --unscored:#8a6d00;
  --infra:#666; }
* { box-sizing: border-box; }
body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,
  Arial,sans-serif; color:#1a1a1a; background:#fff; }
@media (prefers-color-scheme: dark) {
  body { color:#e6e6e6; background:#14161a; }
  aside, .toolbar, .detail pre, .tool-block pre { background:#1c1f25 !important;
    border-color:#3a3f47 !important; }
  .row:hover { background:#22262d !important; }
  .row.selected { background:#28344a !important; }
  input, textarea, select { background:#1c1f25; color:#e6e6e6; border-color:#444; }
}
.layout { display:flex; height:100vh; }
aside { width:300px; flex:0 0 300px; border-right:1px solid #8885; background:#f7f7f8;
  display:flex; flex-direction:column; overflow:hidden; }
.toolbar { padding:.6em .7em; border-bottom:1px solid #8885; }
.toolbar select { width:100%; padding:.3em; }
.trace-list { overflow-y:auto; flex:1; }
.row { padding:.5em .7em; border-bottom:1px solid #8882; cursor:pointer; font-size:.9em; }
.row:hover { background:#eee; }
.row.selected { background:#dbe7ff; }
.row .rid { font-family:ui-monospace,monospace; font-size:.85em; opacity:.8; }
.dot { display:inline-block; width:.6em; height:.6em; border-radius:50%; margin-right:.35em; }
.dot.done { background:#1a7f37; } .dot.pending { background:transparent; border:1px solid #8888; }
main { flex:1; overflow-y:auto; padding:1.2em 1.6em; }
.badge { display:inline-block; padding:.1em .55em; border-radius:1em; font-size:.78em;
  font-weight:600; color:#fff; margin-right:.3em; }
.v-pass { background:var(--pass); } .v-fail { background:var(--fail); }
.v-unscored { background:var(--unscored); } .v-infra { background:var(--infra); }
.detail header { display:flex; align-items:center; gap:.8em; flex-wrap:wrap; }
.detail h2 { margin:.2em 0; font-family:ui-monospace,monospace; font-size:1.05em; }
.callout { background:#fff3cd; border:1px solid #e6c15c; padding:.5em .8em;
  border-radius:.4em; margin:.6em 0; }
@media (prefers-color-scheme: dark) { .callout { background:#3a3316; border-color:#7a6a2a; } }
.span-tree .span { border-left:3px solid #8886; margin:.5em 0; padding:.35em .8em; }
.span-route { border-color:#5b8dee; } .span-tool { border-color:#c98a2b; }
.span-answer { border-color:#1a7f37; }
.span-tool.span-error { border-color:var(--fail); }
.tool-error { color:var(--fail); font-weight:600; font-size:.85em; }
.span-label { font-weight:600; font-size:.85em; opacity:.85; margin-bottom:.2em; }
.tool-block { margin-top:.3em; } .tool-block-label { font-size:.75em; opacity:.7; }
pre { background:#8882; padding:.6em .8em; border-radius:.4em; overflow-x:auto;
  white-space:pre-wrap; word-break:break-word; margin:.2em 0; }
.match { color:var(--pass); } .mismatch { color:var(--fail); }
.worddiff { line-height:1.7; }
.worddiff del { background:#ffd6d6; text-decoration:line-through; color:#900; }
.worddiff ins { background:#d7f5d7; text-decoration:none; color:#063; }
@media (prefers-color-scheme: dark) {
  .worddiff del { background:#4a1f1f; color:#ff9d9d; }
  .worddiff ins { background:#1f3a24; color:#9de6a3; }
}
.cost-table { border-collapse:collapse; } .cost-table td, .cost-table th
  { border:1px solid #8886; padding:.3em .6em; }
.empty, .note { opacity:.75; font-style:italic; }
.annobar { position:sticky; bottom:0; background:#f0f2f5; border-top:1px solid #8885;
  padding:.7em 1em; margin-top:1.5em; }
@media (prefers-color-scheme: dark) { .annobar { background:#1a1d22; } }
.annobar .row1 { display:flex; gap:.6em; align-items:center; flex-wrap:wrap; }
.annobar button { padding:.4em .9em; border-radius:.4em; border:1px solid #8886;
  cursor:pointer; font-weight:600; }
button.pass-btn { background:#e5f5ea; } button.fail-btn { background:#fde8e8; }
@media (prefers-color-scheme: dark) {
  button.pass-btn { background:#173a22; color:#e6e6e6; }
  button.fail-btn { background:#3a1717; color:#e6e6e6; }
}
.annobar input[type=text], .annobar textarea { width:100%; padding:.4em; margin-top:.4em;
  border-radius:.3em; border:1px solid #8886; font-family:inherit; }
.annobar textarea { min-height:3em; resize:vertical; }
.taxonomy { border-top:1px solid #8885; padding:.6em .7em; font-size:.85em; max-height:30%;
  overflow-y:auto; }
.taxonomy h4 { margin:.2em 0 .4em; }
.taxonomy .cat-row { display:flex; justify-content:space-between; padding:.1em 0; }
.hotkeys { font-size:.78em; opacity:.75; padding:.3em .7em; border-top:1px solid #8885; }
.export { margin-top:1em; }
.export textarea { width:100%; height:6em; font-family:ui-monospace,monospace; font-size:.8em; }
.topbar { display:flex; justify-content:space-between; align-items:baseline;
  padding:.5em 1em; border-bottom:1px solid #8885; }
.topbar h1 { font-size:1.1em; margin:0; }
.stat { font-size:.85em; opacity:.8; }
"""

JS = r"""
(function () {
  var raw = JSON.parse(document.getElementById('trace-data').textContent);
  var initialAnnotations = JSON.parse(
      document.getElementById('annotation-data').textContent);
  var records = raw.records;

  // Latest-wins map (trace_id -> annotation) for badges/taxonomy; the
  // append-only log below is what actually gets exported, so re-reviewing a
  // trace never loses the earlier note.
  // Null-prototype because every key here is authored data — a trace_id, a
  // case_id, a reviewer-typed category. On a plain {} those inherit
  // Object.prototype, so `latest['constructor']` is truthy on an EMPTY map: the
  // trace renders a done dot, drops out of the "unannotated" filter, and the
  // reviewer never sees it — the same silently-skipped-work failure the
  // case_id join below exists to fix, reached by a different door. Object.keys
  // is unaffected; only `x.hasOwnProperty(k)` breaks, hence the .call form.
  var latest = Object.create(null);
  var log = initialAnnotations.slice();

  // Resolve a loaded annotation onto THIS page's records before keying it.
  // A record whose trace_id collided was suffixed to "abc#3", and that suffix
  // is the record's position under whatever --glob built the page — so an
  // annotation exported from an earlier page stops matching the moment the
  // record order shifts (one case added ahead of it is enough). It then reads
  // as unannotated: no dot, back into the "unannotated only" filter, out of
  // the taxonomy counts. annotation-ux.md already names the fix on the write
  // side ("case_id is never suffixed — join on those, not on the #N id"); the
  // export honored it and the load path did not. case_id is only usable as a
  // key where it is unique on this page, and old lines may predate it, hence
  // the fallback to trace_id rather than a straight re-key.
  var byCase = Object.create(null);
  records.forEach(function (r) {
    byCase[r.case_id] =
        Object.prototype.hasOwnProperty.call(byCase, r.case_id) ? null : r;
  });
  function annotationKey(a) {
    var r = a.case_id ? byCase[a.case_id] : null;
    return r ? r.trace_id : a.trace_id;
  }
  log.forEach(function (a) { latest[annotationKey(a)] = a; });

  var reviewer = localStorage.getItem('evalup_reviewer') || '';
  var filterMode = 'all';
  var cur = 0;

  var listEl = document.getElementById('trace-list');
  var mainEl = document.getElementById('main');
  var filterEl = document.getElementById('filter');
  var taxEl = document.getElementById('taxonomy-body');
  var statEl = document.getElementById('stats');
  var reviewerEl = document.getElementById('reviewer');
  var exportEl = document.getElementById('export-text');
  reviewerEl.value = reviewer;

  // Memoized: one annotation keystroke used to re-run this filter three times
  // over every record (save -> render -> renderList), each pass allocating a
  // fresh array, on the hottest interaction in the tool. The only inputs are
  // filterMode and latest, so the cache is dropped exactly where those change.
  var visCache = null;
  function invalidateVisible() { visCache = null; }
  function visible() {
    if (visCache) return visCache;
    visCache = records.filter(function (r) {
      if (filterMode === 'all') return true;
      if (filterMode === 'unannotated') return !latest[r.trace_id];
      if (filterMode === 'fail') return r.verdict === 'fail';
      if (filterMode === 'pass') return r.verdict === 'pass';
      return true;
    });
    return visCache;
  }

  function renderList() {
    var vis = visible();
    listEl.innerHTML = '';
    vis.forEach(function (r, i) {
      var done = !!latest[r.trace_id];
      var div = document.createElement('div');
      div.className = 'row' + (i === cur ? ' selected' : '');
      div.innerHTML = '<span class="dot ' + (done ? 'done' : 'pending') +
          '"></span><b>' + escapeHtml(r.case_id) + '</b><br>' +
          '<span class="rid">' + escapeHtml(r.trace_id || '') + '</span> ' +
          '<span class="badge ' + verdictClass(r.verdict) + '">' +
          escapeHtml(r.verdict || 'unscored') + '</span>';
      div.addEventListener('click', function () { cur = i; render(); });
      listEl.appendChild(div);
    });
    statEl.textContent = vis.length + ' / ' + records.length + ' traces shown, ' +
        Object.keys(latest).length + ' annotated';
  }

  function renderTaxonomy() {
    // Keyed by free text the reviewer typed, the least trustworthy key in the
    // file: on a plain {} a category of "toString" made (counts[c] || 0) + 1
    // concatenate onto a native function and sort into the table as a string.
    var counts = Object.create(null);
    Object.keys(latest).forEach(function (tid) {
      var c = (latest[tid].category || '').trim();
      if (!c) return;
      counts[c] = (counts[c] || 0) + 1;
    });
    var cats = Object.keys(counts).sort(function (a, b) {
      return counts[b] - counts[a];
    });
    taxEl.innerHTML = cats.length ? '' :
        '<span class="empty">No categories yet (open-coding stage — ' +
        'write free-text critiques; categories emerge from clustering ' +
        'them, see annotation-ux.md).</span>';
    cats.forEach(function (c) {
      var row = document.createElement('div');
      row.className = 'cat-row';
      row.innerHTML = '<span>' + escapeHtml(c) + '</span><b>' + counts[c] + '</b>';
      taxEl.appendChild(row);
    });
    var dl = document.getElementById('category-options');
    dl.innerHTML = cats.map(function (c) {
      return '<option value="' + escapeHtml(c) + '">';
    }).join('');
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;',
               "'": '&#39;' }[c];
    });
  }

  // Verdict is untrusted (not schema-enforced), so its class fragment must come
  // from a fixed whitelist, never string-concatenated raw into an attribute.
  var VERDICT_CLASS = { pass: 'v-pass', fail: 'v-fail', unscored: 'v-unscored',
                        infra_error: 'v-unscored', infra_incomplete: 'v-unscored' };
  function verdictClass(v) { return VERDICT_CLASS[v] || 'v-unscored'; }

  function render() {
    var vis = visible();
    if (cur >= vis.length) cur = Math.max(0, vis.length - 1);
    renderList();
    renderTaxonomy();
    if (!vis.length) {
      mainEl.innerHTML = '<p class="empty">No traces match this filter.</p>';
      return;
    }
    var r = vis[cur];
    mainEl.innerHTML = r.detail_html + buildAnnobar(r);
    var existing = latest[r.trace_id];
    document.getElementById('critique').value = existing ? existing.critique || '' : '';
    document.getElementById('category').value = existing ? existing.category || '' : '';
    wireAnnobar(r);
  }

  function buildAnnobar(r) {
    var existing = latest[r.trace_id];
    var labelNote = existing ?
        ('<span class="stat">current label: <b>' + escapeHtml(existing.label) +
         '</b> by ' + escapeHtml(existing.reviewer || '?') + '</span>') : '';
    return '' +
      '<div class="annobar">' +
      '  <div class="row1">' +
      '    <button class="pass-btn" id="btn-pass" title="hotkey: p">Pass (p)</button>' +
      '    <button class="fail-btn" id="btn-fail" title="hotkey: f">Fail (f)</button>' +
      '    <input type="text" id="category" list="category-options" placeholder="category (leave blank while open-coding)" style="max-width:260px">' +
      '    ' + labelNote +
      '  </div>' +
      '  <textarea id="critique" placeholder="Free-text critique (why pass/fail, what was wrong) — hotkey: c to focus"></textarea>' +
      '</div>';
  }

  function wireAnnobar(r) {
    document.getElementById('btn-pass').addEventListener('click', function () { save(r, 'pass'); });
    document.getElementById('btn-fail').addEventListener('click', function () { save(r, 'fail'); });
  }

  function save(r, label) {
    var critique = document.getElementById('critique').value;
    var category = document.getElementById('category').value;
    reviewer = reviewerEl.value;
    localStorage.setItem('evalup_reviewer', reviewer);
    var entry = { trace_id: r.trace_id, case_id: r.case_id, label: label,
      category: category, critique: critique, reviewer: reviewer,
      ts: new Date().toISOString() };
    // A disambiguated id is this VIEWER's ("abc#3", suffixed because two
    // records shared "abc"), and the export is appended to the user's tracked
    // annotations JSONL — so without this the suffix leaks upstream as an id
    // that exists in no dataset, and the "#3" is not even stable, since it is
    // the record's position under whatever --glob produced this page. Carry
    // the real id alongside it so the annotation stays traceable to the run;
    // case_id is never suffixed and remains the reliable join key.
    if (r.duplicate_trace_id) { entry.duplicate_trace_id = r.duplicate_trace_id; }
    latest[r.trace_id] = entry;
    log.push(entry);
    invalidateVisible();  // the 'unannotated' filter reads latest
    // Appended, not re-serialized: the export box holds the whole loaded
    // annotations file, so rebuilding it from `log` on every save (and, before,
    // on every navigation) grew with the session for one new line of output.
    exportEl.value += (exportEl.value ? '\n' : '') + JSON.stringify(entry);
    // Under a filter that this annotation just excluded the record FROM
    // ("unannotated only"), the list has already shifted: cur now points at the
    // next trace, so advancing again steps over it. That walked every other
    // trace on the way down and picked up the missed ones in reverse off the
    // end-of-list clamp (A,C,E,G,H,F,D,B for eight traces) — nothing was lost,
    // but a time-boxed session ended up with a strided sample instead of a
    // prefix, which is a poor base for error analysis. Only advance when the
    // record is still there.
    if (visible().indexOf(r) === -1) { render(); } else { goNext(); }
  }

  // Seeds the box from the annotations file loaded into the page; every later
  // write appends its one line in save().
  function seedExport() {
    exportEl.value = log.map(function (e) { return JSON.stringify(e); }).join('\n');
  }

  function goNext(onlyUnannotated) {
    var vis = visible();
    if (!vis.length) return;
    if (onlyUnannotated) {
      for (var k = 1; k <= vis.length; k++) {
        var idx = (cur + k) % vis.length;
        if (!latest[vis[idx].trace_id]) { cur = idx; render(); return; }
      }
      return;
    }
    cur = Math.min(cur + 1, vis.length - 1);
    render();
  }
  function goPrev() {
    cur = Math.max(cur - 1, 0);
    render();
  }

  filterEl.addEventListener('change', function () {
    filterMode = filterEl.value; invalidateVisible(); cur = 0; render();
  });

  document.getElementById('copy-btn').addEventListener('click', function () {
    exportEl.select();
    try { navigator.clipboard.writeText(exportEl.value); }
    catch (e) { document.execCommand('copy'); }
  });
  document.getElementById('download-btn').addEventListener('click', function () {
    var blob = new Blob([exportEl.value + '\n'], { type: 'application/x-jsonlines' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'annotations.jsonl';
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
  });

  document.addEventListener('keydown', function (e) {
    var tag = (e.target.tagName || '').toLowerCase();
    var typing = tag === 'input' || tag === 'textarea';
    if (typing) {
      if (e.key === 'Escape') e.target.blur();
      return;
    }
    if (e.key === 'j' || e.key === 'ArrowDown') { goNext(); }
    else if (e.key === 'k' || e.key === 'ArrowUp') { goPrev(); }
    else if (e.key === 'n') { goNext(true); }
    else if (e.key === 'p') { var vr = visible()[cur]; if (vr) save(vr, 'pass'); }
    else if (e.key === 'f') { var vr2 = visible()[cur]; if (vr2) save(vr2, 'fail'); }
    else if (e.key === 'c' || e.key === '/') {
      var el = document.getElementById('critique');
      if (el) { el.focus(); e.preventDefault(); }
    }
    else if (e.key === 't') {
      var el2 = document.getElementById('category');
      if (el2) { el2.focus(); e.preventDefault(); }
    }
  });

  seedExport();
  render();
})();
"""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
<div class="topbar">
  <h1>{title}</h1>
  <span class="stat" id="stats"></span>
</div>
<div class="layout">
  <aside>
    <div class="toolbar">
      <select id="filter">
        <option value="all">All traces</option>
        <option value="unannotated">Unannotated only</option>
        <option value="fail">Verdict: fail</option>
        <option value="pass">Verdict: pass</option>
      </select>
    </div>
    <div class="trace-list" id="trace-list"></div>
    <div class="taxonomy">
      <h4>Taxonomy (axial coding)</h4>
      <div id="taxonomy-body"></div>
    </div>
    <div class="hotkeys">
      j/k or &darr;/&uarr; next/prev &middot; n next unannotated &middot;
      p pass &middot; f fail &middot; c critique &middot; t category<br>
      reviewer: <input type="text" id="reviewer" placeholder="your name"
        style="width:8em">
    </div>
  </aside>
  <main id="main"></main>
</div>
<div class="export">
  <div style="padding:0 1em">
    <b>Export annotations</b> — copy/download and append to your tracked
    annotations JSONL (see annotation-ux.md: this viewer does not write to
    disk itself).
    <button id="copy-btn">Copy</button>
    <button id="download-btn">Download .jsonl</button>
    <textarea id="export-text" readonly></textarea>
  </div>
</div>
<datalist id="category-options"></datalist>
<script type="application/json" id="trace-data">{trace_data}</script>
<script type="application/json" id="annotation-data">{annotation_data}</script>
<script>{js}</script>
</body>
</html>
"""

EMPTY_PAGE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>{title}</title>
<style>body{{font-family:-apple-system,sans-serif;max-width:640px;
margin:4em auto;text-align:center;color:#555}}</style></head>
<body>
<h1>Nothing to review</h1>
<p>{source} contained zero case records. That is a valid outcome (e.g. a
fresh run before any case executed) — this is not an error. Re-run
build_review_viewer.py once the run has produced cases.</p>
</body></html>
"""


# Escaping only "</" is the well-known half-measure. HTML has a SECOND way into
# trouble: "<!--" puts the tokenizer in script data escaped state and a following
# "<script" in script data double escaped state, where "</script>" no longer
# closes the element (html.spec.whatwg.org/multipage/parsing.html). A record
# whose answer quotes "<!--<script>" therefore swallowed the annotation-data
# block AND the viewer's own <script>, leaving an inert blank page — verified
# with a spec-compliant parser, which saw no annotation-data element at all.
# Both routes start with "<", so escape the character itself. This is exactly
# Django's _json_script_escapes, for the same <script type="application/json">
# + textContent + JSON.parse pattern this page uses.
_JSON_SCRIPT_TABLE = str.maketrans({"<": "\\u003C", ">": "\\u003E",
                                    "&": "\\u0026"})


def _no_script_break(s):
    """Neutralize every character that can steer the HTML tokenizer out of the
    embedded JSON <script> block. The escapes are JSON string escapes, so the
    block still parses back to the original text."""
    # One pass, not one per character: the input is the whole run serialized to
    # JSON (every record's pre-rendered detail_html included), so three chained
    # str.replace calls meant three full scans and three full-size copies of a
    # multi-megabyte string on the hottest line of the build.
    return s.translate(_JSON_SCRIPT_TABLE)


def _finite(node):
    """The same tree with every non-finite float spelled as a string.

    Python's json emits NaN/Infinity bare and the page's JSON.parse throws on
    them, so ONE such number anywhere in a run -- an expectation, or an app
    response, which no input check of ours can police -- aborted the script and
    rendered an empty page (2026-09 audit). The viewer shows what the run
    holds; "NaN" as text is the faithful rendering of a value that has no JSON
    one. allow_nan=False below turns any path this misses into a build error
    instead of a blank page."""
    if isinstance(node, float) and not math.isfinite(node):
        return str(node)
    if isinstance(node, dict):
        return {key: _finite(value) for key, value in node.items()}
    if isinstance(node, (list, tuple)):
        return [_finite(value) for value in node]
    return node


def build_page(records, annotations, title, source):
    if not records:
        return EMPTY_PAGE.format(title=esc(title), source=esc(source))
    # trace_id is the viewer's primary key: the JS latest[] map, the "already
    # annotated" dot, the "unannotated only" filter and every exported
    # annotation are all keyed by it. Two records sharing one is therefore the
    # same defect the backfill below was written to fix, one level up —
    # annotating either marks both done and hides the other under the filter.
    # Records legitimately collide (a case re-run in the same directory, a
    # --glob sweeping two run dirs, or two records that both lack a trace_id
    # and share a case_id), so disambiguate rather than reject, and flag it in
    # the page for the same reason a synthesized id is flagged: a suffixed id
    # no longer correlates back to the dataset cleanly.
    taken = set()
    for i, r in enumerate(records):
        # Render BEFORE backfilling the ids. render_record flags a synthesized
        # id by checking that neither case_id nor trace_id was present, and
        # backfilling first made that check permanently false — every
        # positional id then rendered as if it were a real one, which is the
        # opposite of the promise in this module's docstring. A synthesized id
        # does not correlate back to the dataset, so hiding it sends the
        # reviewer looking for a case that does not exist.
        r["detail_html"] = render_record(r, i)
        # Not setdefault: it tests key EXISTENCE, not usefulness, so an
        # explicit "trace_id": null — what a harness writes in the trace-less
        # mode render_span_tree supports — survived it untouched. Every such
        # record then collided on the single JS key "null" in the viewer's
        # latest[] map, so annotating one marked them ALL annotated, hid the
        # rest under the "unannotated only" filter, and printed "null" in the
        # sidebar while the detail pane correctly showed the synthesized id.
        if not r.get("case_id"):
            r["case_id"] = r.get("trace_id") or f"case-{i}"
        if not r.get("trace_id"):
            r["trace_id"] = r["case_id"]
        if r["trace_id"] in taken:
            original = r["trace_id"]
            r["trace_id"] = f"{original}#{i}"
            r["duplicate_trace_id"] = original
            # Appended rather than rendered inside render_record: the id is not
            # known to be a duplicate until the ids before it have been seen,
            # which is after that record was rendered.
            r["detail_html"] += (
                f'<div class="callout">Duplicate id <b>{esc(original)}</b> — '
                f'another record in this run carries it too. Annotations are '
                f'keyed per record, so this one was disambiguated to '
                f'<b>{esc(r["trace_id"])}</b>; that suffix is this viewer\'s, '
                f'not the dataset\'s.</div>')
        taken.add(r["trace_id"])
    trace_data = _no_script_break(
        json.dumps(_finite({"records": records}), allow_nan=False))
    annotation_data = _no_script_break(
        json.dumps(_finite(annotations), allow_nan=False))
    return PAGE.format(title=esc(title), css=CSS, js=JS,
                       trace_data=trace_data,
                       annotation_data=annotation_data)


def main():
    ap = argparse.ArgumentParser(
        description="Build a self-contained static HTML trace/annotation "
                    "viewer from a run's case records and (optionally) an "
                    "existing JSONL annotations file. See "
                    "skills/analyze/references/annotation-ux.md.")
    add_version_flag(ap)
    ap.add_argument("run_path",
                    help="a run directory of *.json case records, or a "
                         "single JSON file (array, {cases:[...]}, or one "
                         "record)")
    ap.add_argument("-a", "--annotations", default=None,
                    help="existing annotations JSONL "
                         "(trace_id/label/category/critique/reviewer/ts); "
                         "omit to start with none")
    ap.add_argument("-o", "--output", default=None,
                    help="destination .html path (default: print to stdout)")
    ap.add_argument("--title", default=None,
                    help="page title (default: derived from run_path)")
    ap.add_argument("--glob", default=None, dest="glob_pattern",
                    help="when run_path is a directory, which files hold case "
                         "records. Default: 'cases/*/verdict.json' for a "
                         "run_cases.py run directory (one holding "
                         "manifest.yaml and cases/), otherwise a flat "
                         "'*.json' scan")
    a = ap.parse_args()

    records, source = load_records(a.run_path, a.glob_pattern)
    annotations = []
    if a.annotations:
        annotations = load_jsonl(a.annotations, required_keys=("trace_id",))

    title = a.title or f"Review — {pathlib.Path(a.run_path).name}"
    page = build_page(records, annotations, title, source)

    write_output(a.output, page)


if __name__ == "__main__":
    main()
