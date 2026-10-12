# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""C3 shadow raw → assembler → validator → helper metadata mismatch 독립 대조 (historical-note).

Phase 1 replay 검증 계측기. 주어진 (date, scope)에 대해:
 1. assembler로 c3_shadow raw → assembled reasoning (shadow only)
 2. model-defense validator를 assembled 텍스트에 실행
 3. 경기별: helper `required_tokens`(단일 원천) 결과 vs validator 실제 판정 대조
    + raw의 raw_claimed_satisfied vs 실제 present 대조 (raw 자기주장 불신, 기존 검토)
 4. mismatch 리포트 → c3_shadow/{date}/{scope}/crosscheck__{scope}.json + stdout

live/notification/root reasoning 미접촉. validator/helper는 read-only 사용.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path

import pandas as pd

import _narr_model_defense_validate as V
import _mlb_narr_contract_requirements as REQ
import _mlb_narr_c3_assemble as ASM

ROOT = Path(__file__).resolve().parent


def _helper_required_for_game(date: str, row) -> dict:
    """_v3_games 행 + 컨텍스트로 helper 필요토큰 산출 (validator와 동일 입력)."""
    gpk = V._norm_game_pk(row.get("gamePk"))
    snaps = _CTX["snaps"]; guard = _CTX["guard"]; must = _CTX["must"]; t038 = _CTX["t038"]; retro = _CTX["retro"]
    snap = snaps.get(gpk)
    model_win_loss = V._model_win_loss_side(row); model_run_total = V._model_run_total_side(row)
    return {
        "model_win_loss": model_win_loss, "model_run_total": model_run_total,
        "concrete": V._model_win_loss_is_concrete(row, snap, model_win_loss),
        "t038": len(t038.get(gpk, [])),
        "gates": V._hard_guard_review_gates(guard.get(gpk)),
        "must": [{"leg": V._clean(i.get("leg")), "ack_id": V._clean(i.get("ack_id") or i.get("gate"))} for i in must.get(gpk, [])],
        "retro_id": V._clean((retro.get(gpk) or {}).get("ack_id")),
    }


_CTX: dict = {}


def _read_snapshot_override(path: Path) -> dict[str, dict]:
    snapshots: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        game_pk = V._norm_game_pk(row.get("gamePk"))
        if game_pk:
            snapshots[game_pk] = row
    return snapshots


def _blind_integrity_violations(base: Path, game_pk: str) -> list[str]:
    raw_path = base / "raw" / f"{game_pk}.json"
    review_path = base / "blind_reviews" / f"{game_pk}.json"
    bundle_path = base / "bundles" / f"{game_pk}.json"
    missing = [p.name for p in (raw_path, review_path, bundle_path) if not p.exists()]
    if missing:
        return [f"blind-integrity missing files: {','.join(missing)}"]
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    review = json.loads(review_path.read_text(encoding="utf-8"))
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    review_sha = hashlib.sha256(review_path.read_bytes()).hexdigest()
    violations: list[str] = []
    if str(raw.get("judgment_blind_review_sha256") or "") != review_sha:
        violations.append("blind-integrity raw review hash mismatch")
    bundle_sha = str((((bundle.get("components") or {}).get("judgment_blind_review") or {}).get("sha256")) or "")
    if bundle_sha != review_sha:
        violations.append("blind-integrity bundle review hash mismatch")
    raw_route = str(((raw.get("judgment_independent") or {}).get("win_loss")) or "-").upper()
    review_route = str(review.get("independent_win_loss_route") or "-").upper()
    if raw_route != review_route:
        violations.append("blind-integrity independent WIN_LOSS route changed after reveal")
    raw_ids = list(raw.get("judgment_applicable_ids") or [])
    review_ids = list(review.get("applicable_ids") or [])
    if raw_ids != review_ids:
        violations.append("blind-integrity applicable candidate IDs mismatch")
    if raw.get("context_bundle_sha256") != bundle.get("context_bundle_sha256"):
        violations.append("blind-integrity context bundle hash mismatch")
    return violations


def crosscheck(
    date: str,
    scope: str,
    gamepks: str = "",
    frozen_input_manifest: str = "",
    model_snapshot_jsonl: str = "",
) -> dict:
    global _CTX
    frozen_files = {}
    contract_override = False
    if frozen_input_manifest:
        import _mlb_narr_c3_per_game_run as C3

        manifest_path = C3._manifest_path(frozen_input_manifest).resolve()
        _, frozen_files = C3._load_frozen_manifest(manifest_path, date)
        contract = json.loads(frozen_files["contract_data"].read_text(encoding="utf-8"))
        os.environ["MLB_C3_V3_PATH"] = str(frozen_files["v3"])
        contract_override = True
        _CTX = {
            "snaps": contract.get("model_snapshots") or {},
            "guard": contract.get("guard_rows") or {},
            "must": contract.get("must_review_rows") or {},
            "t038": contract.get("t038_win_loss_conflicts") or {},
            "retro": contract.get("run_total_retro_ack_rows") or {},
        }
        if model_snapshot_jsonl:
            _CTX["snaps"] = _read_snapshot_override(
                Path(model_snapshot_jsonl).resolve()
            )
    elif (ASM.SHADOW / date / scope / "contract_data.json").exists():
        contract = json.loads((ASM.SHADOW / date / scope / "contract_data.json").read_text(encoding="utf-8"))
        contract_override = True
        _CTX = {
            "snaps": contract.get("model_snapshots") or {},
            "guard": contract.get("guard_rows") or {},
            "must": contract.get("must_review_rows") or {},
            "t038": contract.get("t038_win_loss_conflicts") or {},
            "retro": contract.get("run_total_retro_ack_rows") or {},
        }
    else:
        _CTX = {
            "snaps": V._read_model_snapshots(date),
            "guard": V._read_guard_rows(date),
            "must": V._must_review_by_game(date),
            "t038": V._read_t038_model_win_loss_conflicts(date),
            "retro": V._run_total_retro_ack_requirements(date),
        }
    # 1) assemble
    out, nblocks, warns = ASM.assemble(date, scope)
    text = out.read_text(encoding="utf-8")
    # 2) validator
    if contract_override:
        original_readers = (
            V._read_model_snapshots,
            V._read_guard_rows,
            V._must_review_by_game,
            V._read_t038_model_win_loss_conflicts,
            V._run_total_retro_ack_requirements,
        )
        V._read_model_snapshots = lambda _date: _CTX["snaps"]
        V._read_guard_rows = lambda _date: _CTX["guard"]
        V._must_review_by_game = lambda _date: _CTX["must"]
        V._read_t038_model_win_loss_conflicts = lambda _date: _CTX["t038"]
        V._run_total_retro_ack_requirements = lambda _date: _CTX["retro"]
        try:
            report = V.validate_text(date, text, gamepks=gamepks or None)
        finally:
            (
                V._read_model_snapshots,
                V._read_guard_rows,
                V._must_review_by_game,
                V._read_t038_model_win_loss_conflicts,
                V._run_total_retro_ack_requirements,
            ) = original_readers
    else:
        report = V.validate_text(date, text, gamepks=gamepks or None)
    viol_by_gp: dict[str, list] = {}
    viol_by_mu: dict[str, list] = {}
    for v in report["violations"]:
        gp = V._norm_game_pk(v.get("gamePk"))
        if gp:
            viol_by_gp.setdefault(gp, []).append(v)
        viol_by_mu.setdefault(str(v.get("matchup")), []).append(v)
    block_records = V._reasoning_block_records(text)
    blocks_by_gp = {r["gamePk"]: r["block"] for r in block_records if r.get("gamePk")}
    blocks_by_mu: dict[str, list[str]] = {}
    for record in block_records:
        blocks_by_mu.setdefault(record["matchup"], []).append(record["block"])
    matchup_offsets: dict[str, int] = {}
    # sidecar (raw claimed)
    side_path = ASM.SHADOW / date / scope / "assembled" / f"report__{scope}.json"
    raw_claimed = {}
    if side_path.exists():
        for g in json.loads(side_path.read_text(encoding="utf-8")).get("games", []):
            raw_claimed[str(g.get("gamePk"))] = g.get("raw_claimed_satisfied")
    # 3) 경기별 helper vs validator
    df = pd.read_csv(frozen_files["v3"] if frozen_files else ROOT / f"_v3_games_{date}.csv")
    wanted = V._parse_gamepks(gamepks)
    if wanted and "gamePk" in df.columns:
        df = df[df["gamePk"].astype(str).str.replace(r"\.0$", "", regex=True).isin(wanted)].copy()
    rows = []
    mismatches = 0
    blind_integrity_count = 0
    scope_base = ASM.SHADOW / date / scope
    for _, r in df.iterrows():
        mu = V._matchup(r); gpk = V._norm_game_pk(r.get("gamePk"))
        blk = blocks_by_gp.get(gpk)
        if not blk:
            candidates = blocks_by_mu.get(mu, [])
            offset = matchup_offsets.get(mu, 0)
            blk = candidates[offset] if offset < len(candidates) else None
            if blk:
                matchup_offsets[mu] = offset + 1
        if not blk:
            continue  # scope 밖 경기
        prediction = V._parse_prediction(blk)
        ctx = _helper_required_for_game(date, r)
        req = REQ.required_tokens(
            model_win_loss=ctx["model_win_loss"], final_win_loss=prediction["win_loss"], model_run_total=ctx["model_run_total"], final_run_total=prediction["run_total"],
            concrete_model_win_loss=ctx["concrete"], t038_conflict_count=ctx["t038"],
            hard_guard_gates=ctx["gates"], must_review_items=ctx["must"], run_total_retro_ack_id=ctx["retro_id"],
        )
        win_loss_line = V._win_loss_line(blk).lower(); run_total_baseline = V._run_total_baseline(blk).lower()
        miss_win_loss = [t for t in req["win_loss"] if t.lower() not in win_loss_line]
        miss_run_total = [t for t in req["run_total"] if t.lower() not in run_total_baseline]
        game_viols = list(viol_by_gp.get(gpk, viol_by_mu.get(mu, [])))
        blind_viols = _blind_integrity_violations(scope_base, gpk)
        blind_integrity_count += len(blind_viols)
        game_viols.extend({"error": message, "gamePk": gpk, "matchup": mu, "axis": "WIN_LOSS"} for message in blind_viols)
        # mismatch = helper가 필요하다는데 기준점에 없음(=validator도 잡아야) OR validator가 잡았는데 helper는 안 잡음
        helper_flags = bool(miss_win_loss or miss_run_total)
        validator_flags = bool(game_viols)
        mm = helper_flags != validator_flags
        if mm:
            mismatches += 1
        rows.append({
            "gamePk": gpk, "matchup": mu, "win_loss": prediction["win_loss"], "run_total": prediction["run_total"],
            "helper_required_win_loss": req["win_loss"], "helper_required_run_total": req["run_total"],
            "helper_missing_win_loss": miss_win_loss, "helper_missing_run_total": miss_run_total,
            "validator_violations": [v.get("error") for v in game_viols],
            "raw_claimed_satisfied": raw_claimed.get(gpk),
            "mismatch": mm,
        })
    result = {
        "schema": "mlb_narr_c3_crosscheck.v1", "date": date, "scope": scope,
        "assembled_path": str(out), "blocks": nblocks, "assemble_warnings": warns,
        "validator_ok": report["ok"], "validator_checked": report["checked_games"],
        "validator_violations": len(report["violations"]),
        "blind_integrity_violations": blind_integrity_count,
        "helper_validator_mismatches": mismatches, "games": rows,
    }
    if frozen_input_manifest:
        result["frozen_input_manifest"] = frozen_input_manifest
    if model_snapshot_jsonl:
        snapshot_path = Path(model_snapshot_jsonl).resolve()
        result["model_snapshot_override"] = {
            "path": str(snapshot_path),
            "sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
            "rows": len(_CTX["snaps"]),
            "pregame_validator_context_only": True,
        }
    cc = ASM.SHADOW / date / scope / f"crosscheck__{scope}.json"
    cc.parent.mkdir(parents=True, exist_ok=True)
    cc.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="C3 shadow raw → assembler → validator → helper mismatch crosscheck (shadow only).")
    ap.add_argument("--date", required=True)
    ap.add_argument("--scope", required=True)
    ap.add_argument("--gamepks", default="")
    ap.add_argument("--frozen-input-manifest", default="")
    ap.add_argument("--model-snapshot-jsonl", default="")
    args = ap.parse_args()
    res = crosscheck(
        args.date,
        args.scope,
        args.gamepks,
        args.frozen_input_manifest,
        args.model_snapshot_jsonl,
    )
    print(f"[c3-crosscheck] {args.date}/{args.scope} blocks={res['blocks']} "
          f"validator ok={res['validator_ok']} checked={res['validator_checked']} viol={res['validator_violations']} "
          f"| helper<->validator mismatch={res['helper_validator_mismatches']}")
    for g in res["games"]:
        flag = "MISMATCH" if g["mismatch"] else ("VIOL" if g["validator_violations"] else "ok")
        a, h = g["matchup"].split(" @ ")
        print(f"  {(a.split()[-1]+'@'+h.split()[-1]):20} WIN_LOSS={g['win_loss']:4} RUN_TOTAL={g['run_total']:5} {flag}"
              + (f" missML={g['helper_missing_win_loss']} missOU={g['helper_missing_run_total']}" if g["mismatch"] or g["validator_violations"] else ""))
    return 2 if (res["helper_validator_mismatches"] or res.get("blind_integrity_violations") or not res["validator_ok"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
