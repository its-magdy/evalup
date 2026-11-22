#!/usr/bin/env python3
"""Cost and latency for evalup runs, and the paired diff. Stdlib only.

`stats.py` compares only binary verdicts, so "2% better but 3x more
expensive" is indistinguishable from a free win. This script is the other
half. It reads one run (or two, paired), prices the tokens a trace already
recorded, summarizes latency as order statistics, and tests the paired
differences with tests that are EXACT at every n.

It reads. It writes nothing except the JSON it prints (or `-o`), invokes no
scorer, and re-derives no verdict.

WHAT IT DOES NOT DECIDE. There is exactly one definition of "this change is
better" in this package and it lives in `stats.py`'s keep rule. Cost does not
enter it, does not sit beside it as a second gate, and has no threshold here at
all: this output carries no `keep`, no `better`, no `improved`, no `regressed`
and no budget, and a test greps for every one of those words. The problem is
that the trade-off is INVISIBLE, not that it is ungated -- a cost gate would
need a per-case or per-run budget, nothing declares one, and inventing one
would be an overclaim: a budget no one set. Read this beside `stats.py`'s
verdict and make the trade yourself.
The division "dollars per net fixed case" is deliberately NOT computed here,
though it is the number a reader wants: it needs the discordant-pair count, and
computing that here would be a SECOND implementation of `stats.py`'s pairing.
`by_verdict_change` gives the partition instead -- what the extra spend bought
on the cases whose verdict did not move -- which needs no pairing machinery.

THE PRICE TABLE IS A DECLARED SIDECAR, never bundled and never inferred.
`--prices <file>`, by convention `<state-dir>/prices.json`. Three reasons it is
not shipped inside the plugin: prices change without any signal reaching this
repo, so a bundled table rots silently and its staleness is invisible in the
number it produces; a negotiated rate, a proxy, or a self-hosted model makes
the list price simply wrong; and a table nobody has to look at is a table
nobody dates. It is also NOT in `adapter.yaml`, `profile.yaml` or `plan.json`:
`run_cases.py` never prices anything, so a price key in the plan would be a
declaration with no reader, which this package does not add.
  Shape (see PRICE_SCHEMA): {"schema", "as_of", "currency": "USD", "source",
  "models": {"<gen_ai.request.model>": {"input_per_mtok": "2.50",
  "output_per_mtok": "10.00", "cache_read_per_mtok": "0.25",
  "cache_write_per_mtok": "3.125"}}, "aliases": {"<seen>": "<declared>"}}.
  The two cache tiers are OPTIONAL and come as a pair (one alone is exit 2);
  a table without them prices exactly as it did before they existed. They
  are read only where the trace source declares its cache accounting -- see
  CACHED TOKENS below. No default prices ship, cache tiers included.
  `as_of` and `source` are REQUIRED. The output carries `age_days` so a figure
  copied out of here carries its own staleness with it.
  Prices are DECIMAL STRINGS and are parsed into integer pico-USD per token, so
  every dollar figure below is exact integer arithmetic until it is rendered.
  A float price would make two runs of the same tokens differ in the last bits
  and a paired difference of two floats is not reliably zero when it should be.
MODEL MATCH IS EXACT, on `gen_ai.request.model` verbatim (a call without one
is `<model attribute absent>`, which `traces.source: response` always is;
alias that string to price it). No prefix match, no
family fallback, no "close enough": decision D2's rule is that inference must
be DECLARED per app, and `gpt-4o` and `gpt-4o-2024-08-06` are different prices.
A model the table does not name goes in `unpriced_models` and the run's dollar
figure is WITHHELD ENTIRELY (`usd: null`) rather than reported over the subset
that happened to match -- a partial total is an undercount that reads exactly
like a cheap run, which is the failure this script exists to expose. Declare
an `aliases` entry to map one onto another; that is a declaration, not a
guess. TOKENS are reported either way: tokens are measured, dollars are
derived.

WHICH RUNS CAN BE PRICED, and it is not all of them. The cost input is
`cases/<id>/trajectory.json`, which SS6 marks `# only when: a trace was
collected for this case`. So:
  - a run whose adapter declares a queryable trace source, `correlation` other
    than `none`, and the `gen_ai` convention (or a `mapping_shim`) can be
    priced, for those cases whose trace actually joined;
  - a TRACE-LESS run cannot be priced at all -- there is no token count
    anywhere in its output tree. An adapter that declares `correlation: none`
    is exactly this case: `run_cases.py` puts `cost_latency` in
    `traces.disabled_layers` and no `trajectory.json` is ever written. Such
    a run reports `status:
    "unpriced"` with that reason, and its LATENCY block is still complete.
LATENCY NEEDS NO TRACE. `results.json.cases[].latency_s` is written on every
run (SS10), so every run gets the latency half, priced or not.

WHAT IS NOT MEASURED, stated in the payload and not only here, because a number
copied into a report loses its docstring:
  - CACHED TOKENS, PARTLY (#105). `normalize_trace.py` keeps a chat span's
    `gen_ai.usage.cache_read.input_tokens` / `.cache_creation.input_tokens`
    as the call's `cache_read_input_tokens` / `cache_write_input_tokens`
    when the span carried them, and they are reported here as tokens. Whether
    a cache count is a PART of `input_tokens` or an addition to it is the
    source's accounting, which the convention does not fix, so the price is
    split only where it is DECLARED: an AG-UI stream read as its own trace
    (runner-contract.md SS2b says its cache counts are parts of
    inputTokens; `cache_counts_are_parts` reads that off the manifest).
    There, a call with cache counts priced against a model with both cache
    tiers costs (input - cache_read - cache_write) at the input rate plus
    each cache count at its tier, and the run says `cache_accounting:
    "priced"`. With the tiers absent the call prices exactly as before, at
    the full input rate, and the run says `"unpriced"` with a
    `cache_warning` naming the missing keys -- the error that hides is LARGE
    (a cached read is commonly a tenth of the input price, a cache write
    more than it). Every other source stays `"none"`, with a `cache_note`
    when it did report cache counts, until its accounting is declared.
  - TTFT. Nothing measures it. `latency_s` (`run_cases.py`) is whole-request
    wall clock, spans carry `duration_ms` and no first-token timestamp, and
    the adapter has no `streaming:` declaration. It is
    RESERVED here on the metric side too: buildable only from a first-token
    event this harness does not collect.
  - RETRIES. `results.json` records `latency_s` and NOT `attempts` or
    `retry_count` (those are in `cases/<id>/response.json`, which cannot be
    walked here -- see the seal below). A retried case's `latency_s` is its
    LAST attempt's, so it excludes the failed attempts and the backoff between
    them. Nothing in the readable surface says which cases those were.
  - THE OTHER k-1 REPEATS. At k > 1 the case-level `trajectory.json` is the
    REPRESENTATIVE repeat's (`run_cases.py`'s publish()); `repeats/<n>/` holds
    request/response/verdict only and the other trajectories are scored in a
    scratch dir that is deleted at the end of the case. So cost here is
    per-case-representative and the run's actual spend was roughly k times it.
    `k` is reported beside the total so the multiplier is visible.

THE HOLDOUT SEAL. Case ids come from `results.json.cases`, which carries no
holdout row at all (SS10), and every artifact this script opens is addressed by
one of those ids. `verdicts.jsonl` and `verdicts_for_stats.jsonl` are NOT read
-- both carry holdout ids -- and `cases/` is never listed, only indexed. So the
totals are over VISIBLE cases only: a holdout run spent more than the number
here says, and `holdout_excluded` reports how many cases the gap is, from
`summary.holdout` (a count, never an id).

THE STATISTICS, and the method this file DECLINES. The usual tool for a
continuous paired comparison is a bootstrap. A bootstrap is a Monte Carlo
APPROXIMATION whose answer moves with B and the seed, and `stats.py`'s standing
rule -- "all are EXACT at every n; no chi-square or normal approximation is
used in any DECISION or reported test" -- forbids exactly that. Reproducibility
is also the entire argument for this package having Python in it. So the
bootstrap is declined by name, and three EXACT things are reported instead.
Every one of them is distribution-free and conditions on the observed data.
  1. EXACT SIGN TEST, two-sided, on the paired differences. P(X <= min(up,
     down)) for X ~ Binomial(m, 0.5), doubled and capped -- the same tail
     `stats.py` computes on discordant pairs, imported from it rather than
     re-derived so the two cannot drift. Exact at EVERY n, including n=200.
     Tests DIRECTION: is the candidate dearer on more cases than chance.
  2. EXACT DISTRIBUTION-FREE CI for the MEDIAN paired difference, by inverting
     that sign test over the ORDER STATISTICS of the differences: the interval
     [d_(k+1), d_(m-k)] where k is the largest index with 2*P(X <= k-1) <=
     alpha. This is the magnitude-with-an-interval a bootstrap would give,
     and it needs no resampling: it is exact at every n, assumes nothing about
     the shape of the distribution, and is immune to the heavy tail that makes
     a mean latency meaningless. When m is too small for any interval to have
     the requested coverage (m < 6 at alpha=0.05) it reports `null` with
     `coverage_unattainable`, rather than a narrower interval under a wrong
     label.
  3. EXACT PAIRED PERMUTATION TEST on the TOTAL difference, over all 2^m sign
     flips. Under H0 -- the two configurations are the same, so each pair's two
     values could equally have come from either arm -- the sign vector is
     uniform, which makes this exact by construction with no distributional
     assumption at all. It is the only one of the three that weights MAGNITUDE,
     so it is the one that answers "3x more expensive on a handful of cases".
     Computed by exact integer subset-sum DP over the quantized differences
     (`latency_s` is already integer milliseconds; cost is integer pico-USD),
     never by enumeration and never by sampling. It is DECLINED, not
     approximated, when the DP state space exceeds `--max-exact-states` --
     `run_history.py`'s precedent, which refuses its exact null past a cap
     rather than substituting a normal one. When it is declined the sign test
     and the median CI still stand: both are exact at every n.
WHAT NONE OF THE THREE TESTS: generalization to production traffic (the case
suite is curated, not sampled); independence (cases sharing a `template_id`
are clustered, `stats.py`'s own first declared limit); the RATIO -- the
observed ratio is reported as a description with no interval, because
inverting a test for a ratio needs a different pivot and this file will not
imply an interval it did not compute; anything about the k-1 unobserved
repeats; and cache-corrected prices outside the one declared source, per
the limit above. There is no
multiplicity correction across the metrics tested, matching `stats.py`.

OUTPUT: JSON on stdout, or to `-o PATH`. Nothing is written into a run
directory. SS6 is the complete list of files under `--out` and a test
machine-diffs its `# only when:` markers against `REQUIRED_IF`, so a file there
would need a contract edit -- and would be wrong anyway, since a paired report
is a property of a PAIR and neither run owns it. `-o reports/cost.json` sits
beside `reports/baseline.json` and `run_history.py`'s `reports/history.json`.
Errors go to STDOUT as {"error": ...} with exit 2 (_common.die).

`capability_matrix.cost_latency` is what declares an app priceable, and it is
NOT a per-case layer: it has no row in runner-contract.md SS5's layer table and
no entry in `run_cases.py`'s `LAYER_ORDER`, deliberately (SS5.7). A per-case
cost VERDICT would need a per-case budget and the case format has no such
field. This is run-level, like `score_routing.py` (SS5.2).

Usage: score_cost.py <run-dir> [--baseline <run-dir>] [--prices prices.json]
                     [--alpha 0.05] [--max-exact-states N] [--max-cases N]
                     [-o cost.json]

See also: stats.py (the one keep rule), run_history.py (N runs over time),
reduce_repeats.py (one run, k repeats).
"""
import argparse
import json
import math
import os
from datetime import date
from decimal import Decimal, InvalidOperation

from _common import (
    HARNESS_VERSION,
    add_version_flag,
    die,
    load_json,
    require_range,
    scoring_comparison,
    write_output,
)
from stats import binom_one_sided_p

# SS9(b): both are REQUIRED_ALWAYS, so a directory with neither is not a run.
MANIFEST = "manifest.yaml"
RESULTS = "results.json"
# SS6 `# only when: a trace was collected for this case`. The ONLY cost input.
TRAJECTORY = "trajectory.json"

PRICE_SCHEMA = "evalup/price-table/1"
# #105: optional per model, both or neither. Additive to schema 1: a table
# without them reads, and prices, exactly as before.
CACHE_PRICE_FIELDS = ("cache_read_per_mtok", "cache_write_per_mtok")
# The trajectory's per-call cache keys (normalize_trace.py CACHE_ATTRS), present
# only when the trace reported them.
CACHE_USAGE_KEYS = ("cache_read_input_tokens", "cache_write_input_tokens")
# What a call with no gen_ai.request.model is reported, and priced, under. A
# table may name it in `aliases` (or `models`), which is a declaration: every
# such call in the run is then the model the user says it is.
ABSENT_MODEL = "<model attribute absent>"
# Prices are per MILLION tokens, so USD-per-token is p / 1e6 and pico-USD per
# token is p * 1e6. Integral for any price with at most six decimal places,
# which is four more than any published table uses.
PICO_PER_USD = 10 ** 12
PICO_PER_TOKEN_SCALE = 10 ** 6

# The DP below holds one dict entry per distinct achievable subset sum. The
# guard is on that state count rather than on m, because the state space is
# min(2^m, span/gcd + 1) and it is the second term that binds in practice: 200
# cases of near-identical latency have a small span and cost pennies to test
# exactly, while 25 cases of wildly different cost do not. Measured on this
# machine: 130k states 0.3s, 260k 1.7s, 520k 7.4s, 1.19M 19s -- so the default
# is a few seconds for a typical 200-case latency diff and about fifteen at the
# cap. Past it the test is DECLINED and no approximation is substituted for it,
# which is run_history.py's rule for its own exact null.
MAX_EXACT_STATES = 1_000_000
# Journal-style lists (unpaired ids, unpriced models) are truncated, not
# summarized away: the reader wants a name to go look at.
MAX_REPORTED = 40
# SS9(c) writes "ok" only after the completeness check passes, and run/SKILL.md
# SS3 calls anything else "not quotable". run_history.py applies the same rule.
QUOTABLE_STATUS = "ok"

# The words this file must never emit, because each of them is a verdict and
# stats.py owns the only one in the package. Asserted by a test.
FORBIDDEN_VERDICT_WORDS = ("keep", "better", "worse", "improved", "regressed",
                           "budget", "pass_rate")


class BadRun(Exception):
    """One unreadable artifact names itself rather than sinking the report."""


def _raise(message):
    raise BadRun(message)


def read_run_json(path, on_error=die):
    doc = load_json(path, on_error=on_error)
    if not isinstance(doc, dict):
        on_error(f"{os.path.basename(path)}: expected a JSON object, got "
                 f"{type(doc).__name__}")
    return doc


# -- the price table -------------------------------------------------------

def pico_per_token(field, model, raw):
    """A decimal price string -> integer pico-USD per token, or die.

    A string, not a float, and Decimal, not float(): 2.5e-6 USD per token is
    not representable in binary, so a float table makes the same tokens cost
    marginally different amounts in two runs and a paired difference that
    should be exactly zero is not. Everything downstream of here is integers.
    """
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        die(f"price table: {model}.{field} must be a decimal STRING (got "
            f"{type(raw).__name__}) -- a float price is not exactly "
            "representable and makes two identical runs differ in the last "
            "bits")
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        die(f"price table: {model}.{field}: {raw!r} is not a decimal number")
    if value < 0:
        die(f"price table: {model}.{field} must be >= 0, got {raw!r}")
    scaled = value * PICO_PER_TOKEN_SCALE
    if scaled != scaled.to_integral_value():
        die(f"price table: {model}.{field}: {raw!r} has more than six decimal "
            "places, which is finer than this table's exact integer "
            "resolution (pico-USD per token)")
    return int(scaled)


def load_prices(path):
    """The declared sidecar. Absent is a fact, a malformed one is an error."""
    doc = read_run_json(path)
    if doc.get("schema") != PRICE_SCHEMA:
        die(f"price table: expected schema {PRICE_SCHEMA!r}, got "
            f"{doc.get('schema')!r}")
    for field in ("as_of", "source"):
        if not isinstance(doc.get(field), str) or not doc[field].strip():
            die(f"price table: {field!r} is required and must be a non-empty "
                "string -- a table nobody dates is a table nobody notices "
                "going stale")
    try:
        as_of = date.fromisoformat(doc["as_of"])
    except ValueError:
        die(f"price table: as_of {doc['as_of']!r} is not an ISO date "
            "(YYYY-MM-DD)")
    currency = doc.get("currency", "USD")
    if currency != "USD":
        die(f"price table: currency {currency!r} is not supported; this script "
            "reports USD and will not convert (a rate is another dated fact "
            "nobody would maintain here)")
    models = doc.get("models")
    if not isinstance(models, dict) or not models:
        die("price table: 'models' must be a non-empty object keyed by the "
            "gen_ai.request.model string the trace records")
    table = {}
    for model, entry in models.items():
        if not isinstance(entry, dict):
            die(f"price table: models.{model} must be an object with "
                "input_per_mtok and output_per_mtok")
        rate = [pico_per_token(f, model, entry.get(f))
                for f in ("input_per_mtok", "output_per_mtok")]
        # #105: the two cache tiers are optional, and come as a PAIR. One
        # alone is refused: half a tier leaves the other cache count priced at
        # some rate nobody declared, which is the guess this table exists to
        # stop. A provider that bills cache writes at the input price declares
        # cache_write_per_mtok equal to input_per_mtok.
        present = [f for f in CACHE_PRICE_FIELDS if f in entry]
        if len(present) == 1:
            missing = next(f for f in CACHE_PRICE_FIELDS if f not in present)
            die(f"price table: models.{model} declares {present[0]} without "
                f"{missing}; declare both cache tiers or neither (a provider "
                "with no cache-write surcharge sets cache_write_per_mtok to "
                "its input_per_mtok)")
        rate.extend(pico_per_token(f, model, entry[f]) if present else None
                    for f in CACHE_PRICE_FIELDS)
        table[model] = tuple(rate)
    aliases = doc.get("aliases") or {}
    if not isinstance(aliases, dict):
        die("price table: 'aliases' must be an object mapping a model string "
            "seen in a trace onto one declared in 'models'")
    for seen, declared in aliases.items():
        if declared not in table:
            die(f"price table: alias {seen!r} points at {declared!r}, which is "
                "not in 'models'")
    return {"table": table, "aliases": aliases, "as_of": as_of,
            "source": doc["source"],
            "age_days": (date.today() - as_of).days}


# -- reading one run -------------------------------------------------------

def visible_cases(run_dir):
    """The run's case rows, from results.json only.

    results.json.cases is the sealed-safe per-case record: SS10 says a holdout
    case contributes no row and its id appears nowhere in the file. Every path
    this script opens is built from an id taken from here, so `cases/` is
    indexed and never listed -- listing it would enumerate the sealed ids.
    """
    results = read_run_json(os.path.join(run_dir, RESULTS), on_error=_raise)
    summary = results.get("summary")
    if not isinstance(summary, dict):
        _raise("results.json has no summary object")
    if summary.get("status") != QUOTABLE_STATUS:
        _raise("summary.status is {!r}, not {!r} -- SS9(c) writes 'ok' only "
               "after the completeness check passes, and run/SKILL.md SS3 "
               "calls anything else not quotable".format(
                   summary.get("status"), QUOTABLE_STATUS))
    rows = results.get("cases")
    if not isinstance(rows, list):
        _raise("results.json has no cases list")
    cases = {}
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("case_id"), str):
            cases[row["case_id"]] = row
    return results, summary, cases


def case_usage(run_dir, case_id, row=None):
    """One case's LLM calls, from cases/<id>/trajectory.json, or None.

    Absent is a FACT, not an error: SS6 writes this file only when a trace was
    collected for the case, so a trace-less run has none and a run that lost
    one trace has all but one. Both are reported as counts, never imputed as
    zero cost -- a case priced at zero because its trace went missing is
    indistinguishable in the total from a case that was genuinely free.

    A CONVERSATION (its results.json row says multi_turn) is priced as one
    case: the sum of turns/<t>/trajectory.json over the turns it sent
    (docs/multi-turn.md SS5). Its case-level trajectory.json is only the
    deciding turn's, so reading it alone would price one turn of N. One turn
    without a trajectory withholds the whole case, by the rule above.
    """
    row = row if isinstance(row, dict) else {}
    if row.get("multi_turn"):
        sent = row.get("turns_sent")
        if not isinstance(sent, int) or isinstance(sent, bool) or sent < 1:
            return None
        calls = []
        for turn in range(1, sent + 1):
            usage = trajectory_usage(os.path.join(
                run_dir, "cases", case_id, "turns", str(turn), TRAJECTORY))
            if usage is None:
                return None
            calls.extend(usage)
        return calls
    return trajectory_usage(os.path.join(run_dir, "cases", case_id,
                                         TRAJECTORY))


def trajectory_usage(path):
    """The LLM calls in one trajectory.json, or None when it is absent or
    unreadable.

    One tuple per call: (model, input, output, bad_count, cache_read,
    cache_write). A cache count is None when the trace did not report it,
    which is not the same as zero; one that is not a whole number is a bad
    count like any other.
    """
    if not os.path.isfile(path):
        return None
    try:
        doc = read_run_json(path, on_error=_raise)
    except BadRun:
        return None
    trajectory = doc.get("trajectory")
    if not isinstance(trajectory, dict):
        return None
    calls = []
    for call in trajectory.get("llm_calls") or []:
        if not isinstance(call, dict):
            continue
        usage = call.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        n_in = token_count(usage.get("input_tokens"))
        n_out = token_count(usage.get("output_tokens"))
        bad = n_in is None or n_out is None
        cache = []
        for key in CACHE_USAGE_KEYS:
            if key not in usage:
                cache.append(None)
                continue
            count = token_count(usage[key])
            bad = bad or count is None
            cache.append(count or 0)
        calls.append((call.get("model"), n_in or 0, n_out or 0, bad, *cache))
    return calls


def token_count(value):
    """A recorded token count as an int, or None when what was recorded cannot
    be one.

    Absent is 0: a span with no usage attribute recorded
    nothing to price. An integral float is the int it spells -- `1000.0` is
    what a count looks like after a round trip through a JS number or a
    float-typed metrics field, and it is still a thousand tokens. Everything
    else -- a fractional float, a negative, a string, a bool (True would price
    as one token) -- is None, and price_run WITHHOLDS the dollars over it.

    Returning 0 for those would let an exporter emitting floats produce
    "status": "priced", "usd": 0.0, and price a negative count to negative
    dollars: the zero-imputation case_usage's docstring forbids, one level
    down.
    """
    if value is None:
        return 0
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int) and value >= 0:
        return value
    return None


def cache_counts_are_parts(manifest):
    """True when the run's trace source DECLARES its cache accounting as parts
    of input_tokens. Today that is one source: an AG-UI stream read as its own
    trace (`invocation.response_format: sse`, `sse.protocol: ag-ui`,
    `traces.source: response`; runner-contract.md SS2b). Every other source
    -- a JSON body's usage, an OTLP store -- leaves open whether a cache
    count sits inside input_tokens or beside it, so its cache counts are
    reported and NOT used to split the price (`cache_accounting: "none"`)
    until that source's accounting is declared too.
    """
    if not isinstance(manifest, dict):
        return False
    invocation = manifest.get("invocation")
    traces = manifest.get("traces")
    if not isinstance(invocation, dict) or not isinstance(traces, dict):
        return False
    sse = invocation.get("sse")
    return (traces.get("source") == "response"
            and invocation.get("response_format") == "sse"
            and isinstance(sse, dict) and sse.get("protocol") == "ag-ui")


def price_run(run_dir, cases, prices, cache_parts=False):
    """Per-case pico-USD and the model rollup, or the reason there is none.

    Returns (per_case_pico, block). `per_case_pico` is None whenever the run's
    dollar figure is withheld, so the paired comparison cannot accidentally
    difference a partial total against a complete one.

    `cache_parts` (cache_counts_are_parts) turns on the #105 split. A call
    that reported a cache count, priced against a model whose entry declares
    both cache tiers, costs uncached input (input minus both cache counts) at
    the input rate, each cache count at its own tier, and output as before.
    The split is ALL OR NOTHING per run: when any cache-reporting call's
    model lacks the tiers, every call is priced exactly as before and the
    run says `cache_accounting: "unpriced"`, naming the missing keys. A
    blend of split and unsplit calls would be a figure that is neither the
    old price nor the corrected one -- the partial total this script
    withholds for an unknown model.
    """
    by_model, per_case, with_trace, unpriced = {}, {}, 0, set()
    tokens_in = tokens_out = unusable = 0
    cache_tokens = {key: 0 for key in CACHE_USAGE_KEYS}
    cache_calls = cache_over_input = 0
    no_cache_tier = set()
    # Both prices, per case: the old formula (every input token at the input
    # rate) and the split. The run publishes one of them, never a blend.
    per_case_split = {}
    for case_id in cases:
        calls = case_usage(run_dir, case_id, cases[case_id])
        if calls is None:
            continue
        with_trace += 1
        pico = pico_split = 0
        for model, n_in, n_out, bad_count, c_read, c_write in calls:
            unusable += bad_count
            tokens_in += n_in
            tokens_out += n_out
            # A model that is not a string is the sentinel; "" stays "",
            # which is how the table looked it up before #105.
            name = model if isinstance(model, str) else ABSENT_MODEL
            entry = by_model.setdefault(
                name,
                {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                 "priced": False})
            entry["calls"] += 1
            entry["input_tokens"] += n_in
            entry["output_tokens"] += n_out
            reported = c_read is not None or c_write is not None
            if reported:
                cache_calls += 1
                for field, count in zip(CACHE_USAGE_KEYS, (c_read, c_write)):
                    if count is not None:
                        cache_tokens[field] += count
                        entry[field] = entry.get(field, 0) + count
            if prices is None:
                continue
            # Resolved by the name the output reports, so the "<model
            # attribute absent>" that unpriced_models names can be declared
            # like any other: a `traces.source: response` span carries no
            # gen_ai.request.model at all, and before #105 no table could
            # price one.
            resolved = prices["aliases"].get(name, name)
            rate = prices["table"].get(resolved)
            if rate is None:
                unpriced.add(name)
                continue
            entry["priced"] = True
            old = n_in * rate[0] + n_out * rate[1]
            pico += old
            if not (cache_parts and reported):
                pico_split += old
                continue
            if rate[2] is None:
                no_cache_tier.add(resolved)
                continue
            c_read, c_write = c_read or 0, c_write or 0
            uncached = n_in - c_read - c_write
            if uncached < 0:
                # The declared accounting says these are PARTS of
                # input_tokens; a part larger than its whole is a count this
                # scorer cannot price, withheld like any unusable one.
                cache_over_input += 1
                continue
            pico_split += (uncached * rate[0] + c_read * rate[2]
                           + c_write * rate[3] + n_out * rate[1])
        per_case[case_id] = pico
        per_case_split[case_id] = pico_split
    without_trace = len(cases) - with_trace

    tokens = {"input": tokens_in, "output": tokens_out,
              "total": tokens_in + tokens_out}
    if cache_calls:
        # Measured, so reported whatever the source. Not added into total:
        # under the one declared accounting they are already inside input.
        tokens.update({"cache_read_input": cache_tokens[CACHE_USAGE_KEYS[0]],
                       "cache_write_input": cache_tokens[CACHE_USAGE_KEYS[1]],
                       "calls_reporting_cache": cache_calls})
    block = {
        "cases_with_trace": with_trace,
        "cases_without_trace": without_trace,
        "tokens": tokens,
        "models": {name: {k: v for k, v in entry.items() if k != "priced"}
                   for name, entry in sorted(by_model.items())},
        "cache_accounting": "none",
    }
    if cache_calls and not cache_parts:
        block["cache_note"] = (
            f"{cache_calls} LLM call(s) reported cache counts, but this run's "
            "trace source does not declare whether they are parts of "
            "input_tokens or additions to it (only an AG-UI stream read with "
            "traces.source: response does), so input is priced at the input "
            "rate as recorded and the cache counts are reported, not split "
            "out.")
    elif cache_calls:
        # Not split out of any published price. "priced" is set below, and
        # only where the split total is the one published.
        block["cache_accounting"] = "unpriced"
    if with_trace == 0:
        # NOT reported as zero tokens: a run with no trace did not spend
        # nothing, it recorded nothing, and a zero in a token column is
        # indistinguishable in a total from a genuinely free run.
        del block["tokens"], block["models"]
        block["status"] = "unpriced"
        block["reason"] = (
            "no cases/<id>/trajectory.json in this run: runner-contract.md SS6 "
            "writes it only when a trace was collected, so a trace-less run "
            "(adapter traces.source view-only/none, correlation: none, or a "
            "non-gen_ai convention with no mapping_shim) records no token "
            "count anywhere. Latency below is unaffected.")
        return None, block
    if prices is None:
        block["status"] = "unpriced"
        block["reason"] = ("no price table declared; pass --prices <file>. "
                           "Tokens above are measured and stand on their own.")
        return None, block
    if unusable:
        block["status"] = "unpriced"
        block["calls_with_unusable_token_count"] = unusable
        block["reason"] = (
            f"{unusable} LLM call(s) recorded a token count that is not a "
            "non-negative whole number (a fractional float, a negative, a "
            "string), so the dollar total is withheld: pricing those calls at "
            "zero would read exactly like a cheap run. The token totals above "
            "EXCLUDE them. Fix the exporter or the traces.mapping_shim.")
        return None, block
    # Only when the split is the figure to publish: with a tier missing the
    # run prices as before, and the old formula needs no cache count.
    if cache_over_input and not no_cache_tier:
        block["status"] = "unpriced"
        block["calls_with_cache_over_input"] = cache_over_input
        block["reason"] = (
            f"{cache_over_input} LLM call(s) reported cache counts larger "
            "than their input_tokens. This source's declared accounting makes "
            "a cache count a PART of input_tokens, so the uncached remainder "
            "would be negative; the dollar total is withheld rather than "
            "priced from a count that contradicts its own accounting.")
        return None, block
    if unpriced:
        block["status"] = "unpriced"
        block["unpriced_models"] = sorted(unpriced)[:MAX_REPORTED]
        block["reason"] = (
            f"the price table names no rate for {len(unpriced)} model(s) "
            "this run used, so "
            "the dollar total is withheld rather than reported over the subset "
            "that matched -- a partial total is an undercount that reads like "
            "a cheap run. Model match is EXACT on gen_ai.request.model; add "
            "the model to 'models', or an 'aliases' entry mapping it onto a "
            "declared one.")
        return None, block

    if cache_calls and cache_parts:
        if no_cache_tier:
            block["cache_warning"] = (
                "the price table declares no {} for {}, so every call is "
                "priced as before, cache-read and cache-write tokens at the "
                "input rate. On a cached agent that figure can be far off -- "
                "a cache read usually costs a tenth of input, a write more "
                "than input; add both keys to split them out.".format(
                    " / ".join(CACHE_PRICE_FIELDS),
                    ", ".join(sorted(no_cache_tier)[:MAX_REPORTED])))
        else:
            per_case = per_case_split
            block["cache_accounting"] = "priced"
    total_pico = sum(per_case.values())
    block["status"] = "priced"
    block["usd"] = usd(total_pico)
    block["usd_per_case_with_trace"] = usd(total_pico // with_trace) \
        if with_trace else None
    block["price_table"] = {"as_of": prices["as_of"].isoformat(),
                            "age_days": prices["age_days"],
                            "source": prices["source"],
                            "models_declared": len(prices["table"])}
    return per_case, block


def usd(pico):
    """pico-USD -> a float with six decimal places, for rendering only. Every
    comparison and every test above this line is integer arithmetic."""
    return round(pico / PICO_PER_USD, 6)


# -- latency ---------------------------------------------------------------

def latency_ms(row):
    """results.json's latency_s -> integer milliseconds, or None.

    run_cases.py already rounds to three decimals, so this is a units change
    and not a quantization: the value on disk has millisecond resolution and
    is a float only because JSON has one number type. Integers are what makes
    the permutation DP below exact.
    """
    value = row.get("latency_s")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    # NaN/Infinity parse as floats and int() of one is a traceback; a latency
    # that is not a finite non-negative number was not measured.
    if not math.isfinite(value) or value < 0:
        return None
    return int(round(value * 1000))


def order_statistics(values):
    """Nearest-rank percentiles over the observed sample.

    Nearest-rank, NOT interpolated: an interpolated p90 is a number no case
    took, and the point of a latency percentile is to name a real request.
    `saturated` lists the percentiles that came out equal to `max`, which is
    what small n does to a high percentile -- p95 of 10 cases IS the maximum,
    and reporting the two as if they were separate evidence is the way a
    reader concludes the tail is well characterized when it is not.
    """
    ordered = sorted(values)
    n = len(ordered)
    if not n:
        return None
    out = {"n": n, "min": ordered[0], "max": ordered[-1],
           "total": sum(ordered)}
    for label, q in (("p50", 50), ("p90", 90), ("p95", 95), ("p99", 99)):
        # Nearest-rank: ceil(q/100 * n), 1-indexed.
        rank = -(-q * n // 100)
        out[label] = ordered[max(rank, 1) - 1]
    out["saturated"] = [label for label in ("p50", "p90", "p95", "p99")
                        if out[label] == out["max"]]
    out["method"] = ("nearest-rank order statistics on the observed sample; "
                     "no interpolation, so every value named is one a case "
                     "actually took")
    return out


def seconds(block, keys):
    """Render an integer-millisecond block in seconds, for output only."""
    return {k: (round(v / 1000, 3) if k in keys else v)
            for k, v in block.items()}


# -- the three exact tests -------------------------------------------------

def sign_test_two_sided(diffs):
    """Exact two-sided sign test on the paired differences.

    2 * P(X <= min(up, down)) for X ~ Binomial(m, 0.5), capped at 1 -- the same
    tail stats.py computes on discordant pairs, and imported from that module
    (binom_one_sided_p) rather than re-derived, so the package cannot end up
    with two exact binomials that disagree. Zero differences are DROPPED, which
    is the sign test's standard conditioning: a pair that did not move carries
    no directional information and counting it as half a vote each way is an
    approximation.
    """
    up = sum(1 for d in diffs if d > 0)
    down = sum(1 for d in diffs if d < 0)
    m = up + down
    lo = min(up, down)
    return {"n_nonzero": m, "n_zero": len(diffs) - m,
            "candidate_higher": up, "candidate_lower": down,
            "p_two_sided": (min(1.0, 2 * binom_one_sided_p(m - lo, m))
                            if m else 1.0),
            "exact": True,
            "tests": "direction only -- whether the candidate is higher on "
                     "more cases than chance. Not magnitude."}


def median_diff_ci(diffs, alpha):
    """Exact distribution-free CI for the MEDIAN paired difference.

    The sign test inverted over the order statistics: with the differences
    sorted, [d_(k+1), d_(m-k)] covers the true median with probability at least
    1 - 2*P(X <= k-1) for X ~ Binomial(m, 0.5). Pick the largest k whose
    two-sided tail still fits inside alpha. This is exact at every m, assumes
    nothing about the shape of the difference distribution, and needs no
    resampling -- which is why no bootstrap is needed: the interval it would
    estimate exists in closed form.

    Zero differences are KEPT here, unlike in the sign test above: they are
    real observations of the median's location even though they carry no
    direction. Below m = 6 no interval attains 95% coverage at all (the
    widest, the full range, covers with probability 1 - 2^(1-m)), so the
    interval is null and says so rather than reporting a narrower one under a
    label it has not earned.
    """
    ordered = sorted(diffs)
    m = len(ordered)
    if m == 0:
        return {"point": None, "interval": None,
                "reason": "no paired cases"}
    mid = ordered[m // 2] if m % 2 else (ordered[m // 2 - 1]
                                         + ordered[m // 2]) / 2
    best = None
    for k in range(m // 2, 0, -1):
        # P(X <= k-1) = P(X >= m-k+1) by symmetry, via the imported exact tail.
        if 2 * binom_one_sided_p(m - k + 1, m) <= alpha:
            best = k
            break
    if best is None:
        widest = 1 - 2.0 ** (1 - m) if m else 0.0
        return {"point": mid, "interval": None,
                "coverage_unattainable": True,
                "reason": (
                    f"no interval attains {1 - alpha:.0%} coverage at m={m}"
                    " -- the widest possible one (the full range) covers "
                    f"with probability {widest:.4f}. This is a statement "
                    "about n, not about the data.")}
    return {"point": mid,
            "interval": [ordered[best], ordered[m - best - 1]],
            "coverage": 1 - 2 * binom_one_sided_p(m - best + 1, m),
            "method": ("exact distribution-free: the sign test inverted over "
                       "the order statistics. No resampling, no normal "
                       "approximation, no assumption about the shape of the "
                       "difference distribution.")}


def permutation_test_exact(diffs, max_states=MAX_EXACT_STATES):
    """Exact paired permutation test on the SUM of the differences.

    Under H0 -- the two configurations are the same, so each pair's two values
    could equally have come from either arm -- every one of the 2^m sign
    vectors is equally likely. With a_i = |d_i| and T = sum a_i, the statistic
    S = sum s_i a_i equals T - 2 * (subset sum of the flipped indices), so the
    exact null distribution of S is the subset-sum COUNT distribution of the
    a_i. That is computed here as an integer DP over {subset_sum: count}: exact
    arithmetic, no enumeration of 2^m vectors, no sampling, no seed.

    The state space is min(2^m, T/g + 1) entries where g is the gcd, so the
    cost is driven by the SPREAD of the differences rather than by m alone.
    Past `max_states` the test is DECLINED and nothing is substituted for it,
    which is run_history.py's rule for its own exact null. The sign test and
    the median interval are exact at every m and still stand.

    Zero differences are dropped: flipping a zero changes nothing, so they
    scale the state space without moving the distribution.
    """
    values = [abs(d) for d in diffs if d]
    m = len(values)
    if m == 0:
        return {"tested": False,
                "reason": "every paired difference is exactly zero"}
    total = sum(values)
    observed = sum(diffs)
    dp = {0: 1}
    for a in values:
        nxt = dict(dp)
        for s, count in dp.items():
            nxt[s + a] = nxt.get(s + a, 0) + count
        dp = nxt
        if len(dp) > max_states:
            return {"tested": False, "m": m,
                    "reason": (f"the exact null needs more than {max_states} distinct "
                               f"subset sums at m={m}; this script declines the "
                               "test rather than substituting a normal "
                               "approximation or a bootstrap for it (see "
                               "run_history.py's --max-exact-runs). Raise "
                               "--max-exact-states to spend the memory, or "
                               "read the sign test and median interval above, "
                               "which are exact at every n."
                               )}
    space = 2 ** m
    # S = total - 2*subset. |S| >= |observed| is the two-sided tail.
    tail = sum(count for subset, count in dp.items()
               if abs(total - 2 * subset) >= abs(observed))
    return {"tested": True, "m": m, "statistic_sum": observed,
            "p_two_sided": min(1.0, tail / space),
            "null_states": len(dp), "exact": True,
            "method": ("exact enumeration of all 2^m sign flips, as an integer "
                       "subset-sum DP over the null distribution. This is the "
                       "only one of the three tests that weights MAGNITUDE.")}


def paired_block(diffs, alpha, max_states, scale=1, unit=None):
    """The three exact tests over one metric's paired differences."""
    out = {"n_paired": len(diffs),
           "total_delta": sum(diffs) / scale,
           "mean_delta": (sum(diffs) / len(diffs) / scale) if diffs else None,
           "median_delta_ci": scaled_ci(median_diff_ci(diffs, alpha), scale),
           "sign_test": sign_test_two_sided(diffs),
           "permutation_test": scaled_permutation(
               permutation_test_exact(diffs, max_states), scale)}
    if unit:
        out["unit"] = unit
    return out


def scaled_ci(block, scale):
    if scale == 1:
        return block
    out = dict(block)
    if out.get("point") is not None:
        out["point"] = round(out["point"] / scale, 9)
    if out.get("interval"):
        out["interval"] = [round(v / scale, 9) for v in out["interval"]]
    return out


def scaled_permutation(block, scale):
    if scale == 1 or block.get("statistic_sum") is None:
        return block
    out = dict(block)
    out["statistic_sum"] = round(out["statistic_sum"] / scale, 9)
    return out


# -- assembly --------------------------------------------------------------

def load_run(run_dir):
    if not os.path.isdir(run_dir):
        die(f"{run_dir} is not a directory (expected a run directory, one "
            f"containing {MANIFEST})")
    if not os.path.isfile(os.path.join(run_dir, MANIFEST)):
        die(f"{run_dir} holds no {MANIFEST} -- a run directory is one "
            "containing it.")
    manifest = read_run_json(os.path.join(run_dir, MANIFEST))
    try:
        results, summary, cases = visible_cases(run_dir)
    except BadRun as exc:
        die(f"{os.path.basename(run_dir)}: {exc}")
    holdout = summary.get("holdout")
    return {
        "dir": run_dir,
        "manifest": manifest,
        "results": results,
        "summary": summary,
        "cases": cases,
        "meta": {
            "run_id": results.get("run_id") or manifest.get("run_id"),
            "mode": results.get("mode") or manifest.get("mode"),
            "k": manifest.get("k"),
            "dataset_version": results.get("dataset_version"),
            "harness_version": results.get("harness_version"),
            "scoring_version": (results.get("scoring_version")
                                if results.get("scoring_version") is not None
                                else manifest.get("scoring_version")),
            "selecting_split": manifest.get("selecting_split"),
            "app_git_sha": ((manifest.get("app") or {}).get("git_sha")
                            if isinstance(manifest.get("app"), dict) else None),
            "visible_cases": len(cases),
            "holdout_excluded": (holdout or {}).get("n")
            if isinstance(holdout, dict) else 0,
        },
    }


def latency_block(cases):
    values = [latency_ms(row) for row in cases.values()]
    present = [v for v in values if v is not None]
    stats_block = order_statistics(present)
    block = {"unit": "seconds",
             "missing": len(values) - len(present),
             "missing_note": ("a case with no latency_s was skipped by a "
                              "safety gate or never got a response; it is "
                              "absent from the order statistics, never "
                              "imputed")}
    if stats_block is None:
        block["n"] = 0
        return block
    block.update(seconds(stats_block,
                         ("min", "max", "total", "p50", "p90", "p95", "p99")))
    return block


def turns_sent(row):
    """How many turns a results.json row sent: a conversation's turns_sent,
    and 1 for a single-turn case (including a run older than the field)."""
    return row.get("turns_sent") if row.get("multi_turn") else 1


def comparability(base, cand):
    """SS5.6's pair rule, restated. A warning, never a refusal: this script
    makes no decision, so a mismatched pair is a caveat on a number rather than
    an input the runner would reject."""
    notes = []
    # #38: the scoring version is the key; the plugin version is not. A run
    # older than the field is keyed on its release (_common.scoring_key),
    # and says so.
    _, mismatch, warning = scoring_comparison(base["meta"], cand["meta"])
    if mismatch:
        # Worded by the field that actually differed: two pre-field runs
        # differ on their release, not on a recorded scoring version.
        why = ("a different harness version, on a run that predates "
               "scoring_version, may score verdicts differently"
               if mismatch.startswith("harness_version") else
               "a different scoring version changes what a verdict means")
        notes.append(f"{mismatch} -- {why}")
    if warning:
        notes.append(warning)
    for key, why in (("dataset_version", "a different suite is a different "
                                         "denominator"),
                     ("k", "a different k prices a different number of "
                           "repeats per case"),
                     ("selecting_split", "a different split selects a "
                                         "different case set"),
                     ("mode", "a different mode selects a different case set")):
        if base["meta"].get(key) != cand["meta"].get(key):
            notes.append("{}: {!r} vs {!r} -- {}".format(
                key, base["meta"].get(key), cand["meta"].get(key), why))
    return notes


def by_verdict_change(base, cand, shared, lat, cost_pico):
    """What the extra spend bought, partitioned by whether the verdict moved.

    This is the closest this script comes to "2% better but 3x more
    expensive", and it is deliberately a PARTITION rather than a rate: it
    reports what was spent on the cases whose verdict did not change at all,
    which needs no discordant-pair count and therefore no second copy of
    stats.py's pairing. Dividing dollars by net fixed cases is left to the
    reader, on purpose.
    """
    groups = {"unchanged": [], "changed": []}
    for case_id in shared:
        b = base["cases"][case_id].get("verdict")
        c = cand["cases"][case_id].get("verdict")
        groups["unchanged" if b == c else "changed"].append(case_id)
    out = {}
    for name, ids in groups.items():
        entry = {"n": len(ids)}
        deltas = [lat[i] for i in ids if i in lat]
        if deltas:
            entry["latency_total_delta_s"] = round(sum(deltas) / 1000, 3)
        if cost_pico is not None:
            spend = [cost_pico[i] for i in ids if i in cost_pico]
            if spend:
                entry["cost_usd_delta"] = usd(sum(spend))
        out[name] = entry
    out["note"] = ("a verdict that did not change is the case where extra "
                   "spend bought nothing measurable. This is a partition, not "
                   "a rate: stats.py owns the pairing that would turn it into "
                   "one.")
    return out


def compare(base, cand, base_cost, cand_cost, a):
    shared = sorted(set(base["cases"]) & set(cand["cases"]))
    only_base = sorted(set(base["cases"]) - set(cand["cases"]))
    only_cand = sorted(set(cand["cases"]) - set(base["cases"]))
    out = {
        "baseline_run_id": base["meta"]["run_id"],
        "candidate_run_id": cand["meta"]["run_id"],
        "n_paired": len(shared),
        "only_in_baseline": only_base[:MAX_REPORTED],
        "only_in_candidate": only_cand[:MAX_REPORTED],
        "comparability_warnings": comparability(base, cand),
        "attrition_note": ("case ids come from results.json.cases, which "
                           "carries no holdout row, so an id sealed in either "
                           "run is unpaired here by construction and is not "
                           "listed above."),
    }
    if not shared:
        out["note"] = ("the two runs share no visible case id, so there is "
                       "nothing to pair. That is a fact about the pair, not "
                       "an error.")
        return out

    # docs/multi-turn.md SS4: a conversation's latency and tokens are sums
    # over the turns it SENT. A baseline that failed at turn 1 beside a
    # candidate that got through turn 4 is not 3.6 s slower, it got further
    # -- so a pair whose turns_sent differ is refused, and named.
    mismatched = [i for i in shared
                  if turns_sent(base["cases"][i]) != turns_sent(cand["cases"][i])]
    if mismatched:
        out["turns_sent_mismatch"] = {
            "n": len(mismatched), "case_ids": mismatched[:MAX_REPORTED],
            "note": ("excluded from the paired latency and cost tests: these "
                     "conversations sent a different number of turns in the "
                     "two runs, so their sums measure how far each got, not "
                     "how fast or costly the app was")}
    comparable = [i for i in shared if i not in set(mismatched)]
    lat = {}
    for case_id in comparable:
        b, c = (latency_ms(base["cases"][case_id]),
                latency_ms(cand["cases"][case_id]))
        if b is not None and c is not None:
            lat[case_id] = c - b
    out["latency"] = paired_block([lat[i] for i in sorted(lat)], a.alpha,
                                  a.max_exact_states, scale=1000,
                                  unit="seconds")
    out["latency"]["cases_dropped_for_missing_latency"] = \
        len(comparable) - len(lat)

    cost_pico = None
    if base_cost is None or cand_cost is None:
        out["cost"] = {"status": "unpriced",
                       "reason": ("a paired dollar figure needs both runs "
                                  "priced; see each run's cost block for the "
                                  "side that is not.")}
    else:
        cost_pico = {i: cand_cost[i] - base_cost[i] for i in comparable
                     if i in base_cost and i in cand_cost}
        block = paired_block([cost_pico[i] for i in sorted(cost_pico)],
                             a.alpha, a.max_exact_states,
                             scale=PICO_PER_USD, unit="USD")
        block["status"] = "priced"
        block["cases_dropped_for_missing_trace"] = \
            len(comparable) - len(cost_pico)
        base_total = sum(base_cost[i] for i in cost_pico)
        cand_total = sum(cand_cost[i] for i in cost_pico)
        block["baseline_total_usd"] = usd(base_total)
        block["candidate_total_usd"] = usd(cand_total)
        block["ratio"] = (round(cand_total / base_total, 4)
                          if base_total else None)
        block["ratio_note"] = ("observed on the paired cases, DESCRIPTIVE "
                               "only. No interval: inverting a test for a "
                               "ratio needs a different pivot, and this file "
                               "will not imply an interval it did not "
                               "compute.")
        out["cost"] = block

    out["by_verdict_change"] = by_verdict_change(base, cand, shared, lat,
                                                 cost_pico)
    return out


def declared_limits(runs):
    ks = sorted({r["meta"].get("k") for r in runs})
    return {
        "cache_accounting": (
            "cost.cache_accounting says which applies. 'priced': the trace "
            "source declares cache counts as parts of input_tokens (an "
            "AG-UI stream read with traces.source: response) and the price "
            "table has cache_read_per_mtok and cache_write_per_mtok for the "
            "model, so uncached input, cache reads, cache writes and output "
            "are priced separately. 'unpriced': the same source reported "
            "cache counts, but a model lacks those keys (or no table was "
            "given, or the dollars were withheld for the reason stated), so no "
            "published figure splits them out; with cache_warning, the "
            "figure prices cached tokens at the full input rate -- on a "
            "cached agent a large error, in either direction. 'none': "
            "no cache count was reported, or the source does not declare "
            "whether they sit inside input_tokens or beside it, so they are "
            "reported as tokens and not split out of the price."),
        "ttft": (
            "NOT MEASURED. latency_s is whole-request wall clock; spans carry "
            "duration_ms and no first-token timestamp. Step 10 removed the "
            "adapter's inert streaming: declaration; the METRIC is reserved "
            "here for the same reason -- it needs a first-token event this "
            "harness does not collect."),
        "retries": (
            "NOT VISIBLE. results.json records latency_s and not attempts or "
            "retry_count, and cases/<id>/response.json (which has them) "
            "cannot be walked without enumerating sealed holdout ids. A "
            "retried case's latency_s is its LAST attempt's, excluding the "
            "failed attempts and the backoff between them."),
        "repeats": (
            "k = {}. At k > 1 the case-level trajectory.json is the "
            "REPRESENTATIVE repeat's; the other repeats' trajectories are "
            "scored in a scratch directory that is deleted at the end of the "
            "case, so cost here is per-case-representative and the run spent "
            "roughly k times it.".format(", ".join(str(k) for k in ks))),
        "holdout": (
            "totals are over VISIBLE cases only. results.json.cases carries "
            "no holdout row, so a holdout run spent more than these numbers "
            "say; holdout_excluded is the count of cases the gap is."),
        "list_price": (
            "a dollar figure here is the DECLARED table's list price over the "
            "tokens the trace reported. It is not an invoice: negotiated "
            "rates, provider discounts, batch tiers and any cache count left "
            "unsplit (cache_accounting other than 'priced') "
            "are all outside it."),
    }


def main():
    ap = argparse.ArgumentParser(
        description="Cost and latency for one evalup run, or a paired "
                    "diff of two. Reports; decides nothing.")
    add_version_flag(ap)
    ap.add_argument("run", help="the run directory to report on")
    ap.add_argument("--baseline",
                    help="a second run directory; adds the paired block")
    ap.add_argument("--prices",
                    help="the declared price-table sidecar (see the module "
                         "docstring). Without it, tokens are reported and "
                         "dollars are not.")
    ap.add_argument("--alpha", type=float, default=0.05,
                    help="two-sided level for the sign test and the coverage "
                         "of the median interval; it gates no decision "
                         "(default: 0.05)")
    ap.add_argument("--max-exact-states", type=int, default=MAX_EXACT_STATES,
                    help="decline the exact permutation test above this many "
                         "distinct subset sums rather than approximate it "
                         f"(default: {MAX_EXACT_STATES})")
    ap.add_argument("-o", "--out", help="write JSON here instead of stdout")
    a = ap.parse_args()
    # After parse_args, not an argparse `type=`: argparse writes usage text to
    # STDERR with no JSON payload (see _common.require_range).
    require_range("--alpha", a.alpha, 0, 1, exclusive=True)
    require_range("--max-exact-states", a.max_exact_states, lo=1)

    prices = load_prices(a.prices) if a.prices else None
    cand = load_run(a.run)
    cand_cost, cand_block = price_run(
        cand["dir"], cand["cases"], prices,
        cache_counts_are_parts(cand["manifest"]))

    out = {
        "harness_version": HARNESS_VERSION,
        "run": cand["meta"],
        "latency": latency_block(cand["cases"]),
        "cost": cand_block,
    }
    runs = [cand]
    if a.baseline:
        base = load_run(a.baseline)
        runs.append(base)
        base_cost, base_block = price_run(
            base["dir"], base["cases"], prices,
            cache_counts_are_parts(base["manifest"]))
        out["baseline"] = {"run": base["meta"], "cost": base_block,
                           "latency": latency_block(base["cases"])}
        out["paired"] = compare(base, cand, base_cost, cand_cost, a)
    out["alpha"] = a.alpha
    out["declared_limits"] = declared_limits(runs)
    out["not_a_decision"] = (
        "stats.py owns the one definition of `better` in this package and "
        "cost is not in it. Nothing here is a gate, a threshold or a budget; "
        "read these numbers beside stats.py's verdict and make the trade "
        "yourself.")
    out["declined"] = (
        "The 2026-08 review SS4.3 recommends a bootstrap. A bootstrap is a "
        "Monte Carlo approximation whose answer moves with B and the seed, "
        "and stats.py's standing rule is that no approximation appears in a "
        "decision or a reported test. The exact sign test, the exact "
        "distribution-free median interval and the exact paired permutation "
        "test above replace it, and the interval it was wanted for exists in "
        "closed form.")
    write_output(a.out, json.dumps(out, indent=2) + "\n")


if __name__ == "__main__":
    main()
