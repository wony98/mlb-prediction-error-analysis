# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""Validate narrator reasoning against the model-defense contract.

This is a pre-send guard. It blocks only contract failures:

- final WIN_LOSS differs from official model WIN_LOSS but the block does not contain
  "model-defense:" with concrete model support being rejected
- final WIN_LOSS passes a concrete official model WIN_LOSS side but the block does not
  contain "model-pass-defense:" with concrete model support being passed
- final concrete WIN_LOSS keeps/opposes a model side that has multiple historical-note
  model-side conflict cards but the block does not contain
  "opponent-route-review:"
- final RUN_TOTAL differs from official model RUN_TOTAL, or passes against a concrete model
  RUN_TOTAL side, but the run total baseline does not include line-band, run_total-model-defense, and
  both "UPPER route" and "LOWER route"
- final RUN_TOTAL agrees with official model RUN_TOTAL but the run total baseline does not include
  line-band and the matching dominant route
- final WIN_LOSS is kept on a calibrated hard guard-review game but the block does
  not contain "guard-review:"
- final WIN_LOSS is kept on a strong-review game after the WIN_LOSS line itself admits
  thin/contradictory risk, but the block does not contain "risk-resolution:"
- final WIN_LOSS/RUN_TOTAL trips a narrow must-review signal but the matching axis line does
  not contain "signal-ack: <gate>"
- any in-scope game has no matched reasoning block

It does not judge whether the prediction is right, and it never changes predictions.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent
OUT_ROOT = ROOT / "postmortem" / "fullread_v4" / "model_defense_context"
GUARD_DIR = ROOT / "postmortem" / "fullread_v4" / "pregame_signal_guards"
SNAPSHOT_ROOT = ROOT / "postmortem" / "fullread_v4" / "model_reason_snapshots"
T038_ROOT = ROOT / "postmortem" / "fullread_v4" / "t038_pregame_signal_cards"

SELF_ADMITTED_WIN_LOSS_RISK_PATTERNS = [
    "우위가 묽",
    "우위가 얇",
    "얇지만",
    "thin edge",
    "thin-edge",
    "weak edge",
    "경고 win_loss-",
    "win_loss-cc",
    "win_loss-ops",
    "win_loss-ld",
    "close call",
    "coinflip",
]


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, float) and math.isnan(value):
        return False
    return str(value).strip().lower() not in {"", "nan", "none", "null"}


def _clean(value: Any) -> str:
    return str(value).strip() if _present(value) else ""


def _num(value: Any, default: float = 0.0) -> float:
    text = _clean(value)
    if not text:
        return default
    try:
        x = float(text)
        if math.isnan(x):
            return default
        return x
    except Exception:
        return default


def _bool(row: dict[str, Any], key: str) -> bool:
    return _clean(row.get(key)).lower() in {"1", "1.0", "true", "yes"}


def _date_iso(raw: str) -> str:
    value = str(raw or "").strip()
    if "-" in value:
        return value
    if len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:8]}"
    raise ValueError(f"invalid date: {raw!r}")


def _parse_gamepks(value: Any) -> set[str]:
    if not value:
        return set()
    if isinstance(value, (list, tuple, set)):
        parts = value
    else:
        parts = str(value).replace(";", ",").split(",")
    out = set()
    for part in parts:
        text = str(part).strip()
        if not text:
            continue
        try:
            text = str(int(float(text)))
        except Exception:
            pass
        out.add(text)
    return out


def _norm_game_pk(value: Any) -> str:
    text = _clean(value)
    if not text:
        return ""
    try:
        return str(int(float(text)))
    except Exception:
        return text


def _read_guard_rows(date: str) -> dict[str, dict[str, str]]:
    path = GUARD_DIR / f"mlb_narrator_pregame_signal_guard_{date}.csv"
    if not path.exists():
        return {}
    import csv

    out: dict[str, dict[str, str]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            game_pk = _clean(row.get("gamePk"))
            if game_pk:
                out[game_pk] = row
    return out


def _hard_guard_review_gates(row: dict[str, str] | None) -> list[str]:
    """Calibrated narrow WIN_LOSS review gates.

    These are not auto-remove or auto-flip gates. They only force the narrator
    to explicitly address a walk-forward-supported miss-prone cluster before
    sending a concrete WIN_LOSS side.
    """
    if not row:
        return []
    lineup_delta = _num(row.get("prediction_lineup_ops_delta"))
    player_xwoba = _num(row.get("player_lineup_xwoba_diff"))
    hand_ops = _num(row.get("hand_lineup_split_ops_diff"))

    lineup_bad = lineup_delta <= -0.18
    player_split_bad = player_xwoba <= -0.025 or hand_ops <= -0.120

    gates: list[str] = []
    if lineup_bad and player_split_bad:
        gates.append("win_loss_lineup_player_split_combo")
    return gates


def _matchup(row: pd.Series) -> str:
    return f"{_clean(row.get('away_team'))} @ {_clean(row.get('home_team'))}"


def _model_win_loss_side(row: pd.Series) -> str:
    prediction_team = _clean(row.get("prediction_team"))
    if prediction_team and prediction_team == _clean(row.get("home_team")):
        return "HOME"
    if prediction_team and prediction_team == _clean(row.get("away_team")):
        return "AWAY"
    return "-"


def _model_run_total_side(row: pd.Series) -> str:
    side = _clean(row.get("run_total_prediction_side")).upper()
    if side in {"UPPER", "LOWER"}:
        return side
    side = _clean(row.get("run_total_prediction")).upper()
    if side in {"UPPER", "LOWER"}:
        return side
    return "-"


def _reasoning_block_records(text: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for block in re.split(r"(?m)(?=^### )", text or ""):
        m = re.match(
            r"^###\s+(.+?)(?:\s+\[gamePk=(\d+)\])?\s+(?:\S*\s*)?RUN_TOTAL\b",
            block,
        )
        if m:
            records.append({
                "matchup": m.group(1).strip(),
                "gamePk": _norm_game_pk(m.group(2)),
                "block": block,
            })
    return records


def _reasoning_blocks(text: str) -> dict[str, str]:
    """Compatibility matchup map for callers that do not need DH identity."""
    out: dict[str, str] = {}
    for record in _reasoning_block_records(text):
        out[record["matchup"]] = record["block"]
    return out


def _parse_prediction(block: str) -> dict[str, str]:
    win_loss = run_total = "-"
    for line in (block or "").splitlines():
        text = line.strip()
        m = re.search(
            r"(?:예측|최종예측|FINAL_PREDICTION|\?)?\s*:?\s*.*?\bWIN_LOSS\s*=\s*(HOME|AWAY|-)(?=\s|$).*?\bRUN_TOTAL\s*=\s*(UPPER|LOWER|-)(?=\s|$)",
            text,
            re.I,
        )
        if m:
            win_loss, run_total = m.group(1).upper(), m.group(2).upper()
    return {"win_loss": win_loss, "run_total": run_total}


def _win_loss_line(block: str) -> str:
    for line in (block or "").splitlines():
        text = line.strip()
        if text.lower().startswith("win_loss:"):
            return text
    return ""


def _run_total_baseline(block: str) -> str:
    for line in (block or "").splitlines():
        text = line.strip()
        if text.lower().startswith("run_total:"):
            return text
    return ""


def _axis_line(block: str, axis: str) -> str:
    return _win_loss_line(block) if axis.upper() == "WIN_LOSS" else _run_total_baseline(block)


def _read_model_snapshots(date: str) -> dict[str, dict[str, Any]]:
    snapshot_root = Path(
        os.environ.get(
            "MLB_MODEL_REASON_SNAPSHOT_OUT_ROOT",
            str(SNAPSHOT_ROOT),
        )
    )
    path = snapshot_root / f"mlb_model_reason_snapshot_{date}.jsonl"
    if not path.exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8-sig") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if _clean(row.get("date")) != date:
                continue
            game_pk = _norm_game_pk(row.get("gamePk"))
            if not game_pk:
                continue
            old = out.get(game_pk)
            old_key = _clean(old.get("generated_at_kst")) if old else ""
            new_key = _clean(row.get("generated_at_kst"))
            if old is None or new_key >= old_key:
                out[game_pk] = row
    return out


def _read_t038_model_win_loss_conflicts(date: str) -> dict[str, list[dict[str, Any]]]:
    path = T038_ROOT / f"pregame_signal_prompt_view_{date}.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for game in payload.get("games") or []:
        game_pk = _norm_game_pk(game.get("gamePk"))
        if not game_pk:
            continue
        hits = []
        for card in game.get("prominent_cards") or []:
            conflicts = [str(x) for x in (card.get("conflicts_with") or [])]
            if any(x.startswith("MODEL_WIN_LOSS:") for x in conflicts):
                hits.append(card)
        if hits:
            out[game_pk] = hits
    return out


def _snapshot_support_items(snapshot: dict[str, Any] | None, model_win_loss: str) -> list[dict[str, Any]]:
    if not snapshot:
        return []
    if model_win_loss == "HOME":
        return list(snapshot.get("top_home_positive_features") or [])
    if model_win_loss == "AWAY":
        return list(snapshot.get("top_away_positive_features") or [])
    return list(snapshot.get("top_abs_features") or [])


def _snapshot_support_tokens(snapshot: dict[str, Any] | None, model_win_loss: str) -> list[str]:
    tokens: list[str] = []
    for item in _snapshot_support_items(snapshot, model_win_loss)[:8]:
        feature = _clean(item.get("feature"))
        if feature:
            tokens.append(feature.lower())
            tokens.extend(part for part in re.split(r"[^a-z0-9_]+", feature.lower()) if len(part) >= 6)
    return sorted(set(tokens), key=len, reverse=True)


def _line_has_model_feature(line: str, snapshot: dict[str, Any] | None, model_win_loss: str) -> bool:
    lower = (line or "").lower()
    return any(token and token in lower for token in _snapshot_support_tokens(snapshot, model_win_loss))


def _model_win_loss_is_concrete(row: pd.Series, snapshot: dict[str, Any] | None, model_win_loss: str) -> bool:
    if model_win_loss not in {"HOME", "AWAY"}:
        return False
    if _clean(row.get("win_loss_zone")).lower() == "value":
        return True
    p = _num(row.get("prediction_p"), 0.0)
    if p >= 0.56:
        return True
    if _snapshot_support_items(snapshot, model_win_loss):
        return True
    return False


def _has_model_pass_defense(line: str) -> bool:
    return re.search(r"\bmodel-pass-defense\s*:", line or "", re.I) is not None


def _has_model_defense(line: str) -> bool:
    return re.search(r"\bmodel-defense\s*:", line or "", re.I) is not None


def _has_opponent_route_review(line: str) -> bool:
    return re.search(r"\bopponent-route-review\s*:", line or "", re.I) is not None


def _model_defense_quality_violations(
    *,
    row: pd.Series,
    game_pk: str,
    matchup: str,
    block: str,
    snapshot: dict[str, Any] | None,
    model_win_loss: str,
    final_win_loss: str,
    t038_conflicts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Transparency-only WIN_LOSS checks.

    These checks force the narrator to show how model evidence was weighed.
    They intentionally do not say the narrator must keep, pass, or oppose the
    model side.
    """
    out: list[dict[str, Any]] = []
    win_loss_line = _win_loss_line(block)
    if not win_loss_line:
        return out
    model_team = _clean(row.get("prediction_team"))
    concrete_model = _model_win_loss_is_concrete(row, snapshot, model_win_loss)

    if final_win_loss == "-" and concrete_model:
        if not _has_model_pass_defense(win_loss_line):
            out.append(
                {
                    "gamePk": game_pk,
                    "matchup": matchup,
                    "axis": "WIN_LOSS",
                    "model": model_win_loss,
                    "final": final_win_loss,
                    "model_team": model_team,
                    "error": "WIN_LOSS model concrete but narrator pass missing literal model-pass-defense:",
                }
            )
        elif not _line_has_model_feature(win_loss_line, snapshot, model_win_loss):
            out.append(
                {
                    "gamePk": game_pk,
                    "matchup": matchup,
                    "axis": "WIN_LOSS",
                    "model": model_win_loss,
                    "final": final_win_loss,
                    "model_team": model_team,
                    "error": "WIN_LOSS model-pass-defense missing concrete model feature/support reference",
                }
            )

    if final_win_loss in {"HOME", "AWAY"} and model_win_loss in {"HOME", "AWAY"} and final_win_loss != model_win_loss:
        if not _has_model_defense(win_loss_line):
            out.append(
                {
                    "gamePk": game_pk,
                    "matchup": matchup,
                    "axis": "WIN_LOSS",
                    "model": model_win_loss,
                    "final": final_win_loss,
                    "model_team": model_team,
                    "error": "WIN_LOSS override missing literal model-defense:",
                }
            )
        elif not _line_has_model_feature(win_loss_line, snapshot, model_win_loss):
            out.append(
                {
                    "gamePk": game_pk,
                    "matchup": matchup,
                    "axis": "WIN_LOSS",
                    "model": model_win_loss,
                    "final": final_win_loss,
                    "model_team": model_team,
                    "error": "WIN_LOSS model-defense missing concrete model feature/support reference",
                }
            )

    conflict_cards = [
        {
            "category": _clean(card.get("category")),
            "strength": _clean(card.get("strength")),
            "summary": _clean(card.get("summary"))[:180],
        }
        for card in t038_conflicts
    ]
    if final_win_loss in {"HOME", "AWAY"} and model_win_loss in {"HOME", "AWAY"} and len(conflict_cards) >= 2:
        if not _has_opponent_route_review(win_loss_line):
            out.append(
                {
                    "gamePk": game_pk,
                    "matchup": matchup,
                    "axis": "WIN_LOSS",
                    "model": model_win_loss,
                    "final": final_win_loss,
                    "model_team": model_team,
                    "conflict_cards": conflict_cards[:5],
                    "error": "WIN_LOSS model-side historical-note conflicts require literal opponent-route-review:",
                }
            )
    return out


def _has_signal_ack(line: str, ack_id: str) -> bool:
    if not line or not ack_id:
        return False
    pattern = r"\bsignal-ack\s*:\s*" + re.escape(ack_id.lower()) + r"\b"
    return re.search(pattern, line.lower()) is not None


def _has_line_band(text: str) -> bool:
    return re.search(r"\bline[- ]band\s*:", text or "", re.I) is not None


def _has_run_total_model_defense(text: str) -> bool:
    return re.search(r"\brun_total-model-defense\s*:", text or "", re.I) is not None


def _has_upper_route(text: str) -> bool:
    return re.search(r"\bupper\s+route\s*:", text or "", re.I) is not None


def _has_lower_route(text: str) -> bool:
    return re.search(r"\blower\s+route\s*:", text or "", re.I) is not None


def _missing_run_total_contract_tokens(model_run_total: str, final_run_total: str, run_total_text: str) -> list[str]:
    """Return required RUN_TOTAL reasoning literals that are absent from the run total baseline.

    This guard checks whether the reasoning route was written, not whether the
    final side is correct. It deliberately avoids forcing UPPER/LOWER/Pass.
    """
    model_run_total = (model_run_total or "-").upper()
    final_run_total = (final_run_total or "-").upper()
    if final_run_total not in {"UPPER", "LOWER", "-"}:
        return []
    if final_run_total == "-" and model_run_total not in {"UPPER", "LOWER"}:
        return []

    missing: list[str] = []
    if not _has_line_band(run_total_text):
        missing.append("line-band:")

    needs_full_defense = model_run_total in {"UPPER", "LOWER"} and (
        final_run_total == "-" or (final_run_total in {"UPPER", "LOWER"} and final_run_total != model_run_total)
    )
    if needs_full_defense:
        if not _has_run_total_model_defense(run_total_text):
            missing.append("run_total-model-defense:")
        if not _has_upper_route(run_total_text):
            missing.append("UPPER route:")
        if not _has_lower_route(run_total_text):
            missing.append("LOWER route:")
        return missing

    if final_run_total == "UPPER" and not _has_upper_route(run_total_text):
        missing.append("UPPER route:")
    elif final_run_total == "LOWER" and not _has_lower_route(run_total_text):
        missing.append("LOWER route:")
    return missing


def _must_review_by_game(date: str) -> dict[str, list[dict[str, Any]]]:
    try:
        from _mlb_narrator_guard_context import must_review_annotations
    except Exception:
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for ann in must_review_annotations(date):
        game_pk = _norm_game_pk(ann.get("gamePk"))
        if not game_pk:
            continue
        items = ann.get("items") or []
        if items:
            out.setdefault(game_pk, []).extend(items)
    return out


def _self_admitted_win_loss_risk_reasons(block: str, guard_row: dict[str, str] | None) -> list[str]:
    """Find cases where the narrator says the risk out loud, then keeps WIN_LOSS.

    This is deliberately narrower than the postgame adherence ledger: it only
    fires pre-send when the pregame guard already called the game
    WARN_STRONG_MANUAL_REVIEW and the WIN_LOSS line itself contains a thin-edge /
    warning cue. The fix is not an automatic pass; the narrator must either
    write a concrete risk-resolution: defense or output WIN_LOSS=-.
    """
    if not guard_row:
        return []
    if _clean(guard_row.get("narrator_attention")) != "WARN_STRONG_MANUAL_REVIEW":
        return []
    line = _win_loss_line(block)
    if not line:
        return []
    hay = line.lower()
    hits = [pat for pat in SELF_ADMITTED_WIN_LOSS_RISK_PATTERNS if pat in hay]
    # Korean connective that often signals "I see the warning, but I still prediction it".
    if "경고" in line and ("있으나" in line or "하지만" in line or "but" in hay):
        hits.append("warning_but_kept")
    return sorted(set(hits))


def _has_risk_resolution(block: str) -> bool:
    lower = (block or "").lower()
    return "risk-resolution:" in lower or "guard-review:" in lower


def _run_total_retro_ack_requirements(date: str) -> dict[str, dict[str, Any]]:
    enabled = (os.environ.get("MLB_RUN_TOTAL_RETRO_ACK_CONTEXT_ENABLE") or "").strip().lower()
    disabled = (os.environ.get("MLB_RUN_TOTAL_RETRO_ACK_CONTEXT_DISABLE") or "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"} or disabled in {"1", "true", "yes", "on"}:
        return {}
    try:
        from _mlb_run_total_retro_dominance_context import build_payload

        payload = build_payload(date)
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for game in payload.get("games") or []:
        if not game.get("ack_required"):
            continue
        gp = _norm_game_pk(game.get("gamePk"))
        if gp:
            out[gp] = game
    return out


def _has_run_total_retro_ack(run_total_baseline: str, ack_id: str) -> bool:
    lower = (run_total_baseline or "").lower()
    if "run_total-retro-ack:" not in lower:
        return False
    return not ack_id or ack_id.lower() in lower


def validate_text(date: str, reasoning_text: str, gamepks: Any = None) -> dict[str, Any]:
    date = _date_iso(date)
    v3 = Path(os.environ.get("MLB_C3_V3_PATH") or (ROOT / f"_v3_games_{date}.csv"))
    report = {
        "schema": "mlb_narr_model_defense_validate.v1.4",
        "date": date,
        "ok": False,
        "reasoning_len": len(reasoning_text or ""),
        "checked_games": 0,
        "expected_games": 0,
        "violations": [],
        "warnings": [],
        "guardrail": "Pre-send contract validator only. No prediction mutation, no Notification, no auto flip/remove/pass.",
    }
    if not v3.exists():
        report["warnings"].append(f"missing {v3.name}")
        report["ok"] = True
        return report
    df = pd.read_csv(v3)
    report["expected_games"] = int(len(df))
    wanted = _parse_gamepks(gamepks)
    if wanted and "gamePk" in df.columns:
        df = df[df["gamePk"].astype(str).str.replace(r"\.0$", "", regex=True).isin(wanted)].copy()
        report["expected_games"] = int(len(df))
    contract_path_raw = (os.environ.get("MLB_C3_CONTRACT_DATA_PATH") or "").strip()
    contract_data: dict[str, Any] = {}
    if contract_path_raw:
        contract_path = Path(contract_path_raw)
        if not contract_path.is_absolute():
            contract_path = ROOT / contract_path
        try:
            contract_data = json.loads(contract_path.read_text(encoding="utf-8"))
            report["contract_data_path"] = str(contract_path)
        except Exception as exc:
            report["violations"].append({
                "gamePk": "",
                "matchup": "",
                "axis": "BOTH",
                "error": f"C3 contract data unavailable: {type(exc).__name__}: {exc}",
            })
            contract_data = {}
    if contract_path_raw and contract_data:
        guard_rows = contract_data.get("guard_rows") or {}
        must_review_rows = contract_data.get("must_review_rows") or {}
        run_total_retro_ack_rows = contract_data.get("run_total_retro_ack_rows") or {}
        model_snapshots = contract_data.get("model_snapshots") or {}
        t038_win_loss_conflicts = contract_data.get("t038_win_loss_conflicts") or {}
    else:
        guard_rows = _read_guard_rows(date)
        must_review_rows = _must_review_by_game(date)
        run_total_retro_ack_rows = _run_total_retro_ack_requirements(date)
        model_snapshots = _read_model_snapshots(date)
        t038_win_loss_conflicts = _read_t038_model_win_loss_conflicts(date)

    block_records = _reasoning_block_records(reasoning_text or "")
    blocks_by_game_pk = {
        record["gamePk"]: record["block"]
        for record in block_records
        if record.get("gamePk")
    }
    blocks_by_matchup: dict[str, list[str]] = {}
    for record in block_records:
        blocks_by_matchup.setdefault(record["matchup"], []).append(record["block"])
    matchup_offsets: dict[str, int] = {}
    missing_blocks = []
    for _, row in df.iterrows():
        matchup = _matchup(row)
        game_pk = _norm_game_pk(row.get("gamePk"))
        block = blocks_by_game_pk.get(game_pk, "")
        if not block:
            candidates = blocks_by_matchup.get(matchup, [])
            offset = matchup_offsets.get(matchup, 0)
            if offset < len(candidates):
                block = candidates[offset]
                matchup_offsets[matchup] = offset + 1
        if not block:
            missing_blocks.append({"gamePk": game_pk, "matchup": matchup})
            continue
        report["checked_games"] += 1
        prediction = _parse_prediction(block)
        model_win_loss = _model_win_loss_side(row)
        model_run_total = _model_run_total_side(row)
        final_win_loss = prediction["win_loss"]
        final_run_total = prediction["run_total"]
        lower_block = block.lower()
        guard_row = guard_rows.get(game_pk)
        snapshot = model_snapshots.get(game_pk)
        hard_guard_gates = _hard_guard_review_gates(guard_row)
        if final_win_loss in {"HOME", "AWAY"} and hard_guard_gates:
            if "guard-review:" not in lower_block:
                report["violations"].append(
                    {
                        "gamePk": game_pk,
                        "matchup": matchup,
                        "axis": "WIN_LOSS",
                        "final": final_win_loss,
                        "gates": hard_guard_gates,
                        "error": "WIN_LOSS hard guard-review gate missing literal guard-review:",
                    }
                )
        self_risk_reasons = _self_admitted_win_loss_risk_reasons(block, guard_row)
        if final_win_loss in {"HOME", "AWAY"} and self_risk_reasons and not _has_risk_resolution(block):
            report["violations"].append(
                {
                    "gamePk": game_pk,
                    "matchup": matchup,
                    "axis": "WIN_LOSS",
                    "final": final_win_loss,
                    "risk_reasons": self_risk_reasons,
                    "error": "WIN_LOSS self-admitted strong-review risk missing literal risk-resolution:",
                }
            )
        report["violations"].extend(
            _model_defense_quality_violations(
                row=row,
                game_pk=game_pk,
                matchup=matchup,
                block=block,
                snapshot=snapshot,
                model_win_loss=model_win_loss,
                final_win_loss=final_win_loss,
                t038_conflicts=t038_win_loss_conflicts.get(game_pk, []),
            )
        )
        for item in must_review_rows.get(game_pk, []):
            leg = _clean(item.get("leg")).upper()
            ack_id = _clean(item.get("ack_id") or item.get("gate"))
            if leg not in {"WIN_LOSS", "RUN_TOTAL"} or not ack_id:
                continue
            required = False
            if leg == "WIN_LOSS":
                # v1 rule: if model-side concrete WIN_LOSS support is present and
                # the narrator passes or opposes the model WIN_LOSS side, the WIN_LOSS line
                # must acknowledge that signal. Keeping the model side does not
                # require an ack, avoiding broad token noise.
                required = (
                    final_win_loss == "-"
                    or (
                        final_win_loss in {"HOME", "AWAY"}
                        and model_win_loss in {"HOME", "AWAY"}
                        and final_win_loss != model_win_loss
                    )
                )
            elif leg == "RUN_TOTAL":
                # v1 rule: if model-side UPPER route support exists and the
                # narrator chooses the opposite total, the run total baseline must ack it.
                # A pass does not trigger the token in v1.
                required = (
                    final_run_total in {"UPPER", "LOWER"}
                    and model_run_total in {"UPPER", "LOWER"}
                    and final_run_total != model_run_total
                )
            if required and not _has_signal_ack(_axis_line(block, leg), ack_id):
                report["violations"].append(
                    {
                        "gamePk": game_pk,
                        "matchup": matchup,
                        "axis": leg,
                        "gate": item.get("gate"),
                        "ack_id": ack_id,
                        "required_when": item.get("required_when"),
                        "error": "must-review signal missing axis-scoped signal-ack:",
                    }
                )
        run_total_baseline = _run_total_baseline(block)
        retro_req = run_total_retro_ack_rows.get(game_pk)
        if retro_req and not _has_run_total_retro_ack(run_total_baseline, _clean(retro_req.get("ack_id"))):
            report["violations"].append(
                {
                    "gamePk": game_pk,
                    "matchup": matchup,
                    "axis": "RUN_TOTAL",
                    "final": final_run_total,
                    "ack_id": retro_req.get("ack_id"),
                    "retro_dominance": retro_req.get("retro_dominance"),
                    "dominance_class": retro_req.get("dominance_class"),
                    "error": "RUN_TOTAL strong same-line-band retrospective dominance missing literal run_total-retro-ack:",
                }
            )
        missing_run_total_tokens = _missing_run_total_contract_tokens(model_run_total, final_run_total, run_total_baseline)
        if missing_run_total_tokens:
            needs_full_defense = model_run_total in {"UPPER", "LOWER"} and (
                final_run_total == "-"
                or (final_run_total in {"UPPER", "LOWER"} and final_run_total != model_run_total)
            )
            report["violations"].append(
                {
                    "gamePk": _clean(row.get("gamePk")),
                    "matchup": matchup,
                    "axis": "RUN_TOTAL",
                    "model": model_run_total,
                    "final": final_run_total,
                    "missing_tokens": missing_run_total_tokens,
                    "error": (
                        "RUN_TOTAL model-different/pass reasoning missing required line-band/run_total-model-defense/routes"
                        if needs_full_defense
                        else "RUN_TOTAL model-aligned/concrete reasoning missing required line-band/dominant route"
                    ),
                }
            )
    if missing_blocks:
        report["warnings"].append(f"missing reasoning blocks: {len(missing_blocks)}")
        report["missing_blocks"] = missing_blocks[:20]
        for missing in missing_blocks:
            report["violations"].append(
                {
                    "gamePk": missing.get("gamePk"),
                    "matchup": missing.get("matchup"),
                    "axis": "BLOCK",
                    "error": "missing reasoning block for in-scope game",
                }
            )
    report["ok"] = not report["violations"]
    return report


def write_report(report: dict[str, Any], scope: str = "") -> Path:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    suffix = f"__{scope}" if scope else ""
    path = OUT_ROOT / f"mlb_narr_model_defense_validate_{report['date']}{suffix}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate MLB narrator model-defense contract.")
    ap.add_argument("--date", required=True)
    ap.add_argument("--reasoning", required=True)
    ap.add_argument("--gamepks", default="")
    ap.add_argument("--scope", default="")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()

    text = Path(args.reasoning).read_text(encoding="utf-8", errors="replace")
    report = validate_text(args.date, text, gamepks=args.gamepks)
    if args.write:
        path = write_report(report, scope=args.scope)
        print(f"[model-defense-validate] wrote {path}")
    if report["ok"]:
        print(f"[model-defense-validate] ok checked={report['checked_games']} warnings={len(report['warnings'])}")
        return 0
    print(
        f"[model-defense-validate] violations={len(report['violations'])} checked={report['checked_games']}",
        flush=True,
    )
    try:
        print(json.dumps(report["violations"][:10], ensure_ascii=False, indent=2))
    except UnicodeEncodeError:
        print(json.dumps(report["violations"][:10], ensure_ascii=True, indent=2))
    return 2 if args.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
