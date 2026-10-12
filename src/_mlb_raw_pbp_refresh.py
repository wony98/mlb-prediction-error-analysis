# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""Refresh MLB raw StatsAPI play-by-play JSON for final games.

Research/data-layer helper only:
- no Notification
- no prediction mutation
- no narrator output mutation

Writes final-game raw PBP to:
  _deepread_v3/raw_plays/YYYY-MM-DD/{gamePk}.json
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import json
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
OUT_ROOT = ROOT / "_deepread_v3" / "raw_plays"
PARTIAL_DIR = ROOT / "postmortem" / "partial_results"
REPORT_DIR = ROOT / "postmortem" / "raw_pbp_refresh"
STATSAPI_URL = "https://statsapi.mlb.com/api/v1/game/{gamePk}/playByPlay"
SCHEDULE_URL = (
    "https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date}"
)
KST = timezone(timedelta(hours=9))


def norm_date(value: str) -> tuple[str, str]:
    raw = str(value).strip().replace("-", "")
    if len(raw) != 8 or not raw.isdigit():
        raise SystemExit(f"[ERR] invalid date: {value}")
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}", raw


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def load_final_games(date_md: str) -> list[dict[str, Any]]:
    _date_md, ymd = norm_date(date_md)
    path = PARTIAL_DIR / f"{ymd}_mlb_partial_status.json"
    data = read_json(path, {})
    games = []
    for game in data.get("games") or []:
        if str(game.get("state", "")).lower() != "final":
            continue
        game_pk = str(game.get("game_id") or game.get("gamePk") or "").strip()
        if not game_pk:
            continue
        games.append(
            {
                "gamePk": game_pk,
                "away": game.get("away") or "",
                "home": game.get("home") or "",
                "away_score": game.get("away_score"),
                "home_score": game.get("home_score"),
            }
        )
    return games


def load_replay_games(date_md: str, source_path: Path) -> list[dict[str, Any]]:
    """Load target identities from a validated, result-blind replay source."""
    games: list[dict[str, Any]] = []
    with source_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            row_date = str(row.get("date") or "")[:10]
            if row_date != date_md:
                raise ValueError(
                    f"replay source date mismatch: expected {date_md}, got {row_date}"
                )
            game_pk = str(row.get("gamePk") or "").strip()
            if not game_pk:
                raise ValueError("replay source row missing gamePk")
            games.append(
                {
                    "gamePk": game_pk,
                    "away": row.get("away_team") or "",
                    "home": row.get("home_team") or "",
                    "away_score": None,
                    "home_score": None,
                    "require_final": True,
                }
            )
    if not games:
        raise ValueError(f"replay source has no games: {source_path}")
    if len({game["gamePk"] for game in games}) != len(games):
        raise ValueError(f"replay source has duplicate gamePk: {source_path}")
    return games


def load_official_statuses(date_md: str, timeout: int) -> dict[str, dict[str, str]]:
    url = SCHEDULE_URL.format(date=date_md)
    req = urllib.request.Request(
        url, headers={"User-Agent": "kbo-step6-raw-pbp-refresh/1.1"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return {
        str(game.get("gamePk")): {
            "detailed": str((game.get("status") or {}).get("detailedState") or ""),
            "abstract": str((game.get("status") or {}).get("abstractGameState") or ""),
        }
        for date_row in payload.get("dates") or []
        for game in date_row.get("games") or []
    }


def load_official_final_games(date_md: str, timeout: int) -> list[dict[str, Any]]:
    """Read only game identity/team/status from the official schedule endpoint."""
    url = SCHEDULE_URL.format(date=date_md)
    req = urllib.request.Request(
        url, headers={"User-Agent": "kbo-step6-raw-pbp-refresh/1.2"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    games: list[dict[str, Any]] = []
    for date_row in payload.get("dates") or []:
        for game in date_row.get("games") or []:
            status = game.get("status") or {}
            if str(status.get("abstractGameState") or "") != "Final":
                continue
            teams = game.get("teams") or {}
            games.append(
                {
                    "gamePk": str(game["gamePk"]),
                    "away": ((teams.get("away") or {}).get("team") or {}).get(
                        "name", ""
                    ),
                    "home": ((teams.get("home") or {}).get("team") or {}).get(
                        "name", ""
                    ),
                    "away_score": None,
                    "home_score": None,
                    "require_final": True,
                    "official_status": status.get("detailedState"),
                    "official_abstract": status.get("abstractGameState"),
                }
            )
    return games


def target_path(date_md: str, game_pk: str) -> Path:
    return OUT_ROOT / date_md / f"{game_pk}.json"


def fetch_one(date_md: str, game: dict[str, Any], write: bool, force: bool, timeout: int) -> dict[str, Any]:
    game_pk = str(game["gamePk"])
    out = target_path(date_md, game_pk)
    if game.get("require_final") and game.get("official_abstract") != "Final":
        return {
            "gamePk": game_pk,
            "matchup": f"{game.get('away')} @ {game.get('home')}",
            "status": "not_final_skipped",
            "official_status": game.get("official_status") or "UNKNOWN",
            "official_abstract": game.get("official_abstract") or "UNKNOWN",
            "path": str(out),
        }
    if out.exists() and out.stat().st_size > 0 and not force:
        return {
            "gamePk": game_pk,
            "matchup": f"{game.get('away')} @ {game.get('home')}",
            "status": "exists",
            "path": str(out),
        }
    if not write:
        return {
            "gamePk": game_pk,
            "matchup": f"{game.get('away')} @ {game.get('home')}",
            "status": "would_fetch",
            "path": str(out),
        }
    url = STATSAPI_URL.format(gamePk=game_pk)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "kbo-step6-raw-pbp-refresh/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        plays = payload.get("allPlays") or []
        if not plays:
            return {
                "gamePk": game_pk,
                "matchup": f"{game.get('away')} @ {game.get('home')}",
                "status": "empty_allPlays",
                "url": url,
            }
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "gamePk": game_pk,
            "matchup": f"{game.get('away')} @ {game.get('home')}",
            "status": "fetched",
            "path": str(out),
            "plays": len(plays),
        }
    except Exception as exc:
        return {
            "gamePk": game_pk,
            "matchup": f"{game.get('away')} @ {game.get('home')}",
            "status": "fetch_failed",
            "error": repr(exc),
            "url": url,
        }


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Refresh MLB raw StatsAPI PBP JSON for final games.")
    ap.add_argument("date", help="YYYY-MM-DD or YYYYMMDD")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--timeout", type=int, default=25)
    ap.add_argument(
        "--replay-source",
        type=Path,
        help=(
            "Validated frozen_pregame_sources CSV to supply game identities "
            "when production partial-status files must remain untouched."
        ),
    )
    ap.add_argument(
        "--official-schedule",
        action="store_true",
        help=(
            "Use only gamePk/team/final status from the official schedule when "
            "the production partial-status file is absent."
        ),
    )
    args = ap.parse_args()

    if args.replay_source and args.official_schedule:
        ap.error("--replay-source and --official-schedule are mutually exclusive")

    date_md, ymd = norm_date(args.date)
    if args.replay_source:
        games = load_replay_games(date_md, args.replay_source)
    elif args.official_schedule:
        games = load_official_final_games(date_md, args.timeout)
    else:
        games = load_final_games(date_md)
    if args.replay_source:
        statuses = load_official_statuses(date_md, args.timeout)
        missing_status = [
            game["gamePk"] for game in games if game["gamePk"] not in statuses
        ]
        if missing_status:
            raise RuntimeError(f"official status missing for gamePks: {missing_status}")
        for game in games:
            game["official_status"] = statuses[game["gamePk"]]["detailed"]
            game["official_abstract"] = statuses[game["gamePk"]]["abstract"]
    if not games:
        print(f"[raw_pbp] {date_md}: final games 0 or partial status missing")
        return 0

    results: list[dict[str, Any]] = []
    with cf.ThreadPoolExecutor(max_workers=max(1, min(args.workers, len(games)))) as ex:
        futs = {ex.submit(fetch_one, date_md, g, args.write, args.force, args.timeout): g for g in games}
        for fut in cf.as_completed(futs):
            res = fut.result()
            results.append(res)
            print(f"[raw_pbp] {res.get('gamePk')} {res.get('status')} {res.get('path', '')}")

    counts: dict[str, int] = {}
    for res in results:
        counts[res["status"]] = counts.get(res["status"], 0) + 1
    report = {
        "schema": "mlb_raw_pbp_refresh.v1",
        "generated_at_kst": datetime.now(KST).replace(microsecond=0).isoformat(),
        "date": date_md,
        "source_mode": (
            "replay_source"
            if args.replay_source
            else "official_schedule"
            if args.official_schedule
            else "partial_status"
        ),
        "write": bool(args.write),
        "force": bool(args.force),
        "target_games": len(games),
        "final_games": sum(
            not game.get("require_final") or game.get("official_abstract") == "Final"
            for game in games
        ),
        "counts": counts,
        "results": sorted(results, key=lambda r: str(r.get("gamePk"))),
    }
    if args.write:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = REPORT_DIR / f"{ymd}_mlb_raw_pbp_refresh_report.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[raw_pbp] wrote {path}")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))

    failed = counts.get("fetch_failed", 0) + counts.get("empty_allPlays", 0)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
