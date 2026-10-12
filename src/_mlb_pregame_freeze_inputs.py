# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests


ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "postmortem" / "automation_logs" / "colab_offload" / "input_snapshots"
MLB_API = "https://statsapi.mlb.com/api/v1"

MARKET_PRICE_COLUMNS = [
    "win_loss_home_open_imp",
    "win_loss_away_open_imp",
    "win_loss_home_close_imp",
    "win_loss_away_close_imp",
    "win_loss_home_imp_shift_max",
    "win_loss_home_imp_shift_min",
    "win_loss_home_n_markets",
    "win_loss_home_close_std",
    "win_loss_home_n_down",
    "win_loss_home_n_up",
    "run_total_upper_open_imp_avg",
    "run_total_upper_close_imp_avg",
    "run_total_upper_imp_shift_avg",
    "run_total_n_markets_upper",
    "run_total_lower_open_imp_avg",
    "run_total_lower_close_imp_avg",
    "run_total_lower_imp_shift_avg",
    "run_total_main_baseline",
    "run_total_dominant_baseline",
    "run_total_upper_market_fee_shift",
    "run_total_baseline_balance_gap",
    "run_total_baseline_candidate_count",
    "run_total_baseline_selection_method",
]

PRIMARY_COVERAGE_COLUMNS = [
    "win_loss_home_open_imp",
    "win_loss_away_open_imp",
    "win_loss_home_close_imp",
    "win_loss_away_close_imp",
    "run_total_upper_open_imp_avg",
    "run_total_lower_open_imp_avg",
    "run_total_main_baseline",
]

WEATHER_COLUMNS = [
    "venue",
    "temperature",
    "weather_condition",
    "wind_speed",
    "wind_direction",
    "precip_prob",
    "precip_mm",
    "dome",
    "temp_c",
    "wind_kmh",
    "hp_ump_name",
]

PRIMARY_WEATHER_COLUMNS = [
    "wind_kmh",
    "precip_mm",
]


def norm(value) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower()) if value is not None else ""


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def clean_value(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return float(value)
    if hasattr(value, "item"):
        try:
            return clean_value(value.item())
        except Exception:
            pass
    return value


def clean_record(record: dict) -> dict:
    return {str(k): clean_value(v) for k, v in record.items()}


def normalize_date(value: str) -> str:
    raw = str(value).strip()
    if re.fullmatch(r"\d{8}", raw):
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw
    raise SystemExit(f"date must be YYYY-MM-DD or YYYYMMDD, got {value!r}")


def parse_gamepks(value: str | None) -> set[int]:
    out: set[int] = set()
    for part in str(value or "").replace(";", ",").split(","):
        raw = part.strip()
        if not raw:
            continue
        try:
            out.add(int(float(raw)))
        except Exception:
            raise SystemExit(f"invalid gamePk in --gamepks: {raw!r}")
    return out


def load_target_games(date: str, gamepks: set[int] | None = None) -> pd.DataFrame:
    v3_path = ROOT / f"_v3_games_{date}.csv"
    if v3_path.exists():
        df = pd.read_csv(v3_path)
        cols = [c for c in ["gamePk", "home_team", "away_team", "status"] if c in df.columns]
        out = df[cols].copy()
        out["source"] = str(v3_path)
    else:
        url = f"{MLB_API}/schedule"
        params = {"sportId": 1, "date": date}
        data = requests.get(url, params=params, timeout=30).json()
        rows = []
        for day in data.get("dates", []):
            for game in day.get("games", []):
                teams = game.get("teams") or {}
                rows.append(
                    {
                        "gamePk": game.get("gamePk"),
                        "home_team": ((teams.get("home") or {}).get("team") or {}).get("name"),
                        "away_team": ((teams.get("away") or {}).get("team") or {}).get("name"),
                        "status": ((game.get("status") or {}).get("detailedState")),
                        "source": "statsapi_schedule",
                    }
                )
        out = pd.DataFrame(rows)
    if out.empty:
        raise SystemExit(f"no target games found for {date}")
    out["gamePk"] = pd.to_numeric(out["gamePk"], errors="coerce").astype("Int64")
    if gamepks:
        before = len(out)
        out = out[out["gamePk"].astype("Int64").isin(gamepks)].copy()
        if out.empty:
            raise SystemExit(f"--gamepks matched 0/{before} target games for {date}: {sorted(gamepks)}")
    out["hn"] = out["home_team"].map(norm)
    out["an"] = out["away_team"].map(norm)
    return out.drop_duplicates(["gamePk"], keep="first")


def market_price_from_v3(date: str) -> tuple[pd.DataFrame, dict]:
    path = ROOT / f"_v3_games_{date}.csv"
    if not path.exists():
        raise SystemExit(f"source v3 requested but missing: {path}")
    df = pd.read_csv(path)
    keep = ["home_team", "away_team", "gamePk"] + [c for c in MARKET_PRICE_COLUMNS if c in df.columns]
    out = ensure_market_feature_aliases(df[keep].copy())
    out["hn"] = out["home_team"].map(norm)
    out["an"] = out["away_team"].map(norm)
    return out, {"mode": "v3", "path": str(path)}


def weather_from_v3(date: str) -> tuple[pd.DataFrame, dict]:
    path = ROOT / f"_v3_games_{date}.csv"
    if not path.exists():
        return pd.DataFrame(), {"mode": "v3", "path": str(path), "missing": True}
    df = pd.read_csv(path)
    keep = ["gamePk", "home_team", "away_team"] + [c for c in WEATHER_COLUMNS if c in df.columns]
    if "gamePk" not in df.columns:
        return pd.DataFrame(), {"mode": "v3", "path": str(path), "missing_gamePk": True}
    out = df[keep].copy()
    out["gamePk"] = pd.to_numeric(out["gamePk"], errors="coerce").astype("Int64")
    return out, {"mode": "v3", "path": str(path)}


def market_price_from_csv(path: Path) -> tuple[pd.DataFrame, dict]:
    if not path.exists():
        raise SystemExit(f"market_price csv missing: {path}")
    df = pd.read_csv(path)
    if "home_team" not in df.columns or "away_team" not in df.columns:
        raise SystemExit("market_price csv must contain home_team and away_team columns")
    df = ensure_market_feature_aliases(df.copy())
    df["hn"] = df["home_team"].map(norm)
    df["an"] = df["away_team"].map(norm)
    return df, {"mode": "csv", "path": str(path)}


def american_to_implied(values: pd.Series) -> pd.Series:
    market_price = pd.to_numeric(values, errors="coerce")
    positive = 100.0 / (market_price + 100.0)
    negative = (-market_price) / ((-market_price) + 100.0)
    return positive.where(market_price >= 0, negative).where(market_price.notna())


def ensure_market_feature_aliases(df: pd.DataFrame) -> pd.DataFrame:
    """Derive snapshot feature aliases from frozen basic market columns."""
    out = df.copy()
    for side in ("home", "away"):
        basic = f"{side}_WIN_LOSS"
        if basic not in out.columns:
            continue
        implied = american_to_implied(out[basic])
        for suffix in ("open_imp", "close_imp"):
            column = f"win_loss_{side}_{suffix}"
            if column not in out.columns:
                out[column] = implied
            else:
                out[column] = pd.to_numeric(out[column], errors="coerce").combine_first(implied)
    for side in ("upper", "lower"):
        basic = f"{side}_win_loss"
        if basic not in out.columns:
            continue
        implied = american_to_implied(out[basic])
        for suffix in ("open_imp_avg", "close_imp_avg"):
            column = f"run_total_{side}_{suffix}"
            if column not in out.columns:
                out[column] = implied
            else:
                out[column] = pd.to_numeric(out[column], errors="coerce").combine_first(implied)
    if "run_total_baseline" in out.columns:
        line = pd.to_numeric(out["run_total_baseline"], errors="coerce")
        for column in ("run_total_main_baseline", "run_total_dominant_baseline"):
            if column not in out.columns:
                out[column] = line
            else:
                out[column] = pd.to_numeric(out[column], errors="coerce").combine_first(line)
    return out


def weather_from_csv(path: Path) -> tuple[pd.DataFrame, dict]:
    if not path.exists():
        return pd.DataFrame(), {"mode": "csv", "path": str(path), "missing": True}
    df = pd.read_csv(path)
    if "gamePk" not in df.columns:
        return pd.DataFrame(), {"mode": "csv", "path": str(path), "missing_gamePk": True}
    keep = ["gamePk"] + [c for c in ["home_team", "away_team"] if c in df.columns] + [c for c in WEATHER_COLUMNS if c in df.columns]
    out = df[keep].copy()
    out["gamePk"] = pd.to_numeric(out["gamePk"], errors="coerce").astype("Int64")
    return out, {"mode": "csv", "path": str(path)}


def market_price_from_live(*args, **kwargs):
    raise RuntimeError('Live market collection is not included. Supply the reviewed CSV/snapshot input.')


def build_snapshot(
    date: str,
    source: str,
    market_price_csv: Path | None,
    market_price_stale_after_minutes: int,
    weather_stale_after_minutes: int,
    gamepks: set[int] | None = None,
) -> dict:
    target_games = load_target_games(date, gamepks=gamepks)
    if source == "v3":
        market_price, source_meta = market_price_from_v3(date)
        weather, weather_source_meta = weather_from_v3(date)
    elif source == "csv":
        if market_price_csv is None:
            raise SystemExit("--market_price-csv is required with --source csv")
        market_price, source_meta = market_price_from_csv(market_price_csv)
        weather, weather_source_meta = weather_from_csv(market_price_csv)
    elif source == "live":
        market_price, source_meta = market_price_from_live()
        weather = pd.DataFrame()
        weather_source_meta = {"mode": "none", "reason": "live market_price source does not provide weather freeze rows"}
    else:
        raise SystemExit(f"unsupported source: {source}")

    if market_price.empty:
        market_price = pd.DataFrame(columns=["home_team", "away_team", "hn", "an", *MARKET_PRICE_COLUMNS])
    if "hn" not in market_price.columns:
        market_price["hn"] = market_price.get("home_team", "").map(norm)
    if "an" not in market_price.columns:
        market_price["an"] = market_price.get("away_team", "").map(norm)
    market_price = market_price.drop_duplicates(["hn", "an"], keep="first")

    merged = target_games[["gamePk", "home_team", "away_team", "hn", "an"]].merge(
        market_price[["hn", "an"] + [c for c in MARKET_PRICE_COLUMNS if c in market_price.columns]],
        on=["hn", "an"],
        how="left",
        suffixes=("", "_market_price"),
    )
    present_cols = [c for c in PRIMARY_COVERAGE_COLUMNS if c in merged.columns]
    if present_cols:
        matched_mask = merged[present_cols].notna().any(axis=1)
    else:
        matched_mask = pd.Series(False, index=merged.index)
    missing = merged.loc[~matched_mask, ["gamePk", "away_team", "home_team"]]

    empty_numeric = pd.Series(float("nan"), index=merged.index, dtype="float64")
    upper_imp = pd.to_numeric(
        merged["run_total_upper_close_imp_avg"], errors="coerce"
    ) if "run_total_upper_close_imp_avg" in merged else empty_numeric.copy()
    lower_imp = pd.to_numeric(
        merged["run_total_lower_close_imp_avg"], errors="coerce"
    ) if "run_total_lower_close_imp_avg" in merged else empty_numeric.copy()
    main_baseline = pd.to_numeric(
        merged["run_total_main_baseline"], errors="coerce"
    ) if "run_total_main_baseline" in merged else empty_numeric.copy()
    run_total_balance_gap = (upper_imp - lower_imp).abs()
    has_run_total_baseline = main_baseline.notna()
    invalid_run_total_mask = has_run_total_baseline & run_total_balance_gap.gt(0.12)
    invalid_run_total = merged.loc[invalid_run_total_mask, ["gamePk", "away_team", "home_team"]].copy()

    if weather.empty:
        weather = pd.DataFrame(columns=["gamePk", *WEATHER_COLUMNS])
    weather = weather.drop_duplicates(["gamePk"], keep="first") if "gamePk" in weather.columns else weather
    weather_cols = [c for c in WEATHER_COLUMNS if c in weather.columns]
    if "gamePk" in weather.columns and weather_cols:
        weather_join = target_games[["gamePk", "away_team", "home_team"]].merge(
            weather[["gamePk"] + weather_cols],
            on="gamePk",
            how="left",
        )
        weather_present_cols = [c for c in PRIMARY_WEATHER_COLUMNS if c in weather_join.columns]
        if weather_present_cols:
            weather_matched_mask = weather_join[weather_present_cols].notna().any(axis=1)
        else:
            weather_matched_mask = pd.Series(False, index=weather_join.index)
    else:
        weather_join = target_games[["gamePk", "away_team", "home_team"]].copy()
        weather_present_cols = []
        weather_matched_mask = pd.Series(False, index=weather_join.index)
    weather_missing = weather_join.loc[~weather_matched_mask, ["gamePk", "away_team", "home_team"]]
    weather_records = (
        weather[["gamePk"] + [c for c in ["home_team", "away_team"] if c in weather.columns] + weather_cols]
        .to_dict("records")
        if "gamePk" in weather.columns
        else []
    )

    created = datetime.now(timezone.utc).replace(microsecond=0)
    market_price_expires = created + timedelta(minutes=market_price_stale_after_minutes)
    weather_expires = created + timedelta(minutes=weather_stale_after_minutes)
    expires = min(market_price_expires, weather_expires)
    market_price_cols = [c for c in MARKET_PRICE_COLUMNS if c in market_price.columns]
    market_price_records = market_price[["home_team", "away_team", "hn", "an"] + market_price_cols].to_dict("records")
    game_records = target_games[["gamePk", "home_team", "away_team", "hn", "an", "status", "source"]].to_dict("records")

    return {
        "schema": "mlb_pregame_input_snapshot.v1",
        "target_date": date,
        "gamepks_filter": sorted(int(x) for x in gamepks) if gamepks else [],
        "created_utc": created.isoformat().replace("+00:00", "Z"),
        "stale_after_minutes": int(min(market_price_stale_after_minutes, weather_stale_after_minutes)),
        "expires_utc": expires.isoformat().replace("+00:00", "Z"),
        "ttl": {
            "market_price_stale_after_minutes": int(market_price_stale_after_minutes),
            "weather_stale_after_minutes": int(weather_stale_after_minutes),
            "market_price_expires_utc": market_price_expires.isoformat().replace("+00:00", "Z"),
            "weather_expires_utc": weather_expires.isoformat().replace("+00:00", "Z"),
        },
        "source": source_meta,
        "single_freeze_rule": "This one file must be consumed by both local reference and remote payload; do not re-freeze separately.",
        "target_games": [clean_record(r) for r in game_records],
        "market_price": {
            "stale_after_minutes": int(market_price_stale_after_minutes),
            "expires_utc": market_price_expires.isoformat().replace("+00:00", "Z"),
            "columns": market_price_cols,
            "rows": [clean_record(r) for r in market_price_records],
        },
        "weather": {
            "source": weather_source_meta,
            "stale_after_minutes": int(weather_stale_after_minutes),
            "expires_utc": weather_expires.isoformat().replace("+00:00", "Z"),
            "columns": weather_cols,
            "rows": [clean_record(r) for r in weather_records],
        },
        "coverage": {
            "target_games": int(len(target_games)),
            "market_price_rows": int(len(market_price)),
            "matched_games": int(matched_mask.sum()),
            "missing_games": int((~matched_mask).sum()),
            "missing_gamepks": [int(x) for x in missing["gamePk"].dropna().tolist()],
            "missing_matchups": [
                f"{r.away_team} @ {r.home_team}" for _, r in missing.iterrows()
            ],
            "required_columns_present": present_cols,
            "market_price_coverage_ok": bool((~matched_mask).sum() == 0),
            "run_total_integrity_ok": bool(invalid_run_total_mask.sum() == 0),
            "invalid_run_total_games": int(invalid_run_total_mask.sum()),
            "invalid_run_total_gamepks": [int(x) for x in invalid_run_total["gamePk"].dropna().tolist()],
            "invalid_run_total_matchups": [
                f"{r.away_team} @ {r.home_team}" for _, r in invalid_run_total.iterrows()
            ],
            "weather_rows": int(len(weather_records)),
            "weather_matched_games": int(weather_matched_mask.sum()),
            "weather_missing_games": int((~weather_matched_mask).sum()),
            "weather_missing_gamepks": [int(x) for x in weather_missing["gamePk"].dropna().tolist()],
            "weather_missing_matchups": [
                f"{r.away_team} @ {r.home_team}" for _, r in weather_missing.iterrows()
            ],
            "weather_required_columns_present": weather_present_cols,
            "weather_coverage_ok": bool((~weather_matched_mask).sum() == 0),
            "coverage_ok": bool((~matched_mask).sum() == 0 and invalid_run_total_mask.sum() == 0),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Freeze pregame volatile MLB inputs for Colab/local parity.")
    ap.add_argument("--date", required=True, help="MLB date as YYYY-MM-DD or YYYYMMDD.")
    ap.add_argument("--source", choices=["live", "v3", "csv"], default="live")
    ap.add_argument("--market_price-csv", help="Optional market_price CSV source for --source csv.")
    ap.add_argument("--gamepks", default="", help="Optional comma-separated gamePk subset to freeze.")
    ap.add_argument("--stale-after-minutes", type=int, default=None, help="Backward-compatible TTL applied to both market_price and weather if split TTL flags are omitted.")
    ap.add_argument("--market_price-stale-after-minutes", type=int, default=None, help="TTL for frozen market_price. Production default is 90 minutes.")
    ap.add_argument("--weather-stale-after-minutes", type=int, default=None, help="TTL for frozen weather/umpire rows. Production default is 1440 minutes.")
    ap.add_argument("--out", help="Output JSON path. Defaults under postmortem/automation_logs/colab_offload/input_snapshots.")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--allow-missing-market_price", action="store_true", help="Write even when target-game market_price coverage is incomplete.")
    ap.add_argument(
        "--allow-unknown-weather-replay",
        action="store_true",
        help="Research replay only: record that no frozen historical pregame weather exists.",
    )
    args = ap.parse_args()

    date = normalize_date(args.date)
    base_ttl = args.stale_after_minutes
    market_price_ttl = args.market_price_stale_after_minutes if args.market_price_stale_after_minutes is not None else (base_ttl if base_ttl is not None else 90)
    weather_ttl = args.weather_stale_after_minutes if args.weather_stale_after_minutes is not None else (base_ttl if base_ttl is not None else 1440)
    snapshot = build_snapshot(
        date,
        args.source,
        Path(args.market_price_csv) if args.market_price_csv else None,
        market_price_ttl,
        weather_ttl,
        parse_gamepks(args.gamepks),
    )
    if args.allow_unknown_weather_replay:
        replay_date = str(os.environ.get("MLB_REPLAY_ASOF_DATE") or "").strip()
        if replay_date != date:
            raise SystemExit(
                "--allow-unknown-weather-replay requires MLB_REPLAY_ASOF_DATE "
                f"to equal {date}"
            )
        snapshot["weather"]["rows"] = []
        snapshot["weather"]["columns"] = []
        snapshot["coverage"]["weather_rows"] = 0
        snapshot["coverage"]["weather_matched_games"] = 0
        snapshot["coverage"]["weather_missing_games"] = int(
            snapshot["coverage"]["target_games"]
        )
        snapshot["coverage"]["weather_missing_gamepks"] = list(
            snapshot["gamepks_filter"]
        )
        snapshot["coverage"]["weather_coverage_ok"] = False
        snapshot["replay_weather_policy"] = {
            "mode": "UNKNOWN",
            "reason": "NO_FROZEN_HISTORICAL_PREGAME_FORECAST",
            "same_for_all_arms": True,
            "research_only": True,
        }
    coverage_ok = bool((snapshot.get("coverage") or {}).get("coverage_ok"))

    if args.out:
        out = Path(args.out)
        if not out.is_absolute():
            out = ROOT / out
    else:
        out = OUT_DIR / f"mlb_pregame_input_snapshot_{date}_{utc_stamp()}.json"

    print(
        json.dumps(
            {
                "date": date,
                "source": snapshot["source"],
                "ttl": snapshot["ttl"],
                "coverage": snapshot["coverage"],
                "write": bool(args.write),
                "out": str(out),
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    if not coverage_ok and not args.allow_missing_market_price:
        print("[freeze-inputs] coverage incomplete; refusing to write without --allow-missing-market_price", file=sys.stderr)
        return 2

    if args.write:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        digest = file_sha256(out)
        out.with_suffix(out.suffix + ".sha256").write_text(digest + "\n", encoding="utf-8")
        print(f"[freeze-inputs] wrote={out}")
        print(f"[freeze-inputs] sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
