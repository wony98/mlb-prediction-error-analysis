# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""_mlb_reason_master_freshness.py — pregame 누적회고 신선도 검증 (historical-note ①, warn-first).

목적(#51/#54/#57/#59 합의): 네레이터가 읽는 누적회고 정본 `_reason_master.json`이
target_date 직전까지 최신인지 확인. 5일 stale 같은 구멍을 분석 전에 표면화.

판정: max(_reason_master.date) >= target_date - 1day  →  PASS
      아니면  →  WARN (며칠 stale + 빠진 날짜 + 정확한 catch-up 체인 명령 출력)

원칙:
  - warn-first: 기본은 **비차단**(exit 0). 분석을 막지 않고 경고만.
  - --strict: 향후 승급용. stale 이면 exit 2 (block).
  - 자동 catch-up 실행 안 함 — "다음 명령"만 출력(사람/운영 에이전트가 실행).

usage:
  py _mlb_reason_master_freshness.py 2026-06-12
  py _mlb_reason_master_freshness.py            # 기본 = ET 기준 오늘
  py _mlb_reason_master_freshness.py 2026-06-12 --strict
  py _mlb_reason_master_freshness.py 2026-06-12 --json
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timedelta, timezone
from _mlb_result_status import fetch_mlb_result_status_meta

ROOT = os.path.dirname(os.path.abspath(__file__))
MASTER = os.path.join(ROOT, "_reason_master.json")
REASON_CONTENT = os.path.join(ROOT, "_reason_content")


def _et_today() -> str:
    # StatsAPI 날짜 기준 = US/ET. KST 오늘과 다를 수 있어 ET-4(여름 EDT) 근사.
    return (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%d")


def _master_max_date() -> tuple[str | None, int]:
    if not os.path.exists(MASTER):
        return None, 0
    m = json.load(open(MASTER, encoding="utf-8"))
    ds = sorted({g.get("date") for g in m if g.get("date")})
    return (ds[-1] if ds else None), len(m)


def _content_dates() -> set[str]:
    """staging(_reason_content/{date}) 폴더에 존재하는 날짜 — catch-up 갭 계산용."""
    if not os.path.isdir(REASON_CONTENT):
        return set()
    return {d for d in os.listdir(REASON_CONTENT) if os.path.isdir(os.path.join(REASON_CONTENT, d)) and d.startswith("2026-")}


def check(target: str) -> dict:
    max_date, n_games = _master_max_date()
    td = datetime.strptime(target, "%Y-%m-%d").date()
    need_through = td - timedelta(days=1)  # master 는 '어제'까지 커버해야 함

    result = {
        "schema": "mlb_reason_master_freshness.v1",
        "research_only": True,
        "target_date": target,
        "need_master_through": need_through.isoformat(),
        "master_max_date": max_date,
        "master_games": n_games,
        "ok": False,
        "days_stale": None,
        "missing_dates": [],
        "catch_up_status": [],
        "catch_up": [],
    }
    if max_date is None:
        result["error"] = "_reason_master.json 없음/빈 파일"
        return result

    md = datetime.strptime(max_date, "%Y-%m-%d").date()
    result["ok"] = md >= need_through
    if result["ok"]:
        return result

    # stale — 빠진 날짜(어제까지 - master 이후) 산출
    result["days_stale"] = (need_through - md).days
    have = _content_dates()
    miss = []
    d = md + timedelta(days=1)
    while d <= need_through:
        ds = d.isoformat()
        row = {"date": ds, "staging_exists": ds in have}
        try:
            meta = fetch_mlb_result_status_meta(ds)
            no_result = sorted(pk for pk, m in meta.items() if m.get("no_result"))
            final_scored = sorted(pk for pk, m in meta.items() if m.get("is_final_scored"))
            row.update({
                "schedule_games": len(meta),
                "expected_deepread_games": len(final_scored),
                "final_scored_gamePks": final_scored,
                "excluded_no_result_gamePks": no_result,
            })
        except Exception as e:
            row["status_fetch_error"] = str(e)
        miss.append(row)
        result["catch_up_status"].append(row)
        d += timedelta(days=1)
    result["missing_dates"] = miss
    # 정확한 catch-up 체인(#57/#59): staging 없는 날은 풀체인, 있는 날은 recap만으로 충분
    need_full = [x["date"] for x in miss if not x["staging_exists"]]
    if need_full:
        start, end = need_full[0], need_full[-1]
        batch = "recap_" + start[5:].replace("-", "") + "_" + end[5:].replace("-", "")
        wf_cmd = f"py _make_batch_wf.py {batch} {start} {end} opus"
    else:
        wf_cmd = "# staging exists for all missing dates; run only the matching workflow ingest if output is already available"
    result["catch_up"] = [
        f"# 빠진 {len(miss)}일 (staging無 {len(need_full)}일) 누적회고 채우기 — 정본 체인",
        *[f"py _reason_bundles.py {dt}" for dt in need_full],
        wf_cmd + "   # 미독 경기 LLM 배치 생성 → workflow 실행",
        "py _recap_to_master.py <workflow_output.json>   # _reason_master.json 신규 gpk append (dedup)",
        "# 이후 merge-quality validator 로 row마다 pitcher_reads|hitter_reads·date·gamePk 확인",
    ]
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="pregame 누적회고(_reason_master) 신선도 검증 (warn-first)")
    ap.add_argument("target", nargs="?", default=None, help="YYYY-MM-DD (기본 = ET 오늘)")
    ap.add_argument("--strict", action="store_true", help="stale 이면 exit 2 (block). 기본은 warn-only exit 0")
    ap.add_argument("--json", action="store_true", help="JSON 출력")
    args = ap.parse_args()

    target = args.target or _et_today()
    r = check(target)

    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        if r.get("error"):
            print(f"[freshness] ERROR target={target}: {r['error']}")
        elif r["ok"]:
            print(f"[freshness] PASS — _reason_master max={r['master_max_date']} ≥ 필요 {r['need_master_through']} (target {target}, {r['master_games']}경기)")
        else:
            print(f"[freshness] ⚠ WARN — 누적회고 {r['days_stale']}일 STALE")
            print(f"            target={target} → master는 {r['need_master_through']}까지 필요한데 현재 max={r['master_max_date']}")
            print(f"            빠진 날짜: {', '.join(x['date'] + ('' if x['staging_exists'] else '(staging無)') for x in r['missing_dates'])}")
            print("            ── catch-up (자동실행 안 함, 다음 명령만) ──")
            for x in r.get("catch_up_status", []):
                if x.get("excluded_no_result_gamePks"):
                    print(
                        f"            no_result excluded {x['date']}: {x['excluded_no_result_gamePks']} "
                        f"(expected_deepread={x.get('expected_deepread_games')}/{x.get('schedule_games')})"
                    )
            for ln in r["catch_up"]:
                print(f"            {ln}")

    if args.strict and not r["ok"]:
        return 2
    return 0  # warn-first: 기본 비차단


if __name__ == "__main__":
    raise SystemExit(main())
