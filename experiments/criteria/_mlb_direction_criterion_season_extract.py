# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
from __future__ import annotations

import argparse
import concurrent.futures as futures
import csv
import hashlib
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from functools import lru_cache

import jsonschema

import _mlb_direction_criterion_quality_parity_5 as quality
import _mlb_direction_criterion_symmetric_v3 as v3
from _mlb_direction_criterion_calibration_source import drilldown_indices
from _mlb_direction_criterion_pilot_prepare import compact_original_pbp
from _mlb_judgment_routing_pilot import sanitize_v3_row


ROOT = Path(__file__).resolve().parent
BASE = ROOT / "postmortem/fullread_v4/direction_criterion_season_2026"
PACKETS = ROOT / "postmortem/fullread_v4/mlb_fullread_v4_packets.jsonl"
FEATURE_FILES = {
    "bullpen_usage": ROOT / "postmortem/fullread_v4/mlb_fullread_v4_bullpen_pregame_features.csv",
    "handedness_matchup": ROOT / "postmortem/fullread_v4/mlb_fullread_v4_handedness_matchup_pregame_features.csv",
    "player_contact": ROOT / "postmortem/fullread_v4/mlb_fullread_v4_player_contact_pregame_features.csv",
    "run_environment": ROOT / "postmortem/fullread_v4/mlb_fullread_v4_run_context_pregame_features.csv",
}
BULLPEN_PROJECTION = (
    ROOT / "postmortem/fullread_v4/mlb_fullread_v4_bullpen_projection_backtest_2026_projection.csv"
)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    tmp.replace(path)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def clean_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if value not in (None, "")}


def _number(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        number = float(value)
        return None if math.isnan(number) else number
    except (TypeError, ValueError):
        return None


def _predicted_diff(row: dict[str, Any], home_key: str, away_key: str, prediction_home: bool) -> float | None:
    home = _number(row.get(home_key))
    away = _number(row.get(away_key))
    if home is None or away is None:
        return None
    return home - away if prediction_home else away - home


def _final_score_index(date: str) -> dict[str, dict[str, Any]]:
    path = ROOT / "postmortem" / "partial_results" / f"{date.replace('-', '')}_mlb_partial_status.json"
    if not path.exists():
        return {}
    payload = read_json(path)
    return {
        str(game.get("game_id") or game.get("gamePk")): game
        for game in payload.get("games") or []
        if str(game.get("state") or "").lower() == "final"
    }


def _extension_packet(date: str, row: dict[str, Any], score: dict[str, Any], bullpen: dict[str, Any]) -> dict[str, Any]:
    game_pk = str(row["gamePk"])
    away = str(row.get("away_team") or score.get("away") or "")
    home = str(row.get("home_team") or score.get("home") or "")
    away_score = _number(score.get("away_score"))
    home_score = _number(score.get("home_score"))
    prediction = str(row.get("win_prediction") or row.get("prediction_team") or "")
    prediction_home = prediction == home
    winner = home if home_score is not None and away_score is not None and home_score > away_score else away
    lineup_delta = _predicted_diff(row, "today_home_lineup_sum", "today_away_lineup_sum", prediction_home)
    if lineup_delta is None:
        lineup_delta = _predicted_diff(row, "home_lineup_ops", "away_lineup_ops", prediction_home)
    pregame = {
        "prediction_p": _number(row.get("prediction_p")) or _number(row.get("win_conf")),
        "implied_prediction": _number(row.get("win_implied")),
        "edge": _number(row.get("prediction_edge")) if _number(row.get("prediction_edge")) is not None else _number(row.get("win_edge")),
        "home_WIN_LOSS": _number(row.get("home_WIN_LOSS")),
        "away_WIN_LOSS": _number(row.get("away_WIN_LOSS")),
        "run_total_baseline": _number(row.get("run_total_baseline")),
        "home_sp_name": row.get("home_sp_name") or "",
        "away_sp_name": row.get("away_sp_name") or "",
        "bat30_diff": _predicted_diff(row, "home_bat_roll30_xwoba", "away_bat_roll30_xwoba", prediction_home),
        "bat14_diff": _predicted_diff(row, "home_bat_roll14_xwoba", "away_bat_roll14_xwoba", prediction_home),
        "bat30_barrel_diff": _predicted_diff(row, "home_bat_roll30_barrel_pct", "away_bat_roll30_barrel_pct", prediction_home),
        "sp30_diff": _predicted_diff(row, "home_sp_roll30_xwoba", "away_sp_roll30_xwoba", prediction_home),
        "sp14_diff": _predicted_diff(row, "home_sp_roll14_xwoba", "away_sp_roll14_xwoba", prediction_home),
        "sp30_bb_diff": _predicted_diff(row, "home_sp_roll30_bb_pct", "away_sp_roll30_bb_pct", prediction_home),
        "sp30_barrel_diff": _predicted_diff(row, "home_sp_roll30_barrel_pct", "away_sp_roll30_barrel_pct", prediction_home),
        "lineup_delta": lineup_delta,
        "weather": {
            "temp_c": _number(row.get("temp_c")),
            "wind_kmh": _number(row.get("wind_kmh")),
            "precip_mm": _number(row.get("precip_mm")),
            "humidity_pct": _number(row.get("humidity_pct")),
            "dome": _number(row.get("dome")),
        },
        "bullpen": {"source": "bullpen:pregame.date_lt_target", "feature_row": clean_row(bullpen)},
        "coverage": "v3_historical_replay_extension",
    }
    return {
        "schema": "mlb_fullread_v4_packet.v1.research_extension",
        "season": int(date[:4]),
        "date": date,
        "gamePk": game_pk,
        "matchup": f"{away} @ {home}",
        "away": away,
        "home": home,
        "win_loss_prediction": prediction,
        "win_loss_prediction_side": "HOME" if prediction_home else "AWAY",
        "winner": winner,
        "win_loss_hit": int(bool(prediction and winner and prediction == winner)),
        "known_signal_flags": {"c3": False, "c4": False, "note": "Research extension; no active prediction mutation."},
        "pregame": pregame,
        "postgame": {
            "away_score": away_score,
            "home_score": home_score,
            "total": away_score + home_score if away_score is not None and home_score is not None else None,
        },
        "timing_policy": {
            "evidence_cutoff": f"date < {date}",
            "pregame_source": "historical pregame replay",
            "postgame_source": "official final score and raw PBP",
        },
    }


def _v3_extension_packets(season: int, existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not existing:
        return []
    existing_ids = {str(row["gamePk"]) for row in existing}
    latest_date = max(str(row["date"]) for row in existing)
    bullpen_index = {
        str(row["gamePk"]): row for row in csv_rows(FEATURE_FILES["bullpen_usage"])
    }
    rows: list[dict[str, Any]] = []
    for path in sorted(ROOT.glob(f"_v3_games_{season}-??-??.csv")):
        date = path.stem.replace("_v3_games_", "")
        if date <= latest_date:
            continue
        scores = _final_score_index(date)
        for row in csv_rows(path):
            game_pk = str(row.get("gamePk") or "")
            raw = ROOT / "_deepread_v3" / "raw_plays" / date / f"{game_pk}.json"
            ledger = ROOT / "postmortem" / "raw_pbp_event_ledger" / "games" / date / f"{game_pk}.json"
            if not game_pk or game_pk in existing_ids or game_pk not in scores:
                continue
            if not raw.exists() or not ledger.exists() or game_pk not in bullpen_index:
                continue
            rows.append(_extension_packet(date, row, scores[game_pk], bullpen_index[game_pk]))
            existing_ids.add(game_pk)
    return rows


def packet_rows(season: int) -> list[dict[str, Any]]:
    rows = []
    with PACKETS.open(encoding="utf-8-sig") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if str(row.get("date") or "").startswith(f"{season}-"):
                rows.append(row)
    rows.extend(_v3_extension_packets(season, rows))
    return sorted(rows, key=lambda row: (str(row["date"]), int(row["gamePk"])))


@lru_cache(maxsize=1)
def feature_indexes() -> tuple[dict[str, dict[str, dict[str, str]]], dict[str, list[dict[str, str]]]]:
    features: dict[str, dict[str, dict[str, str]]] = {}
    for name, path in FEATURE_FILES.items():
        features[name] = {str(row["gamePk"]): clean_row(row) for row in csv_rows(path)}
    projections: dict[str, list[dict[str, str]]] = {}
    for row in csv_rows(BULLPEN_PROJECTION):
        projections.setdefault(str(row["gamePk"]), []).append(clean_row(row))
    for rows in projections.values():
        rows.sort(key=lambda row: (str(row.get("team")), int(row.get("team_projection_rank") or 999)))
    return features, projections


def v3_row(date: str, game_pk: str) -> tuple[Path, dict[str, str]]:
    path = ROOT / f"_v3_games_{date}.csv"
    for row in csv_rows(path):
        if str(row.get("gamePk")) == game_pk:
            return path, row
    raise RuntimeError(f"missing v3 row {date}/{game_pk}")


def final_state(packet: dict[str, Any], row: dict[str, str]) -> dict[str, Any]:
    post = packet.get("postgame") or {}
    away = post.get("away_score")
    home = post.get("home_score")
    line_raw = row.get("run_total_baseline") or (packet.get("pregame") or {}).get("run_total_baseline")
    line = float(line_raw) if line_raw not in (None, "") else None
    total = float(away) + float(home) if away is not None and home is not None else None
    win_loss = "UNRESOLVED"
    if away is not None and home is not None:
        win_loss = "SIDE_A" if float(away) > float(home) else "SIDE_B" if float(home) > float(away) else "PUSH"
    run_total = "UNRESOLVED"
    if total is not None and line is not None:
        run_total = "UPPER" if total > line else "LOWER" if total < line else "PUSH"
    return {"away_score": away, "home_score": home, "total": total, "run_total_baseline": line, "WIN_LOSS": win_loss, "RUN_TOTAL": run_total}


def prepare(season: int, chunk_size: int, preserve_prefix_manifest: Path | None = None) -> dict[str, Any]:
    packets = packet_rows(season)
    features, projections = feature_indexes()
    games = []
    excluded = []
    for packet in packets:
        gp = str(packet["gamePk"])
        date = str(packet["date"])
        raw = ROOT / "_deepread_v3/raw_plays" / date / f"{gp}.json"
        ledger = ROOT / "postmortem/raw_pbp_event_ledger/games" / date / f"{gp}.json"
        v3_path, row = v3_row(date, gp)
        missing = [name for name, path in (("raw_pbp", raw), ("event_ledger", ledger)) if not path.exists()]
        if missing:
            excluded.append({"gamePk": gp, "date": date, "reason": "no_play_or_missing_raw", "missing": missing})
            continue
        ledger_data = read_json(ledger)
        if not bool((ledger_data.get("integrity") or {}).get("ok")):
            excluded.append({"gamePk": gp, "date": date, "reason": "event_ledger_integrity_failed"})
            continue
        feature_missing = [name for name, index in features.items() if gp not in index]
        if feature_missing:
            excluded.append({"gamePk": gp, "date": date, "reason": "pregame_feature_missing", "missing": feature_missing})
            continue
        game = {
            "gamePk": gp,
            "date": date,
            "matchup": str(packet.get("matchup") or f"{packet.get('away')} @ {packet.get('home')}"),
            "away_team": packet.get("away"),
            "home_team": packet.get("home"),
            "source_paths": {
                "raw_pbp": str(raw),
                "event_ledger": str(ledger),
                "v3_slate": str(v3_path),
            },
            "source_sha256": {
                "raw_pbp": sha256(raw),
                "event_ledger": sha256(ledger),
                "v3_slate": sha256(v3_path),
            },
            "final_state": final_state(packet, row),
            "pregame_coverage": {
                "v3": True,
                **{name: gp in index for name, index in features.items()},
                "bullpen_player_projection": gp in projections,
            },
        }
        games.append(game)
    preserved_prefix_games = 0
    appended_games = len(games)
    if preserve_prefix_manifest is not None:
        prefix = read_json(preserve_prefix_manifest)
        if int(prefix.get("season") or 0) != season:
            raise RuntimeError(f"prefix manifest season mismatch: {prefix.get('season')} != {season}")
        if int(prefix.get("chunk_size") or 0) != chunk_size:
            raise RuntimeError(f"prefix manifest chunk size mismatch: {prefix.get('chunk_size')} != {chunk_size}")
        prefix_ids = [str(game["gamePk"]) for game in prefix.get("games") or []]
        if len(prefix_ids) != len(set(prefix_ids)):
            raise RuntimeError("prefix manifest contains duplicate gamePks")
        candidate_by_id = {str(game["gamePk"]): game for game in games}
        missing_prefix = [game_pk for game_pk in prefix_ids if game_pk not in candidate_by_id]
        if missing_prefix:
            raise RuntimeError(f"current sources no longer cover prefix gamePks: {missing_prefix[:20]}")
        prefix_set = set(prefix_ids)
        games = [candidate_by_id[game_pk] for game_pk in prefix_ids] + [
            game for game in games if str(game["gamePk"]) not in prefix_set
        ]
        preserved_prefix_games = len(prefix_ids)
        appended_games = len(games) - preserved_prefix_games
    for index, game in enumerate(games):
        game["chunk"] = index // chunk_size
    payload = {
        "schema": "mlb_direction_criterion_season_manifest.v1",
        "research_only": True,
        "season": season,
        "chunk_size": chunk_size,
        "packet_games": len(packets),
        "eligible_games": len(games),
        "excluded_games": excluded,
        "chunk_count": math.ceil(len(games) / chunk_size),
        "preserved_prefix_manifest": str(preserve_prefix_manifest or ""),
        "preserved_prefix_games": preserved_prefix_games,
        "appended_games": appended_games,
        "feature_sources": {name: {"path": str(path), "sha256": sha256(path)} for name, path in FEATURE_FILES.items()},
        "bullpen_projection_source": {"path": str(BULLPEN_PROJECTION), "sha256": sha256(BULLPEN_PROJECTION)},
        "games": games,
        "guardrails": {
            "player_mapped_full_flow": True,
            "pregame_only_criteria": True,
            "common_semantic_gate": True,
            "production": "HOLD",
            "notification": "HOLD",
            "auto_flip": False,
        },
    }
    write_json(BASE / "SEASON_SOURCE_MANIFEST.json", payload)
    write_json(
        BASE / "PREPARE_REPORT.json",
        {
            key: payload[key]
            for key in (
                "schema",
                "season",
                "packet_games",
                "eligible_games",
                "excluded_games",
                "chunk_count",
                "preserved_prefix_manifest",
                "preserved_prefix_games",
                "appended_games",
            )
        },
    )
    return payload


@lru_cache(maxsize=1)
def manifest() -> dict[str, Any]:
    return read_json(BASE / "SEASON_SOURCE_MANIFEST.json")


@lru_cache(maxsize=1)
def packet_index() -> dict[str, dict[str, Any]]:
    return {str(row["gamePk"]): row for row in packet_rows(int(manifest()["season"]))}


def build_game_source(game: dict[str, Any]) -> dict[str, Any]:
    gp = str(game["gamePk"])
    date = str(game["date"])
    out = BASE / "shared_source" / gp
    raw_path = Path(game["source_paths"]["raw_pbp"])
    ledger_path = Path(game["source_paths"]["event_ledger"])
    raw = read_json(raw_path)
    ledger = read_json(ledger_path)
    v3_path, row = v3_row(date, gp)
    packets = packet_index()
    features, projections = feature_indexes()
    packet = packets[gp]
    tier1_path = out / "tier1_compact_allplays.json"
    tier2_path = out / "tier2_exact_raw_drilldown.json"
    pregame_path = out / "frozen_pregame_packet.json"
    axis_path = out / "axis_reference.json"
    tier1 = compact_original_pbp(raw_path)
    categories = drilldown_indices(raw, ledger)
    selected = sorted({idx for values in categories.values() for idx in values})
    tier2 = {
        "schema": "mlb_direction_criterion_exact_raw_drilldown.v1",
        "source_raw_path": str(raw_path),
        "source_raw_sha256": sha256(raw_path),
        "categories": categories,
        "selected_indices": selected,
        "exact_allPlays": [{"allPlays_index": idx, "play": raw["allPlays"][idx]} for idx in selected],
    }
    v3_facts = sanitize_v3_row(row)
    v3_facts["axis_inputs"]["fullread_packet_pregame"] = packet.get("pregame") or {}
    v3_facts["axis_inputs"]["season_feature_ledgers"] = {
        name: features[name][gp] for name in FEATURE_FILES
    }
    projected_by_side: dict[str, list[dict[str, Any]]] = {"away": [], "home": []}
    for projection in projections.get(gp) or []:
        team = str(projection.get("team") or "")
        side = "away" if team == str(game.get("away_team") or "") else "home" if team == str(game.get("home_team") or "") else None
        if side:
            projected_by_side[side].append(
                {
                    "name": projection.get("pitcher_name"),
                    "pitcher_ids": [projection.get("pitcher_id")],
                    "projection": projection,
                }
            )
    deterministic_bullpen_path = {
        "source": "season-internal date-before-target projection backtest",
        "pre_knowable_ok": True,
        "player_cards": projected_by_side,
    }
    safe_packet = {
        "schema": "mlb_direction_criterion_season_pregame.v1",
        "date": date,
        "gamePk": gp,
        "evidence_cutoff": f"date < {date}",
        "v3_non_bullpen_facts": v3_facts,
        "deterministic_bullpen_path": deterministic_bullpen_path,
        "coverage_note": "Empty early-season metrics are unknown, not zero evidence. All supplied historical features are date-before-target/pre-knowable sources.",
    }
    axis = {
        "schema": "mlb_direction_criterion_season_axis_reference.v1",
        "gamePk": gp,
        "date": date,
        "away_team": row.get("away_team") or game.get("away_team"),
        "home_team": row.get("home_team") or game.get("home_team"),
        "run_total_baseline": game["final_state"]["run_total_baseline"],
        "observed_WIN_LOSS": game["final_state"]["WIN_LOSS"],
        "observed_RUN_TOTAL": game["final_state"]["RUN_TOTAL"],
        "note": "Observed directions grade the retrospective read; they are never admissible pregame criterion evidence.",
    }
    write_json(tier1_path, tier1)
    write_json(tier2_path, tier2)
    write_json(pregame_path, safe_packet)
    write_json(axis_path, axis)
    built = dict(game)
    built["source_paths"] = {
        **game["source_paths"],
        "tier1_compact_allplays": str(tier1_path),
        "tier2_exact_raw_drilldown": str(tier2_path),
        "frozen_pregame_packet": str(pregame_path),
        "axis_reference": str(axis_path),
    }
    built["coverage"] = {
        "raw_allPlays": len(raw.get("allPlays") or []),
        "tier1_allPlays": len(tier1.get("allPlays") or []),
        "tier2_exact_indices": len(selected),
        "event_ledger_integrity_ok": bool((ledger.get("integrity") or {}).get("ok")),
    }
    write_json(out / "source_record.json", built)
    return built


def output_schema(game_pk: str) -> dict[str, Any]:
    schema = quality.schema(game_pk)
    props = schema["properties"]
    props["WIN_LOSS"]["properties"]["observed_direction"]["enum"] = ["SIDE_A", "SIDE_B", "PUSH", "UNRESOLVED"]
    props["RUN_TOTAL"]["properties"]["observed_direction"]["enum"] = ["UPPER", "LOWER", "PUSH", "UNRESOLVED"]
    return schema


def safe_identity_tokens(game: dict[str, Any]) -> set[str]:
    import re

    tokens: set[str] = set()
    tokens.update(part.lower() for part in re.findall(r"[A-Za-z]{3,}", str(game.get("matchup") or "")))
    for entry in v3.retrospective_census(game)["entries"]:
        summary = entry.get("summary") or {}
        if not isinstance(summary, dict):
            continue
        for key in ("batter", "pitcher"):
            value = summary.get(key)
            if isinstance(value, dict):
                value = value.get("name") or value.get("fullName")
            if isinstance(value, str) and value.strip():
                tokens.update(part.lower() for part in re.findall(r"[A-Za-z]{3,}", value))
    return tokens


def prompt(game: dict[str, Any]) -> str:
    gp = str(game["gamePk"])
    retro = v3.retrospective_census(game)
    pre = v3.pregame_census(game)
    axis = read_json(Path(game["source_paths"]["axis_reference"]))
    return (
        "# MLB season judgment-criterion extraction\n\n"
        "Research only. Read the complete player-mapped game flow before judging. For WIN_LOSS and RUN_TOTAL separately explain why the result happened, "
        "separate pregame-knowable evidence from realized execution/luck, and derive this exact kind of criterion: with this pregame evidence combination, "
        "what should have been prioritized and how should the game have been judged to choose the observed direction? This is not a generic warning list. "
        "Preserve both supporting and opposing evidence. Generalize only identities into SIDE_A/SIDE_B/MATCHUP roles; do not erase the concrete baseball mechanism. "
        "Do not use model direction as the answer anchor. Empty early-season fields mean unknown. PUSH/UNRESOLVED axes must state why no directional RUN_TOTAL criterion is bankable. "
        "Every retrospective claim needs RA refs and every pregame fact/criterion condition needs PB refs.\n\n"
        f"{quality.HARDENED_CONTRACT}\n\nReturn one strict JSON object only.\n\n"
        f"## Schema\n{json.dumps(output_schema(gp), ensure_ascii=False)}\n\n"
        f"## Identity and grading reference\n{json.dumps(axis, ensure_ascii=False, separators=(',', ':'))}\n\n"
        f"## Complete retrospective census\n{json.dumps(retro, ensure_ascii=False, separators=(',', ':'))}\n\n"
        f"## Complete frozen pregame census\n{json.dumps(pre, ensure_ascii=False, separators=(',', ':'))}\n"
    )


def validate(game: dict[str, Any], path: Path) -> list[str]:
    errors: list[str] = []
    try:
        row = quality.parse_jsonish(path)
        jsonschema.validate(row, output_schema(str(game["gamePk"])))
    except Exception as exc:
        return [f"schema:{type(exc).__name__}:{exc}"]
    retro_ids = {entry["census_id"] for entry in v3.retrospective_census(game)["entries"]}
    pre = v3.pregame_census(game)
    pre_ids = {entry["census_id"] for entry in [*pre["entries"], *pre["structural_comparisons"]]}
    for ref in quality.all_refs(row, "retrospective_refs"):
        if ref not in retro_ids:
            errors.append("bad_retro_ref:" + ref)
    for ref in quality.all_refs(row, "pregame_refs"):
        if ref not in pre_ids:
            errors.append("bad_pregame_ref:" + ref)
    for axis in ("WIN_LOSS", "RUN_TOTAL"):
        expected = str(game["final_state"][axis])
        if row[axis]["observed_direction"] != expected:
            errors.append(f"{axis}_direction:{row[axis]['observed_direction']}!={expected}")
        cause_ids = {item["cause_id"] for item in row[axis]["decisive_causes"]}
        for fact in row[axis]["preknowable_factors"]:
            if not set(fact["linked_cause_ids"]).issubset(cause_ids):
                errors.append(f"{axis}_bad_cause_link:{fact['fact_id']}")
        text = json.dumps(row[axis]["judgment_criterion"], ensure_ascii=False).lower()
        # Some player surnames are ordinary criterion verbs/nouns. Full-name and
        # uncommon-token leakage remains blocked; these generic words are not identity evidence.
        generic_identity_homonyms = {"early", "judge", "mark", "story", "will"}
        leaks = sorted(
            token for token in safe_identity_tokens(game)
            if token not in generic_identity_homonyms
            and __import__("re").search(rf"\b{__import__('re').escape(token)}\b", text)
        )
        if leaks:
            errors.append(f"{axis}_identity_leak:" + ",".join(leaks[:8]))
    forbidden = __import__("re").compile(r"(?i)\bhome[-\s]+role\b|\baway[-\s]+role\b|\bhome\s+team\b|\baway\s+team\b|\bfavorite\b|\bunderdog\b|홈팀|원정팀|정배|역배")
    for axis in ("WIN_LOSS", "RUN_TOTAL"):
        if forbidden.search(json.dumps(row[axis]["judgment_criterion"], ensure_ascii=False)):
            errors.append(f"{axis}_non_role_neutral_label")
    errors.extend(quality.ledger_semantic_errors(game, row))
    return sorted(set(errors))


def run_codex_bridge(prompt_path: Path, output_path: Path, game_pk: str, timeout: int) -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(ROOT / "_codex_exec_bridge.py"),
        "--req", str(prompt_path),
        "--out", str(output_path),
        "--model", "gpt-5.5",
        "--effort", "high",
        "--timeout", str(timeout),
        "--retries", "0",
        "--slot", f"season-criterion-{game_pk}",
        "--direct-pipe",
        "--ignore-user-config",
    ]
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        text=True,
        capture_output=True,
    )
    return {
        "ok": proc.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0,
        "returncode": proc.returncode,
        "stdout_tail": proc.stdout[-2000:],
        "stderr_tail": proc.stderr[-2000:],
    }


def chunk_games(chunk: int) -> list[dict[str, Any]]:
    return [game for game in manifest()["games"] if int(game["chunk"]) == chunk]


def run_one(game_meta: dict[str, Any], timeout: int, overwrite: bool) -> dict[str, Any]:
    game = build_game_source(game_meta)
    gp = str(game["gamePk"])
    prompt_path = BASE / "prompts" / f"{gp}.md"
    output_path = BASE / "outputs" / f"{gp}.json"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt(game), encoding="utf-8")
    if output_path.exists() and not overwrite:
        errors = validate(game, output_path)
        if not errors:
            return {"gamePk": gp, "ok": True, "reused": True, "errors": []}
    run = run_codex_bridge(prompt_path, output_path, gp, timeout)
    errors = validate(game, output_path) if run.get("ok") and output_path.exists() else ["bridge_failed"]
    report = {
        "gamePk": gp,
        "date": game["date"],
        "ok": not errors,
        "errors": errors,
        "run": run,
        "prompt_sha256": sha256(prompt_path),
        "output_sha256": sha256(output_path) if output_path.exists() else None,
    }
    write_json(BASE / "validation/games" / f"{gp}.json", report)
    return report


def run_chunk(chunk: int, workers: int, timeout: int, overwrite: bool) -> dict[str, Any]:
    selected = chunk_games(chunk)
    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(lambda game: run_one(game, timeout, overwrite), selected))
    passed_rows = []
    for game in selected:
        gp = str(game["gamePk"])
        output = BASE / "outputs" / f"{gp}.json"
        source = read_json(BASE / "shared_source" / gp / "source_record.json")
        errors = validate(source, output) if output.exists() else ["missing"]
        if not errors:
            passed_rows.append(quality.parse_jsonish(output))
    chunk_path = BASE / "chunks" / f"chunk_{chunk:03d}.jsonl"
    write_jsonl(chunk_path, passed_rows)
    report = {
        "schema": "mlb_direction_criterion_season_chunk_report.v1",
        "chunk": chunk,
        "expected": len(selected),
        "passed": len(passed_rows),
        "failed": len(selected) - len(passed_rows),
        "ok": len(passed_rows) == len(selected),
        "rows": rows,
        "chunk_path": str(chunk_path),
        "chunk_sha256": sha256(chunk_path),
    }
    write_json(BASE / "validation/chunks" / f"chunk_{chunk:03d}.json", report)
    return report


def repair_one(game_meta: dict[str, Any], timeout: int) -> dict[str, Any]:
    game = read_json(BASE / "shared_source" / str(game_meta["gamePk"]) / "source_record.json")
    gp = str(game["gamePk"])
    original_path = BASE / "outputs" / f"{gp}.json"
    original = quality.parse_jsonish(original_path)
    current_errors = validate(game, original_path)
    if not current_errors:
        return {"gamePk": gp, "ok": True, "reused": True, "errors": []}
    repair_prompt = BASE / "repair_prompts" / f"{gp}.md"
    candidate = BASE / "repair_candidates" / f"{gp}.json"
    repair_prompt.parent.mkdir(parents=True, exist_ok=True)
    repair_prompt.write_text(
        prompt(game)
        + "\n\n## Bounded repair instruction\n"
        + "The prior JSON below failed only the listed deterministic semantic gates. Return a complete replacement JSON. "
        + "Preserve every valid direction, game-flow fact, cause, and criterion meaning. Correct only the cited grounding defects: "
        + "a named player must have a retrospective ref whose summary contains that player; a realized bullpen/relief cause must either link to a true pregame bullpen/fatigue/player-card PB ref or be explicitly marked post-only execution with overlapping retrospective refs. "
        + "For RUN_TOTAL bilateral grounding errors, do not keep words such as both, bilateral, each side, or two-sided when the cited barrel/xwOBA/contact references ground only one side. Either cite same-mechanism pregame evidence for SIDE_A and SIDE_B, or rewrite that complete factor and its criterion linkage as a one-sided claim matching the available refs. "
        + "Do not invent evidence and do not weaken the detailed baseball mechanism.\n\n"
        + f"Errors: {json.dumps(current_errors, ensure_ascii=False)}\n\n"
        + f"Prior JSON: {json.dumps(original, ensure_ascii=False, separators=(',', ':'))}\n",
        encoding="utf-8",
    )
    run = run_codex_bridge(repair_prompt, candidate, gp + "-repair", timeout)
    errors = validate(game, candidate) if run.get("ok") and candidate.exists() else ["bridge_failed"]
    if not errors:
        candidate.replace(original_path)
    report = {
        "gamePk": gp,
        "ok": not errors,
        "errors_before": current_errors,
        "errors": errors,
        "run": run,
        "output_sha256": sha256(original_path) if original_path.exists() else None,
    }
    write_json(BASE / "validation/repairs" / f"{gp}.json", report)
    return report


def repair_chunk(chunk: int, workers: int, timeout: int) -> dict[str, Any]:
    selected = []
    for game in chunk_games(chunk):
        gp = str(game["gamePk"])
        source_path = BASE / "shared_source" / gp / "source_record.json"
        output_path = BASE / "outputs" / f"{gp}.json"
        if not source_path.exists() or not output_path.exists() or validate(read_json(source_path), output_path):
            selected.append(game)
    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(lambda game: repair_one(game, timeout), selected))
    passed_rows = []
    validation_rows = []
    for game in chunk_games(chunk):
        gp = str(game["gamePk"])
        source_path = BASE / "shared_source" / gp / "source_record.json"
        output_path = BASE / "outputs" / f"{gp}.json"
        errors = validate(read_json(source_path), output_path) if source_path.exists() and output_path.exists() else ["missing"]
        validation_rows.append({"gamePk": gp, "ok": not errors, "errors": errors})
        if not errors:
            passed_rows.append(quality.parse_jsonish(output_path))
    chunk_path = BASE / "chunks" / f"chunk_{chunk:03d}.jsonl"
    write_jsonl(chunk_path, passed_rows)
    report = {
        "schema": "mlb_direction_criterion_season_chunk_repair_report.v1",
        "chunk": chunk,
        "expected": len(chunk_games(chunk)),
        "passed": len(passed_rows),
        "failed": len(chunk_games(chunk)) - len(passed_rows),
        "ok": len(passed_rows) == len(chunk_games(chunk)),
        "repair_rows": rows,
        "validation_rows": validation_rows,
        "chunk_path": str(chunk_path),
        "chunk_sha256": sha256(chunk_path),
    }
    write_json(BASE / "validation/chunks" / f"chunk_{chunk:03d}.json", report)
    return report


def validate_all() -> dict[str, Any]:
    m = manifest()
    expected = {str(game["gamePk"]): game for game in m["games"]}
    rows: dict[str, dict[str, Any]] = {}
    duplicates = []
    for path in sorted((BASE / "chunks").glob("chunk_*.jsonl")):
        for row in [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]:
            gp = str(row.get("gamePk"))
            if gp in rows:
                duplicates.append(gp)
            rows[gp] = row
    missing = sorted(set(expected) - set(rows), key=int)
    extra = sorted(set(rows) - set(expected), key=int)
    payload = {
        "schema": "mlb_direction_criterion_season_validation.v1",
        "expected": len(expected),
        "rows": len(rows),
        "missing": missing,
        "extra": extra,
        "duplicates": sorted(set(duplicates), key=int),
        "excluded": m["excluded_games"],
        "ok": not missing and not extra and not duplicates,
    }
    write_json(BASE / "SEASON_VALIDATION.json", payload)
    if payload["ok"]:
        write_jsonl(BASE / "mlb_direction_criteria_2026.jsonl", [rows[gp] for gp in sorted(rows, key=int)])
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--season", type=int, default=2026)
    parser.add_argument("--chunk-size", type=int, default=20)
    parser.add_argument(
        "--preserve-prefix-manifest",
        type=Path,
        help="Preserve this manifest's gamePk order as the immutable chunk prefix and append only newly eligible games.",
    )
    parser.add_argument("--run-chunk", type=int)
    parser.add_argument("--repair-chunk", type=int)
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--validate", action="store_true")
    args = parser.parse_args()
    if args.prepare:
        prepared = prepare(args.season, args.chunk_size, args.preserve_prefix_manifest)
        print(json.dumps({key: prepared[key] for key in ("season", "packet_games", "eligible_games", "excluded_games", "chunk_count")}, ensure_ascii=False, indent=2))
    if args.run_chunk is not None:
        print(json.dumps(run_chunk(args.run_chunk, args.workers, args.timeout, args.overwrite), ensure_ascii=False, indent=2))
    if args.repair_chunk is not None:
        print(json.dumps(repair_chunk(args.repair_chunk, args.workers, args.timeout), ensure_ascii=False, indent=2))
    if args.validate:
        print(json.dumps(validate_all(), ensure_ascii=False, indent=2))
    if not any((args.prepare, args.run_chunk is not None, args.repair_chunk is not None, args.validate)):
        parser.error("choose --prepare, --run-chunk, or --validate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
