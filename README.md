# sync-service

Implementation of [design.md](../design.md) / [architecture.md](../architecture.md) ([OST-3](https://linear.app/100xteam/issue/OST-3)): a bidirectional production ↔ open source sync service. A commit to either repo's default branch scrubs (or restores) production-specific detail, confirms the far repo still installs and runs with the change applied, and opens a PR — nothing auto-merges. **There's no divergence detection** — a run always overwrites the far side's tracked files with the near side's current content; see design-history.md's v5 note for what that gives up (a real OST-5 acceptance criterion) and why it's a deliberate tradeoff for this use case, not a default recommendation. Full-repo tracking and reverse-sync are beyond OST-3's original v1 (downstream-only) scope; see design-history.md's "v2"–"v5" sections for the full history, including an auto-merge attempt that was tried and reverted.

This directory is a standalone project. It doesn't inherit CI/build conventions from any other repo.

## Layout

```
src/sync_service/
├── cli.py          entrypoint: `sync-service run --config ... --source-repo ... --dest-repo ... --base ... --head ... [--direction forward|reverse]`
├── config.py       pydantic schema for the sync/*.yaml mapping config (redact + its inverse, hydrate)
├── diff.py         which mappings did base..head touch (the trigger) — source-side or dest-side depending on direction
├── scrub.py        exclude list + regex substitution, direction-agnostic (redact fwd, hydrate rev)
├── secretscan.py   demo secret-scan gate — swap for `gitleaks` in production, see DEPLOY.md
├── breakcheck.py   runs break_check.install / .run before a PR is opened (the break check)
├── llm_client.py   shared OpenAI structured-output call used by pr_writer.py/safety_review.py --
│                   only the plumbing is shared; each caller keeps its own fail-open/fail-closed decision.
├── pr_writer.py    optional LLM-written human-readable PR title/body -- see design-history.md's v10 note.
│                   Off by default (`llm_pr.enabled`); deterministic fallback always available.
├── safety_review.py  optional LLM semantic safety review -- see design-history.md's v12 note.
│                   Off by default (`llm_safety_review.enabled`); fails *closed* (halts) on
│                   any error, unlike pr_writer.py -- this is a security gate, not cosmetic.
├── publish.py      branch + commit + `gh pr create` (no-op detection: nothing to commit -> no PR).
│                   Rewords the commit's subject to pr_writer's generated title before pushing --
│                   see design-history.md's v14 note for why that's not a reopening of v7's leak.
├── notify.py       comment on the source commit + Slack post (if SLACK_WEBHOOK_URL is set) on
│                   every halt/error and PR opened -- see design-history.md's v13 note.
└── slack.py        best-effort Slack Incoming Webhook post; never raises, off unless configured
tests/unit/         one test file per module
tests/integration/  full end-to-end scenarios (both directions) through cli.main() against
                    two local git repos — no GitHub needed
action.yml          composite GitHub Action wrapping the CLI, for real deployment
```

## Walkthrough: run everything and see the whole picture

### 1. One-time setup

```bash
uv sync --dev
```
Creates `.venv/` and installs `pydantic`, `pyyaml`, `pytest`. No GitHub token, no `gitleaks` binary, no real repos needed for any of this.

### 2. Run the tests (the mechanism, in isolation and end-to-end)

```bash
uv run pytest -v
```
84 tests, each exercising one decision from `design.md`/`architecture.md` with no GitHub involved: `test_diff.py` (trigger matching, including whole-repo mappings; reading the real committer's name/email off a commit), `test_scrub.py` (exclude + redact + hydrate, plus the always-excluded mechanical dirs and which categories actually fired), `test_publish.py` (a real change commits; genuinely identical content backs out cleanly instead of crashing `git commit`; branch renaming; idempotency tracked via a dedicated ref rather than the branch name itself, so a clean title-derived slug carries no sha at all; crediting a commit's Author independently of its Committer), `test_breakcheck.py`/`test_cli.py` (token/commit-message scoping, that the far-side commit's Author matches the real production committer while the Committer stays the bot, and that a re-run of the same commit range is still recognized as already-synced through the branch rename), `test_pr_writer.py` (deterministic fallback on every OpenAI failure mode, and proof that production commit messages/excluded files/scrubbed values/secret matches never reach the model), `test_safety_review.py` (every failure mode is a hard halt, never an implicit pass, and the exit code distinguishes "blocked by a real finding" from "couldn't even check"), `test_slack.py`/`test_notify.py` (every Slack failure mode returns `False` without raising, and a publish failure now actually reaches a notification, not just an exit code), `test_integration.py` (full runs through `cli.main()`: two mappings touched in one commit, the no-divergence-tracking overwrite behavior, a break-check halt/revert/retry cycle, a no-op run, and the reverse direction with `hydrate`).

### 3. See one end-to-end scenario narrated, with output

```bash
uv run pytest tests/integration/ -v -s
```

`-s` shows each scenario's real `cli.main()` output (scrubbing, redaction, PR dry-run text) rather than just pass/fail — useful for seeing the whole system react to a commit without leaving any state behind afterward. `test_reverse_direction_hydrates_and_opens_a_pr_onto_production` is a good one to start with: it drives an OSS commit through `--direction reverse` and shows the resulting PR onto the production repo, with `hydrate` restoring the real endpoint.

### 4. Drive the CLI yourself, one command at a time (optional, deeper look)

Point it at any two local git repos you control:

```bash
# forward: production -> OSS
uv run sync-service run \
  --config path/to/sync/monitoring.yaml \
  --source-repo /path/to/a/prod-checkout \
  --dest-repo /path/to/an/oss-checkout \
  --base <sha-before> --head <sha-after>

# reverse: OSS -> production
uv run sync-service run \
  --config path/to/sync/monitoring.yaml \
  --source-repo /path/to/a/prod-checkout \
  --dest-repo /path/to/an/oss-checkout \
  --base <sha-before> --head <sha-after> \
  --direction reverse
```

`--source-repo`/`--dest-repo` always name the same two physical repos (production/OSS); `--direction` decides which one's commit range is being evaluated and which one a PR gets proposed onto.

## Deploying against real repos

See [DEPLOY.md](./DEPLOY.md) — token setup, the actual GitHub Actions workflow YAML for both directions, and the `gitleaks` swap. (No more bootstrapping step — that only existed for the manifest, which v5 removed.)
