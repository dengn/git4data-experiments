# git4data 场景地图：已覆盖 + 尚未覆盖的想法

本仓库从「ML 持续学习」出发，逐步把 MatrixOne 的 **git4data**（snapshot / 时间旅行 /
CLONE / `DATA BRANCH` 行级 diff·merge·pick / PITR）压到一个又一个真实场景上验证。这份
文档是**场景地图**：左边是已实测的，右边是还没做、但 git4data 看起来契合的点子，留作后续。

git4data 真正吃得开的判据（反复验证出来的四条）：
1. **数据是结构化/半结构化、且以行/表为单位演进**（不是海量不可解析的字节）；
2. **需要「版本」语义**：可命名、可回到、可比对、可合并、可挑拣；
3. **版本之上还要直接计算**（SQL/向量/聚合），不想把数据搬去别处；
4. **要么协作（多人/多机/多分支）、要么可审计（回滚/复盘/PITR）**。
四条命中越多，越是 git4data 的主场。

---

## 一、已覆盖（每条都有可重跑的实测脚本）

| 场景 | 脚本 / 文档 | git4data 用到的能力 |
|---|---|---|
| 大规模零拷贝版本 | `exp_scale.py` | snapshot/clone/branch/restore 与数据量无关 |
| 增量训练只训 delta | `exp_incremental_diff.py` | `DATA BRANCH DIFF` 取变更行 |
| 协作打标合并冲突 | `exp_concurrent_merge.py` | `MERGE WHEN CONFLICT FAIL/SKIP/ACCEPT` |
| SFT 数据策展 | `exp_sft_curation.py` | 原地 SQL + 行级 DIFF 溯源 |
| RLHF/偏好数据 | `exp_rlhf_preference.py` | SQL 共识 + cherry-pick 改判 + pin 快照 |
| Feature Store（vs Tecton） | `exp_feature_store.py` | PIT as-of join + 特征值版本化/快照=发布 |
| Write-Audit-Publish | `exp_write_audit_publish.py` | 暂存分支→门禁→原子 MERGE 发布 |
| 持续标注 + 复现 | `exp_continuous_annotation.py` | 训练快照 DIFF + PITR 还原某刻 |
| 实时流版本化 | `exp_stream_versioning.py` | 每事务一版本 + PITR 任意微秒 |
| 多模态目录 | `exp_multimodal_catalog.py` | datalink 编目 + vecf32 + 目录版本化 |
| 非结构化引用边界 | `exp_stage_datalink.py` | 只版本「引用」非「字节」 |
| lakeFS × MatrixOne 集成 | `exp_integration_poc.py` / `exp_multimodal_pipeline.py` | 字节级时间旅行（组合）|
| Agent 进化 | `exp_agent_evolution.py` | branch 进化 + RESTORE 回滚 + cherry-pick 技能 |
| OTel agent trace 后端 | `exp_otel_agent_trace.py` | 快照=agent 版本 + DIFF A/B |
| vs ClickHouse（trace 后端） | `exp_clickhouse_vs_matrixone.py` | 版本化/可变更/可联接 vs 摄入/OLAP |
| durable execution（vs Temporal/DBOS/Restate） | `durable_exec/` | 执行日志快照/版本化可审计回放 |
| vs Neon / Supabase（BaaS） | `exp_neon_branching.py` | 即时 CoW 分支 + 行级 diff/merge |
| **具身智能 3D 记忆**（vs OctoMap/rosbag） | `exp_robot_memory_3d.py` | 漂移 DIFF + 机群 MERGE + 回滚 + 时间旅行 |

---

## 二、尚未覆盖、但 git4data 看起来契合的想法

> 下面是「还没做」的点子。打 ✅ 的是四条判据命中多、最值得下一个做的。

### A. AI / Agent 记忆与知识
- ✅ **RAG 知识库的可版本化与可回滚**：文档/chunk/embedding 表随知识更新而演进；
  「这次回答用的是哪一版知识库」= pin 快照；坏文档入库后 `RESTORE` 回滚；两版知识库
  `DATA BRANCH DIFF` 看「改了哪些 chunk」。比向量库单纯 upsert 多了版本与审计。
- ✅ **Agent 长期记忆 / memory store 的分支**：给每个用户/会话的记忆开分支，实验性人格/
  策略在分支上演进，验证后 MERGE 回主线；坏记忆回滚。（与 `exp_agent_evolution` 同源但面向"记忆"而非"技能"）
- **多 Agent 协作的共享世界状态**：多个 agent 并发改同一份「世界状态/黑板」，用行级
  MERGE 冲突策略仲裁（类似机器人机群合并，但对象是符号状态而非体素）。
- **Prompt / 工具链配置的版本化与灰度**：prompt 模板、tool schema、路由规则存表，
  快照=发布版本，分支=灰度实验，DIFF=配置变更评审，RESTORE=一键回滚。

### B. 数据治理 / 合规
- ✅ **GDPR/「被遗忘权」的可审计删除**：删除某用户数据前 snapshot，删除后用 DIFF 出具
  「确切删了哪些行」的合规证据；保留窗口内 PITR 可验证。
- **数据合同（data contract）回归**：上游 schema/分布变化时，分支上验证再 MERGE，
  挡住破坏性变更进入生产训练集。
- **审计追溯**：任意「模型决策当时所依据的数据」用 PITR/快照精确重建（金融/医疗合规）。

### C. 时序 / IoT / 物理世界（机器人是其中一例）
- **数字孪生状态版本化**：工厂/电网/楼宇的孪生状态表按时刻快照，「故障发生时系统是什么
  状态」用 PITR 重建；what-if 在分支上推演。
- **传感器标定（calibration）版本化**：标定参数表的每次变更 = 一版本，回滚错误标定，
  DIFF 对比两次标定差异。
- **地图/路网更新的灰度发布**：导航地图按区域分支更新，DIFF 评审，MERGE 发布，
  事故回滚（与机器人体素地图同构，面向 HD map）。

### D. 软件 / 系统
- **配置中心 / Feature Flag 的版本化**：配置即数据，分支灰度 + 行级 DIFF 评审 + 秒级回滚。
- **CDC / 数据管道的可回放**：每批入仓 snapshot，管道 bug 后回滚重跑（部分被
  `exp_stream_versioning` 触及，可做成"管道级 WAP"）。
- **多租户 SaaS 的「按租户分支」**：给租户开 CoW 分支做沙箱/试用/演示，用完丢弃
  （Neon 工作流的多租户变体，`exp_neon_branching` 的延伸）。

### E. 科学 / 仿真
- **实验数据集的可复现快照**：每次 paper/run pin 数据版本，审稿复现用 PITR。
- **仿真参数扫描（parameter sweep）分支**：每组参数一分支，结果 DIFF 对比，择优 MERGE。

---

## 三、提醒自己的边界（别过度推销）

- **海量不可解析字节**（视频/大图原始流的内容级版本、整仓库多文件原子提交）→ 仍是
  lakeFS 主场，git4data 只版本「引用」。组合使用最佳。
- **专用引擎的内核能力**（OctoMap 的 3D 几何、ClickHouse 的 OLAP/quantile/TTL、
  Temporal 的确定性重放、Supabase 的 BaaS 应用层）→ git4data 不替代，定位是
  **「版本化 + SQL + 可联接」的数据管理层**，叠在这些场景之上提供 git 语义。

> 取舍判据见本文件开头四条；逐场景的诚实结论见 `COMPARISON.md` 与 `LIMITATIONS.md`。
