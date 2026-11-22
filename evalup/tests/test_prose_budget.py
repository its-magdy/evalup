"""The prose budget (#42 item 3): the two long documents stay short.

`docs/runner-contract.md` holds the runner's rules only; why a rule exists
goes in the commit message that makes it, and git history carries it. On
2026-11-22 it was trimmed from ~19.8k tokens to ~14.3k.

`CHANGELOG.md` says what each release contains, briefly. A released
section is capped; `[Unreleased]` is not, because it collects entries
between releases, and the release commit condenses it under the cap when
it becomes the dated section (CONTRIBUTING.md, "A release is one commit").
The long-form pre-release notes are at the v0.1.0 tag.

Tokens are counted as in test_context_budget.py: words x 1.33.
"""
import re
import unittest
from pathlib import Path

from test_context_budget import tokens

ROOT = Path(__file__).resolve().parent.parent

CONTRACT = "docs/runner-contract.md"
CONTRACT_BUDGET_TOKENS = 15_000
RELEASE_SECTION_BUDGET_TOKENS = 2_000

SECTION = re.compile(r"^## \[([^\]]+)\]", re.MULTILINE)


def changelog_sections():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    heads = list(SECTION.finditer(text))
    return {m.group(1): text[m.start():heads[i + 1].start()
                             if i + 1 < len(heads) else len(text)]
            for i, m in enumerate(heads)}


class TestProseBudget(unittest.TestCase):
    def test_runner_contract_is_within_budget(self):
        self.assertLessEqual(
            round(tokens(CONTRACT)), CONTRACT_BUDGET_TOKENS,
            "the runner contract holds rules; move the rationale into the "
            "commit message")

    def test_changelog_keeps_an_unreleased_section_first(self):
        names = list(changelog_sections())
        self.assertEqual(names[0], "Unreleased")
        self.assertGreater(len(names), 1, "no released section")

    def test_each_released_section_is_within_budget(self):
        for name, body in changelog_sections().items():
            if name == "Unreleased":
                continue
            with self.subTest(release=name):
                self.assertLessEqual(
                    round(len(body.split()) * 1.33),
                    RELEASE_SECTION_BUDGET_TOKENS,
                    "a released CHANGELOG section says what the release "
                    "contains, briefly; the detail is in git history")


if __name__ == "__main__":
    unittest.main()
