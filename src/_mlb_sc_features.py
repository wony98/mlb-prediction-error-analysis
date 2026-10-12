# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""Phase 1: sc_raw → 선발투수 as-of-date rolling feature derive.

leakage-free: 경기일 D의 rolling feature는 D 이전 등판(start)만 사용.

출력 (game_pk, side[home/away] 단위):
  sp_roll{14,30}_csw, _whiff, _zone, _k_pct, _bb_pct, _barrel_pct, _ev, _xwoba, _velo, _n_starts

usage: py _mlb_sc_features.py [season]   (season 없으면 2020~2025 전체)
"""
import sys, io
import numpy as np
import pandas as pd
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

SC_DIR = Path('savant_xera/sc_raw')
OUT = Path('mlb_sp_rolling_5seasons.csv')

WHIFF_DESC = {'swinging_strike', 'swinging_strike_blocked'}
SWING_DESC = WHIFF_DESC | {'foul', 'foul_tip', 'hit_into_play', 'foul_bunt', 'bunt_foul_tip', 'missed_bunt'}
CSW_DESC = WHIFF_DESC | {'called_strike'}
K_EVENTS = {'strikeout', 'strikeout_double_play'}
BB_EVENTS = {'walk', 'intent_walk'}

COLS = ['game_pk', 'game_date', 'inning', 'inning_topbot', 'pitcher', 'at_bat_number',
        'pitch_number', 'description', 'events', 'type', 'zone', 'launch_speed',
        'launch_speed_angle', 'estimated_woba_using_speedangle', 'release_speed', 'home_team', 'away_team']


def build_pitcher_gamelog(sc):
    """경기×투수 단위 집계 + 선발 여부(side) 태깅."""
    # 선발 식별: 각 (game_pk, inning_topbot)에서 inning==1 첫 투수
    #   Top(원정 타격)=home 투수 / Bot(홈 타격)=away 투수
    first = sc[sc.inning == 1].sort_values(['game_pk', 'inning_topbot', 'at_bat_number', 'pitch_number'])
    starters = first.groupby(['game_pk', 'inning_topbot']).first().reset_index()[['game_pk', 'inning_topbot', 'pitcher']]
    starters['side'] = np.where(starters.inning_topbot == 'Top', 'home', 'away')
    starters = starters[['game_pk', 'pitcher', 'side']].rename(columns={'pitcher': 'sp_id'})

    # 투수별 경기 집계 (그 투수가 던진 모든 피치)
    g = _agg_gamelog(sc, 'pitcher').rename(columns={'pitcher': 'sp_id'})
    return g, starters


def _agg_gamelog(sc, groupkey):
    """game_pk × game_date × groupkey 단위 피치 집계. groupkey='pitcher' or 'bat_team'."""
    d = sc.copy()
    d['is_whiff'] = d.description.isin(WHIFF_DESC)
    d['is_swing'] = d.description.isin(SWING_DESC)
    d['is_csw'] = d.description.isin(CSW_DESC)
    d['in_zone'] = d.zone <= 9
    d['is_pa'] = d.events.notna()
    d['is_k'] = d.events.isin(K_EVENTS)
    d['is_bb'] = d.events.isin(BB_EVENTS)
    d['is_bbe'] = d.type == 'X'
    d['is_barrel'] = d.launch_speed_angle == 6
    # EV/xwOBA는 BBE(type==X) 전용 — launch_speed는 파울에도 기록되므로 마스킹 필수
    d['ev_bbe'] = d.launch_speed.where(d.is_bbe)
    d['xwoba_bbe'] = d.estimated_woba_using_speedangle.where(d.is_bbe)

    return d.groupby(['game_pk', 'game_date', groupkey]).agg(
        pitches=('type', 'size'),
        whiffs=('is_whiff', 'sum'), swings=('is_swing', 'sum'), csw=('is_csw', 'sum'),
        in_zone=('in_zone', 'sum'), pa=('is_pa', 'sum'), k=('is_k', 'sum'), bb=('is_bb', 'sum'),
        bbe=('is_bbe', 'sum'), barrels=('is_barrel', 'sum'),
        ev_sum=('ev_bbe', 'sum'),  # BBE 전용 launch_speed 합 → sum/bbe = 평균 EV
        xwoba_sum=('xwoba_bbe', 'sum'),
        velo_sum=('release_speed', 'sum'),
    ).reset_index()


def build_team_bat_gamelog(sc):
    """타격팀×경기 단위 집계 + 게임별 home/away 팀 매핑.
    inning_topbot: Top → away 타격 / Bot → home 타격."""
    d = sc.copy()
    d['bat_team'] = np.where(d.inning_topbot == 'Top', d.away_team, d.home_team)
    gl = _agg_gamelog(d, 'bat_team')
    teams = sc.groupby('game_pk')[['home_team', 'away_team']].first().reset_index()
    return gl, teams


def rolling_asof(gamelog, windows=(14, 30), key='sp_id', prefix='sp_roll'):
    """주체(투수 또는 타격팀)별 시간순 정렬 후, 각 경기일 '이전' window일 누적으로 rolling 계산.

    key: 그룹 주체 컬럼 ('sp_id' or 'bat_team')
    prefix: 출력 feature 접두 ('sp_roll' or 'bat_roll')
    """
    gl = gamelog.sort_values([key, 'game_date']).copy()
    gl['game_date'] = pd.to_datetime(gl['game_date'])
    out_rows = []
    for kval, g in gl.groupby(key, sort=False):
        g = g.reset_index(drop=True)
        dates = g['game_date'].values
        for i in range(len(g)):
            cur = dates[i]
            row = {'game_pk': g.at[i, 'game_pk'], key: kval}
            for w in windows:
                lo = cur - np.timedelta64(w, 'D')
                # 이전 경기만 (strictly before current date)
                mask = (dates >= lo) & (dates < cur)
                sub = g[mask]
                pref = f'{prefix}{w}_'
                if len(sub) == 0:
                    for mk in ['csw', 'whiff', 'zone', 'k_pct', 'bb_pct', 'barrel_pct', 'ev', 'xwoba', 'velo']:
                        row[pref + mk] = np.nan
                    row[pref + 'n_starts'] = 0
                    continue
                pit = sub.pitches.sum(); sw = sub.swings.sum(); pa = sub.pa.sum(); bbe = sub.bbe.sum()
                row[pref + 'csw'] = sub.csw.sum() / pit if pit else np.nan
                row[pref + 'whiff'] = sub.whiffs.sum() / sw if sw else np.nan
                row[pref + 'zone'] = sub.in_zone.sum() / pit if pit else np.nan
                row[pref + 'k_pct'] = sub.k.sum() / pa if pa else np.nan
                row[pref + 'bb_pct'] = sub.bb.sum() / pa if pa else np.nan
                row[pref + 'barrel_pct'] = sub.barrels.sum() / bbe if bbe else np.nan
                row[pref + 'ev'] = sub.ev_sum.sum() / bbe if bbe else np.nan
                row[pref + 'xwoba'] = sub.xwoba_sum.sum() / bbe if bbe else np.nan
                row[pref + 'velo'] = sub.velo_sum.sum() / pit if pit else np.nan
                row[pref + 'n_starts'] = len(sub)
            out_rows.append(row)
    return pd.DataFrame(out_rows)


def process_season(season):
    """반환: (sp_df, bat_df) — 둘 다 (game_pk, side) 단위."""
    p = SC_DIR / f'sc_raw_{season}.parquet'
    sc = pd.read_parquet(p, columns=COLS)

    # --- SP (선발투수 rolling) ---
    gl, starters = build_pitcher_gamelog(sc)
    sp_roll = rolling_asof(gl, key='sp_id', prefix='sp_roll')
    sp = starters.merge(sp_roll, on=['game_pk', 'sp_id'], how='left')
    sp['season'] = season

    # --- BAT (팀 타격 rolling) ---
    bgl, teams = build_team_bat_gamelog(sc)
    bat_roll = rolling_asof(bgl, key='bat_team', prefix='bat_roll')
    # game당 home/away 두 행으로 펼쳐 side 부착
    home = teams[['game_pk', 'home_team']].rename(columns={'home_team': 'bat_team'}); home['side'] = 'home'
    away = teams[['game_pk', 'away_team']].rename(columns={'away_team': 'bat_team'}); away['side'] = 'away'
    sides = pd.concat([home, away], ignore_index=True)
    bat = sides.merge(bat_roll, on=['game_pk', 'bat_team'], how='left')
    bat['season'] = season
    return sp, bat


if __name__ == '__main__':
    seasons = [int(sys.argv[1])] if len(sys.argv) > 1 else [2020, 2021, 2022, 2023, 2024, 2025]
    sp_all, bat_all = [], []
    for s in seasons:
        print(f'[{s}] processing...', flush=True)
        sp, bat = process_season(s)
        print(f'  -> SP {len(sp)} / BAT {len(bat)} (game,side) rows', flush=True)
        sp_all.append(sp); bat_all.append(bat)
    sp_res = pd.concat(sp_all, ignore_index=True)
    bat_res = pd.concat(bat_all, ignore_index=True)
    suf = f'_{seasons[0]}' if len(seasons) == 1 else '_5seasons'
    sp_out = SC_DIR.parent.parent / f'mlb_sp_rolling{suf}.csv'
    bat_out = SC_DIR.parent.parent / f'mlb_team_bat_rolling{suf}.csv'
    sp_res.to_csv(sp_out, index=False)
    bat_res.to_csv(bat_out, index=False)
    print(f'saved SP {len(sp_res)} -> {sp_out}')
    print(f'saved BAT {len(bat_res)} -> {bat_out}')
    # sanity
    print('\n=== SP sanity (sp_roll30, n_starts>=3) ===')
    s = sp_res[sp_res['sp_roll30_n_starts'] >= 3]
    for c in ['sp_roll30_csw', 'sp_roll30_whiff', 'sp_roll30_k_pct', 'sp_roll30_bb_pct', 'sp_roll30_barrel_pct', 'sp_roll30_ev', 'sp_roll30_xwoba', 'sp_roll30_velo']:
        print(f'  {c:22s} mean={s[c].mean():.3f}  p10={s[c].quantile(.1):.3f}  p90={s[c].quantile(.9):.3f}')
    print(f'  rows n_starts>=3: {len(s)}/{len(sp_res)}')
    print('\n=== BAT sanity (bat_roll30, n_starts>=10) ===')
    b = bat_res[bat_res['bat_roll30_n_starts'] >= 10]
    for c in ['bat_roll30_k_pct', 'bat_roll30_bb_pct', 'bat_roll30_whiff', 'bat_roll30_barrel_pct', 'bat_roll30_ev', 'bat_roll30_xwoba']:
        print(f'  {c:22s} mean={b[c].mean():.3f}  p10={b[c].quantile(.1):.3f}  p90={b[c].quantile(.9):.3f}')
    print(f'  rows n_games>=10: {len(b)}/{len(bat_res)}')
