# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
import sys, io

import numpy as np

import pandas as pd

from sklearn.metrics import roc_auc_score

import warnings

from _mlb_train_v3 import clean_universe, fit_eval

SPA = ['spa_era', 'spa_whip', 'spa_k9', 'spa_bb9', 'spa_hr9', 'spa_ip']

def is_closing_market(c):
    """클로징/움직임 기반 마켓 컬럼 — RUN_TOTAL 모델이 학습하면 '클로징 베끼기'가 됨."""
    cl = c.lower()
    return ('close' in cl) or ('shift' in cl) or ('market_fee' in cl)
