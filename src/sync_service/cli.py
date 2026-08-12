"""CLI entrypoint — v2, bidirectional.

`sync-service run --direction forward` is production -> OSS (redact, break_check).
`sync-service run --direction reverse` is OSS -> production (hydrate, reverse_break_check).
Both directions run through the same engine (`run_direction`) with source/dest and
redact/hydrate swapped — the mechanism is identical either way.

Either direction, on finding that the far side already changed something since the
last sync (a real outside contribution), also raises a *separate* proposal to carry
that edit back to whichever side it originated from — see `_propose_reverse` — so a
change is never silently overwritten or silently dropped, regardless of which repo's
push triggered this run.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from . import breakcheck, diff, notify, publish, scrub, secretscan, state
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
    reverse_rules: list[RedactRule],
) -> str:
    """near = the side whose commit triggered this run; far = the side we're proposing to."""
    branch = publish.branch_name(f"{branch_prefix}/{mapping.key}", head_sha)
    if publish.branch_exists(far_repo, branch):
        print(f"[{label}:{mapping.key}] PR already exists for this (mapping, head_sha) — skipping (idempotent re-run)")
        return "skipped-exists"

    desired = scrub.apply(near_repo, near_path, far_path, near_exclude, near_rules)
    if not desired:
        print(f"[{label}:{mapping.key}] nothing under {near_path}/ to propagate")
        return "empty"
    print(f"[{label}:{mapping.key}] scrubbed {len(desired)} file(s) -> {far_path}/")

    hits = secretscan.scan(desired)
    if hits:
        notify.comment_on_commit(
            head_sha,
            f"[{label}] secret scan hit in {mapping.key}: {hits[0]['rule']} in {hits[0]['path']}. Halted, no PR.",
        )
        return "secret-halt"

    manifest = state.load(far_repo, mapping.key)
    classified = state.classify(far_repo, manifest, desired, mapping.key)

    conflicts = [p for p, r in classified.items() if r.status == "conflict"]
    if conflicts:
        lines = [f"  - {p}: far side and this change both touched it" for p in conflicts]
        notify.comment_on_commit(
            head_sha,
            f"[{label}] {mapping.key}: sync halted, manual reconciliation needed "
            f"(auto-merge attempted, could not resolve cleanly):\n" + "\n".join(lines),
        )
        return "conflict-halt"

    diverged = [p for p, r in classified.items() if r.status == "merged"]
    if diverged:
        _propose_reverse(
            mapping=mapping,
            label=label,
            origin_repo=far_repo,
            origin_path=far_path,
            target_repo=near_repo,
            target_path=near_path,
            transform_rules=reverse_rules,
            base_branch=base_branch,
            diverged_paths=diverged,
        )

    for rel_path, result in classified.items():
        far_file = far_repo / rel_path
        far_file.parent.mkdir(parents=True, exist_ok=True)
        far_file.write_text(result.write_content)
    state.write(far_repo, mapping.key, head_sha, {p: r.write_content for p, r in classified.items()})

    if far_break_check is not None:
        check = breakcheck.run(far_repo, far_break_check)
        if not check.passed:
            notify.comment_on_commit(
                head_sha,
                f"[{label}] {mapping.key}: break check failed at `{check.failed_step}`:\n{check.output}",
            )
            publish.discard_working_tree_changes(far_repo)
            return "breakcheck-halt"
        break_note = "Break check passed."
    else:
        print(f"[{label}:{mapping.key}] no break check configured for this direction — relying on the far repo's own CI")
        break_note = "No break check configured for this direction."

    original_message = diff.commit_message(near_repo, head_sha)
    original_subject = original_message.splitlines()[0] if original_message else f"@ {head_sha[:12]}"
    commit_message = f"{original_message}\n\nSync-Source: {mapping.key} @ {head_sha[:12]} ({label})"
    publish.commit_to_branch(far_repo, branch, message=commit_message)
    title = f"[{label}] {mapping.key}: {original_subject}"
    body = (
        f"Automated {label} sync, mapping `{mapping.key}` (`{near_path}` -> `{far_path}`).\n\n"
        f"Files changed: {', '.join(sorted(classified))}\n\n"
        + (f"Auto-merged with a concurrent far-side edit on: {', '.join(sorted(diverged))}.\n\n" if diverged else "")
        + f"{break_note} Nothing auto-merges — human review required."
    )
    result = publish.open_pr(far_repo, branch, base_branch, title, body)
    print(f"[{label}:{mapping.key}] {result}")
    return "opened"


def _propose_reverse(
    *,
    mapping: Mapping,
    label: str,
    origin_repo: Path,
    origin_path: str,
    target_repo: Path,
    target_path: str,
    transform_rules: list[RedactRule],
    base_branch: str,
    diverged_paths: list[str],
) -> None:
    """The far side had its own edit since the last sync — an outside contribution this
    run didn't originate. Propose carrying it back to the other side on its own PR,
    independent of whatever this run is otherwise doing with it. Not break-checked;
    review it like any other external change. Skips silently if already proposed."""
    origin_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=origin_repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    branch = publish.branch_name(f"reverse-sync/{mapping.key}", origin_head)
    if publish.branch_exists(target_repo, branch):
        return

    desired = scrub.apply(origin_repo, origin_path, target_path, [], transform_rules)
    if not desired:
        return

    hits = secretscan.scan(desired)
    if hits:
        notify.comment_on_commit(
            origin_head,
            f"[{label}] reverse-proposal secret scan hit for {mapping.key}: "
            f"{hits[0]['rule']} in {hits[0]['path']}. Not proposed.",
        )
        return

    for rel_path, text in desired.items():
        target_file = target_repo / rel_path
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text(text)

    original_message = diff.commit_message(origin_repo, origin_head)
    commit_message = f"{original_message}\n\nSync-Source: {mapping.key} @ {origin_head[:12]} (reverse-proposal)"
    publish.commit_to_branch(target_repo, branch, message=commit_message)
    title = f"[reverse-sync] {mapping.key}: bring in an outside edit ({', '.join(sorted(diverged_paths))})"
    body = (
        f"An edit landed on the other side of `{mapping.key}` since the last sync, "
        f"outside this service: {', '.join(sorted(diverged_paths))}.\n\n"
        "Proposing it back here so it isn't silently lost or overwritten. Not "
        "break-checked automatically — review like any other external change."
    )
    result = publish.open_pr(target_repo, branch, base_branch, title, body)
    print(f"[{label}:{mapping.key}] reverse proposal: {result}")


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

    if args.command == "run":
        config = SyncConfig.load(args.config)
        source_repo = Path(args.source_repo)
        dest_repo = Path(args.dest_repo)
        by_mapping = {m.key: m for m in config.mappings}

        if args.direction == "forward":
            near_repo, path_attr = source_repo, "source"
        else:
            near_repo, path_attr = dest_repo, "dest"

        files = diff.changed_files(near_repo, args.base, args.head)
        hits = diff.match(files, config.mappings, path_attr=path_attr)
        if not hits:
            print("no mapping touched — no-op")
            return 0

        for key in hits:
            m = by_mapping[key]
            if args.direction == "forward":
                run_direction(
                    mapping=m,
                    near_repo=source_repo, near_path=m.source, near_exclude=m.exclude, near_rules=m.redact,
                    far_repo=dest_repo, far_path=m.dest, far_break_check=m.break_check,
                    head_sha=args.head, base_branch=args.base_branch,
                    branch_prefix="sync", label="sync", reverse_rules=m.hydrate,
                )
            else:
                run_direction(
                    mapping=m,
                    near_repo=dest_repo, near_path=m.dest, near_exclude=[], near_rules=m.hydrate,
                    far_repo=source_repo, far_path=m.source, far_break_check=m.reverse_break_check,
                    head_sha=args.head, base_branch=args.base_branch,
                    branch_prefix="reverse-sync", label="reverse-sync", reverse_rules=m.redact,
                )
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
