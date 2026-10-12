# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
_narr_reasoning.py — headless claude -p로 네레이터 추론 섹션 자동 생성 (separate narrator lane)

run_mlb_v3.py 데일리 예측 메시지의 🧭 섹션을, 수치 verdict 대신 *컨텍스트-추론*으로 생성한다.
narr_context.py(사실 고정) + _narr_rules.md(조건부 패턴북)를 claude -p에 먹여 예측별 추론을 받는다.

설계 원칙:
- **best-effort + 폴백**: claude -p 실패/타임아웃/빈 출력이면 None 반환 → 호출측(run_mlb_v3)이
  기존 수치 verdict 섹션으로 폴백. 메시지는 절대 안 깨진다.
- **separate lane**: 모델예측 본문은 덮지 않고, 네레이터예측을 별도 저장/표시/채점한다.
- **pre-game 예측 모드 전용** (결과/회고 모드는 호출측이 기존 로직 유지).
- headless 패턴: stdin pipe, --bare 금지, PYTHONUTF8=1 (common/fix_headless_claude_pipe).
"""
import os
import sys
import json
import re
import subprocess
import math

ROOT = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_SKILL = os.path.join(ROOT, "..", "prompts")
SKILL = os.environ.get("NARR_SKILL_DIR") or _DEFAULT_SKILL
if not os.path.isabs(SKILL):
    SKILL = os.path.join(ROOT, SKILL)
CTX = os.environ.get("NARR_CONTEXT_SCRIPT") or os.path.join(ROOT, "narr_context.py")
RULES = os.environ.get("NARR_RULES_PATH") or os.path.join(ROOT, "..", "prompts", "_narr_rules.md")
SEASON_CAUTION = os.path.join(ROOT, "postmortem", "mlb_season_improvement_action_20260608.md")
SEASON_SUPPORT = os.path.join(ROOT, "postmortem", "mlb_season_win_reason_action_20260608.md")


def _provider():
    return (os.environ.get("NARR_REASONING_PROVIDER") or "").strip().lower()


def _using_codex_provider():
    return _provider() in {"codex", "gpt", "codex-file", "gpt-file"}


def _codex_scope_suffix():
    """부분 실행 시 codex bridge 파일을 scope별로 분리(historical-note 조건3 A-lite).
    env NARR_CODEX_SCOPE_ID가 있으면 `__{scope}` 접미. 전체 슬레이트면 빈값(기존 파일명 호환)."""
    sid = (os.environ.get("NARR_CODEX_SCOPE_ID") or "").strip()
    return f"__{sid}" if sid else ""


def _codex_reasoning_path(date):
    return _out_path(f"_codex_gpt_reasoning_{date}{_codex_scope_suffix()}.txt")


def _codex_prompt_path(date):
    return _out_path(f"_codex_gpt_prompt_{date}{_codex_scope_suffix()}.md")


def _codex_handoff_path(date):
    return _out_path(f"_codex_gpt_handoff_{date}{_codex_scope_suffix()}.md")


def _write_codex_handoff(date):
    prompt_path = _codex_prompt_path(date)
    reasoning_path = _codex_reasoning_path(date)
    # 부분 실행이면 rerun에 반드시 같은 --gamepks가 붙어야 같은 scope 파일을 소비한다(historical-note 블로커2).
    scope_id = (os.environ.get("NARR_CODEX_SCOPE_ID") or "").strip()
    scope_gp = (os.environ.get("NARR_CODEX_GAMEPKS") or "").strip()
    gp_flag = f" --gamepks {scope_gp}" if scope_gp else ""
    scope_note = (
        f"\nScope: this is a PARTIAL run (scope id `{scope_id}`, target gamePks: {scope_gp}).\n"
        f"You MUST keep the identical `--gamepks {scope_gp}` on rerun, or it will read a different scope file.\n"
        if scope_gp else
        "\nScope: full slate (no --gamepks). Rerun without --gamepks.\n"
    )
    handoff = f"""# Codex Narrator Reasoning Handoff - {date}

Purpose:
- Generate MLB narrator reasoning with Codex/GPT instead of Claude.
- Use only the prompt file below. It already contains pregame-safe facts, salience digest, player-card context, calibrated guard context, and output rules.
{scope_note}
Read:
- `{prompt_path}`

Write:
- `{reasoning_path}`

Strict output contract:
- Include every game in the prompt order.
- Use exactly the narrator block format requested in the prompt.
- WIN_LOSS final token must be `HOME`, `AWAY`, or `-`.
- RUN_TOTAL final token must be `UPPER`, `LOWER`, or `-`.
- Do not use postgame/result information.
- Do not auto flip/remove by rule; infer from the full pregame context.

After writing the reasoning file, rerun the SAME command (keep the --gamepks scope identical):

```powershell
py run_mlb_v3.py --date {date}{gp_flag} --codex-narrator
```
"""
    open(_codex_handoff_path(date), "w", encoding="utf-8").write(handoff)


def _output_dir():
    out = (os.environ.get("NARR_OUTPUT_DIR") or "").strip()
    if not out:
        return ROOT
    os.makedirs(out, exist_ok=True)
    return out


def _out_path(name):
    return os.path.join(_output_dir(), name)


def _ledger_as_of():
    try:
        j = json.load(open(os.path.join(ROOT, "_narr_trajectory_ledger.json"), encoding="utf-8"))
        return j.get("as_of", "?")
    except Exception:
        return "?"


def _env():
    e = dict(os.environ)
    e["PYTHONUTF8"] = "1"
    e["PYTHONIOENCODING"] = "utf-8"
    return e


def _build_run_total_focused_retrospective_context(date, skip_matchups=None):
    """RUN_TOTAL-only date-safe focused retrospective context.

    This is not the old score-based RUN_TOTAL route layer. It mirrors the postgame
    retrospective separation: line result, pregame-knowable UPPER/LOWER factors,
    post-only causes, volatility/noise, and original raw-PBP-backed evidence.
    """
    disabled = (os.environ.get("MLB_RUN_TOTAL_FOCUSED_CONTEXT_DISABLE") or "").strip().lower()
    if disabled in {"1", "true", "yes", "on"}:
        return "(disabled by MLB_RUN_TOTAL_FOCUSED_CONTEXT_DISABLE; RUN_TOTAL-focused context is reasoning-only when active.)"
    try:
        from _mlb_run_total_focused_narrator_context import build_prompt_context

        return build_prompt_context(date, skip_matchups=skip_matchups)
    except Exception as e:
        return f"(RUN_TOTAL focused retrospective context unavailable: {type(e).__name__}: {e})"


def _build_run_total_retro_dominance_context(date, skip_matchups=None):
    """Date-safe RUN_TOTAL same-line-band dominance context.

    This is an ack layer, not a prediction layer. It mirrors the WIN_LOSS signal-ack shape:
    strong RUN_TOTAL retrospective signals must be acknowledged, not obeyed.
    """
    disabled = (os.environ.get("MLB_RUN_TOTAL_RETRO_ACK_CONTEXT_DISABLE") or "").strip().lower()
    if disabled in {"1", "true", "yes", "on"}:
        return "(disabled by MLB_RUN_TOTAL_RETRO_ACK_CONTEXT_DISABLE; RUN_TOTAL retro ack context inactive.)"
    try:
        from _mlb_run_total_retro_dominance_context import build_prompt_context

        return build_prompt_context(date, skip_matchups=skip_matchups)
    except Exception as e:
        return f"(RUN_TOTAL retro dominance context unavailable: {type(e).__name__}: {e})"


def _build_model_defense_context(date, skip_matchups=None):
    """Pregame-only model-defense context for narrator reasoning.

    This does not make predictions. It forces the narrator to defend the official
    model side before overriding it, and to split RUN_TOTAL into UPPER-vs-LOWER routes.
    """
    disabled = (os.environ.get("MLB_MODEL_DEFENSE_CONTEXT_DISABLE") or "").strip().lower()
    if disabled in {"1", "true", "yes", "on"}:
        return "(disabled by MLB_MODEL_DEFENSE_CONTEXT_DISABLE; model-defense context is review-only when active.)"
    try:
        from _narr_model_defense_context import build_prompt_context

        return build_prompt_context(date, skip_matchups=skip_matchups)
    except Exception as e:
        return f"(model-defense context unavailable: {type(e).__name__}: {e})"


def _build_bvp_player_memory_context(date, skip_matchups=None):
    """Date-safe direct batter-vs-probable-starter memory context.

    This is a prompt visibility layer only. It can support or challenge a read
    when corroborated by other pregame facts, but it must not force flips,
    removals, passes, Notification changes, or production/narrator prediction mutation.
    """
    disabled = (os.environ.get("MLB_BVP_CONTEXT_DISABLE") or "").strip().lower()
    if disabled in {"1", "true", "yes", "on"}:
        return "(disabled by MLB_BVP_CONTEXT_DISABLE; BvP player-memory context is research-only when active.)"
    try:
        from _mlb_bvp_narrator_context import build_prompt_context

        return build_prompt_context(date, skip_matchups=skip_matchups, write=True)
    except Exception as e:
        return f"(BvP player-memory context unavailable: {type(e).__name__}: {e})"


def _short_prompt_text(value, max_chars=220):
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 14].rstrip() + " ...[truncated]"


def _model_digest_by_matchup(model_defense_context):
    out = {}
    current = ""
    for raw in (model_defense_context or "").splitlines():
        line = raw.strip()
        if line.startswith("[") and "] gamePk=" in line:
            current = line.split("]", 1)[0].lstrip("[").strip()
            continue
        if current and line.startswith("model:"):
            out[current] = line
            current = ""
    return out


def _build_top_collision_digest(date, t038_signal_card_context, model_defense_context, skip_matchups=None):
    """Compact read-first index for the most conflict-heavy pregame cards.

    This is B-only anti-lost-in-the-middle plumbing: it repeats no new facts
    beyond existing historical-note/model-defense artifacts and must not force pass/flip.
    """
    disabled = (os.environ.get("MLB_TOP_COLLISION_DIGEST_DISABLE") or "").strip().lower()
    if disabled in {"1", "true", "yes", "on"}:
        return "(disabled by MLB_TOP_COLLISION_DIGEST_DISABLE)"
    enabled = (os.environ.get("MLB_TOP_COLLISION_DIGEST_ENABLE") or "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return "(disabled; set MLB_TOP_COLLISION_DIGEST_ENABLE=1 for research B-only runs)"
    skip = {str(x).strip() for x in (skip_matchups or set()) if str(x).strip()}
    model_by_matchup = _model_digest_by_matchup(model_defense_context)
    prompt_view_path = os.path.join(
        ROOT,
        "postmortem",
        "fullread_v4",
        "t038_pregame_signal_cards",
        f"pregame_signal_prompt_view_{date}.json",
    )
    games = []
    try:
        with open(prompt_view_path, encoding="utf-8") as f:
            payload = json.load(f)
        games = list(payload.get("games") or [])
    except Exception:
        games = []

    lines = [
        "[TOP COLLISION DIGEST - READ FIRST]",
        "experiment=true; warn_only=true; source=historical-note+model-defense; no_auto_flip_remove_pass=1; no_prediction_mutation=1.",
        "This is an index, not a risk score. Counts and labels below must not downgrade a prediction, force pass, force model side, or force opponent side.",
        "Decision order: decide WIN_LOSS/RUN_TOTAL from the full slate first; after deciding, use this index only to verify that no listed pregame item was ignored.",
        "If a listed item is not decisive, explicitly discount it in one clause and keep the independent decision.",
        "",
    ]
    added = 0
    for game in games:
        matchup = str(game.get("matchup") or "").strip()
        if not matchup or matchup in skip:
            continue
        prominent = list(game.get("prominent_cards") or [])
        conflict_cards = [
            c for c in prominent
            if c.get("conflicts_with") or "conflict_with_model" in (c.get("tags") or [])
        ]
        high_cards = [
            c for c in prominent
            if str(c.get("strength") or "").upper() in {"L4", "L5"}
        ]
        selected = []
        seen = set()
        for card in conflict_cards + high_cards + prominent:
            sid = str(card.get("signal_id") or id(card))
            if sid in seen:
                continue
            seen.add(sid)
            selected.append(card)
            if len(selected) >= 5:
                break
        model_line = model_by_matchup.get(matchup, "")
        if not selected and not model_line:
            continue
        added += 1
        lines.append(
            f"[{matchup}] gamePk={game.get('gamePk')} "
            f"prominent={len(prominent)} conflict={len(conflict_cards)} high={len(high_cards)}"
        )
        if model_line:
            lines.append(f"- MODEL: {_short_prompt_text(model_line, 260)}")
        for card in selected:
            tags = ",".join(str(x) for x in (card.get("tags") or [])) or "-"
            conflicts = "; ".join(str(x) for x in (card.get("conflicts_with") or []))
            summary = _short_prompt_text(card.get("summary"), 220)
            line = f"- INDEX: {card.get('category')} strength={card.get('strength')} tags={tags}: {summary}"
            lines.append(line)
            if conflicts:
                lines.append(f"  model_diff_refs={_short_prompt_text(conflicts, 180)}; lookup_only_not_veto=1")
        lines.append("")
    if not added:
        lines.append("- no conflict/high-priority cards after scope filtering.")
    return "\n".join(lines).strip()


def _build_t038_pregame_signal_card_context(date, skip_matchups=None):
    """Build the historical-note compact signal-card prompt view.

    historical-note is warn-only. It surfaces already-built pregame facts so the narrator
    can explicitly engage prominent conflicts/chalk without any hard gate,
    automatic flip, removal, pass, or Notification mutation.
    """
    disabled = (os.environ.get("MLB_T038_SIGNAL_CARDS_DISABLE") or "").strip().lower()
    if disabled in {"1", "true", "yes", "on"}:
        return "(disabled by MLB_T038_SIGNAL_CARDS_DISABLE; historical-note signal cards are warn-only when active.)"
    try:
        from _mlb_pregame_signal_cards import (
            OUT_ROOT,
            build_cards,
            build_prompt_view,
            summarize,
            write_json,
            write_jsonl,
            write_markdown,
        )

        cards, metadata = build_cards(date, gamepks=None)
        summary = summarize(cards, date, metadata)
        prompt_view, display_log = build_prompt_view(cards, date, metadata)

        os.makedirs(str(OUT_ROOT), exist_ok=True)
        cards_path = OUT_ROOT / f"pregame_signal_cards_{date}.jsonl"
        summary_path = OUT_ROOT / f"pregame_signal_cards_summary_{date}.json"
        prompt_view_path = OUT_ROOT / f"pregame_signal_prompt_view_{date}.json"
        display_log_path = OUT_ROOT / f"pregame_signal_display_log_{date}.json"
        md_path = OUT_ROOT / f"pregame_signal_cards_report_{date}.md"
        write_jsonl(cards_path, cards)
        write_json(summary_path, summary)
        write_json(prompt_view_path, prompt_view)
        write_json(display_log_path, display_log)
        write_markdown(summary, prompt_view, md_path)

        skip = {str(x).strip() for x in (skip_matchups or set()) if str(x).strip()}
        games = [g for g in prompt_view.get("games") or [] if str(g.get("matchup") or "").strip() not in skip]
        prominent_total = sum(int(g.get("prominent_count") or 0) for g in games)
        max_prominent = max((int(g.get("prominent_count") or 0) for g in games), default=0)

        lines = [
            "[historical-note PREGAME SIGNAL CARD VIEW]",
            "warn_only=true; enforce_must_engage=0; no_auto_flip_remove_pass=1; no_prediction_mutation=1.",
            "Purpose: prevent pregame-known signals from being buried. Use or reject the cards in reasoning, but do not treat them as commands.",
            "If a prominent historical-note card changes or materially challenges your WIN_LOSS/RUN_TOTAL judgment, mention t038-engage: in the relevant WIN_LOSS/run total baseline. This is logging only, not a hard validator.",
            f"date={date}; games={len(games)}; prominent_cards={prominent_total}; max_prominent_per_game={max_prominent}; artifact={prompt_view_path}",
            "",
        ]
        for game in games:
            matchup = str(game.get("matchup") or "").strip()
            prominent = list(game.get("prominent_cards") or [])
            conflict_count = sum(1 for c in prominent if c.get("conflicts_with") or "conflict_with_model" in (c.get("tags") or []))
            market_count = sum(1 for c in prominent if c.get("model_chalk_tag") == "market_chalk")
            high_count = sum(1 for c in prominent if str(c.get("strength") or "").upper() in {"L4", "L5"})
            lines.append(
                f"[{matchup}] gamePk={game.get('gamePk')} prominent={len(prominent)} "
                f"conflict_cards={conflict_count} market_chalk_cards={market_count} high_strength_cards={high_count}"
            )
            lines.append(
                "WARNING LINE: conflict/chalk/prominent cards must be considered, but warn-only means no forced prediction, no forced pass, no hard repair."
            )
            if prominent:
                lines.append("PROMINENT:")
                for card in prominent[:8]:
                    tags = ",".join(str(x) for x in (card.get("tags") or [])) or "-"
                    conflicts = "; ".join(str(x) for x in (card.get("conflicts_with") or []))
                    lines.append(
                        f"- {card.get('category')} {card.get('treatment')}/{card.get('strength')} "
                        f"tags={tags}: {_short_prompt_text(card.get('summary'), 240)}"
                    )
                    if conflicts:
                        lines.append(f"  conflicts={_short_prompt_text(conflicts, 220)}")
                    excerpts = [x for x in (card.get("excerpts") or []) if str(x).strip()]
                    if excerpts:
                        lines.append(f"  excerpt={_short_prompt_text(excerpts[0], 220)}")
                if len(prominent) > 8:
                    lines.append(f"- ... {len(prominent) - 8} more prominent cards omitted from prompt view; see artifact.")
            compact = game.get("compact_by_category") or {}
            if compact:
                lines.append("COMPACT CATEGORY SUMMARY:")
                for cat in sorted(compact):
                    vals = [_short_prompt_text(x, 120) for x in (compact.get(cat) or [])[:2]]
                    if vals:
                        lines.append(f"- {cat}: {' | '.join(vals)}")
            lines.append("")
        return "\n".join(lines).strip()
    except Exception as e:
        return f"(historical-note pregame signal-card context unavailable: {type(e).__name__}: {e})"


def _build_pregame_evidence_matrix_context(date, skip_matchups=None):
    """Compact Evidence Matrix view for anti-burial checking.

    The matrix keeps every historical-note card in a sidecar artifact, but only promotes
    the most important/conflicting cards into a compact prompt checklist. It is
    research-only when explicitly enabled and must not force pass/flip/remove
    or mutate predictions.
    """
    disabled = (os.environ.get("MLB_EVIDENCE_MATRIX_DISABLE") or "").strip().lower()
    if disabled in {"1", "true", "yes", "on"}:
        return "(disabled by MLB_EVIDENCE_MATRIX_DISABLE; evidence matrix is warn-only when active.)"
    enabled = (os.environ.get("MLB_EVIDENCE_MATRIX_ENABLE") or "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return "(disabled by default; paired replay showed pre-prediction Evidence Matrix shrink risk. Set MLB_EVIDENCE_MATRIX_ENABLE=1 for research replay only.)"
    try:
        from _mlb_pregame_evidence_matrix import OUT_ROOT, build_matrix, matrix_prompt_context, output_paths, write_json, write_markdown

        matrix = build_matrix(date)
        prompt_text = matrix_prompt_context(matrix, skip_matchups=skip_matchups)
        paths = output_paths(date, out_dir=OUT_ROOT)
        os.makedirs(str(OUT_ROOT), exist_ok=True)
        write_json(paths["matrix"], matrix)
        with open(paths["prompt"], "w", encoding="utf-8", newline="\n") as f:
            f.write(prompt_text + "\n")
        write_markdown(matrix, prompt_text, paths["report"])
        return prompt_text
    except Exception as e:
        return f"(pregame evidence matrix unavailable: {type(e).__name__}: {e})"


def _build_dual_layer_judgment_context(date, skip_matchups=None):
    """Date-safe narrator no-miss + judgment-criteria context.

    This is a compact prompt visibility layer. It does not create predictions, force
    pass/flip/remove, mutate narrator output, or send Notification. The full static
    pack is used only when its source max date is strictly before the target
    date; otherwise it disables itself to avoid replay leakage.
    """
    disabled = (os.environ.get("MLB_DUAL_LAYER_JUDGMENT_DISABLE") or "").strip().lower()
    if disabled in {"1", "true", "yes", "on"}:
        return "(disabled by MLB_DUAL_LAYER_JUDGMENT_DISABLE; dual-layer context inactive.)"
    path = os.path.join(
        ROOT,
        "postmortem",
        "raw_pbp_fullflow",
        "judgment_criteria",
        "final_judgment_criteria",
        "mlb_narrator_dual_layer_prompt_pack_2026.json",
    )
    if not os.path.exists(path):
        return "(dual-layer judgment context unavailable: pack artifact missing.)"
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        meta = payload.get("meta") or {}
        source_max = str(meta.get("source_date_max") or "").strip()
        if source_max and source_max >= str(date):
            return (
                f"(dual-layer judgment context disabled for date safety: "
                f"source_date_max={source_max} is not before target={date}.)"
            )

        lines = [
            "[NARRATOR NO-MISS + JUDGMENT CRITERIA]",
            "warn_only=true; source=rawPBP judgment criteria; no_auto_flip_remove_pass=1; no_prediction_mutation=1.",
            "Purpose: two separate checks. Layer 1 prevents burying pregame evidence. Layer 2 tells how to judge visible evidence.",
            "Use inside WIN_LOSS/RUN_TOTAL reasoning only when material. Do not add new output lines, do not force pass, and do not obey this as a filter.",
            f"source_date_max={source_max}; artifact={path}",
            "",
            "LAYER 1 - MUST-NOT-BURY EVIDENCE:",
        ]
        cards = payload.get("layer_1_must_not_bury", {}).get("cards") or []
        for card in cards[:12]:
            lines.append(
                "- "
                f"{card.get('handle')}: {_short_prompt_text(card.get('label'), 80)}; "
                f"support={card.get('case_support')}; roles={card.get('roles_by_support')}; "
                f"check={_short_prompt_text(card.get('narrator_must_not_bury'), 220)}"
            )

        lines.extend(
            [
                "",
                "LAYER 2 - JUDGMENT CRITERIA SUMMARY:",
                "Exact match is not required. If route family + one core handle overlaps, treat as tier-2 observation only.",
            ]
        )
        roles = payload.get("layer_2_judgment_criteria", {}).get("roles") or {}
        for role in ("WIN_LOSS_WIN", "WIN_LOSS_LOSS", "RUN_TOTAL_UPPER", "RUN_TOTAL_LOWER"):
            role_cards = list(roles.get(role) or [])[:4]
            if not role_cards:
                continue
            lines.append(f"{role}:")
            for card in role_cards:
                combo = "; ".join(
                    _short_prompt_text(x.get("label"), 55)
                    for x in (card.get("pregame_combo") or [])[:4]
                    if str(x.get("label") or "").strip()
                )
                route = "; ".join(
                    _short_prompt_text(x.get("label"), 45)
                    for x in (card.get("route_to_inspect") or [])[:3]
                    if str(x.get("label") or "").strip()
                )
                lines.append(
                    f"- {card.get('criterion_id')} cases={card.get('cases')}: "
                    f"pregame=[{combo}] -> inspect=[{route}]"
                )

        gap = payload.get("layer_3_gap_controls") or {}
        counts = gap.get("mature_classification_counts") or gap.get("classification_counts") or {}
        if counts:
            lines.extend(
                [
                    "",
                    "LAYER 3 - KNOWN GAP CONTROLS:",
                    f"gap_counts={counts}",
                    "RUN_TOTAL_UPPER handle extraction is the weakest area: require concrete run source before trusting an UPPER read.",
                    "Post-only/noise remains quarantined unless repeated into date<target player/team memory.",
                ]
            )
        return "\n".join(lines).strip()
    except Exception as e:
        return f"(dual-layer judgment context unavailable: {type(e).__name__}: {e})"


def _build_correct_judgment_standards_context(date, skip_matchups=None):
    """Partial raw-PBP correct-judgment standards context.

    This layer is only worked examples from already completed date<target reads.
    It is not a filter and cannot mutate predictions.
    """
    disabled = (os.environ.get("MLB_CORRECT_JUDGMENT_CONTEXT_DISABLE") or "").strip().lower()
    if disabled in {"1", "true", "yes", "on"}:
        return "(disabled by MLB_CORRECT_JUDGMENT_CONTEXT_DISABLE; partial correct-judgment context inactive.)"
    script = os.path.join(ROOT, "_mlb_correct_judgment_standards_context.py")
    if not os.path.exists(script):
        return "(partial correct-judgment context unavailable: helper missing.)"
    try:
        gp = (os.environ.get("NARR_CODEX_GAMEPKS") or "").strip()
        cmd = [sys.executable, script, "--date", date, "--write", "--max-cases", "18"]
        if gp:
            cmd.extend(["--gamepks", gp])
        cp = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=90,
            env=os.environ.copy(),
            cwd=ROOT,
        )
        text = (cp.stdout or "").strip()
        if cp.returncode != 0:
            return f"(partial correct-judgment context unavailable: helper rc={cp.returncode}; stderr={(cp.stderr or '')[-240:]})"
        return text or "(partial correct-judgment context unavailable: helper returned empty output.)"
    except Exception as e:
        return f"(partial correct-judgment context unavailable: {type(e).__name__}: {e})"


def _ensure_news(date, env, timeout=420, max_age_h=4):
    """비구조 뉴스 캐시 보장(멱등·best-effort). 없거나 stale(>max_age_h)면 _narr_news.py 재실행.
    확정 라인업 재실행 시 fresh 뉴스 반영 (단 빠른 재실행은 캐시 재사용 — 90 웹서치 절약).
    실패해도 무시 — narr_context는 캐시 없으면 [뉴스] 줄 생략(폴백)."""
    import time
    cache = os.path.join(ROOT, f"_narr_news_{date}.json")
    if os.path.exists(cache):
        age_h = (time.time() - os.path.getmtime(cache)) / 3600
        if age_h < max_age_h:
            return                       # 신선 → 재사용
        # stale → 아래서 재fetch
    try:
        subprocess.run([sys.executable, os.path.join(ROOT, "_narr_news.py"), date],
                       capture_output=True, text=True, encoding="utf-8",
                       timeout=timeout, env=env, cwd=ROOT)
    except Exception:
        pass


def _prewarm(date):
    """narr_context가 쓰는 statsapi 캐시(il_map ~35s + regulars ~160s) 콜드면 미리 생성.
    이걸 안 하면 narr_context 첫 산출이 ~200s 걸려 ctx_timeout 초과→None(라이브 네레이터 사망).
    statsapi만·claude토큰0·비치명(실패해도 ctx_timeout이 흡수)."""
    try:
        sys.path.insert(0, ROOT)
        import _mlb_recent_lineups as _rl
        _rl.il_map(date); _rl.regulars(date)
    except Exception:
        pass


def _filter_games(text, skip):
    """확정(frozen) 경기 블록을 ctx/priors에서 제거 — '[Away @ Home]' 헤더 매칭. skip=matchup 문자열 set."""
    if not skip:
        return text
    import re
    parts = re.split(r"(?m)(?=^\[[^\]\n]+ @ [^\]\n]+\])", text or "")
    out = []
    for p in parts:
        m = re.match(r"^\[([^\]\n]+ @ [^\]\n]+)\]", p)
        if m and m.group(1).strip() in skip:
            continue
        out.append(p)
    return "".join(out)


def _read_short(path, max_chars=9000):
    try:
        txt = open(path, encoding="utf-8").read().strip()
    except Exception:
        return ""
    if len(txt) <= max_chars:
        return txt
    return txt[:max_chars].rstrip() + "\n...[truncated]"


def _truthy(v):
    if isinstance(v, bool):
        return v
    try:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return False
    except Exception:
        pass
    return str(v).strip().lower() in {"true", "1", "yes", "y"}


def _f(v, default=None):
    try:
        if v is None:
            return default
        x = float(v)
        if math.isnan(x):
            return default
        return x
    except Exception:
        return default


def _team_loc(row, team):
    if team == row.get("home_team"):
        return "HOME"
    if team == row.get("away_team"):
        return "AWAY"
    return "-"


def build_season_rule_signals(date, skip_matchups=None):
    """Return per-slate support/caution marks from season postmortem mining.

    These are review marks only: they tell the narrator why a prediction can win and
    why it can fail, but they never force a flip or hard threshold.
    """
    try:
        import pandas as pd
    except Exception:
        return ""
    path = (
        os.environ.get("MLB_C3_V3_PATH")
        or os.environ.get("NARR_V3_INPUT_PATH")
        or os.path.join(ROOT, f"_v3_games_{date}.csv")
    )
    if not os.path.exists(path):
        return ""
    try:
        df = pd.read_csv(path)
    except Exception:
        return ""
    lines = [
        "시즌 회고 support/caution 마크 — 자동 산출(예측 강제 아님).",
        "각 경기에서 '왜 이길 수 있나'와 '왜 틀릴 수 있나'를 둘 다 확인하고, 최종 예측은 사전근거로만 판단한다.",
    ]
    for _, r in df.iterrows():
        matchup = f"{r.get('away_team')} @ {r.get('home_team')}"
        if skip_matchups and matchup in skip_matchups:
            continue
        support = []
        caution = []
        win_loss_zone = str(r.get("win_loss_zone", "") or "")
        prediction_team = r.get("prediction_team")
        prediction_loc = _team_loc(r, prediction_team)
        prediction_p = _f(r.get("prediction_p"))
        win_loss_implied = _f(r.get("win_loss_implied"), _f(r.get("win_implied")))
        prediction_market_price = _f(r.get("prediction_market_price"))
        lop_gap = _f(r.get("lop_gap"))
        lop_delta = _f(r.get("prediction_lop_delta"))
        win_loss_loss_score = _f(r.get("win_loss_loss_score"))
        run_total_side = str(r.get("run_total_prediction_side", "") or "").upper()
        run_total_baseline = _f(r.get("run_total_baseline"))
        temp_f = _f(r.get("temperature"))
        temp_c = _f(r.get("temp_c"))
        park = _f(r.get("park_factor"))
        dome = _f(r.get("dome"), 0)
        ump_runs = _f(r.get("ump_avg_runs"), _f(r.get("hp_ump_runs_mean")))

        if win_loss_zone in {"clean", "super"}:
            support.append(f"WIN-LOSS-CLEAN({win_loss_zone})")
        strong_win_loss = win_loss_zone == "super" or _truthy(r.get("win_loss_strong_combo"))
        close_win_loss = _truthy(r.get("is_close_call")) or (prediction_p is not None and prediction_p < 0.55)
        if strong_win_loss:
            support.append("WIN-LOSS-STRONG")
        if _truthy(r.get("is_value")):
            support.append("WIN-LOSS-SELECTION")
        if lop_gap is not None and lop_gap >= 0.05:
            support.append(f"WIN-LOSS-LINEUP-ADV(lop_gap={lop_gap:+.3f})")
        if lop_delta is not None and lop_delta >= 1.00:
            support.append(f"WIN-LOSS-LINEUP-JUMP(delta={lop_delta:+.2f})")
        if prediction_market_price is not None and prediction_market_price >= 2.0:
            support.append(f"WIN-LOSS-UNDERDOG(market_price={prediction_market_price:.2f})")
        if strong_win_loss and lop_delta is not None and lop_delta >= 1.00:
            support.append("WIN-LOSS-ELITE-JUMP")
        if strong_win_loss and lop_gap is not None and lop_gap >= 0.05:
            support.append("WIN-LOSS-ELITE-ALIGN")
        if _truthy(r.get("is_value")) and win_loss_implied is not None and win_loss_implied >= 0.60:
            support.append("WIN-LOSS-SELECTION-FAV")
        if strong_win_loss and not close_win_loss:
            support.append("WIN-LOSS-STRONG-NOTCLOSE")

        if close_win_loss:
            close_s = f"{prediction_p:.0%}" if prediction_p is not None else "flag"
            caution.append(f"WIN_LOSS-CC(close {close_s})")
        if lop_delta is not None and lop_delta <= -0.50:
            caution.append(f"WIN_LOSS-LD(lineup_drop={lop_delta:+.2f})")
        if lop_gap is not None and lop_gap <= -0.05:
            caution.append(f"WIN_LOSS-OPS(lop_gap={lop_gap:+.3f})")
        if close_win_loss and lop_delta is not None and lop_delta <= -0.50:
            caution.append("WIN_LOSS-RED-CLOSE-DROP")
        if close_win_loss and lop_gap is not None and lop_gap <= -0.05:
            caution.append("WIN_LOSS-RED-CLOSE-OPS")
        if close_win_loss and win_loss_loss_score is not None and win_loss_loss_score >= 2:
            caution.append(f"WIN_LOSS-RED-CLOSE-LOSSSTACK({win_loss_loss_score:g})")
        if close_win_loss and win_loss_implied is not None and win_loss_implied < 0.50:
            caution.append(f"WIN_LOSS-RED-CLOSE-DOG({win_loss_implied:.0%})")

        if _truthy(r.get("run_total_is_value")):
            support.append("WIN-RUN_TOTAL-SELECTION")
        if _truthy(r.get("p7_mk_sp_barrel_min")):
            support.append("WIN-RUN_TOTAL-BARREL")
        if run_total_side == "UPPER" and run_total_baseline is not None and run_total_baseline <= 8.0:
            support.append(f"WIN-RUN_TOTAL-LOWOVER(line={run_total_baseline:g})")
        if run_total_side == "LOWER" and ((dome == 1) or (temp_c is not None and temp_c <= 20)):
            support.append("WIN-RUN_TOTAL-COOLUNDER")

        if run_total_side == "LOWER" and (_truthy(r.get("mk_run_total_wind_lower_risk")) or _truthy(r.get("run_total_wind_risk"))):
            caution.append("RUN_TOTAL-WIND(LOWER wind risk)")
        if run_total_side == "LOWER" and run_total_baseline is not None and run_total_baseline >= 10.0:
            caution.append(f"RUN_TOTAL-HIGHU(LOWER {run_total_baseline:g})")
        hot = (temp_c is not None and temp_c >= 26) or (temp_f is not None and temp_f >= 80)
        if run_total_side == "LOWER" and run_total_baseline is not None and run_total_baseline >= 10.0 and park is not None and park >= 105 and dome != 1 and hot:
            extra = []
            if park is not None:
                extra.append(f"park={park:g}")
            if temp_f is not None:
                extra.append(f"{temp_f:g}F")
            if ump_runs is not None:
                extra.append(f"ump_runs={ump_runs:.1f}")
            caution.append("RUN_TOTAL-HIGHU-ENV(" + ",".join(extra) + ")")

        run_total_baseline_s = f"{run_total_baseline:g}" if run_total_baseline is not None else "?"
        lines.append(
            f"[{matchup}] model_WIN_LOSS={prediction_loc}:{prediction_team} model_RUN_TOTAL={run_total_side} {run_total_baseline_s}\n"
            f"  지원: {', '.join(support) if support else '-'}\n"
            f"  경고: {', '.join(caution) if caution else '-'}"
        )
    return "\n".join(lines)


def build_reasoning(date, ctx_timeout=300, llm_timeout=600, skip_matchups=None):
    """네레이터 추론 *본문 기준점들*(헤더 없음) 반환. 실패 시 None.
    skip_matchups: 확정·프리즈된 경기('Away @ Home' 문자열 set) → claude -p에서 제외(미확정만 빠르게 재추론).
    이미지 렌더(_narr_image) / 텍스트 폴백 양쪽이 이걸 쓴다."""
    try:
        env = _env()
        if os.environ.get("NARR_SKIP_NEWS") != "1":
            _ensure_news(date, env)            # 0) 비구조 뉴스 prefetch (멱등, ~3.5min, 캐시 있으면 즉시)
        if os.environ.get("NARR_SKIP_PREWARM") != "1":
            _prewarm(date)                     # 0.5) statsapi 캐시 예열 (콜드 narr_context ctx_timeout 사망 방지)
        # 1) 사실 고정 컨텍스트 (deterministic)
        fc = subprocess.run([sys.executable, CTX, date], capture_output=True, text=True,
                            encoding="utf-8", timeout=ctx_timeout, env=env, cwd=ROOT)
        ctx = (fc.stdout or "").strip()
        if not ctx or ctx.startswith("[ERR"):
            print(f"[narr_reasoning] narr_context 실패 rc={fc.returncode} len={len(ctx)} "
                  f"head={ctx[:80]!r} stderr={(fc.stderr or '')[-200:]!r} → 수치 폴백", file=sys.stderr)
            return None
        # 1.5) 누적 회고 사전지식 (선수별 직전 read) — 만든 거 다 적용
        priors = ""
        try:
            subprocess.run([sys.executable, os.path.join(ROOT, "_narr_priors.py"), date],
                           capture_output=True, timeout=120, env=env, cwd=ROOT)
            pf = os.path.join(ROOT, f"_narr_priors_{date}.txt")
            if os.path.exists(pf):
                priors = open(pf, encoding="utf-8").read().strip()
        except Exception:
            priors = ""
        # 1.55) Per-game salience map. This surfaces already-known facts and
        # cumulative date<target reads before the long context, without changing predictions.
        salience = ""
        try:
            subprocess.run([sys.executable, os.path.join(ROOT, "_narr_salience_digest.py"), date, "--write"],
                           capture_output=True, timeout=120, env=env, cwd=ROOT)
            sf = os.path.join(ROOT, f"_narr_salience_digest_{date}.txt")
            if os.path.exists(sf):
                salience = open(sf, encoding="utf-8").read().strip()
        except Exception:
            salience = ""
        # 1.6) 확정 경기 제외(미확정만 빠르게 재추론) — ctx·priors에서 frozen 경기 블록 제거
        if skip_matchups:
            ctx = _filter_games(ctx, skip_matchups)
            priors = _filter_games(priors, skip_matchups)
            salience = _filter_games(salience, skip_matchups)
            import re as _re2
            if not _re2.search(r"(?m)^\[[^\]\n]+ @ [^\]\n]+\]", ctx):
                print(f"[narr_reasoning] 미확정 경기 0 — claude -p 생략(전부 frozen)", file=sys.stderr)
                return None
        rules = open(RULES, encoding="utf-8").read()
        season_support = _read_short(SEASON_SUPPORT)
        season_caution = _read_short(SEASON_CAUTION)
        season_signals = build_season_rule_signals(date, skip_matchups)
        try:
            from _mlb_narrator_guard_context import build_prompt_context

            calibrated_guard_context = build_prompt_context(date, skip_matchups=skip_matchups)
        except Exception as e:
            calibrated_guard_context = f"(calibrated guard context unavailable: {type(e).__name__}: {e})"
        model_defense_context = _build_model_defense_context(date, skip_matchups=skip_matchups)
        t038_signal_card_context = _build_t038_pregame_signal_card_context(date, skip_matchups=skip_matchups)
        evidence_matrix_context = _build_pregame_evidence_matrix_context(date, skip_matchups=skip_matchups)
        dual_layer_judgment_context = _build_dual_layer_judgment_context(date, skip_matchups=skip_matchups)
        correct_judgment_standards_context = _build_correct_judgment_standards_context(date, skip_matchups=skip_matchups)
        bvp_player_memory_context = _build_bvp_player_memory_context(date, skip_matchups=skip_matchups)
        run_total_focused_retrospective_context = _build_run_total_focused_retrospective_context(date, skip_matchups=skip_matchups)
        run_total_retro_dominance_context = _build_run_total_retro_dominance_context(date, skip_matchups=skip_matchups)
        top_collision_digest = _build_top_collision_digest(
            date,
            t038_signal_card_context,
            model_defense_context,
            skip_matchups=skip_matchups,
        )

        prompt = f"""너는 MLB 네레이터 정독 분석가다. [룰북]의 *읽기 원칙*을 [사실](직전폼·날씨·심판·부상)과
[누적회고](선수별 직전 경기 read + deepread_v2 사전근거)에 적용해, 각 경기의 RUN_TOTAL/WIN_LOSS 예측을 *직접* 추론·도출한다. 모델예측은 명령이 아니지만, [MODEL DEFENSE]의 모델 근거는 반드시 방어/반박하고 결론을 낸다.

⛔ 절대 규칙:
- 이 단계에서는 수익률 논의 대신 주어진 사실로 경기 내용을 설명하라.
- separate narrator lane: 모델 예측 본문은 덮지 않는다. 대신 네레이터 독립 예측을 별도 섹션·별도 JSON으로 저장하고 결과를 채점한다.
- **사전근거 필수**: pregame 예측 근거는 경기 전에 알 수 있던 사실만 쓴다. [누적회고]의 deepread_v2 블록은 timing=pre_knowable만 포함되어 있다. post_only/noise를 예측 근거로 쓰지 마라.
- deepread_v2 근거ID(`drv2:...`)가 WIN_LOSS/RUN_TOTAL 판단에 직접 쓰이면 해당 WIN_LOSS/RUN_TOTAL 근거문 안에 그 ID를 그대로 포함하라. 근거ID가 없는 기존 누적회고 문장은 구체 날짜·선수·메커니즘을 인용하라.
- **사전 기준/체크리스트/lean 점수/임계값 금지.** [사실]의 각 선수 *직전 폼 시퀀스*를 보고 양팀 궤적을 *통째로* 읽어 어느 쪽이 우세한지·뭐가 불확실한지 스스로 판단하라. [룰북]은 임계값표가 아니라 '어떻게 읽나' 원칙이다.
- **guard-review 필수**: [CALIBRATED HISTORICAL GUARD CONTEXT]에 `hard_guard_review`가 있는 경기에서 WIN_LOSS을 HOME/AWAY로 유지하면, WIN_LOSS 근거문 안에 literal `guard-review:`를 쓰고 상대 타자/라인업/불펜 경고를 왜 넘길 수 있는지 직접 설명하라. 설명 못 하면 WIN_LOSS=`-`.
- **signal-ack 필수**: [CALIBRATED HISTORICAL GUARD CONTEXT]에 `must_review`가 있는 WIN_LOSS/RUN_TOTAL 신호의 stated condition이 걸리면, 반드시 같은 axis line(WIN_LOSS line 또는 run total baseline)에 literal `signal-ack: <gate>`를 쓰고 그 신호를 사용/기각/할인했는지 설명하라. ack는 예측 명령이 아니라 silent burial 방지 표시다.
- **run_total-retro-ack 필수**: [RUN_TOTAL RETRO DOMINANCE ACK CONTEXT]에 `ack_required=true`가 있는 경기의 RUN_TOTAL 근거문에는 literal `run_total-retro-ack:`와 해당 `ack_id`를 쓰고, 그 같은 기준점대 회고 우세 신호를 오늘 사용/기각/패스했는지 설명하라. ack는 예측 명령이 아니라 silent burial 방지 표시다.
- **risk-resolution 필수**: WARN_STRONG_MANUAL_REVIEW 경기에서 WIN_LOSS 근거문 안에 `경고`, `WIN_LOSS-CC`, `WIN_LOSS-OPS`, `얇지만`, `우위가 묽다/얇다`처럼 스스로 위험을 인정하면서도 HOME/AWAY를 유지하면, 같은 WIN_LOSS 근거문 안에 literal `risk-resolution:`을 쓰고 왜 그 위험이 최종 WIN_LOSS을 꺾지 못하는지 구체적으로 해결하라. 해결 못 하면 WIN_LOSS=`-`.
- **model-pass-defense 필수**: 공식 모델 WIN_LOSS이 HOME/AWAY이고 네레이터가 WIN_LOSS=`-`로 패스하면, WIN_LOSS 근거문 안에 literal `model-pass-defense:`를 쓰고 모델이 본 구체 feature/support(예: away_bat_roll14_csw 같은 모델근거명)와 그걸 패스할 만큼 강한 사전 충돌을 같이 설명하라. "양쪽 리스크"만 쓰면 부족하다.
- **model-defense 내용 필수**: 모델과 반대 WIN_LOSS을 내면 literal `model-defense:`만 쓰지 말고, 모델이 본 구체 feature/support를 무엇으로 기각했는지와 상대 승리 루트를 같이 써라. 모델을 자동으로 따르라는 뜻이 아니라 silent burial 금지다.
- **opponent-route-review 필수**: historical-note/모델방어 문맥에서 모델팀에 여러 WIN_LOSS conflict가 보이면, HOME/AWAY를 내기 전 WIN_LOSS 근거문에 literal `opponent-route-review:`를 쓰고 모델 유지 루트와 상대 승리 루트를 비교하라. 이 토큰은 예측 명령이 아니라 비교 기록이다.
- **⭐ 신호 신뢰도 위계 (전 시즌 실측, 이게 저울의 비중이다):** ① **선발 붕괴 연속 = 제일 단단(+21pp 지속)** ② 선발 호투 연속 = 약신호(+6pp) ③ 불펜 가용·결장·IL·첫등판 = 구조적 사실 ④ **타자 단발 핫/콜드 = 노이즈**. 단, player-mapped rawPBP 카드에서 시즌 구간+최근 경기 정타/장타/타구질이 같은 방향으로 반복되고 라인업/좌우 매치업 경고와 겹치면, 그것은 “타자 폼”이 아니라 **라인업 손상/상대타자 위험 클러스터**로 읽는다. 이 위계는 읽기 calibration일 뿐, 신호→예측/skip/점수 매핑 금지.
- **⚠️ 타자 일별 폼 단독 금지, 클러스터는 필수 확인**: 직전 2+연속 락인만으로 예측을 기울이지 마라. 하지만 특정 타자의 시즌+최근 rawPBP 정타 흐름, 상대 선발의 HR/정타 허용 궤적, 라인업 OPS/좌우 split, 결장/휴식이 함께 같은 방향이면 WIN_LOSS/RUN_TOTAL 위험으로 반드시 다룬다. 오늘 같은 구조에서 hitter/lineup cluster를 선발 한 줄 근거 아래 묻지 마라.
- **투수 궤적 = 무겁게**: 선발 시즌흐름 시퀀스에 **붕괴 연속(붕붕붕)이면 강신호**(구위·제구 실재·지속), 호투 연속은 약신호, 진동형(호붕호붕)은 고변수=불확실. 불펜 연속 불안·연투 이탈은 구조 신호. 첫등판(신인·외국·복귀)=불확실 고변수.
- **favorite 우위가 묽은 구조면 확신 낮춰 중립**: 자팀 선발 불확실(짧음·첫등판·**붕괴 연속**)·자팀 불펜 뒷문 약·상대 에이스. "favorite 진다"가 아니라 "우위가 묽다". (*"자팀 타선 콜드라서"는 노이즈라 근거 금지.*) 단발 blown·완봉 blowout은 *변수*라 중립화 근거 아님.
- **RUN_TOTAL는 WIN_LOSS과 *같은 깊이*로 따로 읽되, 동력은 *양 선발 붕괴 궤적 + 불펜/이닝 구조*다(타선 일별 폼 아님).** *양* 선발이 붕괴 궤적+양 불펜 얇으면 UPPER, 양 선발 안정·깊으면 LOWER. **⚠️ 타자 핫/콜드로 RUN_TOTAL 읽지 마라 — 노이즈다(콜드 타선도 그냥 친다).** **한 선발만 붕괴=WIN_LOSS 신호(blowout, 총합 낮음), 양 선발 붕괴라야 RUN_TOTAL UPPER.** 구조는 *방향*이지 크기 보장 아님(높은 기준점은 미달 가능). 진짜 평범할 때만 `-`.
- **RUN_TOTAL·WIN_LOSS 둘 다 매 경기 판단하라. RUN_TOTAL만 보고 WIN_LOSS을 흘리지 마라.** WIN_LOSS은 선발·불펜·타선 궤적을 저울질해 우세 방향을 낸다 — 중립 남발 금지(진짜 팽팽하거나 묽은 구조일 때만 ⚪).
- 반드시 [사실]의 구체 궤적(직전 폼 시퀀스·실시간 결장/불펜)을 인용. 감으로 말하지 마라.
- **[누적회고]의 선발 *시즌흐름 시퀀스*(호호붕호평…)와 직전 read를 통째로 읽어라.** 컷·flag·"우선해라" 명령 없다 — 그 투수가 시즌을 *어떻게 흘러왔나*를 눈으로 보고 판단: 연속 붕괴 추세면 약하게, 꾸준 호투면 강하게, **호투-붕괴 번갈이 진동형이면 고변수=불확실(어느 쪽도 단정 금지)**. read에 정타·홈런 피격이 *반복*되면 표면 우위(favorite·핫타선)에 묻지 말고 같이 저울에 올려라 — 단 이것도 통째 읽기지 강제 규칙이 아니다. (검증: 적중한 날은 이 신호를 적용했고, 빗나간 날은 상대 선발 반복붕괴를 표면우위로 눌러서 틀렸다 — Burrows/Vásquez/Sheehan류 반복 피격을 표면으로 덮지 마라.)
- **시즌 회고 support/caution은 '왜 이길 수 있나 / 왜 틀릴 수 있나'를 빠뜨리지 않기 위한 표시다.** 절대 hard flip·점수·임계값으로 쓰지 마라. 같은 경기에서 support와 caution이 같이 있으면 둘 다 본 뒤 야구 근거로 결론을 낸다.
- `WIN-*` 마크는 보존해야 할 승리 구조, `WIN_LOSS-*`/`RUN_TOTAL-*` 마크는 주의 구조다. 관련 마크가 있으면 WIN_LOSS/RUN_TOTAL 근거문에 짧게 포함하라(예: 지원 WIN-LOSS-CLEAN, 경고 RUN_TOTAL-HIGHU). 단, postgame-only bucket은 사전예측 근거 금지.
- 매 경기 WIN_LOSS과 RUN_TOTAL 각각에 대해 “이 예측이 맞을 이유”와 “틀릴 위험”을 동시에 검토하라. 최종 한 줄 근거에는 더 중요한 쪽을 쓰되, 경고가 있으면 숨기지 마라.
- 모델 예측과 같은 말 반복은 정보가 아니다 → 중립. 못 읽는 변수(분산·실책·단발 동시폭발·굴절 끝내기)는 인정하고 억지 규칙화 금지.

{top_collision_digest or "(none)"}

[룰북]
{rules}

[놓치지 말 것]
{salience or "(none)"}

{t038_signal_card_context or "(none)"}

[PREGAME EVIDENCE MATRIX / ANTI-BURIAL CHECK]
{evidence_matrix_context or "(none)"}

[NARRATOR NO-MISS / JUDGMENT CRITERIA LAYER]
{dual_layer_judgment_context or "(none)"}

{correct_judgment_standards_context or "(none)"}

{bvp_player_memory_context or "(none)"}

[사실]
{ctx}

[누적회고] (선수별 *직전 추세* + *시즌 전체 흐름 시퀀스*(978경기 개인 맵핑, 호호붕…/부평락…) + deepread_v2 사전근거. %·기준 버킷 말고 그 선수가 시즌을 어떻게 흘러왔나 통째로 읽어라)
{priors}

[시즌 승리근거 룰] (전 시즌 회고에서 실제로 맞은 구조. 예측 강제 아님, support로만 사용)
{season_support or "(없음)"}

[시즌 실패주의 룰] (전 시즌 회고에서 자주 틀린 구조. 예측 강제 아님, caution으로만 사용)
{season_caution or "(없음)"}

[이번 슬레이트 support/caution 마크] (분석 때 반드시 확인. support와 caution이 충돌하면 사전 야구근거로 최종 판단)
{season_signals or "(없음)"}

[CALIBRATED HISTORICAL GUARD CONTEXT] (research-only, date < target only. Use for re-check/no-play review/confidence cap/support boost. Never auto flip or auto remove. Labels here are reading prompts only, not prediction/skip/score mappings.)
{calibrated_guard_context or "(none)"}

[MODEL DEFENSE / PREDICTION-DELTA CONTROL CONTEXT] (pregame-only, research-only. Supersedes any wording that says to ignore model predictions. The model side is not a command, but it must be defended before narrator override. No auto flip/remove/pass.)
{model_defense_context or "(none)"}
historical-note decision-order rule: decide WIN_LOSS/RUN_TOTAL normally first, then add required model-defense/model-pass-defense/opponent-route-review/RUN_TOTAL route text. These tokens are an audit trail, not a reason to reduce prediction count, force pass, force model side, or create extra RUN_TOTAL legs.

[RUN_TOTAL-FOCUSED POSTGAME RETROSPECTIVE CONTEXT] (RUN_TOTAL-only. Same separation philosophy as postgame/result retrospective: why the total went UPPER/LOWER, what was pregame-knowable, what was post-only/noise. Uses only date < target rows. Never auto prediction/flip/remove from this section alone.)
{run_total_focused_retrospective_context or "(none)"}

[RUN_TOTAL RETRO DOMINANCE ACK CONTEXT] (same-line-band date-safe RUN_TOTAL retrospective dominance. Ack-only, not a prediction command.)
{run_total_retro_dominance_context or "(none)"}

[출력 형식] — **[사실]에 나온 모든 경기를 빠짐없이, 그 순서대로** 출력한다. 모델예측을 복붙하지 말고
*네레이터가 직접 추론한 예측*만 낸다. 단, 모델과 다르면 `model-defense:`로 모델 근거를 방어/반박하고, 모델예측을 패스하면 `model-pass-defense:`로 왜 패스인지 방어하며, 모델팀 conflict가 많으면 `opponent-route-review:`로 상대 루트를 비교한다. hard_guard_review가 있으면 `guard-review:`로 위험 신호를 방어/반박한다. 미가공 부등호(< >)·앰퍼샌드(&) 금지.
각 경기 블록은 정확히 아래 4줄:
### {{AWY}} @ {{HOM}} · RUN_TOTAL {{기준점}}
WIN_LOSS: {{밀 팀 풀네임 or -}} — {{한 줄 근거: 선발·불펜·타선 궤적+누적회고+부상+환경+support/caution 인용}}
RUN_TOTAL: {{UPPER|LOWER|-}} {{기준점}} — {{한 줄 근거: 양 선발·불펜·기준점·환경+support/caution 인용}}
PREDICTION: WIN_LOSS={{HOME|AWAY|-}} RUN_TOTAL={{UPPER|LOWER|-}}
서론·맺음말·요약줄 없이 경기 블록들만 출력하라. 진짜 팽팽/불확실하면 `-`(억지 예측 금지). deepread_v2 근거ID가 있으면 WIN_LOSS/RUN_TOTAL 근거문 안에 `근거 drv2:...` 형태로 포함하라.

**'예측:' 줄 규칙 (기계가 읽음, 정확히 지켜라)** — 네레이터가 *미는 방향*:
- WIN_LOSS: 미는 팀의 위치(그 팀이 홈이면 HOME·원정이면 AWAY), 불확실=`-`. HOME=홈팀(@뒤)·AWAY=원정팀(@앞).
- RUN_TOTAL: UPPER/LOWER, 불확실=`-`. 값은 정확히 UPPER/LOWER/HOME/AWAY/- 중 하나만."""

        # Codex/GPT bridge: Claude Code 한도 소진 시, 이 채팅의 GPT/Codex가
        # _codex_gpt_reasoning_{date}.txt 를 작성해두면 그 본문을 네레이터 출력으로 사용한다.
        # 파일이 없으면 같은 프롬프트를 _codex_gpt_prompt_{date}.md 로 덤프하고 수치 폴백한다.
        # 기본 동작은 기존 Claude 호출 그대로이며, NARR_REASONING_PROVIDER=codex 일 때만 활성화.
        if _using_codex_provider():
            cpath = _codex_reasoning_path(date)
            if os.path.exists(cpath):
                out = open(cpath, encoding="utf-8").read().strip()
                if len(out) >= 20:
                    print(f"[narr_reasoning] Codex/GPT reasoning 사용: {os.path.basename(cpath)}", file=sys.stderr)
                    return out.replace("<", "‹").replace(">", "›").replace("&", "＆")
            try:
                open(_codex_prompt_path(date), "w", encoding="utf-8").write(prompt)
                _write_codex_handoff(date)
                print(f"[narr_reasoning] Codex/GPT prompt 작성: {os.path.basename(_codex_prompt_path(date))}", file=sys.stderr)
                print(f"[narr_reasoning] Codex/GPT handoff 작성: {os.path.basename(_codex_handoff_path(date))}", file=sys.stderr)
            except Exception as e:
                print(f"[narr_reasoning] Codex/GPT prompt 작성 실패: {e}", file=sys.stderr)
            return None

        # claude -p는 transient 실패(nonzero exit·빈 출력·일시적 타임아웃)가 잦은 외부 LLM 호출.
        # 단발 호출+silent swallow면 한 번만 흔들려도 수치 폴백으로 떨어짐 → 재시도 + 사유 로깅(stderr).
        last = "?"
        for attempt in range(1, 3):   # 최대 2회
            try:
                r = subprocess.run("claude -p --model claude-opus-4-8", input=prompt, capture_output=True, text=True,
                                   encoding="utf-8", timeout=llm_timeout, shell=True, env=env, cwd=ROOT)
            except subprocess.TimeoutExpired:
                last = f"timeout({llm_timeout}s)"
                print(f"[narr_reasoning] claude -p 시도{attempt} {last}", file=sys.stderr)
                continue
            out = (r.stdout or "").strip()
            if r.returncode == 0 and len(out) >= 20:
                # (2026-06-08) 결정적 누락 가드 은퇴: SP깊이/⚠️WIN_LOSS-A 마커 기반이라 무기준 narr_context와 불호환
                # + 가드 자체가 "이 마커 반드시 반영" 기준 강제라 도입 철학(통째 읽고 판단)과 충돌. _apply_omission_guard 제거.
                # HTML 안전(메시지 텍스트·이미지 렌더 공통): 미가공 < > & 가 섞이면 parse_mode/HTML 깨짐 → 치환
                return out.replace("<", "‹").replace(">", "›").replace("&", "＆")
            last = f"rc={r.returncode} len={len(out)}"
            print(f"[narr_reasoning] claude -p 시도{attempt} 실패 {last} stderr={(r.stderr or '')[-200:]!r}", file=sys.stderr)
        print(f"[narr_reasoning] claude -p 2회 모두 실패({last}) → 수치 폴백", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[narr_reasoning] build_reasoning 예외: {type(e).__name__}: {e}", file=sys.stderr)
        return None

def _parse_gamepks_arg(gamepks):
    if not gamepks:
        return []
    if isinstance(gamepks, (list, tuple, set)):
        return [str(x).strip() for x in gamepks if str(x).strip()]
    return [x.strip() for x in str(gamepks).replace(";", ",").split(",") if x.strip()]


def _matchups_for_gamepks(date, gamepks):
    if not gamepks:
        return set(), set(), []
    wanted = {str(x).strip() for x in gamepks if str(x).strip()}
    try:
        import pandas as pd
        path = (
            os.environ.get("MLB_C3_V3_PATH")
            or os.environ.get("NARR_V3_INPUT_PATH")
            or os.path.join(ROOT, f"_v3_games_{date}.csv")
        )
        df = pd.read_csv(path)
    except Exception:
        return set(), set(), []
    target, all_matchups, rows = set(), set(), []
    for _, r in df.iterrows():
        try:
            game_pk = str(int(r.get("gamePk")))
        except Exception:
            continue
        matchup = f"{r.get('away_team')} @ {r.get('home_team')}"
        all_matchups.add(matchup)
        if game_pk in wanted:
            target.add(matchup)
            rows.append(r.to_dict())
    return target, all_matchups, rows


def _build_isolated_prompt(date, ctx, priors, salience, skip_matchups=None):
    """Compose a narrator prompt from already-built root inputs only."""
    rules = open(RULES, encoding="utf-8").read()
    season_support = _read_short(SEASON_SUPPORT)
    season_caution = _read_short(SEASON_CAUTION)
    season_signals = build_season_rule_signals(date, skip_matchups)
    try:
        from _mlb_narrator_guard_context import build_prompt_context

        calibrated_guard_context = build_prompt_context(date, skip_matchups=skip_matchups)
    except Exception as e:
        calibrated_guard_context = f"(calibrated guard context unavailable: {type(e).__name__}: {e})"
    model_defense_context = _build_model_defense_context(date, skip_matchups=skip_matchups)
    t038_signal_card_context = _build_t038_pregame_signal_card_context(date, skip_matchups=skip_matchups)
    evidence_matrix_context = _build_pregame_evidence_matrix_context(date, skip_matchups=skip_matchups)
    dual_layer_judgment_context = _build_dual_layer_judgment_context(date, skip_matchups=skip_matchups)
    correct_judgment_standards_context = _build_correct_judgment_standards_context(date, skip_matchups=skip_matchups)
    bvp_player_memory_context = _build_bvp_player_memory_context(date, skip_matchups=skip_matchups)
    run_total_focused_retrospective_context = _build_run_total_focused_retrospective_context(date, skip_matchups=skip_matchups)
    run_total_retro_dominance_context = _build_run_total_retro_dominance_context(date, skip_matchups=skip_matchups)
    top_collision_digest = _build_top_collision_digest(
        date,
        t038_signal_card_context,
        model_defense_context,
        skip_matchups=skip_matchups,
    )
    return f"""You are the MLB narrator reasoning agent.

Use the same production rulebook and pregame-only evidence standard.
This is an isolated consensus rerun for pre-send QA. Do not mention this QA.
Read only the supplied inputs. Do not use postgame/result knowledge.

Hard rules:
- Separate narrator lane: make your own WIN_LOSS/RUN_TOTAL judgment.
- Use only facts knowable before the game.
- If evidence is genuinely uncertain, output '-' for that axis.
- Never auto-flip from a rule. Reason from the slate facts and prior reads.
- historical-note is explanation discipline, not prediction pressure: make the independent narrator decision first, then attach required model-defense/model-pass-defense/opponent-route-review/RUN_TOTAL route text.
- If [CALIBRATED HISTORICAL GUARD CONTEXT] marks an WIN_LOSS item as hard_guard_review and you keep HOME/AWAY, include literal guard-review: in the WIN_LOSS line and directly address the risk. If you cannot defend it, output WIN_LOSS=-.
- If [CALIBRATED HISTORICAL GUARD CONTEXT] marks an item as must_review and its stated condition applies, include literal signal-ack: <gate> in the matching WIN_LOSS or run total baseline only, then explain whether you used, rejected, or discounted that signal. The ack is not a prediction command.
- If [RUN_TOTAL RETRO DOMINANCE ACK CONTEXT] marks a game with ack_required=true, include literal `run_total-retro-ack:` plus that ack_id in the run total baseline, then explain whether you used, rejected, or passed despite the same-line-band retrospective dominance. This is not a prediction command.
- If the slate is WARN_STRONG_MANUAL_REVIEW and your WIN_LOSS line admits thin/conflicting risk (for example 경고, WIN_LOSS-CC, WIN_LOSS-OPS, 얇지만, 우위가 묽다/얇다) while keeping HOME/AWAY, include literal risk-resolution: in the WIN_LOSS line and explain why that risk does not break the prediction. If you cannot resolve it, output WIN_LOSS=-.
- If your WIN_LOSS differs from the model side, include literal model-defense: in the WIN_LOSS line and cite the concrete model feature/support being rejected plus why the opponent route wins.
- If your WIN_LOSS is '-' while the model WIN_LOSS side is concrete, include literal model-pass-defense: in the WIN_LOSS line and cite the concrete model feature/support being passed over plus the exact pregame conflict that justifies pass.
- If model-side historical-note conflict cards materially challenge the model WIN_LOSS side and your final WIN_LOSS is concrete, include literal opponent-route-review: in the WIN_LOSS line and compare model retention against the opponent route. This is visibility only, not a forced flip/pass rule.
- Preserve the exact output format. Output one block for every game in [FACTS].

{top_collision_digest or "(none)"}

[RULEBOOK]
{rules}

[SALIENCE DIGEST]
{salience or "(none)"}

{t038_signal_card_context or "(none)"}

[PREGAME EVIDENCE MATRIX / ANTI-BURIAL CHECK]
{evidence_matrix_context or "(none)"}

[NARRATOR NO-MISS / JUDGMENT CRITERIA LAYER]
{dual_layer_judgment_context or "(none)"}

{correct_judgment_standards_context or "(none)"}

{bvp_player_memory_context or "(none)"}

[FACTS]
{ctx}

[PRIORS]
{priors or "(none)"}

[SEASON SUPPORT]
{season_support or "(none)"}

[SEASON CAUTION]
{season_caution or "(none)"}

[SLATE SUPPORT/CAUTION MARKS]
{season_signals or "(none)"}

[CALIBRATED HISTORICAL GUARD CONTEXT]
{calibrated_guard_context or "(none)"}

[MODEL DEFENSE / PREDICTION-DELTA CONTROL CONTEXT]
{model_defense_context or "(none)"}
historical-note decision-order rule: decide WIN_LOSS/RUN_TOTAL normally first, then add required model-defense/model-pass-defense/opponent-route-review/RUN_TOTAL route text. These tokens are an audit trail, not a reason to reduce prediction count, force pass, force model side, or create extra RUN_TOTAL legs.

[RUN_TOTAL-FOCUSED POSTGAME RETROSPECTIVE CONTEXT]
{run_total_focused_retrospective_context or "(none)"}

[RUN_TOTAL RETRO DOMINANCE ACK CONTEXT]
{run_total_retro_dominance_context or "(none)"}

[OUTPUT FORMAT]
For every game in [FACTS], output exactly:
### {{AWY}} @ {{HOM}} 쨌 RUN_TOTAL {{line}}
WIN_LOSS: {{short reason or -}}
RUN_TOTAL: {{UPPER|LOWER|-}} {{line}} line-band: {{low|normal|high|extreme-high}}; {{if final RUN_TOTAL differs from model_RUN_TOTAL or final RUN_TOTAL is '-' while model_RUN_TOTAL is concrete, include literal run_total-model-defense: plus literal UPPER route: and LOWER route:; if final RUN_TOTAL agrees with model_RUN_TOTAL, include the dominant UPPER route: or LOWER route:}}
PREDICTION: WIN_LOSS={{HOME|AWAY|-}} RUN_TOTAL={{UPPER|LOWER|-}}
"""


def _parse_final_prediction_sentinel(block):
    """Category variant: read ONLY the authoritative 'FINAL_PREDICTION: WIN_LOSS=.. RUN_TOTAL=..' sentinel
    (last occurrence). Ignores mid-block category verdict lines like 'RUN_TOTAL=UPPER압력'.
    Falls back to a markdown-stripped '예측:' line if the sentinel is missing.
    """
    win_loss = run_total = None
    for ln in (block or "").splitlines():
        m = re.search(r"FINAL_PREDICTION:\s*WIN_LOSS\s*=\s*(HOME|AWAY|-)\s+RUN_TOTAL\s*=\s*(UPPER|LOWER|-)", ln, re.I)
        if m:
            win_loss = None if m.group(1) == "-" else m.group(1).upper()
            run_total = None if m.group(2) == "-" else m.group(2).upper()
    if win_loss is None and run_total is None:
        for ln in (block or "").splitlines():
            s = ln.strip().lstrip("*").strip()
            if s.startswith("예측"):
                mm = re.search(r"WIN_LOSS\s*=\s*(HOME|AWAY|-)", s, re.I)
                oo = re.search(r"RUN_TOTAL\s*=\s*(UPPER|LOWER|-)", s, re.I)
                if mm:
                    win_loss = None if mm.group(1) == "-" else mm.group(1).upper()
                if oo:
                    run_total = None if oo.group(1) == "-" else oo.group(1).upper()
    return {"win_loss": win_loss, "run_total": run_total}


def _build_category_structured_prompt(date, ctx, priors, salience, skip_matchups=None):
    """historical-note prototype: category-structured reasoning variant (research-only).
    Forces a per-category directional verdict + mandatory interaction review + overrule
    accounting, and softens the too-strong one-sided-collapse heuristic. The production
    _build_reasoning()/_build_isolated_prompt() paths are untouched.
    """
    rules = open(RULES, encoding="utf-8").read()
    season_support = _read_short(SEASON_SUPPORT)
    season_caution = _read_short(SEASON_CAUTION)
    season_signals = build_season_rule_signals(date, skip_matchups)
    try:
        from _mlb_narrator_guard_context import build_prompt_context

        calibrated_guard_context = build_prompt_context(date, skip_matchups=skip_matchups)
    except Exception as e:
        calibrated_guard_context = f"(calibrated guard context unavailable: {type(e).__name__}: {e})"
    model_defense_context = _build_model_defense_context(date, skip_matchups=skip_matchups)
    t038_signal_card_context = _build_t038_pregame_signal_card_context(date, skip_matchups=skip_matchups)
    evidence_matrix_context = _build_pregame_evidence_matrix_context(date, skip_matchups=skip_matchups)
    dual_layer_judgment_context = _build_dual_layer_judgment_context(date, skip_matchups=skip_matchups)
    correct_judgment_standards_context = _build_correct_judgment_standards_context(date, skip_matchups=skip_matchups)
    bvp_player_memory_context = _build_bvp_player_memory_context(date, skip_matchups=skip_matchups)
    run_total_focused_retrospective_context = _build_run_total_focused_retrospective_context(date, skip_matchups=skip_matchups)
    run_total_retro_dominance_context = _build_run_total_retro_dominance_context(date, skip_matchups=skip_matchups)
    top_collision_digest = _build_top_collision_digest(
        date,
        t038_signal_card_context,
        model_defense_context,
        skip_matchups=skip_matchups,
    )
    return f"""You are the MLB narrator reasoning agent (category-structured variant, research prototype).

Use the same production rulebook and pregame-only evidence standard.
This is an isolated research rerun. Do not mention this. Read only the supplied inputs.
Do not use postgame/result knowledge.

Hard rules:
- Separate narrator lane: make your own WIN_LOSS/RUN_TOTAL judgment from slate facts and prior reads.
- Use only facts knowable before the game. If genuinely uncertain on an axis, output '-'.
- Never auto-flip from a rule. Do NOT prediction by counting categories — counts are not a decision rule.

[RULEBOOK OVERRIDE — supersedes any conflicting rulebook line for THIS run]
- 룰북의 "한 선발 붕괴 = WIN_LOSS 신호, 한쪽만 무너지면 blowout이라 총합 낮다(자동 LOWER)"는 너무 강하다. 다음으로 대체:
  한 선발 붕괴는 기본적으로 WIN_LOSS 쪽 신호일 수 *있지만*, 그 붕괴가 HR/볼넷/정타 반복이고 + 짧은 이닝/얇은 불펜 + 타자친화 구장·환경·상대 핵심타자 컨택이 겹치면,
  패팀이 0점이어도 *이긴 팀 단독으로* 기준점을 넘기는 UPPER route가 성립한다. one-sided collapse를 자동 LOWER로 번역하지 말고, UPPER route와 LOWER route를 *둘 다* 검토하라.
- 타자 일별 핫/콜드는 여전히 노이즈. 선발 궤적·불펜/이닝 구조·구장/환경이 RUN_TOTAL 동력이다.

{top_collision_digest or "(none)"}

[RULEBOOK]
{rules}

[SALIENCE DIGEST]
{salience or "(none)"}

{t038_signal_card_context or "(none)"}

[PREGAME EVIDENCE MATRIX / ANTI-BURIAL CHECK]
{evidence_matrix_context or "(none)"}

[NARRATOR NO-MISS / JUDGMENT CRITERIA LAYER]
{dual_layer_judgment_context or "(none)"}

{correct_judgment_standards_context or "(none)"}

{bvp_player_memory_context or "(none)"}

[FACTS]
{ctx}

[PRIORS]
{priors or "(none)"}

[SEASON SUPPORT]
{season_support or "(none)"}

[SEASON CAUTION]
{season_caution or "(none)"}

[SLATE SUPPORT/CAUTION MARKS]
{season_signals or "(none)"}

[CALIBRATED HISTORICAL GUARD CONTEXT]
{calibrated_guard_context or "(none)"}

[MODEL DEFENSE / PREDICTION-DELTA CONTROL CONTEXT]
{model_defense_context or "(none)"}
historical-note decision-order rule: decide WIN_LOSS/RUN_TOTAL normally first, then add required model-defense/model-pass-defense/opponent-route-review/RUN_TOTAL route text. These tokens are an audit trail, not a reason to reduce prediction count, force pass, force model side, or create extra RUN_TOTAL legs.

[RUN_TOTAL-FOCUSED POSTGAME RETROSPECTIVE CONTEXT]
{run_total_focused_retrospective_context or "(none)"}

[RUN_TOTAL RETRO DOMINANCE ACK CONTEXT]
{run_total_retro_dominance_context or "(none)"}

[REASONING PROTOCOL — category-structured, anti-burial]
For EACH game, reason category-by-category so no read gets buried in a narrative.
- Each category emits a QUALITATIVE directional verdict only: RUN_TOTAL=UPPER압력|LOWER압력|- , WIN_LOSS=HOME|AWAY|- , plus a one-line 근거 and an uncertainty note.
- '-' (neutral/unclear) is a valid and encouraged verdict when the read is thin. Do NOT manufacture false confidence.
- Verdicts are qualitative directions, NOT scores. Do not sum them, do not prediction by majority count.
- An INTERACTION section is MANDATORY (categories interact multiplicatively, not additively).
- historical-note defense literals are audit evidence, not scoring terms. Decide the final WIN_LOSS/RUN_TOTAL first, then add required defense/route literals without shrinking predictions by default.
- Then synthesis. For EVERY category whose verdict opposes your final prediction, you MUST write a one-line 기각(overrule) reason. A signal may die, but why it died must be on the record.
- If [CALIBRATED HISTORICAL GUARD CONTEXT] marks an WIN_LOSS item as hard_guard_review and you keep HOME/AWAY, include literal guard-review: in the WIN_LOSS synthesis line and directly address the risk. If you cannot defend it, output WIN_LOSS=-.
- If [CALIBRATED HISTORICAL GUARD CONTEXT] marks an item as must_review and its stated condition applies, include literal signal-ack: <gate> in the matching WIN_LOSS or RUN_TOTAL synthesis line only, then explain whether you used, rejected, or discounted that signal. The ack is not a prediction command.
- If [RUN_TOTAL RETRO DOMINANCE ACK CONTEXT] marks a game with ack_required=true, include literal `run_total-retro-ack:` plus that ack_id in the RUN_TOTAL synthesis line and explain whether you used, rejected, or passed despite the same-line-band retrospective dominance. This is not a prediction command.
- If the slate is WARN_STRONG_MANUAL_REVIEW and your WIN_LOSS synthesis admits thin/conflicting risk (for example 경고, WIN_LOSS-CC, WIN_LOSS-OPS, 얇지만, 우위가 묽다/얇다) while keeping HOME/AWAY, include literal risk-resolution: in the WIN_LOSS synthesis line and explain why that risk does not break the prediction. If you cannot resolve it, output WIN_LOSS=-.
- If your WIN_LOSS differs from the model side, include literal model-defense: in the WIN_LOSS synthesis line and cite the concrete model feature/support being rejected plus why the opponent route wins.
- If your WIN_LOSS is '-' while the model WIN_LOSS side is concrete, include literal model-pass-defense: in the WIN_LOSS synthesis line and cite the concrete model feature/support being passed over plus the exact pregame conflict that justifies pass.
- If model-side historical-note conflict cards materially challenge the model WIN_LOSS side and your final WIN_LOSS is concrete, include literal opponent-route-review: in the WIN_LOSS synthesis line and compare model retention against the opponent route. This is visibility only, not a forced flip/pass rule.
- Every final RUN_TOTAL synthesis line must include literal line-band:.
- If your final RUN_TOTAL differs from model_RUN_TOTAL, or final RUN_TOTAL is '-' while model_RUN_TOTAL is concrete, include literal run_total-model-defense: and explicitly compare literal UPPER route: and literal LOWER route: using starters, bullpens, lineups, park/weather/umpire, and line level.
- If your final RUN_TOTAL agrees with model_RUN_TOTAL, include line-band: plus the dominant route literal (`UPPER route:` for UPPER, `LOWER route:` for LOWER). The route text is a reasoning audit, not a forced prediction rule.

[OUTPUT FORMAT]
For every game in [FACTS], output exactly:
### {{AWY}} @ {{HOM}} · RUN_TOTAL {{line}}
[카테고리]
- 선발/투수흐름: RUN_TOTAL={{UPPER압력|LOWER압력|-}} WIN_LOSS={{HOME|AWAY|-}} · 근거 · (불확실성)
- 불펜/이닝구조: RUN_TOTAL={{UPPER압력|LOWER압력|-}} WIN_LOSS={{HOME|AWAY|-}} · 근거 · (불확실성)
- 타선/타자폼/매치업: RUN_TOTAL={{UPPER압력|LOWER압력|-}} WIN_LOSS={{HOME|AWAY|-}} · 근거 · (불확실성)
- 라인업/결장/휴식: RUN_TOTAL={{UPPER압력|LOWER압력|-}} WIN_LOSS={{HOME|AWAY|-}} · 근거 · (불확실성)
- 환경/구장/날씨/주심: RUN_TOTAL={{UPPER압력|LOWER압력|-}} WIN_LOSS={{HOME|AWAY|-}} · 근거 · (불확실성)
- 시장/모델/기준점 sanity: 참고만 · 한줄
- h2h/최근 맞대결: 참고만 · 한줄
[상호작용]
- UPPER route: 약한선발 × 얇은불펜 × 구장/환경이 실점 route를 키우나 / one-sided collapse가 단독으로 기준점 넘나
- magnitude 분리(예측 규칙 아님, "기준점 넘는 경로가 충분히 구체적인가"를 서술하는 장치): ordinary(단독 4~6 = 기준점 근처/미달) / line-clear(단독 7+, 또는 상대 3점만으로 기준점 초과) / tail(HR·BB·정타반복 + 짧은이닝 + 얇은펜 + 구장/주심 겹쳐 9+ 단독폭발). one-sided collapse면 이 셋 중 어디인지 명시.
- LOWER route: 선발 지배·깊이·통제환경이 총득점을 누르나 (표면조건 아닌 구체근거로)
- 5질문: 1)약한선발×얇은불펜 이닝실점 route? 2)HR/BB/정타피격형×구장/환경 장타·출루누적? 3)한쪽 선발만 붕괴해도 상대 단독 기준점넘김 route? 4)환경 LOWER가 collapse risk를 실제 상쇄할 만큼 구체적인가 표면조건인가? 5)WIN_LOSS blowout route가 RUN_TOTAL에선 low인가 high인가 따로 판단?
- 충돌 해소(기각): 최종예측과 반대 방향 낸 카테고리마다 한 줄 기각 사유.
WIN_LOSS: {{종합 — 카테고리/상호작용에서 어떻게 도출됐는지}}
RUN_TOTAL: line-band: {{low|normal|high|extreme-high}}; {{if differing/passing against concrete model_RUN_TOTAL include run_total-model-defense:}} UPPER route: {{득점 상승 경로}}; LOWER route: {{득점 억제 경로}}; {{종합 — UPPER route vs LOWER route 어느 쪽이 왜 이겼는지, magnitude route 어디로 봤는지}}
예측(서술용): WIN_LOSS={{HOME|AWAY|-}} RUN_TOTAL={{UPPER|LOWER|-}}
FINAL_PREDICTION: WIN_LOSS={{HOME|AWAY|-}} RUN_TOTAL={{UPPER|LOWER|-}}
"""


# Narrator output headers vary between full team names and abbreviations
# ("Pittsburgh Pirates @ Athletics" vs "PIT @ ATH") for the same game. Canonicalize
# block-map keys to full names ONLY for matching — stored team names stay from v3.
_NARR_ABBR_TO_FULL = {
    "AZ": "Arizona Diamondbacks", "ARI": "Arizona Diamondbacks", "ATL": "Atlanta Braves",
    "BAL": "Baltimore Orioles", "BOS": "Boston Red Sox", "CHC": "Chicago Cubs",
    "CWS": "Chicago White Sox", "CHW": "Chicago White Sox", "CIN": "Cincinnati Reds",
    "CLE": "Cleveland Guardians", "COL": "Colorado Rockies", "DET": "Detroit Tigers",
    "HOU": "Houston Astros", "KC": "Kansas City Royals", "LAA": "Los Angeles Angels",
    "LAD": "Los Angeles Dodgers", "MIA": "Miami Marlins", "MIL": "Milwaukee Brewers",
    "MIN": "Minnesota Twins", "NYM": "New York Mets", "NYY": "New York Yankees",
    "ATH": "Athletics", "OAK": "Athletics", "PHI": "Philadelphia Phillies",
    "PIT": "Pittsburgh Pirates", "SD": "San Diego Padres", "SDP": "San Diego Padres",
    "SF": "San Francisco Giants", "SFG": "San Francisco Giants", "SEA": "Seattle Mariners",
    "STL": "St. Louis Cardinals", "TB": "Tampa Bay Rays", "TBR": "Tampa Bay Rays",
    "TEX": "Texas Rangers", "TOR": "Toronto Blue Jays", "WSH": "Washington Nationals",
    "WAS": "Washington Nationals", "Oakland Athletics": "Athletics",
}


def _canon_matchup_key(s):
    s = (s or "").split("·")[0].strip()
    def _team(x):
        x = (x or "").strip()
        return _NARR_ABBR_TO_FULL.get(x.upper(), _NARR_ABBR_TO_FULL.get(x, x))
    if "@" not in s:
        return _team(s)
    left, right = s.split("@", 1)
    return f"{_team(left.strip())} @ {_team(right.strip())}"


def build_reasoning_isolated(date, out_dir, gamepks=None, llm_timeout=600, prompt_variant="default", salience_override_path=None, prompt_only=False):
    """Build narrator reasoning in out_dir without mutating root inputs.

    prompt_variant: "default" = production-equivalent isolated prompt (QA consensus).
                    "category" = historical-note category-structured research prototype.
    """
    out_dir = os.path.abspath(str(out_dir))
    os.makedirs(out_dir, exist_ok=True)
    gamepks = _parse_gamepks_arg(gamepks)
    input_paths = {
        "context": os.path.join(ROOT, f"_narr_context_{date}.txt"),
        "priors": os.path.join(ROOT, f"_narr_priors_{date}.txt"),
        "salience": os.path.abspath(str(salience_override_path)) if salience_override_path else os.path.join(ROOT, f"_narr_salience_digest_{date}.txt"),
        "v3": os.path.join(ROOT, f"_v3_games_{date}.csv"),
    }
    missing = [k for k, p in input_paths.items() if not os.path.exists(p) or os.path.getsize(p) == 0]
    report = {
        "schema": "narr_reasoning_isolated.v1",
        "date": date,
        "out_dir": out_dir,
        "prompt_variant": prompt_variant,
        "prompt_only": bool(prompt_only),
        "gamepks": gamepks,
        "root_inputs": input_paths,
        "missing_inputs": missing,
        "ok": False,
    }
    report_path = os.path.join(out_dir, f"_narr_isolated_report_{date}.json")
    if missing:
        open(report_path, "w", encoding="utf-8").write(json.dumps(report, ensure_ascii=False, indent=2))
        return report

    ctx = open(input_paths["context"], encoding="utf-8").read().strip()
    priors = open(input_paths["priors"], encoding="utf-8").read().strip()
    salience = open(input_paths["salience"], encoding="utf-8").read().strip()
    target_matchups, all_matchups, target_rows = _matchups_for_gamepks(date, gamepks)
    skip_matchups = None
    if gamepks:
        if not target_matchups:
            report["error"] = "no target gamePks matched _v3_games"
            open(report_path, "w", encoding="utf-8").write(json.dumps(report, ensure_ascii=False, indent=2))
            return report
        skip_matchups = all_matchups - target_matchups
        ctx = _filter_games(ctx, skip_matchups)
        priors = _filter_games(priors, skip_matchups)
        salience = _filter_games(salience, skip_matchups)

    if prompt_variant == "category":
        prompt = _build_category_structured_prompt(date, ctx, priors, salience, skip_matchups=skip_matchups)
    else:
        prompt = _build_isolated_prompt(date, ctx, priors, salience, skip_matchups=skip_matchups)
    prompt_path = os.path.join(out_dir, f"_narr_prompt_{date}.md")
    reasoning_path = os.path.join(out_dir, f"_narr_reasoning_{date}.txt")
    predictions_path = os.path.join(out_dir, f"_narr_predictions_{date}.json")
    open(prompt_path, "w", encoding="utf-8").write(prompt)
    report.update({"prompt_path": prompt_path, "reasoning_path": reasoning_path, "predictions_path": predictions_path, "report_path": report_path})
    if prompt_only:
        report.update({
            "ok": True,
            "prompt_written": True,
            "parsed_predictions": 0,
            "provider": "codex_prompt_only",
            "note": "Prompt written for Codex/manual narrator fill; no external LLM was called.",
        })
        open(report_path, "w", encoding="utf-8").write(json.dumps(report, ensure_ascii=False, indent=2))
        return report

    env = _env()
    try:
        r = subprocess.run(
            "claude -p --model claude-opus-4-8",
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=llm_timeout,
            shell=True,
            env=env,
            cwd=ROOT,
        )
        text = (r.stdout or "").strip()
        report.update({"llm_rc": r.returncode, "stderr_tail": (r.stderr or "")[-500:], "stdout_len": len(text)})
        if r.returncode != 0 or len(text) < 20:
            open(reasoning_path, "w", encoding="utf-8").write(text)
            report["error"] = "llm_failed_or_empty"
            open(report_path, "w", encoding="utf-8").write(json.dumps(report, ensure_ascii=False, indent=2))
            return report
        text = text.replace("<", "＜").replace(">", "＞").replace("&", "＆")
        open(reasoning_path, "w", encoding="utf-8").write(text)

        rows = target_rows
        if not rows:
            try:
                import pandas as pd

                rows = [r.to_dict() for _, r in pd.read_csv(input_paths["v3"]).iterrows()]
            except Exception:
                rows = []
        block_map = reasoning_blocks(text)
        block_map_canon = {_canon_matchup_key(k): v for k, v in block_map.items()}
        parsed_predictions = []
        for row in rows:
            try:
                game_pk = int(row.get("gamePk"))
            except Exception:
                continue
            matchup = f"{row.get('away_team')} @ {row.get('home_team')}"
            block = block_map.get(matchup) or block_map_canon.get(_canon_matchup_key(matchup), "")
            if prompt_variant == "category":
                # Category blocks contain many mid-block "RUN_TOTAL=UPPER압력 / WIN_LOSS=HOME" verdict
                # lines. A block-wide regex would grab a category verdict as the prediction.
                # Read ONLY the authoritative ASCII sentinel line instead.
                prediction = _parse_final_prediction_sentinel(block)
            else:
                parsed = parse_predictions(block)
                prediction = parsed[0] if parsed else {"run_total": None, "win_loss": None}
                if prediction.get("win_loss") is None and prediction.get("run_total") is None and block:
                    # Isolated rerun marks the prediction line with varying glyphs (🎯 / → / 예측:);
                    # parse_predictions only reads "예측:". Fall back to a tolerant block scan so the
                    # decision line (WIN_LOSS=HOME/AWAY/- RUN_TOTAL=UPPER/LOWER/-) is captured regardless.
                    _mm = re.search(r"WIN_LOSS\s*=\s*(HOME|AWAY|-)", block, re.I)
                    _oo = re.search(r"RUN_TOTAL\s*=\s*(UPPER|LOWER|-)", block, re.I)
                    _mlv = _mm.group(1).upper() if _mm else "-"
                    _ouv = _oo.group(1).upper() if _oo else "-"
                    prediction = {"win_loss": None if _mlv == "-" else _mlv, "run_total": None if _ouv == "-" else _ouv}
            win_loss_team = None
            if prediction.get("win_loss") == "HOME":
                win_loss_team = row.get("home_team")
            elif prediction.get("win_loss") == "AWAY":
                win_loss_team = row.get("away_team")
            parsed_predictions.append(
                {
                    "gamePk": game_pk,
                    "away": row.get("away_team"),
                    "home": row.get("home_team"),
                    "run_total": prediction.get("run_total"),
                    "win_loss": win_loss_team,
                    "line": row.get("run_total_baseline"),
                    "isolated": True,
                    "source": "build_reasoning_isolated",
                }
            )
        json.dump({str(p["gamePk"]): p for p in parsed_predictions}, open(predictions_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        report.update({"ok": True, "parsed_predictions": len(parsed_predictions)})
        open(report_path, "w", encoding="utf-8").write(json.dumps(report, ensure_ascii=False, indent=2))
        return report
    except subprocess.TimeoutExpired as e:
        report.update({"error": "timeout", "stderr_tail": str(e)})
        open(report_path, "w", encoding="utf-8").write(json.dumps(report, ensure_ascii=False, indent=2))
        return report


def build_section(date, **kw):
    """텍스트 폴백용: 헤더 포함 섹션 문자열. 실패 시 None."""
    out = build_reasoning(date, **kw)
    if not out:
        return None
    return f"\n<b>━ 🧭 네레이터 추론 (정독 궤적, as-of {_ledger_as_of()}) ━</b>\n{out}"


def _reasoning_block_matchup(block):
    """Return the matchup from legacy or gamePk-qualified narrator headers."""
    import re

    match = re.match(
        r"^###\s+(.+?)\s+(?:\[gamePk=\d+\]\s*)?(?:[·|—-]\s*)?RUN_TOTAL\b",
        block or "",
    )
    return match.group(1).strip() if match else ""


def reasoning_blocks(text):
    """추론 텍스트 → {'Away @ Home': '### 블록 전체'} (frozen 경기 블록 재사용용).
    ⚠️ 더블헤더는 같은 matchup 키가 2개라 뒤 블록이 앞을 덮어쓴다(prediction 유실). 순서가 필요하면
    reasoning_blocks_ordered를 써라."""
    import re
    out = {}
    for b in re.split(r"(?m)(?=^### )", text or ""):
        matchup = _reasoning_block_matchup(b)
        if matchup:
            out[matchup] = b.rstrip()
    return out


def reasoning_blocks_ordered(text):
    """추론 텍스트 → [(matchup, block), ...] 파일 등장 순서 유지.
    더블헤더처럼 같은 matchup이 여러 번 나오면 dict의 last-wins 충돌 없이 순서대로 전부 보존한다
    (2026-07-07 MIL@STL G1 밀워키 예측이 G2 PASS 블록에 덮여 유실된 버그 수정)."""
    import re
    out = []
    for b in re.split(r"(?m)(?=^### )", text or ""):
        matchup = _reasoning_block_matchup(b)
        if matchup:
            out.append((matchup, b.rstrip()))
    return out


def save_reasoning(date, text):
    try:
        open(_out_path(f"_narr_reasoning_{date}.txt"), "w", encoding="utf-8").write(text or "")
        return True
    except Exception:
        return False


def load_reasoning(date):
    try:
        if _using_codex_provider() and os.path.exists(_codex_reasoning_path(date)):
            return open(_codex_reasoning_path(date), encoding="utf-8").read()
        return open(_out_path(f"_narr_reasoning_{date}.txt"), encoding="utf-8").read()
    except Exception:
        return None


def load_saved_reasoning(date):
    """frozen 경기 reasoning block 재사용 전용 — production 저장본 `_narr_reasoning_{date}.txt`만 읽는다.
    load_reasoning과 달리 codex bridge 임시파일(`_codex_gpt_reasoning_*`)을 절대 읽지 않아,
    부분 재실행 시 stale bridge 파일이 frozen block 병합에 섞이는 6/21 사고를 막는다(historical-note)."""
    try:
        return open(_out_path(f"_narr_reasoning_{date}.txt"), encoding="utf-8").read()
    except Exception:
        return ""


def parse_evidence_refs(reasoning_text):
    """Reasoning blocks -> per-game evidence metadata.

    deepread ids look like drv2:.../drv3:... and may be embedded in WIN_LOSS/RUN_TOTAL lines.
    Existing outputs without ids remain valid; they simply get empty refs.
    """
    import re
    metas = []
    for b in re.split(r"(?m)(?=^### )", reasoning_text or ""):
        if not b.strip().startswith("### "):
            continue
        win_loss_refs, run_total_refs = [], []
        for ln in b.splitlines():
            refs = re.findall(r"(?:drv[23]|lens3):[A-Za-z0-9_.:-]+", ln)
            if ln.startswith("WIN_LOSS:"):
                win_loss_refs.extend(refs)
            elif ln.startswith("RUN_TOTAL:"):
                run_total_refs.extend(refs)
        metas.append({
            "win_loss_evidence_refs": sorted(set(win_loss_refs)),
            "run_total_evidence_refs": sorted(set(run_total_refs)),
            "pre_knowable_ok": True,
            "post_only_excluded": True,
            "narrator_lane": "separate",
        })
    return metas


def parse_predictions(reasoning_text):
    """'예측: RUN_TOTAL=X WIN_LOSS=Y' 기준점을 경기 순서대로 파싱 → [{'run_total':'UPPER'|'LOWER'|None, 'win_loss':'HOME'|'AWAY'|None}, ...].
    네레이터의 독자 방향 예측(지지=모델방향/주의=반대/중립=None). 결과대입 때 ✅/❌ 추적용."""
    import re
    predictions = []
    for ln in (reasoning_text or '').split('\n'):
        ln = ln.strip()
        # Codex bridge occasionally emits "예측 WIN_LOSS=... RUN_TOTAL=..." without the colon.
        # Treat that as parseable, but still require an explicit prediction-label prefix
        # so category verdict lines containing WIN_LOSS=/RUN_TOTAL= are not mistaken for predictions.
        if not re.match(r'^(?:예측\s*:?\s*|최종예측\s*:?\s*|PREDICTION\s*:?\s*|FINAL_PREDICTION\s*:?\s*|예측\(서술용\)\s*:?\s*)', ln, re.I):
            continue
        mo = re.search(r'RUN_TOTAL\s*=\s*(UPPER|LOWER|-)', ln, re.I)
        mm = re.search(r'WIN_LOSS\s*=\s*(HOME|AWAY|-)', ln, re.I)
        ouv = mo.group(1).upper() if mo else '-'
        mlv = mm.group(1).upper() if mm else '-'
        predictions.append({'run_total': None if ouv == '-' else ouv, 'win_loss': None if mlv == '-' else mlv})
    return predictions


def save_predictions(date, predictions):
    """predictions: [{'gamePk','away','home','run_total','win_loss'}] → _narr_predictions_{date}.json (gamePk 키). 결과모드가 로드."""
    import json
    try:
        path = _out_path(f'_narr_predictions_{date}.json')
        # 누적: 부분분석(subset)마다 덮어쓰면 이전 클러스터 예측이 날아가 full-slate 표가 안 채워짐.
        # gamePk별 병합 → 밤새 클러스터별 분석이 점진적으로 누적되고, 같은 경기 재분석은 최신으로 갱신.
        d = {}
        try:
            existing = json.load(open(path, encoding='utf-8'))
            if isinstance(existing, dict):
                d.update(existing)
        except Exception:
            pass
        for p in predictions:
            d[str(p['gamePk'])] = p
        json.dump(d, open(path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        return True
    except Exception:
        return False


def load_predictions(date):
    import json
    try:
        if _using_codex_provider() and os.path.exists(_codex_reasoning_path(date)):
            return None
        return json.load(open(_out_path(f'_narr_predictions_{date}.json'), encoding='utf-8'))
    except Exception:
        return None


def load_saved_predictions(date):
    """Read production _narr_predictions regardless of Codex bridge state."""
    import json
    try:
        return json.load(open(_out_path(f'_narr_predictions_{date}.json'), encoding='utf-8'))
    except Exception:
        return None


if __name__ == "__main__":
    d = sys.argv[1] if len(sys.argv) > 1 else None
    if not d:
        print("usage: py _narr_reasoning.py YYYY-MM-DD")
        sys.exit(1)
    sec = build_section(d)
    print(sec if sec else "[FALLBACK] build_reasoning returned None (호출측이 수치 verdict로 폴백)")
