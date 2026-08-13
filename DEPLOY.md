# Deploying sync-service against real repos

This is the path from "the demo runs on my laptop against two throwaway local
repos" to "a real commit to the production monitoring repo opens a real PR on
the open source monitoring repo — and a real commit to the OSS repo opens a
real PR back onto production." Follow architecture.md §9's build order: get
the shared tool working end to end against a throwaway repo pair first, *then*
wire OST-4, *then* OST-5. Don't skip straight to production repos.

**This is bidirectional (v2), beyond OST-3/4/5's current written scope** — see
design.md's "v2" section for what that means and why it's called out separately.
Everything below assumes you want both directions live; if you only want the
original v1 (downstream-only) behavior, just skip §5 (the reverse workflow) and
never enable `hydrate` in the mapping config — forward-only still works.

**Read design.md's v5 note before deploying this against a real repo pair.**
There is no divergence detection anymore (removed entirely, not simplified) — a
sync always overwrites the far side's tracked files with the near side's current
content. [OST-5](https://linear.app/100xteam/issue/OST-5)'s "outside contributions
on main are not overwritten" acceptance criterion is **not met** by this version.
That's fine for a repo pair only you (or a small team) push to; it's a real risk
for OST-4/OST-5's actual repos, which are explicitly meant to take outside
contributions. Don't deploy this version against a repo where that matters without
consciously deciding to accept it, or reintroducing some form of divergence check
first.

## 0. Prerequisites

- `gh` CLI available on the runner (already preinstalled on GitHub-hosted
  Actions runners — nothing to add if you're using `ubuntu-latest`).
- A place to host this code as its own repo (e.g. `100x/sync-service`) — the
  composite action in `action.yml` currently installs it via
  `uv tool install "git+https://github.com/100x/sync-service"`. Update that
  URL once the repo exists, or point it at a specific tag once you cut one.

## 1. Auth: a token for writing to the destination repo

Per architecture.md §6, don't use a personal token. Two options, either works:

**GitHub App (preferred)** — create an app scoped to just the destination
repos (e.g. `portfolio-monitoring-oss`, the 100xtools OSS repo), with
`contents:write` + `pull_requests:write`. Install it only on those repos.
Generate an installation token in the workflow (e.g. via
`actions/create-github-app-token`) and pass it as `dest-token`.

**Fine-scoped PAT** — a PAT scoped only to the destination repo(s), same two
permissions, stored as an org-level Actions secret (e.g.
`SYNC_SERVICE_DEST_TOKEN`), not a personal access token tied to one person's
account.

Either way: the token needs write access to the *destination* repo only. The
*production* repo's own default `GITHUB_TOKEN` is enough for everything else
(reading the diff, commenting on the source commit).

**If you're enabling the reverse direction too** (§5), you need a *second*
token the same shape, scoped to write to the **production** repo instead —
`contents:write` + `pull_requests:write` there. Don't reuse the forward
token; it's scoped to the wrong repo. A GitHub App with two separate
installations (one per repo, or one app installed on both with the workflow
picking the right installation token) works fine too.

## 2. Try it against a throwaway repo pair first

Before touching the real monitoring or 100xtools repos, create two scratch
GitHub repos (e.g. `you/sync-service-test-prod`, `you/sync-service-test-oss`)
and run the CLI against them locally, the same way `demo/run_demo.py` does
but with real `git remote`s instead of none:

```bash
git clone https://github.com/you/sync-service-test-prod prod
git clone https://github.com/you/sync-service-test-oss oss
# ... make a commit in prod under some sync/*.yaml-mapped path ...
uv run sync-service run \
  --config prod/sync/test.yaml \
  --source-repo prod \
  --dest-repo oss \
  --base <base-sha> --head <head-sha>
```

With a real remote configured, `publish.open_pr` (in
`src/sync_service/publish.py`) takes the `git push` + `gh pr create` path
instead of the dry-run print — confirm a real PR shows up on the throwaway
OSS repo before moving on.

## 3. Where the mapping config lives

The CLI itself still takes `--config <path-to-a-file>` — that's what §2's
manual run, `demo/run_demo.py`, and the test suite all use, and a standalone
`sync/<name>.yaml` file (see `design.md`/architecture.md §3 for the schema)
next to the code it maps is a perfectly good way to keep one for local use or
version-control history.

**The composite Action (`action.yml`) is different: its `config` input is the
mapping YAML's actual *content*, embedded inline in the calling workflow —
not a path.** The action writes it to a temp file itself before invoking the
CLI. This means the workflow file is fully self-contained (nothing else to
go look up), at the cost of the same text needing to be duplicated into
*both* `sync.yaml` (production repo) and `reverse-sync.yaml` (OSS repo) if
you're running both directions — see §4/§5's examples. If you'd rather keep
one shared file instead of duplicating the block, revert `action.yml`'s
`config` input to take a path again and have both workflows point at the
same `sync/<name>.yaml`; the CLI doesn't care which way it arrives.

Concretely, for OST-4 that's the monitoring mapping; for OST-5 it's
`tools` (three `mappings` entries, one per production source) in each of the
three 100xtools production sources.

**`source`/`dest` are optional — omit both to track the whole repo** instead
of a subdirectory (see design.md's v3 section). `.git` and the counterpart
checkout `action.yml` makes are always excluded regardless, but anything else
that shouldn't cross — a real `.env`, an internal-only folder — needs its own
entry in `exclude`. There's no wildcard support there: list exact paths, not
`*.env` or similar.

**No bootstrapping step needed anymore.** Earlier versions tracked a manifest
per mapping and required seeding it before the first run in a new direction, or
every pre-existing file at the mapped path would be treated as an unresolvable
conflict. That tracking is gone as of v5 (see design.md's v5 note) — there's
nothing to bootstrap, and the first run in either direction just works. The
tradeoff is the one described at the top of this file: no protection against
overwriting a change that landed on the far side outside this tool.

## 4. Add the forward workflow to the production repo

```yaml
# .github/workflows/sync.yaml, in the production repo
name: sync-service
on:
  push:
    branches: [main]
    # match the mapping's source(s) below, plus this file itself so editing
    # the embedded config also triggers a run
    paths: ["src/portmon/**", ".github/workflows/sync.yaml"]

jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0   # need base..head history, not just the tip

      - uses: 100x/sync-service@v1
        with:
          config: |
            mappings:
              - key: portmon
                source: src/portmon
                dest: plugin
                break_check:
                  install: "pip install -e ."
                  run: "portmon run --deal demo/ost6-deal-01"
          direction: forward
          counterpart-repo: 100x-oss/portfolio-monitoring   # the OSS repo
          counterpart-branch: main
          counterpart-token: ${{ secrets.SYNC_SERVICE_DEST_TOKEN }}
          base: ${{ github.event.before }}
          head: ${{ github.sha }}
```

Two things worth knowing about `base`/`head` for a push-triggered workflow:

- `github.event.before` is all-zeros on a branch's first-ever push (no prior
  commit to diff against). Handle that edge case by falling back to
  `HEAD~1` or skipping the run — it won't come up for `main`, which already
  exists, but will if you ever point this at a newly created branch.
- `fetch-depth: 0` on the checkout step is required — `sync_service.diff`
  runs `git diff base..head`, which needs the actual history, not just the
  tip commit that a shallow (default) checkout gives you.

100xtools' three production sources each get their own copy of this workflow
(pointed at their own `sync/*.yaml`), per architecture.md §9 step 3.

## 5. Add the reverse workflow to the OSS repo (only if you want OSS -> production live)

Same action, opposite roles — and the **same mapping config text**, pasted
into this workflow too. It's the one piece of real duplication this approach
costs: the block below must stay identical to §4's, by hand, since each
workflow is independently self-contained. (See §3 if that tradeoff isn't
worth it — a shared file works too.)

```yaml
# .github/workflows/reverse-sync.yaml, in the OSS repo
name: sync-service-reverse
on:
  push:
    branches: [main]
    paths: ["plugin/**"]   # match the mapping's dest(s)

jobs:
  reverse-sync:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: 100x/sync-service@v1
        with:
          config: |
            mappings:
              - key: portmon
                source: src/portmon
                dest: plugin
                break_check:
                  install: "pip install -e ."
                  run: "portmon run --deal demo/ost6-deal-01"
          direction: reverse
          counterpart-repo: 100x/portfolio-monitoring       # the production repo
          counterpart-branch: main
          counterpart-token: ${{ secrets.SYNC_SERVICE_PROD_TOKEN }}   # the *second* token from §1
          base: ${{ github.event.before }}
          head: ${{ github.sha }}
```

Without this workflow, an outside OSS edit isn't proposed back at all — there's
no opportunistic detection anymore (that depended on the manifest, removed in
v5). The only way an OSS-side change reaches production is this workflow
actually running; skipping it means that direction just doesn't happen.

## 6. Swap the demo secret scanner for gitleaks

`src/sync_service/secretscan.py` ships a handful of built-in regexes so the
demo has zero external dependencies. Real deployment should use
[`gitleaks`](https://github.com/gitleaks/gitleaks) instead — it's the tool
named in architecture.md §8. Simplest swap: write the `desired` dict out to a
scratch directory and shell out to `gitleaks detect --no-git --source
<dir>`, treating a non-zero exit as a hit — same halt path, just a stronger
gate. Do this before pointing the workflow at a production repo with real
tenant data in it.

## 7. Optional: human-readable PR titles/bodies via an LLM

Off by default. `sync_service.pr_writer` (see design.md/architecture.md's v10 note)
turns the deterministic PR title/body (`Sync <mapping> changes`, a bare file list)
into readable prose -- **advisory only**, never part of the sync/security decision.
It only ever sees this mapping's own already-scrubbed, already-validated candidate
diff (via `git diff` on the *far* repo, never the near/production repo directly),
never the production commit message, never anything scrub/secretscan already
stripped, never a secret-scan hit (it doesn't run at all on a halted mapping).

To enable it:

1. Add `llm_pr: enabled: true` alongside `mappings:` in the mapping config.
2. Put `OPENAI_API_KEY` in a GitHub **Environment** (not a plain repo/org secret) --
   e.g. an environment named `production` -- and add `environment: production` to
   the calling workflow's job (not `action.yml`; composite actions don't have a job
   context of their own, this has to be set where the job is actually defined).
3. Pass it through as an input, same pattern as `counterpart-token`:
   ```yaml
   jobs:
     sync:
       runs-on: ubuntu-latest
       environment: production
       steps:
         - uses: chinh100x/sync-service@main
           with:
             config: |
               mappings: [...]
               llm_pr:
                 enabled: true
             openai-api-key: ${{ secrets.OPENAI_API_KEY }}
             # ...the rest as in §4
   ```
4. Optional: `openai-model` input / `OPENAI_PR_MODEL` env override if you don't want
   `pr_writer.py`'s built-in default.

Nothing about this key is persisted anywhere, and `breakcheck.py` strips it (with
`GH_TOKEN`/`GITHUB_TOKEN`) before running any far-side install/run command -- the
same reasoning as v9's credential-persistence fix, extended to this key.
`OPENAI_API_KEY` is never required: leaving `llm_pr.enabled: false` (the default),
omitting the secret, or any OpenAI-side failure (timeout, rate limit, bad auth,
malformed output) all fall back to the plain deterministic PR title/body with no
effect on whether the sync itself succeeds.

## 8. Roll out in build order

1. Confirm the throwaway-repo-pair run in step 2 works for every scenario
   `demo/run_demo.py` exercises locally: a clean forward sync, a rejected PR
   (secret scan hit), an outside far-side edit getting silently overwritten
   by the next sync (know this is expected — see design.md's v5 note, not a
   bug to chase), a break check failure with a working-tree revert, and —
   if you're enabling §5 — an explicit `--direction reverse` run.
2. **OST-4**: wire `sync/monitoring.yaml` into the monitoring production
   repo, make one real commit, confirm the PR against the OST-6 demo deal
   looks right and the break check actually ran `portmon run --deal
   demo/ost6-deal-01` against the propagated code.
3. **OST-5**: wire `sync/tools.yaml` (three mappings) into the three
   100xtools production sources, confirm one PR per tool (never batched).
   **Before wiring this one for real**: OST-5's own acceptance criteria
   require outside contributions on `main` not be overwritten — this version
   doesn't do that (see design.md's v5 note) — decide explicitly whether
   that's acceptable for this repo, or bring back divergence detection first.
4. Only if OSS -> production is actually wanted (it's beyond OST-3/4/5's
   current scope, see design.md's v2 section): add `hydrate` rules to the
   mapping config and wire §5's reverse workflow — no bootstrapping needed
   anymore — and confirm a real OSS contribution shows up as a real PR on
   the production repo.
