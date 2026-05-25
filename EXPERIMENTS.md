# 实验设计说明：每个实验「为什么做 / 测什么 / MatrixOne 的能力价值」

> 配套 [README.md](README.md)（教程）与 [COMPARISON.md](COMPARISON.md)（深度报告）。
> 每个实验都给三件事：**设计理由**（针对什么真实问题）、**测试点**（验证哪条能力/论断）、
> **MatrixOne 能力价值**（这条能力让你能做什么、相对替代方案的价值）。

---

## A. 核心原语对照（2 个 demo）

### `matrixone/run_demo.py` — git4data 五幕全流程
- **设计理由**：先在一个连贯的 ML 持续学习剧情里把 git4data 的全部核心原语串起来，作为后续所有实验的认知底座。
- **测试点**：每批 `CREATE SNAPSHOT`、time-travel 复现、`DATA BRANCH DIFF`(行级)、`RESTORE` 回滚、`DATA BRANCH CREATE/PICK/MERGE`(分支清洗+合并) 能否在真实流程里跑通。
- **能力价值**：证明 git4data 是**一套完整的 git 语义**（不是零散功能）；且 diff/merge 行级、带 cherry-pick——对结构化训练数据比文件级更精细。

### `lakefs_demo/run_demo.py` — lakeFS 同剧情对照
- **设计理由**：同一套数据/模型/剧情在 lakeFS 上跑，保证对比是 apples-to-apples，差异只在「版本控制动词」。
- **测试点**：lakeFS `commit/tag/diff/revert/merge` 能否完成同样五幕；精度是否与 MatrixOne 逐位一致（验证两边训练等价）。
- **能力价值（对照得出）**：lakeFS 的 diff/merge 是**对象/文件级、作用于整仓库**，反衬出 MatrixOne 的**行级粒度 + 版本上可直接 SQL**。

---

## B. 规模与增量

### `exp_scale.py` — 大数据量下是否依然便宜
- **设计理由**：版本控制要上生产，第一个疑虑是「数据大了还便宜吗」。
- **测试点**：1 万→100 万行下 snapshot/clone/branch/restore 的耗时是否随数据量增长；diff 成本跟什么走。
- **能力价值**：这些都是 **copy-on-write 元数据操作，~30ms 恒定、与数据量无关** → 可以高频打快照（每次训练/每个实验都 pin 一版）而几乎零成本；diff 只随**变更量**走。

### `exp_incremental_diff.py` — 持续学习只训练增量
- **设计理由**：持续学习里数据只增量变化，每轮全量重训是 O(K·N) 的浪费。
- **测试点**：能否用 `DATA BRANCH DIFF` 取「自上次训练快照以来变更的行」，只对 delta 训练；成本是否 ∝ 变更量。
- **能力价值**：**行级 DIFF 让「只处理增量」精确可行**——每轮成本 ∝ 变更量而非数据集大小（6 轮 6012 vs 全量 21000 行，差距随轮数二次增长）。数据版本控制直接转化为训练成本节省。

---

## C. 真实 ML-Ops 工作流

### `exp_continuous_annotation.py` — 多人持续标注 + 周期训练
- **设计理由**：标注长期累积、训练周期性；要说清「这次训练用的是哪一刻的数据」「两次训练差多少」。
- **测试点**：每次训练 pin 快照 + `DATA BRANCH DIFF` 算两次训练差异 + `PITR` 恢复一个**没打快照**的时刻。
- **能力价值**：DIFF 精确回答「增 300/改 80」；**PITR 能恢复到任意时间点**（连「周三 3 点」这种没显式打标的时刻也行）——lakeFS 只能回到离散 commit。

### `exp_stream_versioning.py` — 实时流（Kafka→训练集）版本化
- **设计理由**：实时流场景里 lakeFS 的批量 commit 模型做不到「每事务一版本」。
- **测试点**：逐条 INSERT 是否每条都是一个持久事务版本；PITR 能否重建「截至任意微秒」的训练集。
- **能力价值**：**数据库天生每事务持久 + PITR 任意时间点 = 等价「每事务一个可恢复版本」**，无需在流路径里插批量 commit（实测重建到 201/381 精确）。这是 MatrixOne 相对 lakeFS 最独特的优势之一。

### `exp_write_audit_publish.py` — Write-Audit-Publish
- **设计理由**：WAP 是 MLOps 行业标准——生产永远不暴露未审计数据。
- **测试点**：Write(分支)→Audit(分支上 SQL 质量门禁)→Publish(原子合并) 闭环；审计期间生产是否零暴露。
- **能力价值**：**分支隔离 + 原地 SQL 审计 + 行级原子合并 = 在数据库里实现 WAP**，无需外部编排；审计逻辑就是 SQL，门禁随手可加（实测抓出 15/15/10，生产零暴露后再发布）。

### `exp_concurrent_merge.py` — 并发标注分支合并冲突
- **设计理由**：多人/多管道并行打数据，难免改到同一行，必须能检测冲突而非静默丢失。
- **测试点**：两分支改同一主键，`MERGE WHEN CONFLICT FAIL/SKIP/ACCEPT` 的语义是否正确。
- **能力价值**：**行级三方合并 + 冲突策略** → 支持「分支式数据协作 + 冲突裁决」，这是 lakeFS 文件级合并给不了的细粒度（多标注员协作刚需）。

### `exp_sft_curation.py` — SFT 数据策展
- **设计理由**：SFT 策展（去重/质量过滤/去 eval 污染/配比）本质是集合运算，且要可复现、可溯源。
- **测试点**：整条策展能否在版本化表上原地 SQL 完成；`DATA BRANCH DIFF` 能否给出「砍了哪些」的行级溯源。
- **能力价值**：**「计算 + 版本」一处**——去重/过滤/去污染一条 SQL，结果即一个可复现版本，diff 能回答「4836 条分别因何被删」。lakeFS 要外部引擎且只有文件级 diff。

### `exp_rlhf_preference.py` — RLHF 偏好数据治理
- **设计理由**：RLHF 偏好数据要算标注一致性/共识、剔除争议、按 reward-model 运行固定版本。
- **测试点**：SQL 聚合算共识、`WHERE` 筛争议、`PICK` 改判、按训练运行 pin 快照能否串成闭环。
- **能力价值**：**标注一致性是 SQL 聚合、争议是一个 WHERE、改判是 cherry-pick、每次训练 pin 快照可复现**——把偏好数据治理变成库内可版本化工作流。

### `exp_feature_store.py` — Feature Store 后端 + git4data 特征版本化（对比 Tecton）
- **设计理由**：feature store 是 ML 平台标配；看 MatrixOne 能否当其后端，并对比事实标准 Tecton。
- **测试点**：① point-in-time 正确的 as-of join（核心，防泄漏）；② 滚动特征物化；③ online(最新值点查)+offline(全历史) 同一 HTAP 表；④ 分支改特征定义(7d→14d)行级 `DIFF`/`MERGE`、快照=特征发布、PITR。
- **能力价值**：PIT join 就是一段 SQL（实测无泄漏）；**HTAP 单存储消除 Tecton 双存储(offline S3 + online DynamoDB)的一致性难题**；**git4data 版本化*物化特征值*——Tecton（只版本化特征*定义*）在数据层没有的**：每次训练 pin 一版可复现、分支做特征工程实验并行级 diff(实测 7d→14d 改 120 行)、PITR 回到任意特征状态。边界：MatrixOne 是数据库非特征平台，缺声明式特征框架/托管物化编排/在线 SLA——最佳是「MO 当后端 + 薄特征层」。

---

## D. 边界与诚实对比

### `exp_branch_advanced.py` — schema 演进 & 库级原子
- **设计理由**：摸清 `DATA BRANCH` 的两个边界。
- **测试点**：分支上 `ALTER ADD COLUMN` 后 diff/merge 是否还能用；库级 snapshot/restore 是否多表原子一致。
- **能力价值**：诚实标注限制（**schema 必须一致、diff/merge 是按表的**）；同时确认**库级快照/恢复能把 features+labels 多表一起原子版本化**（覆盖「整训练集一个一致版本」）。

### `exp_stage_datalink.py` — 非结构化文件：版本化「引用」还是「字节」
- **设计理由**：搞清 git4data 对外部文件到底版本化引用还是字节——这决定多模态怎么落地。
- **测试点**：快照后覆盖 OSS 文件，time-travel 读到旧字节还是新字节；`DATA BRANCH DIFF ... OUTPUT FILE` 能否导出可重放 SQL 补丁。
- **能力价值**：明确「**版本化的是 datalink 引用、不是字节**」——指引正确架构（字节交 lakeFS）；`OUTPUT FILE` 把变更集导成事务 SQL 补丁，便于跨环境同步数据集 delta（lakeFS 无此形态）。

### `exp_multimodal_catalog.py` — 多模态目录侧能力
- **设计理由**：探边界——多模态非结构化文件 MatrixOne 能管到什么程度。
- **测试点**：stage+datalink 编目 OSS 文件、`load_file` 读内容、`vecf32` 向量近重检测、git4data 版本化目录。
- **能力价值**：能把原始文件**编目 + 引用 + 语义检索**进结构化数据集，并版本化目录/标签/embedding；但字节本体不被版本化——明确了能力边界与该配合 lakeFS 的点。

### `exp_bench_curation.py` — 正面性能基准（诚实）
- **设计理由**：避免自卖自夸，正面量化 MatrixOne 原地策展 vs lakeFS+DuckDB 的真实性能。
- **测试点**：同一份 SFT 策展，两种架构端到端耗时（warmup + 中位数）。
- **能力价值（诚实）**：裸吞吐 **lakeFS+DuckDB 反而更快**——所以 MatrixOne 的价值**不是算得快**，而是「**存+版本+计算+服务一处、行级语义、可复现、少运维**」。这条让选型理由站得住、不虚。

### `exp_clickhouse_vs_matrixone.py` — 对比 ClickHouse 作 agent/OTel trace 后端
- **设计理由**：ClickHouse 是 OTel trace 存储的事实标准（OTel Collector 一等 exporter、SigNoz/Uptrace 都用它），所以是 agent-trace 场景最该对比的基线。看 MatrixOne 在这个场景的关键点上有没有优势。
- **测试点**：同一份 OTel span 两边都建表/摄入/跑相同可观测性查询（按模型聚合 token、error 数、延迟）；再对照关键能力点——版本化(snapshot/DIFF/PITR)、行级可变更(给 span 打 eval 标签)、JOIN 到模型注册表。ClickHouse 用进程内 chdb，MatrixOne 远程（带网络延迟说明）。
- **能力价值（诚实）**：**ClickHouse 完胜纯 trace 存储**——摄入吞吐(28ms vs 112ms 远程)、OLAP 函数(有 `quantile`，MO 无)、原生 TTL、成熟生态。**MatrixOne 在 agent-迭代角度的差异点**：同一份 trace 还能 **git4data 版本化**（snapshot per agent 版本、`DATA BRANCH DIFF` v2 vs v1=1000、PITR）、**行级可变更**（普通 `UPDATE` 给 144 个 error span 打标，26ms 事务级；ClickHouse 是后台 mutation 重操作）、**可 JOIN**（spans ⋈ model_registry 算每模型成本）。结论：高吞吐实时监控用 ClickHouse；要把 trace 当**可版本化/可标注/可联接训练数据**的迭代资产，MatrixOne 有独特价值——务实做法是两者组合。

---

## E. 集成与端到端

### `exp_integration_poc.py` — lakeFS × MatrixOne 集成原理
- **设计理由**：既然字节交 lakeFS、结构化交 MatrixOne，验证「两层如何拼起来拿到字节级版本」。
- **测试点**：MatrixOne 目录 pin lakeFS commit，解析到不同数据集版本能否读回不同字节；DIFF 能否指出变更的资产。
- **能力价值**：**MatrixOne 快照固化「目录 + 对应 lakeFS commit」** → 与 lakeFS 组合达成单方做不到的**字节级时间旅行 + 行级「改了啥」+ 可直读 URL**，血缘贯通。

### `exp_multimodal_pipeline.py` — 端到端持续迭代流水线（capstone）
- **设计理由**：把集成原理跑成一条真实、持续迭代的端到端流水线（多模态版本化 + 打标 + 训练）。
- **测试点**：4 轮迭代（数据流入 / 标签清洗 / 字节重导出）各自在对的层版本化；历史模型可精确复现；同资产不同版本读回不同字节；血缘谱系完整。
- **能力价值**：MatrixOne 作为「**结构化大脑**」——目录/标签/embedding/数据集版本/模型注册全在库内可版本化、可 SQL、可复现，并以 lakeFS commit 串起字节血缘；证明它是数据平台流水线的可靠中枢。

---

## F. 非 ML / Agent 基础设施

### `exp_agent_evolution.py` — Agent trace + branching 进化
- **设计理由**：验证 git4data 的价值不限于 ML——「agent trace + branching 进化」这种 git 式范式是否成立。
- **测试点**：branch→从失败 trace 学习→对比→`MERGE` 的进化循环、坏变异 `RESTORE` 回滚、并行探索冲突、`PICK` 技能。
- **能力价值**：**行级 branch/diff/merge/cherry-pick + 快照/PITR 天然映射「探索-择优-合并-回滚 + 谁学了什么可追溯/可复现/可审计」**——把 agent 自我进化变成可治理的版本控制工作流。

### `exp_otel_agent_trace.py` — OpenTelemetry agent trace 接入 MatrixOne
- **设计理由**：OTel 是 agent/LLM trace 接入的事实标准（GenAI semantic conventions + OTLP + Collector，后端常落 ClickHouse）。验证 MatrixOne 能否当这个 trace 后端，并叠加 git4data。
- **测试点**：用**真实 `opentelemetry.sdk` + 自定义 `SpanExporter`** 把 agent 运行的 span 树（`invoke_agent` 根 + `chat {model}`/`execute_tool` 子 span，`gen_ai.request.model`/`gen_ai.usage.*_tokens` 等属性）写入 MatrixOne；SQL 重建 trace 树、聚合 token/延迟、按 `status='ERROR'` 找失败 span；再叠加 git4data：快照=「某 agent 版本的 trace」、`DATA BRANCH DIFF` 比新增 span、SQL 做版本 A/B。
- **能力价值**：标准 OTel `SpanExporter` 即可把 `gen_ai.*` span 映射进 SQL 表（生产里就是 OTel Collector 的一个 exporter）；trace 树/聚合/错误检索都是普通 SQL；git4data 让**同一存储既是可观测性后端、又是可版本化的 agent 迭代基质**（snapshot per 版本、行级 DIFF、跨版本 cost/error A/B：实测 v1 gpt-4o 1025 tok/2 err vs v2 gpt-4o-mini 962/1）。对照 ClickHouse 式纯 trace 存储，多了「版本化 + 行级 diff/merge」。

### `agent_otel/`（包，`python -m agent_otel.run`）— 真实 agent 接入
- **设计理由**：上面 `exp_otel_agent_trace` 的 span 是模拟生成的；这里要一个**真正会跑的 agent**（真实多步工具调用循环），验证「真实 agent → OTel → MatrixOne」整条链路。
- **测试点**：真实 agent loop（`invoke_agent`→反复 `chat`/`execute_tool`→final）；工具 `calculator`/`kb_lookup` **真实执行**；LLM **可插拔**（`ANTHROPIC_API_KEY`/`OPENAI_API_KEY` 走真实 Claude/OpenAI tool-use，否则确定性本地规划器）；真实 OTel `TracerProvider`+自定义 `SpanExporter`→MatrixOne；再用 SQL 重建 trace 树/聚合 token/工具频次/error span + git4data 快照。
- **能力价值**：证明 MatrixOne 能直接接**真实 agent 运行时**产生的 OTel trace（不是手工造的 span）：实测 agent 真算出 `47*19=893`、`法国人口×2=134000000`、`Hamlet→Shakespeare`、`10%光速=29979.2`、`Atlantis→工具报错`，24 span 入库后全程 SQL 可观测；同一存储再叠加 git4data 版本化。生产形态 = agent SDK→OTLP→OTel Collector(带 MatrixOne exporter)，本包把那个 exporter 内联了。

---

## G. 其它后端场景

### `durable_exec/`（包，`python -m durable_exec.run`）— MatrixOne 作 durable execution engine
- **设计理由**：设想 MatrixOne 当 Temporal / DBOS 这类 **durable execution** 产品的后端——把工作流当作可崩溃恢复、每步 exactly-once 的持久程序。验证它能否担此角色。
- **测试点**：DBOS 式引擎——`wf_exec`+`wf_step` 表；**每步的业务副作用与 checkpoint 同一 ACID 事务提交**；同 wf_id 重入时已完成步靠主键跳过（exactly-once）；retry 带 attempts；`recoverable()`=SQL 查 RUNNING；执行日志 SQL 可观测。订单工作流在 charge 后崩溃 → 重入恢复。
- **能力价值**：实测——崩溃后重入，reserve/charge **skipped**、inventory 仍 9、payments 仍 1、status COMPLETED → **副作用恰好一次**（EXACTLY-ONCE: True）；retry 第 2 次成功、attempts=2。**这是与 trace 监控相反的工作负载**：需要 ACID 事务 + 持久可查状态 + 主键幂等，正是数据库/ MatrixOne 的强项。DBOS 模型 MatrixOne 完全能担；Temporal 的 timers/signals/replay 等需上层引擎。git4data 可对执行日志快照/版本化做可审计回放。

### `durable_exec/framework.py` + `app.py`（`python -m durable_exec.app`）— 拼成一个 mini-DBOS
- **设计理由**：把「持久 exactly-once 步骤 + durable timer(CREATE TASK) + durable queue(FOR UPDATE)」拼成**一个装饰器框架**，验证「DBOS-on-MatrixOne」端到端可行。
- **测试点**：`@workflow`/`@transaction`(业务写+checkpoint 同事务)/`@step`(外部副作用)、`dbos.sleep()` durable timer、`enqueue()`+`worker()` 队列派发、崩溃 requeue 重入跳过已完成步。3 订单 + 2 并发 worker，order-2 在 charge 后崩溃。
- **能力价值**：实测 **ALL EXACTLY-ONCE & COMPLETE: True**——payments=3（崩溃的也只扣一次）/inventory=96/emails=3/timers FIRED=3；崩溃工作流 attempt 2 跳过已 checkpoint 的 reserve/charge。⇒ **~300 行就在 MatrixOne 上覆盖了 DBOS 核心**（编程模型+持久步骤+timer+queue+恢复），`@transaction` 与业务写同库同事务，还能叠 git4data 时间旅行。仍缺确定性重放/signals/child-wf/版本化/可视化/多语言 SDK——见 COMPARISON §17/§17.1。

### `durable_exec/scheduling.py`（`python -m durable_exec.scheduling`）— 用 CREATE TASK + FOR UPDATE 自建 durable timer/queue
- **设计理由**：durable execution 还需要两大原语——**定时器**（睡到某刻再继续）和**队列**（持久、恰好处理一次）。验证它们能否用 MatrixOne 内置 `CREATE TASK`（v3.0.11 实测可用）+ `FOR UPDATE` 行锁**在库内原生自建**，无需外部调度器/broker。
- **测试点**：① durable timer——`timers` 表 + `CREATE TASK` cron 轮询 `UPDATE … FIRED WHERE due_at<=now()`；② durable queue——2 个并发线程 worker 用 `SELECT … FOR UPDATE` claim（`SKIP LOCKED` 不支持→锁上串行）+ `UPDATE` 标 DONE。
- **能力价值**：实测——到期 timer 被库内调度器**按时 FIRED**、未到期保持 PENDING（`SHOW TASK RUNS` 见 SUCCESS），**无外部 poller**；12 条消息被 2 worker **恰好各处理一次**（各 6 条，DONE=12，EXACTLY-ONCE: True）。⇒ **durable execution 三大原语（持久步骤 exactly-once + durable timer + durable queue）都能在 MatrixOne 原生自建**，是「DBOS-on-MatrixOne」的可用底座。坑：`CREATE TASK` 的 `BEGIN…END` 体内分号在标准 CLI 会被截断，需 DELIMITER 或驱动整条发送。

### `exp_neon_branching.py`（`python -m experiments.exp_neon_branching`）— 对标 Neon 的 DB 分支 / BaaS 对比
- **设计理由**：Neon 主打即时 CoW 数据库分支（branch-per-PR/CI/dev）+ PITR；Supabase 是 Postgres 之上的完整 BaaS。看 MatrixOne 作"可分支数据库 / BaaS 底座"差多少。
- **测试点**：把 prod 库零拷贝 `CLONE` 出 dev 分支并计时；分支上跑 schema 迁移+数据变更；验证 prod 隔离不受影响；`DATA BRANCH DIFF` 行级看变更；`DROP` 丢弃分支。
- **能力价值**：实测——185ms 即时 CoW 分支（1000+5000 行）、prod 完全隔离、行级 diff 出 +100 行、46ms 丢弃 → **Neon 的 dev/preview 分支工作流在 MatrixOne 上成立，且多了行级 diff/merge**。诚实注脚：改 schema 的表不能行级 diff；`CLONE` 无血缘故 DIFF 认增改不认删（全量含删用 `DATA BRANCH CREATE`）。BaaS 差距见 COMPARISON §18：对标 Neon 差距小（主要差 serverless per-branch endpoint/缩到零），对标 Supabase 差距大（缺 API/Auth/Realtime/Storage/Functions/RLS/SDK 整个应用层；实测 RLS/CDC v3.0.11 无）。

### `exp_robot_memory_3d.py`（`python -m experiments.exp_robot_memory_3d`）— 机器人 3D 空间记忆 + git4data（具身智能）
- **设计理由**：具身智能/机器人的「记忆」来自 IoT 传感器（LiDAR/深度相机/声呐）的连续 3D 点流，落成的标准结构是**体素地图/占据栅格**（OctoMap/TSDF 那一类）。但机器人记忆不是静态的：世界会**漂移**（家具移动）、多机器人探索不同区域需**合并**地图、传感器抖动会注入**幽灵障碍**需**回滚**、还常要问「上周二机器人认为这里有什么」（时间旅行）——这些都是空间数据上的 git 操作。验证 MatrixOne 能否在一个库里同时担「3D 记忆」与「记忆的版本控制」。
- **测试点**：5 幕——① IoT 点流 → `FLOOR(x)/FLOOR(y)/FLOOR(z)` 体素化 + `UPDATE…JOIN` **原地增量融合**（occ 累加，故 DIFF 认 UPDATED 而非删+插）；② 世界漂移：`CREATE SNAPSHOT` v1/v2 + `DATA BRANCH DIFF` 行级看出**哪些体素变了**；③ 在**历史版本上**做 3D 最近邻查询（`{snapshot=}` + 算术距离 kNN，时间旅行×空间查询）；④ 机群：`DATA BRANCH CREATE/DIFF/MERGE` 合并第二台机器人的地图，争用体素 `WHEN CONFLICT FAIL/SKIP`；⑤ `RESTORE … FROM SNAPSHOT` 回滚一批幽灵障碍。外加 **DuckDB 基线**对照。
- **能力价值**：实测——3000 点→300 体素；重访后 DIFF 出 **UPDATED=300（占据漂移）+ INSERTED=50（新发现的角落）**；同一 3D kNN 在 v1 与 live 各自作答（dock 体素 occ 10→14）；机群合并 FAIL 检出争用体素、SKIP 保中央权威值并纳入对方 3 个新走廊体素；幽灵批 353→363 后 RESTORE 回 353。**DuckDB 同样能跑 3D 查询（分析力对等），但没有任何原生版本控制**（"快照"只能 `CREATE TABLE … AS SELECT *` 全量物理拷贝、无 CoW，无时间旅行/行级 DIFF/MERGE 冲突策略/RESTORE/PITR）。诚实边界：MatrixOne **不是 3D 引擎**（无八叉树/光线投射/真空间索引，体素靠 `FLOOR`+算术距离模拟）；它独有的是把 **SQL 体素融合 + git4data（快照/时间旅行、漂移 DIFF、机群 MERGE 带冲突、RESTORE/PITR）压在同一份数据、同一个 ACID 库里**。vs rosbag/MCAP（只录原始流不可查不可合）、OctoMap/TSDF（3D 结构强但无 SQL/无机群合并/无快照管理层）、TSDB（时间聚合强但无空间合并/版本）、PostGIS（真 3D 几何但版本化靠手搓表拷贝/审计触发）、向量库（近邻好但非占据栅格融合+版本）。

## 一句话总览：MatrixOne git4data 的能力价值落在四处

1. **零成本高频版本**（snapshot/clone/branch/restore 与数据量无关）→ 每次训练/实验都能 pin 一版。
2. **行级语义**（DIFF/MERGE/PICK + 冲突策略）→ 增量训练、协作打标、策展溯源、WAP 都比文件级更精细。
3. **版本之上直接计算**（SQL + 向量）→ 策展/共识/质量门禁/语义检索无需搬数据到外部引擎。
4. **任意时间点 + 每事务版本**（PITR）→ 持续标注「恢复到某刻」、实时流「每事务一版本」，lakeFS 的离散 commit 做不到。

边界：海量**非结构化字节**的内容级版本、整仓库多文件原子，仍是 lakeFS 主场 → **组合使用**最优（见 `exp_integration_poc` / `exp_multimodal_pipeline`）。
