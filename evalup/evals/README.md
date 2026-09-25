# evalup plugin evals

Five `claude plugin eval` cases, one per behaviour the 2026-09-24 field test
found weakest. Each grader's body names the field-test issue it came from.

```sh
# from the repository root; the two doc-only cases need no tool grant
claude plugin eval ./evalup --ablation none --runs 1 --case help-matches-contract
claude plugin eval ./evalup --ablation none --runs 1 --case run-ci-gate-auth-check

# the three app-bound cases build ./app in the run's workspace and start the
# demo server on 127.0.0.1:18731 from their fixture.sh (--scaffold runs it as
# you); they need Bash, Write and Edit granted on the command line
claude plugin eval ./evalup --ablation none --runs 1 --scaffold \
  --allow-tools 'Bash(python3 *)' 'Bash(git -C *)' 'Bash(curl *)' Write Edit \
  --case 'start-headless-completes'          # or discover-stays-in-app-path, generate-no-self-review
```

`fixtures/` holds the demo server (the quickstart app as a standalone script),
its README, the planted `findings.md` and the shared scaffold. Results land in
`results/` (ignored). A case's `allowed_tools` cannot grant Bash — only the
command line can — so the app-bound cases run with a wider Bash grant than the
README's headless recipe; the recipe itself is proven by running it.
