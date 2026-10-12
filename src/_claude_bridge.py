# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""Stable short-call bridge from Codex/Python to Claude Code.

This is intentionally not a full narrator engine. It is a bounded helper for
Claude cross-checks, rebuttals, and independent short reviews while Codex owns
the main pipeline.

Required successful local shape, proven 2026-06-24:

    claude --safe-mode --model opus --effort high --tools "" --no-session-persistence \
        --output-format json -p "Return exactly CLAUDE_OK and nothing else."

The bridge never prints tokens, never passes tools to Claude, and writes only
the Claude `result` text to --out after validating the outer JSON response.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "postmortem" / "automation_logs" / "claude_bridge"

AUTH_PAT = (
    "invalid authentication credentials",
    "failed to authenticate",
    "authentication failed",
    "unauthorized",
    "login required",
    "not logged in",
)
TRANSIENT_PAT = (
    "rate limit",
    "overloaded",
    "temporarily",
    "timeout",
    "timed out",
    "connection",
    "network",
    "502",
    "503",
    "500",
)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_health(slot: str, status: str, detail: dict) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        rec = {"ts": _utc(), "slot": slot or "manual", "status": status, "detail": detail}
        with (LOG_DIR / "health.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _has_auth_env(env: dict[str, str]) -> bool:
    return bool((env.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip() or (env.get("ANTHROPIC_AUTH_TOKEN") or "").strip())


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    # The working smoke test used both env names. If the user stored only one,
    # mirror it into the other for the child process without printing it.
    cc = (env.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip()
    at = (env.get("ANTHROPIC_AUTH_TOKEN") or "").strip()
    if cc and not at:
        env["ANTHROPIC_AUTH_TOKEN"] = cc
    elif at and not cc:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = at

    # Avoid a stale API key taking precedence over the OAuth path.
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"):
        if name in env:
            env.pop(name, None)
    return env


def _classify(rc: int, raw_stdout: str, raw_stderr: str, parsed: dict | None, result_text: str) -> str:
    blob = " ".join([raw_stdout or "", raw_stderr or "", json.dumps(parsed or {}, ensure_ascii=False)]).lower()
    if rc == -9:
        return "timeout"
    if parsed is None:
        return "json_parse"
    if not (result_text or "").strip():
        return "empty"
    if rc == 0 and not parsed.get("is_error"):
        return "ok"
    api_status = parsed.get("api_error_status") if isinstance(parsed, dict) else None
    auth_like = (
        api_status == 401
        or any(p in blob for p in AUTH_PAT)
        or bool(re.search(r"\b401\b", blob))
    )
    if auth_like:
        return "auth"
    if rc != 0:
        return "transient" if any(p in blob for p in TRANSIENT_PAT) else "rc_error"
    if parsed.get("is_error"):
        return "api_error"
    return "ok"


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            proc.kill()
    except Exception:
        pass


def _run_once(prompt: str, *, model: str, effort: str, timeout: int) -> tuple[int, str, str, dict | None, str, float]:
    exe = shutil.which("claude") or "claude"
    cmd = [
        exe,
        "--safe-mode",
        "--model",
        model,
        "--effort",
        effort,
        "--tools",
        "",
        "--no-session-persistence",
        "--output-format",
        "json",
        "-p",
    ]
    env = _child_env()
    t0 = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(ROOT),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        stdout, stderr = proc.communicate(input=prompt, timeout=timeout)
        rc = int(proc.returncode or 0)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except Exception:
            stdout, stderr = "", ""
        rc = -9
    dur = round(time.monotonic() - t0, 2)
    parsed = None
    result = ""
    try:
        parsed = json.loads(stdout or "{}")
        result = str(parsed.get("result") or "")
    except Exception:
        parsed = None
    return rc, stdout or "", stderr or "", parsed, result, dur


def call_claude(
    prompt: str,
    *,
    model: str = "opus",
    effort: str = "high",
    timeout: int = 180,
    retries: int = 0,
    slot: str = "manual",
    expect_json: bool = False,
) -> tuple[int, str, dict]:
    """Call Claude and return (rc, result_text, meta).

    rc: 0 ok, 4 auth, 5 exhausted/other failure, 6 result JSON validation failed.
    """
    env = _child_env()
    if not _has_auth_env(env):
        meta = {"class": "auth_env_missing", "hint": "Set CLAUDE_CODE_OAUTH_TOKEN and ANTHROPIC_AUTH_TOKEN, then restart Codex."}
        _write_health(slot, "auth_env_missing", meta)
        return 4, "", meta

    attempts = max(1, int(retries) + 1)
    last_meta: dict = {}
    for attempt in range(1, attempts + 1):
        rc, stdout, stderr, parsed, result, dur = _run_once(prompt, model=model, effort=effort, timeout=timeout)
        cls = _classify(rc, stdout, stderr, parsed, result)
        usage = {}
        if isinstance(parsed, dict):
            usage = {
                "total_cost_usd": parsed.get("total_cost_usd"),
                "modelUsage": parsed.get("modelUsage"),
                "duration_ms": parsed.get("duration_ms"),
                "api_error_status": parsed.get("api_error_status"),
            }
        last_meta = {
            "class": cls,
            "rc": rc,
            "attempt": attempt,
            "duration_s": dur,
            "model": model,
            "effort": effort,
            "stderr_tail": (stderr or "")[-300:],
            "usage": usage,
        }
        _write_health(slot, cls, last_meta)
        if cls == "ok":
            if expect_json:
                try:
                    json.loads(result)
                except Exception as e:
                    last_meta["json_error"] = f"{type(e).__name__}: {e}"
                    _write_health(slot, "result_json_invalid", last_meta)
                    return 6, result, last_meta
            return 0, result, last_meta
        if cls == "auth":
            return 4, result, last_meta
        if cls in {"timeout", "transient", "empty"} and attempt < attempts:
            time.sleep(20)
            continue
        break
    return 5, "", last_meta


def _read_prompt(req: Path, max_chars: int, allow_large: bool) -> tuple[int, str]:
    if not req.exists() or req.stat().st_size == 0:
        print(f"[claude_bridge] req missing/empty: {req}", file=sys.stderr)
        return 2, ""
    text = req.read_text(encoding="utf-8", errors="replace")
    if max_chars > 0 and len(text) > max_chars and not allow_large:
        print(
            f"[claude_bridge] req too large: {len(text)} chars > {max_chars}. "
            "Use --allow-large only for deliberate full-context Claude calls.",
            file=sys.stderr,
        )
        return 2, ""
    return 0, text


def bridge(
    req: Path,
    out: Path,
    *,
    model: str,
    effort: str,
    timeout: int,
    retries: int,
    slot: str,
    max_chars: int,
    allow_large: bool,
    expect_json: bool,
) -> int:
    rc, prompt = _read_prompt(req, max_chars, allow_large)
    if rc:
        return rc
    rc, result, meta = call_claude(
        prompt,
        model=model,
        effort=effort,
        timeout=timeout,
        retries=retries,
        slot=slot,
        expect_json=expect_json,
    )
    if rc != 0:
        print(f"[claude_bridge] failed class={meta.get('class')} rc={rc}", file=sys.stderr)
        return rc
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(out) + ".tmp")
    tmp.write_text(result, encoding="utf-8")
    os.replace(tmp, out)
    print(f"[claude_bridge] OK -> {out} ({len(result)} chars)")
    cost = (meta.get("usage") or {}).get("total_cost_usd")
    if cost is not None:
        print(f"[claude_bridge] cost_usd={cost}")
    return 0


def preflight(model: str, effort: str, timeout: int, slot: str) -> int:
    rc, result, meta = call_claude(
        "Return exactly CLAUDE_OK and nothing else.",
        model=model,
        effort=effort,
        timeout=timeout,
        retries=0,
        slot=slot or "preflight",
    )
    ok = rc == 0 and result.strip() == "CLAUDE_OK"
    print(f"[claude_preflight] {'OK' if ok else 'FAIL'} class={meta.get('class')} rc={rc} result={result.strip()[:40]!r}")
    return 0 if ok else (rc or 5)


def main() -> int:
    ap = argparse.ArgumentParser(description="Bounded Claude Code short-call bridge for Codex-owned pipelines.")
    ap.add_argument("--req", help="Prompt file to send to Claude.")
    ap.add_argument("--out", help="File to write Claude result text.")
    ap.add_argument("--model", default="opus", help="Claude model alias. Default: opus for production cross-check.")
    ap.add_argument("--effort", default="high", help="Claude effort level. Default: high.")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--retries", type=int, default=0)
    ap.add_argument("--slot", default="manual", help="Health-log slot id.")
    ap.add_argument("--max-chars", type=int, default=40000, help="Fail if prompt exceeds this many chars unless --allow-large.")
    ap.add_argument("--allow-large", action="store_true", help="Allow prompt larger than --max-chars.")
    ap.add_argument("--expect-json", action="store_true", help="Require Claude result text to be valid JSON.")
    ap.add_argument("--preflight", action="store_true", help="Run a tiny CLAUDE_OK smoke test.")
    args = ap.parse_args()

    if args.preflight:
        return preflight(args.model, args.effort, args.timeout, args.slot)
    if not args.req or not args.out:
        print("--req and --out are required unless --preflight is used", file=sys.stderr)
        return 2
    return bridge(
        Path(args.req),
        Path(args.out),
        model=args.model,
        effort=args.effort,
        timeout=args.timeout,
        retries=args.retries,
        slot=args.slot,
        max_chars=args.max_chars,
        allow_large=args.allow_large,
        expect_json=args.expect_json,
    )


if __name__ == "__main__":
    sys.exit(main())
