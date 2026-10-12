# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""Phase 4~6: LightGBM walk-forward 학습 + baseline 대비 평가.

walk-forward: train 2021~2023 / val 2024 / test 2025 (2026 제외).
각 타깃(home_win, upper)에 대해 base-feature(기존 v2) vs full-feature(신규 250) 동일 split 비교.

usage: py _mlb_train_v3.py
"""
import sys, io
import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.metrics import roc_auc_score, log_loss, accuracy_score
import warnings
warnings.filterwarnings('ignore')

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# 기존 v2 feature (apples-to-apples baseline)
BASE_WIN_LOSS = ['era_diff', 'k9_diff', 'bb9_diff', 'ops_diff', 'wr5_diff', 'wr10_diff',
           'rs10_diff', 'bp_fatigue_1d_diff', 'run_total_baseline', 'run_margin', 'home_WIN_LOSS', 'park_HR']
BASE_RUN_TOTAL = ['era_sum', 'whip_sum', 'k9_sum', 'bb9_sum', 'hr9_sum', 'ops_sum', 'rs5_sum', 'rs10_sum',
           'ra5_sum', 'ra10_sum', 'bp_fatigue_sum', 'park_factor', 'park_R', 'park_HR', 'park_H',
           'era_max', 'whip_max', 'run_margin', 'home_WIN_LOSS', 'run_total_baseline', 'era_vs_run_total', 'ops_vs_run_total']

# 누설/비feature 제외 (타깃·결과·id·문자열)
EXCLUDE = {'home_win', 'upper', '홈팀결과', '홈팀득점', '원정팀득점', '총득점',
           'gamePk', 'season', 'home_sp_id', 'away_sp_id', 'home_team_id', 'prior_for_season',
           'home_team', 'away_team', 'date', 'home_sp_name', 'away_sp_name', 'datetime_kst'}

LGB_PARAMS = dict(objective='binary', n_estimators=600, learning_rate=0.02,
                  num_leaves=31, max_depth=5, min_child_samples=80,
                  subsample=0.8, colsample_bytree=0.7, reg_lambda=2.0,
                  random_state=42, n_jobs=-1, verbose=-1)


def feature_universe(df):
    num = df.select_dtypes(include=[np.number]).columns
    return [c for c in num if c not in EXCLUDE]


# 누설 컬럼: master 시즌/최근 누적 스탯 (현재경기 포함 = post-inclusive). lag1/roll/prior는 clean.
_LEAK_STEMS = ['era', 'whip', 'k9', 'bb9', 'hr9', 'ops', 'wr5', 'wr10', 'rs5', 'rs10', 'ra5', 'ra10']


def is_leaky(c):
    if 'lag1' in c or 'roll' in c or 'prior' in c:  # 내 as-of-date feature는 clean
        return False
    cl = c.lower()
    for s in _LEAK_STEMS:
        if cl in (f'home_sp_{s}', f'away_sp_{s}', f'home_lineup_{s}', f'away_lineup_{s}',
                  f'home_{s}', f'away_{s}', f'{s}_diff', f'{s}_sum', f'{s}_max', f'{s}_vs_run_total'):
            return True
    return False


def clean_universe(df):
    return [c for c in feature_universe(df) if not is_leaky(c)]


def fit_eval(df, target, feats, label):
    df = df[df[target].notna()]  # 타깃 NaN 제거 (run_total_baseline 결측 등)
    tr = df[df.season <= 2023]
    va = df[df.season == 2024]
    te = df[df.season == 2025]
    Xtr, ytr = tr[feats], tr[target]
    Xva, yva = va[feats], va[target]
    Xte, yte = te[feats], te[target]
    m = lgb.LGBMClassifier(**LGB_PARAMS)
    m.fit(Xtr, ytr, eval_set=[(Xva, yva)], eval_metric='auc',
          callbacks=[lgb.early_stopping(50, verbose=False)])
    out = {}
    for nm, X, y in [('val', Xva, yva), ('test', Xte, yte)]:
        p = m.predict_proba(X)[:, 1]
        out[nm] = dict(auc=roc_auc_score(y, p), ll=log_loss(y, p),
                       acc=accuracy_score(y, (p > 0.5).astype(int)), n=len(y))
    print(f'\n=== {label} ({len(feats)} feats, best_iter={m.best_iteration_}) ===')
    for nm in ['val', 'test']:
        o = out[nm]
        print(f'  {nm}: AUC={o["auc"]:.4f}  logloss={o["ll"]:.4f}  acc={o["acc"]:.4f}  (n={o["n"]})')
    return m, out


def sweep_topk(df, target, full_feats, importance, grid=(15, 25, 40, 60, 90, 130)):
    """top-K importance subset sweep. K는 val AUC로 선택, test는 보고용."""
    print(f'\n--- {target} top-K sweep (K는 val로 선택) ---')
    best = None
    for k in grid:
        feats = list(importance.head(k).index)
        _, o = fit_eval_quiet(df, target, feats)
        tag = ''
        if best is None or o['val']['auc'] > best[1]:
            best = (k, o['val']['auc'], o); tag = ' *'
        print(f'  K={k:3d}: val AUC={o["val"]["auc"]:.4f} acc={o["val"]["acc"]:.4f} | test AUC={o["test"]["auc"]:.4f} acc={o["test"]["acc"]:.4f}{tag}')
    k, _, o = best
    print(f'  >>> best-by-val K={k}: test AUC={o["test"]["auc"]:.4f} acc={o["test"]["acc"]:.4f}')
    return k


def fit_eval_quiet(df, target, feats):
    df = df[df[target].notna()]
    tr, va, te = df[df.season <= 2023], df[df.season == 2024], df[df.season == 2025]
    m = lgb.LGBMClassifier(**LGB_PARAMS)
    m.fit(tr[feats], tr[target], eval_set=[(va[feats], va[target])], eval_metric='auc',
          callbacks=[lgb.early_stopping(50, verbose=False)])
    out = {}
    for nm, X, y in [('val', va[feats], va[target]), ('test', te[feats], te[target])]:
        p = m.predict_proba(X)[:, 1]
        out[nm] = dict(auc=roc_auc_score(y, p), acc=accuracy_score(y, (p > 0.5).astype(int)))
    return m, out


def main():
    df = pd.read_parquet('master_5seasons_train.parquet')
    print('rows', len(df), '| 시즌:', df.groupby('season').size().to_dict())
    full = feature_universe(df)
    print(f'full feature universe: {len(full)}개')

    results = {}
    for target, base in [('home_win', BASE_WIN_LOSS), ('upper', BASE_RUN_TOTAL)]:
        print(f'\n{"="*60}\nTARGET: {target}\n{"="*60}')
        base = [c for c in base if c in df.columns]
        mb, ob = fit_eval(df, target, base, f'{target} BASE')
        mf, of = fit_eval(df, target, full, f'{target} FULL')
        results[target] = (ob, of, mf, full)
        # importance top
        imp = pd.Series(mf.feature_importances_, index=full).sort_values(ascending=False)
        print(f'  [FULL top15 importance] {imp.head(15).round(0).to_dict()}')
        d_auc = of['test']['auc'] - ob['test']['auc']
        d_acc = of['test']['acc'] - ob['test']['acc']
        print(f'  >>> FULL vs BASE (test 2025): ΔAUC {d_auc:+.4f}  Δacc {d_acc:+.4f}')
        sweep_topk(df, target, full, imp)


if __name__ == '__main__':
    main()
