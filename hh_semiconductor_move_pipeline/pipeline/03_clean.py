"""[03] 数据清理 → artifacts/clean/*.parquet

只做最基础的、无业务判断的清理：

  1. 空值率 > NULL_DROP_THRESHOLD 的列直接删
  2. 名字带 time/ts/date 的列统一转 datetime64
  3. dtype 是 object 的列尝试转 category（节省内存 + LightGBM 友好）

业务级的清理（异常值剔除、单位换算）放到 04_aggregate 之后再做。
"""
from __future__ import annotations

import sys
import pathlib

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _config import RAW_DIR, CLEAN_DIR, NULL_DROP_THRESHOLD  # noqa: E402

TIME_COL_HINTS = ("time", "_at", "date", "timestamp")


def _is_time_col(col: str) -> bool:
    c = col.lower()
    return any(h in c for h in TIME_COL_HINTS)


def clean_one(path: pathlib.Path) -> dict:
    df = pd.read_parquet(path)
    n0_cols = len(df.columns)
    n_rows = len(df)

    # 1. 删高空值列
    null_rates = df.isna().mean()
    drop_cols = null_rates[null_rates > NULL_DROP_THRESHOLD].index.tolist()
    df = df.drop(columns=drop_cols)

    # 2. 时间列统一为 datetime64（保留时区如果原本带）
    #    只对 object/string 列做转换 —— 否则 pd.to_datetime 会把整数列
    #    误当成纳秒时间戳转出一堆 datetime64（典型踩坑：n_events / n_lots 等
    #    业务计数列名里含 'ts' 子串虽然已通过缩窄 HINTS 排除，但保险起见
    #    再加一道 dtype 守门）。
    for col in list(df.columns):
        if (
            _is_time_col(col)
            and not pd.api.types.is_datetime64_any_dtype(df[col])
            and (df[col].dtype == object or pd.api.types.is_string_dtype(df[col]))
        ):
            try:
                df[col] = pd.to_datetime(df[col], errors="coerce")
            except Exception:
                pass

    # 3. object/string → category（仅当唯一值 < 30% 行数时）
    cat_cols = []
    str_like = [c for c in df.columns
                if df[c].dtype == object or pd.api.types.is_string_dtype(df[c])]
    for col in str_like:
        if df[col].nunique(dropna=True) < 0.3 * len(df):
            df[col] = df[col].astype("category")
            cat_cols.append(col)

    out = CLEAN_DIR / path.name
    df.to_parquet(out, index=False)

    return dict(
        table=path.stem,
        rows=n_rows,
        cols_before=n0_cols,
        cols_after=len(df.columns),
        dropped=drop_cols,
        category=cat_cols,
    )


def main() -> None:
    paths = sorted(RAW_DIR.glob("*.parquet"))
    if not paths:
        print(f"[03] ❌ {RAW_DIR} 为空，先跑 01_load.py")
        sys.exit(1)

    print(f"[03] 清理数据：{len(paths)} 张表  (空值率阈值={NULL_DROP_THRESHOLD:.0%})")
    print(f"     输出目录: {CLEAN_DIR}")
    print()
    for p in paths:
        info = clean_one(p)
        print(f"     {info['table']:<28} "
              f"{info['rows']:>10,} 行  "
              f"{info['cols_before']:>2} → {info['cols_after']:>2} 列  "
              f"category={len(info['category'])}  "
              f"删除={info['dropped']}")


if __name__ == "__main__":
    main()
