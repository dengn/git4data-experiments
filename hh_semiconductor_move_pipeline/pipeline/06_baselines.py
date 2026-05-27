"""[06] Baseline 评估 —— lag / rolling 特征直接当预测值的 MAPE

不训练任何模型，纯算"如果你用 lag_1 当今天的预测，MAPE 是多少"。

为什么单独跑这一段：
  1. 给 LightGBM 一个明确对比靶子 —— 不显著胜过这些 baseline 就别上 ML
  2. 看哪个简单 baseline 已经够用 —— 业务要求宽松时不必上模型
  3. 各 FAB / 各 loop 拆开看 —— 哪类样本基线已经很好（模型增益空间小）

4 种 baseline（来自 05_features.py 的特征列）：
  - lag_1         昨天的 y  当今天的预测（最朴素，惯性假设）
  - lag_7         上周同期 y  当今天的预测（周期假设）
  - roll_7_mean   最近 7 天的均值（短期趋势）
  - roll_28_mean  最近 28 天的均值（中期趋势）

3 种评估视角：
  - Overall：全量数据（剔除 NaN 暖启动期）
  - Last 30 days：最后 30 个工厂日（最接近部署期表现）
  - 5-fold TSCV：按完整工厂日切分，与 07_train 同口径，可直接比较

输出：
  artifacts/models/baselines_overall.csv
  artifacts/models/baselines_cv.csv
  artifacts/models/baselines_by_group.csv
"""
from __future__ import annotations

import sys
import pathlib

import numpy as np
import pandas as pd
from sklearn.metrics import mean_absolute_percentage_error

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _config import (  # noqa: E402
    FEATURES_DIR, MODELS_DIR,
    LAG_FEATURES, CV_FOLDS, CV_TEST_DAYS, FINAL_HOLDOUT_DAYS,
)


# 只对历史 y 派生出的特征做评估
# （wip_total_lag_1 / down_hours_lag_1 这类外生 lag 跟 y 量级不同，不能直接当预测）
BASELINE_COLS = LAG_FEATURES  # 默认 = ["lag_1", "lag_7", "roll_7_mean", "roll_28_mean"]


def mape(y: pd.Series, yhat: pd.Series) -> float:
    """MAPE %，自动剔除 y=0 的行避免 inf"""
    mask = y > 0
    if mask.sum() == 0:
        return float("nan")
    return float(mean_absolute_percentage_error(y[mask], yhat[mask]) * 100)


def daily_mape(df: pd.DataFrame, pred_col: str) -> float:
    """对客口径：先按 op_day 汇总全厂 daily MOVE，再算 MAPE。"""
    agg = (
        df.groupby("op_day", as_index=False)[["y", pred_col]]
          .sum()
    )
    return mape(agg["y"], agg[pred_col])


def date_splits(df: pd.DataFrame, n_splits: int, test_days: int) -> list[np.ndarray]:
    """按完整 op_day 切 valid，避免同一天被行级切分拆进 train/valid。"""
    dates = pd.Index(pd.to_datetime(df["op_day"].dropna().unique())).sort_values()
    required_days = n_splits * test_days
    if len(dates) <= required_days:
        raise RuntimeError(
            f"[06] 数据天数不足: {len(dates)} 天，无法做 "
            f"{n_splits} 折 × {test_days} 天 CV"
        )

    splits = []
    for i in range(n_splits):
        start = len(dates) - test_days * (n_splits - i)
        end = start + test_days
        valid_dates = set(dates[start:end])
        va_idx = df.index[df["op_day"].isin(valid_dates)].to_numpy()
        splits.append(va_idx)
    return splits


def drop_final_holdout(df: pd.DataFrame, holdout_days: int) -> pd.DataFrame:
    """与 07_train 对齐：CV 不看最后 holdout_days 个完整工厂日。"""
    dates = pd.Index(pd.to_datetime(df["op_day"].dropna().unique())).sort_values()
    if len(dates) <= holdout_days:
        raise RuntimeError(f"[06] 数据天数不足，无法预留 {holdout_days} 天 final holdout")
    holdout_dates = set(dates[-holdout_days:])
    return df.loc[~df["op_day"].isin(holdout_dates)].reset_index(drop=True)


def overall_table(feat: pd.DataFrame) -> pd.DataFrame:
    """每个 baseline 的全量 daily-total MAPE"""
    rows = []
    for b in BASELINE_COLS:
        df = feat.dropna(subset=[b, "y"])
        rows.append({
            "baseline": b,
            "n_rows":   len(df),
            "MAPE":     round(daily_mape(df, b), 2),
        })
    return pd.DataFrame(rows)


def recent_window_table(feat: pd.DataFrame, days: int) -> pd.DataFrame:
    """最后 N 天 MAPE"""
    cutoff = feat["op_day"].max() - pd.Timedelta(days=days)
    win = feat[feat["op_day"] >= cutoff]
    rows = []
    for b in BASELINE_COLS:
        df = win.dropna(subset=[b, "y"])
        rows.append({
            "baseline": b,
            "n_rows":   len(df),
            "MAPE":     round(daily_mape(df, b), 2),
        })
    return pd.DataFrame(rows)


def cv_table(feat: pd.DataFrame, n_splits: int, test_days: int) -> pd.DataFrame:
    """5 折时间序列 CV，与 07_train.py 同口径"""
    df = feat.dropna(subset=BASELINE_COLS + ["y"]).sort_values("op_day").reset_index(drop=True)
    fold_splits = date_splits(df, n_splits, test_days)

    rows = []
    for b in BASELINE_COLS:
        fold_mapes = []
        for va in fold_splits:
            valid = df.iloc[va]
            fold_mapes.append(daily_mape(valid, b))
        row = {"baseline": b}
        for i, m in enumerate(fold_mapes):
            row[f"fold_{i}"] = round(m, 2)
        row["mean"] = round(float(np.mean(fold_mapes)), 2)
        row["std"]  = round(float(np.std(fold_mapes)), 2)
        rows.append(row)
    return pd.DataFrame(rows)


def per_group_table(feat: pd.DataFrame, group_col: str) -> pd.DataFrame:
    """按 fab_id 或 loop 拆分各 baseline 的 MAPE"""
    rows = []
    for g, df in feat.dropna(subset=BASELINE_COLS + ["y"]).groupby(group_col, observed=True):
        row = {group_col: str(g), "n_rows": len(df)}
        for b in BASELINE_COLS:
            row[b] = round(mape(df["y"], df[b]), 2)
        rows.append(row)
    return pd.DataFrame(rows)


def _print(title: str, df: pd.DataFrame) -> None:
    print(f"\n     【{title}】")
    print(df.to_string(index=False).replace("\n", "\n     "))


def main() -> None:
    print("[06] Baseline 评估 (lag/rolling 当预测值)")
    feat = pd.read_parquet(FEATURES_DIR / "features.parquet")
    feat = feat.dropna(subset=["y"]).reset_index(drop=True)
    print(f"     特征表: {len(feat):,} 行 | 时间范围 {feat['op_day'].min().date()} ~ {feat['op_day'].max().date()}")
    print(f"     评估 baseline: {BASELINE_COLS}")

    # 1. 全量
    overall = overall_table(feat)
    _print("全量 MAPE（剔除 NaN 暖启动期）", overall)

    # 2. 最后 30 天
    recent = recent_window_table(feat, 30)
    _print(f"最后 30 天 MAPE", recent)

    # 3. 5 折 TSCV
    cv_feat = drop_final_holdout(feat, FINAL_HOLDOUT_DAYS)
    cv = cv_table(cv_feat, CV_FOLDS, CV_TEST_DAYS)
    _print(f"{CV_FOLDS} 折时间序列 CV（排除最后 {FINAL_HOLDOUT_DAYS} 天，与 07_train 同口径）", cv)

    # 4. 按 loop 拆分（去 fab 后不再有 FAB 维度，只看 loop）
    per_loop = per_group_table(feat, "loop")
    _print("按 loop 拆分 MAPE（全量）", per_loop)

    # 持久化
    overall.to_csv(MODELS_DIR / "baselines_overall.csv", index=False)
    cv.to_csv(MODELS_DIR / "baselines_cv.csv", index=False)
    by_group = (per_loop.rename(columns={"loop": "group"})
                        .assign(group_type="loop"))
    by_group.to_csv(MODELS_DIR / "baselines_by_group.csv", index=False)

    print()
    print(f"     → {MODELS_DIR}/baselines_overall.csv")
    print(f"     → {MODELS_DIR}/baselines_cv.csv")
    print(f"     → {MODELS_DIR}/baselines_by_group.csv")

    # 最佳 baseline 推荐
    best = cv.sort_values("mean").iloc[0]
    print()
    print(f"     >>> 最强 baseline (按 5 折 CV 均值): {best['baseline']}  "
          f"MAPE = {best['mean']:.2f}% ± {best['std']:.2f}%")
    print(f"     >>> 训练模型必须显著低于这个数字才证明 ML 有价值")


if __name__ == "__main__":
    main()
