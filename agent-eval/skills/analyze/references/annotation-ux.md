# Annotation UX — Open Coding, Axial Coding, Calibration, the Flywheel

Why this exists: the operating-economics finding (EVAL-DESIGN-RECOMMENDATION
§14) is that **the error-analysis session, not the harness, is the adoption
hook** — teams that spend 30 minutes reading real traces get a ranked failure
taxonomy before any judge or CI exists; teams that build infra first get a
pipeline nobody trusts. What makes that session ~10x faster (Hamel Husain):
everything on one screen, one-keystroke binary pass/fail with a free-text
critique box right beside it, hotkey navigation, and domain-specific
rendering instead of raw JSON. `build_review_viewer.py` is that screen. This
doc is the workflow it exists to serve.

## The tool

`${CLAUDE_PLUGIN_ROOT}/scripts/build_review_viewer.py <run-path> [-a
annotations.jsonl] [-o out.html]` renders a run's case records (a directory
of `runs/<run-id>/**/*.json`, or a single JSON file) plus an optional
existing annotations JSONL into one self-contained static HTML file: a trace
list + live taxonomy sidebar on the left, the selected trace's span tree
(router decision → tool call+args → result → final answer), expected-vs-actual
word diff, and per-stage latency/cost on the right, and a pass/fail +
critique bar pinned at the bottom. See the script's own docstring for the
exact record shape it reads.

## Storage format — plain JSONL, one line per annotation event

```jsonc
{"trace_id": "4bf92f...", "case_id": "billing-happy-3f9a2c1d",
 "label": "fail", "category": "hallucinated-count",
 "critique": "Answer says 0 expiring licences; get_licences returned [] \
(stub tool) but the true count from the seed fixture is 3. Model should \
have flagged the empty result as suspicious rather than reporting it as 0.",
 "reviewer": "priya", "ts": "2026-08-02T14:03:00Z"}
```

JSONL over YAML/a database: one changed or added annotation is one changed
line, so a PR diff shows exactly what changed — no re-indentation cascade, no
merge conflict on an unrelated row. Re-reviewing a trace **appends** a new
line rather than overwriting; the viewer treats the JSONL as an append-only
log and shows only the latest entry per `trace_id` for badges/counts, so
annotation history is never destroyed by a second look.

Store it under the state location, e.g. `.agent-eval/annotations/<name>.jsonl`
— pick a name per review pass (`calibration-2026-08.jsonl`,
`traffic-mining-w32.jsonl`) rather than one growing file, so a given pass's
scope is legible from its filename, the same discipline as named dataset
splits.

## Why manual-edit, not a sidecar

The recommendation (§22) offers two write paths for a git-native, file-based
plugin: a tiny localhost sidecar process, or a documented manual-edit flow.
This build takes **manual-edit**: the viewer is a single static HTML file
that opens with no server, no port, no process to manage, and no dependency
beyond a browser — matching the plugin's stdlib-only, zero-install posture
(`help/SKILL.md`: "Reviewer... never needs Claude Code: datasets are plain
YAML, reports are HTML/markdown"). Annotations made in a session live in the
page's memory (and the reviewer name in `localStorage` only, for convenience
across reloads); an **Export** panel at the bottom shows the full JSONL text
(session annotations appended to whatever was loaded) with **Copy** and
**Download `.jsonl`** buttons. The reviewer pastes/replaces that text into
the tracked annotations file themselves — one deliberate save point per
session, and the file that lands in git is exactly what the reviewer saw.

The tradeoff: annotations are lost if the tab closes before exporting (no
autosave), and two reviewers can't see each other's labels live. Both are
acceptable for a solo/small-team file-based workflow; if a team outgrows this
(concurrent reviewers, needs autosave), the natural upgrade is the sidecar
option — a `python3 -m http.server`-adjacent tiny script that appends POSTed
annotations straight to the JSONL — deliberately not built yet because
nothing in the field-tested workflow has needed it.

## The workflow: open coding → axial coding

**1. Open coding (no categories yet).** Show the full trace + a single
free-text box + next-hotkey. The `category` field stays blank. Force
qualitative observation before any taxonomy exists — naming categories too
early anchors the reviewer on the first few failure modes seen and hides the
rest. Cadence: review **at least 100 traces** before concluding you've seen
the failure landscape; **stop a sitting** when ~20 consecutive traces add
nothing new to what you've been writing.

**2. Axial coding (cluster into a taxonomy).** Re-read the free-text
critiques, cluster them (Claude can propose a first clustering — it is
reading the same critiques a human would), and assign each trace one
`category` going forward. The sidebar's taxonomy panel counts categories
live as you (re-)annotate, so a session doing axial coding sees its own
clustering forming in real time — the same feedback loop that makes a spot
check surface "3 issues = 60% of problems" (the NurtureBoss pattern
`EVAL-DESIGN-RECOMMENDATION.md` §14 cites) instead of 40 one-off notes nobody
can act on.

Order by frequency × severity, same discipline as `analyze --cluster`'s
failure-cluster ordering — this viewer's taxonomy sidebar and that skill
command's cluster output are two views of the same idea (one interactive/
exploratory, one a generated report) and should agree.

## Theoretical-saturation stopping rule

Reused from qualitative-research method because it is the honest answer to
"how many do I have to look at": **review ≥100 traces before declaring the
taxonomy discovered**, and within any sitting, **stop once ~20 consecutive
traces produce no category you haven't already written down.** Fewer than
that and you're extrapolating from a handful of anecdotes; more than that
with no new categories is diminishing returns better spent elsewhere
(labeling for calibration, writing regression cases from what you found).
This rule appears three times in this plugin on purpose — `analyze
--cluster`'s cadence guidance, `analyze --label`'s calibration cadence, and
here — because it is one rule, not three: it is how you know a sample of
traces has told you what it has to tell you.

## Calibration UX

When the judge has a provisional rubric, use the same viewer (or `analyze
--label`'s one-at-a-time flow, which walks the identical steps
conversationally) to run the calibration pass: for 25–50 examples, the
judge's provisional per-dimension verdict sits beside a blank human column;
the reviewer marks agree / disagree / edit-rubric, with a critique on every
disagreement. Track **TPR and TNR live, never raw accuracy** — a 90%-pass
app makes an always-pass judge look 90% "accurate" while catching 0% of real
failures, exactly the trap `docs/rubric-format.md`'s calibration workflow and
`analyze --label` both guard against. **EvalGen's rule applies here too:**
the rubric-edit control sits right next to the grading action, because
editing the rubric mid-session is not a distraction from labeling — criteria
drift is what grading real cases discovers, and pretending the rubric was
final before you'd seen 30 examples is how a rubric ships broken.

## The flywheel — critiques become the next asset, every time

A written critique is not disposable feedback; it is raw material for four
downstream assets, and none of them happen automatically — someone has to
spend the critique:

1. **Judge few-shots.** A critique detailed enough to explain *why* a verdict
   was right or wrong, spliced as an `<input><output><critique>` example into
   the judge prompt, is the single measured lever for judge-human agreement
   (+15–20pp, §22). Write critiques as if they'll be read by the judge, not
   just by you.
2. **New regression cases.** Every axial category that gets a name should
   mint at least one permanent case reproducing it — a failure mode you can
   name but didn't add to the dataset will recur silently and you'll
   re-discover it later at full cost.
3. **Synthetic generation seeds.** `generate` should be pointed at the
   categories with the most annotated instances — the taxonomy sidebar's
   counts are literally the prioritized backlog for "what to generate more
   of."
4. **`analyze --mine` promotion.** A real trace that got annotated here and
   turned out to represent a whole category is exactly the candidate
   `--mine` promotes into the dataset (`origin: real-trace:<id>`) — the
   annotation you already wrote is most of the review `--mine` would ask you
   to redo.

Skipping this step is the specific way an annotation habit stops paying off:
the review session feels productive in the room and produces nothing that
outlives it. The taxonomy sidebar and the export panel exist to make "what
did I just learn, and where does it go" a single glance, not a follow-up
task that never happens.
