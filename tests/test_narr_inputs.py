# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""TDD: 네레이터 분석 입력 점검 — *회고가 분석에 실제로 반영되는가*를 결정적으로 가드.

플럼빙/커버리지만 테스트(LLM이 옳게 가중했나는 기준/eval 영역이라 제외):
  1. parse_predictions 새 포맷(순수 예측) → 전 경기 WIN_LOSS+RUN_TOTAL 값 유효
  2. priors 커버리지 — 슬레이트 모든 SP가 _narr_priors에 끌려옴(조용한 누락 0)
  3. priors leak-free — target date 이상 read 없음
  4. current_il MLB-only(마이너 노이즈 0) [네트워크, best-effort]
  4.5 시즌흐름 시퀀스 커버리지 — 이력 충분한 SP가 priors에 시즌흐름 누락 0(통째 읽기 재료 보장)
  5. freeze roundtrip — frozen 예측 저장→로드 보존
  [RUN_TOTAL 전용 커버리지 — WIN_LOSS이 SP 누락 가드하듯, RUN_TOTAL 1차 동력 '양 타선 궤적' 입력 누락 가드. 방향강제는 안 함(기준)]
  6. RUN_TOTAL 기준점 커버리지 — 전 경기 run_total_baseline 존재(누락=RUN_TOTAL예측 null)
  7. RUN_TOTAL 타선 궤적 커버리지 — 라인업 타자 중 *이력 있는* 선수가 priors에 조용히 드롭 0(이름정규화·로직 회귀 가드)
  8. deepread_v2 as-of — pre_knowable만 포함, target date 이상 누수 0
  9. player_identity — 슬레이트 선수는 mapped 또는 unmapped로 명시(조용한 누락 0)
 10. 환경 블록 — narr_context 저장본이 있으면 경기마다 [환경] 존재
 11. reasoning 블록 수 — 저장본이 있으면 슬레이트 경기 수와 동일
 12. deepread_v2 QA — unmapped player_id에 역할 suffix 금지, 야수등판 pre_knowable 금지

usage: py test_narr_inputs.py [YYYY-MM-DD]
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
import pandas as _pd
DATE = sys.argv[1] if len(sys.argv) > 1 else (_pd.Timestamp.utcnow() - _pd.Timedelta(hours=4)).strftime("%Y-%m-%d")
fail = []


# ── 1. parse_predictions 새 포맷 ──
import _narr_reasoning as NR
sample = (
    "### Athletics @ Houston Astros · RUN_TOTAL 9.0\n"
    "WIN_LOSS: Houston Astros — Burrows 우위, ATH 타선 부상\n"
    "RUN_TOTAL: LOWER 9.0 — Jump 직전 8이닝 1실점 지배\n"
    "예측: WIN_LOSS=HOME RUN_TOTAL=LOWER\n"
    "### Boston Red Sox @ New York Yankees · RUN_TOTAL 8.0\n"
    "WIN_LOSS: - — 진짜 팽팽\n"
    "RUN_TOTAL: - — 불확실\n"
    "예측: WIN_LOSS=- RUN_TOTAL=-\n")
pk = NR.parse_predictions(sample)
if len(pk) != 2:
    fail.append(f"parse_predictions 경기수 {len(pk)} != 2")
elif not (pk[0] == {"win_loss": "HOME", "run_total": "LOWER"} and pk[1] == {"win_loss": None, "run_total": None}):
    fail.append(f"parse_predictions 값 오류: {pk}")

meta = NR.parse_evidence_refs(
    "### A @ B · RUN_TOTAL 8.5\n"
    "WIN_LOSS: B — 선발 근거 drv2:1_A_SP lens3:1_MARKET\n"
    "RUN_TOTAL: LOWER 8.5 — 불펜 근거 drv2:1_B_RUN_TOTAL lens3:1_ENV\n"
    "예측: WIN_LOSS=HOME RUN_TOTAL=LOWER\n")
if not (len(meta) == 1
        and meta[0]["win_loss_evidence_refs"] == ["drv2:1_A_SP", "lens3:1_MARKET"]
        and meta[0]["run_total_evidence_refs"] == ["drv2:1_B_RUN_TOTAL", "lens3:1_ENV"]):
    fail.append(f"parse_evidence_refs 오류: {meta}")


# ── 2. priors 커버리지 (슬레이트 SP 전원 끌려옴) ──
import pandas as pd
csv = os.environ.get("NARR_V3_INPUT_PATH") or os.path.join(
    ROOT, f"_v3_games_{DATE}.csv"
)
pf = os.environ.get("NARR_PRIORS_INPUT_PATH") or os.path.join(
    ROOT, f"_narr_priors_{DATE}.txt"
)

# -- 1.5. lineup/bullpen name-list storage format diagnostics --
if os.path.exists(csv):
    from _mlb_name_list import get_parse_diagnostics, parse_name_list, reset_parse_diagnostics

    reset_parse_diagnostics()
    _name_df = pd.read_csv(csv)
    for _name_col in (
        "home_lineup_names", "away_lineup_names",
        "home_bullpen_names", "away_bullpen_names",
    ):
        if _name_col in _name_df.columns:
            for _name_value in _name_df[_name_col]:
                parse_name_list(_name_value)
    _name_diag = get_parse_diagnostics()
    if _name_diag["malformed_bracket_count"]:
        fail.append(
            "name-list malformed bracket values: "
            f"count={_name_diag['malformed_bracket_count']} "
            f"samples={_name_diag['malformed_bracket_samples']}"
        )

if os.path.exists(csv) and os.path.exists(pf):
    df = pd.read_csv(csv)
    ptxt = open(pf, encoding="utf-8").read()
    sp_names = []
    for c in ("home_sp_name", "away_sp_name"):
        sp_names += [str(x) for x in df.get(c, pd.Series(dtype=str)).dropna() if str(x).strip()]
    missing = [nm for nm in sp_names if nm.split()[-1] not in ptxt and nm not in ptxt]
    if missing:
        fail.append(f"priors 커버리지 누락 SP(조용한 드롭): {missing}")
    # 모든 SP가 'read):' 또는 '회고 없음' 둘 중 하나로 명시됐는지 (누락 0)
    n_sp_blocks = len(re.findall(r"선발 .+? — .+? \(직전 \d+경기 read\):|회고 기록 없음", ptxt))
    if n_sp_blocks < len(sp_names):
        fail.append(f"priors SP 블록 {n_sp_blocks} < 슬레이트 SP {len(sp_names)} (누락)")
else:
    print(f"[skip] 2: 슬레이트/priors 파일 없음 ({DATE}) — 생성 후 재실행")


# ── 3. priors leak-free ──
P, H = NR_priors_idx = __import__("_narr_priors").build_index()
leak = []
for nm, seq in list(P.items())[:2000]:
    for d, _ in seq:
        pass
# 핵심: _recent가 date 미만만 반환
import _narr_priors as NP
probe = next((nm for nm, s in P.items() if len(s) >= 3), None)
if probe:
    rec = NP._recent(P, probe, DATE, 10)
    if any(d >= DATE for d, _ in rec):
        fail.append(f"priors leak: {probe} 에 {DATE} 이상 read 포함")


# ── 4. current_il MLB-only (네트워크 best-effort) ──
try:
    import _mlb_injuries as INJ
    mlb = INJ._mlb_teams()
    if mlb:
        il = INJ.current_il(DATE)
        bad = [t for t in il if t not in mlb]
        if bad:
            fail.append(f"current_il 비-MLB 팀 누출(마이너 노이즈): {bad[:3]}")
    else:
        print("[skip] 4: MLB 팀 목록 fetch 실패(오프라인)")
except Exception as e:
    print(f"[skip] 4: current_il 네트워크 예외 — {type(e).__name__}")


# ── 4.5 시즌흐름 시퀀스 커버리지 (이력 충분한 SP가 priors에 시즌흐름 누락 0) ──
#     옛 ≥3 '반복신호 flag'는 임계값+방향강제(기준)라 제거(2026-06-08). 무기준 = raw 시퀀스를 통째로 읽기.
#     TDD는 그 시퀀스가 *전 SP에 실제로 실리나*(coverage)만 가드 — 방향은 LLM 통째 읽기 몫.
try:
    import _narr_priors as NP2
    if os.path.exists(csv) and os.path.exists(pf):
        df2 = pd.read_csv(csv)
        ptxt2 = open(pf, encoding="utf-8").read()
        miss_seq = []
        for c in ("home_sp_name", "away_sp_name"):
            for nm in [str(x) for x in df2.get(c, pd.Series(dtype=str)).dropna() if str(x).strip()]:
                ssn = NP2.sp_season_form(nm, DATE)
                if ssn and ssn not in ptxt2:        # 시퀀스 산출됐는데 priors에 없음 = 조용한 드롭
                    miss_seq.append(nm)
        if miss_seq:
            fail.append(f"시즌흐름 커버리지 누락(이력 있는데 priors 드롭): {sorted(set(miss_seq))[:8]}")
except Exception as e:
    print(f"[skip] 4.5: 시즌흐름 커버리지 — {type(e).__name__}")


# ── 5. freeze roundtrip ──
import json
_tmp_date = "2099-01-01"
NR.save_predictions(_tmp_date, [{"gamePk": 1, "away": "A", "home": "B", "run_total": "UPPER", "win_loss": "B", "line": 8.5,
                           "confirmed": True, "frozen": True}])
ld = NR.load_predictions(_tmp_date)
if not (ld and ld.get("1", {}).get("frozen") is True):
    fail.append(f"freeze roundtrip 실패: {ld}")
try:
    os.remove(os.path.join(ROOT, f"_narr_predictions_{_tmp_date}.json"))
except Exception:
    pass


# ══════════ RUN_TOTAL 전용 커버리지 (WIN_LOSS SP 가드의 RUN_TOTAL판 — 타선 궤적·기준점 누락 방지) ══════════
# RUN_TOTAL 1차 동력은 *양 타선 궤적*(2026-06-08 회고: RUN_TOTAL를 구조신호 단축으로 읽다 미스 → 타선 궤적 우선으로 정정).
# 타선 궤적이 입력에서 조용히 빠지면 고친 프롬프트도 *없는 걸 못 읽음* → 결정적 커버리지만 가드(방향강제 X).

# ── 6. RUN_TOTAL 기준점 커버리지 ──
if os.path.exists(csv):
    df6 = pd.read_csv(csv)
    run_total_baseline_cols = [c for c in ("run_total_baseline", "run_total_main_baseline", "run_total_dominant_baseline") if c in df6.columns]
    if not run_total_baseline_cols:
        fail.append("RUN_TOTAL: run_total_baseline/alias 컬럼 없음(슬레이트 스키마 변경)")
    else:
        line_any = df6[run_total_baseline_cols].apply(lambda row: any(pd.notna(v) for v in row), axis=1)
        missing_line = df6.loc[~line_any].copy()
        if not missing_line.empty:
            explicit_skip = (
                missing_line.get("run_total_zone", pd.Series("", index=missing_line.index)).fillna("").astype(str).str.lower().eq("skip")
                & ~missing_line.get("run_total_is_value", pd.Series(False, index=missing_line.index)).fillna(False).astype(bool)
            )
            bad_missing = missing_line.loc[~explicit_skip]
            if not bad_missing.empty:
                fail.append(f"RUN_TOTAL 기준점 누락 {len(bad_missing)}경기(RUN_TOTAL예측 null 위험)")
            else:
                print(f"[info] 6: RUN_TOTAL 기준점 없는 {len(missing_line)}경기는 run_total_zone=skip으로 명시 처리")
else:
    print(f"[skip] 6: 슬레이트 파일 없음 ({DATE})")

# ── 7. RUN_TOTAL 타선 궤적 커버리지 (이력 있는 라인업 타자 priors 드롭 0) ──
if os.path.exists(csv) and os.path.exists(pf):
    import _narr_priors as NP7
    df7 = pd.read_csv(csv)
    ptxt7 = open(pf, encoding="utf-8").read()
    _, H7 = NP7.build_index()
    n_hist = 0
    dropped = []
    for _, row in df7.iterrows():
        for sidecol in ("home_lineup_names", "away_lineup_names"):
            for nm in NP7._names(row.get(sidecol)):
                if NP7._recent(H7, nm, DATE, 3):     # 이력 있음 → priors에 *반드시* 나와야
                    n_hist += 1
                    if nm not in ptxt7:
                        dropped.append(nm)
    if dropped:
        fail.append(f"RUN_TOTAL 타선 커버리지 누락(이력 있는데 priors 조용한 드롭): {sorted(set(dropped))[:8]}")
    elif n_hist == 0:
        print("[skip] 7: 슬레이트 라인업 미확정/이력 타자 0 (라이브 라인업 전이면 정상)")
else:
    print(f"[skip] 7: 슬레이트/priors 파일 없음 ({DATE})")


# ── 8. deepread_v2 as-of: pre_knowable only + leak-free ──
try:
    import _deepread_v2 as DR2
    probs = DR2.validate_asof(DATE)
    if probs:
        fail.append(f"deepread_v2 validate_asof 실패: {probs[:5]}")
    packet = DR2.build_asof(DATE, write=True)
    bad = [ev.get("event_id") for ev in packet.get("events", [])
           if ev.get("timing") != "pre_knowable" or str(ev.get("date")) >= DATE]
    if bad:
        fail.append(f"deepread_v2 asof 누수/비사전근거 포함: {bad[:5]}")
except Exception as e:
    fail.append(f"deepread_v2 as-of 예외: {type(e).__name__}: {e}")


# ── 9. player_identity coverage: mapped 또는 unmapped 명시 ──
try:
    import _deepread_v2 as DR2I
    if os.path.exists(csv):
        ident = DR2I.update_identity_from_slate(DATE)
        df9 = pd.read_csv(csv)
        miss_ident = []
        for _, row in df9.iterrows():
            for sidecol in ("home_sp_name", "away_sp_name", "home_lineup_names", "away_lineup_names",
                            "home_bullpen_names", "away_bullpen_names"):
                vals = NP._names(row.get(sidecol)) if sidecol.endswith("_names") else [row.get(sidecol)]
                for nm in vals:
                    if pd.notna(nm) and str(nm).strip():
                        nk = DR2I.norm_name(nm)
                        if nk not in ident.get("aliases", {}) and nk not in ident.get("unmapped", {}):
                            miss_ident.append(str(nm))
        if miss_ident:
            fail.append(f"player_identity 누락(명시적 unmapped도 없음): {sorted(set(miss_ident))[:8]}")
    else:
        print(f"[skip] 9: 슬레이트 파일 없음 ({DATE})")
except Exception as e:
    fail.append(f"player_identity coverage 예외: {type(e).__name__}: {e}")


# ── 10. 환경 블록 coverage (저장본이 있을 때만) ──
ctxf = os.environ.get("NARR_CONTEXT_INPUT_PATH") or os.path.join(
    ROOT, f"_narr_context_{DATE}.txt"
)
if os.path.exists(csv) and os.path.exists(ctxf):
    df10 = pd.read_csv(csv)
    ctxt = open(ctxf, encoding="utf-8").read()
    env_blocks = len(re.findall(r"(?m)^\s*\[환경\]", ctxt))
    if env_blocks < len(df10):
        fail.append(f"환경 블록 {env_blocks} < 경기 {len(df10)} (날씨/심판 missing_explicit 필요)")
    env_required = [c for c in ("temperature", "weather_condition", "wind_speed", "wind_direction", "hp_ump_name") if c in df10.columns]
    env_missing = []
    for _, row in df10.iterrows():
        miss = []
        for c in env_required:
            v = row.get(c)
            if pd.isna(v) or not str(v).strip():
                miss.append(c)
        if miss:
            env_missing.append(f"{row.get('away_team')} @ {row.get('home_team')}: {','.join(miss)}")
    if env_missing and env_blocks < len(df10):
        fail.append(f"환경 사전값 누락(온도/상태/바람/풍향/주심): {env_missing[:8]}")
    elif env_missing:
        print(f"[info] 10: CSV 환경 컬럼은 비었지만 narr_context 환경 블록 {env_blocks}/{len(df10)} 존재")
else:
    print(f"[skip] 10: narr_context 저장본 없음 또는 슬레이트 없음 ({DATE})")


# ── 11. reasoning 블록 수 coverage (저장본이 있을 때만) ──
rtxt = NR.load_reasoning(DATE)
if os.path.exists(csv) and rtxt:
    df11 = pd.read_csv(csv)
    n_blocks = len(re.findall(r"(?m)^### .+? @ .+? · RUN_TOTAL", rtxt))
    if n_blocks != len(df11):
        fail.append(f"reasoning 블록 수 {n_blocks} != 경기 {len(df11)}")
else:
    print(f"[skip] 11: reasoning 저장본 없음 또는 슬레이트 없음 ({DATE})")


# ── 12. deepread_v2 QA invariants from sample review ──
try:
    import _deepread_v2 as DR2Q
    evs = DR2Q.load_events()
    role_suffix = [ev.get("player_id") for ev in evs
                   if re.search(r":(SP|RP|BAT)$", str(ev.get("player_id", "")))]
    if role_suffix:
        fail.append(f"deepread_v2 player_id 역할 suffix 잔존: {sorted(set(role_suffix))[:8]}")
    pp_pre = [ev.get("event_id") for ev in evs
              if ev.get("mechanism") == "position_player_pitching_noise"
              and ev.get("timing") == "pre_knowable"]
    if pp_pre:
        fail.append(f"야수/포수 가비지 등판이 pre_knowable에 포함: {pp_pre[:8]}")
except Exception as e:
    fail.append(f"deepread_v2 QA 예외: {type(e).__name__}: {e}")


# -- 13. deepread_v2 corpus QA for all-season ingest outputs --
try:
    import _deepread_v2 as DR2ALL
    evs_all = DR2ALL.load_events()
    if evs_all:
        event_ids = [ev.get("event_id") for ev in evs_all]
        dup_ids = [eid for eid, n in __import__("collections").Counter(event_ids).items() if n > 1]
        if dup_ids:
            fail.append(f"deepread_v2 duplicate event_id: {dup_ids[:8]}")

        required = ("gamePk", "date", "player_id", "role", "team", "read_text", "mechanism", "timing", "forward_use")
        missing_required = [
            ev.get("event_id") for ev in evs_all
            if any(k not in ev for k in required)
        ]
        if missing_required:
            fail.append(f"deepread_v2 required field missing: {missing_required[:8]}")

        bad_timing = [ev.get("event_id") for ev in evs_all if ev.get("timing") not in DR2ALL.TIMINGS]
        bad_forward = [ev.get("event_id") for ev in evs_all if ev.get("forward_use") not in DR2ALL.FORWARD_USES]
        if bad_timing:
            fail.append(f"deepread_v2 invalid timing: {bad_timing[:8]}")
        if bad_forward:
            fail.append(f"deepread_v2 invalid forward_use: {bad_forward[:8]}")

        role_suffix_all = [
            ev.get("player_id") for ev in evs_all
            if re.search(r":(SP|RP|BAT)$", str(ev.get("player_id", "")))
        ]
        if role_suffix_all:
            fail.append(f"deepread_v2 corpus role suffix player_id: {sorted(set(role_suffix_all))[:8]}")

        pp_pre_all = [
            ev.get("event_id") for ev in evs_all
            if ev.get("mechanism") == "position_player_pitching_noise" and ev.get("timing") == "pre_knowable"
        ]
        if pp_pre_all:
            fail.append(f"deepread_v2 corpus position-player pitching pre_knowable: {pp_pre_all[:8]}")

        game_ids = {str(ev.get("gamePk")) for ev in evs_all if ev.get("role") != "ENV" and ev.get("gamePk") not in (None, "")}
        env_ids = {str(ev.get("gamePk")) for ev in evs_all if ev.get("role") == "ENV"}
        env_missing = sorted(game_ids - env_ids)
        if env_missing:
            fail.append(f"deepread_v2 ENV missing for games: {env_missing[:8]}")

        asof_dir = os.path.join(ROOT, "_deepread_v2", "asof")
        if os.path.isdir(asof_dir):
            for fn in os.listdir(asof_dir):
                if not fn.endswith(".json"):
                    continue
                target = fn[:-5]
                packet = json.load(open(os.path.join(asof_dir, fn), encoding="utf-8"))
                bad_asof = [
                    ev.get("event_id") for ev in packet.get("events", [])
                    if ev.get("timing") != "pre_knowable" or str(ev.get("date", "")) >= target
                ]
                if bad_asof:
                    fail.append(f"deepread_v2 asof corpus leak {fn}: {bad_asof[:5]}")
                    break

        rpt_path = os.path.join(ROOT, "_deepread_v2", "ingest_all_report.json")
        if os.path.exists(rpt_path):
            rpt = json.load(open(rpt_path, encoding="utf-8"))
            if not rpt.get("dry_run") and int(rpt.get("games_ingested", 0)) != len(env_ids):
                fail.append(f"deepread_v2 all report games_ingested != ENV events: {rpt.get('games_ingested')} vs {len(env_ids)}")
            if rpt.get("warnings"):
                fail.append(f"deepread_v2 all report warnings: {rpt.get('warnings')[:8]}")
    else:
        print("[skip] 13: deepread_v2 events.jsonl empty")
except Exception as e:
    fail.append(f"deepread_v2 corpus QA exception: {type(e).__name__}: {e}")


# -- 14. deepread_v3 raw-PBP ledger QA when outputs exist --
try:
    import _deepread_v3 as DR3
    dr3_path = os.path.join(ROOT, "_deepread_v3", "review_factors.jsonl")
    if os.path.exists(dr3_path):
        probs3 = DR3.validate(DATE)
        if probs3:
            fail.append(f"deepread_v3 validate 실패: {probs3[:8]}")
        pkt3 = DR3.build_asof(DATE, write=False)
        bad3 = [
            ev.get("event_id") for ev in pkt3.get("events", [])
            if ev.get("timing") != "pre_knowable"
            or ev.get("status") != "ref_backed"
            or not ev.get("refs")
            or str(ev.get("date", "")) >= DATE
        ]
        if bad3:
            fail.append(f"deepread_v3 asof leak/ref 문제: {bad3[:8]}")
    else:
        print("[skip] 14: deepread_v3 review_factors 없음")
except Exception as e:
    fail.append(f"deepread_v3 QA 예외: {type(e).__name__}: {e}")


# -- 15. season postmortem support/caution marks reach narrator input --
try:
    sig = NR.build_season_rule_signals(DATE)
    if os.path.exists(csv):
        if not sig.strip():
            fail.append("season support/caution signals empty despite slate csv")
        elif "지원:" not in sig or "경고:" not in sig:
            fail.append("season support/caution signals missing 지원/경고 lanes")
        elif DATE == "2026-06-08":
            required_pairs = {
                "Milwaukee Brewers @ Athletics": "RUN_TOTAL-HIGHU",
                "Milwaukee Brewers @ Athletics env": "RUN_TOTAL-HIGHU-ENV",
                "Washington Nationals @ San Francisco Giants": "RUN_TOTAL-WIND",
                "Seattle Mariners @ Baltimore Orioles": "WIN_LOSS-CC",
                "Houston Astros @ Los Angeles Angels": "WIN-LOSS-ELITE-ALIGN",
                "Boston Red Sox @ Tampa Bay Rays": "WIN_LOSS-RED-CLOSE-LOSSSTACK",
                "Cincinnati Reds @ San Diego Padres": "WIN-RUN_TOTAL-LOWOVER",
            }
            miss_sig = [
                f"{label}:{mark}" for label, mark in required_pairs.items()
                if label.split(" env")[0] not in sig or mark not in sig
            ]
            if miss_sig:
                fail.append(f"season support/caution 2026-06-08 expected marks missing: {miss_sig}")
except Exception as e:
    fail.append(f"season support/caution QA 예외: {type(e).__name__}: {e}")


# -- 16. deepread_v3 lens question-ledger QA when outputs exist --
try:
    import _deepread_v3_lens as DR3L
    lens_path = os.path.join(ROOT, "_deepread_v3", "lens_factors.jsonl")
    if os.path.exists(lens_path):
        probs_lens = DR3L.validate(DATE)
        if probs_lens:
            fail.append(f"deepread_v3_lens validate 실패: {probs_lens[:8]}")
        pkt_lens = DR3L.build_asof(DATE, write=False)
        bad_lens = [
            ev.get("event_id") for ev in pkt_lens.get("events", [])
            if ev.get("timing") != "pre_knowable"
            or ev.get("status") != "ref_backed"
            or not ev.get("refs")
            or str(ev.get("date", "")) >= DATE
        ]
        if bad_lens:
            fail.append(f"deepread_v3_lens asof leak/ref 문제: {bad_lens[:8]}")
        if DATE == "2026-06-08":
            rows = DR3L.load_lens_rows()
            def has(gpk, lens, **kw):
                for r in rows:
                    if str(r.get("gamePk")) != str(gpk) or r.get("lens_id") != lens:
                        continue
                    ok = True
                    for k, v in kw.items():
                        if k == "forward_uses":
                            ok = ok and all(x in r.get("forward_uses", []) for x in v)
                        else:
                            ok = ok and r.get(k) == v
                    if ok:
                        return True
                return False
            if not has("824998", "ENV_PARK_WEATHER", direction="caution", strength="strong"):
                fail.append("deepread_v3_lens 6/8 MIL@ATH ENV_PARK_WEATHER strong caution 누락")
            if not has("823289", "BULLPEN_AVAILABILITY", forward_uses=["WIN_LOSS", "RUN_TOTAL"]):
                fail.append("deepread_v3_lens 6/8 CIN@SD BULLPEN_AVAILABILITY WIN_LOSS/RUN_TOTAL 누락")
            if not any(r.get("lens_id") == "MARKET_MODEL_ALIGNMENT" and r.get("direction") == "caution" for r in rows):
                fail.append("deepread_v3_lens close-call MARKET_MODEL_ALIGNMENT caution 누락")
            old_flag = os.environ.get("NARR_INCLUDE_DEEPREAD_V3_LENS")
            try:
                os.environ.pop("NARR_INCLUDE_DEEPREAD_V3_LENS", None)
                if DR3L.format_asof_for_slate(DATE, df=pd.read_csv(csv)).strip():
                    fail.append("deepread_v3_lens flag off인데 priors block 노출")
                os.environ["NARR_INCLUDE_DEEPREAD_V3_LENS"] = "1"
                if "lens3:" not in DR3L.format_asof_for_slate(DATE, df=pd.read_csv(csv)):
                    fail.append("deepread_v3_lens flag on인데 lens3 block 미노출")
            finally:
                if old_flag is None:
                    os.environ.pop("NARR_INCLUDE_DEEPREAD_V3_LENS", None)
                else:
                    os.environ["NARR_INCLUDE_DEEPREAD_V3_LENS"] = old_flag
    else:
        print("[skip] 16: deepread_v3_lens lens_factors 없음")
except Exception as e:
    fail.append(f"deepread_v3_lens QA 예외: {type(e).__name__}: {e}")


if fail:
    print("FAIL:")
    for f in fail:
        print("  -", f)
    sys.exit(1)
print(f"PASS — 네레이터 입력 점검({DATE}): parse_predictions/evidence·priors커버리지·leak-free·IL·freeze "
      f"+ RUN_TOTAL[기준점·타선궤적] + deepread_v2[asof·identity·환경·reasoning] "
      f"+ deepread_v3[raw-PBP refs·asof] + season support/caution "
      f"+ deepread_v3_lens[질문ledger·asof] 커버리지")
