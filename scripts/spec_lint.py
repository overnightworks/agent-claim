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
* ``unresolved_reference``: a ``(see ...)`` parenthetical, or a behaviour-table data cell (its
  row-label column is prose, not a citation, and is never scanned), names an ``E-<PREFIX>-nn``
  that no ``###`` heading defines, or a plain ``ID-nn`` that no ``- [ ] [ID-nn]`` bullet
  anywhere in ``specs/`` defines -- criteria and examples cite across files routinely, so both
  namespaces are read tree-wide, not per file.
* ``missing_section``: the file has no ``## Behavior table``, no ``## Never``, or no
  ``## Examples`` heading.
* ``implementation_name``: a criterion's text, with every backtick-quoted span removed first,
  still names a Python-shaped identifier outside any literal -- ``an_identifier_like_this`` or
  ``a_call(...)``. A shell only ever observes a command, flag, path, or printed literal, and
  aco's house style already backticks every one of those; the calibration pass over all 23
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
none needed today.

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
SEE_REFERENCE_PATTERN = re.compile(r"\(see ([^)]+)\)")
EXAMPLE_TOKEN_PATTERN = re.compile(r"\bE-[A-Za-z0-9]+-\d+\b")
ID_TOKEN_PATTERN = re.compile(r"\b[A-Z]+-\d+\b")
BACKTICK_LITERAL_PATTERN = re.compile(r"`[^`]*`")
# SPEC-6, adapted: a shell never observes a bare Python identifier or call; command names,
# flags, paths and printed literals are always backticked in aco's house style, so anything
# matching this shape outside a backtick literal is an implementation name that leaked in.
IMPLEMENTATION_CALL_PATTERN = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\([^)]*\)")
IMPLEMENTATION_UNDERSCORE_PATTERN = re.compile(r"\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]*\b")
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


def _reference_tokens(content: str) -> tuple[list[str], list[str]]:
    """``(example ids, plain criterion ids)`` named by one ``(see ...)`` parenthetical."""
    example_tokens = EXAMPLE_TOKEN_PATTERN.findall(content)
    remainder = EXAMPLE_TOKEN_PATTERN.sub(" ", content)
    return example_tokens, ID_TOKEN_PATTERN.findall(remainder)


def _check_see_references(
    spec_file: SpecFile, criterion_ids: frozenset[str], example_ids: frozenset[str]
) -> list[Finding]:
    findings: list[Finding] = []
    for lineno, line in enumerate(spec_file.lines, 1):
        for match in SEE_REFERENCE_PATTERN.finditer(line):
            examples, criteria = _reference_tokens(match.group(1))
            for token in examples:
                if token not in example_ids:
                    findings.append(
                        Finding(
                            Rule.UNRESOLVED_REFERENCE,
                            spec_file.name,
                            lineno,
                            f"(see {token}) names no `### {token}` heading anywhere in specs/",
                        )
                    )
            for token in criteria:
                if token not in criterion_ids:
                    findings.append(
                        Finding(
                            Rule.UNRESOLVED_REFERENCE,
                            spec_file.name,
                            lineno,
                            f"(see {token}) names no `[{token}]` criterion anywhere in specs/",
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


def check_implementation_names(spec_file: SpecFile) -> list[Finding]:
    findings: list[Finding] = []
    for criterion in spec_file.criteria:
        stripped = BACKTICK_LITERAL_PATTERN.sub(" ", criterion.text)
        match = IMPLEMENTATION_CALL_PATTERN.search(
            stripped
        ) or IMPLEMENTATION_UNDERSCORE_PATTERN.search(stripped)
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
    findings.sort(key=lambda f: (f.file, f.line, f.rule.value))
    return findings


def load_exemptions(path: Path = EXEMPTIONS_FILE) -> dict[str, str]:
    """``{"file:line:rule": reason}`` from *path* (one per line, ``key — reason``)."""
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


def unexempted(findings: list[Finding], exemptions: dict[str, str]) -> list[Finding]:
    return [f for f in findings if f.exemption_key not in exemptions]


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

    findings = analyze(load_spec_dir(SPEC_DIR))
    exemptions = load_exemptions(EXEMPTIONS_FILE)
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
