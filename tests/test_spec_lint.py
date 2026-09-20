"""Behavioral tests for `scripts/spec_lint.py` (issue #385).

Each parametrized case drives `analyze` -- the module's own real entry point for one
rule -- against a minimal fixture spec built from `GREEN_SPEC` by flipping exactly the
one detail that rule polices; the fixture stays green everywhere else, so a red case
proves that rule alone fired, and every rule also carries a green case (`GREEN_SPEC`
itself) proving it stays silent on clean text. A last, unparametrized test drives the
same `analyze` over the repository's own `specs/` tree: every one of its 23 files must
already be clean under this gate.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, NamedTuple

import pytest

_MODULE_PATH = Path(__file__).parent.parent / "scripts" / "spec_lint.py"
_REPOSITORY_SPEC_DIR = Path(__file__).parent.parent / "specs"


def _load_spec_lint() -> ModuleType:
    spec = importlib.util.spec_from_file_location("spec_lint", _MODULE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: the module's own frozen dataclasses resolve their
    # annotations (`from __future__ import annotations`) against `sys.modules`
    # while the class body executes, which needs the entry to already exist.
    sys.modules["spec_lint"] = module
    spec.loader.exec_module(module)
    return module


spec_lint = _load_spec_lint()
analyze = spec_lint.analyze
load_spec_file = spec_lint.load_spec_file
load_spec_dir = spec_lint.load_spec_dir

GREEN_SPEC = """# Sample

A minimal fixture spec for spec_lint's own tests.

## Behavior table

| state \\ trigger | `sample` |
|---|---|
| default | SAMPLE-01 |

- [ ] [SAMPLE-01] `sample` prints `ok`, exit `0` (see E-SAMPLE-01).

## Never

- `sample` never prints twice.

## Examples

### E-SAMPLE-01 -- the default case

Setup: bare-remote

```console
$ sample
ok
exit 0
```
"""


def _line_number(text: str, needle: str) -> int:
    """The 1-based line number of the first line of *text* containing *needle*."""
    for lineno, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return lineno
    raise AssertionError(f"{needle!r} not found in fixture text")


_CRITERION_LINE_TEXT = "[SAMPLE-01] `sample` prints `ok`, exit `0` (see E-SAMPLE-01)."
CRITERION_LINE = _line_number(GREEN_SPEC, _CRITERION_LINE_TEXT)
EXAMPLE_HEADING_LINE = _line_number(GREEN_SPEC, "### E-SAMPLE-01")

_LONG_CRITERION = "`sample` prints `ok`, exit `0` (see E-SAMPLE-01), " + "x" * 200 + "."
RED_CRITERION_LENGTH = GREEN_SPEC.replace(
    "`sample` prints `ok`, exit `0` (see E-SAMPLE-01).", _LONG_CRITERION
)
RED_ID_SEQUENCE = GREEN_SPEC.replace(
    "- [ ] [SAMPLE-01] `sample` prints `ok`, exit `0` (see E-SAMPLE-01).",
    "- [ ] [SAMPLE-01] `sample` prints `ok`, exit `0` (see E-SAMPLE-01).\n"
    "- [ ] [SAMPLE-03] `sample --other` prints `ok`, exit `0`.",
)
RED_UNRESOLVED_REFERENCE = GREEN_SPEC.replace("(see E-SAMPLE-01)", "(see E-SAMPLE-99)")
RED_MISSING_SECTION = GREEN_SPEC.replace("## Never\n\n- `sample` never prints twice.\n\n", "")
RED_IMPLEMENTATION_NAME = GREEN_SPEC.replace(
    "`sample` prints `ok`, exit `0` (see E-SAMPLE-01).",
    "`sample` calls render_output() and prints `ok`, exit `0` (see E-SAMPLE-01).",
)
RED_MISSING_SETUP = GREEN_SPEC.replace("Setup: bare-remote\n\n", "")

# `see` reference shorthand (issue #385 review): a keyword anywhere in the line, a `/`
# shorthand sharing a prefix, a `..` range, and a comma list, each proven against the
# criterion's own valid `SAMPLE-01` alongside one deliberately unresolved id.
RED_SEE_ANYWHERE_IN_LINE = GREEN_SPEC.replace("(see E-SAMPLE-01)", "(context; see E-SAMPLE-99)")
RED_SEE_SLASH_SHORTHAND = GREEN_SPEC.replace("(see E-SAMPLE-01)", "(see SAMPLE-01/99)")
RED_SEE_RANGE_SHORTHAND = GREEN_SPEC.replace("(see E-SAMPLE-01)", "(see SAMPLE-01..03)")
RED_SEE_COMMA_LIST = GREEN_SPEC.replace("(see E-SAMPLE-01)", "(see E-SAMPLE-01, E-SAMPLE-02)")


def _analyze_text(tmp_path: Path, text: str) -> list[Any]:
    fixture = tmp_path / "sample.spec.md"
    fixture.write_text(text, encoding="utf-8")
    return analyze([load_spec_file(fixture)])


def test_unresolved_behavior_table_cell_is_caught(tmp_path: Path) -> None:
    """A blank line always separates ``## Behavior table`` from its rows in real specs; the
    row scan must not stop there before ever reaching a cell (regression for issue #385)."""
    red_text = GREEN_SPEC.replace("| default | SAMPLE-01 |", "| default | SAMPLE-99 |")
    assert red_text != GREEN_SPEC
    expected_line = _line_number(red_text, "| default | SAMPLE-99 |")

    findings = _analyze_text(tmp_path, red_text)

    assert [(f.rule.value, f.line) for f in findings] == [("unresolved_reference", expected_line)]
    assert "SAMPLE-99" in findings[0].detail


class _ExpectedFinding(NamedTuple):
    rule: str
    line: int
    detail_substring: str


RULE_FAMILY_CASES: tuple[tuple[str, str, tuple[_ExpectedFinding, ...]], ...] = (
    (
        "criterion_length:red",
        RED_CRITERION_LENGTH,
        (_ExpectedFinding("criterion_length", CRITERION_LINE, "> 200"),),
    ),
    ("criterion_length:green", GREEN_SPEC, ()),
    (
        "id_sequence:red",
        RED_ID_SEQUENCE,
        (_ExpectedFinding("id_sequence", 1, "SAMPLE-02 is missing"),),
    ),
    ("id_sequence:green", GREEN_SPEC, ()),
    (
        "unresolved_reference:red",
        RED_UNRESOLVED_REFERENCE,
        (_ExpectedFinding("unresolved_reference", CRITERION_LINE, "E-SAMPLE-99"),),
    ),
    ("unresolved_reference:green", GREEN_SPEC, ()),
    (
        "missing_section:red",
        RED_MISSING_SECTION,
        (_ExpectedFinding("missing_section", 1, "no `## Never` section"),),
    ),
    ("missing_section:green", GREEN_SPEC, ()),
    (
        "implementation_name:red",
        RED_IMPLEMENTATION_NAME,
        (_ExpectedFinding("implementation_name", CRITERION_LINE, "render_output()"),),
    ),
    ("implementation_name:green", GREEN_SPEC, ()),
    (
        "missing_setup:red",
        RED_MISSING_SETUP,
        (_ExpectedFinding("missing_setup", EXAMPLE_HEADING_LINE, "no `Setup:` line"),),
    ),
    ("missing_setup:green", GREEN_SPEC, ()),
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [pytest.param(text, expected, id=case_id) for case_id, text, expected in RULE_FAMILY_CASES],
)
def test_rule_family_red_and_green_cases(
    tmp_path: Path, text: str, expected: tuple[_ExpectedFinding, ...]
) -> None:
    findings = _analyze_text(tmp_path, text)

    assert [(f.rule.value, f.line) for f in findings] == [(e.rule, e.line) for e in expected]
    for finding, expectation in zip(findings, expected, strict=True):
        assert expectation.detail_substring in finding.detail
        assert finding.file == "sample.spec.md"


def _criterion_with_snippet(snippet: str) -> str:
    return GREEN_SPEC.replace(
        "`sample` prints `ok`, exit `0` (see E-SAMPLE-01).",
        f"`sample` prints `ok`, mentions {snippet}, exit `0` (see E-SAMPLE-01).",
    )


# The `implementation_name` detector table from the issue #385 review: seven bare
# implementation shapes that must be flagged, and five backtick-quoted house-style
# literals that must not be.
DETECTOR_TABLE: tuple[tuple[str, str, bool], ...] = (
    ("leading_underscore_snake_case", "_git_run", True),
    ("python_file_name", "checkout.py", True),
    ("call_with_parens", "hook_command_paths()", True),
    ("dotted_attribute_upper_snake", "HookToolEffect.COMMAND_TEXT", True),
    ("plain_snake_case", "load_brief_config", True),
    ("dotted_attribute_lower_snake", "store.claim_lifecycle", True),
    ("long_snake_case", "test_run_starts_the_session", True),
    ("backticked_command_and_flag", "`aco claim --scope`", False),
    ("backticked_contract_file", "`board.toml`", False),
    ("backticked_ref_name", "`refs/aco/state`", False),
    ("backticked_json_flag", "`--json`", False),
    ("backticked_trailer_key", "`Work-Item:`", False),
)


@pytest.mark.parametrize(
    ("snippet", "expected_flagged"),
    [pytest.param(snippet, flagged, id=case_id) for case_id, snippet, flagged in DETECTOR_TABLE],
)
def test_implementation_name_detector_table(
    tmp_path: Path, snippet: str, expected_flagged: bool
) -> None:
    findings = _analyze_text(tmp_path, _criterion_with_snippet(snippet))

    flagged = any(f.rule.value == "implementation_name" for f in findings)

    assert flagged is expected_flagged


REFERENCE_SHORTHAND_CASES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("see_anywhere_in_line_not_only_after_paren", RED_SEE_ANYWHERE_IN_LINE, ("E-SAMPLE-99",)),
    ("see_slash_shorthand_shares_prefix", RED_SEE_SLASH_SHORTHAND, ("SAMPLE-99",)),
    ("see_range_shorthand_expands_every_id", RED_SEE_RANGE_SHORTHAND, ("SAMPLE-02", "SAMPLE-03")),
    ("see_comma_list_checks_each_id", RED_SEE_COMMA_LIST, ("E-SAMPLE-02",)),
)


@pytest.mark.parametrize(
    ("text", "expected_details"),
    [
        pytest.param(text, details, id=case_id)
        for case_id, text, details in REFERENCE_SHORTHAND_CASES
    ],
)
def test_see_reference_shorthand_is_resolved(
    tmp_path: Path, text: str, expected_details: tuple[str, ...]
) -> None:
    findings = _analyze_text(tmp_path, text)

    assert {f.rule.value for f in findings} == {"unresolved_reference"}
    assert len(findings) == len(expected_details)
    assert all(f.line == CRITERION_LINE for f in findings)
    for expected_detail in expected_details:
        assert any(expected_detail in f.detail for f in findings)


def test_well_formed_exemption_line_parses(tmp_path: Path) -> None:
    ledger = tmp_path / "exemptions.txt"
    ledger.write_text("sample.spec.md:1:criterion_length — accepted for now\n", encoding="utf-8")

    exemptions, malformed = spec_lint.load_exemptions(ledger)

    assert exemptions == {"sample.spec.md:1:criterion_length": "accepted for now"}
    assert malformed == []


def test_exemption_line_without_a_reason_is_malformed(tmp_path: Path) -> None:
    ledger = tmp_path / "exemptions.txt"
    ledger.write_text("sample.spec.md:1:criterion_length\n", encoding="utf-8")

    exemptions, malformed = spec_lint.load_exemptions(ledger)

    assert exemptions == {}
    assert [(f.rule.value, f.line) for f in malformed] == [("malformed_exemption", 1)]


def test_malformed_exemption_line_cannot_exempt_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ledger line naming a malformed_exemption finding as accepted must not suppress that
    finding: trusting the exemption requires trusting the ledger, which a malformed line is
    exactly failing to do (regression for issue #385 review round 2)."""
    spec_dir = tmp_path / "specs"
    spec_dir.mkdir()
    (spec_dir / "sample.spec.md").write_text(GREEN_SPEC, encoding="utf-8")
    ledger = tmp_path / "exemptions.txt"
    ledger.write_text(
        "exemptions.txt:2:malformed_exemption — suppress it\nmalformed\n", encoding="utf-8"
    )
    monkeypatch.setattr(spec_lint, "SPEC_DIR", spec_dir)
    monkeypatch.setattr(spec_lint, "EXEMPTIONS_FILE", ledger)

    exit_code = spec_lint.main(["--ci", "--json"])
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert report["failing"] == ["exemptions.txt:2:malformed_exemption"]


def test_repository_specs_are_clean_under_the_gate() -> None:
    findings = analyze(load_spec_dir(_REPOSITORY_SPEC_DIR))

    assert findings == []
