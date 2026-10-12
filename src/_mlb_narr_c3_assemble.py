# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""C3 per-game raw 예측 → 기존 네레이터 reasoning 텍스트로 조립 (historical-note).

경기별 격리추론 산출 `_c_predictions_raw`(shadow)를 모아 `### {matchup} · RUN_TOTAL {line} / WIN_LOSS: / RUN_TOTAL: / 예측:`
형식으로 조립 → `_narr_model_defense_validate.py`로 검증 가능하게 한다.

★ Phase 0/1/2: **shadow 경로에만 write** (`postmortem/fullread_v4/c3_shadow/{date}/{scope}/assembled/`).
   root `_codex_gpt_reasoning_{date}__{scope}.txt`·production·notification 절대 미접촉(Phase 3 사용자 승인 후).
Codex 교차검증 대상.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SHADOW = ROOT / "postmortem" / "fullread_v4" / "c3_shadow"


def _win_loss_team(matchup: str, win_loss: str) -> str:
    """WIN_LOSS side(HOME/AWAY/-) → 팀명. reasoning 'WIN_LOSS: {team}' 렌더용."""
    win_loss = (win_loss or "-").upper()
    if " @ " not in (matchup or ""):
        return "-"
    away, home = [x.strip() for x in matchup.split(" @ ", 1)]
    if win_loss == "HOME":
        return home
    if win_loss == "AWAY":
        return away
    return "-"


def _fmt_line(v) -> str:
    """run_total_baseline 렌더 — 10.0 → '10.0' 유지(문자열이면 그대로)."""
    if v is None:
        return "?"
    return str(v)


def assemble(date: str, scope: str, raw_dir: str | None = None, out_dir: str | None = None) -> tuple[Path, int, list[str]]:
    base = SHADOW / date / scope
    rd = Path(raw_dir) if raw_dir else base / "raw"
    od = Path(out_dir) if out_dir else base / "assembled"
    # 안전장치: shadow 밖 write 금지
    od_resolved = od.resolve()
    if SHADOW.resolve() not in od_resolved.parents and od_resolved != SHADOW.resolve():
        raise ValueError(f"assembler out_dir must be under c3_shadow (Phase 0/1/2): {od}")
    od.mkdir(parents=True, exist_ok=True)

    rows = []
    warnings: list[str] = []
    for f in sorted(rd.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            warnings.append(f"parse_fail:{f.name}:{type(e).__name__}")
            continue
        rows.append(d)
    # gamePk canonical string 정렬 (기존 검토 fix2)
    rows.sort(key=lambda d: str(d.get("gamePk")))

    blocks = []
    game_index = []  # sidecar: gamePk↔matchup mapping and raw contract claims.
    for d in rows:
        mu = str(d.get("matchup") or "").strip()
        line = _fmt_line(d.get("run_total_baseline"))
        win_loss = str(d.get("win_loss") or "-").upper()
        run_total = str(d.get("run_total") or "-").upper()
        mlr = str(d.get("win_loss_reason") or "").strip()
        our = str(d.get("run_total_reason") or "").strip()
        if not mu or " @ " not in mu:
            warnings.append(f"bad_matchup:{d.get('gamePk')}")
            continue
        team = _win_loss_team(mu, win_loss)
        blocks.append(
            f"### {mu} [gamePk={d.get('gamePk')}] · RUN_TOTAL {line}\n"
            f"WIN_LOSS: {team} — {mlr}\n"
            f"RUN_TOTAL: {run_total} {line} — {our}\n"
            f"예측: WIN_LOSS={win_loss} RUN_TOTAL={run_total}"
        )
        game_index.append({
            "gamePk": str(d.get("gamePk")), "matchup": mu, "win_loss": win_loss, "run_total": run_total,
            # ★ raw의 contract_tokens_satisfied는 신뢰하지 않는다(기존 검토): 조립 텍스트 기준 validator 결과가 최종.
            "raw_claimed_satisfied": d.get("contract_tokens_satisfied"),
            "contract_tokens_required": d.get("contract_tokens_required"),
        })
    text = "\n\n".join(blocks) + "\n"
    out = od / f"reasoning__{scope}.txt"
    out.write_text(text, encoding="utf-8")
    # Sidecar keeps structured mapping even though assembled headings now carry gamePk.
    sidecar = od / f"report__{scope}.json"
    sidecar.write_text(json.dumps({
        "schema": "mlb_narr_c3_assemble_report.v1",
        "date": date, "scope": scope,
        "reasoning_path": str(out), "block_count": len(blocks),
        "warnings": warnings, "games": game_index,
        "note": "assembled headings carry gamePk for doubleheader-safe validation; raw contract claims remain advisory.",
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    return out, len(blocks), warnings


def main() -> int:
    ap = argparse.ArgumentParser(description="Assemble C3 per-game raw predictions into narrator reasoning text (shadow only).")
    ap.add_argument("--date", required=True)
    ap.add_argument("--scope", required=True)
    ap.add_argument("--raw-dir", default="")
    ap.add_argument("--out-dir", default="")
    ap.add_argument("--validate", action="store_true", help="assembled 결과를 model-defense validator로 검증")
    ap.add_argument("--gamepks", default="", help="--validate 시 scope gamepks")
    args = ap.parse_args()

    out, n, warns = assemble(args.date, args.scope, args.raw_dir or None, args.out_dir or None)
    print(f"[c3-assemble] wrote {out} blocks={n} warnings={len(warns)}")
    for w in warns[:20]:
        print(f"  warn: {w}")

    if args.validate:
        import _narr_model_defense_validate as V
        text = out.read_text(encoding="utf-8")
        report = V.validate_text(args.date, text, gamepks=args.gamepks or None)
        print(f"[c3-assemble] validate ok={report['ok']} checked={report['checked_games']} violations={len(report['violations'])}")
        if not report["ok"]:
            for v in report["violations"][:10]:
                print(f"  viol: {v.get('matchup')} | {v.get('axis')} | {v.get('error')}")
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
