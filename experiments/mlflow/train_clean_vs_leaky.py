# MLflow tracking experiment (2026-10-23). Not the original operational deployment.
# Reuses the repository's own feature/leak definitions (src/_mlb_train_v3.py,
# experiments/leakage/_mlb_baseline_clean.py) and records every run with MLflow.
# -*- coding: utf-8 -*-
"""규칙 탐지 누수 피처 세트 vs 제거 세트 재실험을 MLflow로 추적한다.

실험 설계
- 데이터: 운영 당시 학습 테이블(2021~2025, 경기 단위). 저장소에 포함하지 않는다. --data 로 경로 지정.
  원본 열 이름은 이 저장소의 중립 용어 정리(docs/CODE_CHANGELOG.md 참고)와 직접 호환되지 않으므로, 로드 직후
  --column-map(로컬 JSON, 저장소 미포함)으로 중립 이름에 연결한 뒤 저장소의 정의(BASE_WIN_LOSS·BASE_RUN_TOTAL·
  is_leaky·CLEAN_SWAP)를 그대로 쓴다. 매핑 뒤 필요한 열이 없으면 중단한다.
- 분할: train season<=2023 / val 2024 / test 2025 (src/_mlb_train_v3.py 와 동일 프로토콜).
- 타깃: home_win(홈 승리), upper(총득점 기준점 초과). 둘 다 이진 분류.
- 피처 세트(각각 별도 run):
    base_v2_leaky      : 종결 v2 베이스라인 12/22개 피처 (era_diff 등 규칙 탐지 누적 스탯 포함)
    base_v2_clean_swap : 위 베이스라인에서 era/k9/bb9_diff 를 as-of-prior 진짜ERA(d_spa_*)로 교체 (experiments/leakage 와 동일,
                         승패 베이스라인 전용 규칙이라 upper 에서는 base_v2_leaky 와 같다)
    full_leaky         : feature_universe(수치형 전부, 타깃·id 제외) — 원래 _mlb_train_v3 main 의 FULL
    full_clean         : clean_universe = full_leaky 에서 is_leaky() 컬럼 제거 (lag1/roll/prior 는 유지)
    full_clean_plus_spa: full_clean + as-of-prior SP 스탯(spa_*, home/away/diff) — _mlb_train_deploy 와 같은 구성
    market_price_implied: (참고 run) 승패 시장가격에서 환산한 확률의 val/test AUC·acc. 모델 아님. 모델 run 에도 같은 경기
                         집합(시장가격이 있는 경기) 기준 지표를 *_marketgames 로 함께 기록한다.
- "누수" 표기의 범위: src/_mlb_train_v3.is_leaky 의 이름 규칙이 탐지하는 현재 경기 포함 누적 스탯 열이다. 규칙 탐지 0 은
  전체 입력 시점 누수가 없다는 검증이 아니다(시장 피처·기타 열의 예측 시점은 별도 감사 대상).
- as-of-prior SP 병합: (game_pk, side) 키가 중복인 행은 어느 값이 맞는지 원본 근거가 없으므로 해당 경기의 spa 값을
  제외(NaN)하고 목록을 기록한다(fail-closed). 병합은 many_to_one 검증, 병합 후 행 수·gamePk 유일성 검사.
- 모델: LightGBM 분류기, src/_mlb_train_v3.LGB_PARAMS 와 동일 하이퍼파라미터(n_jobs 만 인자화), val AUC early stopping 50.
- 기록: params(타깃·피처세트·피처 수·파라미터·분할), metrics(val/test auc·logloss·acc·n, *_marketgames, best_iteration),
        tags(data_sha256·rows·spa_sha256·column_map_sha256·git_commit·code_sha256(스크립트·피처 정의·requirements)·run_batch_id),
        artifacts(features.json, rule_leaky_columns.json, importance.csv, result.json, data_checks.json, code_snapshot/, 모델).
- 출력: --out 아래에만 쓴다. 저장소 루트의 기존 pkl/JSON 은 건드리지 않는다.

usage (격리 venv 에서; <DATA_ROOT> 는 데이터·매핑·추적 DB 를 두는 로컬 폴더):
  python experiments/mlflow/train_clean_vs_leaky.py --data <DATA_ROOT>/train.parquet --spa <DATA_ROOT>/sp_asof_prior.csv \
      --column-map <DATA_ROOT>/column_map_neutral.json --tracking-uri sqlite:///<DATA_ROOT>/mlflow/mlflow.db \
      --artifact-root file:<DATA_ROOT>/mlflow/mlartifacts --out experiments/mlflow/outputs
  python experiments/mlflow/train_clean_vs_leaky.py ... --quick        # n_estimators 50 smoke
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(REPO / 'experiments' / 'leakage'))

import lightgbm as lgb  # noqa: E402
import mlflow  # noqa: E402
from sklearn.metrics import accuracy_score, log_loss, roc_auc_score  # noqa: E402

from _mlb_train_v3 import BASE_RUN_TOTAL, BASE_WIN_LOSS, EXCLUDE, LGB_PARAMS, clean_universe, feature_universe, is_leaky  # noqa: E402
from _mlb_baseline_clean import CLEAN_SWAP  # noqa: E402

SPA = ['spa_era', 'spa_whip', 'spa_k9', 'spa_bb9', 'spa_hr9', 'spa_ip']
MARKET_HOME, MARKET_AWAY = 'home_WIN_LOSS', 'away_WIN_LOSS'  # 승패 시장가격(미국식 표기) 열의 중립 이름
REQUIRED_AFTER_MAP = ['home_win', 'upper', MARKET_HOME, MARKET_AWAY, 'run_total_baseline', 'run_margin', 'era_vs_run_total', 'ops_vs_run_total']
SCRIPT_VERSION = '2026-10-23.3'
PROTOCOL = 'train<=2023 / val 2024 / test 2025, early_stopping(val auc, 50)'
CODE_FILES = {  # 실행 당시 코드를 고정하기 위해 SHA 와 사본을 run 에 남기는 파일
    'script': HERE / 'train_clean_vs_leaky.py',
    'feature_defs': REPO / 'src' / '_mlb_train_v3.py',
    'clean_swap_defs': REPO / 'experiments' / 'leakage' / '_mlb_baseline_clean.py',
    'requirements': HERE / 'requirements.txt',
}


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def git_commit(repo: Path) -> tuple[str, bool]:
    try:
        r = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--short', 'HEAD'], capture_output=True, text=True, timeout=10)
        # 추적 중인 파일의 변경만 본다. 실행이 만드는 outputs/ 는 untracked 라 dirty 로 세지 않는다.
        dirty = subprocess.run(['git', '-C', str(repo), 'status', '--porcelain', '--untracked-files=no'],
                               capture_output=True, text=True, timeout=10).stdout.strip()
        return (r.stdout.strip() or 'unknown'), bool(dirty)
    except Exception:
        return 'unknown', True


def apply_column_map(df: pd.DataFrame, column_map: dict) -> tuple[pd.DataFrame, dict]:
    """원본 열 이름 → 저장소 중립 이름. 매핑 대상이 실제로 있는지, 결과가 중복되지 않는지, 필요한 열이 갖춰졌는지 검사."""
    present = {k: v for k, v in column_map.items() if k in df.columns}
    out = df.rename(columns=present)
    assert out.columns.is_unique, '매핑 후 열 이름 중복'
    missing = [c for c in REQUIRED_AFTER_MAP if c not in out.columns]
    assert not missing, f'매핑 후 필요한 열이 없음: {missing}'
    return out, {'renamed_columns': len(present), 'map_entries': len(column_map), 'map_entries_absent_in_data': len(column_map) - len(present)}


def merge_spa(df: pd.DataFrame, pr: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """as-of-prior SP 스탯을 경기 행에 붙인다. (game_pk, side) 중복 키는 fail-closed 로 제외하고 기록한다.
    반환 df 는 입력과 행 수·gamePk 집합이 같아야 한다(검증)."""
    n0, keys0 = len(df), set(df['gamePk'])
    dup_mask = pr.duplicated(['game_pk', 'side'], keep=False)
    dup_rows = pr[dup_mask]
    excluded_games = sorted(int(x) for x in dup_rows['game_pk'].unique())
    pr_ok = pr[~dup_mask]
    out = df
    for sd in ('home', 'away'):
        w = pr_ok[pr_ok.side == sd].set_index('game_pk')[SPA].add_prefix(sd + '_')
        assert w.index.is_unique, f'{sd} spa index not unique after dedup'
        out = out.merge(w, left_on='gamePk', right_index=True, how='left', validate='many_to_one')
    for s in SPA:
        out['d_' + s] = out['home_' + s] - out['away_' + s]
    assert len(out) == n0, f'row count changed by merge: {n0} -> {len(out)}'
    assert set(out['gamePk']) == keys0 and not out['gamePk'].duplicated().any(), 'gamePk set/uniqueness changed by merge'
    info = {
        'spa_rows': int(len(pr)), 'spa_duplicate_key_rows': int(dup_mask.sum()),
        'spa_duplicate_keys': int(dup_rows.groupby(['game_pk', 'side']).ngroups),
        'spa_excluded_games': excluded_games, 'spa_excluded_games_in_data': [g for g in excluded_games if g in keys0],
        'spa_coverage_home_era': float(out['home_spa_era'].notna().mean()),
        'spa_policy': 'duplicate (game_pk, side) keys excluded (values set to NaN); no row dropped from games table',
    }
    return out, info


def load_data(data: Path, spa: Path | None, column_map: dict | None):
    df = pd.read_parquet(data)
    checks = {
        'stage': 'before_merge',
        'rows': int(len(df)), 'cols': int(df.shape[1]),
        'seasons': {int(k): int(v) for k, v in df.groupby('season').size().to_dict().items()},
        'gamePk_duplicates': int(df['gamePk'].duplicated().sum()),
        'date_min': str(pd.to_datetime(df['date']).min().date()), 'date_max': str(pd.to_datetime(df['date']).max().date()),
    }
    assert set(checks['seasons']) == {2021, 2022, 2023, 2024, 2025}, checks['seasons']
    assert checks['gamePk_duplicates'] == 0, 'gamePk 중복'
    if column_map:
        df, info = apply_column_map(df, column_map)
        checks.update(info)
    checks['target_nan'] = {t: int(df[t].isna().sum()) for t in ('home_win', 'upper')}
    if spa is not None:
        df, info = merge_spa(df, pd.read_csv(spa))
        checks.update(info)
    checks['stage'] = 'after_merge'
    checks['rows_after_merge'] = int(len(df))
    checks['gamePk_duplicates_after_merge'] = int(df['gamePk'].duplicated().sum())
    checks['split_rows'] = {}
    for t in ('home_win', 'upper'):
        tr, va, te = split(df, t)
        checks['split_rows'][t] = {'train': int(len(tr)), 'val': int(len(va)), 'test': int(len(te))}
    return df, checks


def feature_sets(df: pd.DataFrame, target: str, has_spa: bool) -> dict[str, list[str]]:
    base = BASE_WIN_LOSS if target == 'home_win' else BASE_RUN_TOTAL
    base = [c for c in base if c in df.columns]
    full_leaky = feature_universe(df)
    spa_cols = [c for c in df.columns if 'spa_' in c]
    full_leaky = [c for c in full_leaky if c not in spa_cols]
    full_clean = [c for c in full_leaky if not is_leaky(c)]
    sets = {'base_v2_leaky': base, 'full_leaky': full_leaky, 'full_clean': full_clean}
    if has_spa:
        swap = [CLEAN_SWAP.get(f, f) for f in base]
        sets['base_v2_clean_swap'] = [c for c in swap if c in df.columns]
        sets['full_clean_plus_spa'] = full_clean + [c for c in spa_cols if c in df.columns]
    return sets


def split(df: pd.DataFrame, target: str):
    d = df[df[target].notna()]
    return d[d.season <= 2023], d[d.season == 2024], d[d.season == 2025]


def market_mask(d: pd.DataFrame) -> pd.Series:
    return d[MARKET_HOME].notna() & d[MARKET_AWAY].notna()


def evaluate(m, X, y):
    p = m.predict_proba(X)[:, 1]
    return dict(auc=float(roc_auc_score(y, p)), logloss=float(log_loss(y, p)),
                acc=float(accuracy_score(y, (p > 0.5).astype(int))), n=int(len(y)))


def american_to_prob(home_price, away_price):
    def dec(x):
        x = pd.to_numeric(x, errors='coerce')
        return np.where(x > 0, 1 + x / 100, np.where(x < 0, 1 + 100 / -x, np.nan))
    dh, da = dec(home_price), dec(away_price)
    return (1 / dh) / (1 / dh + 1 / da)


def run_model(df, target, name, feats, params, tags, out_dir: Path, checks_path: Path, snapshot_dir: Path, log_model: bool):
    tr, va, te = split(df, target)
    with mlflow.start_run(run_name=f'{target}__{name}') as run:
        mlflow.set_tags({**tags, 'feature_set': name, 'target': target, 'kind': 'model'})
        mlflow.log_params({'target': target, 'feature_set': name, 'n_features': len(feats), 'protocol': PROTOCOL,
                           'n_train': len(tr), 'n_val': len(va), 'n_test': len(te), **{f'lgb_{k}': v for k, v in params.items()}})
        m = lgb.LGBMClassifier(**params)
        t0 = time.time()
        m.fit(tr[feats], tr[target], eval_set=[(va[feats], va[target])], eval_metric='auc',
              callbacks=[lgb.early_stopping(50, verbose=False)])
        fit_s = time.time() - t0
        res = {s: evaluate(m, X[feats], X[target]) for s, X in (('val', va), ('test', te))}
        for s, X in (('val', va), ('test', te)):  # 시장가격 있는 경기 집합(market_price_implied 와 같은 모집단)
            Xo = X[market_mask(X)]
            res[f'{s}_marketgames'] = evaluate(m, Xo[feats], Xo[target])
        for s, o in res.items():
            for k, v in o.items():
                mlflow.log_metric(f'{s}_{k}', v)
        mlflow.log_metric('best_iteration', int(m.best_iteration_ or 0))
        mlflow.log_metric('fit_seconds', fit_s)
        rd = out_dir / f'{target}__{name}'
        rd.mkdir(parents=True, exist_ok=True)
        json.dump(feats, open(rd / 'features.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        leaky = [c for c in feats if is_leaky(c)]
        json.dump({'rule_leaky_columns_in_set': leaky, 'n_rule_leaky': len(leaky), 'rule': 'src/_mlb_train_v3.is_leaky',
                   'note': 'name-rule detection only; not a full temporal-leakage audit'},
                  open(rd / 'rule_leaky_columns.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        pd.Series(m.feature_importances_, index=feats, name='importance').sort_values(ascending=False).to_csv(rd / 'importance.csv')
        json.dump({'params': params, 'protocol': PROTOCOL, 'results': res, 'best_iteration': int(m.best_iteration_ or 0)},
                  open(rd / 'result.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        for f in ('features.json', 'rule_leaky_columns.json', 'importance.csv', 'result.json'):
            mlflow.log_artifact(str(rd / f))
        mlflow.log_artifact(str(checks_path))
        mlflow.log_artifacts(str(snapshot_dir), artifact_path='code_snapshot')
        if log_model:
            mlflow.lightgbm.log_model(m, name='model')
        print(f'  {target:8s} {name:22s} feats={len(feats):3d} rule_leaky={len(leaky):3d} best_iter={m.best_iteration_} n_train={len(tr)} | '
              f'val AUC={res["val"]["auc"]:.4f} | test AUC={res["test"]["auc"]:.4f} acc={res["test"]["acc"]:.4f} '
              f'| test(market games n={res["test_marketgames"]["n"]}) AUC={res["test_marketgames"]["auc"]:.4f}  run={run.info.run_id[:8]}')
        return res, len(leaky)


def run_market_reference(df, tags, checks_path: Path, snapshot_dir: Path):
    with mlflow.start_run(run_name='home_win__market_price_implied'):
        mlflow.set_tags({**tags, 'feature_set': 'market_price_implied', 'target': 'home_win', 'kind': 'reference'})
        mlflow.log_params({'target': 'home_win', 'feature_set': 'market_price_implied',
                           'note': 'probability implied by win/loss market price, no model; population = games with both prices'})
        d = df[df.home_win.notna()]
        out = {}
        for s, season in (('val', 2024), ('test', 2025)):
            dd = d[(d.season == season) & market_mask(d)]
            p = american_to_prob(dd[MARKET_HOME], dd[MARKET_AWAY])
            ok = ~np.isnan(p)
            y, p = dd.home_win[ok], p[ok]
            out[s] = dict(auc=float(roc_auc_score(y, p)), acc=float(accuracy_score(y, (p > 0.5).astype(int))), n=int(len(y)))
            for k, v in out[s].items():
                mlflow.log_metric(f'{s}_marketgames_{k}', v)
        mlflow.log_artifact(str(checks_path))
        mlflow.log_artifacts(str(snapshot_dir), artifact_path='code_snapshot')
        print(f'  reference market_price_implied: val n={out["val"]["n"]} AUC={out["val"]["auc"]:.4f} | test n={out["test"]["n"]} AUC={out["test"]["auc"]:.4f}')
        return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', required=True)
    ap.add_argument('--spa', default=None, help='as-of-prior SP CSV. 없으면 spa 세트 생략')
    ap.add_argument('--column-map', default=None, help='원본 열 이름 → 저장소 중립 이름 JSON(로컬, 저장소 미포함)')
    ap.add_argument('--tracking-uri', default=f'sqlite:///{(HERE / "mlflow.db").as_posix()}',
                    help='MLflow 3.x 는 파일 저장소(./mlruns)를 유지보수 모드로 두므로 로컬 sqlite 를 기본으로 쓴다')
    ap.add_argument('--artifact-root', default=f'file:{(HERE / "mlartifacts").as_posix()}',
                    help='실험을 새로 만들 때 아티팩트 저장 위치(로컬 폴더)')
    ap.add_argument('--experiment-name', default='mlb-leakage-clean-vs-leaky')
    ap.add_argument('--out', default=str(HERE / 'outputs'))
    ap.add_argument('--targets', default='home_win,upper')
    ap.add_argument('--feature-sets', default='all')
    ap.add_argument('--n-jobs', type=int, default=4)
    ap.add_argument('--quick', action='store_true', help='smoke: n_estimators=50')
    ap.add_argument('--no-log-model', action='store_true')
    ap.add_argument('--export', default=None, help='runs_export.csv 경로(기본 --out/runs_export.csv)')
    a = ap.parse_args()

    data, spa = Path(a.data), (Path(a.spa) if a.spa else None)
    cmap_path = Path(a.column_map) if a.column_map else None
    column_map = json.load(open(cmap_path, encoding='utf-8')) if cmap_path else None
    out_dir = Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    df, checks = load_data(data, spa, column_map)
    checks_path = out_dir / 'data_checks.json'
    json.dump(checks, open(checks_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('data checks:', json.dumps(checks, ensure_ascii=False))

    params = dict(LGB_PARAMS)
    params['n_jobs'] = a.n_jobs
    if a.quick:
        params['n_estimators'] = 50
    commit, dirty = git_commit(REPO)
    batch_id = time.strftime('%Y%m%dT%H%M%S')
    code_sha = {k: sha256_file(p) for k, p in CODE_FILES.items() if p.exists()}
    snapshot_dir = out_dir / 'code_snapshot'
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    for k, p in CODE_FILES.items():
        if p.exists():
            shutil.copyfile(p, snapshot_dir / p.name)
    json.dump({'git_commit': commit, 'git_dirty': dirty, 'code_sha256': code_sha, 'script_version': SCRIPT_VERSION, 'run_batch_id': batch_id,
               'column_map_sha256': sha256_file(cmap_path) if cmap_path else ''},
              open(snapshot_dir / 'code_manifest.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    tags = {'data_file': data.name, 'data_sha256': sha256_file(data), 'data_rows': str(checks['rows']),
            'spa_file': spa.name if spa else '', 'spa_sha256': sha256_file(spa) if spa else '',
            'column_map_sha256': sha256_file(cmap_path) if cmap_path else '',
            'git_commit': commit, 'git_dirty': str(dirty), 'script_version': SCRIPT_VERSION, 'quick': str(a.quick),
            'run_batch_id': batch_id, **{f'code_sha256_{k}': v[:16] for k, v in code_sha.items()}}

    mlflow.set_tracking_uri(a.tracking_uri)
    if mlflow.get_experiment_by_name(a.experiment_name) is None:
        mlflow.create_experiment(a.experiment_name, artifact_location=a.artifact_root)
    mlflow.set_experiment(a.experiment_name)
    print(f'tracking={a.tracking_uri} artifacts={a.artifact_root} experiment={a.experiment_name} quick={a.quick} '
          f'git={commit}{"+dirty" if dirty else ""} batch={batch_id}')

    want = None if a.feature_sets == 'all' else set(a.feature_sets.split(','))
    summary = []
    for target in a.targets.split(','):
        sets = feature_sets(df, target, spa is not None)
        for name, feats in sets.items():
            if want and name not in want:
                continue
            res, n_leaky = run_model(df, target, name, feats, params, tags, out_dir, checks_path, snapshot_dir, not a.no_log_model)
            summary.append({'target': target, 'feature_set': name, 'n_features': len(feats), 'n_rule_leaky': n_leaky,
                            'run_batch_id': batch_id, 'git_commit': commit,
                            **{f'{s}_{k}': v for s, o in res.items() for k, v in o.items()}})
    if 'home_win' in a.targets.split(',') and (want is None or 'market_price_implied' in want):
        mk = run_market_reference(df, tags, checks_path, snapshot_dir)
        summary.append({'target': 'home_win', 'feature_set': 'market_price_implied', 'n_features': 0, 'n_rule_leaky': 0,
                        'run_batch_id': batch_id, 'git_commit': commit,
                        **{f'{s}_marketgames_{k}': v for s, o in mk.items() for k, v in o.items()}})

    pd.DataFrame(summary).to_csv(out_dir / 'summary.csv', index=False, encoding='utf-8-sig')
    exp = mlflow.get_experiment_by_name(a.experiment_name)
    runs = mlflow.search_runs(experiment_ids=[exp.experiment_id], filter_string=f"tags.run_batch_id = '{batch_id}'")
    keep = [c for c in runs.columns if c.split('.')[0] in ('run_id', 'start_time', 'status')
            or c.startswith(('metrics.', 'params.target', 'params.feature_set', 'params.n_features', 'params.n_train',
                             'tags.git_commit', 'tags.git_dirty', 'tags.data_sha256', 'tags.column_map_sha256', 'tags.quick',
                             'tags.run_batch_id', 'tags.code_sha256'))]
    runs[keep].to_csv(a.export or (out_dir / 'runs_export.csv'), index=False, encoding='utf-8-sig')
    print('summary ->', out_dir / 'summary.csv', '| export ->', a.export or (out_dir / 'runs_export.csv'), '| batch', batch_id)


if __name__ == '__main__':
    main()
