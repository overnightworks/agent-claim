"""Root allowlist check (operator ruling 06.09.2026; owner: marketplace
`AGENTS.md`, section "Repository layout").

The repository root holds only what a tool must find there -- the package
manifest and lockfile, scanner configuration, the license -- and the entry
documents README.md/AGENTS.md/CLAUDE.md. Everything else belongs beside its
owner: a baseline beside its check, a fixture beside its test, a helper under
scripts/. This script is the CI gate that holds that rule; `violations` is
its pure, testable core.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ALLOWED_ROOT_FILES = frozenset(
    {
        ".gitignore",
        "AGENTS.md",
        "CLAUDE.md",
        "LICENSE",
        "README.md",
        "pyproject.toml",
        "sonar-project.properties",
        "uv.lock",
    }
)

ALLOWED_ROOT_DIRECTORIES = frozenset({".github", "docs", "scripts", "src", "tests"})

VIOLATION_MESSAGE = (
    "belongs next to its owner (a baseline beside its check, a fixture beside "
    "its test, a helper under scripts/) — not in the repository root"
)


def _git_ls_files(repo_root: Path, *extra_arguments: str) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "ls-files", *extra_arguments],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return tuple(path for path in result.stdout.splitlines() if path)


def _root_entries(repo_root: Path) -> frozenset[str]:
    tracked = _git_ls_files(repo_root)
    untracked = _git_ls_files(repo_root, "--others", "--exclude-standard")
    return frozenset(path.split("/", 1)[0] for path in (*tracked, *untracked))


def violations(repo_root: Path) -> tuple[str, ...]:
    disallowed = _root_entries(repo_root) - ALLOWED_ROOT_FILES - ALLOWED_ROOT_DIRECTORIES
    return tuple(f"{entry}: {VIOLATION_MESSAGE}" for entry in sorted(disallowed))


def main() -> int:
    repo_root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    found = violations(repo_root)
    for violation in found:
        print(violation)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
