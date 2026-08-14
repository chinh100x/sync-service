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
tests/              unit tests for diff/scrub/publish — no GitHub calls needed
demo/run_demo.py    runs the whole flow (both directions) against two local git repos, no GitHub needed
action.yml          composite GitHub Action wrapping the CLI, for real deployment
```

## Walkthrough: run everything and see the whole picture

### 1. One-time setup

```bash
uv sync --dev
```
Creates `.venv/` and installs `pydantic`, `pyyaml`, `pytest`. No GitHub token, no `gitleaks` binary, no real repos needed for any of this.

### 2. Run the unit tests (the mechanism in isolation)

```bash
uv run pytest -v
```
62 tests, each exercising one decision from `design.md`/`architecture.md` with no git/GitHub involved: `test_diff.py` (trigger matching, including whole-repo mappings), `test_scrub.py` (exclude + redact + hydrate, plus the always-excluded mechanical dirs and which categories actually fired), `test_publish.py` (a real change commits; genuinely identical content backs out cleanly instead of crashing `git commit`), `test_breakcheck.py`/`test_cli.py` (token/commit-message scoping), `test_pr_writer.py` (deterministic fallback on every OpenAI failure mode, and proof that production commit messages/excluded files/scrubbed values/secret matches never reach the model), `test_safety_review.py` (every failure mode is a hard halt, never an implicit pass, and the exit code distinguishes "blocked by a real finding" from "couldn't even check"), `test_slack.py`/`test_notify.py` (every Slack failure mode returns `False` without raising, and a publish failure now actually reaches a notification, not just an exit code).

### 3. Run the end-to-end demo (the whole system, both directions)

```bash
uv run python demo/run_demo.py
```

It builds two throwaway local git repos in `/tmp` (`prod-repo`, `oss-repo`) and drives commits through the real CLI in both directions. Read the output top to bottom — each `STEP` header is one commit landing on some repo's `main` and the service reacting to it:

| Step | What happens                                                                       | Which decision it proves                                                                                                                                     |
| ---- | ---------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1    | Commit touches both `src/portmon/` and `src/brk/` → two PRs opened (dry-run print) | trigger fires per-mapping; `internal_reporting.py` excluded; `cag-mcp.internal` URL redacted to `<MCP_ENDPOINT>`                                                                             |
| 2    | A hardcoded API key sneaks into `covenant.py`                                      | secret-scan gate halts the run, no PR, comment printed                                                                                                                                       |
| 3    | An outside OSS edit, then a separate prod change to the same mapping               | **silently overwritten** — no divergence detection anymore, the next forward sync just replaces it. Deliberate tradeoff, not a bug — see design-history.md's v5 note. |
| 4    | `src/brk/mod.py` gets a real bug                                                   | break check runs it, fails, **working tree is reverted** — `brk/mod.py` on OSS main still has the old working code                                                                           |
| 4b   | The bug gets fixed and pushed again                                                | forward sync succeeds on retry — design.md §3's retry path                                                                                                                                   |
| 5    | Commit only touches `README.md`                                                    | no mapping matched → no-op                                                                                                                                                                   |
| 6    | Re-run the exact same base/head as step 1                                          | branch already exists → skipped (idempotent)                                                                                                                                                 |
| 7    | An OSS commit, run with `--direction reverse` directly                             | the *explicit* OSS → production trigger — a real PR onto the production repo, hydrated                                                                                                       |

At the very end it prints a workspace path, e.g.:
```
Workspace left on disk for inspection: /var/folders/.../sync-service-demo-xxxxxx
```
That directory is **not deleted** — that's your window into "the whole picture." Each run of the demo uses a fresh temp dir, so re-running it gives you a new path.

### 4. Inspect what actually happened on disk

Copy the printed path into `$WS` and look around:

```bash
WS=/var/folders/.../sync-service-demo-xxxxxx   # paste your own path here

# both repos' commit history
git -C "$WS/prod-repo" log --oneline
git -C "$WS/oss-repo" log --oneline main

# every sync / reverse-sync branch the service created
git -C "$WS/oss-repo" branch -a
git -C "$WS/prod-repo" branch -a

# the propagated, scrubbed file — this is now unconditionally the near side's
# current content; there's no manifest left to compare it against
cat "$WS/oss-repo/plugin/covenant.py"

# proof the exclude worked — internal_reporting.py never made it across
ls "$WS/oss-repo/plugin/"

# the explicit reverse-sync PR (step 7) that landed on the prod repo, with the real endpoint restored
git -C "$WS/prod-repo" show "reverse-sync/brk/<sha>:src/brk/mod.py"   # fill in <sha> from the demo's printed branch name
```

### 5. Drive the CLI yourself, one command at a time (optional, deeper look)

Same code path as the demo, just point it at any two local git repos you control instead of the script's generated ones:

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
