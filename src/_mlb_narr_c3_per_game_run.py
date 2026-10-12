# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""Research-only C3 per-game MLB narrator runner.

This script builds one prompt/context bundle per game and, only when explicitly
requested with --run-bridge, calls the Codex bridge for each game.  It never
sends Notification and never writes production narrator prediction files.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
import _mlb_direction_contrast_contract as DCC
import _mlb_direction_contrast_call_ledger as CALL_LEDGER
import _mlb_knowable_compliance as KC
import _mlb_narr_contract_requirements as REQ
import _narr_model_defense_validate as MDV

ROOT = Path(__file__).resolve().parent
PY = sys.executable
OUT_ROOT = ROOT / "postmortem" / "fullread_v4" / "c3_shadow"


def _kst_now() -> str:
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9))).replace(microsecond=0).isoformat()


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes((text or "").encode("utf-8"))


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    proc = subprocess.run([PY, *args], cwd=str(ROOT), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        tail = ((proc.stdout or "") + "\n" + (proc.stderr or ""))[-2000:]
        raise RuntimeError(f"command failed rc={proc.returncode}: py {' '.join(args)}\n{tail}")
    return proc


def _scope_id(gamepks: list[str]) -> str:
    return "g" + hashlib.sha1(",".join(sorted(gamepks)).encode("utf-8")).hexdigest()[:8]


def _load_v3_rows(date: str, gamepks: set[str] | None = None, path: Path | None = None) -> list[dict[str, str]]:
    path = path or (ROOT / f"_v3_games_{date}.csv")
    if not path.exists():
        raise FileNotFoundError(path)
    rows: list[dict[str, str]] = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            gp = _clean(row.get("gamePk")).replace(".0", "")
            if gamepks and gp not in gamepks:
                continue
            if not _clean(row.get("prediction_team")):
                continue
            row["gamePk"] = gp
            rows.append(row)
    return rows


def _ensure_c1_c2(date: str, *, force: bool = False) -> None:
    if force or not (ROOT / f"_decision_cards_{date}.json").exists():
        _run(["_mlb_decision_cards.py", date])
    if force or not (ROOT / f"_game_notepads_{date}.json").exists():
        _run(["_mlb_game_notepad.py", date])


def _require_input_snapshot() -> tuple[Path, str]:
    raw_path = _clean(os.environ.get("MLB_PREGAME_INPUT_SNAPSHOT"))
    expected = _clean(os.environ.get("MLB_PREGAME_INPUT_SNAPSHOT_SHA256"))
    if not raw_path or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("C3 primary requires non-empty MLB_PREGAME_INPUT_SNAPSHOT and valid SHA-256")
    path = Path(raw_path)
    if not path.is_absolute():
        path = ROOT / path
    if not path.is_file():
        raise FileNotFoundError(path)
    actual = _sha256_file(path)
    if actual != expected:
        raise ValueError(f"C3 input snapshot hash mismatch: expected={expected} actual={actual}")
    return path.resolve(), expected


def _notepad_index(date: str, path: Path | None = None) -> dict[str, dict[str, Any]]:
    path = path or (ROOT / f"_game_notepads_{date}.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(x.get("gamePk")): x for x in data if isinstance(x, dict)}


def _decision_cards(date: str, path: Path | None = None) -> dict[str, dict[str, Any]]:
    path = path or (ROOT / f"_decision_cards_{date}.json")
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(x.get("gamePk")): x for x in data if isinstance(x, dict)}


def _maybe_build_contexts(date: str, gamepks_csv: str, build_contexts: bool) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    if build_contexts:
        _run(["_narr_model_defense_context.py", "--date", date, "--write"], check=False)
        _run(["_mlb_correct_judgment_standards_context.py", "--date", date, "--gamepks", gamepks_csv, "--write"], check=False)
        _run(["_mlb_pregame_signal_cards.py", "--date", date, "--gamepks", gamepks_csv, "--write"], check=False)
        _run(
            [
                "_mlb_direction_policy_family_operational_advisory.py",
                "--date",
                date,
                "--write",
            ],
            check=True,
        )
    paths["model_defense"] = ROOT / "postmortem" / "fullread_v4" / "model_defense_context" / f"mlb_narr_model_defense_context_{date}.txt"
    # Scope-specific judgment path.
    scope = "__" + _scope_id([x for x in gamepks_csv.split(",") if x])
    paths["judgment_standards"] = (
        ROOT / "postmortem" / "raw_pbp_fullflow" / "judgment_criteria" / "correct_judgment_standards" / "live_context" /
        f"mlb_correct_judgment_standards_context_{date}{scope}.md"
    )
    paths["judgment_standards_json"] = paths["judgment_standards"].with_suffix(".json")
    paths["t038_prompt_view"] = (
        ROOT / "postmortem" / "fullread_v4" / "t038_pregame_signal_cards" /
        f"pregame_signal_prompt_view_{date}{scope}.json"
    )
    paths["policy_family_advisory"] = (
        ROOT
        / "postmortem"
        / "fullread_v4"
        / "direction_criterion_season_2026"
        / "retrieval"
        / "criterion_reliability_dossier"
        / "policy_family_pilot_v1"
        / "operational_advisory"
        / f"mlb_policy_family_advisory_{date}.md"
    )
    if build_contexts and not paths["policy_family_advisory"].is_file():
        raise FileNotFoundError(paths["policy_family_advisory"])
    return paths


def _manifest_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _load_frozen_manifest(path: Path, date: str) -> tuple[dict[str, Any], dict[str, Path]]:
    if not path.exists():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != "mlb_narr_c3_frozen_replay_manifest.v1":
        raise ValueError("unsupported frozen replay manifest schema")
    if _clean(payload.get("date")) != date:
        raise ValueError("frozen replay manifest date mismatch")
    snapshot_sha = _clean(payload.get("input_snapshot_sha256"))
    if not re.fullmatch(r"[0-9a-f]{64}", snapshot_sha):
        raise ValueError("frozen replay manifest has no valid input_snapshot_sha256")
    resolved: dict[str, Path] = {}
    for name, item in (payload.get("files") or {}).items():
        file_path = _manifest_path(_clean((item or {}).get("path")))
        expected = _clean((item or {}).get("sha256"))
        if not file_path.is_file():
            raise FileNotFoundError(f"frozen manifest file missing: {name}={file_path}")
        actual = _sha256_file(file_path)
        if not expected or actual != expected:
            raise ValueError(f"frozen manifest hash mismatch: {name} expected={expected} actual={actual}")
        resolved[name] = file_path
    required = {
        "input_snapshot", "v3", "narr_context", "decision_cards", "notepad_index",
        "model_defense", "judgment_standards", "judgment_standards_json",
        "t038_prompt_view", "contract_data",
    }
    missing = sorted(required - set(resolved))
    if missing:
        raise ValueError(f"frozen manifest missing required files: {missing}")
    if _sha256_file(resolved["input_snapshot"]) != snapshot_sha:
        raise ValueError("frozen snapshot hash does not match manifest input_snapshot_sha256")
    return payload, resolved


def _row_sha(row: dict[str, Any]) -> str:
    return _sha256_text(json.dumps(row, ensure_ascii=False, sort_keys=True))


def _context_bundle(date: str, row: dict[str, str], notepad_path: Path, card: dict[str, Any], context_paths: dict[str, Path], out_dir: Path) -> dict[str, Any]:
    gp = _clean(row.get("gamePk"))
    manifest: dict[str, Any] = {
        "schema": "mlb_c3_context_bundle_manifest.v1",
        "date": date,
        "scope": out_dir.name,
        "gamePk": gp,
        "matchup": f"{_clean(row.get('away_team'))} @ {_clean(row.get('home_team'))}",
        "created_at_kst": _kst_now(),
        "v3_row_sha256": _row_sha(row),
        "notepad_path": str(notepad_path.relative_to(ROOT)) if notepad_path.exists() else str(notepad_path),
        "notepad_sha256": _sha256_file(notepad_path) if notepad_path.exists() else "",
        "decision_card_sha256": _sha256_text(json.dumps(card or {}, ensure_ascii=False, sort_keys=True)),
        "component_projection": {"strategy": "gamePk-scoped-v1", "gamePk": gp},
        "components": {},
    }
    snap = _clean(os.environ.get("MLB_PREGAME_INPUT_SNAPSHOT"))
    snap_sha = _clean(os.environ.get("MLB_PREGAME_INPUT_SNAPSHOT_SHA256"))
    if snap:
        manifest["input_snapshot_path"] = snap
        manifest["input_snapshot_sha256"] = snap_sha
    frozen_manifest = _clean(os.environ.get("MLB_C3_FROZEN_MANIFEST"))
    frozen_manifest_sha = _clean(os.environ.get("MLB_C3_FROZEN_MANIFEST_SHA256"))
    if frozen_manifest:
        manifest["frozen_input_manifest"] = frozen_manifest
        manifest["frozen_input_manifest_sha256"] = frozen_manifest_sha
    for name, path in context_paths.items():
        manifest["components"][name] = {
            "path": str(path.relative_to(ROOT)) if path.exists() else str(path),
            "exists": path.exists(),
            "sha256": _sha256_file(path) if path.exists() else "",
        }
    bundle_hash = _sha256_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    manifest["context_bundle_sha256"] = bundle_hash
    _write_json(out_dir / "bundles" / f"{gp}.json", manifest)
    return manifest


def _truncate_component(text: str, char_limit: int) -> str:
    if len(text) <= char_limit:
        return text
    return text[:char_limit] + "\n...[truncated after gamePk-scoped extraction]\n"


def _extract_text_game_block(text: str, game_pk: str, marker_pattern: str, *, include_preamble: bool) -> str:
    markers = list(re.finditer(marker_pattern, text, flags=re.MULTILINE))
    if not markers:
        return ""
    target_index = next((i for i, marker in enumerate(markers) if marker.group(1) == game_pk), None)
    if target_index is None:
        return ""
    start = markers[target_index].start()
    end = markers[target_index + 1].start() if target_index + 1 < len(markers) else len(text)
    preamble = text[:markers[0].start()].rstrip() if include_preamble else ""
    block = text[start:end].strip()
    return (preamble + "\n\n" + block).strip() if preamble else block


def _extract_t038_game(text: str, game_pk: str) -> str:
    try:
        payload = json.loads(text)
    except Exception:
        return ""
    game = next((item for item in payload.get("games") or [] if str(item.get("gamePk")) == game_pk), None)
    if not game:
        return ""
    scoped = {
        "schema": payload.get("schema"),
        "date": payload.get("date"),
        "research_only": payload.get("research_only"),
        "warn_only": payload.get("warn_only"),
        "policy": payload.get("policy"),
        "game": game,
    }
    return json.dumps(scoped, ensure_ascii=False, separators=(",", ":"))


def _component_snippets(context_paths: dict[str, Path], game_pk: str, *, char_limit: int = 18000) -> dict[str, str]:
    snippets: dict[str, str] = {}
    for name, path in context_paths.items():
        if not path.exists() or not path.is_file():
            snippets[name] = ""
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if name == "model_defense":
            text = _extract_text_game_block(
                text,
                game_pk,
                r"^\[[^\]\n]+\]\s+gamePk=(\d+)\s*$",
                include_preamble=True,
            )
        elif name == "judgment_standards":
            text = _extract_text_game_block(
                text,
                game_pk,
                r"^###\s+.+?\s+gamePk=(\d+)\s*$",
                include_preamble=True,
            )
            lines = text.splitlines()
            run_total_start = next(
                (i for i, line in enumerate(lines) if "[오늘 RUN_TOTAL 득점환경]" in line or "[RUN_TOTAL 매칭 판단기준]" in line),
                None,
            )
            text = "\n".join(lines[run_total_start:]) if run_total_start is not None else ""
        elif name == "t038_prompt_view":
            text = _extract_t038_game(text, game_pk)
        elif name == "judgment_standards_json":
            text = ""
        snippets[name] = _truncate_component(text, char_limit)
    return snippets


def _blind_current_facts(notepad_text: str) -> str:
    """Keep current pregame facts while hiding model and historical answer labels."""
    text = re.sub(r"\s*\|\s*모델예측\(참고\):[^\n]*", "", notepad_text)
    cut = text.find("[⚠️ 기계 플래그]")
    if cut >= 0:
        text = text[:cut]
    cut = text.find("[판단기준 analog")
    if cut >= 0:
        text = text[:cut]
    return text.strip()


def _judgment_game_data(path: Path | None, game_pk: str) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    if not path or not path.exists():
        return [], {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    examples = payload.get("examples") or {}
    candidates = list((examples.get("win_loss_blind_candidates") or {}).get(game_pk) or [])
    revealed = dict((examples.get("win_loss_revealed_standards") or {}).get(game_pk) or {})
    return candidates, revealed


def _blind_review_prompt(
    date: str,
    row: dict[str, str],
    current_facts: str,
    candidates: list[dict[str, Any]],
) -> str:
    gp = _clean(row.get("gamePk"))
    matchup = f"{_clean(row.get('away_team'))} @ {_clean(row.get('home_team'))}"
    return f"""You are the outcome-blind applicability stage for one MLB narrator game.

Hard rules:
- Use only the current pregame facts and historical pregame facts below.
- Historical winners, correct predictions, model predictions, edge, final scores, and judgment directions are intentionally hidden.
- Decide which historical cases are structurally applicable to today's WIN_LOSS decision.
- Different pitcher names are allowed only when their visible pregame role/form structure is genuinely comparable.
- Do not infer or guess any hidden historical outcome.
- Independently state the current WIN_LOSS direction from current facts alone; use '-' if unresolved.
- Independently state the current RUN_TOTAL direction from current facts and the frozen total line alone; use '-' if unresolved.
- The independent RUN_TOTAL route is audit-only. Do not use the model RUN_TOTAL prediction or any historical outcome label.
- Select at most 3 candidate IDs. Reject superficial same-team or same-matchup resemblance.
- Output exactly one JSON object and no markdown.

Required schema:
{{
  "schema": "mlb_judgment_blind_review.v1",
  "date": "{date}",
  "gamePk": "{gp}",
  "matchup": "{matchup}",
  "independent_win_loss_route": "HOME|AWAY|-",
  "independent_run_total_route": "UPPER|LOWER|-",
  "applicable_ids": ["candidate_id"],
  "rejected": [{{"candidate_id": "id", "reason": "structural mismatch"}}],
  "comparison_summary": "concise comparison of starter, lineup, bullpen, handedness/injury/environment evidence",
  "run_total_comparison_summary": "concise threshold comparison of scoring, suppression, one-team blowout, and bullpen-tail routes",
  "outcomes_seen": false,
  "model_seen": false,
  "pre_knowable_only": true
}}

Current pregame facts:
{current_facts}

Outcome-blind historical candidates:
{json.dumps(candidates, ensure_ascii=False, indent=2)}
"""


def _validate_blind_review(payload: dict[str, Any], row: dict[str, str], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    gp = _clean(row.get("gamePk"))
    allowed_ids = {_clean(x.get("candidate_id")) for x in candidates if _clean(x.get("candidate_id"))}
    if _clean(payload.get("gamePk")) != gp:
        raise ValueError(f"{gp} blind review gamePk mismatch")
    route = _clean(payload.get("independent_win_loss_route")).upper() or "-"
    if route not in {"HOME", "AWAY", "-"}:
        raise ValueError(f"{gp} invalid blind independent_win_loss_route={route}")
    run_total_route_present = "independent_run_total_route" in payload
    run_total_route = _clean(payload.get("independent_run_total_route")).upper() or "-"
    if run_total_route not in {"UPPER", "LOWER", "-"}:
        raise ValueError(f"{gp} invalid blind independent_run_total_route={run_total_route}")
    selected: list[str] = []
    for value in payload.get("applicable_ids") or []:
        cid = _clean(value)
        if cid not in allowed_ids:
            raise ValueError(f"{gp} blind review selected unknown candidate={cid}")
        if cid not in selected:
            selected.append(cid)
    if len(selected) > 3:
        raise ValueError(f"{gp} blind review selected more than 3 candidates")
    if payload.get("outcomes_seen") is not False or payload.get("model_seen") is not False:
        raise ValueError(f"{gp} blind review did not attest outcome/model blindness")
    if payload.get("pre_knowable_only") is not True:
        raise ValueError(f"{gp} blind review is not pregame-only")
    payload["schema"] = "mlb_judgment_blind_review.v1"
    payload["date"] = _clean(row.get("date"))
    payload["gamePk"] = gp
    payload["matchup"] = f"{_clean(row.get('away_team'))} @ {_clean(row.get('home_team'))}"
    payload["independent_win_loss_route"] = route
    payload["independent_run_total_route"] = run_total_route
    payload["independent_run_total_route_present"] = run_total_route_present
    payload["applicable_ids"] = selected
    payload["candidate_ids_offered"] = sorted(allowed_ids)
    payload["outcomes_seen"] = False
    payload["model_seen"] = False
    payload["pre_knowable_only"] = True
    # A reused blind review must remain byte-identical to the validated source.
    # Newly generated reviews receive a timestamp; validated reused reviews keep
    # the timestamp that was part of their original frozen payload.
    payload.setdefault("created_at_kst", _kst_now())
    return payload


def _render_selected_standards(review: dict[str, Any], revealed: dict[str, dict[str, Any]]) -> str:
    selected = list(review.get("applicable_ids") or [])
    if not selected:
        return "(blind applicability passed no historical WIN_LOSS case; do not force an analog direction)"
    lines = ["Only these cases passed the outcome-blind applicability stage:"]
    for cid in selected:
        item = revealed.get(cid) or {}
        lines.extend([
            f"- candidate_id={cid} source_date={_clean(item.get('source_date'))} source_matchup={_clean(item.get('source_matchup'))}",
            f"  historical_correct_prediction={_clean(item.get('correct_prediction'))}",
            f"  conditional_standard={_clean(item.get('directional_judgment_standard'))}",
        ])
    return "\n".join(lines)


def _final_visible_blind_review(review: dict[str, Any]) -> dict[str, Any]:
    """Hide audit-only RUN_TOTAL judgment from the final narrator decision."""
    visible = dict(review)
    visible.pop("independent_run_total_route", None)
    visible.pop("independent_run_total_route_present", None)
    visible.pop("run_total_comparison_summary", None)
    return visible


def _attach_blind_review(
    bundle: dict[str, Any],
    out_dir: Path,
    game_pk: str,
    review_path: Path,
    review: dict[str, Any],
) -> dict[str, Any]:
    bundle = dict(bundle)
    components = dict(bundle.get("components") or {})
    components["judgment_blind_review"] = {
        "path": str(review_path.relative_to(ROOT)),
        "exists": True,
        "sha256": _sha256_file(review_path),
    }
    bundle["components"] = components
    bundle["judgment_applicable_ids"] = list(review.get("applicable_ids") or [])
    bundle["judgment_independent_win_loss_route"] = _clean(review.get("independent_win_loss_route")) or "-"
    bundle["judgment_independent_run_total_route"] = _clean(review.get("independent_run_total_route")) or "-"
    bundle["judgment_independent_run_total_route_present"] = bool(
        review.get("independent_run_total_route_present")
    )
    bundle.pop("context_bundle_sha256", None)
    bundle["context_bundle_sha256"] = _sha256_text(
        json.dumps(bundle, ensure_ascii=False, sort_keys=True)
    )
    _write_json(out_dir / "bundles" / f"{game_pk}.json", bundle)
    return bundle


def _model_side(row: dict[str, str]) -> str:
    prediction_team = _clean(row.get("prediction_team"))
    if prediction_team and prediction_team == _clean(row.get("home_team")):
        return "HOME"
    if prediction_team and prediction_team == _clean(row.get("away_team")):
        return "AWAY"
    return "-"


def _run_total_model_side(row: dict[str, str]) -> str:
    side = _clean(row.get("run_total_prediction_side") or row.get("run_total_prediction")).upper()
    return side if side in {"UPPER", "LOWER"} else "-"


def _t038_conflicts_from_path(path: Path | None) -> dict[str, list[dict[str, Any]]]:
    if not path or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for game in payload.get("games") or []:
        game_pk = MDV._norm_game_pk(game.get("gamePk"))
        hits = []
        for card in game.get("prominent_cards") or []:
            conflicts = [str(x) for x in (card.get("conflicts_with") or [])]
            if any(x.startswith("MODEL_WIN_LOSS:") for x in conflicts):
                hits.append(card)
        if game_pk and hits:
            out[game_pk] = hits
    return out


def _contract_data(date: str, t038_path: Path | None = None) -> dict[str, Any]:
    return {
        "guard_rows": MDV._read_guard_rows(date),
        "must_review_rows": MDV._must_review_by_game(date),
        "run_total_retro_ack_rows": MDV._run_total_retro_ack_requirements(date),
        "model_snapshots": MDV._read_model_snapshots(date),
        "t038_win_loss_conflicts": _t038_conflicts_from_path(t038_path) if t038_path else MDV._read_t038_model_win_loss_conflicts(date),
    }


def _token_present(line: str, token: str) -> bool:
    line_l = (line or "").lower()
    token_l = (token or "").lower()
    if not token_l:
        return True
    if token_l.startswith("signal-ack:"):
        return token_l in line_l
    if token_l.startswith("run_total-retro-ack:"):
        return token_l in line_l
    return token_l in line_l


def _apply_contract_requirements(date: str, row: dict[str, str], payload: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    gp = MDV._norm_game_pk(row.get("gamePk"))
    model_win_loss = _model_side(row)
    model_run_total = _run_total_model_side(row)
    final_win_loss = _clean(payload.get("win_loss")).upper() or "-"
    final_run_total = _clean(payload.get("run_total")).upper() or "-"
    snapshot = (data.get("model_snapshots") or {}).get(gp)
    guard_row = (data.get("guard_rows") or {}).get(gp)
    hard_guard_gates = MDV._hard_guard_review_gates(guard_row)
    must_review_items = (data.get("must_review_rows") or {}).get(gp, [])
    retro_req = (data.get("run_total_retro_ack_rows") or {}).get(gp) or {}
    t038_conflicts = (data.get("t038_win_loss_conflicts") or {}).get(gp, [])
    concrete_model_win_loss = MDV._model_win_loss_is_concrete(row, snapshot, model_win_loss)
    req = REQ.required_tokens(
        model_win_loss=model_win_loss,
        final_win_loss=final_win_loss,
        model_run_total=model_run_total,
        final_run_total=final_run_total,
        concrete_model_win_loss=concrete_model_win_loss,
        t038_conflict_count=len(t038_conflicts),
        hard_guard_gates=hard_guard_gates,
        must_review_items=must_review_items,
        run_total_retro_ack_id=_clean(retro_req.get("ack_id")),
    )
    win_loss_reason = _clean(payload.get("win_loss_reason"))
    run_total_reason = _clean(payload.get("run_total_reason"))
    win_loss_tokens_ok = all(_token_present(win_loss_reason, token) for token in req.get("win_loss") or [])
    run_total_tokens_ok = all(_token_present(run_total_reason, token) for token in req.get("run_total") or [])
    win_loss_feature_ok = True
    if req.get("win_loss_feature_token_required"):
        win_loss_feature_ok = MDV._line_has_model_feature(win_loss_reason, snapshot, model_win_loss)
    payload["contract_tokens_required"] = {
        "win_loss": req.get("win_loss") or [],
        "run_total": req.get("run_total") or [],
    }
    payload["contract_tokens_satisfied"] = {
        "win_loss": bool(win_loss_tokens_ok and win_loss_feature_ok),
        "run_total": bool(run_total_tokens_ok),
        "win_loss_feature_token": bool(win_loss_feature_ok),
    }
    payload["contract_tokens_source"] = "_mlb_narr_contract_requirements.required_tokens"
    payload["contract_context"] = {
        "concrete_model_win_loss": bool(concrete_model_win_loss),
        "t038_conflict_count": len(t038_conflicts),
        "hard_guard_gates": hard_guard_gates,
        "must_review_count": len(must_review_items),
        "run_total_retro_ack_id": _clean(retro_req.get("ack_id")),
    }
    return payload


def _contract_prompt_guidance(row: dict[str, str], data: dict[str, Any]) -> str:
    gp = MDV._norm_game_pk(row.get("gamePk"))
    model_win_loss = _model_side(row)
    model_run_total = _run_total_model_side(row)
    snapshot = (data.get("model_snapshots") or {}).get(gp)
    guard_row = (data.get("guard_rows") or {}).get(gp)
    hard_guard_gates = MDV._hard_guard_review_gates(guard_row)
    must_review_items = (data.get("must_review_rows") or {}).get(gp, [])
    retro_req = (data.get("run_total_retro_ack_rows") or {}).get(gp) or {}
    t038_conflicts = (data.get("t038_win_loss_conflicts") or {}).get(gp, [])
    concrete_model_win_loss = MDV._model_win_loss_is_concrete(row, snapshot, model_win_loss)

    win_loss_signal_acks = []
    run_total_signal_acks = []
    for item in must_review_items:
        leg = _clean(item.get("leg")).upper()
        ack = _clean(item.get("ack_id") or item.get("gate"))
        if not ack:
            continue
        if leg == "WIN_LOSS":
            win_loss_signal_acks.append(ack)
        elif leg == "RUN_TOTAL":
            run_total_signal_acks.append(ack)

    lines = [
        "Contract checklist for this game. Put required literals in win_loss_reason/run_total_reason, not only in evidence_refs.",
        f"- WIN_LOSS model side={model_win_loss}; concrete_model_win_loss={concrete_model_win_loss}; t038_conflict_count={len(t038_conflicts)}; hard_guard_gates={hard_guard_gates or []}.",
        f"- RUN_TOTAL model side={model_run_total}.",
    ]
    if concrete_model_win_loss:
        lines.append("- If WIN_LOSS='-', win_loss_reason must include exact literal `model-pass-defense:` plus a concrete model support feature/reference from model-defense context.")
    if model_win_loss in {"HOME", "AWAY"}:
        lines.append("- If WIN_LOSS is a concrete side different from the model WIN_LOSS side, win_loss_reason must include exact literal `model-defense:` plus a concrete model support feature/reference.")
    recall_contract = data.get("r1_baseline_recall") or {}
    recall_tokens = list(
        recall_contract.get("exact_model_support_tokens") or []
    )
    recall_policy = _clean(recall_contract.get("prompt_token_policy"))
    if recall_policy == "all_exact_support_tokens_explicit":
        support_tokens = recall_tokens
    else:
        support_tokens = MDV._snapshot_support_tokens(snapshot, model_win_loss)[:5]
    if concrete_model_win_loss and support_tokens:
        lines.append(
            "- For model-pass-defense/model-defense, cite at least one exact model support feature token: "
            + ", ".join(f"`{token}`" for token in support_tokens)
            + ". `prediction_p` or `prediction_edge` alone does not satisfy the validator."
        )
    if model_win_loss in {"HOME", "AWAY"} and len(t038_conflicts) >= 2:
        lines.append("- If WIN_LOSS is any concrete side, win_loss_reason must include exact literal `opponent-route-review:` because historical-note conflicts are >=2, even when keeping the model side.")
    if hard_guard_gates:
        lines.append("- If WIN_LOSS is any concrete side, win_loss_reason must include exact literal `guard-review:` and explain why the hard guard is resolved.")
    lines.append(
        "- If a concrete WIN_LOSS reason admits thin/weak/coinflip/close-call/contradictory risk, include exact literal "
        "`risk-resolution:` and explain why the selected side still survives that risk; otherwise output WIN_LOSS='-'."
    )
    if win_loss_signal_acks:
        lines.append("- If WIN_LOSS is pass or model override, include axis-scoped literal(s): " + ", ".join(f"`signal-ack: {x}`" for x in win_loss_signal_acks) + ".")

    if model_run_total in {"UPPER", "LOWER"}:
        lines.append("- RUN_TOTAL reason must include `line-band:` whenever RUN_TOTAL is UPPER/LOWER or pass against a concrete model RUN_TOTAL side.")
        lines.append("- If RUN_TOTAL is '-' or differs from the model RUN_TOTAL side, run_total_reason must include exact literals `run_total-model-defense:`, `UPPER route:`, and `LOWER route:`.")
        lines.append("- If RUN_TOTAL keeps model UPPER, run_total_reason must include `UPPER route:`. If RUN_TOTAL keeps model LOWER, include `LOWER route:`.")
    else:
        lines.append("- If RUN_TOTAL is UPPER/LOWER, run_total_reason must include `line-band:` plus the chosen route literal (`UPPER route:` or `LOWER route:`).")
    if run_total_signal_acks:
        lines.append("- If RUN_TOTAL differs from model RUN_TOTAL, include axis-scoped literal(s): " + ", ".join(f"`signal-ack: {x}`" for x in run_total_signal_acks) + ".")
    retro_ack = _clean(retro_req.get("ack_id"))
    if retro_ack:
        lines.append(f"- RUN_TOTAL strong retrospective dominance requires exact literal `run_total-retro-ack: {retro_ack}` in run_total_reason.")
    return "\n".join(lines)


def _prompt_text(
    date: str,
    row: dict[str, str],
    notepad_text: str,
    card: dict[str, Any],
    bundle: dict[str, Any],
    snippets: dict[str, str],
    contract_guidance: str,
    blind_review: dict[str, Any],
    selected_standards: str,
    knowable_inventory: dict[str, Any] | None = None,
    direction_contrasts: dict[str, dict[str, Any]] | None = None,
) -> str:
    gp = _clean(row.get("gamePk"))
    matchup = bundle.get("matchup")
    model_win_loss = _model_side(row)
    model_run_total = _run_total_model_side(row)
    run_total_baseline = _clean(row.get("run_total_baseline"))
    contrast_bundle_sha = (
        DCC.sha256_json(
            [direction_contrasts[axis] for axis in ("WIN_LOSS", "RUN_TOTAL")]
        )
        if direction_contrasts
        else ""
    )
    contrast_hard_rules = ""
    contrast_schema_fields = ""
    contrast_section = ""
    knowable_schema_fields = KC.prompt_schema_field(knowable_inventory) if knowable_inventory else ""
    if direction_contrasts:
        contrast_hard_rules = """
- A validated current-game direction contrast is provided. It is not a historical vote or an automatic direction.
- Derive one lean direction for each axis after reading both current-game routes.
- If unresolved, keep the lean in lean_direction, set release_status=PASS, and output "-" in win_loss/run_total for compatibility.
- If released, win_loss/run_total must equal the corresponding lean_direction.
"""
        contrast_schema_fields = f""",
  "lean_direction": {{"win_loss": "HOME|AWAY", "run_total": "UPPER|LOWER"}},
  "release_status": {{"win_loss": "RELEASE|PASS", "run_total": "RELEASE|PASS"}},
  "direction_contrast_bundle_sha256": "{contrast_bundle_sha}"
"""
        contrast_section = f"""
Validated current-game direction contrasts:
{json.dumps(direction_contrasts, ensure_ascii=False, indent=2)}
"""
    return f"""You are Codex C3, doing one isolated MLB narrator decision for one game only.

Hard rules:
- Use only pregame-knowable information inside the provided notepad/context.
- Do not use final score, live result, or postgame facts.
- The independent WIN_LOSS route was frozen by an outcome-blind stage. Copy it exactly into judgment_independent.win_loss; do not rewrite it after seeing model or historical outcomes.
- Historical WIN_LOSS outcomes below are revealed only for cases selected before outcomes/model were visible. Do not use an unselected historical case.
- Do not auto-flip because of one label. Reason from the full player/team/game context.
- If an axis is genuinely unresolved, output "-" for that axis.
- Required contract literals must be written inside win_loss_reason/run_total_reason exactly as shown below.
- Output exactly one JSON object and no markdown.
{contrast_hard_rules}

{contract_guidance}

Required JSON schema:
{{
  "schema": "mlb_narr_c3_prediction.v1",
  "date": "{date}",
  "scope": "{bundle.get('scope')}",
  "gamePk": "{gp}",
  "matchup": "{matchup}",
  "run_total_baseline": "{run_total_baseline}",
  "win_loss": "HOME|AWAY|-",
  "run_total": "UPPER|LOWER|-",
  "win_loss_reason": "one concise line with required contract tokens when needed",
  "run_total_reason": "one concise line with line-band / route comparison when needed",
  "judgment_independent": {{"win_loss": "HOME|AWAY|-", "run_total": "UPPER|LOWER|-"}},
  "judgment_applicable_ids": ["candidate_id"],
  "judgment_blind_review_sha256": "{bundle.get('components', {}).get('judgment_blind_review', {}).get('sha256', '')}",
  "model": {{"win_loss_side": "{model_win_loss}", "run_total_side": "{model_run_total}", "prediction_team": "{_clean(row.get('prediction_team'))}"}},
  "judgment_diverged_from_model": false,
  "final_diverged_from_model": false,
  "evidence_refs": ["pregame-only references from the notepad"],
  "axis_unresolved": {{"win_loss": false, "run_total": false}},
  "contract_tokens_required": {{"win_loss": [], "run_total": []}},
  "contract_tokens_satisfied": {{"win_loss": false, "run_total": false}},
  "context_bundle_sha256": "{bundle.get('context_bundle_sha256')}",
  "context_bundle_manifest": {{"v3_row_sha256": "{bundle.get('v3_row_sha256')}", "notepad_sha256": "{bundle.get('notepad_sha256')}", "decision_card_sha256": "{bundle.get('decision_card_sha256')}", "input_snapshot_sha256": "{bundle.get('input_snapshot_sha256', '')}"}},
  "v3_row_sha256": "{bundle.get('v3_row_sha256')}",
  "notepad_sha256": "{bundle.get('notepad_sha256')}",
  "input_snapshot_sha256": "{bundle.get('input_snapshot_sha256', '')}",
  "pre_knowable_only": true,
  "postgame_leak_check": "passed"{knowable_schema_fields}{contrast_schema_fields}
}}

Outcome-blind judgment review (immutable for this final stage):
{json.dumps(_final_visible_blind_review(blind_review), ensure_ascii=False, indent=2)}

Historical WIN_LOSS standards revealed only after blind applicability selection:
{selected_standards}

Model snapshot:
- WIN_LOSS model side: {model_win_loss}, model team: {_clean(row.get('prediction_team'))}
- RUN_TOTAL model side: {model_run_total}, run total baseline: {run_total_baseline}
- Edge/probability fields: prediction_p={_clean(row.get('prediction_p'))}, prediction_edge={_clean(row.get('prediction_edge'))}, run_total_signal={_clean(row.get('run_total_signal'))}, run_total_pred_total={_clean(row.get('run_total_pred_total'))}

Decision card:
{json.dumps(card or {}, ensure_ascii=False, indent=2)}

Context bundle manifest:
{json.dumps(bundle, ensure_ascii=False, indent=2)}

Context bundle contents:
[model_defense]
{snippets.get("model_defense") or "(missing)"}

[run_total_judgment_standards]
{snippets.get("judgment_standards") or "(missing)"}

[t038_prompt_view]
{snippets.get("t038_prompt_view") or "(missing)"}

[historical_judgment_policy_calibration]
{snippets.get("policy_family_advisory") or "(missing; historical standards still have no standalone directional authority)"}

Per-game notepad:
{notepad_text}
{contrast_section}
"""


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, flags=re.S)
        if not m:
            raise
        return json.loads(m.group(0))


def _validate_c3_payload(payload: dict[str, Any], row: dict[str, str], bundle: dict[str, Any]) -> dict[str, Any]:
    gp = _clean(row.get("gamePk"))
    payload.setdefault("schema", "mlb_narr_c3_prediction.v1")
    payload.setdefault("date", _clean(row.get("date")))
    payload.setdefault("scope", bundle.get("scope"))
    payload.setdefault("gamePk", gp)
    payload.setdefault("matchup", bundle.get("matchup"))
    payload.setdefault("run_total_baseline", _clean(row.get("run_total_baseline")))
    payload.setdefault("model", {"win_loss_side": _model_side(row), "run_total_side": _run_total_model_side(row), "prediction_team": _clean(row.get("prediction_team"))})
    payload.setdefault("context_bundle_sha256", bundle.get("context_bundle_sha256"))
    payload.setdefault("context_bundle_manifest", {
        "v3_row_sha256": bundle.get("v3_row_sha256"),
        "notepad_sha256": bundle.get("notepad_sha256"),
        "decision_card_sha256": bundle.get("decision_card_sha256"),
        "input_snapshot_sha256": bundle.get("input_snapshot_sha256", ""),
        "components": bundle.get("components", {}),
    })
    payload.setdefault("v3_row_sha256", bundle.get("v3_row_sha256"))
    payload.setdefault("notepad_sha256", bundle.get("notepad_sha256"))
    payload.setdefault("input_snapshot_sha256", bundle.get("input_snapshot_sha256", ""))
    payload.setdefault("pre_knowable_only", True)
    payload.setdefault("postgame_leak_check", "passed")
    for key, allowed in {"win_loss": {"HOME", "AWAY", "-"}, "run_total": {"UPPER", "LOWER", "-"}}.items():
        value = _clean(payload.get(key)).upper() or "-"
        if value not in allowed:
            raise ValueError(f"{gp} invalid {key}: {value}")
        payload[key] = value
    ji = payload.get("judgment_independent")
    if not isinstance(ji, dict):
        ji = {"win_loss": "-", "run_total": "-"}
    ji_win_loss = _clean(ji.get("win_loss")).upper() or "-"
    ji_run_total = _clean(ji.get("run_total")).upper() or "-"
    if ji_win_loss not in {"HOME", "AWAY", "-"}:
        ji_win_loss = "-"
    if ji_run_total not in {"UPPER", "LOWER", "-"}:
        ji_run_total = "-"
    payload["judgment_independent"] = {"win_loss": ji_win_loss, "run_total": ji_run_total}
    payload["judgment_diverged_from_model"] = bool(
        ji_win_loss != _model_side(row)
        or ji_run_total != _run_total_model_side(row)
    )
    payload["final_diverged_from_model"] = bool(
        payload.get("win_loss") != _model_side(row)
        or payload.get("run_total") != _run_total_model_side(row)
    )
    payload.pop("diverged_from_model", None)
    payload.setdefault("llm_provider", "codex")
    payload.setdefault("created_at_kst", _kst_now())
    return payload


def _load_direction_contrasts(
    directory: str | Path,
    game_pk: str,
) -> dict[str, dict[str, Any]]:
    root = Path(directory)
    if not root.is_absolute():
        root = ROOT / root
    rows: dict[str, dict[str, Any]] = {}
    for axis in ("WIN_LOSS", "RUN_TOTAL"):
        path = root / f"{game_pk}_{axis}.json"
        if not path.is_file():
            raise FileNotFoundError(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            payload.get("schema")
            == DCC.DIRECTION_CONTRAST_DETERMINISTIC_SCHEMA
        ):
            reader_payload = dict(payload)
            canonical_fields = {
                key: reader_payload.pop(key, None)
                for key in (
                    "release_status",
                    "conflict",
                    "conflict_derivation",
                )
            }
            if any(value is None for value in canonical_fields.values()):
                raise ValueError(f"{path}:v2_canonical_fields_missing")
            validated = DCC.validate_direction_contrast(reader_payload)
            for key, expected in canonical_fields.items():
                if validated.get(key) != expected:
                    raise ValueError(
                        f"{path}:v2_canonical_{key}_mismatch"
                    )
        else:
            validated = DCC.validate_direction_contrast(payload)
        if validated["target_gamePk"] != game_pk:
            raise ValueError(f"{path}:gamePk_mismatch")
        if validated["axis"] != axis:
            raise ValueError(f"{path}:axis_mismatch")
        rows[axis] = validated
    return rows


def _validate_augmented_c3_payload(
    payload: dict[str, Any],
    direction_contrasts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    lean = payload.get("lean_direction")
    release = payload.get("release_status")
    if not isinstance(lean, dict) or not isinstance(release, dict):
        raise ValueError("augmented C3 missing lean_direction/release_status")
    normalized_lean: dict[str, str] = {}
    normalized_release: dict[str, str] = {}
    for axis, output_key, allowed in (
        ("win_loss", "win_loss", {"HOME", "AWAY"}),
        ("run_total", "run_total", {"UPPER", "LOWER"}),
    ):
        direction = _clean(lean.get(axis)).upper()
        status = _clean(release.get(axis)).upper()
        if direction not in allowed:
            raise ValueError(f"augmented C3 invalid {axis} lean: {direction}")
        if status not in DCC.RELEASE_STATUSES:
            raise ValueError(f"augmented C3 invalid {axis} release: {status}")
        actual = _clean(payload.get(output_key)).upper()
        expected = direction if status == "RELEASE" else "-"
        if actual != expected:
            raise ValueError(
                f"augmented C3 {axis} output/release mismatch: "
                f"actual={actual} expected={expected}"
            )
        normalized_lean[axis] = direction
        normalized_release[axis] = status
    expected_sha = DCC.sha256_json(
        [direction_contrasts[axis] for axis in ("WIN_LOSS", "RUN_TOTAL")]
    )
    actual_sha = _clean(payload.get("direction_contrast_bundle_sha256")).lower()
    if actual_sha != expected_sha:
        raise ValueError(
            "augmented C3 direction contrast hash mismatch: "
            f"expected={expected_sha} actual={actual_sha}"
        )
    payload["lean_direction"] = normalized_lean
    payload["release_status"] = normalized_release
    payload["direction_contrast_bundle_sha256"] = expected_sha
    payload["direction_contrast_response_sha256"] = {
        axis.lower(): direction_contrasts[axis]["response_sha256"]
        for axis in ("WIN_LOSS", "RUN_TOTAL")
    }
    return payload


def _run_bridge_request(
    prompt_path: Path,
    output_path: Path,
    slot: str,
    args: argparse.Namespace,
    *,
    game_pk: str,
    purpose: str,
) -> tuple[int, str]:
    ledger_value = _clean(getattr(args, "call_ledger", ""))
    ledger_path = _manifest_path(ledger_value).resolve() if ledger_value else None
    request_sha = _sha256_file(prompt_path)

    retry_existing_dispatch_id = ""
    if ledger_path is not None and bool(
        getattr(args, "transport_retry_existing", False)
    ):
        ledger_payload = CALL_LEDGER.load(ledger_path)
        retried_ids = {
            _clean(row.get("retry_of_dispatch_id"))
            for row in ledger_payload.get("dispatches") or []
            if _clean(row.get("retry_of_dispatch_id"))
        }
        eligible = [
            row
            for row in ledger_payload.get("dispatches") or []
            if row.get("channel") == "codex_bridge"
            and _clean(row.get("gamePk")) == game_pk
            and _clean(row.get("purpose")) == purpose
            and row.get("attempt_kind") == "normal"
            and row.get("outcome") == "NO_VALID_RESPONSE"
            and _clean(row.get("request_sha256")) == request_sha
            and _clean(row.get("dispatch_id")) not in retried_ids
        ]
        if len(eligible) > 1:
            raise ValueError(
                f"{game_pk}:{purpose}:multiple eligible transport retries"
            )
        if eligible:
            retry_existing_dispatch_id = _clean(
                eligible[0].get("dispatch_id")
            )

    def invoke(
        *,
        attempt_kind: str,
        retry_of_dispatch_id: str = "",
    ) -> tuple[int, str]:
        dispatch_id = ""
        if ledger_path is not None:
            dispatch = CALL_LEDGER.reserve_dispatch(
                path=ledger_path,
                channel="codex_bridge",
                game_pk=game_pk,
                purpose=purpose,
                request_sha256=request_sha,
                attempt_kind=attempt_kind,
                retry_of_dispatch_id=retry_of_dispatch_id,
            )
            dispatch_id = _clean(dispatch.get("dispatch_id"))
        try:
            proc = _run([
                "_codex_exec_bridge.py",
                "--req", str(prompt_path),
                "--out", str(output_path),
                "--effort", args.effort,
                "--timeout", str(args.timeout),
                # A ledgered invocation must expose every actual attempt.
                "--retries", "0" if ledger_path is not None else str(args.retries),
                "--slot", slot,
                *([] if not args.model else ["--model", args.model]),
                "--direct-pipe",
                "--ignore-user-config",
            ], check=False)
        except BaseException:
            if ledger_path is not None and dispatch_id:
                CALL_LEDGER.record_outcome(
                    path=ledger_path,
                    dispatch_id=dispatch_id,
                    outcome="NO_VALID_RESPONSE",
                )
            raise
        if ledger_path is not None and dispatch_id and proc.returncode != 0:
            CALL_LEDGER.record_outcome(
                path=ledger_path,
                dispatch_id=dispatch_id,
                outcome="NO_VALID_RESPONSE",
            )
        return proc.returncode, dispatch_id

    if retry_existing_dispatch_id:
        return invoke(
            attempt_kind="transport_retry",
            retry_of_dispatch_id=retry_existing_dispatch_id,
        )

    rc, dispatch_id = invoke(attempt_kind="normal")
    if ledger_path is None or rc == 0 or int(args.retries or 0) <= 0:
        return rc, dispatch_id
    return invoke(
        attempt_kind="transport_retry",
        retry_of_dispatch_id=dispatch_id,
    )


def _record_bridge_validation(
    args: argparse.Namespace,
    dispatch_id: str,
    output_path: Path,
    *,
    valid: bool,
) -> None:
    ledger_value = _clean(getattr(args, "call_ledger", ""))
    if not ledger_value or not dispatch_id:
        return
    has_response = output_path.is_file() and output_path.stat().st_size > 0
    outcome = (
        "VALID_RESPONSE"
        if valid
        else ("VALIDATOR_FAILED" if has_response else "NO_VALID_RESPONSE")
    )
    CALL_LEDGER.record_outcome(
        path=_manifest_path(ledger_value).resolve(),
        dispatch_id=dispatch_id,
        outcome=outcome,
        response_sha256=_sha256_file(output_path) if has_response else "",
    )


def _reuse_blind_review(
    date: str,
    out_dir: Path,
    reuse_scope: str,
    game_pk: str,
    blind_prompt_path: Path,
    row: dict[str, str],
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    prompt_sha = _sha256_file(blind_prompt_path)
    source_dirs = [out_dir]
    if reuse_scope:
        source_dirs.append(OUT_ROOT / date / reuse_scope)
    for source_dir in source_dirs:
        source_prompt = source_dir / "blind_prompts" / f"{game_pk}.md"
        source_review = source_dir / "blind_reviews" / f"{game_pk}.json"
        if not source_prompt.exists() or not source_review.exists():
            continue
        if _sha256_file(source_prompt) != prompt_sha:
            continue
        try:
            review = _validate_blind_review(
                json.loads(source_review.read_text(encoding="utf-8")), row, candidates
            )
        except Exception:
            continue
        review["blind_prompt_sha256"] = prompt_sha
        return review, str(source_review.relative_to(ROOT))
    return None, ""


def _build_one(date: str, row: dict[str, str], notepads: dict[str, dict[str, Any]], cards: dict[str, dict[str, Any]], context_paths: dict[str, Path], contract_data: dict[str, Any], out_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    gp = _clean(row.get("gamePk"))
    notepad_meta = notepads.get(gp) or {}
    notepad_path = Path(notepad_meta.get("notepad_file") or ROOT / "_notepads" / date / f"{gp}.md")
    if not notepad_path.is_absolute():
        notepad_path = ROOT / notepad_path
    if not notepad_path.exists():
        raise FileNotFoundError(notepad_path)
    notepad_text = notepad_path.read_text(encoding="utf-8", errors="replace")
    card = cards.get(gp, {})
    bundle = _context_bundle(date, row, notepad_path, card, context_paths, out_dir)
    snippets = _component_snippets(context_paths, gp)
    contract_guidance = _contract_prompt_guidance(row, contract_data)
    knowable_inventory = None
    if bool(getattr(args, "knowable_compliance_shadow", False)):
        knowable_inventory = (contract_data.get("knowable_inventories") or {}).get(gp)
        if not knowable_inventory:
            raise ValueError(f"{gp} missing knowable compliance inventory")
        contract_guidance += "\n\n" + KC.prompt_contract(knowable_inventory)
    direction_contrasts = (
        _load_direction_contrasts(args.direction_contrast_dir, gp)
        if _clean(getattr(args, "direction_contrast_dir", ""))
        else None
    )
    candidates, revealed = _judgment_game_data(context_paths.get("judgment_standards_json"), gp)
    if not candidates:
        raise ValueError(f"{gp} no outcome-blind judgment candidates")

    blind_prompt = _blind_review_prompt(date, row, _blind_current_facts(notepad_text), candidates)
    blind_prompt_path = out_dir / "blind_prompts" / f"{gp}.md"
    blind_prompt_path.parent.mkdir(parents=True, exist_ok=True)
    blind_prompt_path.write_text(blind_prompt, encoding="utf-8")

    raw_path = out_dir / "raw" / f"{gp}.json"
    prompt_path = out_dir / "prompts" / f"{gp}.md"
    bridge_out = out_dir / "bridge_outputs" / f"{gp}.txt"
    if not args.run_bridge:
        return {
            "gamePk": gp,
            "matchup": bundle.get("matchup"),
            "status": "blind_prompt_built",
            "blind_prompt_path": str(blind_prompt_path.relative_to(ROOT)),
            "bundle_path": str((out_dir / "bundles" / f"{gp}.json").relative_to(ROOT)),
            "raw_path": str(raw_path.relative_to(ROOT)),
            "context_bundle_sha256": bundle.get("context_bundle_sha256"),
        }

    blind_bridge_out = out_dir / "blind_bridge_outputs" / f"{gp}.txt"
    blind_review, blind_review_reused_from = _reuse_blind_review(
        date,
        out_dir,
        _clean(getattr(args, "reuse_blind_scope", "")),
        gp,
        blind_prompt_path,
        row,
        candidates,
    )
    if blind_review is None:
        if _clean(getattr(args, "call_ledger_arm", "")) == "augmented":
            raise ValueError(
                f"{gp} augmented arm requires exact-SHA blind-review reuse"
            )
        rc, blind_dispatch_id = _run_bridge_request(
            blind_prompt_path,
            blind_bridge_out,
            f"c3-blind:{date}:{gp}",
            args,
            game_pk=gp,
            purpose="baseline_blind_review",
        )
        if rc != 0:
            return {
                "gamePk": gp,
                "matchup": bundle.get("matchup"),
                "status": "blind_bridge_failed",
                "bridge_rc": rc,
                "blind_prompt_path": str(blind_prompt_path.relative_to(ROOT)),
                "blind_bridge_out": str(blind_bridge_out.relative_to(ROOT)),
            }
        try:
            blind_review = _extract_json(blind_bridge_out.read_text(encoding="utf-8", errors="replace"))
            blind_review = _validate_blind_review(blind_review, row, candidates)
        except BaseException:
            _record_bridge_validation(
                args,
                blind_dispatch_id,
                blind_bridge_out,
                valid=False,
            )
            raise
        _record_bridge_validation(
            args,
            blind_dispatch_id,
            blind_bridge_out,
            valid=True,
        )
        blind_review["blind_prompt_sha256"] = _sha256_file(blind_prompt_path)
    blind_review_path = out_dir / "blind_reviews" / f"{gp}.json"
    _write_json(blind_review_path, blind_review)
    bundle = _attach_blind_review(bundle, out_dir, gp, blind_review_path, blind_review)
    selected_standards = _render_selected_standards(blind_review, revealed)
    prompt = _prompt_text(
        date,
        row,
        notepad_text,
        card,
        bundle,
        snippets,
        contract_guidance,
        blind_review,
        selected_standards,
        knowable_inventory,
        direction_contrasts,
    )
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt, encoding="utf-8")

    if bool(getattr(args, "prepare_final_only", False)):
        return {
            "gamePk": gp,
            "matchup": bundle.get("matchup"),
            "status": "final_prompt_built",
            "prompt_path": str(prompt_path.relative_to(ROOT)),
            "blind_prompt_path": str(blind_prompt_path.relative_to(ROOT)),
            "blind_review_path": str(blind_review_path.relative_to(ROOT)),
            "blind_review_reused_from": blind_review_reused_from,
            "context_bundle_sha256": bundle.get("context_bundle_sha256"),
        }

    final_purpose = (
        "augmented_final_c3"
        if _clean(getattr(args, "call_ledger_arm", "")) == "augmented"
        else "baseline_final_c3"
    )
    final_dispatch_id = ""
    if bool(getattr(args, "use_existing_final_output", False)):
        if not bridge_out.is_file() or bridge_out.stat().st_size == 0:
            raise FileNotFoundError(bridge_out)
    else:
        rc, final_dispatch_id = _run_bridge_request(
            prompt_path,
            bridge_out,
            f"c3:{date}:{gp}",
            args,
            game_pk=gp,
            purpose=final_purpose,
        )
        if rc != 0:
            return {
                "gamePk": gp,
                "matchup": bundle.get("matchup"),
                "status": "bridge_failed",
                "bridge_rc": rc,
                "prompt_path": str(prompt_path.relative_to(ROOT)),
                "bridge_out": str(bridge_out.relative_to(ROOT)),
                "blind_review_path": str(blind_review_path.relative_to(ROOT)),
            }
    try:
        payload = _extract_json(bridge_out.read_text(encoding="utf-8", errors="replace"))
        payload = _validate_c3_payload(payload, row, bundle)
        payload["judgment_independent"]["win_loss"] = blind_review["independent_win_loss_route"]
        payload["judgment_blind_run_total_route"] = blind_review["independent_run_total_route"]
        payload["judgment_blind_run_total_route_present"] = bool(
            blind_review.get("independent_run_total_route_present")
        )
        payload["judgment_applicable_ids"] = list(blind_review.get("applicable_ids") or [])
        payload["judgment_blind_review_sha256"] = _sha256_file(blind_review_path)
        payload["judgment_diverged_from_model"] = bool(
            payload["judgment_independent"]["win_loss"] != _model_side(row)
            or payload["judgment_independent"]["run_total"] != _run_total_model_side(row)
        )
        payload = _apply_contract_requirements(date, row, payload, contract_data)
        compliance_validation = None
        compliance_path = None
        if knowable_inventory:
            compliance_receipt, compliance_validation, compliance_enforcement = KC.enforce_required_no_selection(
                knowable_inventory,
                payload.get("knowable_compliance"),
            )
            payload["knowable_compliance"] = compliance_receipt
            payload["knowable_compliance_validation"] = compliance_validation
            payload["knowable_compliance_no_selection_enforcement"] = compliance_enforcement
            compliance_path = out_dir / "knowable_compliance" / f"{gp}.json"
            KC.atomic_write_json(
                compliance_path,
                {
                    "schema": "mlb.knowable_compliance.shadow_sidecar.v1",
                    "date": date,
                    "gamePk": gp,
                    "research_only": True,
                    "production_prediction_mutated": False,
                    "narrator_prediction_mutated": False,
                    "notification_mutated": False,
                    "baseline_c3_output": {"win_loss": payload.get("win_loss"), "run_total": payload.get("run_total")},
                    "inventory": knowable_inventory,
                    "receipt": compliance_receipt,
                    "validation": compliance_validation,
                    "no_selection_enforcement": compliance_enforcement,
                },
            )
        if direction_contrasts:
            for axis in ("win_loss", "run_total"):
                if payload[axis] == "-":
                    payload["release_status"][axis] = "PASS"
            payload = _validate_augmented_c3_payload(payload, direction_contrasts)
    except BaseException:
        _record_bridge_validation(
            args,
            final_dispatch_id,
            bridge_out,
            valid=False,
        )
        raise
    _record_bridge_validation(
        args,
        final_dispatch_id,
        bridge_out,
        valid=True,
    )
    _write_json(raw_path, payload)
    contract_ok = all(bool(v) for v in (payload.get("contract_tokens_satisfied") or {}).values())
    return {
        "gamePk": gp,
        "matchup": bundle.get("matchup"),
        "status": "raw_written" if contract_ok else "raw_written_contract_incomplete",
        "raw_path": str(raw_path.relative_to(ROOT)),
        "prompt_path": str(prompt_path.relative_to(ROOT)),
        "blind_prompt_path": str(blind_prompt_path.relative_to(ROOT)),
        "blind_review_path": str(blind_review_path.relative_to(ROOT)),
        "judgment_applicable_ids": payload.get("judgment_applicable_ids"),
        "judgment_blind_review_sha256": payload.get("judgment_blind_review_sha256"),
        "blind_review_reused_from": blind_review_reused_from,
        "context_bundle_sha256": payload.get("context_bundle_sha256"),
        "contract_tokens_satisfied": payload.get("contract_tokens_satisfied"),
        "knowable_compliance_ok": (
            compliance_validation.get("ok") if compliance_validation is not None else None
        ),
        "knowable_compliance_path": (
            str(compliance_path.relative_to(ROOT)) if compliance_path is not None else ""
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Build/run research-only MLB C3 per-game narrator prompts.")
    ap.add_argument("--date", required=True)
    ap.add_argument("--gamepks", default="")
    ap.add_argument("--scope", default="", help="Reuse an existing shadow scope (repair runs only).")
    ap.add_argument(
        "--merge-existing-manifest",
        action="store_true",
        help="Replace selected gamePk results in an existing scope manifest instead of overwriting it.",
    )
    ap.add_argument("--run-bridge", action="store_true", help="Actually call Codex bridge per game. Default only builds prompts/bundles.")
    ap.add_argument(
        "--knowable-compliance-shadow",
        action="store_true",
        help="Add the historical-note result-blind compliance receipt to this research C3 scope only.",
    )
    ap.add_argument(
        "--require-knowable-compliance-complete",
        action="store_true",
        help="Exit nonzero when a selected shadow receipt fails historical-note validation.",
    )
    ap.add_argument(
        "--prepare-final-only",
        action="store_true",
        help=(
            "Validate/reuse the blind review and build the final C3 prompt, "
            "but do not dispatch the final bridge call."
        ),
    )
    ap.add_argument(
        "--use-existing-final-output",
        action="store_true",
        help=(
            "Validate an already frozen per-game final response in the scope's "
            "bridge_outputs directory without invoking the Codex bridge."
        ),
    )
    ap.add_argument(
        "--require-contract-complete",
        action="store_true",
        help="Exit nonzero when any selected result has an incomplete contract.",
    )
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--effort", default="high")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--retries", type=int, default=1)
    ap.add_argument(
        "--transport-retry-existing",
        action="store_true",
        help=(
            "Resume one prior NO_VALID_RESPONSE only when game, purpose, and "
            "request SHA match exactly; the retry remains separately ledgered."
        ),
    )
    ap.add_argument("--model", default="")
    ap.add_argument(
        "--contract-data-override",
        default="",
        help=(
            "Research-only exact contract-data file. Used for immutable replay "
            "or recall preparation without mutating the frozen input manifest."
        ),
    )
    ap.add_argument("--reuse-blind-scope", default="", help="Reuse a prior blind review only when its blind-prompt SHA256 matches exactly.")
    ap.add_argument("--build-contexts", action="store_true", help="Refresh model-defense/judgment/historical-note context artifacts before prompt build.")
    ap.add_argument("--refresh-c1-c2", action="store_true", help="Rebuild decision cards and per-game notepads from the current prepared v3/context.")
    ap.add_argument("--require-input-snapshot", action="store_true", help="Fail unless the live input snapshot path/hash exists and matches exactly.")
    ap.add_argument("--frozen-input-manifest", default="", help="Hash-verified isolated replay inputs (research/shadow only).")
    ap.add_argument(
        "--direction-contrast-dir",
        default="",
        help=(
            "Research-only directory containing validated GAMEPK_WIN_LOSS.json and "
            "GAMEPK_RUN_TOTAL.json contrasts. Normal C3 behavior is unchanged when omitted."
        ),
    )
    ap.add_argument(
        "--call-ledger",
        default="",
        help="historical-note prospective dispatch ledger. Every bridge attempt is reserved before invocation.",
    )
    ap.add_argument(
        "--call-ledger-arm",
        choices=("baseline", "augmented"),
        default="baseline",
        help="Select the historical-note C3 purpose counters used by --call-ledger.",
    )
    args = ap.parse_args()

    date = args.date
    gamepks = {x.strip() for x in str(args.gamepks or "").replace(";", ",").split(",") if x.strip()} or None
    scope = _clean(args.scope)
    if scope and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", scope):
        raise SystemExit("invalid --scope")
    args.reuse_blind_scope = _clean(args.reuse_blind_scope)
    args.direction_contrast_dir = _clean(args.direction_contrast_dir)
    args.call_ledger = _clean(args.call_ledger)
    args.contract_data_override = _clean(args.contract_data_override)
    if args.reuse_blind_scope and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", args.reuse_blind_scope):
        raise SystemExit("invalid --reuse-blind-scope")
    if args.merge_existing_manifest and (not scope or not gamepks):
        raise SystemExit("--merge-existing-manifest requires --scope and --gamepks")
    if args.call_ledger and not args.run_bridge:
        raise SystemExit("--call-ledger requires --run-bridge")
    if args.require_knowable_compliance_complete and not args.knowable_compliance_shadow:
        raise SystemExit("--require-knowable-compliance-complete requires --knowable-compliance-shadow")
    if args.prepare_final_only and not args.run_bridge:
        raise SystemExit("--prepare-final-only requires --run-bridge")
    if args.use_existing_final_output and not args.run_bridge:
        raise SystemExit("--use-existing-final-output requires --run-bridge")
    if args.prepare_final_only and args.use_existing_final_output:
        raise SystemExit(
            "--prepare-final-only and --use-existing-final-output are exclusive"
        )
    if args.call_ledger and not _manifest_path(args.call_ledger).is_file():
        raise SystemExit(f"call ledger not found: {_manifest_path(args.call_ledger)}")
    if args.call_ledger_arm == "augmented" and not args.direction_contrast_dir:
        raise SystemExit("augmented call-ledger arm requires --direction-contrast-dir")
    frozen_payload: dict[str, Any] = {}
    frozen_files: dict[str, Path] = {}
    if args.frozen_input_manifest:
        if args.build_contexts:
            raise SystemExit("--build-contexts cannot be combined with --frozen-input-manifest")
        frozen_manifest_path = _manifest_path(args.frozen_input_manifest).resolve()
        frozen_payload, frozen_files = _load_frozen_manifest(frozen_manifest_path, date)
        allowed_gamepks = {_clean(x) for x in frozen_payload.get("gamepks") or []}
        if gamepks and not gamepks.issubset(allowed_gamepks):
            raise SystemExit("requested gamePk is outside frozen manifest scope")
        os.environ["MLB_C3_FROZEN_REPLAY"] = "1"
        os.environ["MLB_C3_FROZEN_MANIFEST"] = str(frozen_manifest_path)
        os.environ["MLB_C3_FROZEN_MANIFEST_SHA256"] = _sha256_file(frozen_manifest_path)
        os.environ["MLB_C3_V3_PATH"] = str(frozen_files["v3"])
        os.environ["MLB_C3_NARR_CONTEXT_PATH"] = str(frozen_files["narr_context"])
        os.environ["MLB_PREGAME_INPUT_SNAPSHOT"] = str(frozen_files["input_snapshot"])
        os.environ["MLB_PREGAME_INPUT_SNAPSHOT_SHA256"] = _clean(frozen_payload.get("input_snapshot_sha256"))
        rows = _load_v3_rows(date, gamepks=gamepks, path=frozen_files["v3"])
    else:
        if args.require_input_snapshot:
            try:
                _require_input_snapshot()
            except Exception as exc:
                raise SystemExit(f"C3 snapshot gate failed: {type(exc).__name__}: {exc}")
        _ensure_c1_c2(date, force=args.refresh_c1_c2)
        rows = _load_v3_rows(date, gamepks=gamepks)
    if not rows:
        raise SystemExit("no v3 rows selected")
    gp_csv = ",".join(_clean(r.get("gamePk")) for r in rows)
    context_paths = (
        {
            "model_defense": frozen_files["model_defense"],
            "judgment_standards": frozen_files["judgment_standards"],
            "judgment_standards_json": frozen_files["judgment_standards_json"],
            "t038_prompt_view": frozen_files["t038_prompt_view"],
            **(
                {"policy_family_advisory": frozen_files["policy_family_advisory"]}
                if "policy_family_advisory" in frozen_files
                else {}
            ),
        }
        if frozen_files
        else _maybe_build_contexts(date, gp_csv, args.build_contexts)
    )
    out_dir = OUT_ROOT / date / (scope or _scope_id([_clean(r.get("gamePk")) for r in rows]))
    out_dir.mkdir(parents=True, exist_ok=True)
    notepads = _notepad_index(date, frozen_files.get("notepad_index"))
    cards = _decision_cards(date, frozen_files.get("decision_cards"))
    if args.contract_data_override:
        contract_override_path = _manifest_path(
            args.contract_data_override
        ).resolve()
        if not contract_override_path.is_file():
            raise SystemExit(
                f"contract data override not found: {contract_override_path}"
            )
        contract_data = json.loads(
            contract_override_path.read_text(encoding="utf-8")
        )
    else:
        contract_data = (
            json.loads(frozen_files["contract_data"].read_text(encoding="utf-8"))
            if frozen_files
            else _contract_data(date, context_paths.get("t038_prompt_view"))
        )
    if args.knowable_compliance_shadow:
        t038_games = KC.read_t038_games(context_paths["t038_prompt_view"])
        inventories: dict[str, dict[str, Any]] = {}
        for row in rows:
            gp = MDV._norm_game_pk(row.get("gamePk"))
            if gp not in t038_games:
                raise SystemExit(f"historical-note missing historical-note prompt game: {gp}")
            guard_row = (contract_data.get("guard_rows") or {}).get(gp)
            inventories[gp] = KC.build_inventory(
                date=date,
                row=row,
                snapshot=(contract_data.get("model_snapshots") or {}).get(gp),
                t038_game=t038_games[gp],
                hard_guard_gates=MDV._hard_guard_review_gates(guard_row),
                must_review_items=(contract_data.get("must_review_rows") or {}).get(gp, []),
                guard_row=guard_row,
            )
        contract_data["knowable_inventories"] = inventories
    contract_path = out_dir / "contract_data.json"
    if args.merge_existing_manifest and contract_path.exists():
        existing_contract = json.loads(contract_path.read_text(encoding="utf-8"))
        for key in ("guard_rows", "must_review_rows", "run_total_retro_ack_rows", "model_snapshots", "t038_win_loss_conflicts", "knowable_inventories"):
            merged = dict(existing_contract.get(key) or {})
            merged.update(contract_data.get(key) or {})
            contract_data[key] = merged
    _write_json(contract_path, contract_data)

    results: list[dict[str, Any]] = []
    workers = max(1, int(args.workers or 1))
    if workers == 1:
        for row in rows:
            results.append(_build_one(date, row, notepads, cards, context_paths, contract_data, out_dir, args))
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_build_one, date, row, notepads, cards, context_paths, contract_data, out_dir, args) for row in rows]
            for fut in as_completed(futures):
                results.append(fut.result())
        order = {_clean(r.get("gamePk")): i for i, r in enumerate(rows)}
        results.sort(key=lambda x: order.get(str(x.get("gamePk")), 10**6))

    manifest_path = out_dir / "manifest.json"
    existing_manifest: dict[str, Any] = {}
    if args.merge_existing_manifest:
        if not manifest_path.exists():
            raise SystemExit(f"existing manifest not found: {manifest_path}")
        existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        old_results = list(existing_manifest.get("results") or [])
        replacements = {str(item.get("gamePk")): item for item in results}
        merged_results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in old_results:
            gp = str(item.get("gamePk"))
            merged_results.append(replacements.get(gp, item))
            seen.add(gp)
        merged_results.extend(item for item in results if str(item.get("gamePk")) not in seen)
        results = merged_results

    manifest = {
        "schema": "mlb_narr_c3_per_game_run_manifest.v1",
        "date": date,
        "scope": out_dir.name,
        "run_bridge": bool(args.run_bridge or existing_manifest.get("run_bridge")),
        "workers": workers,
        "created_at_kst": existing_manifest.get("created_at_kst") or _kst_now(),
        "updated_at_kst": _kst_now(),
        "game_count": len(rows),
        "output_dir": str(out_dir.relative_to(ROOT)),
        "results": results,
        "guardrail": "research/shadow only; no Notification; no production narrator mutation",
        "contract_data_path": str(contract_path.relative_to(ROOT)),
        "contract_data_sha256": _sha256_file(contract_path),
        "direction_contrast_enabled": bool(args.direction_contrast_dir),
        "direction_contrast_dir": args.direction_contrast_dir,
        "knowable_compliance_shadow": bool(args.knowable_compliance_shadow),
    }
    if args.contract_data_override:
        manifest["contract_data_override"] = args.contract_data_override
        manifest["contract_data_override_sha256"] = _sha256_file(
            _manifest_path(args.contract_data_override).resolve()
        )
    if frozen_payload:
        manifest["frozen_input_manifest"] = _clean(args.frozen_input_manifest)
        manifest["frozen_input_manifest_sha256"] = _clean(os.environ.get("MLB_C3_FROZEN_MANIFEST_SHA256"))
        manifest["input_snapshot_sha256"] = _clean(frozen_payload.get("input_snapshot_sha256"))
    if args.merge_existing_manifest:
        manifest["game_count"] = len(results)
        manifest["last_repair_gamepks"] = sorted(gamepks or set())
        manifest["last_repair_workers"] = workers
    _write_json(manifest_path, manifest)
    print(json.dumps({
        "date": date,
        "scope": out_dir.name,
        "run_bridge": bool(args.run_bridge),
        "game_count": manifest["game_count"],
        "selected_count": len(rows),
        "output_dir": str(out_dir.relative_to(ROOT)),
        "statuses": {s: sum(1 for r in results if r.get("status") == s) for s in sorted({r.get("status") for r in results})},
    }, ensure_ascii=False, indent=2))
    if args.require_contract_complete and any(
        row.get("status") == "raw_written_contract_incomplete"
        for row in results
    ):
        return 6
    if args.require_knowable_compliance_complete and any(
        row.get("knowable_compliance_ok") is not True
        for row in results
    ):
        return 7
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
