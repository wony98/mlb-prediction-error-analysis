# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""_reason_bundles.py — 경기별 [경기전 정보 + 실제 경기내용] 번들 (네레이터 이유찾기 입력).

각 경기 → _reason_bundles/{date}/{gamePk}.md:
  ① 경기 전 알 수 있던 것 = 시즌 클린 패킷 블록(leak-free 사전신호)
  ② 실제 경기 내용 = 풀 내러티브(타석별 전개)
네레이터는 ②를 *기준 없이* 읽어 이유를 찾고, ①과 대조해 전(사전인지)/후+운(예측불가) 판정.

usage: py _reason_bundles.py YYYY-MM-DD
"""
import os
import re
import sys
import json
import pandas as pd
from _mlb_result_status import no_result_gamepks

ROOT = os.path.dirname(os.path.abspath(__file__))
NARR = os.path.join(ROOT, "_narr2026.parquet")
RAW_PBP_ROOT = os.path.join(ROOT, "_deepread_v3", "raw_plays")
PARTIAL_RESULT_DIR = os.path.join(ROOT, "postmortem", "partial_results")


def packet_blocks(date):
    """{ (away,home) : 패킷 블록텍스트 } — _season_packet_{date}.md 파싱."""
    f = os.path.join(ROOT, f"_season_packet_{date}.md")
    if not os.path.exists(f):
        return {}
    txt = open(f, encoding="utf-8").read()
    out = {}
    for blk in txt.split("\n## ")[1:]:
        blk = "## " + blk
        m = re.match(r"## (.+?) @ (.+?) — ", blk)
        if m:
            out[(m.group(1).strip(), m.group(2).strip())] = blk.strip()
    return out


def _env_blocks(date):
    """{gamePk: '[그날 환경] 날씨·심판 ...'} — _v3_games_{date}.csv(그날 분석 슬레이트)에서. 운/실력 재검증용."""
    f = os.path.join(ROOT, f"_v3_games_{date}.csv")
    if not os.path.exists(f):
        return {}
    try:
        d = pd.read_csv(f)
    except Exception:
        return {}
    out = {}
    for _, r in d.iterrows():
        gpk = r.get("gamePk")
        if pd.isna(gpk):
            continue
        parts = []
        t, ws, pp = r.get("temperature"), r.get("wind_speed"), r.get("precip_prob")
        if pd.notna(t):
            parts.append(f"{float(t):.0f}°F")
        if pd.notna(ws):
            wd = r.get("wind_direction")
            wd_txt = ""
            if pd.notna(wd) and str(wd).strip():
                try:
                    wd_txt = f"({float(wd):.0f}°)"
                except (TypeError, ValueError):
                    wd_txt = f"({str(wd).strip()})"
            parts.append(f"바람 {float(ws):.0f}mph" + wd_txt)
        if pd.notna(pp):
            parts.append(f"강수 {float(pp):.0f}%")
        um = r.get("hp_ump_name")
        if pd.notna(um) and str(um).strip():
            rm = r.get("hp_ump_runs_mean")
            parts.append(f"주심 {um}" + (f"(평균 {float(rm):.1f}득점)" if pd.notna(rm) else ""))
        if parts:
            out[int(gpk)] = "[그날 환경 — 날씨·심판] " + " · ".join(parts)
    return out


def _raw_pbp_path(date, game_pk):
    path = os.path.join(RAW_PBP_ROOT, date, f"{int(game_pk)}.json")
    return path if os.path.exists(path) else None


def _person_name(block):
    return str((block or {}).get("fullName") or "").strip()


def _half_label(about):
    inning = about.get("inning")
    half = str(about.get("halfInning") or "")
    return f"{inning}{'T' if half == 'top' else 'B'}"


def _event_is_key(event_type, result):
    event_type = str(event_type or "").lower()
    if result.get("rbi") or result.get("isScoringPlay"):
        return True
    return event_type in {
        "single", "double", "triple", "home_run", "walk", "intent_walk",
        "hit_by_pitch", "strikeout", "field_error", "force_out",
        "grounded_into_double_play",
    }


def _bases_after(play):
    bases = set()
    for runner in play.get("runners") or []:
        mv = runner.get("movement") or {}
        end = mv.get("end")
        if end in {"1B", "2B", "3B"} and not mv.get("isOut"):
            bases.add(end[0])
    return ",".join(sorted(bases, key=lambda x: "123".index(x))) if bases else "empty"


def _score_delta(prev_score, score):
    if prev_score is None:
        return 0
    return (score[0] - prev_score[0]) + (score[1] - prev_score[1])


def _raw_pbp_summary(date, game_pk):
    path = _raw_pbp_path(date, game_pk)
    if not path:
        return ""
    try:
        raw = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        return f"\n## RAW PBP source summary\nraw_pbp_error: {type(e).__name__}: {e}\n"
    plays = raw.get("allPlays") or []
    if not plays:
        return "\n## RAW PBP source summary\nraw_pbp_error: allPlays empty\n"

    lines = [
        "## RAW PBP source summary",
        f"source: {path}",
        "This section is reconstructed directly from StatsAPI allPlays, not from cached narrative.",
        "",
        "### Chronological at-bat flow",
    ]
    pitchers_by_fielding_side = {"home": [], "away": []}
    pitcher_runs = {}
    batter_events = {}
    prev_pitcher_by_fielding_side = {}
    prev_score = None
    inning_runs = {}

    for play in plays:
        result = play.get("result") or {}
        about = play.get("about") or {}
        matchup = play.get("matchup") or {}
        batter = _person_name(matchup.get("batter"))
        pitcher = _person_name(matchup.get("pitcher"))
        event = str(result.get("event") or result.get("eventType") or "").strip()
        event_type = str(result.get("eventType") or "").strip()
        desc = " ".join(str(result.get("description") or "").split())
        half = str(about.get("halfInning") or "")
        fielding_side = "home" if half == "top" else "away"
        label = _half_label(about)
        away_score = result.get("awayScore")
        home_score = result.get("homeScore")
        if away_score is None or home_score is None:
            continue
        score = (int(away_score), int(home_score))

        if pitcher and prev_pitcher_by_fielding_side.get(fielding_side) != pitcher:
            prev_pitcher_by_fielding_side[fielding_side] = pitcher
            pitchers_by_fielding_side.setdefault(fielding_side, []).append(pitcher)
            lines.append(f"- {label} pitcher use: {fielding_side} defense -> {pitcher} (score {score[0]}-{score[1]})")

        runs = _score_delta(prev_score, score)
        if runs > 0:
            inning_runs[label] = inning_runs.get(label, 0) + runs
            if pitcher:
                pitcher_runs[pitcher] = pitcher_runs.get(pitcher, 0) + runs
        prev_score = score

        if batter:
            be = batter_events.setdefault(batter, {"hr": 0, "xbh": 0, "bb_hbp": 0, "k": 0, "rbi": 0})
            et = event_type.lower()
            if et == "home_run":
                be["hr"] += 1
                be["xbh"] += 1
            elif et in {"double", "triple"}:
                be["xbh"] += 1
            elif et in {"walk", "intent_walk", "hit_by_pitch"}:
                be["bb_hbp"] += 1
            elif et == "strikeout":
                be["k"] += 1
            be["rbi"] += int(result.get("rbi") or 0)

        if _event_is_key(event_type, result):
            outs = (play.get("count") or {}).get("outs")
            bases = _bases_after(play)
            rbi = int(result.get("rbi") or 0)
            rbi_txt = f", RBI={rbi}" if rbi else ""
            desc_txt = f" -- {desc[:220]}" if desc else ""
            lines.append(
                f"- {label} {batter} vs {pitcher}: {event} "
                f"(outs={outs}, bases_after={bases}, score={score[0]}-{score[1]}{rbi_txt}){desc_txt}"
            )

    if inning_runs:
        lines.extend(["", "### Scoring bursts"])
        for label, runs in sorted(inning_runs.items(), key=lambda kv: (int(kv[0][:-1]), kv[0][-1])):
            if runs >= 2:
                lines.append(f"- {label}: {runs} runs")

    lines.extend(["", "### Pitcher usage and run exposure"])
    for side, pitchers in pitchers_by_fielding_side.items():
        if pitchers:
            usage = " -> ".join(dict.fromkeys(pitchers))
            lines.append(f"- {side} defense pitchers: {usage}")
    for pitcher, runs in sorted(pitcher_runs.items(), key=lambda kv: (-kv[1], kv[0])):
        if runs:
            lines.append(f"- runs while pitcher in game: {pitcher} = {runs}")

    hitter_notes = []
    for batter, ev in batter_events.items():
        if ev["hr"] or ev["xbh"] or ev["bb_hbp"] >= 2 or ev["rbi"] >= 2:
            parts = []
            if ev["hr"]:
                parts.append(f"HR={ev['hr']}")
            if ev["xbh"]:
                parts.append(f"XBH={ev['xbh']}")
            if ev["bb_hbp"]:
                parts.append(f"BB/HBP={ev['bb_hbp']}")
            if ev["rbi"]:
                parts.append(f"RBI={ev['rbi']}")
            hitter_notes.append((batter, ", ".join(parts)))
    if hitter_notes:
        lines.extend(["", "### Player event flags"])
        for batter, note in sorted(hitter_notes):
            lines.append(f"- {batter}: {note}")

    return "\n" + "\n".join(lines) + "\n"


def _local_no_result_gamepks(date):
    ymd = str(date).replace("-", "")
    path = os.path.join(PARTIAL_RESULT_DIR, f"{ymd}_mlb_partial_status.json")
    if not os.path.exists(path):
        return set()
    try:
        data = json.load(open(path, encoding="utf-8"))
    except Exception:
        return set()
    out = set()
    for game in data.get("games") or []:
        if str(game.get("state") or "").lower() == "postponed":
            try:
                out.add(int(game.get("game_id")))
            except Exception:
                pass
    return out


def _local_final_games(date):
    ymd = str(date).replace("-", "")
    path = os.path.join(PARTIAL_RESULT_DIR, f"{ymd}_mlb_partial_status.json")
    if not os.path.exists(path):
        return []
    try:
        data = json.load(open(path, encoding="utf-8"))
    except Exception:
        return []
    games = []
    for game in data.get("games") or []:
        if str(game.get("state") or "").lower() == "final":
            try:
                game_pk = int(game.get("game_id"))
            except Exception:
                continue
            games.append({
                "gamePk": game_pk,
                "away": str(game.get("away") or ""),
                "home": str(game.get("home") or ""),
                "final": f"{game.get('away_score')}-{game.get('home_score')}",
            })
    return games


def build(date):
    # 내용 전용 번들 — 피처 패킷(①) 일절 없음. 마커/라벨 0. 경기 전개 + 그날 환경(날씨·심판)만.
    outdir = os.path.join(ROOT, "_reason_content", date)
    os.makedirs(outdir, exist_ok=True)
    nar = pd.read_parquet(NARR)
    nar = nar[nar["date"].astype(str).str.startswith(date)]
    excluded = []
    try:
        no_result = {int(x) for x in no_result_gamepks(date)}
    except Exception as e:
        no_result = _local_no_result_gamepks(date)
        print(f"[content] no_result status fetch failed for {date}: {e}")
        if no_result:
            print(f"[content] using local partial_status no-result games {sorted(no_result)}")
    if no_result and "gamePk" in nar.columns:
        before = len(nar)
        nar = nar[~nar["gamePk"].astype(int).isin(no_result)].copy()
        excluded = sorted(no_result)
        print(f"[content] excluded no-result games {excluded} ({before}->{len(nar)})")
        for excluded_gpk in excluded:
            stale = os.path.join(outdir, f"{excluded_gpk}.md")
            if os.path.exists(stale):
                os.remove(stale)
                print(f"[content] removed stale no-result bundle {stale}")
    env = _env_blocks(date)
    n = 0
    pks = []
    processed = set()
    for _, r in nar.iterrows():
        gpk = int(r["gamePk"])
        processed.add(gpk)
        away, home = str(r["away"]), str(r["home"])
        envline = env.get(gpk, "")
        body = (
            f"# {away} @ {home} — 최종 {r['final']}\n\n"
            + (f"{envline}\n\n" if envline else "")
            + f"## 경기 전개 (타석별 플레이 — 이걸 읽고 왜 이기고 졌는지 찾아라)\n"
            f"{str(r['narrative'])}\n"
            + _raw_pbp_summary(date, gpk)
        )
        open(os.path.join(outdir, f"{gpk}.md"), "w", encoding="utf-8").write(body)
        pks.append(gpk)
        n += 1
    for game in _local_final_games(date):
        gpk = int(game["gamePk"])
        if gpk in processed:
            continue
        if not _raw_pbp_path(date, gpk):
            continue
        away, home = game["away"], game["home"]
        envline = env.get(gpk, "")
        body = (
            f"# {away} @ {home} final {game['final']}\n\n"
            + (f"{envline}\n\n" if envline else "")
            + "## Cached narrative flow\n"
            + "(cached narrative row unavailable; using raw PBP source summary below)\n"
            + _raw_pbp_summary(date, gpk)
        )
        open(os.path.join(outdir, f"{gpk}.md"), "w", encoding="utf-8").write(body)
        pks.append(gpk)
        n += 1
    if excluded:
        with open(os.path.join(outdir, "_excluded_no_result.json"), "w", encoding="utf-8") as f:
            json.dump({
                "schema": "reason_bundles_no_result_exclusions.v1",
                "date": date,
                "excluded_no_result_gamePks": excluded,
                "reason": "scoreless postponed/cancelled/suspended games are not deepread targets",
            }, f, ensure_ascii=False, indent=2)
    print(f"[content] {outdir} — {n}경기")
    print("gamePks:", pks)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: py _reason_bundles.py YYYY-MM-DD"); sys.exit(1)
    build(sys.argv[1])
