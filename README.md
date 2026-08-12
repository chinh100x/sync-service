# sync-service

Implementation of [design.md](../design.md) / [architecture.md](../architecture.md) ([OST-3](https://linear.app/100xteam/issue/OST-3)): a bidirectional production ↔ open source sync service. A commit to either repo's default branch scrubs (or restores) production-specific detail, confirms the far repo still installs and runs with the change applied, and opens a PR — nothing auto-merges. Non-overlapping edits on both sides auto-resolve via a three-way merge; a real same-line conflict still halts for a human. See design.md's "v2" section — this goes beyond OST-3's original v1 (downstream-only) scope.

This directory is a standalone project. It doesn't inherit CI/build conventions from any other repo.

## Layout

```
src/sync_service/
├── cli.py          entrypoint: `sync-service run --config ... --source-repo ... --dest-repo ... --base ... --head ... [--direction forward|reverse]`
├── config.py       pydantic schema for the sync/*.yaml mapping config (redact + its inverse, hydrate)
├── diff.py         which mappings did base..head touch (the trigger) — source-side or dest-side depending on direction
├── scrub.py        exclude list + regex substitution, direction-agnostic (redact fwd, hydrate rev)
├── secretscan.py   demo secret-scan gate — swap for `gitleaks` in production, see DEPLOY.md
├── state.py        sync-state manifest as a blob store + three-way merge (`classify`) instead of a hard stop
├── breakcheck.py   runs break_check.install / .run before a PR is opened (the break check)
├── publish.py      branch + commit + `gh pr create`
└── notify.py       comment on the source commit when a run halts
tests/              unit tests for diff/scrub/state — no GitHub calls needed
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
10 tests, each exercising one decision from `design.md`/`architecture.md` with no git/GitHub involved: `test_diff.py` (trigger matching), `test_scrub.py` (exclude + redact + hydrate), `test_state.py` (clean / new / auto-merged / real-conflict classification).

### 3. Run the end-to-end demo (the whole system, both directions)

```bash
uv run python demo/run_demo.py
```

It builds two throwaway local git repos in `/tmp` (`prod-repo`, `oss-repo`) and drives commits through the real CLI in both directions. Read the output top to bottom — each `STEP` header is one commit landing on some repo's `main` and the service reacting to it:

| Step | What happens                                                                       | Which decision it proves                                                                                                                                                                     |
| ---- | ---------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1    | Commit touches both `src/portmon/` and `src/brk/` → two PRs opened (dry-run print) | trigger fires per-mapping; `internal_reporting.py` excluded; `cag-mcp.internal` URL redacted to `<MCP_ENDPOINT>`                                                                             |
| 2    | A hardcoded API key sneaks into `covenant.py`                                      | secret-scan gate halts the run, no PR, comment printed                                                                                                                                       |
| 3    | An outside OSS comment-only edit, plus a non-overlapping prod change (`audit()`)   | three-way merge auto-resolves cleanly — PR carries **both** edits, and a *separate* reverse-sync PR proposes the outsider's edit back to prod, hydrated (real endpoint, not the placeholder) |
| 4    | An outside OSS edit and a prod edit touch the *same* line                          | real conflict — auto-resolution doesn't fire, halted for manual reconciliation, same as v1                                                                                                   |
| 5    | `src/brk/mod.py` gets a real bug                                                   | break check runs it, fails, **working tree is reverted** — `brk/mod.py` on OSS main still has the old working code                                                                           |
| 5b   | The bug gets fixed and pushed again                                                | forward sync succeeds on retry — design.md §3's retry path                                                                                                                                   |
| 6    | Commit only touches `README.md`                                                    | no mapping matched → no-op                                                                                                                                                                   |
| 7    | Re-run the exact same base/head as step 1                                          | branch already exists → skipped (idempotent)                                                                                                                                                 |
| 8    | An OSS commit, run with `--direction reverse` directly                             | the *explicit* OSS → production trigger — a real PR onto the production repo, hydrated                                                                                                       |

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

# the manifest + blob store that makes the three-way merge possible
cat "$WS/oss-repo/.sync-state/portmon.json"
ls "$WS/oss-repo/.sync-state/portmon/blobs/"

# the propagated, scrubbed file — and its auto-merged version after step 3
cat "$WS/oss-repo/plugin/covenant.py"

# proof the exclude worked — internal_reporting.py never made it across
ls "$WS/oss-repo/plugin/"

# the reverse-sync proposal that landed on the prod repo, with the real endpoint restored
git -C "$WS/prod-repo" show "reverse-sync/portmon/<sha>:src/portmon/covenant.py"   # fill in <sha> from the demo's printed branch name
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

See [DEPLOY.md](./DEPLOY.md) — token setup, the actual GitHub Actions workflow YAML for both directions, the `gitleaks` swap, and the bootstrapping gotcha (pre-existing files with no manifest entry look like conflicts on a direction's very first run).
