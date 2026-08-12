# Deploying sync-service against real repos

This is the path from "the demo runs on my laptop against two throwaway local
repos" to "a real commit to the production monitoring repo opens a real PR on
the open source monitoring repo — and a real commit to the OSS repo opens a
real PR back onto production." Follow architecture.md §9's build order: get
the shared tool working end to end against a throwaway repo pair first, *then*
wire OST-4, *then* OST-5. Don't skip straight to production repos.

**This is bidirectional (v2), beyond OST-3/4/5's current written scope** — see
design.md's "v2" section for what that means and why it's called out separately.
(An earlier auto-merge feature that also lived under "v2" was tried and reverted
in v4 — any divergence is a hard stop again, same as v1.) Everything below assumes
you want both directions live; if you only want the original v1 (downstream-only)
behavior, just skip §5 (the reverse workflow) and never enable `hydrate` in the
mapping config — forward-only still works exactly as before.

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
of a subdirectory (see design.md's v3 section). `.git`, `.sync-state`, and the
counterpart checkout `action.yml` makes are always excluded regardless, but
anything else that shouldn't cross — a real `.env`, an internal-only folder —
needs its own entry in `exclude`. There's no wildcard support there: list
exact paths, not `*.env` or similar. If you're tracking the whole repo, also
use `paths-ignore: [".sync-state/**"]` on the trigger (§4/§5) instead of an
allowlist — a sync landing on the far side's own manifest shouldn't retrigger
a workflow on that side.

**Important operational gotcha**: `state.classify` (architecture.md §5/v2)
treats *any* file that already exists at a mapped path but isn't in the
sync-state manifest as a conflict — "unknown provenance," not "safe to
merge against." If the destination folder already has content before the
first automated run *in that direction*, that first run will halt on every
file in it. Two ways to bootstrap cleanly, per direction:

- Start from an empty destination folder (what the demo does for its first
  forward sync) and let the first automated run populate it, or
- Do one manual initial sync yourself, commit it, then seed the manifest —
  either hand-write `.sync-state/<mapping-key>.json` (`last_source_sha` +
  `sha256:` hash of each file), or call the helper directly:
  ```python
  from pathlib import Path
  from sync_service import state
  state.write(Path("/path/to/checkout"), "portmon", "<sha-you-just-committed>", {
      "plugin/covenant.py": Path("/path/to/checkout/plugin/covenant.py").read_text(),
  })
  ```
  `state.write` handles the hash for you.

**This applies separately to each direction.** Enabling reverse for a mapping
that's only ever run forward means the *production* side has no manifest yet
— the first reverse check will see production's existing files as
unknown-provenance and halt, even though nothing is actually wrong. Bootstrap
production's manifest the same way, once, before turning the reverse workflow
on. See `demo/run_demo.py`'s "bootstrap" step for a worked example of exactly
this.

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
            destination:
              repo: 100x-oss/portfolio-monitoring
              branch: main
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
            destination:
              repo: 100x-oss/portfolio-monitoring
              branch: main
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

Without this workflow, an outside OSS edit still isn't lost — the forward
workflow's own `state.classify` (architecture.md §5/v2) opportunistically
proposes it back the next time *any* production commit triggers a forward
run. This reverse workflow just reacts immediately instead of waiting for
that next unrelated production push.

## 6. Swap the demo secret scanner for gitleaks

`src/sync_service/secretscan.py` ships a handful of built-in regexes so the
demo has zero external dependencies. Real deployment should use
[`gitleaks`](https://github.com/gitleaks/gitleaks) instead — it's the tool
named in architecture.md §8. Simplest swap: write the `desired` dict out to a
scratch directory and shell out to `gitleaks detect --no-git --source
<dir>`, treating a non-zero exit as a hit — same halt path, just a stronger
gate. Do this before pointing the workflow at a production repo with real
tenant data in it.

## 7. Roll out in build order

1. Confirm the throwaway-repo-pair run in step 2 works for every scenario
   `demo/run_demo.py` exercises locally: a clean forward sync, a rejected PR
   (secret scan hit), a far-side divergence (conflict halt, no forward PR —
   but the reverse-sync proposal still fires), a break check failure with a
   working-tree revert, and — if you're enabling §5 — an explicit
   `--direction reverse` run.
2. **OST-4**: wire `sync/monitoring.yaml` into the monitoring production
   repo, make one real commit, confirm the PR against the OST-6 demo deal
   looks right and the break check actually ran `portmon run --deal
   demo/ost6-deal-01` against the propagated code.
3. **OST-5**: wire `sync/tools.yaml` (three mappings) into the three
   100xtools production sources, confirm one PR per tool (never batched),
   and confirm the manifest conflict path by merging an outside PR to the
   OSS repo's `main` before triggering the next sync.
4. Only if OSS -> production is actually wanted (it's beyond OST-3/4/5's
   current scope, see design.md's v2 section): add `hydrate` rules to the
   mapping config, bootstrap production's manifest (§3), wire §5's reverse
   workflow, and confirm a real OSS contribution shows up as a real PR on
   the production repo.
