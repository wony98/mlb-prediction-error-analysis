# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
narr_context.py — 네레이터 추론 컨텍스트 생성기 (mark-only, 사실 고정용)

목적: 추론을 '감(感)'이 아니라 *확인된 사실*에 고정시킨다. 주어진 날짜의 v3 게임 슬레이트를
읽어, 각 경기마다 추론에 필요한 사실 블록을 뽑아준다:
  - 양 선발의 궤적 ledger 상태 + 직전 등판 평균 이닝(조기강판 vs 이닝이터)
  - 오늘 라인업 중 ledger에서 *비중립*(핫/회복/식음/콜드)인 타자만
  - 오늘 가용 불펜 중 ledger에서 *비중립*(하강방화/한방형/제구형/믿을만함)인 팔만
  - RUN_TOTAL 기준점 + 모델 예측(RUN_TOTAL/WIN_LOSS)

핵심 원칙: 대부분 선수는 중립이고, 그건 정상이다. 비중립만 노출해서 "스토리 만들기" 유혹을 줄인다.
ledger의 last_date로 최근성을 표시(오래된 라벨은 단판 영향 약함 — 사후정당화 방지).

사용: py scripts/narr_context.py 2026-05-29
출력: stdout 텍스트 (스킬이 이걸 읽고 _narr_rules.md에 비춰 추론)
"""
import sys, os, json, argparse
import pandas as pd
import numpy as np

# 내보낸 src 디렉터리를 모듈 및 원장 경로의 기준으로 사용한다.
ROOT = os.path.dirname(os.path.abspath(__file__))
PYDEPS = os.path.join(ROOT, "_pydeps")
if os.path.isdir(PYDEPS) and PYDEPS not in sys.path:
    sys.path.insert(0, PYDEPS)

# 정규화 함수: _narr_filter_lib._norm 재사용, 실패 시 로컬 폴백
def _make_norm():
    try:
        sys.path.insert(0, ROOT)
        import _narr_filter_lib as L
        if hasattr(L, "_norm"):
            return L._norm
    except Exception:
        pass
    import unicodedata, re
    def _norm(s):
        if not s:
            return ""
        s = unicodedata.normalize("NFKD", str(s))
        s = "".join(c for c in s if not unicodedata.combining(c))
        s = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", s, flags=re.I)
        s = re.sub(r"[.\s]+", " ", s).strip().lower()
        return s
    return _norm

NORM = _make_norm()


def load_ledger(date=None):
    # date 주어지면 그 날 분석용 leak-free 스냅샷(_narr_trajectory_ledger_{date}.json, date 미만 데이터)을 우선.
    # 없으면 현재 롤링(_narr_trajectory_ledger.json) 폴백 — 단 롤링은 hindsight 가능(라이브 당일만 안전).
    snap = os.path.join(ROOT, f"_narr_trajectory_ledger_{date}.json") if date else None
    p = snap if (snap and os.path.exists(snap)) else os.path.join(ROOT, "_narr_trajectory_ledger.json")
    if not os.path.exists(p):
        return None
    j = json.load(open(p, encoding="utf-8"))
    # 정규화 키 인덱스
    idx = {}
    for grp in ("batters", "relievers", "starters"):
        d = j.get(grp) or {}
        idx[grp] = {NORM(k): (k, v) for k, v in d.items()}
    return {"as_of": j.get("as_of"), "idx": idx}


def lookup(led, grp, name):
    if not led or not name:
        return None
    hit = led["idx"].get(grp, {}).get(NORM(name))
    return hit[1] if hit else None


def days_between(as_of, last_date):
    try:
        a = pd.to_datetime(as_of); b = pd.to_datetime(last_date)
        return int((a - b).days)
    except Exception:
        return None


def sp_recent_ip(target_date):
    """as-of 직전 등판들(현재 경기/날짜 제외) 평균 IP를 sp_id별로 반환. min 3 prior, last 5."""
    p = os.path.join(ROOT, "_box2026_game.parquet")
    if not os.path.exists(p):
        return {}
    try:
        bg = pd.read_parquet(p).copy()
    except Exception:
        return {}
    bg["date"] = pd.to_datetime(bg["date"])
    tgt = pd.to_datetime(target_date)
    bg = bg[bg["date"] < tgt].sort_values("date")
    rows = []
    for _, r in bg.iterrows():
        for side in ("home", "away"):
            sid = r.get(f"{side}_sp_id"); ip = r.get(f"{side}_sp_ip")
            if pd.notna(sid) and pd.notna(ip):
                rows.append({"sp_id": int(sid), "ip": float(ip)})
    if not rows:
        return {}
    lg = pd.DataFrame(rows)
    out = {}
    for sid, sub in lg.groupby("sp_id"):
        last5 = sub["ip"].tail(5)
        if len(last5) >= 3:
            out[sid] = round(float(last5.mean()), 2)
    return out


def fmt_player(name, ent, as_of, kind):
    """비중립이면 한 줄 문자열, 중립/미등재면 None."""
    if not ent:
        return None
    label = ent.get("label", "중립"); emoji = ent.get("emoji", "⚪")
    if label == "중립":
        return None
    age = days_between(as_of, ent.get("last_date"))
    stale = ""
    if age is not None and age > 12:
        stale = f" ⏳(마지막 {ent.get('last_date')}, {age}일전 — 최근성 약함)"
    extra = ""
    if kind == "bat":
        extra = f" r7결과 {ent.get('r7_woba')}/하회 {ent.get('r7_xwoba')} gap {ent.get('gap')}"
    elif kind == "RP":
        extra = f" blown {ent.get('blown')}/late_holds {ent.get('late_holds')}/최근14d방화 {ent.get('recent_blown_14d')}"
    elif kind == "SP":
        extra = f" whiff {ent.get('whiff')}/피woba {ent.get('woba_allowed')}/velo {ent.get('velo')}(Δ{ent.get('velo_drop')})"
    return f"{emoji}{label} {name}{extra}{stale}"


def split_names(value):
    from _mlb_name_list import parse_name_list

    return parse_name_list(value)


_SUFFIX = {"jr", "sr", "ii", "iii", "iv"}
def short_name(nm):
    """성(姓) 표시 — Jr./II 등 접미사 건너뛰고 마지막 실명 토큰."""
    toks = [t for t in str(nm).split() if t.lower().strip(".") not in _SUFFIX]
    return toks[-1] if toks else str(nm)


# ── 구조화 실시간 레이어 (statsapi 기반, 모델이 못 보는 직교 정보) ──
def load_stints():
    p = os.path.join(ROOT, "_narr_bullpen_stints.parquet")
    if not os.path.exists(p):
        return None
    try:
        s = pd.read_parquet(p)
    except Exception:
        return None
    s["date"] = pd.to_datetime(s["date"])
    return s


def bp_avail_map(stints, date):
    """{정규화 투수명: set(타깃일 전 일수)} 최근5일 + stints 최신일(as-of)."""
    if stints is None or stints.empty:
        return {}, None
    t = pd.to_datetime(date)
    prior = stints[stints["date"] < t].copy()
    s = prior[prior["date"] >= t - pd.Timedelta(days=5)].copy()
    m = {}
    if not s.empty:
        s["pn"] = s["pitcher"].map(NORM)
        s["dd"] = (t - s["date"]).dt.days
        for pn, sub in s.groupby("pn"):
            m[pn] = set(int(x) for x in sub["dd"])
    return m, (prior["date"].max() if not prior.empty else None)


def bp_unavailable(bullpen_names, avail):
    """강신호만: 3연투(어제·그제·3일전) 또는 어제+그제 연투 → 오늘 미가용/제한 추정.
    (최근3일 2등판 같은 정상 사용은 제외 — 과다 플래그 방지)"""
    out = []
    for n in bullpen_names:
        days = avail.get(NORM(n))
        if not days:
            continue
        if 1 in days and 2 in days and 3 in days:
            out.append(f"{n.split()[-1]}(3연투 미가용)")
        elif 1 in days and 2 in days:
            out.append(f"{n.split()[-1]}(어제+그제 연투)")
    return out


def team_hot_batters(led, team, as_of, max_age=10):
    """ledger에서 team의 🔥누적핫/📈회복 *최근 활동* 타자 [(name, emoji)]. 결장 판단용."""
    res = []
    if not led:
        return res
    for nn, (orig, v) in led["idx"].get("batters", {}).items():
        if v.get("team") != team or v.get("emoji") not in ("🔥", "📈"):
            continue
        age = days_between(as_of, v.get("last_date"))
        if age is None or age <= max_age:   # 최근 활동만 (IL/방출 제외)
            res.append((orig, v.get("emoji")))
    return res


def load_form_timeline():
    """_form_label_hitters.json (결정적 폼 라벨러 산출). 없으면 {}."""
    f = os.path.join(ROOT, "_form_label_hitters.json")
    try:
        return json.load(open(f, encoding="utf-8"))
    except Exception:
        return {}


def _nkey(s):
    """이름 매칭키 — 액센트·구두점·공백 제거(J.T.↔J. T. 100% 일치)."""
    import unicodedata, re
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def form_streaks(fl_tl, names, date, fl_idx=None):
    """오늘 라인업의 *직전* 연속 폼 → (핫스트릭, 슬럼프) 리스트. date 미만 = leak-free.
    검증: 직전 2+연속 락인=예측가능 핫스트릭 / 단발 락인=예측불가. 슬럼프=3+연속 부진."""
    if fl_idx is None:
        fl_idx = {_nkey(k): k for k in fl_tl}
    hot, cold = [], []
    for nm in names:
        rec = fl_tl.get(nm)
        if rec is None:   # 정규화 폴백 (이니셜 공백차이 등)
            rec = fl_tl.get(fl_idx.get(_nkey(nm)), [])
        seq = [lab for d, lab, *_ in rec if d < date][-5:]
        if len(seq) < 2:
            continue
        nlock = seq.count("락인"); nslump = seq.count("부진")
        last2, last3 = seq[-2:], seq[-3:]
        if last2 == ["락인", "락인"] or last3.count("락인") >= 2:
            hot.append(f"{short_name(nm)}(직전{len(seq)} 락{nlock})")
        elif last3 == ["부진", "부진", "부진"] or (len(seq) >= 4 and seq[-4:].count("부진") >= 3):
            cold.append(f"{short_name(nm)}(직전{len(seq)} 부{nslump})")
    return hot, cold


def hitter_seq(fl_tl, fl_idx, nm, date, k=5):
    """타자 nm의 date 직전 폼 라벨 시퀀스(과거→최근, 최대 k). leak-free(d<date)."""
    rec = fl_tl.get(nm)
    if rec is None:
        rec = fl_tl.get(fl_idx.get(_nkey(nm)), [])
    return [lab for d, lab, *_ in rec if d < date][-k:]


def seq_labels(pairs, k=5):
    """recent_sp/recent_bp 반환 [(date,label)] → 라벨 시퀀스(과거→최근, 최대 k)."""
    return [lab for _d, lab in pairs][-k:]


def _compass(deg):
    """풍향 도(°) → 8방위. StatsAPI 문자열(Out To RF 등)은 그대로 표시."""
    if isinstance(deg, str) and deg.strip() and deg.strip().lower() != "nan":
        return " " + deg.strip()
    try:
        return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((float(deg) + 22.5) // 45) % 8]
    except Exception:
        return ""


def _il_short(desc):
    """'Injured 60-Day' → '60d' / 'Rehab Assignment' → '재활'."""
    for k in ("60", "15", "10", "7"):
        if f"{k}-Day" in str(desc):
            return k + "d"
    return "재활" if "Rehab" in str(desc) else "IL"


class _Tee:
    """stdout + 파일 동시 출력 (스킬은 stdout, 파이프라인은 _narr_context_{date}.txt)."""
    def __init__(self, *streams):
        self.streams = streams
    def write(self, s):
        for st in self.streams:
            st.write(s)
    def flush(self):
        for st in self.streams:
            st.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", default=None, help="YYYY-MM-DD (생략 시 ET 오늘 — run_mlb_v3와 동일)")
    args = ap.parse_args()
    date = args.date or (pd.Timestamp.utcnow() - pd.Timedelta(hours=4)).strftime("%Y-%m-%d")

    csv = os.environ.get("MLB_C3_V3_PATH") or os.environ.get("NARR_V3_INPUT_PATH") or os.path.join(ROOT, f"_v3_games_{date}.csv")
    if not os.path.exists(csv):
        print(f"[ERR] {csv} 없음 — 먼저 run_mlb_v3.py(또는 _make_slate.py)로 슬레이트 생성 필요")
        sys.exit(1)
    # stdout을 파일에도 tee (파이프라인이 읽을 수 있게). 실패해도 stdout은 유지.
    try:
        context_out = os.environ.get("MLB_C3_NARR_CONTEXT_PATH") or os.environ.get("NARR_CONTEXT_OUTPUT_PATH") or os.path.join(ROOT, f"_narr_context_{date}.txt")
        os.makedirs(os.path.dirname(os.path.abspath(context_out)), exist_ok=True)
        _f = open(context_out, "w", encoding="utf-8")
        sys.stdout = _Tee(sys.__stdout__, _f)
    except Exception:
        pass
    df = pd.read_csv(csv)
    led = load_ledger(date)   # date 분석용 leak-free 스냅샷 우선(없으면 롤링 폴백)
    as_of = led["as_of"] if led else "?"
    fl_tl = load_form_timeline()   # 결정적 폼 라벨러 타임라인 (직전 연속 폼=검증된 1순위 예측신호)
    fl_idx = {_nkey(k): k for k in fl_tl}   # 정규화 매칭 인덱스 (라인업↔타임라인 100% 매핑)
    SPF = BPF = None                # 직전 등판 폼 raw 시퀀스 모듈 (선발 호투/평범/붕괴 · 불펜 안정/불안)
    try:
        sys.path.insert(0, ROOT)
        import _sp_form as SPF
        import _bp_form as BPF
    except Exception:
        pass
    sp_prior = {}                   # 첫 등판 선발 baseline (신인=MiLB·외국=해외참고, _mlb_sp_prior 체인)
    try:
        _pp = pd.read_csv(os.path.join(ROOT, "_mlb_sp_prior.csv"))
        sp_prior = {_nkey(x["name"]): x for _, x in _pp.iterrows()}
    except Exception:
        sp_prior = {}
    rip = sp_recent_ip(date)
    avail, bp_asof = bp_avail_map(load_stints(), date)   # 구조화 실시간: 불펜 가용성
    try:                                                  # 결장: 최근 실제 선발 regular (statsapi, 캐시)
        sys.path.insert(0, ROOT)
        import _mlb_recent_lineups as _rl
        regs = _rl.regulars(date)
        il = _rl.il_map(date)                             # IL/부상 = 로스터 status API 사실 (추측 대체)
        rot_il = _rl.rotation_il(date)                    # 최근 선발 ∩ IL = 로테이션 직격(붕괴) 신호 (leak-free as-of)
    except Exception:
        regs = {}
        il = {}
        rot_il = {}
    inj = {}                                              # 구조화 부상 (MLB transactions API, 날짜별 IL+부위, leak-free)
    try:
        import _mlb_injuries
        inj = _mlb_injuries.current_il(date)
    except Exception:
        inj = {}
    news = {}                                             # 비구조 뉴스 (claude -p 웹검색, _narr_news.py 캐시)
    np_path = os.path.join(ROOT, f"_narr_news_{date}.json")
    if os.path.exists(np_path):
        try:
            news = json.load(open(np_path, encoding="utf-8"))
        except Exception:
            news = {}

    print(f"=== {date} 네레이터 추론 컨텍스트 (직전까지, leak-free) · {len(df)}경기 ===")
    print("[원칙] 각 선수의 *직전 폼 시퀀스*(사실)만 준다. lean·점수·결론 없음 — 네레이터가 양팀 전체 궤적을 통째로 읽고 어느 쪽이 궤적상 우위인지·뭐가 불확실한지 스스로 판단하라. 사전 기준/체크리스트 금지. '평범'뿐인 타자는 생략(선발은 항상 표시).")
    print("[폼 라벨] 타자 락인(강타구2+)/평범/부진(강타구0) · 선발 호투/평범/붕괴 · 불펜 안정/불안. 시퀀스 = 과거→최근 순.")
    print(f"[실시간 사실] [환경]날씨·주심 / IL[n]=MLB transactions(부위·IL일수, leak-free·날짜순) / 불펜미가용=최근 연투(as-of {str(bp_asof)[:10] if bp_asof is not None else '?'}) / 결장(비IL)=라인업서 빠진 휴식·스크래치.\n")

    for _, r in df.iterrows():
        away = r.get("away_team"); home = r.get("home_team")
        run_total_baseline = r.get("run_total_baseline"); run_total_prediction = r.get("run_total_prediction_side", r.get("run_total_prediction"))
        win_loss_prediction = r.get("prediction_team", r.get("win_prediction"))
        print(f"[{away} @ {home}]  RUN_TOTAL {run_total_baseline} | 모델예측(참고): RUN_TOTAL={run_total_prediction} · WIN_LOSS={win_loss_prediction}")
        # 환경(날씨·심판) — 만든 데이터 사용(운/실력·RUN_TOTAL 판단 보조, 사전 인지 가능 사실)
        _wx = []
        _t = r.get("temperature"); _ws = r.get("wind_speed"); _wd = r.get("wind_direction"); _pp = r.get("precip_prob")
        if pd.notna(_t):
            _wx.append(f"{float(_t):.0f}°F")
        _wc = r.get("weather_condition")
        if pd.notna(_wc) and str(_wc).strip():
            _wx.append(str(_wc).strip())
        if pd.notna(_ws):
            _wx.append(f"바람 {float(_ws):.0f}mph{_compass(_wd) if pd.notna(_wd) else ''}")
        if pd.notna(_pp):
            _wx.append(f"강수 {float(_pp):.0f}%")
        _um = r.get("hp_ump_name")
        if pd.notna(_um) and str(_um).strip():
            _us = f"주심 {_um}"; _hwr = r.get("hp_ump_home_win_rate"); _rm = r.get("hp_ump_runs_mean"); _vl = r.get("ump_vs_league")
            _det = []
            if pd.notna(_hwr):
                _det.append(f"홈승률 {float(_hwr)*100:.0f}%")
            if pd.notna(_rm):
                _det.append(f"평균 {float(_rm):.1f}득점" + (f"({float(_vl):+.1f} vs리그)" if pd.notna(_vl) else ""))
            if _det:
                _us += "(" + "·".join(_det) + ")"
            _wx.append(_us)
        if _wx:
            print("  [환경] " + " · ".join(_wx))

        # 선발 — 직전 등판 폼 시퀀스(호투/평범/붕괴) + 직전평균 IP. 결론·lean 없음.
        for side, lab in (("home", home), ("away", away)):
            sp_name = r.get(f"{side}_sp_name"); sp_id = r.get(f"{side}_sp_id")
            seq = seq_labels(SPF.recent_sp(str(sp_name), date, 5)) if (SPF and pd.notna(sp_name)) else []
            ipv = rip.get(int(sp_id)) if pd.notna(sp_id) else None
            parts = []
            if seq:
                parts.append("직전폼 " + "·".join(seq))
            if ipv is not None:
                parts.append(f"직전평균 {ipv}이닝")
            if not seq and ipv is None:   # 첫 등판 → prior baseline (신인=MiLB·외국=해외참고, 사실)
                pr = sp_prior.get(_nkey(sp_name))
                if pr is not None and str(pr.get("p_src")) == "foreign_ref":
                    parts.append("첫등판·해외리그 ERA 참고(MLB이력無)")
                elif pr is not None and pd.notna(pr.get("p_era")):
                    lvl = str(pr.get("p_level")); src = str(pr.get("p_src"))
                    base = f"{lvl} ERA{float(pr['p_era']):.2f}" + (f"·{src[3:]}" if lvl == "MLB" else "(신인MiLB)")
                    parts.append(f"첫등판·prior {base}")
                else:
                    parts.append("직전 등판 기록 없음")
            print(f"  선발({str(lab)[:12]}): {sp_name} · " + " · ".join(parts))

        # 타선 — 직전 폼 시퀀스(락인/부진 포함된 타자만; 전부 평범/무기록은 생략)
        for side, lab in (("home", home), ("away", away)):
            names = split_names(r.get(f"{side}_lineup_names"))
            rows = []
            for n in names:
                seq = hitter_seq(fl_tl, fl_idx, n, date, 5)
                if seq and ("락인" in seq or "부진" in seq):
                    rows.append(f"{short_name(n)} {'·'.join(seq)}")
            if rows:
                print(f"  타선({str(lab)[:12]}): " + " / ".join(rows))

        # 불펜 — 직전 폼 시퀀스(불안 포함된 팔만)
        for side, lab in (("home", home), ("away", away)):
            names = split_names(r.get(f"{side}_bullpen_names"))
            rows = []
            for n in names:
                seq = seq_labels(BPF.recent_bp(n, date, 5)) if BPF else []
                if seq and "불안" in seq:
                    rows.append(f"{short_name(n)} {'·'.join(seq)}")
            if rows:
                print(f"  불펜({str(lab)[:12]}): " + " / ".join(rows))
        # [실시간] 불펜 미가용 + 핵심타자 결장 (구조화, 모델 직교)
        rt = []
        for side, lab in (("home", home), ("away", away)):
            unav = bp_unavailable(split_names(r.get(f"{side}_bullpen_names")), avail)
            if unav:
                rt.append(f"{str(lab)[:3]} 불펜미가용 ⚠️ " + " / ".join(unav))
        # 부상 = MLB transactions API(날짜별 IL+부위, leak-free) — 라인업 게시 무관 항상. MLB만(마이너 노이즈 X).
        for side, lab in (("home", home), ("away", away)):
            recs = inj.get(lab, {})
            if not recs:
                continue
            items = sorted(recs.items(), key=lambda x: x[1].get("date", ""), reverse=True)
            parts = []
            for nm, rc in items:
                body = (rc.get("desc") or "").split(". ")[-1].strip()   # "retroactive to …. 부위" → 부위
                body = (body[:18] + "…") if len(body) > 19 else body
                tag = f"{rc.get('pos', '')}·{rc.get('il', '')}" + (f"·{body}" if body else "")
                parts.append(f"{short_name(nm)}({tag})")
            rt.append(f"{str(lab)[:3]} IL[{len(parts)}]: " + " / ".join(parts))
        # 스크래치(비-IL 휴식/추정) = 라인업 게시된 경우만 (regular인데 빠짐 AND IL 아님)
        for side, lab in (("home", home), ("away", away)):
            lu = {NORM(n) for n in split_names(r.get(f"{side}_lineup_names"))}
            if not lu:
                continue
            confirmed = (r.get(f"{side}_lineup_src") == "확정")
            inj_norm = {NORM(n) for n in inj.get(lab, {})}
            scratch = [f"{short_name(nm)}({'휴식·확정✅' if confirmed else '결장추정⏳'})"
                       for nm in regs.get(lab, {}) if NORM(nm) not in lu and NORM(nm) not in inj_norm]
            if scratch:
                rt.append(f"{str(lab)[:3]} 결장(비IL) " + " / ".join(scratch))
        if rt:
            print("  [실시간] " + " · ".join(rt))
        ginfo = news.get(str(r.get("gamePk")))
        if isinstance(ginfo, dict):                       # 신 스키마: leak-free 인증본만 표시
            ginfo = ginfo.get("news") if ginfo.get("leak_free") else None
        else:
            ginfo = None                                  # 레거시 str = 미인증(사후크롤 가능) → 누수 안전상 안 씀
        if ginfo and "정보 없음" not in str(ginfo)[:25]:
            print("  [뉴스] " + str(ginfo).replace("\n", "\n         "))
        print()


if __name__ == "__main__":
    main()
