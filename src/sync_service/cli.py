"""CLI entrypoint — bidirectional, no conflict tracking.

`sync-service run --direction forward` is production -> OSS (redact, break_check).
`sync-service run --direction reverse` is OSS -> production (hydrate, reverse_break_check).
Both directions run through the same engine (`run_direction`) with source/dest and
redact/hydrate swapped — the mechanism is identical either way.

No manifest, no divergence detection: a run always overwrites the far side's tracked
files with the near side's current content. If the far side has its own edits outside
this tool, they're overwritten with no warning — deliberately simpler than tracking
state, at the cost of ever detecting an outside edit before clobbering it.
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
    # Falls back to the mechanical `label:mapping_key` prefix when project_name isn't set.
    project_label = project_name or f"{label}:{mapping.key}"
    # Otherwise a prior mapping/halt in the same run can leave far_repo checked out
    # on its own branch, nesting this mapping's commit inside that one's PR.
    publish.checkout_base(far_repo, base_branch)

    # Tracked via a dedicated ref, not the branch name -- the final name is a clean,
    # title-derived slug with no sha in it, so it can't double as the idempotency key.
    if publish.already_synced(far_repo, mapping.key, head_sha):
        print(f"[{label}:{mapping.key}] PR already exists for this (mapping, head_sha) — skipping (idempotent re-run)")
        return "skipped-exists"

    # Temporary working name -- candidate_diff() below needs a real commit on a real
    # branch before the title (and final branch name) can be generated.
    branch = publish.branch_name(f"{branch_prefix}/{mapping.key}", head_sha)

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

    # Fails *closed*: SafetyReviewUnavailable is a hard halt, never an implicit pass.
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

    # Never the raw production commit message -- free-form human text that never
    # goes through scrub/secretscan. Just a placeholder: reword_commit below
    # replaces it with the generated title before anything is pushed.
    commit_message = f"sync: {mapping.key} @ {head_sha[:12]}"
    # Credits the real near-side committer as Author, distinct from the bot Committer.
    author = diff.commit_author(near_repo, head_sha)
    committed = publish.commit_to_branch(far_repo, branch, message=commit_message, author=author)
    if not committed:
        print(f"[{label}:{mapping.key}] nothing changed vs the far side — no PR")
        return "unchanged"

    # The one place this tool tries to be human-readable rather than purely
    # mechanical. Built only from already-scrubbed, already-validated far-side
    # content; falls back to a deterministic title/body on any failure.
    context = pr_writer.PRContext(
        mapping_key=mapping.key,
        public_reason=mapping.public_reason,
        changed_files=sorted(desired),
        sanitized_diff=diff.candidate_diff(far_repo, base_branch, branch),
        scrubbed_categories=scrubbed_categories,
        validation=pr_writer.ValidationSummary(
            run_command=far_break_check.run if far_break_check is not None else None,
        ),
    )
    title, body = pr_writer.build_pr_content(context, llm_enabled=llm_pr_enabled)
    # Swap the placeholder for the generated title -- same safe, far-side-only
    # source as the PR body. No mechanical trailer: the sha it would carry is
    # already recoverable from record_synced's tracking ref.
    publish.reword_commit(far_repo, message=title)
    # Clean, title-derived name -- idempotency doesn't depend on it (see
    # already_synced above), so it carries no sha except when disambiguating an
    # actual name collision (not a re-run, which already_synced already caught).
    branch = publish.slugify(title)
    if publish.branch_exists(far_repo, branch):
        branch = f"{branch}-{head_sha[:7]}"
    publish.rename_branch(far_repo, branch)
    result = publish.open_pr(far_repo, branch, base_branch, title, body, token=gh_token)
    print(f"[{label}:{mapping.key}] {result.message}")
    if not result.success:
        notify.comment_on_commit(
            head_sha,
            f"[{project_label}] {mapping.key}: publish failed -- {result.message}",
        )
        return "publish-failed"
    # Only mark synced once publish actually succeeded -- a halt/failure must
    # still be retried on the next run, not silently skipped.
    publish.record_synced(far_repo, mapping.key, head_sha, token=gh_token)
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

    # Removed from the environment before breakcheck.run() executes any far-side
    # command; only open_pr()'s one gh pr create call gets it back, explicitly.
    gh_token = os.environ.pop("GH_TOKEN", None)

    if args.command == "run":
        config = SyncConfig.load(args.config)
        source_repo = Path(args.source_repo)
        dest_repo = Path(args.dest_repo)
        by_mapping = {m.key: m for m in config.mappings}

        # setdefault so an explicit SYNC_SERVICE_COMMIT_NAME/_EMAIL always wins.
        # publish.py reads these lazily, so setting them here (after config load,
        # before any commit) takes effect despite publish already being imported.
        if config.project_name:
            slug = config.project_name.lower().replace(" ", "-")
            os.environ.setdefault("SYNC_SERVICE_COMMIT_NAME", f"{config.project_name} Sync Bot")
            os.environ.setdefault("SYNC_SERVICE_COMMIT_EMAIL", f"{slug}-sync-bot@users.noreply.github.com")

        # Attributes swapped between directions; everything else in run_direction
        # is identical either way.
        if args.direction == "forward":
            near_repo, far_repo = source_repo, dest_repo
            near_attr, far_attr, rules_attr, break_attr = "source", "dest", "redact", "break_check"
            branch_prefix = label = "sync"
        else:
            near_repo, far_repo = dest_repo, source_repo
            near_attr, far_attr, rules_attr, break_attr = "dest", "source", "hydrate", "reverse_break_check"
            branch_prefix = label = "reverse-sync"

        files = diff.changed_files(near_repo, args.base, args.head)
        hits = diff.match(files, config.mappings, path_attr=near_attr)

        if not hits:
            print("no mapping touched — no-op")
            return 0

        outcomes = []
        for key in hits:
            m = by_mapping[key]
            outcomes.append(
                run_direction(
                    mapping=m,
                    near_repo=near_repo, near_path=getattr(m, near_attr), near_exclude=m.exclude,
                    near_rules=getattr(m, rules_attr),
                    far_repo=far_repo, far_path=getattr(m, far_attr), far_break_check=getattr(m, break_attr),
                    head_sha=args.head, base_branch=args.base_branch,
                    branch_prefix=branch_prefix, label=label, gh_token=gh_token,
                    llm_pr_enabled=config.llm_pr.enabled,
                    llm_safety_review_enabled=config.llm_safety_review.enabled,
                    project_name=config.project_name,
                )
            )

        # A halt the tool performed correctly is still exit 0 -- that's policy
        # working as intended. A publish failure or a safety review that
        # couldn't even run is a real problem with this run and must not be
        # silently reported as success.
        if "publish-failed" in outcomes or "safety-review-error" in outcomes:
            return 1
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
