# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
import os
import pandas as pd

def append_csv(filepath, new_rows, label, key="gamePk"):
    """key: str 또는 list[str] — 중복 판단 기준 컬럼"""
    if not new_rows:
        print(f"  ⚠️ {label}: 데이터 없음")
        return
    df_new = pd.DataFrame(new_rows)
    if os.path.exists(filepath):
        df_old = pd.read_csv(filepath)
        keys = [key] if isinstance(key, str) else list(key)
        old_ok = all(k in df_old.columns for k in keys)
        new_ok = all(k in df_new.columns for k in keys)

        if old_ok and new_ok:
            if len(keys) == 1:
                new_vals = set(df_new[keys[0]].dropna().unique())
                dup_mask = df_old[keys[0]].isin(new_vals)
            else:
                new_tuples = set(df_new[keys].apply(tuple, axis=1))
                dup_mask = df_old[keys].apply(tuple, axis=1).isin(new_tuples)
            overwritten = dup_mask.sum()
            df_old = df_old[~dup_mask]
            df_merged = pd.concat([df_old, df_new], ignore_index=True)
        else:
            missing = [k for k in keys if k not in df_old.columns]
            print(f"  ⚠️ {label}: 기존 CSV에 {missing} 없음 — append")
            df_merged = pd.concat([df_old, df_new], ignore_index=True)
    else:
        df_merged = df_new
    df_merged.to_csv(filepath, index=False, encoding="utf-8-sig")
    print(f"  ✅ {label}: +{len(df_new)}건 → 총 {len(df_merged)}건")

def historical_save_boundary(DATA_PATH, OL_PATH, SEASON, all_results, all_pitchers, all_umpires, all_starters, all_market_price, all_lineup):
    append_csv(f"{DATA_PATH}/mlb_{SEASON}_정규시즌_경기결과.csv", all_results, "경기결과")
    append_csv(f"{DATA_PATH}/mlb_{SEASON}_정규시즌_투수개인기록.csv", all_pitchers, "투수기록")
    append_csv(f"{DATA_PATH}/mlb_{SEASON}_심판.csv", all_umpires, "심판")
    append_csv(f"{OL_PATH}/mlb_starters_{SEASON}.csv", all_starters, "starters")
    append_csv(f"{OL_PATH}/mlb_market_price_{SEASON}.csv", all_market_price, "market_price", key=["date","home_team"])
    append_csv(f"{OL_PATH}/mlb_lineup_{SEASON}.csv", all_lineup, "lineup")
