# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""MLB lineup OPS 5-season historical backfill (2021-2025, training data).

Reads <DATA_ROOT>/master_5seasons.csv (12,122 games).
Same logic as _mlb_lineup_backfill_par.py but SRC + OUT swapped.
"""
import sys, io, time
import pandas as pd
import requests
from pathlib import Path
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import csv

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

MLB_API = 'https://statsapi.mlb.com/api/v1'
SRC = Path('<DATA_ROOT>/master_5seasons.csv')
OUT = Path('mlb_lineup_5seasons.csv')
N_WORKERS = 8

TEAM_TO_ID = {
    'Arizona Diamondbacks':109,'Atlanta Braves':144,'Baltimore Orioles':110,'Boston Red Sox':111,
    'Chicago Cubs':112,'Chicago White Sox':145,'Cincinnati Reds':113,'Cleveland Guardians':114,
    'Cleveland Indians':114,
    'Colorado Rockies':115,'Detroit Tigers':116,'Houston Astros':117,'Kansas City Royals':118,
    'Los Angeles Angels':108,'Los Angeles Dodgers':119,'Miami Marlins':146,'Milwaukee Brewers':158,
    'Minnesota Twins':142,'New York Mets':121,'New York Yankees':147,'Oakland Athletics':133,
    'Athletics':133,'Philadelphia Phillies':143,'Pittsburgh Pirates':134,'San Diego Padres':135,
    'San Francisco Giants':137,'Seattle Mariners':136,'St. Louis Cardinals':138,'Tampa Bay Rays':139,
    'Texas Rangers':140,'Toronto Blue Jays':141,'Washington Nationals':120,
}

def norm_team(name):
    return name.replace("A's",'Athletics').strip() if name else name

_sess = requests.Session()

def _get_json(url, retries=2):
    for _ in range(retries+1):
        try:
            r = _sess.get(url, timeout=12)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        time.sleep(0.2)
    return None

def box_both_sides_ops(gpk):
    d = _get_json(f'{MLB_API}/game/{gpk}/boxscore')
    if not d: return (None, None, None, None)
    try:
        result = {'home': None, 'away': None, 'home_name': None, 'away_name': None}
        for side in ['home','away']:
            tm = d['teams'][side]
            result[f'{side}_name'] = norm_team(tm.get('team',{}).get('name',''))
            bo = tm.get('battingOrder', [])
            if not bo: continue
            players = tm.get('players', {})
            ops = []
            for pid in bo:
                p = players.get(f'ID{pid}', {})
                s = p.get('seasonStats',{}).get('batting',{}).get('ops')
                try: ops.append(float(s))
                except: pass
            result[side] = round(sum(ops), 3) if ops else None
        return result['home'], result['away'], result['home_name'], result['away_name']
    except Exception:
        return None, None, None, None

def prev_played_gpk(team_id, before_date):
    try:
        start = (pd.to_datetime(before_date) - timedelta(days=14)).date()
        end = (pd.to_datetime(before_date) - timedelta(days=1)).date()
        d = _get_json(f'{MLB_API}/schedule?sportId=1&teamId={team_id}&startDate={start}&endDate={end}&gameType=R')
        if not d: return None
        games = []
        for dx in d.get('dates', []):
            for g in dx.get('games', []):
                if g['status']['detailedState'] == 'Final':
                    games.append((dx['date'], g['gamePk']))
        if not games: return None
        games.sort(reverse=True)
        return games[0][1]
    except Exception:
        return None

def process_game(row):
    gpk = row['gamePk']; date = row['date']
    home = row['home_team']; away = row['away_team']

    today_h, today_a, _, _ = box_both_sides_ops(gpk)

    out = {'gamePk': gpk, 'date': date, 'home_team': home, 'away_team': away,
           'today_home_lineup_sum': today_h, 'today_away_lineup_sum': today_a,
           'home_prev_lineup_sum': None, 'away_prev_lineup_sum': None,
           'home_prev_gpk': None, 'away_prev_gpk': None}

    for side_lbl, team in [('home', home), ('away', away)]:
        tid = TEAM_TO_ID.get(team)
        if not tid: continue
        pg = prev_played_gpk(tid, date)
        if not pg: continue
        out[f'{side_lbl}_prev_gpk'] = pg
        ph, pa, ph_name, pa_name = box_both_sides_ops(pg)
        if ph_name == norm_team(team):
            out[f'{side_lbl}_prev_lineup_sum'] = ph
        elif pa_name == norm_team(team):
            out[f'{side_lbl}_prev_lineup_sum'] = pa
    return out

# --- main ---
m = pd.read_csv(SRC, encoding='utf-8')
# season = 2021-2025 only (2026 ongoing, training 제외)
m = m[m['season'].isin([2021,2022,2023,2024,2025])]
m = m[['gamePk','date','home_team','away_team']].copy()
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
    wr.writerow(['gamePk','date','home_team','away_team',
                 'today_home_lineup_sum','today_away_lineup_sum',
                 'home_prev_lineup_sum','away_prev_lineup_sum',
                 'home_prev_gpk','away_prev_gpk'])
    fh.flush()

start_t = time.time()
done_n = 0
total_n = len(m)
rows = [r for _, r in m.iterrows()]

with ThreadPoolExecutor(max_workers=N_WORKERS) as ex:
    futures = {ex.submit(process_game, r): r for r in rows}
    for fut in as_completed(futures):
        try:
            o = fut.result()
        except Exception as e:
            print(f'  error: {e}', flush=True)
            continue
        with write_lock:
            wr.writerow([o['gamePk'], o['date'], o['home_team'], o['away_team'],
                         o['today_home_lineup_sum'], o['today_away_lineup_sum'],
                         o['home_prev_lineup_sum'], o['away_prev_lineup_sum'],
                         o['home_prev_gpk'], o['away_prev_gpk']])
            fh.flush()
            done_n += 1
            if done_n % 100 == 0:
                el = time.time() - start_t
                eta = el / done_n * (total_n - done_n)
                print(f'  [{done_n}/{total_n}] {o["date"]} {o["home_team"]} '
                      f'today_h={o["today_home_lineup_sum"]} today_a={o["today_away_lineup_sum"]} '
                      f'| eta {eta/60:.1f}min', flush=True)

fh.close()
print(f'done: {OUT}', flush=True)
