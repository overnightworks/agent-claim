"""Pull-request scenario values shared by `tests/test_cli.py` (`check`/
`release` CLI wiring) and `tests/test_github.py` (the GitHub adapter's own
landing-reading behavior). Both import this module directly; pytest's
rootless collection puts `tests/` on `sys.path`, so a plain `import
github_fixtures` resolves here."""

from __future__ import annotations

WORK_ITEM_ISSUE = 72
LANDING_BRANCH = f"codex/issue-{WORK_ITEM_ISSUE}-claims"
