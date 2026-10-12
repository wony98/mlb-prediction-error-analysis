# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
import sys, io

import numpy as np

import pandas as pd

import lightgbm as lgb

import warnings

from _mlb_train_v3 import clean_universe

SPA = ['spa_era', 'spa_whip', 'spa_k9', 'spa_bb9', 'spa_hr9', 'spa_ip']

PARAMS = dict(objective='binary', n_estimators=250, learning_rate=0.02, num_leaves=31,
              max_depth=5, min_child_samples=80, subsample=0.8, colsample_bytree=0.7,
              reg_lambda=2.0, random_state=42, n_jobs=-1, verbose=-1)

def load():
    df = pd.read_parquet('master_5seasons_train.parquet')
    pr = pd.read_csv('mlb_sp_asof_prior.csv')
    for sd in ('home', 'away'):
        w = pr[pr.side == sd].set_index('game_pk')[SPA].add_prefix(sd + '_')
        df = df.merge(w, left_on='gamePk', right_index=True, how='left')
    for s in SPA:
        df['d_' + s] = df['home_' + s] - df['away_' + s]
    return df
