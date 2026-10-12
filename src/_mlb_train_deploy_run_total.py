# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
"""Historical total-runs regression training. Amount tiers and financial-return prose omitted; formulas retained."""
import sys, io, json
import numpy as np
import pandas as pd
import lightgbm as lgb
import joblib
import warnings
warnings.filterwarnings('ignore')
from _mlb_open_validate import load
from _mlb_train_v3 import clean_universe
from _mlb_run_total_validate_clean import is_closing_market
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
TARGET = '총득점'
P = dict(objective='regression', n_estimators=250, learning_rate=0.02, num_leaves=31, max_depth=5, min_child_samples=80, subsample=0.8, colsample_bytree=0.7, reg_lambda=2.0, random_state=42, n_jobs=-1, verbose=-1)
SIGNAL_TH = 1.0

def main():
    df = load()
    feats = [c for c in dict.fromkeys(clean_universe(df)) if not is_closing_market(c)]
    d = df[df[TARGET].notna()]
    m = lgb.LGBMRegressor(**P)
    m.fit(d[feats], d[TARGET])
    bias = float((m.predict(d[feats]) - d.run_total_baseline).mean())
    joblib.dump(m, 'mlb_clean_run_total_model.pkl')
    meta = {'features': feats, 'target': TARGET, 'model_type': 'regressor', 'bias': bias, 'signal_th': SIGNAL_TH, 'n_train': int(len(d)), 'seasons': '2021-2025', 'note': 'Historical total-runs regression and supplied market-line comparison. See report for evaluation limitations.', 'edge_ref': 'Historical total-runs regression and supplied market-line comparison. See report for evaluation limitations.'}
    json.dump(meta, open('mlb_clean_run_total_features.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    imp = pd.Series(m.feature_importances_, index=feats).sort_values(ascending=False)
    print('saved mlb_clean_run_total_model.pkl + mlb_clean_run_total_features.json')
    print(f'feat {len(feats)}, train {len(d)}경기, bias={bias:+.3f}')
    print('top importance:', imp.head(10).round(0).to_dict())
if __name__ == '__main__':
    main()
