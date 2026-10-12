# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
#!/usr/bin/env python
"""Regression checks for narrator lineup/bullpen cell parsing."""

import csv
import importlib.util
import warnings
from pathlib import Path

from _mlb_name_list import (
    get_parse_diagnostics,
    parse_name_list,
    reset_parse_diagnostics,
)


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT / "src" / "narr_context.py"
)
SPEC = importlib.util.spec_from_file_location("narr_context_under_test", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def test_split_names_supports_python_list_repr():
    value = "['Pete Crow-Armstrong', 'Seiya Suzuki', \"Tyler O'Neill\"]"
    assert MODULE.split_names(value) == [
        "Pete Crow-Armstrong",
        "Seiya Suzuki",
        "Tyler O'Neill",
    ]


def test_split_names_supports_json_and_legacy_csv():
    assert MODULE.split_names('["Dylan Crews", "CJ Abrams"]') == [
        "Dylan Crews",
        "CJ Abrams",
    ]
    assert MODULE.split_names("Dylan Crews, CJ Abrams") == [
        "Dylan Crews",
        "CJ Abrams",
    ]


def test_split_names_reads_real_v3_csv_fixture():
    fixture = ROOT / "test_fixtures" / "mlb_v3_list_repr_lineup.csv"
    with fixture.open(encoding="utf-8", newline="") as handle:
        row = next(csv.DictReader(handle))
    home = MODULE.split_names(row["home_lineup_names"])
    away = MODULE.split_names(row["away_lineup_names"])
    assert len(home) == 9
    assert len(away) == 9
    assert home[0] == "Dylan Crews" and home[-1] == "Nasim Nuñez"
    assert away[0] == "Pete Crow-Armstrong" and away[-1] == "Dansby Swanson"
    assert all(not name.startswith("[") and not name.endswith("]") for name in home + away)


def test_malformed_bracketed_value_fails_closed_and_is_counted():
    reset_parse_diagnostics()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        names = parse_name_list("['Broken Name', 'Unclosed Name]")
    diagnostics = get_parse_diagnostics()
    assert names == []
    assert diagnostics["malformed_bracket_count"] == 1
    assert diagnostics["malformed_bracket_samples"] == ["['Broken Name', 'Unclosed Name]"]
    assert any("refusing delimiter fallback" in str(item.message) for item in caught)


def test_bullpen_asof_excludes_target_and_future_rows():
    stints = MODULE.pd.DataFrame(
        {
            "date": MODULE.pd.to_datetime(["2026-08-10", "2026-08-11", "2026-08-17"]),
            "pitcher": ["Prior Arm", "Prior Arm", "Future Arm"],
        }
    )
    availability, as_of = MODULE.bp_avail_map(stints, "2026-08-12")
    assert availability[MODULE.NORM("Prior Arm")] == {1, 2}
    assert MODULE.NORM("Future Arm") not in availability
    assert str(as_of)[:10] == "2026-08-11"


if __name__ == "__main__":
    test_split_names_supports_python_list_repr()
    test_split_names_supports_json_and_legacy_csv()
    test_split_names_reads_real_v3_csv_fixture()
    test_malformed_bracketed_value_fails_closed_and_is_counted()
    test_bullpen_asof_excludes_target_and_future_rows()
    print("PASS - narrator lineup parsing")
