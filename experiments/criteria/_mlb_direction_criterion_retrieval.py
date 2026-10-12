# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
from __future__ import annotations

"""Direction-Criterion Retrieval & Injection (DCRI) - deterministic core.

Research only. Loads the season direction-criterion bank (historical-note chunks), builds a
semantically-keyed index (source census refs -> stable semantic keys), enforces a
date partition (source_date < target date), rebinds criterion conditions onto a
target game's own pregame census, and aggregates applied directions.

The LLM condition-evaluation layer (layer 4) is intentionally NOT here - it will
be wired after cross-review. This module is the deterministic substrate:
  layer1 build-index / layer2 partition / layer3 rebind / layer5 aggregate.

Invariants (thread 기존 검토):
- retrieval never reads model predictions or outcomes for matching;
- source refs are never transplanted directly (positional IDs) - only semantic
  keys (comparison_axis / json-pointer path) are carried across games;
- a condition whose refs cannot be rebound on the target is UNKNOWN (fail-closed);
- conflicting applied directions aggregate to UNRESOLVED, never free-form choice;
- no production / narrator / Notification / automation contact.
"""

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Callable

import _mlb_direction_criterion_symmetric_v3 as v3

ROOT = Path(__file__).resolve().parent
BASE = ROOT / "postmortem" / "fullread_v4" / "direction_criterion_season_2026"
CHUNK_DIR = BASE / "chunks"
MANIFEST = BASE / "SEASON_SOURCE_MANIFEST.json"
OUT_DIR = BASE / "retrieval"
INDEX_PATH = OUT_DIR / "bank_index.json"

AXES = ("WIN_LOSS", "RUN_TOTAL")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def load_manifest_games() -> dict[str, dict[str, Any]]:
    manifest = _read_json(MANIFEST)
    games: dict[str, dict[str, Any]] = {}
    for g in manifest["games"]:
        gp = str(g["gamePk"])
        game = dict(g)
        # Season manifest source_paths lack frozen_pregame_packet; the per-game
        # shared_source dir (written by the season extractor) carries it.
        packet = BASE / "shared_source" / gp / "frozen_pregame_packet.json"
        if packet.exists():
            game["source_paths"] = {**(g.get("source_paths") or {}), "frozen_pregame_packet": str(packet)}
        games[gp] = game
    return games


def load_bank_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(CHUNK_DIR.glob("chunk_*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def census_semantic_map(game: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """census_id -> stable semantic key descriptor for one game (deterministic)."""
    census = v3.pregame_census(game)
    out: dict[str, dict[str, Any]] = {}
    for entry in census.get("entries") or []:
        cid = str(entry["census_id"])
        kind = str(entry.get("kind") or "")
        if kind == "axis_atomic":
            out[cid] = {"kind": "atom", "key": str(entry.get("ref"))}
        elif kind == "bullpen_summary":
            out[cid] = {"kind": "bpsum", "key": str(entry.get("ref"))}
        elif kind == "bullpen_player_card_group":
            path = entry.get("path") or []
            field = str(path[-1]) if path else ""
            out[cid] = {"kind": "bpcard", "key": f"{entry.get('side')}::{field}", "coarse": True}
        else:
            out[cid] = {"kind": "other", "key": str(entry.get("ref") or cid)}
    for comp in census.get("structural_comparisons") or []:
        out[str(comp["census_id"])] = {"kind": "pair", "key": str(comp.get("comparison_axis"))}
    return out


def target_reverse_map(game: dict[str, Any]) -> dict[str, list[str]]:
    """semantic key -> census_ids present on the TARGET game."""
    reverse: dict[str, list[str]] = {}
    for cid, desc in census_semantic_map(game).items():
        reverse.setdefault(f"{desc['kind']}::{desc['key']}", []).append(cid)
    return reverse


def census_evidence_map(
    game: dict[str, Any], packet: dict[str, Any] | None = None
) -> dict[str, dict[str, Any]]:
    """Stable semantic key -> target-local refs and pregame values."""
    census = v3.pregame_census(game, packet=packet)
    out: dict[str, dict[str, Any]] = {}
    for entry in census.get("entries") or []:
        cid = str(entry["census_id"])
        kind = str(entry.get("kind") or "")
        if kind == "axis_atomic":
            key = f"atom::{entry.get('ref')}"
        elif kind == "bullpen_summary":
            key = f"bpsum::{entry.get('ref')}"
        elif kind == "bullpen_player_card_group":
            path = entry.get("path") or []
            key = f"bpcard::{entry.get('side')}::{str(path[-1]) if path else ''}"
        else:
            key = f"other::{entry.get('ref') or cid}"
        value = entry.get("value")
        out[key] = {
            "kind": key.split("::", 1)[0],
            "semantic_key": key,
            "target_refs": [cid],
            "value": value,
        }
    for pair in census.get("structural_comparisons") or []:
        key = f"pair::{pair.get('comparison_axis')}"
        out[key] = {
            "kind": "pair",
            "semantic_key": key,
            "target_refs": [str(pair["census_id"])],
            "away_value": pair.get("away_value"),
            "home_value": pair.get("home_value"),
        }
    return out


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _sign(value: float, tolerance: float = 1e-12) -> int:
    if value > tolerance:
        return 1
    if value < -tolerance:
        return -1
    return 0


def _bool_value(value: Any) -> bool | None:
    token = str(value).strip().casefold()
    if token in {"true", "yes", "1", "on"}:
        return True
    if token in {"false", "no", "0", "off"}:
        return False
    return None


def compare_evidence_signature(
    semantic_key: str,
    source: dict[str, Any],
    target: dict[str, Any],
) -> dict[str, Any]:
    """Compare only direction-safe signatures; ambiguous values stay unscored."""
    if semantic_key.startswith("pair::"):
        source_away = _number(source.get("away_value"))
        source_home = _number(source.get("home_value"))
        target_away = _number(target.get("away_value"))
        target_home = _number(target.get("home_value"))
        if None in (source_away, source_home, target_away, target_home):
            return {"status": "unscored", "reason": "pair_non_numeric_or_missing"}
        source_relation = _sign(source_away - source_home)
        target_relation = _sign(target_away - target_home)
        if source_relation == 0 or target_relation == 0:
            return {
                "status": "unscored",
                "reason": "pair_relation_tied",
                "source_relation": source_relation,
                "target_relation": target_relation,
            }
        return {
            "status": "match" if source_relation == target_relation else "contradiction",
            "reason": "pair_relation",
            "source_relation": source_relation,
            "target_relation": target_relation,
        }

    if semantic_key.startswith("atom::") and semantic_key.casefold().endswith("_diff"):
        source_value = _number(source.get("value"))
        target_value = _number(target.get("value"))
        if source_value is None or target_value is None:
            return {"status": "unscored", "reason": "diff_non_numeric_or_missing"}
        source_relation = _sign(source_value)
        target_relation = _sign(target_value)
        if source_relation == 0 or target_relation == 0:
            return {
                "status": "unscored",
                "reason": "diff_sign_zero",
                "source_relation": source_relation,
                "target_relation": target_relation,
            }
        return {
            "status": "match" if source_relation == target_relation else "contradiction",
            "reason": "diff_sign",
            "source_relation": source_relation,
            "target_relation": target_relation,
        }

    source_bool = _bool_value(source.get("value"))
    target_bool = _bool_value(target.get("value"))
    if source_bool is not None and target_bool is not None:
        return {
            "status": "match" if source_bool == target_bool else "contradiction",
            "reason": "boolean_equality",
            "source_value": source_bool,
            "target_value": target_bool,
        }
    return {"status": "unscored", "reason": "requires_semantic_evaluation"}


def screen_condition(
    condition: dict[str, Any],
    source_evidence: dict[str, dict[str, Any]],
    target_evidence: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    checks = []
    matchup_role = str(condition.get("role_slot") or "").upper() == "MATCHUP"
    for key in condition.get("semantic_keys") or []:
        source = source_evidence.get(key)
        target = target_evidence.get(key)
        if source is None or target is None:
            checks.append({"semantic_key": key, "status": "missing"})
            continue
        comparison = compare_evidence_signature(key, source, target)
        if matchup_role and comparison.get("reason") in {"pair_relation", "diff_sign"}:
            comparison = {
                **comparison,
                "status": "unscored",
                "reason": "matchup_role_requires_level_semantics",
            }
        checks.append({
            "semantic_key": key,
            "target_refs": target.get("target_refs") or [],
            **comparison,
        })
    counts = Counter(check["status"] for check in checks)
    unresolved = counts["unscored"] + counts["missing"]
    # A lone comparable contradiction cannot erase a condition whose remaining
    # meaning still needs semantic evaluation. Reject only a complete single-key
    # contradiction or multiple contradictions with no matching evidence.
    if (
        counts["contradiction"]
        and not counts["match"]
        and (counts["contradiction"] >= 2 or unresolved == 0)
    ):
        status = "structural_contradiction"
    elif counts["match"]:
        status = "open_for_semantic_evaluation"
    else:
        status = "unknown_requires_semantic_evaluation"
    return {
        **condition,
        "screen_status": status,
        "screen_counts": dict(sorted(counts.items())),
        "evidence_checks": checks,
    }


def prepare_evaluation(
    target_gamepk: str,
    target_date: str,
    semantic_key_allowed: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """Exhaustively screen every date-safe criterion without an arbitrary top-N."""
    index = _read_json(INDEX_PATH)
    games = load_manifest_games()
    target = games.get(str(target_gamepk))
    if target is None:
        raise ValueError(f"target not in manifest: {target_gamepk}")
    target_evidence = census_evidence_map(target)
    if semantic_key_allowed is not None:
        target_evidence = {
            key: value
            for key, value in target_evidence.items()
            if semantic_key_allowed(key)
        }
    source_cache: dict[str, dict[str, dict[str, Any]]] = {}
    candidates = []
    rejected = []
    condition_status_counts: Counter[str] = Counter()
    evidence_reason_counts: Counter[str] = Counter()
    soft_retention_counts: Counter[str] = Counter()
    date_safe_criteria = partition(index, target_date)
    cutoff_receipt = assert_retrieval_cutoff_safe(
        date_safe_criteria,
        target_gamepk=target_gamepk,
        target_date=target_date,
    )
    for criterion in date_safe_criteria:
        source_gp = str(criterion["source_gamePk"])
        if source_gp not in source_cache:
            source_cache[source_gp] = census_evidence_map(games[source_gp])
            if semantic_key_allowed is not None:
                source_cache[source_gp] = {
                    key: value
                    for key, value in source_cache[source_gp].items()
                    if semantic_key_allowed(key)
                }
        screened_conditions = [
            screen_condition(condition, source_cache[source_gp], target_evidence)
            for condition in criterion["conditions"]
        ]
        for condition in screened_conditions:
            condition_status_counts[condition["screen_status"]] += 1
            condition_counts = Counter(check["status"] for check in condition["evidence_checks"])
            reasons = {str(check.get("reason") or "missing") for check in condition["evidence_checks"]}
            evidence_reason_counts.update(str(check.get("reason") or "missing") for check in condition["evidence_checks"])
            if "matchup_role_requires_level_semantics" in reasons:
                soft_retention_counts["matchup_relation_deferred"] += 1
            if reasons & {"pair_relation_tied", "diff_sign_zero"}:
                soft_retention_counts["tie_or_zero_deferred"] += 1
            if (
                condition_counts["contradiction"] == 1
                and condition_counts["match"] == 0
                and condition_counts["unscored"] + condition_counts["missing"] > 0
            ):
                soft_retention_counts["single_contradiction_with_unresolved"] += 1
        row = {
            "criterion_id": criterion["criterion_id"],
            "source_gamePk": source_gp,
            "source_date": criterion["source_date"],
            "axis": criterion["axis"],
            "direction": criterion["direction"],
            "conditions": screened_conditions,
            "invalidators": criterion["invalidators"],
            "correct_view_method": criterion.get("correct_view_method"),
        }
        blocking = [
            condition for condition in screened_conditions
            if condition["screen_status"] == "structural_contradiction"
        ]
        if blocking:
            rejected.append({
                "criterion_id": row["criterion_id"],
                "reason": "required_condition_structurally_contradicted",
                "condition_indexes": [screened_conditions.index(condition) for condition in blocking],
            })
        else:
            candidates.append(row)
    report = {
        "schema": "mlb_dcri_evaluation_prepare.v1",
        "research_only": True,
        "production_integration": "HOLD",
        "target_gamePk": str(target_gamepk),
        "target_date": target_date,
        "retrieval_cutoff_receipt": cutoff_receipt,
        "partition_candidates": len(candidates) + len(rejected),
        "semantic_candidates": len(candidates),
        "structurally_rejected": len(rejected),
        "screen_audit": {
            "condition_status_counts": dict(sorted(condition_status_counts.items())),
            "evidence_reason_counts": dict(sorted(evidence_reason_counts.items())),
            "soft_retention_counts": dict(sorted(soft_retention_counts.items())),
        },
        "candidates": candidates,
        "rejected": rejected,
    }
    report["report_sha256"] = _sha({k: v for k, v in report.items() if k != "report_sha256"})
    return report


def _conditions(block: dict[str, Any]) -> list[dict[str, Any]]:
    jc = block.get("judgment_criterion") or {}
    return list(jc.get("required_combination") or [])


def _invalidators(block: dict[str, Any]) -> list[dict[str, Any]]:
    jc = block.get("judgment_criterion") or {}
    return list(jc.get("invalidators") or [])


def build_index_from_sources(
    games: dict[str, dict[str, Any]], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """Build the semantic index from explicitly supplied, already-validated sources."""
    criteria: list[dict[str, Any]] = []
    errors: list[str] = []
    sem_cache: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        gp = str(row.get("gamePk"))
        game = games.get(gp)
        if not game:
            errors.append(f"manifest_missing:{gp}")
            continue
        if gp not in sem_cache:
            try:
                sem_cache[gp] = census_semantic_map(game)
            except Exception as exc:  # packet unreadable -> row unusable, recorded
                errors.append(f"census_failed:{gp}:{type(exc).__name__}")
                sem_cache[gp] = {}
        sem = sem_cache[gp]
        for axis in AXES:
            block = row.get(axis) or {}
            direction = str(block.get("observed_direction") or "")
            if direction in ("", "PUSH", "UNRESOLVED"):
                continue  # non-directional rows are not retrievable direction criteria
            conds = []
            unmapped = 0
            for item in _conditions(block):
                keys = []
                for ref in item.get("pregame_refs") or []:
                    desc = sem.get(str(ref))
                    if desc:
                        keys.append(f"{desc['kind']}::{desc['key']}")
                    else:
                        unmapped += 1
                conds.append({
                    "role_slot": item.get("role_slot"),
                    "condition": item.get("condition"),
                    "semantic_keys": sorted(set(keys)),
                })
            invs = []
            for item in _invalidators(block):
                keys = []
                for ref in item.get("pregame_refs") or []:
                    desc = sem.get(str(ref))
                    if desc:
                        keys.append(f"{desc['kind']}::{desc['key']}")
                invs.append({
                    "role_slot": item.get("role_slot"),
                    "condition": item.get("condition"),
                    "semantic_keys": sorted(set(keys)),
                })
            criteria.append({
                "criterion_id": f"{gp}:{axis}",
                "source_gamePk": gp,
                "source_date": str(game.get("date")),
                "axis": axis,
                "direction": direction,
                "conditions": conds,
                "invalidators": invs,
                "correct_view_method": (block.get("judgment_criterion") or {}).get("correct_view_method"),
                "unmapped_source_refs": unmapped,
            })
    index = {
        "schema": "mlb_dcri_bank_index.v1",
        "research_only": True,
        "production_integration": "HOLD",
        "bank_rows": len(rows),
        "criteria": criteria,
        "errors": errors,
    }
    index["index_sha256"] = _sha({k: v for k, v in index.items()})
    return index


def build_index(limit: int | None = None) -> dict[str, Any]:
    games = load_manifest_games()
    rows = load_bank_rows()
    if limit:
        rows = rows[:limit]
    return build_index_from_sources(games, rows)


def partition(index: dict[str, Any], target_date: str) -> list[dict[str, Any]]:
    """Strict walk-forward partition: only criteria whose source game predates target."""
    return [c for c in index["criteria"] if c["source_date"] < target_date]


def assert_retrieval_cutoff_safe(
    criteria: list[dict[str, Any]],
    *,
    target_gamepk: str,
    target_date: str,
) -> dict[str, Any]:
    """Fail closed when retrieved criteria include the target or its future."""
    target_gamepk = str(target_gamepk)
    target_date = str(target_date)
    own_game = [
        str(row.get("criterion_id") or "")
        for row in criteria
        if str(row.get("source_gamePk") or "") == target_gamepk
    ]
    non_prior = [
        str(row.get("criterion_id") or "")
        for row in criteria
        if not str(row.get("source_date") or "")
        or str(row.get("source_date") or "") >= target_date
    ]
    if own_game or non_prior:
        raise RuntimeError(
            "criterion retrieval cutoff violation: "
            f"target={target_gamepk}@{target_date} "
            f"own_game={own_game[:10]} non_prior={non_prior[:10]}"
        )
    return {
        "target_gamePk": target_gamepk,
        "target_date": target_date,
        "retrieved": len(criteria),
        "r1_own_game_count": 0,
        "r2_non_prior_count": 0,
        "ok": True,
    }


def partition_leak_check(index: dict[str, Any], target_date: str) -> dict[str, Any]:
    part = partition(index, target_date)
    leaks = [c["criterion_id"] for c in part if c["source_date"] >= target_date]
    return {"target_date": target_date, "candidates": len(part), "leaks": leaks, "ok": not leaks}


def rebind(criterion: dict[str, Any], reverse: dict[str, list[str]]) -> dict[str, Any]:
    """Map criterion condition semantic keys onto the target game's census ids.

    A condition with zero rebindable keys is UNKNOWN (fail-closed): it can never
    evaluate TRUE on this target, so a criterion containing it cannot be applied.
    """
    bound_conditions = []
    fully_bindable = True
    for cond in criterion["conditions"]:
        bindings = {k: reverse.get(k, []) for k in cond["semantic_keys"]}
        bound = {k: v for k, v in bindings.items() if v}
        status = "bindable" if bound else "unknown_unbindable"
        if not bound:
            fully_bindable = False
        bound_conditions.append({**cond, "target_bindings": bound, "binding_status": status})
    bound_invalidators = []
    for inv in criterion["invalidators"]:
        bindings = {k: reverse.get(k, []) for k in inv["semantic_keys"]}
        bound_invalidators.append({**inv, "target_bindings": {k: v for k, v in bindings.items() if v}})
    return {
        "criterion_id": criterion["criterion_id"],
        "axis": criterion["axis"],
        "direction": criterion["direction"],
        "conditions": bound_conditions,
        "invalidators": bound_invalidators,
        "eligible_for_evaluation": fully_bindable,
    }


def aggregate(applied: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministic direction aggregation. Conflict -> UNRESOLVED (no free-form prediction)."""
    directions = sorted({str(a["direction"]) for a in applied})
    if not directions:
        return {"direction": "NO_MATCH", "applied": 0}
    if len(directions) > 1:
        return {"direction": "UNRESOLVED", "applied": len(applied), "conflict": directions}
    return {"direction": directions[0], "applied": len(applied)}


def main() -> int:
    parser = argparse.ArgumentParser(description="DCRI deterministic core (research only).")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build-index")
    b.add_argument("--limit", type=int)
    b.add_argument("--write", action="store_true")
    p = sub.add_parser("partition-check")
    p.add_argument("--date", required=True)
    r = sub.add_parser("rebind-smoke")
    r.add_argument("--target-gamepk", required=True)
    r.add_argument("--date", required=True)
    e = sub.add_parser("prepare-evaluation")
    e.add_argument("--target-gamepk", required=True)
    e.add_argument("--date", required=True)
    e.add_argument("--write", action="store_true")
    args = parser.parse_args()

    if args.command == "build-index":
        index = build_index(limit=args.limit)
        report = {
            "bank_rows": index["bank_rows"],
            "criteria": len(index["criteria"]),
            "errors": index["errors"][:10],
            "error_count": len(index["errors"]),
            "index_sha256": index["index_sha256"],
        }
        if args.write:
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            INDEX_PATH.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
            report["path"] = str(INDEX_PATH)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    index = _read_json(INDEX_PATH)
    if args.command == "partition-check":
        print(json.dumps(partition_leak_check(index, args.date), ensure_ascii=False, indent=2))
        return 0

    if args.command == "prepare-evaluation":
        report = prepare_evaluation(args.target_gamepk, args.date)
        summary = {key: report[key] for key in (
            "schema", "target_gamePk", "target_date", "partition_candidates",
            "semantic_candidates", "structurally_rejected", "report_sha256",
        )}
        if args.write:
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            path = OUT_DIR / f"evaluation_prepare_{args.date}_{args.target_gamepk}.json"
            path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
            summary["path"] = str(path)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    games = load_manifest_games()
    target = games.get(str(args.target_gamepk))
    if not target:
        print(json.dumps({"ok": False, "error": "target not in manifest"}))
        return 2
    reverse = target_reverse_map(target)
    part = partition(index, args.date)
    bindable = 0
    sample = None
    for crit in part:
        bound = rebind(crit, reverse)
        if bound["eligible_for_evaluation"]:
            bindable += 1
            if sample is None:
                sample = bound
    print(json.dumps({
        "target_gamePk": str(args.target_gamepk),
        "target_date": args.date,
        "partition_candidates": len(part),
        "fully_bindable": bindable,
        "sample_criterion": (sample or {}).get("criterion_id"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
