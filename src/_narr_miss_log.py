# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""Append missed MLB narrator predictions with validated pregame-readable takeaways.

The old behavior treated any _synth_master.predictable item as
had_predictable=1. That over-counts hindsight-labeled factors. This version
uses _mlb_synth_anti_hindsight_validate.py output when available and falls back
to the legacy raw behavior only when the validation report is missing.
"""

from __future__ import annotations

import json
import glob
import os
import re
import sys

import pandas as pd


ROOT = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(ROOT, "_narr_miss_log.csv")
ANTI_DIR = os.path.join(ROOT, "postmortem", "fullread_v4", "synth_anti_hindsight")
PROMPT_COMPLETENESS_DIR = os.path.join(ROOT, "postmortem", "fullread_v4", "prompt_completeness_validate")


def _load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _load_anti_hindsight(date):
    path = os.path.join(ANTI_DIR, f"mlb_synth_anti_hindsight_{date}.json")
    payload = _load_json(path, None)
    status = "qualified"
    if not payload:
        games = {}
        for candidate in sorted(glob.glob(os.path.join(ANTI_DIR, "mlb_synth_anti_hindsight_*.json"))):
            payload_candidate = _load_json(candidate, None)
            if not payload_candidate:
                continue
            selected = {str(x) for x in payload_candidate.get("selected_dates", []) or []}
            if selected and date not in selected:
                continue
            for g in payload_candidate.get("games", []) or []:
                if str(g.get("date") or "") != date:
                    continue
                gpk = str(g.get("gamePk") or "").strip()
                if gpk:
                    games[gpk] = g
        if games:
            return games, "qualified_range"
        return {}, "missing_fallback_raw"
    games = {}
    for g in payload.get("games", []) or []:
        gpk = str(g.get("gamePk") or "").strip()
        if gpk:
            games[gpk] = g
    return games, status


def _norm_gamepk(v):
    try:
        return str(int(float(v)))
    except Exception:
        return str(v or "").strip()


def _is_miss_value(value):
    try:
        return float(value) == 0.0
    except Exception:
        return str(value).strip() == "0"


def _load_reasoning_text(date):
    path = os.path.join(ROOT, f"_narr_reasoning_{date}.txt")
    if not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def _read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def _final_scoped_reasoning_path(date, scope):
    """Return the best final reasoning artifact for a sent partial scope.

    Daily MLB slates can be sent in several ready-time scopes. The legacy
    `_narr_reasoning_DATE.txt` may contain only the latest/partial generic text,
    so postgame buried-info checks should prefer the scoped artifact that was
    actually sent for the game's manifest scope.
    """
    if not scope:
        return None
    pattern = os.path.join(ROOT, f"_codex_gpt_reasoning_{date}__{scope}*")
    candidates = []
    for path in glob.glob(pattern):
        name = os.path.basename(path)
        if not name.endswith(".txt"):
            continue
        if ".pre_" in name or name.endswith("_smoke.txt") or ".smoke" in name:
            continue
        # Prefer the newest final-stage artifact. This handles normal, repaired,
        # Claude-resolved, and unresolved-pass scoped runs without hard-coding
        # a single final suffix.
        try:
            candidates.append((os.path.getmtime(path), path))
        except OSError:
            continue
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _load_scoped_reasoning_by_game(date):
    by_game = {}
    sources = {}
    for manifest_path in sorted(glob.glob(os.path.join(ROOT, f"_narr_send_manifest_{date}__*.json"))):
        manifest = _load_json(manifest_path, {})
        if not manifest.get("send_ok"):
            continue
        scope = str(manifest.get("scope") or "").strip()
        reasoning_path = _final_scoped_reasoning_path(date, scope)
        if not reasoning_path:
            continue
        text = _read_text(reasoning_path)
        if not text:
            continue
        source = os.path.basename(reasoning_path)
        for gpk in manifest.get("gamepks") or []:
            gk = _norm_gamepk(gpk)
            if not gk:
                continue
            by_game[gk] = text
            sources[gk] = source
    return by_game, sources


def _reasoning_block_for_game(reasoning_text, synth, prediction):
    if not reasoning_text:
        return ""
    away = str(prediction.get("away") or synth.get("away") or "").strip()
    home = str(prediction.get("home") or synth.get("home") or "").strip()
    matchup = str(synth.get("matchup") or "").strip()
    blocks = re.split(r"(?m)(?=^###\s+)", reasoning_text)
    keys = [x for x in (matchup, away, home) if x]
    for block in blocks:
        low = block.lower()
        if matchup and matchup.lower() in low:
            return block
        if away and home and away.lower() in low and home.lower() in low:
            return block
    if len(blocks) == 1:
        return reasoning_text
    return ""


def _term_candidates_from_factors(factors, synth, prediction):
    text_parts = []
    for f in factors or []:
        if f.get("bucket") in {"qualified_predictable", "not_actionable_pre"}:
            text_parts.append(str(f.get("factor") or ""))
            text_parts.append(str(f.get("evidence") or ""))
    text_parts.append(str(synth.get("prediction_takeaway") or ""))
    text = "\n".join(text_parts)
    exclusions = set()
    for value in (
        prediction.get("away"),
        prediction.get("home"),
        prediction.get("win_loss"),
        synth.get("matchup"),
    ):
        raw = str(value or "")
        raw = re.sub(r"\([^)]*\)", " ", raw).replace("@", " ")
        for token in re.findall(r"\b[A-Za-z][A-Za-z.'-]{2,}\b|[가-힣]{2,}", raw):
            exclusions.add(token.lower())
        cleaned = " ".join(raw.split()).lower()
        if cleaned:
            exclusions.add(cleaned)
    terms = set()
    # MLB player/team names and common English labels survive mojibake best.
    for m in re.findall(r"\b[A-Z][A-Za-z.'-]{2,}(?:\s+[A-Z][A-Za-z.'-]{2,}){0,2}\b", text):
        if m.upper() in {"MLB", "UPPER", "LOWER", "RUN_TOTAL", "WIN_LOSS", "SP", "RP", "HR"}:
            continue
        if m.lower() in exclusions:
            continue
        terms.add(m.strip())
    # Korean terms are useful when present; keep only longer concrete chunks.
    for m in re.findall(r"[가-힣A-Za-z0-9.'-]{4,}", text):
        if m.lower() in {"predictable", "qualified", "context", "season", "recent"}:
            continue
        if m.lower() in exclusions:
            continue
        terms.add(m.strip())
    return sorted(terms, key=lambda x: (-len(x), x))[:40]


_KOREAN_SUFFIX_RE = re.compile(
    r"(?:"
    r"의|은|는|이|가|을|를|와|과|도|만|로|으로|에서|에게|부터|까지|처럼|"
    r"에는|에서는|으로는|로는|였다|이었다|였다\.|이었다\."
    r")+$"
)

_GENERIC_HIT_TERMS = {
    "single", "double", "triple", "line", "drive", "lineout", "inning",
    "season", "recent", "predictable", "qualified", "context", "starter",
    "pitcher", "bullpen", "hitter", "damage", "support", "risk", "route",
    "가능했다", "사후정보라", "제외한다", "시즌흐름도", "반복된다",
}


def _term_variants(term):
    raw = str(term or "").strip()
    if not raw:
        return []
    variants = {raw}
    stripped = _KOREAN_SUFFIX_RE.sub("", raw).strip(" .,:;()[]{}'\"")
    if stripped:
        variants.add(stripped)
    # Mixed Korean/English terms such as "Yesavage의" or "Lugo처럼" are
    # common in synth output. Keep the concrete English token so paraphrased
    # narrator text that names the same player is not falsely marked buried.
    for m in re.findall(r"\b[A-Za-z][A-Za-z.'-]{2,}\b", raw):
        variants.add(m.strip(" .,:;()[]{}'\""))
    clean = []
    for v in variants:
        v = str(v or "").strip()
        if len(v) < 4:
            continue
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            continue
        if v.lower() in _GENERIC_HIT_TERMS:
            continue
        clean.append(v)
    return sorted(set(clean), key=lambda x: (-len(x), x))


def _mentions_pregame_context(reasoning_block, factors, synth, prediction):
    if not reasoning_block:
        return False, []
    low = reasoning_block.lower()
    hits = []
    for term in _term_candidates_from_factors(factors, synth, prediction):
        for variant in _term_variants(term):
            if variant.lower() in low:
                hits.append(variant if variant == term else f"{variant}~{term}")
                break
    return bool(hits), hits[:12]


def _load_prompt_completeness_drops(date):
    drops = {}
    pattern = os.path.join(PROMPT_COMPLETENESS_DIR, f"mlb_prompt_completeness_validate_{date}*.json")
    for path in glob.glob(pattern):
        base = os.path.basename(path)
        if "BROKEN_TEST" in base.upper():
            continue
        payload = _load_json(path, {})
        for check in payload.get("checks") or []:
            if check.get("status") != "DROPPED":
                continue
            if check.get("severity") != "hard_candidate":
                continue
            gpk = _norm_gamepk(check.get("gamePk"))
            if not gpk:
                continue
            drops.setdefault(gpk, []).append(
                {
                    "section": check.get("section"),
                    "detail": check.get("detail"),
                    "source": base,
                }
            )
    return drops


def _fourway_label(
    *,
    prompt_drops,
    anti,
    raw_predictable_count,
    had_pregame_context,
    post_or_hindsight_only,
    reasoning_mentioned,
):
    if prompt_drops:
        return "missing_info", "A1_prompt_completeness_hard_drop"
    if not had_pregame_context:
        if post_or_hindsight_only or raw_predictable_count > 0:
            return "unpredictable", "anti_hindsight_post_only_or_hindsight_only"
        return "missing_info", "no_pregame_context_discovered"
    if reasoning_mentioned:
        return "misjudged_info", "pregame_context_was_mentioned_but_final_prediction_missed"
    return "buried_info", "pregame_context_existed_but_not_found_in_saved_reasoning"


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: py _narr_miss_log.py YYYY-MM-DD")
    date = sys.argv[1]

    try:
        from _mlb_narrator_sent_ledger import load_predictions_prefer_sent_ledger

        predictions, _prediction_meta = load_predictions_prefer_sent_ledger(date)
    except Exception:
        predictions = {}
    if not predictions:
        predictions = _load_json(os.path.join(ROOT, f"_narr_predictions_{date}.json"), {})

    synth_by_game = {}
    for g in _load_json(os.path.join(ROOT, "_synth_master.json"), []):
        synth_by_game[_norm_gamepk(g.get("gamePk"))] = g

    anti_by_game, anti_status = _load_anti_hindsight(date)
    reasoning_text = _load_reasoning_text(date)
    scoped_reasoning_by_game, scoped_reasoning_source_by_game = _load_scoped_reasoning_by_game(date)
    prompt_drops_by_game = _load_prompt_completeness_drops(date)

    results = {}
    results_path = os.path.join(ROOT, "_narr_results_master.csv")
    if os.path.exists(results_path):
        rm = pd.read_csv(results_path)
        rm = rm[rm["date"].astype(str) == date]
        for _, r in rm.iterrows():
            results[_norm_gamepk(r.get("gamePk"))] = r

    rows = []
    for gk, p in predictions.items():
        gk = _norm_gamepk(gk)
        rr = results.get(gk)
        if rr is None:
            continue

        win_loss_hit = rr.get("win_loss_hit")
        run_total_hit = rr.get("run_total_hit")
        win_loss_miss = _is_miss_value(win_loss_hit)
        run_total_miss = _is_miss_value(run_total_hit)
        if not (win_loss_miss or run_total_miss):
            continue

        synth = synth_by_game.get(gk, {})
        raw_predictable_count = len(synth.get("predictable") or [])
        anti = anti_by_game.get(gk) or {}

        if anti:
            qualified_predictable_count = int(anti.get("qualified_predictable_count") or 0)
            hindsight_suspect_count = int(anti.get("hindsight_suspect_count") or 0)
            not_actionable_pre_count = int(anti.get("not_actionable_pre_count") or 0)
            post_only_count = int(anti.get("post_only_count") or 0)
            had_pregame_context = int(anti.get("had_pregame_context") or (qualified_predictable_count > 0))
            actionable_miss = int(anti.get("actionable_miss") or 0)
            context_only_not_actionable = int(
                anti.get("context_only_not_actionable")
                if anti.get("context_only_not_actionable") is not None
                else (had_pregame_context and not actionable_miss)
            )
            post_or_hindsight_only = int(anti.get("post_or_hindsight_only") or 0)
            had_predictable = had_pregame_context
            row_anti_status = anti_status
        else:
            qualified_predictable_count = raw_predictable_count
            hindsight_suspect_count = 0
            not_actionable_pre_count = 0
            post_only_count = 0
            had_pregame_context = int(raw_predictable_count > 0)
            actionable_miss = 0
            context_only_not_actionable = int(raw_predictable_count > 0)
            post_or_hindsight_only = 0
            had_predictable = had_pregame_context
            row_anti_status = anti_status

        factors = anti.get("factors") if isinstance(anti, dict) else []
        game_reasoning_text = scoped_reasoning_by_game.get(gk) or reasoning_text
        reasoning_source = scoped_reasoning_source_by_game.get(gk) or f"_narr_reasoning_{date}.txt"
        reasoning_block = _reasoning_block_for_game(game_reasoning_text, synth, p)
        reasoning_mentioned, reasoning_hits = _mentions_pregame_context(reasoning_block, factors, synth, p)
        prompt_drops = prompt_drops_by_game.get(gk) or []
        recap_fourway, recap_fourway_reason = _fourway_label(
            prompt_drops=prompt_drops,
            anti=anti,
            raw_predictable_count=raw_predictable_count,
            had_pregame_context=had_pregame_context,
            post_or_hindsight_only=post_or_hindsight_only,
            reasoning_mentioned=reasoning_mentioned,
        )

        rows.append(
            {
                "date": date,
                "gamePk": gk,
                "matchup": (synth.get("matchup") or f"{p.get('away')}@{p.get('home')}")[:50],
                "win_loss_prediction": p.get("win_loss"),
                "win_loss_miss": int(win_loss_miss),
                "run_total_prediction": f"{p.get('run_total')} {p.get('line')}",
                "run_total_miss": int(run_total_miss),
                "had_predictable": had_predictable,
                "had_pregame_context": had_pregame_context,
                "actionable_miss": actionable_miss,
                "context_only_not_actionable": context_only_not_actionable,
                "post_or_hindsight_only": post_or_hindsight_only,
                "raw_predictable_count": raw_predictable_count,
                "qualified_predictable_count": qualified_predictable_count,
                "hindsight_suspect_count": hindsight_suspect_count,
                "not_actionable_pre_count": not_actionable_pre_count,
                "post_only_count": post_only_count,
                "anti_hindsight_status": row_anti_status,
                "recap_fourway": recap_fourway,
                "recap_fourway_reason": recap_fourway_reason,
                "reasoning_mentioned_pregame_context": int(reasoning_mentioned),
                "reasoning_context_hits": "; ".join(reasoning_hits)[:300],
                "reasoning_source": reasoning_source[:160],
                "a1_prompt_drop_count": len(prompt_drops),
                "a1_prompt_drop_sections": "; ".join(str(x.get("section") or "") for x in prompt_drops)[:200],
                "takeaway": (synth.get("prediction_takeaway") or "")[:300],
            }
        )

    if not rows:
        print(f"[miss_log] {date} missed predictions 0 or ungraded; no rows written")
        return

    new = pd.DataFrame(rows)
    if os.path.exists(LOG):
        old = pd.read_csv(LOG)
        old = old[old["date"].astype(str) != date]
        new = pd.concat([old, new], ignore_index=True)
    new.to_csv(LOG, index=False, encoding="utf-8-sig")

    context = sum(int(r["had_pregame_context"]) for r in rows)
    actionable = sum(int(r["actionable_miss"]) for r in rows)
    context_only = sum(int(r["context_only_not_actionable"]) for r in rows)
    fourway_counts = {}
    for r in rows:
        k = str(r.get("recap_fourway") or "")
        fourway_counts[k] = fourway_counts.get(k, 0) + 1
    print(
        f"[miss_log] {date} missed_games={len(rows)} "
        f"pregame_context={context} actionable_miss={actionable} "
        f"context_only={context_only} no_context_or_postonly={len(rows)-context} "
        f"anti_hindsight_status={anti_status} fourway={fourway_counts}"
    )


if __name__ == "__main__":
    main()
