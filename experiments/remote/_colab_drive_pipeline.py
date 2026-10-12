# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from _colab_drive_api import DriveApi, DriveApiError, FOLDER_MIME


ROOT = Path(__file__).resolve().parent
LOCAL_DRIVE_ROOT = Path(
    os.environ.get("MLB_COLAB_DRIVE_LOCAL_ROOT")
    or r"<DRIVE_ROOT>\mlb-research-workspace"
)
REMOTE_DRIVE_ROOT = os.environ.get(
    "MLB_COLAB_DRIVE_REMOTE_ROOT",
    "<DRIVE_ROOT>/mlb-research-workspace",
)
DRIVE_ROOT_NAME = os.environ.get("MLB_COLAB_DRIVE_ROOT_NAME", "mlb-research-workspace")
LOG_ROOT = ROOT / "postmortem" / "automation_logs" / "colab_drive_pipeline"

STATE_PATHS = {
    "event_ledger": (
        ROOT / "postmortem" / "raw_pbp_event_ledger" / "mlb_raw_pbp_event_ledger.jsonl",
        Path("state/postmortem/raw_pbp_event_ledger/mlb_raw_pbp_event_ledger.jsonl"),
    ),
    "raw_plays": (
        ROOT / "_deepread_v3" / "raw_plays",
        Path("state/_deepread_v3/raw_plays"),
    ),
    "stage2_salience": (
        ROOT / "postmortem" / "raw_pbp_event_ledger" / "stage2_salience",
        Path("state/postmortem/raw_pbp_event_ledger/stage2_salience"),
    ),
}

RETROSPECTIVE_CODE = [
    "_colab_drive_api.py",
    "_mlb_directional_regular_season_retrospective.py",
    "_mlb_directional_regular_season_retrospective_validate.py",
    "_mlb_player_context_cards_build.py",
]

RETROSPECTIVE_OUTPUT_NAMES = [
    "regular_season_directional_retrospective_{season}.jsonl",
    "regular_season_directional_retrospective_{season}_games.csv",
    "regular_season_directional_retrospective_{season}_summary.json",
    "regular_season_directional_retrospective_{season}_summary.md",
    "regular_season_directional_retrospective_{season}_validate_report.json",
    "regular_season_directional_retrospective_{season}_validate_report.md",
]


class DrivePipelineError(RuntimeError):
    pass


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def win_path_to_wsl(path: Path) -> str:
    value = str(path.resolve())
    drive, tail = os.path.splitdrive(value)
    if not drive:
        return value.replace("\\", "/")
    return f"/mnt/{drive[0].lower()}/{tail.lstrip('\\/').replace('\\', '/')}"


def run_colab(args: list[str], *, timeout: int, check: bool = True) -> subprocess.CompletedProcess[str]:
    quoted = " ".join(shlex.quote(part) for part in args)
    shell = f"cd {shlex.quote(win_path_to_wsl(ROOT))} && colab --auth=adc {quoted}"
    print(f"[colab-drive] running: {shell}", flush=True)
    return subprocess.run(
        ["wsl", "-d", "Ubuntu", "--", "bash", "-lc", shell],
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=check,
    )


def acquire_adc_access_token() -> str:
    proc = subprocess.run(
        [
            "wsl",
            "-d",
            "Ubuntu",
            "--",
            "bash",
            "-lc",
            "gcloud auth application-default print-access-token",
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=60,
        check=True,
    )
    token = proc.stdout.strip()
    if not token or any(char.isspace() for char in token):
        raise DrivePipelineError("ADC returned an invalid Google access token.")
    return token


def _drive_file_record(row: dict, relative_path: str) -> dict:
    return {
        "id": str(row["id"]),
        "name": str(row["name"]),
        "relative_path": relative_path,
        "size": int(row.get("size") or 0),
        "md5Checksum": str(row.get("md5Checksum") or ""),
        "modifiedTime": str(row.get("modifiedTime") or ""),
    }


def _walk_drive_files(api: DriveApi, folder_id: str, relative_root: Path) -> list[dict]:
    rows: list[dict] = []
    for child in api.list_children(folder_id):
        relative = relative_root / str(child["name"])
        if child.get("mimeType") == FOLDER_MIME:
            rows.extend(_walk_drive_files(api, str(child["id"]), relative))
        else:
            rows.append(_drive_file_record(child, relative.as_posix()))
    return rows


def build_drive_api_manifest(api: DriveApi) -> dict:
    root = api.unique_child("root", DRIVE_ROOT_NAME, mime_type=FOLDER_MIME)
    root_id = str(root["id"])
    ledger = api.resolve_path(
        [
            "state",
            "postmortem",
            "raw_pbp_event_ledger",
            "mlb_raw_pbp_event_ledger.jsonl",
        ],
        start_id=root_id,
    )
    raw_archive = api.resolve_path(
        ["state", "_deepread_v3", "raw_plays.zip"],
        start_id=root_id,
    )
    salience_folder = api.resolve_path(
        ["state", "postmortem", "raw_pbp_event_ledger", "stage2_salience"],
        start_id=root_id,
    )
    salience = _walk_drive_files(
        api,
        str(salience_folder["id"]),
        Path("postmortem/raw_pbp_event_ledger/stage2_salience"),
    )
    return {
        "schema": "mlb_colab_drive_api_manifest.v1",
        "created_utc": utc_stamp(),
        "root": {"id": root_id, "name": DRIVE_ROOT_NAME},
        "inputs": {
            "event_ledger": _drive_file_record(
                ledger,
                "postmortem/raw_pbp_event_ledger/mlb_raw_pbp_event_ledger.jsonl",
            ),
            "raw_plays_archive": _drive_file_record(raw_archive, "raw_plays.zip"),
            "stage2_salience": salience,
        },
    }


def write_private_temp_file(*, prefix: str, suffix: str, body: str) -> Path:
    descriptor, raw_path = tempfile.mkstemp(prefix=prefix, suffix=suffix, text=True)
    path = Path(raw_path)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def start_colab_session(session: str, gpu: str | None, timeout: int) -> None:
    command = ["new", "-s", session]
    if gpu:
        command.extend(["--gpu", gpu])
    run_colab(command, timeout=timeout)


def path_stats(path: Path) -> dict:
    if not path.exists():
        return {"exists": False, "files": 0, "bytes": 0}
    if path.is_file():
        return {"exists": True, "files": 1, "bytes": path.stat().st_size}
    files = 0
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            files += 1
            total += child.stat().st_size
    return {"exists": True, "files": files, "bytes": total}


def copy_file_if_needed(source: Path, target: Path) -> str:
    if target.exists() and target.stat().st_size == source.stat().st_size:
        return "already_current_by_size"
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".part")
    if temp.exists():
        temp.unlink()
    shutil.copy2(source, temp)
    if temp.stat().st_size != source.stat().st_size:
        raise DrivePipelineError(f"Size mismatch after copy: {source} -> {temp}")
    os.replace(temp, target)
    return "copied"


def copy_tree_resumable(source: Path, target: Path) -> dict:
    copied = 0
    skipped = 0
    bytes_copied = 0
    for child in source.rglob("*"):
        if not child.is_file():
            continue
        relative = child.relative_to(source)
        destination = target / relative
        status = copy_file_if_needed(child, destination)
        if status == "copied":
            copied += 1
            bytes_copied += child.stat().st_size
        else:
            skipped += 1
    return {"copied": copied, "skipped": skipped, "bytes_copied": bytes_copied}


def build_stored_tree_archive(source: Path, target: Path) -> dict:
    expected_files = sum(1 for child in source.rglob("*") if child.is_file())
    if target.exists():
        try:
            with zipfile.ZipFile(target, "r") as archive:
                current_files = sum(1 for row in archive.infolist() if not row.is_dir())
            if current_files == expected_files:
                return {"status": "already_current_by_file_count", "files": current_files, "bytes": target.stat().st_size}
        except zipfile.BadZipFile:
            pass
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".part")
    if temp.exists():
        temp.unlink()
    count = 0
    with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for child in source.rglob("*"):
            if not child.is_file():
                continue
            archive.write(child, child.relative_to(source).as_posix())
            count += 1
    if count != expected_files:
        temp.unlink(missing_ok=True)
        raise DrivePipelineError(f"Raw-play archive count mismatch: {count} != {expected_files}")
    os.replace(temp, target)
    return {"status": "built", "files": count, "bytes": target.stat().st_size}


def command_status(_: argparse.Namespace) -> int:
    rows = []
    for name, (source, relative) in STATE_PATHS.items():
        rows.append(
            {
                "name": name,
                "source": str(source),
                "source_stats": path_stats(source),
                "drive": str(LOCAL_DRIVE_ROOT / relative),
                "drive_stats": path_stats(LOCAL_DRIVE_ROOT / relative),
            }
        )
    payload = {
        "schema": "mlb_colab_drive_status.v1",
        "local_drive_root": str(LOCAL_DRIVE_ROOT),
        "local_drive_available": LOCAL_DRIVE_ROOT.parent.exists(),
        "remote_drive_root": REMOTE_DRIVE_ROOT,
        "state": rows,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    run_colab(["sessions"], timeout=30, check=False)
    return 0


def command_seed(args: argparse.Namespace) -> int:
    LOCAL_DRIVE_ROOT.mkdir(parents=True, exist_ok=True)
    selected = ["event_ledger", "stage2_salience"]
    if args.include_raw_plays:
        selected.append("raw_plays")
    results = []
    for name in selected:
        source, relative = STATE_PATHS[name]
        if not source.exists():
            raise DrivePipelineError(f"Missing seed source: {source}")
        target = LOCAL_DRIVE_ROOT / relative
        started = time.time()
        if source.is_dir():
            detail = copy_tree_resumable(source, target)
        else:
            detail = {"status": copy_file_if_needed(source, target)}
        results.append(
            {
                "name": name,
                "source": str(source),
                "target": str(target),
                "duration_sec": round(time.time() - started, 3),
                "source_stats": path_stats(source),
                "target_stats": path_stats(target),
                "detail": detail,
            }
        )
    if args.include_raw_plays:
        source, relative = STATE_PATHS["raw_plays"]
        archive_target = LOCAL_DRIVE_ROOT / relative.parent / "raw_plays.zip"
        started = time.time()
        detail = build_stored_tree_archive(source, archive_target)
        results.append(
            {
                "name": "raw_plays_archive",
                "source": str(source),
                "target": str(archive_target),
                "duration_sec": round(time.time() - started, 3),
                "target_stats": path_stats(archive_target),
                "detail": detail,
            }
        )
    manifest = {
        "schema": "mlb_colab_drive_seed.v1",
        "created_utc": utc_stamp(),
        "local_drive_root": str(LOCAL_DRIVE_ROOT),
        "remote_drive_root": REMOTE_DRIVE_ROOT,
        "results": results,
    }
    manifest_path = LOCAL_DRIVE_ROOT / "state" / "seed_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


def command_new_session(args: argparse.Namespace) -> int:
    command = ["new", "-s", args.session]
    if args.gpu:
        command.extend(["--gpu", args.gpu])
    run_colab(command, timeout=args.timeout)
    print("[colab-drive] Session created. Google Drive still needs one interactive mount approval.")
    print(f"colab --auth=adc drivemount -s {args.session} /content/drive")
    return 0


def write_probe_script(path: Path) -> None:
    body = f'''from pathlib import Path
import json
root = Path({REMOTE_DRIVE_ROOT!r})
ledger = root / "state/postmortem/raw_pbp_event_ledger/mlb_raw_pbp_event_ledger.jsonl"
raw_root = root / "state/_deepread_v3/raw_plays"
raw_files = [p for p in raw_root.rglob("*.json") if p.is_file()] if raw_root.exists() else []
payload = {{
    "schema": "mlb_colab_drive_probe.v1",
    "drive_mount_exists": Path("/content/drive").exists(),
    "root_exists": root.exists(),
    "root": str(root),
    "state_exists": (root / "state").exists(),
    "event_ledger_exists": ledger.exists(),
    "event_ledger_bytes": ledger.stat().st_size if ledger.exists() else 0,
    "raw_plays_files": len(raw_files),
    "raw_plays_bytes": sum(p.stat().st_size for p in raw_files),
}}
print(json.dumps(payload, ensure_ascii=False, indent=2))
expected = payload["event_ledger_bytes"] == 1953136099 and payload["raw_plays_files"] == 1729 and payload["raw_plays_bytes"] == 1684535762
raise SystemExit(0 if payload["root_exists"] and expected else 2)
'''
    path.write_text(body, encoding="utf-8")


def command_probe(args: argparse.Namespace) -> int:
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    script = LOG_ROOT / f"drive_probe_{args.session}_{utc_stamp()}.py"
    write_probe_script(script)
    proc = run_colab(
        ["exec", "-s", args.session, "-f", win_path_to_wsl(script), "--timeout", str(args.exec_timeout)],
        timeout=args.exec_timeout + 60,
        check=False,
    )
    return proc.returncode


def write_api_probe_script(path: Path, *, token_remote: str, manifest_remote: str, stamp: str) -> None:
    body = f'''from __future__ import annotations
import json
from pathlib import Path

from _colab_drive_api import DriveApi

token_path = Path({token_remote!r})
manifest_path = Path({manifest_remote!r})
token = token_path.read_text(encoding="utf-8").strip()
token_path.unlink(missing_ok=True)
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
api = DriveApi(token)
ledger = manifest["inputs"]["event_ledger"]
response = api._request(
    "GET",
    f"https://www.googleapis.com/drive/v3/files/{{ledger['id']}}",
    params={{"alt": "media"}},
    headers={{"Range": "bytes=0-1023"}},
)
if len(response.content) != 1024:
    raise RuntimeError(f"Drive range probe returned {{len(response.content)}} bytes")
root_id = manifest["root"]["id"]
results = api.ensure_folder(root_id, "results")
probe_root = api.ensure_folder(results["id"], "api_probe")
payload = {{
    "schema": "mlb_colab_drive_api_probe.v1",
    "stamp": {stamp!r},
    "read_bytes": len(response.content),
    "ledger_size": ledger["size"],
    "ok": True,
}}
local = Path("/content/{stamp}_api_probe.json")
local.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
uploaded = api.upload_file(local, parent_id=probe_root["id"], name=f"{stamp}.json")
payload["uploaded_file_id"] = uploaded["id"]
print(json.dumps(payload, ensure_ascii=False, indent=2))
'''
    path.write_text(body, encoding="utf-8")


def command_api_probe(args: argparse.Namespace) -> int:
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = utc_stamp()
    session = args.session or f"mlb-drive-api-probe-{stamp.lower()}"
    token = acquire_adc_access_token()
    api = DriveApi(token)
    manifest = build_drive_api_manifest(api)
    token_file = write_private_temp_file(prefix="mlb_drive_token_", suffix=".txt", body=token)
    manifest_file = write_private_temp_file(
        prefix="mlb_drive_manifest_",
        suffix=".json",
        body=json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    script = LOG_ROOT / f"drive_api_probe_{stamp}.py"
    token_remote = f"/content/.{session}_drive_token"
    manifest_remote = f"/content/{session}_drive_manifest.json"
    helper_remote = "/content/_colab_drive_api.py"
    write_api_probe_script(
        script,
        token_remote=token_remote,
        manifest_remote=manifest_remote,
        stamp=stamp,
    )
    created = False
    uploaded_probe_id: str | None = None
    try:
        start_colab_session(session, args.gpu, args.session_timeout)
        created = True
        run_colab(["upload", "-s", session, win_path_to_wsl(ROOT / "_colab_drive_api.py"), helper_remote], timeout=args.io_timeout)
        run_colab(["upload", "-s", session, win_path_to_wsl(token_file), token_remote], timeout=args.io_timeout)
        run_colab(["upload", "-s", session, win_path_to_wsl(manifest_file), manifest_remote], timeout=args.io_timeout)
        proc = run_colab(
            ["exec", "-s", session, "-f", win_path_to_wsl(script), "--timeout", str(args.exec_timeout)],
            timeout=args.exec_timeout + 60,
            check=False,
        )
        if proc.returncode != 0:
            return proc.returncode
        root_id = str(manifest["root"]["id"])
        results = api.unique_child(root_id, "results", mime_type=FOLDER_MIME)
        probe_root = api.unique_child(str(results["id"]), "api_probe", mime_type=FOLDER_MIME)
        probe_file = api.unique_child(str(probe_root["id"]), f"{stamp}.json")
        uploaded_probe_id = str(probe_file["id"])
        payload = json.loads(api.download_bytes(uploaded_probe_id).decode("utf-8"))
        if not payload.get("ok") or int(payload.get("read_bytes") or 0) != 1024:
            raise DrivePipelineError(f"Remote Drive API probe payload failed: {payload}")
        print(json.dumps({"schema": "mlb_colab_drive_api_probe_local_verify.v1", "session": session, "remote": payload, "ok": True}, ensure_ascii=False, indent=2))
        return 0
    finally:
        if uploaded_probe_id and not args.keep_probe_file:
            try:
                api.delete_file(uploaded_probe_id)
            except Exception as exc:
                print(f"[colab-drive][WARN] Probe cleanup failed: {exc}", file=sys.stderr)
        token_file.unlink(missing_ok=True)
        manifest_file.unlink(missing_ok=True)
        if created and not args.keep_session:
            run_colab(["stop", "-s", session], timeout=60, check=False)


def build_code_archive(path: Path) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in RETROSPECTIVE_CODE:
            source = ROOT / relative
            if not source.exists():
                raise DrivePipelineError(f"Missing code dependency: {source}")
            archive.write(source, relative)
            rows.append({"path": relative, "bytes": source.stat().st_size, "sha256": file_sha256(source)})
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": file_sha256(path),
        "files": rows,
    }


def write_retrospective_remote_script(path: Path, *, session: str, season: int, archive_remote: str) -> None:
    body = f'''from __future__ import annotations
import json
import os
import shutil
import subprocess
import time
import traceback
import zipfile
from datetime import datetime, timezone
from pathlib import Path

drive_root = Path({REMOTE_DRIVE_ROOT!r})
season = {season!r}
archive_path = Path({archive_remote!r})
workspace = Path("/content/{session}_retrospective")
state_root = drive_root / "state"
source_ledger = state_root / "postmortem/raw_pbp_event_ledger/mlb_raw_pbp_event_ledger.jsonl"
source_raw_plays = state_root / "_deepread_v3/raw_plays"
source_raw_archive = state_root / "_deepread_v3/raw_plays.zip"
source_salience = state_root / "postmortem/raw_pbp_event_ledger/stage2_salience"
stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
drive_result_root = drive_root / "results" / f"retrospective_{{season}}" / stamp
local_ledger = workspace / "postmortem/raw_pbp_event_ledger/mlb_raw_pbp_event_ledger.jsonl"
local_raw_plays = workspace / "_deepread_v3/raw_plays"
local_salience = workspace / "postmortem/raw_pbp_event_ledger/stage2_salience"
local_output = workspace / f"postmortem/raw_pbp_fullflow/regular_season_{{season}}_directional"

started = time.time()
result = {{"schema": "mlb_colab_drive_retrospective.v1", "season": season, "ok": False}}
try:
    required = [drive_root, archive_path, source_ledger]
    missing = [str(p) for p in required if not p.exists()]
    if not source_raw_archive.exists() and not source_raw_plays.exists():
        missing.append(f"{{source_raw_archive}} or {{source_raw_plays}}")
    if missing:
        raise RuntimeError(f"Missing Drive inputs: {{missing}}")
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    with zipfile.ZipFile(archive_path, "r") as archive:
        archive.extractall(workspace)
    local_ledger.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_ledger, local_ledger)
    if source_raw_archive.exists():
        local_archive = workspace / "raw_plays.zip"
        shutil.copy2(source_raw_archive, local_archive)
        local_raw_plays.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(local_archive, "r") as raw_archive:
            raw_archive.extractall(local_raw_plays)
    else:
        shutil.copytree(source_raw_plays, local_raw_plays)
    if source_salience.exists():
        shutil.copytree(source_salience, local_salience)

    env = os.environ.copy()
    env.update({{
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "MLB_RAW_PBP_ROOT": str(local_raw_plays),
        "NARR_HELD_ALERT_NOTIFICATION": "0",
        "MLB_COLAB_PARITY_ONLY": "1",
    }})
    commands = [
        ["python", "_mlb_directional_regular_season_retrospective.py", "--season", str(season), "--ledger-path", str(local_ledger), "--out-root", str(local_output), "--write"],
        ["python", "_mlb_directional_regular_season_retrospective_validate.py", "--season", str(season), "--ledger-path", str(local_ledger), "--out-root", str(local_output), "--write"],
    ]
    command_rows = []
    for command in commands:
        t0 = time.time()
        proc = subprocess.run(command, cwd=workspace, env=env, text=True, encoding="utf-8", errors="replace", capture_output=True)
        command_rows.append({{
            "cmd": command,
            "returncode": proc.returncode,
            "duration_sec": round(time.time() - t0, 3),
            "stdout_tail": proc.stdout[-5000:],
            "stderr_tail": proc.stderr[-5000:],
        }})
        if proc.returncode != 0:
            break
    report_path = local_output / f"regular_season_directional_retrospective_{{season}}_validate_report.json"
    validation = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {{}}
    ok = len(command_rows) == len(commands) and all(row["returncode"] == 0 for row in command_rows) and bool(validation.get("validation_ok"))
    result.update({{
        "ok": ok,
        "commands": command_rows,
        "validation_ok": validation.get("validation_ok"),
        "validation_games": validation.get("output_games"),
        "duration_sec": round(time.time() - started, 3),
    }})
    if not ok:
        raise RuntimeError("Retrospective build or validation failed")
    drive_result_root.mkdir(parents=True, exist_ok=False)
    shutil.copytree(local_output, drive_result_root / "output")
    result["drive_result_root"] = str(drive_result_root)
    (drive_result_root / "RUN_RESULT.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
    latest = {{"schema": "mlb_colab_drive_latest.v1", "season": season, "result_root": str(drive_result_root), "validation_ok": True, "updated_utc": stamp}}
    latest_path = drive_root / "results" / f"retrospective_{{season}}" / "LATEST.json"
    latest_path.write_text(json.dumps(latest, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
except Exception as exc:
    result.update({{
        "ok": False,
        "error": f"{{type(exc).__name__}}: {{exc}}",
        "traceback_tail": traceback.format_exc()[-5000:],
        "duration_sec": round(time.time() - started, 3),
    }})
    failure_root = drive_root / "results" / f"retrospective_{{season}}" / "failures"
    failure_root.mkdir(parents=True, exist_ok=True)
    (failure_root / f"{{stamp}}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")

print(json.dumps(result, ensure_ascii=False, indent=2))
raise SystemExit(0 if result.get("ok") else 1)
'''
    path.write_text(body, encoding="utf-8")


def write_retrospective_api_remote_script(
    path: Path,
    *,
    session: str,
    season: int,
    stamp: str,
    archive_remote: str,
    token_remote: str,
    manifest_remote: str,
) -> None:
    body = f'''from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import zipfile
from pathlib import Path

season = {season!r}
stamp = {stamp!r}
archive_path = Path({archive_remote!r})
token_path = Path({token_remote!r})
manifest_path = Path({manifest_remote!r})
workspace = Path("/content/{session}_retrospective")
local_ledger = workspace / "postmortem/raw_pbp_event_ledger/mlb_raw_pbp_event_ledger.jsonl"
local_raw_archive = workspace / "raw_plays.zip"
local_raw_plays = workspace / "_deepread_v3/raw_plays"
local_output = workspace / f"postmortem/raw_pbp_fullflow/regular_season_{{season}}_directional"
started = time.time()
result = {{
    "schema": "mlb_colab_drive_api_retrospective.v1",
    "season": season,
    "stamp": stamp,
    "ok": False,
}}
api = None
manifest = None
try:
    token = token_path.read_text(encoding="utf-8").strip()
    token_path.unlink(missing_ok=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    with zipfile.ZipFile(archive_path, "r") as archive:
        archive.extractall(workspace)
    sys.path.insert(0, str(workspace))
    from _colab_drive_api import DriveApi
    api = DriveApi(token)

    inputs = manifest["inputs"]
    downloads = []
    downloads.append(api.download_file(inputs["event_ledger"]["id"], local_ledger, expected=inputs["event_ledger"]))
    downloads.append(api.download_file(inputs["raw_plays_archive"]["id"], local_raw_archive, expected=inputs["raw_plays_archive"]))
    for row in inputs.get("stage2_salience") or []:
        downloads.append(api.download_file(row["id"], workspace / row["relative_path"], expected=row))
    local_raw_plays.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(local_raw_archive, "r") as raw_archive:
        raw_archive.extractall(local_raw_plays)

    env = os.environ.copy()
    env.update({{
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "MLB_RAW_PBP_ROOT": str(local_raw_plays),
        "NARR_HELD_ALERT_NOTIFICATION": "0",
        "MLB_COLAB_PARITY_ONLY": "1",
    }})
    commands = [
        ["python", "_mlb_directional_regular_season_retrospective.py", "--season", str(season), "--ledger-path", str(local_ledger), "--out-root", str(local_output), "--write"],
        ["python", "_mlb_directional_regular_season_retrospective_validate.py", "--season", str(season), "--ledger-path", str(local_ledger), "--out-root", str(local_output), "--write"],
    ]
    command_rows = []
    for command in commands:
        t0 = time.time()
        proc = subprocess.run(command, cwd=workspace, env=env, text=True, encoding="utf-8", errors="replace", capture_output=True)
        command_rows.append({{
            "cmd": command,
            "returncode": proc.returncode,
            "duration_sec": round(time.time() - t0, 3),
            "stdout_tail": proc.stdout[-5000:],
            "stderr_tail": proc.stderr[-5000:],
        }})
        if proc.returncode != 0:
            break
    report_path = local_output / f"regular_season_directional_retrospective_{{season}}_validate_report.json"
    validation = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {{}}
    ok = len(command_rows) == len(commands) and all(row["returncode"] == 0 for row in command_rows) and bool(validation.get("validation_ok"))
    result.update({{
        "ok": ok,
        "downloads": downloads,
        "commands": command_rows,
        "validation_ok": validation.get("validation_ok"),
        "validation_games": validation.get("output_games"),
        "duration_sec": round(time.time() - started, 3),
    }})
    if not ok:
        raise RuntimeError("Retrospective build or validation failed")

    root_id = manifest["root"]["id"]
    results_folder = api.ensure_folder(root_id, "results")
    family_folder = api.ensure_folder(results_folder["id"], f"retrospective_{{season}}")
    stamp_folder = api.ensure_folder(family_folder["id"], stamp)
    output_folder = api.ensure_folder(stamp_folder["id"], "output")
    uploaded = []
    for source in sorted(local_output.iterdir()):
        if source.is_file():
            uploaded.append(api.upload_file(source, parent_id=output_folder["id"]))
    result.update({{
        "drive_result_root": f"results/retrospective_{{season}}/{{stamp}}",
        "drive_result_folder_id": stamp_folder["id"],
        "drive_output_folder_id": output_folder["id"],
        "uploaded": uploaded,
        "duration_sec": round(time.time() - started, 3),
    }})
    run_result_path = workspace / "RUN_RESULT.json"
    run_result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
    api.upload_file(run_result_path, parent_id=stamp_folder["id"])

    latest = {{
        "schema": "mlb_colab_drive_latest.v2",
        "season": season,
        "result_root": result["drive_result_root"],
        "result_folder_id": stamp_folder["id"],
        "output_folder_id": output_folder["id"],
        "validation_ok": True,
        "updated_utc": stamp,
    }}
    latest_path = workspace / "LATEST.json"
    latest_path.write_text(json.dumps(latest, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
    existing = api.list_children(family_folder["id"], name="LATEST.json")
    if len(existing) > 1:
        raise RuntimeError("Duplicate retrospective LATEST.json files")
    api.upload_file(
        latest_path,
        parent_id=family_folder["id"] if not existing else None,
        existing_id=existing[0]["id"] if existing else None,
        name="LATEST.json",
    )
except Exception as exc:
    result.update({{
        "ok": False,
        "error": f"{{type(exc).__name__}}: {{exc}}",
        "traceback_tail": traceback.format_exc()[-5000:],
        "duration_sec": round(time.time() - started, 3),
    }})
    if api is not None and manifest is not None:
        try:
            root_id = manifest["root"]["id"]
            results_folder = api.ensure_folder(root_id, "results")
            family_folder = api.ensure_folder(results_folder["id"], f"retrospective_{{season}}")
            failure_folder = api.ensure_folder(family_folder["id"], "failures")
            failure_path = workspace / f"{{stamp}}.json"
            failure_path.parent.mkdir(parents=True, exist_ok=True)
            failure_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
            api.upload_file(failure_path, parent_id=failure_folder["id"])
        except Exception as upload_exc:
            result["failure_upload_error"] = f"{{type(upload_exc).__name__}}: {{upload_exc}}"
finally:
    token_path.unlink(missing_ok=True)

print(json.dumps(result, ensure_ascii=False, indent=2))
raise SystemExit(0 if result.get("ok") else 1)
'''
    path.write_text(body, encoding="utf-8")


def command_retrospective(args: argparse.Namespace) -> int:
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = utc_stamp()
    archive = LOG_ROOT / f"retrospective_code_{stamp}.zip"
    script = LOG_ROOT / f"retrospective_remote_{args.session}_{stamp}.py"
    archive_meta = build_code_archive(archive)
    remote_archive = f"/content/{args.session}_retrospective_code.zip"
    write_retrospective_remote_script(
        script,
        session=args.session,
        season=args.season,
        archive_remote=remote_archive,
    )
    print(json.dumps({"code_archive": archive_meta, "remote_drive_root": REMOTE_DRIVE_ROOT}, ensure_ascii=False, indent=2))
    run_colab(["upload", "-s", args.session, win_path_to_wsl(archive), remote_archive], timeout=args.io_timeout)
    proc = run_colab(
        ["exec", "-s", args.session, "-f", win_path_to_wsl(script), "--timeout", str(args.exec_timeout)],
        timeout=args.exec_timeout + 120,
        check=False,
    )
    if args.stop_after:
        run_colab(["stop", "-s", args.session], timeout=60, check=False)
    return proc.returncode


def command_api_retrospective(args: argparse.Namespace) -> int:
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = utc_stamp()
    session = args.session or f"mlb-drive-api-{stamp.lower()}"
    token = acquire_adc_access_token()
    api = DriveApi(token)
    manifest = build_drive_api_manifest(api)
    archive = LOG_ROOT / f"retrospective_api_code_{stamp}.zip"
    script = LOG_ROOT / f"retrospective_api_remote_{session}_{stamp}.py"
    archive_meta = build_code_archive(archive)
    remote_archive = f"/content/{session}_retrospective_code.zip"
    token_remote = f"/content/.{session}_drive_token"
    manifest_remote = f"/content/{session}_drive_manifest.json"
    token_file = write_private_temp_file(prefix="mlb_drive_token_", suffix=".txt", body=token)
    manifest_file = write_private_temp_file(
        prefix="mlb_drive_manifest_",
        suffix=".json",
        body=json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    write_retrospective_api_remote_script(
        script,
        session=session,
        season=args.season,
        stamp=stamp,
        archive_remote=remote_archive,
        token_remote=token_remote,
        manifest_remote=manifest_remote,
    )
    safe_manifest = json.loads(json.dumps(manifest))
    print(
        json.dumps(
            {
                "schema": "mlb_colab_drive_api_launch.v1",
                "session": session,
                "season": args.season,
                "stamp": stamp,
                "code_archive": archive_meta,
                "drive_manifest": safe_manifest,
                "token_persisted": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    created = False
    try:
        start_colab_session(session, args.gpu, args.session_timeout)
        created = True
        run_colab(["upload", "-s", session, win_path_to_wsl(archive), remote_archive], timeout=args.io_timeout)
        run_colab(["upload", "-s", session, win_path_to_wsl(token_file), token_remote], timeout=args.io_timeout)
        run_colab(["upload", "-s", session, win_path_to_wsl(manifest_file), manifest_remote], timeout=args.io_timeout)
        proc = run_colab(
            ["exec", "-s", session, "-f", win_path_to_wsl(script), "--timeout", str(args.exec_timeout)],
            timeout=args.exec_timeout + 120,
            check=False,
        )
        if proc.returncode != 0:
            return proc.returncode

        root_id = str(manifest["root"]["id"])
        results = api.unique_child(root_id, "results", mime_type=FOLDER_MIME)
        family = api.unique_child(str(results["id"]), f"retrospective_{args.season}", mime_type=FOLDER_MIME)
        stamp_folder = api.unique_child(str(family["id"]), stamp, mime_type=FOLDER_MIME)
        output = api.unique_child(str(stamp_folder["id"]), "output", mime_type=FOLDER_MIME)
        report_name = f"regular_season_directional_retrospective_{args.season}_validate_report.json"
        report_file = api.unique_child(str(output["id"]), report_name)
        report = json.loads(api.download_bytes(str(report_file["id"])).decode("utf-8"))
        required = {
            "validation_ok": True,
            "error_count": 0,
            "warning_count": 0,
            "raw_api_missing_items": 0,
        }
        failures = {key: {"actual": report.get(key), "expected": expected} for key, expected in required.items() if report.get(key) != expected}
        if failures:
            raise DrivePipelineError(f"Remote API retrospective validation gate failed: {failures}")
        print(
            json.dumps(
                {
                    "schema": "mlb_colab_drive_api_remote_verify.v1",
                    "session": session,
                    "stamp": stamp,
                    "output_folder_id": output["id"],
                    "validation": {key: report.get(key) for key in ["validation_ok", "source_rows", "output_rows", "output_games", "error_count", "warning_count", "raw_api_missing_items"]},
                    "ok": True,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if args.promote:
            command_api_promote_retrospective(argparse.Namespace(season=args.season))
        return 0
    finally:
        token_file.unlink(missing_ok=True)
        manifest_file.unlink(missing_ok=True)
        if created and not args.keep_session:
            run_colab(["stop", "-s", session], timeout=60, check=False)


def command_promote_retrospective(args: argparse.Namespace) -> int:
    season = args.season
    result_family = LOCAL_DRIVE_ROOT / "results" / f"retrospective_{season}"
    latest_path = result_family / "LATEST.json"
    if not latest_path.exists():
        raise DrivePipelineError(f"Drive latest pointer is missing: {latest_path}")
    latest = json.loads(latest_path.read_text(encoding="utf-8"))
    if not latest.get("validation_ok"):
        raise DrivePipelineError(f"Drive latest pointer is not validated: {latest}")
    stamp = str(latest.get("updated_utc") or "").strip()
    drive_output = result_family / stamp / "output"
    report_name = f"regular_season_directional_retrospective_{season}_validate_report.json"
    report_path = drive_output / report_name
    if not report_path.exists():
        raise DrivePipelineError(f"Validated Drive report is missing: {report_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    required_report = {
        "validation_ok": True,
        "error_count": 0,
        "warning_count": 0,
        "raw_api_missing_items": 0,
    }
    for key, expected in required_report.items():
        if report.get(key) != expected:
            raise DrivePipelineError(f"Drive report failed promotion gate {key}: {report.get(key)!r} != {expected!r}")
    if int(report.get("source_rows") or 0) != int(report.get("output_rows") or -1):
        raise DrivePipelineError("Drive report source/output coverage differs.")

    names = [name.format(season=season) for name in RETROSPECTIVE_OUTPUT_NAMES]
    missing = [name for name in names if not (drive_output / name).is_file()]
    if missing:
        raise DrivePipelineError(f"Drive output files are missing: {missing}")

    local_output = ROOT / "postmortem" / "raw_pbp_fullflow" / f"regular_season_{season}_directional"
    staging = local_output.parent / f".{local_output.name}.colab_stage_{stamp}"
    backup = local_output / "backups" / f"pre_colab_{stamp}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    for name in names:
        shutil.copy2(drive_output / name, staging / name)

    staged_report = json.loads((staging / report_name).read_text(encoding="utf-8"))
    if not staged_report.get("validation_ok"):
        shutil.rmtree(staging)
        raise DrivePipelineError("Staged report lost validation_ok during Drive sync copy.")
    jsonl_name = f"regular_season_directional_retrospective_{season}.jsonl"
    with (staging / jsonl_name).open("rb") as fh:
        staged_rows = sum(1 for line in fh if line.strip())
    expected_rows = int(staged_report.get("output_rows") or 0)
    if staged_rows != expected_rows:
        shutil.rmtree(staging)
        raise DrivePipelineError(f"Staged JSONL row count mismatch: {staged_rows} != {expected_rows}")

    backup.mkdir(parents=True, exist_ok=False)
    moved_old: list[tuple[Path, Path]] = []
    promoted: list[Path] = []
    try:
        for name in names:
            target = local_output / name
            if target.exists():
                old = backup / name
                os.replace(target, old)
                moved_old.append((old, target))
        for name in names:
            target = local_output / name
            os.replace(staging / name, target)
            promoted.append(target)
    except Exception:
        for target in promoted:
            target.unlink(missing_ok=True)
        for old, target in reversed(moved_old):
            if old.exists():
                os.replace(old, target)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)

    promotion = {
        "schema": "mlb_colab_drive_retrospective_promotion.v1",
        "promoted_utc": utc_stamp(),
        "season": season,
        "drive_output": str(drive_output),
        "local_output": str(local_output),
        "backup": str(backup),
        "rows": staged_rows,
        "validation_ok": True,
        "files": names,
    }
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    promotion_path = LOG_ROOT / f"retrospective_promotion_{season}_{stamp}.json"
    promotion_path.write_text(json.dumps(promotion, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(promotion, ensure_ascii=False, indent=2))
    return 0


def command_api_promote_retrospective(args: argparse.Namespace) -> int:
    season = args.season
    token = acquire_adc_access_token()
    api = DriveApi(token)
    manifest = build_drive_api_manifest(api)
    root_id = str(manifest["root"]["id"])
    results = api.unique_child(root_id, "results", mime_type=FOLDER_MIME)
    family = api.unique_child(str(results["id"]), f"retrospective_{season}", mime_type=FOLDER_MIME)
    latest_file = api.unique_child(str(family["id"]), "LATEST.json")
    latest = json.loads(api.download_bytes(str(latest_file["id"])).decode("utf-8"))
    if latest.get("validation_ok") is not True or int(latest.get("season") or 0) != season:
        raise DrivePipelineError(f"Drive API latest pointer is not promotable: {latest}")
    stamp = str(latest.get("updated_utc") or "").strip()
    output_folder_id = str(latest.get("output_folder_id") or "").strip()
    if not stamp or not output_folder_id:
        raise DrivePipelineError("Drive API latest pointer lacks stamp/output_folder_id.")

    names = [name.format(season=season) for name in RETROSPECTIVE_OUTPUT_NAMES]
    children = api.list_children(output_folder_id)
    by_name: dict[str, dict] = {}
    duplicates: list[str] = []
    for row in children:
        name = str(row.get("name") or "")
        if name in by_name:
            duplicates.append(name)
        by_name[name] = row
    if duplicates:
        raise DrivePipelineError(f"Duplicate Drive API output names: {sorted(set(duplicates))}")
    missing = [name for name in names if name not in by_name]
    if missing:
        raise DrivePipelineError(f"Drive API output files are missing: {missing}")

    local_output = ROOT / "postmortem" / "raw_pbp_fullflow" / f"regular_season_{season}_directional"
    staging = local_output.parent / f".{local_output.name}.colab_api_stage_{stamp}"
    backup = local_output / "backups" / f"pre_colab_api_{stamp}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        downloads = []
        for name in names:
            row = by_name[name]
            downloads.append(api.download_file(str(row["id"]), staging / name, expected=row))

        report_name = f"regular_season_directional_retrospective_{season}_validate_report.json"
        report = json.loads((staging / report_name).read_text(encoding="utf-8"))
        required_report = {
            "validation_ok": True,
            "error_count": 0,
            "warning_count": 0,
            "raw_api_missing_items": 0,
        }
        for key, expected in required_report.items():
            if report.get(key) != expected:
                raise DrivePipelineError(f"Downloaded report failed promotion gate {key}: {report.get(key)!r} != {expected!r}")
        if int(report.get("source_rows") or 0) != int(report.get("output_rows") or -1):
            raise DrivePipelineError("Downloaded report source/output coverage differs.")

        jsonl_name = f"regular_season_directional_retrospective_{season}.jsonl"
        with (staging / jsonl_name).open("rb") as handle:
            staged_rows = sum(1 for line in handle if line.strip())
        expected_rows = int(report.get("output_rows") or 0)
        if staged_rows != expected_rows:
            raise DrivePipelineError(f"Downloaded JSONL row count mismatch: {staged_rows} != {expected_rows}")

        backup.mkdir(parents=True, exist_ok=False)
        moved_old: list[tuple[Path, Path]] = []
        promoted: list[Path] = []
        try:
            for name in names:
                target = local_output / name
                if target.exists():
                    old = backup / name
                    os.replace(target, old)
                    moved_old.append((old, target))
            for name in names:
                target = local_output / name
                os.replace(staging / name, target)
                promoted.append(target)
        except Exception:
            for target in promoted:
                target.unlink(missing_ok=True)
            for old, target in reversed(moved_old):
                if old.exists():
                    os.replace(old, target)
            raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)

    promotion = {
        "schema": "mlb_colab_drive_api_retrospective_promotion.v1",
        "promoted_utc": utc_stamp(),
        "season": season,
        "drive_output_folder_id": output_folder_id,
        "local_output": str(local_output),
        "backup": str(backup),
        "rows": staged_rows,
        "validation_ok": True,
        "downloads": downloads,
        "files": names,
    }
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    promotion_path = LOG_ROOT / f"retrospective_api_promotion_{season}_{stamp}.json"
    promotion_path.write_text(json.dumps(promotion, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(promotion, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Operate MLB heavy deterministic stages through Colab and Google Drive.")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status")
    status.set_defaults(func=command_status)

    seed = sub.add_parser("seed")
    seed.add_argument("--include-raw-plays", action="store_true")
    seed.set_defaults(func=command_seed)

    new = sub.add_parser("new-session")
    new.add_argument("--session", required=True)
    new.add_argument("--gpu", choices=["T4", "L4", "G4", "H100", "A100"])
    new.add_argument("--timeout", type=int, default=180)
    new.set_defaults(func=command_new_session)

    probe = sub.add_parser("probe")
    probe.add_argument("--session", required=True)
    probe.add_argument("--exec-timeout", type=int, default=60)
    probe.set_defaults(func=command_probe)

    api_probe = sub.add_parser("api-probe")
    api_probe.add_argument("--session")
    api_probe.add_argument("--gpu", choices=["T4", "L4", "G4", "H100", "A100"])
    api_probe.add_argument("--session-timeout", type=int, default=180)
    api_probe.add_argument("--io-timeout", type=int, default=600)
    api_probe.add_argument("--exec-timeout", type=int, default=180)
    api_probe.add_argument("--keep-session", action="store_true")
    api_probe.add_argument("--keep-probe-file", action="store_true")
    api_probe.set_defaults(func=command_api_probe)

    retrospective = sub.add_parser("retrospective")
    retrospective.add_argument("--session", required=True)
    retrospective.add_argument("--season", type=int, default=2026)
    retrospective.add_argument("--io-timeout", type=int, default=600)
    retrospective.add_argument("--exec-timeout", type=int, default=7200)
    retrospective.add_argument("--stop-after", action="store_true")
    retrospective.set_defaults(func=command_retrospective)

    api_retrospective = sub.add_parser("api-retrospective")
    api_retrospective.add_argument("--session")
    api_retrospective.add_argument("--season", type=int, default=2026)
    api_retrospective.add_argument("--gpu", choices=["T4", "L4", "G4", "H100", "A100"])
    api_retrospective.add_argument("--session-timeout", type=int, default=180)
    api_retrospective.add_argument("--io-timeout", type=int, default=600)
    api_retrospective.add_argument("--exec-timeout", type=int, default=7200)
    api_retrospective.add_argument("--keep-session", action="store_true")
    api_retrospective.add_argument("--promote", action="store_true")
    api_retrospective.set_defaults(func=command_api_retrospective)

    promote = sub.add_parser("promote-retrospective")
    promote.add_argument("--season", type=int, default=2026)
    promote.set_defaults(func=command_promote_retrospective)

    api_promote = sub.add_parser("api-promote-retrospective")
    api_promote.add_argument("--season", type=int, default=2026)
    api_promote.set_defaults(func=command_api_promote_retrospective)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.func(args))
    except (DrivePipelineError, DriveApiError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"[colab-drive][ERR] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
