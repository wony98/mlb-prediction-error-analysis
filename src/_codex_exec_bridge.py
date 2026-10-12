# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""_codex_exec_bridge.py — codex exec 안정 호출 래퍼 (historical-note 표준).

stdin 상속 hang / req 누락 / timeout 좀비 / 인증만료 / 빈출력을 표준 정책으로 처리.
모든 자동화(cron / Windows Task Scheduler / 수동)가 codex를 이 한 곳으로만 부른다.

핵심 정책(historical-note):
- req 파일 가드: 없음/0바이트 = fail-fast(rc=2), 재시도 금지(upstream bug).
- stdin 명시: codex exec stdin = req 파일(상속 hang 방지). cat 파이프 금지.
- 출력 원자성: stdout → .tmp → (rc0·non-empty) 검증 후 out으로 os.replace.
- timeout: TimeoutExpired 시 process tree kill(Windows taskkill /T)로 좀비 차단.
- 실패 분류: auth / timeout / transient / empty / rc_error / ok.
  - auth: 재시도 무익 → rc=4(폴백 금지, 호출자가 사용자 메시지 alert).
  - timeout/transient/empty: sleep 후 retries 회까지 재시도.
  - req 누락: 재시도 금지.
- health 로그: postmortem/automation_logs/codex_bridge/health.jsonl.

usage:
  py _codex_exec_bridge.py --req REQ.md --out OUT.txt [--effort high] [--timeout 600] [--retries 1] [--slot s1]
  py _codex_exec_bridge.py --preflight            # codex 인증 health check(CODEX_AUTH_OK), effort low

rc: 0=ok · 2=req누락(fail-fast) · 3=preflight실패 · 4=auth실패(alert,폴백금지) · 5=재시도소진실패
"""
from __future__ import annotations
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent


def _path_is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _resolve_codex_bin() -> str:
    configured = str(os.environ.get("CODEX_EXEC_BIN") or "").strip()
    if configured:
        return configured
    if os.name == "nt":
        appdata = str(os.environ.get("APPDATA") or "").strip()
        if appdata:
            npm_shim = Path(appdata) / "npm" / "codex.cmd"
            if _path_is_file(npm_shim):
                return str(npm_shim)
    return shutil.which("codex") or "codex"


# Prefer the independently updatable npm CLI over the desktop app's bundled
# executable. Windows `.cmd` shims are handled by run_codex_once.
_CODEX = _resolve_codex_bin()
LOG_DIR = ROOT / "postmortem" / "automation_logs" / "codex_bridge"
# 정상 메타(session id / tokens used)를 오판하지 않게 실제 인증실패 문구만(historical-note + 6/22 오판 수정).
_AUTH_PAT = ("not logged in", "please log in", "please login", "log in to", "sign in to",
             "unauthorized", "authentication failed", "login required", "access denied", "re-authenticate", "401 ")
_TRANSIENT_PAT = (
    "network",
    "rate limit",
    "timed out",
    "timeout",
    "503",
    "502",
    "500",
    "connection",
    "temporarily",
    "selected model is at capacity",
    "model is at capacity",
    "try a different model",
)
_CAPACITY_PAT = (
    "selected model is at capacity",
    "model is at capacity",
    "try a different model",
)


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _configured_model() -> str:
    model = str(os.environ.get("CODEX_EXEC_MODEL") or "").strip()
    if model:
        return model
    cfg = Path.home() / ".codex" / "config.toml"
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    # Only use the first top-level model entry. This preserves the normal CLI
    # model when direct-pipe also has to ignore user config for MCP reliability.
    for line in text.splitlines():
        if line.lstrip().startswith("["):
            break
        m = re.match(r'^\s*model\s*=\s*["\']([^"\']+)["\']\s*$', line)
        if m:
            return m.group(1).strip()
    return ""


def _codex_base_args(effort: str, model: str = "", ignore_user_config: bool = False) -> list[str]:
    base = [_CODEX, "exec", "--skip-git-repo-check"]
    if ignore_user_config:
        base.append("--ignore-user-config")
    model = str(model or "").strip()
    if model:
        base += ["--model", model]
    base += ["-c", f"model_reasoning_effort={effort}"]
    return base


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _kill_tree(proc):
    """timeout 시 자식까지 죽인다(다음 cron 슬롯과 충돌 방지)."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            proc.kill()
    except Exception:
        pass


def run_codex_once(req_path, effort, timeout, model="", direct_pipe=False, ignore_user_config=False):
    """codex exec 1회. stdin = req 파일(명시), timeout 시 tree kill. returns (rc, stdout, stderr, duration)."""
    # 응답(마지막 메시지)은 -o 파일로 받는다(stdout/stderr 메타 혼입 방지, 6/22 수정).
    # A process can host several bridge calls in parallel threads. PID-only
    # naming lets those calls overwrite/read each other's final message.
    last_msg = ROOT / f"_codex_last_{os.getpid()}_{uuid.uuid4().hex}.txt"
    try:
        last_msg.unlink()
    except Exception:
        pass
    exec_base = _codex_base_args(effort, model=model, ignore_user_config=ignore_user_config)
    base = exec_base + ["-o", str(last_msg), "-"]
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    t0 = time.monotonic()
    if direct_pipe:
        # historical-note: large prompts sometimes fail through the raw Popen stdin path in
        # desktop automation, while the explicit PowerShell pipe is stable. Keep
        # model/effort explicit so --ignore-user-config cannot silently change
        # the model.
        req_literal = str(Path(req_path)).replace("'", "''")
        out_literal = str(last_msg).replace("'", "''")
        codex_cmd_literal = str(_CODEX).replace("'", "''")
        codex_args = []
        for arg in exec_base:
            codex_args.append("'" + str(arg).replace("'", "''") + "'")
        ps = "\n".join(
            [
                "$ErrorActionPreference = 'Stop'",
                "$env:PYTHONUTF8 = '1'",
                "$env:PYTHONIOENCODING = 'utf-8'",
                "$utf8NoBom = New-Object System.Text.UTF8Encoding($false)",
                "[Console]::InputEncoding = $utf8NoBom",
                "[Console]::OutputEncoding = $utf8NoBom",
                "$OutputEncoding = $utf8NoBom",
                f"$req = '{req_literal}'",
                f"$out = '{out_literal}'",
                "$codexArgs = @(" + ",".join(codex_args[1:]) + ")",
                f"Get-Content -Raw -Encoding UTF8 -LiteralPath $req | & '{codex_cmd_literal}' @codexArgs -o $out -",
            ]
        )
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(ROOT),
            env=env,
        )
        try:
            sout, serr = proc.communicate(timeout=timeout)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            try:
                sout, serr = proc.communicate(timeout=10)
            except Exception:
                sout, serr = b"", b""
            rc = -9
    else:
        # .cmd/.bat shim은 cmd /c 경유여야 실행됨(Windows). taskkill /T로 자식(node)까지 정리.
        cmd = (["cmd", "/c"] + base) if (os.name == "nt" and str(_CODEX).lower().endswith((".cmd", ".bat"))) else base
        with open(req_path, "rb") as fin:
            proc = subprocess.Popen(cmd, stdin=fin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    cwd=str(ROOT), env=env)
            try:
                sout, serr = proc.communicate(timeout=timeout)
                rc = proc.returncode
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                try:
                    sout, serr = proc.communicate(timeout=10)
                except Exception:
                    sout, serr = b"", b""
                rc = -9  # timeout 표식
    dur = round(time.monotonic() - t0, 1)
    out = ""
    try:
        if last_msg.exists():
            out = last_msg.read_text(encoding="utf-8", errors="replace")
            last_msg.unlink()
    except Exception:
        pass
    # stderr에 진단/auth 단서가 있으니 보존(classify가 본다). stdout 메타는 무시.
    err = (serr or b"").decode("utf-8", "replace")
    return rc, out, err, dur


def classify(rc, stdout, stderr):
    s = (stderr or "").lower()
    out_s = (stdout or "").lower()
    has_out = bool((stdout or "").strip())
    if rc == -9:
        return "timeout"
    # Codex can return a capacity notice through the last-message output with rc=0.
    # Never persist that notice as a successful model response.
    if any(p in s or p in out_s for p in _CAPACITY_PAT):
        return "transient"
    # 성공 출력 우선(6/22 false-auth 수정): codex는 reasoning/telemetry를 stderr로 스트리밍하고
    # 내부 재시도한 transient("401" 등)도 stderr에 남긴다. -o 파일(stdout)에 정상 출력이 있고 rc==0이면
    # 실제 성공이므로 stderr에 auth/transient 문구가 섞여도 ok로 본다. 진짜 auth 실패는 -o가 비어
    # has_out=False라 아래 auth 분기로 정확히 잡힌다(rc=4 폴백금지 보존).
    if rc == 0 and has_out:
        return "ok"
    if any(p in s for p in _AUTH_PAT):
        return "auth"
    if rc != 0:
        return "transient" if any(p in s for p in _TRANSIENT_PAT) else "rc_error"
    if not has_out:
        return "empty"
    return "ok"


def _write_health(slot, status, detail):
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        rec = {"ts": _utc(), "slot": slot, "status": status, "detail": detail}
        with open(LOG_DIR / "health.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def preflight(timeout=90):
    """codex 인증 health check. 짧은 req로 CODEX_AUTH_OK 수신. rc 0=ok / 3=fail."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    req = LOG_DIR / "_preflight_req.txt"
    req.write_text("Reply with exactly one line and nothing else: CODEX_AUTH_OK", encoding="utf-8")
    rc, out, err, dur = run_codex_once(req, "low", timeout, model=_configured_model())
    cls = classify(rc, out, err)
    ok = (cls == "ok") and ("CODEX_AUTH_OK" in out)
    _write_health("preflight", "ok" if ok else cls, {"rc": rc, "dur": dur, "err_tail": (err or "")[-200:]})
    print(f"[codex_preflight] {'OK' if ok else 'FAIL'} cls={cls} rc={rc} dur={dur}s")
    return 0 if ok else 3


def bridge(req_path, out_path, effort, timeout, retries, slot, model="", direct_pipe=False, ignore_user_config=False):
    req = Path(req_path)
    out_file = Path(out_path)
    # req 가드 — 없음/0바이트면 fail-fast(재시도 금지, upstream prompt 생성 실패)
    if not req.exists() or req.stat().st_size == 0:
        _write_health(slot, "req_missing", {"req": str(req)})
        print(f"[codex_bridge] req 없음/빈파일: {req} → rc=2 (재시도 금지)", file=sys.stderr)
        return 2
    attempt = 0
    while True:
        attempt += 1
        effective_model = str(model or "").strip() or _configured_model()
        rc, out, err, dur = run_codex_once(
            req,
            effort,
            timeout,
            model=effective_model,
            direct_pipe=direct_pipe,
            ignore_user_config=ignore_user_config,
        )
        cls = classify(rc, out, err)
        mode = "direct-pipe" if direct_pipe else "popen-stdin"
        print(f"[codex_bridge] attempt{attempt} cls={cls} rc={rc} dur={dur}s mode={mode} model={effective_model or '(default)'} effort={effort}")
        if cls == "ok":
            tmp = Path(str(out_path) + ".tmp")  # 출력 원자성: tmp → 검증통과 후 rename
            out_file.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(out, encoding="utf-8")
            os.replace(tmp, out_file)
            _write_health(slot, "ok", {"rc": rc, "dur": dur, "attempt": attempt, "out": str(out_file), "mode": mode, "model": effective_model, "effort": effort})
            return 0
        _write_health(slot, cls, {"rc": rc, "dur": dur, "attempt": attempt, "err_tail": (err or "")[-300:], "mode": mode, "model": effective_model, "effort": effort})
        if cls == "auth":
            print("[codex_bridge] 인증 실패(auth) — 재로그인 필요. 폴백 금지(호출자 alert).", file=sys.stderr)
            return 4
        if cls in ("timeout", "transient", "empty") and attempt <= retries:
            time.sleep(45)  # tree kill 후 진정 + 일시오류 회복 대기
            continue
        print(f"[codex_bridge] 실패 소진 cls={cls} attempt={attempt} → rc=5", file=sys.stderr)
        return 5


def main():
    ap = argparse.ArgumentParser(description="codex exec 안정 호출 래퍼(historical-note 표준).")
    ap.add_argument("--req", help="prompt 파일(stdin으로 전달).")
    ap.add_argument("--out", help="성공 시 codex stdout을 저장할 경로(원자적 rename).")
    ap.add_argument("--effort", default="high", help="model_reasoning_effort (default high).")
    ap.add_argument("--model", default="", help="Codex model to pass explicitly. Defaults to CODEX_EXEC_MODEL or ~/.codex/config.toml model.")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--retries", type=int, default=1, help="timeout/transient/empty 재시도 횟수.")
    ap.add_argument("--slot", default="", help="health 로그용 슬롯 id.")
    ap.add_argument("--preflight", action="store_true", help="codex 인증 health check만 수행.")
    ap.add_argument("--direct-pipe", action="store_true", help="Use PowerShell Get-Content | codex exec pipe path for large prompt reliability.")
    ap.add_argument("--ignore-user-config", action="store_true", help="Pass --ignore-user-config while preserving explicit --model/--effort.")
    a = ap.parse_args()
    if a.preflight:
        return preflight()
    if not a.req or not a.out:
        print("--req and --out required (또는 --preflight)", file=sys.stderr)
        return 2
    direct_pipe = bool(a.direct_pipe or _truthy(os.environ.get("CODEX_EXEC_DIRECT_PIPE")))
    ignore_user_config = bool(a.ignore_user_config or _truthy(os.environ.get("CODEX_EXEC_IGNORE_USER_CONFIG")))
    return bridge(a.req, a.out, a.effort, a.timeout, a.retries, a.slot or "manual", a.model, direct_pipe, ignore_user_config)


if __name__ == "__main__":
    sys.exit(main())
