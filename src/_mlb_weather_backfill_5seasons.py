# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""MLB weather 5-season historical backfill (2021-2025, training data).

Reads <DATA_ROOT>/master_5seasons.csv. Same logic as _mlb_weather_backfill.py.
"""
import sys, io, time
import pandas as pd
import requests
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import csv

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

MLB_API = 'https://statsapi.mlb.com/api/v1'
OM_API = 'https://archive-api.open-meteo.com/v1/archive'
SRC = Path('<DATA_ROOT>/master_5seasons.csv')
OUT = Path('mlb_weather_5seasons.csv')
N_WORKERS = 8

TEAM_VENUE = {
    'Arizona Diamondbacks':   (33.4453, -112.0667, True),
    'Atlanta Braves':         (33.8908, -84.4678, False),
    'Baltimore Orioles':      (39.2839, -76.6217, False),
    'Boston Red Sox':         (42.3467, -71.0972, False),
    'Chicago Cubs':           (41.9484, -87.6553, False),
    'Chicago White Sox':      (41.8300, -87.6339, False),
    'Cincinnati Reds':        (39.0975, -84.5067, False),
    'Cleveland Guardians':    (41.4962, -81.6852, False),
    'Cleveland Indians':      (41.4962, -81.6852, False),
    'Colorado Rockies':       (39.7559, -104.9942, False),
    'Detroit Tigers':         (42.3390, -83.0485, False),
    'Houston Astros':         (29.7572, -95.3553, True),
    'Kansas City Royals':     (39.0517, -94.4803, False),
    'Los Angeles Angels':     (33.8003, -117.8827, False),
    'Los Angeles Dodgers':    (34.0739, -118.2400, False),
    'Miami Marlins':          (25.7781, -80.2197, True),
    'Milwaukee Brewers':      (43.0280, -87.9712, True),
    'Minnesota Twins':        (44.9817, -93.2776, False),
    'New York Mets':          (40.7571, -73.8458, False),
    'New York Yankees':       (40.8296, -73.9262, False),
    'Oakland Athletics':      (37.7516, -122.2005, False),  # 2021-2024 Oakland Coliseum
    'Athletics':              (38.5800, -121.5133, False),  # 2025+ Sutter Health Sacramento
    'Philadelphia Phillies':  (39.9061, -75.1665, False),
    'Pittsburgh Pirates':     (40.4469, -80.0057, False),
    'San Diego Padres':       (32.7073, -117.1566, False),
    'San Francisco Giants':   (37.7786, -122.3893, False),
    'Seattle Mariners':       (47.5914, -122.3325, True),
    'St. Louis Cardinals':    (38.6226, -90.1928, False),
    'Tampa Bay Rays':         (27.7682, -82.6534, True),
    'Texas Rangers':          (32.7473, -97.0817, True),
    'Toronto Blue Jays':      (43.6414, -79.3894, True),
    'Washington Nationals':   (38.8730, -77.0074, False),
}

TEAM_TZ_OFFSET = {
    'Arizona Diamondbacks':-7,'Atlanta Braves':-4,'Baltimore Orioles':-4,'Boston Red Sox':-4,
    'Chicago Cubs':-5,'Chicago White Sox':-5,'Cincinnati Reds':-4,'Cleveland Guardians':-4,
    'Cleveland Indians':-4,'Colorado Rockies':-6,'Detroit Tigers':-4,'Houston Astros':-5,
    'Kansas City Royals':-5,'Los Angeles Angels':-7,'Los Angeles Dodgers':-7,'Miami Marlins':-4,
    'Milwaukee Brewers':-5,'Minnesota Twins':-5,'New York Mets':-4,'New York Yankees':-4,
    'Oakland Athletics':-7,'Athletics':-7,'Philadelphia Phillies':-4,'Pittsburgh Pirates':-4,
    'San Diego Padres':-7,'San Francisco Giants':-7,'Seattle Mariners':-7,
    'St. Louis Cardinals':-5,'Tampa Bay Rays':-4,'Texas Rangers':-5,'Toronto Blue Jays':-4,
    'Washington Nationals':-4,
}

_sess = requests.Session()

def _get_json(url, retries=2):
    for _ in range(retries+1):
        try:
            r = _sess.get(url, timeout=15)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        time.sleep(0.3)
    return None

def fetch_game_start_utc(gpk):
    d = _get_json(f'{MLB_API}/schedule?gamePk={gpk}')
    if not d: return None
    try:
        for dx in d.get('dates', []):
            for g in dx.get('games', []):
                if str(g.get('gamePk')) == str(gpk):
                    return g.get('gameDate')
    except Exception:
        pass
    return None

_cache = {}
_cache_lock = threading.Lock()

def fetch_hourly(lat, lon, date):
    key = (lat, lon, date)
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    url = (f'{OM_API}?latitude={lat}&longitude={lon}&start_date={date}&end_date={date}'
           '&hourly=temperature_2m,wind_speed_10m,precipitation,relative_humidity_2m'
           '&wind_speed_unit=kmh&timezone=UTC')
    d = _get_json(url)
    if not d:
        with _cache_lock:
            _cache[key] = None
        return None
    h = d.get('hourly', {})
    try:
        times = h.get('time', [])
        temps = h.get('temperature_2m', [])
        winds = h.get('wind_speed_10m', [])
        precs = h.get('precipitation', [])
        hums  = h.get('relative_humidity_2m', [])
        out = []
        for i, t in enumerate(times):
            out.append({
                'utc_hour': t,
                'temp_c': temps[i] if i < len(temps) else None,
                'wind_kmh': winds[i] if i < len(winds) else None,
                'precip_mm': precs[i] if i < len(precs) else None,
                'humidity_pct': hums[i] if i < len(hums) else None,
            })
        with _cache_lock:
            _cache[key] = out
        return out
    except Exception:
        with _cache_lock:
            _cache[key] = None
        return None

def prediction_hour_row(hourly, target_utc_iso):
    if not hourly or not target_utc_iso: return None
    try:
        t = target_utc_iso.replace('Z','').split('+')[0]
        tprefix = t[:13] + ':00'
        for row in hourly:
            if row['utc_hour'] == tprefix:
                return row
        target_h = int(t[11:13])
        best = None; best_d = 99
        for row in hourly:
            h = int(row['utc_hour'][11:13])
            d = min(abs(h - target_h), 24 - abs(h - target_h))
            if d < best_d:
                best_d = d; best = row
        return best
    except Exception:
        return None

def process_game(row):
    gpk = row['gamePk']; date = row['date']; home = row['home_team']
    venue = TEAM_VENUE.get(home)
    if not venue:
        return None
    lat, lon, dome = venue
    start_utc = fetch_game_start_utc(gpk)
    if not start_utc:
        return None
    hourly = fetch_hourly(lat, lon, date)
    if not hourly:
        return None
    prediction = prediction_hour_row(hourly, start_utc)
    if not prediction:
        return None
    tz = TEAM_TZ_OFFSET.get(home, -5)
    try:
        h_utc = int(start_utc[11:13])
        h_local = (h_utc + tz) % 24
    except Exception:
        h_local = None
    return {
        'gamePk': gpk, 'date': date, 'home': home,
        'lat': lat, 'lon': lon, 'dome': int(bool(dome)),
        'temp_c': prediction.get('temp_c'),
        'wind_kmh': prediction.get('wind_kmh'),
        'precip_mm': prediction.get('precip_mm'),
        'humidity_pct': prediction.get('humidity_pct'),
        'game_hour_local': h_local,
        'game_start_utc': start_utc,
    }

# --- main ---
m = pd.read_csv(SRC, encoding='utf-8')
m = m[m['season'].isin([2021,2022,2023,2024,2025])]
m = m[['gamePk','date','home_team']].copy()
print(f'targets: {len(m)} games (5 seasons 2021-2025)', flush=True)

if OUT.exists():
    done = pd.read_csv(OUT)
    done_gpks = set(done['gamePk'].astype(str))
    print(f'  resume: {len(done_gpks)} already done', flush=True)
    m = m[~m['gamePk'].astype(str).isin(done_gpks)]
    print(f'  remaining: {len(m)}', flush=True)

header_needed = not OUT.exists() or OUT.stat().st_size == 0
fh = open(OUT, 'a', newline='', encoding='utf-8')
wr = csv.writer(fh)
write_lock = threading.Lock()
if header_needed:
    wr.writerow(['gamePk','date','home','lat','lon','dome',
                 'temp_c','wind_kmh','precip_mm','humidity_pct',
                 'game_hour_local','game_start_utc'])
    fh.flush()

start_t = time.time()
done_n = 0
total_n = len(m)
rows = [r.rename({'home_team':'home_team'}) for _, r in m.iterrows()]

with ThreadPoolExecutor(max_workers=N_WORKERS) as ex:
    futures = {ex.submit(process_game, r): r for r in rows}
    for fut in as_completed(futures):
        try:
            o = fut.result()
        except Exception as e:
            print(f'  error: {e}', flush=True)
            continue
        if o is None:
            continue
        with write_lock:
            wr.writerow([o['gamePk'], o['date'], o['home'], o['lat'], o['lon'], o['dome'],
                         o['temp_c'], o['wind_kmh'], o['precip_mm'], o['humidity_pct'],
                         o['game_hour_local'], o['game_start_utc']])
            fh.flush()
            done_n += 1
            if done_n % 100 == 0:
                el = time.time() - start_t
                eta = el / done_n * (total_n - done_n)
                print(f'  [{done_n}/{total_n}] {o["date"]} {o["home"]} '
                      f'temp={o["temp_c"]}°C wind={o["wind_kmh"]}km/h '
                      f'| eta {eta/60:.1f}min', flush=True)

fh.close()
print(f'done: {OUT}', flush=True)
