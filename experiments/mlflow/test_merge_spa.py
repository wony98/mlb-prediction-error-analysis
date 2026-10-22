# MLflow tracking experiment (2026-10-23). Synthetic checks for the as-of-prior merge.
# usage: python experiments/mlflow/test_merge_spa.py   (exit 0 = pass)
"""merge_spa 의 반례 검사: 정상 / (game_pk, side) 중복 키 / 중복 키의 값 충돌.
행 수와 gamePk 집합이 바뀌지 않고, 중복 키 경기의 spa 값은 NaN 으로 제외되며 목록이 기록돼야 한다."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from train_clean_vs_leaky import SPA, merge_spa  # noqa: E402


def games(n=5):
    return pd.DataFrame({'gamePk': [100 + i for i in range(n)], 'season': [2021] * n, 'home_win': [1, 0, 1, 0, 1][:n]})


def spa_rows(pks, side, base=3.0):
    return pd.DataFrame({'game_pk': pks, 'sp_id': [1] * len(pks), 'side': [side] * len(pks),
                         **{c: [base] * len(pks) for c in SPA}})


def test_normal():
    df = games()
    pr = pd.concat([spa_rows(df.gamePk, 'home'), spa_rows(df.gamePk, 'away', 2.0)])
    out, info = merge_spa(df, pr)
    assert len(out) == 5 and set(out.gamePk) == set(df.gamePk)
    assert info['spa_duplicate_keys'] == 0 and info['spa_excluded_games'] == []
    assert np.allclose(out['d_spa_era'], 1.0)


def test_duplicate_key_identical_values():
    df = games()
    pr = pd.concat([spa_rows(df.gamePk, 'home'), spa_rows(df.gamePk, 'away', 2.0), spa_rows([101], 'home')])  # 101 home 두 번
    out, info = merge_spa(df, pr)
    assert len(out) == 5, '중복 키가 있어도 경기 행은 늘지 않아야 한다'
    assert info['spa_duplicate_keys'] == 1 and info['spa_excluded_games'] == [101]
    assert np.isnan(out.loc[out.gamePk == 101, 'home_spa_era']).all(), '중복 키 경기의 spa 는 제외(NaN)'
    assert not np.isnan(out.loc[out.gamePk == 101, 'away_spa_era']).any(), '중복 없는 side 는 유지'


def test_duplicate_key_conflicting_values():
    df = games()
    pr = pd.concat([spa_rows(df.gamePk, 'home'), spa_rows(df.gamePk, 'away', 2.0), spa_rows([103], 'away', 9.0)])  # 값 충돌
    out, info = merge_spa(df, pr)
    assert len(out) == 5 and info['spa_excluded_games'] == [103]
    assert np.isnan(out.loc[out.gamePk == 103, 'away_spa_era']).all()
    assert out['gamePk'].is_unique


def test_naive_merge_would_duplicate():
    """참고: 중복 키를 걸러내지 않으면 행이 늘어난다(이전 버전의 결함 재현)."""
    df = games()
    pr = pd.concat([spa_rows(df.gamePk, 'home'), spa_rows(df.gamePk, 'away', 2.0), spa_rows([101], 'home')])
    w = pr[pr.side == 'home'].set_index('game_pk')[SPA].add_prefix('home_')
    naive = df.merge(w, left_on='gamePk', right_index=True, how='left')
    assert len(naive) == 6, '검증 없는 병합은 6행이 된다'


def _games_with_market():
    """apply_column_map 검사용: 원본 이름의 시장 열을 가진 작은 표."""
    df = games()
    for c in ('home_ML', 'away_ML', 'ou_line', 'spread', 'era_vs_ou', 'ops_vs_ou'):
        df[c] = 1.0
    df['over'] = [1, 0, 1, 0, 1]
    return df


FULL_MAP = {'home_ML': 'home_WIN_LOSS', 'away_ML': 'away_WIN_LOSS', 'ou_line': 'run_total_baseline', 'spread': 'run_margin',
            'over': 'upper', 'era_vs_ou': 'era_vs_run_total', 'ops_vs_ou': 'ops_vs_run_total'}


def test_column_map_normal():
    from train_clean_vs_leaky import REQUIRED_AFTER_MAP, apply_column_map
    out, info = apply_column_map(_games_with_market(), FULL_MAP)
    assert all(c in out.columns for c in REQUIRED_AFTER_MAP)
    assert info['renamed_columns'] == 7 and info['map_entries_absent_in_data'] == 0
    assert 'home_ML' not in out.columns, '원본 이름은 남지 않아야 한다'


def test_column_map_missing_required_fails():
    from train_clean_vs_leaky import apply_column_map
    m = dict(FULL_MAP); m.pop('over')  # upper 가 만들어지지 않음
    try:
        apply_column_map(_games_with_market(), m)
    except AssertionError as e:
        assert '필요한 열' in str(e)
    else:
        raise AssertionError('필수 열이 없으면 중단해야 한다')


def test_column_map_collision_fails():
    from train_clean_vs_leaky import apply_column_map
    df = _games_with_market()
    df['home_WIN_LOSS'] = 0.0  # 매핑 대상 이름이 이미 존재 → 중복 열
    try:
        apply_column_map(df, FULL_MAP)
    except AssertionError as e:
        assert '중복' in str(e)
    else:
        raise AssertionError('열 이름 충돌이면 중단해야 한다')


if __name__ == '__main__':
    for fn in (test_normal, test_duplicate_key_identical_values, test_duplicate_key_conflicting_values, test_naive_merge_would_duplicate,
               test_column_map_normal, test_column_map_missing_required_fails, test_column_map_collision_fails):
        fn()
        print('ok', fn.__name__)
    print('all merge checks passed')
