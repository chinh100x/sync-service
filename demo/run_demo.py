"""End-to-end demo of the bidirectional sync service against two local git repos
standing in for "the production repo" and "the open source repo" — no GitHub needed.

Walks through:
  STEP 1 — first sync, two mappings at once (forward)
  STEP 2 — secret scan halt (forward)
  STEP 3 — an outside OSS edit gets silently overwritten by the next forward sync.
           There's no manifest / conflict tracking: a run always overwrites the far
           side's tracked files with the near side's current content. That's the
           deliberate tradeoff of not tracking sync state at all -- simpler, at the
           cost of ever detecting an outside edit before clobbering it.
  STEP 4 — break check failure (forward), working tree reverted
  STEP 4b — bug fixed, forward sync succeeds on retry
  STEP 5 — no-op (touched file isn't under any mapping)
  STEP 6 — idempotent re-run of step 1's range -> skipped
  STEP 7 — an OSS commit, run explicitly with --direction reverse -> a real PR onto
           the production repo, with `hydrate` restoring the real endpoint

Run with:  uv run python demo/run_demo.py
"""
from __future__ import annotations

import contextlib
import io
import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sync_service import cli  # noqa: E402

GIT_ID = ["-c", "user.name=demo", "-c", "user.email=demo@example.com"]

_BRANCH_RE = re.compile(r"Would open PR (\S+) ->")


class _Tee:
    """Writes to the real stdout (so the demo still narrates live) while also
    capturing into a buffer this script parses -- branch names aren't predictable
    from outside (a title-derived slug, occasionally disambiguated with a sha suffix
    on a rare same-title collision), so this reads them back from what cli.main()
    actually printed instead of guessing at the naming scheme."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            s.write(data)

    def flush(self):
        for s in self.streams:
            s.flush()


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

    def run(base: str, head: str, label: str, direction: str = "forward") -> list[str]:
        """Returns the branch name(s) cli.main() actually opened a PR (or dry-run)
        for, in print order -- read back from its own output rather than guessed,
        since the final name isn't derivable from base/head/direction alone anymore."""
        header(label)
        buf = io.StringIO()
        with contextlib.redirect_stdout(_Tee(sys.stdout, buf)):
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
        return _BRANCH_RE.findall(buf.getvalue())

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

    branches = run(sha0, sha1, "STEP 1 — first sync: two mappings, both pass, PR opened (dry-run) for each")

    # simulate a human approving + merging both sync PRs into oss main
    git(oss, "checkout", "main")
    for branch in branches:
        git(oss, "merge", "--no-ff", branch, "-m", f"merge sync PR: {branch}")
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

    # ---- outside contributor edits plugin/covenant.py on oss main ----
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
    print("(demo) an outside contributor merged a change to plugin/covenant.py on oss main")

    # ---- sha3: prod (separately) fixes the secret and adds a new function ----
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
    branches = run(sha2, sha3, "STEP 3 — no state tracking anymore: this silently overwrites the outsider's edit")

    print("\n-- what actually landed on the portmon sync branch --")
    portmon_branch = branches[0]
    merged_text = git(oss, "show", f"{portmon_branch}:plugin/covenant.py").stdout
    print("has the outsider's comment (it doesn't — overwritten):", "polled every 5 minutes" in merged_text)
    print("has prod's audit():", "def audit():" in merged_text)
    print("(demo) this is the deliberate tradeoff of removing .sync-state: no divergence "
          "check means no protection against clobbering an outside edit either)")

    # ---- sha4: brk gets a real bug -> breakcheck-halt (independent mapping, unaffected by portmon's conflict) ----
    sha4_base = rev(prod)
    write(prod, "src/brk/mod.py", "def run():\n    raise RuntimeError('boom')\n\nif __name__ == '__main__':\n    run()\n")
    sha4 = commit(prod, "brk: introduce a bug")
    run(sha4_base, sha4, "STEP 4 — break check fails -> halted, no PR, working tree reverted")
    print("brk/mod.py on oss main still has the old, working version:",
          "boom" not in (oss / "brk" / "mod.py").read_text())

    # ---- sha4b: human fixes the bug and retries ----
    # Deliberately not identical to the pre-bug content, so this actually has something
    # to commit — see test_publish.py for the "genuinely nothing changed" case instead.
    fixed_brk = "def run():\n    print('brk ok, fixed')\n\nif __name__ == '__main__':\n    run()\n"
    write(prod, "src/brk/mod.py", fixed_brk)
    sha4b = commit(prod, "brk: fix the bug")
    run(sha4, sha4b, "STEP 4b — bug fixed, forward sync succeeds on retry")

    # ---- sha5: touches only README, no mapping matches -> no-op ----
    sha5_base = rev(prod)
    write(prod, "README.md", "# production repo (demo)\nupdated\n")
    sha5 = commit(prod, "docs: update README")
    run(sha5_base, sha5, "STEP 5 — no changed file matches any mapping -> no-op")

    # ---- re-run sha0..sha1: PR already exists for (mapping, head_sha) -> skip ----
    run(sha0, sha1, "STEP 6 — idempotent re-run of step 1's range -> skipped, PR already exists")

    # ---- STEP 7: an OSS-side commit, driven explicitly with --direction reverse ----
    oss_base = rev(oss)
    write(
        oss,
        "brk/mod.py",
        git(oss, "show", "HEAD:brk/mod.py").stdout.rstrip("\n") + "\n\n# see <MCP_ENDPOINT> for tool status\n",
    )
    commit(oss, "outside PR: note the status endpoint in brk/mod.py")
    oss_head = rev(oss)
    reverse_branches = run(
        oss_base, oss_head,
        "STEP 7 — OSS push triggers --direction reverse directly -> PR onto the production repo",
        direction="reverse",
    )

    print("\n-- what the explicit reverse run proposed on the prod repo --")
    reverse_brk_branch = reverse_branches[0]
    proposed_brk = git(prod, "show", f"{reverse_brk_branch}:src/brk/mod.py").stdout
    print(f"branch: {reverse_brk_branch}")
    print("hydrated (real endpoint, not the placeholder):",
          "cag-mcp.internal" in proposed_brk and "<MCP_ENDPOINT>" not in proposed_brk)

    print(f"\nWorkspace left on disk for inspection: {workspace}")


if __name__ == "__main__":
    main()
