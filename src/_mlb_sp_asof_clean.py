# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""_mlb_sp_asof_clean.py — per-(gamePk,side) clean as-of SP스탯 (누수0, 100% seed).

방식: _box2026_game(API산 100%)에서 sp_id별 시간순 *누적 − 현재경기* = 진짜 as-of era/k9/bb9/whip/hr9.
시즌 첫 등판(직전 2026 누적 없음) = _mlb_sp_prior.csv(완료시즌 MLB→MiLB 체인)로 seed.
외국수입(foreign_ref)만 seed無(참고). 출력: _mlb_sp_asof_clean.parquet.

컬럼: gamePk, side, sp_id, nprior(직전 2026 등판수), spa_era/k9/bb9/whip/hr9, spa_ip, spa_src, spa_level.
usage: py _mlb_sp_asof_clean.py
"""
import os
import sys
import io
import numpy as np
import pandas as pd

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "_mlb_sp_asof_clean.parquet")


def asof_sp_full(g):
    """게임레벨 박스(g) → per-(gamePk,side) as-of-prior SP스탯. 순수함수(테스트).
    핵심 누수0 불변: c_X = sp_id별 시간순 cumsum − 현재경기 (현재경기 자신 제외)."""
    rows = []
    for side in ("home", "away"):
        c = {f"{side}_sp_id": "sp_id", f"{side}_sp_er": "er", f"{side}_sp_ip": "ip",
             f"{side}_sp_h": "h", f"{side}_sp_bb": "bb", f"{side}_sp_k": "k", f"{side}_sp_hr": "hr"}
        s = g[["gamePk", "date"] + list(c)].rename(columns=c)
        s["side"] = side
        rows.append(s)
    sp = pd.concat(rows).dropna(subset=["sp_id"]).sort_values("date").reset_index(drop=True)
    sp["sp_id"] = sp["sp_id"].astype(int)
    gr = sp.groupby("sp_id")
    for col in ("er", "ip", "h", "bb", "k", "hr"):
        sp[f"c_{col}"] = gr[col].cumsum() - sp[col]   # 누적 − 현재 = as-of-prior (현재경기 제외=누수0)
    sp["nprior"] = gr.cumcount()
    ip = sp["c_ip"]
    sp["spa_era"] = np.where(ip > 0, 9 * sp.c_er / ip, np.nan)
    sp["spa_whip"] = np.where(ip > 0, (sp.c_h + sp.c_bb) / ip, np.nan)
    sp["spa_k9"] = np.where(ip > 0, 9 * sp.c_k / ip, np.nan)
    sp["spa_bb9"] = np.where(ip > 0, 9 * sp.c_bb / ip, np.nan)
    sp["spa_hr9"] = np.where(ip > 0, 9 * sp.c_hr / ip, np.nan)
    sp["spa_ip"] = np.where(ip > 0, ip, np.nan)
    sp["spa_src"] = np.where(ip > 0, "2026_asof", None)
    sp["spa_level"] = np.where(ip > 0, "MLB", None)
    return sp


def build():
    g = pd.read_parquet(os.path.join(ROOT, "_box2026_game.parquet"))
    g["date"] = pd.to_datetime(g["date"])
    sp = asof_sp_full(g)

    # 첫 등판(또는 c_ip==0) = prior seed
    prior = pd.read_csv(os.path.join(ROOT, "_mlb_sp_prior.csv")).set_index("sp_id")
    need = sp[sp["spa_era"].isna()]
    for i, r in need.iterrows():
        pr = prior.loc[r["sp_id"]] if r["sp_id"] in prior.index else None
        if pr is None or pd.isna(pr.get("p_era")):
            sp.at[i, "spa_src"] = pr["p_src"] if pr is not None else "none"
            sp.at[i, "spa_level"] = pr["p_level"] if pr is not None else "—"
            continue
        sp.at[i, "spa_era"] = pr["p_era"]; sp.at[i, "spa_k9"] = pr["p_k9"]
        sp.at[i, "spa_bb9"] = pr["p_bb9"]; sp.at[i, "spa_whip"] = pr["p_whip"]
        sp.at[i, "spa_hr9"] = pr["p_hr9"]; sp.at[i, "spa_ip"] = pr["p_ip"]
        sp.at[i, "spa_src"] = pr["p_src"]; sp.at[i, "spa_level"] = pr["p_level"]

    keep = ["gamePk", "side", "sp_id", "nprior", "spa_era", "spa_k9", "spa_bb9",
            "spa_whip", "spa_hr9", "spa_ip", "spa_src", "spa_level"]
    out = sp[keep].copy()
    out.to_parquet(OUT)
    fill = out["spa_era"].notna().mean()
    print(f"저장 {OUT} — {len(out)}행 | spa_era 채움 {fill:.1%}", flush=True)
    print("spa_src 분포:", out["spa_src"].value_counts().to_dict(), flush=True)
    miss = out[out["spa_era"].isna()]
    print(f"seed無(외국수입 첫등판) {len(miss)}행", flush=True)
    return out


if __name__ == "__main__":
    build()
