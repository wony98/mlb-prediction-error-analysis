# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""_recap_to_master.py — 정독 회고 결과를 master에 병합 + 폼 타임라인 재빌드 + synth_input 생성.

매일 경기 종료 → 정독 워크플로 산출(JSON) → 이 스크립트 1회로 갱신 완료.
입력 JSON은 워크플로 raw output({summary,result,...}) 또는 정독 결과 배열 둘 다 허용.
gamePk → date 는 _reason_content/{date}/{gpk}.md 폴더에서 자동 매핑.

동작(부작용 = 갱신):
  1) 각 경기에 date 부여 (_reason_content 폴더명)
  2) _reason_master.json 백업 후 신규 gpk만 병합(중복 skip)
  3) _player_form_hitters.json / _player_form_pitchers.json 를 master에서 전량 재빌드(날짜순)
  4) 신규 gpk 들의 _synth_input/{gpk}.md 생성(다음 합성② 입력)

usage: py _recap_to_master.py _recap65_out.json
       py _recap65_out.json 자리에 워크플로 .output 경로도 가능
"""
import os
import sys
import glob
import json
import shutil
import subprocess
from collections import defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))
MASTER = os.path.join(ROOT, "_reason_master.json")


def gpk_date_map():
    m = {}
    for f in glob.glob(os.path.join(ROOT, "_reason_content", "2026-*", "*.md")):
        m[os.path.basename(f)[:-3]] = os.path.basename(os.path.dirname(f))
    return m


def load_recap(path):
    d = json.load(open(path, encoding="utf-8"))
    if isinstance(d, dict) and "result" in d:   # 워크플로 raw output
        d = d["result"]
    if not isinstance(d, list):
        raise SystemExit(f"[err] 배열/`result` 키 아님: {path}")
    return d


def rebuild_forms(master):
    H, P = defaultdict(list), defaultdict(list)
    for r in sorted(master, key=lambda x: (x["date"], str(x["gamePk"]))):
        d = r["date"]
        for h in r.get("hitter_reads", []):
            H[h["name"].split(" (")[0].strip()].append([d, h["state"]])
        for p in r.get("pitcher_reads", []):
            P[p["name"].split(" (")[0].strip()].append([d, p.get("role", ""), p.get("read", "")])
    json.dump(H, open(os.path.join(ROOT, "_player_form_hitters.json"), "w", encoding="utf-8"), ensure_ascii=False)
    json.dump(P, open(os.path.join(ROOT, "_player_form_pitchers.json"), "w", encoding="utf-8"), ensure_ascii=False)
    return (sum(len(v) for v in H.values()), len(H), sum(len(v) for v in P.values()), len(P))


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: py _recap_to_master.py <recap.json|workflow.output>")
    recap = load_recap(sys.argv[1])
    g2d = gpk_date_map()
    miss = []
    for r in recap:
        r["date"] = g2d.get(str(r["gamePk"]))
        if not r["date"]:
            miss.append(str(r["gamePk"]))
    if miss:
        raise SystemExit(f"[err] _reason_content 폴더에서 date 못 찾음: {miss}")

    M = json.load(open(MASTER, encoding="utf-8"))
    have = set(str(r["gamePk"]) for r in M)
    add = [r for r in recap if str(r["gamePk"]) not in have]
    shutil.copy(MASTER, MASTER + ".bak")
    M2 = M + add
    json.dump(M2, open(MASTER, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[master] {len(M)} -> {len(M2)} (+{len(add)} 신규, 중복 {len(recap)-len(add)} skip) | 백업 {os.path.basename(MASTER)}.bak")

    hr, hp, pr, pp = rebuild_forms(M2)
    print(f"[form] hitters {hr}rows/{hp} | pitchers {pr}rows/{pp} (master 전량 재빌드)")

    # 신규 gpk synth_input 생성
    if add:
        gpks = [str(r["gamePk"]) for r in add]
        subprocess.run([sys.executable, os.path.join(ROOT, "_synth_input.py"), *gpks], cwd=ROOT,
                       stdout=subprocess.DEVNULL)
        print(f"[synth_input] {len(gpks)}경기 생성 → 다음: py _make_synth_wf.py <name> <s> <e> opus")
    else:
        print("[synth_input] 신규 경기 없음 — skip")

    # 커버리지 경고: played(box) 중 deep-read 누락 — 다신 silent 안 되게(5/21 구멍 재발 방지)
    try:
        bx = pd.read_parquet(os.path.join(ROOT, "_box2026_game.parquet"))
        played = set(bx["gamePk"].astype(int).astype(str))
        dr = set(str(r["gamePk"]) for r in M2)
        gap = played - dr
        if gap:
            import collections
            bx["d"] = bx["date"].astype(str).str[:10]
            gd = collections.Counter(bx[bx["gamePk"].astype(int).astype(str).isin(gap)]["d"])
            print(f"⚠️ [coverage] played {len(played)} 중 deep-read 누락 {len(gap)}경기 — 날짜 {dict(gd)} → _reason_bundles+_make_batch_wf로 메꿔라")
        else:
            print(f"✅ [coverage] played {len(played)} 전 경기 deep-read 완비 (누락 0)")
    except Exception:
        pass


if __name__ == "__main__":
    main()
