# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""_form_label.py — PBP에서 타자 그날 폼(락인/평범/부진)을 *결정적*으로 계산 (에이전트 0, leak-free).

검증된 루브릭 기계 재현:
  강한타구 = 설명에 'line drive' 또는 'sharp', 또는 event∈{Double,Triple,Home Run} (장타).
            라인아웃(line drive 잡힘)도 강한타구로 카운트(정타인데 야수정면=부진 아님).
  락인 = 강한타구 2+ / 평범 = 1 / 부진 = 0.
  빗맞은 안타(soft/bloop/ground ball 단타)는 강한타구 아님.

함수:
  game_form(narrative) -> {batter: ('락인'|'평범'|'부진', strong_count, pa_count)}
  build_timeline()     -> _form_label_hitters.json  {batter: [(date, label, strong, pa)]}  (전 경기)
  recent(name, date, k=5) -> 최근 k경기 라벨 리스트 (date 미만 = leak-free)

usage: py _form_label.py            (타임라인 빌드)
       py _form_label.py --validate (에이전트 라벨과 일치율)
"""
import os
import re
import sys
import json
import pandas as pd
from collections import defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))
NARR = os.environ.get("MLB_NARR_PARQUET") or os.path.join(ROOT, "_narr2026.parquet")
OUT = os.environ.get("MLB_FORM_LABEL_HITTERS_JSON") or os.path.join(ROOT, "_form_label_hitters.json")

PA_RE = re.compile(r"(\d+)회([초말])\s+(.+?)\svs\s+(.+?)\s+\[(.+?)\]\s*(.*)")
STRONG_EVENT = {"Double", "Triple", "Home Run"}


def _is_strong(event, desc):
    d = desc.lower()
    if "line drive" in d or "sharp" in d:
        return True
    if event in STRONG_EVENT:
        # 장타지만 'soft'/'bloop'/'wall'/'ground' 명시면 약타 취급
        if any(w in d for w in ("soft", "bloop", "ground")):
            return False
        return True
    return False


def game_form(narrative):
    """narrative(PBP) → {batter: (label, strong, pa)}."""
    strong = defaultdict(int)
    pa = defaultdict(int)
    for ln in str(narrative).splitlines():
        m = PA_RE.search(ln)
        if not m:
            continue
        batter, event, desc = m.group(3).strip(), m.group(5).strip(), m.group(6)
        pa[batter] += 1
        if _is_strong(event, desc):
            strong[batter] += 1
    out = {}
    for b in pa:
        s = strong[b]
        out[b] = ("락인" if s >= 2 else "평범" if s == 1 else "부진", s, pa[b])
    return out


def build_timeline():
    nar = pd.read_parquet(NARR).sort_values("date")
    tl = defaultdict(list)
    for _, r in nar.iterrows():
        d = str(r["date"])[:10]
        for b, (lab, s, p) in game_form(r["narrative"]).items():
            tl[b].append((d, lab, s, p))
    json.dump(tl, open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"[form_label] {len(tl)}명 타자 타임라인 → {OUT}")
    return tl


def recent(name, date, k=5):
    tl = json.load(open(OUT, encoding="utf-8")) if os.path.exists(OUT) else {}
    return [(d, lab) for d, lab, s, p in tl.get(name, []) if d < date][-k:]


def validate():
    """에이전트 hitter_reads state vs 결정적 라벨 일치율 (deep-read _reason_master)."""
    det = build_timeline()
    detmap = {}  # (date, batter) -> label
    nar = pd.read_parquet(NARR)
    for _, r in nar.iterrows():
        d = str(r["date"])[:10]
        for b, (lab, s, p) in game_form(r["narrative"]).items():
            detmap[(d, b)] = lab
    m = json.load(open(os.path.join(ROOT, "_reason_master.json"), encoding="utf-8"))
    agree = tot = 0
    conf = defaultdict(int)
    for g in m:
        d = g["date"]
        for h in g.get("hitter_reads", []):
            nm = h["name"].split(" (")[0].strip()
            a = h["state"]
            dl = detmap.get((d, nm))
            if dl is None:
                continue
            tot += 1
            conf[(a, dl)] += 1
            if a == dl:
                agree += 1
    print(f"\n에이전트 vs 결정적 라벨 일치: {agree}/{tot} = {agree/tot:.1%}")
    print("혼동(에이전트→결정적):")
    for (a, dl), c in sorted(conf.items(), key=lambda x: -x[1]):
        flag = " ✓" if a == dl else ""
        print(f"  {a}→{dl}: {c}{flag}")


if __name__ == "__main__":
    if "--validate" in sys.argv:
        validate()
    else:
        build_timeline()
