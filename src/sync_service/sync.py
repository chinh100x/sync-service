"""CLI entrypoint — production -> OSS only, no conflict tracking.

No manifest, no divergence detection: a run always overwrites the OSS repo's
tracked files with production's current content. If the OSS side has its own
edits outside this tool, they're overwritten with no warning — deliberately
simpler than tracking state, at the cost of ever detecting an outside edit
before clobbering it.

One real OSS commit per production commit in base..head, each keeping its
own original message and author -- see patch.py's module docstring for how
each commit's changes are resolved (git-structural, never diff-text parsing).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .lib import (
    breakcheck,
    diff,
    notify,
    patch,
    pr_writer,
    publish,
    safety_review,
    scrub,
    secretscan,
)
from .lib.config import Mapping, SyncConfig


def _halt(head_sha: str, message: str, outcome: str) -> str:
    """Every halt notifies the same way (comment on the source commit + Slack)
    before returning its outcome string -- collapses that two-line pattern to one
    call site per gate. Skipped for the one halt (breakcheck) that has a side
    effect between notifying and returning."""
    notify.comment_on_commit(head_sha, message)
    return outcome


def _replay_one_commit(
    *,
    mapping: Mapping,
    source_repo: Path,
    dest_repo: Path,
    sha: str,
    llm_safety_review_enabled: bool,
    llm_safety_review_additional_context: str | None,
) -> tuple[bool, list[str], list[str]] | str:
    """One step of the replay loop: resolve this commit's changed paths, redact
    + scan the result, scan its message, then write/delete and commit -- or
    return a halt reason string on the first thing that doesn't pass (a policy
    decision: a submodule, or a secretscan/blocked-verdict hit on either the
    files or the message). No "conflict" concept here (see patch.py's module
    docstring) -- every write is an unconditional overwrite; there's no
    divergence detection.

    Raises safety_review.SafetyReviewUnavailable uncaught -- an infra failure,
    not a policy decision; the caller (run_mapping's loop) catches it
    separately to produce a distinct exit-1 outcome instead of a generic halt.

    Success returns (committed, scrubbed_categories, touched_paths); `committed`
    is False for a commit that scoped down to nothing after exclude-filtering
    (still not a halt -- just a no-op step)."""
    changed = [
        p
        for p in patch.changed_paths(source_repo, sha, mapping.source)
        if not patch.is_excluded(p, mapping.exclude)
    ]
    if not changed:
        return (False, [], [])

    try:
        resolved = [
            patch.resolve_change(source_repo, sha, mapping.source, mapping.dest, p) for p in changed
        ]
    except patch.SubmoduleNotSupported as exc:
        return (
            f"touches a submodule ({exc}), which has no file content to scrub -- "
            "refusing to replay it automatically"
        )

    categories: set[str] = set()
    written: list[str] = []
    deleted: list[str] = []
    for change in resolved:
        if change.kind == "skip":
            continue  # binary -- never propagated, no mechanical redaction possible
        dest_file = dest_repo / change.dest_path
        if change.kind == "delete":
            dest_file.unlink(missing_ok=True)
            deleted.append(change.dest_path)
            continue
        assert change.content is not None  # guaranteed by ResolvedChange's "write" contract
        redacted, fired = scrub.redact_text(change.content, mapping.redact)
        categories |= set(fired)
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        dest_file.write_text(redacted)
        written.append(change.dest_path)

    if not written and not deleted:
        return (False, [], [])

    # --- secretscan.py + safety_review.py: the hard secret-scan gate and the
    # semantic gate, against just what this commit actually wrote. ---
    file_contents = {p: (dest_repo / p).read_text() for p in written}
    hits = secretscan.scan(file_contents)
    if hits:
        return f"secret scan hit: {hits[0]['rule']} in {hits[0]['path']}"

    # SafetyReviewUnavailable deliberately propagates uncaught -- it's an infra
    # failure (missing key, API error), not a policy decision, and run_mapping's
    # loop catches it separately to produce a distinct "safety-review-error"
    # (exit 1) outcome instead of a generic policy halt (exit 0).
    verdict = safety_review.review(
        safety_review.SafetyReviewContext(mapping_key=mapping.key, files=file_contents),
        enabled=llm_safety_review_enabled,
        additional_context=llm_safety_review_additional_context,
    )
    if verdict is not None and not verdict.passed:
        categories_note = (
            f" (categories: {', '.join(verdict.categories)})" if verdict.categories else ""
        )
        return f"semantic safety review blocked this change: {verdict.summary}{categories_note}"

    # --- The same two gates, run against the commit's own message -- the raw
    # message is used as-is once it clears these, unlike file content (which
    # also goes through scrub.redact_text above). ---
    message = diff.commit_message(source_repo, sha)
    message_hits = secretscan.scan({"<commit message>": message})
    if message_hits:
        return f"secret scan hit in commit message: {message_hits[0]['rule']}"

    # Same as above -- SafetyReviewUnavailable propagates uncaught here too.
    message_verdict = safety_review.review_message(
        message,
        enabled=llm_safety_review_enabled,
        additional_context=llm_safety_review_additional_context,
    )
    if message_verdict is not None and not message_verdict.passed:
        categories_note = (
            f" (categories: {', '.join(message_verdict.categories)})"
            if message_verdict.categories
            else ""
        )
        return (
            f"commit message blocked by safety review: {message_verdict.summary}{categories_note}"
        )

    author = diff.commit_author(source_repo, sha)
    committed = publish.commit_all(dest_repo, message=message, author=author)
    return (committed, sorted(categories), written + deleted)


def run_mapping(
    *,
    mapping: Mapping,
    source_repo: Path,
    dest_repo: Path,
    base_sha: str,
    head_sha: str,
    base_branch: str,
    gh_token: str | None,
    llm_pr_enabled: bool,
    llm_safety_review_enabled: bool,
    llm_safety_review_additional_context: str | None,
    project_name: str | None,
) -> str:
    """One real OSS commit per production commit in base..head, each keeping
    its own message and author. All-or-nothing: a gate failure on any commit
    discards every commit made so far in this batch (see
    publish.discard_branch_and_reset) -- nothing partial ever reaches origin."""
    # Falls back to the mechanical `sync:mapping_key` prefix when project_name isn't set.
    project_label = project_name or f"sync:{mapping.key}"
    # Otherwise a prior mapping/halt in the same run can leave dest_repo checked out
    # on its own branch, nesting this mapping's commit inside that one's PR.
    publish.checkout_base(dest_repo, base_branch)

    # --- Idempotency: has this exact (mapping, head_sha) already been proposed? ---
    # Tracked via a dedicated ref, not the branch name -- the final name is a clean,
    # title-derived slug with no sha in it, so it can't double as the idempotency key.
    if publish.already_synced(dest_repo, mapping.key, head_sha):
        print(
            f"[sync:{mapping.key}] PR already exists for this (mapping, head_sha) "
            "— skipping (idempotent re-run)"
        )
        return _halt(
            head_sha,
            f"[{project_label}] {mapping.key}: PR already exists for this commit "
            "— skipping (idempotent re-run).",
            "skipped-exists",
        )

    # --- patch.py: which source commits does this mapping actually need to replay? ---
    commits = patch.commits_between(source_repo, base_sha, head_sha, mapping.source)
    if not commits:
        if patch.merge_commits_between(source_repo, base_sha, head_sha, mapping.source):
            return _halt(
                head_sha,
                f"[{project_label}] {mapping.key}: only a merge commit touches "
                f"{mapping.source}/ in this range -- replay can't linearize a merge "
                "automatically. Halted, no PR.",
                "replay-halt",
            )
        print(f"[sync:{mapping.key}] no commits under {mapping.source}/ to replay")
        return _halt(
            head_sha,
            f"[{project_label}] {mapping.key}: no commits under {mapping.source}/ in "
            "base..head — no-op, no PR.",
            "empty",
        )

    branch = publish.branch_name(f"sync/{mapping.key}", head_sha)
    publish.create_branch(dest_repo, branch)

    replayed = 0
    all_touched: set[str] = set()
    all_categories: set[str] = set()
    for sha in commits:
        try:
            outcome = _replay_one_commit(
                mapping=mapping,
                source_repo=source_repo,
                dest_repo=dest_repo,
                sha=sha,
                llm_safety_review_enabled=llm_safety_review_enabled,
                llm_safety_review_additional_context=llm_safety_review_additional_context,
            )
        except safety_review.SafetyReviewUnavailable as exc:
            # An infra failure (missing key, API error), not a policy decision --
            # a hard halt, never an implicit pass, and distinct from the generic
            # "replay-halt" below so main() can exit 1 instead of 0 for it.
            publish.discard_branch_and_reset(dest_repo, base_branch, branch)
            return _halt(
                head_sha,
                f"[{project_label}] {mapping.key}: semantic safety review unavailable "
                f"({exc}) -- halted out of caution, no PR.",
                "safety-review-error",
            )
        if isinstance(outcome, str):
            # Discards every commit made so far this batch, not just this one --
            # nothing partial from a halted replay may ever reach origin.
            publish.discard_branch_and_reset(dest_repo, base_branch, branch)
            return _halt(
                head_sha,
                f"[{project_label}] {mapping.key}: replay halted at commit "
                f"{sha[:12]} -- {outcome}. Halted, no PR.",
                "replay-halt",
            )
        committed, categories, touched = outcome
        all_categories |= set(categories)
        all_touched |= set(touched)
        if committed:
            replayed += 1
        print(f"[sync:{mapping.key}] replayed {sha[:12]} ({'committed' if committed else 'no-op'})")

    if replayed == 0:
        publish.discard_branch_and_reset(dest_repo, base_branch, branch)
        print(
            f"[sync:{mapping.key}] nothing changed vs the OSS side across {len(commits)} commit(s)"
        )
        return _halt(
            head_sha,
            f"[{project_label}] {mapping.key}: scrubbed content matches what's "
            f"already on the OSS side across {len(commits)} commit(s) — nothing to "
            "commit, no PR.",
            "unchanged",
        )

    # --- breakcheck.py: runs once, against the final replayed state -- not once
    # per replayed commit. An intermediate commit is a real historical waypoint,
    # not something that individually needs to install/build/pass on its own. ---
    if mapping.break_check is not None:
        check = breakcheck.run(dest_repo, mapping.break_check)
        if not check.passed:
            notify.comment_on_commit(
                head_sha,
                f"[{project_label}] {mapping.key}: break check failed at "
                f"`{check.failed_step}`:\n```\n{check.output}\n```",
            )
            publish.discard_branch_and_reset(dest_repo, base_branch, branch)
            return "breakcheck-halt"

    # --- pr_writer.py + publish.py: generate the PR content, then publish it. ---
    return _finalize_and_publish(
        mapping=mapping,
        dest_repo=dest_repo,
        base_branch=base_branch,
        branch=branch,
        head_sha=head_sha,
        project_label=project_label,
        changed_files=sorted(all_touched),
        scrubbed_categories=sorted(all_categories),
        llm_pr_enabled=llm_pr_enabled,
        gh_token=gh_token,
    )


def _finalize_and_publish(
    *,
    mapping: Mapping,
    dest_repo: Path,
    base_branch: str,
    branch: str,
    head_sha: str,
    project_label: str,
    changed_files: list[str],
    scrubbed_categories: list[str],
    llm_pr_enabled: bool,
    gh_token: str | None,
) -> str:
    """The one place this tool tries to be human-readable rather than purely
    mechanical. Built only from already-scrubbed, already-validated OSS-side
    content; falls back to a deterministic title/body on any failure -- pr_writer
    fails *open*, the opposite of safety_review's fail-closed behavior above.

    The generated title is used for the PR itself only -- it never overwrites
    any replayed commit's own message."""
    context = pr_writer.PRContext(
        mapping_key=mapping.key,
        public_reason=mapping.public_reason,
        changed_files=changed_files,
        sanitized_diff=diff.candidate_diff(dest_repo, base_branch, branch),
        scrubbed_categories=scrubbed_categories,
        validation=pr_writer.ValidationSummary(run_command=mapping.break_check.run),
    )
    title, body = pr_writer.build_pr_content(context, llm_enabled=llm_pr_enabled)
    # Clean, title-derived name -- idempotency doesn't depend on it (see
    # already_synced above), so it carries no sha except when disambiguating an
    # actual name collision (not a re-run, which already_synced already caught).
    branch = publish.slugify(title)
    if publish.branch_exists(dest_repo, branch):
        branch = f"{branch}-{head_sha[:7]}"
    publish.rename_branch(dest_repo, branch)
    result = publish.open_pr(dest_repo, branch, base_branch, title, body, token=gh_token)
    print(f"[sync:{mapping.key}] {result.message}")
    if not result.success:
        return _halt(
            head_sha,
            f"[{project_label}] {mapping.key}: publish failed -- {result.message}",
            "publish-failed",
        )
    # Only mark synced once publish actually succeeded -- a halt/failure must
    # still be retried on the next run, not silently skipped.
    publish.record_synced(dest_repo, mapping.key, head_sha, token=gh_token)
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
    run_p.add_argument(
        "--base-branch", default="main", help="the OSS repo's branch to open the PR against"
    )

    args = parser.parse_args(argv)

    # Removed from the environment before breakcheck.run() executes any OSS-side
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
            os.environ.setdefault(
                "SYNC_SERVICE_COMMIT_EMAIL", f"{slug}-sync-bot@users.noreply.github.com"
            )

        # --- diff.py: what changed, which mappings matched (the trigger). ---
        files = diff.changed_files(source_repo, args.base, args.head)
        hits = diff.match(files, config.mappings)

        if not hits:
            # No Slack/comment here, unlike every outcome inside run_mapping below --
            # this fires on *every* push that doesn't touch any tracked mapping at
            # all, which for a repo without a narrow path filter on its own trigger
            # is most commits. There's no mapping-specific reason to report, and
            # notifying here would turn Slack into a log of unrelated pushes.
            print("no mapping touched — no-op")
            return 0

        outcomes = []
        for key in hits:
            m = by_mapping[key]
            outcomes.append(
                run_mapping(
                    mapping=m,
                    source_repo=source_repo,
                    dest_repo=dest_repo,
                    base_sha=args.base,
                    head_sha=args.head,
                    base_branch=args.base_branch,
                    gh_token=gh_token,
                    llm_pr_enabled=config.llm_pr.enabled,
                    llm_safety_review_enabled=config.llm_safety_review.enabled,
                    llm_safety_review_additional_context=config.llm_safety_review.additional_context,
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
