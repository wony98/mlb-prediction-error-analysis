# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
OUT_ROOT = ROOT / "postmortem" / "fullread_v4" / "prediction_delta_review"
DAY_JSONL = OUT_ROOT / "mlb_prediction_delta_review_{date}.jsonl"
DAY_MD = OUT_ROOT / "mlb_prediction_delta_review_{date}.md"
DAY_JSON = OUT_ROOT / "mlb_prediction_delta_review_{date}_summary.json"
MASTER_JSONL = OUT_ROOT / "mlb_prediction_delta_review_master.jsonl"
SNAPSHOT_ROOT = ROOT / "postmortem" / "fullread_v4" / "model_reason_snapshots"
SNAPSHOT_JSONL = SNAPSHOT_ROOT / "mlb_model_reason_snapshot_{date}.jsonl"

COMPARE_LEGS = (
    ROOT
    / "postmortem"
    / "raw_pbp_fullflow"
    / "prediction_outcome_directional_compare"
    / "mlb_prediction_outcome_directional_legs_2026.jsonl"
)

PREGAME_KEYS = ("pregame_support_items", "pregame_conflict_items")
POST_KEYS = ("post_only_items", "run_total_post_same_side_factors", "run_total_post_opposite_factors", "run_total_noise_factors")
VALID_GRADED_DELTA_CASES = {
    "both_hit",
    "both_miss",
    "model_hit_narrator_miss",
    "model_miss_narrator_hit",
    "model_hit_narrator_pass",
    "model_miss_narrator_pass",
}


def now_kst() -> str:
    return datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="seconds")


def clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "null", "-"} else text


def num(value: Any, default: float | None = None) -> float | None:
    text = clean(value)
    if not text:
        return default
    try:
        return float(text)
    except Exception:
        return default


def hash_num(value: Any) -> float | None:
    value = num(value, None)
    if value is None:
        return None
    return round(float(value), 12)


def feature_vector_hash_from_row(row: dict[str, Any], features: list[str]) -> str:
    values = {feature: hash_num(row.get(feature)) for feature in features}
    payload = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def short(value: Any, limit: int = 320) -> str:
    text = " ".join(clean(value).split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def append_replace_master(rows: list[dict[str, Any]], date: str) -> None:
    old_rows: list[dict[str, Any]] = []
    if MASTER_JSONL.exists():
        with MASTER_JSONL.open("r", encoding="utf-8-sig") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if clean(row.get("date")) != date:
                    old_rows.append(row)
    merged = old_rows + rows
    merged.sort(key=lambda r: (clean(r.get("date")), clean(r.get("gamePk")), clean(r.get("market"))))
    write_jsonl(MASTER_JSONL, merged)


def normalize_team_prediction(value: Any, away: str, home: str) -> str:
    text = clean(value)
    if not text:
        return ""
    up = text.upper()
    if up == "AWAY":
        return away
    if up == "HOME":
        return home
    if text == away or text == home:
        return text
    return text


def normalize_run_total_prediction(value: Any) -> str:
    text = clean(value).upper()
    if text in {"UPPER", "LOWER"}:
        return text
    return ""


def actual_run_total(total: float | None, line: float | None) -> str:
    if total is None or line is None:
        return ""
    if total > line:
        return "UPPER"
    if total < line:
        return "LOWER"
    return "PUSH"


def hit_bool(market: str, prediction: str, actual: str) -> bool | None:
    if not prediction or not actual:
        return None
    if market == "RUN_TOTAL" and actual == "PUSH":
        return None
    return prediction == actual


def load_model_rows(date: str) -> tuple[dict[str, dict[str, str]], list[dict[str, Any]], list[str]]:
    path = ROOT / f"_v3_games_{date}.csv"
    warnings: list[dict[str, Any]] = []
    missing: list[str] = []
    rows: dict[str, dict[str, str]] = {}
    if not path.exists():
        missing.append(str(path))
        return rows, warnings, missing
    for row in read_csv_rows(path):
        game_pk = clean(row.get("gamePk")).split(".")[0]
        if not game_pk:
            continue
        rows[game_pk] = row
        prediction_team = clean(row.get("prediction_team"))
        win_prediction = clean(row.get("win_prediction"))
        if prediction_team and win_prediction and prediction_team != win_prediction:
            warnings.append(
                {
                    "type": "win_prediction_differs_from_prediction_team",
                    "date": date,
                    "gamePk": game_pk,
                    "matchup": f"{clean(row.get('away_team'))} @ {clean(row.get('home_team'))}",
                    "ssot_prediction_team": prediction_team,
                    "legacy_win_prediction": win_prediction,
                }
            )
    return rows, warnings, missing


def load_model_reason_snapshots(date: str) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    path = Path(str(SNAPSHOT_JSONL).format(date=date))
    if not path.exists():
        return {}, [str(path)]
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with path.open("r", encoding="utf-8-sig") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            game_pk = clean(row.get("gamePk")).split(".")[0]
            if game_pk:
                out[game_pk].append(row)
    for rows in out.values():
        rows.sort(key=lambda r: (int(num(r.get("snapshot_version"), 0) or 0), clean(r.get("generated_at_kst")), clean(r.get("run_id"))))
    return dict(out), []


def load_model_feature_names() -> list[str]:
    payload = read_json(ROOT / "mlb_clean_win_loss_features.json", {})
    features = payload.get("features") if isinstance(payload, dict) else []
    return [str(x) for x in features] if isinstance(features, list) else []


def select_model_reason_snapshot(
    snapshots: list[dict[str, Any]],
    model_prediction: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    if not snapshots:
        return {}, warnings
    prediction = [row for row in snapshots if clean(row.get("snapshot_mode")) == "prediction_moment"]
    reconstructed = [row for row in snapshots if clean(row.get("snapshot_mode")) == "final_csv_reconstructed"]
    exact_prediction = [row for row in prediction if clean(row.get("prediction_team")) == model_prediction] if model_prediction else prediction
    exact_reconstructed = [row for row in reconstructed if clean(row.get("prediction_team")) == model_prediction] if model_prediction else reconstructed
    exact = exact_prediction or exact_reconstructed
    pool = exact or prediction or reconstructed or snapshots
    chosen = pool[-1]
    if len(snapshots) > 1:
        warnings.append(
            {
                "type": "multiple_model_reason_snapshots",
                "count": len(snapshots),
                "selected_run_id": clean(chosen.get("run_id")),
                "selected_snapshot_version": int(num(chosen.get("snapshot_version"), 0) or 0),
                "selected_snapshot_mode": clean(chosen.get("snapshot_mode")),
                "rule": "delta_review selects prediction_moment first, otherwise latest reconstructed snapshot matching official prediction_team",
            }
        )
    if clean(chosen.get("snapshot_mode")) == "final_csv_reconstructed":
        warnings.append(
            {
                "type": "model_reason_snapshot_reconstructed_not_prediction_moment",
                "selected_run_id": clean(chosen.get("run_id")),
                "selected_snapshot_version": int(num(chosen.get("snapshot_version"), 0) or 0),
                "rule": "past backfill uses final _v3_games csv feature rows; future prediction_moment snapshots remain preferred",
            }
        )
    if model_prediction and not exact:
        warnings.append(
            {
                "type": "model_reason_snapshot_prediction_mismatch",
                "official_model_prediction": model_prediction,
                "selected_snapshot_prediction": clean(chosen.get("prediction_team")),
                "rule": "snapshot shown as nearest available quantitative model driver, not exact official-prediction proof",
            }
        )
    return chosen, warnings


def compact_snapshot_feature_items(items: Any, limit: int = 8) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(items, list):
        return out
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "feature": clean(item.get("feature")),
                "value": item.get("value"),
                "contribution": item.get("contribution"),
                "direction": clean(item.get("direction")),
                "interpretation_guardrail": "model feature contribution only; not identical to human-readable retrospective signal",
            }
        )
    return out


def compact_model_reason_snapshot(snapshot: dict[str, Any], model_prediction: str, away: str, home: str) -> dict[str, Any]:
    if not snapshot:
        return {}
    snapshot_source_warnings = snapshot.get("source_warnings") if isinstance(snapshot.get("source_warnings"), list) else []
    recomputed_prediction_differs = any(
        isinstance(warn, dict) and clean(warn.get("type")) == "recomputed_prediction_differs_from_stored_prediction_team"
        for warn in snapshot_source_warnings
    )
    if model_prediction == home:
        prediction_side_drivers = compact_snapshot_feature_items(snapshot.get("top_home_positive_features"))
    elif model_prediction == away:
        prediction_side_drivers = compact_snapshot_feature_items(snapshot.get("top_away_positive_features"))
    else:
        prediction_side_drivers = []
    if recomputed_prediction_differs:
        prediction_side_drivers = []
    return {
        "schema": clean(snapshot.get("schema")),
        "generated_at_kst": clean(snapshot.get("generated_at_kst")),
        "run_id": clean(snapshot.get("run_id")),
        "snapshot_version": int(num(snapshot.get("snapshot_version"), 0) or 0),
        "snapshot_mode": clean(snapshot.get("snapshot_mode")) or "unknown_legacy",
        "provenance_quality": clean(snapshot.get("provenance_quality")),
        "model_file": clean(snapshot.get("model_file")),
        "feature_file": clean(snapshot.get("feature_file")),
        "model_class": clean(snapshot.get("model_class")),
        "feature_count": int(num(snapshot.get("feature_count"), 0) or 0),
        "p_home": num(snapshot.get("p_home")),
        "p_home_recomputed_from_final_csv": num(snapshot.get("p_home_recomputed_from_final_csv")),
        "prediction_team": clean(snapshot.get("prediction_team")),
        "recomputed_prediction_team": clean(snapshot.get("recomputed_prediction_team")),
        "prediction_side": clean(snapshot.get("prediction_side")),
        "feature_vector_hash": clean(snapshot.get("feature_vector_hash")),
        "contribution_available": bool(snapshot.get("contribution_available")),
        "usable_for_official_prediction_drivers": not recomputed_prediction_differs,
        "source_warnings": snapshot_source_warnings[:6],
        "prediction_side_quantitative_drivers": prediction_side_drivers,
        "top_abs_features": compact_snapshot_feature_items(snapshot.get("top_abs_features")),
        "interpretation_guardrail": (
            "Prediction-moment quantitative model drivers. Keep separate from salience/directional human-readable signals."
        ),
    }


def load_narrator_predictions(date: str) -> tuple[dict[str, dict[str, Any]], list[str]]:
    try:
        from _mlb_narrator_sent_ledger import load_predictions_prefer_sent_ledger

        payload, meta = load_predictions_prefer_sent_ledger(date)
        if payload:
            out: dict[str, dict[str, Any]] = {}
            for game_pk, row in payload.items():
                out[clean(game_pk).split(".")[0]] = row if isinstance(row, dict) else {}
            if meta.get("source") == "sent_ledger":
                return out, []
    except Exception:
        pass
    path = ROOT / f"_narr_predictions_{date}.json"
    if not path.exists():
        return {}, [str(path)]
    payload = read_json(path, {})
    out: dict[str, dict[str, Any]] = {}
    if isinstance(payload, dict):
        for game_pk, row in payload.items():
            out[clean(game_pk).split(".")[0]] = row if isinstance(row, dict) else {}
    return out, []


def load_results(date: str) -> tuple[dict[str, dict[str, Any]], list[str]]:
    path = ROOT / "_narr_results_master.csv"
    rows: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    if path.exists():
        for row in read_csv_rows(path):
            if clean(row.get("date")) != date:
                continue
            game_pk = clean(row.get("gamePk")).split(".")[0]
            if not game_pk:
                continue
            rows[game_pk] = {
                "date": date,
                "gamePk": game_pk,
                "away": clean(row.get("away")),
                "home": clean(row.get("home")),
                "winner": clean(row.get("winner")),
                "total": num(row.get("total")),
                "narrator_win_loss_hit_source": clean(row.get("win_loss_hit")),
                "narrator_run_total_hit_source": clean(row.get("run_total_hit")),
                "narrator_run_total_baseline_source": num(row.get("run_total_baseline")),
            }
    if not rows:
        missing.append(str(path))
    return rows, missing


def load_synth_by_game() -> dict[str, dict[str, Any]]:
    payload = read_json(ROOT / "_synth_master.json", [])
    out: dict[str, dict[str, Any]] = {}
    if isinstance(payload, list):
        for row in payload:
            if isinstance(row, dict):
                game_pk = clean(row.get("gamePk")).split(".")[0]
                if game_pk:
                    out[game_pk] = row
    return out


def load_miss_log(date: str) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for row in read_csv_rows(ROOT / "_narr_miss_log.csv"):
        if clean(row.get("date")) != date:
            continue
        game_pk = clean(row.get("gamePk")).split(".")[0]
        if game_pk:
            out[game_pk] = row
    return out


def load_directional_compare(date: str) -> dict[tuple[str, str, str], dict[str, Any]]:
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    if not COMPARE_LEGS.exists():
        return out
    with COMPARE_LEGS.open("r", encoding="utf-8-sig") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if clean(row.get("date")) != date:
                continue
            game_pk = clean(row.get("gamePk")).split(".")[0]
            source = clean(row.get("source"))
            market = clean(row.get("market")).upper()
            if game_pk and source and market:
                out[(game_pk, source, market)] = row
    return out


def compact_items(items: Any, limit: int = 4) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(items, list):
        return out
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "relation_to_prediction": clean(item.get("relation_to_prediction")),
                "route_kind": clean(item.get("route_kind")),
                "player_name": clean(item.get("player_name")),
                "team": clean(item.get("team")),
                "direction": clean(item.get("direction")),
                "pregame_classification": clean(item.get("pregame_classification")),
                "summary": short(item.get("summary"), 260),
                "refs": (item.get("refs") or [])[:4],
            }
        )
    return out


def compact_directional_leg(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {}
    return {
        "source": clean(row.get("source")),
        "market": clean(row.get("market")).upper(),
        "prediction": clean(row.get("prediction")),
        "prediction_review_label": clean(row.get("prediction_review_label")),
        "verified_hit": row.get("verified_hit"),
        "relation_counts": row.get("relation_counts") or {},
        "classification_counts": row.get("classification_counts") or {},
        "pregame_support_items": compact_items(row.get("pregame_support_items")),
        "pregame_conflict_items": compact_items(row.get("pregame_conflict_items")),
        "post_only_items": compact_items(row.get("post_only_items"), 3),
        "run_total_review_subtype": clean(row.get("run_total_review_subtype")),
        "run_total_review_why": clean(row.get("run_total_review_why")),
        "run_total_support_factors": compact_items(row.get("run_total_support_factors"), 3),
        "run_total_conflict_factors": compact_items(row.get("run_total_conflict_factors"), 3),
    }


def compact_factor_list(items: Any, limit: int = 5) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not isinstance(items, list):
        return out
    for item in items[:limit]:
        if isinstance(item, dict):
            out.append({k: short(v, 240) for k, v in item.items() if k in {"factor", "why", "element", "verdict", "reason"}})
        else:
            out.append({"text": short(item, 240)})
    return out


def compact_synth(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {}
    return {
        "matchup": clean(row.get("matchup")),
        "verdict_orig": short(row.get("verdict_orig"), 360),
        "predictable": compact_factor_list(row.get("predictable")),
        "not_predictable": compact_factor_list(row.get("not_predictable")),
        "luck_recheck": compact_factor_list(row.get("luck_recheck")),
        "prediction_takeaway": short(row.get("prediction_takeaway"), 320),
        "synth_src": clean(row.get("synth_src")),
    }


def selected_salience_lines(date: str, matchup: str) -> list[str]:
    path = ROOT / f"_narr_salience_digest_{date}.txt"
    if not path.exists():
        return []
    header = f"[{matchup}]"
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    selected: list[str] = []
    active = False
    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if stripped == header:
            active = True
            continue
        if active and stripped.startswith("[") and stripped.endswith("]"):
            break
        if not active:
            continue
        if any(
            key in stripped
            for key in (
                "signal-synthesis:",
                "review-calibration:",
                "signal-layer:",
                "player-card:",
                "rawPBP",
                "news:",
                "뉴스",
            )
        ):
            selected.append(short(stripped, 520))
        if len(selected) >= 12:
            break
    return selected


def reasoning_block(date: str, matchup: str) -> list[str]:
    path = ROOT / f"_narr_reasoning_{date}.txt"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    active = False
    block: list[str] = []
    for line in lines:
        if line.startswith("### "):
            if active:
                break
            active = matchup in line
            if active:
                block.append(short(line, 300))
            continue
        if active:
            block.append(short(line, 520))
    useful = [x for x in block if x.strip()]
    if len(useful) <= 8:
        return useful
    return useful[:5] + useful[-3:]


def delta_case(
    market: str,
    model_prediction: str,
    narrator_prediction: str,
    model_hit: bool | None,
    narrator_hit: bool | None,
    actual: str,
) -> str:
    if market == "RUN_TOTAL" and actual == "PUSH":
        return "run_total_push_or_ungraded"
    if model_prediction and not narrator_prediction:
        if model_hit is True:
            return "model_hit_narrator_pass"
        if model_hit is False:
            return "model_miss_narrator_pass"
        return "model_prediction_narrator_pass"
    if narrator_prediction and not model_prediction:
        return "narrator_prediction_model_pass"
    if not model_prediction and not narrator_prediction:
        return "both_pass"
    if model_hit is True and narrator_hit is True:
        return "both_hit"
    if model_hit is False and narrator_hit is False:
        return "both_miss"
    if model_hit is True and narrator_hit is False:
        return "model_hit_narrator_miss"
    if model_hit is False and narrator_hit is True:
        return "model_miss_narrator_hit"
    return "ungraded_or_unknown"


def evidence_for_case(
    case: str,
    model_leg: dict[str, Any],
    narrator_leg: dict[str, Any],
    synth: dict[str, Any],
    miss_log: dict[str, str],
) -> dict[str, Any]:
    model_support = compact_items(model_leg.get("pregame_support_items"))
    model_conflict = compact_items(model_leg.get("pregame_conflict_items"))
    narr_support = compact_items(narrator_leg.get("pregame_support_items"))
    narr_conflict = compact_items(narrator_leg.get("pregame_conflict_items"))
    post_noise = (
        compact_items(model_leg.get("post_only_items"), 3)
        + compact_items(narrator_leg.get("post_only_items"), 3)
        + compact_factor_list(synth.get("not_predictable"), 3)
        + compact_factor_list(synth.get("luck_recheck"), 3)
    )
    payload = {
        "model_hit_reasons_pregame_knowable": [],
        "narrator_hit_reasons_pregame_knowable": [],
        "ignored_model_signals": [],
        "overweighted_narrator_signals": [],
        "shared_hit_support": [],
        "shared_miss_conflicts": [],
        "post_only_or_noise": post_noise[:8],
        "miss_log_takeaway": short(miss_log.get("takeaway") if miss_log else "", 300),
    }
    if case == "model_hit_narrator_miss":
        payload["model_hit_reasons_pregame_knowable"] = model_support
        payload["ignored_model_signals"] = model_support + narr_conflict
        payload["overweighted_narrator_signals"] = narr_support
    elif case == "model_miss_narrator_hit":
        payload["narrator_hit_reasons_pregame_knowable"] = narr_support
        payload["ignored_model_signals"] = narr_support + model_conflict
        payload["overweighted_narrator_signals"] = model_support
    elif case == "both_hit":
        payload["shared_hit_support"] = (model_support + narr_support)[:8]
    elif case == "both_miss":
        payload["shared_miss_conflicts"] = (model_conflict + narr_conflict)[:8]
    elif case == "model_hit_narrator_pass":
        payload["model_hit_reasons_pregame_knowable"] = model_support
        payload["ignored_model_signals"] = model_support
        payload["shared_miss_conflicts"] = model_conflict
    elif case == "model_miss_narrator_pass":
        payload["shared_miss_conflicts"] = model_conflict
        payload["post_only_or_noise"] = (post_noise + model_support)[:8]
    elif case == "model_prediction_narrator_pass":
        payload["ignored_model_signals"] = model_support
    return payload


def takeaway(case: str, market: str, model_prediction: str, narrator_prediction: str, actual: str) -> str:
    if case == "model_hit_narrator_miss":
        return (
            f"{market}: model SSOT prediction hit while narrator missed. Next review must start from why "
            "prediction_team/run_total_prediction_side was right, then require explicit resolution before overriding it."
        )
    if case == "model_miss_narrator_hit":
        return (
            f"{market}: narrator corrected the model miss. Preserve the narrator-side pregame signal pattern "
            "and check whether model-side support was stale or over-weighted."
        )
    if case == "model_hit_narrator_pass":
        return f"{market}: narrator pass avoided risk but missed a model hit. Recheck whether pass was too conservative when model support was concrete."
    if case == "model_miss_narrator_pass":
        return f"{market}: narrator pass protected against a model miss. Preserve the pass pressure pattern when model support is thin or conflicted."
    if case == "model_prediction_narrator_pass":
        return f"{market}: narrator passed while model had a prediction; keep as ungraded pass-quality review."
    if case == "both_hit":
        return f"{market}: both model and narrator hit. Store overlapping pregame support as confidence context."
    if case == "both_miss":
        return f"{market}: both missed. Separate true post-only variance from shared pregame blind spots."
    if case == "narrator_prediction_model_pass":
        return f"{market}: narrator predicted while model had no official prediction. Review as separate narrator-only edge."
    return f"{market}: ungraded or pass-only case; keep for coverage, not rule changes."


def build_row(
    date: str,
    game_pk: str,
    market: str,
    model_row: dict[str, str],
    narrator_prediction_row: dict[str, Any],
    result_row: dict[str, Any],
    compare: dict[tuple[str, str, str], dict[str, Any]],
    synth_rows: dict[str, dict[str, Any]],
    miss_rows: dict[str, dict[str, str]],
    model_reason_snapshots: dict[str, list[dict[str, Any]]],
    feature_names: list[str],
) -> dict[str, Any] | None:
    away = clean(result_row.get("away")) or clean(model_row.get("away_team")) or clean(narrator_prediction_row.get("away"))
    home = clean(result_row.get("home")) or clean(model_row.get("home_team")) or clean(narrator_prediction_row.get("home"))
    matchup = f"{away} @ {home}" if away and home else ""
    winner = clean(result_row.get("winner"))
    total = result_row.get("total")
    line = None
    if market == "WIN_LOSS":
        model_prediction = normalize_team_prediction(model_row.get("prediction_team"), away, home)
        narrator_prediction = normalize_team_prediction(narrator_prediction_row.get("win_loss"), away, home)
        actual = winner
        model_source_field = "prediction_team"
        narrator_source_field = "_narr_predictions.win_loss"
    else:
        model_prediction = normalize_run_total_prediction(model_row.get("run_total_prediction_side")) or normalize_run_total_prediction(model_row.get("run_total_prediction"))
        narrator_prediction = normalize_run_total_prediction(narrator_prediction_row.get("run_total"))
        line = num(model_row.get("run_total_baseline"), result_row.get("narrator_run_total_baseline_source"))
        narr_line = num(narrator_prediction_row.get("line"), result_row.get("narrator_run_total_baseline_source"))
        if line is None:
            line = narr_line
        actual = actual_run_total(total, line)
        model_source_field = "run_total_prediction_side"
        narrator_source_field = "_narr_predictions.run_total"
    if not model_prediction and not narrator_prediction:
        return None

    source_warnings = []
    model_reason_snapshot: dict[str, Any] = {}
    model_quantitative_drivers: list[dict[str, Any]] = []
    if market == "WIN_LOSS":
        snapshot, snapshot_warnings = select_model_reason_snapshot(model_reason_snapshots.get(game_pk, []), model_prediction)
        source_warnings.extend(snapshot_warnings)
        model_reason_snapshot = compact_model_reason_snapshot(snapshot, model_prediction, away, home)
        model_quantitative_drivers = model_reason_snapshot.get("prediction_side_quantitative_drivers") or []
        for warn in model_reason_snapshot.get("source_warnings") or []:
            if isinstance(warn, dict):
                source_warnings.append(
                    {
                        **warn,
                        "snapshot_mode": model_reason_snapshot.get("snapshot_mode"),
                        "rule": clean(warn.get("rule")) or "model reason snapshot warning propagated to delta review",
                    }
                )
        if snapshot and feature_names:
            present_features = sum(1 for feature in feature_names if feature in model_row)
            final_hash = feature_vector_hash_from_row(model_row, feature_names) if present_features >= int(len(feature_names) * 0.8) else ""
            snap_hash = clean(snapshot.get("feature_vector_hash"))
            if snap_hash and final_hash and snap_hash != final_hash:
                source_warnings.append(
                    {
                        "type": "model_reason_snapshot_feature_hash_differs_from_final_csv",
                        "snapshot_hash": snap_hash,
                        "final_csv_hash": final_hash,
                        "final_csv_feature_columns_present": present_features,
                        "rule": "snapshot remains prediction-moment SSOT for model reasons; final CSV may have been freeze/env/backfill mutated",
                    }
                )

    model_hit = hit_bool(market, model_prediction, actual)
    narrator_hit = hit_bool(market, narrator_prediction, actual)
    case = delta_case(market, model_prediction, narrator_prediction, model_hit, narrator_hit, actual)

    model_leg = compare.get((game_pk, "model_v3", market), {})
    narrator_leg = compare.get((game_pk, "narrator", market), {})
    synth = synth_rows.get(game_pk, {})
    miss_log = miss_rows.get(game_pk, {})
    evidence = evidence_for_case(case, model_leg, narrator_leg, synth, miss_log)
    salience = selected_salience_lines(date, matchup)
    reasoning = reasoning_block(date, matchup)
    salience_items = [
        {
            "source": "salience_digest",
            "summary": line,
            "refs": [f"_narr_salience_digest_{date}.txt:{game_pk}"],
        }
        for line in salience
        if "signal-synthesis:" in line or "review-calibration:" in line or "signal-layer:" in line
    ]
    if case in {"model_hit_narrator_miss", "model_hit_narrator_pass", "model_prediction_narrator_pass"} and not evidence["ignored_model_signals"]:
        evidence["ignored_model_signals"] = salience_items[:4]
    if case == "model_hit_narrator_miss" and not evidence["overweighted_narrator_signals"] and reasoning:
        evidence["overweighted_narrator_signals"] = [
            {
                "source": "narrator_reasoning",
                "summary": short(" | ".join(reasoning[:4]), 520),
                "refs": [f"_narr_reasoning_{date}.txt:{game_pk}"],
            }
        ]
    if case == "model_miss_narrator_hit" and not evidence["narrator_hit_reasons_pregame_knowable"]:
        evidence["narrator_hit_reasons_pregame_knowable"] = salience_items[:4]

    win_prediction = clean(model_row.get("win_prediction"))
    if market == "WIN_LOSS" and model_prediction and win_prediction and model_prediction != win_prediction:
        source_warnings.append(
            {
                "type": "legacy_win_prediction_differs_from_prediction_team",
                "official_model_prediction": model_prediction,
                "legacy_win_prediction": win_prediction,
                "rule": "delta_review uses prediction_team only",
            }
        )
    if market == "RUN_TOTAL":
        narr_line = num(narrator_prediction_row.get("line"), None)
        if narr_line is not None and line is not None and narr_line != line:
            source_warnings.append(
                {
                    "type": "model_run_total_baseline_differs_from_narrator_baseline",
                    "model_line": line,
                    "narrator_line": narr_line,
                }
            )

    return {
        "schema": "mlb_prediction_delta_review.v1",
        "date": date,
        "gamePk": game_pk,
        "matchup": matchup,
        "market": market,
        "actual": actual,
        "actual_winner": winner,
        "actual_total": total,
        "line": line,
        "model_prediction": model_prediction,
        "model_prediction_source": model_source_field,
        "model_hit": model_hit,
        "narrator_prediction": narrator_prediction,
        "narrator_prediction_source": narrator_source_field,
        "narrator_hit": narrator_hit,
        "delta_case": case,
        "source_warnings": source_warnings,
        "model_context": {
            "prediction_p": num(model_row.get("prediction_p")),
            "prediction_edge": num(model_row.get("prediction_edge")),
            "prediction_market_price": num(model_row.get("prediction_market_price") or model_row.get("win_win_loss")),
            "win_loss_drivers": clean(model_row.get("win_loss_drivers")),
            "run_total_signal": clean(model_row.get("run_total_signal")),
            "run_total_drivers": clean(model_row.get("run_total_drivers")),
            "sp_signal_team": clean(model_row.get("sp_signal_team")),
            "sp_signal_strength": clean(model_row.get("sp_signal_strength")),
            "model_reason_snapshot_available": bool(model_reason_snapshot),
        },
        "model_reason_snapshot": model_reason_snapshot,
        "model_quantitative_drivers": model_quantitative_drivers,
        "directional_model_leg": compact_directional_leg(model_leg),
        "directional_narrator_leg": compact_directional_leg(narrator_leg),
        "synth_labels": compact_synth(synth),
        "miss_log": miss_log,
        "salience_digest_lines": salience,
        "narrator_reasoning_excerpt": reasoning,
        **evidence,
        "next_narrator_takeaway": takeaway(case, market, model_prediction, narrator_prediction, actual),
        "refs_used": [
            ref
            for ref in [
                f"_v3_games_{date}.csv:{game_pk}",
                f"_narr_predictions_{date}.json:{game_pk}",
                "_narr_results_master.csv",
                str(COMPARE_LEGS),
                "_synth_master.json",
                "_narr_miss_log.csv",
                f"_narr_salience_digest_{date}.txt",
                f"_narr_reasoning_{date}.txt",
                str(Path(str(SNAPSHOT_JSONL).format(date=date))) if model_reason_snapshot else "",
            ]
            if ref
        ],
        "guardrail": "Research-only delta review. No Notification, production prediction, narrator prediction, auto flip/remove/neutral mutation.",
    }


def build(date: str, write: bool = False) -> dict[str, Any]:
    model_rows, reconcile_warnings, missing_model = load_model_rows(date)
    narrator_predictions, missing_predictions = load_narrator_predictions(date)
    results, missing_results = load_results(date)
    synth_rows = load_synth_by_game()
    miss_rows = load_miss_log(date)
    compare = load_directional_compare(date)
    model_reason_snapshots, missing_snapshots = load_model_reason_snapshots(date)
    feature_names = load_model_feature_names()

    all_game_pks = sorted(set(model_rows) | set(narrator_predictions) | set(results))
    rows: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for game_pk in all_game_pks:
        model_row = model_rows.get(game_pk, {})
        narrator_row = narrator_predictions.get(game_pk, {})
        result_row = results.get(game_pk, {})
        if not result_row:
            skipped.append({"gamePk": game_pk, "reason": "missing_result_row"})
            continue
        for market in ("WIN_LOSS", "RUN_TOTAL"):
            row = build_row(
                date,
                game_pk,
                market,
                model_row,
                narrator_row,
                result_row,
                compare,
                synth_rows,
                miss_rows,
                model_reason_snapshots,
                feature_names,
            )
            if row:
                rows.append(row)

    rows.sort(key=lambda r: (clean(r.get("gamePk")), clean(r.get("market"))))
    case_counts = Counter(clean(r.get("delta_case")) for r in rows)
    market_case_counts = Counter((clean(r.get("market")), clean(r.get("delta_case"))) for r in rows)
    missing_compare = [
        {
            "gamePk": r["gamePk"],
            "market": r["market"],
            "missing_model_leg": not bool(r.get("directional_model_leg")),
            "missing_narrator_leg": bool(r.get("narrator_prediction")) and not bool(r.get("directional_narrator_leg")),
        }
        for r in rows
        if not bool(r.get("directional_model_leg")) or (bool(r.get("narrator_prediction")) and not bool(r.get("directional_narrator_leg")))
    ]
    summary = {
        "schema": "mlb_prediction_delta_review_summary.v1",
        "generated_at_kst": now_kst(),
        "date": date,
        "research_only": True,
        "model_games": len(model_rows),
        "narrator_games": len(narrator_predictions),
        "result_games": len(results),
        "review_legs": len(rows),
        "graded_delta_cases": sum(1 for r in rows if clean(r.get("delta_case")) in VALID_GRADED_DELTA_CASES),
        "delta_case_counts": dict(case_counts),
        "market_delta_case_counts": {f"{k[0]}:{k[1]}": v for k, v in sorted(market_case_counts.items())},
        "win_prediction_reconcile_warnings": reconcile_warnings,
        "missing_inputs": missing_model + missing_predictions + missing_results,
        "model_reason_snapshot_rows_for_date": sum(len(v) for v in model_reason_snapshots.values()),
        "rows_with_model_reason_snapshot": sum(1 for r in rows if bool(r.get("model_reason_snapshot"))),
        "missing_model_reason_snapshot_files": missing_snapshots,
        "skipped": skipped,
        "missing_directional_compare_legs": missing_compare[:40],
        "directional_compare_rows_for_date": len(compare),
        "outputs": {
            "day_jsonl": str(Path(str(DAY_JSONL).format(date=date))),
            "day_md": str(Path(str(DAY_MD).format(date=date))),
            "day_summary_json": str(Path(str(DAY_JSON).format(date=date))),
            "master_jsonl": str(MASTER_JSONL),
            "model_reason_snapshot_jsonl": str(Path(str(SNAPSHOT_JSONL).format(date=date))),
        },
        "guardrails": [
            "Official model WIN_LOSS SSOT is _v3_games_DATE.csv:prediction_team.",
            "Official model RUN_TOTAL SSOT is _v3_games_DATE.csv:run_total_prediction_side, with run_total_prediction fallback only if side is empty.",
            "win_prediction is legacy/reconcile-only and never used as official model WIN_LOSS.",
            "Consumes existing synth/deepread/salience/miss_log/directional labels; no new LLM judgment.",
            "Model reason snapshots are prediction-moment quantitative drivers and are not treated as identical to human-readable signals.",
            "No Notification, production prediction, narrator prediction, auto flip/remove/neutral mutation.",
        ],
    }

    if write:
        day_jsonl = Path(str(DAY_JSONL).format(date=date))
        day_md = Path(str(DAY_MD).format(date=date))
        day_json = Path(str(DAY_JSON).format(date=date))
        write_jsonl(day_jsonl, rows)
        write_json(day_json, summary)
        day_md.write_text(markdown(summary, rows), encoding="utf-8")
        append_replace_master(rows, date)
    return summary


def markdown(summary: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    lines = [
        f"# MLB Prediction Delta Review {summary['date']}",
        "",
        "Research-only review of model-vs-narrator delta cases.",
        "",
        f"- generated_at_kst: `{summary['generated_at_kst']}`",
        f"- model_games: `{summary['model_games']}`",
        f"- narrator_games: `{summary['narrator_games']}`",
        f"- result_games: `{summary['result_games']}`",
        f"- review_legs: `{summary['review_legs']}`",
        f"- directional_compare_rows_for_date: `{summary['directional_compare_rows_for_date']}`",
        f"- model_reason_snapshot_rows_for_date: `{summary.get('model_reason_snapshot_rows_for_date', 0)}`",
        f"- rows_with_model_reason_snapshot: `{summary.get('rows_with_model_reason_snapshot', 0)}`",
        f"- graded_delta_cases: `{summary['graded_delta_cases']}`",
        "",
        "## Delta Cases",
        "",
    ]
    for case, count in sorted(summary["delta_case_counts"].items()):
        lines.append(f"- `{case}`: `{count}`")
    lines.extend(["", "## Market Cases", ""])
    for key, count in sorted(summary["market_delta_case_counts"].items()):
        lines.append(f"- `{key}`: `{count}`")
    lines.extend(["", "## Reconcile Warnings", ""])
    warnings = summary.get("win_prediction_reconcile_warnings") or []
    if not warnings:
        lines.append("- none")
    for warn in warnings[:20]:
        lines.append(
            f"- `{warn['gamePk']}` {warn['matchup']}: prediction_team=`{warn['ssot_prediction_team']}` "
            f"win_prediction=`{warn['legacy_win_prediction']}`"
        )
    lines.extend(["", "## Key Delta Rows", ""])
    interesting = [
        r
        for r in rows
        if clean(r.get("delta_case"))
        in {"model_hit_narrator_miss", "model_miss_narrator_hit", "model_hit_narrator_pass", "model_miss_narrator_pass", "model_prediction_narrator_pass", "both_miss"}
    ][:18]
    if not interesting:
        lines.append("- none")
    for r in interesting:
        model = clean(r.get("model_prediction")) or "PASS"
        narr = clean(r.get("narrator_prediction")) or "PASS"
        lines.append(
            f"- `{r['gamePk']}` {r['matchup']} `{r['market']}` `{r['delta_case']}`: "
            f"model={model} hit={r['model_hit']} | narrator={narr} hit={r['narrator_hit']} | actual={r['actual']}"
        )
        lines.append(f"  - takeaway: {r['next_narrator_takeaway']}")
        for item in (r.get("ignored_model_signals") or [])[:2]:
            lines.append(f"  - ignored/model signal: {item.get('summary')}")
        for item in (r.get("model_quantitative_drivers") or [])[:2]:
            lines.append(
                "  - model quantitative driver: "
                f"{item.get('feature')}={item.get('value')} contrib={item.get('contribution')} "
                "(not identical to narrative signal)"
            )
    lines.extend(["", "## Missing Directional Compare Legs", ""])
    missing = summary.get("missing_directional_compare_legs") or []
    if not missing:
        lines.append("- none")
    else:
        lines.append(f"- count shown: `{len(missing)}`")
        for item in missing[:12]:
            lines.append(
                f"- `{item['gamePk']}` `{item['market']}` "
                f"missing_model={item['missing_model_leg']} missing_narrator={item['missing_narrator_leg']}"
            )
    lines.extend(["", "## Outputs", ""])
    for key, path in summary["outputs"].items():
        lines.append(f"- `{key}`: `{path}`")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Build MLB model-vs-narrator prediction delta review.")
    ap.add_argument("date", nargs="?", help="MLB official date YYYY-MM-DD")
    ap.add_argument("--date", dest="date_opt", help="MLB official date YYYY-MM-DD")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    date = clean(args.date_opt or args.date)
    if not date:
        raise SystemExit("date required, e.g. py _mlb_prediction_delta_review.py 2026-06-22 --write")
    summary = build(date, write=args.write)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
