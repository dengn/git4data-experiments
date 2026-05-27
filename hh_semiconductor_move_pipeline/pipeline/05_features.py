"""[05] 特征工程 → artifacts/features/features.parquet

基于 base.parquet 加：
  - 历史特征：lag_1 / lag_7 / roll_7_mean / roll_28_mean（按 loop 分组）
  - 外生 lag：ls_n_lots_lag_1 / down_hours_lag_1
              （明天预测用今天的 lotstep/OEE 日级信号）

⚠️ 日历特征已整体禁用（_config.CAL_FEATURES = []）—— 客户 2026-05-15 确认
    holiday 对其 Move 不显著（影响主要走 PM 链路，已被 pm_hours/down_hours
    捕捉）。若要恢复，在 _config.CAL_FEATURES 列表里填回来 + 在 main() 重新
    调用 add_calendar() 即可。`huahong_toolkit/features/calendar.py` 留有
    独立实现可直接复用。

防泄露守则：
  - 所有 rolling 都先 shift(1) 再 rolling —— 即"用昨天及之前的"算今天的窗口
  - 外生变量也走 lag(1) —— 现场早上 8 点拿到的是"昨天的"WIP/down
"""
from __future__ import annotations

import sys
import pathlib

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _config import FEATURES_DIR, GROUP_COLS, EXOG_BASE_COLS  # noqa: E402


def add_lag_features(df: pd.DataFrame) -> pd.DataFrame:
    """按 loop 分组造 lag / rolling 特征"""
    out = df.sort_values(GROUP_COLS + ["op_day"]).reset_index(drop=True)
    g = out.groupby(GROUP_COLS, observed=True)["y"]

    out["lag_1"] = g.shift(1)
    out["lag_7"] = g.shift(7)
    # rolling 之前先 shift(1)：t 时刻只能看到 t-1 及以前
    out["roll_7_mean"]  = g.transform(lambda s: s.shift(1).rolling(7).mean())
    out["roll_28_mean"] = g.transform(lambda s: s.shift(1).rolling(28).mean())
    return out


def add_exog_lag(df: pd.DataFrame) -> pd.DataFrame:
    """对 _config.EXOG_BASE_COLS 里每一列做 groupby(loop) + shift(1)，
    生成对应的 <col>_lag_1 训练特征。

    增减特征只改 _config.EXOG_BASE_COLS，不用动这里。

    缺列处理：源列不在 df 里就跳过（mock 兼容 / 字段缺失时不崩）。
    """
    out = df.copy()
    by = out.groupby(GROUP_COLS, observed=True)
    missing = []
    for col in EXOG_BASE_COLS:
        if col in out.columns:
            out[f"{col}_lag_1"] = by[col].shift(1)
        else:
            missing.append(col)
    if missing:
        print(f"     ⚠️ EXOG_BASE_COLS 里这些列在 base.parquet 找不到，已跳过:"
              f" {missing}")
    return out


def main() -> None:
    print("[05] 特征工程")
    base = pd.read_parquet(FEATURES_DIR / "base.parquet")
    print(f"     base.parquet              {len(base):>10,} 行 × {len(base.columns)} 列")

    feat = add_lag_features(base)
    print(f"     + lag/rolling 特征         (+4 列)")

    n_before = len(feat.columns)
    feat = add_exog_lag(feat)
    print(f"     + 外生 lag (lotstep/oee)   (+{len(feat.columns) - n_before} 列)")

    # 日历特征已禁用（CAL_FEATURES = []）。客户确认 holiday 不显著影响 Move。

    # 把 fab_id / loop 设为 category 让 LightGBM 直接吃
    for c in ["fab_id", "loop"]:
        if c in feat.columns and feat[c].dtype.name != "category":
            feat[c] = feat[c].astype("category")

    print()
    print(f"     features 最终              {len(feat):>10,} 行 × {len(feat.columns)} 列")
    print(f"     字段: {list(feat.columns)}")

    out = FEATURES_DIR / "features.parquet"
    feat.to_parquet(out, index=False)
    print(f"\n     → {out}")


if __name__ == "__main__":
    main()
