"""The plugin as Claude Code loads it: manifest, skills, agents.

Every other test here exercises `scripts/`. None of them ever opened a
SKILL.md or an agent file the way the harness does, which is how
`agents/judge.md` shipped for weeks with its whole frontmatter reflowed into
one paragraph — `--- name: judge description: ...` on a single line — so
`name`, `model` and `tools` never registered and the judge agent did not
exist as far as Claude Code was concerned (2026-09 audit; `claude plugin
validate` reports it as "No frontmatter block found").

Stdlib only, so no YAML parser: these check the frontmatter's SHAPE — the
delimiters on their own lines, the keys at column zero — which is exactly
what broke. `claude plugin validate` is the fuller check; it is not run here
because the suite must pass on a machine without the CLI.
"""
import json
import os
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEY_RE = re.compile(r"^([A-Za-z][\w-]*):")


def frontmatter_keys(path):
    """Top-level keys of a `---`-delimited block, or None when there is no
    block. A delimiter only counts when it is the WHOLE line."""
    lines = path.read_text(encoding="utf-8").split("\n")
    if not lines or lines[0] != "---":
        return None
    try:
        end = lines.index("---", 1)
    except ValueError:
        return None
    return {m.group(1): line[m.end():].strip()
            for line in lines[1:end] for m in [KEY_RE.match(line)] if m}


class TestPluginLayout(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads(
            (ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))

    def test_every_skill_has_parseable_frontmatter(self):
        skills = sorted((ROOT / "skills").glob("*/SKILL.md"))
        self.assertTrue(skills)
        for path in skills:
            with self.subTest(skill=path.parent.name):
                keys = frontmatter_keys(path)
                self.assertIsNotNone(keys, "no frontmatter block")
                self.assertEqual(keys.get("name"), path.parent.name)
                self.assertIn("description", keys)

    def test_every_agent_has_parseable_frontmatter(self):
        agents = sorted((ROOT / "agents").glob("*.md"))
        self.assertTrue(agents)
        for path in agents:
            with self.subTest(agent=path.name):
                keys = frontmatter_keys(path)
                self.assertIsNotNone(keys, "no frontmatter block")
                self.assertEqual(keys.get("name"), path.stem)
                for key in ("description", "model", "tools"):
                    self.assertIn(key, keys)

    def test_no_agent_frontmatter_relies_on_the_plugin_root_placeholder(self):
        # Claude Code documents ${CLAUDE_PLUGIN_ROOT} substitution for the
        # Markdown BODY of an agent, not its frontmatter, and `description`
        # is what the delegating model reads. judge.md carried the literal
        # placeholder there (2026-09-30 docs check).
        for path in sorted((ROOT / "agents").glob("*.md")):
            with self.subTest(agent=path.name):
                lines = path.read_text(encoding="utf-8").split("\n")
                block = "\n".join(lines[1:lines.index("---", 1)])
                self.assertNotIn("CLAUDE_PLUGIN_ROOT", block)

    def test_manifest_lists_exactly_the_agents_on_disk(self):
        listed = sorted(Path(p).name for p in self.manifest["agents"])
        on_disk = sorted(p.name for p in (ROOT / "agents").glob("*.md"))
        self.assertEqual(listed, on_disk)
        for rel in self.manifest["agents"]:
            self.assertTrue((ROOT / rel).is_file(), rel)

    def test_manifest_skills_path_exists(self):
        self.assertTrue((ROOT / self.manifest["skills"]).is_dir())

    # --- how a skill reaches a script (2026-09-21 audit + live test) ---------

    def test_every_cli_is_executable(self):
        # Git stores the mode, so a 644 script ships as 644 to every install;
        # 17 of 19 did, while the skills invoked them as bare paths.
        for path in sorted((ROOT / "scripts").glob("*.py")):
            if path.name.startswith("_"):
                continue
            with self.subTest(script=path.name):
                self.assertTrue(os.access(path, os.X_OK), "not executable")
                self.assertTrue(path.read_text(encoding="utf-8").startswith(
                    "#!/usr/bin/env python3"))

    def test_skills_invoke_scripts_through_python3(self):
        # `python3 <path>` works whatever the mode bit and on every platform,
        # and it is the form the skills' allowed-tools rule pre-approves.
        bare = re.compile(r"(?<!python3 )(?<!python3 \")\$\{CLAUDE_PLUGIN_ROOT\}"
                          r"/scripts/[a-z_]+\.py (?:<|-|\[|reports)")
        docs = [*(ROOT / "skills").rglob("*.md"), *(ROOT / "agents").glob("*.md")]
        for path in docs:
            with self.subTest(doc=str(path.relative_to(ROOT))):
                self.assertEqual(bare.findall(path.read_text(encoding="utf-8")),
                                 [])

    def test_every_skill_pre_approves_the_plugins_own_scripts(self):
        # Without this every script call is a permission prompt (~13 before a
        # first score). Both spellings are needed: a model quotes the path when
        # it holds a space, and a live session showed an unquoted-only rule
        # matching nothing. Never a blanket `Bash`.
        for path in sorted((ROOT / "skills").glob("*/SKILL.md")):
            with self.subTest(skill=path.parent.name):
                text = path.read_text(encoding="utf-8")
                block = text.split("\n---\n", 1)[0]
                self.assertIn("allowed-tools:", block)
                self.assertIn("Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/*)",
                              block)
                self.assertIn('Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/*)',
                              block)
                self.assertNotRegex(block, r"allowed-tools:.*\bBash\b(?!\()")

    def test_a_skill_that_loads_another_pre_approves_it(self):
        # Loading a skill that carries its own `allowed-tools` needs approval.
        # Headless, that is a silent denial: `start` could not load `discover`
        # and carried on by reading the file by hand (live test, 2026-09-21).
        # A `Skill(evalup:<name>)` rule on the loader is what was shown to fix
        # it. `optimize` is never listed: only the user may launch it.
        loads = {"start": ("discover", "generate", "run", "analyze"),
                 "help": ("start",)}
        for loader, loaded in loads.items():
            block = (ROOT / "skills" / loader / "SKILL.md").read_text(
                encoding="utf-8").split("\n---\n", 1)[0]
            for name in loaded:
                with self.subTest(loader=loader, loaded=name):
                    self.assertIn(f"Skill(evalup:{name})", block)
            self.assertNotIn("Skill(evalup:optimize)", block)


if __name__ == "__main__":
    unittest.main()
