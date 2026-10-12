# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""full 모델 라이브 feature 구성 — 오늘 경기의 sp_roll/bat_roll(as-of-date) + prior.

최근 2026 statcast(mlb_2026_sc_recent.parquet)에서:
- sp_roll14/30: 각 today 선발(sp_id)의 [date-w, date) rolling
- bat_roll14/30: 각 today 팀(abbrev)의 [date-w, date) 타격 rolling
- pp_prior: sp_id → 2026 pitcher prior(2025 기반)
- bp_prior: 팀 최근 활동타자 평균 prior

반환: games_df에 컬럼 추가 (home_/away_ sp_roll*/bat_roll*/pp_prior*/bat_prior*).
"""
import sys, io
import numpy as np
import pandas as pd
import requests
import _mlb_sc_features as F

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

RECENT = 'mlb_2026_sc_recent.parquet'
ROLL_METRICS = ['csw', 'whiff', 'zone', 'k_pct', 'bb_pct', 'barrel_pct', 'ev', 'xwoba', 'velo']


def _ratios(sub, prefix):
    """gamelog 슬라이스 → rolling 비율 dict."""
    pit = sub.pitches.sum(); sw = sub.swings.sum(); pa = sub.pa.sum(); bbe = sub.bbe.sum()
    d = {}
    d[prefix + 'csw'] = sub.csw.sum() / pit if pit else np.nan
    d[prefix + 'whiff'] = sub.whiffs.sum() / sw if sw else np.nan
    d[prefix + 'zone'] = sub.in_zone.sum() / pit if pit else np.nan
    d[prefix + 'k_pct'] = sub.k.sum() / pa if pa else np.nan
    d[prefix + 'bb_pct'] = sub.bb.sum() / pa if pa else np.nan
    d[prefix + 'barrel_pct'] = sub.barrels.sum() / bbe if bbe else np.nan
    d[prefix + 'ev'] = sub.ev_sum.sum() / bbe if bbe else np.nan
    d[prefix + 'xwoba'] = sub.xwoba_sum.sum() / bbe if bbe else np.nan
    d[prefix + 'velo'] = sub.velo_sum.sum() / pit if pit else np.nan
    d[prefix + 'n_starts'] = len(sub)
    return d


def team_abbr_map():
    """statsapi team_id → abbreviation."""
    try:
        d = requests.get('https://statsapi.mlb.com/api/v1/teams?sportId=1', timeout=15).json()
        return {t['id']: t['abbreviation'] for t in d.get('teams', [])}
    except Exception:
        return {}


def add_live_features(games, date):
    sc = pd.read_parquet(RECENT)
    sc['game_date'] = pd.to_datetime(sc.game_date)
    cur = pd.to_datetime(date)
    # 게임로그
    pgl = F._agg_gamelog(sc, 'pitcher')
    pgl['game_date'] = pd.to_datetime(pgl.game_date)
    d = sc.copy(); d['bat_team'] = np.where(d.inning_topbot == 'Top', d.away_team, d.home_team)
    bgl = F._agg_gamelog(d, 'bat_team')
    bgl['game_date'] = pd.to_datetime(bgl.game_date)
    # prior
    pp = pd.read_csv('mlb_pitcher_season_prior.csv'); pp = pp[pp.season == 2026].set_index('sp_id')
    bp = pd.read_csv('mlb_batter_season_prior.csv'); bp = bp[bp.season == 2026].set_index('batter')
    abbr = team_abbr_map()
    # 팀별 최근 활동타자 (bp_prior 평균용)
    recent_bat = sc.groupby(np.where(sc.inning_topbot == 'Top', sc.away_team, sc.home_team)).batter.apply(lambda s: list(pd.Series(s).value_counts().head(12).index))

    def sp_roll(sp_id, w):
        if pd.isna(sp_id):
            return {}
        g = pgl[(pgl.pitcher == int(sp_id)) & (pgl.game_date >= cur - pd.Timedelta(days=w)) & (pgl.game_date < cur)]
        return _ratios(g, f'sp_roll{w}_')

    def bat_roll(abbrev, w):
        g = bgl[(bgl.bat_team == abbrev) & (bgl.game_date >= cur - pd.Timedelta(days=w)) & (bgl.game_date < cur)]
        return _ratios(g, f'bat_roll{w}_')

    for idx, r in games.iterrows():
        for side in ['home', 'away']:
            ab = abbr.get(r[f'{side}_team_id'])
            for w in (14, 30):
                for k, v in sp_roll(r[f'{side}_sp_id'], w).items():
                    games.at[idx, f'{side}_{k}'] = v
                for k, v in bat_roll(ab, w).items():
                    games.at[idx, f'{side}_{k}'] = v
            # pp_prior
            spid = r[f'{side}_sp_id']
            if pd.notna(spid) and int(spid) in pp.index:
                for c in ['pp_prior_xwoba', 'pp_prior_brl', 'pp_prior_ev', 'pp_prior_k', 'pp_prior_bb', 'pp_prior_whiff', 'pp_prior_csw', 'pp_prior_velo', 'pp_prior_pa']:
                    games.at[idx, f'{side}_{c}'] = pp.at[int(spid), c]
            # bp_prior: 팀 최근 타자 평균
            bats = recent_bat.get(ab, [])
            sub = bp.loc[[b for b in bats if b in bp.index]]
            if len(sub):
                for m, col in [('xwoba', 'bat_prior_xwoba'), ('brl', 'bat_prior_brl'), ('ev', 'bat_prior_ev'), ('k', 'bat_prior_k'), ('bb', 'bat_prior_bb'), ('whiff', 'bat_prior_whiff')]:
                    games.at[idx, f'{side}_{col}'] = sub[f'bp_prior_{m}'].mean()
                games.at[idx, f'{side}_bat_prior_n_known'] = len(sub)
    return games


if __name__ == '__main__':
    import json
    g = pd.read_csv('_v3_games_2026-05-27.csv')
    g = add_live_features(g, '2026-05-27')
    cols = [c for c in g.columns if 'roll30' in c or 'pp_prior' in c or 'bat_prior' in c]
    print('생성된 라이브 feature 수:', len(cols))
    print(g[['home_team', 'home_sp_roll30_xwoba', 'away_sp_roll30_xwoba', 'home_bat_roll30_barrel_pct', 'home_pp_prior_k', 'home_bat_prior_xwoba']].head(6).to_string())
