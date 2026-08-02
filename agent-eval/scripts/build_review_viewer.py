#!/usr/bin/env python3
"""Generate a self-contained static HTML trace/annotation viewer.

Implements the annotation UX in EVAL-DESIGN-RECOMMENDATION.md §22: everything
on one screen (span tree, expected-vs-actual diff, per-stage latency/cost,
the implicated prompt/surface), one-keystroke binary pass/fail with a
free-text critique box beside it, hotkey navigation, and a live
taxonomy (category -> count) sidebar for axial coding. See
skills/analyze/references/annotation-ux.md for the full workflow this
viewer supports and the write-path decision (documented manual-edit flow,
not a localhost sidecar — see that doc's "Why manual-edit, not a sidecar"
section).

INPUT (run traces): a JSON file containing a list of case records, a JSON
file shaped {"cases": [...]} / {"results": [...]}, or a directory of `*.json`
files (one record per file — e.g. runs/<run-id>/cases/*.json). Every field on
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

INPUT (annotations, optional): a JSONL file, one line per annotation event —
`{"trace_id": ..., "label": "pass"|"fail", "category": "...", "critique":
"...", "reviewer": "...", "ts": "..."}` (see case-format's sibling doc,
annotation-ux.md, for the full field contract). Loaded read-only to seed the
viewer's taxonomy counts and per-trace status; the viewer never writes this
file itself (see the docstring above) — it renders an export box the
reviewer copies/downloads and appends to this same file by hand.

Usage: build_review_viewer.py <run-path> [-a annotations.jsonl] [-o out.html]
Error contract: on unreadable/malformed input print {"error": "..."} to
stdout and exit 2; otherwise exit 0. An input that parses fine but contains
zero case records is NOT an error (a fresh run can legitimately have nothing
to review yet) — the viewer renders a "nothing to review" page instead of
guessing at content that was never there.
"""
import argparse
import difflib
import html
import json
import pathlib
import sys

from _common import add_version_flag, die, load_jsonl

TRUNCATE_AT = 4000  # tool-result/answer text beyond this is elided in the
                    # span tree — the raw record is still exported in full
                    # via the embedded JSON, only the rendered HTML is capped
                    # so one giant blob can't make the page unusable.


# --- loading --------------------------------------------------------------

def _read_json_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except OSError as e:
        die(f"bad input: {e}")
    except json.JSONDecodeError as e:
        die(f"bad input: {path}: {e}")


def load_records(run_path):
    """Return (records, source_note). Distinguishes a structurally bad input
    (wrong path, unreadable JSON, a directory with no *.json files at all —
    likely a typo) from a valid input that simply contains zero records
    (a legitimately empty run) — only the former is an error."""
    p = pathlib.Path(run_path)
    if not p.exists():
        die(f"bad input: {run_path}: no such file or directory")

    if p.is_dir():
        files = sorted(p.glob("*.json"))
        if not files:
            die(f"bad input: {run_path}: no *.json files found in this "
                f"directory — check the path")
        records = []
        for f in files:
            doc = _read_json_file(f)
            if isinstance(doc, dict):
                records.append(doc)
            elif isinstance(doc, list):
                records.extend(x for x in doc if isinstance(x, dict))
            else:
                die(f"bad input: {f}: expected a JSON object or array of "
                    f"objects, got {type(doc).__name__}")
        return records, f"{len(files)} file(s) under {run_path}"

    doc = _read_json_file(p)
    if isinstance(doc, list):
        return [x for x in doc if isinstance(x, dict)], str(run_path)
    if isinstance(doc, dict):
        for key in ("cases", "results", "traces", "records"):
            if isinstance(doc.get(key), list):
                return ([x for x in doc[key] if isinstance(x, dict)],
                        str(run_path))
        # A single bare record is also accepted (a one-case run).
        return [doc], str(run_path)
    die(f"bad input: {run_path}: expected a JSON array or object, got "
        f"{type(doc).__name__}")


# --- rendering helpers ------------------------------------------------------

def esc(x):
    return html.escape(str(x), quote=True)


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
    route = rec.get("route") or {}
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

    for tc in (rec.get("tool_calls") or []):
        name = tc.get("name", "?")
        dur = tc.get("duration_ms")
        dur_txt = f'{dur} ms' if dur is not None else "duration n/a"
        args_txt = esc(truncate(as_text(tc.get("args")) or "(none)"))
        result_txt = esc(truncate(as_text(tc.get("result")) or "(no result "
                                  "captured)"))
        parts.append(
            f'<div class="span span-tool">'
            f'<div class="span-label">Tool call: <code>{esc(name)}</code> '
            f'<span class="dur">{esc(dur_txt)}</span></div>'
            f'<div class="tool-block"><span class="tool-block-label">args'
            f'</span><pre>{args_txt}</pre></div>'
            f'<div class="tool-block"><span class="tool-block-label">result'
            f'</span><pre>{result_txt}</pre></div></div>')

    answer = rec.get("answer") or {}
    actual_answer = answer.get("actual")
    if actual_answer is not None:
        parts.append(
            f'<div class="span span-answer">'
            f'<div class="span-label">Final answer</div>'
            f'<pre>{esc(truncate(as_text(actual_answer)))}</pre></div>')
    elif not route and not rec.get("tool_calls"):
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
        block = rec.get(key) or {}
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


def render_cost_table(rec):
    stages = rec.get("stage_costs")
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
    layers = rec.get("layers") or {}
    layer_badges = " ".join(
        f'{esc(name)}: {badge(v)}' for name, v in layers.items())
    surface = rec.get("prompt_surface") or rec.get("surface")
    surface_html = (f'<div class="callout">Implicated surface: '
                    f'<b>{esc(surface)}</b></div>' if surface else "")

    return f"""
<article class="detail">
  <header>
    <h2>{esc(case_id)}{synth}</h2>
    <div class="badges">{badge(verdict)} {layer_badges}</div>
  </header>
  {surface_html}
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
  var latest = {};
  var log = initialAnnotations.slice();
  log.forEach(function (a) { latest[a.trace_id] = a; });

  var reviewer = localStorage.getItem('agent_eval_reviewer') || '';
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

  function visible() {
    return records.filter(function (r) {
      if (filterMode === 'all') return true;
      if (filterMode === 'unannotated') return !latest[r.trace_id];
      if (filterMode === 'fail') return r.verdict === 'fail';
      if (filterMode === 'pass') return r.verdict === 'pass';
      return true;
    });
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
    var counts = {};
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
      updateExport();
      return;
    }
    var r = vis[cur];
    mainEl.innerHTML = r.detail_html + buildAnnobar(r);
    var existing = latest[r.trace_id];
    document.getElementById('critique').value = existing ? existing.critique || '' : '';
    document.getElementById('category').value = existing ? existing.category || '' : '';
    wireAnnobar(r);
    updateExport();
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
    localStorage.setItem('agent_eval_reviewer', reviewer);
    var entry = { trace_id: r.trace_id, case_id: r.case_id, label: label,
      category: category, critique: critique, reviewer: reviewer,
      ts: new Date().toISOString() };
    latest[r.trace_id] = entry;
    log.push(entry);
    updateExport();
    goNext();
  }

  function updateExport() {
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
    filterMode = filterEl.value; cur = 0; render();
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


def _no_script_break(s):
    """A trace's tool result could legitimately contain the literal text
    "</script>" (e.g. an app answer that quotes HTML) — escape the slash so
    the embedded JSON <script> block can't be terminated early by data."""
    return s.replace("</", "<\\/")


def build_page(records, annotations, title, source):
    if not records:
        return EMPTY_PAGE.format(title=esc(title), source=esc(source))
    for i, r in enumerate(records):
        r.setdefault("case_id", r.get("trace_id") or f"case-{i}")
        r.setdefault("trace_id", r.get("case_id"))
        r["detail_html"] = render_record(r, i)
    trace_data = _no_script_break(json.dumps({"records": records}))
    annotation_data = _no_script_break(json.dumps(annotations))
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
    a = ap.parse_args()

    records, source = load_records(a.run_path)
    annotations = []
    if a.annotations:
        annotations = load_jsonl(a.annotations, required_keys=("trace_id",))

    title = a.title or f"Review — {pathlib.Path(a.run_path).name}"
    page = build_page(records, annotations, title, source)

    if a.output:
        try:
            with open(a.output, "w", encoding="utf-8") as f:
                f.write(page)
        except OSError as e:
            die(f"cannot write output: {e}")
    else:
        sys.stdout.write(page)


if __name__ == "__main__":
    main()
