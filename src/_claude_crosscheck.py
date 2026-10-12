# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""Claude cross-check wrapper for Codex-owned prediction/recap flows.

Codex remains the main executor. This script asks Claude for a bounded second
opinion on a Codex-produced reasoning file and writes strict JSON. It does not
send Notification, mutate predictions, or force a flip/pass by itself.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from _claude_bridge import call_claude


ROOT = Path(__file__).resolve().parent


def _scope_id(gamepks_csv: str) -> str:
    gp = [x.strip() for x in str(gamepks_csv or "").replace(";", ",").split(",") if x.strip()]
    if not gp:
        return ""
    return "g" + hashlib.sha1(",".join(sorted(gp)).encode("utf-8")).hexdigest()[:8]


def _read(path: Path, max_chars: int, allow_large: bool, label: str) -> str:
    if not path.exists() or path.stat().st_size == 0:
        raise FileNotFoundError(f"{label} missing/empty: {path}")
    text = path.read_text(encoding="utf-8", errors="replace")
    if max_chars > 0 and len(text) > max_chars and not allow_large:
        return (
            text[:max_chars].rstrip()
            + f"\n\n[TRUNCATED {label}: {len(text)} chars > {max_chars}. "
            "Review only what is visible; mark context_limited=true.]"
        )
    return text


def _default_paths(date: str, scope: str) -> tuple[Path, Path, Path]:
    suf = f"__{scope}" if scope else ""
    return (
        ROOT / f"_codex_gpt_prompt_{date}{suf}.md",
        ROOT / f"_codex_gpt_reasoning_{date}{suf}.txt",
        ROOT / f"_claude_crosscheck_{date}{suf}.json",
    )


def build_prompt(
    *,
    league: str,
    date: str,
    scope: str,
    context_text: str,
    codex_text: str,
    context_limited: bool,
    repair_diff_text: str = "",
) -> str:
    repair_section = ""
    if repair_diff_text.strip():
        repair_section = f"""

[REPAIR PREDICTION DIFFS]
The Codex reasoning was repaired after validator failure. These are first-pass to repaired prediction events.

Definitions:
- fill: first pass was missing/pass (`-`) and repaired reasoning made a concrete prediction. This is not a flip.
- flip: first pass had a concrete prediction and repaired reasoning changed it to another side or pass.

For every flip event, you must explicitly approve or reject that change in `flip_acks`.
If any flip is not supported by the supplied pregame context, set verdict to `review_needed` or `blocker`.
If all flips are supported, set verdict to `ok` and include one approve ack per flip.

{repair_diff_text}
"""
    return f"""You are Claude acting as a bounded cross-checker for a Codex-owned baseball prediction pipeline.

Role:
- Codex is the main executor and final merger.
- You do NOT run tools, browse, mutate predictions, or send Notification.
- Your job is to challenge Codex's reasoning only where the supplied pregame context supports it.
- Do not use postgame/result knowledge.

Review goals:
1. Identify unsupported or weak Codex predictions.
2. Identify pregame-knowable signals in the supplied context that Codex appears to underweight or miss.
3. Identify possible future leakage or references that look postgame-only.
4. Separate "market/model thin edge" from real independent pregame signal.
5. Recommend keep/review/pass/flip only as advisory pressure, never as an automatic command.

Return STRICT JSON only, no markdown.

Schema:
{{
  "schema": "claude_crosscheck.v1",
  "league": "{league}",
  "date": "{date}",
  "scope": "{scope}",
  "context_limited": {str(context_limited).lower()},
  "verdict": "ok|review_needed|blocker",
  "summary": "short Korean summary",
  "flip_acks": [
    {{
      "gamePk": "string gamePk",
      "matchup": "Away @ Home",
      "axis": "WIN_LOSS|RUN_TOTAL",
      "from": "HOME|AWAY|UPPER|LOWER|-",
      "to": "HOME|AWAY|UPPER|LOWER|-",
      "decision": "approve|reject",
      "reason": "specific supplied-pregame-context reason for approving/rejecting the repaired flip"
    }}
  ],
  "items": [
    {{
      "gamePk": null,
      "matchup": "Away @ Home",
      "market": "WIN_LOSS|RUN_TOTAL|BOTH",
      "severity": "note|review|blocker",
      "codex_prediction": "HOME|AWAY|UPPER|LOWER|-|unknown",
      "issue": "what may be wrong or missing",
      "pregame_signal": "specific pregame-knowable signal from supplied context",
      "recommendation": "keep|review|pass|flip|no_action",
      "confidence": "low|medium|high"
    }}
  ]
}}

Rules:
- If context is truncated/limited, do not invent missing evidence. Set context_limited true and lower confidence.
- If you cannot support a concern with supplied pregame context, do not add an item.
- Prefer fewer high-signal items over many vague notes.
- Keep reasons compact.
- If [REPAIR PREDICTION DIFFS] contains any `kind=flip` event, every flip must have a `flip_acks` entry. Generic `verdict=ok` is not enough.
- Do not treat `kind=fill` as a flip. Fill still needs ordinary repaired-run crosscheck, but not a flip rejection.

[SUPPLIED PREGAME CONTEXT / PROMPT]
{context_text}

[CODEX REASONING TO CROSS-CHECK]
{codex_text}
{repair_section}
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="Ask Claude for a bounded JSON cross-check of Codex reasoning.")
    ap.add_argument("--league", default="mlb", choices=["mlb"])
    ap.add_argument("--date", required=True)
    ap.add_argument("--scope", default="")
    ap.add_argument("--gamepks", default="", help="If scope is omitted, compute scope from comma-separated gamePks.")
    ap.add_argument("--context-file", default="", help="Pregame prompt/context file. Defaults to MLB scoped Codex prompt.")
    ap.add_argument("--codex-file", default="", help="Codex reasoning file. Defaults to MLB scoped Codex reasoning.")
    ap.add_argument("--out", default="", help="Output JSON file. Defaults to _claude_crosscheck_DATE__SCOPE.json.")
    ap.add_argument("--repair-diff-file", default="", help="Optional JSON file with first-pass to repaired prediction fill/flip events.")
    ap.add_argument("--model", default="opus")
    ap.add_argument("--effort", default="high")
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--retries", type=int, default=0)
    ap.add_argument("--max-context-chars", type=int, default=70000)
    ap.add_argument("--max-codex-chars", type=int, default=30000)
    ap.add_argument("--allow-large", action="store_true")
    args = ap.parse_args()

    scope = args.scope.strip() or _scope_id(args.gamepks)
    default_context, default_codex, default_out = _default_paths(args.date, scope)
    context_path = Path(args.context_file) if args.context_file else default_context
    codex_path = Path(args.codex_file) if args.codex_file else default_codex
    out_path = Path(args.out) if args.out else default_out

    try:
        context_text = _read(context_path, args.max_context_chars, args.allow_large, "context")
        codex_text = _read(codex_path, args.max_codex_chars, args.allow_large, "codex")
        repair_diff_text = ""
        repair_diff_path = Path(args.repair_diff_file) if args.repair_diff_file else None
        if repair_diff_path:
            repair_diff_text = _read(repair_diff_path, 30000, True, "repair_diff")
    except Exception as e:
        print(f"[claude_crosscheck] input error: {e}", file=sys.stderr)
        return 2

    context_limited = ("[TRUNCATED context:" in context_text) or ("[TRUNCATED codex:" in codex_text)
    prompt = build_prompt(
        league=args.league,
        date=args.date,
        scope=scope,
        context_text=context_text,
        codex_text=codex_text,
        context_limited=context_limited,
        repair_diff_text=repair_diff_text,
    )
    rc, result, meta = call_claude(
        prompt,
        model=args.model,
        effort=args.effort,
        timeout=args.timeout,
        retries=args.retries,
        slot=f"crosscheck:{args.league}:{args.date}:{scope or 'full'}",
        expect_json=True,
    )
    if rc != 0:
        print(f"[claude_crosscheck] Claude failed class={meta.get('class')} rc={rc}", file=sys.stderr)
        return rc

    try:
        payload = json.loads(result)
    except Exception as e:
        print(f"[claude_crosscheck] invalid JSON after bridge validation: {e}", file=sys.stderr)
        return 6

    payload.setdefault("schema", "claude_crosscheck.v1")
    payload.setdefault("league", args.league)
    payload.setdefault("date", args.date)
    payload.setdefault("scope", scope)
    payload["created_at_utc"] = datetime.now(timezone.utc).isoformat()
    payload["source"] = {
        "context_file": str(context_path),
        "codex_file": str(codex_path),
        "repair_diff_file": str(repair_diff_path) if repair_diff_path else "",
        "model": args.model,
        "bridge_class": meta.get("class"),
        "usage": meta.get("usage"),
        "effort": args.effort,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(out_path) + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, out_path)
    print(f"[claude_crosscheck] OK -> {out_path}")
    print(f"[claude_crosscheck] verdict={payload.get('verdict')} items={len(payload.get('items') or [])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
