"""End-to-end demo of the v2 (bidirectional) sync service against two local git repos
standing in for "the production repo" and "the open source repo" — no GitHub needed.

Walks through:
  STEP 1 — first sync, two mappings at once (forward)
  STEP 2 — secret scan halt (forward)
  STEP 3 — outside OSS edit + a non-overlapping prod edit -> auto three-way merge,
           PLUS a separate reverse-sync PR proposing the outsider's edit back to prod
  STEP 4 — outside OSS edit + a prod edit that touch the *same* line -> real conflict,
           still halts (auto-resolution only fires when it's unambiguous)
  STEP 5 — break check failure (forward), working tree reverted
  STEP 6 — no-op (touched file isn't under any mapping)
  STEP 7 — idempotent re-run of step 1's range -> skipped
  STEP 8 — an OSS commit, run explicitly with --direction reverse -> a real PR onto
           the production repo, with `hydrate` restoring the real endpoint

Run with:  uv run python demo/run_demo.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sync_service import cli, state  # noqa: E402

GIT_ID = ["-c", "user.name=demo", "-c", "user.email=demo@example.com"]


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *GIT_ID, *args], cwd=repo, capture_output=True, text=True, check=check)


def rev(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text))


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)
    return rev(repo)


def header(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def main() -> None:
    workspace = Path(tempfile.mkdtemp(prefix="sync-service-demo-"))
    prod = workspace / "prod-repo"
    oss = workspace / "oss-repo"
    prod.mkdir()
    oss.mkdir()
    git(prod, "init", "-q", "-b", "main")
    git(oss, "init", "-q", "-b", "main")
    print(f"workspace: {workspace}")

    # ---- prod repo, sha0: initial state ----
    write(prod, "README.md", "# production repo (demo)\n")
    write(prod, "src/portmon/covenant.py", "def check():\n    return True\n")
    sha0 = commit(prod, "initial")

    # sync/monitoring.yaml lives in the prod repo, next to the code it maps. redact/hydrate
    # are inverses of the same rule: redact scrubs prod -> OSS, hydrate restores OSS -> prod.
    write(
        prod,
        "sync/monitoring.yaml",
        f"""\
        mappings:
          - key: portmon
            source: src/portmon
            dest: plugin
            exclude:
              - src/portmon/internal_reporting.py
            redact:
              - pattern: 'https://cag-mcp\\.internal[^\\s"]*'
                replace: '<MCP_ENDPOINT>'
            hydrate:
              - pattern: '<MCP_ENDPOINT>'
                replace: 'https://cag-mcp.internal/v1/report'
            break_check:
              install: "true"
              run: "python3 plugin/covenant.py"
          - key: brk
            source: src/brk
            dest: brk
            hydrate:
              - pattern: '<MCP_ENDPOINT>'
                replace: 'https://cag-mcp.internal/v1/report'
            break_check:
              install: "true"
              run: "python3 brk/mod.py"
        """,
    )
    commit(prod, "add sync config")

    # ---- oss repo, initial state (plugin/ and brk/ don't exist yet) ----
    write(oss, "README.md", "# open source repo (demo)\n")
    commit(oss, "initial")

    config_path = prod / "sync" / "monitoring.yaml"

    def run(base: str, head: str, label: str, direction: str = "forward") -> None:
        header(label)
        cli.main(
            [
                "run",
                "--config", str(config_path),
                "--source-repo", str(prod),
                "--dest-repo", str(oss),
                "--base", base,
                "--head", head,
                "--direction", direction,
            ]
        )

    # ---- sha1: first real propagation, two mappings touched at once ----
    write(
        prod,
        "src/portmon/covenant.py",
        """\
        ENDPOINT = "https://cag-mcp.internal/v1/report"

        def check():
            print("checked against", ENDPOINT)
            return True
        """,
    )
    write(prod, "src/portmon/internal_reporting.py", "SECRET_TENANT_ID = 'do-not-ship'\n")
    write(prod, "src/brk/mod.py", "def run():\n    print('brk ok')\n\nif __name__ == '__main__':\n    run()\n")
    sha1 = commit(prod, "portmon: add MCP reporting; brk: initial tool")

    run(sha0, sha1, "STEP 1 — first sync: two mappings, both pass, PR opened (dry-run) for each")

    # simulate a human approving + merging both sync PRs into oss main
    git(oss, "checkout", "main")
    for key in ("portmon", "brk"):
        git(oss, "merge", "--no-ff", f"sync/{key}/{sha1[:12]}", "-m", f"merge sync PR: {key}")
    print("(demo) merged both sync PRs into oss main, as if a human approved them")

    # ---- sha2: secret sneaks into covenant.py -> secret-halt ----
    write(
        prod,
        "src/portmon/covenant.py",
        """\
        ENDPOINT = "https://cag-mcp.internal/v1/report"
        API_KEY = "sk_live_abcdef0123456789"

        def check():
            print("checked against", ENDPOINT)
            return True
        """,
    )
    sha2 = commit(prod, "portmon: (accidentally) hardcode an API key")
    run(sha1, sha2, "STEP 2 — secret scan hit -> halted, no PR")

    # ---- outside contributor edits plugin/covenant.py on oss main — a comment, non-overlapping ----
    write(
        oss,
        "plugin/covenant.py",
        """\
        ENDPOINT = "https://cag-mcp.internal/v1/report"

        # outside contributor: noting this is polled every 5 minutes
        def check():
            print("checked against", ENDPOINT)
            return True
        """,
    )
    commit(oss, "outside PR: add a comment to covenant.py")
    oss_head_before_step3 = rev(oss)
    print("(demo) an outside contributor merged a comment-only change to plugin/covenant.py on oss main")

    # ---- sha3: prod fixes the secret AND adds a new function — also non-overlapping ----
    write(
        prod,
        "src/portmon/covenant.py",
        """\
        ENDPOINT = "https://cag-mcp.internal/v1/report"

        def check():
            print("checked against", ENDPOINT)
            return True

        def audit():
            return "ok"
        """,
    )
    sha3 = commit(prod, "portmon: remove the API key, add audit()")
    run(sha2, sha3, "STEP 3 — non-overlapping edits on both sides -> auto three-way merge + reverse-sync proposal")

    print("\n-- what the auto-merge produced (still on the sync/portmon branch) --")
    merged_text = git(oss, "show", f"sync/portmon/{sha3[:12]}:plugin/covenant.py").stdout
    print("has outsider's comment:", "polled every 5 minutes" in merged_text)
    print("has prod's audit():", "def audit():" in merged_text)

    print("\n-- what the reverse-sync proposal put on the prod repo --")
    reverse_branch = f"reverse-sync/portmon/{oss_head_before_step3[:12]}"
    proposed_text = git(prod, "show", f"{reverse_branch}:src/portmon/covenant.py").stdout
    print(f"branch: {reverse_branch}")
    print("proposed content has real endpoint (hydrated, not the placeholder):",
          "cag-mcp.internal" in proposed_text and "<MCP_ENDPOINT>" not in proposed_text)

    # human approves both: the merged forward PR, and the reverse-sync proposal
    git(oss, "checkout", "main")
    git(oss, "merge", "--no-ff", f"sync/portmon/{sha3[:12]}", "-m", "merge sync PR: portmon (auto-merged)")
    git(prod, "checkout", "main")
    git(prod, "merge", "--no-ff", reverse_branch, "-m", "merge reverse-sync PR: portmon")
    print("(demo) human approved both the auto-merged forward PR and the reverse-sync proposal")

    # ---- sha4: outsider AND prod both edit the exact same line -> real conflict ----
    write(
        oss,
        "plugin/covenant.py",
        git(oss, "show", "HEAD:plugin/covenant.py").stdout.replace("return True", "return False  # outsider disagrees"),
    )
    commit(oss, "outside PR: check() should report False during maintenance")
    print("(demo) an outside contributor merged a change to the same line prod is about to touch")

    sha4_base = rev(prod)
    write(
        prod,
        "src/portmon/covenant.py",
        git(prod, "show", "HEAD:src/portmon/covenant.py").stdout.replace("return True", "return 'ok'  # prod refactor"),
    )
    sha4 = commit(prod, "portmon: refactor check()'s return value")
    run(sha4_base, sha4, "STEP 4 — same line edited on both sides -> real conflict, halted (no auto-resolution)")

    # ---- sha5: brk gets a real bug -> breakcheck-halt ----
    sha5_base = rev(prod)
    write(prod, "src/brk/mod.py", "def run():\n    raise RuntimeError('boom')\n\nif __name__ == '__main__':\n    run()\n")
    sha5 = commit(prod, "brk: introduce a bug")
    run(sha5_base, sha5, "STEP 5 — break check fails -> halted, no PR, working tree reverted")
    print("brk/mod.py on oss main still has the old, working version:",
          "boom" not in (oss / "brk" / "mod.py").read_text())

    # ---- sha5b: human fixes the bug and retries — design.md §3's retry path ----
    fixed_brk = "def run():\n    print('brk ok')\n\nif __name__ == '__main__':\n    run()\n"
    write(prod, "src/brk/mod.py", fixed_brk)
    sha5b = commit(prod, "brk: fix the bug")
    run(sha5, sha5b, "STEP 5b — bug fixed, forward sync succeeds on retry")

    # bootstrap the reverse-direction manifest for brk: a one-time step (see DEPLOY.md)
    # so the *next* OSS -> production check has a baseline to compare against, instead
    # of treating production's existing file as unknown-provenance on its very first check.
    state.write(prod, "brk", sha5b, {"src/brk/mod.py": fixed_brk})
    print("(demo) bootstrap: seeded brk's reverse-direction manifest on the prod repo")

    # ---- sha6: touches only README, no mapping matches -> no-op ----
    sha6_base = rev(prod)
    write(prod, "README.md", "# production repo (demo)\nupdated\n")
    sha6 = commit(prod, "docs: update README")
    run(sha6_base, sha6, "STEP 6 — no changed file matches any mapping -> no-op")

    # ---- re-run sha0..sha1: PR already exists for (mapping, head_sha) -> skip ----
    run(sha0, sha1, "STEP 7 — idempotent re-run of step 1's range -> skipped, PR already exists")

    # ---- STEP 8: an OSS-side commit, driven explicitly with --direction reverse ----
    oss_base = rev(oss)
    write(
        oss,
        "brk/mod.py",
        git(oss, "show", "HEAD:brk/mod.py").stdout.rstrip("\n") + "\n\n# see <MCP_ENDPOINT> for tool status\n",
    )
    commit(oss, "outside PR: note the status endpoint in brk/mod.py")
    oss_head = rev(oss)
    run(oss_base, oss_head, "STEP 8 — OSS push triggers --direction reverse directly -> PR onto the production repo", direction="reverse")

    print("\n-- what the explicit reverse run proposed on the prod repo --")
    reverse_brk_branch = f"reverse-sync/brk/{oss_head[:12]}"
    proposed_brk = git(prod, "show", f"{reverse_brk_branch}:src/brk/mod.py").stdout
    print(f"branch: {reverse_brk_branch}")
    print("hydrated (real endpoint, not the placeholder):",
          "cag-mcp.internal" in proposed_brk and "<MCP_ENDPOINT>" not in proposed_brk)

    print(f"\nWorkspace left on disk for inspection: {workspace}")


if __name__ == "__main__":
    main()
