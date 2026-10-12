# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""_narr_synth_lessons.py — synth(사전/사후 분리)의 predictable을 선수/팀 매핑된 교훈 ledger로 (historical-note).

`_synth_master.json`의 각 경기 predictable(사전에 알 수 있었던 요인)만 뽑아 `_narr_synth_lessons.jsonl`에
누적. 선수명/팀명을 factor+evidence에서 매핑(linked_players/teams)해, 다음 분석 때 `_narr_priors`가
*오늘 슬레이트의 그 선수/팀*에게만 라우팅해 통째로 붙인다(선수패턴과 동일 — 기준필터 아님).

원칙(LOCK): 저장은 전부(N cap 없음). predictable만 forward(not_predictable/luck는 _synth_master에 남김).
provenance = ref_backed(drv/lens refs 있음) | synth_backed. player/team 매칭 불가 = unrouted(저장하되 priors 미주입).

usage: py _narr_synth_lessons.py --build
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import unicodedata

ROOT = os.path.dirname(os.path.abspath(__file__))
SMASTER = os.path.join(ROOT, "_synth_master.json")
LEDGER = os.path.join(ROOT, "_narr_synth_lessons.jsonl")
TAKEAWAYS = os.path.join(ROOT, "_narr_synth_takeaways.jsonl")
EXCLUDED = os.path.join(ROOT, "_narr_synth_lessons_audit_excluded.jsonl")
ANTI_DIR = os.path.join(ROOT, "postmortem", "fullread_v4", "synth_anti_hindsight")

TEAM_AB = {
    'Arizona Diamondbacks': 'ARI', 'Atlanta Braves': 'ATL', 'Baltimore Orioles': 'BAL', 'Boston Red Sox': 'BOS',
    'Chicago Cubs': 'CHC', 'Chicago White Sox': 'CWS', 'Cincinnati Reds': 'CIN', 'Cleveland Guardians': 'CLE',
    'Colorado Rockies': 'COL', 'Detroit Tigers': 'DET', 'Houston Astros': 'HOU', 'Kansas City Royals': 'KC',
    'Los Angeles Angels': 'LAA', 'Los Angeles Dodgers': 'LAD', 'Miami Marlins': 'MIA', 'Milwaukee Brewers': 'MIL',
    'Minnesota Twins': 'MIN', 'New York Mets': 'NYM', 'New York Yankees': 'NYY', 'Athletics': 'ATH',
    'Philadelphia Phillies': 'PHI', 'Pittsburgh Pirates': 'PIT', 'San Diego Padres': 'SD', 'San Francisco Giants': 'SF',
    'Seattle Mariners': 'SEA', 'St. Louis Cardinals': 'STL', 'Tampa Bay Rays': 'TB', 'Texas Rangers': 'TEX',
    'Toronto Blue Jays': 'TOR', 'Washington Nationals': 'WSH',
}


def _gpk_date_map():
    m = {}
    try:
        M = json.load(open(os.path.join(ROOT, "_reason_master.json"), encoding="utf-8"))
        for e in M:
            m[str(e.get("gamePk"))] = e.get("date")
    except Exception:
        pass
    return m


def _load_anti_hindsight_index():
    """Return {(gamePk, factor_index): factor_audit}.

    The deepread/synth row remains the source of the retrospective read. This
    audit index only decides whether a synth predictable item can be forwarded
    into active narrator priors or should remain audit-only.
    """
    idx = {}
    if not os.path.isdir(ANTI_DIR):
        return idx
    for path in sorted(glob.glob(os.path.join(ANTI_DIR, "mlb_synth_anti_hindsight_*.json"))):
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception:
            continue
        for game in data.get("games", []) or []:
            gk = str(game.get("gamePk") or "")
            if not gk:
                continue
            for factor in game.get("factors", []) or []:
                try:
                    fidx = int(factor.get("index"))
                except Exception:
                    fidx = None
                if fidx is not None:
                    idx[(gk, fidx)] = factor
                raw_factor = str(factor.get("factor") or "")
                if raw_factor:
                    idx[(gk, "factor", raw_factor)] = factor
    return idx


def _anti_for(index, game_pk, factor_index, item):
    gk = str(game_pk)
    found = index.get((gk, factor_index))
    if found:
        return found
    return index.get((gk, "factor", str(item.get("factor") or "")))


def _forward_lane(audit):
    if not audit:
        return "legacy_unqualified"
    bucket = str(audit.get("bucket") or "")
    if bucket in {"hindsight_suspect", "post_only"}:
        return "audit_only"
    if bucket == "not_actionable_pre":
        return "context_weak"
    if bucket == "qualified_predictable":
        return "context"
    return "context_weak"


_SUFFIX = {"jr", "sr", "ii", "iii", "iv", "v"}


def _surname(full):
    """접미사(Jr./Sr./II/III) 제거한 진짜 성(소문자). 'Bobby Witt Jr.' → 'witt'."""
    parts = [p for p in str(full).split() if p]
    while len(parts) > 1 and parts[-1].lower().strip(".") in _SUFFIX:
        parts.pop()
    return parts[-1].lower() if parts else ""


def _pkey(name):
    """정규화 식별키 = firstname.surname (악센트·접미사 제거, 소문자). 원장↔priors 룩업 일치용.
    first-initial만 쓰면 Ben/Brandon Williamson·Eduardo/Endy Rodriguez가 같은 키로 충돌 → full first name 사용."""
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode()
    parts = [p for p in re.sub(r"[^A-Za-z .'-]", "", s).split() if p]
    suffix = ""
    while len(parts) > 1 and parts[-1].lower().strip(".") in _SUFFIX:
        suffix = parts.pop().lower().strip(".")
    if not parts:
        return ""
    sur = parts[-1].lower()
    first = parts[0].lower().strip(".")
    key = f"{first}.{sur}" if len(parts) > 1 else sur
    return f"{key}.{suffix}" if suffix else key


def _gpk_roster():
    """gpk → {full_name}: 그 경기 등판/출장 선수(정독 reads). 교훈 매칭을 *그 경기 로스터*로 제한."""
    roster = {}
    try:
        M = json.load(open(os.path.join(ROOT, "_reason_master.json"), encoding="utf-8"))
    except Exception:
        return roster
    for e in M:
        gk = str(e.get("gamePk"))
        names = set()
        for blk in ("pitcher_reads", "hitter_reads"):
            for x in e.get(blk, []) or []:
                nm = str(x.get("name", "")).split(" (")[0].strip()
                if nm:
                    names.add(nm)
        if names:
            roster[gk] = names
    return roster


def _link_players(factor, roster_names):
    """factor의 *주인공*을 그 경기 로스터 안에서만 매칭(타 경기·Jr. 교차매칭 차단). 반환 full name 리스트."""
    if not roster_names:
        return []
    sur_map = {}
    for full in roster_names:
        sur_map.setdefault(_surname(full), []).append(full)
    txt = factor or ""
    ascii_txt = unicodedata.normalize("NFKD", txt).encode("ascii", "ignore").decode().lower()
    exact = []
    for full in roster_names:
        s = unicodedata.normalize("NFKD", str(full)).encode("ascii", "ignore").decode()
        parts = [p for p in re.sub(r"[^A-Za-z .'-]", "", s).split() if p]
        while len(parts) > 1 and parts[-1].lower().strip(".") in _SUFFIX:
            parts.pop()
        if len(parts) >= 2:
            phrase = f"{parts[0]} {parts[-1]}".lower()
            if re.search(r"(?<![a-z])" + re.escape(phrase) + r"(?![a-z])", ascii_txt):
                exact.append(full)
    if exact:
        return sorted(set(exact))
    # 1) 주어 추출: 'X의/X이/X가/X은/X는' 앞 이름 → 로스터 매칭(주어 우선, 언급자 오염↓)
    m = re.search(r"([A-Z][A-Za-zÀ-ÖØ-öø-ÿ'.\-]+(?:\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'.\-]+){0,2})\s*(?:의|이|가|은|는)", txt)
    if m:
        cs = _surname(m.group(1))
        if cs and cs in sur_map and len(sur_map[cs]) == 1:
            return sorted(set(sur_map[cs]))
    # 2) 폴백: 로스터 중 factor에 성(접미사 제거) 등장한 선수 전부(같은 경기라 안전)
    toks = {t.lower() for t in re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'.\-]*", txt) if len(t) >= 3}
    hits = []
    for sur, fulls in sur_map.items():
        if sur and sur in toks and len(fulls) == 1:
            hits.extend(fulls)
    return sorted(set(hits))


def _link_teams(text, matchup):
    teams = []
    for full, ab in TEAM_AB.items():
        if full in (matchup or "") or full in text or re.search(r"(?<![A-Za-z])" + ab + r"(?![A-Za-z])", text):
            teams.append(full)
    return sorted(set(teams))


def build():
    try:
        master = json.load(open(SMASTER, encoding="utf-8"))
    except Exception:
        master = []
    try:
        import _narr_factor_label_normalizer as labeler

        label_vocab = labeler.load_vocab()
    except Exception:
        labeler = None
        label_vocab = None
    roster = _gpk_roster()
    gd = _gpk_date_map()
    anti_idx = _load_anti_hindsight_index()
    rows = []
    excluded_rows = []
    takeaway_rows = []
    for g in master:
        gk = str(g.get("gamePk"))
        date = gd.get(gk) or g.get("date") or ""
        mk = g.get("matchup", "")
        rn = roster.get(gk, set())
        raw_predictable = g.get("predictable", []) or []
        usable_predictable = []
        for factor_index, p in enumerate(raw_predictable, start=1):
            factor = str(p.get("factor", "")); evid = str(p.get("evidence", ""))
            audit = _anti_for(anti_idx, gk, factor_index, p)
            lane = _forward_lane(audit)
            label = ""
            if labeler is not None and label_vocab is not None:
                label, _label_source = labeler.label_factor(factor, evid, vocab=label_vocab)
            # 라우팅 = 그 경기 로스터 안에서 factor 주어 매칭(타경기·Jr. 교차 차단). 없으면 팀단위.
            lp = _link_players(factor, rn)
            lt = _link_teams(factor + " " + mk, mk)
            prov = "ref_backed" if re.search(r"(drv[23]|lens3):", factor + " " + evid) else "synth_backed"
            routed = bool(lp or lt)
            row = {
                "date": date, "gamePk": int(gk) if gk.isdigit() else gk, "matchup": mk,
                "factor": factor, "evidence": evid, "label": label, "provenance": prov,
                "linked_players": lp, "linked_keys": [_pkey(x) for x in lp], "linked_teams": lt,
                "routed": routed,
                "forward_use": lane if routed else "unrouted",
                "synth_factor_index": factor_index,
                "anti_hindsight_status": "checked" if audit else "missing_legacy",
                "anti_hindsight_bucket": (audit or {}).get("bucket"),
                "anti_hindsight_actionability": (audit or {}).get("actionability"),
                "anti_hindsight_reason_codes": (audit or {}).get("reason_codes", []),
                "blame_allowed": bool((audit or {}).get("blame_allowed", False)),
            }
            if lane == "audit_only":
                row["routed"] = False
                row["forward_use"] = "audit_only"
                excluded_rows.append(row)
                continue
            rows.append(row)
            usable_predictable.append(p)
        takeaway = str(g.get("prediction_takeaway") or "").strip()
        if takeaway:
            predictable = usable_predictable
            route_text = " ".join(
                [takeaway, mk]
                + [str(p.get("factor", "")) for p in predictable[:6]]
                + [str(p.get("evidence", "")) for p in predictable[:3]]
            )
            lp = _link_players(route_text, rn)
            lt = _link_teams(route_text, mk)
            routed = bool(lp or lt)
            takeaway_rows.append({
                "date": date,
                "gamePk": int(gk) if gk.isdigit() else gk,
                "matchup": mk,
                "takeaway": takeaway,
                "predictable_count": len(predictable),
                "raw_predictable_count": len(raw_predictable),
                "audit_excluded_predictable_count": len(raw_predictable) - len(predictable),
                "linked_players": lp,
                "linked_keys": [_pkey(x) for x in lp],
                "linked_teams": lt,
                "routed": routed,
                "forward_use": "salience" if routed else "unrouted",
            })
    with open(LEDGER, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(EXCLUDED, "w", encoding="utf-8") as f:
        for r in excluded_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(TAKEAWAYS, "w", encoding="utf-8") as f:
        for r in takeaway_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    routed = sum(1 for r in rows if r["routed"])
    tw_routed = sum(1 for r in takeaway_rows if r["routed"])
    print(f"[lessons] {os.path.basename(LEDGER)} ← predictable {len(rows)}개 (routed {routed} / unrouted {len(rows)-routed}) · {len(set(str(r['gamePk']) for r in rows))}경기")
    print(f"[lessons] {os.path.basename(EXCLUDED)} audit-only excluded {len(excluded_rows)} rows")
    print(f"[lessons] {os.path.basename(TAKEAWAYS)} takeaways {len(takeaway_rows)} rows (routed {tw_routed} / unrouted {len(takeaway_rows)-tw_routed})")
    return rows


def load_lessons(before_date):
    """date < before_date 이고 routed=True인 교훈만(leak-free)."""
    out = []
    if not os.path.exists(LEDGER):
        return out
    for ln in open(LEDGER, encoding="utf-8"):
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if r.get("routed") and str(r.get("date", "")) < before_date:
            out.append(r)
    return out


def load_takeaways(before_date):
    """Return routed prediction_takeaway rows with date < before_date."""
    out = []
    if not os.path.exists(TAKEAWAYS):
        return out
    for ln in open(TAKEAWAYS, encoding="utf-8"):
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if r.get("routed") and str(r.get("date", "")) < before_date:
            out.append(r)
    return out


def main():
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="synth predictable → 교훈 ledger")
    ap.add_argument("--build", action="store_true")
    ap.parse_args()
    build()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
