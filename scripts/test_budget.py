#!/usr/bin/env python3
"""Test-efficiency budget: measure ``tests/`` against the tree the change lands on (issue #319).

Adapted from the knowlagentic blueprint (``test_budget.py``, MR !507 @ d5087b0df; see
``SOURCES.txt``). The algorithm is generic; only the CONFIG block below is aco-specific.

Metrics (measured over every ``*.py`` file in ``tests/``; only ``test_*.py`` modules --
the files pytest collects -- are scanned for the test-shaped ones, because ``conftest.py``
and ``_``-prefixed modules such as ``cli_fixtures.py``/``board_fixtures.py`` are the *shared*
homes the per-module copies should move into)
----------------------------------------------------------------------------------------------
(a) ``lines`` -- total lines of every ``*.py`` file in the tree.
(b) ``tests`` -- number of ``test_*`` functions (module-level or inside classes at any
    depth). Reported, not gated.
(c) ``parametrized_share_pct`` -- share of (b) that is parametrized or generated from a
    parametrized family: the function carries a ``parametrize`` decorator, an enclosing class
    does, or the module's ``pytestmark`` contains one. Stored and displayed rounded to one
    decimal, but GATED on the exact ratio ``parametrized/tests`` so a rounding-hidden plain
    test never slips past the gate.
(d) ``clone_lines`` -- redundant lines in near-duplicate test functions. Method: each
    ``test_*`` function is fingerprinted from its AST -- a pre-order stream of node kinds
    plus identifiers (names, attributes, keyword and parameter names) with every literal
    folded to its class (``S`` string/bytes, ``N`` number, ``B`` bool, ``None``); the
    function name, decorators and docstring are excluded. The stream is cut into 5-token
    shingles, functions below 24 tokens are skipped (too small to be a meaningful copy),
    and two functions are clones when the Jaccard similarity of their shingle sets is at
    least 0.80. Clone pairs are found with a prefix filter (only the rarest
    ``n - ceil(0.8 n) + 1`` shingles of each function are indexed; any pair at or above the
    threshold must share one of them) and verified exactly, then joined into
    single-linkage clusters. A cluster charges every member except its largest as
    redundant lines, attributed to the member's file.
(e) ``init_helpers`` -- module-level functions in a ``test_*.py`` module (not tests; fixture
    or plain) whose body calls one of ``SETUP_CALL_NAMES`` with one of ``SETUP_COMMANDS`` as
    its first argument: the per-module setup-wrapper copies a shared fixture should replace.
    aco has not named such a call-and-command pair yet (no scaffolding CLI a test wraps
    per-module the way knowlagentic tests wrap ``invoke_ka("init"/"add", ...)``), so both sets
    are empty below and this metric reads 0 until one is named.
(f) ``literal_siblings`` -- test names in one scope (a class, or the module's top level)
    that differ only by a token in ``SIBLING_TOKEN_GROUPS``: the names are normalised by
    replacing those ``_``-delimited tokens with a placeholder per group, and each group of
    ``k`` names that collapse to one key counts ``k - 1`` siblings -- the literal-only copies
    one parametrized test replaces. No aco-specific token group is named yet, so this also
    reads 0 until one is.
(g) ``spec_ids`` -- reported, never gated. aco has no ``specs/`` tree or ``spec_coverage.py``
    owner yet, so this stays dormant at 0 (frozenset()), the same dormancy
    ``scripts/test_inventory.py`` documents for its own spec-ID section.

Target-relative rule (issue #319, base-selection concept agreed 2026-09-19)
----------------------------------------------------------------------------------------------
There is no committed baseline. ``--ci`` measures the change's own delta on the fly: on a pull
request it resolves ``origin/$GITHUB_BASE_REF`` (a merge-base rebuild target); on a push to
``main`` (``GITHUB_BASE_REF`` unset, ``GITHUB_EVENT_NAME=push``) it uses ``HEAD^1``, the
previous trunk tip, so a landing is judged by what it brought, not by main's whole history;
outside CI (both unset) it falls back to ``origin/main`` for a sensible local diff. ``--target
<ref>`` overrides all three. Whichever ref is resolved, the MERGE-BASE of ``HEAD`` and that
ref is the baseline (``git merge-base``): a branch that lags its target never reads the
target's growth as its own regression, or its consolidation as the branch's loss. The
merge-base's ``tests/`` and ``scripts/test_budget_ratchet.txt`` are exported with ``git
archive`` into a temporary directory and compared against the working tree; the resolved
commits print once on stderr. A merge-base needs the branch point in the clone, so CI checks
out full history and a shallow local clone is deepened first (``git fetch --unshallow``).

Against that baseline:

* (c) falling, or (d), (e) or (f) rising, BLOCKS (``FAIL``, exit 1) -- unless
  ``scripts/test_budget_ratchet.txt`` gained a ``# budget: <why> [SPEC-IDs]`` line in this
  change (present in the head's ledger, absent from the target's copy). A raise is paid for in
  the same change as the test growth it buys and named so a reviewer can weigh it -- never to
  buy green CI. The gate checks that a new line exists; what it says is the reviewer's call.
* (a) rising only WARNS (exit 0, printed prominently): the delta and the five files that moved
  it most -- a new criterion may legitimately add tests.
* An improvement needs no bookkeeping: the next change branching after it is measured against
  a merge-base that contains it.

Usage:
    uv run scripts/test_budget.py                       # vs the merge-base with the default target
    uv run scripts/test_budget.py --target origin/main  # vs the merge-base with another ref
    uv run scripts/test_budget.py --json                # machine-readable
    uv run scripts/test_budget.py --ci                  # exit non-zero on an unjustified regression
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
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from pathlib import Path

# --- aco configuration --------------------------------------------------------------------
# The one place ka-specific names were replaced with aco's own. Everything below this block
# is the generic algorithm.
ROOT = Path()
TREES = ("tests",)  # aco keeps one flat test tree; ka split tests/e2e and tests/unit
RATCHET_REL = Path("scripts/test_budget_ratchet.txt")
RATCHET_ENTRY_PREFIX = "# budget:"
REMOTE = "origin"
BASE_REF_ENV = "GITHUB_BASE_REF"  # set by GitHub Actions on a pull_request event
EVENT_NAME_ENV = "GITHUB_EVENT_NAME"  # "push" on a push to main
DEFAULT_TARGET_BRANCH = "main"  # local fallback when neither env var is set
FIRST_PARENT_TARGET = "HEAD^1"

# (e)/(f): no aco-specific setup-wrapper call or literal-sibling token group is named yet;
# see the module docstring. Populate here, never by re-adding ka's names, once one is.
SETUP_CALL_NAMES: frozenset[str] = frozenset()
SETUP_COMMANDS: frozenset[str] = frozenset()
SIBLING_TOKEN_GROUPS: tuple[frozenset[str], ...] = ()
# --- end aco configuration -----------------------------------------------------------------

FILE_KEYS = ("lines", "tests", "parametrized", "clone_lines", "init_helpers", "literal_siblings")
SHARE_METRIC = "parametrized_share_pct"
TREE_KEYS = (*FILE_KEYS, SHARE_METRIC, "spec_ids")
FAIL = "FAIL"
WARN = "WARN"
# Metrics compared against the target: the direction that is the wrong way (+1 rising,
# -1 falling) and whether such a move blocks (FAIL) or is only reported (WARN).
WATCHED = (
    ("lines", 1, WARN),
    (SHARE_METRIC, -1, FAIL),
    ("clone_lines", 1, FAIL),
    ("init_helpers", 1, FAIL),
    ("literal_siblings", 1, FAIL),
)
_WRONG_WAY = {metric: direction for metric, direction, _ in WATCHED}
# The per-file column that explains a move of each watched metric, and whether it moves with
# the metric (+1) or against it (-1): a falling share is explained by files gaining plain tests.
_EXPLAINS = {
    "lines": ("lines", 1),
    SHARE_METRIC: ("unparametrized", -1),
    "clone_lines": ("clone_lines", 1),
    "init_helpers": ("init_helpers", 1),
    "literal_siblings": ("literal_siblings", 1),
}
TOP_FILES = 5

SHINGLE_SIZE = 5
MIN_TOKENS = 24
CLONE_THRESHOLD = 0.80

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass
class FileMetrics:
    lines: int = 0
    tests: int = 0
    parametrized: int = 0
    clone_lines: int = 0
    init_helpers: int = 0
    literal_siblings: int = 0

    @property
    def unparametrized(self) -> int:
        return self.tests - self.parametrized


@dataclass
class TreeReport:
    files: dict[str, FileMetrics] = field(default_factory=dict)
    credited: frozenset[str] = frozenset()

    def _total(self, key: str) -> int:
        return sum(getattr(metrics, key) for metrics in self.files.values())

    @property
    def lines(self) -> int:
        return self._total("lines")

    @property
    def tests(self) -> int:
        return self._total("tests")

    @property
    def parametrized(self) -> int:
        return self._total("parametrized")

    @property
    def clone_lines(self) -> int:
        return self._total("clone_lines")

    @property
    def init_helpers(self) -> int:
        return self._total("init_helpers")

    @property
    def literal_siblings(self) -> int:
        return self._total("literal_siblings")

    @property
    def parametrized_share_pct(self) -> float:
        return share_pct(self.parametrized, self.tests)

    @property
    def spec_ids(self) -> int:
        return len(self.credited)


@dataclass
class Finding:
    tree: str
    metric: str
    level: str  # FAIL (blocks unless the ledger justifies it) | WARN (reported only)
    current: float
    target: float
    delta: float
    top_files: list[tuple[str, int]]
    detail: str = ""  # what the one-line head hides: the exact share ratio, lines delta detail


class TargetError(Exception):
    """The target ref could not be resolved or exported."""


@dataclass(frozen=True)
class _Unit:
    file: str
    lines: int
    shingles: frozenset[int]


def share(part: int, whole: int) -> Fraction:
    return Fraction(part, whole) if whole else Fraction(0)


def share_pct(part: int, whole: int) -> float:
    return round(float(share(part, whole) * 100), 1)


# --- AST helpers -------------------------------------------------------------------------------


def _callee_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _first_arg(node: ast.Call) -> object:
    if node.args and isinstance(node.args[0], ast.Constant):
        return node.args[0].value
    return None


def _is_parametrize(node: ast.expr) -> bool:
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Attribute):
        return target.attr == "parametrize"
    return isinstance(target, ast.Name) and target.id == "parametrize"


def _decorated_parametrize(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> bool:
    return any(_is_parametrize(decorator) for decorator in node.decorator_list)


def _pytestmark_parametrizes(module: ast.Module) -> bool:
    for stmt in module.body:
        if not isinstance(stmt, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in stmt.targets):
            continue
        marks = stmt.value.elts if isinstance(stmt.value, (ast.List, ast.Tuple)) else [stmt.value]
        if any(_is_parametrize(mark) for mark in marks):
            return True
    return False


def _iter_tests(
    body: list[ast.stmt], scope: str, inherited: bool
) -> Iterator[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef, bool]]:
    """Yield ``(scope, function, parametrized)`` for every test in *body*, classes included."""
    for node in body:
        if isinstance(node, _FUNCTIONS) and node.name.startswith("test_"):
            yield scope, node, inherited or _decorated_parametrize(node)
        elif isinstance(node, ast.ClassDef):
            yield from _iter_tests(
                node.body, f"{scope}.{node.name}", inherited or _decorated_parametrize(node)
            )


def wraps_setup_command(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether *func* calls a name in ``SETUP_CALL_NAMES`` with a first argument in
    ``SETUP_COMMANDS``. Both are empty for aco today (see the module docstring), so this is
    always ``False`` until a per-module setup-wrapper pattern is named."""
    if not SETUP_CALL_NAMES:
        return False
    return any(
        isinstance(node, ast.Call)
        and _callee_name(node) in SETUP_CALL_NAMES
        and _first_arg(node) in SETUP_COMMANDS
        for node in ast.walk(func)
    )


def sibling_key(name: str) -> str:
    """The test name with every token from each ``SIBLING_TOKEN_GROUPS`` group replaced by a
    placeholder for that group. No group is named for aco yet, so this is the identity."""
    if not SIBLING_TOKEN_GROUPS:
        return name
    parts = []
    for part in name.split("_"):
        for index, group in enumerate(SIBLING_TOKEN_GROUPS):
            if part in group:
                parts.append(f"{{group{index}}}")
                break
        else:
            parts.append(part)
    return "_".join(parts)


# --- Clone fingerprint -------------------------------------------------------------------------


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
    elif isinstance(node, ast.Name):
        out.append(node.id)
    elif isinstance(node, ast.Attribute):
        out.append(node.attr)
    elif isinstance(node, ast.keyword):
        out.append(node.arg or "**")
    elif isinstance(node, ast.arg):
        out.append(node.arg)
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


def fingerprint_tokens(source: str) -> list[str]:
    """The normalised token stream of the first function in *source* (see module docstring)."""
    func = ast.parse(source).body[0]
    assert isinstance(func, _FUNCTIONS)
    return _function_tokens(func)


def _shingles(tokens: list[str]) -> frozenset[int]:
    return frozenset(
        hash(tuple(tokens[i : i + SHINGLE_SIZE])) for i in range(len(tokens) - SHINGLE_SIZE + 1)
    )


def jaccard(left: set[int] | frozenset[int], right: set[int] | frozenset[int]) -> float:
    union = len(left | right)
    if union == 0:
        return 0.0
    return len(left & right) / union


def _find(parent: list[int], i: int) -> int:
    while parent[i] != i:
        parent[i] = parent[parent[i]]
        i = parent[i]
    return i


def clone_clusters(units: list[_Unit]) -> list[list[int]]:
    """Single-linkage clusters (as unit indices) of pairs at or above ``CLONE_THRESHOLD``."""
    frequency: Counter[int] = Counter()
    for unit in units:
        frequency.update(unit.shingles)
    parent = list(range(len(units)))
    index: defaultdict[int, list[int]] = defaultdict(list)
    for i, unit in enumerate(units):
        ordered = sorted(unit.shingles, key=lambda s: (frequency[s], s))
        prefix_len = len(ordered) - math.ceil(CLONE_THRESHOLD * len(ordered)) + 1
        candidates: set[int] = set()
        for shingle in ordered[:prefix_len]:
            candidates.update(index[shingle])
            index[shingle].append(i)
        for j in candidates:
            if jaccard(unit.shingles, units[j].shingles) >= CLONE_THRESHOLD:
                parent[_find(parent, i)] = _find(parent, j)
    members: defaultdict[int, list[int]] = defaultdict(list)
    for i in range(len(units)):
        members[_find(parent, i)].append(i)
    return [cluster for cluster in members.values() if len(cluster) > 1]


def _charge_clone_lines(units: list[_Unit], files: dict[str, FileMetrics]) -> None:
    for cluster in clone_clusters(units):
        # The largest member is the representative one would keep; every other member's
        # lines are redundant. Ties keep the earliest (file order) as representative.
        cluster.sort(key=lambda i: (-units[i].lines, i))
        for i in cluster[1:]:
            files[units[i].file].clone_lines += units[i].lines


# --- Measurement -------------------------------------------------------------------------------


def _measure_module(module: ast.Module, rel: str, metrics: FileMetrics, units: list[_Unit]) -> None:
    scopes: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for scope, func, parametrized in _iter_tests(module.body, "", _pytestmark_parametrizes(module)):
        metrics.tests += 1
        metrics.parametrized += int(parametrized)
        scopes[scope][sibling_key(func.name)] += 1
        tokens = _function_tokens(func)
        if len(tokens) >= MIN_TOKENS:
            lines = (func.end_lineno or func.lineno) - func.lineno + 1
            units.append(_Unit(rel, lines, _shingles(tokens)))
    metrics.literal_siblings = sum(
        count - 1 for names in scopes.values() for count in names.values() if count > 1
    )
    metrics.init_helpers = sum(
        1
        for node in module.body
        if isinstance(node, _FUNCTIONS)
        and not node.name.startswith("test_")
        and wraps_setup_command(node)
    )


def _credited_spec_ids(root: Path, tree: str) -> frozenset[str]:
    """The spec IDs *tree* credits. Dormant: aco has no ``specs/`` tree or
    ``spec_coverage.py`` owner yet (see the module docstring), so this is always empty."""
    return frozenset()


def measure_tree(root: Path, tree: str) -> TreeReport:
    report = TreeReport(credited=_credited_spec_ids(root, tree))
    units: list[_Unit] = []
    for path in sorted((root / tree).rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        source = path.read_text(encoding="utf-8")
        metrics = report.files.setdefault(rel, FileMetrics())
        metrics.lines = len(source.splitlines())
        if path.name.startswith("test_"):
            _measure_module(ast.parse(source, filename=str(path)), rel, metrics, units)
    _charge_clone_lines(units, report.files)
    return report


def measure(root: Path = ROOT, trees: tuple[str, ...] = TREES) -> dict[str, TreeReport]:
    return {tree: measure_tree(root, tree) for tree in trees}


def to_json(reports: dict[str, TreeReport]) -> dict[str, object]:
    return {
        "trees": {
            tree: {
                **{key: getattr(report, key) for key in TREE_KEYS},
                "files": {
                    rel: {key: getattr(metrics, key) for key in FILE_KEYS}
                    for rel, metrics in sorted(report.files.items())
                },
            }
            for tree, report in reports.items()
        }
    }


# --- Target ------------------------------------------------------------------------------------


def default_target() -> str:
    """The base ladder (issue #319): a pull request compares against the merge-base with its
    ``GITHUB_BASE_REF``; a push to ``main`` (no base ref, ``GITHUB_EVENT_NAME=push``) compares
    against ``HEAD^1``, its own previous tip; outside CI, ``origin/main`` is a sensible local
    default."""
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
    """The commit *ref* names, fetching a missing ``origin/<branch>`` once before giving up."""
    commit = _commit_of(ref)
    if commit is None and ref.startswith(f"{REMOTE}/"):
        branch = ref[len(REMOTE) + 1 :]
        print(f"fetching {ref} ...", file=sys.stderr)
        _git("fetch", REMOTE, f"+refs/heads/{branch}:refs/remotes/{REMOTE}/{branch}")
        commit = _commit_of(ref)
    if commit is None:
        msg = f"cannot resolve target {ref!r}; fetch it or pass --target <ref>"
        raise TargetError(msg)
    return commit


def merge_base(ref: str, tip: str) -> str:
    """The commit HEAD and the target *tip* diverged from -- the change's own baseline."""
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
    """Extract the test trees and ledger of the merge-base with *ref* under *into*, as a root."""
    commit = merge_base(ref, resolve_target(ref))
    archive = _git("archive", "--format=tar", commit, "--", *TREES)
    if archive.returncode != 0:
        msg = f"cannot export {ref} ({commit[:12]}): {archive.stderr.decode().strip()}"
        raise TargetError(msg)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(into, filter="data")
    ledger = _git("show", f"{commit}:{RATCHET_REL.as_posix()}")
    if ledger.returncode == 0:
        (into / RATCHET_REL).parent.mkdir(parents=True, exist_ok=True)
        (into / RATCHET_REL).write_bytes(ledger.stdout)
    return into


# --- Ledger ------------------------------------------------------------------------------------


def load_ratchet(path: Path) -> list[str]:
    """The ``# budget:`` entries of a ledger; a missing ledger has none."""
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith(RATCHET_ENTRY_PREFIX)
    ]


def new_ratchet_entries(head: list[str], target: list[str]) -> list[str]:
    """The entries the head's ledger has and the target's has not -- what justifies a raise."""
    return list((Counter(head) - Counter(target)).elements())


# --- Comparison --------------------------------------------------------------------------------


def _per_file(report: TreeReport, previous: TreeReport, key: str) -> Counter[str]:
    current = {rel: getattr(metrics, key) for rel, metrics in report.files.items()}
    before = {rel: getattr(metrics, key) for rel, metrics in previous.files.items()}
    return Counter(
        {rel: current.get(rel, 0) - before.get(rel, 0) for rel in current.keys() | before.keys()}
    )


def _top_files(deltas: Counter[str], sign: int) -> list[tuple[str, int]]:
    ranked = sorted(
        ((rel, delta) for rel, delta in deltas.items() if delta * sign > 0),
        key=lambda item: (-item[1] * sign, item[0]),
    )
    return ranked[:TOP_FILES]


def _change(report: TreeReport, previous: TreeReport, metric: str) -> Fraction | int:
    """The exact move of *metric* from the target; the share is compared unrounded."""
    if metric == SHARE_METRIC:
        return share(report.parametrized, report.tests) - share(
            previous.parametrized, previous.tests
        )
    return int(getattr(report, metric)) - int(getattr(previous, metric))


def _delta(change: Fraction | int) -> float:
    return round(float(change * 100), 1) if isinstance(change, Fraction) else change


def _detail(report: TreeReport, previous: TreeReport, metric: str, change: Fraction | int) -> str:
    if metric == SHARE_METRIC:
        return f"{report.parametrized}/{report.tests} vs {previous.parametrized}/{previous.tests}"
    return ""


def compare(reports: dict[str, TreeReport], target: dict[str, TreeReport]) -> list[Finding]:
    """Every watched metric that moved the wrong way from the target; improvements are silent."""
    findings: list[Finding] = []
    for tree, report in reports.items():
        previous = target.get(tree, TreeReport())
        for metric, wrong_way, level in WATCHED:
            change = _change(report, previous, metric)
            if change * wrong_way <= 0:
                continue
            column, along = _EXPLAINS[metric]
            files = _per_file(report, previous, column)
            findings.append(
                Finding(
                    tree,
                    metric,
                    level,
                    getattr(report, metric),
                    getattr(previous, metric),
                    _delta(change),
                    _top_files(files, wrong_way * along),
                    _detail(report, previous, metric, change),
                )
            )
    return findings


def blocks(findings: list[Finding], new_entries: list[str]) -> bool:
    """Whether ``--ci`` fails: a FAIL-level move with no ledger line added in this change."""
    return any(f.level == FAIL for f in findings) and not new_entries


# --- Output ------------------------------------------------------------------------------------


def _number(value: float) -> str:
    return f"{value:.1f}" if isinstance(value, float) else str(value)


def _signed(value: float) -> str:
    return f"{value:+.1f}" if isinstance(value, float) else f"{value:+d}"


def _print_table(reports: dict[str, TreeReport], target: dict[str, TreeReport]) -> None:
    print(f"{'tree':<12}{'metric':<24}{'current':>10}{'target':>10}{'delta':>8}")
    for tree, report in reports.items():
        previous = target.get(tree, TreeReport())
        for metric in TREE_KEYS:
            current: float = getattr(report, metric)
            base: float = getattr(previous, metric)
            delta = _signed(_delta(_change(report, previous, metric)))
            print(f"{tree:<12}{metric:<24}{_number(current):>10}{_number(base):>10}{delta:>8}")


def _print_findings(findings: list[Finding], new_entries: list[str]) -> None:
    for finding in findings:
        rose = _WRONG_WAY[finding.metric] > 0
        head = (
            f"{finding.tree} {finding.metric}: {_number(finding.current)} {'>' if rose else '<'} "
            f"target {_number(finding.target)} ({_signed(finding.delta)})"
        )
        if finding.detail:
            head += f" [{finding.detail}]"
        if finding.level == WARN:
            print(f"{WARN} {head}", file=sys.stderr)
        elif new_entries:
            print(f"ALLOWED {head} -- justified by a new line in {RATCHET_REL}", file=sys.stderr)
        else:
            print(
                f"{FAIL} {head} -- a raise needs a `{RATCHET_ENTRY_PREFIX} <why> [SPEC-IDs]` "
                f"line added to {RATCHET_REL} in this change",
                file=sys.stderr,
            )
        if finding.top_files:
            print("  top files by delta:", file=sys.stderr)
        for rel, delta in finding.top_files:
            print(f"    {delta:+d}  {rel}", file=sys.stderr)
    if new_entries and any(f.level == FAIL for f in findings):
        print(f"  new {RATCHET_REL} line(s) in this change:", file=sys.stderr)
        for entry in new_entries:
            print(f"    {entry}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure the test-efficiency budget against the merge-base with the target."
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--ci", action="store_true", help="exit non-zero on an unjustified regression"
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
    ref: str = args.target or default_target()

    reports = measure(ROOT)
    with tempfile.TemporaryDirectory() as scratch:
        try:
            target_root = export_target(ref, Path(scratch))
        except TargetError as exc:
            print(f"{FAIL}: {exc}", file=sys.stderr)
            return 1
        target = measure(target_root)
        new_entries = new_ratchet_entries(
            load_ratchet(ROOT / RATCHET_REL), load_ratchet(target_root / RATCHET_REL)
        )
    findings = compare(reports, target)

    if args.json:
        payload = {
            **to_json(reports),
            "target": {"ref": ref, **to_json(target)},
            "findings": [asdict(finding) for finding in findings],
            "new_ratchet_entries": new_entries,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_table(reports, target)
    _print_findings(findings, new_entries)

    if not args.ci:
        return 0
    return 1 if blocks(findings, new_entries) else 0


if __name__ == "__main__":
    raise SystemExit(main())
