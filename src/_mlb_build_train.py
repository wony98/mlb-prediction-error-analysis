# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""Phase 3: 학습 master 통합 빌드.

base(master+xERA) + weather + lineup + SP rolling(wide) + SP prior(wide) + BAT rolling(wide)
→ master_5seasons_train.parquet  (gamePk 단위, home_/away_ wide)

usage: py _mlb_build_train.py
"""
import sys, io
import pandas as pd
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

OUT = Path('master_5seasons_train.parquet')


def pivot_side(df, value_cols, id_cols=()):
    """(game_pk, side, *) → gamePk 단위 home_*/away_* wide."""
    keep = ['game_pk', 'side'] + list(value_cols) + list(id_cols)
    df = df[keep].copy()
    out = None
    for side in ['home', 'away']:
        s = df[df.side == side].drop(columns='side').copy()
        ren = {c: f'{side}_{c}' for c in list(value_cols) + list(id_cols)}
        s = s.rename(columns=ren).rename(columns={'game_pk': 'gamePk'})
        out = s if out is None else out.merge(s, on='gamePk', how='outer')
    return out


def add_park(base):
    """홈 구장 statcast park factor (직전시즌 S-1) 병합. team_name→team_id→main_team_id."""
    teams = pd.read_parquet('mlb_cache/teams.parquet')
    name2id = dict(zip(teams.team_name, teams.team_id))
    base['home_team_id'] = base.home_team.map(name2id)
    rows = []
    for yr in range(2020, 2025):
        p = Path(f'savant_xera/corrected/statcast-park-factors__{yr}.csv')
        if not p.exists():
            continue
        d = pd.read_csv(p)[['main_team_id', 'index_runs', 'index_woba', 'index_xwobacon', 'index_hardhit', 'index_obp']]
        d['season'] = yr + 1  # S-1 park factor → season S prior
        rows.append(d)
    pf = pd.concat(rows, ignore_index=True).rename(columns={
        'main_team_id': 'home_team_id', 'index_runs': 'park_idx_runs', 'index_woba': 'park_idx_woba',
        'index_xwobacon': 'park_idx_xwobacon', 'index_hardhit': 'park_idx_hardhit', 'index_obp': 'park_idx_obp'})
    miss = base.home_team_id.isna().sum()
    if miss:
        print(f'  [park] home_team_id 매핑 실패 {miss}건 (team_name 불일치)')
    return base.merge(pf, on=['home_team_id', 'season'], how='left')


def main():
    base = pd.read_csv('master_5seasons_xera_enriched.csv')
    base['gamePk'] = base.gamePk.astype('int64')
    # master의 home/away_sp_id는 2025 전부 NaN인 불량 — sc_raw 기반 id로 대체하므로 제거
    base = base.drop(columns=['home_sp_id', 'away_sp_id'], errors='ignore')
    n0 = len(base)
    print(f'base(xera_enriched): {n0} rows, {len(base.columns)} cols')

    # weather
    w = pd.read_csv('mlb_weather_5seasons.csv')[['gamePk', 'temp_c', 'wind_kmh', 'precip_mm', 'humidity_pct', 'dome']]
    base = base.merge(w, on='gamePk', how='left')

    # lineup
    l = pd.read_csv('mlb_lineup_5seasons.csv')[['gamePk', 'today_home_lineup_sum', 'today_away_lineup_sum',
                                                'home_prev_lineup_sum', 'away_prev_lineup_sum']]
    base = base.merge(l, on='gamePk', how='left')

    # SP rolling (wide) + sp_id 보존
    sp = pd.read_csv('mlb_sp_rolling_5seasons.csv')
    sp_val = [c for c in sp.columns if c.startswith('sp_roll')]
    sp_wide = pivot_side(sp, sp_val, id_cols=['sp_id'])
    base = base.merge(sp_wide, on='gamePk', how='left')

    # SP prior (wide): sc_raw 기반 전 투수 8지표 + leaderboard xera 보조
    pp = pd.read_csv('mlb_pitcher_season_prior.csv')
    pp_cols = [c for c in pp.columns if c.startswith('pp_prior_')]
    lbx = pd.read_csv('mlb_sp_season_prior.csv')[['season', 'sp_id', 'prior_sp_xera']]
    for side in ['home', 'away']:
        pr = pp.rename(columns={'sp_id': f'{side}_sp_id', **{c: f'{side}_{c}' for c in pp_cols}})
        base = base.merge(pr, on=['season', f'{side}_sp_id'], how='left')
        lx = lbx.rename(columns={'sp_id': f'{side}_sp_id', 'prior_sp_xera': f'{side}_prior_sp_xera'})
        base = base.merge(lx, on=['season', f'{side}_sp_id'], how='left')

    # BAT rolling (wide) — home_bat_* = 홈팀 공격 rolling
    bat = pd.read_csv('mlb_team_bat_rolling_5seasons.csv')
    bat_val = [c for c in bat.columns if c.startswith('bat_roll')]
    bat_wide = pivot_side(bat, bat_val)
    base = base.merge(bat_wide, on='gamePk', how='left')

    # BAT prior (wide) — 선발 라인업 직전시즌 집계
    bp = pd.read_csv('mlb_team_bat_prior.csv')
    bp_val = ['bat_prior_xwoba', 'bat_prior_brl', 'bat_prior_ev', 'bat_prior_k', 'bat_prior_bb', 'bat_prior_whiff', 'bat_prior_n_known']
    bp_wide = pivot_side(bp, bp_val)
    base = base.merge(bp_wide, on='gamePk', how='left')

    # line-movement (이미 gamePk 단위 산출됨)
    lm = pd.read_parquet('mlb_cache/_phase7_linemove_features.parquet')
    base = base.merge(lm, on='gamePk', how='left')

    # statcast park factor (홈 구장, 직전시즌 S-1 = pregame 가용)
    base = add_park(base)

    assert len(base) == n0, f'row count changed {n0}->{len(base)}'
    base.to_parquet(OUT, index=False)
    print(f'\nsaved {len(base)} rows, {len(base.columns)} cols -> {OUT}')

    # coverage report
    print('\n=== 신규 feature 커버리지 (non-NaN %) ===')
    groups = {
        'weather': ['temp_c', 'wind_kmh', 'dome'],
        'lineup': ['today_home_lineup_sum', 'away_prev_lineup_sum'],
        'SP roll30': ['home_sp_roll30_xwoba', 'home_sp_roll30_csw'],
        'SP prior(sc)': ['home_pp_prior_xwoba', 'home_pp_prior_velo', 'away_pp_prior_k'],
        'SP prior(xera)': ['home_prior_sp_xera'],
        'BAT roll30': ['home_bat_roll30_xwoba', 'away_bat_roll30_barrel_pct'],
        'BAT prior': ['home_bat_prior_xwoba', 'home_bat_prior_n_known'],
        'line-move': ['win_loss_home_imp_shift_avg', 'run_total_upper_imp_shift_avg'],
        'park(stat)': ['park_idx_runs', 'park_idx_xwobacon'],
        'xERA lag1': ['home_sp_xera_lag1', 'sp_xera_lag1_diff'],
    }
    for g, cols in groups.items():
        for c in cols:
            if c in base.columns:
                print(f'  {g:14s} {c:28s} {100*base[c].notna().mean():5.1f}%')
    print(f'\n총 컬럼 {len(base.columns)}개')


if __name__ == '__main__':
    main()
