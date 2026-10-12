# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""_synth_input.py — 합성② 입력 조립: 경기 정독결론 + 핵심선수 *직전 폼*.

각 경기 → _synth_input/{gpk}.md:
  ① 정독 결론(why/not_luck/luck/verdict, 이 경기서 실제 일어난 것)
  ② 핵심 선수(전 투수 + 락인/부진 타자)의 *이 경기 직전까지* 폼 타임라인
네레이터는 이걸 보고 "각 이유가 사전에 알 수 있었나(전=예측가능) vs 아니었나(후)" 분리 + 운 재검증.

usage: py _synth_input.py            (전체 932 생성)
       py _synth_input.py <gpk> ...  (특정 경기만)
"""
import os
import sys
import json
from collections import defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))
M = json.load(open(os.path.join(ROOT, "_reason_master.json"), encoding="utf-8"))
HF = json.load(open(os.path.join(ROOT, "_player_form_hitters.json"), encoding="utf-8"))
PF = json.load(open(os.path.join(ROOT, "_player_form_pitchers.json"), encoding="utf-8"))
ENV = {}   # {gamePk: '날씨·심판'} — main()에서 날짜별 채움(운/실력 재검증용)


def _load_env(dates):
    """날씨·심판 → ENV. 베이스=_env_backfill.json(statsapi, 전 경기), 위에 _v3_games(최근, 풍향·강수·심판평균 더 풍부) 오버레이."""
    bf = os.path.join(ROOT, "_env_backfill.json")
    if os.path.exists(bf):
        try:
            for g, e in json.load(open(bf, encoding="utf-8")).items():
                ENV[int(g)] = e
        except Exception:
            pass
    try:
        import _reason_bundles as RB
        for d in set(dates):
            ENV.update(RB._env_blocks(d))   # 최근 슬레이트는 더 풍부 → 덮어씀
    except Exception:
        pass


_HC = {"락인": "락", "평범": "평", "부진": "부"}
_PC = {"호투": "호", "평범": "평", "붕괴": "붕"}


def prior_hit(name, date, k=6):
    """name의 date 이전 최근 k경기 state 리스트."""
    seq = [s for d, s in HF.get(name, []) if d < date][-k:]
    return seq


def prior_pit(name, date, k=4):
    seq = [(d, rd) for d, role, rd in PF.get(name, []) if d < date][-k:]
    return seq


def season_hit(name, date):
    """시즌 전체 타자 흐름 시퀀스(과거→최근, date 미만) — 단발인지 시즌 패턴인지 판단용. 버킷 X."""
    seq = [_HC.get(s, s) for d, s in HF.get(name, []) if d < date]
    return "".join(seq) if len(seq) >= 5 else ""


def season_pit(name, date):
    """시즌 전체 선발 폼 시퀀스(_sp_form 라벨, 깨끗) — 시즌 어떻게 흘러왔나 통째."""
    try:
        import _sp_form as SPF
        seq = [_PC.get(l, l) for _d, l in SPF.recent_sp(str(name), date, 99)]
    except Exception:
        return ""
    return "".join(seq) if len(seq) >= 4 else ""


def build_one(r):
    gpk, date = r["gamePk"], r["date"]
    L = [f"# {r['matchup']}  ({date})", ""]
    L.append("## ① 정독 결론 (이 경기에서 실제 일어난 것)")
    L.append(f"- 승자: {r['winner']}")
    L.append(f"- 왜: {r.get('why','')}")
    L.append(f"- 운 아닌 실제 이유: {r.get('not_luck','')}")
    L.append(f"- 운으로 본 것: {r.get('luck','')}")
    L.append(f"- 종합: {r.get('verdict','')}")
    L.append("")
    env = ENV.get(int(gpk)) if str(gpk).lstrip("-").isdigit() else None
    if env:
        L.append(f"## ⓪ 그날 환경 (운/실력 재검증용): {env}")
        L.append("")
    L.append("## ② *전* 선수의 폼 — *직전 추세* + *시즌 전체 흐름* 둘 다 (사전 인지 여부 판단용)")
    L.append("   ※ '변수'라 결론내기 전에 *실제 점수 낸 선수*(홈런·적시타 친 타자)의 폼을 반드시 확인하라. 평범 라벨이어도 그날 친 선수면 폼 검증 필수.")
    L.append("   ※ 직전 단발이 시즌 흐름과 어긋나면 노이즈, 시즌과 같으면 진짜. %·기준 버킷 말고 시퀀스 통째로. 바람 탄 HR은 환경(날씨)으로 분리.")
    L.append("[타자 — 이 경기 주목 타자 *전원*(락인·평범·부진 모두)]")
    for h in r.get("hitter_reads", []):
        nm = h["name"].split(" (")[0].strip()
        pr = prior_hit(nm, date); sea = season_hit(nm, date)
        tag = "·".join(pr) if pr else "직전 기록 없음(시즌초/첫등장)"
        note = (h.get("note") or "")[:40]
        L.append(f"- {nm} (이 경기 {h['state']}{': '+note if note else ''}): 직전 {tag}" + (f" / 시즌흐름 {sea}" if sea else ""))
    L.append("")
    L.append("[투수 — 이 경기 등판 투수 전원(선발+불펜)]")
    for p in r.get("pitcher_reads", []):
        nm = p["name"].split(" (")[0].strip()
        pr = prior_pit(nm, date); sea = season_pit(nm, date)
        hist = " || ".join(f"{d}: {rd}" for d, rd in pr) if pr else "직전 등판 기록 없음"
        L.append(f"- {nm} ({p.get('role','')}, 이 경기: {p.get('read','')[:45]}): 직전 → {hist}" + (f" / 시즌흐름 {sea}" if sea else ""))
    return "\n".join(L)


def main():
    args = [a for a in sys.argv[1:]]
    outdir = os.path.join(ROOT, "_synth_input")
    os.makedirs(outdir, exist_ok=True)
    targets = [r for r in M if not args or str(r["gamePk"]) in args]  # gpk int vs str args 비교 fix(특정 gpk 호출 시 0경기 버그)
    _load_env([r["date"] for r in targets])
    for r in targets:
        open(os.path.join(outdir, f"{r['gamePk']}.md"), "w", encoding="utf-8").write(build_one(r))
    print(f"[synth_input] {len(targets)}경기 → {outdir}")
    if args:
        for r in targets:
            print("\n" + "=" * 70)
            print(build_one(r))


if __name__ == "__main__":
    main()
