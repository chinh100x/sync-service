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
conventions from any other repo, but does follow the org-wide
[100x Engineering Standards](#contributing) (DAP-273).

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
├── pr_writer.py    LLM-written human-readable PR title/body. On by default; deterministic fallback on any failure or missing key.
├── safety_review.py  optional LLM semantic safety review. Off by default; fails *closed* (halts) on any error — a security gate, not cosmetic.
├── publish.py      branch + commit + `gh pr create` (no-op detection: nothing to commit -> no PR)
├── notify.py       comment on the source commit + Slack post (if SLACK_WEBHOOK_URL is set) on every halt/error and PR opened
└── slack.py        best-effort Slack Incoming Webhook post; never raises, off unless configured
tests/unit/         one test file per module
tests/integration/  full end-to-end scenarios through cli.main() against two local git repos — no GitHub needed
action.yml          composite GitHub Action wrapping the CLI, for real deployment
Makefile            `make install` / `make lint` / `make format` / `make typecheck` / `make test` / `make check` (lint + typecheck + test) — same stages CI runs, in the same order
```

## Try it locally

### 1. One-time setup

```bash
make install
pre-commit install   # optional but recommended -- see below
```
`make install` creates `.venv/` and installs `pydantic`, `pyyaml`, `pytest`, `ruff`, `pyright`. No GitHub token, no `gitleaks` binary, no real repos needed for any of this.

`pre-commit install` wires up `.pre-commit-config.yaml`: on every commit, `ruff format` and an import-sort fix run automatically, plus `detect-secrets` checks the diff against `.secrets.baseline`. Needs [`pre-commit`](https://pre-commit.com/) itself installed once (`uv tool install pre-commit`, or any other way you'd normally install a Python tool). The full lint rule set (unused imports/names, undefined names, bugbear) isn't part of this hook — it's a CI gate (below), not a commit blocker.

### 2. Lint, type-check, and run the tests

```bash
make check   # make lint + make typecheck + make test
```
Lint is `ruff` (`select = ["E", "F", "I", "UP", "B"]`, line-length 100). Type checking is `pyright` in basic mode. Tests: 84 total, no GitHub involved — unit tests per module in `tests/unit/`, plus `tests/integration/` for full runs through `cli.main()` (multiple mappings in one commit, the overwrite behavior, a break-check halt/revert/retry cycle, and a no-op run). CI (`.github/workflows/ci.yml`) runs these same three stages as separate, ordered jobs, plus a fourth security-scan stage (Trufflehog/Trivy/Semgrep) on every push/PR.

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

This is the full setup — every feature the tool has, wired in from the start:

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
                public_reason: "Shared monitoring implementation used by the open-source package."
            llm_safety_review:
              enabled: true
              additional_context: "Also flag covenant threshold values and internal deal codenames."
            project_name: Prod
          target-repo: you-oss/portfolio-monitoring   # the OSS repo
          target-branch: main
          target-token: ${{ secrets.SYNC_SERVICE_DEST_TOKEN }}
          openai-api-key: ${{ secrets.OPENAI_API_KEY }}
          slack-webhook-url: ${{ secrets.SLACK_WEBHOOK_URL }}
          slack-channel: ${{ vars.SLACK_CHANNEL }}
          base: ${{ github.event.before }}
          head: ${{ github.sha }}
```

What each piece does, and what happens if you leave it out:

- **`source`/`dest`** are optional in a mapping — omit both to track the whole repo instead of a subdirectory. Anything that shouldn't cross needs its own entry in `exclude` (exact paths only, no wildcards).
- **`public_reason`** — optional, human-authored line explaining why this mapping propagates; shows up in the PR body.
- **Human-readable PR titles/bodies via an LLM** (`pr_writer.py`) — **on by default**, no config needed to enable it. Advisory only, never part of the sync/security decision: on any failure, timeout, or missing `OPENAI_API_KEY`, it falls back to a plain deterministic title/body (`Sync <mapping> changes`, a bare file list) — OpenAI is never a hard dependency of the sync itself. Set `llm_pr: { enabled: false }` in the config to skip the LLM call outright.
- **`llm_safety_review.enabled`** — **off by default**, shown above as `true`. The opposite failure behavior from the PR writer: this is a security gate, and any failure to get a verdict is a hard halt, no PR, never treated as a pass. Catches what a regex can't — a real customer name, an internal codename, proprietary logic described in a comment. Uses the same `openai-api-key` as the PR writer.
- **`llm_safety_review.additional_context`** — optional, project-specific "also watch for this" text — appended to the reviewer's fixed base prompt, never replacing it, so the built-in invariants (never quote the actual sensitive value, bias toward blocking when uncertain) can't be weakened by config.
- **`project_name`** — optional human-readable label (e.g. `Prod`) used in Slack messages and as the commit author's display name; omit to fall back to a mechanical `label:mapping_key` prefix.
- **`slack-webhook-url`/`slack-channel`** (`notify.py`/`slack.py`) — optional, best-effort; a missing or broken webhook never affects whether the sync itself succeeds. `slack-channel` only matters if the webhook's own Slack app honors a channel override.

Two things worth knowing about `base`/`head`:

- `github.event.before` is all-zeros on a branch's first-ever push (no prior commit to diff against) — handle that edge case (e.g. fall back to `HEAD~1`) if you ever point this at a newly created branch.
- `fetch-depth: 0` is required — `sync_service.diff` runs `git diff base..head`, which needs real history, not the tip-only commit a shallow (default) checkout gives you.

### 3. Swap the built-in secret scanner for gitleaks

`src/sync_service/secretscan.py` ships a handful of built-in regexes so local development has zero external dependencies. Real deployment should use [`gitleaks`](https://github.com/gitleaks/gitleaks) instead: write the `desired` dict out to a scratch directory and shell out to `gitleaks detect --no-git --source <dir>`, treating a non-zero exit as a hit (same halt path, just a stronger gate). Do this before pointing the workflow at a repo with real sensitive data in it.

### 4. Roll out order

1. Try it against a throwaway repo pair first (point the CLI at two scratch GitHub repos with a real remote configured, so `open_pr` takes the real `git push` + `gh pr create` path instead of dry-run).
2. Wire the workflow into the real production repo, confirm one real commit produces the PR you expect.

Before deploying against a repo that takes outside contributions on its default branch: this version has no divergence detection, so an outside edit on the OSS side can be silently overwritten by the next sync. Decide explicitly whether that's acceptable, or add a divergence check first.

## Setting the required secrets and variables

All four inputs above are plain **repo-level** Actions secrets/variables — no GitHub Environment needed, so there's nothing to gate behind an `environment:` key in the job.

| Name                      | Kind                            | Where it comes from                                        |
| ------------------------- | ------------------------------- | ---------------------------------------------------------- |
| `SYNC_SERVICE_DEST_TOKEN` | secret                          | a fine-grained PAT scoped to just the OSS repo (see below) |
| `OPENAI_API_KEY`          | secret                          | your OpenAI account                                        |
| `SLACK_WEBHOOK_URL`       | secret                          | the Slack app's Incoming Webhook                           |
| `SLACK_CHANNEL`           | **variable**, nothing sensitive | whatever channel that webhook posts to                     |

### Getting `SYNC_SERVICE_DEST_TOKEN` (fine-grained personal access token)

1. Go to **github.com/settings/personal-access-tokens/new** (or: your GitHub avatar → Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token).
2. **Resource owner**: the account/org that owns the OSS repo.
3. **Repository access**: "Only select repositories" → choose the OSS repo specifically — not "All repositories."
4. **Permissions → Repository permissions**: set **Contents** to `Read and write`, and **Pull requests** to `Read and write`. Leave everything else at its default (`No access`).
5. Set an expiration and click **Generate token**. Copy the value now — GitHub shows it exactly once, and there's no way to view it again later.

(A GitHub App installation token works the same way and is the better choice if you're managing this for a whole org rather than one repo pair, but the fine-grained PAT above is the simpler path for a single repo pair.) Don't use your own personal `gh auth token` here — it's scoped to your whole account, not just the OSS repo, and can get invalidated whenever you re-auth `gh` locally.

### Setting all four via the `gh` CLI

```bash
echo -n "github_pat_xxx" | gh secret set SYNC_SERVICE_DEST_TOKEN --repo chinh100x/prod
echo -n "sk-xxx" | gh secret set OPENAI_API_KEY --repo chinh100x/prod
echo -n "https://hooks.slack.com/services/xxx" | gh secret set SLACK_WEBHOOK_URL --repo chinh100x/prod
echo -n "#sync-service-listener" | gh variable set SLACK_CHANNEL --repo chinh100x/prod
```

Or via the GitHub UI: production repo → **Settings → Secrets and variables → Actions** → **Secrets** tab for the first three (**New repository secret**), **Variables** tab for `SLACK_CHANNEL` (**New repository variable**).

Secrets only ever get written, never read back — GitHub gives no UI/API/`gh` path to view an existing secret's value afterward, regardless of permissions. If you lose track of a value or need to rotate it, generate a new one and overwrite the secret the same way; there's no way to recover the old one. `gh variable`, unlike `gh secret`, does let you read variable values back (`gh variable list`/`gh variable get`) since they aren't sensitive.

## Contributing

Per the org-wide [100x Engineering Standards](https://app.notion.com/p/vireox/100x-Engineering-Standards-3bf1041986e580cbafdbfbb28a090725) (DAP-273):

- **PRs, not direct pushes to `main`.** Branch protection requires at least one human approval (`CODEOWNERS`) and all CI checks green before merge — no exception for AI-authored PRs, same bar as anyone else's.
- **AI authorship is always disclosed**, never passed off as human work: an `Assisted-by:` git trailer on the relevant commits, plus the checkbox in `.github/pull_request_template.md`.
- **A human reviews and merges.** An agent can open a PR proposing a change, but shouldn't push straight to `main` or merge its own PR.
- **Verification is real, not claimed.** Run `make check` against your own local changes before opening a PR — not "it passed before" or "it's already on main."

Local setup: `make install && pre-commit install` (see [Try it locally](#try-it-locally) above). CI runs lint → typecheck → test → security scan, in that order, on every PR.

