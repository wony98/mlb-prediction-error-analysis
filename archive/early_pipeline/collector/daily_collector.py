# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
import os
import pandas as pd

def _append_csv(filepath, new_rows, label, key="gamePk"):
    """기존 CSV에 append (중복 시 덮어쓰기)

    key: str 또는 list[str] — 중복 판단 기준 컬럼
         market_price는 ["date","home_team"], 나머지는 "gamePk"
    """
    if not new_rows:
        return
    df_new = pd.DataFrame(new_rows)
    if os.path.exists(filepath):
        df_old = pd.read_csv(filepath)
        keys = [key] if isinstance(key, str) else list(key)

        # 양쪽 모두 key 컬럼 보유 확인
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
            if overwritten > 0:
                print(f"  📁 {label}: {overwritten}건 덮어쓰기 + {len(df_new)-overwritten}건 신규 → 총 {len(df_merged)}건")
            else:
                print(f"  📁 {label}: +{len(df_new)}건 → 총 {len(df_merged)}건")
        else:
            missing = [k for k in keys if k not in df_old.columns]
            print(f"  ⚠️ {label}: 기존 CSV에 {missing} 컬럼 없음 — 그냥 append")
            df_merged = pd.concat([df_old, df_new], ignore_index=True)
    else:
        df_merged = df_new
        print(f"  📁 {label}: 새 파일 {len(df_merged)}건")
    df_merged.to_csv(filepath, index=False, encoding="utf-8-sig")
