# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date as Date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
POST = ROOT / "postmortem" / "fullread_v4"
MANIFEST = POST / "mlb_fullread_v4_manifest.csv"
OUT_DIR = Path(os.environ.get("MLB_BVP_OUT_DIR") or POST / "bvp_matchup")

PAIR_DAILY_CSV = OUT_DIR / "mlb_bvp_pair_daily.csv"
PAIR_TOTAL_CSV = OUT_DIR / "mlb_bvp_pair_totals.csv"
REPORT_JSON = OUT_DIR / "mlb_bvp_matchup_ledger_report.json"
REPORT_MD = OUT_DIR / "mlb_bvp_matchup_ledger_report.md"

BAD = {"", "nan", "none", "null", "na", "n/a"}
HIT_EVENTS = {"single", "double", "triple", "home_run"}
BB_EVENTS = {"walk", "intent_walk"}
HBP_EVENTS = {"hit_by_pitch"}
K_EVENTS = {"strikeout", "strikeout_double_play"}
SAC_EVENTS = {"sac_fly", "sac_bunt", "sac_fly_double_play", "sac_bunt_double_play"}
EXCLUDE_AB_EVENTS = BB_EVENTS | HBP_EVENTS | SAC_EVENTS | {"catcher_interf"}


@dataclass(frozen=True)
class SourceGame:
    date: str
    game_pk: str
    path: Path
    source: str
    season: str


def clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in BAD else text


def norm_name(name: Any) -> str:
    text = clean(name)
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def split_names(value: Any) -> list[str]:
    from _mlb_name_list import parse_name_list

    return parse_name_list(value)


def safe_int(value: Any) -> int | None:
    text = clean(value)
    if not text:
        return None
    try:
        x = int(float(text))
        return x if x > 0 else None
    except Exception:
        return None


def safe_float(value: Any) -> float | None:
    text = clean(value)
    if not text:
        return None
    try:
        x = float(text)
        return None if math.isnan(x) else x
    except Exception:
        return None


def parse_date(value: Any) -> str:
    text = clean(value)[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except Exception:
        return ""


def date_obj(value: str) -> Date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def get_all_plays(raw: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(raw.get("allPlays"), list):
        return raw["allPlays"]
    return (((raw.get("liveData") or {}).get("plays") or {}).get("allPlays") or [])


def read_manifest_sources(start_season: int, end_season: int) -> list[SourceGame]:
    out: list[SourceGame] = []
    if not MANIFEST.exists():
        return out
    with MANIFEST.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            season = clean(row.get("season"))
            if not season.isdigit() or not (start_season <= int(season) <= end_season):
                continue
            d = parse_date(row.get("date"))
            game_pk = clean(row.get("gamePk"))
            raw_path = Path(clean(row.get("raw_pbp_path")))
            if not d or not game_pk or not raw_path.exists():
                continue
            out.append(SourceGame(d, game_pk, raw_path, "manifest", season))
    return out


def read_deepread_v3_sources(start_season: int, end_season: int) -> list[SourceGame]:
    out: list[SourceGame] = []
    root = ROOT / "_deepread_v3" / "raw_plays"
    if not root.exists():
        return out
    for date_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        d = parse_date(date_dir.name)
        if not d:
            continue
        season = d[:4]
        if not season.isdigit() or not (start_season <= int(season) <= end_season):
            continue
        for path in sorted(date_dir.glob("*.json")):
            out.append(SourceGame(d, path.stem, path, "deepread_v3", season))
    return out


def source_games(start_season: int, end_season: int) -> list[SourceGame]:
    seen: set[tuple[str, str]] = set()
    out: list[SourceGame] = []
    for src in [*read_manifest_sources(start_season, end_season), *read_deepread_v3_sources(start_season, end_season)]:
        key = (src.date, src.game_pk)
        if key in seen:
            continue
        seen.add(key)
        out.append(src)
    out.sort(key=lambda x: (x.date, x.game_pk))
    return out


def event_stats(event_type: str) -> dict[str, int]:
    ev = clean(event_type).lower()
    hit = 1 if ev in HIT_EVENTS else 0
    tb = {"single": 1, "double": 2, "triple": 3, "home_run": 4}.get(ev, 0)
    return {
        "pa": 1,
        "ab": 0 if ev in EXCLUDE_AB_EVENTS else 1,
        "h": hit,
        "tb": tb,
        "hr": 1 if ev == "home_run" else 0,
        "bb": 1 if ev in BB_EVENTS else 0,
        "hbp": 1 if ev in HBP_EVENTS else 0,
        "k": 1 if ev in K_EVENTS else 0,
    }


def final_pitch(play: dict[str, Any]) -> dict[str, Any]:
    for ev in reversed(play.get("playEvents") or []):
        if ev.get("isPitch"):
            return ev
    return {}


def contact_stats(play: dict[str, Any]) -> dict[str, Any]:
    ev = final_pitch(play)
    hit_data = ev.get("hitData") or {}
    ev_speed = safe_float(hit_data.get("launchSpeed"))
    launch_angle = safe_float(hit_data.get("launchAngle"))
    trajectory = clean(hit_data.get("trajectory"))
    bbe = 1 if ev_speed is not None or launch_angle is not None or trajectory else 0
    return {
        "bbe": bbe,
        "hard_hit": 1 if ev_speed is not None and ev_speed >= 95.0 else 0,
        "max_ev": ev_speed,
        "last_pitch_type": clean(((ev.get("details") or {}).get("type") or {}).get("description")),
    }


def add_counter(dst: Counter[str], src: dict[str, Any]) -> None:
    for key, value in src.items():
        if key == "max_ev":
            if value is not None:
                dst[key] = max(float(dst.get(key) or 0.0), float(value))
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            dst[key] += value


def rate(num: Any, den: Any) -> float | None:
    try:
        n = float(num or 0)
        d = float(den or 0)
        return n / d if d else None
    except Exception:
        return None


def stat_row(c: Counter[str]) -> dict[str, Any]:
    pa = int(c.get("pa") or 0)
    ab = int(c.get("ab") or 0)
    h = int(c.get("h") or 0)
    bb = int(c.get("bb") or 0)
    hbp = int(c.get("hbp") or 0)
    tb = int(c.get("tb") or 0)
    obp = rate(h + bb + hbp, pa)
    slg = rate(tb, ab)
    return {
        "pa": pa,
        "ab": ab,
        "h": h,
        "tb": tb,
        "hr": int(c.get("hr") or 0),
        "bb": bb,
        "hbp": hbp,
        "k": int(c.get("k") or 0),
        "bbe": int(c.get("bbe") or 0),
        "hard_hit": int(c.get("hard_hit") or 0),
        "avg": rate(h, ab),
        "obp": obp,
        "slg": slg,
        "ops": (obp + slg) if obp is not None and slg is not None else None,
        "hr_per_pa": rate(c.get("hr"), pa),
        "k_rate": rate(c.get("k"), pa),
        "bb_rate": rate(bb, pa),
        "hard_hit_rate": rate(c.get("hard_hit"), c.get("bbe")),
        "max_ev": c.get("max_ev") if c.get("max_ev") else None,
    }


def pa_bucket(pa: int) -> str:
    if pa <= 0:
        return "no_prior_pair"
    if pa <= 9:
        return "display_1_9"
    if pa <= 24:
        return "observation_10_24"
    return "support_25_plus"


def usage_lane(bucket: str) -> str:
    return {
        "no_prior_pair": "no_direct_history",
        "display_1_9": "display_only_pressure_0",
        "observation_10_24": "observation_standalone_forbidden",
        "support_25_plus": "supporting_only_with_corroboration",
        "official_probable_missing": "display_only_official_probable_missing",
    }.get(bucket, "display_only_pressure_0")


def compact_event(src: SourceGame, play: dict[str, Any]) -> dict[str, Any]:
    result = play.get("result") or {}
    about = play.get("about") or {}
    ev = final_pitch(play)
    details = ev.get("details") or {}
    hit_data = ev.get("hitData") or {}
    return {
        "date": src.date,
        "gamePk": src.game_pk,
        "inning": about.get("inning"),
        "half": about.get("halfInning"),
        "event": result.get("event"),
        "eventType": result.get("eventType"),
        "description": clean(result.get("description"))[:220],
        "pitch_type": clean((details.get("type") or {}).get("description")),
        "launchSpeed": hit_data.get("launchSpeed"),
        "launchAngle": hit_data.get("launchAngle"),
        "trajectory": hit_data.get("trajectory"),
    }


def build_ledger(
    sources: list[SourceGame],
    target_date: str | None,
) -> tuple[
    list[dict[str, Any]],
    dict[tuple[int, int], Counter[str]],
    dict[tuple[int, int], Counter[str]],
    dict[tuple[int, int], list[dict[str, Any]]],
    dict[tuple[int, int], str],
    dict[str, Counter[int]],
    dict[int, str],
    dict[int, Counter[str]],
    dict[str, Any],
]:
    target = date_obj(target_date) if target_date else None
    last2_start = target - timedelta(days=730) if target else None
    daily: dict[tuple[str, int, int], Counter[str]] = defaultdict(Counter)
    total: dict[tuple[int, int], Counter[str]] = defaultdict(Counter)
    last2yr: dict[tuple[int, int], Counter[str]] = defaultdict(Counter)
    latest_date: dict[tuple[int, int], str] = {}
    recent_events: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    name_to_ids: dict[str, Counter[int]] = defaultdict(Counter)
    id_to_names: dict[int, Counter[str]] = defaultdict(Counter)
    pitcher_hand: dict[int, Counter[str]] = defaultdict(Counter)

    parsed_games = 0
    skipped_target_or_future = 0
    parse_errors = 0
    total_plays = 0
    source_dates: list[str] = []

    for src in sources:
        if target and date_obj(src.date) >= target:
            skipped_target_or_future += 1
            continue
        try:
            raw = json.loads(src.path.read_text(encoding="utf-8-sig"))
        except Exception:
            parse_errors += 1
            continue
        plays = get_all_plays(raw)
        if not plays:
            continue
        parsed_games += 1
        source_dates.append(src.date)
        for play in plays:
            matchup = play.get("matchup") or {}
            result = play.get("result") or {}
            batter = matchup.get("batter") or {}
            pitcher = matchup.get("pitcher") or {}
            bid = safe_int(batter.get("id"))
            pid = safe_int(pitcher.get("id"))
            if not bid or not pid:
                continue
            event_type = clean(result.get("eventType"))
            if not event_type:
                continue
            total_plays += 1
            batter_name = clean(batter.get("fullName"))
            pitcher_name = clean(pitcher.get("fullName"))
            if batter_name:
                name_to_ids[norm_name(batter_name)][bid] += 1
                id_to_names[bid][batter_name] += 1
            if pitcher_name:
                name_to_ids[norm_name(pitcher_name)][pid] += 1
                id_to_names[pid][pitcher_name] += 1
            pitch_hand = clean((matchup.get("pitchHand") or {}).get("code")).upper()
            if pitch_hand:
                pitcher_hand[pid][pitch_hand] += 1
            stats = event_stats(event_type)
            stats.update(contact_stats(play))
            pkey = (bid, pid)
            dkey = (src.date, bid, pid)
            add_counter(daily[dkey], stats)
            add_counter(total[pkey], stats)
            if target and last2_start and date_obj(src.date) >= last2_start:
                add_counter(last2yr[pkey], stats)
            if src.date >= latest_date.get(pkey, ""):
                latest_date[pkey] = src.date
            recent_events[pkey].append(compact_event(src, play))
            if len(recent_events[pkey]) > 5:
                recent_events[pkey] = recent_events[pkey][-5:]

    id_to_name = {pid: c.most_common(1)[0][0] for pid, c in id_to_names.items()}
    pitcher_hand_final = {pid: c.most_common(1)[0][0] for pid, c in pitcher_hand.items()}
    daily_rows: list[dict[str, Any]] = []
    for (d, bid, pid), c in sorted(daily.items()):
        row = {
            "date": d,
            "batter_id": bid,
            "batter_name": id_to_name.get(bid, ""),
            "pitcher_id": pid,
            "pitcher_name": id_to_name.get(pid, ""),
            **stat_row(c),
        }
        daily_rows.append(row)
    meta = {
        "source_games": len(sources),
        "parsed_games": parsed_games,
        "parse_errors": parse_errors,
        "skipped_target_or_future_games": skipped_target_or_future,
        "source_date_min": min(source_dates) if source_dates else "",
        "source_date_max": max(source_dates) if source_dates else "",
        "raw_pa_events": total_plays,
        "unique_pairs": len(total),
        "unique_players": len(id_to_name),
    }
    return daily_rows, total, last2yr, recent_events, latest_date, name_to_ids, id_to_name, pitcher_hand_final, meta


def read_v3(date: str) -> list[dict[str, str]]:
    path = Path(os.environ.get("MLB_C3_V3_PATH") or ROOT / f"_v3_games_{date}.csv")
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def resolve_name(name: str, name_to_ids: dict[str, Counter[int]]) -> tuple[int | None, int]:
    key = norm_name(name)
    if not key or key not in name_to_ids:
        return None, 0
    pid, count = name_to_ids[key].most_common(1)[0]
    return int(pid), int(count)


def stale_flag(latest_faced: str, target_date: str) -> bool:
    if not latest_faced:
        return False
    return date_obj(latest_faced) < date_obj(target_date) - timedelta(days=730)


def feature_rows(
    target_date: str,
    games: list[dict[str, str]],
    total: dict[tuple[int, int], Counter[str]],
    last2yr: dict[tuple[int, int], Counter[str]],
    recent_events: dict[tuple[int, int], list[dict[str, Any]]],
    latest_date: dict[tuple[int, int], str],
    name_to_ids: dict[str, Counter[int]],
    id_to_name: dict[int, str],
    pitcher_hand: dict[int, str],
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    card_games: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    expected_resolved: set[tuple[str, str, int, int]] = set()
    actual_resolved: set[tuple[str, str, int, int]] = set()

    for game in games:
        game_pk = clean(game.get("gamePk"))
        home_team = clean(game.get("home_team"))
        away_team = clean(game.get("away_team"))
        matchup = f"{away_team} @ {home_team}".strip(" @")
        game_card: dict[str, Any] = {
            "schema": "mlb_bvp_pregame_card.v1",
            "date": target_date,
            "gamePk": game_pk,
            "matchup": matchup,
            "research_only": True,
            "no_auto_flip_remove_pass": True,
            "groups": [],
        }
        for batting_side, pitcher_side in (("away", "home"), ("home", "away")):
            batting_team = clean(game.get(f"{batting_side}_team"))
            pitcher_team = clean(game.get(f"{pitcher_side}_team"))
            pitcher_id = safe_int(game.get(f"{pitcher_side}_sp_id"))
            pitcher_name = clean(game.get(f"{pitcher_side}_sp_name"))
            if not pitcher_id and pitcher_name:
                pitcher_id, _ = resolve_name(pitcher_name, name_to_ids)
            lineup_names = split_names(game.get(f"{batting_side}_lineup_names"))
            group_entries = []
            for order, batter_name in enumerate(lineup_names, start=1):
                batter_id, name_match_count = resolve_name(batter_name, name_to_ids)
                if not batter_id:
                    rec = {
                        "date": target_date,
                        "gamePk": game_pk,
                        "matchup": matchup,
                        "batting_side": batting_side,
                        "batting_team": batting_team,
                        "pitcher_side": pitcher_side,
                        "pitcher_team": pitcher_team,
                        "batter_order": order,
                        "batter_name": batter_name,
                        "batter_id": "",
                        "batter_id_resolved": False,
                        "pitcher_name": pitcher_name,
                        "pitcher_id": pitcher_id or "",
                        "pair_pa": 0,
                        "bucket": "unresolved_batter_id",
                        "usage_lane": "display_only_unresolved_name",
                    }
                    rows.append(rec)
                    unresolved.append(rec)
                    continue
                if not pitcher_id:
                    rec = {
                        "date": target_date,
                        "gamePk": game_pk,
                        "matchup": matchup,
                        "batting_side": batting_side,
                        "batting_team": batting_team,
                        "pitcher_side": pitcher_side,
                        "pitcher_team": pitcher_team,
                        "batter_order": order,
                        "batter_name": batter_name,
                        "batter_id": batter_id,
                        "batter_id_resolved": True,
                        "pitcher_name": pitcher_name,
                        "pitcher_id": "",
                        "pair_pa": 0,
                        "bucket": "official_probable_missing",
                        "usage_lane": usage_lane("official_probable_missing"),
                        "probable_missing_reason": "official_probable_pitcher_missing",
                        "probable_missing_subreason": "opener_or_bullpen_game_possible",
                    }
                    rows.append(rec)
                    unresolved.append(rec)
                    continue
                expected_resolved.add((game_pk, batting_side, batter_id, pitcher_id))
                pkey = (batter_id, pitcher_id)
                s = stat_row(total.get(pkey, Counter()))
                l2 = stat_row(last2yr.get(pkey, Counter()))
                latest = latest_date.get(pkey, "")
                bucket = pa_bucket(int(s["pa"]))
                events = recent_events.get(pkey, [])[-3:]
                event_text = " | ".join(
                    f"{e.get('date')} {e.get('event')}: {e.get('description')}"
                    for e in events
                    if e.get("description")
                )
                rec = {
                    "date": target_date,
                    "gamePk": game_pk,
                    "matchup": matchup,
                    "batting_side": batting_side,
                    "batting_team": batting_team,
                    "pitcher_side": pitcher_side,
                    "pitcher_team": pitcher_team,
                    "batter_order": order,
                    "batter_name": batter_name,
                    "batter_id": batter_id,
                    "batter_id_resolved": True,
                    "batter_name_match_count": name_match_count,
                    "pitcher_name": pitcher_name or id_to_name.get(pitcher_id, ""),
                    "pitcher_id": pitcher_id,
                    "pitcher_hand": pitcher_hand.get(pitcher_id, ""),
                    "pair_pa": s["pa"],
                    "last2yr_pa": l2["pa"],
                    "latest_faced": latest,
                    "stale": stale_flag(latest, target_date),
                    "bucket": bucket,
                    "usage_lane": usage_lane(bucket),
                    "avg": s["avg"],
                    "obp": s["obp"],
                    "slg": s["slg"],
                    "ops": s["ops"],
                    "hr": s["hr"],
                    "k_rate": s["k_rate"],
                    "bb_rate": s["bb_rate"],
                    "hard_hit_rate": s["hard_hit_rate"],
                    "max_ev": s["max_ev"],
                    "recent_events": event_text[:700],
                }
                rows.append(rec)
                actual_resolved.add((game_pk, batting_side, batter_id, pitcher_id))
                if s["pa"]:
                    group_entries.append(
                        {
                            "batter_order": order,
                            "batter_name": batter_name,
                            "batter_id": batter_id,
                            "pair_pa": s["pa"],
                            "last2yr_pa": l2["pa"],
                            "latest_faced": latest,
                            "stale": stale_flag(latest, target_date),
                            "bucket": bucket,
                            "usage_lane": usage_lane(bucket),
                            "ops": s["ops"],
                            "hr": s["hr"],
                            "k_rate": s["k_rate"],
                            "hard_hit_rate": s["hard_hit_rate"],
                            "recent_events": events,
                        }
                    )
            group_entries.sort(key=lambda x: (int(x.get("pair_pa") or 0), str(x.get("latest_faced") or "")), reverse=True)
            game_card["groups"].append(
                {
                    "batting_team": batting_team,
                    "batting_side": batting_side,
                    "opposing_pitcher_team": pitcher_team,
                    "opposing_pitcher": pitcher_name,
                    "opposing_pitcher_id": pitcher_id,
                    "lineup_count": len(lineup_names),
                    "direct_prior_pair_count": len(group_entries),
                    "entries": group_entries,
                }
            )
        card_games.append(game_card)

    validation = {
        "target_combo_validation_ok": expected_resolved == actual_resolved,
        "expected_resolved_pairs": len(expected_resolved),
        "actual_resolved_pairs": len(actual_resolved),
        "missing_resolved_pairs": len(expected_resolved - actual_resolved),
        "extra_resolved_pairs": len(actual_resolved - expected_resolved),
        "unresolved_rows": len(unresolved),
    }
    return rows, validation, card_games


def target_actual_pitcher_diagnostics(target_date: str, games: list[dict[str, str]], features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Diagnostic-only actual pitcher note for official probable gaps.

    This intentionally must not feed pregame cards. It explains why a target
    card stayed unresolved when the postgame raw PBP later contains actual
    pitcher IDs.
    """
    missing = defaultdict(set)
    for row in features:
        if clean(row.get("bucket")) == "official_probable_missing":
            missing[str(row.get("gamePk") or "")].add(clean(row.get("pitcher_side")))
    if not missing:
        return []
    game_map = {clean(g.get("gamePk")): g for g in games}
    out: list[dict[str, Any]] = []
    for game_pk, sides in sorted(missing.items()):
        raw_path = ROOT / "_deepread_v3" / "raw_plays" / target_date / f"{game_pk}.json"
        if not raw_path.exists():
            out.append(
                {
                    "gamePk": game_pk,
                    "diagnostic_only": True,
                    "excluded_from_pregame_card": True,
                    "raw_pbp_path": str(raw_path.relative_to(ROOT)).replace("\\", "/"),
                    "raw_pbp_exists": False,
                    "missing_probable_sides": sorted(sides),
                    "actual_pitchers_observed": [],
                }
            )
            continue
        try:
            raw = json.loads(raw_path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            out.append(
                {
                    "gamePk": game_pk,
                    "diagnostic_only": True,
                    "excluded_from_pregame_card": True,
                    "raw_pbp_path": str(raw_path.relative_to(ROOT)).replace("\\", "/"),
                    "raw_pbp_exists": True,
                    "raw_pbp_error": type(exc).__name__,
                    "missing_probable_sides": sorted(sides),
                    "actual_pitchers_observed": [],
                }
            )
            continue
        plays = get_all_plays(raw)
        side_pitchers: dict[str, Counter[tuple[int, str]]] = {"away": Counter(), "home": Counter()}
        for play in plays:
            about = play.get("about") or {}
            half = clean(about.get("halfInning")).lower()
            pitcher_side = "away" if half == "bottom" else "home" if half == "top" else ""
            if pitcher_side not in side_pitchers:
                continue
            pitcher = ((play.get("matchup") or {}).get("pitcher") or {})
            pid = safe_int(pitcher.get("id"))
            pname = clean(pitcher.get("fullName"))
            if pid and pname:
                side_pitchers[pitcher_side][(pid, pname)] += 1
        game = game_map.get(game_pk, {})
        for side in sorted(sides):
            pitchers = [
                {"pitcher_id": pid, "pitcher_name": pname, "pa_observed": count}
                for (pid, pname), count in side_pitchers.get(side, Counter()).most_common()
            ]
            out.append(
                {
                    "gamePk": game_pk,
                    "matchup": f"{clean(game.get('away_team'))} @ {clean(game.get('home_team'))}".strip(" @"),
                    "missing_probable_side": side,
                    "missing_probable_team": clean(game.get(f"{side}_team")),
                    "diagnostic_only": True,
                    "excluded_from_pregame_card": True,
                    "postgame_actual_pitchers_observed_but_excluded": bool(pitchers),
                    "raw_pbp_path": str(raw_path.relative_to(ROOT)).replace("\\", "/"),
                    "actual_pitchers_observed": pitchers,
                }
            )
    return out


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fmt_num(value: Any, digits: int = 3) -> str:
    x = safe_float(value)
    if x is None:
        return "-"
    return f"{x:.{digits}f}".rstrip("0").rstrip(".")


def render_card_text(date: str, cards: list[dict[str, Any]]) -> str:
    lines = [
        f"# MLB BvP Player-Memory Cards {date}",
        "",
        "research_only=true; context_only=true; no_auto_flip_remove_pass=true; source_date < target_date.",
        "usage: 1-9 PA display only; 10-24 observation; 25+ supporting only with corroboration.",
        "",
    ]
    for card in cards:
        lines.append(f"## {card.get('matchup')} ({card.get('gamePk')})")
        any_entry = False
        for group in card.get("groups") or []:
            entries = group.get("entries") or []
            header = f"{group.get('batting_team')} hitters vs {group.get('opposing_pitcher')} ({group.get('opposing_pitcher_team')})"
            if not entries:
                lines.append(f"- BvP: {header}: no direct prior PA found/resolved.")
                continue
            any_entry = True
            parts = []
            for ent in entries[:5]:
                stale = " stale" if ent.get("stale") else ""
                parts.append(
                    f"{ent.get('batter_name')} {ent.get('pair_pa')}PA"
                    f"(last2yr={ent.get('last2yr_pa')}, latest={ent.get('latest_faced') or '-'}{stale}, "
                    f"{ent.get('bucket')}, OPS={fmt_num(ent.get('ops'))}, HR={ent.get('hr')})"
                )
            lines.append(f"- BvP: {header}: " + "; ".join(parts))
        if not any_entry:
            lines.append("- BvP summary: this game is mostly no-prior direct pair history; do not infer absence as signal.")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def report_markdown(report: dict[str, Any]) -> str:
    b = report.get("bucket_counts") or {}
    dist = report.get("pairs_25plus_per_game") or {}
    return "\n".join(
        [
            "# MLB BvP Matchup Ledger",
            "",
            f"- schema: `{report.get('schema')}`",
            f"- research_only: `{report.get('research_only')}`",
            f"- target_date: `{report.get('target_date')}`",
            f"- source_games: `{report.get('source_games')}`",
            f"- parsed_games: `{report.get('parsed_games')}`",
            f"- source_date_min/max: `{report.get('source_date_min')}` / `{report.get('source_date_max')}`",
            f"- leak_guard_ok: `{report.get('leak_guard_ok')}`",
            f"- raw PA events: `{report.get('raw_pa_events')}`",
            f"- unique BvP pairs: `{report.get('unique_pairs')}`",
            f"- target games: `{report.get('target_games')}`",
            f"- feature rows: `{report.get('feature_rows')}`",
            f"- unresolved rows: `{report.get('unresolved_rows')}`",
            "",
            "## Bucket Counts",
            "",
            f"- no_prior_pair: `{b.get('no_prior_pair', 0)}`",
            f"- display_1_9: `{b.get('display_1_9', 0)}`",
            f"- observation_10_24: `{b.get('observation_10_24', 0)}`",
            f"- support_25_plus: `{b.get('support_25_plus', 0)}`",
            f"- unresolved_batter_id: `{b.get('unresolved_batter_id', 0)}`",
            f"- official_probable_missing: `{b.get('official_probable_missing', 0)}`",
            f"- unresolved_pitcher_id: `{b.get('unresolved_pitcher_id', 0)}`",
            "",
            "## 25+ PA Pair Distribution",
            "",
            f"- games_with_any_25plus: `{dist.get('games_with_any_25plus', 0)}`",
            f"- min/median/max 25+ pairs per game: `{dist.get('min', 0)}` / `{dist.get('median', 0)}` / `{dist.get('max', 0)}`",
            f"- per_game: `{dist.get('per_game', {})}`",
            "",
            "## Official Probable Missing Diagnostics",
            "",
            f"- official_probable_missing_rows: `{report.get('official_probable_missing_rows', 0)}`",
            f"- official_probable_missing_games: `{report.get('official_probable_missing_games', 0)}`",
            "- diagnostic note: postgame actual pitchers, when observed, are report-only and excluded from pregame cards.",
            "",
            "## Guardrails",
            "",
            "- Context card only.",
            "- Not a gate, not model-defense, not a validator.",
            "- No Notification / production prediction / narrator prediction mutation.",
            "- Direct BvP cannot stand alone; 25+ PA can only support/conflict when corroborated by other pregame facts.",
            "",
        ]
    )


def build(date: str, write: bool, start_season: int, end_season: int) -> dict[str, Any]:
    target_date = parse_date(date)
    if not target_date:
        raise ValueError(f"bad date: {date}")
    sources = source_games(start_season, end_season)
    daily_rows, total, last2yr, recent_events, latest, name_to_ids, id_to_name, pitcher_hand, meta = build_ledger(
        sources, target_date=target_date
    )
    games = read_v3(target_date)
    features, combo_validation, cards = feature_rows(
        target_date,
        games,
        total,
        last2yr,
        recent_events,
        latest,
        name_to_ids,
        id_to_name,
        pitcher_hand,
    )
    bucket_counts = Counter(clean(r.get("bucket")) for r in features)
    probable_missing_games = sorted({clean(r.get("gamePk")) for r in features if clean(r.get("bucket")) == "official_probable_missing"})
    actual_pitcher_diag = target_actual_pitcher_diagnostics(target_date, games, features)
    per_game_25 = Counter(str(r.get("gamePk")) for r in features if clean(r.get("bucket")) == "support_25_plus")
    per_game_values = [per_game_25.get(clean(g.get("gamePk")), 0) for g in games]
    per_game_sorted = sorted(per_game_values)
    median = per_game_sorted[len(per_game_sorted) // 2] if per_game_sorted else 0
    leak_guard_ok = bool(meta.get("source_date_max")) and meta.get("source_date_max") < target_date
    total_rows = []
    for (bid, pid), c in sorted(total.items()):
        row = {
            "batter_id": bid,
            "batter_name": id_to_name.get(bid, ""),
            "pitcher_id": pid,
            "pitcher_name": id_to_name.get(pid, ""),
            "latest_faced": latest.get((bid, pid), ""),
            **stat_row(c),
        }
        total_rows.append(row)
    report = {
        "ok": True,
        "schema": "mlb_bvp_matchup_ledger.v1",
        "research_only": True,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "target_date": target_date,
        "start_season": start_season,
        "end_season": end_season,
        **meta,
        "target_games": len(games),
        "feature_rows": len(features),
        "bucket_counts": dict(bucket_counts),
        "official_probable_missing_rows": int(bucket_counts.get("official_probable_missing", 0)),
        "official_probable_missing_games": len(probable_missing_games),
        "official_probable_missing_gamepks": probable_missing_games,
        "postgame_actual_pitcher_diagnostics": actual_pitcher_diag,
        "postgame_actual_pitcher_diagnostics_rule": "diagnostic-only; excluded from pregame BvP cards and narrator context",
        "pairs_25plus_per_game": {
            "games_with_any_25plus": sum(1 for x in per_game_values if x > 0),
            "min": min(per_game_values) if per_game_values else 0,
            "median": median,
            "max": max(per_game_values) if per_game_values else 0,
            "per_game": dict(per_game_25),
        },
        "leak_guard_ok": leak_guard_ok,
        "target_combo_validation": combo_validation,
        "unresolved_rows": combo_validation.get("unresolved_rows"),
        "outputs": {
            "pair_daily_csv": str(PAIR_DAILY_CSV.relative_to(ROOT)).replace("\\", "/"),
            "pair_totals_csv": str(PAIR_TOTAL_CSV.relative_to(ROOT)).replace("\\", "/"),
            "pregame_features_csv": str((OUT_DIR / f"mlb_bvp_pregame_features_{target_date}.csv").relative_to(ROOT)).replace("\\", "/"),
            "pregame_features_json": str((OUT_DIR / f"mlb_bvp_pregame_features_{target_date}.json").relative_to(ROOT)).replace("\\", "/"),
            "pregame_cards_json": str((OUT_DIR / f"mlb_bvp_pregame_cards_{target_date}.json").relative_to(ROOT)).replace("\\", "/"),
            "pregame_cards_txt": str((OUT_DIR / f"mlb_bvp_pregame_cards_{target_date}.txt").relative_to(ROOT)).replace("\\", "/"),
            "report_json": str(REPORT_JSON.relative_to(ROOT)).replace("\\", "/"),
            "report_md": str(REPORT_MD.relative_to(ROOT)).replace("\\", "/"),
        },
        "guardrails": [
            "source date must be < target_date for pregame features",
            "context card only",
            "no auto flip/remove/pass",
            "no Notification or production/narrator prediction mutation",
        ],
    }
    if write:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        write_csv(PAIR_DAILY_CSV, daily_rows)
        write_csv(PAIR_TOTAL_CSV, total_rows)
        feature_csv = OUT_DIR / f"mlb_bvp_pregame_features_{target_date}.csv"
        feature_json = OUT_DIR / f"mlb_bvp_pregame_features_{target_date}.json"
        card_json = OUT_DIR / f"mlb_bvp_pregame_cards_{target_date}.json"
        card_txt = OUT_DIR / f"mlb_bvp_pregame_cards_{target_date}.txt"
        write_csv(feature_csv, features)
        write_json(feature_json, features)
        write_json(card_json, cards)
        card_txt.write_text(render_card_text(target_date, cards), encoding="utf-8")
        write_json(REPORT_JSON, report)
        REPORT_MD.write_text(report_markdown(report), encoding="utf-8")
    return report


def current_report_ok(target_date: str) -> dict[str, Any] | None:
    target_date = parse_date(target_date)
    if not target_date or not REPORT_JSON.exists():
        return None
    feature_csv = OUT_DIR / f"mlb_bvp_pregame_features_{target_date}.csv"
    feature_json = OUT_DIR / f"mlb_bvp_pregame_features_{target_date}.json"
    card_json = OUT_DIR / f"mlb_bvp_pregame_cards_{target_date}.json"
    card_txt = OUT_DIR / f"mlb_bvp_pregame_cards_{target_date}.txt"
    required = [feature_csv, feature_json, card_json, card_txt, REPORT_MD]
    if any(not p.exists() or p.stat().st_size <= 0 for p in required):
        return None
    v3 = Path(
        os.environ.get("MLB_C3_V3_PATH")
        or ROOT / f"_v3_games_{target_date}.csv"
    )
    allow_stale_target_packet = str(os.environ.get("MLB_BVP_ALLOW_STALE_TARGET_PACKET") or "").strip().lower() in {"1", "true", "yes", "on"}
    if v3.exists() and feature_csv.stat().st_mtime < v3.stat().st_mtime and not allow_stale_target_packet:
        return None
    try:
        report = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    except Exception:
        return None
    if clean(report.get("target_date")) != target_date:
        return None
    if not report.get("ok") or not report.get("research_only") or not report.get("leak_guard_ok"):
        return None
    if clean(report.get("source_date_max")) >= target_date:
        return None
    validation = report.get("target_combo_validation") or {}
    if not validation.get("target_combo_validation_ok"):
        return None
    report["skip_if_current"] = True
    report["skip_reason"] = "current BvP outputs already exist; target packet mtime check passed or was explicitly relaxed"
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="Build research-only MLB direct BvP player-memory cards.")
    ap.add_argument("--date", required=True, help="Target MLB official date YYYY-MM-DD.")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--skip-if-current", action="store_true")
    ap.add_argument("--start-season", type=int, default=2021)
    ap.add_argument("--end-season", type=int, default=2026)
    args = ap.parse_args()
    if args.skip_if_current:
        report = current_report_ok(args.date)
        if report is not None:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
    report = build(args.date, write=args.write, start_season=args.start_season, end_season=args.end_season)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
