# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""_mlb_synth_cli.py — 합성②(사전/사후 분리)를 러너 CLI로 무인 수행 (deepread CLI 미러, historical-note).

목적: 결과대입 회고 사이클에서 정독(deepread) 다음에 "사전에 알 수 있던 정보 vs 아니던 정보"를 갈라
_synth_master에 누적 → 그 predictable이 _narr_synth_lessons로 ledger화돼 다음 분석 priors에 통째로 붙는다.

- 입력: _synth_input/{gpk}.md (recap_to_master가 생성. 없으면 _synth_input.py로 즉석 생성).
- 프롬프트/스키마: _make_synth_wf.PROMPT 재사용(Workflow판과 단일 소스).
- provider: RECAP_PROVIDER>NARR_REASONING_PROVIDER(codex)>claude. claude=claude -p, codex=codex CLI.
- skip-done: _synth_done.json. 병합: _synth_master.json(gamePk 교체) + _synth_done 갱신(synth 전용 ingest — gpt_batch_ingest는 'gpk' 키라 미스매치).

usage:
  py _mlb_synth_cli.py 2026-06-13 --cycle    # synth_input 보강 → 사전/사후 분리 → _synth_master 병합
  py _mlb_synth_cli.py 2026-06-13 --force
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import shutil
import subprocess
import sys
import tempfile

from _codex_exec_bridge import bridge as _codex_bridge

ROOT = os.path.dirname(os.path.abspath(__file__))
MASTER = os.path.join(ROOT, "_reason_master.json")
SMASTER = os.path.join(ROOT, "_synth_master.json")
SDONE = os.path.join(ROOT, "_synth_done.json")
SINPUT = os.path.join(ROOT, "_synth_input")

# deepread CLI 헬퍼 재사용(env·json추출·provider·codex 러너)
try:
    from _mlb_deepread_cli import (
        MAX_LLM_WORKERS,
        _env,
        _extract_json,
        _provider,
        _worker_count,
        deepread_codex as _codex_run,
    )
except Exception:
    MAX_LLM_WORKERS = 2
    _env = _extract_json = _provider = _worker_count = _codex_run = None

try:
    from _make_synth_wf import PROMPT as SYNTH_PROMPT
except Exception:
    SYNTH_PROMPT = None

SCHEMA_HINT = (
    "\n\n[출력 형식] 오직 하나의 JSON 객체만(마크다운 펜스·설명 금지). 키:\n"
    "matchup, winner, verdict_orig, "
    "predictable(array of {factor, evidence}), not_predictable(array of {factor, why}), "
    "luck_recheck(array of {element, verdict(진짜 운|사실은 신호있음|애매), reason}), prediction_takeaway(string)."
)


def _date_gpks(date):
    try:
        M = json.load(open(MASTER, encoding="utf-8"))
        return [str(e["gamePk"]) for e in M if e.get("date") == date and e.get("gamePk") is not None]
    except Exception:
        return []


def _done():
    try:
        return {str(x) for x in json.load(open(SDONE, encoding="utf-8"))}
    except Exception:
        return set()


def _ensure_input(gpks):
    missing = [g for g in gpks if not os.path.exists(os.path.join(SINPUT, f"{g}.md"))]
    if missing:
        subprocess.run([sys.executable, os.path.join(ROOT, "_synth_input.py"), *missing], cwd=ROOT, env=_env())


def synth_claude(path):
    prompt = SYNTH_PROMPT.replace("__PATH__", path) + SCHEMA_HINT
    model = (os.environ.get("CLAUDE_SYNTH_MODEL") or "sonnet").strip()
    cmd = ["claude", "--safe-mode", "--model", model, "-p"]
    for attempt in (1, 2):
        try:
            r = subprocess.run(cmd, input=prompt,
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=900, env=_env(), cwd=ROOT)
        except subprocess.TimeoutExpired:
            print(f"  [synth:claude] timeout 시도{attempt}", file=sys.stderr)
            continue
        d = _extract_json(r.stdout or "")
        if d and "predictable" in d and "not_predictable" in d:
            return d
        print(f"  [synth:claude] 파싱실패 시도{attempt} rc={r.returncode}", file=sys.stderr)
    return None


def synth_codex(path):
    """Run one synth separation through Codex CLI and parse the final JSON object."""
    prompt = (
        SYNTH_PROMPT.replace("__PATH__", path)
        + SCHEMA_HINT
        + "\n\nReturn exactly one JSON object. Do not edit files. Do not include markdown fences."
    )
    model = (
        os.environ.get("CODEX_SYNTH_MODEL")
        or os.environ.get("CODEX_DEEPREAD_MODEL")
        or "gpt-5.5"
    ).strip()
    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", suffix=".synth.prompt.md") as pf:
        pf.write(prompt)
        prompt_path = pf.name
    out_fd, out_path = tempfile.mkstemp(prefix="codex_synth_", suffix=".json.txt")
    os.close(out_fd)
    try:
        rc = _codex_bridge(
            prompt_path,
            out_path,
            "high",
            900,
            1,
            f"mlb-synth-{os.path.basename(path)}",
            model=model,
            direct_pipe=True,
            ignore_user_config=True,
        )
        try:
            out = open(out_path, encoding="utf-8").read()
        except Exception:
            out = ""
        d = _extract_json(out)
        if rc == 0 and d and "predictable" in d and "not_predictable" in d:
            return d
        print(
            f"  [synth:codex] {os.path.basename(path)} bridge failed rc={rc} out_tail={out[-160:]!r}",
            file=sys.stderr,
        )
        if (os.environ.get("SYNTH_CODEX_ALLOW_CLAUDE_FALLBACK") or "1").strip().lower() not in ("0", "false", "no"):
            if rc == 4:
                print(f"  [synth:codex] {os.path.basename(path)} codex unavailable -> claude safe-mode fallback", file=sys.stderr)
                return synth_claude(path)
    finally:
        for p in (prompt_path, out_path):
            try:
                os.remove(p)
            except Exception:
                pass
    return None


def merge_master(results):
    try:
        master = {str(r["gamePk"]): r for r in json.load(open(SMASTER, encoding="utf-8"))}
    except Exception:
        master = {}
    done = _done()
    for d in results:
        gk = str(d["gamePk"])
        d["synth_src"] = "mlb_synth_cli"
        master[gk] = d
        done.add(gk)
    json.dump(list(master.values()), open(SMASTER, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(sorted(done), open(SDONE, "w", encoding="utf-8"), ensure_ascii=False)
    return len(master)


def run(date, provider, force, cycle, workers=MAX_LLM_WORKERS, requested_gamepks=None):
    if SYNTH_PROMPT is None or _env is None:
        raise SystemExit("[err] _make_synth_wf.PROMPT / _mlb_deepread_cli 헬퍼 로드 실패")
    gpks = _date_gpks(date)
    if requested_gamepks:
        gpks = [g for g in gpks if g in requested_gamepks]
    if not gpks:
        print(f"[synth] _reason_master에 {date} 경기 없음(정독 먼저)")
        return []
    if cycle:
        _ensure_input(gpks)
    if not force:
        done = _done()
        gpks = [g for g in gpks if g not in done]
    gpks = [g for g in gpks if os.path.exists(os.path.join(SINPUT, f"{g}.md"))]
    if not gpks:
        print("[synth] 신규 분리 경기 0 — 생략(증분)")
        return []
    fn = {"claude": synth_claude, "codex": synth_codex}[provider]
    actual_workers = _worker_count(workers, len(gpks))
    print(f"[synth] provider={provider} · {len(gpks)}경기 사전/사후 분리 시작 · workers={actual_workers}")
    results = []
    with cf.ThreadPoolExecutor(max_workers=actual_workers) as ex:
        futs = {ex.submit(fn, os.path.join(SINPUT, f"{g}.md")): g for g in gpks}
        for fut in cf.as_completed(futs):
            g = futs[fut]
            try:
                d = fut.result()
            except NotImplementedError as e:
                raise SystemExit(f"[synth] {e}")
            if d:
                d.setdefault("gamePk", int(g))
                results.append(d)
                np_, nn = len(d.get("predictable", [])), len(d.get("not_predictable", []))
                print(f"  ✓ {g} {str(d.get('matchup'))[:26]} 사전{np_} 사후{nn}")
            else:
                print(f"  ✗ {g} 분리 실패", file=sys.stderr)
    return results


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="합성② 사전/사후 분리 (러너 CLI)")
    ap.add_argument("date")
    ap.add_argument("--provider", choices=["claude", "codex"], default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--cycle", action="store_true", help="synth_input 보강→분리→_synth_master 병합 (run_postgame용)")
    ap.add_argument("--out", default=None, help="결과를 이 파일로만 저장(dual-recap provider별 분리, historical-note).")
    ap.add_argument("--no-merge", action="store_true", help="_synth_master에 병합하지 않음(--out과 함께 dual-recap용 — 수렴본만 별도 merge).")
    ap.add_argument("--workers", type=int, default=int(os.environ.get("MLB_RECAP_WORKERS") or 2),
                    help="LLM concurrency; hard-capped at 2 on this host.")
    ap.add_argument("--gamepks", default="", help="Optional comma-separated exact gamePk subset.")
    args = ap.parse_args()

    provider = _provider(args.provider) if _provider else "claude"
    requested = {value.strip() for value in args.gamepks.split(",") if value.strip()}
    results = run(args.date, provider, args.force, args.cycle, args.workers, requested or None)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        json.dump(results, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"[synth] provider={provider} 결과 {len(results)}경기 → {args.out}")
    if not results:
        return 0
    if args.no_merge:
        print("[synth] --no-merge: _synth_master 병합 생략(provider별 결과는 수렴 후 1벌만 merge).")
        return 0
    total = merge_master(results)
    print(f"[synth] _synth_master 병합 {len(results)}경기 (총 {total}) + _synth_done 갱신")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
