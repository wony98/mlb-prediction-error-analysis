# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
import os, json, hashlib, pickle
from datetime import datetime, timezone
from pathlib import Path
def _file_sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def _parse_snapshot_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except Exception:
        return None

def _load_pregame_input_snapshot():
    """Load one frozen volatile-input snapshot for local/Colab parity mode."""
    global _INPUT_SNAPSHOT_CACHE
    if _INPUT_SNAPSHOT_CACHE is not None:
        return _INPUT_SNAPSHOT_CACHE
    path = (os.environ.get('MLB_PREGAME_INPUT_SNAPSHOT') or '').strip()
    if not path:
        _INPUT_SNAPSHOT_CACHE = None
        return None
    if not os.path.isabs(path):
        path = os.path.join(os.getcwd(), path)
    if not os.path.exists(path):
        raise RuntimeError(f'Frozen pregame input snapshot missing: {path}')
    expected_sha = (os.environ.get('MLB_PREGAME_INPUT_SNAPSHOT_SHA256') or '').strip()
    actual_sha = _file_sha256(path)
    if expected_sha and actual_sha.lower() != expected_sha.lower():
        raise RuntimeError(f'Frozen pregame input snapshot hash mismatch: expected {expected_sha}, got {actual_sha}')
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    if str(data.get('target_date')) != str(TODAY):
        raise RuntimeError(f"Frozen pregame input snapshot date mismatch: {data.get('target_date')} != {TODAY}")
    coverage = data.get('coverage') or {}
    weather_rows = ((data.get('weather') or {}).get('rows') or [])
    if weather_rows:
        weather_meta = data.get('weather') or {}
        weather_expires_raw = weather_meta.get('expires_utc') or (data.get('ttl') or {}).get('weather_expires_utc') or data.get('expires_utc')
        expires = _parse_snapshot_time(weather_expires_raw)
        if expires and datetime.now(timezone.utc) > expires and os.environ.get('MLB_PREGAME_INPUT_SNAPSHOT_ALLOW_STALE') != '1':
            raise RuntimeError(f'Frozen pregame weather snapshot expired at {weather_expires_raw}; refusing stale weather inputs')
    if weather_rows and not coverage.get('weather_coverage_ok', True):
        raise RuntimeError(f'Frozen pregame input snapshot weather coverage incomplete: {coverage}')
    data['_path'] = path
    data['_sha256'] = actual_sha
    _INPUT_SNAPSHOT_CACHE = data
    return data

def apply_frozen_weather(df, reason=''):
    if _REPLAY_ASOF.enabled(TODAY):
        return df
    snapshot = _load_pregame_input_snapshot()
    if not snapshot:
        return df
    rows = ((snapshot.get('weather') or {}).get('rows') or [])
    if not rows:
        return df
    if 'gamePk' not in df.columns:
        raise RuntimeError('Frozen weather snapshot supplied but df has no gamePk column')
    w = pd.DataFrame(rows)
    if w.empty or 'gamePk' not in w.columns:
        return df
    w['gamePk'] = pd.to_numeric(w['gamePk'], errors='coerce').astype('Int64')
    cols = [
        c for c in [
            'venue', 'temperature', 'weather_condition', 'wind_speed', 'wind_direction',
            'precip_prob', 'precip_mm', 'dome', 'temp_c', 'wind_kmh', 'hp_ump_name',
        ]
        if c in w.columns
    ]
    if not cols:
        return df
    out = df.copy()
    gamepks = pd.to_numeric(out['gamePk'], errors='coerce').astype('Int64')
    applied = 0
    for col in cols:
        mp = w.dropna(subset=['gamePk']).drop_duplicates('gamePk', keep='first').set_index('gamePk')[col]
        vals = gamepks.map(mp)
        mask = vals.notna()
        if mask.any():
            out.loc[mask, col] = vals[mask].to_numpy()
            applied = max(applied, int(mask.sum()))
    print(f"  [input_snapshot] frozen weather applied ({reason}): rows={applied} sha={snapshot['_sha256'][:12]}")
    return out

def _safe_int(v, default=0):
    try:
        if pd.isna(v):
            return default
        return int(v)
    except Exception:
        return default

def norm_team(name):
    if not isinstance(name, str): return name
    name = name.strip()
    return TEAM_NORM.get(name, name)

def legacy_model_loading_excerpt(DRIVE):
    with open(f'{DRIVE}/종결v2_승패_LR.pkl','rb') as f: wp_lr = pickle.load(f)
    with open(f'{DRIVE}/종결v2_승패_LGB.pkl','rb') as f: wp_lgb = pickle.load(f)
    with open(f'{DRIVE}/종결v2_총득점_LR.pkl','rb') as f: uo_lr = pickle.load(f)
    with open(f'{DRIVE}/종결v2_총득점_LGB.pkl','rb') as f: uo_lgb = pickle.load(f)
    WP_F = list(wp_lr.feature_names_in_)
    UO_F = list(uo_lr.feature_names_in_)
