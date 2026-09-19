#!/usr/bin/env python
"""Report cross-file near-duplicate function pairs in ``src/`` and ``tests/`` (issue #319).

Adapted from the knowlagentic blueprint (``similar_methods.py``, MR !506 @ 9367be37; see
``SOURCES.txt``). The algorithm is generic; only the CONFIG block below is aco-specific.

Detection
---------
Every ``ast.FunctionDef``/``ast.AsyncFunctionDef`` in ``src/`` and ``tests/`` (module-level,
a method at any class depth, or a nested function) is fingerprinted as a token stream: a
pre-order AST walk keeping node kinds, with every literal folded to its class (``S`` string,
``N`` number, ``B`` bool, ``None``) *and* every identifier (a ``Name``, an ``Attribute``'s
``.attr``, a parameter, a non-``**`` keyword-argument name) folded to ``ID`` -- so a copy that
only renamed its locals still matches. A function under ``MIN_TOKENS`` tokens is skipped (too
small to be a meaningful copy). The stream is cut into ``SHINGLE_SIZE``-token shingles and two
functions are a pair when the Jaccard similarity of their shingle sets is at least
``SIMILARITY_THRESHOLD`` -- a verbatim copy is simply the score-1.0 case of this same check,
not a second detector, because identical source normalises to identical tokens. Pairs are
found with the same rarest-shingle prefix filter as ``test_budget.py::clone_clusters`` (only
each function's rarest shingles are indexed; a pair at or above the threshold must share one),
verified exactly, and reported individually rather than clustered -- this script names pairs to
fix, it does not charge redundant lines. Only cross-*file* pairs are reported; two near-duplicate
functions in the same file are a different (renaming) problem.

Shared candidates with ``scripts/test_budget.py``
--------------------------------------------------
``_literal_class``/``_emit``/``_function_tokens``/``_shingles``/``jaccard`` mirror
``test_budget.py``'s tokeniser (extended here to also fold identifiers), and
``merge_base``/``export_target``/``TargetError``/``default_target`` mirror its merge-base
target-resolution contract. A follow-up chore should unify both pairs into one shared module;
this script does not import from it so it can land independently.

Target-relative ``--ci``
------------------------
There is no committed baseline. The base ladder (issue #319, agreed 2026-09-19) is the same as
``test_budget.py``'s: on a pull request, ``origin/$GITHUB_BASE_REF``; on a push to ``main``
(``GITHUB_BASE_REF`` unset, ``GITHUB_EVENT_NAME=push``), ``HEAD^1``; outside CI, ``origin/main``.
``--target <ref>`` overrides all three. The MERGE-BASE of ``HEAD`` and the resolved ref is the
baseline; ``src/`` and ``tests/`` of that merge-base are exported with ``git archive`` into a
temporary directory (never a checkout or stash, so the working tree is left untouched). It
fails only when the current tree has a pair absent from that baseline -- an old pair earns no
free pass, but it does not block a change that did not add it. Unlike ``test_budget.py``, this
script does not fetch a missing ref automatically (no silent network I/O in a measurement
command): CI fetches the base ref before invoking this script, and a local run needs an
up-to-date checkout of the target branch. A shallow local clone has no merge-base to find; the
error names the fix (``git fetch --unshallow``).

Exemptions
----------
``scripts/similar_methods_exemptions.txt``, next to this script, is the one ledger of
``"relpath:qualname"`` keys (either side of a pair) that are never reported, one per line as
``relpath:qualname — reason`` -- e.g. a ``Protocol`` stub body is a deliberately identical
shape, not re-implementation. Never a per-line marker in the flagged file. Written by hand, like
``test_budget.py``'s ``# budget:`` ledger, only when a pair is deliberately accepted; empty when
there is nothing to exempt.

Usage:
    uv run scripts/similar_methods.py                        # report pairs in the working tree
    uv run scripts/similar_methods.py --json                 # machine-readable
    uv run scripts/similar_methods.py --ci                   # exit non-zero on a new pair
    uv run scripts/similar_methods.py --ci --target origin/main
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import math
import os
import subprocess
import sys
import tarfile
import tempfile
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

# --- aco configuration --------------------------------------------------------------------
ROOT = Path()
TREES = ("src", "tests")
REMOTE = "origin"
BASE_REF_ENV = "GITHUB_BASE_REF"  # set by GitHub Actions on a pull_request event
EVENT_NAME_ENV = "GITHUB_EVENT_NAME"  # "push" on a push to main
DEFAULT_TARGET_BRANCH = "main"  # local fallback when neither env var is set
FIRST_PARENT_TARGET = "HEAD^1"
EXEMPTIONS_REL = Path("scripts/similar_methods_exemptions.txt")
# --- end aco configuration -----------------------------------------------------------------

SHINGLE_SIZE = 5
MIN_TOKENS = 25
SIMILARITY_THRESHOLD = 0.9
FAIL = "FAIL"

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


# --- Token fingerprint ---------------------------------------------------------------------


def _literal_class(value: object) -> str:
    if isinstance(value, bool):
        return "B"
    if value is None:
        return "None"
    if isinstance(value, (int, float, complex)):
        return "N"
    if isinstance(value, (str, bytes)):
        return "S"
    return type(value).__name__


def _emit(node: ast.AST, out: list[str]) -> None:
    if isinstance(node, ast.expr_context):
        return
    out.append(type(node).__name__)
    if isinstance(node, ast.Constant):
        out.append(_literal_class(node.value))
    elif isinstance(node, (ast.Name, ast.Attribute, ast.arg)):
        out.append("ID")
    elif isinstance(node, ast.keyword):
        out.append("ID" if node.arg else "**")
    for child in ast.iter_child_nodes(node):
        _emit(child, out)


def _function_tokens(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    body = func.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    tokens: list[str] = []
    _emit(func.args, tokens)
    for stmt in body:
        _emit(stmt, tokens)
    return tokens


def _shingles(tokens: list[str]) -> frozenset[int]:
    return frozenset(
        hash(tuple(tokens[i : i + SHINGLE_SIZE])) for i in range(len(tokens) - SHINGLE_SIZE + 1)
    )


def jaccard(left: frozenset[int], right: frozenset[int]) -> float:
    union = len(left | right)
    if union == 0:
        return 0.0
    return len(left & right) / union


# --- Function discovery --------------------------------------------------------------------


def _iter_functions(
    body: list[ast.stmt], scope: str
) -> Iterator[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Yield ``(qualname, function)`` for every function in *body*, classes and nesting included."""
    for node in body:
        if isinstance(node, _FUNCTIONS):
            qualname = f"{scope}.{node.name}" if scope else node.name
            yield qualname, node
            yield from _iter_functions(node.body, qualname)
        elif isinstance(node, ast.ClassDef):
            qualname = f"{scope}.{node.name}" if scope else node.name
            yield from _iter_functions(node.body, qualname)


@dataclass(frozen=True)
class _Unit:
    file: str
    qualname: str
    line: int
    shingles: frozenset[int]


def collect_units(root: Path, trees: tuple[str, ...] = TREES) -> list[_Unit]:
    units: list[_Unit] = []
    for tree in trees:
        for path in sorted((root / tree).rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for qualname, func in _iter_functions(module.body, ""):
                tokens = _function_tokens(func)
                if len(tokens) < MIN_TOKENS:
                    continue
                units.append(_Unit(rel, qualname, func.lineno, _shingles(tokens)))
    return units


# --- Pairing ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Pair:
    file_a: str
    line_a: int
    qualname_a: str
    file_b: str
    line_b: int
    qualname_b: str
    score: float

    @property
    def identity(self) -> frozenset[tuple[str, str]]:
        return frozenset({(self.file_a, self.qualname_a), (self.file_b, self.qualname_b)})


def _make_pair(a: _Unit, b: _Unit, score: float) -> Pair:
    left, right = sorted((a, b), key=lambda u: (u.file, u.line, u.qualname))
    return Pair(left.file, left.line, left.qualname, right.file, right.line, right.qualname, score)


def find_pairs(units: list[_Unit]) -> list[Pair]:
    """Cross-file pairs at or above ``SIMILARITY_THRESHOLD`` (see module docstring for the
    tokeniser and the rarest-shingle prefix filter, mirrored from
    ``scripts/test_budget.py::clone_clusters`` to avoid an O(n^2) full comparison)."""
    frequency: Counter[int] = Counter()
    for unit in units:
        frequency.update(unit.shingles)
    index: defaultdict[int, list[int]] = defaultdict(list)
    pairs: list[Pair] = []
    for i, unit in enumerate(units):
        ordered = sorted(unit.shingles, key=lambda s: (frequency[s], s))
        prefix_len = len(ordered) - math.ceil(SIMILARITY_THRESHOLD * len(ordered)) + 1
        candidates: set[int] = set()
        for shingle in ordered[:prefix_len]:
            candidates.update(index[shingle])
            index[shingle].append(i)
        for j in candidates:
            other = units[j]
            if other.file == unit.file:
                continue
            score = jaccard(unit.shingles, other.shingles)
            if score >= SIMILARITY_THRESHOLD:
                pairs.append(_make_pair(other, unit, score))
    return pairs


def load_exemptions(path: Path) -> dict[str, str]:
    """``"relpath:qualname" -> reason`` from *path* (one per line, ``key — reason``); a
    missing ledger has none. Never populated by hand-editing the flagged file."""
    if not path.exists():
        return {}
    exemptions: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, _, reason = stripped.partition(" — ")
        exemptions[key.strip()] = reason.strip()
    return exemptions


def _is_exempt(pair: Pair, exemptions: dict[str, str]) -> bool:
    return (
        f"{pair.file_a}:{pair.qualname_a}" in exemptions
        or f"{pair.file_b}:{pair.qualname_b}" in exemptions
    )


def _package(relpath: str) -> str:
    """The reporting bucket for one file: its top-level tree, or its one ``src`` package (aco
    keeps a single flat package, unlike knowlagentic's ``src/<pkg>/...`` split)."""
    parts = relpath.split("/")
    if parts[0] == "src" and len(parts) > 1:
        return parts[1]
    return parts[0]


def summarize(pairs: list[Pair]) -> dict[str, int]:
    """Pair count per package touched -- a cross-package pair counts toward both."""
    counts: Counter[str] = Counter()
    for pair in pairs:
        counts.update({_package(pair.file_a), _package(pair.file_b)})
    return dict(sorted(counts.items()))


def measure(root: Path, exemptions: dict[str, str]) -> list[Pair]:
    pairs = [pair for pair in find_pairs(collect_units(root)) if not _is_exempt(pair, exemptions)]
    pairs.sort(key=lambda p: (-p.score, p.file_a, p.line_a, p.file_b, p.line_b))
    return pairs


def new_pairs(current: list[Pair], baseline: list[Pair]) -> list[Pair]:
    """The pairs in *current* absent from *baseline* -- what a change actually added."""
    baseline_ids = {pair.identity for pair in baseline}
    return [pair for pair in current if pair.identity not in baseline_ids]


# --- Target ------------------------------------------------------------------------------------


class TargetError(Exception):
    """The target ref could not be resolved or exported."""


def default_target() -> str:
    """The base ladder (issue #319): a pull request compares against the merge-base with its
    ``GITHUB_BASE_REF``; a push to ``main`` (no base ref, ``GITHUB_EVENT_NAME=push``) compares
    against ``HEAD^1``; outside CI, ``origin/main`` is a sensible local default."""
    base_ref = os.environ.get(BASE_REF_ENV)
    if base_ref:
        return f"{REMOTE}/{base_ref}"
    if os.environ.get(EVENT_NAME_ENV) == "push":
        return FIRST_PARENT_TARGET
    return f"{REMOTE}/{DEFAULT_TARGET_BRANCH}"


def _git(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], capture_output=True, check=False)


def _commit_of(ref: str) -> str | None:
    result = _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return result.stdout.decode().strip() if result.returncode == 0 else None


def resolve_target(ref: str) -> str:
    """The commit *ref* names. Does not fetch: a measurement command performs no network I/O,
    so a missing ref is the caller's job (CI's fetch step, or ``git fetch`` locally)."""
    commit = _commit_of(ref)
    if commit is None:
        branch = ref[len(REMOTE) + 1 :] if ref.startswith(f"{REMOTE}/") else ref
        msg = (
            f"cannot resolve target {ref!r}; run `git fetch {REMOTE} {branch}` or "
            f"pass --target <ref>"
        )
        raise TargetError(msg)
    return commit


def merge_base(ref: str, tip: str) -> str:
    """The commit HEAD and the target *tip* diverged from -- the run's own baseline."""
    result = _git("merge-base", "HEAD", tip)
    if result.returncode != 0:
        msg = (
            f"no merge-base between HEAD and {ref} ({tip[:12]}); on a shallow clone deepen it "
            f"first (git fetch --unshallow) so the branch point is reachable"
        )
        raise TargetError(msg)
    base = result.stdout.decode().strip()
    print(f"baseline: merge-base {base[:12]} of HEAD and {ref} ({tip[:12]})", file=sys.stderr)
    return base


def export_target(ref: str, into: Path) -> Path:
    """Extract ``src/`` and ``tests/`` of the merge-base with *ref* under *into*, as a scan root."""
    commit = merge_base(ref, resolve_target(ref))
    archive = _git("archive", "--format=tar", commit, "--", *TREES)
    if archive.returncode != 0:
        msg = f"cannot export {ref} ({commit[:12]}): {archive.stderr.decode().strip()}"
        raise TargetError(msg)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(into, filter="data")
    return into


# --- Output --------------------------------------------------------------------------------


def to_json(
    pairs: list[Pair], summary: dict[str, int], ref: str | None, new: list[Pair] | None
) -> dict[str, object]:
    payload: dict[str, object] = {"pairs": [asdict(pair) for pair in pairs], "summary": summary}
    if ref is not None:
        payload["target"] = ref
        payload["new_pairs"] = [asdict(pair) for pair in new or []]
    return payload


def _print_report(pairs: list[Pair], summary: dict[str, int]) -> None:
    for pair in pairs:
        print(f"{pair.file_a}:{pair.line_a} <-> {pair.file_b}:{pair.line_b} {pair.score:.2f}")
    print()
    print("summary:")
    for package, count in summary.items():
        print(f"  {package}: {count}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report cross-file near-duplicate function pairs in src/ and tests/."
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--ci", action="store_true", help="exit non-zero on a pair absent from the target"
    )
    parser.add_argument(
        "--target",
        metavar="REF",
        default=None,
        help=f"git ref to compare against (default: {REMOTE}/${BASE_REF_ENV} on a pull "
        f"request, HEAD^1 on a push to {DEFAULT_TARGET_BRANCH}, else {REMOTE}/"
        f"{DEFAULT_TARGET_BRANCH})",
    )
    args = parser.parse_args(argv)
    exemptions = load_exemptions(EXEMPTIONS_REL)

    pairs = measure(ROOT, exemptions)
    summary = summarize(pairs)

    if not args.ci:
        if args.json:
            print(json.dumps(to_json(pairs, summary, None, None), indent=2, sort_keys=True))
        else:
            _print_report(pairs, summary)
        return 0

    ref = args.target or default_target()
    with tempfile.TemporaryDirectory() as scratch:
        try:
            target_root = export_target(ref, Path(scratch))
        except TargetError as exc:
            print(f"{FAIL}: {exc}", file=sys.stderr)
            return 1
        baseline = measure(target_root, exemptions)
    added = new_pairs(pairs, baseline)

    if args.json:
        print(json.dumps(to_json(pairs, summary, ref, added), indent=2, sort_keys=True))
    else:
        _print_report(pairs, summary)
        for pair in added:
            print(
                f"{FAIL} new pair absent from {ref}: {pair.file_a}:{pair.line_a} <-> "
                f"{pair.file_b}:{pair.line_b} {pair.score:.2f}",
                file=sys.stderr,
            )

    return 1 if added else 0


if __name__ == "__main__":
    raise SystemExit(main())
