# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""_mlb_deepread_cli.py — 정독 회고 deepread를 **러너의 CLI**로 수행(무인 가능).

설계(사용자 2026-06-14): 결과대입 파이프라인(run_postgame)에 회고 갱신까지 한 사이클로 포함.
deepread(LLM 정독)는 **누가 돌리냐에 따라 그 CLI**를 쓴다 — Claude가 돌리면 `claude -p`,
Codex가 돌리면 codex CLI. 네레이터의 NARR_REASONING_PROVIDER 스위치와 동일 철학.

- provider 결정: RECAP_PROVIDER 환경변수(claude|codex) > NARR_REASONING_PROVIDER(codex면 codex) > 기본 claude.
- RUBRIC/스키마는 `_make_batch_wf.RUBRIC` 재사용(Workflow판과 단일 소스, 드리프트 0).
- skip-done: 이미 _reason_master에 있는 그날 gamePk 제외(증분, 재실행 낭비 0).
- 출력: _recap_{date8}_out.json (배열) → run_postgame이 `_recap_to_master.py`로 인제스트.

usage:
  py _mlb_deepread_cli.py 2026-06-12                 # 러너 CLI 자동
  py _mlb_deepread_cli.py 2026-06-12 --provider claude
  py _mlb_deepread_cli.py 2026-06-12 --force         # skip-done 무시
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

from _codex_exec_bridge import bridge as _codex_bridge

ROOT = os.path.dirname(os.path.abspath(__file__))
MASTER = os.path.join(ROOT, "_reason_master.json")
MAX_LLM_WORKERS = 2


def _worker_count(requested: int, jobs: int) -> int:
    if jobs <= 0:
        return 0
    return max(1, min(MAX_LLM_WORKERS, int(requested or 1), jobs))

try:
    import _make_batch_wf as _WF
    RUBRIC = _WF.RUBRIC
except Exception:
    RUBRIC = None

SCHEMA_HINT = (
    "\n\n[출력 형식] 오직 하나의 JSON 객체만 출력하라(마크다운 펜스·설명·서론 금지). 키:\n"
    "matchup(string), winner(string), final(string), "
    "pitcher_reads(array of {name, side, role, read} — 등판한 모든 투수 누락 금지), "
    "hitter_reads(array of {name, side, state(락인|평범|부진), note} — 주목 타자 각각), "
    "flow(string), why(string), not_luck(string), luck(string), "
    "verdict(주로 실력/구조|주로 운|반반)."
)


def _provider(cli_arg: str | None) -> str:
    if cli_arg:
        return cli_arg
    rp = (os.environ.get("RECAP_PROVIDER") or "").strip().lower()
    if rp in ("claude", "codex"):
        return rp
    if (os.environ.get("NARR_REASONING_PROVIDER") or "").strip().lower() in ("codex", "gpt", "codex-file", "gpt-file"):
        return "codex"
    return "claude"


def _done_gpks(date: str) -> set[str]:
    try:
        M = json.load(open(MASTER, encoding="utf-8"))
        return {str(e.get("gamePk")) for e in M if e.get("date") == date}
    except Exception:
        return set()


def _pbp_paths(date: str) -> list[str]:
    return sorted(glob.glob(os.path.join(ROOT, "_reason_content", date, "*.md")))


def _gpk_of(path: str) -> str:
    return os.path.basename(path)[:-3]


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    # 펜스 제거 후 첫 { ~ 마지막 } 추출
    t = re.sub(r"```(?:json)?", "", text)
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(t[i:j + 1])
    except Exception:
        return None


def _env() -> dict:
    e = dict(os.environ)
    e["PYTHONUTF8"] = "1"
    e["PYTHONIOENCODING"] = "utf-8"
    # claude.exe(~/.local/bin) 무인 PATH 보강
    e["PATH"] = os.path.join(os.path.expanduser("~"), ".local", "bin") + os.pathsep + e.get("PATH", "")
    return e


def deepread_claude(path: str) -> dict | None:
    """claude -p 헤드리스: RUBRIC으로 PBP를 Read·정독 → JSON 1개."""
    prompt = RUBRIC.replace("__PATH__", path) + SCHEMA_HINT
    for attempt in (1, 2):
        try:
            r = subprocess.run("claude -p --model claude-opus-4-8", input=prompt,
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=900, shell=True, env=_env(), cwd=ROOT)
        except subprocess.TimeoutExpired:
            print(f"  [deepread:claude] {_gpk_of(path)} 시도{attempt} timeout", file=sys.stderr)
            continue
        d = _extract_json(r.stdout or "")
        if d and d.get("pitcher_reads"):
            return d
        print(f"  [deepread:claude] {_gpk_of(path)} 시도{attempt} 파싱실패 rc={r.returncode}", file=sys.stderr)
    return None


def deepread_codex(path: str) -> dict | None:
    """Run one deepread through Codex CLI and parse the final JSON object."""
    prompt = (
        RUBRIC.replace("__PATH__", path)
        + SCHEMA_HINT
        + "\n\nReturn exactly one JSON object. Do not edit files. Do not include markdown fences."
    )
    # Keep recap on the same CLI-verified model as C3. Reading the desktop
    # config here can select a model newer than the installed CLI supports.
    model = (os.environ.get("CODEX_DEEPREAD_MODEL") or "gpt-5.5").strip()
    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", suffix=".prompt.md") as pf:
        pf.write(prompt)
        prompt_path = pf.name
    out_fd, out_path = tempfile.mkstemp(prefix="codex_deepread_", suffix=".json.txt")
    os.close(out_fd)
    try:
        rc = _codex_bridge(
            prompt_path,
            out_path,
            "high",
            1200,
            1,
            f"mlb-deepread-{_gpk_of(path)}",
            model=model,
            direct_pipe=True,
            ignore_user_config=True,
        )
        try:
            out = open(out_path, encoding="utf-8").read()
        except Exception:
            out = ""
        d = _extract_json(out)
        if rc == 0 and d and d.get("pitcher_reads"):
            return d
        print(
            f"  [deepread:codex] {_gpk_of(path)} bridge failed rc={rc} out_tail={out[-160:]!r}",
            file=sys.stderr,
        )
    finally:
        for p in (prompt_path, out_path):
            try:
                os.remove(p)
            except Exception:
                pass
    return None


def run(
    date: str,
    provider: str,
    force: bool,
    workers: int = MAX_LLM_WORKERS,
    requested_gamepks: set[str] | None = None,
) -> list[dict]:
    if RUBRIC is None:
        raise SystemExit("[err] _make_batch_wf.RUBRIC 로드 실패")
    paths = _pbp_paths(date)
    if requested_gamepks:
        paths = [path for path in paths if _gpk_of(path) in requested_gamepks]
    if not paths:
        print(f"[deepread] _reason_content/{date}/*.md 없음 — _reason_bundles.py {date} 먼저")
        return []
    if not force:
        done = _done_gpks(date)
        before = len(paths)
        paths = [p for p in paths if _gpk_of(p) not in done]
        if before - len(paths):
            print(f"[deepread] skip-done {before - len(paths)}경기 제외 → 신규 {len(paths)}경기")
    if not paths:
        print("[deepread] 신규 정독 경기 0 — 생략")
        return []
    fn = {"claude": deepread_claude, "codex": deepread_codex}[provider]
    actual_workers = _worker_count(workers, len(paths))
    print(f"[deepread] provider={provider} · {len(paths)}경기 정독 시작 · workers={actual_workers}")
    results: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=actual_workers) as ex:
        futs = {ex.submit(fn, p): p for p in paths}
        for fut in cf.as_completed(futs):
            p = futs[fut]
            try:
                d = fut.result()
            except NotImplementedError as e:
                raise SystemExit(f"[deepread] {e}")
            if d:
                d.setdefault("gamePk", int(_gpk_of(p)))
                results.append(d)
                print(f"  ✓ {_gpk_of(p)} {str(d.get('matchup'))[:28]} 투수{len(d.get('pitcher_reads', []))}")
            else:
                print(f"  ✗ {_gpk_of(p)} 정독 실패", file=sys.stderr)
    return results


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="정독 deepread (러너 CLI)")
    ap.add_argument("date", help="YYYY-MM-DD")
    ap.add_argument("--provider", choices=["claude", "codex"], default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--out", default=None)
    ap.add_argument("--workers", type=int, default=int(os.environ.get("MLB_RECAP_WORKERS") or 2),
                    help="LLM concurrency; hard-capped at 2 on this host.")
    ap.add_argument("--gamepks", default="", help="Optional comma-separated exact gamePk subset.")
    ap.add_argument("--cycle", action="store_true",
                    help="한 사이클: _reason_bundles → deepread → _recap_to_master(신규>0일 때만). run_postgame용.")
    args = ap.parse_args()

    provider = _provider(args.provider)
    if args.cycle:
        # ① 정독소스 빌드(인제스트된 final만). 비치명.
        subprocess.run([sys.executable, os.path.join(ROOT, "_reason_bundles.py"), args.date],
                       cwd=ROOT, env=_env())

    requested = {value.strip() for value in args.gamepks.split(",") if value.strip()}
    results = run(args.date, provider, args.force, args.workers, requested or None)
    if not results:
        print("[deepread] 신규 정독 0 — 인제스트 생략(증분)")
        return 0
    outp = args.out or os.path.join(ROOT, f"_recap_{args.date.replace('-', '')}_out.json")
    os.makedirs(os.path.dirname(os.path.abspath(outp)), exist_ok=True)
    json.dump(results, open(outp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[deepread] {len(results)}경기 → {os.path.basename(outp)}")

    if args.cycle:
        # ③ master 인제스트 + 폼 재빌드 + priors 신선(자동).
        rc = subprocess.run([sys.executable, os.path.join(ROOT, "_recap_to_master.py"), outp],
                            cwd=ROOT, env=_env()).returncode
        print(f"[deepread] _recap_to_master rc={rc}")
        return rc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
