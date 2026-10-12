# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""MLB statcast (Baseball Savant) daily backfill — barrel% + EV team aggregates.

For each date in 2026 season:
  - fetch all batted balls (type='X')
  - aggregate per team (batting side via inning_topbot, pitching side as opposite)
Output: mlb_statcast_daily.csv with rows (date, team, batting_bb, batting_barrel, batting_ev_sum,
                                          pitching_bb, pitching_barrel, pitching_ev_sum)
A second pass computes 14d rolling means and merges to gamePk.
"""
import sys, io, time
import pandas as pd
import requests
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import csv

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

OUT = Path('mlb_statcast_daily.csv')
SRC = Path('backtest_master_mlb.csv')
N_WORKERS = 4  # savant is more sensitive — be polite

_sess = requests.Session()

def fetch_day(date_str):
    """Return DataFrame of batted balls for a single date, or None."""
    url = ('https://baseballsavant.mlb.com/statcast_search/csv'
           '?hfGT=R%7C&hfSea=2026%7C'
           f'&game_date_gt={date_str}&game_date_lt={date_str}'
           '&player_type=batter&type=details&all=true')
    for _ in range(3):
        try:
            r = _sess.get(url, timeout=60)
            if r.status_code == 200 and r.text and len(r.text) > 500:
                df = pd.read_csv(io.StringIO(r.text))
                if len(df) > 0:
                    bb = df[df['type'] == 'X'].copy() if 'type' in df.columns else df
                    return bb
        except Exception as e:
            pass
        time.sleep(1.0)
    return None

def aggregate_day(date_str, bb):
    """Aggregate batted balls into team-level rows.
    inning_topbot: 'Top' → away team batting / home pitching
                   'Bot' → home team batting / away pitching
    """
    out = {}  # team → dict
    if bb is None or bb.empty:
        return []
    for _, r in bb.iterrows():
        ev = r.get('launch_speed')
        ls_angle = r.get('launch_speed_angle')
        topbot = r.get('inning_topbot')
        home = r.get('home_team'); away = r.get('away_team')
        is_top = (topbot == 'Top')
        bat = away if is_top else home
        pit = home if is_top else away
        barrel = 1 if (pd.notna(ls_angle) and int(ls_angle) == 6) else 0
        ev_v = float(ev) if pd.notna(ev) else None
        # batting side
        if bat not in out: out[bat] = {'team':bat, 'batting_bb':0, 'batting_barrel':0,
                                       'batting_ev_sum':0.0, 'batting_ev_n':0,
                                       'pitching_bb':0, 'pitching_barrel':0,
                                       'pitching_ev_sum':0.0, 'pitching_ev_n':0}
        if pit not in out: out[pit] = {'team':pit, 'batting_bb':0, 'batting_barrel':0,
                                       'batting_ev_sum':0.0, 'batting_ev_n':0,
                                       'pitching_bb':0, 'pitching_barrel':0,
                                       'pitching_ev_sum':0.0, 'pitching_ev_n':0}
        out[bat]['batting_bb'] += 1
        out[bat]['batting_barrel'] += barrel
        if ev_v is not None:
            out[bat]['batting_ev_sum'] += ev_v
            out[bat]['batting_ev_n'] += 1
        out[pit]['pitching_bb'] += 1
        out[pit]['pitching_barrel'] += barrel
        if ev_v is not None:
            out[pit]['pitching_ev_sum'] += ev_v
            out[pit]['pitching_ev_n'] += 1
    rows = []
    for t, d in out.items():
        d['date'] = date_str
        rows.append(d)
    return rows

def process_date(date_str):
    bb = fetch_day(date_str)
    if bb is None:
        return date_str, []
    return date_str, aggregate_day(date_str, bb)

# --- main ---
m = pd.read_csv(SRC)
m = m[m['win_loss_result'].isin(['W','L'])]
dates = sorted(m['date'].unique())
print(f'target dates: {len(dates)} ({dates[0]} ~ {dates[-1]})')

if OUT.exists():
    done = pd.read_csv(OUT)
    done_dates = set(done['date'].astype(str))
    print(f'  resume: {len(done_dates)} dates done')
    dates = [d for d in dates if d not in done_dates]
    print(f'  remaining: {len(dates)}')

header_needed = not OUT.exists() or OUT.stat().st_size == 0
fh = open(OUT, 'a', newline='', encoding='utf-8')
wr = csv.writer(fh)
write_lock = threading.Lock()
if header_needed:
    wr.writerow(['date','team','batting_bb','batting_barrel','batting_ev_sum','batting_ev_n',
                 'pitching_bb','pitching_barrel','pitching_ev_sum','pitching_ev_n'])
    fh.flush()

start_t = time.time()
done_n = 0
total_n = len(dates)

with ThreadPoolExecutor(max_workers=N_WORKERS) as ex:
    futures = {ex.submit(process_date, d): d for d in dates}
    for fut in as_completed(futures):
        try:
            date_str, rows = fut.result()
        except Exception as e:
            print(f'  error: {e}', flush=True)
            continue
        with write_lock:
            for d in rows:
                wr.writerow([d['date'], d['team'], d['batting_bb'], d['batting_barrel'],
                             round(d['batting_ev_sum'], 2), d['batting_ev_n'],
                             d['pitching_bb'], d['pitching_barrel'],
                             round(d['pitching_ev_sum'], 2), d['pitching_ev_n']])
            fh.flush()
            done_n += 1
            if done_n % 5 == 0:
                el = time.time() - start_t
                eta = el / done_n * (total_n - done_n)
                print(f'  [{done_n}/{total_n}] {date_str} teams={len(rows)} | eta {eta/60:.1f}min', flush=True)

fh.close()
print(f'done: {OUT}')
