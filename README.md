# git4data 实验教程：用 18 个实验学会并评估 MatrixOne 的「数据版本控制」能力

> 一份**动手教程**：通过一组**可独立重跑、自清理**的实验，系统学习 MatrixOne
> **git4data**（快照 / 时间旅行 / 克隆 / 回滚 / PITR / `DATA BRANCH` 分支·diff·merge·
> cherry-pick），并以业界「git for data」的事实标准 **lakeFS** 作为对照基准，搞清楚
> 它**能做什么、好不好用、边界在哪、典型怎么用**。
>
> 想直接看结论与深度分析：**[COMPARISON.md](COMPARISON.md)**（§1–12，全部由下方实验实测背书）。

---

## 结论速览（TL;DR）

- **git4data 是一套完整的「git for data」**：snapshot/clone/restore/PITR + 原生
  `DATA BRANCH` 的 **行级** diff / merge（带冲突策略）/ cherry-pick——能力不输 lakeFS，
  在**结构化数据**上粒度更细（行级 vs lakeFS 的文件级）。
- **最契合的场景**：结构化训练数据的版本化/复现、持续标注、实时流（每事务一版本 + PITR
  任意时间点）、SFT/RLHF 数据策展、Write-Audit-Publish、增量训练、Agent 进化。
- **lakeFS 仍是主场的地方**：海量**非结构化字节**（图像/视频/语料/权重）的内容级版本、
  多引擎直读、整仓库多文件原子提交。
- **诚实的性能结论**：纯策展吞吐 lakeFS+DuckDB 反而更快；MatrixOne 的价值在
  **「存+版本+计算+服务」一处、行级语义、可复现、少运维**，不是裸算速度。
- **最佳实践**：**组合使用**——lakeFS 管字节、MatrixOne 管结构化目录/策展，目录记
  lakeFS commit 串血缘（见[第 6 课](#第-6-课--组合使用lakefs--matrixone)）。

---

## 目录

1. [为什么拿 lakeFS 做对照](#1-为什么拿-lakefs-做对照)
2. [背景：git4data 与 lakeFS 各是什么](#2-背景git4data-与-lakefs-各是什么)
3. [环境准备](#3-环境准备)
4. [教程路径（按顺序学）](#4-教程路径按顺序学)
5. [做了哪些测试（实验全表）](#5-做了哪些测试实验全表)
6. [典型用法建议（可直接抄的配方）](#6-典型用法建议可直接抄的配方)
7. [仓库结构](#7-仓库结构)
8. [安全与复现说明](#8-安全与复现说明)

---

## 1. 为什么拿 lakeFS 做对照

「给数据做版本控制」这件事，**lakeFS 是事实上的参照物**——它是最成熟、git 语义最完整的
对象存储数据版本工具（`commit` / `branch` / `merge` / `diff` / `revert` / `tag`）。

所以评估 MatrixOne git4data 是否「够用」，最有说服力的方法不是孤立地试它的命令，而是
**拿它和 lakeFS 跑同一套场景做 apples-to-apples 对比**：相同的数据、相同的模型、相同的
剧情，只替换「版本控制的动词」。这样能干净地回答三个问题：

1. **能力是否完整**——snapshot / branch / diff / merge / rollback 是否都有、好不好用？
2. **真实工作流里是否顺手**——ML/数据平台的持续学习、策展、审计发布、复现，谁更省事？
3. **边界在哪**——哪些场景 lakeFS 明显更合适，哪些反而是 git4data 的强项？

本仓库的两个 demo（`matrixone/run_demo.py` 与 `lakefs_demo/run_demo.py`）就是这套对照的
骨架：**同一个数据生成器 + 同一个增量模型 + 同一套五幕剧情**，因此两边精度数字逐位一致，
差异只体现在「版本控制怎么做」。

---

## 2. 背景：git4data 与 lakeFS 各是什么

| 能力 | git 类比 | **MatrixOne git4data** | **lakeFS** |
|---|---|---|---|
| 提交 / 打标 | commit / tag | `CREATE SNAPSHOT`（表/库/账户级） | `commit` + `tag`（**整仓库原子**） |
| 读历史版本 | checkout | `SELECT … {snapshot='…'}`（时间旅行） | 读 `ref`（commit/tag/branch）下对象 |
| 连续时间点恢复 | —— | `CREATE PITR … RANGE` + `RESTORE … FROM PITR "ts"`（**任意时刻**） | 无（只能到离散 commit） |
| 克隆 / 分支 | branch | `DATA BRANCH CREATE`（带血缘）/ `CLONE`（零拷贝） | `branch create`（零拷贝、整仓库） |
| 差异 | diff | `DATA BRANCH DIFF`（**行级**，增/改/删） | `diff`（**对象/整文件级**） |
| 合并 | merge | `DATA BRANCH MERGE … WHEN CONFLICT FAIL/SKIP/ACCEPT`（**行级三方**） | `merge`（对象级三方） |
| cherry-pick | cherry-pick | `DATA BRANCH PICK … KEYS(…)`（**按主键摘行**） | 无（只能合整分支） |
| 回滚 | revert/reset | `RESTORE … FROM SNAPSHOT`（覆盖式） | `revert`（反向提交、留全历史） |
| 版本上计算 | —— | **原生 SQL**（聚合/JOIN/向量） | 需外部引擎（Spark/DuckDB） |
| 数据类型 | —— | 结构化表（+ `datalink` 引用文件、`vecf32` 向量） | 任意 blob（字节级、内容寻址） |
| 部署 | —— | 一套数据库，零额外组件 | server + 元数据 KV + 对象存储 |

> 一句话：**结构化数据上 git4data 粒度更细（行级）且自带计算；非结构化字节与整仓库原子
> 是 lakeFS 的主场。** 二者互补而非互斥。

---

## 3. 环境准备

```bash
pip3 install -r requirements.txt   # scikit-learn / numpy / pymysql / lakefs / boto3 (+duckdb 选装)
```

**MatrixOne 连接**（多数实验只需这个）——新建 `.mo.cnf`（已 gitignore）：
```ini
[client]
host=<your-matrixone-host>
port=6001
user=<account>:<user>:<role>
password=<password>
```
> 所有脚本只在自建的 `ml_git4data_demo` / `mld_*` 库里操作、用完即删，**不碰其它库**。

**lakeFS（仅对照 demo 与少数实验需要）**——需 Docker + 对象存储（S3/OSS/MinIO）：
```bash
cp .lakefs.env.example .lakefs.env   # 填入 OSS 桶/endpoint/AK/SK（已 gitignore）
bash lakefs_demo/start_lakefs.sh     # 本地起 lakeFS server，blockstore 指向 OSS，监听 127.0.0.1:8200
```

---

## 4. 教程路径（按顺序学）

每节都给出 **学什么 / 怎么跑 / 会看到什么**。建议依次跑。

### 第 0 课 — 跑通两套对照 demo
**学什么**：感受同一套 ML 剧情在 git4data 与 lakeFS 上的「同与不同」。
```bash
python3 -m matrixone.run_demo     # MatrixOne git4data（开箱即用，直连）
python3 -m lakefs_demo.run_demo   # lakeFS（需先 start_lakefs.sh）
```
**会看到**：五幕——持续流入+增量学习、版本 diff、复现历史模型、坏批次回滚、分支清洗实验+合并。
两边精度一致：`0.977→0.989`、复现 `==True`、投毒 `0.886→恢复 0.989`、清洗 `0.989→0.9955`。
差异在动词：MatrixOne 用 `SNAPSHOT/DATA BRANCH DIFF/RESTORE/MERGE/PICK`，lakeFS 用
`commit/diff/revert/merge`。

### 第 1 课 — 大数据量下为何依然「便宜」
```bash
python3 -m experiments.exp_scale
```
**会看到**：1 万→100 万行，`snapshot/branch/clone/restore` 始终 ~30ms（元数据/copy-on-write、
与数据量无关），`DIFF` 只随**变更量**走。⇒ 版本控制的成本不随数据规模膨胀。

### 第 2 课 — 持续学习只训练「增量」
```bash
python3 -m experiments.exp_incremental_diff
```
**会看到**：用 `DATA BRANCH DIFF live AGAINST 上次训练快照` 取出**恰好变更的行**喂
`partial_fit`；每轮处理量恒为 ~1k，6 轮共 6012 行 vs 全量重训 21000 行（差距随轮数二次增长）。

### 第 3 课 — 真实 ML-Ops 工作流
```bash
python3 -m experiments.exp_continuous_annotation  # 持续标注：训练快照 + DIFF 算两次训练差异 + PITR 恢复"周三3点"
python3 -m experiments.exp_stream_versioning      # 实时流：每条 INSERT 一个事务版本，PITR 重建任意微秒
python3 -m experiments.exp_write_audit_publish    # WAP：写暂存分支→SQL 门禁审计→原子 MERGE 发布
python3 -m experiments.exp_concurrent_merge       # 多标注员并发分支合并冲突 FAIL/SKIP/ACCEPT
python3 -m experiments.exp_sft_curation           # SFT 策展：去重/过滤/去污染原地 SQL + 行级溯源
python3 -m experiments.exp_rlhf_preference        # RLHF：SQL 算标注共识 + cherry-pick 改判 + pin 快照
python3 -m experiments.exp_feature_store          # Feature Store：PIT as-of join + git4data 特征版本化（对比 Tecton）
```
**会看到**：每个脚本对应一个真实诉求，并打印关键数字（详见[第 5 节](#5-做了哪些测试实验全表)）。

### 第 4 课 — 边界与诚实对比
```bash
python3 -m experiments.exp_branch_advanced    # schema 演进会打断 diff/merge；库级快照/恢复多表原子
python3 -m experiments.exp_stage_datalink     # datalink 编目 OSS 文件；快照只版本"引用"非"字节"；DIFF OUTPUT FILE 导出 SQL 补丁（需 OSS）
python3 -m experiments.exp_multimodal_catalog # 多模态：datalink + vecf32 近重检测，目录侧版本化（需 OSS）
python3 -m experiments.exp_bench_curation     # 正面基准：MatrixOne 原地 SQL vs lakeFS+DuckDB（需 OSS+duckdb）
python3 -m experiments.exp_clickhouse_vs_matrixone  # 对比 ClickHouse 作 trace 后端：摄入/OLAP vs 版本化/可变更/联接（需 chdb）
python3 -m experiments.exp_neon_branching  # 对标 Neon DB 分支 / 对比 Supabase BaaS（差多少）
```
**会看到**：git4data 在**非结构化字节**上只版本「引用」不版本「字节」；性能基准上 lakeFS+DuckDB
裸吞吐反而更快——**优势在集成度而非速度**。作 OTel trace 后端时 **ClickHouse 在摄入/OLAP 函数/
TTL/生态上完胜**，MatrixOne 的差异点是同一份 trace **可版本化(snapshot/DIFF/PITR) + 行级可变更
(标注/打分) + 可联接**到数据集/模型表。**诚实地承认边界**正是这几课的目的。

### 第 5 课 — 组合使用：lakeFS + MatrixOne
```bash
python3 -m experiments.exp_integration_poc       # 集成原理（最小版，需 OSS + lakeFS）
python3 -m experiments.exp_multimodal_pipeline   # 端到端持续迭代流水线（capstone，需 OSS + lakeFS）
```
**集成原理（`exp_integration_poc`）**：lakeFS 提交图像字节(C1)→ MatrixOne 目录 pin C1 → 快照
`dataset_v1`；字节改变→lakeFS C2 → 目录 pin C2 → `dataset_v2`。**把目录解析到 v1 读回旧字节、
解析到 v2 读回新字节**——字节级时间旅行由「lakeFS 存字节 + MatrixOne 目录 pin commit」组合
达成（两者单独都做不到）。

**端到端流水线（`exp_multimodal_pipeline`，capstone）**：把上面原理跑成一条**持续迭代**的
多模态「版本化 + 打标 + 训练」流水线，4 轮迭代：
- R1 原始数据落 lakeFS(commit) → MatrixOne 编目+打标 → 快照=数据集版本 → 训练+注册模型；
- R2 新数据持续流入 → 新 commit，`DATA BRANCH DIFF` 显示新增了哪些资产；
- R3 在分支上**修正噪声标签** → diff → merge（acc 0.868→0.904）；
- R4 某资产**原始字节重导出** → 新 lakeFS commit → 目录重新 pin。
**会看到**：每个模型版本都 pin 住（catalog 快照 + lakeFS commit）；可**精确复现**任一历史模型；
asset#5 在 v2/v4 解析到不同 lakeFS commit 读回**不同字节**；最后打印完整血缘谱系
（model → catalog 快照 → lakeFS commit → acc）。

### 第 6 课 — 非 ML / Agent 基础设施：branching 进化 + OpenTelemetry trace 接入
```bash
python3 -m experiments.exp_agent_evolution    # 用 branching 让 agent 进化
python3 -m experiments.exp_otel_agent_trace   # 把 OTel agent trace 接进 MatrixOne（需 opentelemetry-sdk）
```
**进化（`exp_agent_evolution`）**：把 agent 的 trace + 可演化「大脑」存成版本化表；branch→从失败
trace 学习→对比→`MERGE` 赢家，成功率 `50%→70%→90%→100%`；坏变异用 `RESTORE` 回滚；并行探索
分支冲突用 `WHEN CONFLICT` 裁决；`PICK` 只提拔验证过的技能。⇒ git4data 天然适配「探索-择优-合并-回滚」。

**OTel 接入（`exp_otel_agent_trace`）**：用**真实 opentelemetry SDK + 自定义 `SpanExporter`** 把
agent 运行的 span 树（`invoke_agent` 根 + `chat {model}`/`execute_tool` 子 span，`gen_ai.*` 属性）
写进 MatrixOne；SQL 重建 trace 树、聚合 token/延迟、定位 error span；再叠加 git4data：快照=「某 agent
版本的 trace」，`DATA BRANCH DIFF` 比新增 span，SQL 做版本 A/B（v1 gpt-4o 1025 tok/2 err vs
v2 gpt-4o-mini 962/1）。⇒ **同一存储既做可观测性后端、又做版本化的 agent 迭代基质**。

**真实 agent 接入（`agent_otel/`）**：一个**真正会跑的 agent**——真实的多步工具调用循环
（`calculator`/`kb_lookup` 真实执行）+ 真实 OTel instrumentation，span 经真实 `SpanExporter`
落 MatrixOne。LLM **可插拔**：设了 `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` 就用真实 Claude/OpenAI
（tool-use 协议），没有就用确定性本地规划器（无 key 也能跑）。
```bash
python3 -m agent_otel.run                           # 本地规划器（开箱即用）
DEEPSEEK_API_KEY=sk-... python3 -m agent_otel.run   # 真实 DeepSeek（OpenAI 兼容）
ANTHROPIC_API_KEY=sk-... python3 -m agent_otel.run  # 真实 Claude
OPENAI_API_KEY=sk-... python3 -m agent_otel.run     # 真实 OpenAI
```
**会看到**：agent 真实算出 `47*19=893`、`法国人口×2=134000000`、`Hamlet→Shakespeare`、
`10% 光速=29979.2`、`Atlantis→工具报错(error span)`；真实 span 入库；再用 SQL 重建 trace
树、按 trace 聚合 token、统计工具调用频次、定位 error span。**实测用真实 DeepSeek 跑过**：
真模型自主决定工具调用、real token 用量入库；甚至能看到模型用了略不对的实体导致 `kb_lookup`
报错（被记成 OTel error span，SQL 可定位）——正是 trace 后端的价值。

---

## 5. 做了哪些测试（实验全表）

> 全部实跑于 **MatrixOne v3.0.11**，每个脚本**自建库 → 跑 → 清理**，可独立重复运行。
> 每个实验的**设计理由 / 测试点 / MatrixOne 能力价值**逐条说明见 **[EXPERIMENTS.md](EXPERIMENTS.md)**；
> 实测到的 **MatrixOne 缺陷/限制汇总**见 **[LIMITATIONS.md](LIMITATIONS.md)**（诚实清单）。

| 脚本 | 验证的能力 | 关键实测结果 | 依赖 |
|---|---|---|---|
| `matrixone/run_demo.py` | git4data 五幕全流程 | snapshot/time-travel/restore + 原生 DATA BRANCH diff/merge/pick | MatrixOne |
| `lakefs_demo/run_demo.py` | lakeFS 同剧情对照 | commit/diff/revert/merge；精度与 MatrixOne 逐位一致 | lakeFS+OSS |
| `exp_scale.py` | 大规模零拷贝 | 1万→100万行 snapshot/branch/clone/restore ~30ms 恒定；diff ∝ 变更量 | MatrixOne |
| `exp_incremental_diff.py` | 增量训练 | 每轮只训 ~1k delta；6 轮 6012 vs 全量 21000 行 | MatrixOne |
| `exp_concurrent_merge.py` | 行级合并冲突 | FAIL 检出 pk 冲突，SKIP/ACCEPT 各自裁决 | MatrixOne |
| `exp_sft_curation.py` | SFT 策展 | 原地 SQL 8000→3164；DIFF 溯源 DELETED=4836；410ms | MatrixOne |
| `exp_rlhf_preference.py` | RLHF 偏好数据 | SQL 共识（一致 2174/争议 826）；cherry-pick 改判；pin 快照 | MatrixOne |
| `exp_feature_store.py` | **Feature Store**（对比 Tecton） | 无泄漏 PIT as-of join；HTAP 单存储 online+offline；git4data 版本化特征值（7d→14d 改 120 行 DIFF、快照=特征发布、PITR） | MatrixOne |
| `exp_write_audit_publish.py` | WAP 模式 | 审计抓出 15/15/10，生产零暴露；修复后原子 MERGE 发布 1300 | MatrixOne |
| `exp_continuous_annotation.py` | 持续标注 + 复现 | DIFF 两次训练 INSERTED 300/UPDATED 80；PITR 还原未打标的"周三3点" | MatrixOne |
| `exp_stream_versioning.py` | 实时流版本化 | 500 事件=500 事务；PITR 重建到任意微秒（201/381 精确） | MatrixOne |
| `exp_multimodal_catalog.py` | 多模态目录 | datalink 编目 + `vecf32` 近重(dist 0.02)；git4data 版本化目录 | MatrixOne+OSS |
| `exp_branch_advanced.py` | 边界 | schema 演进打断 DIFF/MERGE；库级快照/恢复多表原子一致 | MatrixOne |
| `exp_stage_datalink.py` | 非结构化引用 | stage+datalink+`load_file`；**只版本引用非字节**；DIFF OUTPUT FILE→SQL 补丁 | MatrixOne+OSS |
| `exp_bench_curation.py` | 正面性能基准 | 同份策展：lakeFS+DuckDB 更快（225/383ms vs 585/1961ms）——MO 强在集成非速度 | MatrixOne+OSS+DuckDB |
| `exp_integration_poc.py` | lakeFS×MatrixOne 集成 | 目录 pin lakeFS commit → 字节级时间旅行 + 行级"改了啥" + 可直读 URL | lakeFS+MatrixOne+OSS |
| `exp_multimodal_pipeline.py` | **端到端 capstone**：多模态版本化+打标+训练+持续迭代 | 4 轮：落 lakeFS→编目打标→训练→注册；新数据/清洗标签/重导出字节各自版本化；m2 精确复现；字节级血缘 | lakeFS+MatrixOne+OSS |
| `exp_agent_evolution.py` | 非 ML：Agent 进化 | branch 进化 50%→100%；RESTORE 回滚；冲突裁决；cherry-pick 技能 | MatrixOne |
| `exp_otel_agent_trace.py` | OTel agent trace 后端 | 真实 OTel SpanExporter 写入 gen_ai.* span；SQL 重建 trace 树/聚合/找 error；快照=agent 版本，DIFF + SQL A/B（v1 vs v2 token/err） | MatrixOne+otel-sdk |
| `exp_clickhouse_vs_matrixone.py` | **对比 ClickHouse**（trace 后端） | 同份 span 两边跑：摄入 28ms vs 112ms(远程)、查询 44 vs 52ms；CH 有 quantile、MO 无；MO 独有 snapshot/DIFF/PITR + 行级 UPDATE 标注 + JOIN 模型注册表算成本 | MatrixOne+chdb |

---

## 6. 典型用法建议（可直接抄的配方）

### 配方 1 · 训练数据集版本化 + 复现 + 「两次训练差多少」
```sql
CREATE SNAPSHOT run_20260524 FOR TABLE db dataset;                 -- 每次训练前 pin 一版
SELECT ... FROM db.dataset {snapshot='run_20260524'};              -- 复现：读那一版重训
DATA BRANCH DIFF db.dataset {snapshot='run_B'}
           AGAINST db.dataset {snapshot='run_A'} OUTPUT SUMMARY;   -- 增/改/删行级差异
```

### 配方 2 · 分支做数据实验（清洗/重标/混合）→ 择优合并
```sql
DATA BRANCH CREATE TABLE db.exp FROM db.dataset;                   -- 隔离分支，不污染主线
-- 在 db.exp 上用 SQL 清洗 / 重标 / 调配比 ...
DATA BRANCH DIFF db.exp AGAINST db.dataset OUTPUT SUMMARY;         -- 看改了什么
DATA BRANCH MERGE db.exp INTO db.dataset WHEN CONFLICT ACCEPT;     -- 赢了就合并
DATA BRANCH PICK  db.exp INTO db.dataset KEYS(1,2,3);              -- 或只摘验证过的行
```

### 配方 3 · Write-Audit-Publish（生产零暴露）
```sql
DATA BRANCH CREATE TABLE db.staging FROM db.prod;                  -- Write
-- Audit：在 staging 上跑质量门禁（空值/标签合法/类别均衡/去重/去 eval 污染）
DATA BRANCH MERGE db.staging INTO db.prod WHEN CONFLICT FAIL;      -- 审计通过才 Publish（原子）
```

### 配方 4 · 持续/增量学习：只处理 delta
```sql
DATA BRANCH DIFF db.dataset AGAINST db.dataset {snapshot='last_trained'};
-- 把这批 INSERT/UPDATE 行喂给 partial_fit；成本 ∝ 变更量而非数据集大小
```

### 配方 5 · 坏数据回滚 / 任意时间点恢复
```sql
RESTORE ACCOUNT <acct> DATABASE db TABLE t FROM SNAPSHOT good_snap;-- 回到某快照
CREATE PITR p FOR DATABASE db RANGE 7 'd';                         -- 开 PITR（7 天）
RESTORE DATABASE db TABLE t FROM PITR p "2026-05-24 15:00:00";     -- 回到任意时刻
```

### 配方 6 · 组合平台：lakeFS 管字节，MatrixOne 管目录
- 原始字节（图像/视频/语料/权重）→ lakeFS 提交版本化；
- MatrixOne 目录表存 `lakefs_commit` + 标签 + 划分 + `embedding(vecf32)`，`CREATE SNAPSHOT` 即「数据集版本」；
- 解析某数据集版本时，按目录里的 commit 从 lakeFS 取**确切字节**——端到端血缘、字节级可复现。
- 完整代码见 `experiments/exp_integration_poc.py`，架构论证见 [COMPARISON.md](COMPARISON.md) §8/§11。

> **怎么选**：结构化训练数据/持续学习/审计发布/复现 → MatrixOne git4data；海量非结构化字节
> 的内容级版本 / 整仓库原子 / 多引擎直读 → lakeFS；大多数真实平台 → 两者组合。

---

## 7. 仓库结构

```
common/          共享：合成数据流(data_stream) + 增量模型(model, SGDClassifier.partial_fit)
config.py        领域常量 + 从 .mo.cnf 读 MatrixOne 连接
matrixone/       git4data 实现：mo_client / git4data(原语封装) / repo / run_demo
lakefs_demo/     lakeFS 实现：lk_config / start_lakefs.sh / run_demo
agent_otel/      真实 agent 接入：tools / llm(可插拔) / agent(OTel 埋点) / exporter(→MatrixOne) / run
durable_exec/    DBOS 式 durable execution on MatrixOne：framework(@workflow/@transaction/@step)+app(完整 mini-DBOS)
                 / run(崩溃恢复+exactly-once) / scheduling(CREATE TASK 定时器 + FOR UPDATE 队列)。python -m durable_exec.app
experiments/     18 个可独立重跑、自清理的实验脚本（见第 5 节）
LIMITATIONS.md   MatrixOne 实测缺陷/限制汇总（诚实清单）
COMPARISON.md    深度报告（§1-12：能力对比/ML场景/性能基准/集成/平台架构/Agent进化）
README.md        本教程
```

---

## 8. 安全与复现说明

- **密钥不入库**：`.mo.cnf`、`.lakefs.env` 含明文凭证，已在 `.gitignore` 排除；仓库只提供
  `.lakefs.env.example` 模板。clone 后请自建这两个文件。
- **可复现 & 自清理**：每个脚本都是确定性的（数据由固定种子/索引生成），并在结束时删除自建
  的库/快照/PITR/stage 及 OSS 前缀，可反复运行、互不残留。
- **lakeFS 实跑踩过的坑**（已在脚本里处理）：端口走 `127.0.0.1:8200` 避免 IPv4/IPv6 冲突；
  OSS 需虚拟主机风格寻址；boto3 批量删除需 Content-MD5（改单删）；AccessKey 须启用。详见
  [COMPARISON.md](COMPARISON.md) §3。
