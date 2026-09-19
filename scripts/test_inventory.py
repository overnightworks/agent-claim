#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""Assertion inventory of one test module -- what a spec-driven rewrite must not lose.

Adapted from the knowlagentic blueprint (``test_inventory.py``, MR !495 @ 1fc0a6a8; see
``SOURCES.txt``). The algorithm is generic; only the CONFIG block below is aco-specific.

A test file is rewritten wholesale (parametrized families replacing bespoke classes) under
one contract: nothing the old file *proved* may silently vanish. This script turns a test
module into that evidence -- a deterministic, diffable inventory -- so a reviewer compares the
BEFORE and AFTER inventories instead of re-reading thousands of lines of old test bodies. It is
stdlib + ``ast`` only and runs no test.

Three sections, each sorted so two runs over the same file are byte-identical:

(a) **Spec IDs** -- every criterion ID cited after a ``Spec:`` marker in a ``test_*``
    docstring (the comma-separated list that follows ``Spec:`` up to the first
    ``--``/em-dash/``.``; a prose mention elsewhere in the docstring is not a citation).
    Dormant for aco: there is no ``specs/`` tree or ``spec_coverage.py`` owner yet, so no test
    docstring carries a ``Spec:`` marker and this section is always empty -- the detection
    itself needs no aco-specific change, it simply finds nothing until a spec system exists.
(b) **Assert literals** -- every literal a test *asserts about*, with the tests that hold it.
    A literal is a ``str``/``bytes``/``int``/``bool`` constant (an f-string contributes its
    constant parts). It counts when it sits in an *assertion site* -- a bare ``assert``'s test
    expression (never its failure message), a ``pytest.raises(...)`` call, a call to a
    helper whose name starts with ``assert``/``_assert``, or a call to ``RUN_CASE`` (a shared
    scenario helper name; empty for aco -- see CONFIG) -- or in the backward slice feeding one:
    the statements binding a name an assertion site reads, followed transitively through
    simple ``Name`` targets (``x = ...``, ``for x in ...``, ``with ... as x``, ``x := ...``)
    inside the test body -- and the ``parametrize`` argument values of the test or its class,
    which bind the parameter names exactly as an assignment would (a table-driven family's
    expected values live there, not in the body; ``ids=`` is labelling, never a value). A
    module-level constant read inside a site or slice contributes its literal tree, and a
    module-level ``assert*`` helper called from a test contributes its own assertion-site
    literals to the caller. Setup that only *writes* state binds no read name, so it does not
    count.
(c) **Fixtures and helpers** -- the fixtures a test requests (its parameters, plus
    ``request.getfixturevalue("...")``) and the module-scope callables it invokes (imported
    names, module-level functions, ``self._helper`` methods), each with the tests using it.

A metrics header precedes the sections: file lines, test count, class count (and how many hold
a single test), parametrized tests and their share, and clone lines. A test is parametrized
when it, its class, or a module-level fixture it requests carries ``parametrize``/``params=``.
**Clone lines** follow the same method as the per-file inventory: each test body is tokenized,
literals are normalised (``NUMBER``/``STRING`` tokens collapse to one placeholder each, comments
and layout tokens are dropped), the token stream is cut into overlapping 6-token shingles, and
two tests whose shingle sets have Jaccard similarity >= 0.80 join one clone cluster
(transitively). A cluster's redundant lines are its members' total lines minus its largest
member -- the copies, not the original.

CI rewrite gate (issue #319)
-----------------------------
``--ci`` runs over the whole ``tests/`` tree instead of one file. The base ladder is the same
as ``test_budget.py``'s and ``similar_methods.py``'s: a pull request compares against the
merge-base with ``origin/$GITHUB_BASE_REF``; a push to ``main`` (no base ref,
``GITHUB_EVENT_NAME=push``) compares against ``HEAD^1``; outside CI, ``origin/main``.

A test module counts as **rewritten** when the diff against the base removes ``git diff
--numstat``'s own deleted-line count for at least ``REWRITE_DROP_THRESHOLD_PCT`` percent of its
base line count -- the simplest honest signal that a file was rebuilt rather than edited, and
the same number a reviewer sees in the diff stat. A brand-new file (absent at the base) has
nothing to shrink and is never "rewritten" by this rule.

With no rewritten module in the diff, the job SKIPS with a one-line sentence naming the base and
the threshold (exit 0). With one or more, each rewritten module's BASE and HEAD inventories are
compared: every assert-literal key the base held that HEAD does not is a dropped literal. A
dropped literal blocks (``FAIL``, exit 1) unless it is listed in
``scripts/test_inventory_dropped.txt`` (module path, literal ``repr()``, reason -- tab
separated, one per line, written by hand in the same change that drops it, the same "a reviewer
reads the reason" exception ``test_budget.py``'s ``# budget:`` ledger and
``similar_methods.py``'s exemption ledger use). Spec IDs are compared the same way but, being
always empty today, never produce a finding.

Usage:
    uv run scripts/test_inventory.py tests/test_cli.py           # text inventory of one file
    uv run scripts/test_inventory.py tests/test_cli.py --json    # machine-readable
    uv run scripts/test_inventory.py --ci                        # the CI rewrite gate
    uv run scripts/test_inventory.py --ci --target origin/main
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import os
import re
import subprocess
import sys
import textwrap
import tokenize
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

# --- aco configuration --------------------------------------------------------------------
TESTS_DIR = Path("tests")
TEST_MODULE_GLOB = "test_*.py"
REMOTE = "origin"
BASE_REF_ENV = "GITHUB_BASE_REF"  # set by GitHub Actions on a pull_request event
EVENT_NAME_ENV = "GITHUB_EVENT_NAME"  # "push" on a push to main
DEFAULT_TARGET_BRANCH = "main"  # local fallback when neither env var is set
FIRST_PARENT_TARGET = "HEAD^1"
REWRITE_DROP_THRESHOLD_PCT = 50
DROPPED_REL = Path("scripts/test_inventory_dropped.txt")
# The shared e2e scenario helper whose keyword arguments count as assertions (ka: `run_case` in
# `tests/e2e/_helpers.py`). aco names no such helper yet; empty is inert (no callee is named "").
RUN_CASE = ""
# --- end aco configuration -----------------------------------------------------------------

SPEC_ID_PATTERN = re.compile(r"\b([A-Z]+-[A-Z]*\d+)\b")
_SPEC_CITATION_PREFIX = re.compile(r"\bSpec:")
_SPEC_CITATION_TERMINATOR = re.compile(r"--|—|\.")
_ASSERT_HELPER_PATTERN = re.compile(r"^_?assert")
_RAISES = "raises"
_FIXTURE_LOOKUP = "getfixturevalue"
_PARAMETRIZE = "parametrize"
_FIXTURE_PARAMS = "params"
SHINGLE_SIZE = 6
CLONE_THRESHOLD = 0.80
FAIL = "FAIL"
_LITERAL_TYPES = (str, bytes, int)
_LAYOUT_TOKENS = frozenset(
    {
        tokenize.COMMENT,
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.ENCODING,
        tokenize.ENDMARKER,
    }
)
# f-string tokens exist only on 3.12+; on older runtimes the whole f-string is one STRING.
_STRING_TOKENS = frozenset(
    {tokenize.STRING}
    | {getattr(tokenize, name, None) for name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END")}
    - {None}
)

FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True)
class TestCase:
    qualname: str
    node: FunctionNode
    parametrized: bool
    param_units: tuple[tuple[tuple[str, ...], ast.expr], ...] = ()

    @property
    def line_count(self) -> int:
        return (self.node.end_lineno or self.node.lineno) - self.node.lineno + 1


@dataclass
class ModuleScope:
    """What a test body can reach at module level: literal constants, assert helpers, callables."""

    constants: dict[str, ast.expr] = field(default_factory=dict)
    assert_helpers: dict[str, FunctionNode] = field(default_factory=dict)
    callables: set[str] = field(default_factory=set)
    param_fixtures: set[str] = field(default_factory=set)


@dataclass
class Inventory:
    path: str
    lines: int
    classes: int
    single_test_classes: int
    parametrized: int
    clone_clusters: list[list[str]]
    clone_lines: int
    spec_ids: dict[str, list[str]]
    literals: dict[str, list[str]]
    fixtures: dict[str, list[str]]
    helpers: dict[str, list[str]]
    tests: int

    @property
    def parametrized_pct(self) -> float:
        return round(self.parametrized * 100 / self.tests, 1) if self.tests else 0.0


# --- (a) spec citations ----------------------------------------------------------------------


def iter_spec_citations(doc: str) -> list[str]:
    """IDs inside every ``Spec:`` zone of *doc*, in order of appearance."""
    cited: list[str] = []
    for marker in _SPEC_CITATION_PREFIX.finditer(doc):
        rest = doc[marker.end() :]
        terminator = _SPEC_CITATION_TERMINATOR.search(rest)
        zone = rest[: terminator.start()] if terminator else rest
        cited.extend(SPEC_ID_PATTERN.findall(zone))
    return cited


# --- module scope ----------------------------------------------------------------------------


def _is_literal_tree(node: ast.expr) -> bool:
    if isinstance(node, (ast.Constant, ast.JoinedStr)):
        return True
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_is_literal_tree(element) for element in node.elts)
    if isinstance(node, ast.Dict):
        return all(
            key is not None and _is_literal_tree(key) and _is_literal_tree(value)
            for key, value in zip(node.keys, node.values, strict=True)
        )
    return False


def _has_params_keyword(func: FunctionNode) -> bool:
    for decorator in func.decorator_list:
        if isinstance(decorator, ast.Call) and any(
            keyword.arg == _FIXTURE_PARAMS for keyword in decorator.keywords
        ):
            return True
    return False


def module_scope(tree: ast.Module) -> ModuleScope:
    scope = ModuleScope()
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            value = node.value
            if (
                isinstance(target, ast.Name)
                and (not isinstance(node, ast.Assign) or len(node.targets) == 1)
                and value is not None
                and _is_literal_tree(value)
            ):
                scope.constants[target.id] = value
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope.callables.add(node.name)
            if _ASSERT_HELPER_PATTERN.match(node.name):
                scope.assert_helpers[node.name] = node
            if _has_params_keyword(node):
                scope.param_fixtures.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            scope.callables.update(
                (alias.asname or alias.name).split(".")[0] for alias in node.names
            )
    return scope


# --- test discovery --------------------------------------------------------------------------


def _is_test_function(node: ast.AST) -> bool:
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
        "test_"
    )


def _is_parametrized(node: FunctionNode | ast.ClassDef) -> bool:
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute) and target.attr == _PARAMETRIZE:
            return True
    return False


def _parameter_names(func: FunctionNode) -> set[str]:
    arguments = func.args
    names = {arg.arg for arg in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]}
    return names - {"self", "cls"}


def _parametrize_argnames(node: ast.expr) -> tuple[str, ...]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return tuple(name.strip() for name in node.value.split(",") if name.strip())
    if isinstance(node, (ast.Tuple, ast.List)):
        return tuple(
            element.value
            for element in node.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        )
    return ()


def _parametrize_units(node: FunctionNode | ast.ClassDef) -> list[tuple[tuple[str, ...], ast.expr]]:
    """(parameter names, argvalues expression) per ``parametrize`` decorator on *node*."""
    units: list[tuple[tuple[str, ...], ast.expr]] = []
    for decorator in node.decorator_list:
        if not (
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == _PARAMETRIZE
        ):
            continue
        keywords = {keyword.arg: keyword.value for keyword in decorator.keywords}
        names_node = decorator.args[0] if decorator.args else keywords.get("argnames")
        values_node = decorator.args[1] if len(decorator.args) > 1 else keywords.get("argvalues")
        if names_node is None or values_node is None:
            continue
        units.append((_parametrize_argnames(names_node), values_node))
    return units


def _make_case(func: FunctionNode, owner: ast.ClassDef | None, scope: ModuleScope) -> TestCase:
    qualname = f"{owner.name}::{func.name}" if owner is not None else func.name
    parametrized = (
        _is_parametrized(func)
        or (owner is not None and _is_parametrized(owner))
        or bool(_parameter_names(func) & scope.param_fixtures)
    )
    param_units = _parametrize_units(func) + (_parametrize_units(owner) if owner else [])
    return TestCase(qualname, func, parametrized, tuple(param_units))


def iter_tests(tree: ast.Module, scope: ModuleScope) -> tuple[list[TestCase], int, int]:
    """Every ``test_*`` function, class-qualified, plus (class count, single-test class count).

    A class holding no ``test_*`` function -- a per-family ``Case`` NamedTuple, a fake -- is a
    parameter record, not a test family, and is not counted.
    """
    cases: list[TestCase] = []
    classes = single = 0
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            members = [sub for sub in node.body if _is_test_function(sub)]
            if not members:
                continue
            classes += 1
            single += len(members) == 1
            cases.extend(_make_case(sub, node, scope) for sub in members)  # type: ignore[arg-type]
        elif _is_test_function(node):
            cases.append(_make_case(node, None, scope))  # type: ignore[arg-type]
    return cases, classes, single


# --- (b) assert literals ---------------------------------------------------------------------


def _callee_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_assertion_call(call: ast.Call) -> bool:
    name = _callee_name(call)
    return name is not None and (
        name in (_RAISES, RUN_CASE) or bool(_ASSERT_HELPER_PATTERN.match(name))
    )


def assertion_sites(func: FunctionNode) -> list[ast.expr]:
    """The expressions a test asserts: bare ``assert`` tests, ``raises``, ``assert*`` and
    ``RUN_CASE`` calls."""
    sites: list[ast.expr] = []
    for node in ast.walk(func):
        if isinstance(node, ast.Assert):
            sites.append(node.test)
        elif isinstance(node, ast.Call) and _is_assertion_call(node):
            sites.append(node)
    return sites


def _target_names(target: ast.expr) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for element in target.elts for name in _target_names(element)]
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    return []


def _bound_names(stmt: ast.stmt) -> tuple[list[str], list[ast.expr]]:
    """(names *stmt* binds through simple targets, the expressions it binds them from)."""
    if isinstance(stmt, ast.Assign):
        targets: list[ast.expr] = list(stmt.targets)
        values: list[ast.expr] = [stmt.value]
    elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        targets, values = [stmt.target], [stmt.value] if stmt.value is not None else []
    elif isinstance(stmt, (ast.For, ast.AsyncFor)):
        targets, values = [stmt.target], [stmt.iter]
    elif isinstance(stmt, (ast.With, ast.AsyncWith)):
        targets = [item.optional_vars for item in stmt.items if item.optional_vars is not None]
        values = [item.context_expr for item in stmt.items]
    else:
        return [], []
    return [name for target in targets for name in _target_names(target)], values


def _binding_units(func: FunctionNode) -> list[tuple[list[str], list[ast.expr]]]:
    units: list[tuple[list[str], list[ast.expr]]] = []
    for node in ast.walk(func):
        if isinstance(node, ast.stmt):
            names, values = _bound_names(node)
            if names:
                units.append((names, values))
        elif isinstance(node, ast.NamedExpr):
            units.append(([node.target.id], [node.value]))
    return units


def _loaded_names(nodes: Iterable[ast.AST]) -> set[str]:
    return {
        sub.id
        for root in nodes
        for sub in ast.walk(root)
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load)
    }


def backward_slice(
    func: FunctionNode,
    sites: list[ast.expr],
    extra_units: tuple[tuple[tuple[str, ...], ast.expr], ...] = (),
) -> list[ast.expr]:
    """Expressions that bind a name an assertion site reads, followed transitively.

    *extra_units* are bindings made outside the body -- a ``parametrize`` decorator's
    argument values bound to its parameter names.
    """
    units = _binding_units(func) + [(list(names), [value]) for names, value in extra_units]
    wanted = _loaded_names(sites)
    sliced: list[ast.expr] = []
    taken: set[int] = set()
    changed = True
    while changed:
        changed = False
        for index, (names, values) in enumerate(units):
            if index in taken or not wanted.intersection(names):
                continue
            taken.add(index)
            sliced.extend(values)
            wanted |= _loaded_names(values)
            changed = True
    return sliced


def _constants(node: ast.AST) -> list[object]:
    return [
        sub.value
        for sub in ast.walk(node)
        if isinstance(sub, ast.Constant) and isinstance(sub.value, _LITERAL_TYPES)
    ]


def literal_key(value: object) -> str:
    return repr(value)


def test_literals(case: TestCase, scope: ModuleScope) -> set[str]:
    """Literal keys the test asserts about (see the module docstring for the rule)."""
    func = case.node
    sites = assertion_sites(func)
    roots: list[ast.AST] = [*sites, *backward_slice(func, sites, case.param_units)]
    found = [value for root in roots for value in _constants(root)]
    for name in _loaded_names(roots):
        if name in scope.constants:
            found.extend(_constants(scope.constants[name]))
    for call in (sub for root in roots for sub in ast.walk(root) if isinstance(sub, ast.Call)):
        helper = scope.assert_helpers.get(_callee_name(call) or "")
        if helper is not None:
            found.extend(value for site in assertion_sites(helper) for value in _constants(site))
    return {literal_key(value) for value in found}


# --- (c) fixtures and helpers ----------------------------------------------------------------


def test_fixtures(func: FunctionNode) -> set[str]:
    fixtures = _parameter_names(func)
    for node in ast.walk(func):
        if not (isinstance(node, ast.Call) and _callee_name(node) == _FIXTURE_LOOKUP and node.args):
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            fixtures.add(first.value)
    return fixtures


def test_helpers(func: FunctionNode, scope: ModuleScope) -> set[str]:
    helpers: set[str] = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        if isinstance(callee, ast.Name) and callee.id in scope.callables:
            helpers.add(callee.id)
        elif (
            isinstance(callee, ast.Attribute)
            and isinstance(callee.value, ast.Name)
            and callee.value.id == "self"
        ):
            helpers.add(f"self.{callee.attr}")
    return helpers


# --- clone lines -----------------------------------------------------------------------------


def _normalised_tokens(source: str) -> list[str]:
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return source.split()
    out: list[str] = []
    for token in tokens:
        if token.type in _LAYOUT_TOKENS:
            continue
        if token.type == tokenize.NUMBER:
            out.append("<num>")
        elif token.type in _STRING_TOKENS:
            out.append("<str>")
        else:
            out.append(token.string)
    return out


def shingles(source: str, size: int = SHINGLE_SIZE) -> frozenset[tuple[str, ...]]:
    tokens = _normalised_tokens(source)
    if len(tokens) <= size:
        return frozenset({tuple(tokens)})
    return frozenset(tuple(tokens[i : i + size]) for i in range(len(tokens) - size + 1))


def jaccard(left: frozenset[tuple[str, ...]], right: frozenset[tuple[str, ...]]) -> float:
    union = len(left | right)
    return len(left & right) / union if union else 0.0


def _case_source(case: TestCase, lines: list[str]) -> str:
    end = case.node.end_lineno or case.node.lineno
    return textwrap.dedent("\n".join(lines[case.node.lineno - 1 : end]))


def clone_clusters(cases: list[TestCase], lines: list[str]) -> list[list[TestCase]]:
    """Groups of tests whose normalised token shingles are (transitively) near-identical."""
    signatures = [shingles(_case_source(case, lines)) for case in cases]
    parent = list(range(len(cases)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for i in range(len(cases)):
        for j in range(i + 1, len(cases)):
            if jaccard(signatures[i], signatures[j]) >= CLONE_THRESHOLD:
                parent[find(i)] = find(j)
    grouped: dict[int, list[TestCase]] = defaultdict(list)
    for index, case in enumerate(cases):
        grouped[find(index)].append(case)
    return [group for group in grouped.values() if len(group) > 1]


def redundant_lines(clusters: list[list[TestCase]]) -> int:
    return sum(
        sum(case.line_count for case in cluster) - max(case.line_count for case in cluster)
        for cluster in clusters
    )


# --- assembly --------------------------------------------------------------------------------


def _sorted_index(pairs: Iterable[tuple[str, str]]) -> dict[str, list[str]]:
    index: dict[str, set[str]] = defaultdict(set)
    for key, test in pairs:
        index[key].add(test)
    return {key: sorted(index[key]) for key in sorted(index)}


def build_inventory_from_source(source: str, display_path: str) -> Inventory:
    tree = ast.parse(source, filename=display_path)
    scope = module_scope(tree)
    cases, classes, single = iter_tests(tree, scope)
    lines = source.splitlines()
    clusters = clone_clusters(cases, lines)
    spec_pairs: list[tuple[str, str]] = []
    literal_pairs: list[tuple[str, str]] = []
    fixture_pairs: list[tuple[str, str]] = []
    helper_pairs: list[tuple[str, str]] = []
    for case in cases:
        doc = ast.get_docstring(case.node) or ""
        spec_pairs.extend((spec_id, case.qualname) for spec_id in iter_spec_citations(doc))
        literal_pairs.extend((key, case.qualname) for key in test_literals(case, scope))
        fixture_pairs.extend((name, case.qualname) for name in test_fixtures(case.node))
        helper_pairs.extend((name, case.qualname) for name in test_helpers(case.node, scope))
    return Inventory(
        path=display_path,
        lines=len(lines),
        classes=classes,
        single_test_classes=single,
        parametrized=sum(case.parametrized for case in cases),
        clone_clusters=sorted(sorted(case.qualname for case in cluster) for cluster in clusters),
        clone_lines=redundant_lines(clusters),
        spec_ids=_sorted_index(spec_pairs),
        literals=_sorted_index(literal_pairs),
        fixtures=_sorted_index(fixture_pairs),
        helpers=_sorted_index(helper_pairs),
        tests=len(cases),
    )


def build_inventory(path: Path) -> Inventory:
    return build_inventory_from_source(path.read_text(encoding="utf-8"), path.as_posix())


# --- rendering -------------------------------------------------------------------------------


def _render_section(title: str, index: dict[str, list[str]]) -> list[str]:
    out = ["", f"## {title} ({len(index)})"]
    for key, tests in index.items():
        out.append(key)
        out.extend(f"    {test}" for test in tests)
    return out


def render_text(inventory: Inventory) -> str:
    out = [
        f"# test inventory: {inventory.path}",
        f"lines: {inventory.lines}",
        f"tests: {inventory.tests}",
        f"classes: {inventory.classes} ({inventory.single_test_classes} single-test)",
        f"parametrized: {inventory.parametrized} ({inventory.parametrized_pct}%)",
        f"clone lines: {inventory.clone_lines} in {len(inventory.clone_clusters)} clusters",
    ]
    out.extend(_render_section("spec ids", inventory.spec_ids))
    out.extend(_render_section("literals", inventory.literals))
    out.extend(_render_section("fixtures", inventory.fixtures))
    out.extend(_render_section("helpers", inventory.helpers))
    out.extend(["", f"## clone clusters ({len(inventory.clone_clusters)})"])
    out.extend("- " + " | ".join(cluster) for cluster in inventory.clone_clusters)
    return "\n".join(out) + "\n"


def render_json(inventory: Inventory) -> str:
    payload = {
        "path": inventory.path,
        "lines": inventory.lines,
        "tests": inventory.tests,
        "classes": inventory.classes,
        "single_test_classes": inventory.single_test_classes,
        "parametrized": inventory.parametrized,
        "parametrized_pct": inventory.parametrized_pct,
        "clone_lines": inventory.clone_lines,
        "clone_clusters": inventory.clone_clusters,
        "spec_ids": inventory.spec_ids,
        "literals": inventory.literals,
        "fixtures": inventory.fixtures,
        "helpers": inventory.helpers,
    }
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


# --- CI rewrite gate ---------------------------------------------------------------------------


class TargetError(Exception):
    """The target ref could not be resolved."""


def default_target() -> str:
    """The base ladder (issue #319), shared with ``test_budget.py``/``similar_methods.py``."""
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


def git_show_file(commit: str, relpath: str) -> str | None:
    """The content of *relpath* at *commit*, or ``None`` when it does not exist there."""
    result = _git("show", f"{commit}:{relpath}")
    return result.stdout.decode("utf-8") if result.returncode == 0 else None


def changed_test_module_stats(base: str) -> dict[str, tuple[int, int]]:
    """``path -> (added, deleted)`` from ``git diff --numstat`` against *base*, for every
    tracked test module the diff touches."""
    pattern = f"{TESTS_DIR.as_posix()}/{TEST_MODULE_GLOB}"
    result = _git("diff", "--numstat", base, "HEAD", "--", pattern)
    stats: dict[str, tuple[int, int]] = {}
    for line in result.stdout.decode("utf-8").splitlines():
        added, deleted, path = line.split("\t", 2)
        if added == "-" or deleted == "-":  # binary diff; never true for a .py file
            continue
        stats[path] = (int(added), int(deleted))
    return stats


def rewritten_modules(base: str) -> list[str]:
    """Test modules whose diff against *base* removes >= ``REWRITE_DROP_THRESHOLD_PCT`` percent
    of their base line count (see the module docstring)."""
    rewritten: list[str] = []
    for path, (_added, deleted) in changed_test_module_stats(base).items():
        base_source = git_show_file(base, path)
        if base_source is None:  # new at HEAD, nothing to shrink
            continue
        base_lines = len(base_source.splitlines())
        if base_lines and deleted * 100 / base_lines >= REWRITE_DROP_THRESHOLD_PCT:
            rewritten.append(path)
    return sorted(rewritten)


def load_dropped(path: Path) -> dict[str, frozenset[str]]:
    """``module path -> {literal repr}`` from *path* (tab-separated ``path, key, reason`` per
    line); a missing ledger allows nothing. Written by hand in the change that drops the
    literal, never generated."""
    if not path.exists():
        return {}
    grouped: defaultdict[str, set[str]] = defaultdict(set)
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        module_path, key, _reason = line.split("\t", 2)
        grouped[module_path].add(key)
    return {module_path: frozenset(keys) for module_path, keys in grouped.items()}


def undocumented_drops(
    base_inventory: Inventory, head_inventory: Inventory, allowed: frozenset[str]
) -> list[str]:
    """Assert-literal keys the base inventory held that HEAD dropped, minus *allowed*."""
    dropped = set(base_inventory.literals) - set(head_inventory.literals)
    return sorted(dropped - allowed)


def run_ci(target: str | None) -> int:
    ref = target or default_target()
    try:
        base = merge_base(ref, resolve_target(ref))
    except TargetError as exc:
        print(f"{FAIL}: {exc}", file=sys.stderr)
        return 1
    rewritten = rewritten_modules(base)
    if not rewritten:
        print(
            f"test-inventory: no {TEST_MODULE_GLOB} diff against {base[:12]} removes "
            f"{REWRITE_DROP_THRESHOLD_PCT}% or more of its base line count; skipping "
            "(no rewritten test module in this change).",
        )
        return 0

    dropped_allowed = load_dropped(DROPPED_REL)
    failed = False
    for path in rewritten:
        base_source = git_show_file(base, path)
        assert base_source is not None  # rewritten_modules() already required a base version
        base_inventory = build_inventory_from_source(base_source, path)
        head_inventory = build_inventory(Path(path))
        undocumented = undocumented_drops(
            base_inventory, head_inventory, dropped_allowed.get(path, frozenset())
        )
        if undocumented:
            failed = True
            print(
                f"{FAIL} {path}: literal(s) dropped by the rewrite without a {DROPPED_REL} entry:",
                file=sys.stderr,
            )
            for key in undocumented:
                print(f"    {key}", file=sys.stderr)
        else:
            dropped_count = len(set(base_inventory.literals) - set(head_inventory.literals))
            print(f"OK {path}: rewritten, {dropped_count} literal(s) dropped, all documented")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assertion inventory of test modules")
    parser.add_argument("test_file", type=Path, nargs="?", help="a single test module to inventory")
    parser.add_argument("--json", action="store_true", help="machine-readable JSON output")
    parser.add_argument("--ci", action="store_true", help="run the whole-tree CI rewrite gate")
    parser.add_argument(
        "--target",
        metavar="REF",
        default=None,
        help=f"(--ci only) git ref to compare against (default: {REMOTE}/${BASE_REF_ENV} on a "
        f"pull request, HEAD^1 on a push to {DEFAULT_TARGET_BRANCH}, else {REMOTE}/"
        f"{DEFAULT_TARGET_BRANCH})",
    )
    args = parser.parse_args(argv)

    if args.ci:
        return run_ci(args.target)
    if args.test_file is None:
        parser.error("test_file is required unless --ci is given")
    if not args.test_file.is_file():
        print(f"error: {args.test_file} is not a file", file=sys.stderr)
        return 1
    inventory = build_inventory(args.test_file)
    sys.stdout.write(render_json(inventory) if args.json else render_text(inventory))
    return 0


if __name__ == "__main__":
    sys.exit(main())
