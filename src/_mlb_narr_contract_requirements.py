# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""model-defense 계약의 '필요 토큰' 산출 단일 원천 (historical-note).

C3 per-game runner와 `_narr_model_defense_validate.py`가 **같은 함수**를 불러
`contract_tokens_required`를 자동유도한다. 하드코딩 금지 — 규칙 2개 위험 제거.

순수함수: 예측/모델/가드 등 이미 계산된 입력만 받아 필요 리터럴 마커 리스트를 돌려준다.
검사(presence) 로직은 validator에 남고, 여기선 '무엇이 필요한가'만 결정한다.
Codex 교차검증 대상(기존 검토 조건 b): validator/C3 runner 동일 호출 + 기존 validator 회귀 0.
"""
from __future__ import annotations
from typing import Any


def required_tokens(
    *,
    model_win_loss: str,          # "HOME"|"AWAY"|"-"
    final_win_loss: str,          # "HOME"|"AWAY"|"-"
    model_run_total: str,          # "UPPER"|"LOWER"|"-"
    final_run_total: str,          # "UPPER"|"LOWER"|"-"
    concrete_model_win_loss: bool,
    t038_conflict_count: int = 0,
    hard_guard_gates: list[str] | None = None,
    must_review_items: list[dict[str, Any]] | None = None,
    run_total_retro_ack_id: str = "",
) -> dict[str, Any]:
    """필요 리터럴 마커 리스트 {win_loss:[...], run_total:[...], win_loss_feature_token_required:bool}.

    `_narr_model_defense_validate.py`의 결정 로직과 1:1 대응:
    - model-pass-defense: WIN_LOSS pass + concrete model → (+ 피처토큰)
    - model-defense: WIN_LOSS override(모델과 반대) → (+ 피처토큰)
    - opponent-route-review: concrete WIN_LOSS + t038 모델충돌 ≥2
    - guard-review: concrete WIN_LOSS + hard guard gate
    - signal-ack:<id>: must-review (WIN_LOSS pass/override, RUN_TOTAL 반대)
    - RUN_TOTAL: line-band 항상, 모델과 다르거나 pass면 run_total-model-defense+양 route, 같으면 해당 route
    - run_total-retro-ack:<id>: retro dominance 요구시
    ※ risk-resolution: 은 '얇음 자백' 텍스트 조건부라 required에 안 넣음(validator가 텍스트로 판정) → conditional로 별도 반환.
    """
    hard_guard_gates = hard_guard_gates or []
    must_review_items = must_review_items or []
    win_loss: list[str] = []
    run_total: list[str] = []

    win_loss_concrete = final_win_loss in ("HOME", "AWAY")
    model_concrete_side = model_win_loss in ("HOME", "AWAY")

    # --- WIN_LOSS ---
    if final_win_loss == "-" and concrete_model_win_loss:
        win_loss.append("model-pass-defense:")
    if win_loss_concrete and model_concrete_side and final_win_loss != model_win_loss:
        win_loss.append("model-defense:")
    if win_loss_concrete and model_concrete_side and t038_conflict_count >= 2:
        win_loss.append("opponent-route-review:")
    if win_loss_concrete and hard_guard_gates:
        win_loss.append("guard-review:")

    # --- must-review signal-ack (axis별) ---
    for item in must_review_items:
        leg = str(item.get("leg") or "").upper()
        ack = str(item.get("ack_id") or item.get("gate") or "").strip()
        if not ack:
            continue
        if leg == "WIN_LOSS":
            need = (final_win_loss == "-") or (win_loss_concrete and model_concrete_side and final_win_loss != model_win_loss)
            if need:
                win_loss.append(f"signal-ack: {ack}")
        elif leg == "RUN_TOTAL":
            need = (final_run_total in ("UPPER", "LOWER") and model_run_total in ("UPPER", "LOWER") and final_run_total != model_run_total)
            if need:
                run_total.append(f"signal-ack: {ack}")

    # --- RUN_TOTAL ---
    line_band_needed = not (final_run_total == "-" and model_run_total not in ("UPPER", "LOWER"))
    if final_run_total in ("UPPER", "LOWER", "-") and line_band_needed:
        run_total.insert(0, "line-band:")
        needs_full = model_run_total in ("UPPER", "LOWER") and (
            final_run_total == "-" or (final_run_total in ("UPPER", "LOWER") and final_run_total != model_run_total)
        )
        if needs_full:
            run_total += ["run_total-model-defense:", "UPPER route:", "LOWER route:"]
        elif final_run_total == "UPPER":
            run_total.append("UPPER route:")
        elif final_run_total == "LOWER":
            run_total.append("LOWER route:")

    if run_total_retro_ack_id:
        run_total.append(f"run_total-retro-ack: {run_total_retro_ack_id}")

    win_loss_feature_token_required = ("model-pass-defense:" in win_loss) or ("model-defense:" in win_loss)
    # 중복 제거(순서 보존)
    def _dedup(xs):
        seen = set(); out = []
        for x in xs:
            if x not in seen:
                seen.add(x); out.append(x)
        return out

    return {
        "win_loss": _dedup(win_loss),
        "run_total": _dedup(run_total),
        "win_loss_feature_token_required": win_loss_feature_token_required,
        "win_loss_conditional": ["risk-resolution:"],  # WARN_STRONG + 얇음자백 텍스트일 때만 (validator가 텍스트로 판정)
    }
