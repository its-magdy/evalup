"""The first session's context budget (#13): at most ~25k tokens of plugin text.

A first `/evalup:start` runs start -> discover -> generate -> run in one
session, and every file those skills send Claude to lands in the main
context. On 2026-11-19 that was ~34k tokens before a single tool result,
so the budget is now a test rather than a hope.

The cap covers the single-turn first session, the path #12 keeps stop-free:
lean start -> discover -> generate -> a smoke run. It does not cover every
session. `conversations.md` is CONDITIONAL by design (#13 item 1): an app
that keeps conversations pays for it on top, and so does any path that meets
a CONDITIONAL file's condition.

What is counted (FIRST_SESSION): the four SKILL.md files, whole, plus the
references they send Claude to unconditionally on that path. What is not
(CONDITIONAL): every other plugin file they or those references point at,
each with the condition under which it is read -- a mode other than smoke,
an adapter that declares conversations, a user who asks what a term means,
a step lean mode defers, or a file read in a subagent's own context (the
test-generator agent and the case-format re-read it does there).

How the list stays honest: the test collects every `*.md` name the four
skills and the counted references mention -- `${CLAUDE_PLUGIN_ROOT}/...`
paths, relative links and bare file names -- and fails when one resolves to
a plugin file in neither list. So a new reference cannot join the path
without being counted or given a stated condition. The scan stops one level
below the skills, at the counted references: the files those point at are
looked up for one field, not read through. A name that is not a plugin file
must be one of the app's own (APP_FILES), so a typo fails too.

Tokens are measured as words x 1.33, never lines or bytes: the plugin's
prose is long-lined YAML comments and tables, where line counts lie.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUDGET_TOKENS = 25_000
TOKENS_PER_WORD = 1.33

SKILLS = tuple(f"skills/{name}/SKILL.md"
               for name in ("start", "discover", "generate", "run"))

# Read on every first session: the reason is the step that reads it.
FIRST_SESSION = {
    "skills/discover/references/adapter-contract.md":
        "discover writes adapter.yaml from it",
    "skills/discover/references/profile-schema.md":
        "discover writes profile.yaml from it; start states the stage from it",
    "skills/discover/references/topology-detection.md":
        "discover step 2's signal catalogue",
    "skills/generate/references/case-format.md":
        "generate's case schema and the validator's error/warning line",
    "skills/generate/references/suite-sizing.md":
        "generate's quotas and the metadata caveat",
}

# Pointed at from the path, read only when the condition holds.
CONDITIONAL = {
    "skills/discover/references/rerun.md":
        "a later discover: an existing profile.yaml, or --diff",
    "skills/generate/references/generation-method.md":
        "rationale: read only when a rule is challenged",
    "skills/generate/references/conversations.md":
        "only when the adapter declares invocation.conversation",
    "skills/run/references/run-modes.md":
        "before a run in any mode but smoke",
    "skills/run/references/ci-gate.md": "wiring a run into CI",
    "skills/run/references/judge-pass.md":
        "after a run in which a selected case pins a rubric",
    "skills/generate/references/label-lifecycle.md":
        "after review: a wrong accepted label, a bump, a re-pin, a bulk "
        "accept, an owner not in this session, choosing canaries, sealing "
        "more after a look",
    "skills/discover/references/oracle.md":
        "an oracle offer (discover step 5), an expect.result case, or a "
        "seeded fixture built by code (generate §2b's dump)",
    "skills/discover/references/interview.md":
        "discover step 7, deferred on a lean first session; the label "
        "review's rule confirmations, after the first result",
    "skills/discover/references/adapters/dotnet.md": "a .NET app only",
    "skills/discover/references/adapters/exec.md":
        "an app driven through invocation.mode: exec only",
    "docs/concepts.md": "only when the user asks what a term means",
    "docs/runner-contract.md": "the runner's spec, read for a specific rule",
    "docs/multi-turn.md": "conversations only",
    "docs/rubric-format.md":
        "rubrics are authored by the test-generator subagent, in its own "
        "context",
    "docs/traces-jaeger.md": "a Jaeger trace store only",
    "agents/test-generator.md": "read by the subagent, in its own context",
}

# Names the skills mention that are the app's or the state location's own
# files, never the plugin's.
APP_FILES = {"findings.md", "report.md", "prompts/router.md",
             # the in-repo business docs discover step 3 reads (#25)
             "GLOSSARY.md", "CONTEXT.md", "AGENTS.md"}

MD_NAME = re.compile(r"(\$\{CLAUDE_PLUGIN_ROOT\}/)?([\w./-]*[\w-]\.md)\b")


def plugin_markdown():
    files = [*ROOT.joinpath("skills").rglob("*.md"),
             *ROOT.joinpath("docs").rglob("*.md"),
             *ROOT.joinpath("agents").glob("*.md")]
    return {p.relative_to(ROOT).as_posix() for p in files}


def mentioned(rel, known):
    """Plugin files `rel` names, and the names that resolve to none."""
    path = ROOT / rel
    by_basename = {}
    for k in known:
        by_basename.setdefault(k.rsplit("/", 1)[-1], []).append(k)
    found, unresolved = set(), set()
    for m in MD_NAME.finditer(path.read_text(encoding="utf-8")):
        rooted, name = m.groups()
        name = name.lstrip("/")
        if rooted:
            candidates = [name]
        else:
            here = path.parent.relative_to(ROOT).as_posix()
            skill_dir = "/".join(here.split("/")[:2])
            candidates = [f"{here}/{name}", f"{skill_dir}/{name}",
                          f"skills/{name}", name]
            candidates = [str(Path(c)) for c in candidates]
        hit = next((c for c in candidates if c in known), None)
        if hit is None and not rooted:
            same = by_basename.get(name.rsplit("/", 1)[-1], [])
            hit = same[0] if len(same) == 1 else None
        if hit is not None:
            found.add(hit)
        elif name not in APP_FILES:
            unresolved.add(name)
    return found, unresolved


def tokens(rel):
    return len((ROOT / rel).read_text(encoding="utf-8").split()) \
        * TOKENS_PER_WORD


class TestFirstSessionBudget(unittest.TestCase):
    def test_every_listed_file_exists(self):
        for rel in (*SKILLS, *FIRST_SESSION, *CONDITIONAL):
            with self.subTest(file=rel):
                self.assertTrue((ROOT / rel).is_file())

    def test_every_file_on_the_path_is_counted_or_conditional(self):
        known = plugin_markdown()
        scanned = (*SKILLS, *FIRST_SESSION)
        reached = set()
        for rel in scanned:
            found, unresolved = mentioned(rel, known)
            reached |= found
            with self.subTest(file=rel):
                self.assertEqual(unresolved, set(),
                                 "names no plugin file and is not in "
                                 "APP_FILES")
                stray = found - set(SKILLS) - set(FIRST_SESSION) \
                    - set(CONDITIONAL)
                self.assertEqual(stray, set(),
                                 "a reference on the first-session path "
                                 "must be counted (FIRST_SESSION) or given "
                                 "its condition (CONDITIONAL)")
        # Neither list may keep an entry nothing on the path points at.
        self.assertEqual(set(FIRST_SESSION) - reached, set())
        self.assertEqual(set(CONDITIONAL) - reached, set())

    def test_first_session_load_is_within_budget(self):
        load = {rel: round(tokens(rel)) for rel in (*SKILLS, *FIRST_SESSION)}
        total = sum(load.values())
        self.assertLessEqual(
            total, BUDGET_TOKENS,
            "first-session plugin text is over budget; largest first: "
            + ", ".join(f"{k} {v}" for k, v in
                        sorted(load.items(), key=lambda kv: -kv[1])))


if __name__ == "__main__":
    unittest.main()
