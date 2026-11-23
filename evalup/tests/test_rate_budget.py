#!/usr/bin/env python3
"""Tests for the declared rate budget, adapter.invocation.rate_limit (#76).

The budget paces every app call the runner makes, retries included, so a
rate-limited app never sees more than N calls in a window. With no budget
declared, nothing the runner sends or writes changes.

Run: python3 -m unittest discover -s tests -v   (from the plugin root)
"""
import contextlib
import json
import pathlib
import random
import shlex
import sys
import threading
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_run_cases import (  # noqa: E402 - the shared fixtures
    SCRIPTS,
    FakeApp,
    RunnerCase,
    make_case,
    make_plan,
)

sys.path.insert(0, str(SCRIPTS))
import run_cases  # noqa: E402


class FakeClock:
    """A clock that moves only when the budget sleeps or a call takes time."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        assert seconds > 0
        self.now += seconds


def most_in_any_window(times, window):
    """The largest number of `times` inside one half-open window
    [t, t + window), over every t: checking each time as a window's start is
    enough, since sliding a window's start up to its first member loses none
    of them."""
    times = sorted(times)
    return max((sum(1 for u in times if t <= u < t + window) for t in times),
               default=0)


class TestRateBudgetUnit(unittest.TestCase):
    def drive(self, spec, calls, seed):
        """`calls` calls with random latencies through one budget. Returns
        (start, finish) per call: the app receives each call somewhere in
        between, so both ends are the worst cases for a window count."""
        clock, rng = FakeClock(), random.Random(seed)
        budget = run_cases.RateBudget(spec, clock=clock, sleep=clock.sleep)
        spans = []

        def send():
            start = clock.now
            clock.now += rng.choice([0.0, 0.01, 0.3, 2.0, 7.5])
            spans.append((start, clock.now))
            if rng.random() < 0.3:      # a failed call counts too
                raise run_cases.TransportError("timeout", "timed out")
            return "ok"
        for _ in range(calls):
            with contextlib.suppress(run_cases.TransportError):
                budget.call(send)
            clock.now += rng.choice([0.0, 0.0, 0.5, 3.0])   # scoring, waits
        return spans

    def test_no_window_receives_more_than_n(self):
        for seed in range(25):
            with self.subTest(seed=seed):
                spans = self.drive({"requests": 4, "window_s": 10}, 60, seed)
                self.assertEqual(len(spans), 60)
                # Whenever in its span each call reached the app.
                for pick in (0, 1):
                    seen = [span[pick] for span in spans]
                    self.assertLessEqual(most_in_any_window(seen, 10), 4)
                # The rule itself: call i+N starts a whole window after
                # call i FINISHED, so latency cannot squeeze the gap.
                for i in range(len(spans) - 4):
                    self.assertGreaterEqual(spans[i + 4][0] - spans[i][1],
                                            10 - 1e-9)

    def test_min_interval_runs_from_the_previous_finish(self):
        for seed in range(10):
            with self.subTest(seed=seed):
                spans = self.drive({"min_interval_s": 3}, 30, seed)
                for before, after in zip(spans, spans[1:]):
                    self.assertGreaterEqual(after[0] - before[1], 3 - 1e-9)

    def test_both_forms_hold_at_once(self):
        spans = self.drive({"min_interval_s": 1, "requests": 3,
                            "window_s": 20}, 40, seed=7)
        self.assertLessEqual(most_in_any_window([s for s, _ in spans], 20), 3)
        for before, after in zip(spans, spans[1:]):
            self.assertGreaterEqual(after[0] - before[1], 1 - 1e-9)

    def test_the_first_call_never_waits(self):
        clock = FakeClock()
        budget = run_cases.RateBudget({"requests": 1, "window_s": 600},
                                      clock=clock, sleep=clock.sleep)
        self.assertEqual(budget.call(lambda: "x"), "x")
        self.assertEqual(clock.now, 1000.0)
        self.assertEqual(budget.wait_s(), 600)

    def test_floor(self):
        budget = run_cases.RateBudget({"requests": 20, "window_s": 600})
        # 20 fit in the first window; call 21 waits one window.
        self.assertEqual(budget.floor_s(20), 0)
        self.assertEqual(budget.floor_s(21), 600)
        self.assertEqual(budget.floor_s(60), 1200)
        self.assertEqual(budget.floor_s(61), 1800)
        both = run_cases.RateBudget({"min_interval_s": 40, "requests": 20,
                                     "window_s": 600})
        self.assertEqual(both.floor_s(21), 800)     # 20 gaps of 40 s
        self.assertEqual(both.floor_s(1), 0)
        # A window, then 4 gaps of 2 s inside the next one.
        short = {"min_interval_s": 2, "requests": 20, "window_s": 600}
        self.assertEqual(run_cases.RateBudget(short).floor_s(25), 608)

    def test_floor_is_what_the_budget_does(self):
        """floor_s is the zero-latency schedule the budget itself keeps."""
        for spec in ({"min_interval_s": 2, "requests": 20, "window_s": 600},
                     {"min_interval_s": 40, "requests": 20, "window_s": 600},
                     {"requests": 3, "window_s": 10},
                     {"min_interval_s": 1.5}):
            for calls in (1, 2, 3, 4, 7, 21, 25, 41):
                with self.subTest(spec=spec, calls=calls):
                    clock = FakeClock()
                    budget = run_cases.RateBudget(spec, clock=clock,
                                                  sleep=clock.sleep)
                    starts = []
                    for _ in range(calls):
                        budget.call(lambda s=starts, c=clock:
                                    s.append(c.now))
                    self.assertAlmostEqual(starts[-1] - starts[0],
                                           budget.floor_s(calls))


class TestRateLimitValidation(unittest.TestCase):
    def assert_refused(self, block, fragment):
        with self.assertRaises(run_cases.RunnerExit) as ctx:
            run_cases.validate_rate_limit(block)
        self.assertEqual(ctx.exception.code, run_cases.EXIT_INPUT)
        self.assertIn(fragment, ctx.exception.message)

    def test_accepted(self):
        for block in (None, {"min_interval_s": 2},
                      {"requests": 20, "window_s": 600},
                      {"min_interval_s": 0.5, "requests": 1, "window_s": 1}):
            with self.subTest(block=block):
                run_cases.validate_rate_limit(block)

    def test_refused(self):
        cases = [
            ("20/10m", "must be an object"),
            ({}, "declares no budget"),
            ({"requests": None}, "declares no budget"),
            ({"per_minute": 5}, "unknown key(s)"),
            ({"min_interval_s": 0}, "min_interval_s must be"),
            ({"min_interval_s": -1}, "min_interval_s must be"),
            ({"min_interval_s": True}, "min_interval_s must be"),
            ({"min_interval_s": float("inf")}, "min_interval_s must be"),
            ({"requests": 1, "window_s": 1e9}, "window_s must be"),
            ({"requests": 20}, "go together"),
            ({"window_s": 600}, "go together"),
            ({"requests": 0, "window_s": 600}, "requests must be"),
            ({"requests": 2.5, "window_s": 600}, "requests must be"),
            ({"requests": 20, "window_s": "600"}, "window_s must be"),
        ]
        for block, fragment in cases:
            with self.subTest(block=block):
                self.assert_refused(block, fragment)


class ArrivalLog:
    """A responder that records when each request arrived and answers 429
    to every third chat request, so retries are part of the traffic."""

    def __init__(self, every=3):
        self.arrivals, self.every, self.asks = [], every, 0
        self.lock = threading.Lock()

    def __call__(self, path, body, headers):
        with self.lock:
            self.arrivals.append(time.monotonic())
            if path == "/":
                return 200, {"message": "up"}, {}
            self.asks += 1
            if self.every and self.asks % self.every == 0:
                return 429, {"error": "slow down"}, {}
        return 200, {"message": "ok"}, {}


class TestPacedRun(RunnerCase):
    """The runner as a subprocess against a real HTTP server."""

    def run_with(self, responder, rate_limit, cases=4, extra_argv=(),
                 declare_null=False):
        app = FakeApp(responder)
        self.addCleanup(app.close)
        plan = make_plan(self.state, app.base_url,
                         cases=[make_case(f"c-{i:04d}") for i in range(cases)])
        plan["execution"]["max_attempts"] = 3
        plan["execution"]["backoff_s"] = [0, 0]
        if rate_limit is not None or declare_null:
            plan["adapter"]["invocation"]["rate_limit"] = rate_limit
        rc, payload, proc = self.invoke(plan, extra_argv=extra_argv)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        return payload

    def run_log(self):
        path = self.out_dir() / "run.log"
        return [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines()]

    def test_no_window_receives_more_than_n_retries_included(self):
        app = ArrivalLog()
        self.run_with(app, {"requests": 2, "window_s": 0.5}, cases=5)
        # Health check + 5 cases, asks 3 and 6 a 429 retried: 8 calls.
        self.assertEqual(len(app.arrivals), 1 + 5 + 2)
        self.assertLessEqual(most_in_any_window(app.arrivals, 0.5), 2)
        retries = [r for r in self.run_log() if r["event"] == "retry"]
        self.assertEqual(len(retries), 2)
        self.assertTrue(any(r["event"] == "pace" for r in self.run_log()))
        manifest = self.read("manifest.yaml")
        self.assertEqual(manifest["invocation"]["rate_limit"],
                         {"requests": 2, "window_s": 0.5})
        # Pacing is transport: the verdicts are the unpaced run's.
        self.assertEqual({v["verdict"] for v in
                          self.jsonl("verdicts_for_stats.jsonl")}, {"pass"})

    def test_a_conversation_restart_is_paced(self):
        """A 429 on a turn restarts the whole conversation (multi-turn.md
        SS3); every resent turn is an attempt the budget counts."""
        app = ArrivalLog()
        server = FakeApp(app)
        self.addCleanup(server.close)
        cases = []
        for i in range(2):
            case = make_case(f"c-convo-{i:02d}")
            case["input"] = {"turns": [{"user": "first"},
                                       {"user": "second"}]}
            cases.append(case)
        plan = make_plan(self.state, server.base_url, cases=cases)
        plan["execution"]["max_attempts"] = 3
        plan["execution"]["backoff_s"] = [0, 0]
        plan["adapter"]["invocation"]["conversation"] = {"style": "client-id"}
        plan["adapter"]["invocation"]["rate_limit"] = {"requests": 2,
                                                       "window_s": 0.5}
        rc, _, proc = self.invoke(plan)
        self.assertEqual(rc, 0, proc.stdout + proc.stderr)
        # Health check, 2 turns, then a 429 on turn 1 and both turns again.
        self.assertEqual(len(app.arrivals), 1 + 2 + 1 + 2)
        self.assertLessEqual(most_in_any_window(app.arrivals, 0.5), 2)
        self.assertTrue(any(r["event"] == "retry"
                            and r.get("restart") == "conversation"
                            for r in self.run_log()))

    def test_min_interval(self):
        app = ArrivalLog(every=0)
        self.run_with(app, {"min_interval_s": 0.2}, cases=3)
        gaps = [b - a for a, b in zip(app.arrivals, app.arrivals[1:])]
        self.assertEqual(len(gaps), 3)
        self.assertGreaterEqual(min(gaps), 0.2)

    def test_no_budget_writes_what_it_always_did(self):
        app = ArrivalLog()
        self.run_with(app, None, cases=5)
        self.assertEqual(len(app.arrivals), 8)
        self.assertFalse(any(r["event"] == "pace" for r in self.run_log()))
        manifest = self.read("manifest.yaml")
        self.assertEqual(sorted(manifest["invocation"]),
                         ["mode", "response_format", "sse", "timeout_enforced",
                          "timeout_s", "url"])

    def test_a_null_block_is_no_budget(self):
        app = ArrivalLog()
        self.run_with(app, None, cases=5, declare_null=True)
        self.assertEqual(len(app.arrivals), 8)
        self.assertNotIn("rate_limit",
                         self.read("manifest.yaml")["invocation"])

    def test_dry_run_states_the_pace(self):
        payload = self.run_with(ArrivalLog(), {"requests": 2, "window_s": 600},
                                cases=5, extra_argv=["--dry-run"])
        self.assertEqual(payload["app_calls_planned"], 5)
        self.assertEqual(payload["app_calls_max"], 15)
        self.assertEqual(payload["pace_floor_s"], 1200)    # calls 3 and 5
        self.assertEqual(payload["pace_max_s"], 7 * 600)
        self.assertEqual(payload["rate_limit"],
                         {"requests": 2, "window_s": 600})
        self.assertIn("retries included", payload["pace_note"])
        # #144: 1200 s fits under the 30-minute background cap.
        self.assertNotIn("resume_expected", payload)

    def test_a_pace_at_the_background_cap_says_a_resume_is_coming(self):
        # #144: 7 calls at 2 per 600 s is a 1800 s floor -- the cap itself,
        # and latency adds to it.
        payload = self.run_with(ArrivalLog(), {"requests": 2, "window_s": 600},
                                cases=7, extra_argv=["--dry-run"])
        self.assertEqual(payload["pace_floor_s"], 1800)
        block = payload["resume_expected"]
        self.assertEqual(block["gap_s"], 600)
        argv = shlex.split(block["command"])
        self.assertEqual(argv[:4], ["evalup", "run-cases", "--plan",
                                    str(self.tmp / "plan.json")])
        self.assertEqual(argv[4:7], ["--out", payload["out"], "--resume"])
        self.assertEqual(argv[7], "--not-before")
        self.assertNotIn("--baseline-verdicts", argv)
        self.assertIn("no new holdout look", block["note"])
        self.assertIn("600 s", block["note"])

    def test_dry_run_without_a_budget_is_unchanged(self):
        payload = self.run_with(ArrivalLog(), None, extra_argv=["--dry-run"])
        for key in ("rate_limit", "pace_floor_s", "pace_max_s", "pace_note"):
            self.assertNotIn(key, payload)

    def test_a_malformed_budget_is_exit_2_before_any_call(self):
        app = ArrivalLog()
        server = FakeApp(app)
        self.addCleanup(server.close)
        plan = make_plan(self.state, server.base_url)
        plan["adapter"]["invocation"]["rate_limit"] = {"requests": 20}
        rc, payload, proc = self.invoke(plan)
        self.assertEqual(rc, 2, proc.stdout + proc.stderr)
        self.assertIn("go together", payload["error"])
        self.assertEqual(app.arrivals, [])


if __name__ == "__main__":
    unittest.main()
