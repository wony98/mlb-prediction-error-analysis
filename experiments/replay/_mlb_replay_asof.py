# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
from __future__ import annotations

"""Strict date-before-target helpers for research-only MLB replay."""

import os
import json
import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parent


def target_date() -> str:
    return str(os.environ.get("MLB_REPLAY_ASOF_DATE") or "").strip()


def enabled(date: str | None = None) -> bool:
    target = target_date()
    if not target:
        return False
    if date is not None and str(date) != target:
        raise RuntimeError(f"replay as-of date mismatch: env={target} requested={date}")
    return True


def _path(env_name: str, default_name: str) -> Path:
    raw = str(os.environ.get(env_name) or "").strip()
    path = Path(raw) if raw else ROOT / default_name
    if not path.is_absolute():
        path = ROOT / path
    if not path.is_file():
        raise FileNotFoundError(path)
    return path.resolve()


@lru_cache(maxsize=1)
def _box_game() -> pd.DataFrame:
    frame = pd.read_parquet(
        _path("MLB_REPLAY_BOX_GAME_PARQUET", "_box2026_game.parquet")
    ).copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    return frame


@lru_cache(maxsize=1)
def _box_bat() -> pd.DataFrame:
    frame = pd.read_parquet(
        _path("MLB_REPLAY_BOX_BAT_PARQUET", "_box2026_bat.parquet")
    ).copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    return frame


@lru_cache(maxsize=1)
def _statcast_daily() -> pd.DataFrame:
    frame = pd.read_csv(
        _path("MLB_REPLAY_STATCAST_DAILY_CSV", "mlb_statcast_daily.csv")
    ).copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    return frame


@lru_cache(maxsize=1)
def _pregame_sources() -> pd.DataFrame:
    frame = pd.read_csv(
        _path(
            "MLB_REPLAY_PREGAME_SOURCES_CSV",
            "_missing_replay_pregame_sources.csv",
        )
    ).copy()
    required = {
        "gamePk",
        "date",
        "home_team",
        "away_team",
        "home_lineup_ids",
        "away_lineup_ids",
        "home_WIN_LOSS",
        "away_WIN_LOSS",
        "run_total_baseline",
        "temperature",
        "wind_speed",
        "wind_direction",
        "precip_prob",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise RuntimeError(f"replay pregame source columns missing: {missing}")
    frame["gamePk"] = pd.to_numeric(frame["gamePk"], errors="raise").astype(int)
    if frame["gamePk"].duplicated().any():
        raise RuntimeError("duplicate gamePk in replay pregame source")
    dates = set(frame["date"].astype(str).str[:10])
    target = target_date()
    if target and dates != {target}:
        raise RuntimeError(
            f"replay pregame source date mismatch: target={target} rows={sorted(dates)}"
        )
    return frame.set_index("gamePk", drop=False).sort_index()


def _decode_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid replay list value: {value!r}") from exc
    if not isinstance(parsed, list):
        raise RuntimeError(f"replay list value is not an array: {value!r}")
    return parsed


def target_game(game_pk: Any, date: str) -> dict[str, Any]:
    enabled(date)
    try:
        key = int(float(game_pk))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"invalid replay gamePk: {game_pk!r}") from exc
    frame = _pregame_sources()
    if key not in frame.index:
        raise RuntimeError(f"replay pregame source missing gamePk={key}")
    row = frame.loc[key].to_dict()
    for column in (
        "home_lineup_ids",
        "away_lineup_ids",
        "home_lineup_names",
        "away_lineup_names",
        "home_bullpen_ids",
        "away_bullpen_ids",
        "home_bullpen_names",
        "away_bullpen_names",
    ):
        row[column] = _decode_list(row.get(column))
    return row


def target_games(date: str) -> list[dict[str, Any]]:
    enabled(date)
    return [target_game(game_pk, date) for game_pk in _pregame_sources().index]


def market_price(game_pk: Any, date: str) -> dict[str, float | None]:
    row = target_game(game_pk, date)
    return {
        key: (
            None
            if pd.isna(row.get(key))
            else float(row[key])
        )
        for key in (
            "away_WIN_LOSS",
            "home_WIN_LOSS",
            "run_total_baseline",
            "upper_win_loss",
            "lower_win_loss",
            "run_margin",
        )
    }


def weather(game_pk: Any, date: str) -> dict[str, Any]:
    row = target_game(game_pk, date)
    required = (
        "temperature",
        "wind_speed",
        "wind_direction",
        "precip_prob",
    )
    missing = [key for key in required if pd.isna(row.get(key))]
    if missing:
        raise RuntimeError(
            f"replay historical forecast incomplete gamePk={game_pk}: {missing}"
        )
    return {
        "temperature": float(row["temperature"]),
        "wind_speed": float(row["wind_speed"]),
        "wind_direction": int(float(row["wind_direction"])),
        "precip_prob": int(float(row["precip_prob"])),
        "weather_condition": (
            None
            if pd.isna(row.get("weather_condition"))
            else str(row["weather_condition"])
        ),
        "wind_kmh": (
            None if pd.isna(row.get("wind_kmh")) else float(row["wind_kmh"])
        ),
        "precip_mm": (
            None if pd.isna(row.get("precip_mm")) else float(row["precip_mm"])
        ),
        "dome": int(float(row.get("dome") or 0)),
    }


def lineup(game_pk: Any, side: str, date: str) -> dict[str, Any]:
    if side not in ("home", "away"):
        raise RuntimeError(f"invalid replay lineup side: {side}")
    row = target_game(game_pk, date)
    return {
        "ids": [int(value) for value in row[f"{side}_lineup_ids"]],
        "names": [str(value) for value in row[f"{side}_lineup_names"]],
        "bullpen_ids": [
            int(value) for value in row.get(f"{side}_bullpen_ids", [])
        ],
        "bullpen_names": [
            str(value) for value in row.get(f"{side}_bullpen_names", [])
        ],
    }


def umpire(game_pk: Any, date: str) -> tuple[Any, str]:
    row = target_game(game_pk, date)
    umpire_id = row.get("hp_ump_id")
    if pd.isna(umpire_id):
        umpire_id = None
    elif umpire_id is not None:
        umpire_id = int(float(umpire_id))
    name = row.get("hp_ump_name")
    return umpire_id, "" if pd.isna(name) else str(name)


def _prior(frame: pd.DataFrame, date: str) -> pd.DataFrame:
    enabled(date)
    cutoff = pd.Timestamp(date)
    return frame[frame["date"] < cutoff].copy()


def pitcher_stats(pitcher_id: Any, date: str) -> dict[str, float]:
    enabled(date)
    try:
        pitcher_id = int(float(pitcher_id))
    except (TypeError, ValueError):
        return {}
    frame = _prior(_box_game(), date)
    rows: list[pd.DataFrame] = []
    for side in ("home", "away"):
        selected = frame[
            pd.to_numeric(frame[f"{side}_sp_id"], errors="coerce") == pitcher_id
        ].copy()
        if selected.empty:
            continue
        rows.append(
            selected.rename(
                columns={
                    f"{side}_sp_ip": "ip",
                    f"{side}_sp_er": "er",
                    f"{side}_sp_h": "h",
                    f"{side}_sp_bb": "bb",
                    f"{side}_sp_k": "k",
                    f"{side}_sp_hr": "hr",
                }
            )[["ip", "er", "h", "bb", "k", "hr"]]
        )
    if not rows:
        return {}
    stats = pd.concat(rows, ignore_index=True).apply(
        pd.to_numeric, errors="coerce"
    ).fillna(0)
    totals = stats.sum()
    ip = float(totals["ip"])
    if ip <= 0:
        return {}
    return {
        "era": 9.0 * float(totals["er"]) / ip,
        "whip": (float(totals["h"]) + float(totals["bb"])) / ip,
        "k9": 9.0 * float(totals["k"]) / ip,
        "bb9": 9.0 * float(totals["bb"]) / ip,
        "hr9": 9.0 * float(totals["hr"]) / ip,
        "ip": ip,
    }


def batter_ops(batter_id: Any, date: str) -> float | None:
    enabled(date)
    try:
        batter_id = int(float(batter_id))
    except (TypeError, ValueError):
        return None
    frame = _prior(_box_bat(), date)
    frame = frame[
        pd.to_numeric(frame["batter_id"], errors="coerce") == batter_id
    ]
    if frame.empty:
        return None
    columns = ("ab", "h", "bb", "hbp", "d2", "d3", "hr")
    totals = frame[list(columns)].apply(
        pd.to_numeric, errors="coerce"
    ).fillna(0).sum()
    ab = float(totals["ab"])
    h = float(totals["h"])
    bb = float(totals["bb"])
    hbp = float(totals["hbp"])
    pa_obp = ab + bb + hbp
    if ab <= 0 or pa_obp <= 0:
        return None
    obp = (h + bb + hbp) / pa_obp
    total_bases = (
        h + float(totals["d2"]) + 2.0 * float(totals["d3"])
        + 3.0 * float(totals["hr"])
    )
    return obp + total_bases / ab


def lineup_ops(batter_ids: list[Any], date: str) -> float | None:
    values = [
        value
        for value in (batter_ops(player_id, date) for player_id in batter_ids)
        if value is not None
    ]
    return sum(values) / len(values) if values else None


_TEAM_ABBR = {
    "Arizona Diamondbacks": "AZ",
    "Atlanta Braves": "ATL",
    "Baltimore Orioles": "BAL",
    "Boston Red Sox": "BOS",
    "Chicago Cubs": "CHC",
    "Chicago White Sox": "CWS",
    "Cincinnati Reds": "CIN",
    "Cleveland Guardians": "CLE",
    "Colorado Rockies": "COL",
    "Detroit Tigers": "DET",
    "Houston Astros": "HOU",
    "Kansas City Royals": "KC",
    "Los Angeles Angels": "LAA",
    "Los Angeles Dodgers": "LAD",
    "Miami Marlins": "MIA",
    "Milwaukee Brewers": "MIL",
    "Minnesota Twins": "MIN",
    "New York Mets": "NYM",
    "New York Yankees": "NYY",
    "Athletics": "ATH",
    "Oakland Athletics": "ATH",
    "Philadelphia Phillies": "PHI",
    "Pittsburgh Pirates": "PIT",
    "San Diego Padres": "SD",
    "San Francisco Giants": "SF",
    "Seattle Mariners": "SEA",
    "St. Louis Cardinals": "STL",
    "Tampa Bay Rays": "TB",
    "Texas Rangers": "TEX",
    "Toronto Blue Jays": "TOR",
    "Washington Nationals": "WSH",
}


def statcast_rolling(team: str, date: str, days: int = 14) -> dict[str, float]:
    frame = _prior(_statcast_daily(), date)
    abbr = _TEAM_ABBR.get(str(team))
    if not abbr:
        return {}
    cutoff = pd.Timestamp(date)
    rows = frame[
        (frame["team"] == abbr)
        & (frame["date"] >= cutoff - pd.Timedelta(days=days))
    ].copy()
    if len(rows) < 3:
        return {}
    totals = rows[
        [
            "batting_bb",
            "batting_barrel",
            "batting_ev_sum",
            "batting_ev_n",
            "pitching_bb",
            "pitching_barrel",
            "pitching_ev_sum",
            "pitching_ev_n",
        ]
    ].apply(pd.to_numeric, errors="coerce").fillna(0).sum()

    def ratio(numerator: str, denominator: str) -> float | None:
        den = float(totals[denominator])
        return float(totals[numerator]) / den if den > 0 else None

    values = {
        "bat_barrel14": ratio("batting_barrel", "batting_bb"),
        "bat_ev14": ratio("batting_ev_sum", "batting_ev_n"),
        "pit_barrel_allowed14": ratio("pitching_barrel", "pitching_bb"),
        "pit_ev_allowed14": ratio("pitching_ev_sum", "pitching_ev_n"),
    }
    return {key: value for key, value in values.items() if value is not None}


def audit(date: str) -> dict[str, Any]:
    enabled(date)
    cutoff = pd.Timestamp(date)
    game = _box_game()
    bat = _box_bat()
    statcast = _statcast_daily()
    pregame_path = _path(
        "MLB_REPLAY_PREGAME_SOURCES_CSV",
        "_missing_replay_pregame_sources.csv",
    )
    pregame = _pregame_sources()
    digest = hashlib.sha256(pregame_path.read_bytes()).hexdigest()
    return {
        "target_date": date,
        "box_game_path": str(
            _path("MLB_REPLAY_BOX_GAME_PARQUET", "_box2026_game.parquet")
        ),
        "box_bat_path": str(
            _path("MLB_REPLAY_BOX_BAT_PARQUET", "_box2026_bat.parquet")
        ),
        "statcast_daily_path": str(
            _path("MLB_REPLAY_STATCAST_DAILY_CSV", "mlb_statcast_daily.csv")
        ),
        "box_game_rows_at_or_after_target": int((game["date"] >= cutoff).sum()),
        "box_bat_rows_at_or_after_target": int((bat["date"] >= cutoff).sum()),
        "statcast_rows_at_or_after_target": int(
            (statcast["date"] >= cutoff).sum()
        ),
        "pregame_sources_path": str(pregame_path),
        "pregame_sources_sha256": digest,
        "pregame_source_rows": int(len(pregame)),
        "pregame_source_dates": sorted(
            set(pregame["date"].astype(str).str[:10])
        ),
        "calculation_filter": "date < target_date",
    }
