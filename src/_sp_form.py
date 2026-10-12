# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""_sp_form.py — 선발 직전 등판 폼(호투/평범/붕괴) 결정적 라벨러 (leak-free, 에이전트0).

검증(932 전수): 선발폼이 예측신호 22% (호투추세 + 초반/붕괴 취약). 박스 ip/er로 등판별 라벨:
  호투 = ip≥6.0 & er≤2  (또는 ip≥5.0 & er≤1)
  붕괴 = ip<5.0 또는 er≥5
  평범 = 그 외
원본 _box2026_game.parquet(전수, API산). 이름은 _sp_underlying_gamelog id↔name.

함수: build_timeline()->_sp_form.json {name:[(date,label,ip,er)]} · recent_sp(name,date,k=4)
usage: py _sp_form.py [--validate]
"""
import os
import sys
import json
import pandas as pd
from collections import defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))
BOX = os.environ.get("MLB_BOX_GAME_PARQUET") or os.path.join(ROOT, "_box2026_game.parquet")
SPL = os.path.join(ROOT, "_sp_underlying_gamelog.parquet")
OUT = os.environ.get("MLB_SP_FORM_JSON") or os.path.join(ROOT, "_sp_form.json")


def _ip(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def label(ip, er):
    if (ip >= 6.0 and er <= 2) or (ip >= 5.0 and er <= 1):
        return "호투"
    if ip < 5.0 or er >= 5:
        return "붕괴"
    return "평범"


def _id2name():
    try:
        s = pd.read_parquet(SPL)
        return dict(zip(s["pitcher"].astype("Int64"), s["name"]))
    except Exception:
        return {}


def build_timeline():
    g = pd.read_parquet(BOX)
    g["date"] = pd.to_datetime(g["date"])
    id2n = _id2name()
    rows = []
    for side in ("home", "away"):
        for _, r in g.iterrows():
            sid = r.get(f"{side}_sp_id")
            if pd.isna(sid):
                continue
            nm = id2n.get(int(sid))
            if not nm:
                continue
            ip, er = _ip(r.get(f"{side}_sp_ip")), _ip(r.get(f"{side}_sp_er"))
            rows.append((nm, str(r["date"])[:10], label(ip, er), ip, er))
    rows.sort(key=lambda x: x[1])
    tl = defaultdict(list)
    for nm, d, lab, ip, er in rows:
        tl[nm].append((d, lab, ip, er))
    json.dump(tl, open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"[sp_form] {len(tl)}명 선발 타임라인 → {OUT}")
    return tl


import re as _re
import unicodedata as _ud


def nkey(s):
    """이름 매칭키 — 액센트·구두점·공백 전부 제거(이니셜 J.T.↔J. T. 100% 일치)."""
    s = _ud.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return _re.sub(r"[^a-z0-9]", "", s.lower())


def recent_sp(name, date, k=4):
    tl = json.load(open(OUT, encoding="utf-8")) if os.path.exists(OUT) else {}
    seq = tl.get(name)
    if seq is None:   # 정규화 폴백
        idx = {nkey(kk): kk for kk in tl}
        kk = idx.get(nkey(name))
        seq = tl.get(kk, []) if kk else []
    return [(d, lab) for d, lab, *_ in seq if d < date][-k:]


def streak_note(name, date):
    """narr_context용 한 줄: 직전 등판 폼 패턴. 신호 약하면 None."""
    seq = [lab for d, lab in recent_sp(name, date, 4)]
    if len(seq) < 2:
        return None
    last2, last3 = seq[-2:], seq[-3:]
    if last2 == ["호투", "호투"] or seq.count("호투") >= 3:
        return f"호투추세(직전{len(seq)}: 호투{seq.count('호투')})"
    if last2 == ["붕괴", "붕괴"] or last3.count("붕괴") >= 2:
        return f"붕괴추세(직전{len(seq)}: 붕괴{seq.count('붕괴')})"
    return None


def validate():
    tl = build_timeline()
    # _reason_master pitcher_reads(선발만)의 read 텍스트에 '호투/압도' vs '붕괴/강판' 키워드 대조
    det = {}
    for nm, v in tl.items():
        for d, lab, ip, er in v:
            det[(d, nm)] = lab
    m = json.load(open(os.path.join(ROOT, "_reason_master.json"), encoding="utf-8"))
    agree = tot = 0
    for g in m:
        d = g["date"]
        for p in g.get("pitcher_reads", []):
            if "선발" not in str(p.get("role", "")):
                continue
            nm = p["name"].split(" (")[0].strip()
            dl = det.get((d, nm))
            if dl is None:
                continue
            txt = p.get("read", "")
            agt = "호투" if any(w in txt for w in ("호투", "압도", "봉쇄", "지배")) else ("붕괴" if any(w in txt for w in ("붕괴", "강판", "무너", "두들겨")) else "평범")
            tot += 1
            if agt == dl:
                agree += 1
    print(f"에이전트(read 키워드) vs 결정적 선발라벨 일치: {agree}/{tot} = {agree/max(tot,1):.0%}")


if __name__ == "__main__":
    if "--validate" in sys.argv:
        validate()
    else:
        build_timeline()
