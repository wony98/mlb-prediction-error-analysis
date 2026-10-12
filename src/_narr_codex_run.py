# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""_narr_codex_run.py — 반자동(semi-auto) 코덱스 네레이터 헬퍼 (4스텝 로드맵 #4 / Owner 결정 B).

무인 cron은 모델+메시지까지만(`codex_automation_runner` + `--model-notification-only`).
네레이터 예측은 **반자동** — 사용자 호출 시 클로드+코덱스 *둘이* 본다. 이 헬퍼는 그중 **코덱스 레인**을
한 번에 묶어 돌린다(클로드 1차 verdict/충돌검토는 별도 SOP, 여기 포함 안 함).

흐름(historical-note 표준):
  1차) run_mlb_v3.py --date D --gamepks G --codex-narrator
        → scoped 프롬프트 `_codex_gpt_prompt_D__SCOPE.md` 작성 후 handoff에서 자체 종료(메시지 송신 전).
  bridge) _codex_exec_bridge.py --req PROMPT --out REASONING --effort high --slot narr:D
        → codex 안정 호출. auth 실패(rc=4)면 **폴백 금지 · alert 후 중단**(올패스 금지, Owner 룰).
  2차) run_mlb_v3.py --date D --gamepks G --send-gamepks G --codex-narrator
        → reasoning 파싱 → _narr_predictions_D.json(frozen) 저장 + 메시지 송신.
  검증) 2차 stdout `notification table send: True` + _narr_predictions_D.json에 target gamePk frozen row 실재.

SCOPE = 'g' + sha1(','.join(sorted(gamepks)))[:8]  (run_mlb_v3.py와 동일 — 같은 --gamepks면 같은 파일).

usage:
  py _narr_codex_run.py --date 2026-06-22 --gamepks 778123,778124 [--effort high] [--timeout 600]
                        [--skip-first]  # 프롬프트 이미 있으면 1차 생략
                        [--dry]         # 2차 송신 생략(프롬프트+reasoning까지만, 검증 점검용)

rc: 0=ok · 2=인자/프롬프트 누락 · 3=1차 프롬프트 생성 실패 · 4=codex auth 실패(alert,폴백금지)
    · 5=bridge 실패(소진) · 6=2차/검증 실패
"""
from __future__ import annotations
import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from _mlb_send_owner_lock import SendLockError, send_lock_context

ROOT = Path(__file__).resolve().parent
PY = sys.executable

# 헬퍼 자체 stdout/stderr를 UTF-8로 고정(Windows cp949에서 em-dash/이모지 print 크래시 방지).
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass


def scope_id(gamepks_csv: str) -> str:
    """run_mlb_v3.py(line 50~53)와 동일 공식 — 같은 gamepks set은 같은 scope/파일명."""
    gp = [x.strip() for x in str(gamepks_csv or "").replace(";", ",").split(",") if x.strip()]
    if not gp:
        return ""
    return "g" + hashlib.sha1(",".join(sorted(gp)).encode("utf-8")).hexdigest()[:8]


def _paths(date: str, scope: str):
    suf = f"__{scope}" if scope else ""
    return (ROOT / f"_codex_gpt_prompt_{date}{suf}.md",
            ROOT / f"_codex_gpt_reasoning_{date}{suf}.txt")


def _has_machine_prediction_contract(text: str) -> bool:
    return _machine_prediction_contract_report(text).get("ok") is True


def _machine_prediction_contract_report(text: str, expected_games: int | None = None) -> dict:
    """Validate that every narrator block has exactly one parseable prediction line."""
    try:
        import _narr_model_defense_validate as _MDV
        records = _MDV._reasoning_block_records(text or "")
    except Exception:
        records = []
    missing = []
    multiple = []
    parsed_count = 0
    machine_line_re = re.compile(
        r"^\s*(?:(?:예측|최종예측|FINAL_PREDICTION)\s*:\s*|\?\?\s*)?"
        r"WIN_LOSS\s*=\s*(HOME|AWAY|-)\s+RUN_TOTAL\s*=\s*(UPPER|LOWER|-)\s*$",
        flags=re.I | re.MULTILINE,
    )
    has_gamepk_records = bool(records) and all(str(item.get("gamePk") or "").strip() for item in records)
    if has_gamepk_records:
        for item in records:
            block = str(item.get("block") or "")
            prediction_lines = machine_line_re.findall(block)
            label = f"{item.get('matchup')} [gamePk={item.get('gamePk')}]"
            if not prediction_lines:
                missing.append(label)
            elif len(prediction_lines) != 1:
                multiple.append({"matchup": label, "count": len(prediction_lines)})
            else:
                parsed_count += 1
        block_count = len(records)
    else:
        blocks = [part.strip() for part in re.split(r"(?=^###\s+)", text or "", flags=re.MULTILINE) if part.strip().startswith("###")]
        for block in blocks:
            heading = block.splitlines()[0].lstrip("# ").strip()
            prediction_lines = machine_line_re.findall(block)
            if not prediction_lines:
                missing.append(heading)
            elif len(prediction_lines) != 1:
                multiple.append({"matchup": heading, "count": len(prediction_lines)})
            else:
                parsed_count += 1
        block_count = len(blocks)
    ok = bool(block_count) and not missing and not multiple
    if expected_games is not None:
        ok = ok and block_count == int(expected_games)
    return {
        "ok": ok,
        "blocks": block_count,
        "expected_games": expected_games,
        "parsed": parsed_count,
        "missing_prediction_line": missing,
        "multiple_prediction_lines": multiple,
    }


def _handoff_path(date: str, scope: str) -> Path:
    suf = f"__{scope}" if scope else ""
    return ROOT / f"_codex_gpt_handoff_{date}{suf}.md"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _resolve_path(path_value: str) -> Path:
    path = Path(str(path_value).strip())
    if not path.is_absolute():
        path = ROOT / path
    return path


def _set_input_snapshot_env(path: Path, sha256: str | None = None) -> tuple[Path, str]:
    if not path.exists():
        raise FileNotFoundError(path)
    digest = sha256 or _sha256_file(path)
    os.environ["MLB_PREGAME_INPUT_SNAPSHOT"] = str(path)
    os.environ["MLB_PREGAME_INPUT_SNAPSHOT_SHA256"] = digest
    return path, digest


def _current_input_snapshot_path() -> Path | None:
    raw = (os.environ.get("MLB_PREGAME_INPUT_SNAPSHOT") or "").strip()
    if not raw:
        return None
    try:
        return _resolve_path(raw)
    except Exception:
        return None


def _run_mlb_v3_args(
    date: str,
    gp: str,
    *,
    send: bool = False,
    no_notification: bool = False,
    c3_prepare: bool = False,
    no_prediction_freeze: bool = False,
) -> list[str]:
    cmd = [PY, "run_mlb_v3.py", "--date", date, "--gamepks", gp]
    if send:
        cmd += ["--send-gamepks", gp]
    if no_notification:
        cmd.append("--no-notification")
    if no_prediction_freeze:
        cmd.append("--no-prediction-freeze")
    cmd.append("--c3-prepare" if c3_prepare else "--codex-narrator")
    snap = _current_input_snapshot_path()
    if snap:
        cmd += ["--input-snapshot", str(snap)]
    return cmd


def _c3_base(date: str, scope: str) -> Path:
    return ROOT / "postmortem" / "fullread_v4" / "c3_shadow" / date / scope


def _c3_problem_gamepks(date: str, scope: str) -> list[str]:
    base = _c3_base(date, scope)
    problems: set[str] = set()
    manifest = _load_json(base / "manifest.json")
    for item in manifest.get("results") or []:
        game_pk = str(item.get("gamePk") or "").strip()
        if game_pk and item.get("status") != "raw_written":
            problems.add(game_pk)
    crosscheck = _load_json(base / f"crosscheck__{scope}.json")
    for item in crosscheck.get("games") or []:
        game_pk = str(item.get("gamePk") or "").strip()
        if game_pk and (item.get("mismatch") or item.get("validator_violations")):
            problems.add(game_pk)
    return sorted(problems)


def _run_c3_primary(a, date: str, gp: str, scope: str, reasoning_path: Path) -> tuple[int, Path | None]:
    model = str(
        a.c3_model
        or a.model
        or os.environ.get("CODEX_EXEC_MODEL")
        or "gpt-5.6-sol"
    )
    workers = max(1, int(a.c3_workers or 1))
    if bool(getattr(a, "knowable_compliance_shadow_only", False)):
        workers = min(workers, 2)
    base_cmd = [
        PY,
        "_mlb_narr_c3_per_game_run.py",
        "--date", date,
        "--scope", scope,
        "--run-bridge",
        "--workers", str(workers),
        "--effort", str(a.effort),
        "--timeout", str(a.timeout),
        "--retries", str(a.retries),
        "--model", model,
        "--build-contexts",
        "--require-input-snapshot",
    ]
    if bool(getattr(a, "knowable_compliance_shadow_only", False)):
        base_cmd.extend([
            "--knowable-compliance-shadow",
            "--require-knowable-compliance-complete",
        ])
    first_cmd = [*base_cmd, "--gamepks", gp, "--refresh-c1-c2"]
    rc, _ = run_cmd_capture(first_cmd)
    if rc != 0:
        print(f"[narr_codex_run] C3 per-game bridge failed rc={rc}", file=sys.stderr)
        return 5, None
    if bool(getattr(a, "knowable_compliance_shadow_only", False)):
        manifest_path = ROOT / "postmortem" / "fullread_v4" / "c3_shadow" / date / scope / "manifest.json"
        manifest = _load_json(manifest_path)
        results = manifest.get("results") or []
        invalid = [str(item.get("gamePk")) for item in results if item.get("knowable_compliance_ok") is not True]
        if invalid:
            print(f"[narr_codex_run] historical-note shadow validation failed gamePks={invalid}", file=sys.stderr)
            return 5, None
        return 0, manifest_path

    max_repairs = max(0, min(int(a.repair_max_rounds or 0), 3)) if a.repair_on_validate_fail else 0
    for repair_round in range(max_repairs + 1):
        rc_cross, _ = run_cmd_capture([
            PY,
            "_mlb_narr_c3_crosscheck.py",
            "--date", date,
            "--scope", scope,
            "--gamepks", gp,
        ])
        problems = _c3_problem_gamepks(date, scope)
        if rc_cross == 0 and not problems:
            break
        if repair_round >= max_repairs or not problems:
            print(
                f"[narr_codex_run] C3 crosscheck failed rc={rc_cross} problems={problems} -> send blocked",
                file=sys.stderr,
            )
            return 5, None
        repair_csv = ",".join(problems)
        print(f"[narr_codex_run] C3 selective repair round={repair_round + 1} gamePks={repair_csv}")
        rc_repair, _ = run_cmd_capture([
            *base_cmd,
            "--gamepks", repair_csv,
            "--merge-existing-manifest",
        ])
        if rc_repair != 0:
            print(f"[narr_codex_run] C3 selective repair failed rc={rc_repair}", file=sys.stderr)
            return 5, None

    rc_promote, _ = run_cmd_capture([
        PY,
        "_mlb_narr_c3_promote.py",
        "--date", date,
        "--scope", scope,
        "--gamepks", gp,
        "--require-contract-data",
        "--promote",
        "--out", str(reasoning_path),
    ])
    if rc_promote != 0 or not reasoning_path.exists() or reasoning_path.stat().st_size < 20:
        print(f"[narr_codex_run] C3 promotion failed rc={rc_promote}; send blocked", file=sys.stderr)
        return 5, None
    review = _load_json(_c3_base(date, scope) / "promotion_review.json")
    contract_path = _c3_base(date, scope) / "contract_data.json"
    if not contract_path.exists():
        print("[narr_codex_run] C3 contract data missing after promotion; send blocked", file=sys.stderr)
        return 5, None
    os.environ["MLB_C3_CONTRACT_DATA_PATH"] = str(contract_path)
    context_path = Path(str(review.get("crosscheck_context_path") or ""))
    if not context_path.is_absolute():
        context_path = ROOT / context_path
    if not context_path.exists():
        print("[narr_codex_run] C3 compact crosscheck context missing; send blocked", file=sys.stderr)
        return 5, None
    print(
        f"[narr_codex_run] C3 primary promoted: games={review.get('game_count')} "
        f"snapshot={str(review.get('input_snapshot_sha256') or '')[:12]} warnings={len(review.get('warnings') or [])}"
    )
    return 0, context_path


def _freeze_input_snapshot_from_v3(date: str, scope: str, gp: str) -> tuple[Path, str] | tuple[None, None]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = ROOT / "postmortem" / "fullread_v4" / "live_input_snapshots" / date
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"mlb_pregame_input_snapshot_{date}__{scope}_{stamp}.json"
    rc, _ = run_cmd_capture([
        PY,
        "_mlb_pregame_freeze_inputs.py",
        "--date",
        date,
        "--source",
        "v3",
        "--gamepks",
        gp,
        "--write",
        "--out",
        str(out),
    ])
    if rc != 0 or not out.exists():
        print(f"[narr_codex_run] input snapshot freeze failed rc={rc}; send blocked", file=sys.stderr)
        return None, None
    digest = _sha256_file(out)
    sha_sidecar = out.with_suffix(out.suffix + ".sha256")
    try:
        if sha_sidecar.exists():
            sidecar_digest = sha_sidecar.read_text(encoding="utf-8").strip()
            if sidecar_digest:
                digest = sidecar_digest
    except Exception:
        pass
    _set_input_snapshot_env(out, digest)
    print(f"[narr_codex_run] input snapshot frozen: {out.relative_to(ROOT)} sha={digest[:12]}")
    return out, digest


def _parse_gamepks_csv(gamepks_csv: str) -> list[str]:
    return [x.strip() for x in str(gamepks_csv or "").replace(";", ",").split(",") if x.strip()]


def _snapshot_dual_prediction_artifacts(
    date: str,
    scope: str,
    gamepks_csv: str,
    tag: str,
    *,
    send_attempted: bool | None = None,
    notification_send_result: bool | None = None,
) -> Path | None:
    """Preserve dual-prediction artifacts before scoped files can be overwritten."""
    if not scope:
        return None
    suf = f"__{scope}"
    reasoning_mode = (os.environ.get("MLB_NARR_REASONING_MODE_EFFECTIVE") or "legacy").strip().lower()
    candidates: list[tuple[str, Path]] = [
        ("codex_reasoning", ROOT / f"_codex_gpt_reasoning_{date}{suf}.txt"),
        ("claude_indep", ROOT / f"_claude_indep_{date}{suf}.json"),
        ("claude_crosscheck", ROOT / f"_claude_crosscheck_{date}{suf}.json"),
        ("dual_verdicts", ROOT / f"_narr_dual_verdicts_{date}{suf}.json"),
        ("send_manifest", ROOT / f"_narr_send_manifest_{date}{suf}.json"),
        ("predictions", ROOT / f"_narr_predictions_{date}.json"),
        ("v3_games", ROOT / f"_v3_games_{date}.csv"),
        ("c3_promotion_review", _c3_base(date, scope) / "promotion_review.json"),
        ("c3_crosscheck", _c3_base(date, scope) / f"crosscheck__{scope}.json"),
        ("c3_crosscheck_context", _c3_base(date, scope) / "assembled" / f"crosscheck_context__{scope}.md"),
    ]
    if reasoning_mode != "c3":
        candidates.insert(0, ("codex_prompt", ROOT / f"_codex_gpt_prompt_{date}{suf}.md"))
    input_snapshot = _current_input_snapshot_path()
    if input_snapshot and input_snapshot.exists():
        candidates.append(("input_snapshot", input_snapshot))
    existing = [(name, path) for name, path in candidates if path.exists() and path.is_file()]
    if not existing:
        return None

    now_utc = datetime.now(timezone.utc)
    stamp = now_utc.strftime("%Y%m%dT%H%M%SZ")
    safe_tag = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(tag or "snapshot"))
    out_dir = ROOT / "postmortem" / "fullread_v4" / "dual_prediction_artifacts" / date / scope / f"{stamp}_{safe_tag}"
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "schema": "mlb_dual_prediction_artifact_snapshot.v1",
        "date": date,
        "scope": scope,
        "reasoning_mode": reasoning_mode,
        "tag": safe_tag,
        "gamepks": _parse_gamepks_csv(gamepks_csv),
        "created_at_utc": now_utc.isoformat(),
        "created_at_kst": now_utc.astimezone(timezone(timedelta(hours=9))).isoformat(),
        "send_attempted": send_attempted,
        "notification_send_result": notification_send_result,
        "artifacts": {},
    }
    copy_names = {
        "codex_prompt": "codex_prompt.md",
        "codex_reasoning": "codex_reasoning.txt",
        "claude_indep": "claude_indep.json",
        "claude_crosscheck": "claude_crosscheck.json",
        "dual_verdicts": "dual_verdicts.json",
        "send_manifest": "send_manifest.json",
        "predictions": "predictions_after_send.json" if safe_tag.startswith("post") else "predictions.json",
        "v3_games": "v3_games.csv",
        "input_snapshot": "input_snapshot.json",
        "c3_promotion_review": "c3_promotion_review.json",
        "c3_crosscheck": "c3_crosscheck.json",
        "c3_crosscheck_context": "c3_crosscheck_context.md",
    }
    for name, src in existing:
        dst = out_dir / copy_names.get(name, src.name)
        shutil.copy2(src, dst)
        manifest["artifacts"][name] = {
            "source_path": str(src.relative_to(ROOT)),
            "snapshot_path": str(dst.relative_to(ROOT)),
            "sha256": _sha256_file(dst),
            "bytes": dst.stat().st_size,
            "source_mtime_utc": datetime.fromtimestamp(src.stat().st_mtime, timezone.utc).isoformat(),
        }
    (out_dir / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[narr_codex_run] artifact snapshot saved: {out_dir.relative_to(ROOT)}")
    return out_dir


def run_cmd_capture(args, extra_env=None):
    """라이브 echo + stdout/stderr 캡처해 (rc, text) 반환."""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # Codex narrator lane defaults: preserve raw-PBP/deepread priors if the
    # caller forgot to seed the shell env. Explicit caller env still wins.
    env.setdefault("NARR_REASONING_PROVIDER", "codex")
    env.setdefault("NARR_INCLUDE_DEEPREAD_V3", "1")
    env.setdefault("NARR_INCLUDE_DEEPREAD_V3_LENS", "1")
    if extra_env:
        env.update(extra_env)
    print(f">>> {' '.join(str(a) for a in args)}", flush=True)
    proc = subprocess.Popen(
        [str(a) for a in args], cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    lines = []
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        lines.append(line)
    proc.wait()
    return proc.returncode, "".join(lines)




def verify_narr_predictions(date: str, gamepks_csv: str) -> tuple[bool, str]:
    """_narr_predictions_DATE.json에 target gamePk가 frozen row로 실재하는지."""
    p = ROOT / f"_narr_predictions_{date}.json"
    if not p.exists():
        return False, "narr_predictions_missing"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"narr_predictions_parse_error={type(e).__name__}"
    # 저장 포맷: {gamePk(str): {...,'frozen':bool}} (run_mlb_v3 save_predictions).
    predictions = data if isinstance(data, dict) else {}
    want = [x.strip() for x in str(gamepks_csv or "").replace(";", ",").split(",") if x.strip()]
    miss, unfrozen = [], []
    for g in want:
        pv = predictions.get(str(g))
        if not isinstance(pv, dict):
            miss.append(g)
        elif not pv.get("frozen"):
            unfrozen.append(g)
    if miss:
        return False, f"gamepks_missing_in_predictions={miss}"
    if unfrozen:
        return False, f"gamepks_not_frozen={unfrozen}"
    return True, f"ok({len(want)} frozen)"


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _repair_report_path(date: str, run_name: str) -> Path:
    return ROOT / "postmortem" / "fullread_v4" / "t036_no_silent_signal" / "repair_runs" / date / run_name / "repair_report.json"


def _prediction_change_events_from_repair_report(report: dict) -> list[dict]:
    events: list[dict] = []
    for rr in report.get("rounds") or []:
        for guard in rr.get("ritual_guards") or []:
            first_prediction = guard.get("first_pass_prediction") or {}
            repaired_prediction = guard.get("repaired_prediction") or {}
            base = {
                "round": rr.get("round"),
                "gamePk": guard.get("gamePk"),
                "matchup": guard.get("matchup"),
                "first_pass_prediction": first_prediction,
                "repaired_prediction": repaired_prediction,
            }
            events.extend(_classify_prediction_events(base))
    return events


def _prediction_changes_from_repair_report(report: dict) -> list[dict]:
    """Backward-compatible helper used by tests and older handoff notes."""
    return _prediction_change_events_from_repair_report(report)


def _side(value) -> str:
    return str(value or "-").strip().upper() or "-"


def _axis_prediction_event(base: dict, axis: str, first_side: str, repaired_side: str) -> dict | None:
    real = {"HOME", "AWAY"} if axis == "WIN_LOSS" else {"UPPER", "LOWER"}
    first_is_real = first_side in real
    repaired_is_real = repaired_side in real
    if first_side == repaired_side:
        return None
    if not first_is_real and repaired_is_real:
        kind = "fill"
    elif first_is_real and repaired_side != first_side:
        kind = "flip"
    else:
        kind = "other"
    return {
        **base,
        "axis": axis,
        "kind": kind,
        "first_side": first_side,
        "repaired_side": repaired_side,
    }


def _classify_prediction_events(change: dict) -> list[dict]:
    first_prediction = change.get("first_pass_prediction") or {}
    repaired_prediction = change.get("repaired_prediction") or {}
    events = []
    win_loss_event = _axis_prediction_event(change, "WIN_LOSS", _side(first_prediction.get("win_loss_side")), _side(repaired_prediction.get("win_loss_side")))
    if win_loss_event:
        events.append(win_loss_event)
    run_total_event = _axis_prediction_event(change, "RUN_TOTAL", _side(first_prediction.get("run_total_side")), _side(repaired_prediction.get("run_total_side")))
    if run_total_event:
        events.append(run_total_event)
    return events


def _surface_repair_prediction_events(date: str, scope: str, run_name: str, events: list[dict], report_path: Path) -> tuple[Path, Path] | tuple[None, None]:
    if not events:
        return None, None
    now_utc = datetime.now(timezone.utc)
    log_dir = ROOT / "postmortem" / "automation_logs" / "mlb_narrator_repair"
    log_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "mlb_narrator_repair_prediction_change_surface.v1",
        "date": date,
        "scope": scope,
        "run_name": run_name,
        "created_at_utc": now_utc.isoformat(),
        "created_at_kst": now_utc.astimezone(timezone(timedelta(hours=9))).isoformat(),
        "report_path": str(report_path),
        "events": events,
        "fills": [e for e in events if e.get("kind") == "fill"],
        "flips": [e for e in events if e.get("kind") == "flip"],
        "policy": "fills require repaired-run Claude crosscheck; flips require explicit Claude flip_ack approval or hard-stop",
    }
    json_path = ROOT / f"_narr_repair_surface_{date}__{scope}.json"
    md_path = ROOT / f"_narr_repair_surface_{date}__{scope}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# MLB Narrator Repair Prediction Change Surface",
        "",
        f"- date: `{date}`",
        f"- scope: `{scope}`",
        f"- run_name: `{run_name}`",
        f"- report: `{report_path}`",
        "- policy: `fill=crosscheck-required, flip=explicit-crosscheck-ack-required`",
        "",
    ]
    for item in events:
        lines.append(
            f"- `{item.get('kind')}` `{item.get('axis')}` `{item.get('matchup')}` gamePk=`{item.get('gamePk')}` "
            f"{item.get('first_side')} -> {item.get('repaired_side')}"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with (log_dir / "prediction_event_surface.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return json_path, md_path


def _write_repair_diff_payload(date: str, scope: str, run_name: str, report_path: Path, events: list[dict]) -> Path:
    path = ROOT / f"_narr_repair_diff_{date}__{scope}.json"
    payload = {
        "schema": "mlb_narrator_repair_prediction_diff.v1",
        "date": date,
        "scope": scope,
        "run_name": run_name,
        "report_path": str(report_path),
        "events": events,
        "fills": [e for e in events if e.get("kind") == "fill"],
        "flips": [e for e in events if e.get("kind") == "flip"],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _combine_diff_payloads(date: str, scope: str, paths: list[Path | None]) -> Path | None:
    real_paths = [p for p in paths if p and Path(p).exists()]
    if not real_paths:
        return None
    events: list[dict] = []
    sources = []
    for path in real_paths:
        data = _load_json(Path(path))
        sources.append(str(path))
        for event in data.get("events") or []:
            if isinstance(event, dict):
                events.append(event)
    out = ROOT / f"_narr_combined_review_diff_{date}__{scope}.json"
    payload = {
        "schema": "mlb_narrator_combined_prediction_diff.v1",
        "date": date,
        "scope": scope,
        "sources": sources,
        "events": events,
        "fills": [e for e in events if e.get("kind") == "fill"],
        "flips": [e for e in events if e.get("kind") == "flip"],
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out


def _crosscheck_verdict_allows_send(cross_path: Path, repair_diff_path: Path | None = None) -> tuple[bool, str]:
    data = _load_json(cross_path)
    verdict = str(data.get("verdict") or "").strip().lower()
    if not verdict:
        return False, "missing verdict"
    if verdict != "ok":
        return False, f"verdict={verdict}"
    diff = _load_json(repair_diff_path) if repair_diff_path else {}
    flips = diff.get("flips") or []
    if not flips:
        return True, "ok"
    acks = data.get("flip_acks") or data.get("repair_flip_acks") or []
    approved = set()
    for ack in acks if isinstance(acks, list) else []:
        decision = str(ack.get("decision") or ack.get("verdict") or ack.get("approval") or "").strip().lower()
        if decision not in {"approve", "approved", "ok"}:
            continue
        key = (
            str(ack.get("gamePk") or ack.get("game_pk") or "").strip(),
            str(ack.get("axis") or "").strip().upper(),
        )
        approved.add(key)
    missing = []
    for flip in flips:
        key = (str(flip.get("gamePk") or "").strip(), str(flip.get("axis") or "").strip().upper())
        if key not in approved:
            missing.append({"gamePk": key[0], "axis": key[1], "matchup": flip.get("matchup")})
    if missing:
        return False, f"missing explicit flip ack: {missing}"
    return True, f"ok with {len(flips)} flip ack(s)"


def _hard_stop_alert_enabled(a) -> bool:
    if getattr(a, "dry", False):
        return False
    return str(os.environ.get("NARR_HELD_ALERT_NOTIFICATION") or "1").strip().lower() not in {"0", "false", "no", "off"}


def _safe_scope_label(value: str) -> str:
    text = str(value or "").strip() or "full"
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in text)


def _run_slate_completeness_audit(date: str, scope: str, gamepks_csv: str, stage: str) -> None:
    """Warn-only target/full slate completeness reports."""
    if str(os.environ.get("MLB_NARR_COMPLETENESS_AUDIT_DISABLE") or "").strip().lower() in {"1", "true", "yes", "on"}:
        return
    safe = _safe_scope_label(scope)
    stage_safe = _safe_scope_label(stage)
    checks = [
        [PY, "_mlb_narrator_slate_completeness.py", "--date", date, "--gamepks", gamepks_csv, "--scope", f"{safe}_{stage_safe}_target", "--write"],
        [PY, "_mlb_narrator_slate_completeness.py", "--date", date, "--scope", f"{safe}_{stage_safe}_full", "--write"],
    ]
    for cmd in checks:
        try:
            rc, _ = run_cmd_capture(cmd)
            if rc != 0:
                print(f"[narr_codex_run] slate completeness warn rc={rc} (non-blocking)")
        except Exception as exc:
            print(f"[narr_codex_run] slate completeness warn: {type(exc).__name__}: {exc}")


def _notify_hard_stop(a, date: str, scope: str, gamepks_csv: str, reason: str, details: dict | None = None) -> Path:
    """Persist a local audit receipt; no outbound delivery."""
    out_dir = Path(os.environ.get('MLB_OUTPUT_DIR', 'outputs')) / 'hard_stop'
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f'held_{date}__{scope}.json'
    payload = {'date': date, 'scope': scope, 'gamepks': _parse_gamepks_csv(gamepks_csv), 'reason': reason, 'details': details or {}, 'delivery': 'local_file_only'}
    path.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding='utf-8')
    _run_slate_completeness_audit(date, scope, gamepks_csv, f'held_{reason}')
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="반자동 코덱스 네레이터 헬퍼(코덱스 레인).")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (생략+--today면 ET-오늘 자동).")
    ap.add_argument("--gamepks", default=None, help="comma-separated gamePk (생략+--today면 그날 전경기 자동).")
    ap.add_argument("--today", action="store_true", help="실전 루틴용: ET-오늘 날짜 + 그날 MLB 전경기 gamePk 자동 계산.")
    ap.add_argument("--effort", default="high", help="codex model_reasoning_effort (default high).")
    ap.add_argument("--timeout", type=int, default=600, help="bridge codex 호출 timeout(s).")
    ap.add_argument("--retries", type=int, default=1, help="bridge 재시도 횟수.")
    ap.add_argument("--model", default="", help="Codex model for bridge/repair. Defaults to CODEX_EXEC_MODEL or ~/.codex/config.toml model.")
    ap.add_argument(
        "--reasoning-mode",
        choices=["c3", "legacy", "direct-review"],
        default=(
            (os.environ.get("MLB_NARR_REASONING_MODE") or "c3").strip().lower()
            if (os.environ.get("MLB_NARR_REASONING_MODE") or "c3").strip().lower() in {"c3", "legacy", "direct-review"}
            else "c3"
        ),
        help="c3=default; legacy=explicit fallback; direct-review=consume completed direct reviews, dry-only and no LLM/send.",
    )
    ap.add_argument("--c3-workers", type=int, default=int(os.environ.get("MLB_NARR_C3_WORKERS") or 8))
    ap.add_argument(
        "--knowable-compliance-shadow-only",
        action="store_true",
        help="Run historical-note C3 accounting in an isolated shadow scope; never promote, freeze predictions, or send Notification.",
    )
    ap.add_argument(
        "--c3-model",
        default=os.environ.get("MLB_NARR_C3_MODEL", "gpt-5.6-sol"),
        help="Verified model for C3 per-game bridge (default gpt-5.6-sol).",
    )
    ap.add_argument("--skip-first", action="store_true", help="프롬프트가 이미 있으면 1차 생략.")
    ap.add_argument("--dry", action="store_true", help="2차 송신 생략(프롬프트+reasoning까지만).")
    ap.add_argument("--direct-review-index", type=Path)
    ap.add_argument("--direct-review-dir", type=Path)
    ap.add_argument("--direct-review-output", type=Path)
    ap.add_argument("--input-snapshot", default="",
                    help="Use this frozen pregame input snapshot for both run_mlb_v3 stages.")
    ap.add_argument("--no-prediction-freeze", action="store_true",
                    help="Research/repair only: do not restore prior frozen predictions when recomputing from a corrected input snapshot.")
    ap.set_defaults(freeze_input_snapshot=True)
    ap.add_argument("--freeze-input-snapshot", dest="freeze_input_snapshot", action="store_true",
                    help="After first run, freeze the produced v3 inputs and reuse that snapshot for the send stage. Default on.")
    ap.add_argument("--no-freeze-input-snapshot", dest="freeze_input_snapshot", action="store_false",
                    help="Disable automatic input snapshot freeze. Use only for debugging.")
    ap.add_argument("--repair-on-validate-fail", action="store_true",
                    help="If model-defense validation fails, re-reason affected blocks, revalidate, then require Claude crosscheck.")
    ap.add_argument("--repair-max-rounds", type=int, default=3)
    ap.add_argument("--repair-chunk-size", type=int, default=4)
    ap.add_argument("--repair-timeout", type=int, default=900)
    ap.add_argument("--claude-crosscheck", action="store_true",
                    help="Codex reasoning 생성 후 Claude 짧은 JSON 교차검증을 수행한다. 기본은 warn-only.")
    ap.add_argument("--claude-crosscheck-required", action="store_true",
                    help="Claude 교차검증 실패 시 runner를 실패 처리한다.")
    ap.add_argument("--claude-timeout", type=int, default=240,
                    help="Claude 교차검증 timeout seconds.")
    ap.add_argument("--claude-model", default="opus",
                    help="Claude 교차검증 모델. 운영 기본 opus.")
    ap.add_argument("--claude-effort", default="high",
                    help="Claude 교차검증 effort. 운영 기본 high.")
    ap.add_argument("--claude-allow-large", action="store_true",
                    help="Claude 교차검증에 큰 prompt를 허용한다.")
    ap.set_defaults(resolve_claude_review=True)
    ap.add_argument("--resolve-claude-review", dest="resolve_claude_review", action="store_true",
                    help="If required Claude crosscheck returns review_needed, re-reason affected blocks once and crosscheck again.")
    ap.add_argument("--no-resolve-claude-review", dest="resolve_claude_review", action="store_false",
                    help="Disable automatic Claude review_needed resolution.")
    ap.add_argument("--send-owner", default=os.environ.get("MLB_SEND_OWNER", "codex_manual"),
                    help="Owner name for the shared MLB send lock. Default: MLB_SEND_OWNER or codex_manual.")
    a = ap.parse_args()

    if a.reasoning_mode == "direct-review":
        if (not a.dry or a.today or not a.date or not a.gamepks
                or not all((a.direct_review_index,a.direct_review_dir,a.direct_review_output))
                or a.claude_crosscheck or a.claude_crosscheck_required or a.repair_on_validate_fail
                or a.input_snapshot or a.knowable_compliance_shadow_only):
            print("[narr_codex_run] direct-review requires explicit date/gamepks/index/review-dir/output and --dry; no prepare/repair/crosscheck/send", file=sys.stderr)
            return 2
        sys.path.insert(0, str(ROOT / "tools"))
        from mlb_direct_review_run import run as run_direct_review
        try:
            report=run_direct_review(a.date,a.gamepks,a.direct_review_index,a.direct_review_dir,a.direct_review_output)
        except (AssertionError,KeyError,TypeError,ValueError,OSError) as exc:
            print(f"[narr_codex_run] direct-review invalid request: {type(exc).__name__}: {exc}",file=sys.stderr)
            return 2
        print(f"[narr_codex_run] direct-review {report['status']}; games={len(report['games'])}; LLM=0; Notification=False")
        return 0 if report['ok'] else 6
    if any((a.direct_review_index,a.direct_review_dir,a.direct_review_output)):
        print("[narr_codex_run] direct review arguments require --reasoning-mode direct-review",file=sys.stderr)
        return 2

    date, gp = a.date, a.gamepks
    if a.today:
        # 실전: ET-오늘 + 그날 전경기 gamePk 자동 (매일 같은 명령으로 돌게).
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        date = (_dt.now(_tz.utc) - _td(hours=4)).strftime("%Y-%m-%d")
        try:
            import _mlb_schedule as _sch
            games = _sch.fetch_games(date)
        except Exception as e:
            print(f"[narr_codex_run] --today 스케줄 조회 실패: {type(e).__name__}: {e} → rc=2", file=sys.stderr)
            return 2
        if not games:
            print(f"[narr_codex_run] --today {date}: 경기 없음(오프데이) → 종료 rc=0")
            return 0
        gp = ",".join(str(g["gamePk"]) for g in games)
        print(f"[narr_codex_run] --today 해석: date={date} 경기수={len(games)}")
    if not date or not gp:
        print("[narr_codex_run] --date/--gamepks 필요(또는 --today) → rc=2", file=sys.stderr)
        return 2
    scope = scope_id(gp)
    if not scope:
        print("[narr_codex_run] --gamepks 비어있음 → rc=2", file=sys.stderr)
        return 2
    # 중복 실행 방지 잠금(2026-06-22 동시 실행 사고 방지): 같은 날짜로 이미 실행 중이면 종료.
    # 45분(2700s) 넘은 lock은 죽은 실행으로 보고 회수한다.
    if str(a.input_snapshot or "").strip():
        try:
            snap_path = _resolve_path(a.input_snapshot)
            snap_path, snap_sha = _set_input_snapshot_env(snap_path)
            try:
                snap_label = str(snap_path.relative_to(ROOT))
            except Exception:
                snap_label = str(snap_path)
            print(f"[narr_codex_run] using supplied input snapshot: {snap_label} sha={snap_sha[:12]}")
        except Exception as exc:
            print(f"[narr_codex_run] invalid --input-snapshot: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
    import time as _t
    lock = ROOT / f"_narr_codex_run_{date}.lock"
    if lock.exists():
        age = _t.time() - lock.stat().st_mtime
        if age < 2700:
            print(f"[narr_codex_run] 이미 실행 중(lock age {int(age)}s) → 중복 실행 방지, 종료 rc=7", file=sys.stderr)
            return 7
        print(f"[narr_codex_run] stale lock 회수(age {int(age)}s)")
    lock.write_text(str(os.getpid()))
    try:
        if getattr(a, "dry", False) and a.reasoning_mode != "c3":
            return _pipeline(a, date, gp, scope)
        if a.knowable_compliance_shadow_only:
            if a.reasoning_mode != "c3":
                print("[narr_codex_run] historical-note shadow-only requires --reasoning-mode c3", file=sys.stderr)
                return 2
            return _pipeline(a, date, gp, scope)
        try:
            with send_lock_context(
                date,
                owner=a.send_owner,
                scope=scope,
                stage="assemble",
                command=" ".join(sys.argv),
                metadata={"runner": "_narr_codex_run.py", "gamepks": gp},
            ) as send_lock:
                print(
                    f"[narr_codex_run] send owner lock "
                    f"{'reused' if send_lock.reused else 'acquired'}: "
                    f"{send_lock.path.relative_to(ROOT)} owner={send_lock.owner} scope={send_lock.scope}"
                )
                return _pipeline(a, date, gp, scope)
        except SendLockError as exc:
            print(f"[narr_codex_run] send owner lock blocked duplicate run: {exc}", file=sys.stderr)
            return 7
    finally:
        try:
            lock.unlink()
        except Exception:
            pass


def _pipeline(a, date: str, gp: str, scope: str) -> int:
    prompt_path, reasoning_path = _paths(date, scope)
    c3_mode = a.reasoning_mode == "c3"
    os.environ["MLB_NARR_REASONING_MODE_EFFECTIVE"] = a.reasoning_mode
    os.environ.pop("MLB_C3_CONTRACT_DATA_PATH", None)
    crosscheck_context_path = prompt_path
    print(f"[narr_codex_run] date={date} scope={scope} reasoning_mode={a.reasoning_mode}")
    print(f"  prompt   = {'per-game C3 shadow prompts' if c3_mode else prompt_path.name}")
    print(f"  reasoning= {reasoning_path.name}")

    # --- prepare: shared v3/guard/context inputs; C3 skips the giant prompt ---
    handoff_path = _handoff_path(date, scope)
    stale_paths = [p for p in (prompt_path, reasoning_path, handoff_path) if p.exists()]
    if stale_paths and not a.skip_first:
        _snapshot_dual_prediction_artifacts(date, scope, gp, "pre_overwrite")
    if reasoning_path.exists():
        reasoning_path.unlink()
        print(f"[narr_codex_run] removed stale scoped reasoning before 1st run: {reasoning_path.name}")
    if c3_mode:
        v3_path = ROOT / f"_v3_games_{date}.csv"
        context_path = ROOT / f"_narr_context_{date}.txt"
        if a.skip_first and v3_path.exists() and context_path.exists():
            print("[narr_codex_run] C3 prepare 생략(--skip-first, v3/context 존재).")
        else:
            rc1, _ = run_cmd_capture(
                _run_mlb_v3_args(
                    date,
                    gp,
                    no_notification=True,
                    c3_prepare=True,
                    no_prediction_freeze=a.no_prediction_freeze,
                ),
            )
            if rc1 != 0 or not v3_path.exists() or v3_path.stat().st_size == 0:
                print(f"[narr_codex_run] C3 prepare 실패(rc={rc1}, v3 missing/empty) -> rc=3", file=sys.stderr)
                return 3
            narr_context_script = ROOT / "narr_context.py"
            rc_ctx, _ = run_cmd_capture([PY, str(narr_context_script), date])
            if rc_ctx != 0 or not context_path.exists() or context_path.stat().st_size == 0:
                print(f"[narr_codex_run] C3 narr_context build failed rc={rc_ctx} -> rc=3", file=sys.stderr)
                return 3
            print(f"[narr_codex_run] C3 prepare OK - v3={v3_path.stat().st_size}B context={context_path.stat().st_size}B")
    else:
        if not a.skip_first:
            for stale in (prompt_path, handoff_path):
                if stale.exists():
                    stale.unlink()
                    print(f"[narr_codex_run] removed stale scoped {stale.name} before 1st run")
        if a.skip_first and prompt_path.exists() and prompt_path.stat().st_size > 0:
            print("[narr_codex_run] legacy 1차 생략(--skip-first, 프롬프트 존재).")
        else:
            rc1, _ = run_cmd_capture(
                _run_mlb_v3_args(
                    date,
                    gp,
                    no_notification=True,
                    no_prediction_freeze=a.no_prediction_freeze,
                ),
            )
            if not (prompt_path.exists() and prompt_path.stat().st_size > 0):
                print(f"[narr_codex_run] 1차 프롬프트 생성 실패(rc={rc1}, {prompt_path.name} 없음) -> rc=3", file=sys.stderr)
                return 3
            print(f"[narr_codex_run] legacy 1차 OK - {prompt_path.name} ({prompt_path.stat().st_size}B)")

    # --- bridge: codex 안정 호출 ---
    if a.freeze_input_snapshot and not _current_input_snapshot_path():
        snap_path, _snap_sha = _freeze_input_snapshot_from_v3(date, scope, gp)
        if not snap_path:
            _notify_hard_stop(
                a,
                date,
                scope,
                gp,
                "input_snapshot_freeze_failed",
                {"policy": "same-scope narrator send requires frozen v3 input snapshot"},
            )
            return 9

    if c3_mode:
        rc_c3, c3_context = _run_c3_primary(a, date, gp, scope, reasoning_path)
        if a.knowable_compliance_shadow_only and rc_c3 == 0:
            print(f"[narr_codex_run] historical-note shadow complete; no promotion/send: {c3_context}")
            return 0
        if rc_c3 != 0 or c3_context is None:
            return rc_c3 or 5
        crosscheck_context_path = c3_context
    else:
        if (os.environ.get("MLB_PROMPT_COMPLETENESS_VALIDATE_DISABLE") or "").strip().lower() not in {"1", "true", "yes"}:
            rc_pc, _ = run_cmd_capture([
                PY, "_mlb_prompt_completeness_validate.py",
                "--date", date,
                "--prompt", str(prompt_path),
                "--gamepks", gp,
                "--scope", scope,
                "--write",
            ])
            if rc_pc != 0:
                print(f"[narr_codex_run] prompt completeness warn-only validator failed rc={rc_pc}; continuing", file=sys.stderr)

        rc_b, _ = run_cmd_capture([
            PY, "_codex_exec_bridge.py", "--req", str(prompt_path), "--out", str(reasoning_path),
            "--effort", a.effort, "--timeout", str(a.timeout), "--retries", str(a.retries),
            "--slot", f"narr:{date}",
            "--model", str(a.model or ""),
            "--direct-pipe",
            "--ignore-user-config",
        ])
        if rc_b == 4:
            print("\n" + "=" * 60, file=sys.stderr)
            print("[narr_codex_run] CODEX auth failed; no fallback/all-pass. Send blocked.", file=sys.stderr)
            print("=" * 60, file=sys.stderr)
            return 4
        if rc_b != 0 or not (reasoning_path.exists() and reasoning_path.stat().st_size >= 20):
            print(f"[narr_codex_run] legacy bridge failed rc={rc_b}; send blocked", file=sys.stderr)
            return 5
        print(f"[narr_codex_run] legacy bridge OK - {reasoning_path.name} ({reasoning_path.stat().st_size}B)")

    # 검토포인트C(historical-note): bridge 산출물이 narrator contract(경기블록 ### + 기계판독 prediction 줄)를
    # 갖췄는지 2차 소비 전에 pre-validate. 빈/엉뚱 출력이 _narr_predictions/메시지로 새는 걸 막는다.
    _rtxt = reasoning_path.read_text(encoding="utf-8", errors="replace")
    _contract = _machine_prediction_contract_report(_rtxt, expected_games=len(_parse_gamepks_csv(gp)))
    if not _contract.get("ok"):
        print(f"[narr_codex_run] reasoning가 narrator contract(### + per-game machine-readable prediction line) 미충족 -> rc=5", file=sys.stderr)
        print(json.dumps(_contract, ensure_ascii=False, indent=2), file=sys.stderr)
        return 5
    print("[narr_codex_run] reasoning contract OK (### + per-game machine-readable prediction line)")

    repair_applied = False
    repair_diff_path: Path | None = None
    no_selection_marker_path: Path | None = None
    rc_v, _ = run_cmd_capture([
        PY, "_narr_model_defense_validate.py",
        "--date", date,
        "--reasoning", str(reasoning_path),
        "--gamepks", gp,
        "--scope", scope,
        "--write",
        "--strict",
    ])
    if rc_v != 0:
        if not a.repair_on_validate_fail:
            print("[narr_codex_run] model-defense contract failed -> rc=5 (send blocked)", file=sys.stderr)
            return 5
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_name = f"normal_{scope}_{stamp}"
        repaired_path = ROOT / f"_codex_gpt_reasoning_{date}__{scope}.repaired.txt"
        print(f"[narr_codex_run] model-defense failed -> repair loop start run={run_name}")
        rc_r, _ = run_cmd_capture([
            PY, "_mlb_narrator_reasoning_repair.py",
            "--date", date,
            "--reasoning", str(reasoning_path),
            "--out", str(repaired_path),
            "--gamepks", gp,
            "--run-name", run_name,
            "--max-rounds", str(a.repair_max_rounds),
            "--chunk-size", str(a.repair_chunk_size),
            "--effort", str(a.effort),
            "--model", str(a.model or ""),
            "--llm-timeout", str(a.repair_timeout),
        ])
        report_path = _repair_report_path(date, run_name)
        report = _load_json(report_path)
        events = _prediction_change_events_from_repair_report(report)
        if events:
            surface_json, surface_md = _surface_repair_prediction_events(date, scope, run_name, events, report_path)
            repair_diff_path = _write_repair_diff_payload(date, scope, run_name, report_path, events)
            fill_count = sum(1 for e in events if e.get("kind") == "fill")
            flip_count = sum(1 for e in events if e.get("kind") == "flip")
            other_count = len(events) - fill_count - flip_count
            print(
                f"[narr_repair] PREDICTION EVENTS fill={fill_count} flip={flip_count} other={other_count} "
                f"surface={surface_json} {surface_md} diff={repair_diff_path}",
                file=sys.stderr,
            )
        if rc_r != 0 or not (repaired_path.exists() and repaired_path.stat().st_size >= 20):
            print(f"[narr_codex_run] repair failed rc={rc_r} -> rc=5 (send blocked)", file=sys.stderr)
            _notify_hard_stop(
                a,
                date,
                scope,
                gp,
                "repair_failed",
                {"repair_rc": rc_r, "report_path": str(report_path), "repaired_path": str(repaired_path)},
            )
            return 5
        rc_v2, _ = run_cmd_capture([
            PY, "_narr_model_defense_validate.py",
            "--date", date,
            "--reasoning", str(repaired_path),
            "--gamepks", gp,
            "--scope", scope,
            "--write",
            "--strict",
        ])
        if rc_v2 != 0:
            print("[narr_codex_run] repaired reasoning still fails validator -> rc=5 (send blocked)", file=sys.stderr)
            _notify_hard_stop(
                a,
                date,
                scope,
                gp,
                "repaired_validation_failed",
                {"validator_rc": rc_v2, "report_path": str(report_path), "repaired_path": str(repaired_path)},
            )
            return 5
        _repaired_text = repaired_path.read_text(encoding="utf-8", errors="replace")
        _repaired_contract = _machine_prediction_contract_report(_repaired_text, expected_games=len(_parse_gamepks_csv(gp)))
        if not _repaired_contract.get("ok"):
            print("[narr_codex_run] repaired reasoning fails machine prediction contract -> rc=5 (send blocked)", file=sys.stderr)
            print(json.dumps(_repaired_contract, ensure_ascii=False, indent=2), file=sys.stderr)
            _notify_hard_stop(
                a,
                date,
                scope,
                gp,
                "repaired_machine_contract_failed",
                {"contract": _repaired_contract, "report_path": str(report_path), "repaired_path": str(repaired_path)},
            )
            return 5
        tmp_original = ROOT / f"_codex_gpt_reasoning_{date}__{scope}.pre_repair_{stamp}.txt"
        shutil.copy2(reasoning_path, tmp_original)
        os.replace(repaired_path, reasoning_path)
        repair_applied = True
        print(f"[narr_codex_run] repair OK -> reasoning replaced; original snapshot={tmp_original.name}")

    crosscheck_required = bool(a.claude_crosscheck_required or repair_applied)
    crosscheck_requested = bool(a.claude_crosscheck or crosscheck_required)
    if repair_applied and not a.claude_crosscheck_required:
        print("[narr_codex_run] repair applied -> Claude crosscheck REQUIRED before send")
    if crosscheck_requested:
        cross_path = ROOT / f"_claude_crosscheck_{date}__{scope}.json"
        cmd = [
            PY, "_claude_crosscheck.py",
            "--league", "mlb",
            "--date", date,
            "--scope", scope,
            "--context-file", str(crosscheck_context_path),
            "--codex-file", str(reasoning_path),
            "--out", str(cross_path),
            "--timeout", str(a.claude_timeout),
            "--model", str(a.claude_model),
            "--effort", str(a.claude_effort),
        ]
        if a.claude_allow_large:
            cmd.append("--allow-large")
        if repair_diff_path:
            cmd += ["--repair-diff-file", str(repair_diff_path)]
        rc_c, _ = run_cmd_capture(cmd)
        if rc_c != 0:
            msg = f"[narr_codex_run] Claude crosscheck failed rc={rc_c}"
            if crosscheck_required:
                print(msg + " -> rc=8", file=sys.stderr)
                _notify_hard_stop(
                    a,
                    date,
                    scope,
                    gp,
                    "claude_crosscheck_unavailable",
                    {"crosscheck_rc": rc_c, "repair_diff_path": str(repair_diff_path or ""), "cross_path": str(cross_path)},
                )
                return 8
            print(msg + " (warn-only; continuing)", file=sys.stderr)
        elif cross_path.exists():
            print(f"[narr_codex_run] Claude crosscheck OK -> {cross_path.name}")
            if crosscheck_required:
                allowed, why = _crosscheck_verdict_allows_send(cross_path, repair_diff_path)
                if not allowed:
                    if getattr(a, "resolve_claude_review", True) and why == "verdict=review_needed":
                        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                        run_name = f"claude_review_{scope}_{stamp}"
                        resolved_path = ROOT / f"_codex_gpt_reasoning_{date}__{scope}.claude_resolved.txt"
                        print(f"[narr_codex_run] Claude review_needed -> auto resolve affected blocks run={run_name}")
                        rc_res, _ = run_cmd_capture([
                            PY, "_mlb_narrator_claude_review_resolve.py",
                            "--date", date,
                            "--reasoning", str(reasoning_path),
                            "--crosscheck", str(cross_path),
                            "--out", str(resolved_path),
                            "--gamepks", gp,
                            "--run-name", run_name,
                            "--chunk-size", str(a.repair_chunk_size),
                            "--effort", str(a.effort),
                            "--model", str(a.model or ""),
                            "--llm-timeout", str(a.repair_timeout),
                        ])
                        review_diff_path = ROOT / f"_narr_claude_review_diff_{date}__{scope}.json"
                        if rc_res == 0 and resolved_path.exists() and resolved_path.stat().st_size >= 20:
                            resolved_text = resolved_path.read_text(encoding="utf-8", errors="replace")
                            resolved_contract = _machine_prediction_contract_report(resolved_text, expected_games=len(_parse_gamepks_csv(gp)))
                            if not resolved_contract.get("ok"):
                                print("[narr_codex_run] Claude-review resolved reasoning fails machine contract -> rc=8", file=sys.stderr)
                                _notify_hard_stop(
                                    a,
                                    date,
                                    scope,
                                    gp,
                                    "claude_review_resolve_machine_contract_failed",
                                    {"contract": resolved_contract, "resolved_path": str(resolved_path), "cross_path": str(cross_path)},
                                )
                                return 8
                            rc_v3, _ = run_cmd_capture([
                                PY, "_narr_model_defense_validate.py",
                                "--date", date,
                                "--reasoning", str(resolved_path),
                                "--gamepks", gp,
                                "--scope", scope,
                                "--write",
                                "--strict",
                            ])
                            if rc_v3 != 0:
                                print("[narr_codex_run] Claude-review resolved reasoning fails validator -> rc=8", file=sys.stderr)
                                _notify_hard_stop(
                                    a,
                                    date,
                                    scope,
                                    gp,
                                    "claude_review_resolve_validation_failed",
                                    {"validator_rc": rc_v3, "resolved_path": str(resolved_path), "cross_path": str(cross_path)},
                                )
                                return 8
                            tmp_before_review = ROOT / f"_codex_gpt_reasoning_{date}__{scope}.pre_claude_review_{stamp}.txt"
                            shutil.copy2(reasoning_path, tmp_before_review)
                            os.replace(resolved_path, reasoning_path)
                            combined_diff_path = _combine_diff_payloads(date, scope, [repair_diff_path, review_diff_path])
                            second_cross_path = ROOT / f"_claude_crosscheck_{date}__{scope}.post_review.json"
                            second_cmd = [
                                PY, "_claude_crosscheck.py",
                                "--league", "mlb",
                                "--date", date,
                                "--scope", scope,
                                "--context-file", str(crosscheck_context_path),
                                "--codex-file", str(reasoning_path),
                                "--out", str(second_cross_path),
                                "--timeout", str(a.claude_timeout),
                                "--model", str(a.claude_model),
                                "--effort", str(a.claude_effort),
                            ]
                            if a.claude_allow_large:
                                second_cmd.append("--allow-large")
                            if combined_diff_path:
                                second_cmd += ["--repair-diff-file", str(combined_diff_path)]
                            rc_c2, _ = run_cmd_capture(second_cmd)
                            if rc_c2 == 0 and second_cross_path.exists():
                                allowed2, why2 = _crosscheck_verdict_allows_send(second_cross_path, combined_diff_path)
                                if allowed2:
                                    print(f"[narr_codex_run] Claude review auto-resolve passed second crosscheck ({why2})")
                                    cross_path = second_cross_path
                                    repair_diff_path = combined_diff_path or repair_diff_path
                                    allowed = True
                                else:
                                    pass_path = ROOT / f"_codex_gpt_reasoning_{date}__{scope}.unresolved_pass.txt"
                                    pass_run_name = f"unresolved_pass_{scope}_{stamp}"
                                    pass_marker_path = ROOT / f"_narr_no_selection_filter_{date}__{scope}.json" if not a.dry else None
                                    print(f"[narr_codex_run] Claude review auto-resolve still blocked ({why2}) -> pass unresolved axes run={pass_run_name}")
                                    pass_cmd = [
                                        PY, "_mlb_narrator_unresolved_pass.py",
                                        "--date", date,
                                        "--reasoning", str(reasoning_path),
                                        "--crosscheck", str(second_cross_path),
                                        "--out", str(pass_path),
                                        "--gamepks", gp,
                                        "--scope", scope,
                                        "--run-name", pass_run_name,
                                    ]
                                    if pass_marker_path:
                                        pass_cmd += ["--marker-out", str(pass_marker_path)]
                                    rc_pass, _ = run_cmd_capture(pass_cmd)
                                    pass_diff_path = ROOT / f"_narr_unresolved_pass_diff_{date}__{scope}.json"
                                    if rc_pass == 0 and pass_path.exists() and pass_path.stat().st_size >= 20:
                                        pass_text = pass_path.read_text(encoding="utf-8", errors="replace")
                                        pass_contract = _machine_prediction_contract_report(pass_text, expected_games=len(_parse_gamepks_csv(gp)))
                                        if not pass_contract.get("ok"):
                                            print("[narr_codex_run] unresolved-pass reasoning fails machine contract -> rc=8", file=sys.stderr)
                                            _notify_hard_stop(
                                                a,
                                                date,
                                                scope,
                                                gp,
                                                "unresolved_pass_machine_contract_failed",
                                                {"contract": pass_contract, "second_cross_path": str(second_cross_path), "pass_path": str(pass_path)},
                                            )
                                            return 8
                                        rc_v4, _ = run_cmd_capture([
                                            PY, "_narr_model_defense_validate.py",
                                            "--date", date,
                                            "--reasoning", str(pass_path),
                                            "--gamepks", gp,
                                            "--scope", scope,
                                            "--write",
                                            "--strict",
                                        ])
                                        if rc_v4 != 0:
                                            print("[narr_codex_run] unresolved-pass reasoning fails validator -> rc=8", file=sys.stderr)
                                            _notify_hard_stop(
                                                a,
                                                date,
                                                scope,
                                                gp,
                                                "unresolved_pass_validation_failed",
                                                {"validator_rc": rc_v4, "second_cross_path": str(second_cross_path), "pass_path": str(pass_path)},
                                            )
                                            return 8
                                        tmp_before_pass = ROOT / f"_codex_gpt_reasoning_{date}__{scope}.pre_unresolved_pass_{stamp}.txt"
                                        shutil.copy2(reasoning_path, tmp_before_pass)
                                        os.replace(pass_path, reasoning_path)
                                        repair_diff_path = _combine_diff_payloads(date, scope, [combined_diff_path, pass_diff_path]) or combined_diff_path or repair_diff_path
                                        if pass_marker_path and pass_marker_path.exists():
                                            no_selection_marker_path = pass_marker_path
                                        cross_path = second_cross_path
                                        allowed = True
                                        print("[narr_codex_run] unresolved second-review items converted to PASS and validated")
                                    else:
                                        print(f"[narr_codex_run] unresolved-pass failed rc={rc_pass} -> rc=8", file=sys.stderr)
                                        _notify_hard_stop(
                                            a,
                                            date,
                                            scope,
                                            gp,
                                            "unresolved_pass_failed",
                                            {"pass_rc": rc_pass, "why": why2, "second_cross_path": str(second_cross_path), "pass_path": str(pass_path)},
                                        )
                                        return 8
                            else:
                                print(f"[narr_codex_run] Claude review second crosscheck unavailable rc={rc_c2} -> rc=8", file=sys.stderr)
                                _notify_hard_stop(
                                    a,
                                    date,
                                    scope,
                                    gp,
                                    "claude_review_second_crosscheck_unavailable",
                                    {"crosscheck_rc": rc_c2, "first_cross_path": str(cross_path), "second_cross_path": str(second_cross_path)},
                                )
                                return 8
                        else:
                            print(f"[narr_codex_run] Claude review auto-resolve failed rc={rc_res} -> rc=8", file=sys.stderr)
                            _notify_hard_stop(
                                a,
                                date,
                                scope,
                                gp,
                                "claude_review_resolve_failed",
                                {"resolve_rc": rc_res, "resolved_path": str(resolved_path), "cross_path": str(cross_path)},
                            )
                            return 8
                    if allowed:
                        pass
                    else:
                        print(f"[narr_codex_run] Claude crosscheck flagged repaired/required reasoning ({why}) -> rc=8", file=sys.stderr)
                        _notify_hard_stop(
                            a,
                            date,
                            scope,
                            gp,
                            "claude_crosscheck_blocked_repaired_reasoning",
                            {"why": why, "repair_diff_path": str(repair_diff_path or ""), "cross_path": str(cross_path)},
                        )
                        return 8

    if (os.environ.get("MLB_SIGNAL_ADJUDICATION_VALIDATE_DISABLE") or "").strip().lower() not in {"1", "true", "yes"}:
        rc_sa, _ = run_cmd_capture([
            PY, "_mlb_signal_adjudication_validate.py",
            "--date", date,
            "--reasoning", str(reasoning_path),
            "--gamepks", gp,
            "--scope", scope,
            "--write",
        ])
        if rc_sa != 0:
            print(f"[narr_codex_run] signal adjudication warn-only validator failed rc={rc_sa}; continuing", file=sys.stderr)

    _snapshot_dual_prediction_artifacts(date, scope, gp, "pre_send_reasoning_ready", send_attempted=False)

    if a.dry:
        print("[narr_codex_run] --dry: 2차 송신 생략. reasoning까지 완료.")
        return 0

    report_path = Path(os.environ.get('MLB_OUTPUT_DIR', 'outputs')) / f'reasoning_ready_{date}__{scope}.json'
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps({'date': date, 'scope': scope, 'reasoning_path': str(reasoning_path), 'delivery': 'local_file_only'}, ensure_ascii=False), encoding='utf-8')
    print(f'Prepared reasoning reference saved: {report_path}. No delivery stage was run.')
    return 0


if __name__ == "__main__":
    sys.exit(main())
