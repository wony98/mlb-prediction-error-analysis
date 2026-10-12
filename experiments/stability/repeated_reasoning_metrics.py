# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
from __future__ import annotations

"""Compute preregistered K=5 self-consistency metrics for historical-note panel v2."""

import argparse
from collections import Counter
from itertools import combinations
import json
import math
from pathlib import Path
from typing import Any


MIN_STABLE_ITEM_RATIO = 0.80
MIN_PAIRWISE_EXACT = 0.80
MIN_FLEISS_KAPPA = 0.60
MIN_MODAL_STABILITY = 0.80
STAGE0_MIN_STABLE_ITEM_RATIO = 0.50
STAGE0_MIN_PAIRWISE_EXACT = 0.60
EXPECTED_K = 5


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _load_output(run: dict[str, Any], root: Path) -> dict[str, Any]:
    if isinstance(run.get("output"), dict):
        return run["output"]
    path = Path(str(run.get("output_path") or ""))
    if not path.is_absolute():
        path = root / path
    if not path.is_file():
        raise FileNotFoundError(path)
    value = read_json(path)
    if not isinstance(value, dict):
        raise ValueError(f"output must be an object: {path}")
    return value


def _label(output: dict[str, Any], stage: str, axis: str, dimension: str) -> str:
    row = (output.get("axes") or {}).get(axis) or {}
    if stage == "L1":
        if dimension == "direction":
            return str(row["forced_lean_direction"])
        if dimension == "threshold":
            return "MET" if row["selection_threshold_met"] is True else "NOT_MET"
        if dimension == "combined":
            return f"{row['forced_lean_direction']}|{int(row['selection_threshold_met'] is True)}"
    elif stage == "L2":
        if dimension == "mechanism":
            return str(row["outcome_mechanism"])
        if dimension == "realized_direction":
            return str(row["realized_route_direction"])
        if dimension == "combined":
            return f"{row['outcome_mechanism']}|{row['realized_route_direction']}"
    elif stage == "ARM":
        if dimension == "direction":
            return str(row["forced_lean_direction"])
        if dimension == "threshold":
            return "MET" if row["selection_threshold_met"] is True else "NOT_MET"
        if dimension == "decision":
            return str(row["final_decision"])
        if dimension == "combined":
            return (
                f"{row['forced_lean_direction']}|"
                f"{int(row['selection_threshold_met'] is True)}|{row['final_decision']}"
            )
    raise ValueError(f"unsupported label: stage={stage}, dimension={dimension}")


def _pairwise_exact(labels: list[str]) -> float:
    pairs = list(combinations(labels, 2))
    return sum(left == right for left, right in pairs) / len(pairs)


def _normalized_entropy(labels: list[str]) -> float:
    counts = Counter(labels)
    if len(counts) <= 1:
        return 0.0
    total = len(labels)
    entropy = -sum((count / total) * math.log(count / total) for count in counts.values())
    return entropy / math.log(len(counts))


def _fleiss_kappa(label_sets: list[list[str]]) -> float | None:
    if not label_sets:
        raise ValueError("no label sets")
    k = len(label_sets[0])
    if k < 2 or any(len(labels) != k for labels in label_sets):
        raise ValueError("Fleiss kappa requires a fixed K >= 2")
    categories = sorted({label for labels in label_sets for label in labels})
    counts = [Counter(labels) for labels in label_sets]
    observed = sum(
        (sum(count[value] ** 2 for value in categories) - k) / (k * (k - 1))
        for count in counts
    ) / len(counts)
    denominator = len(counts) * k
    proportions = {
        value: sum(count[value] for count in counts) / denominator for value in categories
    }
    expected = sum(value * value for value in proportions.values())
    if math.isclose(expected, 1.0):
        return None
    return (observed - expected) / (1.0 - expected)


def _dimension_report(units: list[dict[str, Any]]) -> dict[str, Any]:
    label_sets = [unit["labels"] for unit in units]
    item_rows = []
    marginal = Counter()
    for unit in units:
        labels = unit["labels"]
        counts = Counter(labels)
        marginal.update(labels)
        modal_label, modal_count = sorted(
            counts.items(), key=lambda item: (-item[1], item[0])
        )[0]
        item_rows.append(
            {
                "gamePk": unit["gamePk"],
                "selection_rank": unit["selection_rank"],
                "axis": unit["axis"],
                "labels": labels,
                "modal_label": modal_label,
                "modal_stability": modal_count / len(labels),
                "pairwise_exact_agreement": _pairwise_exact(labels),
                "normalized_entropy": _normalized_entropy(labels),
                "stable": modal_count / len(labels) >= MIN_MODAL_STABILITY,
            }
        )
    total_labels = sum(marginal.values())
    stable_ratio = sum(row["stable"] for row in item_rows) / len(item_rows)
    pairwise = sum(row["pairwise_exact_agreement"] for row in item_rows) / len(item_rows)
    kappa = _fleiss_kappa(label_sets)
    gate = (
        stable_ratio >= MIN_STABLE_ITEM_RATIO
        and pairwise >= MIN_PAIRWISE_EXACT
        and kappa is not None
        and kappa >= MIN_FLEISS_KAPPA
    )
    return {
        "item_count": len(item_rows),
        "k": len(label_sets[0]),
        "stable_item_ratio": stable_ratio,
        "mean_pairwise_exact_agreement": pairwise,
        "fleiss_kappa": kappa,
        "marginal_distribution": {
            key: {"count": marginal[key], "share": marginal[key] / total_labels}
            for key in sorted(marginal)
        },
        "gate": {
            "min_stable_item_ratio": MIN_STABLE_ITEM_RATIO,
            "min_pairwise_exact_agreement": MIN_PAIRWISE_EXACT,
            "min_fleiss_kappa": MIN_FLEISS_KAPPA,
            "pass": gate,
        },
        "items": item_rows,
    }


def _dimensions(stage: str) -> tuple[str, ...]:
    if stage == "L1":
        return ("direction", "threshold", "combined")
    if stage == "L2":
        return ("mechanism", "realized_direction", "combined")
    if stage == "ARM":
        return ("direction", "threshold", "decision", "combined")
    raise ValueError(f"unsupported stage: {stage}")


def calculate(payload: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    root = root or Path.cwd()
    stage = str(payload.get("stage") or "").upper()
    items = payload.get("items") or []
    if not items:
        raise ValueError("items are required")
    expected_ranks = list(range(1, len(items) + 1))
    actual_ranks = sorted(int(item["selection_rank"]) for item in items)
    if actual_ranks != expected_ranks:
        raise ValueError(f"selection ranks must be contiguous: {actual_ranks}")
    dimensions = _dimensions(stage)
    units_by_dimension: dict[str, list[dict[str, Any]]] = {
        dimension: [] for dimension in dimensions
    }
    for item in items:
        runs = item.get("runs") or []
        if len(runs) != EXPECTED_K:
            raise ValueError(f"{item.get('gamePk')}: expected K={EXPECTED_K}")
        outputs = [_load_output(run, root) for run in runs]
        for axis in ("WIN_LOSS", "RUN_TOTAL"):
            for dimension in dimensions:
                units_by_dimension[dimension].append(
                    {
                        "gamePk": str(item["gamePk"]),
                        "selection_rank": int(item["selection_rank"]),
                        "axis": axis,
                        "labels": [
                            _label(output, stage, axis, dimension) for output in outputs
                        ],
                    }
                )

    primary = {
        dimension: _dimension_report(units)
        for dimension, units in units_by_dimension.items()
    }
    report: dict[str, Any] = {
        "schema": "mlb.t071.fixture_panel_v2.self_consistency_metrics.v1",
        "stage": stage,
        "game_count": len(items),
        "k": EXPECTED_K,
        "primary": primary,
    }
    if stage == "L1":
        stage0 = {
            dimension: _dimension_report(
                [unit for unit in units if unit["selection_rank"] <= 3]
            )
            for dimension, units in units_by_dimension.items()
        }
        stage0_futility = any(
            stage0[dimension]["stable_item_ratio"] < STAGE0_MIN_STABLE_ITEM_RATIO
            or stage0[dimension]["mean_pairwise_exact_agreement"]
            < STAGE0_MIN_PAIRWISE_EXACT
            for dimension in ("direction", "threshold")
        )
        report["stage0"] = {
            "descriptive_only": True,
            "selection_ranks": [1, 2, 3],
            "futility_thresholds": {
                "min_stable_item_ratio": STAGE0_MIN_STABLE_ITEM_RATIO,
                "min_pairwise_exact_agreement": STAGE0_MIN_PAIRWISE_EXACT,
            },
            "futility_stop": stage0_futility,
            "dimensions": stage0,
        }
        if len(items) == 10:
            sensitivity = {
                dimension: _dimension_report(
                    [unit for unit in units if unit["selection_rank"] >= 4]
                )
                for dimension, units in units_by_dimension.items()
            }
            primary_pass = all(
                primary[dimension]["gate"]["pass"]
                for dimension in ("direction", "threshold")
            )
            sensitivity_pass = all(
                sensitivity[dimension]["gate"]["pass"]
                for dimension in ("direction", "threshold")
            )
            report["sensitivity_7_game"] = {
                "selection_ranks": list(range(4, 11)),
                "diagnostic_only": True,
                "dimensions": sensitivity,
            }
            report["stage0_conditioning_sensitivity"] = bool(
                primary_pass and not sensitivity_pass
            )
            report["gate_pass"] = bool(
                primary_pass and not report["stage0_conditioning_sensitivity"]
            )
        else:
            report["gate_pass"] = False
            report["gate_status"] = "STAGE0_CANNOT_DECLARE_PASS"
    elif stage == "L2":
        report["gate_pass"] = bool(primary["mechanism"]["gate"]["pass"])
    else:
        report["gate_pass"] = all(
            primary[dimension]["gate"]["pass"]
            for dimension in ("direction", "threshold", "decision")
        )
    return report


def self_test() -> None:
    assert math.isclose(_pairwise_exact(["A"] * 5), 1.0)
    assert math.isclose(_pairwise_exact(["A"] * 4 + ["B"]), 0.6)
    assert math.isclose(_pairwise_exact(["A"] * 3 + ["B"] * 2), 0.4)
    assert math.isclose(_fleiss_kappa([["A"] * 5, ["B"] * 5]), 1.0)
    assert _fleiss_kappa([["A"] * 5, ["A"] * 5]) is None
    mixed = _dimension_report(
        [
            {"gamePk": "1", "selection_rank": 1, "axis": "WIN_LOSS", "labels": ["A"] * 5},
            {
                "gamePk": "2",
                "selection_rank": 2,
                "axis": "WIN_LOSS",
                "labels": ["A"] * 4 + ["B"],
            },
        ]
    )
    assert math.isclose(mixed["stable_item_ratio"], 1.0)
    assert math.isclose(mixed["mean_pairwise_exact_agreement"], 0.8)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        print("SELF_TEST_OK")
        return 0
    if not args.input:
        parser.error("--input is required unless --self-test is used")
    report = calculate(read_json(args.input), args.input.parent)
    if args.output:
        write_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
