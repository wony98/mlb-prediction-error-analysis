# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""Shared MLB result-status helpers.

This module is intentionally small and side-effect free except for the
StatsAPI fetch function. It prevents each postgame/deepread/synth lane from
inventing its own definition of "postponed/no result".
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import json

try:
    import requests
except Exception:  # Codex bundled Python may not include requests.
    requests = None

MLB_API = "https://statsapi.mlb.com/api/v1"

NO_RESULT_DETAILS = {"Postponed", "Cancelled", "Canceled", "Suspended"}
NO_RESULT_CODES = {"DI", "DR", "CA"}


def _team_name(team_block: dict[str, Any]) -> str:
    return str(((team_block or {}).get("team") or {}).get("name") or "")


def _score_present(team_block: dict[str, Any]) -> bool:
    return (team_block or {}).get("score") is not None


def is_no_result_status(meta_or_game: dict[str, Any]) -> bool:
    """True only for scoreless postponed/cancelled/suspended games.

    The score guard is important: a suspended game can later resume and become
    a real final with scores, and must not stay excluded forever.
    """
    if not isinstance(meta_or_game, dict):
        return False

    if "no_result" in meta_or_game and "has_score" in meta_or_game:
        return bool(meta_or_game.get("no_result")) and not bool(meta_or_game.get("has_score"))

    status = meta_or_game.get("status") or {}
    if not status and {"detailed_state", "status_code", "has_score"} & set(meta_or_game):
        detailed = str(meta_or_game.get("detailed_state") or "")
        code = str(meta_or_game.get("status_code") or "")
        has_score = bool(meta_or_game.get("has_score"))
        return (detailed in NO_RESULT_DETAILS or code in NO_RESULT_CODES) and not has_score

    teams = meta_or_game.get("teams") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    detailed = str(status.get("detailedState") or "")
    code = str(status.get("statusCode") or "")
    has_score = _score_present(home) and _score_present(away)
    return (detailed in NO_RESULT_DETAILS or code in NO_RESULT_CODES) and not has_score


def game_status_meta(game: dict[str, Any]) -> dict[str, Any]:
    status = game.get("status") or {}
    teams = game.get("teams") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    has_score = _score_present(home) and _score_present(away)
    detailed = str(status.get("detailedState") or "")
    code = str(status.get("statusCode") or "")
    no_result = (detailed in NO_RESULT_DETAILS or code in NO_RESULT_CODES) and not has_score
    abstract = str(status.get("abstractGameState") or "")
    return {
        "gamePk": int(game.get("gamePk")) if str(game.get("gamePk") or "").isdigit() else game.get("gamePk"),
        "official_date": game.get("officialDate") or "",
        "away": _team_name(away),
        "home": _team_name(home),
        "abstract_state": abstract,
        "detailed_state": detailed,
        "status_code": code,
        "reason": status.get("reason") or "",
        "has_score": bool(has_score),
        "away_score": away.get("score") if has_score else None,
        "home_score": home.get("score") if has_score else None,
        "no_result": bool(no_result),
        "is_final_scored": bool(abstract == "Final" and has_score and not no_result),
    }


def meta_from_schedule_payload(payload: dict[str, Any]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for day in (payload or {}).get("dates", []):
        for game in day.get("games", []):
            meta = game_status_meta(game)
            try:
                out[int(meta["gamePk"])] = meta
            except Exception:
                continue
    return out


def fetch_mlb_result_status_meta(date: str) -> dict[int, dict[str, Any]]:
    params = {"sportId": 1, "date": date, "gameType": "R"}
    if requests is not None:
        r = requests.get(f"{MLB_API}/schedule", params=params, timeout=20)
        r.raise_for_status()
        return meta_from_schedule_payload(r.json())
    url = f"{MLB_API}/schedule?{urlencode(params)}"
    req = Request(url, headers={"User-Agent": "kbo-step6-mlb-result-status/1.0"})
    with urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return meta_from_schedule_payload(payload)


def final_scored_gamepks(date: str) -> set[int]:
    return {pk for pk, meta in fetch_mlb_result_status_meta(date).items() if meta.get("is_final_scored")}


def no_result_gamepks(date: str) -> set[int]:
    return {pk for pk, meta in fetch_mlb_result_status_meta(date).items() if meta.get("no_result")}
