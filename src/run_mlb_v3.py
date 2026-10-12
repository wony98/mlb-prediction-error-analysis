# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
'MLB v3 연구용 코드 발췌본. 고정된 경기 전 입력으로 모델 확률·예상 총점을 계산하고 근거를 파일로 보관한다. 실시간 시장가격 수집과 송신·금액 계산은 포함하지 않는다. 전체 실행 환경과 의존성은 별도로 필요하다.'
import sys, io, json, argparse, time, re, asyncio, os, hashlib, math
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests
import numpy as np
import pandas as pd
from _mlb_career_era import career_stats_from_splits
import _mlb_replay_asof as _REPLAY_ASOF
import _narr_filter_lib as _flt   # 기존 근거 필터 모듈 참조.
from _mlb_result_status import fetch_mlb_result_status_meta
from _mlb_send_owner_lock import SendLockError, send_lock_context

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

MLB_API = "https://statsapi.mlb.com/api/v1"

ap = argparse.ArgumentParser()
ap.add_argument('--date', default=None, help='미국 경기일 YYYY-MM-DD. 생략 시 코드의 UTC-4 날짜 계산을 사용한다.')
ap.add_argument('--th', type=float, default=0.08)
ap.add_argument('--no-notification', action='store_true')
ap.add_argument('--codex-narrator', action='store_true', help='Use Codex/GPT file bridge for narrator reasoning. First run writes _codex_gpt_prompt_DATE.md and exits; rerun after _codex_gpt_reasoning_DATE.txt exists.')
ap.add_argument('--c3-prepare', action='store_true', help='Prepare v3/guard inputs for per-game C3 reasoning without building the legacy giant narrator prompt. Requires --no-notification.')
ap.add_argument('--preview', action='store_true', help='과거 호출 호환용 옵션. 원래 송신 UI는 이 발췌본에 포함하지 않는다.')
ap.add_argument('--result', action='store_true', help='저장된 경기 표에 공식 경기 결과를 대조한다.')
ap.add_argument('--pregame-replay', action='store_true', help='경기 상태·결과 열을 정리해 사전 입력 replay를 허용한다. strict as-of 설정과는 별개다.')
ap.add_argument('--include-pending-results', action='store_true', help='결과 처리에서 아직 끝나지 않은 경기의 대기 행도 포함한다.')
ap.add_argument('--resend-results', action='store_true', help='기존 처리 기록이 있는 Final 경기도 다시 포함한다. 발췌본은 외부 송신을 하지 않는다.')
ap.add_argument('--send-gamepks', default='', help='과거 옵션명 유지. 결과 표 대상 gamePk를 쉼표로 제한한다.')
ap.add_argument('--gamepks', default='', help='Analyze only these comma-separated gamePk values before model/guard/Notification.')
ap.add_argument('--ready-only', action='store_true', help='Analyze only gamePks marked ready in _mlb_pregame_readiness_DATE.json.')
ap.add_argument('--model-notification-only', action='store_true', help='Unattended/cron path: hard-skip all narrator reasoning (build_reasoning/codex handoff/claude -p). Builds NO new _narr_predictions. Notification sends model table + any already-frozen narrator rows. Owner decision B: cron=model+notification only, narrator predictions=semi-auto.')
ap.add_argument('--allow-fallback-lineup', action='store_true', help='Allow sending predictions built on unconfirmed (fallback) lineups. Default off: pregame send drops rows whose home/away lineup names are empty (names-present hard gate). For forced_partial / manual exceptions only.')
ap.add_argument('--input-snapshot', default='', help='Frozen pregame input snapshot JSON. When supplied, volatile live fetches such as OddsPortal are read from this file and not re-fetched.')
ap.add_argument('--no-prediction-freeze', action='store_true', help='Research/repair only: recompute predictions from the supplied frozen inputs instead of restoring prior confirmed-lineup predictions.')
A, _ = ap.parse_known_args()  # 다른 호출자의 추가 인자는 여기서 해석하지 않는다.
if A.c3_prepare and not A.no_notification:
    ap.error('--c3-prepare requires --no-notification')
# codex provider 활성 판정은 env까지 본다(historical-note 블로커1): --codex-narrator 없이
# NARR_REASONING_PROVIDER=codex env로 켜는 운영 경로에서도 partial scope id가 잡혀야 한다.
_cn_provider = (os.environ.get('NARR_REASONING_PROVIDER') or '').strip().lower()
_cn_codex_active = A.codex_narrator or _cn_provider in {'codex', 'gpt', 'codex-file', 'gpt-file'}
if A.codex_narrator:
    os.environ['NARR_REASONING_PROVIDER'] = 'codex'
# 부분 실행(--gamepks) + codex active면 bridge 파일을 scope별로 분리(historical-note A-lite).
# 같은 gamepks set은 같은 scope id → 1차(prompt 생성)/2차(reasoning 소비)가 같은 파일을 가리킴.
if _cn_codex_active:
    _cn_gp = [x.strip() for x in str(A.gamepks or '').replace(';', ',').split(',') if x.strip()]
    if _cn_gp:
        import hashlib as _hl
        os.environ['NARR_CODEX_SCOPE_ID'] = 'g' + _hl.sha1(','.join(sorted(_cn_gp)).encode('utf-8')).hexdigest()[:8]
        os.environ['NARR_CODEX_GAMEPKS'] = ','.join(_cn_gp)  # handoff rerun 안내가 같은 --gamepks를 출력하도록

_et = datetime.now(timezone.utc) - timedelta(hours=4)
DATE = A.date if A.date else str(_et.date())
print(f'MLB v3 분석: {DATE} (th={A.th})')

V3_OUTPUT_DIR = Path(
    os.environ.get('MLB_V3_OUTPUT_DIR') or Path(__file__).resolve().parent
).resolve()


def v3_games_path(date):
    return V3_OUTPUT_DIR / f'_v3_games_{date}.csv'


POSTGAME_LEAK_COL_RE = re.compile(
    r"(home_score|away_score|linescore|winner|result|actual|outcome|settle|final_score|"
    r"abstractGameState|codedGameState|inning|current_inning)",
    re.I,
)


def sanitize_pregame_replay(df):
    """Drop live/result fields so post-run analysis uses only pregame inputs."""
    out = df.copy()
    drop_cols = [c for c in out.columns if POSTGAME_LEAK_COL_RE.search(str(c))]
    if drop_cols:
        out = out.drop(columns=drop_cols)
    if 'status' in out.columns:
        out['status'] = 'Pregame-Replay'
    out['pregame_replay'] = True
    return out


def assert_pregame_status(df, allow_replay=False):
    """Block live pregame analysis once any target game has started."""
    if allow_replay:
        return
    if 'status' not in df.columns:
        return
    allowed = {'', 'scheduled', 'pre-game', 'preview', 'warmup'}
    bad_rows = []
    for _, row in df.iterrows():
        status = str(row.get('status') or '').strip()
        if status.lower() not in allowed:
            bad_rows.append(
                f"{row.get('away_team', '?')} @ {row.get('home_team', '?')} status={status}"
            )
    if bad_rows:
        joined = '; '.join(bad_rows)
        raise RuntimeError(
            "pregame analysis blocked: live/final game status detected. "
            f"Use --pregame-replay for sanitized pregame replay, --result for postgame mode, "
            f"or rerun before first pitch. {joined}"
        )


def has_non_pregame_status(df):
    if 'status' not in df.columns:
        return False
    allowed = {'', 'scheduled', 'pre-game', 'preview', 'warmup', 'pregame-replay'}
    return any(str(v or '').strip().lower() not in allowed for v in df['status'])


def selected_send_gamepks():
    vals = []
    for x in str(getattr(A, 'send_gamepks', '') or '').split(','):
        x = x.strip()
        if not x:
            continue
        try:
            vals.append(int(x))
        except Exception:
            pass
    return set(vals)


def _send_lock_scope():
    scope = (os.environ.get('NARR_CODEX_SCOPE_ID') or '').strip()
    if scope:
        return scope
    vals = []
    for source in (getattr(A, 'send_gamepks', ''), getattr(A, 'gamepks', '')):
        vals.extend([x.strip() for x in str(source or '').replace(';', ',').split(',') if x.strip()])
    if not vals:
        return 'full'
    return 'g' + hashlib.sha1(','.join(sorted(set(vals))).encode('utf-8')).hexdigest()[:8]


def _send_lock_owner():
    owner = (os.environ.get('MLB_SEND_OWNER') or '').strip()
    if owner:
        return owner
    if getattr(A, 'model_notification_only', False):
        return 'codex_auto'
    if getattr(A, 'codex_narrator', False):
        return 'codex_manual'
    return 'run_mlb_v3_direct'


def _requires_send_owner_lock():
    return not bool(getattr(A, 'no_notification', False) or getattr(A, 'preview', False))


def _parse_gamepks(value):
    vals = []
    for x in str(value or '').split(','):
        x = x.strip()
        if not x:
            continue
        try:
            vals.append(int(x))
        except Exception:
            pass
    return set(vals)


def _load_dual_verdicts(date):
    """Load optional Claude/Codex convergence artifact for the current narrator scope."""
    scope = (os.environ.get('NARR_CODEX_SCOPE_ID') or '').strip()
    candidates = []
    if scope:
        candidates.append(Path(f'_narr_dual_verdicts_{date}__{scope}.json'))
    candidates.append(Path(f'_narr_dual_verdicts_{date}.json'))

    def _rows(payload):
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ('games', 'verdicts', 'items', 'rows'):
                if isinstance(payload.get(key), list):
                    return payload[key]
            if all(isinstance(v, dict) for v in payload.values()):
                return list(payload.values())
        return []

    for path in candidates:
        if not path.exists():
            continue
        try:
            payload = json.load(open(path, encoding='utf-8'))
        except Exception as e:
            print(f'  [dual-verdict] warn load failed {path.name}: {type(e).__name__}: {e}')
            continue
        by_gpk, by_matchup = {}, {}
        for row in _rows(payload):
            if not isinstance(row, dict):
                continue
            conv = row.get('convergence') if isinstance(row.get('convergence'), dict) else {}
            item = {'row': row, 'convergence': conv, 'source_scope': scope or row.get('scope') or ''}
            gpk = row.get('gamePk') or row.get('game_pk') or row.get('game_id')
            if gpk is not None:
                try:
                    by_gpk[str(int(gpk))] = item
                except Exception:
                    by_gpk[str(gpk)] = item
            matchup = str(row.get('matchup') or '').strip()
            if matchup:
                by_matchup[matchup] = item
        print(f'  [dual-verdict] loaded {path.name}: gamePk={len(by_gpk)} matchup={len(by_matchup)}')
        return {'path': str(path), 'scope': scope, 'by_gpk': by_gpk, 'by_matchup': by_matchup}
    return None


def _dual_verdict_for(dual, row):
    if not dual:
        return None
    try:
        gpk = str(int(row.gamePk))
    except Exception:
        gpk = str(row.get('gamePk', ''))
    item = dual.get('by_gpk', {}).get(gpk)
    if item:
        return item
    matchup = f"{row.away_team} @ {row.home_team}"
    return dual.get('by_matchup', {}).get(matchup)


def _norm_dual_prediction(v):
    s = str(v or '').strip().upper()
    return None if s in {'', '-', 'NONE', 'NULL', 'NAN'} else s


def selected_analysis_gamepks(date):
    wanted = set()
    explicit = _parse_gamepks(getattr(A, 'gamepks', ''))
    if explicit:
        wanted |= explicit
    if getattr(A, 'ready_only', False):
        path = Path(f'_mlb_pregame_readiness_{date}.json')
        if not path.exists():
            raise RuntimeError(f'--ready-only requested but {path} is missing; run _mlb_pregame_readiness.py {date} --write first')
        payload = json.loads(path.read_text(encoding='utf-8'))
        ready = _parse_gamepks(','.join(str(x) for x in payload.get('ready_gamepks') or []))
        print(f"  [ready-only] readiness ready={len(ready)}/{payload.get('games', '?')} from {path.name}")
        wanted = (wanted & ready) if wanted else ready
    return wanted


def restrict_analysis_df(df, date):
    wanted = selected_analysis_gamepks(date)
    if not wanted:
        if getattr(A, 'ready_only', False):
            print('  [ready-only] ready gamePk 0; analysis skipped')
            return df.iloc[0:0].copy()
        return df
    if 'gamePk' not in df.columns:
        raise RuntimeError('analysis gamePk filter requested but gamePk column is missing')
    out = df[df['gamePk'].astype(int).isin(wanted)].copy()
    print(f'  [analysis-filter] selected {len(out)}/{len(df)} games: {sorted(wanted)}')
    missing = sorted(wanted - set(out['gamePk'].dropna().astype(int).tolist()))
    if missing:
        print(f'  [analysis-filter] requested gamePk not in slate: {missing}')
    return out


def analysis_filter_requested():
    return bool(str(getattr(A, 'gamepks', '') or '').strip()) or bool(getattr(A, 'ready_only', False))


def write_v3_games_csv(date, df):
    path = v3_games_path(date)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not analysis_filter_requested() or 'gamePk' not in df.columns:
        df.to_csv(path, index=False)
        return
    pks = set(df['gamePk'].dropna().astype(int).tolist())
    existing = pd.DataFrame()
    if path.exists():
        try:
            existing = pd.read_csv(path)
        except Exception:
            existing = pd.DataFrame()
    if len(existing) and 'gamePk' in existing.columns:
        existing = existing[~existing['gamePk'].astype(int).isin(pks)].copy()
    merged = pd.concat([existing, df], ignore_index=True, sort=False)
    sort_cols = [c for c in ['kst', 'gamePk'] if c in merged.columns]
    if sort_cols:
        merged = merged.sort_values(sort_cols, na_position='last', kind='stable')
    merged.to_csv(path, index=False)
    print(f'  [partial-v3] merged {len(df)} analyzed rows into {path.name}; total_rows={len(merged)}')


def scope_v3_games_csv_for_narrator(date, df):
    """Temporarily make _v3_games_DATE.csv match a partial narrator scope.

    Most narrator context builders read the root v3 CSV. When a --gamepks run
    excludes only one game from a doubleheader, matchup-string filtering cannot
    distinguish the two identical "Away @ Home" headers. This temporary scope
    keeps prompt generation gamePk-exact while preserving the merged full slate
    for the final Notification table.
    """
    if not analysis_filter_requested() or 'gamePk' not in df.columns:
        return None
    path = v3_games_path(date)
    if not path.exists():
        return None
    backup = path.with_suffix(path.suffix + f'.narr_scope_{os.getpid()}.bak')
    backup.write_bytes(path.read_bytes())
    df.to_csv(path, index=False)
    print(f'  [narrator] scoped v3 csv for prompt: rows={len(df)}')
    return backup


def restore_v3_games_csv_after_narrator(date, backup):
    if not backup:
        return
    path = v3_games_path(date)
    try:
        path.write_bytes(Path(backup).read_bytes())
    finally:
        try:
            Path(backup).unlink()
        except Exception:
            pass


def filter_to_gamepks(df, pks):
    if not pks or 'gamePk' not in df.columns:
        return df
    return df[df['gamePk'].astype(int).isin({int(x) for x in pks})].copy()


def restrict_send_df(df):
    wanted = selected_send_gamepks()
    if not wanted:
        return df
    if 'gamePk' not in df.columns:
        print('  [send-filter] gamePk column missing; no send filter applied')
        return df
    out = df[df['gamePk'].astype(int).isin(wanted)].copy()
    print(f'  [send-filter] selected {len(out)}/{len(df)} games for Notification: {sorted(wanted)}')
    return out


def _lineup_names_present(row) -> bool:
    """ASCII-safe lineup-confirmed check: both teams' lineup names non-empty.

    Replaces the encoding-fragile `lineup_src == '확정'` literal comparison
    (which is mojibake in run_mlb_v3.py and breaks under PYTHONUTF8=1).
    run_mlb_predict.py writes 9 names when confirmed, empty when fallback, so
    names-present is structurally equivalent to confirmed but encoding-immune.
    """
    for col in ('home_lineup_names', 'away_lineup_names'):
        s = row.get(col)
        if s is None or (isinstance(s, float) and pd.isna(s)):
            return False
        if not str(s).strip():
            return False
    return True


def _lineup_names_blank(value) -> bool:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return True
    return not str(value).strip()


def _lineup_names_from_feed(game_pk, side):
    try:
        url = f"https://statsapi.mlb.com/api/v1.1/game/{int(game_pk)}/feed/live"
        feed = requests.get(url, timeout=15).json()
        box_team = (((feed.get('liveData') or {}).get('boxscore') or {}).get('teams') or {}).get(side) or {}
        order = box_team.get('battingOrder') or []
        if len(order) < 9:
            return []
        box_players = box_team.get('players') or {}
        game_players = (feed.get('gameData') or {}).get('players') or {}
        names = []
        for pid in order:
            key = f'ID{pid}'
            person = (box_players.get(key) or {}).get('person') or {}
            name = person.get('fullName') or (game_players.get(key) or {}).get('fullName')
            if name:
                names.append(str(name).strip())
        return names if len(names) >= 9 else []
    except Exception:
        return []


def backfill_lineup_names_from_feed(df):
    """Fill missing lineup name arrays from StatsAPI feed/live battingOrder.

    run_mlb_predict.py normally writes names when confirmed. Occasionally the
    boxscore endpoint exposes battingOrder but omits one side's player names,
    while feed/live has the same order plus gameData player names. The send
    gate is names-present, so fill only missing names from that authoritative
    feed and leave fallback rows empty.
    """
    if 'gamePk' not in df.columns:
        return df
    for col in ('home_lineup_names', 'away_lineup_names'):
        if col not in df.columns:
            df[col] = ''
        else:
            # CSV inference turns an all-empty column into float64. Confirmed
            # lineup strings must be assigned through an object column.
            df[col] = df[col].astype('object')
    for col in ('home_lineup_src', 'away_lineup_src'):
        if col in df.columns:
            df[col] = df[col].astype('object')
    filled = 0
    for idx, row in df.iterrows():
        gpk = row.get('gamePk')
        for side in ('home', 'away'):
            col = f'{side}_lineup_names'
            if not _lineup_names_blank(row.get(col)):
                continue
            names = _lineup_names_from_feed(gpk, side)
            if not names:
                continue
            df.at[idx, col] = ','.join(names)
            src_col = f'{side}_lineup_src'
            if src_col in df.columns and _lineup_names_blank(row.get(src_col)):
                df.at[idx, src_col] = 'feed_live'
            filled += 1
    if filled:
        print(f'  [lineup-feed] backfilled lineup_names sides={filled}')
    return df


def restrict_lineup_confirmed_df(df, allow_fallback=False):
    """Hard gate before pregame Notification send: drop rows whose lineup is
    unconfirmed (names-present basis). Prevents fallback predictions from leaking
    (824751 Texas@Boston 2026-06-14 incident). allow_fallback bypasses for
    forced_partial / manual exceptions. Fail-closed if name columns are absent
    (cannot verify confirmation -> do not send)."""
    if allow_fallback:
        return df
    if 'home_lineup_names' not in df.columns or 'away_lineup_names' not in df.columns:
        print('  [lineup-gate] lineup_names columns missing; fail-closed (0 sent)')
        return df.iloc[0:0].copy()
    mask = df.apply(_lineup_names_present, axis=1)
    dropped = df[~mask]
    for _, r in dropped.iterrows():
        away = str(r.get('away_team', '?'))[:3]
        home = str(r.get('home_team', '?'))[:3]
        print(f"  [lineup-gate] DROP unconfirmed lineup: gamePk={r.get('gamePk')} {away}@{home}")
    kept = df[mask].copy()
    print(f'  [lineup-gate] {len(kept)}/{len(df)} games passed names-present gate')
    return kept


def _gamepks_arg(gamepks):
    vals = []
    for x in gamepks or []:
        try:
            vals.append(str(int(float(x))))
        except Exception:
            text = str(x).strip()
            if text:
                vals.append(text)
    return ",".join(sorted(set(vals)))


def run_pregame_guard_pipeline(date, gamepks=None):
    """Research-only narrator guard refresh.

    Runs after _v3_games_DATE.csv exists and before narrator/notification output.
    It never mutates production predictions or narrator predictions; failure blocks pregame
    narrator output so missing guard coverage is visible instead of silent.
    """
    import subprocess
    py = f'"{sys.executable}"'
    scoped_gamepks = _gamepks_arg(gamepks)
    scope_arg = f" --gamepks {scoped_gamepks}" if scoped_gamepks else ""
    if scoped_gamepks:
        print(f"  [pregame_guard] scoped gamePks={scoped_gamepks}", flush=True)
    steps = [
        ("quality_audit", f"{py} _mlb_retrospective_separation_quality_audit.py --write", True),
        ("registry", f"{py} _mlb_retrospective_signal_registry.py --write --skip-if-fresh", True),
        # 2026-06-13: player_contact/handedness는 --date 타깃 패치로 교체(전체 재빌드 회피).
        # 풀 재빌드 시 handedness가 10분+ 타임아웃 → 트리거 막힘. --date는 해당 날짜 행만
        # 기존 historical ledger 위에 패치(date<target). 측정: handedness 29s / player_contact 16.5s.
        # run_context(0.3s)/quality_audit(0.6s)는 빠르므로 풀 유지. registry(54s)는 --date 미지원 → 추후 skip-aware 후보.
        ("player_contact", f"{py} _mlb_fullread_v4_player_contact_ledger.py --date {date} --write --skip-if-current", True),
        ("run_context", f"{py} _mlb_fullread_v4_run_context_ledger.py --write", True),
        ("handedness", f"{py} _mlb_fullread_v4_handedness_matchup_ledger.py --date {date} --write --skip-if-current", True),
        ("bullpen_projection", f"{py} _mlb_fullread_v4_bullpen_appearance_projection.py --date {date} --write --skip-if-current", False),
        ("guard_validate", f"{py} _mlb_narrator_pregame_guard_validate.py --dates {date} --write{scope_arg}", True),
        ("anti_burial_context_validate", f"{py} _mlb_anti_burial_context_validate.py --date {date} --write --strict{scope_arg}", True),
        ("directional_retro_validate", f"{py} _mlb_directional_regular_season_retrospective_validate.py --season 2026 --write --skip-if-current", True),
        ("directional_player_cards", f"{py} _mlb_player_context_cards_build.py --as-of-date {date} --target-date {date} --write --skip-if-current", True),
        ("signal_layers", f"{py} _mlb_signal_layer_build.py --date {date} --write{scope_arg}", True),
        ("bvp_player_memory", f"{py} _mlb_bvp_matchup_ledger.py --date {date} --start-season 2021 --end-season 2026 --write --skip-if-current", False),
        ("t038_signal_cards", f"{py} _mlb_pregame_signal_cards.py --date {date} --write", False),
        ("evidence_matrix", f"{py} _mlb_pregame_evidence_matrix.py --date {date} --write{scope_arg}", False),
    ]
    env = dict(__import__("os").environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    for name, cmd, critical in steps:
        print(f"  [pregame_guard] {name}: {cmd}", flush=True)
        rc = subprocess.run(cmd, shell=True, cwd=Path(__file__).resolve().parent, env=env).returncode
        if rc != 0:
            msg = f"[pregame_guard] {name} failed rc={rc}"
            if critical:
                raise RuntimeError(msg)
            print(f"  [WARN] {msg}", flush=True)


def get_pitcher_season(pid):
    '공식 투수 기록을 조회해 누적 횟수에서 ERA·WHIP·K9·BB9·HR9를 계산한다. 과거 replay는 별도 날짜 제한 경로를 사용한다.'
    if pid is None or (isinstance(pid, float) and pd.isna(pid)):
        return {}
    pid = int(pid)  # 공식 API 요청의 선수 ID를 정수로 변환한다.
    if _REPLAY_ASOF.enabled(DATE):
        return _REPLAY_ASOF.pitcher_stats(pid, DATE)
    try:
        url = f"{MLB_API}/people/{pid}?hydrate=stats(group=[pitching],type=[yearByYear])"
        d = requests.get(url, timeout=10).json()
        sp = d.get('people', [{}])[0].get('stats', [])
        if not sp:
            return {}
        splits = sp[0].get('splits', [])
        if not splits:
            return {}
        return career_stats_from_splits(splits)  # 누적 횟수를 합한 뒤 비율을 다시 계산한다.
    except Exception as e:
        print(f'  ?좑툘 pid={pid} stats ?ㅽ뙣: {e}')
        return {}


def norm(s):
    return re.sub(r'[^a-z0-9]', '', s.lower()) if isinstance(s, str) else ''


SNAPSHOT_MARKET_PRICE_COLUMNS = [
    'win_loss_home_open_imp',
    'win_loss_away_open_imp',
    'win_loss_home_close_imp',
    'win_loss_away_close_imp',
    'win_loss_home_imp_shift_max',
    'win_loss_home_imp_shift_min',
    'win_loss_home_n_markets',
    'win_loss_home_close_std',
    'win_loss_home_n_down',
    'win_loss_home_n_up',
    'run_total_upper_open_imp_avg',
    'run_total_upper_close_imp_avg',
    'run_total_upper_imp_shift_avg',
    'run_total_n_markets_upper',
    'run_total_lower_open_imp_avg',
    'run_total_lower_close_imp_avg',
    'run_total_lower_imp_shift_avg',
    'run_total_main_baseline',
    'run_total_dominant_baseline',
    'run_total_upper_market_fee_shift',
    'run_total_baseline_balance_gap',
    'run_total_baseline_candidate_count',
    'run_total_baseline_selection_method',
]


def _file_sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _parse_snapshot_time(value):
    if not value:
        return None
    raw = str(value).strip()
    if raw.endswith('Z'):
        raw = raw[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _pregame_snapshot_path():
    return (A.input_snapshot or os.environ.get('MLB_PREGAME_INPUT_SNAPSHOT') or '').strip()


def _load_pregame_input_snapshot(date):
    path = _pregame_snapshot_path()
    if not path:
        return None, None
    p = Path(path)
    if not p.is_absolute():
        p = Path.cwd() / p
    if not p.exists():
        raise RuntimeError(f'Frozen pregame input snapshot missing: {p}')
    expected_sha = (os.environ.get('MLB_PREGAME_INPUT_SNAPSHOT_SHA256') or '').strip()
    actual_sha = _file_sha256(p)
    if expected_sha and actual_sha.lower() != expected_sha.lower():
        raise RuntimeError(f'Frozen pregame input snapshot hash mismatch: expected {expected_sha}, got {actual_sha}')
    data = json.loads(p.read_text(encoding='utf-8'))
    if data.get('target_date') != date:
        raise RuntimeError(f"Frozen pregame input snapshot date mismatch: {data.get('target_date')} != {date}")
    market_price_meta = data.get('market_price') or {}
    market_price_expires_raw = market_price_meta.get('expires_utc') or (data.get('ttl') or {}).get('market_price_expires_utc') or data.get('expires_utc')
    expires = _parse_snapshot_time(market_price_expires_raw)
    allow_stale = os.environ.get('MLB_PREGAME_INPUT_SNAPSHOT_ALLOW_STALE') == '1'
    if expires and datetime.now(timezone.utc) > expires and not allow_stale:
        raise RuntimeError(f'Frozen pregame market_price snapshot expired at {market_price_expires_raw}; refusing stale market_price inputs')
    coverage = data.get('coverage') or {}
    if coverage and not coverage.get('market_price_coverage_ok', coverage.get('coverage_ok', True)):
        raise RuntimeError(f'Frozen pregame input snapshot market_price coverage incomplete: {coverage}')
    data['_path'] = str(p)
    data['_sha256'] = actual_sha
    return data, p


def _snapshot_market_price_dataframe(snapshot):
    market_price = snapshot.get('market_price') or {}
    rows = market_price.get('rows') or []
    od = pd.DataFrame(rows)
    if od.empty:
        raise RuntimeError('Frozen pregame input snapshot has no market_price rows')
    if 'home_team' not in od.columns or 'away_team' not in od.columns:
        raise RuntimeError('Frozen pregame input snapshot market_price rows must contain home_team and away_team')
    if 'hn' not in od.columns:
        od['hn'] = od.home_team.map(norm)
    if 'an' not in od.columns:
        od['an'] = od.away_team.map(norm)
    for c in SNAPSHOT_MARKET_PRICE_COLUMNS:
        if c in od.columns:
            od[c] = pd.to_numeric(od[c], errors='coerce')
    return od


def attach_opening_market_price(df):
    '공급된 snapshot 또는 strict replay의 시장 입력을 연결한다. 실시간 시장 수집은 제외됐다.'
    snapshot, _snapshot_path = _load_pregame_input_snapshot(DATE)
    if snapshot:
        od = _snapshot_market_price_dataframe(snapshot)
        print(f"  [input_snapshot] frozen market_price: {Path(snapshot['_path']).name} sha={snapshot['_sha256'][:12]} rows={len(od)}")
    elif _REPLAY_ASOF.enabled(DATE):
        print('  [replay market_price] live OddsPortal fetch blocked; using date-safe cached historical features')
        return df
    else:
        raise RuntimeError("Provide a frozen pregame snapshot or strict replay cached input; live market collection is not included.")
    if not len(od):
        print('  live market_price 0 rows')
        return df
    od['hn'] = od.home_team.map(norm); od['an'] = od.away_team.map(norm)
    od = od.drop_duplicates(['hn', 'an'], keep='first')
    upper_imp = pd.to_numeric(od.get('run_total_upper_close_imp_avg'), errors='coerce')
    lower_imp = pd.to_numeric(od.get('run_total_lower_close_imp_avg'), errors='coerce')
    main_baseline = pd.to_numeric(od.get('run_total_main_baseline'), errors='coerce')
    if upper_imp is not None and lower_imp is not None and main_baseline is not None:
        invalid_run_total = main_baseline.notna() & (upper_imp - lower_imp).abs().gt(0.12)
        if invalid_run_total.any():
            bad = od.loc[invalid_run_total, ['away_team', 'home_team', 'run_total_main_baseline']].to_dict('records')
            raise RuntimeError(f'Invalid alternate run total baseline selected; refusing pregame run: {bad}')
    df['hn'] = df.home_team.map(norm); df['an'] = df.away_team.map(norm)
    odc = [c for c in od.columns if c not in ('home_team', 'away_team', 'event_id')]  # 원본의 손상된 주석을 정리했다. 실제 처리는 아래 코드와 변경표를 참조한다.
    df = df.merge(od[odc], on=['hn', 'an'], how='left')
    n = df.win_loss_home_open_imp.notna().sum()
    print(f'  시장 입력 연결: {n}/{len(df)} 경기 (WIN_LOSS/RUN_TOTAL 시장 입력)')
    return df.drop(columns=['hn', 'an'])


def imp_to_american(imp):
    if not imp or imp <= 0 or imp >= 1:
        return np.nan
    dec = 1.0 / imp
    return (dec - 1) * 100 if dec >= 2 else -100.0 / (dec - 1)


def assemble_features(df, date, feat_path='mlb_clean_win_loss_features.json'):
    '모델 메타데이터의 특성 목록에 맞춰 경기 입력과 행렬 X를 구성한다.'
    import os
    from _mlb_live_features import add_live_features
    from _mlb_bt_may import fill_all_missing
    feats = json.load(open(feat_path, encoding='utf-8'))['features']

    # 0) predictions_v2 base merge
    pv_name = f'predictions_v2_{date}.csv'
    data_drive = os.environ.get('MLB_DATA_DRIVE')
    predict_output_drive = os.environ.get('MLB_PREDICT_OUTPUT_DRIVE')
    pv_candidates = [
        pv_name,
        os.path.join(os.getcwd(), pv_name),
        os.path.join(predict_output_drive, str(date)[:4], pv_name) if predict_output_drive else None,
        os.path.join(data_drive, str(date)[:4], pv_name) if data_drive else None,
        os.path.join('<DATA_ROOT>', str(date)[:4], pv_name),
    ]
    pvf = None
    for cand in pv_candidates:
        if not cand:
            continue
        try:
            if os.path.exists(cand):
                pvf = cand
                break
        except OSError:
            continue
    if pvf:
        pv = pd.read_csv(pvf)
        addc = [c for c in pv.columns if c != 'gamePk' and c not in df.columns]
        df = df.merge(pv[['gamePk'] + addc], on='gamePk', how='left')
        print(f'  predictions_v2 base merge: +{len(addc)} columns ({pvf})')
    else:
        print(f'  ⚠️ predictions_v2_{date} not found -> base features may be incomplete (run run_mlb_predict first)')
    if _REPLAY_ASOF.enabled(date):
        required = ('home_lineup_names', 'away_lineup_names')
        missing = [
            column
            for column in required
            if column not in df.columns
            or df[column].fillna('').astype(str).str.strip().eq('').any()
        ]
        if missing:
            raise RuntimeError(f'frozen replay lineup names incomplete: {missing}')
    else:
        df = backfill_lineup_names_from_feed(df)

    # 1) rolling/prior (raise on error)
    df = add_live_features(df, date)
    # 선발 기록을 학습 입력의 spa 열로 연결한다.
    for sd in ['home', 'away']:
        for st, c in [('era', 'spa_era'), ('whip', 'spa_whip'), ('k9', 'spa_k9'),
                      ('bb9', 'spa_bb9'), ('hr9', 'spa_hr9'), ('ip', 'spa_ip')]:
            df[f'{sd}_{c}'] = df.get(f'{sd}_sp_{st}')
    for c in ['spa_era', 'spa_whip', 'spa_k9', 'spa_bb9', 'spa_hr9', 'spa_ip']:
        df[f'd_{c}'] = df.get(f'home_{c}') - df.get(f'away_{c}')
    # 기온·바람 단위를 변환하고 주심·불펜 차이를 구성한다.
    df['temp_c'] = (pd.to_numeric(df.get('temperature'), errors='coerce') - 32) / 1.8
    df['wind_kmh'] = pd.to_numeric(df.get('wind_speed'), errors='coerce') * 1.609
    df['ump_avg_runs'] = df.get('hp_ump_runs_mean')
    for m in ['bp_fatigue_1d', 'bp_fatigue_3d']:
        home_m = pd.to_numeric(df.get(f'home_{m}', pd.Series(0, index=df.index)), errors='coerce').fillna(0)
        away_m = pd.to_numeric(df.get(f'away_{m}', pd.Series(0, index=df.index)), errors='coerce').fillna(0)
        df[f'{m}_diff'] = home_m - away_m
    home_bp_1d = pd.to_numeric(df.get('home_bp_fatigue_1d', pd.Series(0, index=df.index)), errors='coerce').fillna(0)
    away_bp_1d = pd.to_numeric(df.get('away_bp_fatigue_1d', pd.Series(0, index=df.index)), errors='coerce').fillna(0)
    df['bp_fatigue_sum'] = home_bp_1d + away_bp_1d
    # 4) WIN_LOSS line-move
    df['mdt'] = pd.to_datetime(df.date)
    df['hn'] = df.home_team.map(norm); df['an'] = df.away_team.map(norm)
    df['oh'] = df.get('win_loss_home_open_imp'); df['oa'] = df.get('win_loss_away_open_imp')
    df['ch'] = df.get('win_loss_home_close_imp'); df['ca'] = df.get('win_loss_away_close_imp')
    df['win_loss_home_open_imp_avg'] = df.oh; df['win_loss_away_open_imp_avg'] = df.oa
    df['win_loss_home_close_imp_avg'] = df.ch; df['win_loss_away_close_imp_avg'] = df.ca
    df['win_loss_home_imp_shift_avg'] = df.ch - df.oh; df['win_loss_away_imp_shift_avg'] = df.ca - df.oa
    # Historical replay rows already carry frozen pregame WIN_LOSS market_price. Preserve
    # them when live close-implied columns are unavailable instead of silently
    # replacing valid replay market_price with NaN.
    _existing_home_win_loss = pd.to_numeric(df.get('home_WIN_LOSS'), errors='coerce')
    _existing_away_win_loss = pd.to_numeric(df.get('away_WIN_LOSS'), errors='coerce')
    df['home_WIN_LOSS'] = df.ch.apply(imp_to_american).combine_first(_existing_home_win_loss)
    df['away_WIN_LOSS'] = df.ca.apply(imp_to_american).combine_first(_existing_away_win_loss)
    # 결측 입력을 보강한 뒤 현재 시장값을 보존한다.
    _live = ['run_total_upper_open_imp_avg', 'run_total_upper_close_imp_avg', 'run_total_upper_imp_shift_avg', 'run_total_n_markets_upper',
             'run_total_lower_open_imp_avg', 'run_total_lower_close_imp_avg', 'run_total_lower_imp_shift_avg', 'run_total_main_baseline',
             'run_total_dominant_baseline', 'run_total_upper_market_fee_shift', 'win_loss_home_imp_shift_max', 'win_loss_home_imp_shift_min',
             'win_loss_home_n_markets', 'win_loss_home_close_std', 'win_loss_home_n_down', 'win_loss_home_n_up']
    live_df = df[['gamePk'] + [c for c in _live if c in df.columns]].copy()
    df = fill_all_missing(df, norm)
    ld = live_df.groupby('gamePk', as_index=True).last()  # gamePk 기준으로 정렬된 마지막 행을 선택한다.
    for c in ld.columns:
        m = df.gamePk.map(ld[c])
        df[c] = df[c].where(df[c].notna(), m) if c in df.columns else m
    if 'run_total_baseline' not in df.columns:
        df['run_total_baseline'] = np.nan
    # A fresh market main baseline must replace a stale historical/model alias.
    # Keeping the old alias first can silently turn an alternate total into
    # the narrator threshold even when the frozen market_price snapshot is correct.
    _resolved_run_total_baseline = pd.Series(np.nan, index=df.index, dtype='float64')
    for _run_total_alias in ('run_total_main_baseline', 'run_total_dominant_baseline', 'run_total_baseline'):
        if _run_total_alias in df.columns:
            _resolved_run_total_baseline = _resolved_run_total_baseline.combine_first(
                pd.to_numeric(df[_run_total_alias], errors='coerce')
            )
    df['run_total_baseline'] = _resolved_run_total_baseline
    if 'run_total_main_baseline' in df.columns:
        _main_baseline = pd.to_numeric(df['run_total_main_baseline'], errors='coerce')
        _line_mismatch = _main_baseline.notna() & df['run_total_baseline'].notna() & ((_main_baseline - df['run_total_baseline']).abs() > 1e-9)
        if _line_mismatch.any():
            bad = df.loc[_line_mismatch, ['gamePk', 'away_team', 'home_team', 'run_total_main_baseline', 'run_total_baseline']].to_dict('records')
            raise RuntimeError(f'RUN_TOTAL main-line alias mismatch after resolution: {bad}')
    # 6) X (202)
    X = pd.DataFrame(index=df.index)
    for c in feats:
        if c in df.columns:
            X[c] = df[c]
    X = X.reindex(columns=feats).apply(pd.to_numeric, errors='coerce')
    print(f'  feature coverage: {int(X.notna().any().sum())}/{len(feats)}')
    return df, X


def _json_clean_scalar(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        value = float(value)
        if not math.isfinite(value):
            return None
        return round(value, 12)
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    return str(value) if not isinstance(value, (int, str)) else value


def _feature_vector_hash(values):
    payload = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _model_contrib_matrix(model, X):
    try:
        return model.predict(X, pred_contrib=True)
    except TypeError:
        pass
    booster = getattr(model, 'booster_', None)
    if booster is None:
        return None
    try:
        return booster.predict(X, pred_contrib=True)
    except Exception:
        return None


def _top_contrib_items(features, values, contribs, side, limit=12):
    items = []
    for feature, value, contribution in zip(features, values, contribs):
        contribution = _json_clean_scalar(contribution)
        if contribution is None:
            continue
        if side == 'home' and contribution <= 0:
            continue
        if side == 'away' and contribution >= 0:
            continue
        items.append({
            'feature': str(feature),
            'value': _json_clean_scalar(value),
            'contribution': contribution,
            'direction': side,
        })
    items.sort(key=lambda x: abs(float(x.get('contribution') or 0)), reverse=True)
    return items[:limit]


def _top_abs_contrib_items(features, values, contribs, limit=16):
    items = []
    for feature, value, contribution in zip(features, values, contribs):
        contribution = _json_clean_scalar(contribution)
        if contribution is None:
            continue
        items.append({
            'feature': str(feature),
            'value': _json_clean_scalar(value),
            'contribution': contribution,
            'direction': 'home' if contribution > 0 else 'away' if contribution < 0 else 'neutral',
        })
    items.sort(key=lambda x: abs(float(x.get('contribution') or 0)), reverse=True)
    return items[:limit]


def write_model_reason_snapshot(date, df, X, model, model_path='mlb_clean_win_loss_model.pkl', feat_path='mlb_clean_win_loss_features.json'):
    """Append prediction-moment WIN_LOSS model reason snapshots before freeze/env mutation."""
    out_dir = Path(
        os.environ.get('MLB_MODEL_REASON_SNAPSHOT_OUT_ROOT')
        or Path('postmortem') / 'fullread_v4' / 'model_reason_snapshots'
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    day_jsonl = out_dir / f'mlb_model_reason_snapshot_{date}.jsonl'
    summary_json = out_dir / f'mlb_model_reason_snapshot_{date}_summary.json'
    features = list(X.columns)
    contrib = _model_contrib_matrix(model, X)
    contrib_available = contrib is not None
    existing_versions = {}
    if day_jsonl.exists():
        try:
            with day_jsonl.open('r', encoding='utf-8-sig') as f:
                for line in f:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    game_pk = str(row.get('gamePk') or '').split('.')[0]
                    if game_pk:
                        existing_versions[game_pk] = max(existing_versions.get(game_pk, 0), int(row.get('snapshot_version') or 0))
        except Exception:
            existing_versions = {}

    generated = datetime.now(timezone(timedelta(hours=9))).isoformat(timespec='seconds')
    run_id_src = '|'.join([
        date,
        generated,
        str(os.getpid()),
        str(getattr(A, 'gamepks', '') or ''),
        str(getattr(A, 'ready_only', False)),
        str(os.environ.get('NARR_CODEX_SCOPE_ID') or ''),
    ])
    run_id = hashlib.sha1(run_id_src.encode('utf-8')).hexdigest()[:16]
    rows = []
    for pos, (idx, r) in enumerate(df.iterrows()):
        game_pk = str(_json_clean_scalar(r.get('gamePk')) or '').split('.')[0]
        if not game_pk:
            continue
        values = [_json_clean_scalar(v) for v in X.iloc[pos].tolist()]
        feature_hash = _feature_vector_hash(dict(zip(features, values)))
        p_home = _json_clean_scalar(r.get('p_home'))
        prediction_team = str(r.get('prediction_team') or '')
        home = str(r.get('home_team') or '')
        away = str(r.get('away_team') or '')
        if prediction_team == home:
            prediction_side = 'HOME'
        elif prediction_team == away:
            prediction_side = 'AWAY'
        else:
            prediction_side = ''
        row_contrib = None
        bias = None
        if contrib_available:
            try:
                row_contrib = list(contrib[pos])
                if len(row_contrib) == len(features) + 1:
                    bias = _json_clean_scalar(row_contrib[-1])
                    row_contrib = row_contrib[:-1]
                elif len(row_contrib) != len(features):
                    row_contrib = None
            except Exception:
                row_contrib = None
        existing_versions[game_pk] = existing_versions.get(game_pk, 0) + 1
        rows.append({
            'schema': 'mlb_model_reason_snapshot.v1',
            'snapshot_mode': 'prediction_moment',
            'provenance_quality': 'exact_pre_freeze_env_backfill_prediction_vector',
            'generated_at_kst': generated,
            'run_id': run_id,
            'snapshot_version': existing_versions[game_pk],
            'date': date,
            'gamePk': game_pk,
            'away_team': away,
            'home_team': home,
            'market': 'WIN_LOSS',
            'model_file': str(model_path),
            'feature_file': str(feat_path),
            'model_class': f"{type(model).__module__}.{type(model).__name__}",
            'feature_count': len(features),
            'non_null_feature_count': int(pd.Series(values).notna().sum()),
            'p_home': p_home,
            'prediction_team': prediction_team,
            'prediction_side': prediction_side,
            'feature_vector_hash': feature_hash,
            'contribution_available': row_contrib is not None,
            'contribution_bias': bias,
            'top_home_positive_features': _top_contrib_items(features, values, row_contrib or [], 'home'),
            'top_away_positive_features': _top_contrib_items(features, values, row_contrib or [], 'away'),
            'top_abs_features': _top_abs_contrib_items(features, values, row_contrib or []),
            'scope': {
                'analysis_filter_requested': analysis_filter_requested(),
                'gamepks_arg': str(getattr(A, 'gamepks', '') or ''),
                'ready_only': bool(getattr(A, 'ready_only', False)),
                'pregame_replay': bool(getattr(A, 'pregame_replay', False)),
                'narr_code_scope_id': str(os.environ.get('NARR_CODEX_SCOPE_ID') or ''),
            },
            'guardrail': 'Prediction-moment research snapshot before freeze/env/backfill mutation. Does not change predictions or Notification.',
        })

    with day_jsonl.open('a', encoding='utf-8', newline='\n') as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
    summary = {
        'schema': 'mlb_model_reason_snapshot_summary.v1',
        'generated_at_kst': generated,
        'date': date,
        'run_id': run_id,
        'rows_appended': len(rows),
        'contribution_available': contrib_available,
        'day_jsonl': str(day_jsonl),
        'summary_json': str(summary_json),
        'guardrail': 'Research-only. Snapshot is append/versioned and never overwrites production or narrator predictions.',
    }
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f"  [model-reason] snapshot appended: {len(rows)} rows -> {day_jsonl}")


def build_features_and_prediction(df, th, model_path='mlb_clean_win_loss_model.pkl', feat_path='mlb_clean_win_loss_features.json'):
    '저장된 승패 모델로 확률을 계산하고 시장 확률과의 차이를 구한다.'
    import joblib
    model = joblib.load(model_path)
    df, X = assemble_features(df, DATE, feat_path)
    df['p_home'] = model.predict_proba(X)[:, 1]

    # 원본의 손상된 주석을 정리했다. 실제 처리는 아래 코드와 변경표를 참조한다.
    iho, iao = df.oh, df.oa
    df['ph_open'] = iho / (iho + iao)
    df['edge_home'] = df.p_home - df.ph_open
    df['edge_away'] = (1 - df.p_home) - (1 - df.ph_open)
    df['prediction_team'] = np.where(df.p_home > 0.5, df.home_team, df.away_team)
    df['prediction_p'] = np.where(df.p_home > 0.5, df.p_home, 1 - df.p_home)
    df['prediction_edge'] = np.where(df.p_home > 0.5, df.edge_home, df.edge_away)
    df['prediction_market_price'] = np.where(df.p_home > 0.5, 1 / df.oh, 1 / df.oa)
    df['is_value'] = df.prediction_edge >= th
    try:
        write_model_reason_snapshot(DATE, df, X, model, model_path=model_path, feat_path=feat_path)
    except Exception as e:
        print(f'  [model-reason] snapshot failed warn-only: {type(e).__name__}: {e}')
    return df


# RUN_TOTAL는 총득점 회귀와 기준점·bias 차이로 계산한다.
_RUN_TOTAL_META = None


def _run_total_meta(path='mlb_clean_run_total_features.json'):
    global _RUN_TOTAL_META
    if _RUN_TOTAL_META is None:
        _RUN_TOTAL_META = json.load(open(path, encoding='utf-8'))
    return _RUN_TOTAL_META




def run_total_prediction_from_signal(signal):
    return 'UPPER' if signal > 0 else 'LOWER'




def build_run_total_prediction(df):
    '총득점 모델의 예측에서 기준선과 bias를 빼고 RUN_TOTAL 방향을 계산한다.'
    import joblib
    m = _run_total_meta()
    model = joblib.load('mlb_clean_run_total_model.pkl')
    feats, bias = m['features'], m['bias']
    X = pd.DataFrame(index=df.index)
    for c in feats:
        if c in df.columns:
            X[c] = df[c]
    X = X.reindex(columns=feats).apply(pd.to_numeric, errors='coerce')
    print(f'  RUN_TOTAL feature coverage: {int(X.notna().any().sum())}/{len(feats)}')
    df['run_total_pred_total'] = model.predict(X)
    df['run_total_signal'] = df.run_total_pred_total - pd.to_numeric(df.get('run_total_baseline'), errors='coerce') - bias
    df['run_total_prediction_side'] = df.run_total_signal.apply(lambda s: run_total_prediction_from_signal(s) if pd.notna(s) else None)
    # 선택 방향에 맞는 공급된 시장 확률의 역수를 구한다.
    _oi = pd.to_numeric(df.get('run_total_upper_open_imp_avg'), errors='coerce')
    _ui = pd.to_numeric(df.get('run_total_lower_open_imp_avg'), errors='coerce')
    df['run_total_prediction_market_price'] = np.where(df.run_total_prediction_side == 'UPPER', 1.0 / _oi, 1.0 / _ui)
    # 바람 위험 표시를 별도 열로 보관한다.
    df['run_total_wind_risk'] = pd.to_numeric(df.get('wind_kmh'), errors='coerce') >= 20
    return df




# 과거 clean-zone 관련 설명; 금액 계산은 발췌본에서 제외했다.




def _boolish(value):
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):
        pass
    if isinstance(value, str):
        return value.strip().lower() in {'1', 'true', 't', 'yes', 'y'}
    return bool(value)


def recompute_loss_scores_from_current_fields(df):
    """Recompute loss-score context after late weather/env backfills.

    predictions_v2 carries these columns, but run_mlb_v3 may replace weather
    from StatsAPI/env_guard later. Recomputing here keeps narrator context and
    Colab parity tied to the final pregame row, not a stale intermediate CSV.
    """
    from _loss_score_lib import win_loss_loss_score, run_total_loss_score, is_clean_zone

    out = df.copy()

    def _driver(v):
        if isinstance(v, list):
            return '+'.join(str(x) for x in v if x and str(x).lower() != 'none')
        return str(v or '')

    def _win_loss_loss_row(r):
        return win_loss_loss_score({
            'prediction_lop_delta': r.get('prediction_lineup_ops_delta'),
            'lop_gap': r.get('lop_gap'),
            'win_loss_implied': r.get('win_implied'),
            'home_pit_barrel_allowed14': r.get('home_pit_barrel_allowed14'),
            'drivers': _driver(r.get('win_loss_drivers')),
            'win_loss_strong_combo': _boolish(r.get('win_loss_strong_combo', False)),
        })

    def _run_total_loss_row(r):
        return run_total_loss_score({
            'run_total_drivers': _driver(r.get('run_total_drivers')),
            'run_total_drift_watch': _boolish(r.get('run_total_drift_watch', False)),
            'wind_kmh': r.get('wind_kmh'),
            'precip_mm': r.get('precip_mm'),
            'run_total_tag': r.get('run_total_tag'),
        })

    out['win_loss_loss_score'] = out.apply(_win_loss_loss_row, axis=1)
    out['run_total_loss_score'] = out.apply(_run_total_loss_row, axis=1)
    out['win_loss_loss_partial'] = out.get('home_pit_barrel_allowed14', pd.Series(index=out.index, dtype='float64')).isna()
    wind = pd.to_numeric(out.get('wind_kmh', pd.Series(index=out.index, dtype='float64')), errors='coerce')
    precip = pd.to_numeric(out.get('precip_mm', pd.Series(index=out.index, dtype='float64')), errors='coerce')
    out['run_total_loss_partial'] = wind.isna() | precip.isna()

    win_loss_selection = out.get('win_selection', out.get('is_value', pd.Series(False, index=out.index))).fillna(False).map(_boolish)
    run_total_selection = out.get('run_total_selection', out.get(pd.Series(False, index=out.index))).fillna(False).map(_boolish)
    out['win_loss_clean_zone'] = [
        bool(is_clean_zone(score) and selection and not partial)
        for score, selection, partial in zip(out['win_loss_loss_score'], win_loss_selection, out['win_loss_loss_partial'])
    ]
    out['run_total_clean_zone'] = [
        bool(is_clean_zone(score) and selection and not partial)
        for score, selection, partial in zip(out['run_total_loss_score'], run_total_selection, out['run_total_loss_partial'])
    ]
    print(
        f"  [loss_score] recomputed from current v3 fields: "
        f"WIN_LOSS clean={int(pd.Series(out['win_loss_clean_zone']).sum())} "
        f"RUN_TOTAL clean={int(pd.Series(out['run_total_clean_zone']).sum())}"
    )
    return out


# 동결해서 보존할 예측 열; 입력 특성은 별도다.
_FREEZE_COLS = ['p_home', 'prediction_team', 'prediction_p', 'prediction_edge', 'prediction_market_price', 'is_value', 'prediction_home',
                'run_total_pred_total', 'run_total_signal', 'run_total_prediction_side',
                'run_total_wind_risk', 'run_total_baseline']


def apply_prediction_freeze(df, date):
    '이전 파일에서 양 팀 라인업이 확정된 경기의 지정 예측 열을 보존한다.'
    import os
    if A.no_prediction_freeze:
        print('  [freeze] explicitly bypassed for research/repair recomputation')
        df['is_frozen'] = False
        return df
    df['is_frozen'] = False
    path = v3_games_path(date)
    if not path.exists():
        print('  [freeze] previous v3 file missing; first run without freeze')
        return df
    try:
        prev = pd.read_csv(path)
        if 'home_lineup_names' not in prev.columns or 'away_lineup_names' not in prev.columns:
            print('  [freeze] previous CSV has no lineup_names; skip freeze (fail-closed)')
            return df
        # confirmed = names-present (ASCII-safe). Replaces encoding-fragile
        # lineup_src == '확정' literal (was mojibake -> always False under utf-8).
        fmask = prev.apply(_lineup_names_present, axis=1)
        fpks = set(prev.loc[fmask, 'gamePk'])
        if not fpks:
            print('  [freeze] no confirmed previous games; skip freeze')
            return df
        pidx = prev.drop_duplicates('gamePk').set_index('gamePk')
        cols = [c for c in _FREEZE_COLS if c in pidx.columns]
        n = 0
        for _i, _gpk in df.gamePk.items():
            if _gpk in fpks and _gpk in pidx.index:
                for c in cols:
                    df.at[_i, c] = pidx.at[_gpk, c]
                df.at[_i, 'is_frozen'] = True
                n += 1
        print(f'  PREDICTION FREEZE: 라인업 확정 {n}/{len(df)} 기존 예측 보존')
    except Exception as _e:
        print(f'  freeze failed ({_e}); continue without freeze')
    return df


def assert_run_total_baseline_alias_parity(df):
    if 'run_total_main_baseline' not in df.columns or 'run_total_baseline' not in df.columns:
        return
    main_baseline = pd.to_numeric(df['run_total_main_baseline'], errors='coerce')
    used_line = pd.to_numeric(df['run_total_baseline'], errors='coerce')
    mismatch = main_baseline.notna() & used_line.notna() & ((main_baseline - used_line).abs() > 1e-9)
    if mismatch.any():
        bad = df.loc[mismatch, ['gamePk', 'away_team', 'home_team', 'run_total_main_baseline', 'run_total_baseline']].to_dict('records')
        raise RuntimeError(f'run total baseline changed after prediction freeze; refusing narrator input: {bad}')


def _abbr_sp(name):
    '선수 이름을 성과 이름 첫 글자의 표시 형태로 줄인다.'
    if not isinstance(name, str) or ' ' not in name:
        return name or '?'
    a, b = name.split(' ', 1)
    return f'{a[0]}.{b}'


def write_analysis_report(date, df, th, results=None):
    """Portfolio replacement: save an analysis table instead of sending a message."""
    out = Path(os.environ.get('MLB_OUTPUT_DIR', 'outputs'))
    out.mkdir(parents=True, exist_ok=True)
    df.to_csv(out / f'analysis_{date}.csv', index=False, encoding='utf-8')
    if results is not None:
        (out / f'results_{date}.json').write_text(json.dumps(results, ensure_ascii=False, default=str), encoding='utf-8')


def fetch_games(date):
    if _REPLAY_ASOF.enabled(date):
        rows = _REPLAY_ASOF.target_games(date)
        frame = pd.DataFrame(rows)
        if frame.empty:
            return frame
        keep = [
            'gamePk', 'date', 'home_team', 'away_team',
            'home_team_id', 'away_team_id',
            'home_sp_id', 'away_sp_id', 'home_sp_name', 'away_sp_name',
            'status', 'game_time_utc',
            'home_lineup_names', 'away_lineup_names',
        ]
        frame = frame[[column for column in keep if column in frame.columns]].copy()
        frame['kst'] = pd.to_datetime(
            frame.pop('game_time_utc'), utc=True, errors='coerce'
        ).dt.tz_convert('Asia/Seoul').dt.strftime('%m/%d %H:%M')
        return frame
    url = f"{MLB_API}/schedule?sportId=1&date={date}&hydrate=probablePitcher,linescore"
    d = requests.get(url, timeout=15).json()
    rows = []
    for day in d.get('dates', []):
        for g in day.get('games', []):
            home, away = g['teams']['home'], g['teams']['away']
            h_sp = home.get('probablePitcher', {}) or {}
            a_sp = away.get('probablePitcher', {}) or {}
            # 공식 UTC 시작 시각을 KST 표시 시각으로 변환한다.
            kst = ''
            try:
                gd = datetime.fromisoformat(g['gameDate'].replace('Z', '+00:00')) + timedelta(hours=9)
                kst = gd.strftime('%m/%d %H:%M')
            except Exception:
                pass
            rows.append({
                'gamePk': g['gamePk'], 'date': g['officialDate'], 'kst': kst,
                'home_team': home['team']['name'], 'away_team': away['team']['name'],
                'home_team_id': home['team']['id'], 'away_team_id': away['team']['id'],
                'home_sp_id': h_sp.get('id'), 'away_sp_id': a_sp.get('id'),
                'home_sp_name': h_sp.get('fullName', '?'), 'away_sp_name': a_sp.get('fullName', '?'),
                'status': g['status']['detailedState'],
            })
    return pd.DataFrame(rows)


def fetch_results(date):
    '공식 일정에서 팀과 점수를 읽는다. Final 경기만 점수를 반영하고 나머지는 None으로 남긴다.'
    import os
    r = requests.get(f"{MLB_API}/schedule", params={'sportId': 1, 'date': date}, timeout=20)
    out = {}
    for d in r.json().get('dates', []):
        for g in d.get('games', []):
            h, a = g['teams']['home'], g['teams']['away']
            is_final = g.get('status', {}).get('abstractGameState') == 'Final'
            asc = a.get('score') if is_final else None       # 종료되지 않은 경기의 점수는 결과로 반영하지 않는다.
            hsc = h.get('score') if is_final else None
            out[g['gamePk']] = (a['team']['name'], asc, h['team']['name'], hsc)
    return out


def fetch_result_status_meta(date):
    """Backward-compatible wrapper for callers in this file."""
    return fetch_mlb_result_status_meta(date)


def result_summary_path(date):
    return Path('postmortem') / 'partial_results' / f"{date.replace('-', '')}_mlb_v3_result_summary.json"


def load_sent_result_gamepks(date):
    path = result_summary_path(date)
    if not path.exists():
        return set()
    try:
        d = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return set()
    vals = d.get('sent_final_gamePks') or d.get('final_gamePks') or []
    out = set()
    for v in vals:
        try:
            out.add(int(v))
        except Exception:
            pass
    return out


def write_result_partial_summary(date, df, results, sent_gamepks, prior_sent_gamepks=None):
    out_dir = Path('postmortem') / 'partial_results'
    out_dir.mkdir(parents=True, exist_ok=True)
    prior_sent_gamepks = set(prior_sent_gamepks or [])
    sent_gamepks = {int(x) for x in (sent_gamepks or [])}
    cumulative_sent = sorted(prior_sent_gamepks | sent_gamepks)
    final_pks = {
        int(pk) for pk, v in (results or {}).items()
        if v and v[1] is not None and v[3] is not None
    }
    all_pks = [int(x) for x in df.get('gamePk', pd.Series(dtype='int64')).dropna().astype(int).tolist()]
    status_meta = fetch_result_status_meta(date)
    postponed = [pk for pk in all_pks if pk not in final_pks and (status_meta.get(pk) or {}).get('no_result')]
    remaining = [pk for pk in all_pks if pk not in final_pks and pk not in set(postponed)]
    payload = {
        'schema': 'mlb_v3_result_partial_summary.v1',
        'generated_at_kst': datetime.now(timezone(timedelta(hours=9))).replace(microsecond=0).isoformat(),
        'date': date,
        'total_prediction_rows': len(df),
        'final_result_rows': len(final_pks),
        'notification_rows': len(sent_gamepks),
        'notification_gamePks': sorted(sent_gamepks),
        'sent_final_gamePks': cumulative_sent,
        'already_sent_before_run': sorted(prior_sent_gamepks),
        'include_pending_results': bool(A.include_pending_results),
        'resend_results': bool(A.resend_results),
        'final_gamePks': sorted(final_pks),
        'remaining_gamePks': remaining,
        'postponed_gamePks': postponed,
        'postponed_status': {str(pk): status_meta.get(pk) for pk in postponed},
        'policy': {
            'default_result_send': 'newly-final games only',
            'remaining_games': 'rerun result pipeline later; only newly-final games should be updated',
            'postponed_games': 'scoreless postponed/cancelled/suspended games are not result-send candidates for this date',
        },
    }
    path = result_summary_path(date)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print(
        f"  [partial_result] wrote {path} final={len(final_pks)} "
        f"sent_now={len(sent_gamepks)} sent_total={len(cumulative_sent)} "
        f"remaining={len(remaining)} postponed={len(postponed)}"
    )


def main():
    if A.result:
        # 원본의 손상된 주석을 정리했다. 실제 처리는 아래 코드와 변경표를 참조한다.
        path = v3_games_path(DATE)
        if not path.exists():
            print(f'{path} 파일 없음 (사전 분석 산출물 필요)'); return
        df = pd.read_csv(path)
        res = fetch_results(DATE)
        final_pks = {
            int(pk) for pk, v in res.items()
            if v and v[1] is not None and v[3] is not None
        }
        all_pks = [int(x) for x in df.get('gamePk', pd.Series(dtype='int64')).dropna().astype(int).tolist()]
        status_meta = fetch_result_status_meta(DATE)
        postponed_pks = [pk for pk in all_pks if pk not in final_pks and (status_meta.get(pk) or {}).get('no_result')]
        remaining_pks = [pk for pk in all_pks if pk not in final_pks and pk not in set(postponed_pks)]
        prior_sent = set() if A.resend_results else load_sent_result_gamepks(DATE)
        print(
            f'result mode: games={len(df)}, final={len(final_pks)}, '
            f'pending={len(remaining_pks)}, postponed={len(postponed_pks)}, previously_sent={len(prior_sent)}'
        )
        send_df = df
        if not A.include_pending_results:
            send_pks = final_pks if A.resend_results else (final_pks - prior_sent)
            send_df = df[df['gamePk'].astype(int).isin(send_pks)].copy()
            if remaining_pks:
                print(f'  [partial_result] final-only send; remaining gamePks={remaining_pks}')
            if postponed_pks:
                print(f'  [partial_result] postponed/no-result gamePks={postponed_pks}')
            if prior_sent:
                print(f'  [partial_result] skip already-sent final gamePks={sorted(prior_sent & final_pks)}')
        else:
            send_pks = set(all_pks)
        send_df = restrict_send_df(send_df)
        sent_gamepks = [
            int(x) for x in send_df.get('gamePk', pd.Series(dtype='int64')).dropna().astype(int).tolist()
            if int(x) in final_pks
        ]
        if A.no_notification:
            write_result_partial_summary(DATE, df, res, [], prior_sent)
            print('  [partial_result] --no-notification; result Notification send skipped')
            return
        write_result_partial_summary(DATE, df, res, sent_gamepks, prior_sent)
        if len(send_df) == 0:
            print('  [partial_result] 새로 종료한 경기 없음; 결과 처리 생략')
            return
        write_analysis_report(DATE, send_df, A.th, results=res)
        return
    df = fetch_games(DATE)
    if not len(df):
        print('경기 없음 또는 일정 확인 필요.')
        return
    df = restrict_analysis_df(df, DATE)
    if not len(df):
        print('  [analysis-filter] analysis rows 0; exiting before model/guard/Notification')
        return
    auto_replay = bool(A.pregame_replay)
    if has_non_pregame_status(df) and not A.pregame_replay:
        auto_replay = True
        print('  [pregame-replay] live/final status detected; automatically using sanitized pregame replay.')
    assert_pregame_status(df, allow_replay=auto_replay)
    if auto_replay:
        A.pregame_replay = True
        print('  [pregame-replay] live/final status allowed; postgame fields will be sanitized.')
        df = sanitize_pregame_replay(df)
    print(f'경기 {len(df)}개 ' + ', '.join(f"{r.away_team[:3]}@{r.home_team[:3]}" for _, r in df.iterrows()))
    # 선발투수의 경기 전 기록을 연결한다.
    for idx, r in df.iterrows():
        for side in ['home', 'away']:
            st = get_pitcher_season(r[f'{side}_sp_id'])
            for k, v in st.items():
                df.at[idx, f'{side}_sp_{k}'] = v
        time.sleep(0.2)
    print('SP ?ㅽ꺈 fetch ?꾨즺')
    # 제공된 snapshot 또는 과거 replay의 시장 입력을 연결한다.
    print('제공된 시장 입력 연결...')
    df = attach_opening_market_price(df)
    # 모델 특성을 구성하고 확률과 시장 차이를 계산한다.
    df = build_features_and_prediction(df, A.th)
    # 승패와 별도로 총득점과 방향을 계산한다.
    df = build_run_total_prediction(df)
    # 과거 clean-zone 관련 설명; 금액 계산은 발췌본에서 제외했다.
    # 저장 전에 확정 라인업의 기존 예측을 동결한다.
    df = apply_prediction_freeze(df, DATE)
    assert_run_total_baseline_alias_parity(df)
    # 빈 경기 환경값은 공식 feed의 자료로 보강한다.
    # CSV 갱신 뒤 환경 입력을 다시 점검한다.
    if os.environ.get('MLB_SKIP_ENV_GUARD') == '1':
        print('  [env_guard] skipped by MLB_SKIP_ENV_GUARD=1')
    else:
        try:
            from _mlb_env_guard import apply_env_guard
            _tmp_env_path = v3_games_path(DATE)
            _prev_env = df.copy()
            _partial_pks = set(_prev_env['gamePk'].dropna().astype(int).tolist()) if 'gamePk' in _prev_env.columns else set()
            write_v3_games_csv(DATE, _prev_env)
            _env_report = apply_env_guard(DATE, write=True, strict=True)
            df = filter_to_gamepks(pd.read_csv(_tmp_env_path), _partial_pks)
            if _env_report.get('updated_count'):
                print(f"  [env_guard] backfilled {int(_env_report.get('updated_count', 0))} games")
            if _env_report.get('warning_remaining_count'):
                print(f"  [env_guard] official-unavailable env fields remain: {int(_env_report.get('warning_remaining_count', 0))} games")
        except Exception as e:
            print(f'  ??env_guard ?ㅽ뙣: {type(e).__name__}: {e}')
            raise
    df = recompute_loss_scores_from_current_fields(df)
    # 입력 근거의 표시용 집계이며 별도 예측 성과를 보장하지 않는다.
    # 과거 표시 규칙의 관찰값; 현재 성능으로 해석하지 않는다.
    from _mlb_bb_adv import bb_adv_row, SIGNALS_CLEAN
    _bb = df.apply(lambda r: bb_adv_row(r, signals=SIGNALS_CLEAN, prediction_home=bool(r.get('prediction_home'))), axis=1)
    df['bb_adv_cnt'] = [int(x[0]) for x in _bb]
    df['bb_adv_n'] = [int(x[1]) for x in _bb]
    df['bb_adv_tier'] = [x[2] for x in _bb]
    _bb_counts = df['bb_adv_tier'].astype(str).value_counts().to_dict()
    print(f"  [bb_adv] tiers={_bb_counts}")
    if A.pregame_replay:
        df = sanitize_pregame_replay(df)
    write_v3_games_csv(DATE, df)
    if os.environ.get('MLB_SKIP_PREGAME_GUARD') == '1':
        print('  [pregame_guard] skipped by MLB_SKIP_PREGAME_GUARD=1')
    else:
        try:
            _guard_gamepks = df['gamePk'].dropna().astype(int).tolist() if analysis_filter_requested() and 'gamePk' in df.columns else None
            run_pregame_guard_pipeline(DATE, gamepks=_guard_gamepks)
        except Exception as e:
            print(f'  ??pregame guard ?ㅽ뙣: {type(e).__name__}: {e}')
            raise
    nv = int(df.is_value.sum())
    print(f'\n=== {len(df)} games summary (value={nv}, edge>={A.th}) ===')
    for _, r in df.sort_values('prediction_edge', ascending=False).iterrows():
        mark = 'VALUE' if r.is_value else '  '
        prediction_team = '' if pd.isna(r.prediction_team) else str(r.prediction_team)
        opp = r.away_team if prediction_team and prediction_team == str(r.home_team) else r.home_team
        opp = '' if pd.isna(opp) else str(opp)
        od = f"{r.prediction_market_price:.2f}" if pd.notna(r.prediction_market_price) else '?'
        pp = f"{r.prediction_p:.0%}" if pd.notna(r.prediction_p) else '-'
        ed = f"{r.prediction_edge:+.2f}" if pd.notna(r.prediction_edge) else '-'
        print(f"  {mark} {prediction_team[:16]:16s} (vs {opp[:12]:12s}) 모델{pp} 시장가격{od} edge{ed}")
    # 분석 결과를 로컬 파일로 기록하는 단계.
    # `--codex-narrator` first-pass runs are allowed to build prompts, but
    # `--no-notification` must still be authoritative.  Do not let the Codex bridge
    # flag reopen the send path during verification/dry runs.
    if A.preview or A.codex_narrator or not A.no_notification:
        send_df = restrict_send_df(df)
        send_df = restrict_lineup_confirmed_df(send_df, allow_fallback=A.allow_fallback_lineup)
        if len(send_df) == 0:
            print('  [send-filter] selected games 0 (after lineup gate); Notification send skipped')
            return
        write_analysis_report(DATE, send_df, A.th)


def _main_entry():
    if not _requires_send_owner_lock():
        return main()
    try:
        with send_lock_context(
            DATE,
            owner=_send_lock_owner(),
            scope=_send_lock_scope(),
            stage='assemble',
            command=' '.join(sys.argv),
            metadata={
                'result_mode': bool(getattr(A, 'result', False)),
                'codex_narrator': bool(getattr(A, 'codex_narrator', False)),
                'send_gamepks': getattr(A, 'send_gamepks', ''),
                'gamepks': getattr(A, 'gamepks', ''),
            },
        ) as _lock:
            if _lock.reused:
                print(f"  [send-lock] reused {os.path.basename(str(_lock.path))} owner={_lock.owner} scope={_lock.scope}")
            else:
                print(f"  [send-lock] acquired {os.path.basename(str(_lock.path))} owner={_lock.owner} scope={_lock.scope}")
            return main()
    except SendLockError as exc:
        print(f"  [send-lock] blocked duplicate send owner: {exc}", file=sys.stderr)
        raise SystemExit(7)


if __name__ == '__main__':
    _main_entry()
