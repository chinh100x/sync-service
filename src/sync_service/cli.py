"""CLI entrypoint — bidirectional, no conflict tracking.

`sync-service run --direction forward` is production -> OSS (redact, break_check).
`sync-service run --direction reverse` is OSS -> production (hydrate, reverse_break_check).
Both directions run through the same engine (`run_direction`) with source/dest and
redact/hydrate swapped — the mechanism is identical either way.

No manifest, no divergence detection: a run always overwrites the far side's tracked
files with the near side's current content (after the secret scan and break check both
pass). If the far side has its own edits outside this tool, they're overwritten with no
warning — deliberately simpler than tracking state, at the cost of the "don't overwrite
an outside contribution" guarantee earlier versions had. See design-history.md's v5 note.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import breakcheck, diff, notify, pr_writer, publish, safety_review, scrub, secretscan
from .config import BreakCheck, Mapping, RedactRule, SyncConfig


def run_direction(
    *,
    mapping: Mapping,
    near_repo: Path,
    near_path: str,
    near_exclude: list[str],
    near_rules: list[RedactRule],
    far_repo: Path,
    far_path: str,
    far_break_check: BreakCheck | None,
    head_sha: str,
    base_branch: str,
    branch_prefix: str,
    label: str,
    gh_token: str | None,
    llm_pr_enabled: bool,
    llm_safety_review_enabled: bool,
    project_name: str | None,
) -> str:
    """near = the side whose commit triggered this run; far = the side we're proposing to."""
    # Human-readable prefix for every Slack-bound notification below (secret/break-
    # check/safety-review halts, publish failures, PR-opened) -- falls back to the
    # old mechanical `label:mapping_key` format when the config doesn't set
    # project_name, so existing demo/test configs see no behavior change.
    project_label = project_name or f"{label}:{mapping.key}"
    # Reset far_repo to base_branch before touching it. Without this, a prior mapping
    # processed in the same run (or a prior breakcheck-halt) can leave far_repo checked
    # out on *its own* branch — nesting this mapping's commit inside that one's PR
    # instead of both branching independently off base_branch. See design-history.md's v8 note.
    publish.checkout_base(far_repo, base_branch)

    branch = publish.branch_name(f"{branch_prefix}/{mapping.key}", head_sha)
    if publish.branch_exists(far_repo, branch):
        print(f"[{label}:{mapping.key}] PR already exists for this (mapping, head_sha) — skipping (idempotent re-run)")
        return "skipped-exists"

    desired, scrubbed_categories = scrub.apply(near_repo, near_path, far_path, near_exclude, near_rules)
    if not desired:
        print(f"[{label}:{mapping.key}] nothing under {near_path}/ to propagate")
        return "empty"
    print(f"[{label}:{mapping.key}] scrubbed {len(desired)} file(s) -> {far_path}/")

    hits = secretscan.scan(desired)
    if hits:
        notify.comment_on_commit(
            head_sha,
            f"[{project_label}] secret scan hit in {mapping.key}: {hits[0]['rule']} in {hits[0]['path']}. Halted, no PR.",
        )
        return "secret-halt"

    # Same input, same point in the pipeline as secretscan.scan() above -- neither
    # touches far_repo's working tree at all until both pass. Unlike pr_writer below,
    # this fails *closed*: SafetyReviewUnavailable means "couldn't confirm this is
    # safe," which is a hard halt, not an implicit pass. See safety_review.py's
    # module docstring and design-history.md's v12 note.
    try:
        verdict = safety_review.review(
            safety_review.SafetyReviewContext(mapping_key=mapping.key, files=desired),
            enabled=llm_safety_review_enabled,
        )
        if verdict is not None:
            print(f"[safety-review:{mapping.key}] passed" if verdict.passed else f"[safety-review:{mapping.key}] blocked")
    except safety_review.SafetyReviewUnavailable as exc:
        notify.comment_on_commit(
            head_sha,
            f"[{project_label}] {mapping.key}: semantic safety review unavailable ({exc}) -- halted out of caution, no PR.",
        )
        return "safety-review-error"

    if verdict is not None and not verdict.passed:
        categories = f" (categories: {', '.join(verdict.categories)})" if verdict.categories else ""
        notify.comment_on_commit(
            head_sha,
            f"[{project_label}] {mapping.key}: semantic safety review blocked this change: {verdict.summary}{categories}. Halted, no PR.",
        )
        return "safety-review-halt"

    for rel_path, text in desired.items():
        far_file = far_repo / rel_path
        far_file.parent.mkdir(parents=True, exist_ok=True)
        far_file.write_text(text)

    if far_break_check is not None:
        check = breakcheck.run(far_repo, far_break_check)
        if not check.passed:
            notify.comment_on_commit(
                head_sha,
                f"[{project_label}] {mapping.key}: break check failed at `{check.failed_step}`:\n{check.output}",
            )
            publish.discard_working_tree_changes(far_repo)
            return "breakcheck-halt"
    else:
        print(f"[{label}:{mapping.key}] no break check configured for this direction — relying on the far repo's own CI")

    # Deliberately not the production commit message. It's free-form human text that
    # never goes through scrub/secretscan — see design-history.md's v7 note. The SHA alone is
    # safe to expose (it's just a hash) and is enough to correlate back to the source
    # commit for anyone who already has access to that repo.
    commit_message = f"sync: {mapping.key} @ {head_sha[:12]}"
    committed = publish.commit_to_branch(far_repo, branch, message=commit_message)
    if not committed:
        print(f"[{label}:{mapping.key}] nothing changed vs the far side — no PR")
        return "unchanged"

    # The PR title/body are the one place this tool tries to be human-readable rather
    # than purely mechanical -- see design-history.md's v10 note. Everything fed into it is
    # already-scrubbed, already-validated far-side content (this mapping's own
    # candidate diff, changed-file list, break-check outcome); it never touches
    # near_repo/production directly, and it can fail over to a fully deterministic
    # title/body with no effect on whether the sync itself succeeds.
    context = pr_writer.PRContext(
        mapping_key=mapping.key,
        public_reason=mapping.public_reason,
        changed_files=sorted(desired),
        sanitized_diff=diff.candidate_diff(far_repo, base_branch, branch),
        scrubbed_categories=scrubbed_categories,
        validation=pr_writer.ValidationSummary(
            # None (nothing to report) when no break_check is configured for this
            # direction -- reaching this line already means it passed if one was.
            run_command=far_break_check.run if far_break_check is not None else None,
        ),
    )
    title, body = pr_writer.build_pr_content(context, llm_enabled=llm_pr_enabled)
    # Reword the commit (still local, not yet pushed) from the mechanical placeholder
    # above to the same title just generated for the PR -- same safe, far-side-only
    # source, not a reopening of v7's leak. See design-history.md's v14 note.
    publish.reword_commit(far_repo, message=f"{title}\n\n{commit_message}")
    result = publish.open_pr(far_repo, branch, base_branch, title, body, token=gh_token)
    print(f"[{label}:{mapping.key}] {result.message}")
    if not result.success:
        # Previously the only outcome that never reached notify.py at all -- a real
        # push/PR-creation failure showed up only as a print, invisible to anyone not
        # reading this run's own logs. Now routed through the same channel as every
        # other halt/error, Slack included.
        notify.comment_on_commit(
            head_sha,
            f"[{project_label}] {mapping.key}: publish failed -- {result.message}",
        )
        return "publish-failed"
    notify.pr_opened(project_label, title, result.message)
    return "opened"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sync-service")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="run the sync for a base..head range")
    run_p.add_argument("--config", required=True)
    run_p.add_argument("--source-repo", required=True, help="the production repo checkout")
    run_p.add_argument("--dest-repo", required=True, help="the OSS repo checkout")
    run_p.add_argument("--base", required=True)
    run_p.add_argument("--head", required=True)
    run_p.add_argument("--base-branch", default="main", help="the far repo's branch to open the PR against")
    run_p.add_argument(
        "--direction",
        choices=["forward", "reverse"],
        default="forward",
        help="forward = production -> OSS (triggered by a production push); "
        "reverse = OSS -> production (triggered by an OSS push)",
    )

    args = parser.parse_args(argv)

    # Captured and removed from the ambient environment before anything else runs --
    # in particular before breakcheck.run() executes any far-side install/run command.
    # Only publish.open_pr()'s one gh pr create call gets it back, explicitly.
    gh_token = os.environ.pop("GH_TOKEN", None)

    if args.command == "run":
        config = SyncConfig.load(args.config)
        source_repo = Path(args.source_repo)
        dest_repo = Path(args.dest_repo)
        by_mapping = {m.key: m for m in config.mappings}

        # A project_name gives the commit author identity a meaningful name too, not
        # just Slack messages -- setdefault so an explicit SYNC_SERVICE_COMMIT_NAME/
        # _EMAIL (set directly in the workflow) always wins over this derived default.
        # publish.py reads these lazily, so setting them here (after config is loaded,
        # before any commit happens) takes effect even though publish was already
        # imported. See design-history.md's v17 note.
        if config.project_name:
            slug = config.project_name.lower().replace(" ", "-")
            os.environ.setdefault("SYNC_SERVICE_COMMIT_NAME", f"{config.project_name} Sync Bot")
            os.environ.setdefault("SYNC_SERVICE_COMMIT_EMAIL", f"{slug}-sync-bot@users.noreply.github.com")

        if args.direction == "forward":
            near_repo, path_attr = source_repo, "source"
        else:
            near_repo, path_attr = dest_repo, "dest"

        files = diff.changed_files(near_repo, args.base, args.head)
        hits = diff.match(files, config.mappings, path_attr=path_attr)

        if not hits:
            print("no mapping touched — no-op")
            return 0

        outcomes = []
        for key in hits:
            m = by_mapping[key]
            if args.direction == "forward":
                outcome = run_direction(
                    mapping=m,
                    near_repo=source_repo, near_path=m.source, near_exclude=m.exclude, near_rules=m.redact,
                    far_repo=dest_repo, far_path=m.dest, far_break_check=m.break_check,
                    head_sha=args.head, base_branch=args.base_branch,
                    branch_prefix="sync", label="sync", gh_token=gh_token,
                    llm_pr_enabled=config.llm_pr.enabled,
                    llm_safety_review_enabled=config.llm_safety_review.enabled,
                    project_name=config.project_name,
                )
            else:
                outcome = run_direction(
                    mapping=m,
                    near_repo=dest_repo, near_path=m.dest, near_exclude=m.exclude, near_rules=m.hydrate,
                    far_repo=source_repo, far_path=m.source, far_break_check=m.reverse_break_check,
                    head_sha=args.head, base_branch=args.base_branch,
                    branch_prefix="reverse-sync", label="reverse-sync", gh_token=gh_token,
                    llm_pr_enabled=config.llm_pr.enabled,
                    llm_safety_review_enabled=config.llm_safety_review.enabled,
                    project_name=config.project_name,
                )
            outcomes.append(outcome)

        # A halt the tool performed correctly (secret/breakcheck/safety-review-halt)
        # is still exit 0 -- that's policy working as intended. A publish failure or
        # a safety review that couldn't even run (misconfigured key, API error,
        # content too large) is a real problem with this run and must not be
        # silently reported as success -- see design-history.md's v9/v12 notes for
        # why each of those distinctions exists.
        if "publish-failed" in outcomes or "safety-review-error" in outcomes:
            return 1
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
