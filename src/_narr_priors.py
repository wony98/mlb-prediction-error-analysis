# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""_narr_priors.py — 오늘 슬레이트 선수들의 *전날까지 누적 회고 read*를 뽑아 분석에 붙인다.

매일 정독 회고(_reason_master, 경기별 선수 read)가 쌓은 *선수 개개인의 사전 인지 가능한 패턴*
(이 투수는 정타에 약하다·이 불펜은 제구붕괴 반복·이 타자는 연속 락인)을, 오늘 그 경기 나오는
선수들 것 *전부* 끌어와 적용 → 추론 → 예측 도출. 유형 평균(❌)이 아니라 선수별 누적 지식.

leak-free: target date *미만* read만. 출력 _narr_priors_{date}.txt (narr_context와 짝, 분석이 같이 읽음).

usage: py _narr_priors.py YYYY-MM-DD
"""
import os
import re
import sys
import json
import unicodedata
from collections import defaultdict

import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))


def _nk(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _names(s):
    from _mlb_name_list import parse_name_list

    return parse_name_list(s)


def _prior_key(s):
    s = unicodedata.normalize("NFKC", str(s or ""))
    chars = []
    for ch in s:
        if "\uac00" <= ch <= "\ud7a3":
            chars.append(ch)
            continue
        for d in unicodedata.normalize("NFKD", ch):
            if not unicodedata.combining(d):
                chars.append(d)
    return re.sub(r"[^0-9a-zA-Z가-힣]", "", "".join(chars)).lower()


def _id_key(v):
    s = str(v or "").strip()
    if not s or s.lower() == "nan":
        return ""
    try:
        return str(int(float(s)))
    except Exception:
        return s


def _load_foreign_rookie_prior():
    path = os.path.join(ROOT, "postmortem", "mlb_foreign_rookie_prior_ledger.csv")
    if not os.path.exists(path):
        return {"by_id": {}, "by_name": {}}
    df = pd.read_csv(path)
    by_id, by_name = defaultdict(list), defaultdict(list)
    for _, row in df.iterrows():
        r = {k: row.get(k) for k in df.columns}
        pid = _id_key(r.get("player_id"))
        if pid:
            by_id[pid].append(r)
        for name_col in ("player_name", "source_name"):
            nk = _prior_key(r.get(name_col))
            if nk:
                by_name[nk].append(r)
    return {"by_id": dict(by_id), "by_name": dict(by_name)}


def _role_ok(r, role):
    rr = str(r.get("role") or "").upper()
    sr = str(r.get("source_role") or "").upper()
    if role == "P":
        return sr == "P" or rr in {"SP", "RP", "P"}
    if role == "BAT":
        return sr == "BAT" or rr == "BAT"
    return True


def _volume(r):
    for k in ("pa_or_bf", "ip"):
        try:
            v = float(str(r.get(k) or "").replace(",", ""))
            return v
        except Exception:
            pass
    return 0.0


def _interesting_prior_source(r):
    src = str(r.get("resolved_source") or "")
    return src in {
        "NPB", "KBO",
        "MILB_AAA_HITTING", "MILB_AAA_PITCHING", "MILB_STATCAST",
        "STATSAPI_MINOR_HITTING", "STATSAPI_MINOR_PITCHING",
        "MLB_LOW_SAMPLE", "RAW_PBP_LOW_SAMPLE", "POSITION_PLAYER_PITCHING_NOISE",
    }


def _find_prior(index, player_id="", name="", role=""):
    rows = []
    pid = _id_key(player_id)
    if pid:
        rows.extend(index["by_id"].get(pid, []))
    nk = _prior_key(name)
    if nk:
        rows.extend(index["by_name"].get(nk, []))
    seen, out = set(), []
    for r in rows:
        key = (str(r.get("player_id")), str(r.get("resolved_source")), str(r.get("source_role")), str(r.get("source_season")))
        if key in seen or not _role_ok(r, role) or not _interesting_prior_source(r):
            continue
        seen.add(key)
        out.append(r)
    return sorted(out, key=_volume, reverse=True)


def _fmt_num(v, digits=3):
    try:
        if str(v).strip() == "" or str(v).lower() == "nan":
            return ""
        return f"{float(v):.{digits}f}".rstrip("0").rstrip(".")
    except Exception:
        return ""


def _format_prior_line(slot, r):
    src = str(r.get("resolved_source") or "")
    league = str(r.get("source_league") or "")
    season = str(r.get("source_season") or "")
    src_name = str(r.get("source_name") or r.get("player_name") or "")
    team = str(r.get("team_source") or "")
    role = "BAT" if str(slot).startswith("타자") else str(r.get("source_role") or r.get("role") or "")
    vol = _fmt_num(r.get("pa_or_bf"), 0)
    if src == "POSITION_PLAYER_PITCHING_NOISE":
        return f"    {slot}: {src_name} -> 야수 가비지 등판 기록만 있음; true bullpen prior로 사용 금지 ({src})"
    if role == "BAT":
        slash = "/".join(x for x in (_fmt_num(r.get("avg")), _fmt_num(r.get("obp")), _fmt_num(r.get("slg"))) if x)
        ops = _fmt_num(r.get("ops"))
        xwoba = _fmt_num(r.get("xwoba"))
        parts = [f"{league} {season}".strip(), team, f"PA={vol}" if vol else "", f"slash={slash}" if slash else "", f"OPS={ops}" if ops else "", f"xwOBA={xwoba}" if xwoba else ""]
    else:
        ip = _fmt_num(r.get("ip"), 1)
        parts = [f"{league} {season}".strip(), team, f"BF={vol}" if vol else "", f"IP={ip}" if ip else ""]
        for k, label in (("era", "ERA"), ("k9", "K9"), ("bb9", "BB9"), ("hr9", "HR9"), ("xwoba", "xwOBA")):
            val = _fmt_num(r.get(k))
            if val:
                parts.append(f"{label}={val}")
    body = " · ".join(x for x in parts if x and x.lower() != "nan")
    return f"    {slot}: {src_name} -> {src} | {body}"


def _emit_foreign_rookie_prior_block(emit, date, df):
    index = _load_foreign_rookie_prior()
    emitted = []
    for _, r in df.iterrows():
        away, home = r.get("away_team"), r.get("home_team")
        game_lines, seen = [], set()
        for side, lab in (("away", away), ("home", home)):
            sp_name = r.get(f"{side}_sp_name")
            for pr in _find_prior(index, r.get(f"{side}_sp_id"), sp_name, "P")[:1]:
                key = ("SP", str(pr.get("player_id")), str(pr.get("resolved_source")))
                if key not in seen:
                    seen.add(key)
                    game_lines.append(_format_prior_line(f"선발 {str(lab)[:12]} {sp_name}", pr))
            for nm in _names(r.get(f"{side}_lineup_names")):
                for pr in _find_prior(index, "", nm, "BAT")[:1]:
                    key = ("BAT", str(pr.get("player_id")), str(pr.get("resolved_source")))
                    if key not in seen:
                        seen.add(key)
                        game_lines.append(_format_prior_line(f"타자 {str(lab)[:12]} {nm}", pr))
            for nm in _names(r.get(f"{side}_bullpen_names")):
                for pr in _find_prior(index, "", nm, "P")[:1]:
                    key = ("BP", str(pr.get("player_id")), str(pr.get("resolved_source")))
                    if key not in seen:
                        seen.add(key)
                        game_lines.append(_format_prior_line(f"불펜 {str(lab)[:12]} {nm}", pr))
        if game_lines:
            emitted.append(f"[{away} @ {home}]")
            emitted.extend(game_lines[:14])
    emit("")
    emit("[foreign_rookie_prior] no_known_prior=0; NPB/KBO/AAA/하위마이너/low-sample source context. 점수/하드 플립 금지.")
    if not emitted:
        emit("  이번 slate에서 별도 foreign/rookie/lower-minor prior 노출 대상 없음.")
    else:
        for ln in emitted:
            emit(ln)


def _emit_win_loss_matchup_warning_block(emit, date, df):
    path = os.path.join(ROOT, "postmortem", "mlb_win_loss_matchup_v2.csv")
    if not os.path.exists(path):
        return
    try:
        m = pd.read_csv(path)
    except Exception:
        return
    if "date" not in m.columns:
        return
    day = m[m["date"].astype(str) == str(date)].copy()
    if day.empty:
        return

    def yes(row, col):
        return str(row.get(col, "")).strip().lower() in {"true", "1", "yes", "y"}

    warn_rows = []
    for _, row in day.iterrows():
        flags = []
        if yes(row, "win_loss_remove_watch_v3_bplu"):
            flags.append(str(row.get("win_loss_remove_watch_v3_bplu_rule_id") or "WIN_LOSS_BPLU_V3"))
        if yes(row, "win_loss_remove_watch_v2_strict"):
            flags.append("WIN_LOSS_MATCHUP_V2_STRICT")
        elif yes(row, "win_loss_remove_watch_v2"):
            flags.append("WIN_LOSS_MATCHUP_V2_WATCH")
        if flags:
            warn_rows.append((row, flags))
    if not warn_rows:
        return

    emit("")
    emit("[win_loss_matchup_warning] research/manual-veto lane only; pregame fields only, no result leakage, no hard flip.")
    for row, flags in warn_rows:
        away = str(row.get("away") or "")
        home = str(row.get("home") or "")
        prediction = str(row.get("prediction_team") or "")
        p = _fmt_num(row.get("prediction_p"))
        direction = f"{row.get('direction_bucket', '')} {row.get('direction_edge', '')}".strip()
        sp = f"{row.get('prediction_sp_name', '')} vs {row.get('opp_sp_name', '')}".strip()
        sp_bp_lu = f"sp/bp/lineup={row.get('sp_disadv', '')}/{row.get('bp_disadv', '')}/{row.get('lineup_ops_disadv', '')}"
        impact = row.get("impact_disadv", "")
        emit(f"  [{away} @ {home}] prediction={prediction} p={p} flags={','.join(flags)} dir={direction}")
        emit(f"    SP={sp}; {sp_bp_lu}; impact_disadv={impact}")


def build_index():
    """_reason_master → {투수nk: [(date,read)]}, {타자nk: [(date,state,note)]} (날짜순)."""
    M = json.load(open(os.path.join(ROOT, "_reason_master.json"), encoding="utf-8"))
    P, H = defaultdict(list), defaultdict(list)
    for g in sorted(M, key=lambda x: x["date"]):
        d = g["date"]
        for p in g.get("pitcher_reads", []):
            P[_nk(p["name"].split(" (")[0])].append((d, p.get("read", "")))
        for h in g.get("hitter_reads", []):
            H[_nk(h["name"].split(" (")[0])].append((d, h.get("state", ""), h.get("note", "")))
    return P, H


def _recent(idx, name, date, k):
    seq = [x for x in idx.get(_nk(name), []) if x[0] < date]
    return seq[-k:]


_SUPPORT_LABELS = {
    "선발지배",
    "타자락인",
    "불펜안정",
    "마무리안정",
    "득점권응집",
    "양선발호투저득점",
    "타자반등",
}
_RISK_LABELS = {
    "타자콜드",
    "선발진동형",
    "선발붕괴",
    "선발제구난조",
    "선발초반불안",
    "선발장타허용",
    "불펜불안정",
    "불펜붕괴",
    "하위타선변수",
    "수비실책반복",
    "득점권침묵",
}
_MIXED_LABELS = {"기타관찰", "주심존변수", "날씨변수"}
_SUPPORT_TEXT_RE = re.compile(
    r"호투|지배|봉쇄|억제|락인|강세|안정|셧다운|무실점|정타 지속|생산 지속|반등|위기 진화|득점권 응집"
)
_RISK_TEXT_RE = re.compile(
    r"붕괴|난조|불안|취약|침묵|콜드|무기력|피홈런|볼넷|장타 허용|실점 위험|득점권.*불발|부진|흔들|실책|혼란"
)


def _active_lesson_lane(lesson):
    lane = str(lesson.get("forward_use") or "")
    if lane not in {"context", "context_weak"}:
        return ""
    bucket = str(lesson.get("anti_hindsight_bucket") or "")
    if bucket in {"hindsight_suspect", "post_only"}:
        return ""
    return lane


def _lesson_polarity(lesson):
    """Return support/risk/mixed for display only; never a prediction rule."""
    label = str(lesson.get("label") or "").strip()
    text = f"{lesson.get('factor') or ''} {lesson.get('evidence') or ''}"
    support = label in _SUPPORT_LABELS or bool(_SUPPORT_TEXT_RE.search(text))
    risk = label in _RISK_LABELS or bool(_RISK_TEXT_RE.search(text))
    if support and risk:
        return "mixed"
    if support:
        return "support"
    if risk:
        return "risk"
    if label in _MIXED_LABELS:
        return "mixed"
    return "mixed"


def _team_side(team, away, home):
    team_s = str(team or "")
    if team_s and team_s == str(home or ""):
        return "HOME"
    if team_s and team_s == str(away or ""):
        return "AWAY"
    return str(team_s or "TEAM")[:14]


def _opp_team(team, away, home):
    team_s = str(team or "")
    if team_s == str(home or ""):
        return str(away or "")
    if team_s == str(away or ""):
        return str(home or "")
    return ""


def _team_aliases(team):
    raw = str(team or "").strip()
    if not raw:
        return set()
    parts = [p for p in re.split(r"\s+", raw) if p]
    aliases = {raw.lower()}
    if parts:
        aliases.add(parts[-1].lower())
        aliases.add(parts[0].lower())
    if len(parts) >= 2:
        aliases.add(" ".join(parts[-2:]).lower())
        aliases.add(" ".join(parts[:2]).lower())
    return {a for a in aliases if len(a) >= 3}


def _infer_lesson_focus_team(lesson, away, home):
    text = f"{lesson.get('factor') or ''} {lesson.get('evidence') or ''}".lower()
    scores = {}
    for team in (away, home):
        aliases = _team_aliases(team)
        score = sum(1 for a in aliases if re.search(rf"(?<![a-z]){re.escape(a)}(?![a-z])", text))
        scores[str(team or "")] = score
    away_s, home_s = str(away or ""), str(home or "")
    if scores.get(away_s, 0) > scores.get(home_s, 0):
        return away_s
    if scores.get(home_s, 0) > scores.get(away_s, 0):
        return home_s
    return ""


def _lesson_run_total_hint(lesson, role_hint, polarity):
    label = str(lesson.get("label") or "")
    text = f"{lesson.get('factor') or ''} {lesson.get('evidence') or ''}"
    if label == "양선발호투저득점" or "저득점" in text:
        return "LOWER 지지"
    if label in {"주심존변수", "날씨변수"}:
        return "RUN_TOTAL 맥락"
    role = str(role_hint or "")
    if role in {"sp", "bp"}:
        if polarity == "support":
            return "LOWER 맥락"
        if polarity == "risk":
            return "UPPER 맥락"
    if role == "bat":
        if polarity == "support":
            return "UPPER 맥락"
        if polarity == "risk":
            return "LOWER 맥락"
    if label in {"타자락인", "득점권응집", "타자반등"}:
        return "UPPER 맥락"
    if label in {"타자콜드", "득점권침묵", "하위타선변수"}:
        return "LOWER 맥락"
    return "RUN_TOTAL 맥락"


def _lesson_render_tag(lesson, entity_team, away, home, role_hint=""):
    lane = _active_lesson_lane(lesson)
    if not lane:
        return ""
    weak = lane == "context_weak"
    polarity = _lesson_polarity(lesson)
    run_total_hint = _lesson_run_total_hint(lesson, role_hint, polarity)
    if polarity == "mixed":
        return f"[혼재/맥락 · {run_total_hint}]"
    if role_hint == "bp":
        kind = "약불펜맥락" if weak else "불펜맥락"
        return f"[{kind} · {run_total_hint}]"
    if role_hint == "team":
        kind = "약팀맥락" if weak else "팀맥락"
        return f"[{kind} · {run_total_hint}]"

    if polarity == "support":
        kind = "약지지" if weak else "지지"
        opp_kind = "약위험" if weak else "위험"
    else:
        kind = "약위험" if weak else "위험"
        opp_kind = "약지지" if weak else "지지"
    if not entity_team or not _opp_team(entity_team, away, home):
        return f"[{kind} · {run_total_hint}]"

    side = _team_side(entity_team, away, home)
    opp = _team_side(_opp_team(entity_team, away, home), away, home)
    if polarity == "support":
        return f"[{side} WIN_LOSS {kind} / {opp} WIN_LOSS {opp_kind} · {run_total_hint}]"
    return f"[{side} WIN_LOSS {kind} / {opp} WIN_LOSS {opp_kind} · {run_total_hint}]"


# 시즌 폼 시퀀스 단일자 맵(_sp_form 호투/평범/붕괴 → 호/평/붕). %·버킷·임계값 없이 흐름 통째 노출용.
_SPC = {"호투": "호", "평범": "평", "붕괴": "붕"}
_BTC = {"락인": "락", "평범": "평", "부진": "부"}


def sp_season_form(name, date):
    """시즌 전체 선발 폼 *시퀀스 그대로*(과거→최근, date 미만). %로 버킷 X — 개인 흐름 통째 맵핑."""
    try:
        import _sp_form as SPF
        seq = [_SPC.get(l, l) for _d, l in SPF.recent_sp(str(name), date, 99)]
    except Exception:
        return ""
    if len(seq) < 4:
        return ""
    return f"시즌흐름({len(seq)}): {''.join(seq)}"


def bat_season_form(name, date):
    """시즌 전체 타자 폼 *시퀀스 그대로*(과거→최근). %로 버킷 X — 개인 흐름 통째."""
    try:
        import _form_label as FL
        seq = [_BTC.get(lab, lab) for d, lab, *_ in FL.recent(name, date, 999)]
    except Exception:
        return ""
    if len(seq) < 5:
        return ""
    return f"시즌흐름({len(seq)}): {''.join(seq)}"


def main():
    date = sys.argv[1] if len(sys.argv) > 1 else (pd.Timestamp.utcnow() - pd.Timedelta(hours=4)).strftime("%Y-%m-%d")
    csv = os.environ.get("NARR_V3_INPUT_PATH") or os.path.join(
        ROOT, f"_v3_games_{date}.csv"
    )
    if not os.path.exists(csv):
        print(f"[ERR] {csv} 없음 — 슬레이트 먼저 생성")
        sys.exit(1)
    df = pd.read_csv(csv)
    P, H = build_index()
    out_path = os.environ.get("NARR_PRIORS_OUTPUT_PATH") or os.path.join(
        ROOT, f"_narr_priors_{date}.txt"
    )
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    out = open(out_path, "w", encoding="utf-8")

    def emit(s=""):
        print(s); out.write(s + "\n")

    def _emit_delta_lessons_block():
        try:
            import _narr_delta_lessons as _DL
            _delta_block = _DL.format_asof_for_slate(date, df=df)
            if _delta_block:
                emit(_delta_block)
                emit("")
        except Exception as e:
            emit(f"[delta-review lessons WARN] delta-review read-pressure block failed: {type(e).__name__}: {e}")
            emit("")

    emit(f"=== {date} 누적 회고 사전지식 (전날까지 deep-read read, leak-free) · {len(df)}경기 ===")
    emit("[원칙] 각 선수가 *직전 경기들에서 실제로 어땠나*(정독 read)를 그대로 붙인다. 오늘 매치업에 이 패턴들을")
    emit("       전부 적용해 추론하고 예측을 도출하라. 유형 평균/일률 가중 금지 — 이 경기 이 선수에게 무엇이 반복되나.")
    emit("       deepread_v2 블록은 timing=pre_knowable 근거만 붙고, post_only/noise는 pregame 예측 근거에서 제외한다.")
    emit("")
    _emit_delta_lessons_block()
    # 교훈(synth 사전/사후 분리의 predictable) — 선수/팀 라우팅, leak-free(date<target). 통째로 붙임(기준/점수/예측변경 X).
    from collections import defaultdict as _dd
    _pl_idx, _tm_idx = _dd(list), _dd(list)
    _pk = lambda x: str(x or "")
    try:
        import _narr_synth_lessons as _SL
        _pk = _SL._pkey   # 정규화 키(firstname.surname[.suffix]) — ledger linked_keys ↔ priors 선수명 일치
        for _L in _SL.load_lessons(date):
            for _k in _L.get("linked_keys", []):
                _pl_idx[_k].append(_L)
            for _t in _L.get("linked_teams", []):
                _tm_idx[_t].append(_L)
    except Exception:
        pass

    def _emit_run_total_synth_lessons_block():
        """Render existing RUN_TOTAL synth lessons in the priors lane, date<target only.

        This is visibility context only. It does not alter WIN_LOSS/RUN_TOTAL predictions, scoring,
        Notification output, or any pre-send gate.
        """
        try:
            import _narr_run_total_synth_lessons as _RUN_TOTAL
            lessons = _RUN_TOTAL.load_lessons(date)
            takeaways = _RUN_TOTAL.load_takeaways(date)
        except Exception as e:
            emit(f"[run_total_synth_lessons WARN] unavailable: {type(e).__name__}: {e}")
            return
        if not lessons and not takeaways:
            return
        try:
            cap = int(os.environ.get("NARR_RUN_TOTAL_SYNTH_LESSON_CAP", "5"))
        except Exception:
            cap = 5
        try:
            takeaway_cap = int(os.environ.get("NARR_RUN_TOTAL_SYNTH_TAKEAWAY_CAP", "2"))
        except Exception:
            takeaway_cap = 2
        cap = max(cap, 0)
        takeaway_cap = max(takeaway_cap, 0)

        def _short(v, limit=150):
            text = " ".join(str(v or "").split())
            return text if len(text) <= limit else text[: max(0, limit - 3)].rstrip() + "..."

        def _row_player_keys(row):
            names = []
            for side in ("home", "away"):
                names.append(row.get(f"{side}_sp_name"))
                names.extend(_names(row.get(f"{side}_lineup_names")))
                names.extend(_names(row.get(f"{side}_bullpen_names")))
            return {_pk(x) for x in names if _pk(x)}

        def _match_mode(item, player_keys, team_names):
            linked_keys = {str(x) for x in (item.get("linked_keys") or []) if str(x)}
            linked_teams = {str(x) for x in (item.get("linked_teams") or []) if str(x)}
            if player_keys & linked_keys:
                return "player"
            if team_names & linked_teams:
                return "team"
            return ""

        emitted = False
        for _, row in df.iterrows():
            away, home = str(row.get("away_team") or ""), str(row.get("home_team") or "")
            if not away and not home:
                continue
            player_keys = _row_player_keys(row)
            team_names = {away, home}
            matched_lessons = []
            for x in sorted(lessons, key=lambda r: str(r.get("date") or ""), reverse=True):
                if _match_mode(x, player_keys, team_names) == "player":
                    matched_lessons.append(x)
            matched_takeaways = []
            for x in sorted(takeaways, key=lambda r: str(r.get("date") or ""), reverse=True):
                mode = _match_mode(x, player_keys, team_names)
                has_linked_players = bool(x.get("linked_keys") or x.get("linked_players"))
                if mode == "player" or (mode == "team" and not has_linked_players):
                    matched_takeaways.append((x, mode))
            if cap > 0:
                matched_lessons = matched_lessons[:cap]
            if takeaway_cap > 0:
                matched_takeaways = matched_takeaways[:takeaway_cap]
            if not matched_lessons and not matched_takeaways:
                continue
            if not emitted:
                emit("")
                emit("[RUN_TOTAL synth lessons] date<target, routed, research-only. Show these as RUN_TOTAL visibility context; never auto prediction/flip/remove from this block alone.")
                emitted = True
            emit(f"[{away} @ {home}]")
            for item in matched_lessons:
                direction = str(item.get("direction") or "RUN_TOTAL")
                emit(
                    f"  RUN_TOTAL-lesson {direction} ({item.get('date')}): "
                    f"{_short(item.get('factor'), 130)} -> {_short(item.get('evidence'), 170)}"
                )
            for item, mode in matched_takeaways:
                tag = "team-level" if mode == "team" else "player-linked"
                emit(f"  RUN_TOTAL-takeaway {tag} ({item.get('date')}): {_short(item.get('takeaway'), 220)}")
        if emitted:
            emit("")

    _emit_run_total_synth_lessons_block()

    try:
        _lesson_cap = int(os.environ.get("NARR_SYNTH_LESSON_CAP", "4"))   # 기본 4(선수당 교훈 중앙값 보존, historical-note). 0=전부
    except Exception:
        _lesson_cap = 4
    # 섹션별 cap(historical-note): 불펜은 등판 불확실 → 타이트 cap2 + 비중립 read만, 팀 cap3, 선발/타자 = _lesson_cap(4).
    _all_mode = _lesson_cap <= 0
    _bp_cap = 0 if _all_mode else 2
    _team_cap = 0 if _all_mode else 3
    _BP_RISK = re.compile(r"붕괴|난조|불안|위기|만루|빅이닝|폭발|자초|자멸|취약|흔들|블론|역전|피홈런|볼넷|조기")

    def _bp_risky(recs):
        # 직전 read에 붕괴/불안 흔적 있을 때만 불펜 교훈 부착(stable 호투 교훈=pregame noise, #169). 전부모드면 무조건.
        if _all_mode:
            return True
        return bool(_BP_RISK.search(" ".join(rd for _d, rd in recs)))

    def _lesson_lines(key, idx, shown, cap=None, entity_team="", away_team="", home_team="", role_hint=""):
        # entity당 최근 cap개만(선수 read가 직전 N경기인 것과 동일 — 시즌 전체 폭주 방지, 기준필터 아님).
        # NARR_SYNTH_LESSON_CAP=0 이하면 해당 slate entity에 매칭된 교훈을 전부 출력한다.
        if cap is None:
            cap = _lesson_cap
        out = []
        for _L in sorted(idx.get(key, []), key=lambda x: str(x.get("date", "")), reverse=True):
            if not _active_lesson_lane(_L):
                continue
            _render_team = entity_team
            if role_hint == "team":
                _focus = _infer_lesson_focus_team(_L, away_team, home_team)
                if _focus and _focus != entity_team:
                    continue
                if _focus:
                    _render_team = _focus
                elif {str(away_team or ""), str(home_team or "")}.issubset(set(_L.get("linked_teams") or [])):
                    _render_team = ""
            _id = (_L.get("date"), _L.get("factor"), _render_team or "")
            if _id in shown:
                continue
            shown.add(_id)
            _tag = _lesson_render_tag(_L, _render_team, away_team, home_team, role_hint=role_hint) or "[혼재/맥락]"
            out.append(f"      └{_tag}({_L.get('date')}): {str(_L.get('factor'))[:55]} — {str(_L.get('evidence'))[:80]}")
            if cap > 0 and len(out) >= cap:
                break
        return out

    def _game_balance_lines(row, away_team, home_team, cap_each=3):
        buckets = {"support": [], "risk": [], "mixed": []}
        seen = set()

        def add_lesson(lesson, entity_label, entity_team, role_hint):
            if not _active_lesson_lane(lesson):
                return
            render_team = entity_team
            if role_hint == "team":
                focus = _infer_lesson_focus_team(lesson, away_team, home_team)
                if focus:
                    render_team = focus
                elif {str(away_team or ""), str(home_team or "")}.issubset(set(lesson.get("linked_teams") or [])):
                    render_team = ""
            tag = _lesson_render_tag(lesson, render_team, away_team, home_team, role_hint=role_hint)
            if not tag:
                return
            key = (lesson.get("date"), lesson.get("factor"))
            if key in seen:
                return
            seen.add(key)
            pol = _lesson_polarity(lesson)
            bucket = pol if pol in buckets else "mixed"
            label = str(lesson.get("label") or "").strip()
            lane = "약 | " if str(lesson.get("forward_use") or "") == "context_weak" else ""
            prefix = f"{entity_label}: " if entity_label else ""
            line = (
                f"    - {tag} {prefix}{lane}{label} {lesson.get('date')}: "
                f"{str(lesson.get('factor'))[:70]} — {str(lesson.get('evidence'))[:95]}"
            )
            buckets[bucket].append((str(lesson.get("date") or ""), line))

        def add_player(name, team, role_hint):
            key = _pk(name)
            if not key:
                return
            for lesson in _pl_idx.get(key, []):
                add_lesson(lesson, str(name), team, role_hint)

        for side, team in (("home", home_team), ("away", away_team)):
            add_player(row.get(f"{side}_sp_name"), team, "sp")
            for nm in _names(row.get(f"{side}_lineup_names")):
                add_player(nm, team, "bat")

        out = []
        labels = (
            ("support", "[사전 회고 신호: 지지]"),
            ("risk", "[사전 회고 신호: 위험]"),
            ("mixed", "[사전 회고 신호: 혼재/맥락]"),
        )
        for bucket, title in labels:
            rows = sorted(buckets[bucket], key=lambda x: x[0], reverse=True)[:cap_each]
            if rows:
                out.append(f"  {title}")
                out.extend(line for _date, line in rows)
        return out

    if _pl_idx or _tm_idx:
        cap_note = "전부" if _lesson_cap <= 0 else f"entity당 최근 {_lesson_cap}개"
        emit(f"[교훈] 선수/팀 read 옆 지지/위험/혼재 표시는 과거 경기서 *사전에 알 수 있었던* active memory의 렌더링 분리다. 기준/점수/예측변경 아님 — 통째로 읽고 오늘 반복되나 보라. 출력={cap_note}(저장은 전부).")
        emit("")
    for _, r in df.iterrows():
        away, home = r.get("away_team"), r.get("home_team")
        emit(f"[{away} @ {home}]")
        _shown = set()
        for _ln in _game_balance_lines(r, away, home):
            emit(_ln)
        for side, lab in (("home", home), ("away", away)):
            sp = r.get(f"{side}_sp_name")
            if pd.notna(sp) and sp:
                recs = _recent(P, sp, date, 4)
                ssn = sp_season_form(sp, date)
                emit(f"  선발 {str(lab)[:12]} — {sp} (직전 {len(recs)}경기 read):")
                if ssn:
                    emit(f"    [시즌 베이스라인] {ssn}")   # 직전(추세)과 시즌(베이스라인) 둘 다 — small-N 보정
                for d, rd in recs:
                    emit(f"    {d}: {rd[:150]}")
                if not recs:
                    emit("    (회고 기록 없음 — 첫등판/신인/외국/장기결장)")
                for _ln in _lesson_lines(_pk(sp), _pl_idx, _shown, entity_team=lab, away_team=away, home_team=home, role_hint="sp"):
                    emit(_ln)
        # 타자: 라인업 있으면 그 타자들, 없으면 생략(라이브 라인업 미게시 흔함)
        for side, lab in (("home", home), ("away", away)):
            lu = _names(r.get(f"{side}_lineup_names"))
            if not lu:
                continue
            lines = []
            for nm in lu:
                recs = _recent(H, nm, date, 3)
                if not recs:
                    continue
                seq = " · ".join(f"{d[5:]} {st}" for d, st, _ in recs)
                last_note = recs[-1][2]
                ssn = bat_season_form(nm, date)   # 직전(추세) + 시즌(베이스라인)
                lines.append(f"    {nm}: 직전 {seq}" + (f" / {ssn}" if ssn else "") + (f" — {last_note[:60]}" if last_note else ""))
                lines.extend(_lesson_lines(_pk(nm), _pl_idx, _shown, entity_team=lab, away_team=away, home_team=home, role_hint="bat"))
            if lines:
                emit(f"  타자 {str(lab)[:12]} (직전 상태·근거):")
                for ln in lines:
                    emit(ln)
        # 불펜: 비중립(직전 read에 불안 흔적) 핵심만 — 라이브 가용 불펜명. 등판예정 선발은 제외(staff fan-out 이중처리 차단, #169).
        _sp_set = {str(r.get("home_sp_name") or ""), str(r.get("away_sp_name") or "")}
        for side, lab in (("home", home), ("away", away)):
            pen = _names(r.get(f"{side}_bullpen_names"))
            if not pen:
                continue
            blocks = []
            for nm in pen:
                if str(nm) in _sp_set:   # 선발 중복 제외
                    continue
                recs = _recent(P, nm, date, 2)
                if recs:
                    blk = [f"    {nm}: " + " || ".join(f"{d[5:]}: {rd[:80]}" for d, rd in recs)]
                    if _bp_risky(recs):   # 붕괴/불안 read 보유 불펜만 교훈 부착(cap2)
                        blk += _lesson_lines(_pk(nm), _pl_idx, _shown, cap=_bp_cap, entity_team=lab, away_team=away, home_team=home, role_hint="bp")
                    blocks.append(blk)
            if blocks:
                emit(f"  불펜 {str(lab)[:12]} (직전 등판 read):")
                for blk in blocks[:8]:
                    for ln in blk:
                        emit(ln)
        # 팀 단위 교훈(선수에 안 붙은 것) — 경기 블록 끝
        for _t in (away, home):
            _tl = _lesson_lines(_t, _tm_idx, _shown, cap=_team_cap, entity_team=_t, away_team=away, home_team=home, role_hint="team")
            if _tl:
                emit(f"  팀 교훈 {str(_t)[:14]}:")
                for _ln in _tl:
                    emit(_ln)
        emit("")
    try:
        _emit_foreign_rookie_prior_block(emit, date, df)
    except Exception as e:
        emit(f"[foreign_rookie_prior WARN] source context 블록 생성 실패: {type(e).__name__}: {e}")
    try:
        _emit_win_loss_matchup_warning_block(emit, date, df)
    except Exception as e:
        emit(f"[win_loss_matchup_warning WARN] WIN_LOSS manual-veto 블록 생성 실패: {type(e).__name__}: {e}")
    try:
        import _deepread_v2 as DR2
        emit("")
        emit(DR2.format_asof_for_slate(date, df=df))
        probs = DR2.validate_asof(date)
        if probs:
            emit(f"[deepread_v2 WARN] {' / '.join(probs[:5])}")
    except Exception as e:
        emit(f"[deepread_v2 WARN] 구조화 사전근거 블록 생성 실패: {type(e).__name__}: {e}")
    try:
        import _deepread_v3 as DR3
        v3_block = DR3.format_asof_for_slate(date, df=df)
        if v3_block:
            emit("")
            emit(v3_block)
            probs = DR3.validate(date)
            if probs:
                emit(f"[deepread_v3 WARN] {' / '.join(probs[:5])}")
    except Exception as e:
        emit(f"[deepread_v3 WARN] raw-PBP 근거 블록 생성 실패: {type(e).__name__}: {e}")
    try:
        import _deepread_v3_lens as DR3L
        lens_block = DR3L.format_asof_for_slate(date, df=df)
        if lens_block:
            emit("")
            emit(lens_block)
            probs = DR3L.validate(date)
            if probs:
                emit(f"[deepread_v3_lens WARN] {' / '.join(probs[:5])}")
    except Exception as e:
        emit(f"[deepread_v3_lens WARN] 질문 렌즈 블록 생성 실패: {type(e).__name__}: {e}")
    out.close()
    print(f"\n[priors] {out_path} 저장")


if __name__ == "__main__":
    main()
