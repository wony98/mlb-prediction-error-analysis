# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""오프닝 기준점 전략용 배포 모델 학습+저장 (간단 clean 모델, forward-test grade).

feature = 시장(home_WIN_LOSS/away_WIN_LOSS/run_margin/run_total_baseline) + as-of-prior SP(spa_era/whip/k9/bb9/hr9, home/away/diff).
누설 없음(spa는 현재경기 제외). 2021~2025 전체 학습.

산출: mlb_clean_win_loss_model.pkl (LGBMClassifier) + mlb_clean_win_loss_features.json
서빙: 라이브에서 동일 feature(시장 market_price + pregame SP) 구성 → predict_proba → 오프닝 implied 대비 edge.

usage: py _mlb_train_deploy.py
"""
import sys, io, json
import numpy as np
import pandas as pd
import lightgbm as lgb
import joblib
import warnings
warnings.filterwarnings('ignore')
from _mlb_train_v3 import clean_universe

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

SPA = ['spa_era', 'spa_whip', 'spa_k9', 'spa_bb9', 'spa_hr9', 'spa_ip']
P = dict(objective='binary', n_estimators=250, learning_rate=0.02, num_leaves=31, max_depth=5,
         min_child_samples=80, subsample=0.8, colsample_bytree=0.7, reg_lambda=2.0,
         random_state=42, n_jobs=-1, verbose=-1)


def main():
    df = pd.read_parquet('master_5seasons_train.parquet')
    pr = pd.read_csv('mlb_sp_asof_prior.csv')
    for sd in ('home', 'away'):
        w = pr[pr.side == sd].set_index('game_pk')[SPA].add_prefix(sd + '_')
        df = df.merge(w, left_on='gamePk', right_index=True, how='left')
    for s in SPA:
        df['d_' + s] = df['home_' + s] - df['away_' + s]
    # FULL clean feature universe (leak-free 전부). 개인용이라 서빙 부담 무관.
    feats = list(dict.fromkeys(clean_universe(df)))

    d = df[df.home_win.notna()]
    m = lgb.LGBMClassifier(**P)
    m.fit(d[feats], d.home_win)
    joblib.dump(m, 'mlb_clean_win_loss_model.pkl')
    meta = {
        'features': feats, 'target': 'home_win', 'n_train': int(len(d)),
        'seasons': '2021-2025', 'params': P,
        'note': 'Historical as-of-prior SP features and market-probability comparison; see report for timing limitations.',
        'edge_ref': 'Opening implied probability normalized across both sides; research comparison only.',
    }
    json.dump(meta, open('mlb_clean_win_loss_features.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    imp = pd.Series(m.feature_importances_, index=feats).sort_values(ascending=False)
    print('saved mlb_clean_win_loss_model.pkl + mlb_clean_win_loss_features.json')
    print(f'feat {len(feats)}, train {len(d)}경기')
    print('top importance:', imp.head(8).round(0).to_dict())


if __name__ == '__main__':
    main()
