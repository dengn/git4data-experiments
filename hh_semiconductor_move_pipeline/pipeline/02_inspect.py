"""[02] 数据体检 —— 行数 / 字段 / dtype / 空值率 / 唯一值数 / 时间范围

只读，不写文件。客户每次拿到新数据先跑这个看一眼健康度。

判定逻辑：
  - 空值率 > 80% → 高空值⚠️（03_clean 会删）
  - 唯一值 = 1 → 常量列⚠️（无信息量）
  - 唯一值 = 行数 → 疑似 PK
"""
from __future__ import annotations

import sys
import pathlib

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _config import RAW_DIR, NULL_DROP_THRESHOLD  # noqa: E402


TIME_COL_HINTS = ("time", "_at", "date", "timestamp")


def _is_time_col(col: str) -> bool:
    c = col.lower()
    return any(h in c for h in TIME_COL_HINTS)


def inspect_one(path: pathlib.Path) -> None:
    df = pd.read_parquet(path)
    print(f"\n{'='*70}")
    print(f"## {path.stem}")
    print(f"   {len(df):,} 行 × {len(df.columns)} 列  ({path.stat().st_size/1024:.1f} KB)")
    print(f"{'-'*70}")

    # 字段级体检
    print(f"   {'字段':<25} {'dtype':<18} {'空值率':>7} {'唯一值':>10}  备注")
    for col in df.columns:
        nrate = df[col].isna().mean() * 100
        try:
            nuniq = df[col].nunique(dropna=True)
        except TypeError:
            nuniq = -1

        flags = []
        if nrate > NULL_DROP_THRESHOLD * 100:
            flags.append("⚠️ 高空值")
        if nuniq == 1:
            flags.append("⚠️ 常量")
        if 0 < nuniq == len(df):
            flags.append("候选 PK")

        nuniq_str = f"{nuniq:,}" if nuniq >= 0 else "n/a"
        print(f"   {col:<25} {str(df[col].dtype):<18} {nrate:>6.1f}% {nuniq_str:>10}  "
              f"{', '.join(flags)}")

    # 时间范围（只对 datetime dtype 或字符串可解析为时间的列做）
    for col in df.columns:
        if not _is_time_col(col):
            continue
        s = df[col]
        if pd.api.types.is_datetime64_any_dtype(s):
            t = s
        elif s.dtype == object or pd.api.types.is_string_dtype(s):
            t = pd.to_datetime(s, errors="coerce")
            if t.isna().all():
                continue
        else:
            continue  # 数值列名字里有 "time" 是巧合（如 uptime），跳过
        tmin, tmax = t.min(), t.max()
        if pd.notna(tmin):
            print(f"   时间范围({col}): {tmin}  ~  {tmax}  跨度={tmax-tmin}")


def main() -> None:
    paths = sorted(RAW_DIR.glob("*.parquet"))
    if not paths:
        print(f"[02] ❌ {RAW_DIR} 为空，先跑 01_load.py")
        sys.exit(1)
    print(f"[02] 数据体检：扫描 {len(paths)} 个 parquet 文件")
    for p in paths:
        inspect_one(p)


if __name__ == "__main__":
    main()
