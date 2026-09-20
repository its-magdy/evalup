# Migrating a state dir to the per-run layout

Older state dirs kept run output in three sibling places: `runs/<run-id>/` for
per-case material, `baselines/current.json` + `baselines/<run-id>.verdicts.jsonl`
for the pinned baseline, and `reports/<run-id>.md` for the report. Everything a
run writes now lives in one directory, `reports/<run-id>/`, and the pinned
baseline is named by a pointer file, `reports/baseline.json`.

`run` detects the old layout (no `reports/baseline.json`, but `baselines/` or
`runs/` present) and stops rather than treating it as a first run — otherwise a
real baseline would be silently replaced by whatever ran next.

## Migrate

From the state dir (`<app-repo>/.evalup/` by default), for the pinned
baseline run id — `baselines/current.json` names it as `baseline_run_id`:

```bash
RUN_ID=$(python3 -c 'import json;print(json.load(open("baselines/current.json"))["baseline_run_id"])')

mkdir -p "reports/$RUN_ID"
# Per-case material, manifest, and any per-run jsonl move as-is.
mv runs/"$RUN_ID"/* "reports/$RUN_ID"/
# The report was a loose file; it becomes report.md inside the run directory.
[ -f "reports/$RUN_ID.md" ] && mv "reports/$RUN_ID.md" "reports/$RUN_ID/report.md"
# stats.py's paired input now lives with its run, not beside the pointer.
[ -f "baselines/$RUN_ID.verdicts.jsonl" ] && \
  mv "baselines/$RUN_ID.verdicts.jsonl" "reports/$RUN_ID/verdicts_for_stats.jsonl"
```

Then write the pointer, carrying the fields forward from `baselines/current.json`:

```bash
python3 - <<'PY'
import json
old = json.load(open("baselines/current.json"))
json.dump({"run_id": old["baseline_run_id"],
           "dataset_version": old["dataset_version"],
           "harness_version": old.get("harness_version")},
          open("reports/baseline.json", "w"), indent=2)
PY
```

Fill in `harness_version` by hand if the old pointer predates it. A baseline
whose harness version is unknown cannot be diffed against — `run` refuses
cross-version diffs — so the honest options are to record the version the run
actually used or to pin a fresh baseline.

Finally, remove the empty old directories:

```bash
rmdir "runs/$RUN_ID" runs 2>/dev/null; rm -rf baselines
```

## What does *not* move

- `datasets/`, `adapter.yaml`, `profile.yaml` — unchanged.
- `candidates/` — still a sibling of `reports/`. An `optimize` candidate is a
  proposed change, not a run's output; the runs that measure it land in
  `reports/` like any other run.
- Hook scratch state (`.last-smoke`, `smoke-hook.log`) —
  `${CLAUDE_PLUGIN_ROOT}/docs/smoke.sh.example` keeps it in `.hook-state/`,
  since `reports/` holds only run directories.

## Not migrating

Pinning a fresh baseline is always a valid alternative: run
`/evalup:run --baseline` and delete `runs/` and `baselines/`. You lose the
ability to diff against history from before the switch, which is the same thing
you lose on any harness-version bump.
