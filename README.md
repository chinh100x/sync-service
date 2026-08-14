# sync-service

A production -> open source sync service. A commit to the production repo's
default branch scrubs production-specific detail, confirms the OSS repo still
installs and runs with the change applied, and opens a PR — nothing
auto-merges.

**No divergence detection** — a run always overwrites the OSS repo's tracked
files with production's current content. If the OSS side has its own edits
outside this tool, they're overwritten with no warning. That's a deliberate
tradeoff (simplicity over conflict-tracking), not a bug.

*OSS -> production (the reverse direction) isn't implemented yet — planned for later.*

This directory is a standalone project. It doesn't inherit CI/build
conventions from any other repo.

## Layout

```
src/sync_service/
├── cli.py          entrypoint: `sync-service run --config ... --source-repo ... --dest-repo ... --base ... --head ...`
├── config.py       pydantic schema for the sync/*.yaml mapping config
├── diff.py         which mappings did base..head touch (the trigger)
├── scrub.py        exclude list + regex substitution (redact)
├── secretscan.py   built-in secret-scan gate — swap for `gitleaks` before deploying against a real repo (see below)
├── breakcheck.py   runs break_check.install / .run before a PR is opened (the break check)
├── llm_client.py   shared OpenAI structured-output call used by pr_writer.py/safety_review.py
├── pr_writer.py    optional LLM-written human-readable PR title/body. Off by default; deterministic fallback always available.
├── safety_review.py  optional LLM semantic safety review. Off by default; fails *closed* (halts) on any error — a security gate, not cosmetic.
├── publish.py      branch + commit + `gh pr create` (no-op detection: nothing to commit -> no PR)
├── notify.py       comment on the source commit + Slack post (if SLACK_WEBHOOK_URL is set) on every halt/error and PR opened
└── slack.py        best-effort Slack Incoming Webhook post; never raises, off unless configured
tests/unit/         one test file per module
tests/integration/  full end-to-end scenarios through cli.main() against two local git repos — no GitHub needed
action.yml          composite GitHub Action wrapping the CLI, for real deployment
Makefile            `make install` / `make lint` / `make test` / `make check` (lint + test) — same targets CI runs
```

## Try it locally

### 1. One-time setup

```bash
make install
```
Creates `.venv/` and installs `pydantic`, `pyyaml`, `pytest`, `ruff`. No GitHub token, no `gitleaks` binary, no real repos needed for any of this.

### 2. Lint and run the tests

```bash
make check   # make lint + make test
```
Lint is `ruff` (unused imports/names, undefined names — not layered with style rules this codebase doesn't follow). Tests: 82 total, no GitHub involved — unit tests per module in `tests/unit/`, plus `tests/integration/` for full runs through `cli.main()` (multiple mappings in one commit, the overwrite behavior, a break-check halt/revert/retry cycle, and a no-op run). CI (`.github/workflows/ci.yml`) runs the same `make check` on every push/PR.

### 3. See one end-to-end scenario narrated, with output

```bash
uv run pytest tests/integration/ -v -s
```
`-s` shows each scenario's real `cli.main()` output (scrubbing, redaction, PR dry-run text) instead of just pass/fail.

### 4. Drive the CLI yourself, one command at a time

Point it at any two local git repos you control:

```bash
uv run sync-service run \
  --config path/to/sync/monitoring.yaml \
  --source-repo /path/to/a/prod-checkout \
  --dest-repo /path/to/an/oss-checkout \
  --base <sha-before> --head <sha-after>
```

With no `git remote` configured, `open_pr` just prints the PR it would have opened (dry-run) instead of pushing/creating one for real.

## Deploying against real repos

### 0. Prerequisites

- `gh` CLI on the runner — already preinstalled on GitHub-hosted `ubuntu-latest` runners.
- This code hosted as its own repo (e.g. `you/sync-service`) so a workflow can reference it as `uses: you/sync-service@main`.

### 1. Auth: a token for writing to the OSS repo

Don't use a personal token. Either works:

- **GitHub App** (preferred) — scoped to just the OSS repo, with `contents:write` + `pull_requests:write`. Generate an installation token in the workflow (e.g. via `actions/create-github-app-token`).
- **Fine-scoped PAT** — same two permissions, stored as an org/repo Actions secret, not tied to one person's account.

The token needs write access to the **OSS** repo only — the production repo's own default `GITHUB_TOKEN` covers everything else (reading the diff, commenting on the source commit).

### 2. Add the workflow to the production repo

```yaml
# .github/workflows/sync.yaml, in the production repo
name: sync-service
on:
  push:
    branches: [main]
    paths: ["src/portmon/**", ".github/workflows/sync.yaml"]

jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0   # need base..head history, not just the tip

      - uses: you/sync-service@main
        with:
          config: |
            mappings:
              - key: portmon
                source: src/portmon
                dest: plugin
                break_check:
                  install: "pip install -e ."
                  run: "portmon run --deal demo-01"
          target-repo: you-oss/portfolio-monitoring   # the OSS repo
          target-branch: main
          target-token: ${{ secrets.SYNC_SERVICE_DEST_TOKEN }}
          base: ${{ github.event.before }}
          head: ${{ github.sha }}
```

Two things worth knowing about `base`/`head`:

- `github.event.before` is all-zeros on a branch's first-ever push (no prior commit to diff against) — handle that edge case (e.g. fall back to `HEAD~1`) if you ever point this at a newly created branch.
- `fetch-depth: 0` is required — `sync_service.diff` runs `git diff base..head`, which needs real history, not the tip-only commit a shallow (default) checkout gives you.

`source`/`dest` are optional in a mapping — omit both to track the whole repo instead of a subdirectory. Anything that shouldn't cross needs its own entry in `exclude` (exact paths only, no wildcards).

### 3. Swap the built-in secret scanner for gitleaks

`src/sync_service/secretscan.py` ships a handful of built-in regexes so local development has zero external dependencies. Real deployment should use [`gitleaks`](https://github.com/gitleaks/gitleaks) instead: write the `desired` dict out to a scratch directory and shell out to `gitleaks detect --no-git --source <dir>`, treating a non-zero exit as a hit (same halt path, just a stronger gate). Do this before pointing the workflow at a repo with real sensitive data in it.

### 4. Optional features

All off by default — enable any combination by adding fields to the mapping config's `config:` block and passing the matching secret through as an input.

**Human-readable PR titles/bodies via an LLM** (`pr_writer.py`) — advisory only, never part of the sync/security decision. Falls back to a plain deterministic title/body (`Sync <mapping> changes`, a bare file list) on any failure or missing key.
```yaml
config: |
  mappings: [...]
  llm_pr:
    enabled: true
openai-api-key: ${{ secrets.OPENAI_API_KEY }}
```
Put `OPENAI_API_KEY` in a GitHub **Environment** (not a plain repo/org secret), and add `environment: production` (or whatever it's named) to the job.

**LLM semantic safety review** (`safety_review.py`) — the opposite failure behavior from the PR writer: this is a security gate, and any failure to get a verdict is a hard halt, no PR (never treated as a pass). Catches what a regex can't: a real customer name, an internal codename, proprietary logic described in a comment.
```yaml
config: |
  mappings: [...]
  llm_safety_review:
    enabled: true
openai-api-key: ${{ secrets.OPENAI_API_KEY }}   # same key/Environment as above
```

**Slack notifications** (`notify.py`/`slack.py`) — best-effort; a missing or broken webhook never affects whether the sync itself succeeds.
```yaml
slack-webhook-url: ${{ secrets.SLACK_WEBHOOK_URL }}
slack-channel: ${{ secrets.SLACK_CHANNEL }}   # optional; only some webhook apps honor a channel override
```

### 5. Roll out order

1. Try it against a throwaway repo pair first (point the CLI at two scratch GitHub repos with a real remote configured, so `open_pr` takes the real `git push` + `gh pr create` path instead of dry-run).
2. Wire the workflow into the real production repo, confirm one real commit produces the PR you expect.

Before deploying against a repo that takes outside contributions on its default branch: this version has no divergence detection, so an outside edit on the OSS side can be silently overwritten by the next sync. Decide explicitly whether that's acceptable, or add a divergence check first.
