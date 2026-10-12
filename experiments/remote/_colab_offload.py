# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
from __future__ import annotations

import argparse
import csv
from collections import Counter
import hashlib
import json
import math
import os
import shlex
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = ROOT / "postmortem" / "automation_logs" / "colab_offload"

DEFAULT_PROBE_FILES = [
    "AGENTS.md",
    "codex_pipeline.py",
    "run_mlb_predict.py",
    "run_mlb_v3.py",
    "run_postgame.py",
    "_narr_codex_run.py",
    "_narr_reasoning.py",
    "_claude_bridge.py",
    "_claude_crosscheck.py",
    "_codex_exec_bridge.py",
]

CORE_DOC_FILES = [
    "AGENTS.md",
    "postmortem/fullread_v4/MLB_ANALYSIS_PIPELINE_TRIGGER_2026-06-12.md",
]

ROOT_DATA_FILES = [
    "backtest_master_mlb.csv",
    "day_score_log.csv",
    "mlb_clean_win_loss_features.json",
    "mlb_clean_run_total_features.json",
    "mlb_clean_win_loss_model.pkl",
    "mlb_clean_run_total_model.pkl",
    "mlb_simple_win_loss_model.pkl",
    "mlb_2026_sc_recent.parquet",
    "mlb_pitcher_season_prior.csv",
    "mlb_batter_season_prior.csv",
    "mlb_statcast_daily.csv",
    "mlb_statcast_rolling.csv",
    "mlb_weather_backfill.csv",
    "mlb_lineup_sums_backfill.csv",
    "_reason_master.json",
    "_synth_master.json",
    "_narr_results_master.csv",
    "_narr_synth_lessons.jsonl",
    "_narr_synth_takeaways.jsonl",
    "_narr_trajectory_ledger.json",
    "_box2026_game.parquet",
    "_box2026_bat.parquet",
    "_narr2026.parquet",
    "_sp_underlying_gamelog.parquet",
    "_narr_bullpen_stints.parquet",
    "_form_label_hitters.json",
    "_sp_form.json",
    "_bp_form.json",
]

MLB_CACHE_RUNTIME_FILES = [
    "mlb_cache/teams.parquet",
    "mlb_cache/vsteam_full.parquet",
    "mlb_cache/pitcher_pull.parquet",
    "mlb_cache/statcast_pitcher.parquet",
    "mlb_cache/lineups_history.parquet",
    "mlb_cache/batter_handedness.parquet",
    "mlb_cache/pitcher_handedness.parquet",
    "mlb_cache/event_id_gamepk_map.parquet",
    "mlb_cache/oddsportal_inventory.parquet",
    "mlb_cache/oddsportal_win_loss_opens.parquet",
    "mlb_cache/oddsportal_ou_opens.parquet",
    "savant_xera/corrected/statcast-park-factors__2025.csv",
    "savant_xera/pybaseball_lb/statcast_pitcher_expected_stats__2025.csv",
]

PREGAME_DATE_INPUTS = [
    "_v3_games_{date}.csv",
    "_narr_context_{date}.txt",
    "_narr_priors_{date}.txt",
    "_narr_news_{date}.json",
    "_narr_salience_digest_{date}.txt",
    "postmortem/fullread_v4/model_defense_context/mlb_narr_model_defense_context_{date}.txt",
    "postmortem/fullread_v4/model_defense_context/mlb_narr_model_defense_context_{date}.json",
    "postmortem/fullread_v4/model_defense_context/mlb_narr_model_defense_validate_{date}.json",
]

PREGAME_EXPECTED_OUTPUTS = [
    "_v3_games_{date}.csv",
    "_codex_gpt_prompt_{date}.md",
    "_codex_gpt_reasoning_{date}.txt",
    "_narr_reasoning_{date}.txt",
    "_narr_predictions_{date}.json",
    "_narr_send_manifest_{date}.json",
    "_narr_high_conf_{date}.json",
    "postmortem/fullread_v4/model_defense_context/mlb_narr_model_defense_context_{date}.txt",
    "postmortem/fullread_v4/model_defense_context/mlb_narr_model_defense_context_{date}.json",
]

PREGAME_GUARD_CACHE_FILES = [
    "postmortem/fullread_v4/pregame_signal_guards/mlb_retrospective_signal_registry.json",
    "postmortem/fullread_v4/pregame_signal_guards/mlb_retrospective_signal_registry.md",
    "postmortem/fullread_v4/mlb_fullread_v4_bullpen_pregame_features.csv",
    "postmortem/fullread_v4/mlb_fullread_v4_player_contact_pregame_features.csv",
    "postmortem/fullread_v4/mlb_fullread_v4_player_contact_ledger_report.json",
    "postmortem/fullread_v4/mlb_fullread_v4_handedness_matchup_pregame_features.csv",
    "postmortem/fullread_v4/mlb_fullread_v4_handedness_matchup_ledger_report.json",
    "postmortem/fullread_v4/mlb_fullread_v4_run_context_pregame_features.csv",
    "postmortem/fullread_v4/mlb_fullread_v4_bullpen_appearance_projection.csv",
    "postmortem/fullread_v4/mlb_fullread_v4_bullpen_appearance_team_projection.csv",
    "postmortem/fullread_v4/mlb_fullread_v4_bullpen_projection_backtest_{year}_projection.csv",
    "postmortem/fullread_v4/anti_burial_walkforward/mlb_anti_burial_calibration_contract.json",
    "postmortem/fullread_v4/model_reason_snapshots/mlb_model_reason_snapshot_{date}.jsonl",
    "postmortem/fullread_v4/model_reason_snapshots/mlb_model_reason_snapshot_{date}_summary.json",
]

POSTGAME_GUARD_CACHE_FILES = [
    *PREGAME_GUARD_CACHE_FILES,
    "postmortem/fullread_v4/mlb_fullread_v4_manifest.csv",
    "postmortem/fullread_v4/mlb_fullread_v4_bullpen_stints.csv",
    "postmortem/fullread_v4/mlb_fullread_v4_bullpen_missing_report.csv",
    "postmortem/fullread_v4/mlb_fullread_v4_bullpen_projection_validation_team.csv",
]

POSTGAME_EXPECTED_OUTPUTS = [
    "day_score_log.csv",
    "_v3_games_{date}.csv",
    "_narr_predictions_{date}.json",
    "_narr_results_master.csv",
    "_narr_recommend_perf.csv",
    "_narr_recommend_calib.json",
    "_narr_miss_log.csv",
    "_narr_synth_lessons.jsonl",
    "_narr_synth_takeaways.jsonl",
    "postmortem/partial_results/{ymd}_mlb_v3_result_summary.json",
    "postmortem/fullread_v4/pregame_signal_guards/mlb_postgame_guard_refresh_report.json",
    "postmortem/fullread_v4/pregame_signal_guards/mlb_postgame_guard_refresh_report.md",
    "postmortem/raw_pbp_event_ledger/mlb_raw_pbp_event_ledger.jsonl",
    "postmortem/raw_pbp_fullflow/regular_season_2026_directional/regular_season_directional_retrospective_2026.jsonl",
    "postmortem/raw_pbp_fullflow/run_total_focused_retrospective/mlb_run_total_focused_retrospective_2026.jsonl",
]

POSTGAME_DATE_INPUTS = [
    "_narr_predictions_{date}.json",
    "_narr_reasoning_{date}.txt",
    "_narr_recommend_{date}.json",
    "postmortem/fullread_v4/model_reason_snapshots/mlb_model_reason_snapshot_{date}.jsonl",
    "postmortem/fullread_v4/model_reason_snapshots/mlb_model_reason_snapshot_{date}_summary.json",
    "postmortem/fullread_v4/pregame_signal_guards/mlb_narrator_pregame_signal_guard_{date}.csv",
    "postmortem/fullread_v4/pregame_signal_guards/mlb_narrator_pregame_signal_guard_{date}.json",
]

MLB_LOCAL_DATA_DRIVE = Path(os.environ.get("MLB_LOCAL_DATA_DRIVE", r"<DATA_ROOT>"))
MLB_REMOTE_DATA_DRIVE = "/content/mlb_data"
MLB_DATA_PAYLOAD_ROOT = "mlb_data"
INPUT_SNAPSHOT_PAYLOAD_ROOT = ".codex_payload/input_snapshots"

GDRIVE_MLB_DEPENDENCIES = [
    ("park_factors_2025.csv", True),
    ("master_5seasons.csv", True),
    ("종결v2_승패_LR.pkl", True),
    ("종결v2_승패_LGB.pkl", True),
    ("종결v2_총득점_LR.pkl", True),
    ("종결v2_총득점_LGB.pkl", True),
    ("{year}/predictions_v2_{date}.csv", False),
    ("{year}/predictions_v2_{year}_all.csv", True),
]

SANITIZED_NARRATOR_SKILL_FILES = [
    (
        "src/narr_context.py",
        ".codex_payload/skills/mlb-narrative-prediction-reasoning/scripts/narr_context.py",
        True,
    ),
    (
        "prompts/_narr_rules.md",
        ".codex_payload/skills/mlb-narrative-prediction-reasoning/references/_narr_rules.md",
        True,
    ),
]

SECRET_PATTERNS = (
    ".env",
    ".git/",
    ".claude/",
    "application_default_credentials",
    "credentials.json",
    "token",
    "secret",
    "oauth",
    "key",
)


class ColabOffloadError(RuntimeError):
    pass


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def win_path_to_wsl(path: Path) -> str:
    resolved = path.resolve()
    drive = resolved.drive.rstrip(":").lower()
    if not drive:
        raise ColabOffloadError(f"Cannot convert non-drive path to WSL path: {resolved}")
    rest = resolved.relative_to(resolved.anchor).as_posix()
    return f"/mnt/{drive}/{rest}"


def ensure_relative_safe(rel: str) -> Path:
    rel_path = Path(rel)
    if rel_path.is_absolute() or ".." in rel_path.parts:
        raise ColabOffloadError(f"Unsafe payload path: {rel}")
    normalized = rel.replace("\\", "/").lower()
    for pattern in SECRET_PATTERNS:
        if pattern in normalized:
            raise ColabOffloadError(f"Refusing to include secret-like path: {rel}")
    abs_path = (ROOT / rel_path).resolve()
    try:
        abs_path.relative_to(ROOT)
    except ValueError as exc:
        raise ColabOffloadError(f"Payload path escapes repo: {rel}") from exc
    return abs_path


def normalize_rel(rel: str) -> str:
    return rel.replace("\\", "/").lstrip("/")


def is_secret_like(rel: str) -> bool:
    normalized = normalize_rel(rel).lower()
    return any(pattern in normalized for pattern in SECRET_PATTERNS)


def normalize_date(value: str) -> str:
    s = value.strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return s
    raise ColabOffloadError(f"Expected date as YYYY-MM-DD or YYYYMMDD, got: {value}")


def ymd_no_dash(date: str) -> str:
    return date.replace("-", "")


def format_template(template: str, date: str) -> str:
    return template.format(date=date, ymd=ymd_no_dash(date), year=date[:4])


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def local_file_row(
    rel: str,
    *,
    role: str,
    required: bool,
    max_large_bytes: int,
) -> dict:
    arcname = normalize_rel(rel)
    row = {
        "path": arcname,
        "role": role,
        "required": required,
        "packaging": "repo_relative",
    }
    if is_secret_like(arcname):
        row.update(
            {
                "status": "blocked_secret_policy",
                "reason": "Path matches Colab payload secret/policy block list.",
            }
        )
        return row
    try:
        abs_path = ensure_relative_safe(arcname)
    except ColabOffloadError as exc:
        row.update({"status": "unsafe_path", "reason": str(exc)})
        return row
    if not abs_path.exists():
        row.update({"status": "missing"})
        return row
    if not abs_path.is_file():
        row.update({"status": "not_file"})
        return row
    size = abs_path.stat().st_size
    row.update(
        {
            "status": "included",
            "bytes": size,
            "sha256": file_sha256(abs_path),
            "large": size > max_large_bytes,
        }
    )
    if size > max_large_bytes:
        row["large_reason"] = f"File exceeds large-file threshold ({max_large_bytes} bytes)."
    return row


def external_file_row(path_template: str, *, date: str, role: str, required: bool) -> dict:
    path_str = format_template(path_template, date)
    row = {
        "path": path_str,
        "role": role,
        "required": required,
        "status": "external_not_packaged",
        "packaging": "external_windows_path",
        "reason": "File is outside <WORKSPACE> and is not uploaded by Phase 2 manifest.",
    }
    try:
        path = Path(path_str)
        if path.exists() and path.is_file():
            row["external_exists_locally"] = True
            row["bytes"] = path.stat().st_size
        else:
            row["external_exists_locally"] = False
    except OSError:
        row["external_exists_locally"] = None
    return row


def payload_copy_row(source: Path, payload_path: str, *, role: str, required: bool) -> dict:
    arcname = normalize_rel(payload_path)
    row = {
        "path": arcname,
        "payload_path": arcname,
        "source_path": str(source),
        "role": role,
        "required": required,
        "packaging": "payload_copy",
    }
    if is_secret_like(arcname):
        row.update(
            {
                "status": "blocked_secret_policy",
                "reason": "Payload destination matches secret/policy block list.",
            }
        )
        return row
    try:
        if source.exists() and source.is_file():
            row.update(
                {
                    "status": "included",
                    "bytes": source.stat().st_size,
                    "sha256": file_sha256(source),
                }
            )
        else:
            row.update({"status": "missing"})
    except OSError as exc:
        row.update({"status": "source_error", "reason": str(exc)})
    return row


def gdrive_payload_row(rel_template: str, *, date: str, required: bool) -> dict:
    rel = normalize_rel(format_template(rel_template, date))
    source = MLB_LOCAL_DATA_DRIVE / Path(rel.replace("/", os.sep))
    return payload_copy_row(
        source,
        f"{MLB_DATA_PAYLOAD_ROOT}/{rel}",
        role="external_payload_copy/run_mlb_predict_gdrive",
        required=required,
    )


def input_snapshot_payload_row(snapshot_path: Path, *, required: bool = True) -> dict:
    source = snapshot_path.resolve()
    return payload_copy_row(
        source,
        f"{INPUT_SNAPSHOT_PAYLOAD_ROOT}/{source.name}",
        role="frozen_input_snapshot/pregame",
        required=required,
    )


def sanitized_skill_payload_rows() -> list[dict]:
    rows = []
    for source_rel, payload_rel, required in SANITIZED_NARRATOR_SKILL_FILES:
        rows.append(
            payload_copy_row(
                ROOT / Path(source_rel),
                payload_rel,
                role="sanitized_payload_copy/narrator_skill",
                required=required,
            )
        )
    return rows


def add_unique(rows: list[dict], row: dict, seen: set[tuple[str, str]]) -> None:
    key = (row.get("path", ""), row.get("role", ""))
    if key in seen:
        return
    seen.add(key)
    rows.append(row)


def root_python_files() -> list[str]:
    out = []
    for path in sorted(ROOT.glob("*.py")):
        name = path.name
        if name.startswith("_tmp_"):
            continue
        out.append(name)
    return out


def existing_glob_files(pattern: str, *, limit: int = 5000) -> tuple[list[str], bool]:
    files = [p for p in sorted(ROOT.glob(pattern)) if p.is_file()]
    truncated = len(files) > limit
    return [str(p.relative_to(ROOT)).replace("\\", "/") for p in files[:limit]], truncated


def build_profile_manifest(profile: str, date: str, *, max_large_mb: int) -> dict:
    date = normalize_date(date)
    max_large_bytes = max_large_mb * 1024 * 1024
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add_local(rel: str, role: str, required: bool) -> None:
        add_unique(
            rows,
            local_file_row(rel, role=role, required=required, max_large_bytes=max_large_bytes),
            seen,
        )

    def add_external(template: str, role: str, required: bool) -> None:
        add_unique(rows, external_file_row(template, date=date, role=role, required=required), seen)

    for rel in root_python_files():
        add_local(rel, "code/root_py", True)
    for rel in CORE_DOC_FILES:
        add_local(rel, "docs/operating_memory", True)
    for rel in ROOT_DATA_FILES:
        add_local(rel, "data/root_runtime", False)
    for rel in MLB_CACHE_RUNTIME_FILES:
        add_local(rel, "data/mlb_cache_runtime", True)

    for row in sanitized_skill_payload_rows():
        add_unique(rows, row, seen)

    if profile == "mlb-pregame":
        for rel in PREGAME_DATE_INPUTS:
            add_local(format_template(rel, date), "date_input/pregame", False)
        for rel in PREGAME_GUARD_CACHE_FILES:
            add_local(format_template(rel, date), "cache/pregame_guard", True)
        for rel in PREGAME_EXPECTED_OUTPUTS:
            add_local(format_template(rel, date), "expected_output/pregame_compare", False)
        for template, required in GDRIVE_MLB_DEPENDENCIES:
            add_unique(rows, gdrive_payload_row(template, date=date, required=required), seen)
        for pattern in [
            f"_codex_gpt_prompt_{date}__*.md",
            f"_codex_gpt_reasoning_{date}__*.txt",
            f"postmortem/fullread_v4/t038_pregame_signal_cards/*{date}*",
            f"postmortem/fullread_v4/pregame_signal_guards/*{date}*",
            f"postmortem/fullread_v4/model_defense_context/*{date}*",
            f"postmortem/fullread_v4/high_conf_recommendation_research/*{date}*",
        ]:
            files, truncated = existing_glob_files(pattern)
            for rel in files:
                add_local(rel, "date_context/pregame_glob", False)
            if truncated:
                rows.append(
                    {
                        "path": pattern,
                        "role": "date_context/pregame_glob",
                        "required": False,
                        "status": "glob_truncated",
                    }
                )
    elif profile == "mlb-postgame":
        for rel in PREGAME_DATE_INPUTS:
            add_local(format_template(rel, date), "date_input/postgame_existing_pregame", False)
        for rel in POSTGAME_DATE_INPUTS:
            add_local(format_template(rel, date), "date_input/postgame", False)
        for rel in POSTGAME_GUARD_CACHE_FILES:
            add_local(format_template(rel, date), "cache/postgame_guard", True)
        for rel in POSTGAME_EXPECTED_OUTPUTS:
            add_local(format_template(rel, date), "expected_output/postgame_compare", False)
        for template, required in GDRIVE_MLB_DEPENDENCIES:
            add_unique(rows, gdrive_payload_row(template, date=date, required=required), seen)
        for pattern in [
            f"_deepread_v3/raw_plays/{date}/*.json",
            f"_reason_content/{date}/*.md",
            f"_synth_content/{date}/*.md",
            f"_run_total_reason_content/{date}/*.md",
            f"postmortem/**/{date}*",
            f"postmortem/**/*{ymd_no_dash(date)}*",
        ]:
            files, truncated = existing_glob_files(pattern, limit=2500)
            for rel in files:
                add_local(rel, "date_context/postgame_glob", False)
            if truncated:
                rows.append(
                    {
                        "path": pattern,
                        "role": "date_context/postgame_glob",
                        "required": False,
                        "status": "glob_truncated",
                    }
                )
    else:
        raise ColabOffloadError(f"Unknown profile: {profile}")

    status_counts = Counter(row.get("status", "unknown") for row in rows)
    required_problem_rows = [
        row
        for row in rows
        if row.get("required")
        and row.get("status") not in {"included"}
    ]
    required_external = [
        row
        for row in rows
        if row.get("required") and row.get("status") == "external_not_packaged"
    ]
    required_policy_blocked = [
        row
        for row in rows
        if row.get("required") and row.get("status") == "blocked_secret_policy"
    ]
    required_missing = [
        row
        for row in rows
        if row.get("required") and row.get("status") == "missing"
    ]
    included_rows = [row for row in rows if row.get("status") == "included"]
    large_rows = [row for row in included_rows if row.get("large")]

    production_ready = not required_problem_rows
    manifest = {
        "schema": "codex_colab_profile_manifest.v1",
        "created_utc": utc_stamp(),
        "profile": profile,
        "date": date,
        "repo_root": str(ROOT),
        "research_only_until_parity": True,
        "remote_env": {
            "MLB_DATA_DRIVE": MLB_REMOTE_DATA_DRIVE,
            "NARR_SKILL_DIR": ".codex_payload/skills/mlb-narrative-prediction-reasoning",
        },
        "remote_execution_ready": production_ready,
        "remote_execution_ready_reason": (
            "All required rows are included."
            if production_ready
            else "Required rows are missing, external, or blocked by policy; do not run production remotely."
        ),
        "max_large_mb": max_large_mb,
        "summary": {
            "rows": len(rows),
            "status_counts": dict(status_counts),
            "included_files": len(included_rows),
            "included_bytes": sum(int(row.get("bytes") or 0) for row in included_rows),
            "large_files": len(large_rows),
            "required_problem_rows": len(required_problem_rows),
            "required_missing": len(required_missing),
            "required_external_not_packaged": len(required_external),
            "required_policy_blocked": len(required_policy_blocked),
        },
        "required_problem_rows": required_problem_rows,
        "files": rows,
        "next_gate": "Run local-vs-remote parity check before trusting remote outputs.",
    }
    return manifest


def write_manifest_markdown(manifest: dict, path: Path) -> None:
    summary = manifest["summary"]
    lines = [
        f"# Colab Offload Manifest - {manifest['profile']} - {manifest['date']}",
        "",
        f"- created_utc: `{manifest['created_utc']}`",
        f"- research_only_until_parity: `{manifest['research_only_until_parity']}`",
        f"- remote_execution_ready: `{manifest['remote_execution_ready']}`",
        f"- reason: {manifest['remote_execution_ready_reason']}",
        f"- rows: `{summary['rows']}`",
        f"- included_files: `{summary['included_files']}`",
        f"- included_bytes: `{summary['included_bytes']}`",
        f"- large_files: `{summary['large_files']}`",
        f"- required_problem_rows: `{summary['required_problem_rows']}`",
        f"- required_missing: `{summary['required_missing']}`",
        f"- required_external_not_packaged: `{summary['required_external_not_packaged']}`",
        f"- required_policy_blocked: `{summary['required_policy_blocked']}`",
        "",
        "## Status Counts",
        "",
    ]
    for status, count in sorted(summary["status_counts"].items()):
        lines.append(f"- `{status}`: `{count}`")
    lines.extend(["", "## Required Problems", ""])
    problems = manifest.get("required_problem_rows") or []
    if not problems:
        lines.append("- none")
    else:
        for row in problems[:80]:
            lines.append(
                f"- `{row.get('status')}` `{row.get('path')}`"
                + (f" - {row.get('reason')}" if row.get("reason") else "")
            )
        if len(problems) > 80:
            lines.append(f"- ... truncated {len(problems) - 80} more rows")
    lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def manifest_command(args: argparse.Namespace) -> int:
    manifest = build_profile_manifest(args.profile, args.date, max_large_mb=args.max_large_mb)
    if args.write:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = utc_stamp()
        base = LOG_DIR / f"{manifest['profile']}_{manifest['date']}_{stamp}_manifest"
        json_path = base.with_suffix(".json")
        md_path = base.with_suffix(".md")
        json_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        write_manifest_markdown(manifest, md_path)
        print(f"[colab-offload] manifest json={json_path}")
        print(f"[colab-offload] manifest md={md_path}")
    print(json.dumps({"summary": manifest["summary"], "remote_execution_ready": manifest["remote_execution_ready"]}, ensure_ascii=False, indent=2))
    return 0


def profile_payload_rows(profile: str, date: str, *, max_large_mb: int) -> list[dict]:
    manifest = build_profile_manifest(profile, date, max_large_mb=max_large_mb)
    return [
        row
        for row in manifest.get("files", [])
        if row.get("packaging") == "payload_copy" and row.get("status") == "included"
    ]


def package_command(args: argparse.Namespace) -> int:
    date = normalize_date(args.date)
    stamp = utc_stamp()
    archive = Path(args.archive) if args.archive else LOG_DIR / f"{args.profile}_{date}_{stamp}_payload.zip"
    if not archive.is_absolute():
        archive = ROOT / archive
    includes = list(DEFAULT_PROBE_FILES)
    if args.include_code:
        includes = root_python_files()
        includes.extend(CORE_DOC_FILES)
    includes.extend(args.include or [])
    extra_rows = profile_payload_rows(args.profile, date, max_large_mb=args.max_large_mb)
    if getattr(args, "input_snapshot", None):
        extra_rows.append(input_snapshot_payload_row(Path(args.input_snapshot), required=True))
    manifest = build_payload(includes, archive, extra_rows=extra_rows)
    local_manifest = archive.with_suffix(".manifest.json")
    local_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("[colab-offload] package ok")
    print(f"[colab-offload] archive={archive}")
    print(f"[colab-offload] manifest={local_manifest}")
    print(
        json.dumps(
            {
                "archive_bytes": manifest["archive_bytes"],
                "files": len(manifest["files"]),
                "included": sum(1 for row in manifest["files"] if row.get("status") == "included"),
                "missing": sum(1 for row in manifest["files"] if row.get("status") == "missing"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def profile_included_manifest_rows(profile: str, date: str, *, max_large_mb: int) -> tuple[dict, list[str], list[dict]]:
    manifest = build_profile_manifest(profile, date, max_large_mb=max_large_mb)
    if not manifest.get("remote_execution_ready"):
        raise ColabOffloadError(
            f"Profile manifest is not remote-ready: {profile} {date}; "
            f"required_problem_rows={manifest.get('summary', {}).get('required_problem_rows')}"
        )
    repo_includes = [
        row["path"]
        for row in manifest.get("files", [])
        if row.get("packaging") == "repo_relative" and row.get("status") == "included"
    ]
    payload_rows = [
        row
        for row in manifest.get("files", [])
        if row.get("packaging") == "payload_copy" and row.get("status") == "included"
    ]
    return manifest, repo_includes, payload_rows


def filter_stage_payload(
    stage: str,
    includes: list[str],
    extra_rows: list[dict],
) -> tuple[list[str], list[dict], list[str]]:
    """Trim broad profile payload rows that are not inputs for a narrower parity stage."""
    removed: list[str] = []
    if stage != "postgame-deterministic":
        return includes, extra_rows, removed
    exact_excludes = {
        "postmortem/raw_pbp_event_ledger/mlb_raw_pbp_event_ledger.jsonl",
        "postmortem/raw_pbp_fullflow/regular_season_2026_directional/regular_season_directional_retrospective_2026.jsonl",
        "postmortem/raw_pbp_fullflow/run_total_focused_retrospective/mlb_run_total_focused_retrospective_2026.jsonl",
    }
    prefix_excludes = (
        "postmortem/automation_logs/colab_offload/",
    )

    def keep(rel: str) -> bool:
        norm = normalize_rel(rel)
        if norm in exact_excludes:
            return False
        return not any(norm.startswith(prefix) for prefix in prefix_excludes)

    filtered_includes: list[str] = []
    for rel in includes:
        if keep(rel):
            filtered_includes.append(rel)
        else:
            removed.append(rel)
    filtered_extra: list[dict] = []
    for row in extra_rows:
        rel = normalize_rel(str(row.get("payload_path") or row.get("path") or ""))
        if keep(rel):
            filtered_extra.append(row)
        else:
            removed.append(rel)
    return filtered_includes, filtered_extra, sorted(set(removed))


def remote_stage_commands(stage: str, date: str) -> list[list[str]]:
    date = normalize_date(date)
    if stage == "env-smoke":
        return []
    if stage == "predict-only":
        return [["python", "run_mlb_predict.py", "--date", date, "--no-notification"]]
    if stage == "pregame-guard-only":
        return [["python", "_mlb_pregame_guard_only.py", "--date", date]]
    if stage == "postgame-deterministic":
        return [["python", "_mlb_postgame_deterministic_only.py", "--date", date]]
    if stage == "pregame-model-base":
        return [
            ["python", "run_mlb_predict.py", "--date", date, "--no-notification"],
            [
                "python",
                "run_mlb_v3.py",
                "--date",
                date,
                "--no-notification",
                "--model-notification-only",
            ],
        ]
    raise ColabOffloadError(f"Unknown remote parity stage: {stage}")


def remote_stage_outputs(stage: str, date: str) -> list[str]:
    date = normalize_date(date)
    year = date[:4]
    outputs = [
        f"mlb_data/{year}/predictions_v2_{date}.csv",
        f"mlb_data/{year}/predictions_v2_{year}_all.csv",
    ]
    if stage == "env-smoke":
        return []
    if stage == "predict-only":
        return outputs
    if stage == "pregame-guard-only":
        return [
            f"postmortem/fullread_v4/pregame_signal_guards/mlb_narrator_pregame_signal_guard_{date}.json",
            f"postmortem/fullread_v4/pregame_signal_guards/mlb_narrator_pregame_signal_guard_{date}.csv",
            f"postmortem/fullread_v4/t038_pregame_signal_cards/pregame_signal_cards_{date}.jsonl",
            f"postmortem/fullread_v4/t038_pregame_signal_cards/pregame_signal_cards_summary_{date}.json",
            f"postmortem/fullread_v4/t038_pregame_signal_cards/pregame_signal_prompt_view_{date}.json",
            f"postmortem/fullread_v4/t038_pregame_signal_cards/pregame_signal_display_log_{date}.json",
        ]
    if stage == "pregame-model-base":
        return [
            f"_v3_games_{date}.csv",
            f"_narr_high_conf_{date}.json",
            f"postmortem/fullread_v4/model_defense_context/mlb_narr_model_defense_context_{date}.json",
            *outputs,
        ]
    if stage == "postgame-deterministic":
        ymd = ymd_no_dash(date)
        return [
            f"mlb_data/{year}/predictions_v2_{year}_all.csv",
            "day_score_log.csv",
            "backtest_master_mlb.csv",
            f"_v3_games_{date}.csv",
            "_narr_results_master.csv",
            "_narr_recommend_perf.csv",
            "_narr_recommend_calib.json",
            "_narr_miss_log.csv",
            f"postmortem/partial_results/{ymd}_mlb_partial_status.json",
            f"postmortem/partial_results/{ymd}_mlb_v3_result_summary.json",
            f"postmortem/raw_pbp_refresh/{ymd}_mlb_raw_pbp_refresh_report.json",
            "postmortem/fullread_v4/pregame_signal_guards/mlb_postgame_guard_refresh_report.json",
            "postmortem/fullread_v4/pregame_signal_guards/mlb_postgame_guard_refresh_report.md",
            f"postmortem/fullread_v4/guard_meta_backtest/mlb_narrator_guard_daily_audit_{date}.json",
            f"postmortem/fullread_v4/guard_meta_backtest/mlb_narrator_guard_daily_audit_{date}.csv",
            f"postmortem/fullread_v4/guard_meta_backtest/mlb_narrator_guard_daily_audit_{date}.md",
            f"postmortem/fullread_v4/synth_anti_hindsight/mlb_synth_anti_hindsight_{date}.json",
            f"postmortem/fullread_v4/synth_anti_hindsight/mlb_synth_anti_hindsight_{date}.md",
            f"postmortem/fullread_v4/prediction_delta_review/mlb_prediction_delta_review_{date}.jsonl",
            f"postmortem/fullread_v4/prediction_delta_review/mlb_prediction_delta_review_{date}.md",
            f"postmortem/fullread_v4/prediction_delta_review/mlb_prediction_delta_review_{date}_summary.json",
            "postmortem/fullread_v4/prediction_delta_review/mlb_prediction_delta_review_master.jsonl",
            f"postmortem/automation_logs/colab_offload/postgame_deterministic/mlb_postgame_deterministic_{date}.json",
            f"postmortem/automation_logs/colab_offload/postgame_deterministic/mlb_postgame_deterministic_{date}.md",
        ]
    raise ColabOffloadError(f"Unknown remote parity stage: {stage}")


def stage_comparison_specs(stage: str, date: str) -> list[dict]:
    date = normalize_date(date)
    year = date[:4]
    pregame_result_columns = [
        "실제_홈득점",
        "실제_원정득점",
        "실제_홈승",
        "실제_총득점",
        "승패_적중",
        "총득점_적중",
    ]
    specs = []
    if stage in {"predict-only", "pregame-model-base"}:
        specs.extend(
            [
                {
                    "path": f"mlb_data/{year}/predictions_v2_{date}.csv",
                    "local_path": str(MLB_LOCAL_DATA_DRIVE / year / f"predictions_v2_{date}.csv"),
                    "type": "csv_normalized",
                    "key": ["date", "gamePk"],
                },
                {
                    "path": f"mlb_data/{year}/predictions_v2_{year}_all.csv",
                    "local_path": str(MLB_LOCAL_DATA_DRIVE / year / f"predictions_v2_{year}_all.csv"),
                    "type": "csv_tail_for_date",
                    "date": date,
                    "key": ["date", "gamePk"],
                    "ignore_columns": pregame_result_columns,
                },
            ]
        )
    if stage == "pregame-model-base":
        specs.insert(
            0,
            {
                "path": f"_v3_games_{date}.csv",
                "local_path": str(ROOT / f"_v3_games_{date}.csv"),
                "type": "csv_normalized",
                "key": ["gamePk"],
            },
        )
        specs.append(
            {
                "path": f"postmortem/fullread_v4/model_defense_context/mlb_narr_model_defense_context_{date}.json",
                "local_path": str(
                    ROOT
                    / "postmortem"
                    / "fullread_v4"
                    / "model_defense_context"
                    / f"mlb_narr_model_defense_context_{date}.json"
                ),
                "type": "json_normalized",
                "optional": True,
            }
        )
    if stage == "pregame-guard-only":
        specs.extend(
            [
                {
                    "path": f"postmortem/fullread_v4/pregame_signal_guards/mlb_narrator_pregame_signal_guard_{date}.csv",
                    "local_path": str(
                        ROOT
                        / "postmortem"
                        / "fullread_v4"
                        / "pregame_signal_guards"
                        / f"mlb_narrator_pregame_signal_guard_{date}.csv"
                    ),
                    "type": "csv_normalized",
                    "key": ["date", "gamePk"],
                },
                {
                    "path": f"postmortem/fullread_v4/pregame_signal_guards/mlb_narrator_pregame_signal_guard_{date}.json",
                    "local_path": str(
                        ROOT
                        / "postmortem"
                        / "fullread_v4"
                        / "pregame_signal_guards"
                        / f"mlb_narrator_pregame_signal_guard_{date}.json"
                    ),
                    "type": "json_normalized",
                    "ignore_keys": ["source_csv", "path"],
                },
                {
                    "path": f"postmortem/fullread_v4/t038_pregame_signal_cards/pregame_signal_cards_{date}.jsonl",
                    "local_path": str(
                        ROOT
                        / "postmortem"
                        / "fullread_v4"
                        / "t038_pregame_signal_cards"
                        / f"pregame_signal_cards_{date}.jsonl"
                    ),
                    "type": "jsonl_tail_hash",
                    "date": date,
                },
                {
                    "path": f"postmortem/fullread_v4/t038_pregame_signal_cards/pregame_signal_cards_summary_{date}.json",
                    "local_path": str(
                        ROOT
                        / "postmortem"
                        / "fullread_v4"
                        / "t038_pregame_signal_cards"
                        / f"pregame_signal_cards_summary_{date}.json"
                    ),
                    "type": "json_normalized",
                    "ignore_keys": ["created_at_kst", "source_files", "section_source_files"],
                },
                {
                    "path": f"postmortem/fullread_v4/t038_pregame_signal_cards/pregame_signal_prompt_view_{date}.json",
                    "local_path": str(
                        ROOT
                        / "postmortem"
                        / "fullread_v4"
                        / "t038_pregame_signal_cards"
                        / f"pregame_signal_prompt_view_{date}.json"
                    ),
                    "type": "json_normalized",
                    "ignore_keys": ["created_at_kst", "source_files", "section_source_files"],
                },
                {
                    "path": f"postmortem/fullread_v4/t038_pregame_signal_cards/pregame_signal_display_log_{date}.json",
                    "local_path": str(
                        ROOT
                        / "postmortem"
                        / "fullread_v4"
                        / "t038_pregame_signal_cards"
                        / f"pregame_signal_display_log_{date}.json"
                    ),
                    "type": "json_normalized",
                    "ignore_keys": ["created_at_kst"],
                },
            ]
        )
    if stage == "postgame-deterministic":
        ymd = ymd_no_dash(date)
        specs.extend(
            [
                {
                    "path": f"mlb_data/{year}/predictions_v2_{year}_all.csv",
                    "local_path": str(MLB_LOCAL_DATA_DRIVE / year / f"predictions_v2_{year}_all.csv"),
                    "type": "csv_tail_for_date",
                    "date": date,
                    "key": ["date", "gamePk"],
                },
                {
                    "path": "day_score_log.csv",
                    "local_path": str(ROOT / "day_score_log.csv"),
                    "type": "csv_tail_for_date",
                    "date": date,
                },
                {
                    "path": "backtest_master_mlb.csv",
                    "local_path": str(ROOT / "backtest_master_mlb.csv"),
                    "type": "csv_tail_for_date",
                    "date": date,
                    "key": ["date", "game_id"],
                },
                {
                    "path": f"_v3_games_{date}.csv",
                    "local_path": str(ROOT / f"_v3_games_{date}.csv"),
                    "type": "csv_normalized",
                    "key": ["gamePk"],
                },
                {
                    "path": "_narr_results_master.csv",
                    "local_path": str(ROOT / "_narr_results_master.csv"),
                    "type": "csv_tail_for_date",
                    "date": date,
                    "key": ["date", "gamePk"],
                    "optional": True,
                },
                {
                    "path": "_narr_recommend_perf.csv",
                    "local_path": str(ROOT / "_narr_recommend_perf.csv"),
                    "type": "csv_tail_for_date",
                    "date": date,
                    "key": ["date", "gamePk", "axis"],
                    "optional": True,
                },
                {
                    "path": "_narr_recommend_calib.json",
                    "local_path": str(ROOT / "_narr_recommend_calib.json"),
                    "type": "json_normalized",
                    "optional": True,
                },
                {
                    "path": "_narr_miss_log.csv",
                    "local_path": str(ROOT / "_narr_miss_log.csv"),
                    "type": "csv_tail_for_date",
                    "date": date,
                    "key": ["date", "gamePk"],
                    "optional": True,
                },
                {
                    "path": f"postmortem/partial_results/{ymd}_mlb_partial_status.json",
                    "local_path": str(ROOT / "postmortem" / "partial_results" / f"{ymd}_mlb_partial_status.json"),
                    "type": "json_normalized",
                    "ignore_keys": ["generated_at_kst"],
                },
                {
                    "path": f"postmortem/partial_results/{ymd}_mlb_v3_result_summary.json",
                    "local_path": str(ROOT / "postmortem" / "partial_results" / f"{ymd}_mlb_v3_result_summary.json"),
                    "type": "json_normalized",
                    "ignore_keys": ["generated_at_kst", "notification_rows", "notification_gamePks", "already_sent_before_run"],
                },
                {
                    "path": f"postmortem/raw_pbp_refresh/{ymd}_mlb_raw_pbp_refresh_report.json",
                    "local_path": str(ROOT / "postmortem" / "raw_pbp_refresh" / f"{ymd}_mlb_raw_pbp_refresh_report.json"),
                    "type": "json_normalized",
                    "ignore_keys": ["generated_at_kst", "path"],
                    "optional": True,
                },
                {
                    "path": "postmortem/fullread_v4/pregame_signal_guards/mlb_postgame_guard_refresh_report.json",
                    "local_path": str(
                        ROOT
                        / "postmortem"
                        / "fullread_v4"
                        / "pregame_signal_guards"
                        / "mlb_postgame_guard_refresh_report.json"
                    ),
                    "type": "json_normalized",
                    "ignore_keys": ["generated_at_utc", "path"],
                    "optional": True,
                },
                {
                    "path": f"postmortem/fullread_v4/guard_meta_backtest/mlb_narrator_guard_daily_audit_{date}.json",
                    "local_path": str(
                        ROOT
                        / "postmortem"
                        / "fullread_v4"
                        / "guard_meta_backtest"
                        / f"mlb_narrator_guard_daily_audit_{date}.json"
                    ),
                    "type": "json_normalized",
                    "ignore_keys": ["predictions_path", "guard_path", "json", "csv", "markdown"],
                    "optional": True,
                },
                {
                    "path": f"postmortem/fullread_v4/synth_anti_hindsight/mlb_synth_anti_hindsight_{date}.json",
                    "local_path": str(
                        ROOT
                        / "postmortem"
                        / "fullread_v4"
                        / "synth_anti_hindsight"
                        / f"mlb_synth_anti_hindsight_{date}.json"
                    ),
                    "type": "json_normalized",
                    "optional": True,
                },
                {
                    "path": f"postmortem/fullread_v4/prediction_delta_review/mlb_prediction_delta_review_{date}.jsonl",
                    "local_path": str(
                        ROOT
                        / "postmortem"
                        / "fullread_v4"
                        / "prediction_delta_review"
                        / f"mlb_prediction_delta_review_{date}.jsonl"
                    ),
                    "type": "jsonl_tail_hash",
                    "date": date,
                    "optional": True,
                },
                {
                    "path": f"postmortem/fullread_v4/prediction_delta_review/mlb_prediction_delta_review_{date}_summary.json",
                    "local_path": str(
                        ROOT
                        / "postmortem"
                        / "fullread_v4"
                        / "prediction_delta_review"
                        / f"mlb_prediction_delta_review_{date}_summary.json"
                    ),
                    "type": "json_normalized",
                    "ignore_keys": ["generated_at_kst", "outputs"],
                    "optional": True,
                },
                {
                    "path": "postmortem/fullread_v4/prediction_delta_review/mlb_prediction_delta_review_master.jsonl",
                    "local_path": str(
                        ROOT
                        / "postmortem"
                        / "fullread_v4"
                        / "prediction_delta_review"
                        / "mlb_prediction_delta_review_master.jsonl"
                    ),
                    "type": "jsonl_tail_hash",
                    "date": date,
                    "optional": True,
                },
                {
                    "path": f"postmortem/automation_logs/colab_offload/postgame_deterministic/mlb_postgame_deterministic_{date}.json",
                    "local_path": str(
                        ROOT
                        / "postmortem"
                        / "automation_logs"
                        / "colab_offload"
                        / "postgame_deterministic"
                        / f"mlb_postgame_deterministic_{date}.json"
                    ),
                    "type": "json_normalized",
                    "ignore_keys": ["generated_at_kst", "duration_sec", "steps", "stdout_tail", "stderr_tail"],
                    "optional": True,
                },
            ]
        )
    return specs


def write_remote_command_script(
    path: Path,
    *,
    remote_payload: str,
    remote_root: str,
    remote_result: str,
    remote_outputs_zip: str,
    stage: str,
    date: str,
    commands: list[list[str]],
    expected_outputs: list[str],
    command_timeout: int,
    input_snapshot_payload_path: str | None = None,
    input_snapshot_sha256: str | None = None,
) -> None:
    body = f"""\
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
import traceback
import zipfile
from pathlib import Path

payload = Path({remote_payload!r})
remote_root = Path({remote_root!r})
result_path = Path({remote_result!r})
outputs_zip = Path({remote_outputs_zip!r})
stage = {stage!r}
date = {date!r}
commands = {json.dumps(commands, ensure_ascii=False)!r}
expected_outputs = {json.dumps(expected_outputs, ensure_ascii=False)!r}
command_timeout = {int(command_timeout)!r}
input_snapshot_payload_path = {json.dumps(input_snapshot_payload_path, ensure_ascii=False)!r}
input_snapshot_sha256 = {json.dumps(input_snapshot_sha256, ensure_ascii=False)!r}

commands = json.loads(commands)
expected_outputs = json.loads(expected_outputs)
input_snapshot_payload_path = json.loads(input_snapshot_payload_path)
input_snapshot_sha256 = json.loads(input_snapshot_sha256)

def safe_remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)

def file_sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

started = time.time()
command_results = []
collected_outputs = []
missing_outputs = []
input_checks = {{}}
ok = False
error = None

try:
    if remote_root.exists():
        shutil.rmtree(remote_root)
    remote_root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(payload, "r") as zf:
        zf.extractall(remote_root)

    manifest_path = remote_root / "_colab_payload_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    remote_env = dict(manifest.get("remote_env") or {{}})

    # The manifest documents /content/mlb_data.  The payload unpacks under a
    # session directory, so create a session-local symlink/copy at that stable
    # path before running commands.
    payload_mlb_data = remote_root / "mlb_data"
    stable_mlb_data = Path("/content/mlb_data")
    if payload_mlb_data.exists():
        try:
            if stable_mlb_data.exists() or stable_mlb_data.is_symlink():
                safe_remove(stable_mlb_data)
            stable_mlb_data.symlink_to(payload_mlb_data, target_is_directory=True)
        except Exception:
            if stable_mlb_data.exists() or stable_mlb_data.is_symlink():
                safe_remove(stable_mlb_data)
            shutil.copytree(payload_mlb_data, stable_mlb_data)
        remote_env["MLB_DATA_DRIVE"] = str(stable_mlb_data)

    env = os.environ.copy()
    env.update(remote_env)
    env.update(
        {{
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "COLAB_OFFLOAD_PAYLOAD": "1",
            "NARR_HELD_ALERT_NOTIFICATION": "0",
            "MLB_COLAB_PARITY_ONLY": "1",
        }}
    )
    if stage == "pregame-model-base":
        env["MLB_SKIP_PREGAME_GUARD"] = "1"

    if input_snapshot_payload_path:
        snapshot_target = remote_root / str(input_snapshot_payload_path)
        snapshot_check = {{
            "exists": snapshot_target.exists(),
            "bytes": snapshot_target.stat().st_size if snapshot_target.exists() else None,
            "sha256": file_sha256(snapshot_target) if snapshot_target.exists() else None,
            "expected_sha256": input_snapshot_sha256,
        }}
        snapshot_check["sha256_match"] = bool(
            snapshot_check["sha256"]
            and input_snapshot_sha256
            and snapshot_check["sha256"].lower() == str(input_snapshot_sha256).lower()
        )
        input_checks["input_snapshot"] = snapshot_check
        if not snapshot_check["exists"] or not snapshot_check["sha256_match"]:
            raise RuntimeError(f"input snapshot missing or hash mismatch: {{snapshot_check}}")
        env["MLB_PREGAME_INPUT_SNAPSHOT"] = str(snapshot_target)
        env["MLB_PREGAME_INPUT_SNAPSHOT_SHA256"] = str(input_snapshot_sha256)

    for rel in [
        "run_mlb_predict.py",
        "run_mlb_v3.py",
        "run_postgame.py",
        "_mlb_postgame_deterministic_only.py",
        "mlb_data/park_factors_2025.csv",
        "mlb_data/master_5seasons.csv",
        "mlb_data/2026/predictions_v2_2026_all.csv",
        ".codex_payload/skills/mlb-narrative-prediction-reasoning/scripts/narr_context.py",
        ".codex_payload/skills/mlb-narrative-prediction-reasoning/references/_narr_rules.md",
    ]:
        target = remote_root / rel
        input_checks[rel] = {{
            "exists": target.exists(),
            "bytes": target.stat().st_size if target.exists() else None,
        }}

    os.chdir(remote_root)
    for idx, cmd in enumerate(commands, start=1):
        t0 = time.time()
        proc = subprocess.run(
            cmd,
            cwd=remote_root,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=command_timeout,
        )
        command_results.append(
            {{
                "index": idx,
                "cmd": cmd,
                "returncode": proc.returncode,
                "duration_sec": round(time.time() - t0, 3),
                "stdout_tail": proc.stdout[-6000:],
                "stderr_tail": proc.stderr[-6000:],
            }}
        )
        if proc.returncode != 0:
            break

    ok = all(row.get("returncode") == 0 for row in command_results)
    if not commands:
        ok = True
    ok = ok and all(row.get("exists") for row in input_checks.values())

    with zipfile.ZipFile(outputs_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel in expected_outputs:
            rel = str(rel).replace("\\\\", "/").lstrip("/")
            candidates = [remote_root / rel, Path("/content") / rel]
            src = next((candidate for candidate in candidates if candidate.exists() and candidate.is_file()), None)
            if src is None:
                missing_outputs.append(rel)
                continue
            zf.write(src, rel)
            collected_outputs.append({{"path": rel, "bytes": src.stat().st_size}})

    result = {{
        "schema": "codex_colab_remote_parity_run.v1",
        "ok": bool(ok),
        "stage": stage,
        "date": date,
        "remote_python": platform.python_version(),
        "remote_platform": platform.platform(),
        "remote_root": str(remote_root),
        "payload_path": str(payload),
        "outputs_zip": str(outputs_zip),
        "duration_sec": round(time.time() - started, 3),
        "env": {{
            "MLB_DATA_DRIVE": env.get("MLB_DATA_DRIVE"),
            "NARR_SKILL_DIR": env.get("NARR_SKILL_DIR"),
            "COLAB_OFFLOAD_PAYLOAD": env.get("COLAB_OFFLOAD_PAYLOAD"),
            "MLB_PREGAME_INPUT_SNAPSHOT": env.get("MLB_PREGAME_INPUT_SNAPSHOT"),
            "MLB_PREGAME_INPUT_SNAPSHOT_SHA256": env.get("MLB_PREGAME_INPUT_SNAPSHOT_SHA256"),
        }},
        "commands": command_results,
        "input_checks": input_checks,
        "expected_outputs": expected_outputs,
        "collected_outputs": collected_outputs,
        "missing_outputs": missing_outputs,
        "production_remote_allowed": False,
    }}
except Exception as exc:
    error = f"{{type(exc).__name__}}: {{exc}}"
    result = {{
        "schema": "codex_colab_remote_parity_run.v1",
        "ok": False,
        "stage": stage,
        "date": date,
        "remote_python": platform.python_version(),
        "remote_platform": platform.platform(),
        "remote_root": str(remote_root),
        "payload_path": str(payload),
        "outputs_zip": str(outputs_zip),
        "duration_sec": round(time.time() - started, 3),
        "commands": command_results,
        "input_checks": input_checks,
        "expected_outputs": expected_outputs,
        "collected_outputs": collected_outputs,
        "missing_outputs": missing_outputs,
        "error": error,
        "traceback_tail": traceback.format_exc()[-6000:],
        "production_remote_allowed": False,
    }}

result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
print(json.dumps(result, ensure_ascii=False, indent=2))
"""
    path.write_text(body, encoding="utf-8")


def compare_stage_outputs(stage: str, date: str, remote_root: Path) -> dict:
    specs = stage_comparison_specs(stage, date)
    rows = []
    critical_diffs = 0
    optional_diffs = 0
    optional_diffs = 0
    for spec in specs:
        local_path = Path(spec["local_path"])
        remote_path = remote_root / spec["path"]
        optional = bool(spec.get("optional"))
        row = {
            "path": spec["path"],
            "local_path": str(local_path),
            "remote_path": str(remote_path),
            "type": spec.get("type"),
            "optional": optional,
            "local_exists": local_path.exists(),
            "remote_exists": remote_path.exists(),
        }
        if not local_path.exists() and not remote_path.exists() and optional:
            row["status"] = "skipped_missing_optional"
        elif not local_path.exists() or not remote_path.exists():
            row["status"] = "missing_side"
            if not optional:
                critical_diffs += 1
            else:
                optional_diffs += 1
        else:
            try:
                local_hash, local_bytes = comparison_digest(local_path, spec)
                remote_hash, remote_bytes = comparison_digest(remote_path, spec)
                row.update(
                    {
                        "local_digest": local_hash,
                        "remote_digest": remote_hash,
                        "local_normalized_bytes": local_bytes,
                        "remote_normalized_bytes": remote_bytes,
                    }
                )
                if local_hash == remote_hash:
                    row["status"] = "match"
                else:
                    row["status"] = "diff"
                    if not optional:
                        critical_diffs += 1
                    else:
                        optional_diffs += 1
            except Exception as exc:  # noqa: BLE001
                row["status"] = "compare_error"
                row["error"] = str(exc)
                if not optional:
                    critical_diffs += 1
                else:
                    optional_diffs += 1
        rows.append(row)
    return {
        "schema": "codex_colab_stage_parity_check.v1",
        "created_utc": utc_stamp(),
        "stage": stage,
        "date": normalize_date(date),
        "remote_root": str(remote_root),
        "comparisons": len(rows),
        "critical_diffs": critical_diffs,
        "optional_diffs": optional_diffs,
        "parity_ok": critical_diffs == 0,
        "production_remote_allowed": critical_diffs == 0,
        "rows": rows,
    }


def resolve_input_snapshot(path_value: str | None) -> tuple[Path | None, str | None, str | None, dict | None]:
    if not path_value:
        return None, None, None, None
    path = Path(path_value)
    if not path.is_absolute():
        path = ROOT / path
    row = input_snapshot_payload_row(path, required=True)
    if row.get("status") != "included":
        raise ColabOffloadError(f"Input snapshot is not packageable: {path} status={row.get('status')}")
    return path.resolve(), row["payload_path"], row["sha256"], row


def run_local_stage_reference(
    *,
    stage: str,
    date: str,
    input_snapshot: Path | None,
    input_snapshot_sha256: str | None,
    command_timeout: int,
) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = utc_stamp()
    result_path = LOG_DIR / f"local-ref-{stage}_{date}_{stamp}_result.json"
    env = os.environ.copy()
    env.update(
        {
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "MLB_COLAB_PARITY_ONLY": "1",
            "NARR_HELD_ALERT_NOTIFICATION": "0",
        }
    )
    if stage == "pregame-model-base":
        env["MLB_SKIP_PREGAME_GUARD"] = "1"
    if input_snapshot:
        env["MLB_PREGAME_INPUT_SNAPSHOT"] = str(input_snapshot)
        env["MLB_PREGAME_INPUT_SNAPSHOT_SHA256"] = str(input_snapshot_sha256 or file_sha256(input_snapshot))

    results = []
    ok = True
    for idx, cmd in enumerate(remote_stage_commands(stage, date), start=1):
        local_cmd = [sys.executable if part == "python" else part for part in cmd]
        t0 = time.time()
        proc = subprocess.run(
            local_cmd,
            cwd=ROOT,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=command_timeout,
        )
        row = {
            "index": idx,
            "cmd": local_cmd,
            "returncode": proc.returncode,
            "duration_sec": round(time.time() - t0, 3),
            "stdout_tail": proc.stdout[-6000:],
            "stderr_tail": proc.stderr[-6000:],
        }
        results.append(row)
        if proc.returncode != 0:
            ok = False
            break

    payload = {
        "schema": "codex_colab_local_stage_reference.v1",
        "created_utc": stamp,
        "stage": stage,
        "date": date,
        "ok": ok,
        "input_snapshot": str(input_snapshot) if input_snapshot else None,
        "input_snapshot_sha256": input_snapshot_sha256,
        "commands": results,
    }
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not ok:
        raise ColabOffloadError(f"Local stage reference failed: {result_path}")
    return result_path


def remote_parity_run_command(args: argparse.Namespace) -> int:
    date = normalize_date(args.date)
    stamp = utc_stamp()
    session = args.session or f"codex-parity-{args.stage}-{date}-{int(time.time()) % 100000}".replace("_", "-")
    remote_payload = f"/content/{session}_payload.zip"
    remote_root = f"/content/{session}_payload"
    remote_result = f"/content/{session}_remote_result.json"
    remote_outputs_zip = f"/content/{session}_remote_outputs.zip"

    snapshot_path, snapshot_payload_path, snapshot_sha256, snapshot_row = resolve_input_snapshot(
        getattr(args, "input_snapshot", None)
    )
    local_ref_result = None
    if getattr(args, "refresh_local_ref", False) and not getattr(args, "dry_run", False):
        local_ref_result = run_local_stage_reference(
            stage=args.stage,
            date=date,
            input_snapshot=snapshot_path,
            input_snapshot_sha256=snapshot_sha256,
            command_timeout=args.command_timeout,
        )

    manifest, includes, extra_rows = profile_included_manifest_rows(
        args.profile,
        date,
        max_large_mb=args.max_large_mb,
    )
    if snapshot_row:
        extra_rows.append(snapshot_row)
    includes, extra_rows, stage_payload_removed = filter_stage_payload(args.stage, includes, extra_rows)
    archive = LOG_DIR / f"{session}_{stamp}_payload.zip"
    remote_script = LOG_DIR / f"{session}_{stamp}_remote_command.py"
    local_manifest = LOG_DIR / f"{session}_{stamp}_payload_manifest.json"
    local_profile_manifest = LOG_DIR / f"{session}_{stamp}_profile_manifest.json"
    local_result = LOG_DIR / f"{session}_{stamp}_remote_result.json"
    local_outputs_zip = LOG_DIR / f"{session}_{stamp}_remote_outputs.zip"
    local_outputs_dir = LOG_DIR / f"{session}_{stamp}_remote_outputs"
    local_parity = LOG_DIR / f"{session}_{stamp}_stage_parity.json"

    payload_manifest = build_payload(includes, archive, extra_rows=extra_rows)
    local_manifest.write_text(json.dumps(payload_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    local_profile_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    commands = remote_stage_commands(args.stage, date)
    expected_outputs = remote_stage_outputs(args.stage, date)
    write_remote_command_script(
        remote_script,
        remote_payload=remote_payload,
        remote_root=remote_root,
        remote_result=remote_result,
        remote_outputs_zip=remote_outputs_zip,
        stage=args.stage,
        date=date,
        commands=commands,
        expected_outputs=expected_outputs,
        command_timeout=args.command_timeout,
        input_snapshot_payload_path=snapshot_payload_path,
        input_snapshot_sha256=snapshot_sha256,
    )

    if args.dry_run:
        print("[colab-offload] remote parity dry-run")
        print(f"[colab-offload] profile_manifest={local_profile_manifest}")
        print(f"[colab-offload] payload={archive}")
        print(f"[colab-offload] script={remote_script}")
        print(json.dumps({"stage": args.stage, "date": date, "commands": commands, "expected_outputs": expected_outputs, "input_snapshot": str(snapshot_path) if snapshot_path else None, "input_snapshot_sha256": snapshot_sha256, "stage_payload_removed_count": len(stage_payload_removed)}, ensure_ascii=False, indent=2))
        return 0

    created = False
    try:
        new_args = ["new", "-s", session]
        if args.gpu:
            new_args.extend(["--gpu", args.gpu])
        run_wsl_colab(new_args, timeout=args.new_timeout)
        created = True
        run_wsl_colab(["upload", "-s", session, win_path_to_wsl(archive), remote_payload], timeout=args.io_timeout)
        run_wsl_colab(["exec", "-s", session, "-f", win_path_to_wsl(remote_script), "--timeout", str(args.exec_timeout)], timeout=args.exec_timeout + 60, check=False)
        run_wsl_colab(["download", "-s", session, remote_result, win_path_to_wsl(local_result)], timeout=args.io_timeout, check=False)
        run_wsl_colab(["download", "-s", session, remote_outputs_zip, win_path_to_wsl(local_outputs_zip)], timeout=args.io_timeout, check=False)
    finally:
        if created:
            run_wsl_colab(["stop", "-s", session], timeout=args.stop_timeout, check=False)

    if not local_result.exists():
        raise ColabOffloadError(f"Remote result was not downloaded: {local_result}")
    remote_result_data = json.loads(local_result.read_text(encoding="utf-8"))

    if local_outputs_dir.exists():
        shutil.rmtree(local_outputs_dir)
    local_outputs_dir.mkdir(parents=True, exist_ok=True)
    if local_outputs_zip.exists():
        with zipfile.ZipFile(local_outputs_zip, "r") as zf:
            zf.extractall(local_outputs_dir)

    stage_parity = compare_stage_outputs(args.stage, date, local_outputs_dir)
    stage_parity["remote_result"] = str(local_result)
    stage_parity["remote_ok"] = bool(remote_result_data.get("ok"))
    stage_parity["remote_missing_outputs"] = remote_result_data.get("missing_outputs") or []
    stage_parity["profile_manifest"] = str(local_profile_manifest)
    stage_parity["payload_manifest"] = str(local_manifest)
    stage_parity["payload_archive"] = str(archive)
    stage_parity["stage_payload_removed"] = stage_payload_removed
    stage_parity["input_snapshot"] = str(snapshot_path) if snapshot_path else None
    stage_parity["input_snapshot_sha256"] = snapshot_sha256
    stage_parity["local_ref_result"] = str(local_ref_result) if local_ref_result else None
    stage_parity["remote_input_snapshot_check"] = (remote_result_data.get("input_checks") or {}).get("input_snapshot")
    stage_parity["production_remote_allowed"] = bool(stage_parity["remote_ok"] and stage_parity["parity_ok"])
    local_parity.write_text(json.dumps(stage_parity, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("[colab-offload] remote parity run complete")
    print(f"[colab-offload] result={local_result}")
    print(f"[colab-offload] outputs={local_outputs_dir}")
    print(f"[colab-offload] parity={local_parity}")
    print(
        json.dumps(
            {
                "stage": args.stage,
                "date": date,
                "remote_ok": stage_parity["remote_ok"],
                "critical_diffs": stage_parity["critical_diffs"],
                "parity_ok": stage_parity["parity_ok"],
                "production_remote_allowed": stage_parity["production_remote_allowed"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if remote_result_data.get("ok") and stage_parity["parity_ok"] else 1


def stage_parity_check_command(args: argparse.Namespace) -> int:
    date = normalize_date(args.date)
    remote_root = Path(args.remote_root)
    if not remote_root.is_absolute():
        remote_root = ROOT / remote_root
    report = compare_stage_outputs(args.stage, date, remote_root)
    if args.write:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = utc_stamp()
        out = LOG_DIR / f"{args.stage}_{date}_{stamp}_stage_parity_check.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[colab-offload] stage parity check={out}")
    print(
        json.dumps(
            {
                "stage": report["stage"],
                "date": report["date"],
                "comparisons": report["comparisons"],
                "critical_diffs": report["critical_diffs"],
                "parity_ok": report["parity_ok"],
                "production_remote_allowed": report["production_remote_allowed"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["parity_ok"] else 1


def build_parity_plan(profile: str, date: str) -> dict:
    date = normalize_date(date)
    if profile == "mlb-pregame":
        comparisons = [
            {"path": f"_v3_games_{date}.csv", "type": "csv_normalized", "key": ["gamePk"]},
            {"path": f"_narr_predictions_{date}.json", "type": "json_normalized", "key": ["gamePk"]},
            {"path": f"_narr_send_manifest_{date}.json", "type": "json_normalized", "optional": True},
            {"path": f"_narr_high_conf_{date}.json", "type": "json_normalized", "optional": True},
            {
                "path": f"postmortem/fullread_v4/model_defense_context/mlb_narr_model_defense_context_{date}.json",
                "type": "json_normalized",
                "optional": True,
            },
            {
                "path": f"postmortem/fullread_v4/model_defense_context/mlb_narr_model_defense_context_{date}.txt",
                "type": "text_sha256",
                "optional": True,
            },
        ]
        command_shape = [
            f"py run_mlb_predict.py --date {date} --no-notification",
            f"py _narr_codex_run.py --date {date} --gamepks GAMEPKS --effort high --timeout 600 --repair-on-validate-fail",
        ]
    elif profile == "mlb-postgame":
        comparisons = [
            {"path": "day_score_log.csv", "type": "csv_tail_for_date", "date": date},
            {"path": "_narr_results_master.csv", "type": "csv_tail_for_date", "date": date},
            {"path": "_narr_miss_log.csv", "type": "csv_tail_for_date", "date": date, "optional": True},
            {"path": f"postmortem/partial_results/{ymd_no_dash(date)}_mlb_v3_result_summary.json", "type": "json_normalized", "optional": True},
            {"path": "postmortem/fullread_v4/pregame_signal_guards/mlb_postgame_guard_refresh_report.json", "type": "json_normalized", "optional": True},
            {"path": "_narr_synth_lessons.jsonl", "type": "jsonl_tail_hash", "optional": True},
            {"path": "_narr_synth_takeaways.jsonl", "type": "jsonl_tail_hash", "optional": True},
        ]
        command_shape = [f"py run_postgame.py {date} mlb"]
    else:
        raise ColabOffloadError(f"Unknown profile: {profile}")

    return {
        "schema": "codex_colab_parity_plan.v1",
        "created_utc": utc_stamp(),
        "profile": profile,
        "date": date,
        "production_allowed": False,
        "production_allowed_reason": "Parity plan only. Production remote use requires an actual local-vs-remote diff report with zero critical diffs.",
        "required_order": [
            "snapshot local outputs before local reference run",
            "run local reference command in a clean output directory or explicitly snapshot changed files",
            "run the same command remotely using an approved complete manifest",
            "download remote outputs into an isolated directory",
            "normalize and compare the files listed below",
            "allow production remote execution only if critical diffs are zero",
        ],
        "command_shape": command_shape,
        "comparisons": comparisons,
        "known_phase2_blockers": [
            "G-drive model/master/prediction dependencies are now copied into the payload under mlb_data; remote runs must set MLB_DATA_DRIVE=/content/mlb_data after unpack.",
            "Exported narrator context/rules are copied fresh into .codex_payload/skills; remote runs must set NARR_SKILL_DIR=.codex_payload/skills/mlb-narrative-prediction-reasoning after unpack.",
            "Codex bridge execution from Colab is not proven. If a stage invokes codex.exe/desktop bridge, keep that stage local or design a separate approved LLM route.",
            "Production remote execution still requires a real local-vs-remote parity report with critical_diffs=0.",
        ],
    }


def parity_plan_command(args: argparse.Namespace) -> int:
    plan = build_parity_plan(args.profile, args.date)
    if args.write:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = utc_stamp()
        path = LOG_DIR / f"{plan['profile']}_{plan['date']}_{stamp}_parity_plan.json"
        path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[colab-offload] parity plan={path}")
    print(json.dumps({"profile": plan["profile"], "date": plan["date"], "comparisons": len(plan["comparisons"]), "production_allowed": False}, ensure_ascii=False, indent=2))
    return 0


def stable_hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def strip_json_keys(value, ignore_keys: set[str]):
    if not ignore_keys:
        return value
    if isinstance(value, dict):
        return {
            key: strip_json_keys(item, ignore_keys)
            for key, item in value.items()
            if key not in ignore_keys
        }
    if isinstance(value, list):
        return [strip_json_keys(item, ignore_keys) for item in value]
    return value


def normalize_json_file(path: Path, *, ignore_keys: list[str] | None = None) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    data = strip_json_keys(data, set(ignore_keys or []))
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_csv_scalar(value: object) -> str:
    text = "" if value is None else str(value).strip()
    if not text or text.lower() in {"nan", "none", "<na>"}:
        return ""
    try:
        number = float(text)
    except ValueError:
        return text
    if not math.isfinite(number):
        return ""
    return format(round(number, 10), ".10g")


def normalize_csv_file(
    path: Path,
    *,
    key: list[str] | None = None,
    filter_date: str | None = None,
    ignore_columns: list[str] | None = None,
) -> str:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        fields = reader.fieldnames or []
    ignore = set(ignore_columns or [])
    if filter_date:
        ymd = ymd_no_dash(filter_date)
        rows = [
            row
            for row in rows
            if any(str(value) in {filter_date, ymd} for value in row.values())
        ]
    ordered_fields = sorted(field for field in fields if field not in ignore)
    normalized_rows = [
        {field: normalize_csv_scalar(row.get(field, "")) for field in ordered_fields}
        for row in rows
    ]
    if key:
        normalized_rows.sort(key=lambda row: tuple(str(row.get(k, "")) for k in key))
    else:
        normalized_rows.sort(key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True))
    return json.dumps(normalized_rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_jsonl_tail(path: Path, *, filter_date: str | None = None, max_lines: int = 5000) -> str:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if filter_date:
        ymd = ymd_no_dash(filter_date)
        lines = [line for line in lines if filter_date in line or ymd in line]
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    normalized = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            normalized.append(json.loads(stripped))
        except json.JSONDecodeError:
            normalized.append(stripped)
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def comparison_digest(path: Path, spec: dict) -> tuple[str, int]:
    ctype = spec.get("type")
    if ctype == "json_normalized":
        text = normalize_json_file(path, ignore_keys=spec.get("ignore_keys"))
    elif ctype == "csv_normalized":
        text = normalize_csv_file(path, key=spec.get("key"), ignore_columns=spec.get("ignore_columns"))
    elif ctype == "csv_tail_for_date":
        text = normalize_csv_file(
            path,
            key=spec.get("key"),
            filter_date=spec.get("date"),
            ignore_columns=spec.get("ignore_columns"),
        )
    elif ctype == "jsonl_tail_hash":
        text = normalize_jsonl_tail(path, filter_date=spec.get("date"))
    elif ctype == "text_sha256":
        data = path.read_bytes()
        return hashlib.sha256(data).hexdigest(), len(data)
    else:
        data = path.read_bytes()
        return hashlib.sha256(data).hexdigest(), len(data)
    return stable_hash_text(text), len(text.encode("utf-8"))


def parity_check_command(args: argparse.Namespace) -> int:
    plan_path = Path(args.plan)
    if not plan_path.is_absolute():
        plan_path = ROOT / plan_path
    remote_root = Path(args.remote_root)
    if not remote_root.is_absolute():
        remote_root = ROOT / remote_root
    local_root = Path(args.local_root).resolve() if args.local_root else ROOT
    plan = json.loads(plan_path.read_text(encoding="utf-8"))

    rows = []
    critical_diffs = 0
    for spec in plan.get("comparisons", []):
        rel = spec["path"]
        local_path = (local_root / rel).resolve()
        remote_path = (remote_root / rel).resolve()
        optional = bool(spec.get("optional"))
        row = {
            "path": rel,
            "type": spec.get("type"),
            "optional": optional,
            "local_exists": local_path.exists(),
            "remote_exists": remote_path.exists(),
        }
        if not local_path.exists() and not remote_path.exists() and optional:
            row["status"] = "skipped_missing_optional"
        elif not local_path.exists() or not remote_path.exists():
            row["status"] = "missing_side"
            if not optional:
                critical_diffs += 1
            else:
                optional_diffs += 1
        else:
            try:
                local_hash, local_bytes = comparison_digest(local_path, spec)
                remote_hash, remote_bytes = comparison_digest(remote_path, spec)
                row.update(
                    {
                        "local_digest": local_hash,
                        "remote_digest": remote_hash,
                        "local_normalized_bytes": local_bytes,
                        "remote_normalized_bytes": remote_bytes,
                    }
                )
                if local_hash == remote_hash:
                    row["status"] = "match"
                else:
                    row["status"] = "diff"
                    if not optional:
                        critical_diffs += 1
                    else:
                        optional_diffs += 1
            except Exception as exc:  # noqa: BLE001 - report comparison failure, do not hide it.
                row["status"] = "compare_error"
                row["error"] = str(exc)
                if not optional:
                    critical_diffs += 1
                else:
                    optional_diffs += 1
        rows.append(row)

    report = {
        "schema": "codex_colab_parity_check.v1",
        "created_utc": utc_stamp(),
        "plan": str(plan_path),
        "profile": plan.get("profile"),
        "date": plan.get("date"),
        "local_root": str(local_root),
        "remote_root": str(remote_root),
        "comparisons": len(rows),
        "critical_diffs": critical_diffs,
        "optional_diffs": optional_diffs,
        "parity_ok": critical_diffs == 0,
        "production_remote_allowed": critical_diffs == 0,
        "rows": rows,
    }
    if args.write:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = utc_stamp()
        out = LOG_DIR / f"{report['profile']}_{report['date']}_{stamp}_parity_check.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[colab-offload] parity check={out}")
    print(json.dumps({k: report[k] for k in ["profile", "date", "comparisons", "critical_diffs", "parity_ok"]}, ensure_ascii=False, indent=2))
    return 0 if report["parity_ok"] else 1


def build_payload(includes: list[str], archive: Path, *, extra_rows: list[dict] | None = None) -> dict:
    archive.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        seen: set[str] = set()

        def add_zip_file(abs_path: Path, arcname: str, row_base: dict) -> None:
            arcname = normalize_rel(arcname)
            if is_secret_like(arcname):
                rows.append(
                    {
                        **row_base,
                        "path": arcname,
                        "status": "blocked_secret_policy",
                        "reason": "Payload destination matches secret/policy block list.",
                    }
                )
                return
            if not abs_path.exists() or not abs_path.is_file():
                rows.append({**row_base, "path": arcname, "status": "missing"})
                return
            if arcname in seen:
                return
            seen.add(arcname)
            zf.write(abs_path, arcname)
            rows.append(
                {
                    **row_base,
                    "path": arcname,
                    "status": "included",
                    "bytes": abs_path.stat().st_size,
                    "sha256": file_sha256(abs_path),
                }
            )

        for rel in includes:
            try:
                abs_path = ensure_relative_safe(rel)
            except ColabOffloadError as exc:
                rows.append(
                    {
                        "packaging": "repo_relative",
                        "path": normalize_rel(rel),
                        "status": "blocked_secret_policy",
                        "reason": str(exc),
                    }
                )
                continue
            add_zip_file(abs_path, rel.replace("\\", "/"), {"packaging": "repo_relative"})
        for row in extra_rows or []:
            if row.get("status") != "included":
                rows.append(row)
                continue
            source_path = row.get("source_path")
            payload_path = row.get("payload_path") or row.get("path")
            if not source_path or not payload_path:
                rows.append({**row, "status": "bad_payload_row"})
                continue
            add_zip_file(Path(source_path), payload_path, {k: v for k, v in row.items() if k not in {"status", "bytes", "sha256"}})
        manifest = {
            "schema": "codex_colab_payload_manifest.v1",
            "created_utc": utc_stamp(),
            "repo_root": str(ROOT),
            "remote_env": {
                "MLB_DATA_DRIVE": MLB_REMOTE_DATA_DRIVE,
                "NARR_SKILL_DIR": ".codex_payload/skills/mlb-narrative-prediction-reasoning",
            },
            "files": rows,
        }
        zf.writestr("_colab_payload_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    manifest["archive"] = str(archive)
    manifest["archive_bytes"] = archive.stat().st_size
    manifest["archive_sha256"] = file_sha256(archive)
    return manifest


def write_remote_probe_script(path: Path, remote_payload: str, remote_root: str, remote_result: str) -> None:
    body = f"""\
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import zipfile
from pathlib import Path

payload = Path({remote_payload!r})
remote_root = Path({remote_root!r})
result_path = Path({remote_result!r})

def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

if remote_root.exists():
    shutil.rmtree(remote_root)
remote_root.mkdir(parents=True, exist_ok=True)

with zipfile.ZipFile(payload, "r") as zf:
    zf.extractall(remote_root)

manifest_path = remote_root / "_colab_payload_manifest.json"
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
included = [row for row in manifest.get("files", []) if row.get("status") == "included"]
missing = [row for row in manifest.get("files", []) if row.get("status") == "missing"]

checks = {{}}
for rel in [
    "AGENTS.md",
    "run_mlb_predict.py",
    "run_mlb_v3.py",
    "run_postgame.py",
    "_narr_codex_run.py",
    "mlb_data/park_factors_2025.csv",
    "mlb_data/master_5seasons.csv",
    "mlb_data/2026/predictions_v2_2026_all.csv",
    ".codex_payload/skills/mlb-narrative-prediction-reasoning/scripts/narr_context.py",
    ".codex_payload/skills/mlb-narrative-prediction-reasoning/references/_narr_rules.md",
]:
    target = remote_root / rel
    checks[rel] = {{
        "exists": target.exists(),
        "bytes": target.stat().st_size if target.exists() else None,
    }}

result = {{
    "schema": "codex_colab_probe_result.v1",
    "ok": True,
    "remote_python": platform.python_version(),
    "remote_platform": platform.platform(),
    "remote_cwd": os.getcwd(),
    "remote_root": str(remote_root),
    "payload_path": str(payload),
    "payload_bytes": payload.stat().st_size,
    "payload_sha256": sha256(payload),
    "manifest_files": len(manifest.get("files", [])),
    "included_files": len(included),
    "missing_files": len(missing),
    "missing": missing,
    "checks": checks,
}}

result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False, indent=2))
"""
    path.write_text(body, encoding="utf-8")


def run_wsl_colab(args: list[str], *, timeout: int, check: bool = True) -> subprocess.CompletedProcess[str]:
    quoted = " ".join(shlex.quote(part) for part in args)
    script = f"cd {shlex.quote(win_path_to_wsl(ROOT))} && colab --auth=adc {quoted}"
    cmd = ["wsl", "-d", "Ubuntu", "--", "bash", "-lc", script]
    print(f"[colab-offload] running: {script}", flush=True)
    return subprocess.run(cmd, text=True, check=check, timeout=timeout)


def probe(args: argparse.Namespace) -> int:
    if bool(args.profile) != bool(args.date):
        raise ColabOffloadError("--profile and --date must be supplied together for probe profile payloads.")
    stamp = utc_stamp()
    session = args.session or f"codex-probe-{int(time.time()) % 100000}"
    remote_payload = f"/content/{session}_payload.zip"
    remote_root = f"/content/{session}_payload"
    remote_result = f"/content/{session}_probe_result.json"

    archive = LOG_DIR / f"{session}_{stamp}_payload.zip"
    remote_script = LOG_DIR / f"{session}_{stamp}_remote_probe.py"
    local_result = LOG_DIR / f"{session}_{stamp}_probe_result.json"
    local_manifest = LOG_DIR / f"{session}_{stamp}_payload_manifest.json"

    includes = list(DEFAULT_PROBE_FILES)
    includes.extend(args.include or [])
    extra_rows = []
    if args.profile and args.date:
        extra_rows = profile_payload_rows(args.profile, normalize_date(args.date), max_large_mb=args.max_large_mb)
    manifest = build_payload(includes, archive, extra_rows=extra_rows)
    local_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    write_remote_probe_script(remote_script, remote_payload, remote_root, remote_result)

    created = False
    try:
        new_args = ["new", "-s", session]
        if args.gpu:
            new_args.extend(["--gpu", args.gpu])
        run_wsl_colab(new_args, timeout=args.new_timeout)
        created = True
        run_wsl_colab(["upload", "-s", session, win_path_to_wsl(archive), remote_payload], timeout=args.io_timeout)
        run_wsl_colab(["exec", "-s", session, "-f", win_path_to_wsl(remote_script), "--timeout", str(args.exec_timeout)], timeout=args.exec_timeout + 60)
        run_wsl_colab(["download", "-s", session, remote_result, win_path_to_wsl(local_result)], timeout=args.io_timeout)
    finally:
        if created:
            run_wsl_colab(["stop", "-s", session], timeout=args.stop_timeout, check=False)

    if not local_result.exists():
        raise ColabOffloadError(f"Probe result was not downloaded: {local_result}")
    result = json.loads(local_result.read_text(encoding="utf-8"))
    if not result.get("ok"):
        raise ColabOffloadError(f"Remote probe failed: {local_result}")

    print("[colab-offload] probe ok")
    print(f"[colab-offload] session={session}")
    print(f"[colab-offload] payload={archive}")
    print(f"[colab-offload] manifest={local_manifest}")
    print(f"[colab-offload] result={local_result}")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safe Colab offload wrapper for staged MLB pipeline migration.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_probe = sub.add_parser("probe", help="Upload a small payload, run a remote probe, download result, and stop.")
    p_probe.add_argument("--session", help="Named Colab session. Defaults to a codex-probe-* name.")
    p_probe.add_argument("--gpu", default=None, choices=["T4", "L4", "G4", "H100", "A100"], help="Optional Colab GPU. Omit for CPU.")
    p_probe.add_argument("--include", action="append", help="Additional safe relative file to include in the probe payload.")
    p_probe.add_argument("--profile", choices=["mlb-pregame", "mlb-postgame"], help="Optional profile payload rows to include in the probe zip.")
    p_probe.add_argument("--date", help="Required with --profile. MLB date as YYYY-MM-DD or YYYYMMDD.")
    p_probe.add_argument("--max-large-mb", type=int, default=50)
    p_probe.add_argument("--new-timeout", type=int, default=180)
    p_probe.add_argument("--io-timeout", type=int, default=180)
    p_probe.add_argument("--exec-timeout", type=int, default=60)
    p_probe.add_argument("--stop-timeout", type=int, default=90)
    p_probe.set_defaults(func=probe)

    p_manifest = sub.add_parser("manifest", help="Build a profile manifest for MLB Colab offload without remote execution.")
    p_manifest.add_argument("--profile", required=True, choices=["mlb-pregame", "mlb-postgame"])
    p_manifest.add_argument("--date", required=True, help="MLB date as YYYY-MM-DD or YYYYMMDD.")
    p_manifest.add_argument("--max-large-mb", type=int, default=50, help="Threshold for large-file reporting.")
    p_manifest.add_argument("--write", action="store_true", help="Write JSON and Markdown reports under postmortem/automation_logs/colab_offload.")
    p_manifest.set_defaults(func=manifest_command)

    p_package = sub.add_parser("package", help="Build a local Colab payload zip for a profile/date without remote execution.")
    p_package.add_argument("--profile", required=True, choices=["mlb-pregame", "mlb-postgame"])
    p_package.add_argument("--date", required=True, help="MLB date as YYYY-MM-DD or YYYYMMDD.")
    p_package.add_argument("--archive", help="Optional output zip path. Defaults under postmortem/automation_logs/colab_offload.")
    p_package.add_argument("--include", action="append", help="Additional safe repo-relative file to include.")
    p_package.add_argument("--include-code", action="store_true", help="Include all root Python files and core docs instead of the small probe file set.")
    p_package.add_argument("--max-large-mb", type=int, default=50)
    p_package.add_argument("--input-snapshot", help="Frozen pregame input snapshot JSON to include under .codex_payload/input_snapshots.")
    p_package.set_defaults(func=package_command)

    p_remote = sub.add_parser("remote-parity-run", help="Run an approved no-Notification remote stage and compare downloaded outputs.")
    p_remote.add_argument("--profile", required=True, choices=["mlb-pregame", "mlb-postgame"])
    p_remote.add_argument("--date", required=True, help="MLB date as YYYY-MM-DD or YYYYMMDD.")
    p_remote.add_argument(
        "--stage",
        required=True,
        choices=["env-smoke", "predict-only", "pregame-model-base", "pregame-guard-only", "postgame-deterministic"],
        help="Allowlisted remote stage. No Notification or production mutation.",
    )
    p_remote.add_argument("--session", help="Named Colab session. Defaults to a codex-parity-* name.")
    p_remote.add_argument("--gpu", default=None, choices=["T4", "L4", "G4", "H100", "A100"], help="Optional Colab GPU. Omit for CPU.")
    p_remote.add_argument("--max-large-mb", type=int, default=50)
    p_remote.add_argument("--command-timeout", type=int, default=900)
    p_remote.add_argument("--new-timeout", type=int, default=240)
    p_remote.add_argument("--io-timeout", type=int, default=300)
    p_remote.add_argument("--exec-timeout", type=int, default=960)
    p_remote.add_argument("--stop-timeout", type=int, default=120)
    p_remote.add_argument("--input-snapshot", help="Frozen pregame input snapshot JSON. Same file/hash is used for local reference and remote payload.")
    p_remote.add_argument("--refresh-local-ref", action="store_true", help="Run the same no-Notification stage locally before packaging, using the same input snapshot.")
    p_remote.add_argument("--dry-run", action="store_true", help="Build payload/script but do not create a Colab session.")
    p_remote.set_defaults(func=remote_parity_run_command)

    p_stage_check = sub.add_parser("stage-parity-check", help="Recompare downloaded remote stage outputs without rerunning Colab.")
    p_stage_check.add_argument("--date", required=True, help="MLB date as YYYY-MM-DD or YYYYMMDD.")
    p_stage_check.add_argument("--stage", required=True, choices=["env-smoke", "predict-only", "pregame-model-base", "pregame-guard-only", "postgame-deterministic"])
    p_stage_check.add_argument("--remote-root", required=True, help="Downloaded remote outputs root.")
    p_stage_check.add_argument("--write", action="store_true")
    p_stage_check.set_defaults(func=stage_parity_check_command)

    p_parity = sub.add_parser("parity-plan", help="Write the local-vs-remote output comparison plan for a profile/date.")
    p_parity.add_argument("--profile", required=True, choices=["mlb-pregame", "mlb-postgame"])
    p_parity.add_argument("--date", required=True, help="MLB date as YYYY-MM-DD or YYYYMMDD.")
    p_parity.add_argument("--write", action="store_true")
    p_parity.set_defaults(func=parity_plan_command)

    p_check = sub.add_parser("parity-check", help="Compare local outputs to a downloaded remote-output directory using a parity plan.")
    p_check.add_argument("--plan", required=True, help="Path to a parity-plan JSON file.")
    p_check.add_argument("--remote-root", required=True, help="Directory containing downloaded remote outputs at repo-relative paths.")
    p_check.add_argument("--local-root", default=str(ROOT), help="Local repo root to compare against. Defaults to <WORKSPACE>.")
    p_check.add_argument("--write", action="store_true")
    p_check.set_defaults(func=parity_check_command)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        return args.func(args)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode) from exc
    except ColabOffloadError as exc:
        print(f"[colab-offload] ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
