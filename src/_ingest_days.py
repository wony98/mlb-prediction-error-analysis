# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""_ingest_days.py — 특정 날짜 final 경기를 폼 소스 3종에 증분 인제스트 후 폼 재빌드.

master 정체로 인제스트 멈춘 날(6/5~) 직접 채움. boxfetch/narrative 로직 재사용, statsapi(한도무관).
대상: _box2026_game·_box2026_bat (SP폼) / _narr2026 (타자폼) → 그 뒤 폼 타임라인 + 불펜 stints 재빌드.

usage: py _ingest_days.py 2026-06-05 2026-06-06
"""
import os
import sys
import time
import json
from pathlib import Path
import numpy as np
import pandas as pd
import requests
from _narr_pbp import build_narrative

API = "https://statsapi.mlb.com/api/v1"
ROOT = os.path.dirname(os.path.abspath(__file__))
RAW_PBP_ROOT = os.path.join(ROOT, "_deepread_v3", "raw_plays")
BOX_CACHE_ROOT = os.path.join(ROOT, "postmortem", "t075_boxscore_cache")


def parse_cli(argv):
    skip_rebuild = "--skip-rebuild" in argv
    local_cache = "--local-cache" in argv
    dates = [arg for arg in argv if not arg.startswith("--")]
    return dates, skip_rebuild, local_cache


def game_date_map(frame):
    return {
        int(row.gamePk): pd.to_datetime(row.date).strftime("%Y-%m-%d")
        for row in frame[["gamePk", "date"]].itertuples(index=False)
    }


def finals_from_cache(date, root=BOX_CACHE_ROOT):
    out = []
    folder = os.path.join(root, date)
    for path in sorted(Path(folder).glob("*.json")):
        try:
            data = json.load(open(path, encoding="utf-8-sig"))
            teams = data["teams"]
            home = teams["home"]["team"]["name"]
            away = teams["away"]["team"]["name"]
            out.append((int(path.stem), date, home, away))
        except Exception as exc:
            raise RuntimeError(f"invalid cached boxscore {path}: {exc}") from exc
    return out


def _ipf(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return 0.0
    w = np.floor(x)
    return float(w + round((x - w) * 10) / 3.0)


def _get(url):
    for _ in range(2):
        try:
            return requests.get(url, timeout=15).json()
        except Exception:
            time.sleep(0.4)
    return None


def finals(date):
    d = _get(f"{API}/schedule?sportId=1&date={date}")
    out = []
    for day in (d or {}).get("dates", []):
        for g in day.get("games", []):
            status = g.get("status", {})
            stt = str(status.get("detailedState", ""))
            abstract = str(status.get("abstractGameState", ""))
            if abstract == "Final" or "Final" in stt or "Completed" in stt:
                out.append((g["gamePk"], date, g["teams"]["home"]["team"]["name"], g["teams"]["away"]["team"]["name"]))
    return out


def box_rows(pk, date):
    """boxfetch 로직 재현 → (game_row, [bat_rows])."""
    cache_path = os.path.join(BOX_CACHE_ROOT, date, f"{int(pk)}.json")
    try:
        d = json.load(open(cache_path, encoding="utf-8-sig")) if os.path.exists(cache_path) else None
    except Exception:
        d = None
    if not d:
        d = _get(f"{API}/game/{pk}/boxscore")
    if not d or "teams" not in d:
        return None, []
    rec = {"gamePk": pk, "date": pd.to_datetime(date)}
    brows = []
    for side in ("home", "away"):
        t = d["teams"][side]; players = t["players"]
        rec[f"{side}_runs"] = t["teamStats"]["batting"].get("runs", np.nan)
        bp_ip = bp_er = 0.0; sp_done = False
        for pid, p in players.items():
            pit = p.get("stats", {}).get("pitching", {})
            if pit and pit.get("inningsPitched") is not None:
                ip = _ipf(pit.get("inningsPitched", 0)); er = float(pit.get("earnedRuns", 0) or 0)
                if int(pit.get("gamesStarted", 0) or 0) == 1 and not sp_done:
                    rec[f"{side}_sp_id"] = p["person"]["id"]; rec[f"{side}_sp_ip"] = ip; rec[f"{side}_sp_er"] = er
                    rec[f"{side}_sp_h"] = float(pit.get("hits", 0) or 0); rec[f"{side}_sp_bb"] = float(pit.get("baseOnBalls", 0) or 0)
                    rec[f"{side}_sp_k"] = float(pit.get("strikeOuts", 0) or 0); rec[f"{side}_sp_hr"] = float(pit.get("homeRuns", 0) or 0)
                    sp_done = True
                else:
                    bp_ip += ip; bp_er += er
            bat = p.get("stats", {}).get("batting", {})
            if bat and bat.get("atBats") is not None and (float(bat.get("atBats", 0) or 0) + float(bat.get("baseOnBalls", 0) or 0)) > 0:
                order = p.get("battingOrder")
                brows.append({"gamePk": pk, "date": pd.to_datetime(date), "side": side, "batter_id": p["person"]["id"],
                              "starter": bool(order and str(order).endswith("00")),
                              "ab": float(bat.get("atBats", 0) or 0), "h": float(bat.get("hits", 0) or 0),
                              "bb": float(bat.get("baseOnBalls", 0) or 0), "hbp": float(bat.get("hitByPitch", 0) or 0),
                              "d2": float(bat.get("doubles", 0) or 0), "d3": float(bat.get("triples", 0) or 0), "hr": float(bat.get("homeRuns", 0) or 0)})
        rec[f"{side}_bp_ip"] = round(bp_ip, 1); rec[f"{side}_bp_er"] = bp_er
    return rec, brows


def narr_row(pk, date, home, away):
    raw_path = os.path.join(RAW_PBP_ROOT, date, f"{int(pk)}.json")
    try:
        d = json.load(open(raw_path, encoding="utf-8-sig")) if os.path.exists(raw_path) else None
    except Exception:
        d = None
    if not d:
        d = _get(f"{API}/game/{pk}/playByPlay")
    if not d or "allPlays" not in d:
        return None
    plays = d["allPlays"]
    narr = build_narrative(plays, away, home)
    last = plays[-1].get("result", {}) if plays else {}
    fin = f"{last.get('awayScore', '?')}-{last.get('homeScore', '?')}"
    return {"gamePk": pk, "date": pd.to_datetime(date), "away": away, "home": home, "final": fin, "narrative": narr}


def main():
    dates, skip_rebuild, local_cache = parse_cli(sys.argv[1:])
    if not dates:   # 인자 없으면 최근 3일(ET) — 어제까지 finals 인제스트 (파이프라인 일일 폼 신선화, 멱등)
        et = pd.Timestamp.utcnow() - pd.Timedelta(hours=4)
        dates = [(et - pd.Timedelta(days=k)).strftime("%Y-%m-%d") for k in (3, 2, 1)]
        print(f"[ingest] 인자 없음 → 최근 3일 기본 {dates}", flush=True)
    g = pd.read_parquet(os.path.join(ROOT, "_box2026_game.parquet"))
    b = pd.read_parquet(os.path.join(ROOT, "_box2026_bat.parquet"))
    n = pd.read_parquet(os.path.join(ROOT, "_narr2026.parquet"))
    complete_box = g[pd.notna(g.get("home_runs")) & pd.notna(g.get("away_runs"))]
    box_dates = game_date_map(complete_box)
    narr_dates = game_date_map(n)
    ng, nb, nn = [], [], []
    for date in dates:
        final_rows = finals_from_cache(date) if local_cache else finals(date)
        for pk, dt, home, away in final_rows:
            # Suspended/rescheduled games can keep the same gamePk while the
            # official final date moves. Match identity and date, not gamePk alone.
            in_box = box_dates.get(int(pk)) == date
            in_narr = narr_dates.get(int(pk)) == date
            if in_box and in_narr:
                continue
            if not in_box:
                gr, br = box_rows(pk, dt)
                if gr:
                    ng.append(gr); nb += br
            if not in_narr:
                nr = narr_row(pk, dt, home, away)
                if nr:
                    nn.append(nr)
            time.sleep(0.05)
        print(f"  {date}: +{len([x for x in ng if str(x['date'])[:10]==date])}경기 인제스트", flush=True)
    if ng:
        ng_df = pd.DataFrame(ng)
        nb_df = pd.DataFrame(nb)
        new_pks = set(ng_df["gamePk"].astype(int))
        pd.concat([g[~g["gamePk"].astype(int).isin(new_pks)], ng_df], ignore_index=True).to_parquet(os.path.join(ROOT, "_box2026_game.parquet"))
        pd.concat([b[~b["gamePk"].astype(int).isin(new_pks)], nb_df], ignore_index=True).to_parquet(os.path.join(ROOT, "_box2026_bat.parquet"))
    if nn:
        nn_df = pd.DataFrame(nn)
        new_pks = set(nn_df["gamePk"].astype(int))
        pd.concat([n[~n["gamePk"].astype(int).isin(new_pks)], nn_df], ignore_index=True).to_parquet(os.path.join(ROOT, "_narr2026.parquet"))
    print(f"[ingest] box +{len(ng)} / bat +{len(nb)} / narr +{len(nn)}", flush=True)
    if skip_rebuild:
        print("[ingest] --skip-rebuild: form/bullpen rebuild deferred", flush=True)
        return
    # 폼 재빌드
    import _form_label, _sp_form
    _form_label.build_timeline()
    _sp_form.build_timeline()
    # 불펜 stints + _bp_form (ledger가 narr에서 재빌드 + 훅으로 _bp_form)
    import subprocess
    subprocess.run([sys.executable, os.path.join(ROOT, "_narr_bullpen_ledger.py")], cwd=ROOT)


if __name__ == "__main__":
    main()
