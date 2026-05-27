"""Pipeline 共享配置 —— 所有路径、常量、超参的唯一来源。

每个阶段脚本开头用：
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from _config import *

现场切换真实数据只改：
    - START_DATE / END_DATE           （数据范围）
    - 01_load.py 里的 EXPECTED_TABLES（如果表名不一致）

时间口径假设：
    所有源表的 DATE 字段都已经是工厂日（客户 2026-05-11 确认）。
    pipeline 直接拿来用，不再做日历日 → 工厂日的换算。
"""
from __future__ import annotations

from pathlib import Path

# ---------- 路径 ----------
PROJ_ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = PROJ_ROOT / "artifacts"
RAW_DIR = ARTIFACTS / "raw"
CLEAN_DIR = ARTIFACTS / "clean"
FEATURES_DIR = ARTIFACTS / "features"
MODELS_DIR = ARTIFACTS / "models"
PREDS_DIR = ARTIFACTS / "predictions"
EXPLAIN_DIR = ARTIFACTS / "explain"

for _p in [RAW_DIR, CLEAN_DIR, FEATURES_DIR, MODELS_DIR, PREDS_DIR, EXPLAIN_DIR]:
    _p.mkdir(parents=True, exist_ok=True)


# ---------- 时间 ----------
START_DATE = "2024-11-01"             # 训练数据起点（mock 用，现场改）
END_DATE = "2026-04-30"               # 训练数据终点


# ---------- 业务粒度 ----------
# 2026-05-15 决策：去掉 fab 维度，只按 loop × op_day 聚合
# 原因：客户 tb_product_list.fabid 全 null，"product_list 拿可信 fab" 这条路
#       走不通；prodindex.fabid 也已确认部分混淆。客户验收口径只要全厂 daily
#       move（不区分 fab6/fab8），所以直接跨 fab 求和最干净。
# 影响：训练样本从 4368 行（2 FAB × 12 loop × 365 天）变成 2184 行；
#       merge 时不再 on fab_id；FAB-day SHAP 解释不再产出。
GROUP_COLS = ["loop"]                 # 训练聚合粒度（只按 loop）
TIME_COL = "op_day"                   # 工厂日字段名
TARGET_COL = "y"                      # 标签字段名


# ---------- Label 重建配置（客户 SQL 复刻）----------
# 客户 2026-05-18 给的最终 label SQL（替代 PDF 末尾的旧版本）：
#   SELECT histdate,
#          sum(n20_move1) saqp, ..., sum(n20_move12) passn20,
#          sum(move1), ..., sum(move8),
#          sum(bsi_move1), ..., sum(bsi_move6)
#   FROM mfgcim.TB_DAILY_PRODINDEX A
#   WHERE A.productiontype = 'Production'
#   GROUP BY histdate
#
# 跟旧版本的关键差异：
#   ① MOVE 列从 12 → 26：N20 12 个 + 标准 8 个 + BSI 6 个
#   ② 不再 join tb_product_list、不再做 tech 白名单过滤
#
# LOOP_COLUMN_MAP：源表里 MOVE 宽列名 → pipeline 用的 loop 名。
# 命名约定：客户只明确给了 SAQP 和 PASSN20 两个业务名，其他 24 个直接沿用列名
#           结构（n20_loop2..n20_loop11 / loop1..loop8 / bsi_loop1..bsi_loop6），
#           等客户给业务命名时一键替换即可。
# 兼容性：mock 只生成 move1..move4，pipeline 用"列名 intersect"逻辑自动取交集：
#         mock 端只用 4 个普通 loop，现场端自动启用全 26 个 loop。
LOOP_COLUMN_MAP = {
    # —— N20 系列（12 个）——
    "n20_move1":  "SAQP",         # 客户明确命名
    "n20_move2":  "n20_loop2",
    "n20_move3":  "n20_loop3",
    "n20_move4":  "n20_loop4",
    "n20_move5":  "n20_loop5",
    "n20_move6":  "n20_loop6",
    "n20_move7":  "n20_loop7",
    "n20_move8":  "n20_loop8",
    "n20_move9":  "n20_loop9",
    "n20_move10": "n20_loop10",
    "n20_move11": "n20_loop11",
    "n20_move12": "PASSN20",      # 客户明确命名
    # —— 标准系列（11 个）——
    # mock 只有 move1..move4，现场有 move1..move11
    # 2026-05-19: 补齐 move9..move11（PDF 字段表确认存在，之前 _config 只列到 move8）
    "move1":  "loop1",
    "move2":  "loop2",
    "move3":  "loop3",
    "move4":  "loop4",
    "move5":  "loop5",
    "move6":  "loop6",
    "move7":  "loop7",
    "move8":  "loop8",
    "move9":  "loop9",
    "move10": "loop10",
    "move11": "loop11",
    # —— BSI 背照式系列（6 个）——
    "bsi_move1": "bsi_loop1",
    "bsi_move2": "bsi_loop2",
    "bsi_move3": "bsi_loop3",
    "bsi_move4": "bsi_loop4",
    "bsi_move5": "bsi_loop5",
    "bsi_move6": "bsi_loop6",
}

# tb_daily_prodindex.productiontype 过滤（只算正式生产，不算工程片）
PRODUCTIONTYPE_FILTER = "Production"

# ⚠️ [DEPRECATED 2026-05-18] 客户已确认新 label SQL 不再做 tech 白名单过滤。
# 保留常量本体以避免其他模块 import 报错，下一轮清理时删。
TECH_WHITELIST = [
    "H1",
    "N20B", "N20B-*", "N20B-F8", "N20B-M", "N20B-S",
    "N20C", "N20C-*", "N20C-BM", "N20C-M",
    "N20D-M",
]

# ⚠️ [DEPRECATED 2026-05-18] step1b（prodindex 派生特征）已删，priority 不再用。
# 保留常量本体以避免其他模块 import 报错。
PRIORITY_HIGH_THRESHOLD = 2


# ---------- 特征列 ----------
LAG_FEATURES = ["lag_1", "lag_7", "roll_7_mean", "roll_28_mean"]

# ─── 外生 lag 特征 ─────────────────────────────────────────────────
# 原则：base.parquet 里的所有日级聚合列都必须 lag(1) 才能进模型，
# 否则就是泄露（T 08:00 拿不到 T 当天的聚合，只能拿到 T-1 的）。
#
# EXOG_BASE_COLS = "要 lag 的源列名列表"
# EXOG_LAG_FEATURES = 自动生成 "<col>_lag_1" 训练特征名
#
# 排除（不进训练）的 base 列：
#   - total_machine_move / total_prdmove / total_engmove
#     → 都是 OEE 表里聚出来的 MOVE，本身就是 label 的另一种算法；
#       lag(1) 后跟 LAG_FEATURES 的 lag_1 信息重复
#   - total_fab6/8_prodmove / total_fab6/8_engmove
#       FAB 拆分 MOVE，同上
#   - ls_total_move
#       lotstep 派生的 MOVE 总量，同上
#   → 这些列留在 base.parquet 做 label 对账用，不进特征
EXOG_BASE_COLS = [
    # ─── OEE 派生 —— 机台稼动 / 排队 ─────────────────────────────────
    # 来源：agg_oee_loop_day（Oracle 端由 summary_oee_new + 机台能力/flow 映射
    # 分摊到 loop×op_day）。旧 summary_oee_new 广播口径仅作本地 fallback。
    "down_hours", "pm_hours", "idle_hours",
    "mean_uptime_rate", "mean_oee_rate", "mean_eff_rate", "mean_prod_rate",
    # 2026-05-19: actualwph/targetwph 是机台速率(WPH)，跨机台 sum 没物理意义
    #   → 在 04_aggregate.step3_oee_day 改为 mean，列名同步从 total_* → mean_*
    "mean_actualwph", "mean_targetwph",
    "mean_qtime", "total_hold_mfg", "total_wait_mfg",
    # ─── OEE 稼动细分（P2 新增 2026-05-15）──────────────────────────
    # avai/run：跟 down/pm/idle 互补的时段拆分，给模型更精细的稼动信号
    "total_avai", "total_run",
    # ee/pe 类 hold/wait：工程/工艺侧停机（量级小但跟 Move 直接负相关，
    # mfg 那一组是大头，这里补"非制造侧"的卡停）
    "total_hold_ee", "total_hold_pe",
    "total_wait_ee", "total_wait_pe",
    # 速度 / 效率口径：跟 mean_uptime_rate / actualwph 互校验
    "mean_uptime_rate_ie", "mean_pwph", "mean_effi",
    # ⚠️ [DEPRECATED 2026-05-18] prodindex 派生的 14 个 prod_* 字段已删
    #   原因：客户明确说 tb_daily_prodindex 里的非-MOVE 字段都不适合做特征。
    #   影响：step1b 整个函数已删；base.parquet 不再含 prod_waferstart 等列；
    #         样本量靠 26 loop（vs 旧 12 loop）扩大来补偿。
    # ─── lotstephistory loop 日级聚合 —— WIP / 跑货活跃度 ─────────────
    "ls_n_events", "ls_n_lots",
    "ls_avg_qt", "ls_avg_rt",
    "ls_n_products",
    # ─── prodindex loop 级 QT / HT（2026-05-19 客户更新）───────────────
    # 04_aggregate.step1b_prodindex_qt_ht 把 prodindex 里
    # qt1..qt11 / n20_qt1..n20_qt12 / bsi_qt1..bsi_qt6（ht 同理）
    # 按 LOOP_COLUMN_MAP 的命名规则 unpivot 成 (loop, op_day, prod_qt, prod_ht)。
    "prod_qt", "prod_ht",
]
EXOG_LAG_FEATURES = [f"{c}_lag_1" for c in EXOG_BASE_COLS]

# ─── 同日已知特征（不 lag）─────────────────────────────────────────────
# 物理含义：D 日 08:00 拍下的 WIP 快照已经发生在 D 日 Move 计算窗口之前，
#   是 D 日 op_day 时刻的 "已知初始条件"，不存在泄露 → 不需要 lag(1)。
# 跟 EXOG_BASE_COLS（D-1 日全天累计统计）是两类特征，处理路径不同：
#   - EXOG_BASE_COLS         → 05_features.add_exog_lag 自动 lag(1)
#   - WIP_SAMEDAY_FEATURES   → 04_aggregate.step5_wip_snapshot 直接 merge，05 不动
# 数据源：Oracle 端已聚合到 (loop, op_day)，导出表 agg_wip_snapshot_loop_day。
# 字段定义和聚合公式见 Oracle数据导出SQL清单.md § 2.6。
WIP_SAMEDAY_FEATURES = [
    "wip8_total_qty",       # D 日 08:00 待加工片数
    "wip8_n_lots",          # 活跃 lot 数
    "wip8_n_run",           # status='RUN'
    "wip8_n_wait",          # status='Wait'
    "wip8_n_qhld",          # Q-time hold
    "wip8_n_rhld",          # R-time hold
    "wip8_n_bank",          # bankstate='Bank'
    "wip8_n_high_priority", # priority<=2
    "wip8_avg_priority",
    "wip8_n_rework",        # returnstepsequence>0
    "wip8_n_recipe",        # 配方多样性
    "wip8_n_capability",    # 设备能力多样性
    "wip8_qht_sum",         # 累计 Q-time hold 时间
    "wip8_age_max_h",       # 队列最老 lot 等待小时数
]

# 日历特征：客户 2026-05-15 确认 holiday 对其 Move 影响不显著（路径主要走 PM，
# PM 已经在 oee 派生的 pm_hours / down_hours 里）。整组拿掉，包括非 holiday 的
# is_weekend / is_month_end / day_of_year。如要恢复 month_end（半导体月末冲量）
# 把对应字段重新加入这个列表 + 05_features.add_calendar() 重启即可。
CAL_FEATURES: list[str] = []
CAT_FEATURES = ["loop"]               # LightGBM 类别原生支持（去 fab 后只剩 loop）

FEATURE_COLS = (
    LAG_FEATURES + EXOG_LAG_FEATURES + WIP_SAMEDAY_FEATURES
    + CAL_FEATURES + CAT_FEATURES
)


# ---------- 数据清理阈值 ----------
NULL_DROP_THRESHOLD = 0.80            # 空值率 > 80% 直接删列


# ---------- 模型选择 ----------
# "lightgbm" → 走 LGB_PARAMS；"xgboost" → 走 XGB_PARAMS
# 切换后 07_train.py 自动用对应的模型类 + 超参，其他脚本无需改动
MODEL_TYPE = "xgboost"


# ---------- LightGBM 超参（针对小数据强正则）----------
# objective="regression_l1" (MAE) —— 默认 "regression" 是 MSE，对大单元 loop 容量
# 倾斜，跟 MAPE 评测口径错位。MAE 在量级稳定的预测下跟 MAPE 近似同号，且比
# "mape" 原生 objective 更稳（"mape" 在 y→0 时梯度会爆）。
LGB_PARAMS = dict(
    objective="regression_l1",
    n_estimators=500,
    learning_rate=0.03,
    max_depth=5,
    num_leaves=15,
    min_child_samples=20,
    reg_alpha=0.5,
    reg_lambda=2.0,
    feature_fraction=0.7,
    bagging_fraction=0.8,
    bagging_freq=5,
    random_state=42,
    verbose=-1,
)


# ---------- XGBoost 超参（跟 LGB_PARAMS 对位）----------
# 命名差异速查：
#   LGB                       XGB
#   objective=regression_l1   objective=reg:absoluteerror   (MAE)
#   num_leaves / max_depth    max_depth (XGB 没有 num_leaves)
#   min_child_samples         min_child_weight
#   feature_fraction          colsample_bytree
#   bagging_fraction          subsample
#   bagging_freq              （无对应）
#   verbose=-1                verbosity=0
#   early_stopping(callback)  early_stopping_rounds (构造时传)
# enable_categorical=True 让 XGB 自动处理 category dtype 列（不能再用 categorical_feature）
XGB_PARAMS = dict(
    objective="reg:absoluteerror",
    n_estimators=500,
    learning_rate=0.03,
    max_depth=5,
    min_child_weight=5,
    reg_alpha=0.5,
    reg_lambda=2.0,
    colsample_bytree=0.7,
    subsample=0.8,
    gamma=0.0,
    random_state=42,
    enable_categorical=True,
    early_stopping_rounds=50,
    verbosity=0,
)


# ---------- 时间序列 CV ----------
CV_FOLDS = 5
CV_TEST_DAYS = 30                     # 每折 hold-out 30 个工厂日
FINAL_HOLDOUT_DAYS = 60               # 最后 N 天只做最终评估，不参与 Optuna/CV


# ---------- Optuna 超参搜索 ----------
# > 0：开启搜索，跑 N 个 trial 找最优超参后再做最终训练
# = 0：关闭搜索，直接用上面 LGB_PARAMS 默认值（开发调试时省时间）
# 时间预算：mock 上 30 trials ≈ 1-2 分钟；现场更大数据集 ≈ 5 分钟
# 搜索空间和目标函数定义在 07_train.py:tune_lgb_params
OPTUNA_N_TRIALS = 30
