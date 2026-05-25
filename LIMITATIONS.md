# MatrixOne 实测缺陷 / 限制清单（v3.0.11）

> 本清单只收录**本项目实验里真实撞到、并已验证**的问题，标注来源实验与规避方法。
> 严重度：🟥 硬限制（结构性/无简单 workaround） · 🟧 坑（有 workaround，注意即可） ·
> 🟨 缺失功能（某场景需要但没有） · ⬜ 能力边界（定位问题，非真正缺陷）。
> 配套 [COMPARISON.md](COMPARISON.md) / [EXPERIMENTS.md](EXPERIMENTS.md)。

## 1. git4data / `DATA BRANCH` 的限制

| # | 现象 | 来源 | 影响 | 规避 |
|---|---|---|---|---|
|1.1|🟥 `DATA BRANCH DIFF/MERGE/PICK` **只有表级**，无 `DATABASE` 形式（写 `DATA BRANCH DIFF DATABASE …` 直接语法错）|exp_branch_advanced|整库的 diff/merge 要逐表做，没有"一条命令原子合并整库所有表"|逐表脚本化；整库一致性靠 `SNAPSHOT FOR DATABASE`+`RESTORE DATABASE`（这俩是原子的）|
|1.2|🟥 diff/merge 要求**两表 schema 完全一致**；分支上 `ALTER ADD COLUMN` 后报 `schema is not equivalent`|exp_branch_advanced|特征列/字段演进后无法跨版本 diff/merge|演进前先对齐 schema，或把新列加到双方|
|1.3|🟧 `MERGE`/`PICK` **不能在显式事务 `BEGIN…COMMIT` 内执行**（报 `unclassified statement appears in uncommitted transaction`）|本清单实测|无法把 merge 和其它写操作包进一个事务|autocommit 下单独执行|
|1.4|🟧 **DIFF 行身份坑**：对一批行 `DELETE` 全删再 `INSERT` 同主键，会被识别成 INSERTED 而非 UPDATED（DIFF `UPDATED=0`）|exp_sft/早期|想看"改了哪些行"时结果失真|用**原地 `UPDATE … JOIN`**（多表更新已验证可用）而非删后重插|
|1.5|🟧 `DATA BRANCH DIFF/MERGE` **强烈依赖主键**；`CREATE TABLE … AS SELECT`(CTAS) 建的表**没有主键**|exp_rlhf_preference|对 CTAS 结果直接做 DATA BRANCH 会不准/报错|先 `CREATE TABLE(... PRIMARY KEY)` 再 `INSERT … SELECT`|
|1.6|⬜ `RESTORE` 是**覆盖式还原**，本身不产生"这是一次回滚"的提交记录|matrixone demo|审计链不如 lakeFS `revert`（反向提交留全历史）|靠快照命名/外部登记留痕|

## 2. 快照 / RESTORE / PITR —— 语法坑与不一致

| # | 现象 | 来源 | 规避 |
|---|---|---|---|
|2.1|🟧 普通 `SELECT` 的 `{snapshot=}` 是**语句级作用域**：一条 SQL 里写两个不同快照会塌缩成一个（想用 `EXCEPT` 直接比两快照恒为空）|matrixone demo|用 `DATA BRANCH DIFF t {snapshot='b'} AGAINST t {snapshot='a'}`，或各自 `CLONE` 成临时表再比|
|2.2|🟧 **库级 `CLONE` 必须用库级快照**；拿表级快照 `CLONE DATABASE` 报 `cannot use a table-level snapshot to clone … database`|matrixone demo|先 `CREATE SNAPSHOT … FOR DATABASE`|
|2.3|🟧 `RESTORE … FROM SNAPSHOT` **必须带 `ACCOUNT <租户名>`**（租户名是用户名串第一段，不是登录用户）；`RESTORE TABLE …` 直接写会语法错|matrixone demo|`RESTORE ACCOUNT <tenant> DATABASE db TABLE t FROM SNAPSHOT s`|
|2.4|🟥 `RESTORE … FROM PITR` 的语法**与 SNAPSHOT 版不一致**：PITR 版**不能带 `ACCOUNT`**（带了就语法错），正确是 `RESTORE [DATABASE db [TABLE t]] FROM PITR <name> "ts"`|exp_continuous_annotation / PITR 验证|两种 restore 记两套写法|
|2.5|🟥 `{MO_TS=}` 时间戳时间旅行**不可靠**：拒绝日期字符串（`invalid debug timestamp string`），纳秒整数报 `table does not exist`|PITR 验证|别用 MO_TS，用快照 / PITR|

## 3. SQL 函数 / 特性缺失（尤其分析与可观测向）

| # | 缺失 | 来源 | 影响 / 规避 |
|---|---|---|---|
|3.1|🟨 **无 p95/p99 百分位**：只有 `median`(=p50)；`quantile`、`approx_percentile`、`percentile_cont(...) within group` 都不支持|§15.1 实测|时延分位数算不了——可观测/SLA 核心指标缺失；无好规避|
|3.2|🟨 **无 `uniq`/HLL** 近似基数（有 `approx_count_distinct`）|§15.1 实测|大基数近似去重受限|
|3.3|🟨 **无时间分桶函数** `date_trunc` / `to_start_of_minute`|§15.1 实测|"指标随时间"曲线要手算桶|
|3.4|🟨 **无原生 TTL**（`CREATE TABLE … TTL …` 语法错）|§15.1 实测|监控数据自动过期要手动删分区/外部调度|
|3.5|🟨 **无 `CREATE MATERIALIZED VIEW`**（语法错）|§15.1 实测|没有流式增量 rollup|
|3.6|🟧 **无 `GREATEST`/`LEAST`**|exp_rlhf_preference|用 `CASE WHEN a>b THEN a ELSE b END`|
|3.7|🟧 **`SUM(布尔)` 报错** `bad value [BOOL]`（如 `SUM(status='ERROR')`）|exp_otel_agent_trace|改 `SUM(CASE WHEN … THEN 1 ELSE 0 END)`|

## 4. 非结构化 / `datalink` 的边界

| # | 现象 | 来源 | 说明 |
|---|---|---|---|
|4.1|🟥 `datalink` **只版本化"引用"不版本化"字节"**：打快照后覆盖 OSS 文件，time-travel 读到的仍是**新字节**|exp_stage_datalink|外部大文件的字节级内容版本化做不到——要么字节存进表内 `BLOB`（则被版本化），要么交给 lakeFS|
|4.2|🟧 `datalink` 在 **INSERT 时即校验 stage 存在**|exp_multimodal_catalog|不能先插引用后建 stage；得先 `CREATE STAGE`|

## 5. 性能 / 架构（诚实，含远程实例 caveat）

| # | 现象 | 来源 | 说明 |
|---|---|---|---|
|5.1|⬜ **纯策展/分析裸吞吐不如专用引擎**：同份 SFT 策展 lakeFS+DuckDB 更快（225/383ms vs 585/1961ms）|exp_bench_curation|DuckDB 是进程内专用 OLAP；MO 是远程通用库。MO 价值在集成/版本/行级语义，不是裸速|
|5.2|⬜ **海量实时 trace 摄入/监控不是它的料**：摄入 28ms(CH) vs 112ms(MO 远程)，且缺 percentile/TTL/MV/时间分桶|exp_clickhouse_vs_matrixone / §15.1|firehose 监控用 ClickHouse；MO 管"要版本化/标注/联接"的那部分|
|5.3|🟧 **远程实例逐语句网络往返**：多条顺序 DELETE/INSERT 各付一次 RTT（如逐条插入 ~50 txn/s）|exp_stream_versioning / bench|同机部署或批量摄入可大幅缓解（部署问题，非引擎固有）|

## 区分：以下是「定位边界」而非缺陷
- 不是对象存储数据湖：海量非结构化字节的内容寻址/多引擎直读/整仓库原子提交 → 那是 lakeFS 的活。
- 不是特征平台：缺声明式特征定义框架、托管物化编排、在线服务 SLA、注册表/监控 → 那是 Tecton/Hopsworks 的活。
- 不是 APM/可观测产品：没有 trace UI、采样、告警、service map、trace↔log↔metric 关联生态 → 那是 ClickHouse+SigNoz/Grafana 的活。

---

**总体判断**：git4data 作为**结构化数据的版本控制**功能完整、行级粒度优秀，但 (a) 分支/合并是**表级**、要求 **schema 一致**、不能进事务；(b) 快照/PITR 的**语法不一致、有若干坑**；(c) 作**分析/可观测引擎**缺一批关键函数（percentile/uniq/时间分桶/TTL/MV）；(d) `datalink` **不版本化字节**。多数是"注意即可"的坑或缺失功能，真正硬的是 1.1/1.2（表级 + schema 一致）、2.4/2.5（PITR/MO_TS）、3.1（无分位数）、4.1（不版本化字节）。
