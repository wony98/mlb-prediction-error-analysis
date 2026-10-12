# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""베이스라인 모델: 누수 era vs 진짜ERA(clean) — walk-forward per-game 승자 적중 비교.

베이스라인 WIN_LOSS feature에서 era_diff/k9_diff/bb9_diff(누수 시즌누적)를 d_spa_*(진짜ERA as-of-prior)로 교체.
"era 누수만 잡으면 베이스라인 57.5%가 어디로?" 답.
"""
import sys, io
import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.metrics import roc_auc_score, accuracy_score
import warnings
warnings.filterwarnings('ignore')

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# 베이스라인 v2 WIN_LOSS feature (종결v2_모델설정)
BASE_WIN_LOSS = ['era_diff', 'k9_diff', 'bb9_diff', 'ops_diff', 'wr5_diff', 'wr10_diff',
           'rs10_diff', 'bp_fatigue_1d_diff', 'run_total_baseline', 'run_margin', 'home_WIN_LOSS', 'park_HR']
# 누수 SP누적 → 진짜ERA as-of-prior 교체 매핑
CLEAN_SWAP = {'era_diff': 'd_spa_era', 'k9_diff': 'd_spa_k9', 'bb9_diff': 'd_spa_bb9'}
P = dict(objective='binary', n_estimators=250, learning_rate=0.02, num_leaves=15, max_depth=4,
         min_child_samples=80, reg_lambda=2.0, random_state=42, n_jobs=-1, verbose=-1)


def main():
    df = pd.read_parquet('master_5seasons_train.parquet')
    spa = pd.read_csv('mlb_sp_asof_prior.csv')  # 진짜 ERA
    for sd in ('home', 'away'):
        w = spa[spa.side == sd].set_index('game_pk')[['spa_era', 'spa_k9', 'spa_bb9']].add_prefix(sd + '_')
        df = df.merge(w, left_on='gamePk', right_index=True, how='left')
    for s in ['spa_era', 'spa_k9', 'spa_bb9']:
        df[f'd_{s}'] = df[f'home_{s}'] - df[f'away_{s}']
    df = df[df.home_win.notna()]

    clean_feats = [CLEAN_SWAP.get(f, f) for f in BASE_WIN_LOSS]
    print('누수 baseline feat:', BASE_WIN_LOSS)
    print('clean baseline feat:', clean_feats)
    print()

    def wf(feats, label):
        accs = []
        for test in [2022, 2023, 2024, 2025]:
            tr, te = df[df.season < test], df[df.season == test]
            m = lgb.LGBMClassifier(**P); m.fit(tr[feats], tr.home_win)
            p = m.predict_proba(te[feats])[:, 1]
            acc = accuracy_score(te.home_win, (p > 0.5).astype(int))
            auc = roc_auc_score(te.home_win, p)
            accs.append((test, acc, auc, len(te)))
        print(f'=== {label} ===')
        tot_c = tot_n = 0
        for t, a, au, n in accs:
            tot_c += a * n; tot_n += n
            print(f'  {t}: acc={a:.1%} AUC={au:.3f} (n={n})')
        print(f'  종합 acc={tot_c/tot_n:.1%}\n')

    wf(BASE_WIN_LOSS, '베이스라인 (누수 era 포함)')
    wf(clean_feats, '베이스라인 (era→진짜ERA clean)')
    # 시장 천장
    def a2d(x):
        x = pd.to_numeric(x, errors='coerce'); return np.where(x > 0, 1 + x/100, np.where(x < 0, 1 + 100/-x, np.nan))
    d25 = df[(df.season == 2025) & df.home_WIN_LOSS.notna() & df.away_WIN_LOSS.notna()]
    dh, da = a2d(d25.home_WIN_LOSS), a2d(d25.away_WIN_LOSS)
    ph = (1/dh) / (1/dh + 1/da)
    print(f'(참고) 2025 시장 implied AUC={roc_auc_score(d25.home_win, ph):.3f} acc={accuracy_score(d25.home_win,(ph>0.5).astype(int)):.1%}')


if __name__ == '__main__':
    main()
