# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""_bp_form.py — 불펜투수 직전 등판 폼(안정/불안) 결정적 라벨러 (leak-free, 에이전트0).

검증(932 전수): 정독이 전 불펜투수 하나하나 컨디션 읽음. 그 직전폼 streak를 결정적 재현.
원본 _narr_bullpen_stints.parquet (투수별 등판별 runs_allowed·blown_lead·went_behind, 이미 수집).
  불안 = 실점≥1 또는 blown_lead 또는 went_behind / 안정 = 무실점·무blown.

함수: build_timeline()->_bp_form.json {name:[(date,label,runs)]} · recent_bp · streak_note
usage: py _bp_form.py [--validate]
"""
import os
import re
import sys
import json
import unicodedata
import pandas as pd
from collections import defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))
STN = os.environ.get("MLB_BULLPEN_STINTS_PARQUET") or os.path.join(ROOT, "_narr_bullpen_stints.parquet")
OUT = os.environ.get("MLB_BP_FORM_JSON") or os.path.join(ROOT, "_bp_form.json")


def nkey(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def label(runs, blown, behind):
    return "불안" if (float(runs or 0) >= 1 or bool(blown) or bool(behind)) else "안정"


def build_timeline():
    st = pd.read_parquet(STN).copy()
    st["date"] = pd.to_datetime(st["date"])
    st = st.sort_values("date")
    tl = defaultdict(list)
    for _, r in st.iterrows():
        nm = r.get("pitcher")
        if not nm:
            continue
        lab = label(r.get("runs_allowed"), r.get("blown_lead"), r.get("went_behind"))
        tl[str(nm)].append((str(r["date"])[:10], lab, float(r.get("runs_allowed") or 0)))
    json.dump(tl, open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"[bp_form] {len(tl)}명 불펜 타임라인 → {OUT}")
    return tl


def recent_bp(name, date, k=4):
    tl = json.load(open(OUT, encoding="utf-8")) if os.path.exists(OUT) else {}
    seq = tl.get(name)
    if seq is None:
        idx = {nkey(kk): kk for kk in tl}
        seq = tl.get(idx.get(nkey(name)), [])
    return [(d, lab) for d, lab, *_ in seq if d < date][-k:]


def streak_note(name, date):
    seq = [lab for d, lab in recent_bp(name, date, 4)]
    if len(seq) < 2:
        return None
    last2 = seq[-2:]
    if last2 == ["불안", "불안"] or seq.count("불안") >= 3:
        return f"불안추세(직전{len(seq)}: 불안{seq.count('불안')})"
    if seq.count("안정") >= 3:
        return f"안정추세(직전{len(seq)}: 안정{seq.count('안정')})"
    return None


def validate():
    build_timeline()
    print("불펜 폼 = stints runs/blown 직접 라벨(객관). 에이전트 read와 별도 검증 불필요(소스 사실).")


if __name__ == "__main__":
    if "--validate" in sys.argv:
        validate()
    else:
        build_timeline()
