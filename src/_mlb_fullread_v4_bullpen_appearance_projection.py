# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parent
POST = Path(
    os.environ.get(
        "MLB_BULLPEN_PROJECTION_OUT_ROOT",
        str(ROOT / "postmortem" / "fullread_v4"),
    )
)

STINTS_CSV = Path(
    os.environ.get(
        "MLB_BULLPEN_STINTS_CSV",
        str(ROOT / "postmortem/fullread_v4/mlb_fullread_v4_bullpen_stints.csv"),
    )
)
ROLES_DAILY_CSV = POST / "mlb_fullread_v4_reliever_roles_daily.csv"
PROJECTION_CSV = POST / "mlb_fullread_v4_bullpen_appearance_projection.csv"
TEAM_PROJECTION_CSV = POST / "mlb_fullread_v4_bullpen_appearance_team_projection.csv"
REPORT_JSON = POST / "mlb_fullread_v4_bullpen_appearance_projection_report.json"
REPORT_MD = POST / "mlb_fullread_v4_bullpen_appearance_projection_report.md"

BAD = {"", "nan", "none", "null", "na", "n/a"}


def clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in BAD else text


def norm_name(value: Any) -> str:
    text = clean(value)
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def num(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or str(value).strip().lower() in BAD:
            return default
        x = float(value)
        return default if math.isnan(x) else x
    except Exception:
        return default


def truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def split_names(value: Any) -> list[str]:
    from _mlb_name_list import parse_name_list

    return parse_name_list(value)


def date_iso(value: str) -> str:
    raw = value.strip().replace("-", "")
    if len(raw) != 8 or not raw.isdigit():
        raise SystemExit(f"[ERR] date format invalid: {value}")
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"


def read_v3_files(target_date: str | None, season: str | None = None) -> list[Path]:
    if target_date:
        override = os.environ.get("MLB_C3_V3_PATH", "").strip()
        path = Path(override) if override else ROOT / f"_v3_games_{date_iso(target_date)}.csv"
        return [path] if path.exists() else []
    if season:
        return sorted(ROOT.glob(f"_v3_games_{season}-*.csv"))
    return sorted(ROOT.glob("_v3_games_*.csv"))


def _date_rows(path: Path, date: str) -> int:
    if not path.exists():
        return 0
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            return sum(1 for row in csv.DictReader(f) if clean(row.get("date"))[:10] == date)
    except Exception:
        return 0


def _file_sha1(path: Path) -> str:
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _date_cache_current(target_date: str) -> dict[str, Any] | None:
    date = date_iso(target_date)
    override = os.environ.get("MLB_C3_V3_PATH", "").strip()
    v3_path = Path(override) if override else ROOT / f"_v3_games_{date}.csv"
    if not v3_path.exists() or not PROJECTION_CSV.exists() or not TEAM_PROJECTION_CSV.exists():
        return None
    source_hash = _file_sha1(v3_path)
    try:
        report = json.loads(REPORT_JSON.read_text(encoding="utf-8")) if REPORT_JSON.exists() else {}
        if source_hash and report.get("source_v3_hash") == source_hash:
            projection_rows = _date_rows(PROJECTION_CSV, date)
            team_rows = _date_rows(TEAM_PROJECTION_CSV, date)
            if projection_rows > 0 and team_rows > 0:
                return {
                    "ok": True,
                    "schema": "mlb_fullread_v4_bullpen_appearance_projection.v1",
                    "research_only": True,
                    "cache_status": "same_date_current",
                    "target_date": date,
                    "v3_files": [v3_path.name],
                    "projection_rows": projection_rows,
                    "team_projection_rows": team_rows,
                    "source_v3_hash": source_hash,
                }
    except Exception:
        pass
    try:
        cutoff = v3_path.stat().st_mtime
        if PROJECTION_CSV.stat().st_mtime < cutoff or TEAM_PROJECTION_CSV.stat().st_mtime < cutoff:
            return None
    except OSError:
        return None
    projection_rows = _date_rows(PROJECTION_CSV, date)
    team_rows = _date_rows(TEAM_PROJECTION_CSV, date)
    if projection_rows <= 0 or team_rows <= 0:
        return None
    return {
        "ok": True,
        "schema": "mlb_fullread_v4_bullpen_appearance_projection.v1",
        "research_only": True,
        "cache_status": "same_date_current",
        "target_date": date,
        "v3_files": [v3_path.name],
        "projection_rows": projection_rows,
        "team_projection_rows": team_rows,
        "source_v3_hash": source_hash,
    }


STINT_USECOLS = [
    "date",
    "gamePk",
    "team",
    "pitcher_id",
    "pitcher_name",
    "is_reliever",
    "pitch_count",
    "pa",
    "runs_allowed",
    "entry_inning",
    "entry_margin",
    "strikeouts",
    "walk_hbp",
    "home_runs",
    "late",
    "long_relief",
    "entered_lead",
    "entered_tie",
    "entered_trail",
    "blown_lead",
]


def load_stints(target_date: str | None = None) -> pd.DataFrame:
    if not STINTS_CSV.exists():
        raise FileNotFoundError(f"missing {STINTS_CSV}")
    if STINTS_CSV.suffix.casefold() in {".parquet", ".pq"}:
        df = pd.read_parquet(STINTS_CSV)
        df = df[[column for column in STINT_USECOLS if column in df.columns]].copy()
        for column in ("gamePk", "pitcher_id"):
            if column in df.columns:
                df[column] = df[column].astype(str)
    else:
        df = pd.read_csv(
            STINTS_CSV,
            dtype={"gamePk": str, "pitcher_id": str},
            usecols=lambda c: c in STINT_USECOLS,
        )
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    if target_date:
        target = pd.Timestamp(date_iso(target_date))
        start = target - pd.Timedelta(days=90)
        df = df[(df["date"] < target) & (df["date"] >= start)].copy()
    for col in [
        "pitch_count",
        "pa",
        "runs_allowed",
        "entry_inning",
        "entry_margin",
        "strikeouts",
        "walk_hbp",
        "home_runs",
    ]:
        df[col] = pd.to_numeric(df.get(col), errors="coerce").fillna(0)
    for col in ["is_reliever", "late", "long_relief", "entered_lead", "entered_tie", "entered_trail", "blown_lead"]:
        df[col] = df.get(col, "").map(truthy)
    df["norm_pitcher_name"] = df["pitcher_name"].map(norm_name)
    return df[df["is_reliever"] & df["date"].notna()].copy()


def build_daily_roles(stints: pd.DataFrame) -> pd.DataFrame:
    if stints.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    group_cols = ["date", "gamePk", "team", "pitcher_id", "pitcher_name", "norm_pitcher_name"]
    for keys, g in stints.groupby(group_cols, dropna=False):
        date, game_pk, team, pitcher_id, pitcher_name, norm_pitcher = keys
        entry_min = int(g["entry_inning"].min()) if len(g) else 0
        pitches = int(g["pitch_count"].sum())
        pa = int(g["pa"].sum())
        late_pitches = int(g.loc[g["late"], "pitch_count"].sum())
        leverage_stints = int(((g["entry_inning"] >= 7) & (g["entry_margin"].abs() <= 3)).sum())
        close_lead_stints = int(((g["entry_inning"] >= 7) & (g["entered_lead"] | g["entered_tie"])).sum())
        long_relief = bool(g["long_relief"].any() or pitches >= 30 or pa >= 8)
        mop_up = bool((g["entered_trail"] & (g["entry_margin"].abs() >= 4)).any())
        rows.append(
            {
                "date": pd.Timestamp(date).strftime("%Y-%m-%d"),
                "gamePk": clean(game_pk),
                "team": clean(team),
                "pitcher_id": clean(pitcher_id),
                "pitcher_name": clean(pitcher_name),
                "norm_pitcher_name": clean(norm_pitcher),
                "pitches": pitches,
                "pa": pa,
                "entry_inning_min": entry_min,
                "late_pitches": late_pitches,
                "runs_allowed": int(g["runs_allowed"].sum()),
                "home_runs": int(g["home_runs"].sum()),
                "walk_hbp": int(g["walk_hbp"].sum()),
                "strikeouts": int(g["strikeouts"].sum()),
                "leverage_stints": leverage_stints,
                "close_lead_stints": close_lead_stints,
                "long_relief_game": int(long_relief),
                "mop_up_game": int(mop_up),
                "blown_lead_game": int(g["blown_lead"].any()),
            }
        )
    out = pd.DataFrame(rows)
    out["date"] = pd.to_datetime(out["date"])
    return out


def recent_window(daily: pd.DataFrame, team: str, target_date: str, days: int) -> pd.DataFrame:
    target = pd.Timestamp(target_date)
    start = target - pd.Timedelta(days=days)
    return daily[(daily["team"] == team) & (daily["date"] < target) & (daily["date"] >= start)].copy()


def all_prior(daily: pd.DataFrame, team: str, target_date: str) -> pd.DataFrame:
    target = pd.Timestamp(target_date)
    return daily[(daily["team"] == team) & (daily["date"] < target)].copy()


def recent_window_team(team_daily: pd.DataFrame, target_date: str, days: int) -> pd.DataFrame:
    if team_daily.empty:
        return team_daily.copy()
    target = pd.Timestamp(target_date)
    start = target - pd.Timedelta(days=days)
    return team_daily[(team_daily["date"] < target) & (team_daily["date"] >= start)].copy()


def all_prior_team(team_daily: pd.DataFrame, target_date: str) -> pd.DataFrame:
    if team_daily.empty:
        return team_daily.copy()
    target = pd.Timestamp(target_date)
    return team_daily[team_daily["date"] < target].copy()


def role_from_history(w30: pd.DataFrame) -> tuple[str, float]:
    if w30.empty:
        return "unknown", 0.0
    app = len(w30)
    late_app = int((w30["late_pitches"] > 0).sum())
    leverage = int(w30["leverage_stints"].sum())
    close_lead = int(w30["close_lead_stints"].sum())
    long_app = int(w30["long_relief_game"].sum())
    mop = int(w30["mop_up_game"].sum())
    avg_pitches = float(w30["pitches"].mean() or 0)
    avg_entry = float(w30["entry_inning_min"].replace(0, pd.NA).dropna().mean() or 0)
    late_share = late_app / app if app else 0.0
    leverage_share = leverage / app if app else 0.0
    close_share = close_lead / app if app else 0.0
    if avg_pitches >= 45 or (avg_pitches >= 35 and app <= 3):
        return "rotation_or_bulk_depth", min(0.65, 0.25 + 0.04 * app)
    if long_app >= 2 or avg_pitches >= 28 or (avg_entry and avg_entry <= 5.5 and avg_pitches >= 20):
        return "long_relief", min(0.85, 0.35 + 0.08 * long_app)
    if mop >= max(2, app // 2):
        return "mop_up_depth", min(0.7, 0.25 + 0.05 * mop)
    if app >= 3 and avg_entry >= 8.0 and close_share >= 0.45 and avg_pitches <= 24:
        return "late_relief_pool", min(1.0, 0.50 + 0.12 * close_share + 0.05 * leverage)
    if app >= 3 and avg_entry >= 7.0 and (leverage_share >= 0.35 or close_share >= 0.30) and avg_pitches <= 26:
        return "high_leverage_bridge", min(0.9, 0.42 + 0.10 * leverage_share + 0.04 * late_app)
    if app >= 3 and late_share >= 0.40 and avg_entry >= 6.5:
        return "middle_late_bridge", min(0.8, 0.35 + 0.08 * late_share + 0.02 * late_app)
    return "middle_relief", min(0.72, 0.28 + 0.025 * app + 0.015 * late_app)


def availability_features(w_prior: pd.DataFrame, pitcher_id: str, target_date: str) -> dict[str, Any]:
    target = pd.Timestamp(target_date)
    p = w_prior[w_prior["pitcher_id"].astype(str) == str(pitcher_id)].copy()
    if p.empty:
        return {
            "last_app_date": "",
            "days_rest": "",
            "pitches_1d": 0,
            "pitches_2d": 0,
            "pitches_3d": 0,
            "apps_3d": 0,
            "apps_7d": 0,
            "b2b": 0,
            "three_in_3": 0,
            "long_relief_3d": 0,
            "availability": "unknown",
            "availability_penalty": 20,
        }
    p = p.sort_values("date")
    last_date = p["date"].max()
    days_rest = int((target - last_date).days)

    def since(days: int) -> pd.DataFrame:
        return p[(p["date"] < target) & (p["date"] >= target - pd.Timedelta(days=days))]

    w1, w2, w3, w7 = since(1), since(2), since(3), since(7)
    pitches_1d = int(w1["pitches"].sum())
    pitches_2d = int(w2["pitches"].sum())
    pitches_3d = int(w3["pitches"].sum())
    apps_3d = int(w3["date"].nunique())
    apps_7d = int(w7["date"].nunique())
    b2b = int(len(set(w2["date"].dt.strftime("%Y-%m-%d"))) >= 2)
    three_in_3 = int(apps_3d >= 3)
    long_relief_3d = int(w3["long_relief_game"].sum())

    if pitches_1d >= 35 or pitches_2d >= 55 or three_in_3 or (days_rest == 1 and int(w1["long_relief_game"].sum()) > 0):
        availability = "likely_unavailable"
        penalty = 60
    elif pitches_1d >= 25 or pitches_3d >= 65 or b2b:
        availability = "limited"
        penalty = 40
    elif pitches_1d >= 15 or pitches_3d >= 45 or apps_3d >= 2:
        availability = "monitor"
        penalty = 22
    else:
        availability = "available"
        penalty = 0
    return {
        "last_app_date": pd.Timestamp(last_date).strftime("%Y-%m-%d"),
        "days_rest": days_rest,
        "pitches_1d": pitches_1d,
        "pitches_2d": pitches_2d,
        "pitches_3d": pitches_3d,
        "apps_3d": apps_3d,
        "apps_7d": apps_7d,
        "b2b": b2b,
        "three_in_3": three_in_3,
        "long_relief_3d": long_relief_3d,
        "availability": availability,
        "availability_penalty": penalty,
    }


def starter_short_leash(row: dict[str, Any], side: str) -> tuple[int, str]:
    prefix = f"{side}_"
    flags: list[str] = []
    era = num(row.get(prefix + "sp_era"))
    xera = num(row.get(prefix + "sp_xera_lag1") or row.get(prefix + "prior_sp_xera"))
    barrel14 = num(row.get(prefix + "sp_roll14_barrel_pct"))
    xwoba14 = num(row.get(prefix + "sp_roll14_xwoba"))
    n_starts14 = num(row.get(prefix + "sp_roll14_n_starts"))
    if era is not None and era >= 5.00:
        flags.append(f"sp_era={era:.2f}")
    if xera is not None and xera >= 4.75:
        flags.append(f"sp_xera={xera:.2f}")
    if barrel14 is not None and barrel14 >= 0.10:
        flags.append(f"sp_14d_barrel={barrel14:.3f}")
    if xwoba14 is not None and xwoba14 >= 0.390:
        flags.append(f"sp_14d_xwoba={xwoba14:.3f}")
    if n_starts14 is not None and n_starts14 <= 1:
        flags.append(f"limited_recent_sp_sample={n_starts14:.0f}")
    return (1 if len(flags) >= 2 else 0), "; ".join(flags)


def team_rows_from_slate(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            date = clean(row.get("date"))[:10] or path.stem.replace("_v3_games_", "")
            for side in ["home", "away"]:
                opp_side = "away" if side == "home" else "home"
                team = clean(row.get(f"{side}_team") or row.get(f"{side}_team_name"))
                opp = clean(row.get(f"{opp_side}_team") or row.get(f"{opp_side}_team_name"))
                bullpen_names = split_names(row.get(f"{side}_bullpen_names"))
                short_leash, short_reason = starter_short_leash(row, side)
                rows.append(
                    {
                        "date": date,
                        "gamePk": clean(row.get("gamePk")),
                        "matchup": f"{clean(row.get('away_team'))} @ {clean(row.get('home_team'))}",
                        "team": team,
                        "opp": opp,
                        "side": side,
                        "starter_name": clean(row.get(f"{side}_sp_name")),
                        "listed_bullpen_names": bullpen_names,
                        "starter_short_leash_risk": short_leash,
                        "starter_short_leash_reasons": short_reason,
                        "source_v3": path.name,
                    }
                )
    return rows


def resolve_pitchers(team_prior: pd.DataFrame, listed_names: list[str]) -> list[dict[str, str]]:
    latest_by_norm: dict[str, dict[str, str]] = {}
    if listed_names and not team_prior.empty:
        tmp = team_prior.sort_values("date")
        for _, r in tmp.iterrows():
            n = clean(r.get("norm_pitcher_name"))
            if n:
                latest_by_norm[n] = {
                    "pitcher_id": clean(r.get("pitcher_id")),
                    "pitcher_name": clean(r.get("pitcher_name")),
                    "norm_pitcher_name": n,
                    "listed": "1",
                    "candidate_source": "listed_history",
                }
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for name in listed_names:
        n = norm_name(name)
        found = latest_by_norm.get(n)
        if found:
            key = found["pitcher_id"] or n
            if key not in seen:
                out.append(found)
                seen.add(key)
        elif n and n not in seen:
            out.append(
                {
                    "pitcher_id": f"name:{n}",
                    "pitcher_name": name,
                    "norm_pitcher_name": n,
                    "listed": "1",
                    "candidate_source": "listed_only",
                }
            )
            seen.add(n)

    if not team_prior.empty:
        cutoff30 = team_prior["date"].max() - pd.Timedelta(days=30)
        recent_pool = team_prior[team_prior["date"] >= cutoff30].copy()
        if recent_pool.empty:
            recent_pool = team_prior.copy()
        recent_rows: list[dict[str, Any]] = []
        for pid, g in recent_pool.groupby("pitcher_id", dropna=False):
            pid_clean = clean(pid)
            if not pid_clean or pid_clean in seen:
                continue
            g = g.sort_values("date")
            recent_rows.append(
                {
                    "pitcher_id": pid_clean,
                    "pitcher_name": clean(g.iloc[-1].get("pitcher_name")),
                    "norm_pitcher_name": clean(g.iloc[-1].get("norm_pitcher_name")),
                    "listed": "0",
                    "candidate_source": "recent_usage",
                    "recent_apps": int(g["gamePk"].nunique()),
                    "recent_pitches": int(g["pitches"].sum()),
                    "last_date": g["date"].max(),
                }
            )
        recent_rows.sort(key=lambda r: (-int(r.get("recent_apps", 0)), -int(r.get("recent_pitches", 0)), str(r.get("pitcher_name"))))
        for r in recent_rows[:14]:
            key = clean(r["pitcher_id"]) or clean(r["norm_pitcher_name"])
            if key and key not in seen:
                out.append({k: clean(v) for k, v in r.items() if k not in {"recent_apps", "recent_pitches", "last_date"}})
                seen.add(key)
    return out


def pitcher_history_features(team_prior: pd.DataFrame, pitcher_id: str, target_date: str) -> dict[str, Any]:
    target = pd.Timestamp(target_date)
    p = team_prior[team_prior["pitcher_id"].astype(str) == str(pitcher_id)].copy()
    if p.empty:
        return {
            "apps_7d_hist": 0,
            "apps_14d_hist": 0,
            "apps_30d_hist": 0,
            "late_apps_30d": 0,
            "long_apps_30d": 0,
            "mop_apps_30d": 0,
            "avg_entry_30d": 0.0,
            "avg_pitches_30d": 0.0,
            "recent_usage_score": 0,
        }

    def win(days: int) -> pd.DataFrame:
        return p[(p["date"] < target) & (p["date"] >= target - pd.Timedelta(days=days))]

    w7, w14, w30 = win(7), win(14), win(30)
    entry = w30["entry_inning_min"].replace(0, pd.NA).dropna()
    apps7 = int(w7["date"].nunique())
    apps14 = int(w14["date"].nunique())
    apps30 = int(w30["date"].nunique())
    late_apps = int((w30["late_pitches"] > 0).sum())
    long_apps = int(w30["long_relief_game"].sum())
    mop_apps = int(w30["mop_up_game"].sum())
    recent_usage_score = min(38, apps7 * 6 + max(0, apps14 - apps7) * 3 + max(0, apps30 - apps14) * 1.2)
    return {
        "apps_7d_hist": apps7,
        "apps_14d_hist": apps14,
        "apps_30d_hist": apps30,
        "late_apps_30d": late_apps,
        "long_apps_30d": long_apps,
        "mop_apps_30d": mop_apps,
        "avg_entry_30d": round(float(entry.mean()) if len(entry) else 0.0, 2),
        "avg_pitches_30d": round(float(w30["pitches"].mean()) if len(w30) else 0.0, 2),
        "recent_usage_score": round(recent_usage_score, 2),
    }


def _daily_records_by_team(daily: pd.DataFrame) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    if daily.empty:
        return out
    for r in daily.to_dict("records"):
        team = clean(r.get("team"))
        if not team:
            continue
        r["date"] = pd.Timestamp(r["date"])
        out.setdefault(team, []).append(r)
    for rows in out.values():
        rows.sort(key=lambda x: (x["date"], clean(x.get("pitcher_id"))))
    return out


def _mean(values: list[float]) -> float:
    return float(sum(values) / len(values)) if values else 0.0


def _recent_records(records: list[dict[str, Any]], target: pd.Timestamp, days: int) -> list[dict[str, Any]]:
    start = target - pd.Timedelta(days=days)
    return [r for r in records if r["date"] < target and r["date"] >= start]


def role_from_history_records(records: list[dict[str, Any]]) -> tuple[str, float]:
    if not records:
        return "unknown", 0.0
    app = len(records)
    late_app = sum(1 for r in records if int(r.get("late_pitches") or 0) > 0)
    leverage = sum(int(r.get("leverage_stints") or 0) for r in records)
    close_lead = sum(int(r.get("close_lead_stints") or 0) for r in records)
    long_app = sum(int(r.get("long_relief_game") or 0) for r in records)
    mop = sum(int(r.get("mop_up_game") or 0) for r in records)
    avg_pitches = _mean([float(r.get("pitches") or 0) for r in records])
    entry_vals = [float(r.get("entry_inning_min") or 0) for r in records if float(r.get("entry_inning_min") or 0) > 0]
    avg_entry = _mean(entry_vals)
    late_share = late_app / app if app else 0.0
    leverage_share = leverage / app if app else 0.0
    close_share = close_lead / app if app else 0.0
    if avg_pitches >= 45 or (avg_pitches >= 35 and app <= 3):
        return "rotation_or_bulk_depth", min(0.65, 0.25 + 0.04 * app)
    if long_app >= 2 or avg_pitches >= 28 or (avg_entry and avg_entry <= 5.5 and avg_pitches >= 20):
        return "long_relief", min(0.85, 0.35 + 0.08 * long_app)
    if mop >= max(2, app // 2):
        return "mop_up_depth", min(0.7, 0.25 + 0.05 * mop)
    if app >= 3 and avg_entry >= 8.0 and close_share >= 0.45 and avg_pitches <= 24:
        return "late_relief_pool", min(1.0, 0.50 + 0.12 * close_share + 0.05 * leverage)
    if app >= 3 and avg_entry >= 7.0 and (leverage_share >= 0.35 or close_share >= 0.30) and avg_pitches <= 26:
        return "high_leverage_bridge", min(0.9, 0.42 + 0.10 * leverage_share + 0.04 * late_app)
    if app >= 3 and late_share >= 0.40 and avg_entry >= 6.5:
        return "middle_late_bridge", min(0.8, 0.35 + 0.08 * late_share + 0.02 * late_app)
    return "middle_relief", min(0.72, 0.28 + 0.025 * app + 0.015 * late_app)


def availability_features_records(records: list[dict[str, Any]], target: pd.Timestamp) -> dict[str, Any]:
    if not records:
        return {
            "last_app_date": "",
            "days_rest": "",
            "pitches_1d": 0,
            "pitches_2d": 0,
            "pitches_3d": 0,
            "apps_3d": 0,
            "apps_7d": 0,
            "b2b": 0,
            "three_in_3": 0,
            "long_relief_3d": 0,
            "availability": "unknown",
            "availability_penalty": 20,
        }
    last_date = max(r["date"] for r in records)
    days_rest = int((target - last_date).days)

    def since(days: int) -> list[dict[str, Any]]:
        start = target - pd.Timedelta(days=days)
        return [r for r in records if r["date"] < target and r["date"] >= start]

    w1, w2, w3, w7 = since(1), since(2), since(3), since(7)
    pitches_1d = sum(int(r.get("pitches") or 0) for r in w1)
    pitches_2d = sum(int(r.get("pitches") or 0) for r in w2)
    pitches_3d = sum(int(r.get("pitches") or 0) for r in w3)
    apps_3d = len({r["date"].strftime("%Y-%m-%d") for r in w3})
    apps_7d = len({r["date"].strftime("%Y-%m-%d") for r in w7})
    b2b = int(len({r["date"].strftime("%Y-%m-%d") for r in w2}) >= 2)
    three_in_3 = int(apps_3d >= 3)
    long_relief_3d = sum(int(r.get("long_relief_game") or 0) for r in w3)
    if pitches_1d >= 35 or pitches_2d >= 55 or three_in_3 or (days_rest == 1 and sum(int(r.get("long_relief_game") or 0) for r in w1) > 0):
        availability = "likely_unavailable"
        penalty = 60
    elif pitches_1d >= 25 or pitches_3d >= 65 or b2b:
        availability = "limited"
        penalty = 40
    elif pitches_1d >= 15 or pitches_3d >= 45 or apps_3d >= 2:
        availability = "monitor"
        penalty = 22
    else:
        availability = "available"
        penalty = 0
    return {
        "last_app_date": pd.Timestamp(last_date).strftime("%Y-%m-%d"),
        "days_rest": days_rest,
        "pitches_1d": pitches_1d,
        "pitches_2d": pitches_2d,
        "pitches_3d": pitches_3d,
        "apps_3d": apps_3d,
        "apps_7d": apps_7d,
        "b2b": b2b,
        "three_in_3": three_in_3,
        "long_relief_3d": long_relief_3d,
        "availability": availability,
        "availability_penalty": penalty,
    }


def pitcher_history_features_records(records: list[dict[str, Any]], target: pd.Timestamp) -> dict[str, Any]:
    def win(days: int) -> list[dict[str, Any]]:
        start = target - pd.Timedelta(days=days)
        return [r for r in records if r["date"] < target and r["date"] >= start]

    w7, w14, w30 = win(7), win(14), win(30)
    entry_vals = [float(r.get("entry_inning_min") or 0) for r in w30 if float(r.get("entry_inning_min") or 0) > 0]
    apps7 = len({r["date"].strftime("%Y-%m-%d") for r in w7})
    apps14 = len({r["date"].strftime("%Y-%m-%d") for r in w14})
    apps30 = len({r["date"].strftime("%Y-%m-%d") for r in w30})
    late_apps = sum(1 for r in w30 if int(r.get("late_pitches") or 0) > 0)
    long_apps = sum(int(r.get("long_relief_game") or 0) for r in w30)
    mop_apps = sum(int(r.get("mop_up_game") or 0) for r in w30)
    recent_usage_score = min(38, apps7 * 6 + max(0, apps14 - apps7) * 3 + max(0, apps30 - apps14) * 1.2)
    return {
        "apps_7d_hist": apps7,
        "apps_14d_hist": apps14,
        "apps_30d_hist": apps30,
        "late_apps_30d": late_apps,
        "long_apps_30d": long_apps,
        "mop_apps_30d": mop_apps,
        "avg_entry_30d": round(_mean(entry_vals), 2),
        "avg_pitches_30d": round(_mean([float(r.get("pitches") or 0) for r in w30]), 2),
        "recent_usage_score": round(recent_usage_score, 2),
    }


def _candidate_pitchers_from_records(prior: list[dict[str, Any]], listed_names: list[str]) -> list[dict[str, str]]:
    if listed_names:
        return []
    if not prior:
        return []
    last_prior_date = max(r["date"] for r in prior)
    cutoff30 = last_prior_date - pd.Timedelta(days=30)
    recent_pool = [r for r in prior if r["date"] >= cutoff30] or prior
    grouped: dict[str, dict[str, Any]] = {}
    for r in recent_pool:
        pid = clean(r.get("pitcher_id"))
        if not pid:
            continue
        g = grouped.setdefault(
            pid,
            {
                "pitcher_id": pid,
                "pitcher_name": clean(r.get("pitcher_name")),
                "norm_pitcher_name": clean(r.get("norm_pitcher_name")),
                "listed": "0",
                "candidate_source": "recent_usage",
                "gamePks": set(),
                "recent_pitches": 0,
                "last_date": r["date"],
            },
        )
        g["pitcher_name"] = clean(r.get("pitcher_name")) or g["pitcher_name"]
        g["norm_pitcher_name"] = clean(r.get("norm_pitcher_name")) or g["norm_pitcher_name"]
        g["gamePks"].add(clean(r.get("gamePk")))
        g["recent_pitches"] += int(r.get("pitches") or 0)
        if r["date"] >= g["last_date"]:
            g["last_date"] = r["date"]
    rows = list(grouped.values())
    rows.sort(key=lambda r: (-len(r["gamePks"]), -int(r["recent_pitches"]), str(r.get("pitcher_name"))))
    return [
        {
            "pitcher_id": clean(r["pitcher_id"]),
            "pitcher_name": clean(r["pitcher_name"]),
            "norm_pitcher_name": clean(r["norm_pitcher_name"]),
            "listed": "0",
            "candidate_source": "recent_usage",
        }
        for r in rows[:14]
    ]


def projection_for_team(
    team_row: dict[str, Any],
    daily: pd.DataFrame,
    daily_by_team: dict[str, pd.DataFrame] | None = None,
    daily_records_by_team: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    target_date = clean(team_row["date"])
    team = clean(team_row["team"])
    listed_names = team_row["listed_bullpen_names"]
    target_ts = pd.Timestamp(target_date)
    prior_records: list[dict[str, Any]] | None = None
    records_by_pid: dict[str, list[dict[str, Any]]] | None = None
    if daily_records_by_team is not None and not listed_names:
        team_records = daily_records_by_team.get(team, [])
        prior_records = [r for r in team_records if r["date"] < target_ts]
        w30_records = [r for r in prior_records if r["date"] >= target_ts - pd.Timedelta(days=30)]
        records_by_pid = {}
        for r in prior_records:
            records_by_pid.setdefault(clean(r.get("pitcher_id")), []).append(r)
        pitchers = _candidate_pitchers_from_records(prior_records, listed_names)
    else:
        team_daily = daily_by_team.get(team, pd.DataFrame()) if daily_by_team is not None else daily[daily["team"] == team].copy()
        prior = all_prior_team(team_daily, target_date)
        w30_team = recent_window_team(team_daily, target_date, 30)
        pitchers = resolve_pitchers(prior, listed_names)
    rows: list[dict[str, Any]] = []
    for pinfo in pitchers:
        pid = pinfo["pitcher_id"]
        if records_by_pid is not None:
            pitcher_records = records_by_pid.get(clean(pid), [])
            p30_records = [r for r in w30_records if clean(r.get("pitcher_id")) == clean(pid)]
            role, role_conf = role_from_history_records(p30_records)
            avail = availability_features_records(pitcher_records, target_ts)
            hist = pitcher_history_features_records(pitcher_records, target_ts)
        else:
            p30 = w30_team[w30_team["pitcher_id"].astype(str) == str(pid)]
            role, role_conf = role_from_history(p30)
            avail = availability_features(prior, pid, target_date)
            hist = pitcher_history_features(prior, pid, target_date)
        candidate_source = clean(pinfo.get("candidate_source")) or ("listed_history" if pinfo.get("listed") == "1" else "recent_usage")
        listed_bonus = 7 if pinfo.get("listed") == "1" else 0
        history_bonus = int(num(hist.get("recent_usage_score"), 0) or 0)
        rest_bonus = 0
        days_rest = num(avail.get("days_rest"), None)
        pitches_1d = int(avail.get("pitches_1d") or 0)
        if days_rest is not None:
            if days_rest == 1 and pitches_1d <= 18:
                rest_bonus = 7
            elif days_rest == 2:
                rest_bonus = 10
            elif 3 <= days_rest <= 5:
                rest_bonus = 7
            elif days_rest >= 10 and int(hist.get("apps_14d_hist") or 0) == 0:
                rest_bonus = -8
        short_bonus = 0
        if int(team_row["starter_short_leash_risk"]) and role in {"long_relief", "middle_relief"}:
            short_bonus = 8
        if role in {"closer_or_setup", "late_relief_pool", "high_leverage_bridge"}:
            role_base = 40
        elif role == "middle_late_bridge":
            role_base = 36
        elif role == "long_relief":
            role_base = 26
        elif role == "rotation_or_bulk_depth":
            role_base = 10
        elif role == "middle_relief":
            role_base = 30
        elif role == "mop_up_depth":
            role_base = 18
        else:
            role_base = 8
        unknown_penalty = 14 if candidate_source == "listed_only" and int(hist.get("apps_30d_hist") or 0) == 0 else 0
        score = max(
            0,
            min(
                100,
                round(
                    role_base
                    + role_conf * 12
                    + history_bonus
                    + listed_bonus
                    + rest_bonus
                    + short_bonus
                    - int(avail["availability_penalty"])
                    - unknown_penalty
                ),
            ),
        )
        if avail["availability"] == "likely_unavailable":
            lane = "avoid_projected"
        elif role in {"closer_or_setup", "late_relief_pool"}:
            lane = "late_high_leverage"
        elif role == "high_leverage_bridge":
            lane = "bridge_high_leverage"
        elif role == "middle_late_bridge":
            lane = "middle_late_bridge"
        elif role == "long_relief" or short_bonus:
            lane = "long_or_bulk"
        elif role == "rotation_or_bulk_depth":
            lane = "bulk_depth_emergency"
        elif role == "mop_up_depth":
            lane = "depth_low_leverage"
        else:
            lane = "middle_bridge"
        rows.append(
            {
                "date": target_date,
                "gamePk": clean(team_row["gamePk"]),
                "matchup": clean(team_row["matchup"]),
                "team": team,
                "opp": clean(team_row["opp"]),
                "side": clean(team_row["side"]),
                "starter_name": clean(team_row["starter_name"]),
                "starter_short_leash_risk": int(team_row["starter_short_leash_risk"]),
                "starter_short_leash_reasons": clean(team_row["starter_short_leash_reasons"]),
                "pitcher_id": pid,
                "pitcher_name": pinfo["pitcher_name"],
                "listed_in_slate_bullpen": int(pinfo.get("listed") == "1"),
                "candidate_source": candidate_source,
                "projected_role": role,
                "role_confidence": round(role_conf, 3),
                "usage_lane": lane,
                "appearance_score": score,
                **hist,
                **{k: v for k, v in avail.items() if k != "availability_penalty"},
                "pre_knowable_ok": True,
                "source_v3": clean(team_row["source_v3"]),
            }
        )
    rows.sort(key=lambda r: (-int(r["appearance_score"]), r["pitcher_name"]))
    late_seen = 0
    for r in rows:
        if r["projected_role"] != "late_relief_pool":
            continue
        late_seen += 1
        if late_seen <= 2 and r["availability"] != "likely_unavailable":
            r["projected_role"] = "closer_or_setup"
            r["usage_lane"] = "late_high_leverage"
        elif late_seen <= 4 and r["availability"] != "likely_unavailable":
            r["projected_role"] = "middle_late_bridge"
            r["usage_lane"] = "middle_late_bridge"
            r["appearance_score"] = max(0, int(r["appearance_score"]) - 6)
        else:
            r["projected_role"] = "middle_relief"
            r["usage_lane"] = "middle_bridge"
            r["appearance_score"] = max(0, int(r["appearance_score"]) - 12)

    rows.sort(key=lambda r: (-int(r["appearance_score"]), r["pitcher_name"]))
    selected: list[dict[str, Any]] = []
    remaining = [r for r in rows if r["usage_lane"] != "avoid_projected"]
    lane_targets = [
        {"late_high_leverage", "bridge_high_leverage"},
        {"middle_late_bridge", "middle_bridge"},
        {"long_or_bulk"},
        {"depth_low_leverage", "bulk_depth_emergency"},
    ]
    seen_ids: set[str] = set()
    for lanes in lane_targets:
        prediction = next((r for r in remaining if r["usage_lane"] in lanes and clean(r["pitcher_id"]) not in seen_ids), None)
        if prediction:
            selected.append(prediction)
            seen_ids.add(clean(prediction["pitcher_id"]))
    for r in rows:
        key = clean(r["pitcher_id"])
        if key not in seen_ids:
            selected.append(r)
            seen_ids.add(key)
    rows = selected
    for i, r in enumerate(rows, 1):
        r["team_projection_rank"] = i
    return rows


def team_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    available = [r for r in rows if r["availability"] in {"available", "monitor"}]
    limited = [r for r in rows if r["availability"] == "limited"]
    unavailable = [r for r in rows if r["availability"] == "likely_unavailable"]
    likely = [
        r
        for r in rows
        if r["usage_lane"] not in {"avoid_projected", "bulk_depth_emergency", "depth_low_leverage"}
        and int(r["appearance_score"]) >= 40
    ][:6]
    primary = likely[:4]
    high_lev = [r for r in available if r["usage_lane"] in {"late_high_leverage", "bridge_high_leverage"}]
    long_bulk = [r for r in available if r["usage_lane"] == "long_or_bulk"]
    return {
        "date": rows[0]["date"] if rows else "",
        "gamePk": rows[0]["gamePk"] if rows else "",
        "matchup": rows[0]["matchup"] if rows else "",
        "team": rows[0]["team"] if rows else "",
        "opp": rows[0]["opp"] if rows else "",
        "starter_name": rows[0]["starter_name"] if rows else "",
        "starter_short_leash_risk": rows[0]["starter_short_leash_risk"] if rows else 0,
        "available_or_monitor_count": len(available),
        "limited_count": len(limited),
        "likely_unavailable_count": len(unavailable),
        "fresh_high_leverage_count": len(high_lev),
        "fresh_long_bulk_count": len(long_bulk),
        "primary_relievers": "; ".join(r["pitcher_name"] for r in primary),
        "likely_relievers": "; ".join(r["pitcher_name"] for r in likely),
        "limited_relievers": "; ".join(r["pitcher_name"] for r in limited[:5]),
        "unavailable_relievers": "; ".join(r["pitcher_name"] for r in unavailable[:5]),
        "pre_knowable_ok": True,
    }


def _read_csv_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _merge_existing_by_season(path: Path, rows: list[dict[str, Any]], season: str | None) -> list[dict[str, Any]]:
    if not season:
        return rows
    existing = [row for row in _read_csv_rows(path) if clean(row.get("date"))[:4] != season]
    return existing + rows


def build(target_date: str | None, write: bool, skip_if_current: bool = False, season: str | None = None) -> dict[str, Any]:
    if write and target_date and skip_if_current:
        cached = _date_cache_current(target_date)
        if cached is not None:
            return cached
    stints = load_stints(target_date)
    daily = build_daily_roles(stints)
    v3_files = read_v3_files(target_date, season=season)
    team_targets: list[dict[str, Any]] = []
    for path in v3_files:
        team_targets.extend(team_rows_from_slate(path))
    projections: list[dict[str, Any]] = []
    team_rows: list[dict[str, Any]] = []
    daily_by_team = {clean(team): g.copy() for team, g in daily.groupby("team", dropna=False)} if len(daily) else {}
    daily_records_by_team = _daily_records_by_team(daily)
    for tr in team_targets:
        rows = projection_for_team(tr, daily, daily_by_team=daily_by_team, daily_records_by_team=daily_records_by_team)
        projections.extend(rows)
        if rows:
            team_rows.append(team_summary(rows))

    report = {
        "ok": True,
        "schema": "mlb_fullread_v4_bullpen_appearance_projection.v1",
        "research_only": True,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "target_date": date_iso(target_date) if target_date else "all_v3_files",
        "season": season or "",
        "source_stints": str(STINTS_CSV.relative_to(ROOT)).replace("\\", "/"),
        "daily_role_rows": int(len(daily)),
        "v3_files": [p.name for p in v3_files],
        "source_v3_file": v3_files[0].name if target_date and v3_files else "",
        "source_v3_hash": _file_sha1(v3_files[0]) if target_date and v3_files else "",
        "team_targets": len(team_targets),
        "projection_rows": len(projections),
        "team_projection_rows": len(team_rows),
        "outputs": {
            "daily_roles": str(ROLES_DAILY_CSV.relative_to(ROOT)).replace("\\", "/"),
            "projection": str(PROJECTION_CSV.relative_to(ROOT)).replace("\\", "/"),
            "team_projection": str(TEAM_PROJECTION_CSV.relative_to(ROOT)).replace("\\", "/"),
            "report_json": str(REPORT_JSON.relative_to(ROOT)).replace("\\", "/"),
            "report_md": str(REPORT_MD.relative_to(ROOT)).replace("\\", "/"),
        },
        "guardrails": [
            "Research-only bullpen appearance projection.",
            "All player usage windows use date < target.",
            "Does not touch production predictions, narrator predictions, or Notification.",
            "Appearance scores are descriptive, not automatic prediction filters.",
        ],
    }
    if write:
        daily_out = daily.copy()
        if len(daily_out):
            daily_out["date"] = daily_out["date"].dt.strftime("%Y-%m-%d")
        daily_out.to_csv(ROLES_DAILY_CSV, index=False, encoding="utf-8-sig")
        pd.DataFrame(_merge_existing_by_season(PROJECTION_CSV, projections, season)).to_csv(PROJECTION_CSV, index=False, encoding="utf-8-sig")
        pd.DataFrame(_merge_existing_by_season(TEAM_PROJECTION_CSV, team_rows, season)).to_csv(TEAM_PROJECTION_CSV, index=False, encoding="utf-8-sig")
        REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = [
            "# MLB bullpen appearance projection",
            "",
            "Research-only. All usage windows are `date < target`.",
            "",
            f"- target date: `{report['target_date']}`",
            f"- daily role rows: `{report['daily_role_rows']}`",
            f"- team targets: `{report['team_targets']}`",
            f"- projection rows: `{report['projection_rows']}`",
            f"- team projection rows: `{report['team_projection_rows']}`",
            "",
            "Outputs:",
            "",
            f"- `{report['outputs']['daily_roles']}`",
            f"- `{report['outputs']['projection']}`",
            f"- `{report['outputs']['team_projection']}`",
            f"- `{report['outputs']['report_json']}`",
            "",
            "Guardrails:",
            "",
            "- Research-only.",
            "- No production prediction mutation.",
            "- No narrator prediction mutation.",
            "- No Notification send.",
        ]
        REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="Build research-only MLB bullpen appearance projection.")
    ap.add_argument("--date", help="Optional YYYY-MM-DD target date. Defaults to all _v3_games_*.csv files.")
    ap.add_argument("--season", help="Optional season to rebuild and merge all _v3_games_SEASON-*.csv rows.")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--skip-if-current", action="store_true", help="Skip same-date rebuild when outputs are newer than _v3_games_DATE.csv.")
    args = ap.parse_args()
    if args.date and args.season:
        ap.error("--date and --season are mutually exclusive")
    report = build(args.date, write=args.write, skip_if_current=args.skip_if_current, season=args.season)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
