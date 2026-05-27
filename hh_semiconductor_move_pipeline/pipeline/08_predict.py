"""[08] 当日推理 → artifacts/predictions/<op_day>_loop.csv

模拟客户现场每天早上 8:00 的预测脚本：
  1. 加载已训练模型
  2. 默认拿 features.parquet 中"最新一个工厂日"作为"今天"
     测试时也可用 --date 指定最后 FINAL_HOLDOUT_DAYS 天中的某一天
  3. 出 loop 级预测（去 fab 后这就是最终交付粒度）
  4. 保存到 predictions/<op_day>_loop.csv

去 fab 后不再产出 FAB 级聚合（客户验收口径是全厂 daily move，
单个 op_day 对应一行 sum(pred) 就够了，需要时下游脚本自取）。
"""
from __future__ import annotations

import argparse
import pickle
import sys
import pathlib

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _config import FEATURES_DIR, MODELS_DIR, PREDS_DIR, FINAL_HOLDOUT_DAYS  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--date",
        help="Factory date to predict, for example 2026-04-01. "
             "Must be within the final holdout window.",
    )
    parser.add_argument(
        "--allow-any-date",
        action="store_true",
        help="Allow dates outside the final holdout window.",
    )
    parser.add_argument("--out-dir", help="Output dir; default artifacts/predictions")
    return parser.parse_args()


def pick_predict_day(feat: pd.DataFrame, date_arg: str | None, allow_any_date: bool) -> pd.Timestamp:
    dates = pd.Index(pd.to_datetime(feat["op_day"].dropna().unique())).sort_values()
    if len(dates) == 0:
        raise RuntimeError("features.parquet has no op_day")

    holdout_dates = dates[-FINAL_HOLDOUT_DAYS:]
    holdout_start = holdout_dates.min().date()
    holdout_end = holdout_dates.max().date()

    if date_arg:
        day = pd.to_datetime(date_arg)
        if day not in set(dates):
            raise RuntimeError(f"{day.date()} not found in features.parquet")
        if not allow_any_date and day not in set(holdout_dates):
            raise RuntimeError(
                f"{day.date()} is outside final {FINAL_HOLDOUT_DAYS} holdout days "
                f"({holdout_start} ~ {holdout_end}). "
                f"Use --allow-any-date only for debugging."
            )
        return day

    return dates.max()


def main() -> None:
    args = parse_args()
    print("[08] 当日推理")

    # 1. 模型
    with open(MODELS_DIR / "model.pkl", "rb") as f:
        bundle = pickle.load(f)
    model = bundle["model"]
    FEATURE_COLS = bundle["feature_cols"]
    print(f"     模型: 训练于 {bundle['trained_on_range'][0]} ~ "
          f"{bundle['trained_on_range'][1]}, {bundle['trained_on_rows']} 行")

    # 2. 特征表
    feat = pd.read_parquet(FEATURES_DIR / "features.parquet")
    feat = feat.dropna(subset=FEATURE_COLS).reset_index(drop=True)

    # 模拟"今天"；测试时可以指定最后 FINAL_HOLDOUT_DAYS 天内的任意一天
    today = pick_predict_day(feat, args.date, args.allow_any_date)
    today_rows = feat[feat["op_day"] == today].copy()
    if today_rows.empty:
        raise RuntimeError(f"No feature rows for {today.date()}")
    print(f"     预测工厂日: {today.date()}  ({len(today_rows)} 个 loop)")

    # 3. 预测
    today_rows["pred"] = model.predict(today_rows[FEATURE_COLS])

    # 4. loop 级输出（去 fab 后这就是最终交付粒度）
    loop_df = today_rows[["loop", "y", "pred"]].copy()
    loop_df["err_pct"] = ((loop_df["pred"] - loop_df["y"]).abs()
                          / loop_df["y"] * 100).round(2)
    loop_df["pred"] = loop_df["pred"].round(0).astype(int)
    print()
    print(f"     === Loop 级预测（{today.date()}）===")
    print(loop_df.to_string(index=False))

    # 全厂大盘（loop 求和），打印用、不落盘
    total_pred = int(loop_df["pred"].sum())
    total_actual = int(loop_df["y"].sum())
    err_pct = abs(total_pred - total_actual) / total_actual * 100 if total_actual else 0
    print()
    print(f"     === 全厂大盘（Σ loop）===")
    print(f"     pred_total={total_pred}  actual_total={total_actual}  "
          f"err_pct={err_pct:.2f}%")

    # 5. 持久化
    op_day_str = today.date().isoformat()
    out_dir = pathlib.Path(args.out_dir) if args.out_dir else PREDS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    loop_out = out_dir / f"{op_day_str}_loop.csv"
    loop_df.to_csv(loop_out, index=False)
    print(f"\n     → {loop_out}")


if __name__ == "__main__":
    main()
