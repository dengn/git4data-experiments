"""[09] SHAP 可解释性分析 → artifacts/explain/

为客户验收提供"为什么今天这样预测"的可解释性证据。比 LightGBM gain
重要度强在：
  - gain 只能告诉你"哪些特征重要"，shap 能告诉你"对今天这条预测，
    每个特征把它从 base value 推高 / 拉低了多少"
  - shap 满足 additivity:  pred = base_value + Σ_feature shap_value
  - shap 满足 consistency 公理（gain 不满足，强相关特征会互相挤压排名）

两类输出（去 fab 后没有 FAB 维度聚合解释）：
  1) global_shap_importance.csv          —— 历史样本上的 mean(|shap|)
  2) <op_day>_loop_shap.csv              —— 当日 loop 逐特征贡献
"""
from __future__ import annotations

import pickle
import sys
import pathlib

import numpy as np
import pandas as pd
import shap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _config import FEATURES_DIR, MODELS_DIR, EXPLAIN_DIR  # noqa: E402


GLOBAL_SHAP_SAMPLE = 5000  # 全局重要度采样上限，避免大数据时慢


def main() -> None:
    print("[09] SHAP 可解释性分析")

    # ---- 1. 模型 ----
    with open(MODELS_DIR / "model.pkl", "rb") as f:
        bundle = pickle.load(f)
    model = bundle["model"]
    FEATURE_COLS = bundle["feature_cols"]
    print(f"     模型: 训练于 {bundle['trained_on_range'][0]} ~ "
          f"{bundle['trained_on_range'][1]}, {bundle['trained_on_rows']} 行")

    # ---- 2. 特征表（同 08，dropna 后用）----
    feat = pd.read_parquet(FEATURES_DIR / "features.parquet")
    feat = feat.dropna(subset=FEATURE_COLS).reset_index(drop=True)

    explainer = shap.TreeExplainer(model)
    base_value = float(np.asarray(explainer.expected_value).ravel()[0])
    print(f"     base value E[f(X)] = {base_value:.2f}")
    print()

    # ---- 3. 全局 SHAP 重要度 ----
    if len(feat) > GLOBAL_SHAP_SAMPLE:
        sample = feat.sample(GLOBAL_SHAP_SAMPLE, random_state=42).reset_index(drop=True)
    else:
        sample = feat
    print(f"     [3] 全局 SHAP 采样 {len(sample):,} / {len(feat):,} 行")

    shap_global = explainer.shap_values(sample[FEATURE_COLS])
    global_imp = pd.DataFrame({
        "feature": FEATURE_COLS,
        "mean_abs_shap": np.abs(shap_global).mean(axis=0),
        "mean_shap": shap_global.mean(axis=0),
    }).sort_values("mean_abs_shap", ascending=False).reset_index(drop=True)

    g_out = EXPLAIN_DIR / "global_shap_importance.csv"
    global_imp.to_csv(g_out, index=False)
    print(f"     → {g_out}")
    print()
    print(f"     全局 SHAP 重要度 Top-5:")
    print(global_imp.head(5).to_string(index=False))
    print()

    # ---- 4. 当日 loop 级逐样本解释 ----
    today = feat["op_day"].max()
    today_rows = feat[feat["op_day"] == today].copy().reset_index(drop=True)
    print(f"     [4] 解释工厂日: {today.date()}  "
          f"({len(today_rows)} 个 loop)")

    shap_today = explainer.shap_values(today_rows[FEATURE_COLS])
    pred_today = model.predict(today_rows[FEATURE_COLS])

    # additivity 校验: base + Σshap ≈ pred
    recon = base_value + shap_today.sum(axis=1)
    max_err = float(np.abs(recon - pred_today).max())
    print(f"     additivity 校验  max|base+Σshap − pred| = {max_err:.6f}")

    # SHAP 列加 shap_ 前缀，避免和分类特征 loop 冲突
    shap_cols = [f"shap_{c}" for c in FEATURE_COLS]
    loop_long = pd.DataFrame(shap_today, columns=shap_cols)
    loop_long.insert(0, "loop", today_rows["loop"].values)
    loop_long.insert(1, "pred", pred_today.round(2))
    loop_long.insert(2, "base_value", round(base_value, 2))

    op_day_str = today.date().isoformat()
    loop_out = EXPLAIN_DIR / f"{op_day_str}_loop_shap.csv"
    loop_long.to_csv(loop_out, index=False)
    print(f"     → {loop_out}")
    print()

    # ---- 5. 控制台打印 loop 级 SHAP 解读 ----
    print(f"     === Loop 级 SHAP 解读（Top-3 推高 / 拉低）===")
    for _, row in loop_long.iterrows():
        contribs = row[shap_cols].astype(float)
        contribs.index = [c.removeprefix("shap_") for c in contribs.index]
        contribs = contribs.sort_values(ascending=False)
        top_pos = contribs.head(3)
        top_neg = contribs.tail(3).iloc[::-1]
        print(f"\n     loop={row['loop']}  pred={row['pred']:.0f}  "
              f"= base({base_value:.0f}) + Σshap({contribs.sum():+.0f})")
        print(f"       推高 Top-3: "
              + ", ".join(f"{f}={v:+.1f}" for f, v in top_pos.items()))
        print(f"       拉低 Top-3: "
              + ", ".join(f"{f}={v:+.1f}" for f, v in top_neg.items()))


if __name__ == "__main__":
    main()
