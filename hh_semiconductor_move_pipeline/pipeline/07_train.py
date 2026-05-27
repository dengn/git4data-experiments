"""[07] 训练 LightGBM + 5 折时间序列 CV → artifacts/models/

策略：
  - 按完整工厂日做时间序列 CV（避免同一天被切到 train/valid 两边）
  - 最后 FINAL_HOLDOUT_DAYS 天只做最终评估，不参与 Optuna / CV
  - 最后再用全量数据训"最终模型"持久化

输出：
  models/model.pkl              —— 最终 LightGBM 模型 + 特征列定义
  models/metrics.json           —— CV 指标 + baseline 对比
  models/feature_importance.csv —— LightGBM gain 重要度
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
import pathlib

import numpy as np
import pandas as pd
import lightgbm as lgb
import xgboost as xgb
import optuna
from sklearn.metrics import mean_absolute_percentage_error

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _config import (  # noqa: E402
    FEATURES_DIR, MODELS_DIR,
    FEATURE_COLS, CAT_FEATURES,
    MODEL_TYPE, LGB_PARAMS, XGB_PARAMS,
    CV_FOLDS, CV_TEST_DAYS, FINAL_HOLDOUT_DAYS, OPTUNA_N_TRIALS,
)

# 主参数 / 模型类按 MODEL_TYPE 选定，下游一致用 BASE_PARAMS
if MODEL_TYPE == "lightgbm":
    BASE_PARAMS = LGB_PARAMS
elif MODEL_TYPE == "xgboost":
    BASE_PARAMS = XGB_PARAMS
else:
    raise ValueError(f"不支持的 MODEL_TYPE: {MODEL_TYPE!r}, 应为 'lightgbm' / 'xgboost'")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--final-train-end-date",
        help="最终保存 model.pkl 时，只使用 op_day <= 该日期的数据训练；"
             "例如 2026-03-31。默认使用全量 features.parquet。",
    )
    return parser.parse_args()


def _make_model(params: dict):
    """按 MODEL_TYPE 返回 sklearn 接口的回归器"""
    if MODEL_TYPE == "lightgbm":
        return lgb.LGBMRegressor(**params)
    else:
        return xgb.XGBRegressor(**params)


def _fit_one(model, train, valid, feature_cols, use_early_stopping: bool):
    """统一两个模型的 fit 接口差异：
    - LGB：categorical_feature= + callbacks=early_stopping(50)
    - XGB：enable_categorical 已在构造时传，early_stopping_rounds 也在构造时传
    """
    if MODEL_TYPE == "lightgbm":
        kwargs = {"categorical_feature": CAT_FEATURES}
        if use_early_stopping and valid is not None:
            kwargs["eval_set"] = [(valid[feature_cols], valid["y"])]
            kwargs["callbacks"] = [lgb.early_stopping(50, verbose=False)]
    else:
        kwargs = {"verbose": False}
        if use_early_stopping and valid is not None:
            kwargs["eval_set"] = [(valid[feature_cols], valid["y"])]
    model.fit(train[feature_cols], train["y"], **kwargs)
    return model


def _best_iter(model) -> int:
    """LGB: best_iteration_  /  XGB: best_iteration"""
    if MODEL_TYPE == "lightgbm":
        return model.best_iteration_ or BASE_PARAMS["n_estimators"]
    return getattr(model, "best_iteration", None) or BASE_PARAMS["n_estimators"]


def _feature_importance(model, feature_cols) -> pd.DataFrame:
    """LGB 给 gain+split 两列；XGB 只取 gain（split 用 0 占位保持 schema 一致）"""
    if MODEL_TYPE == "lightgbm":
        return pd.DataFrame({
            "feature": feature_cols,
            "gain": model.booster_.feature_importance(importance_type="gain"),
            "split": model.booster_.feature_importance(importance_type="split"),
        })
    booster = model.get_booster()
    gain_dict = booster.get_score(importance_type="gain")
    return pd.DataFrame({
        "feature": feature_cols,
        "gain":    [gain_dict.get(c, 0.0) for c in feature_cols],
        "split":   [0] * len(feature_cols),
    })


def mape(y_true, y_pred) -> float:
    return float(mean_absolute_percentage_error(y_true, y_pred) * 100)


def fab_day_mape(valid_df: pd.DataFrame, pred: np.ndarray, y_col: str = "y") -> float:
    """对客口径 MAPE：先按 op_day 跨 loop 求和聚成 FAB-day 总 Move，再算 MAPE。

    与 08_predict.py 输出粒度对齐。loop 间误差互相抵消，FAB-day MAPE 通常
    显著低于 loop-day MAPE。
    """
    agg = (
        pd.DataFrame({
            "op_day": valid_df["op_day"].values,
            "y_true": valid_df[y_col].values,
            "y_pred": pred,
        })
        .groupby("op_day", as_index=False)[["y_true", "y_pred"]].sum()
    )
    return mape(agg["y_true"], agg["y_pred"])


def daily_total_frame(valid_df: pd.DataFrame, pred: np.ndarray, y_col: str = "y") -> pd.DataFrame:
    """按 op_day 聚合后的真实/预测/绝对误差明细。"""
    agg = (
        pd.DataFrame({
            "op_day": valid_df["op_day"].values,
            "y_true": valid_df[y_col].values,
            "y_pred": pred,
        })
        .groupby("op_day", as_index=False)[["y_true", "y_pred"]].sum()
    )
    agg["abs_error"] = (agg["y_true"] - agg["y_pred"]).abs()
    agg["ape"] = np.where(agg["y_true"] > 0, agg["abs_error"] / agg["y_true"] * 100, np.nan)
    return agg


def daily_total_metrics(valid_df: pd.DataFrame, pred: np.ndarray, y_col: str = "y") -> dict:
    """对客 daily-total 口径，同时给 MAPE 和绝对误差分布。"""
    agg = daily_total_frame(valid_df, pred, y_col)
    return {
        "mape": float(agg["ape"].mean()),
        "mae": float(agg["abs_error"].mean()),
        "median_abs_error": float(agg["abs_error"].median()),
        "p90_abs_error": float(agg["abs_error"].quantile(0.90)),
        "max_abs_error": float(agg["abs_error"].max()),
    }


def date_splits(df: pd.DataFrame, n_splits: int, test_days: int) -> list[tuple[np.ndarray, np.ndarray]]:
    """按完整 op_day 切 CV fold，valid 每折固定 test_days 天。"""
    dates = pd.Index(pd.to_datetime(df["op_day"].dropna().unique())).sort_values()
    required_days = n_splits * test_days
    if len(dates) <= required_days:
        raise RuntimeError(
            f"[07] 数据天数不足: {len(dates)} 天，无法做 "
            f"{n_splits} 折 × {test_days} 天 CV"
        )

    splits = []
    for i in range(n_splits):
        start = len(dates) - test_days * (n_splits - i)
        end = start + test_days
        train_dates = set(dates[:start])
        valid_dates = set(dates[start:end])
        tr_idx = df.index[df["op_day"].isin(train_dates)].to_numpy()
        va_idx = df.index[df["op_day"].isin(valid_dates)].to_numpy()
        splits.append((tr_idx, va_idx))
    return splits


def split_final_holdout(df: pd.DataFrame, holdout_days: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """最后 holdout_days 个完整工厂日留作最终评估。"""
    dates = pd.Index(pd.to_datetime(df["op_day"].dropna().unique())).sort_values()
    min_days = holdout_days + CV_FOLDS * CV_TEST_DAYS + 1
    if len(dates) < min_days:
        raise RuntimeError(
            f"[07] 数据天数不足: {len(dates)} 天；至少需要 "
            f"{holdout_days} 天 final holdout + {CV_FOLDS}×{CV_TEST_DAYS} 天 CV + 1 天训练"
        )

    holdout_dates = set(dates[-holdout_days:])
    dev = df.loc[~df["op_day"].isin(holdout_dates)].reset_index(drop=True)
    holdout = df.loc[df["op_day"].isin(holdout_dates)].reset_index(drop=True)
    return dev, holdout


def _print_last_day_predictions(valid: pd.DataFrame, pred: np.ndarray, indent: str = "             ") -> None:
    """打印 valid 集最后一个工厂日的真实 vs 预测 Move 数值。
    FAB-day（跨 loop 求和）+ loop-day（按 loop 明细）双口径都列。
    便于人眼对账"是不是某些 loop 的预测错得离谱"。
    """
    last_day = valid["op_day"].max()
    mask = (valid["op_day"] == last_day).values

    loops_last = np.asarray(valid["loop"].values)[mask]
    y_last     = np.asarray(valid["y"].values)[mask].astype(float)
    model_last = np.asarray(pred)[mask].astype(float)
    lag1_last  = np.asarray(valid["lag_1"].values)[mask].astype(float)
    r7_last    = np.asarray(valid["roll_7_mean"].values)[mask].astype(float)

    print(f"{indent}── valid 末日 {last_day.date()} Move 实测 vs 预测 ──")

    # FAB-day：跨 loop 求和
    y_fab    = int(round(y_last.sum()))
    model_fab = int(round(model_last.sum()))
    lag1_fab = int(round(lag1_last.sum()))
    r7_fab   = int(round(r7_last.sum()))
    print(f"{indent}[FAB-day]   y_true={y_fab:>9,}  "
          f"{MODEL_TYPE}={model_fab:>9,}  lag-1={lag1_fab:>9,}  roll-7={r7_fab:>9,}")

    # loop-day：按 loop 排序明细
    print(f"{indent}[loop-day]")
    print(f"{indent}  {'loop':<14}{'y_true':>10}{MODEL_TYPE:>10}{'lag-1':>10}{'roll-7':>10}")
    order = np.argsort(loops_last.astype(str))
    for i in order:
        print(f"{indent}  {str(loops_last[i]):<14}"
              f"{int(round(y_last[i])):>10,}"
              f"{int(round(model_last[i])):>10,}"
              f"{int(round(lag1_last[i])):>10,}"
              f"{int(round(r7_last[i])):>10,}")


def _suggest_params(trial: optuna.Trial) -> dict:
    """按 MODEL_TYPE 返回该 trial 的搜索空间结果"""
    if MODEL_TYPE == "lightgbm":
        return {
            "learning_rate":     trial.suggest_float("learning_rate", 0.01, 0.1, log=True),
            "num_leaves":        trial.suggest_int("num_leaves", 7, 63),
            "max_depth":         trial.suggest_int("max_depth", 3, 8),
            "min_child_samples": trial.suggest_int("min_child_samples", 10, 100),
            "reg_alpha":         trial.suggest_float("reg_alpha", 0.01, 5.0, log=True),
            "reg_lambda":        trial.suggest_float("reg_lambda", 0.01, 5.0, log=True),
            "feature_fraction":  trial.suggest_float("feature_fraction", 0.5, 1.0),
        }
    # xgboost
    return {
        "learning_rate":    trial.suggest_float("learning_rate", 0.01, 0.1, log=True),
        "max_depth":        trial.suggest_int("max_depth", 3, 8),
        "min_child_weight": trial.suggest_int("min_child_weight", 1, 20),
        "subsample":        trial.suggest_float("subsample", 0.5, 1.0),
        "colsample_bytree": trial.suggest_float("colsample_bytree", 0.5, 1.0),
        "reg_alpha":        trial.suggest_float("reg_alpha", 0.01, 5.0, log=True),
        "reg_lambda":       trial.suggest_float("reg_lambda", 0.01, 5.0, log=True),
        "gamma":            trial.suggest_float("gamma", 1e-3, 1.0, log=True),
    }


def tune_model_params(
    feat: pd.DataFrame, feature_cols: list[str], fold_splits: list[tuple[np.ndarray, np.ndarray]]
) -> dict:
    """Optuna 跑 N 次 trial，每次评 5 折 CV mean FAB-day MAPE，返回最佳超参。

    搜索空间和模型类按 MODEL_TYPE 自动切换；n_estimators 不搜（early_stopping 决定）。
    目标函数与主指标对齐（FAB-day MAPE 最小化）。
    """
    optuna.logging.set_verbosity(optuna.logging.WARNING)

    def objective(trial: optuna.Trial) -> float:
        params = {**BASE_PARAMS, **_suggest_params(trial)}
        fold_mapes = []
        for tr, va in fold_splits:
            train, valid = feat.iloc[tr], feat.iloc[va]
            model = _fit_one(_make_model(params), train, valid, feature_cols, True)
            fold_mapes.append(fab_day_mape(valid, model.predict(valid[feature_cols])))
        return float(np.mean(fold_mapes))

    print(f"     Optuna 搜索 {OPTUNA_N_TRIALS} trials ({MODEL_TYPE}, 目标=FAB-day MAPE 最小化)...")
    study = optuna.create_study(direction="minimize", sampler=optuna.samplers.TPESampler(seed=42))
    study.optimize(objective, n_trials=OPTUNA_N_TRIALS, show_progress_bar=False)
    print(f"     ✓ 最优 FAB-day MAPE = {study.best_value:.3f}% (trial #{study.best_trial.number})")
    print(f"     ✓ 最优参数: {study.best_params}")
    return study.best_params


def main() -> None:
    args = parse_args()
    print(f"[07] 训练 {MODEL_TYPE} (5 折时间序列 CV)")
    feat = pd.read_parquet(FEATURES_DIR / "features.parquet")

    # FEATURE_COLS 在 _config 是写死的"应有全集"，客户库缺字段时 base / features
    # 会少几列（被 03_clean 删 / step3 跳过）。这里过滤成"实际可用"的列，下游
    # bundle 和 08/09 读 bundle["feature_cols"] 时自动跟着对齐。
    feature_cols = [c for c in FEATURE_COLS if c in feat.columns]
    missing = [c for c in FEATURE_COLS if c not in feat.columns]
    if missing:
        print(f"     ⚠️ FEATURE_COLS 里 {len(missing)} 个特征列不在 features.parquet"
              f"（多半是被 03_clean 删 / step3 跳过的源字段），已剔除: {missing}")

    feat = feat.dropna(subset=feature_cols + ["y"]).reset_index(drop=True)
    feat = feat.sort_values("op_day").reset_index(drop=True)
    print(f"     特征表: {len(feat):,} 行 × {len(feature_cols)} 个特征")

    dev_feat, holdout = split_final_holdout(feat, FINAL_HOLDOUT_DAYS)
    fold_splits = date_splits(dev_feat, CV_FOLDS, CV_TEST_DAYS)
    print(f"     Dev/Optuna/CV: {len(dev_feat):,} 行 | "
          f"{dev_feat['op_day'].min().date()} ~ {dev_feat['op_day'].max().date()}")
    print(f"     Final holdout: {len(holdout):,} 行 | "
          f"{holdout['op_day'].min().date()} ~ {holdout['op_day'].max().date()} "
          f"({FINAL_HOLDOUT_DAYS} 天，不参与调参/CV)")
    print(f"     每折 valid = {CV_TEST_DAYS} 个完整工厂日")
    print()

    # Optuna 搜超参（OPTUNA_N_TRIALS=0 时跳过，直接用 BASE_PARAMS 默认值）
    if OPTUNA_N_TRIALS > 0:
        best_params = tune_model_params(dev_feat, feature_cols, fold_splits)
        train_params = {**BASE_PARAMS, **best_params}
        print()
    else:
        print(f"     Optuna 已跳过 (OPTUNA_N_TRIALS=0)，使用 {MODEL_TYPE} 默认参数")
        train_params = BASE_PARAMS

    # 主指标：FAB-day（对客口径）；保留 loop-day 作为保守口径参考
    fd_lgb, fd_lag1, fd_r7 = [], [], []
    ld_lgb, ld_lag1, ld_r7 = [], [], []
    best_rounds = []   # 每折 early_stopping 命中的最佳 round 数

    for fold, (tr, va) in enumerate(fold_splits):
        train, valid = dev_feat.iloc[tr], dev_feat.iloc[va]
        # early_stopping: 50 round 无提升就停（lgb 走 callbacks，xgb 走构造参数）
        model = _fit_one(_make_model(train_params), train, valid, feature_cols, True)
        best_rounds.append(_best_iter(model))
        pred = model.predict(valid[feature_cols])

        # FAB-day 口径（对客验收口径，与 08_predict 对齐）
        m_lgb_fd = fab_day_mape(valid, pred)
        m_lag1_fd = fab_day_mape(valid, valid["lag_1"].values)
        m_r7_fd = fab_day_mape(valid, valid["roll_7_mean"].values)
        fd_lgb.append(m_lgb_fd)
        fd_lag1.append(m_lag1_fd)
        fd_r7.append(m_r7_fd)

        # loop-day 口径（保守，仅作参考）
        m_lgb_ld = mape(valid["y"], pred)
        m_lag1_ld = mape(valid["y"], valid["lag_1"])
        m_r7_ld = mape(valid["y"], valid["roll_7_mean"])
        ld_lgb.append(m_lgb_ld)
        ld_lag1.append(m_lag1_ld)
        ld_r7.append(m_r7_ld)

        tr_range = f"{train['op_day'].min().date()}~{train['op_day'].max().date()}"
        va_range = f"{valid['op_day'].min().date()}~{valid['op_day'].max().date()}"
        print(f"     Fold {fold}:  train [{tr_range}]  valid [{va_range}]"
              f"  best_iter={best_rounds[-1]}")
        print(f"             [FAB-day] {MODEL_TYPE}={m_lgb_fd:5.2f}%   "
              f"lag-1={m_lag1_fd:5.2f}%   roll-7={m_r7_fd:5.2f}%")
        print(f"             [loop-day参考] {MODEL_TYPE}={m_lgb_ld:5.2f}%   "
              f"lag-1={m_lag1_ld:5.2f}%   roll-7={m_r7_ld:5.2f}%")
        _print_last_day_predictions(valid, pred)

    print()
    print(f"     {'='*60}")
    print(f"     CV {CV_FOLDS} 折平均 MAPE —— FAB-day 口径（对客验收口径）")
    print(f"     {'='*60}")
    print(f"     {MODEL_TYPE:<14}: {np.mean(fd_lgb):>5.2f}% ± {np.std(fd_lgb):.2f}%")
    print(f"     Baseline lag-1: {np.mean(fd_lag1):>5.2f}% ± {np.std(fd_lag1):.2f}%")
    print(f"     Baseline r7   : {np.mean(fd_r7):>5.2f}% ± {np.std(fd_r7):.2f}%")
    gain = (np.mean(fd_lag1) - np.mean(fd_lgb)) / np.mean(fd_lag1) * 100
    print(f"     {MODEL_TYPE} 改善 : {gain:+.1f}% (vs lag-1, FAB-day)")
    print()
    print(f"     {'-'*60}")
    print(f"     [参考] loop-day 口径（保守口径，loop 间误差不抵消）")
    print(f"     {'-'*60}")
    print(f"     {MODEL_TYPE:<14}: {np.mean(ld_lgb):>5.2f}% ± {np.std(ld_lgb):.2f}%")
    print(f"     Baseline lag-1: {np.mean(ld_lag1):>5.2f}% ± {np.std(ld_lag1):.2f}%")
    print(f"     Baseline r7   : {np.mean(ld_r7):>5.2f}% ± {np.std(ld_r7):.2f}%")

    # n_estimators 取 CV 折最佳 round 均值 × 1.1，避免最终模型盲跑 500 棵树过拟合。
    final_n_est = max(int(np.mean(best_rounds) * 1.1), 50)

    # ---- 最终 holdout 评估：不参与 Optuna / CV 的最后 N 天 ----
    eval_params = {**train_params, "n_estimators": final_n_est}
    eval_params.pop("early_stopping_rounds", None)
    holdout_model = _fit_one(_make_model(eval_params), dev_feat, None, feature_cols, False)
    holdout_pred = holdout_model.predict(holdout[feature_cols])
    holdout_lag1 = holdout["lag_1"].values
    holdout_r7 = holdout["roll_7_mean"].values

    holdout_model_metrics = daily_total_metrics(holdout, holdout_pred)
    holdout_lag1_metrics = daily_total_metrics(holdout, holdout_lag1)
    holdout_r7_metrics = daily_total_metrics(holdout, holdout_r7)
    holdout_gain = (
        (holdout_lag1_metrics["mape"] - holdout_model_metrics["mape"])
        / holdout_lag1_metrics["mape"] * 100
    )

    print()
    print(f"     {'='*60}")
    print(f"     FINAL HOLDOUT（最后 {FINAL_HOLDOUT_DAYS} 天，未参与 Optuna/CV）")
    print(f"     {'='*60}")
    print(f"     {MODEL_TYPE:<14}: MAPE={holdout_model_metrics['mape']:>5.2f}%  "
          f"MAE={holdout_model_metrics['mae']:,.0f}  "
          f"P90AE={holdout_model_metrics['p90_abs_error']:,.0f}")
    print(f"     Baseline lag-1: MAPE={holdout_lag1_metrics['mape']:>5.2f}%  "
          f"MAE={holdout_lag1_metrics['mae']:,.0f}  "
          f"P90AE={holdout_lag1_metrics['p90_abs_error']:,.0f}")
    print(f"     Baseline r7   : MAPE={holdout_r7_metrics['mape']:>5.2f}%  "
          f"MAE={holdout_r7_metrics['mae']:,.0f}  "
          f"P90AE={holdout_r7_metrics['p90_abs_error']:,.0f}")

    # ---- 训练最终持久化模型 ----
    final_train = feat
    if args.final_train_end_date:
        final_end = pd.to_datetime(args.final_train_end_date)
        final_train = feat.loc[pd.to_datetime(feat["op_day"]) <= final_end].copy()
        if final_train.empty:
            raise RuntimeError(f"--final-train-end-date={args.final_train_end_date} 后无训练数据")
        print(f"\n     训练最终模型（截止 {final_end.date()}，{len(final_train):,} 行）...")
    else:
        print(f"\n     训练最终模型（全量 {len(final_train):,} 行）...")
    print(f"     CV 平均 best_iter = {np.mean(best_rounds):.0f} → 最终 n_estimators = {final_n_est}")
    # 全量训练时不需要 early_stopping（没有 valid 集），但 XGB 构造参里的
    # early_stopping_rounds 会要求 eval_set —— 删掉这个参数防 XGB 报错
    final_params = {**train_params, "n_estimators": final_n_est}
    final_params.pop("early_stopping_rounds", None)
    final_model = _fit_one(_make_model(final_params), final_train, None, feature_cols, False)

    # 保存模型 + 元信息
    bundle = {
        "model": final_model,
        "model_type": MODEL_TYPE,
        "feature_cols": feature_cols,
        "cat_features": CAT_FEATURES,
        "trained_on_rows": len(final_train),
        "trained_on_range": (str(final_train["op_day"].min().date()),
                             str(final_train["op_day"].max().date())),
        "final_train_end_date": args.final_train_end_date,
    }
    with open(MODELS_DIR / "model.pkl", "wb") as f:
        pickle.dump(bundle, f)

    metrics = {
        "model_type": MODEL_TYPE,
        "cv_folds": CV_FOLDS,
        "cv_test_days": CV_TEST_DAYS,
        "cv_scope": "dev_only_used_for_model_selection",
        "final_holdout_days": FINAL_HOLDOUT_DAYS,
        "primary_metric_scope": "final_holdout_fab_day",
        "final_n_estimators": final_n_est,
        "final_train_end_date": args.final_train_end_date,
        "final_train_range": (str(final_train["op_day"].min().date()),
                              str(final_train["op_day"].max().date())),
        "final_train_rows": int(len(final_train)),
        "dev_range": (str(dev_feat["op_day"].min().date()),
                      str(dev_feat["op_day"].max().date())),
        "holdout_range": (str(holdout["op_day"].min().date()),
                          str(holdout["op_day"].max().date())),
        # —— FINAL HOLDOUT：未参与 Optuna / CV，才作为最终泛化估计 ——
        "holdout_fab_day_mape": holdout_model_metrics["mape"],
        "holdout_fab_day_mae": holdout_model_metrics["mae"],
        "holdout_fab_day_median_abs_error": holdout_model_metrics["median_abs_error"],
        "holdout_fab_day_p90_abs_error": holdout_model_metrics["p90_abs_error"],
        "holdout_fab_day_max_abs_error": holdout_model_metrics["max_abs_error"],
        "holdout_lag1_fab_day_mape": holdout_lag1_metrics["mape"],
        "holdout_lag1_fab_day_mae": holdout_lag1_metrics["mae"],
        "holdout_lag1_fab_day_p90_abs_error": holdout_lag1_metrics["p90_abs_error"],
        "holdout_r7_fab_day_mape": holdout_r7_metrics["mape"],
        "holdout_r7_fab_day_mae": holdout_r7_metrics["mae"],
        "holdout_r7_fab_day_p90_abs_error": holdout_r7_metrics["p90_abs_error"],
        "holdout_improvement_vs_lag1_pct": float(holdout_gain),
        # —— Dev CV：仍保留作调参/稳定性参考，不再当最终泛化误差 ——
        "dev_cv_fab_day_mape_mean": float(np.mean(fd_lgb)),
        "dev_cv_fab_day_mape_std": float(np.std(fd_lgb)),
        "dev_cv_lag1_fab_day_mape_mean": float(np.mean(fd_lag1)),
        "dev_cv_lag1_fab_day_mape_std": float(np.std(fd_lag1)),
        "dev_cv_r7_fab_day_mape_mean": float(np.mean(fd_r7)),
        "dev_cv_r7_fab_day_mape_std": float(np.std(fd_r7)),
        "dev_cv_improvement_vs_lag1_pct": float(gain),
        # —— loop-day（保守口径，仅参考）——
        "dev_cv_loop_day_mape_mean": float(np.mean(ld_lgb)),
        "dev_cv_loop_day_mape_std": float(np.std(ld_lgb)),
        "lag1_loop_day_mape_mean": float(np.mean(ld_lag1)),
        "lag1_loop_day_mape_std": float(np.std(ld_lag1)),
        "r7_loop_day_mape_mean": float(np.mean(ld_r7)),
        "r7_loop_day_mape_std": float(np.std(ld_r7)),
        # —— 旧字段（向后兼容）：保留为 Dev CV FAB-day 数值 ——
        "lgb_fab_day_mape_mean": float(np.mean(fd_lgb)),
        "lgb_fab_day_mape_std": float(np.std(fd_lgb)),
        "lag1_fab_day_mape_mean": float(np.mean(fd_lag1)),
        "lag1_fab_day_mape_std": float(np.std(fd_lag1)),
        "r7_fab_day_mape_mean": float(np.mean(fd_r7)),
        "r7_fab_day_mape_std": float(np.std(fd_r7)),
        "lgb_improvement_vs_lag1_pct": float(gain),
        "lgb_loop_day_mape_mean": float(np.mean(ld_lgb)),
        "lgb_loop_day_mape_std": float(np.std(ld_lgb)),
        "lgb_mape_mean": float(np.mean(fd_lgb)),
        "lgb_mape_std": float(np.std(fd_lgb)),
        "lag1_mape_mean": float(np.mean(fd_lag1)),
        "lag1_mape_std": float(np.std(fd_lag1)),
        "r7_mape_mean": float(np.mean(fd_r7)),
        "r7_mape_std": float(np.std(fd_r7)),
        "feature_cols": feature_cols,
    }
    with open(MODELS_DIR / "metrics.json", "w") as f:
        json.dump(metrics, f, indent=2, ensure_ascii=False)

    # 特征重要度（lgb 给 gain+split，xgb 只 gain，split 列填 0 保持 schema 一致）
    imp = (
        _feature_importance(final_model, feature_cols)
        .sort_values("gain", ascending=False)
        .reset_index(drop=True)
    )
    imp.to_csv(MODELS_DIR / "feature_importance.csv", index=False)

    print(f"\n     → {MODELS_DIR}/model.pkl")
    print(f"     → {MODELS_DIR}/metrics.json")
    print(f"     → {MODELS_DIR}/feature_importance.csv")
    print()
    print(f"     特征重要度 Top-5:")
    print(imp.head(5).to_string(index=False))


if __name__ == "__main__":
    main()
