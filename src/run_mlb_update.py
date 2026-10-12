# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""
run_mlb_update.py (로컬 변환 — 원본: MLB 경기 후 결과 갱신 스크립트.ipynb)

매일 MLB 경기 종료 후 1회 실행:
- predictions_v2_2026_all.csv 에서 실제 결과 미갱신 날짜 찾기
- MLB statsapi로 경기 결과 fetch
- 실제_홈득점/원정득점/홈승/총득점 + 승패_적중/총득점_적중 추가
- 시즌 적중 결과 집계 출력

원본과 차이:
- Colab drive.mount → 로컬 <DRIVE_ROOT> 경로로 직접 접근
"""
import argparse
import sys, io, os
if sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass

# ============================================================
# MLB 경기 후 결과 갱신 스크립트 (로컬 실행)
# 매일 경기 종료 후 1회 실행
# → predictions_v2_2026_all.csv에 실제 결과 + 적중 여부 추가
# ============================================================
import requests, pandas as pd, numpy as np

DRIVE = os.environ.get('MLB_DATA_DRIVE', r'<DATA_ROOT>')  # Colab parity uses /content/mlb_data
MLB_API = "https://statsapi.mlb.com/api/v1"
ALL_PATH = os.path.join(DRIVE, '2026', 'predictions_v2_2026_all.csv')


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument('date', nargs='?', help='optional target date (YYYY-MM-DD or YYYYMMDD)')
    args = ap.parse_args()
    if not args.date:
        return None
    raw = args.date.replace('-', '')
    if len(raw) != 8 or not raw.isdigit():
        raise SystemExit(f'invalid date: {args.date}')
    return f'{raw[:4]}-{raw[4:6]}-{raw[6:8]}'


target_date = parse_args()

pred = pd.read_csv(ALL_PATH)
pred['date'] = pd.to_datetime(pred['date'], errors='coerce').dt.strftime('%Y-%m-%d')
print(f"누적 예측: {len(pred)}행")

# 결과 없는 날짜만 처리
if '실제_홈득점' in pred.columns:
    need_update = pred[pred['실제_홈득점'].isna()]['date'].unique()
else:
    need_update = pred['date'].unique()
    pred['실제_홈득점'] = np.nan
    pred['실제_원정득점'] = np.nan
    pred['실제_홈승'] = np.nan
    pred['실제_총득점'] = np.nan
    pred['승패_적중'] = np.nan
    pred['총득점_적중'] = np.nan

if target_date is not None:
    if not (pred['date'] == target_date).any():
        print(f'[WARN] target date not present in legacy predictions; skip v2 update: {target_date}')
        need_update = np.array([], dtype=object)
    else:
        need_update = np.array([target_date])

print(f"갱신 필요 날짜: {list(need_update)}\n")

updated = 0
for game_date in need_update:
    url = f"{MLB_API}/schedule?sportId=1&date={game_date}&gameType=R&hydrate=linescore"
    data = requests.get(url, timeout=10).json()

    results = {}
    for d in data.get('dates', []):
        for g in d.get('games', []):
            if g['status']['abstractGameState'] == 'Final':
                hs = g['teams']['home'].get('score', 0)
                aws = g['teams']['away'].get('score', 0)
                results[g['gamePk']] = (float(hs), float(aws))

    if not results:
        print(f"  {game_date}: 아직 미종료")
        continue

    mask = pred['date'] == game_date
    for idx in pred[mask].index:
        pk = pred.at[idx, 'gamePk']
        res = results.get(pk)
        if not res: continue

        hs, aws = res
        home_win = 1 if hs > aws else 0
        total = hs + aws

        pred.at[idx, '실제_홈득점'] = hs
        pred.at[idx, '실제_원정득점'] = aws
        pred.at[idx, '실제_홈승'] = home_win
        pred.at[idx, '실제_총득점'] = total

        # 승패 적중
        pred_home = 1 if pred.at[idx, 'win_prob'] >= 0.5 else 0
        pred.at[idx, '승패_적중'] = 1 if pred_home == home_win else 0

        # 총득점 적중
        run_total = pred.at[idx, 'run_total_baseline']
        if pd.notna(run_total) and total != run_total:
            pred_upper = 1 if pred.at[idx, 'run_total_prob'] >= 0.5 else 0
            actual_upper = 1 if total > run_total else 0
            pred.at[idx, '총득점_적중'] = 1 if pred_upper == actual_upper else 0

        updated += 1

    matched = pred[(pred['date']==game_date) & pred['실제_홈득점'].notna()]
    print(f"  {game_date}: {len(matched)}경기 갱신")

# 저장 (로컬 Google Drive mount 특성상 직접 to_csv)
pred.to_csv(ALL_PATH, index=False)
print(f"\n[SAVED] {ALL_PATH} ({updated}경기 갱신)")

# ============================================================
# 시즌 성적 리포트
# ============================================================
# Portfolio excerpt: result matching and CSV persistence above are retained.
# Financial-return and automatic downstream-report phases are not included.
has_result = pred[pred['실제_홈득점'].notna()]
run_total_valid = has_result[has_result['총득점_적중'].notna()]
print({'scored_games': len(has_result), 'win_loss_accuracy': has_result['승패_적중'].mean(),
       'run_total_scored_games': len(run_total_valid), 'run_total_accuracy': run_total_valid['총득점_적중'].mean()})
