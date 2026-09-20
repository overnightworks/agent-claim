#!/usr/bin/env python3
"""Mechanical style lint for ``specs/*.spec.md`` (issue #385).

Ported from the knowlagentic blueprint (``agents-blueprint/gates/spec_lint.py``, copied into
the handover folder ``/home/felix-hummert/git/handover-2026-09-19/``). That gate's rules are
shaped for its own repository's spec dialect (a ratchet of accepted debt, a ``{types: ...}``
axis marker, a ``$ ka`` transcript check, a SPEC-2/SPEC-15 result-before-trigger ordering
rule) which aco's house format does not use; ``missing_transcript`` in particular would fire on
every aco spec, since aco's example transcripts open ``$ aco``, not ``$ ka``, inside a fenced
block introduced by its own ``Setup:`` line rather than a bare ``$ ka`` opener. This gate keeps
only the rules that survive calibration against aco's 23 ``specs/*.spec.md`` files and adapts
them to the house shape: a ``- [ ] [ID] ...`` criterion is always exactly one physical line (no
continuation-line joining is needed here), a behaviour table cites criterion IDs by their bare
token, and a named example is a ``### E-<PREFIX>-nn`` heading followed by a ``Setup:`` line
before its first fenced block.

Checks (one finding per hit, each named ``file:line``):

* ``criterion_length``: the criterion text -- everything after ``[ID]`` on its own line,
  whitespace-trimmed -- exceeds ``MAX_CRITERION_LENGTH`` characters.
* ``id_sequence``: a file's own criterion IDs share one prefix; the numeric suffixes used by
  live criteria, together with any suffix a ``- <ID> (retired ...)``/``(removed ...)`` note
  names, cover every integer from the lowest to the highest used with no gap and no duplicate.
* ``unresolved_reference``: every ``see ...`` reference list anywhere in a line -- not only
  right after ``(`` -- or a behaviour-table data cell (its row-label column is prose, not a
  citation, and is never scanned), names an ``E-<PREFIX>-nn`` that no ``###`` heading defines,
  or a plain ``ID-nn`` that no ``- [ ] [ID-nn]`` bullet anywhere in ``specs/`` defines --
  criteria and examples cite across files routinely, so both namespaces are read tree-wide, not
  per file. A ``see`` list may comma-join several ids (``see A-01, A-02``), shorthand a further
  number under the same prefix with ``/`` (``A-01/99`` means ``A-01`` and ``A-99``), or name an
  inclusive run with ``..`` (``A-01..A-99``/``A-01..99``); each id the shorthand expands to is
  checked on its own.
* ``missing_section``: the file has no ``## Behavior table``, no ``## Never``, or no
  ``## Examples`` heading.
* ``implementation_name``: a criterion's text, with every backtick-quoted span removed first,
  still names a Python-shaped identifier outside any literal: a snake_case identifier with a
  lowercase letter on both sides of an underscore (``_git_run``, ``claim_lifecycle``), a
  Python file name (``checkout.py``), a call (``hook_command_paths()``), or a dotted attribute
  whose final segment is not a contract file extension (``store.claim_lifecycle``,
  ``HookToolEffect.COMMAND_TEXT``, but not ``board.toml`` or ``next.spec.md``). A shell only
  ever observes a command, flag, path, or printed literal, and aco's house style already
  backticks every one of those, an UPPER_SNAKE environment name (``XDG_CONFIG_HOME``), a
  trailer key (``Work-Item:``), and an id (``CLAIM-15``); the calibration pass over all 23
  files found this pattern firing exactly zero times on real criteria, so a hit here is new
  debt, not a house convention this gate has never seen.
* ``missing_setup``: a ``### E-<PREFIX>-nn`` example has no ``Setup:`` line before its first
  fenced block (the block itself may be preceded by prose, as several examples do; the scan
  reads every line up to that fence, not a fixed lookahead).

Exemptions
----------
``scripts/spec_lint_exemptions.txt``, next to this script, is the one ledger of accepted
findings: one ``file:line:rule — reason`` line, written by hand only when a finding is
deliberately accepted. Empty when there is nothing to exempt -- the calibration pass leaves
none needed today. A line missing its `` — reason`` tail cannot be parsed into a key at all,
so it surfaces as its own ``malformed_exemption`` finding and always fails ``--ci``.

Usage:
    uv run scripts/spec_lint.py             # human-readable report
    uv run scripts/spec_lint.py --json      # machine-readable
    uv run scripts/spec_lint.py --ci        # exit non-zero on an unexempted finding
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

SPEC_DIR = Path("specs")
EXEMPTIONS_FILE = Path(__file__).resolve().parent / "spec_lint_exemptions.txt"
MAX_CRITERION_LENGTH = 200

# A criterion is always one physical line in aco's house format: no continuation-line
# joining, unlike the blueprint's ``_iter_blocks``.
CRITERION_PATTERN = re.compile(r"^- \[ \] \[([A-Z]+-\d+)\](.*)$")
EXAMPLE_HEADING_PATTERN = re.compile(r"^### (E-[A-Za-z0-9]+-\d+)\b")
# A retirement note is a checkbox-less bullet naming the retired id(s), e.g.
# ``- LAND-36 (retired 19.09.2026, issue #359): ...``.
RETIRED_NOTE_PATTERN = re.compile(
    r"^-\s*([A-Z]+-\d+(?:\s*,\s*[A-Z]+-\d+)*)\s*\((?:retired|removed)\b", re.IGNORECASE
)
# A reference id is a compound example id (``E-<PREFIX>-nn``) or a plain criterion id
# (``<PREFIX>-nn``); both shapes can follow ``see`` or a shorthand separator below.
_REFERENCE_ID = r"(?:E-[A-Za-z0-9]+-\d+|[A-Za-z]+-\d+)"
# One ``see`` reference expression: a plain id, a ``..`` range (``A-01..A-99``/``A-01..99``),
# or a ``/`` shorthand (``A-01/99``) naming further numbers under the same prefix.
_REFERENCE_EXPRESSION = rf"{_REFERENCE_ID}(?:\.\.(?:{_REFERENCE_ID}|\d+)|(?:/\d+)+)?"
EXAMPLE_TOKEN_PATTERN = re.compile(r"\bE-[A-Za-z0-9]+-\d+\b")
ID_TOKEN_PATTERN = re.compile(r"\b[A-Z]+-\d+\b")
BACKTICK_LITERAL_PATTERN = re.compile(r"`[^`]*`")
# ``see`` opens a reference list anywhere in a line, not only right after ``(``: a leading
# clause (``(#310; see E-NEXT-06)``) introduces one just as a bare ``(see ...)`` does.
SEE_KEYWORD_PATTERN = re.compile(r"\bsee\b\s+")
REFERENCE_LIST_PATTERN = re.compile(rf"{_REFERENCE_EXPRESSION}(?:\s*,\s*{_REFERENCE_EXPRESSION})*")
# SPEC-6, adapted: a shell never observes a bare Python identifier, file, call, or attribute
# access; command names, flags, paths and printed literals are always backticked in aco's
# house style, so anything matching one of the shapes below outside a backtick literal is an
# implementation name that leaked in.
IMPLEMENTATION_CALL_PATTERN = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\([^)]*\)")
# A Python module file, e.g. ``checkout.py`` -- always implementation, never a contract file
# (those use ``.toml``/``.json``/``.lock``/``.md``, excluded below).
PYTHON_FILENAME_PATTERN = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\.py\b")
# A snake_case identifier: some underscore in it has a lowercase letter on both sides
# (``_git_run``, ``claim_lifecycle``). An UPPER_SNAKE environment name (``XDG_CONFIG_HOME``)
# has no such underscore -- both its neighbours are uppercase -- so it never matches here.
_IDENTIFIER_PATTERN = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_LOWERCASE_SNAKE_UNDERSCORE_PATTERN = re.compile(r"[a-z]_[a-z]")
# A dotted attribute access, e.g. ``store.claim_lifecycle`` or ``HookToolEffect.COMMAND_TEXT``.
# Each segment must be at least two characters: a real module, class, or attribute name always
# is, while a one-letter segment is prose shorthand (``e.g.``, ``i.e.``), not an identifier.
# Excluded below when its final segment is a contract file extension: those dotted chains
# (``board.toml``, ``next.spec.md``) name a file, not a Python attribute.
DOTTED_ATTRIBUTE_PATTERN = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]+(?:\.[A-Za-z_][A-Za-z0-9_]+)+\b")
CONTRACT_FILE_EXTENSIONS = frozenset({"toml", "json", "lock", "md"})
BEHAVIOR_TABLE_HEADING_PATTERN = re.compile(r"^## Behavior table\b")
NEVER_HEADING_PATTERN = re.compile(r"^## Never\b")
EXAMPLES_HEADING_PATTERN = re.compile(r"^## Examples\b")
TABLE_SEPARATOR_PATTERN = re.compile(r"^\|[\s:|-]+$")
SETUP_LINE_PATTERN = re.compile(r"^Setup:")

REQUIRED_SECTIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("## Behavior table", BEHAVIOR_TABLE_HEADING_PATTERN),
    ("## Never", NEVER_HEADING_PATTERN),
    ("## Examples", EXAMPLES_HEADING_PATTERN),
)


class Rule(Enum):
    CRITERION_LENGTH = "criterion_length"
    ID_SEQUENCE = "id_sequence"
    UNRESOLVED_REFERENCE = "unresolved_reference"
    MISSING_SECTION = "missing_section"
    IMPLEMENTATION_NAME = "implementation_name"
    MISSING_SETUP = "missing_setup"
    MALFORMED_EXEMPTION = "malformed_exemption"


@dataclass(frozen=True)
class Finding:
    rule: Rule
    file: str
    line: int
    detail: str

    @property
    def location(self) -> str:
        return f"{self.file}:{self.line}"

    @property
    def exemption_key(self) -> str:
        return f"{self.file}:{self.line}:{self.rule.value}"


@dataclass(frozen=True)
class Criterion:
    file: str
    line: int
    spec_id: str
    text: str


@dataclass(frozen=True)
class SpecFile:
    name: str
    lines: tuple[str, ...]
    criteria: tuple[Criterion, ...]
    retired_ids: frozenset[str]
    example_ids: frozenset[str]


def _extract_criteria(name: str, lines: list[str]) -> list[Criterion]:
    criteria = []
    for lineno, line in enumerate(lines, 1):
        match = CRITERION_PATTERN.match(line)
        if match is not None:
            criteria.append(Criterion(name, lineno, match.group(1), match.group(2).strip()))
    return criteria


def _extract_retired_ids(lines: list[str]) -> frozenset[str]:
    retired: set[str] = set()
    for line in lines:
        match = RETIRED_NOTE_PATTERN.match(line.strip())
        if match is not None:
            retired.update(ID_TOKEN_PATTERN.findall(match.group(1)))
    return frozenset(retired)


def _extract_example_ids(lines: list[str]) -> frozenset[str]:
    return frozenset(
        match.group(1) for line in lines if (match := EXAMPLE_HEADING_PATTERN.match(line))
    )


def load_spec_file(path: Path) -> SpecFile:
    lines = path.read_text(encoding="utf-8").splitlines()
    return SpecFile(
        name=path.name,
        lines=tuple(lines),
        criteria=tuple(_extract_criteria(path.name, lines)),
        retired_ids=_extract_retired_ids(lines),
        example_ids=_extract_example_ids(lines),
    )


def load_spec_dir(spec_dir: Path = SPEC_DIR) -> list[SpecFile]:
    return [load_spec_file(path) for path in sorted(spec_dir.glob("*.spec.md"))]


def check_criterion_length(spec_file: SpecFile) -> list[Finding]:
    return [
        Finding(
            Rule.CRITERION_LENGTH,
            spec_file.name,
            c.line,
            f"[{c.spec_id}] is {len(c.text)} chars from the id to line end, "
            f"> {MAX_CRITERION_LENGTH}",
        )
        for c in spec_file.criteria
        if len(c.text) > MAX_CRITERION_LENGTH
    ]


def check_id_sequence(spec_file: SpecFile) -> list[Finding]:
    findings: list[Finding] = []
    by_prefix: dict[str, list[Criterion]] = defaultdict(list)
    for criterion in spec_file.criteria:
        prefix, _, _ = criterion.spec_id.rpartition("-")
        by_prefix[prefix].append(criterion)
    if len(by_prefix) > 1:
        prefixes = ", ".join(sorted(by_prefix))
        findings.append(
            Finding(Rule.ID_SEQUENCE, spec_file.name, 1, f"more than one ID prefix: {prefixes}")
        )
    retired_by_prefix: dict[str, set[int]] = defaultdict(set)
    for retired_id in spec_file.retired_ids:
        prefix, _, number = retired_id.rpartition("-")
        retired_by_prefix[prefix].add(int(number))
    for prefix, criteria in by_prefix.items():
        seen: dict[int, Criterion] = {}
        width = len(criteria[0].spec_id.rpartition("-")[2])
        for criterion in criteria:
            number = int(criterion.spec_id.rpartition("-")[2])
            earlier = seen.get(number)
            if earlier is not None:
                findings.append(
                    Finding(
                        Rule.ID_SEQUENCE,
                        spec_file.name,
                        criterion.line,
                        f"{criterion.spec_id} duplicates {earlier.spec_id} at line {earlier.line}",
                    )
                )
                continue
            seen[number] = criterion
        used = sorted(seen)
        retired = retired_by_prefix[prefix]
        for gap in range(used[0], used[-1] + 1):
            if gap in seen or gap in retired:
                continue
            findings.append(
                Finding(
                    Rule.ID_SEQUENCE,
                    spec_file.name,
                    1,
                    f"{prefix}-{gap:0{width}d} is missing between "
                    f"{prefix}-{used[0]:0{width}d} and {prefix}-{used[-1]:0{width}d}, "
                    "and no retirement note names it",
                )
            )
    return findings


def _split_reference_id(reference_id: str) -> tuple[str, str]:
    prefix, _, number = reference_id.rpartition("-")
    return prefix, number


def _expand_range(expression: str) -> list[str]:
    """``A-01..A-99``/``A-01..99`` -- every id from the start to the end number, inclusive."""
    start, _, end = expression.partition("..")
    prefix, start_number = _split_reference_id(start)
    end_number = _split_reference_id(end)[1] if "-" in end else end
    width = len(start_number)
    return [
        f"{prefix}-{number:0{width}d}" for number in range(int(start_number), int(end_number) + 1)
    ]


def _expand_slash(expression: str) -> list[str]:
    """``A-01/99`` -- the given id plus one further id per ``/``-separated number, same prefix."""
    first, *further_numbers = expression.split("/")
    prefix, first_number = _split_reference_id(first)
    width = len(first_number)
    return [first] + [f"{prefix}-{int(number):0{width}d}" for number in further_numbers]


def _expand_reference_expression(expression: str) -> list[str]:
    """One comma-separated ``see`` expression -- a plain id, a ``..`` range, or a ``/``
    shorthand -- expanded to every id it names."""
    if ".." in expression:
        return _expand_range(expression)
    if "/" in expression:
        return _expand_slash(expression)
    return [expression]


def _check_see_references(
    spec_file: SpecFile, criterion_ids: frozenset[str], example_ids: frozenset[str]
) -> list[Finding]:
    findings: list[Finding] = []
    for lineno, line in enumerate(spec_file.lines, 1):
        for keyword in SEE_KEYWORD_PATTERN.finditer(line):
            reference_list = REFERENCE_LIST_PATTERN.match(line, keyword.end())
            if reference_list is None:
                continue
            for expression in reference_list.group(0).split(","):
                for token in _expand_reference_expression(expression.strip()):
                    is_example = EXAMPLE_TOKEN_PATTERN.fullmatch(token) is not None
                    known_ids = example_ids if is_example else criterion_ids
                    if token in known_ids:
                        continue
                    owner = f"`### {token}` heading" if is_example else f"`[{token}]` criterion"
                    findings.append(
                        Finding(
                            Rule.UNRESOLVED_REFERENCE,
                            spec_file.name,
                            lineno,
                            f"see {token} names no {owner} anywhere in specs/",
                        )
                    )
    return findings


def _behavior_table_rows(lines: tuple[str, ...]) -> list[tuple[int, str]]:
    """Data rows of the file's own ``## Behavior table`` -- its header and separator dropped."""
    heading = next(
        (i for i, line in enumerate(lines) if BEHAVIOR_TABLE_HEADING_PATTERN.match(line)), None
    )
    if heading is None:
        return []
    # A blank line separates the heading from its table, matching every real spec's own layout.
    start = heading + 1
    while start < len(lines) and lines[start].strip() == "":
        start += 1
    rows: list[tuple[int, str]] = []
    seen_separator = False
    for i in range(start, len(lines)):
        line = lines[i]
        if not line.strip().startswith("|"):
            break
        if not seen_separator:
            # The header row precedes the ``|---|---|`` separator; both are prose, not cells.
            if TABLE_SEPARATOR_PATTERN.match(line.strip()):
                seen_separator = True
            continue
        rows.append((i + 1, line))
    return rows


def _check_behavior_table(spec_file: SpecFile, criterion_ids: frozenset[str]) -> list[Finding]:
    findings: list[Finding] = []
    for lineno, row in _behavior_table_rows(spec_file.lines):
        # The first column is the row's own state/trigger label -- prose, never a citation.
        data_cells = row.strip().strip("|").split("|")[1:]
        for cell in data_cells:
            for token in ID_TOKEN_PATTERN.findall(cell):
                if token not in criterion_ids:
                    findings.append(
                        Finding(
                            Rule.UNRESOLVED_REFERENCE,
                            spec_file.name,
                            lineno,
                            f"behaviour-table cell names {token}, which no `[{token}]` "
                            "criterion anywhere in specs/ defines",
                        )
                    )
    return findings


def check_references(
    spec_file: SpecFile, criterion_ids: frozenset[str], example_ids: frozenset[str]
) -> list[Finding]:
    return _check_see_references(spec_file, criterion_ids, example_ids) + _check_behavior_table(
        spec_file, criterion_ids
    )


def check_required_sections(spec_file: SpecFile) -> list[Finding]:
    return [
        Finding(Rule.MISSING_SECTION, spec_file.name, 1, f"no `{label}` section")
        for label, pattern in REQUIRED_SECTIONS
        if not any(pattern.match(line) for line in spec_file.lines)
    ]


def _find_lowercase_snake_identifier(text: str) -> re.Match[str] | None:
    return next(
        (
            match
            for match in _IDENTIFIER_PATTERN.finditer(text)
            if _LOWERCASE_SNAKE_UNDERSCORE_PATTERN.search(match.group(0))
        ),
        None,
    )


def _find_dotted_attribute(text: str) -> re.Match[str] | None:
    return next(
        (
            match
            for match in DOTTED_ATTRIBUTE_PATTERN.finditer(text)
            if match.group(0).rpartition(".")[2] not in CONTRACT_FILE_EXTENSIONS
        ),
        None,
    )


def check_implementation_names(spec_file: SpecFile) -> list[Finding]:
    findings: list[Finding] = []
    for criterion in spec_file.criteria:
        stripped = BACKTICK_LITERAL_PATTERN.sub(" ", criterion.text)
        match = (
            PYTHON_FILENAME_PATTERN.search(stripped)
            or IMPLEMENTATION_CALL_PATTERN.search(stripped)
            or _find_lowercase_snake_identifier(stripped)
            or _find_dotted_attribute(stripped)
        )
        if match is not None:
            findings.append(
                Finding(
                    Rule.IMPLEMENTATION_NAME,
                    spec_file.name,
                    criterion.line,
                    f"[{criterion.spec_id}] names `{match.group(0)}` outside a backtick literal",
                )
            )
    return findings


def check_example_setup(spec_file: SpecFile) -> list[Finding]:
    findings: list[Finding] = []
    lines = spec_file.lines
    for i, line in enumerate(lines):
        heading = EXAMPLE_HEADING_PATTERN.match(line)
        if heading is None:
            continue
        found = False
        for other in lines[i + 1 :]:
            if other.startswith("#") or other.strip().startswith("```"):
                break
            if SETUP_LINE_PATTERN.match(other.strip()):
                found = True
                break
        if not found:
            findings.append(
                Finding(
                    Rule.MISSING_SETUP,
                    spec_file.name,
                    i + 1,
                    f"{heading.group(1)} has no `Setup:` line before its first fenced block",
                )
            )
    return findings


def _finding_sort_key(finding: Finding) -> tuple[str, int, str]:
    return (finding.file, finding.line, finding.rule.value)


def analyze(spec_files: list[SpecFile]) -> list[Finding]:
    criterion_ids = frozenset(c.spec_id for f in spec_files for c in f.criteria)
    example_ids = frozenset(eid for f in spec_files for eid in f.example_ids)
    findings: list[Finding] = []
    for spec_file in spec_files:
        findings.extend(check_criterion_length(spec_file))
        findings.extend(check_id_sequence(spec_file))
        findings.extend(check_references(spec_file, criterion_ids, example_ids))
        findings.extend(check_required_sections(spec_file))
        findings.extend(check_implementation_names(spec_file))
        findings.extend(check_example_setup(spec_file))
    findings.sort(key=_finding_sort_key)
    return findings


def load_exemptions(path: Path = EXEMPTIONS_FILE) -> tuple[dict[str, str], list[Finding]]:
    """``({"file:line:rule": reason}, malformed-line findings)`` from *path* (one line per
    exemption, ``key — reason``). A line missing the ` — reason`` tail cannot be parsed into a
    key at all, so it is reported as its own ``malformed_exemption`` finding instead of being
    silently dropped or silently accepted -- there is no ledger for the ledger, so it always
    fails ``--ci``."""
    if not path.exists():
        return {}, []
    exemptions: dict[str, str] = {}
    malformed: list[Finding] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, reason = stripped.partition(" — ")
        if not separator or not reason.strip():
            malformed.append(
                Finding(
                    Rule.MALFORMED_EXEMPTION,
                    path.name,
                    lineno,
                    f"{stripped!r} has no ` — <reason>` after its key",
                )
            )
            continue
        exemptions[key.strip()] = reason.strip()
    return exemptions, malformed


def unexempted(findings: list[Finding], exemptions: dict[str, str]) -> list[Finding]:
    # A malformed_exemption finding is never exempt: exempting a ledger line requires trusting
    # the ledger, and a malformed line is exactly the ledger failing that trust -- there is no
    # ledger for the ledger.
    return [
        f
        for f in findings
        if f.rule is Rule.MALFORMED_EXEMPTION or f.exemption_key not in exemptions
    ]


def _print_report(findings: list[Finding]) -> None:
    if not findings:
        print("spec_lint: no findings")
        return
    by_rule: dict[Rule, list[Finding]] = defaultdict(list)
    for finding in findings:
        by_rule[finding.rule].append(finding)
    for rule in Rule:
        rule_findings = by_rule.get(rule, [])
        if not rule_findings:
            continue
        print(f"\n{rule.value} ({len(rule_findings)} finding(s))")
        for finding in rule_findings:
            print(f"  {finding.location}: {finding.detail}")


def _to_json(findings: list[Finding], failing: list[Finding]) -> dict[str, object]:
    return {
        "findings": [
            {
                "rule": f.rule.value,
                "file": f.file,
                "line": f.line,
                "detail": f.detail,
            }
            for f in findings
        ],
        "failing": [f.exemption_key for f in failing],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mechanical style lint for specs/*.spec.md")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--ci", action="store_true", help="exit non-zero on an unexempted finding")
    args = parser.parse_args(argv)

    exemptions, malformed_exemptions = load_exemptions(EXEMPTIONS_FILE)
    all_findings = analyze(load_spec_dir(SPEC_DIR)) + malformed_exemptions
    findings = sorted(all_findings, key=_finding_sort_key)
    failing = unexempted(findings, exemptions)

    if args.json:
        print(json.dumps(_to_json(findings, failing), indent=2, sort_keys=True))
    else:
        _print_report(findings)
        if failing:
            print(f"\nFAIL: {len(failing)} unexempted finding(s)", file=sys.stderr)

    if not args.ci:
        return 0
    return 1 if failing else 0


if __name__ == "__main__":
    raise SystemExit(main())
