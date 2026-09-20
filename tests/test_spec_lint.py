"""Behavioral tests for `scripts/spec_lint.py` (issue #385).

Each parametrized case drives `analyze` -- the module's own real entry point for one
rule -- against a minimal fixture spec built from `GREEN_SPEC` by flipping exactly the
one detail that rule polices; the fixture stays green everywhere else, so a red case
proves that rule alone fired. A last, unparametrized test drives the same `analyze`
over the repository's own `specs/` tree: every one of its 23 files must already be
clean under this gate.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

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


def _analyze_text(tmp_path: Path, text: str) -> list[Any]:
    fixture = tmp_path / "sample.spec.md"
    fixture.write_text(text, encoding="utf-8")
    return analyze([load_spec_file(fixture)])


def test_green_fixture_has_no_findings(tmp_path: Path) -> None:
    assert _analyze_text(tmp_path, GREEN_SPEC) == []


def test_unresolved_behavior_table_cell_is_caught(tmp_path: Path) -> None:
    """A blank line always separates ``## Behavior table`` from its rows in real specs; the
    row scan must not stop there before ever reaching a cell (regression for issue #385)."""
    red_text = GREEN_SPEC.replace("| default | SAMPLE-01 |", "| default | SAMPLE-99 |")
    assert red_text != GREEN_SPEC

    findings = _analyze_text(tmp_path, red_text)

    assert {f.rule.value for f in findings} == {"unresolved_reference"}
    assert any("SAMPLE-99" in f.detail for f in findings)


@pytest.mark.parametrize(
    ("rule", "red_text", "detail_substring"),
    [
        pytest.param(
            "criterion_length",
            RED_CRITERION_LENGTH,
            "> 200",
            id="criterion_length",
        ),
        pytest.param(
            "id_sequence",
            RED_ID_SEQUENCE,
            "SAMPLE-02 is missing",
            id="id_sequence",
        ),
        pytest.param(
            "unresolved_reference",
            RED_UNRESOLVED_REFERENCE,
            "E-SAMPLE-99",
            id="unresolved_reference",
        ),
        pytest.param(
            "missing_section",
            RED_MISSING_SECTION,
            "no `## Never` section",
            id="missing_section",
        ),
        pytest.param(
            "implementation_name",
            RED_IMPLEMENTATION_NAME,
            "render_output()",
            id="implementation_name",
        ),
        pytest.param(
            "missing_setup",
            RED_MISSING_SETUP,
            "no `Setup:` line",
            id="missing_setup",
        ),
    ],
)
def test_red_fixture_is_caught_by_its_own_rule_only(
    tmp_path: Path, rule: str, red_text: str, detail_substring: str
) -> None:
    assert red_text != GREEN_SPEC, "fixture must actually differ from the green baseline"

    findings = _analyze_text(tmp_path, red_text)

    assert {f.rule.value for f in findings} == {rule}
    assert any(detail_substring in f.detail for f in findings)
    assert all(f.file == "sample.spec.md" for f in findings)


def test_repository_specs_are_clean_under_the_gate() -> None:
    findings = analyze(load_spec_dir(_REPOSITORY_SPEC_DIR))

    assert findings == []
