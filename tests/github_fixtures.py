"""Pull-request scenario values shared by `tests/test_cli.py` (`check`/
`release` CLI wiring) and `tests/test_github.py` (the GitHub adapter's own
landing-reading behavior). Both import this module directly; pytest's
rootless collection puts `tests/` on `sys.path`, so a plain `import
github_fixtures` resolves here."""

from __future__ import annotations

WORK_ITEM_ISSUE = 72
LANDING_BRANCH = f"codex/issue-{WORK_ITEM_ISSUE}-claims"
# A well-formed 40-hex sha (issue #397): the merge commit a merged `Landing`
# carries, matching `protocol.COMMIT_PATTERN` -- its own digits never mean
# anything beyond "a plausible sha", so every scenario that just needs one
# shares this instead of inventing a fresh, equally arbitrary string.
MERGE_COMMIT_SHA = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
