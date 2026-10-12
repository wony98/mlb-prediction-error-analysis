# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""_mlb_ump_backfill.py — postgame 주심 backfill (기록 100%).

배경(historical-note): 주심(hp_ump_name)은 첫구 ~3h 전에야 StatsAPI feed에 배정됨.
아침/이른 pregame 분석 시점엔 0/15 → pregame CSV에 주심 빈 채로 남음.
경기 후(또는 첫구 ~3h 후)엔 feed/live officials에 100% 존재 → 이걸 backfill해 기록/회고 완결.

대상: `_v3_games_{date}.csv`의 `hp_ump_name` (+옵션 1B/2B/3B). narr_context [환경]·ump 통계가 이걸 읽음.
원칙: **빈 값만 채움**(기존 보존). predictions/narrator/Notification/master 무접촉 — env 컬럼만.

usage:
  py _mlb_ump_backfill.py 2026-06-12 --write     # 과거일 backfill
  py _mlb_ump_backfill.py 2026-06-12             # 콘솔만(미저장)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
_POS = {"home plate": "hp_ump_name", "first base": "1b_ump_name",
        "second base": "2b_ump_name", "third base": "3b_ump_name"}


def _get(url: str):
    with urllib.request.urlopen(url, timeout=25) as r:
        return json.load(r)


def _officials(game_pk) -> dict:
    """feed/live officials → {hp_ump_name:.., 1b_ump_name:..}. 미배정이면 빈 dict."""
    try:
        feed = _get(f"https://statsapi.mlb.com/api/v1.1/game/{int(game_pk)}/feed/live")
    except Exception:
        return {}
    offs = (((feed.get("liveData") or {}).get("boxscore") or {}).get("officials") or [])
    out = {}
    for item in offs:
        col = _POS.get(str(item.get("officialType", "")).lower())
        name = (item.get("official") or {}).get("fullName")
        if col and name:
            out[col] = str(name)
    return out


def _missing(v) -> bool:
    if v is None:
        return True
    s = str(v).strip().lower()
    return s in ("", "nan", "none", "null")


def backfill(date: str, write: bool) -> dict:
    csv = os.path.join(ROOT, f"_v3_games_{date}.csv")
    if not os.path.exists(csv):
        return {"ok": False, "error": f"{os.path.basename(csv)} 없음 — backfill 대상 CSV 필요"}
    df = pd.read_csv(csv)
    if "gamePk" not in df.columns:
        return {"ok": False, "error": "gamePk 컬럼 없음"}
    # ump 컬럼은 문자열(주심명)이므로 object dtype 강제 (전부 NaN이면 float로 읽혀 문자열 set 시 LossySetitemError)
    for _c in _POS.values():
        if _c in df.columns:
            df[_c] = df[_c].astype(object)
        else:
            df[_c] = pd.Series([None] * len(df), dtype=object)
    df = df.copy()  # de-fragment (컬럼 단건 추가 후)

    filled, already, unavailable = 0, 0, 0
    details = []
    for idx, row in df.iterrows():
        gpk = row.get("gamePk")
        if pd.isna(gpk):
            continue
        if not _missing(row.get("hp_ump_name")):
            already += 1
            continue
        offs = _officials(gpk)
        hp = offs.get("hp_ump_name")
        if not hp:
            unavailable += 1
            details.append({"gamePk": str(int(gpk)), "status": "unavailable"})
            continue
        for col, name in offs.items():
            if col not in df.columns:
                df[col] = None
            if _missing(df.at[idx, col]):
                df.at[idx, col] = name
        filled += 1
        details.append({"gamePk": str(int(gpk)), "hp_ump_name": hp})

    if write and filled:
        df.to_csv(csv, index=False, encoding="utf-8-sig")

    return {
        "ok": True,
        "schema": "mlb_ump_backfill.v1",
        "read_only_predictions": True,
        "date": date,
        "games": len(df),
        "filled": filled,
        "already_present": already,
        "unavailable": unavailable,
        "written": bool(write and filled),
        "details": details,
    }


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="postgame 주심(hp_ump) backfill into _v3_games_{date}.csv")
    ap.add_argument("date", help="YYYY-MM-DD")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    r = backfill(args.date, args.write)
    if not r.get("ok"):
        print(f"[ump_backfill] ERROR {r.get('error')}")
        return 1
    print(f"[ump_backfill] {args.date} — filled {r['filled']} / already {r['already_present']} / "
          f"unavailable {r['unavailable']} (총 {r['games']}경기){' [저장]' if r['written'] else ''}")
    for d in r["details"]:
        if "hp_ump_name" in d:
            print(f"  + {d['gamePk']}: {d['hp_ump_name']}")
        else:
            print(f"  - {d['gamePk']}: 주심 미배정(아직 pregame/no feed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
