# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Build MLB fullread_v4 bullpen usage ledger from raw StatsAPI PBP.

Research-only. This creates pregame-knowable bullpen usage features for the
fullread_v4 universe. It does not touch production predictions, narrator files,
Notification, or live slate files.
"""
from __future__ import annotations

import argparse
import csv
import concurrent.futures as cf
import importlib.util
import json
import math
import os
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

import _mlb_raw_pbp_refresh as raw_pbp_refresh


ROOT = Path(__file__).resolve().parent
POST = ROOT / "postmortem" / "fullread_v4"
MANIFEST = POST / "mlb_fullread_v4_manifest.csv"
DEEP_REVIEW_SCRIPT = ROOT / "_mlb_win_loss_deep_case_review.py"

STINTS_CSV = POST / "mlb_fullread_v4_bullpen_stints.csv"
TEAM_USAGE_CSV = POST / "mlb_fullread_v4_bullpen_team_game_usage.csv"
PREGAME_CSV = POST / "mlb_fullread_v4_bullpen_pregame_features.csv"
MISSING_CSV = POST / "mlb_fullread_v4_bullpen_missing_report.csv"
REPORT_JSON = POST / "mlb_fullread_v4_bullpen_ledger_report.json"
REPORT_MD = POST / "mlb_fullread_v4_bullpen_ledger_report.md"
CHUNK_DIR = POST / "bullpen_chunks"
PARTIAL_RESULTS = ROOT / "postmortem" / "partial_results"

BAD = {"", "nan", "none", "null", "na", "n/a"}


def load_deep_review_module():
    spec = importlib.util.spec_from_file_location("deep_case_review", DEEP_REVIEW_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {DEEP_REVIEW_SCRIPT}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def clean(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    return "" if s.lower() in BAD else s


def safe_int(value: Any, default: int | None = None) -> int | None:
    try:
        if value is None or str(value).strip().lower() in BAD:
            return default
        x = int(float(value))
        return x
    except Exception:
        return default


def safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or str(value).strip().lower() in BAD:
            return default
        x = float(value)
        return default if math.isnan(x) else x
    except Exception:
        return default


def parse_date(value: Any) -> date:
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def date_iso(value: str) -> str:
    raw = value.strip().replace("-", "")
    if len(raw) != 8 or not raw.isdigit():
        raise SystemExit(f"[ERR] date format invalid: {value}")
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def is_pa(play: dict[str, Any]) -> bool:
    about = play.get("about") or {}
    result = play.get("result") or {}
    return "atBatIndex" in about or bool(result.get("event") or result.get("eventType"))


def pitch_count(play: dict[str, Any]) -> int:
    events = play.get("playEvents") or []
    count = 0
    for event in events:
        details = event.get("details") or {}
        if event.get("isPitch") is True or details.get("isPitch") is True or clean(event.get("type")) == "pitch":
            count += 1
    if count:
        return count
    idx = play.get("pitchIndex") or []
    return len(idx) if isinstance(idx, list) else 0


def pitcher_info(play: dict[str, Any]) -> tuple[str, str]:
    matchup = play.get("matchup") or {}
    pitcher = matchup.get("pitcher") or {}
    pid = clean(pitcher.get("id"))
    name = clean(pitcher.get("fullName"))
    if not pid and name:
        pid = f"name:{name.lower()}"
    return pid, name


def result_scores(play: dict[str, Any], prev_away: int, prev_home: int) -> tuple[int, int]:
    result = play.get("result") or {}
    away = safe_int(result.get("awayScore"), prev_away)
    home = safe_int(result.get("homeScore"), prev_home)
    return away if away is not None else prev_away, home if home is not None else prev_home


def half_and_inning(play: dict[str, Any]) -> tuple[str, int]:
    about = play.get("about") or {}
    half = clean(about.get("halfInning")).lower()
    inning = safe_int(about.get("inning"), 0) or 0
    return half, inning


def parse_game_stints(row: dict[str, Any], raw: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    game_pk = clean(row.get("gamePk"))
    game_date = clean(row.get("date"))
    season = safe_int(row.get("season"), 0) or 0
    away = clean(row.get("away"))
    home = clean(row.get("home"))
    plays = [p for p in (raw.get("allPlays") or []) if is_pa(p)]
    if not plays:
        return [], {"season": season, "date": game_date, "gamePk": game_pk, "missing": "no_pa_plays"}

    segments: list[dict[str, Any]] = []
    starters: dict[str, str] = {}
    current: dict[str, Any] | None = None
    away_score = home_score = 0

    def close_segment(exit_away: int, exit_home: int) -> None:
        nonlocal current
        if current is None:
            return
        current["exit_away_score"] = exit_away
        current["exit_home_score"] = exit_home
        if current["team_side"] == "home":
            runs = exit_away - current["entry_away_score"]
            margin = current["entry_home_score"] - current["entry_away_score"]
        else:
            runs = exit_home - current["entry_home_score"]
            margin = current["entry_away_score"] - current["entry_home_score"]
        current["entry_margin"] = margin
        current["runs_allowed"] = max(0, runs)
        current["entered_lead"] = margin > 0
        current["entered_tie"] = margin == 0
        current["entered_trail"] = margin < 0
        current["late"] = int(current["entry_inning"]) >= 7
        current["blown_lead"] = bool(margin > 0 and current["runs_allowed"] >= margin)
        current["went_behind"] = bool(margin > 0 and (margin - current["runs_allowed"]) < 0)
        current["long_relief"] = bool(current["pitch_count"] >= 30 or current["pa"] >= 8)
        segments.append(current)
        current = None

    for idx, play in enumerate(plays):
        half, inning = half_and_inning(play)
        if half not in {"top", "bottom"}:
            continue
        team_side = "home" if half == "top" else "away"
        team = home if team_side == "home" else away
        opp = away if team_side == "home" else home
        pid, pname = pitcher_info(play)
        if not pid:
            continue
        if team_side not in starters:
            starters[team_side] = pid
        is_reliever = pid != starters[team_side]
        if current is None or current["pitcher_id"] != pid or current["team_side"] != team_side:
            close_segment(away_score, home_score)
            current = {
                "season": season,
                "date": game_date,
                "gamePk": game_pk,
                "team": team,
                "opp": opp,
                "team_side": team_side,
                "pitcher_id": pid,
                "pitcher_name": pname,
                "is_reliever": bool(is_reliever),
                "stint_order": 0,
                "entry_inning": inning,
                "entry_half": half,
                "entry_pa_index": idx,
                "entry_away_score": away_score,
                "entry_home_score": home_score,
                "pa": 0,
                "pitch_count": 0,
                "strikeouts": 0,
                "walk_hbp": 0,
                "home_runs": 0,
            }
        current["pa"] += 1
        current["pitch_count"] += pitch_count(play)
        event_type = clean((play.get("result") or {}).get("eventType")).lower()
        if event_type == "strikeout":
            current["strikeouts"] += 1
        if event_type in {"walk", "hit_by_pitch"}:
            current["walk_hbp"] += 1
        if event_type == "home_run":
            current["home_runs"] += 1
        away_score, home_score = result_scores(play, away_score, home_score)

    close_segment(away_score, home_score)

    orders: Counter[str] = Counter()
    for seg in segments:
        key = str(seg["team_side"])
        orders[key] += 1
        seg["stint_order"] = orders[key]
        seg["role"] = "RP" if seg["is_reliever"] else "SP"
    return segments, None


def load_manifest() -> pd.DataFrame:
    if not MANIFEST.exists():
        raise FileNotFoundError(f"missing manifest: {MANIFEST}")
    df = pd.read_csv(MANIFEST, dtype={"gamePk": str})
    df["date"] = df["date"].astype(str).str[:10]
    df["gamePk"] = df["gamePk"].astype(str).str.split(".").str[0]
    return df


def partial_final_gamepks(target: str) -> set[str]:
    path = PARTIAL_RESULTS / f"{target.replace('-', '')}_mlb_partial_status.json"
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        vals = data.get("final_game_ids") or []
        return {str(v).strip() for v in vals if str(v).strip()}
    except Exception:
        return set()


def v3_patch_path(target: str) -> Path:
    override = clean(os.environ.get("MLB_REPLAY_V3_INPUT_PATH"))
    if not override:
        return ROOT / f"_v3_games_{target}.csv"
    path = Path(override).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"missing MLB_REPLAY_V3_INPUT_PATH: {path}")
    expected_name = f"_v3_games_{target}.csv"
    if path.name != expected_name:
        raise RuntimeError(
            f"replay v3 filename mismatch: {path.name} != {expected_name}"
        )
    return path


def load_v3_date_patch_targets(target: str, known_gamepks: set[str]) -> pd.DataFrame:
    path = v3_patch_path(target)
    if not path.exists():
        return pd.DataFrame()
    try:
        slate = pd.read_csv(path, dtype={"gamePk": str})
    except Exception:
        return pd.DataFrame()
    final_gamepks = partial_final_gamepks(target)
    rows: list[dict[str, Any]] = []
    for row in slate.to_dict("records"):
        game_pk = clean(row.get("gamePk")).split(".")[0]
        if not game_pk or game_pk in known_gamepks:
            continue
        if final_gamepks and game_pk not in final_gamepks:
            continue
        away = clean(row.get("away_team") or row.get("away"))
        home = clean(row.get("home_team") or row.get("home"))
        if not away or not home:
            continue
        rows.append({
            "schema": "mlb_fullread_v4_live_v3_patch.v1",
            "season": safe_int(target[:4], 0) or 0,
            "date": target,
            "gamePk": game_pk,
            "matchup": f"{away} @ {home}",
            "away": away,
            "home": home,
            "home_sp_name": clean(row.get("home_sp_name")),
            "away_sp_name": clean(row.get("away_sp_name")),
        })
    return pd.DataFrame(rows)


def load_raw_date_patch_targets(target: str, known_gamepks: set[str]) -> pd.DataFrame:
    """Recover postgame identity from official raw PBP when manifests lag.

    This path is intentionally limited to already-downloaded target-date raw
    files and reads only gamePk/team identity needed by the stint parser.
    """
    raw_dir = ROOT / "_deepread_v3" / "raw_plays" / target
    if not raw_dir.is_dir():
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for path in sorted(raw_dir.glob("*.json")):
        game_pk = path.stem.strip()
        if not game_pk or game_pk in known_gamepks:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        teams = ((payload.get("gameData") or {}).get("teams") or {})
        away = clean((teams.get("away") or {}).get("name"))
        home = clean((teams.get("home") or {}).get("name"))
        if not away or not home:
            continue
        rows.append(
            {
                "schema": "mlb_fullread_v4_raw_pbp_date_patch.v1",
                "season": safe_int(target[:4], 0) or 0,
                "date": target,
                "gamePk": game_pk,
                "matchup": f"{away} @ {home}",
                "away": away,
                "home": home,
                "home_sp_name": "",
                "away_sp_name": "",
            }
        )
        known_gamepks.add(game_pk)
    return pd.DataFrame(rows)


def load_official_date_patch_targets(
    target: str, known_gamepks: set[str]
) -> pd.DataFrame:
    """Use only official Final game identity when local manifests have no teams."""
    rows: list[dict[str, Any]] = []
    for game in raw_pbp_refresh.load_official_final_games(target, timeout=30):
        game_pk = clean(game.get("gamePk"))
        if not game_pk or game_pk in known_gamepks:
            continue
        away = clean(game.get("away"))
        home = clean(game.get("home"))
        if not away or not home:
            continue
        rows.append(
            {
                "schema": "mlb_fullread_v4_official_schedule_date_patch.v1",
                "season": safe_int(target[:4], 0) or 0,
                "date": target,
                "gamePk": game_pk,
                "matchup": f"{away} @ {home}",
                "away": away,
                "home": home,
                "home_sp_name": "",
                "away_sp_name": "",
            }
        )
        known_gamepks.add(game_pk)
    return pd.DataFrame(rows)


def load_v3_pregame_targets(manifest: pd.DataFrame) -> pd.DataFrame:
    """Add live slate rows missing from fullread manifest as pregame targets only."""
    known_gamepks = set(manifest["gamePk"].astype(str))
    rows: list[dict[str, Any]] = []
    for path in sorted(ROOT.glob("_v3_games_*.csv")):
        try:
            slate = pd.read_csv(path, dtype={"gamePk": str})
        except Exception:
            continue
        for row in slate.to_dict("records"):
            game_pk = clean(row.get("gamePk")).split(".")[0]
            if not game_pk or game_pk in known_gamepks:
                continue
            game_date = clean(row.get("date")) or path.stem.replace("_v3_games_", "")
            game_date = game_date[:10]
            away = clean(row.get("away") or row.get("away_team"))
            home = clean(row.get("home") or row.get("home_team"))
            prediction = clean(row.get("win_loss_prediction") or row.get("win_prediction"))
            if not prediction:
                side = clean(row.get("win_side") or row.get("win_loss_prediction_side")).lower()
                if side in {"home", "h"} or "홈" in side:
                    prediction = home
                elif side in {"away", "a"} or "원정" in side:
                    prediction = away
            rows.append(
                {
                    "season": safe_int(game_date[:4], 0) or 0,
                    "date": game_date,
                    "gamePk": game_pk,
                    "matchup": f"{away} @ {home}" if away and home else clean(row.get("matchup")),
                    "away": away,
                    "home": home,
                    "win_loss_prediction": prediction,
                    "winner": "",
                    "win_loss_hit": "",
                    "source_v3": path.name,
                }
            )
            known_gamepks.add(game_pk)
    if not rows:
        return manifest.copy()
    return pd.concat([manifest, pd.DataFrame(rows)], ignore_index=True, sort=False)


def build_stints_for_row(row: dict[str, Any], deep_mod) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    season = safe_int(row.get("season"), 0) or 0
    game_pk = clean(row.get("gamePk"))
    game_date = clean(row.get("date"))
    raw_path = deep_mod.find_raw_path(season, game_date, game_pk)
    if raw_path is None:
        return [], {"season": season, "date": game_date, "gamePk": game_pk, "missing": "raw_pbp_missing"}
    try:
        raw = json.loads(Path(raw_path).read_text(encoding="utf-8"))
        rows, miss = parse_game_stints(row, raw)
        return rows, miss
    except Exception as exc:
        return [], {"season": season, "date": game_date, "gamePk": game_pk, "missing": "raw_parse_failed", "detail": repr(exc)}


def build_stints(manifest: pd.DataFrame, deep_mod, workers: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    stints: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    rows = manifest.to_dict("records")
    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = [ex.submit(build_stints_for_row, row, deep_mod) for row in rows]
        for fut in cf.as_completed(futures):
            rows_out, miss = fut.result()
            stints.extend(rows_out)
            if miss:
                missing.append(miss)
    stints.sort(key=lambda r: (str(r.get("date")), str(r.get("gamePk")), str(r.get("team_side")), int(r.get("stint_order", 0))))
    missing.sort(key=lambda r: (str(r.get("date")), str(r.get("gamePk")), str(r.get("missing"))))
    return stints, missing


def build_stints_sequential_reference(manifest: pd.DataFrame, deep_mod) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    stints: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for row in manifest.to_dict("records"):
        season = safe_int(row.get("season"), 0) or 0
        game_pk = clean(row.get("gamePk"))
        game_date = clean(row.get("date"))
        raw_path = deep_mod.find_raw_path(season, game_date, game_pk)
        if raw_path is None:
            missing.append({"season": season, "date": game_date, "gamePk": game_pk, "missing": "raw_pbp_missing"})
            continue
        try:
            raw = json.loads(Path(raw_path).read_text(encoding="utf-8"))
            rows, miss = parse_game_stints(row, raw)
            stints.extend(rows)
            if miss:
                missing.append(miss)
        except Exception as exc:
            missing.append({"season": season, "date": game_date, "gamePk": game_pk, "missing": "raw_parse_failed", "detail": repr(exc)})
    return stints, missing


def team_game_usage(stints: list[dict[str, Any]], manifest: pd.DataFrame) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for stint in stints:
        by_key[(str(stint["gamePk"]), str(stint["team"]))].append(stint)

    rows: list[dict[str, Any]] = []
    for row in manifest.to_dict("records"):
        game_pk = clean(row.get("gamePk"))
        for team_side, team, opp in (
            ("away", clean(row.get("away")), clean(row.get("home"))),
            ("home", clean(row.get("home")), clean(row.get("away"))),
        ):
            items = by_key.get((game_pk, team), [])
            relievers = [x for x in items if as_bool(x.get("is_reliever"))]
            sp_items = [x for x in items if not as_bool(x.get("is_reliever"))]
            pitcher_ids = [str(x.get("pitcher_id")) for x in relievers]
            rows.append(
                {
                    "season": safe_int(row.get("season"), 0) or 0,
                    "date": clean(row.get("date")),
                    "gamePk": game_pk,
                    "team": team,
                    "opp": opp,
                    "team_side": team_side,
                    "reliever_count": len(set(pitcher_ids)),
                    "relief_pa": sum(safe_int(x.get("pa"), 0) or 0 for x in relievers),
                    "relief_pitches": sum(safe_int(x.get("pitch_count"), 0) or 0 for x in relievers),
                    "relief_runs_allowed": sum(safe_int(x.get("runs_allowed"), 0) or 0 for x in relievers),
                    "late_relief_pitches": sum(safe_int(x.get("pitch_count"), 0) or 0 for x in relievers if as_bool(x.get("late"))),
                    "long_relief_stints": sum(1 for x in relievers if as_bool(x.get("long_relief"))),
                    "blown_leads": sum(1 for x in relievers if as_bool(x.get("blown_lead"))),
                    "went_behind": sum(1 for x in relievers if as_bool(x.get("went_behind"))),
                    "relief_walk_hbp": sum(safe_int(x.get("walk_hbp"), 0) or 0 for x in relievers),
                    "relief_home_runs": sum(safe_int(x.get("home_runs"), 0) or 0 for x in relievers),
                    "starter_pitches": sum(safe_int(x.get("pitch_count"), 0) or 0 for x in sp_items),
                    "starter_pa": sum(safe_int(x.get("pa"), 0) or 0 for x in sp_items),
                    "reliever_ids": ";".join(sorted(set(pitcher_ids))),
                    "reliever_names": ";".join(sorted(set(clean(x.get("pitcher_name")) for x in relievers if clean(x.get("pitcher_name"))))),
                }
            )
    return rows


def augment_manifest_with_stints(
    manifest: pd.DataFrame, stints: list[dict[str, Any]]
) -> pd.DataFrame:
    """Recover lagging game identities from already parsed stint rows."""
    known = {clean(v).split(".")[0] for v in manifest["gamePk"].astype(str)}
    games: dict[str, dict[str, Any]] = {}
    for stint in stints:
        game_pk = clean(stint.get("gamePk")).split(".")[0]
        if not game_pk or game_pk in known:
            continue
        side = clean(stint.get("team_side")).lower()
        team = clean(stint.get("team"))
        opp = clean(stint.get("opp"))
        if side not in {"away", "home"} or not team or not opp:
            continue
        row = games.setdefault(
            game_pk,
            {
                "schema": "mlb_fullread_v4_stint_identity_patch.v1",
                "season": safe_int(stint.get("season"), 0) or 0,
                "date": clean(stint.get("date"))[:10],
                "gamePk": game_pk,
                "away": "",
                "home": "",
                "win_loss_prediction": "",
                "winner": "",
                "win_loss_hit": "",
            },
        )
        if side == "away":
            row["away"] = team
            row["home"] = opp
        else:
            row["home"] = team
            row["away"] = opp

    rows: list[dict[str, Any]] = []
    for row in games.values():
        if not row["away"] or not row["home"] or not row["date"]:
            continue
        row["matchup"] = f'{row["away"]} @ {row["home"]}'
        rows.append(row)
    if not rows:
        return manifest.copy()
    out = pd.concat([manifest, pd.DataFrame(rows)], ignore_index=True, sort=False)
    out["gamePk"] = out["gamePk"].astype(str).str.split(".").str[0]
    out["date"] = out["date"].astype(str).str[:10]
    return out.sort_values(["date", "gamePk"]).reset_index(drop=True)


def bucket_pitches(value: int) -> str:
    if value <= 0:
        return "0"
    if value <= 30:
        return "1-30"
    if value <= 60:
        return "31-60"
    if value <= 90:
        return "61-90"
    return "90+"


def bucket_count(value: int) -> str:
    if value <= 0:
        return "0"
    if value == 1:
        return "1"
    if value == 2:
        return "2"
    return "3+"


def recent_team_features(team: str, target_date: date, usage_by_team: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    rows = [r for r in usage_by_team.get(team, []) if parse_date(r["date"]) < target_date]
    windows = {
        "1d": [r for r in rows if parse_date(r["date"]) >= target_date - timedelta(days=1)],
        "2d": [r for r in rows if parse_date(r["date"]) >= target_date - timedelta(days=2)],
        "3d": [r for r in rows if parse_date(r["date"]) >= target_date - timedelta(days=3)],
        "7d": [r for r in rows if parse_date(r["date"]) >= target_date - timedelta(days=7)],
    }
    previous = max(rows, key=lambda r: (r["date"], str(r["gamePk"]))) if rows else None

    def sum_field(win: str, field: str) -> int:
        return sum(safe_int(r.get(field), 0) or 0 for r in windows[win])

    pitcher_day: dict[str, set[str]] = defaultdict(set)
    for r in windows["3d"]:
        for pid in clean(r.get("reliever_ids")).split(";"):
            if pid:
                pitcher_day[pid].add(str(r["date"]))
    b2b = sum(1 for days in pitcher_day.values() if len(days) >= 2)
    three_in_3 = sum(1 for days in pitcher_day.values() if len(days) >= 3)

    out = {
        "bp_games_1d": len(windows["1d"]),
        "bp_games_2d": len(windows["2d"]),
        "bp_games_3d": len(windows["3d"]),
        "bp_relief_pitches_1d": sum_field("1d", "relief_pitches"),
        "bp_relief_pitches_2d": sum_field("2d", "relief_pitches"),
        "bp_relief_pitches_3d": sum_field("3d", "relief_pitches"),
        "bp_late_pitches_2d": sum_field("2d", "late_relief_pitches"),
        "bp_long_relief_3d": sum_field("3d", "long_relief_stints"),
        "bp_blown_leads_7d": sum_field("7d", "blown_leads"),
        "bp_runs_allowed_3d": sum_field("3d", "relief_runs_allowed"),
        "bp_walk_hbp_3d": sum_field("3d", "relief_walk_hbp"),
        "bp_home_runs_3d": sum_field("3d", "relief_home_runs"),
        "bp_b2b_relievers_3d": b2b,
        "bp_3in3_relievers_3d": three_in_3,
        "bp_prev_game_relief_pitches": safe_int(previous.get("relief_pitches"), 0) if previous else 0,
        "bp_prev_game_reliever_count": safe_int(previous.get("reliever_count"), 0) if previous else 0,
        "bp_prev_game_runs_allowed": safe_int(previous.get("relief_runs_allowed"), 0) if previous else 0,
        "bp_prev_game_blown_leads": safe_int(previous.get("blown_leads"), 0) if previous else 0,
        "bp_prev_game_date": previous.get("date") if previous else "",
        "bp_prev_gamePk": previous.get("gamePk") if previous else "",
    }
    out["bp_1d_pitch_bucket"] = bucket_pitches(int(out["bp_relief_pitches_1d"]))
    out["bp_3d_pitch_bucket"] = bucket_pitches(int(out["bp_relief_pitches_3d"]))
    out["bp_b2b_bucket"] = bucket_count(int(out["bp_b2b_relievers_3d"]))
    out["bp_long_relief_bucket"] = bucket_count(int(out["bp_long_relief_3d"]))
    out["bp_blown_7d_bucket"] = bucket_count(int(out["bp_blown_leads_7d"]))
    return out


def pregame_features(manifest: pd.DataFrame, usage_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    usage_by_team: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in usage_rows:
        usage_by_team[str(row["team"])].append(row)
    for rows in usage_by_team.values():
        rows.sort(key=lambda r: (str(r["date"]), str(r["gamePk"])))

    out: list[dict[str, Any]] = []
    for row in manifest.to_dict("records"):
        target = parse_date(row["date"])
        prediction = clean(row.get("win_loss_prediction"))
        away = clean(row.get("away"))
        home = clean(row.get("home"))
        opp = home if prediction == away else away if prediction == home else ""
        prediction_feat = recent_team_features(prediction, target, usage_by_team) if prediction else {}
        opp_feat = recent_team_features(opp, target, usage_by_team) if opp else {}

        def pf(key: str) -> int:
            return int(prediction_feat.get(key, 0) or 0)

        def of(key: str) -> int:
            return int(opp_feat.get(key, 0) or 0)

        out.append(
            {
                "season": safe_int(row.get("season"), 0) or 0,
                "date": clean(row.get("date")),
                "gamePk": clean(row.get("gamePk")),
                "matchup": clean(row.get("matchup")),
                "win_loss_prediction": prediction,
                "opp": opp,
                "winner": clean(row.get("winner")),
                "win_loss_hit": safe_int(row.get("win_loss_hit"), 0) or 0,
                "prediction_bp_relief_pitches_1d": pf("bp_relief_pitches_1d"),
                "opp_bp_relief_pitches_1d": of("bp_relief_pitches_1d"),
                "prediction_bp_relief_pitches_3d": pf("bp_relief_pitches_3d"),
                "opp_bp_relief_pitches_3d": of("bp_relief_pitches_3d"),
                "prediction_bp_late_pitches_2d": pf("bp_late_pitches_2d"),
                "opp_bp_late_pitches_2d": of("bp_late_pitches_2d"),
                "prediction_bp_b2b_relievers_3d": pf("bp_b2b_relievers_3d"),
                "opp_bp_b2b_relievers_3d": of("bp_b2b_relievers_3d"),
                "prediction_bp_long_relief_3d": pf("bp_long_relief_3d"),
                "opp_bp_long_relief_3d": of("bp_long_relief_3d"),
                "prediction_bp_blown_leads_7d": pf("bp_blown_leads_7d"),
                "opp_bp_blown_leads_7d": of("bp_blown_leads_7d"),
                "prediction_bp_runs_allowed_3d": pf("bp_runs_allowed_3d"),
                "opp_bp_runs_allowed_3d": of("bp_runs_allowed_3d"),
                "prediction_bp_prev_game_relief_pitches": pf("bp_prev_game_relief_pitches"),
                "opp_bp_prev_game_relief_pitches": of("bp_prev_game_relief_pitches"),
                "prediction_bp_prev_game_runs_allowed": pf("bp_prev_game_runs_allowed"),
                "opp_bp_prev_game_runs_allowed": of("bp_prev_game_runs_allowed"),
                "prediction_bp_prev_game_blown_leads": pf("bp_prev_game_blown_leads"),
                "opp_bp_prev_game_blown_leads": of("bp_prev_game_blown_leads"),
                "prediction_bp_1d_pitch_bucket": prediction_feat.get("bp_1d_pitch_bucket", "0"),
                "opp_bp_1d_pitch_bucket": opp_feat.get("bp_1d_pitch_bucket", "0"),
                "prediction_bp_3d_pitch_bucket": prediction_feat.get("bp_3d_pitch_bucket", "0"),
                "opp_bp_3d_pitch_bucket": opp_feat.get("bp_3d_pitch_bucket", "0"),
                "prediction_bp_b2b_bucket": prediction_feat.get("bp_b2b_bucket", "0"),
                "opp_bp_b2b_bucket": opp_feat.get("bp_b2b_bucket", "0"),
                "prediction_bp_long_relief_bucket": prediction_feat.get("bp_long_relief_bucket", "0"),
                "opp_bp_long_relief_bucket": opp_feat.get("bp_long_relief_bucket", "0"),
                "prediction_bp_blown_7d_bucket": prediction_feat.get("bp_blown_7d_bucket", "0"),
                "opp_bp_blown_7d_bucket": opp_feat.get("bp_blown_7d_bucket", "0"),
                "bp_pitches_1d_diff_prediction_minus_opp": pf("bp_relief_pitches_1d") - of("bp_relief_pitches_1d"),
                "bp_pitches_3d_diff_prediction_minus_opp": pf("bp_relief_pitches_3d") - of("bp_relief_pitches_3d"),
                "bp_b2b_diff_prediction_minus_opp": pf("bp_b2b_relievers_3d") - of("bp_b2b_relievers_3d"),
                "bp_blown_7d_diff_prediction_minus_opp": pf("bp_blown_leads_7d") - of("bp_blown_leads_7d"),
                "prediction_bp_prev_game_date": prediction_feat.get("bp_prev_game_date", ""),
                "opp_bp_prev_game_date": opp_feat.get("bp_prev_game_date", ""),
                "prediction_bp_prev_gamePk": prediction_feat.get("bp_prev_gamePk", ""),
                "opp_bp_prev_gamePk": opp_feat.get("bp_prev_gamePk", ""),
                "pre_knowable_ok": True,
            }
        )
    return out


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({k for row in rows for k in row})
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def markdown_report(report: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# MLB fullread_v4 bullpen ledger",
            "",
            "Research-only bullpen usage ledger built from raw StatsAPI PBP. No production predictions, narrator files, or Notification were changed.",
            "",
            "## Coverage",
            "",
            f"- manifest games: `{report['manifest_games']:,}`",
            f"- raw parsed games: `{report['raw_parsed_games']:,}`",
            f"- missing games: `{report['missing_games']:,}`",
            f"- stints: `{report['stints']:,}`",
            f"- reliever stints: `{report['reliever_stints']:,}`",
            f"- team-game usage rows: `{report['team_usage_rows']:,}`",
            f"- pregame feature rows: `{report['pregame_feature_rows']:,}`",
            "",
            "## Feature Families",
            "",
            "- Previous 1/2/3 day relief pitch load.",
            "- Back-to-back and 3-in-3 reliever appearances from prior days.",
            "- Long relief usage from prior 3 days.",
            "- Previous 7 day bullpen blown leads.",
            "- Previous game bullpen pitches, runs allowed, and blown leads.",
            "- Prediction side minus opponent bullpen load diffs.",
            "",
            "## Guardrails",
            "",
            "- These are pregame-known usage features only: all windows use `date < target`.",
            "- No thresholds are production rules here; buckets are descriptive audit bins.",
            "- This fixes the fullread_v4 bullpen coverage gap for auditing, but GPT fullread prompts must be regenerated/rerun if we want the LLM prose itself to explicitly reason over bullpen.",
            "",
            "## Output Files",
            "",
            f"- `{STINTS_CSV.relative_to(ROOT)}`",
            f"- `{TEAM_USAGE_CSV.relative_to(ROOT)}`",
            f"- `{PREGAME_CSV.relative_to(ROOT)}`",
            f"- `{MISSING_CSV.relative_to(ROOT)}`",
            f"- `{REPORT_JSON.relative_to(ROOT)}`",
            "",
        ]
    )


def build(write: bool, dry_run: bool, workers: int) -> dict[str, Any]:
    manifest = load_manifest()
    pregame_targets = load_v3_pregame_targets(manifest)
    deep_mod = load_deep_review_module()
    stints, missing = build_stints(manifest, deep_mod, workers=workers)
    usage = team_game_usage(stints, manifest)
    pregame = pregame_features(pregame_targets, usage)

    parsed_games = len({str(s["gamePk"]) for s in stints})
    reliever_stints = sum(1 for s in stints if bool(s.get("is_reliever")))
    report = {
        "ok": True,
        "manifest_games": int(manifest["gamePk"].nunique()),
        "raw_parsed_games": parsed_games,
        "missing_games": len({str(m.get("gamePk")) for m in missing}),
        "stints": len(stints),
        "reliever_stints": reliever_stints,
        "team_usage_rows": len(usage),
        "pregame_feature_rows": len(pregame),
        "pregame_target_rows": int(pregame_targets["gamePk"].nunique()),
        "pregame_v3_extra_rows": int(pregame_targets["gamePk"].nunique() - manifest["gamePk"].nunique()),
        "missing_counts": dict(Counter(str(m.get("missing")) for m in missing)),
        "by_season": manifest.groupby("season").size().to_dict(),
        "write": write,
        "dry_run": dry_run,
        "workers": workers,
        "outputs": {
            "stints": str(STINTS_CSV),
            "team_usage": str(TEAM_USAGE_CSV),
            "pregame": str(PREGAME_CSV),
            "missing": str(MISSING_CSV),
            "json": str(REPORT_JSON),
            "markdown": str(REPORT_MD),
        },
    }
    if write and not dry_run:
        write_csv(STINTS_CSV, stints)
        write_csv(TEAM_USAGE_CSV, usage)
        write_csv(PREGAME_CSV, pregame)
        write_csv(MISSING_CSV, missing)
        REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        REPORT_MD.write_text(markdown_report(report), encoding="utf-8")
    return report


def chunk_paths(chunk_idx: int) -> dict[str, Path]:
    return {
        "stints": CHUNK_DIR / f"stints_chunk_{chunk_idx:03d}.csv",
        "missing": CHUNK_DIR / f"missing_chunk_{chunk_idx:03d}.csv",
        "report": CHUNK_DIR / f"report_chunk_{chunk_idx:03d}.json",
    }


def build_chunk_range(write: bool, dry_run: bool, workers: int, chunk_start: int, chunk_end: int, chunk_size: int) -> dict[str, Any]:
    manifest = load_manifest()
    deep_mod = load_deep_review_module()
    chunk_count = math.ceil(len(manifest) / chunk_size)
    if chunk_start < 0 or chunk_end < chunk_start or chunk_end >= chunk_count:
        raise ValueError(f"bad chunk range {chunk_start}..{chunk_end}; available 0..{chunk_count - 1}")

    CHUNK_DIR.mkdir(parents=True, exist_ok=True)
    reports: list[dict[str, Any]] = []
    for chunk_idx in range(chunk_start, chunk_end + 1):
        start = chunk_idx * chunk_size
        end = min(start + chunk_size, len(manifest))
        subset = manifest.iloc[start:end].copy()
        stints, missing = build_stints(subset, deep_mod, workers=workers)
        parsed_games = len({str(s["gamePk"]) for s in stints})
        report = {
            "ok": True,
            "chunk": chunk_idx,
            "chunk_size": chunk_size,
            "start_row": start,
            "end_row_exclusive": end,
            "manifest_games": int(subset["gamePk"].nunique()),
            "raw_parsed_games": parsed_games,
            "missing_games": len({str(m.get("gamePk")) for m in missing}),
            "stints": len(stints),
            "reliever_stints": sum(1 for s in stints if bool(s.get("is_reliever"))),
            "write": write,
            "dry_run": dry_run,
            "workers": workers,
        }
        if write and not dry_run:
            p = chunk_paths(chunk_idx)
            write_csv(p["stints"], stints)
            write_csv(p["missing"], missing)
            p["report"].write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        reports.append(report)
        print(json.dumps(report, ensure_ascii=False))

    return {
        "ok": True,
        "mode": "chunk_range",
        "chunk_start": chunk_start,
        "chunk_end": chunk_end,
        "chunk_size": chunk_size,
        "chunks": len(reports),
        "manifest_games": sum(int(r["manifest_games"]) for r in reports),
        "raw_parsed_games": sum(int(r["raw_parsed_games"]) for r in reports),
        "missing_games": sum(int(r["missing_games"]) for r in reports),
        "stints": sum(int(r["stints"]) for r in reports),
        "reliever_stints": sum(int(r["reliever_stints"]) for r in reports),
        "write": write,
        "dry_run": dry_run,
        "workers": workers,
    }


def merge_chunk_outputs(write: bool, dry_run: bool) -> dict[str, Any]:
    manifest = load_manifest()
    pregame_targets = load_v3_pregame_targets(manifest)
    stints: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    stint_files = sorted(CHUNK_DIR.glob("stints_chunk_*.csv"))
    missing_files = sorted(CHUNK_DIR.glob("missing_chunk_*.csv"))
    for path in stint_files:
        stints.extend(read_csv(path))
    for path in missing_files:
        missing.extend(read_csv(path))

    stints.sort(key=lambda r: (str(r.get("date")), str(r.get("gamePk")), str(r.get("team_side")), int(float(r.get("stint_order", 0) or 0))))
    missing.sort(key=lambda r: (str(r.get("date")), str(r.get("gamePk")), str(r.get("missing"))))

    usage = team_game_usage(stints, manifest)
    pregame = pregame_features(pregame_targets, usage)

    parsed_games = len({str(s["gamePk"]) for s in stints})
    reliever_stints = sum(1 for s in stints if str(s.get("is_reliever")).lower() in {"true", "1"})
    report = {
        "ok": True,
        "mode": "merge_chunks",
        "manifest_games": int(manifest["gamePk"].nunique()),
        "raw_parsed_games": parsed_games,
        "missing_games": len({str(m.get("gamePk")) for m in missing if clean(m.get("gamePk"))}),
        "stints": len(stints),
        "reliever_stints": reliever_stints,
        "team_usage_rows": len(usage),
        "pregame_feature_rows": len(pregame),
        "pregame_target_rows": int(pregame_targets["gamePk"].nunique()),
        "pregame_v3_extra_rows": int(pregame_targets["gamePk"].nunique() - manifest["gamePk"].nunique()),
        "missing_counts": dict(Counter(str(m.get("missing")) for m in missing if clean(m.get("missing")))),
        "by_season": manifest.groupby("season").size().to_dict(),
        "chunk_stint_files": len(stint_files),
        "chunk_missing_files": len(missing_files),
        "write": write,
        "dry_run": dry_run,
        "outputs": {
            "stints": str(STINTS_CSV),
            "team_usage": str(TEAM_USAGE_CSV),
            "pregame": str(PREGAME_CSV),
            "missing": str(MISSING_CSV),
            "json": str(REPORT_JSON),
            "markdown": str(REPORT_MD),
        },
    }
    if write and not dry_run:
        write_csv(STINTS_CSV, stints)
        write_csv(TEAM_USAGE_CSV, usage)
        write_csv(PREGAME_CSV, pregame)
        write_csv(MISSING_CSV, missing)
        REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        REPORT_MD.write_text(markdown_report(report), encoding="utf-8")
    return report


def refresh_pregame_only(write: bool, dry_run: bool) -> dict[str, Any]:
    manifest = load_manifest()
    pregame_targets = load_v3_pregame_targets(manifest)
    usage = read_csv(TEAM_USAGE_CSV)
    missing = read_csv(MISSING_CSV)
    if not usage:
        raise FileNotFoundError(f"missing or empty team usage ledger: {TEAM_USAGE_CSV}")

    existing_gamepks: set[str] = set()
    existing_rows = 0
    existing_fieldnames: list[str] | None = None
    if PREGAME_CSV.exists() and PREGAME_CSV.stat().st_size > 0:
        with PREGAME_CSV.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            existing_fieldnames = list(reader.fieldnames or [])
            for row in reader:
                existing_rows += 1
                game_pk = clean(row.get("gamePk")).split(".")[0]
                if game_pk:
                    existing_gamepks.add(game_pk)

    if existing_gamepks:
        target_df = pregame_targets[~pregame_targets["gamePk"].astype(str).isin(existing_gamepks)].copy()
    else:
        target_df = pregame_targets
    pregame = pregame_features(target_df, usage) if len(target_df) else []

    old_report: dict[str, Any] = {}
    if REPORT_JSON.exists():
        try:
            old_report = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
        except Exception:
            old_report = {}
    final_rows = existing_rows + len(pregame) if existing_gamepks else len(pregame)
    report = {
        "ok": True,
        "mode": "pregame_only",
        "manifest_games": int(manifest["gamePk"].nunique()),
        "raw_parsed_games": int(old_report.get("raw_parsed_games", manifest["gamePk"].nunique())),
        "missing_games": int(old_report.get("missing_games", 0)),
        "stints": int(old_report.get("stints", 0)),
        "reliever_stints": int(old_report.get("reliever_stints", 0)),
        "pregame_target_rows": int(pregame_targets["gamePk"].nunique()),
        "pregame_v3_extra_rows": int(pregame_targets["gamePk"].nunique() - manifest["gamePk"].nunique()),
        "pregame_appended_rows": len(pregame),
        "team_usage_rows": len(usage),
        "pregame_feature_rows": final_rows,
        "missing_counts": dict(Counter(str(m.get("missing")) for m in missing if clean(m.get("missing")))),
        "by_season": manifest.groupby("season").size().to_dict(),
        "write": write,
        "dry_run": dry_run,
        "outputs": {
            "team_usage": str(TEAM_USAGE_CSV),
            "pregame": str(PREGAME_CSV),
            "missing": str(MISSING_CSV),
            "json": str(REPORT_JSON),
            "markdown": str(REPORT_MD),
        },
    }
    if write and not dry_run:
        if existing_fieldnames and PREGAME_CSV.exists():
            with PREGAME_CSV.open("a", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=existing_fieldnames, extrasaction="ignore")
                writer.writerows(pregame)
        else:
            write_csv(PREGAME_CSV, pregame)
        REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        REPORT_MD.write_text(markdown_report(report), encoding="utf-8")
    return report


def refresh_derived_only(write: bool, dry_run: bool) -> dict[str, Any]:
    """Rebuild team usage and pregame features from the current stint ledger."""
    manifest = load_manifest()
    stints = read_csv(STINTS_CSV)
    missing = read_csv(MISSING_CSV)
    if not stints:
        raise FileNotFoundError(f"missing or empty stint ledger: {STINTS_CSV}")

    usage_manifest = augment_manifest_with_stints(manifest, stints)
    pregame_targets = load_v3_pregame_targets(usage_manifest)
    usage = team_game_usage(stints, usage_manifest)
    pregame = pregame_features(pregame_targets, usage)
    parsed_gamepks = {clean(s.get("gamePk")).split(".")[0] for s in stints}
    parsed_gamepks.discard("")
    reliever_stints = sum(1 for s in stints if as_bool(s.get("is_reliever")))
    dates = [clean(s.get("date"))[:10] for s in stints if clean(s.get("date"))]
    report = {
        "ok": True,
        "mode": "derived_only",
        "manifest_games": int(manifest["gamePk"].nunique()),
        "usage_manifest_games": int(usage_manifest["gamePk"].nunique()),
        "stint_identity_patch_games": int(
            usage_manifest["gamePk"].nunique() - manifest["gamePk"].nunique()
        ),
        "raw_parsed_games": len(parsed_gamepks),
        "max_stint_date": max(dates) if dates else "",
        "missing_games": len(
            {clean(m.get("gamePk")).split(".")[0] for m in missing if clean(m.get("gamePk"))}
        ),
        "stints": len(stints),
        "reliever_stints": reliever_stints,
        "team_usage_rows": len(usage),
        "pregame_feature_rows": len(pregame),
        "pregame_target_rows": int(pregame_targets["gamePk"].nunique()),
        "pregame_v3_extra_rows": int(
            pregame_targets["gamePk"].nunique() - usage_manifest["gamePk"].nunique()
        ),
        "missing_counts": dict(
            Counter(str(m.get("missing")) for m in missing if clean(m.get("missing")))
        ),
        "by_season": usage_manifest.groupby("season").size().to_dict(),
        "write": write,
        "dry_run": dry_run,
        "outputs": {
            "stints": str(STINTS_CSV),
            "team_usage": str(TEAM_USAGE_CSV),
            "pregame": str(PREGAME_CSV),
            "missing": str(MISSING_CSV),
            "json": str(REPORT_JSON),
            "markdown": str(REPORT_MD),
        },
    }
    if write and not dry_run:
        write_csv(TEAM_USAGE_CSV, usage)
        write_csv(PREGAME_CSV, pregame)
        REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        REPORT_MD.write_text(markdown_report(report), encoding="utf-8")
    return report


def build_date_patch(
    target_date: str,
    write: bool,
    dry_run: bool,
    workers: int,
    stints_only: bool = False,
    official_schedule: bool = False,
) -> dict[str, Any]:
    target = date_iso(target_date)
    manifest = load_manifest()
    date_manifest = manifest[manifest["date"].astype(str).str[:10] == target].copy()
    v3_patch_targets = load_v3_date_patch_targets(target, set(date_manifest["gamePk"].astype(str)))
    if not v3_patch_targets.empty:
        date_manifest = pd.concat([date_manifest, v3_patch_targets], ignore_index=True, sort=False)
    known_gamepks = set(date_manifest["gamePk"].astype(str))
    raw_patch_targets = load_raw_date_patch_targets(target, known_gamepks)
    if not raw_patch_targets.empty:
        date_manifest = pd.concat([date_manifest, raw_patch_targets], ignore_index=True, sort=False)
    official_patch_targets = pd.DataFrame()
    if official_schedule:
        official_patch_targets = load_official_date_patch_targets(
            target, set(date_manifest["gamePk"].astype(str))
        )
        if not official_patch_targets.empty:
            date_manifest = pd.concat(
                [date_manifest, official_patch_targets],
                ignore_index=True,
                sort=False,
            )
    if date_manifest.empty:
        raise SystemExit(f"[ERR] no manifest rows for {target}")

    deep_mod = load_deep_review_module()
    date_stints, date_missing = build_stints(date_manifest, deep_mod, workers=workers)

    existing_stints = read_csv(STINTS_CSV)
    existing_missing = read_csv(MISSING_CSV)
    patch_gamepks = {str(g).split(".")[0] for g in date_manifest["gamePk"].astype(str)}
    merged_stints = [r for r in existing_stints if clean(r.get("gamePk")).split(".")[0] not in patch_gamepks] + date_stints
    merged_missing = [r for r in existing_missing if clean(r.get("gamePk")).split(".")[0] not in patch_gamepks] + date_missing
    merged_stints.sort(
        key=lambda r: (
            str(r.get("date")),
            str(r.get("gamePk")),
            str(r.get("team_side")),
            int(float(r.get("stint_order", 0) or 0)),
        )
    )
    merged_missing.sort(key=lambda r: (str(r.get("date")), str(r.get("gamePk")), str(r.get("missing"))))

    pregame_targets = load_v3_pregame_targets(manifest)
    if stints_only:
        usage = read_csv(TEAM_USAGE_CSV)
        pregame = read_csv(PREGAME_CSV)
    else:
        usage = team_game_usage(merged_stints, manifest)
        pregame = pregame_features(pregame_targets, usage)

    parsed_games = len({str(s["gamePk"]) for s in merged_stints})
    reliever_stints = sum(1 for s in merged_stints if as_bool(s.get("is_reliever")))
    date_parsed_games = len({str(s["gamePk"]) for s in date_stints})
    report = {
        "ok": True,
        "mode": "date_patch",
        "stints_only": stints_only,
        "target_date": target,
        "manifest_games": int(manifest["gamePk"].nunique()),
        "target_manifest_games": int(date_manifest["gamePk"].nunique()),
        "target_v3_patch_games": int(v3_patch_targets["gamePk"].nunique()) if not v3_patch_targets.empty else 0,
        "target_raw_identity_patch_games": int(raw_patch_targets["gamePk"].nunique()) if not raw_patch_targets.empty else 0,
        "target_official_schedule_patch_games": int(official_patch_targets["gamePk"].nunique()) if not official_patch_targets.empty else 0,
        "target_raw_parsed_games": date_parsed_games,
        "target_missing_games": len({str(m.get("gamePk")) for m in date_missing if clean(m.get("gamePk"))}),
        "raw_parsed_games": parsed_games,
        "missing_games": len({str(m.get("gamePk")) for m in merged_missing if clean(m.get("gamePk"))}),
        "stints": len(merged_stints),
        "reliever_stints": reliever_stints,
        "team_usage_rows": len(usage),
        "pregame_feature_rows": len(pregame),
        "pregame_target_rows": int(pregame_targets["gamePk"].nunique()),
        "pregame_v3_extra_rows": int(pregame_targets["gamePk"].nunique() - manifest["gamePk"].nunique()),
        "missing_counts": dict(Counter(str(m.get("missing")) for m in merged_missing if clean(m.get("missing")))),
        "by_season": manifest.groupby("season").size().to_dict(),
        "write": write,
        "dry_run": dry_run,
        "workers": workers,
        "outputs": {
            "stints": str(STINTS_CSV),
            "team_usage": str(TEAM_USAGE_CSV),
            "pregame": str(PREGAME_CSV),
            "missing": str(MISSING_CSV),
            "json": str(REPORT_JSON),
            "markdown": str(REPORT_MD),
        },
    }
    if write and not dry_run:
        write_csv(STINTS_CSV, merged_stints)
        if not stints_only:
            write_csv(TEAM_USAGE_CSV, usage)
            write_csv(PREGAME_CSV, pregame)
        write_csv(MISSING_CSV, merged_missing)
        REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        REPORT_MD.write_text(markdown_report(report), encoding="utf-8")
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--date", help="Patch only one game date into existing stints/team/pregame outputs.")
    ap.add_argument("--stints-only", action="store_true", help="With --date, patch stints/missing only and skip full team/pregame recomputation.")
    ap.add_argument(
        "--official-schedule",
        action="store_true",
        help="With --date, recover missing game identity from official Final schedule rows.",
    )
    ap.add_argument("--chunk-start", type=int)
    ap.add_argument("--chunk-end", type=int)
    ap.add_argument("--chunk-size", type=int, default=250)
    ap.add_argument("--merge-chunks", action="store_true")
    ap.add_argument("--pregame-only", action="store_true")
    ap.add_argument(
        "--derived-only",
        action="store_true",
        help="Rebuild team usage and pregame features from the current stint ledger.",
    )
    args = ap.parse_args()
    if args.date:
        result = build_date_patch(
            target_date=args.date,
            write=args.write,
            dry_run=args.dry_run,
            workers=args.workers,
            stints_only=args.stints_only,
            official_schedule=args.official_schedule,
        )
    elif args.derived_only:
        result = refresh_derived_only(write=args.write, dry_run=args.dry_run)
    elif args.pregame_only:
        result = refresh_pregame_only(write=args.write, dry_run=args.dry_run)
    elif args.merge_chunks:
        result = merge_chunk_outputs(write=args.write, dry_run=args.dry_run)
    elif args.chunk_start is not None:
        chunk_end = args.chunk_start if args.chunk_end is None else args.chunk_end
        result = build_chunk_range(
            write=args.write,
            dry_run=args.dry_run,
            workers=args.workers,
            chunk_start=args.chunk_start,
            chunk_end=chunk_end,
            chunk_size=args.chunk_size,
        )
    else:
        result = build(write=args.write, dry_run=args.dry_run, workers=args.workers)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
