# 华虹半导体 Move 分析项目 — git4data 应用指南

> 面向项目算法开发者，说明 mo git4data 特性在项目交付中该怎么做、不该怎么做。
>
> 编写日期: 2026-05-25 | 基于 MatrixOne v3.0.11 git4data（实验验证见本目录各 exp_* 脚本）

---

## 目录

1. [核心原则](#1-核心原则)
2. [该做的：四个高价值场景](#2-该做的四个高价值场景)
3. [不该做的：四个浪费/反模式](#3-不该做的四个浪费反模式)
4. [快照生命周期管理](#4-快照生命周期管理)
5. [已知坑与规避](#5-已知坑与规避)
6. [完整代码模板](#6-完整代码模板)

---

## 1. 核心原则

**只在需要时可追溯、需隔离、需恢复的地方版本化。其他环节不要碰。**

筛选标准（全部满足才用 git4data）：

- ✅ 数据是**结构化表**
- ✅ 运行时**消耗快照资源后能带来排查/回溯价值**
- ✅ 快照/分支的维护成本 **<** 其带来的调试便利性

---

## 2. 该做的：四个高价值场景

### 2.1 每次预测运行前 — 打 features 快照（最高价值）

**问题**：系统每天 07:50/08:20/16:00 跑三次预测 Pipeline。如果某次报告数字异常，你第一个问的是"模型当时看到的 features 是什么"——但当前表已被后续运行覆盖。

**做法**：

```python
# scheduler/pipeline.py — 每个预测版本运行前

predict_version = f"pred_{date}_{version}"  # e.g. pred_20260410_v2

# 对参与预测的核心事实表和物化特征表打快照
mo.execute(f"CREATE SNAPSHOT {predict_version} FOR TABLE fact_move, fact_machine_status, fact_wip_snapshot, feature_engineered")

# 然后正常运行预测
run_prediction(date, version)
```

**回溯查询**：

```sql
-- 出问题时，直接在快照上查模型当时的输入
SELECT * FROM feature_engineered {snapshot='pred_20260410_v2'} WHERE fab_id = 'FAB-A';

-- 对比今天和昨天的特征分布差异
SELECT capability, AVG(wip_cv) FROM feature_engineered {snapshot='pred_20260410_v2'}
GROUP BY capability
EXCEPT
SELECT capability, AVG(wip_cv) FROM feature_engineered {snapshot='pred_20260409_v2'}
GROUP BY capability;

-- 看两天输入数据量差异
SELECT COUNT(*), 'fact_move' AS tbl FROM fact_move {snapshot='pred_20260410_v2'}
UNION ALL
SELECT COUNT(*), 'fact_move' FROM fact_move {snapshot='pred_20260409_v2'};
```

**覆盖哪些表**：

| 表 | 必要性 | 原因 |
|------|----------|------|
| `feature_engineered`（物化特征表） | **必须** | 模型直接输入，调试偏差的第一现场 |
| `fact_move` | 建议 | 原始Move数据，用于验证数据质量问题 |
| `fact_machine_status` | 建议 | 机台状态，影响Uptime计算 |
| `fact_wip_snapshot` | 可选 | WIP分布，若偏差排查发现WIP相关特征异常时需回溯 |

**保留策略**：7天（预测快照7天后可删，见[快照生命周期管理](#4-快照生命周期管理)）。

---

### 2.2 每次模型训练前 — 打训练集快照

**问题**：月度/触发式重训后模型精度波动，需要知道"这次训练数据跟上个月有什么不同"。MLflow记录了模型 artifact，但训练数据的具体内容只有快照能锁住。

**做法**：

```python
# training/pipeline.py — 训练数据准备完成后、训练开始前

train_snapshot = f"train_{run_date}"  # e.g. train_20260501

# 训练数据准备（SQL 抽取 6 个月历史）
mo.execute("""
    CREATE TABLE train_dataset AS
    SELECT ... FROM fact_move, fact_machine_status, ... WHERE date >= DATE_SUB(:run_date, INTERVAL 6 MONTH)
""")

# 对训练数据集打快照
mo.execute(f"CREATE SNAPSHOT {train_snapshot} FOR TABLE train_dataset")

# 然后训练
train_model("train_dataset", run_date)
```

**回溯查询**：

```sql
-- 行级对比两个月的训练数据差异
DATA BRANCH DIFF train_dataset {snapshot='train_20260501'}
           AGAINST train_dataset {snapshot='train_20260401'} OUTPUT SUMMARY;
-- 输出: INSERTED=3200, DELETED=1500, UPDATED=800

-- 看某类产品是否突然增多
SELECT product, COUNT(*), SUM(actual_move)
FROM train_dataset {snapshot='train_20260501'}
GROUP BY product
EXCEPT
SELECT product, COUNT(*), SUM(actual_move)
FROM train_dataset {snapshot='train_20260401'}
GROUP BY product;
```

**保留策略**：3个月（训练快照保留到下次重训确认没问题后，保留最近2个版本即可）。

---

### 2.3 特征工程迭代 — 用 DATA BRANCH 隔离

**问题**：你开发新特征（例如 WIP CV 从 7 日均值改为 14 日均值），想在不影响生产 Pipeline 的情况下验证效果，还要精确知道改了哪些特征值。

**做法**：

```sql
-- Step 1: 从生产特征表创建分支（零拷贝，不影响生产）
DATA BRANCH CREATE TABLE feature_exp FROM feature_prod;

-- Step 2: 在分支上开发新特征（SQL 原地更新）
UPDATE feature_exp SET wip_cv_14d = (
    SELECT STDDEV(wip_qty) / AVG(wip_qty)
    FROM fact_wip_snapshot w
    WHERE w.fab_id = feature_exp.fab_id
      AND w.capability = feature_exp.capability
      AND w.timestamp BETWEEN DATE_SUB(:date, INTERVAL 14 DAY) AND :date
);

-- Step 3: 对比分支与生产，精确看到改了哪些行
DATA BRANCH DIFF feature_exp AGAINST feature_prod OUTPUT SUMMARY;
-- 期望输出: UPDATED=120（只有 120 行特征值变了，其他行不变）

-- 如果在分支上验证预测效果好
-- Step 4: 合并回生产
DATA BRANCH MERGE feature_exp INTO feature_prod WHEN CONFLICT ACCEPT;
```

**对比"无 git4data"的做法**：

| | 无 git4data | 有 git4data |
|--|-------------|-------------|
| 隔离方式 | 新建表或Python离线计算 | DATA BRANCH，零拷贝 |
| 如何对比 | 手写 SELECT join 两表 | `DATA BRANCH DIFF` 秒出 |
| 合并风险 | 手动 COPY/MERGE，可能漏行 | 原子 MERGE + 冲突策略 |
| 回退 | 需写回退 SQL | RESTORE 即可 |

**什么时候用 DATA BRANCH**：仅当改动涉及**特征值的实质性变更**（计算逻辑改、窗口期调、新特征加入）且需要在生产旁并行验证时。

**什么时候不用**：仅加一个简单 SQL 过滤条件（如 WHERE clause）或直接在现有特征表上 UPDATE 可完成时，不用开分支。

---

### 2.4 误操作/坏数据恢复 — 用 PITR 兜底

**问题**：增量同步拉入损坏数据，或误 UPDATE/DELETE。7天内的事故可用 PITR 秒级恢复。

**设置 PITR**（项目初始化时执行一次）：

```sql
-- 为整个项目的库开启 PITR，保留 14 天窗口
CREATE PITR hh_move_pitr FOR DATABASE hh_move RANGE 14 'd';
```

**恢复**：

```sql
-- 恢复到坏数据进入前的任意时刻
RESTORE DATABASE hh_move TABLE result_prediction FROM PITR hh_move_pitr "2026-04-10 08:00:00";

-- ⚠️ 注意：PITR RESTORE 的语法不带 ACCOUNT（与 SNAPSHOT RESTORE 不一致！）
```

**vs SNAPSHOT RESTORE**：

```sql
-- SNAPSHOT 版（需要 ACCOUNT 名）
RESTORE ACCOUNT <tenant> DATABASE hh_move TABLE fact_move FROM SNAPSHOT daily_backup_20260410;

-- PITR 版（不需要 ACCOUNT，语法不一致）
RESTORE DATABASE hh_move fact_move FROM PITR hh_move_pitr "2026-04-10 08:00:00";
```

**保留策略**：PITR 设置14天即可，不用额外维护。

---

## 3. 不该做的：四个浪费/反模式

### 3.1 每次报告生成都打库级快照 ❌

**不要这样**：

```python
# ❌ 反模式：每次生成报告都打全库快照
for version in ['v1', 'v2', 'noon']:
    mo.execute(f"CREATE SNAPSHOT report_{date}_{version} FOR DATABASE hh_move")
    generate_report(date, version)
```

**理由**：
- 每天3个快照 × 365天 = 1095个快照/年
- 报告Pipeline读取的result_*表本身有版本字段，自带结果追踪
- 90%的快照永远不会被回溯，等于白建

**正确做法**：只对预测前的时间敏感数据（features + 核心事实表）打快照，省掉报告生成和结果表。

### 3.2 用于在线学习参数校准 ❌

**不要这样**：

```sql
-- ❌ 反模式：用 git4data 版本化每次参数校准
CREATE SNAPSHOT param_calib_20260410 FOR TABLE calib_params;
```

**理由**：
- 参数校准的输入（最近3天偏差）和输出（PM/Unbalance/Down系数）都已经记录在 `result_prediction` 表和日志中
- 参数本身是几十个浮点数，直接存表里带时间戳就够了
- git4data 在这里解决的问题（"上次校准的值是什么"）已经被现有表结构解决

**正确做法**：`SELECT * FROM calib_params ORDER by version DESC LIMIT 2;`

### 3.3 用于 NL2SQL/对话引擎的 SQL 模板版本化 ❌

**理由**：SQL 模板是代码，不是数据。代码版本用 git，不是 git4data。

### 3.4 在显式事务内做 DATA BRANCH MERGE/PICK ❌

**不要这样**：

```sql
BEGIN;
UPDATE feature_prod SET ...;
DATA BRANCH MERGE feature_exp INTO feature_prod WHEN CONFLICT ACCEPT;  -- ❌ 报错
COMMIT;
```

**理由**：MatrixOne v3.0.11 限制——`MERGE`/`PICK` 不能在显式事务 `BEGIN…COMMIT` 内执行，会报 `unclassified statement appears in uncommitted transaction`。必须在 autocommit 下单独执行。

**正确做法**：MERGE 作为独立操作执行，不包在事务里。

---

## 4. 快照生命周期管理

用完不删 = 存储膨胀。必须设置清理策略。

### 4.1 按类型设置保留期

| 快照类型 | 命名模式 | 保留期 | 说明 |
|----------|----------|--------|------|
| 预测快照 | `pred_YYYYMMDD_v[1-3]` | **7天** | 调试周期通常不超过一周 |
| 训练快照 | `train_YYYYMMDD` | **3个月** | 保留到下次重训确认没问题，保留最近2版 |
| 分支数据 | `feature_exp_*` | **分支合并后即删** | 可手动 DROP 分支表 |

### 4.2 自动清理（CREATE TASK）

MatrixOne v3.0.11 内置 `CREATE TASK`，可用于定时清理过期快照。

```sql
-- 每天凌晨清理 7 天前的预测快照
CREATE TASK cleanup_pred_snapshots
    SCHEDULE  '0 3 * * *'
    BEGIN
        -- 需要先查询系统表获得快照列表，用 DROP SNAPSHOT 逐个删除
        -- 注意：此处需要编写查询 mo_snapshots() 或等效系统表的逻辑
        -- 具体语法需参考 MatrixOne 系统表文档
    END;
```

⚠️ 注意：`CREATE TASK` 的 `BEGIN…END` 体内含分号时，标准 CLI 会被截断。需要用 DELIMITER 或通过驱动整条发送。

### 4.3 手动清理（调试完成即删）

```sql
-- 预测快照：确认调试没问题后立即删
DROP SNAPSHOT pred_20260410_v2;

-- 分支表：合并后 DROP
DROP TABLE feature_exp;
```

---

## 5. 已知坑与规避

| # | 现象 | 规避 |
|---|------|------|
| 1 | `RESTORE … FROM PITR` **不能带** `ACCOUNT`（语法错）；`RESTORE … FROM SNAPSHOT` **必须带** `ACCOUNT` | 两种 restore 记两套写法，别混 |
| 2 | `SELECT` 的 `{snapshot=}` 是语句级作用域：同一条 SQL 里两个不同快照会塌缩成一个 | 用 `DATA BRANCH DIFF t {snapshot='b'} AGAINST t {snapshot='a'}` 替代 |
| 3 | `MERGE`/`PICK` 不能在 `BEGIN…COMMIT` 事务内执行 | 作为独立操作执行 |
| 4 | `DATA BRANCH DIFF/MERGE` 只有表级，无库级形式 | 多表 diff/merge 需逐表执行 |
| 5 | diff/merge 要求两表 schema 完全一致；分支上 ALTER ADD COLUMN 后报错 | 特征演进前先对齐 schema |
| 6 | CTAS 建的表没有主键，不能直接做 DATA BRANCH | 先 `CREATE TABLE(… PRIMARY KEY)` 再 `INSERT … SELECT` |
| 7 | 删后再插同一主键的行，DIFF 会识别为 INSERTED 而非 UPDATED | 用原地 `UPDATE … JOIN` 而非 DELETE+INSERT |
| 8 | `{MO_TS=}` 时间戳时间旅行不可靠（拒绝日期字符串、纳秒整数报错） | 用快照名或 PITR，别用 MO_TS |

---

## 6. 完整代码模板

### 6.1 预测 Pipeline（每日运行）

```python
# scheduler/daily_pipeline.py

import datetime
from mo_client import MOClient

mo = MOClient()

date = datetime.date.today().isoformat()

for version in ['v1', 'v2', 'noon']:
    snapshot_name = f"pred_{date}_{version}"

    # ✅ 打快照：锁定预测时的数据状态
    mo.execute(f"""
        CREATE SNAPSHOT {snapshot_name}
        FOR TABLE fact_move, fact_machine_status, fact_wip_snapshot, feature_engineered
    """)

    # 正常运行预测
    run_prediction(date, version)

# 清理 7 天前的旧快照（可选）
mo.execute("DROP SNAPSHOT pred_20260410_v1");
mo.execute("DROP SNAPSHOT pred_20260410_v2");
mo.execute("DROP SNAPSHOT pred_20260410_noon");
```

### 6.2 模型训练 Pipeline（月度/触发式运行）

```python
# training/retrain.py

run_date = "2026-05-01"
snapshot_name = f"train_{run_date}"

# 准备训练数据
mo.execute("""
    CREATE TABLE train_dataset (
        ... 字段定义 + PRIMARY KEY ...
    )
""")
mo.execute(f"INSERT INTO train_dataset SELECT ... FROM fact_move WHERE ...")

# ✅ 打快照
mo.execute(f"CREATE SNAPSHOT {snapshot_name} FOR TABLE train_dataset")

# 训练
train_and_register("train_dataset", run_date)

# ✅ 日志记录（写入模型注册表或实验跟踪系统）
log_artifact("training_data_snapshot", snapshot_name)  # 与 MLflow run 关联
```

### 6.3 特征工程实验（不定期）

```python
# experiments/feature_iteration.py

branch_name = "feature_wip_cv_14d"

# ✅ 分支隔离
mo.execute(f"DATA BRANCH CREATE TABLE {branch_name} FROM feature_prod")

# 在分支上计算新特征
mo.execute(f"""
    UPDATE {branch_name} SET wip_cv = (
        SELECT STDDEV(wip_qty) / AVG(wip_qty)
        FROM fact_wip_snapshot w
        WHERE w.fab_id = {branch_name}.fab_id
          AND w.timestamp BETWEEN DATE_SUB('2026-04-10', INTERVAL 14 DAY) AND '2026-04-10'
    )
""")

# ✅ 看改了哪些行
diff_result = mo.execute(f"DATA BRANCH DIFF {branch_name} AGAINST feature_prod OUTPUT SUMMARY")
print(diff_result)  # 期望: UPDATED=120

# 如果效果好 → 合并
mo.execute(f"DATA BRANCH MERGE {branch_name} INTO feature_prod WHEN CONFLICT ACCEPT")

# ✅ 清理：DROP 分支表
mo.execute(f"DROP TABLE {branch_name}")
```

---

## 总原则：分层版本控制

```
┌─────────────────────────────────────────────────┐
│  Pipeline 版本 （Cron + DAG 定义） → git         │  代码版本
├─────────────────────────────────────────────────┤
│  模型版本（权重/hyperparams/指标） → MLflow      │  artifact 版本
├─────────────────────────────────────────────────┤
│  Run 输出（预测值/归因/预警）→ result_* 表带 version│  业务输出版本
├─────────────────────────────────────────────────┤
│  ★ 训练数据版本 → git4data SNAPSHOT              │  数据版本 ← 新增
│  ★ 预测输入版本 → git4data SNAPSHOT              │  数据版本 ← 新增
├─────────────────────────────────────────────────┤
│  原始数据 → Oracle 侧自有备份                    │  数据源版本
└─────────────────────────────────────────────────┘
```

git4data 补的是"训练数据和预测输入"这一层的数据版本化——刚好是其他工具覆盖不到的空白。

用对地方，成本极低（3-5行代码 + ~30ms/snapshot），收益在每次排查偏差时兑现。
