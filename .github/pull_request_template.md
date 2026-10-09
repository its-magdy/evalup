## What and why

<!-- What changes, and the reason. The commit messages carry the reasoning
     long-term, so say it there too. -->

## Checks

<!-- evalup/CONTRIBUTING.md, "The three checks". CI runs them too; tick what
     you ran locally. -->

- [ ] `python3 -m unittest discover -s tests` (from `evalup/`)
- [ ] `ruff check --config ruff.toml .`
- [ ] The 3.9 floor: `uv run --python 3.9 --with pytest --with pytest-subtests python -m pytest tests -q`

## Scope

- [ ] Does not change what a scorer's number means, and adds no layer (the
      runner and scorers take bugfixes only)
- [ ] `evalup/CHANGELOG.md` updated if users would notice the change
