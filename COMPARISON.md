# lakeFS vs MatrixOne git4data —— 面向 ML「持续流入 + 持续学习」场景的能力对比

> 这是**深度报告**；想按教程动手上手，请先看 **[README.md](README.md)**。

> 本报告基于本仓库两个**可运行 demo** 的实测结论：
> - `matrixone/run_demo.py` —— 实跑于 MatrixOne Cloud `v3.0.11`
> - `lakefs_demo/run_demo.py` —— lakeFS server（Docker）+ OSS blockstore（S3 兼容）
>
> 两个 demo 使用**完全相同**的数据生成器（`common/data_stream.py`）、增量模型
> （`common/model.py`，`SGDClassifier.partial_fit`）和五幕剧情，因此精度数字
> 跨系统一致——差异只体现在「版本控制的动作（verbs）」上。

---

## 0. 场景与验证设计

模拟一个二分类模型在数据持续流入下的持续学习，并把每个真实 MLOps 诉求映射到
一个版本控制原语：

| 幕 | MLOps 诉求 | 验证点 |
|---|---|---|
| ACT 1 | 数据分批持续流入，模型增量学习 | 每批一次「提交」，模型版本→数据版本可追溯 |
| ACT 2 | 两个数据版本之间发生了什么变化 | diff |
| ACT 3 | 复现某个历史模型（合规/排障/审计） | 按版本回到历史数据重训，指标 bit 级一致 |
| ACT 4 | 上游来了一批坏数据（投毒/错标） | 回滚数据到坏批次之前并恢复 |
| ACT 5 | 在不影响主线的前提下做数据清洗实验 | 分支 → 实验 → diff → cherry-pick → 合并 |

实测精度曲线（两系统一致）：

```
ACT1 干净批次:  v0=0.977  v1=0.980  v2=0.978  v3=0.970  v4=0.979  v5=0.989
ACT2 版本 diff: MO 原生 DATA BRANCH DIFF = INSERTED 3000 / lakeFS = added 3 files
ACT3 复现 v3:   0.970  (== 注册表记录, reproducible == True)
ACT4 投毒批次:  0.886  -> 回滚/revert -> 0.989  (恢复)
ACT5 清洗实验:  main 0.989 -> 分支修正 310 行错标 -> 0.9955
              -> MO: DIFF 310 UPDATE / PICK 53 行 / MERGE 其余  |  lakeFS: diff 6 files / merge -> main 0.9955
```

---

## 1. 能力映射总表

| 版本控制能力 | git 类比 | **lakeFS** | **MatrixOne git4data** |
|---|---|---|---|
| 提交/打标 | commit / tag | `commit` + `tag`（原生，**全仓库原子**） | `CREATE SNAPSHOT`（表/库/账户级） |
| 读历史版本 | checkout（只读） | 读 `ref`（commit/tag/branch）下对象 | `SELECT … {snapshot='…'}`（time-travel） |
| 连续时间点恢复 | —— | 无（提交是离散点） | `CREATE PITR … RANGE`（连续区间） |
| 分支 | branch | `branch create`（原生、零拷贝、全仓库） | `DATA BRANCH CREATE TABLE … FROM`（带血缘）/ `CLONE`（零拷贝复制） |
| 差异 | diff | `diff`（原生，**对象/整文件级**） | `DATA BRANCH DIFF … AGAINST`（原生，**行级**，INSERT/UPDATE/DELETE 分类，LCA 感知） |
| 合并 | merge | `merge`（原生，对象级三方合并 + 冲突） | `DATA BRANCH MERGE … WHEN CONFLICT FAIL/SKIP/ACCEPT`（原生，**行级**三方合并 + 冲突） |
| cherry-pick | cherry-pick | 无（只能 merge 整分支） | `DATA BRANCH PICK … KEYS(…)`（原生，**按主键摘取部分行**） |
| 回滚 | revert / reset | `revert`（原生，生成反向提交、留全历史） | `RESTORE … FROM SNAPSHOT`（覆盖式还原） |
| 版本上做计算 | —— | 需把对象读出来再算（外部引擎） | **可直接在快照上跑 SQL/聚合/JOIN** |

一句话（已据 v3.0.11 实测修正）：**两者都有原生 git 语义**。对**结构化表**，MatrixOne
的 diff/merge/cherry-pick 是**行级**且更细、还能直接在版本上跑 SQL；lakeFS 的版本控制
是**对象/整文件级**但**作用于整个仓库（多文件原子提交/分支/合并）**，且天生管任意
blob、贴着对象存储数据湖、可被多引擎共享。**粒度 MatrixOne 细，原子范围 lakeFS 广。**

---

## 2. 维度详解

### 2.1 数据模型：对象 vs 结构化表
- **lakeFS**：版本化的是**对象存储里的文件**（CSV/Parquet/图片/任意 blob）。它不
  关心文件内容，天然适配非结构化/半结构化数据（图像、文本、音频、特征文件、模型
  权重）。代价是 diff/merge 只能到**对象（整文件）粒度**——本 demo 清洗修改了 310
  行，但 6 个批次文件全被重写，**实测 lakeFS native diff 报告的是「6 objects
  changed」**，看不到是哪 310 行。要在 lakeFS 拿到行级需配合表格式（Iceberg/Delta
  on lakeFS）。
- **MatrixOne**：版本化的是**数据库表**。天生结构化、强 schema，**原生** `DATA
  BRANCH DIFF` 做到**行级**并区分 INSERT/UPDATE/DELETE（本 demo 实测：版本间
  `INSERTED=3000`、清洗实验 `UPDATED=310`）。
- **MatrixOne 也能管非结构化文件（更正：早先说"只能结构化"过于绝对）**：通过
  `CREATE STAGE`（指向 S3/OSS/fs 的命名外部位置）+ `datalink` 列 + `load_file()`，
  一张表可以**编目**外部 OSS 上的图像/文档（哪些文件属于哪个数据集版本、标签、划分），
  并用 git4data 版本化这张"引用表"。**但实测关键语义**（`experiments/exp_stage_datalink.py`）：
  **git4data 版本化的是「引用」不是「字节」**——打快照后覆盖 OSS 上的文件，time-travel
  读到的仍是新字节。要内容版本化，要么把字节存进表内 `BLOB` 列（则被版本化），要么用
  lakeFS（内容寻址、字节级版本）。
- **补充（实测）**：若把文件**字节直接存进表内 `BLOB` 列**，则 git4data **会内容版本化
  字节**——快照后覆盖 BLOB，time-travel 仍读到旧字节（`exp` 验证）。所以"内容版本化"
  MatrixOne 也能做，区别在于 lakeFS 把字节放在对象存储用**内容寻址**管理（适合海量大
  文件、多引擎直读），而 MatrixOne 是把字节当作表数据放进 DB（适合中小文件、与结构化
  字段同库同事务，但大媒体文件偏重、且不便被 DB 外的引擎直接按文件访问）。

> 适应性：训练集是**结构化特征表**→ MatrixOne 更顺手（且 diff/merge 行级）；要把原始
> 文件**编目+引用**进结构化数据集 → MatrixOne 的 stage/datalink 够用；中小文件要**内容
> 级版本** → MatrixOne 表内 BLOB 也行；**海量大文件的内容级版本 + 多引擎直读** → lakeFS。

### 2.2 diff / merge / cherry-pick：两者都原生，但粒度与范围不同
> 更正：早先误判「MatrixOne 无原生 diff/merge」。实测 v3.0.11 **有完整的 `DATA
> BRANCH` 命令族**，且是行级。本节据此重写。

- **lakeFS**：`diff`/`merge`/`revert` 是一等公民，作用于**整个仓库**——一次 commit /
  branch / merge 覆盖仓库内所有文件，给出跨多文件的**单一一致版本**；merge 是
  **对象级**三方合并（冲突 = 同一文件两边都改）。
- **MatrixOne `DATA BRANCH` 命令族（实测可用）**：
  - `DATA BRANCH CREATE TABLE t2 FROM t1`：建立**带血缘 DAG** 的分支，供 DIFF/MERGE
    自动识别最近公共祖先（LCA）。
  - `DATA BRANCH DIFF t2 AGAINST t1 [OUTPUT SUMMARY|COUNT|LIMIT n|FILE]`：**行级**
    diff，输出每行的 INSERT/UPDATE/DELETE；支持跨两个 `{SNAPSHOT=}` 比较同一张表。
  - `DATA BRANCH MERGE src INTO dst WHEN CONFLICT FAIL|SKIP|ACCEPT`：**行级三方合并**
    + 冲突策略（实测 FAIL 真报冲突、ACCEPT 覆盖）。
  - `DATA BRANCH PICK src INTO dst KEYS(…)`：**cherry-pick 指定主键的行**——lakeFS 没有
    对应的行级摘取能力。
- 本 demo ACT5 完整跑了：分支 → 清洗 → `DIFF`(310 UPDATE) → `PICK` 53 行(batch 0 先灰度)
  → `MERGE` 其余。**结论反转**：对结构化表，MatrixOne 的分支/合并比 lakeFS **更细
  （行级 vs 文件级）且多了 cherry-pick**；lakeFS 的优势在于**整仓库原子**与**任意文件类型**。
- 一个真实差异：MatrixOne `DATA BRANCH DIFF/MERGE` 是**按表**的；要把多张表当一个单元
  原子地分支/合并，需逐表处理（库级有 snapshot/clone/restore，但无库级 diff/merge）。
  lakeFS 的提交/分支/合并天生是**整仓库**的，多文件一致性更省心。

### 2.3 回滚语义：反向提交 vs 覆盖还原
- **lakeFS `revert`**：生成一个**新的反向提交**，历史完整保留、可审计、可再 revert
  回去——真正的 git 语义。
- **MatrixOne `RESTORE`**：把表/库**覆盖式**还原到某快照的状态。简单直接，但它本身
  不产生「这是一次回滚」的提交记录（需要靠快照命名/外部登记来留痕）。好处是还原后
  数据仍可被后续快照继续版本化。

### 2.4 版本之上的「计算能力」——MatrixOne 的独门优势
- lakeFS 只管版本，**不提供计算**：要在某版本上算指标、做特征工程、join 维表，必须
  把对象读到外部引擎（Spark/DuckDB/pandas）里算。
- MatrixOne 是 HTAP 数据库，**可以直接在快照上跑 SQL**：`SELECT … {snapshot=…}`
  做聚合、统计漂移、关联标签表、生成训练视图，无需搬数据。对「持续学习里需要频繁
  在历史版本上做数据质量分析/特征统计」的场景，这是实打实的便利。

### 2.5 连续恢复：PITR
- MatrixOne 额外提供 **PITR**（`CREATE PITR … RANGE 1 'h'`），可在一个时间区间内恢复
  到**任意时间点**，而不止离散快照点。对「误删/误改后要回到事故发生前某一刻」很有用。
- lakeFS 只能回到**已有的离散提交**，没有连续时间点恢复（除非提交足够密集）。

### 2.6 运维与部署成本
- **lakeFS**：需要常驻 **server 进程** + 元数据 KV（本 demo 用 local，生产用
  PostgreSQL/DynamoDB）+ 对象存储后端。本 demo 用单 Docker 容器 + 本地元数据 +
  OSS blockstore，最轻形态。客户端 `lakectl`/Python SDK。
- **MatrixOne**：本就是一套数据库（本 demo 直连其 Cloud 实例），git4data 是库内置
  能力，**零额外组件**——会用 MySQL 协议就能用。

### 2.7 与训练代码的集成
- 两边在本 demo 里都用**同一套 Python ML 代码**：
  - lakeFS：把对象读成 bytes → 解析 → 喂给 `partial_fit`。
  - MatrixOne：`SELECT … {snapshot=…}` 直接出 numpy 数组喂给 `partial_fit`。
- 复现性两边都成立（`reproducible == True`），因为数据 + 顺序 + 随机种子都被钉死。
  MatrixOne 少一层「读文件再解析」，对结构化特征更直接。

---

## 3. 实测中发现的「坑」/限制（重要）

### MatrixOne
1. **`DATA BRANCH DIFF/MERGE` 强烈建议表有主键**（用隐藏假主键也行，但有 PK 更准）；
   **两表 schema 必须完全一致**——分支上 `ALTER ADD COLUMN` 后 diff/merge 直接报
   `schema is not equivalent`（特征列演进需先对齐，见 §5.4）；MERGE/PICK **不能在显式
   事务 `BEGIN…COMMIT` 内**执行。
2. **行级 diff 跨快照 OK，但普通 `SELECT` 的 `{snapshot=}` 是语句级作用域**：一条
   普通 SQL 里写两个不同快照会塌缩为一个（所以早先想用 `EXCEPT` 直接比两快照恒为空）。
   正确做法就是用 `DATA BRANCH DIFF t {SNAPSHOT='b'} AGAINST t {SNAPSHOT='a'}`，它能
   正确比较两个快照。
3. **库级 clone 需要库级快照**：表级快照 `CLONE DATABASE` 会报
   `cannot use a table-level snapshot to clone … database`，得先建库级快照。
4. **`RESTORE` 语法须带 account 名**：`RESTORE ACCOUNT <租户名> DATABASE … TABLE …
   FROM SNAPSHOT …`；这里的 account 是用户名串里的**第一段租户名**，不是登录用户名。
5. **`DATA BRANCH` 的粒度**：`DATA BRANCH CREATE DATABASE` 可**整库分支**（实测可用），
   但 `DATA BRANCH DIFF/MERGE/PICK` **只有表级**（无 `DATABASE` 形式，实测报语法错误）。
   即：整库可原子 snapshot/restore、可整库 branch，但**整库的 diff/merge 要逐表做**，
   没有"一条命令合并整库所有表"的原子提交。
6. 时间戳形式 time-travel（`{MO_TS=}`）需纳秒整数、不接受日期字符串；用快照更省心。

### lakeFS
1. **diff/merge 仅到对象（整文件）粒度**：行级变更不可见，需配合 Iceberg/Delta 表
   格式或自行做内容比对。
2. **不提供计算**：所有「在某版本上算东西」都要外部引擎。
3. **OSS 作为 S3 兼容后端（已实跑验证）**：lakeFS server 配
   `BLOCKSTORE_TYPE=s3` + `S3_ENDPOINT=https://oss-cn-shanghai.aliyuncs.com` +
   `S3_REGION` + `FORCE_PATH_STYLE=false` 即可对接 OSS，repo 数据真实落在
   `s3://<bucket>/<repo>/` 下。实测注意点：
   - **必须虚拟主机风格寻址**（`bucket.endpoint`）；path-style 会被 OSS 拒
     （`SecondLevelDomainForbidden`）。lakeFS 侧 `FORCE_PATH_STYLE=false`，用
     boto3 清理命名空间时需 `Config(s3={'addressing_style':'virtual'})`。
   - lakeFS 启动时会 `HeadBucket` 探测 region，OSS 对此返回 403，但 lakeFS 会
     **自动回退到配置的 region**，不影响使用。
   - OSS 的批量 `DeleteObjects` 需要 `Content-MD5`（新版 boto3 默认不发）→ 报
     `MissingArgument`；改用单对象 `DeleteObject` 即可。
   - AccessKey 必须处于**启用**状态，否则 OSS 报 `InvalidAccessKeyId: disabled`。
   - lakeFS server 本身需常驻（本 demo 单 Docker 容器 + 本地元数据 KV）。

---

## 4. 结论：git4data 能否满足「持续流入 + 持续学习」？

**能，而且比初判更强。** MatrixOne git4data 在本 demo 中完整覆盖了持续学习场景的全部
关键诉求，且具备**完整的原生 git 语义**（不止快照/回滚）：

- ✅ **持续流入 + 版本化**：每批 `CREATE SNAPSHOT`，模型版本→数据快照血缘清晰。
- ✅ **复现性**：time-travel 重训得到 bit 级一致指标（合规/审计/排障刚需）。
- ✅ **坏数据回滚**：`RESTORE` 一键还原并恢复模型精度。
- ✅ **原生分支/diff/合并/cherry-pick**：`DATA BRANCH CREATE/DIFF/MERGE/PICK`，
  **行级**、带 LCA 三方合并与冲突策略（FAIL/SKIP/ACCEPT）。本 demo 实测分支清洗
  →行级 diff(310)→cherry-pick(53)→merge 全跑通。
- ✅ **版本上直接 SQL**：历史数据质量分析/特征统计无需搬数据——lakeFS 做不到。
- ➕ **额外**：PITR 连续时间点恢复；单组件、零额外运维。

**与 lakeFS 的真实分界**（不再是"有没有 diff/merge"，而是粒度与范围）：

| | **MatrixOne git4data** | **lakeFS** |
|---|---|---|
| diff/merge 粒度 | **行级**（INSERT/UPDATE/DELETE + cherry-pick） | **对象/整文件级** |
| 版本作用范围 | **按表**（多表原子需逐表） | **整仓库原子**（多文件一致版本） |
| 数据类型 | 结构化表（含向量列） | **任意 blob**（图像/文本/音频/权重） |
| 版本上计算 | **原生 SQL** | 需外部引擎 |
| 历史模型 | 命名快照 + 分支血缘 | 不可变 commit DAG，revert 留全历史 |
| 部署 | 一套数据库，零额外组件 | server + 元数据 KV + 对象存储 |
| 连续恢复 | **PITR** | 仅离散提交 |
| 生态 | MySQL 协议 / SQL | 贴对象存储数据湖，多引擎(Spark/Trino…)共享 |

### 选型建议

| 你的情况 | 倾向 |
|---|---|
| 训练数据是**结构化表**，想要**行级** diff/合并/cherry-pick | **MatrixOne**：粒度更细、版本上直接查、运维轻 |
| 数据量大 + 持续学习要**只训练增量**（每轮成本 ∝ 变更量） | **MatrixOne**：行级 DIFF 取 delta + 零拷贝快照（§5.1/5.2） |
| 要把原始文件**编目/引用**进结构化数据集（路径+标签+划分） | **MatrixOne**：stage + datalink 够用（但只版本引用） |
| 要对**海量原始文件做内容级版本**（图像/文本/音频/权重逐版本回溯） | **lakeFS**：内容寻址、字节级版本 |
| 需要把**很多文件/表当一个单元**原子提交/分支/合并 | **lakeFS**：整仓库一致版本（MO 库级仅快照/恢复，无库级 diff/merge） |
| 需要连续时间点恢复（PITR）、HTAP 即席分析 | **MatrixOne** |
| 已有**对象存储数据湖** + 多引擎(Spark/Trino)读同一份数据 | **lakeFS**：不改数据布局，语言无关 |
| 想要「一套系统」既存又管版本、最少组件 | **MatrixOne** |
| 需要可浏览的不可变提交历史、PR 式数据评审协作 | **lakeFS**：commit DAG + 分支工作流成熟 |

> 实务折中：用 **MatrixOne 承载结构化训练集 + git4data 做版本/复现/回滚/行级
> diff/合并/即席分析**，用 **lakeFS 承载非结构化原始数据湖 + 整仓库分支/合并协作**，
> 二者并不互斥。对你最初的问题——**git4data 完全能满足持续学习场景，且在结构化数据
> 的分支/合并粒度上反而强于 lakeFS**。

---

## 5. 深度实验与实测数据（`experiments/`，均实跑于 v3.0.11，自清理）

### 5.1 大数据量下 git4data 是否更有优势（`exp_scale.py`）
服务端 `generate_series` 造数，行数 1万→100万（100×），测各操作延迟：

| rows | SNAPSHOT | BRANCH | CLONE | DIFF(改1k行) | RESTORE |
|---|---|---|---|---|---|
| 1万 | 24ms | 36ms | 29ms | 1586ms | 83ms |
| 10万 | 47ms | 32ms | 29ms | 1631ms | 54ms |
| 100万 | 26ms | 35ms | 31ms | 1923ms | 58ms |

**结论**：snapshot/branch/clone/restore 是**元数据/copy-on-write 操作，与数据量无关，恒定
几十毫秒**；diff 只随**变更量**走（改 1k 行，表涨 100× 时间仅 +20%）。数据量越大，
"零拷贝快照/克隆 + 行级增量 diff" 的相对优势越明显。注：lakeFS 的 commit/branch 也是
元数据零拷贝（这点二者打平）；MatrixOne 在大规模上的**额外**优势是——能在任意版本上
**直接跑 SQL 做聚合/质量分析/特征统计而不搬数据**（lakeFS 需外部引擎读全部对象）。

### 5.2 持续学习「只训练增量」（`exp_incremental_diff.py`）—— 最贴合 ML 的深度需求
每轮新增 1 批 + 少量历史行的迟到修正；用 `DATA BRANCH DIFF live AGAINST 上次训练的快照`
取出**恰好变更的行**（INSERT+UPDATE），只对这个 delta 做 `partial_fit`：

| cycle | 库内行数 | 本轮 delta | 增量累计处理 | 全量重训累计 | acc |
|---|---|---|---|---|---|
| 0 | 1000 | 1000 | 1000 | 1000 | 0.977 |
| 2 | 3000 | 1004 | 3007 | 6000 | 0.975 |
| 5 | 6000 | 1002 | 6012 | 21000 | 0.9905 |

**结论**：每轮处理量恒为 ~1k（≈delta），**与数据集总量无关**；6 轮下增量共处理 6012 行 vs
全量重训 21000 行（3.5×，且差距随轮数**二次增长**——100 轮可达数十倍）。这是数据版本控制
给持续学习带来的**核心效率红利**：每轮成本 ∝ 变更量，而非数据集大小。

### 5.3 并发标注分支的合并冲突（`exp_concurrent_merge.py`）
两组从同一基线分支并行重标注、在 row 3 上分歧，合并 A 后再合并 B：

- `WHEN CONFLICT FAIL` → **检测到 pk(3) 冲突并报错**（强制人工裁决，不静默丢失任一方）；
- `WHEN CONFLICT SKIP` → 保留目标(A)的值；`ACCEPT` → 采用来源(B)的值。

**结论**：**行级三方合并 + 冲突策略**，正是多标注员/多管道协作打数据所需；lakeFS 只能
在**整文件**层面发现冲突。

### 5.4 边界：schema 演进 与 库级原子（`exp_branch_advanced.py`）
- **schema 演进**：分支上 `ALTER ADD COLUMN` 后，`DATA BRANCH DIFF/MERGE` 报
  `schema is not equivalent` 拒绝执行——**特征列增减会打断 diff/merge，需先对齐 schema**。
- **库级原子**：`DATA BRANCH DIFF/MERGE` 是**按表**的；但 `CREATE SNAPSHOT FOR
  DATABASE` + `RESTORE … DATABASE` 能把 **features+labels 多表一起原子**打快照/回滚
  （实测两表同时回到一致时点）。覆盖"整训练集一个一致版本"的需求（只是没有库级 diff/merge）。

### 5.5 非结构化文件 与 变更导出（`exp_stage_datalink.py`）
- `CREATE STAGE`(→OSS) + `datalink` 列 + `load_file()`：表可编目并读取 OSS 上的文件；
  **快照版本化的是引用而非字节**（覆盖 OSS 文件后 time-travel 读到新内容）。
- `DATA BRANCH DIFF … OUTPUT FILE 'stage://…'`：把变更集导出为**可重放的事务 SQL 补丁**
  到 OSS（`BEGIN; … DELETE keys; INSERT rows; COMMIT`），便于跨环境同步数据集 delta——
  lakeFS 无此 SQL 补丁形态。

---

## 6. ML 训练场景适配：SFT / RLHF 这类**结构化数据策展**最契合 MatrixOne

LLM 微调阶段（pretraining 之后）的数据大多是**结构化行 + 丰富元数据**，而"策展"本质是
**集合运算**：去重、按质量/长度过滤、去 eval 污染、按比例混合、标注一致性/共识、主动学习
采样。这些恰好是 SQL，而且需要"版本化 + 行级溯源 + 可复现"。MatrixOne 把**计算与版本放在
一处**，正中靶心；lakeFS 只能存文件 + 靠外部引擎（Spark/DuckDB）算，且 diff 只到文件级。

### 6.1 SFT 数据策展（`exp_sft_curation.py`，实测）
整条流水线都是**版本化表上的原地 SQL**，每步可快照、`DATA BRANCH DIFF` 给行级溯源：

```
raw 8000 → 去重(prompt_hash,留最高质量) -1189 → 质量<0.5 -3239
        → token>2048 -368 → 去 eval 污染 -40 → curated 3164   [流水线 410ms]
DATA BRANCH DIFF curated AGAINST raw → DELETED=4836（精确到行的"砍了哪些"溯源）
分支实验(quality≥0.7) → DIFF 显示会再砍 1241 行 → 决定后 MERGE 或丢弃
```

价值：去重/过滤/去污染/混合一条 SQL 搞定、结果是可复现的版本、diff 能回答"这 4836 条
分别因何被删"。lakeFS 存 jsonl 时这些都要外部引擎，且 file-level diff 答不出"删了哪些样本"。

### 6.2 RLHF / DPO 偏好数据（`exp_rlhf_preference.py`，实测）
3000 个偏好对 × 3 标注员 = 9000 票，全流程 SQL + 原生分支语义：

```
SQL 聚合算共识/一致性: 一致(3-0)=2174, 有争议(2-1)=826
senior 在 review 分支上改判 20 个争议对 → DATA BRANCH DIFF = UPDATED 20(行级)
DATA BRANCH PICK 把这 20 个改判 cherry-pick 回 consensus
reward-model 训练集 = 一致 + 改判 = 2194；其余 806 争议对排除；标签均衡 {0:1104,1:1090}
每次 reward-model 训练 pin 一个快照 → 可复现；新标注员加入 → DIFF 给出"哪些对的标签翻转"
```

价值：**标注者一致性/共识是 SQL 聚合**、争议对是一个 `WHERE`、**改判是 cherry-pick**、
每轮训练 pin 快照可复现。多标注员并行还可走"分支/合并 + 行级冲突 FAIL/SKIP/ACCEPT"
（见 `exp_concurrent_merge.py`）。lakeFS 要做这些一致性数学得另起外部引擎。

### 6.3 还有哪些训练数据场景吃这套能力
- **主动学习**：按预测不确定性 SQL 选样 → 标注 → 版本化 → 只训增量（§5.2）。
- **数据去污染/去重**：维护 eval/污染集，反连接过滤，版本化干净集。
- **数据配比实验**：分支上调源/语言比例，DIFF 看变化，A/B 后 MERGE。
- **reward/打分漂移分析**：存各模型版本对样本的打分，跨版本 diff 出"哪些样本打分变了"。
- **在线/持续学习**：新数据流入 → DATA BRANCH DIFF 取 delta → 增量训练（§5.2）。

### 6.4 边界（何时仍偏 lakeFS）
- **TB 级原始语料 / 多模态大文件**（pretraining 文本、图像、音频、视频、模型权重）的**内容级
  逐版本回溯** + **多引擎直读** → lakeFS 更自然（SFT/RLHF 数据集通常是万~百万级结构化样本，
  正好落在 MatrixOne 舒适区）。
- 要把**多张表当一个单元**做"一次原子 merge" → lakeFS（MatrixOne 整库可原子 snapshot/restore，
  但 merge 仍逐表，见 §3.5）。

**小结**：**SFT/RLHF/偏好/主动学习这类"结构化 + 需策展 + 需复现"的训练数据，MatrixOne
git4data 有实打实优势**——但这个优势是**集成度 / 行级语义 / 可复现性 / 运维简单**
（计算+版本+服务一处、行级 diff/merge/cherry-pick、增量、可复现），**不是"算得更快"**
（专用 OLAP 引擎如 DuckDB 在原始吞吐上反而更快，见 §7 实测基准）；真正海量非结构化
原始语料仍是 lakeFS 的主场。

---

## 7. 正面性能基准：同一份 SFT 策展（`exp_bench_curation.py`，实测）

同一份确定性数据、同样 4 步策展（去重/质量/长度/去污染），对比两种架构（warmup +
3 次取中位数）：

| N | MatrixOne 原地 SQL（策展） | lakeFS+DuckDB（读+算+写回提交） | 明细 |
|---|---|---|---|
| 5 万 | 585 ms | **225 ms** | read 42 + duckdb 27 + write+commit 156 |
| 20 万 | 1961 ms | **383 ms** | read 80 + duckdb 67 + write+commit 236 |

**诚实结论（推翻了我先前的直觉）**：在这个规模上 **lakeFS+DuckDB 的原始策展吞吐反而更快**——
DuckDB 是世界级的进程内 OLAP 引擎（compute 仅 27–67ms），parquet 小、OSS 往返便宜；而
这里的 MatrixOne 是**远程云实例**，4 条顺序 DELETE 各自要一次网络往返 + copy-on-write 开销。

所以 **MatrixOne 的优势不在"算得快"**，而在：
- **一套系统**搞定 存+版本+计算+对外服务，**省去独立计算引擎(DuckDB/Spark)+编排+胶水**；
- **行级** diff/merge/cherry-pick 与冲突处理（lakeFS+DuckDB 只能整 parquet 文件版本化，
  两个标注员的行级改动无法原生合并，得重算）；
- 原地 copy-on-write 快照、可在任意版本上直接 SQL、PITR、可复现与治理。

**公平起见的注意点**：① 这里的 MatrixOne 是远程云实例，**同机部署会大幅缩小差距**；
② lakeFS 路线每轮都**整文件重写**，在更大规模/更多轮次下其"读全量+写全量"成本会线性
增长（本基准未测）；③ DuckDB 极快但需要你自己搭"读 lakeFS→算→写回提交"的流水线。
**一句话：要原始批处理吞吐选 DuckDB/Spark+lakeFS；要"版本化+行级语义+少运维"选 MatrixOne。**

---

## 8. lakeFS 与 MatrixOne 能配合用吗？——能，而且互补

两者不在同一层，**最佳实践是组合而非二选一**：lakeFS 管**非结构化原始数据湖**的
字节级版本，MatrixOne 管**结构化策展数据集**的行级版本 + SQL。常见组合：

**架构 A：两层数据栈（推荐）**
- lakeFS（对象存储）：原始/非结构化资产——对话日志、文档、图像、音频、模型权重、原始
  抓取——按 commit 版本化。
- MatrixOne（git4data）：从某个 lakeFS commit 抽取/策展出的**结构化表**——特征、标签、
  偏好对、标注、embedding（向量列）——行级版本化 + SQL 策展（§6）。
- 流水线：读 lakeFS 某 commit 的原始数据 → 抽取/清洗 → 落 MatrixOne 表 → `CREATE SNAPSHOT`。

**架构 B：共享 OSS + 血缘对链**
- 两者可落在**同一个 OSS 桶**。把**lakeFS 的 commit id 存进 MatrixOne 数据集表的一列**，
  每个 MatrixOne 快照即可追溯到它所源自的 lakeFS 原始数据版本——
  端到端血缘：原始(lakeFS commit) → 策展(MatrixOne snapshot) → 模型(registry)。

**架构 C：MatrixOne 直接读 lakeFS 版本化的文件**
- 方式①：MatrixOne `CREATE STAGE`/`LOAD` 指向 **lakeFS 的 S3 网关**（lakeFS 本身暴露
  S3 兼容端点，bucket=repo、key=`ref/path`），从而按**逻辑路径 + 版本**读文件。
- 方式②：用 lakeFS API 把逻辑路径解析成 OSS 上的**物理地址**，再让 MatrixOne STAGE 从
  **共享 OSS** 读该物理对象。
- 注意：本 demo 的 lakeFS server 跑在本地 `127.0.0.1:8200`，**MatrixOne 云实例无法直连本机**；
  生产中需让 lakeFS 端点对 MatrixOne 可达（或共享 OSS 走方式②）。

**RLHF/SFT 落地示例**：原始对话/转写/图像放 lakeFS；抽取出的偏好对、标注一致性、打分、
embedding 放 MatrixOne（向量列 + git4data）；每次 reward-model 训练在 MatrixOne pin 一个
快照，并记录对应的 lakeFS commit → 全链路可复现、可审计。

> **可运行 PoC**：`experiments/exp_integration_poc.py` 实跑了这套集成——lakeFS 提交图像字节
> (C1)→ MatrixOne 目录表 pin C1 + 存 embedding → 快照 dataset_v1；图像字节改变 →lakeFS C2 →
> 目录 pin C2 → dataset_v2。**实测效果**：把目录解析到 dataset_v1 读回 C1 的旧字节、解析到
> dataset_v2 读回 C2 的新字节——**字节级时间旅行由"lakeFS 存字节 + MatrixOne 目录 pin commit"
> 组合达成**（两者单独都做不到）；`DATA BRANCH DIFF` 精确指出变更的那一个 asset；并能解析出
> 训练 dataloader 可直读的物理 URL。这正是 §10「与其重造不如集成」的落地证明。

> **端到端 capstone**：`experiments/exp_multimodal_pipeline.py` 把这套集成跑成一条**持续迭代**
> 的多模态「版本化 + 打标 + 训练」流水线（4 轮）：原始字节落 lakeFS(commit) → MatrixOne 编目+
> 打标 + `vecf32` embedding → 每轮快照=数据集版本并训练+注册模型；新数据流入(新 commit)、分支上
> 清洗噪声标签(`DIFF`/`MERGE`，acc 0.868→0.904)、某资产原始字节重导出(新 commit、目录重 pin)
> 各自在对的层版本化。实测可**精确复现历史模型**(m2 retrain==registry)、对同一资产在不同数据集
> 版本读回**不同字节**、并打印 model→catalog 快照→lakeFS commit→acc 的完整血缘谱系。

---

## 9. 四类工程场景的匹配度（均设计了实验并实测）

| 场景 | MatrixOne 匹配度 | 关键能力 / 实测 | lakeFS 对照 |
|---|---|---|---|
| 多人协作/持续标注 + 周期训练 | ★★★★★ | 每次训练 pin 快照；`DATA BRANCH DIFF` 精确给出两次训练间 **增/改/删行级差异**；**PITR 恢复到任意未打标的时刻** | 可用显式 commit 充当"训练版本"，但**取不到未 commit 的任意时刻** |
| 实时数据流（Kafka→训练集） | ★★★★★ | **每条 INSERT 即一个持久事务版本**；PITR 重建"截至任意微秒"的训练集 | **批量 commit 模型**，无法"每事务一版本"；高频流逐条 commit 不现实 |
| Write-Audit-Publish | ★★★★☆ | Write=`DATA BRANCH CREATE`、Audit=分支上 SQL 质量门禁（生产零暴露）、Publish=`DATA BRANCH MERGE` 原子发布 | **同样支持**（branch→checks→merge），是 lakeFS 的经典模式；审计需外部引擎、合并到文件级 |
| 多模态 图像/视频/文档 | ★★★☆☆（目录侧）/ ✗（字节侧） | datalink 编目 + `load_file` 读取 + `vecf32` 向量近重检测 + git4data 版本化**目录/标签/embedding** | **★★★★★（字节侧）**：内容寻址、字节级版本、多引擎直读——大文件本体的版本化主场 |

### 9.1 多人协作 / 持续标注（`exp_continuous_annotation.py`）
实测：训练 W1 快照 500 条 → 中途加到 650（**不打快照**）→ 训练 W2 快照 800 条。
`DATA BRANCH DIFF W2 AGAINST W1` = **INSERTED 300 / UPDATED 80 / DELETED 0**（"长了 300 条新标
+ 80 条重标"，精确到行）。再用 **PITR 恢复到那个没打快照的"周三 3 点"** → 还原出 650 条的中间
态，再回滚到当前 800 条。**"两次训练差多少、差在哪"有了确定答案；任意历史时刻可复现。**

### 9.2 实时数据流版本化（`exp_stream_versioning.py`）
实测：500 个事件 = **500 个独立事务**落库；之后 **PITR 重建**"刚过第 200 个事件那一刻" →
正好 **201 行**，"第 380 个" → **381 行**（微秒级时间点）。**每个事务都是可恢复的版本，训练集
可重建到流的任意微秒，无需批量 commit 步骤**——这是 lakeFS 批量提交模型做不到的。
（注：本实例为远程云，逐条插入 ~50 txn/s 受网络限制；同机/批量摄入会高得多，要点是"每事务
一版本 + PITR"而非裸吞吐；生产中常配 MatrixOne 的流式摄入。）

### 9.3 Write-Audit-Publish（`exp_write_audit_publish.py`）
实测：生产 1000 条干净标签。Write 到 staging 分支灌入 340 条新批次（含 15 非法标签/15 重复/
10 eval 污染）。**Audit#1** 在分支上跑 SQL 门禁 → 抓出 15/15/10，**生产仍 1000 条零暴露**。
分支上修复后 **Audit#2 全过** → **`DATA BRANCH MERGE` 原子发布** → 生产 1300 条。
WAP 闭环成立；MatrixOne 用**原地 SQL 审计 + 行级原子合并**，lakeFS 用外部引擎审计 + 文件级合并。

### 9.4 多模态（`exp_multimodal_catalog.py`）
实测：用 stage+datalink 编目 5 个图像/视频/文档（指向 OSS），`load_file` 读出文档内容，
`l2_distance` 在 SQL 里**找出近重复对（dist=0.02）**，分支上删近重 + 改 split →
`DATA BRANCH DIFF` = DELETED 1 / UPDATED 1。**分工**：MatrixOne 版本化**目录/标签/划分/
embedding** 并做语义 SQL；**媒体字节本体不被快照版本化**（datalink=引用，见 §2.1/§5.5），
字节级版本交给 lakeFS。**这是最该"二者配合"的场景**（见 §8）。

> 总览：场景 1、2（持续标注、实时流）**MatrixOne 优势显著**，核心是
> **"数据库式每事务版本 + PITR 任意时间点 + 行级 DIFF"**；场景 3（WAP）两者都行；
> 场景 4（多模态字节）**lakeFS 主场**，MatrixOne 管目录侧——正是组合使用的理由。

---

## 10. 若要原生补齐"多模态字节级版本化"，MatrixOne 需要哪些能力

现状（已实测）：`datalink` 版本化的是**引用字符串**不是**字节**；表内 `BLOB` 会被快照
版本化但大媒体不现实。要让 MatrixOne 像 lakeFS 那样**对外部大文件做字节级版本**，需要补：

1. **不可变 / 内容寻址的对象版本身份**——核心。要么 MatrixOne 自管一个**内容寻址 blockstore**
   （写即新对象、永不覆盖、按 content-hash 去重），要么对接**对象存储原生版本**（OSS/S3
   bucket versioning），并把对象的 **content-hash / version-id 记进 datalink**。
2. **快照/分支捕获该字节版本身份**——快照时固化 hash/version-id，使 time-travel 能解析到
   **当时的确切字节**（今天只固化了 URL）。`DATA BRANCH DIFF` 即可凭 hash 列把"文件内容变了"
   识别成一次行 UPDATE。
3. **写路径管控 / 变更捕获**——"改文件"必须生成**新的不可变版本**、旧版本仍被旧快照引用；
   带外覆盖必须被拦截或被记录（否则就是今天观察到的"只版本引用"）。即媒体要**写穿
   MatrixOne**（像写穿 lakeFS），或与对象存储版本机制联动。
4. **物理地址解析 + 预签名 URL**——给定快照/ref，能解析出**确切字节版本的物理 OSS URL**，
   让训练 dataloader **直接从对象存储高吞吐读字节**，数据库不进大数据通路（类比 lakeFS 的
   physical_address / S3 网关）。
5. **块级去重 / 增量存储**——GB 级视频、模型权重改一点不该整文件复制；需内容定义分块 + 去重
   （比 lakeFS 的整文件去重更细更省）。
6. **垃圾回收 / 保留策略**——快照/PITR 过期后，回收不再被引用的历史字节对象（跨快照/分支
   的引用计数），否则存储无限膨胀。
7. **目录 + 字节的原子一致版本**——一个数据集版本要**同时**固化目录行与所引用的确切字节版本
   （扩展现有库级快照到外部对象）。
8. **媒体的 branch/diff/merge + 完整性校验 + 血缘**——分支上替换/新增媒体、按文件冲突合并、
   校验和、媒体版本→目录版本→模型的血缘链。

**两条落地路线**：
- **轻量务实**：靠 OSS/S3 **bucket versioning** + datalink 记 version-id + 生命周期做 GC。
  快，但依赖后端、无去重/合并控制、GC 与桶生命周期耦合。
- **完整对标 lakeFS**：在 MatrixOne 内嵌一套内容寻址 blockstore + 提交图（写穿、去重、GC、
  物理解析）——本质是"把 lakeFS 做进数据库"，工程量大且偏离数据库本行。

**建议**：与其重造，不如**深度集成**——让 `datalink` 把 **lakeFS 的 ref/commit-id 当作版本坐标**：
MatrixOne 快照固化"该数据集对应的 lakeFS commit"，字节版本交给 lakeFS、目录/标签/embedding
版本交给 git4data，一条血缘贯通（见 §8）。这样各取所长，而非让任一方重复造轮子。

---

## 11. 参考架构：非结构化数据版本管理平台怎么搭

**核心原则：字节与事实分层版本化。** 原始字节用对象存储原生的版本层管，结构化目录/策展
用数据库管，不要让一个工具全干。

**分层（自下而上）**
1. **对象存储底座**：OSS / S3 / MinIO——所有原始字节（图像/视频/音频/文档/语料/模型权重）。
2. **字节版本层**：**lakeFS**——repo/branch/commit/tag/merge/diff/revert/GC，格式无关、多引擎
   直读。**生产元数据用 PostgreSQL/DynamoDB**（非本地 KV），或直接用 lakeFS Cloud 托管。
3. **结构化目录层**：**MatrixOne（git4data）**——资产目录表（id / 模态 / 逻辑路径 /
   **lakeFS commit-id** / content-hash / size / label / split / **embedding(vecf32)** / 质量 / 血缘），
   行级版本化 + SQL 策展 + 向量近重 + PITR。
4. **血缘对链**：目录行携带 **lakeFS ref/commit-id** → 一个数据集版本 =（MatrixOne 目录快照
   + 它指向的若干 lakeFS commit）。端到端：原始字节(lakeFS) → 目录/策展(MatrixOne) → 模型(registry)。
5. **计算层**：Spark / Ray / DuckDB / Flink 按 ref 读版本化数据做清洗/特征/embedding；结构化
   策展用 MatrixOne SQL；产出按 WAP 写回。
6. **接入/服务层**：训练 dataloader 经 **lakeFS 解析的物理/预签名 URL 直读字节**（高吞吐、
   DB 不进大数据通路）；pin 一个 ref 保证可复现。
7. **治理**：GC/保留策略、访问控制、审计、配额与成本。

**摄入纪律**：统一走 **Write-Audit-Publish**（写 lakeFS 分支 → 质量审计 → merge 到主），每个
数据集版本 **pin ref**，目录表记下对应 commit。

**按数据形态/规模选型**
| 你的情况 | 字节版本层选择 |
|---|---|
| 纯文件、要整个湖的 git 语义 | **lakeFS** |
| 数据偏表格、要引擎生态 | **Iceberg/Delta + Nessie**（Nessie 给 Iceberg 类 git 分支） |
| 小团队 / 需求简单 | **DVC** 或对象存储原生版本 + manifest 清单 |
| 超大文件且频繁小改 | lakeFS + **块级去重**（XetHub/Git-LFS 思路）评估 |
| 不想自运维 | **lakeFS Cloud**（托管控制面，自带 OSS/S3） |

结构化目录/策展/可复现组装这一层，无论字节层选谁，都建议用 **MatrixOne（或同类带版本的库）**。

**分阶段落地**
- **Crawl**：OSS + lakeFS(quickstart) 把原始数据 commit 起来 + 一张 MatrixOne 目录表记 commit-id。
- **Walk**：WAP 摄入流程、Spark/DuckDB 处理、embedding 入向量列、血缘贯通、换生产级元数据库。
- **Run**：GC/保留/配额、访问控制/审计、多分支协作、与训练/CI/编排（Airflow/Argo）打通。

> 对你现状：**OSS + lakeFS + MatrixOne 骨架已就位**。下一步把"目录表记 lakeFS commit-id"的
> 血缘补上 + WAP 摄入，再按规模决定是否上生产元数据库 / lakeFS Cloud / 块级去重。

---

## 12. 非 ML 场景：用 git4data 存 Agent trace + branching 做 Agent 进化（实测可行）

设想：把 agent 的可演化"大脑"（learned skills/memory 表）和执行 `trace` 日志都存进
git4data，**agent 进化 = git 工作流**：基线 → 分支大脑 → 从失败 trace 学习 → 跑变体 →
对比 → 择优 `MERGE`（否则丢弃）。`exp_agent_evolution.py` 用一个**确定性 agent 模拟器**
（某类问题 T 可解 ⟺ memory 里有 T 的 rule 且质量≥0.5）实测了全流程：

| git4data 能力 | 在 Agent 进化中的角色 | 实测 |
|---|---|---|
| 表存储 + 快照 | trace + 大脑作为版本化表；每代一个**不可变 agent 版本** | gen0..gen3 各一快照 |
| `DATA BRANCH CREATE` | 把大脑 fork 成隔离变体去探索 | 每代分支 memory |
| 从 trace 学习 + 对比 + `MERGE` | 一个**进化代**：读失败 trace→学新 skill→赢了才合并 | 50%→70%→90%→**100%**（3 代） |
| `DATA BRANCH DIFF` | "这一代学到了什么"（精确到 skill 行） | 每代 INSERTED=2/2/1 |
| `RESTORE` | 撤销一次**坏的自我编辑**（回滚到上一好版本） | 故意degrade skill#3→90%→还原→100% |
| `MERGE WHEN CONFLICT` | 调和**并行探索分支**对同一 skill 的冲突 | 两 explorer 改 skill#0→冲突→ACCEPT=0.99 |
| `DATA BRANCH PICK` | 只提拔**验证过的 skill**（其余不动） | cherry-pick {1,2}，3/4 保持原样 |
| PITR / 时间旅行 | 复现 agent 任意历史时刻的精确状态、可审计 | 快照即可回到任一代 |

**结论：能正常工作，且契合度很高。** 这类"探索-择优-合并/回滚 + 谁学了什么可追溯/可复现/
可审计"的进化范式，恰好就是 git 的语义，MatrixOne git4data 的**行级 branch/diff/merge/
cherry-pick + 快照/PITR** 把它们一一对上了。要点与边界：
- 适合把 agent 的**结构化状态**（memory/skills/config/偏好/few-shot 例子/打分）放进来——
  行级 diff/merge 让"两条进化路线如何合并、冲突如何裁决"变得可控；
- agent 产生的**大块原始 trace/产物**（长对话、图像、网页快照）仍建议走 §8/§11 的组合：
  字节放 lakeFS、结构化 trace/索引/打分放 MatrixOne，目录记 lakeFS commit 串血缘；
- 真实落地时 agent 的"成功/奖励"信号来自真实评测而非模拟器，但版本控制层的玩法不变。

---

## 13. Feature Store 场景：MatrixOne 作后端 + git4data 特征版本化，对比 Tecton

### 13.1 Feature store 的核心职责
1. **特征变换 + 物化**：定义 raw→feature 的转换（batch/stream/on-demand），把特征值物化下来；
2. **point-in-time 正确的训练集生成**：对每个 label 事件（时刻 T），取**截至 T**的特征值（防泄漏/防 train-serve skew）——这是 feature store 的灵魂；
3. **在线 + 离线服务**：online（低延迟取最新值）+ offline（全历史用于训练）；
4. **特征定义/值的版本化与持续迭代**。

### 13.2 Tecton（事实标准）画像
托管特征**平台**：声明式 FeatureView（Python/SQL）→ 托管的批/流/按需**物化**；**双存储**
（offline=S3/数仓，online=DynamoDB/Redis，需保证两边一致）；`get_historical_features` 做
**PIT time-travel join** 生成训练集；生产级在线服务 + **新鲜度 SLA/监控**；特征**定义**版本走
代码/git；多源 connector 生态。（开源对应物是 Feast。）

### 13.3 MatrixOne 作后端的契合度（`exp_feature_store.py` 实测）
| 能力 | MatrixOne 怎么做（实测） | Tecton |
|---|---|---|
| 特征物化 | SQL 聚合 `INSERT…SELECT`（滚动窗口）；增量物化只算新 asof 点 | 托管 Spark/流式物化 |
| **PIT 正确训练集** | `ROW_NUMBER() … asof_day<=label_day` 的 as-of join——实测无泄漏（user0@day12 取 as-of cnt=2 而非最新 11） | `get_historical_features` |
| online + offline | **同一 HTAP 表**：最新值点查(~19ms) + 全历史 PIT join——**无双存储 skew** | 两套存储，需维护一致 |
| 特征**值**版本化 | **git4data 快照** = 每次特征发布；可时间旅行复现某版训练集 | 不强调（版本化的是定义） |
| 改特征定义 | 分支上 in-place 重算 → `DATA BRANCH DIFF`（实测 7d→14d 改了 **120** 行）→ `MERGE` | 改代码 → 重新物化 |
| 任意时点 | `PITR` 恢复到任意时刻 | 离散物化点 |

### 13.4 结论
- **MatrixOne 能胜任 feature store 的「后端」**：PIT join / 物化 / online+offline / 持续迭代
  都能用 SQL 在一处完成；且 **git4data 给特征版本化带来 Tecton 数据层没有的能力**——
  特征值按发布快照、分支做特征工程实验并行级 diff/merge、PITR 任意时点、每次训练 pin 一版可复现。
- **HTAP 单存储**天然消除 Tecton 双存储的 online/offline 一致性难题。
- **MatrixOne 的 gap（它是数据库不是特征平台）**：没有声明式特征定义框架、没有托管的流式/
  按需物化与编排、没有内置在线服务 SLA/监控/新鲜度、没有多源 connector 生态——这些要在上面
  自建一层（或接 Feast 这类把 MatrixOne 当 offline+online store 的开源框架）。
- **最佳形态**：**MatrixOne 当「存储 + 版本 + PIT 引擎」后端，上面薄薄一层特征定义/编排/监控**；
  对中小规模或已重度用数据库的团队，这套比引入完整 Tecton 平台更轻、且自带可复现的特征版本治理。

### 13.5 校正：feature 最重要的能力排序，以及「版本化」的真实定位
（早先 §13 把 git4data 版本化抬得偏高，这里据行业实践校正。）

**按重要性排序**：
1. **Point-in-time 正确性**——feature store 存在的根本理由（防泄漏/防 train-serve skew）。
2. **低延迟在线服务 + online/offline 一致性**——区别于「直接查数仓」的关键（Tecton sub-10ms p99）。
3. **特征复用 / 发现 / 治理（registry）**——组织级价值：定义一次、多团队多模型复用、口径统一。
4. **新鲜度 / 物化编排**——batch+stream 物化、backfill、freshness SLA。
5. **变换管理**——声明式定义 batch/stream/on-demand，训练与服务同一套逻辑。
6. **监控**——特征漂移 / 质量 / 新鲜度 / 服务指标。
7. **版本化 / 血缘（可复现）**——重要，但属第二梯队，且很大一部分已被下面两机制覆盖。

**「版本化」到底重不重要？——重要，但不是头号，且大部分特征*值*版本化已被覆盖**：
- (a) **时间轴本身**：特征值带时间戳 + PIT as-of join，本就能「取任意时刻的特征值」——这已是一种版本化，不必显式 snapshot/branch。
- (b) **lakehouse 表格式**：Hopsworks 用 **Hudi**、Databricks 用 **Delta**、SageMaker 用 **Iceberg**，都自带 time-travel + commit log + 行级变更追踪。「特征数据版本化」是成熟能力，**不是 git4data 独有**（Hopsworks 甚至以「Versioned Feature Data with a Lakehouse」为卖点）。

git4data 在版本化上真正的**增量价值**，集中在时间轴/lakehouse 覆盖不到的三点：
1. **特征定义变更的版本化**：同一时刻 T，「7d 定义算的值」vs「14d 定义算的值」时间轴区分不了；snapshot/branch 能把「定义版本→物化值」钉住并行级 diff。
2. **数据修正下的复现**：PIT join 复现的是「按*当前*数据 as-of T」；若事后修正了历史 raw 数据，as-of join 会返回修正后的值。要「按数据集版本 bit 级复现、不受后续修正影响」需 snapshot——两种不同的复现语义。
3. **分支式特征工程实验 + 行级 diff/merge/cherry-pick**：比 Delta/Hudi 快照、Iceberg 分支更细。

### 13.6 更广的 feature store 产品对比
| 产品 | 类型 | 在线服务 | PIT | 物化 | 特征值版本化机制 |
|---|---|---|---|---|---|
| **Tecton** | 托管平台 | sub-10ms p99 | ✅ | batch+stream+on-demand（强） | 定义走代码/git；值版本化弱 |
| **Feast** | 开源轻量 | BYO(Redis/DynamoDB) | ✅ | 弱（主要 BYO 物化） | 靠底层 store；**可把 MatrixOne 当 offline+online store** |
| **Hopsworks** | 开源+托管平台 | RonDB（低延迟） | ✅ | batch+stream | **Hudi**：time-travel + commit log + 行级变更（版本化最强之一） |
| **Databricks FS** | 平台 | DynamoDB/Cosmos | ✅ | batch(+DLT 流) | **Delta** time-travel + Unity Catalog 血缘 |
| **SageMaker FS** | 托管 | ~10–25ms p99 | ✅ | batch | offline 用 **Iceberg/S3** time-travel |
| **Vertex AI FS** | 托管 | Bigtable(~10ms) | ✅ | batch | BigQuery 底座 |
| **Featureform** | 虚拟/元数据层 | 编排已有设施 | 依赖底层 | 依赖底层 | 依赖底层 |
| **MatrixOne+git4data** | HTAP 数据库（作后端） | 同库点查 | ✅(SQL) | SQL/INSERT…SELECT（编排需自建） | **行级 DIFF/MERGE/PICK + 快照 + 每事务 PITR**（比 Delta/Hudi 更细） |

**洞察**：主流 feature store 的「特征值版本化」普遍靠 **lakehouse 格式（Hudi/Delta/Iceberg）**拿到，
所以 MatrixOne git4data 在版本化维度**对标的是它们的 time-travel**——差异在**行级 diff/merge/
cherry-pick 更细 + 每事务 PITR 时间粒度更细 + HTAP 单存储 online/offline 合一**。但要成为 feature
store，**真正缺的不是版本化，而是上面那层平台能力**（声明式定义、托管物化编排、在线服务 SLA、
注册表治理、监控）——这正是 Tecton/Hopsworks 的强项。**所以 git4data 版本化是「锦上添花的差异点」，
不是选型首要标准**。

---

## 14. OpenTelemetry agent trace 接入（`exp_otel_agent_trace.py`，实测）

**背景**：agent/LLM trace 的接入事实标准是 **OpenTelemetry**——GenAI semantic conventions
定义了 `gen_ai.*` 属性与 span 形态（`invoke_agent` 根 span，子 `chat {model}` LLM 调用、
`execute_tool {name}` 工具调用），经 OTLP → OTel Collector → 后端（常见落 ClickHouse 这类列存）。

**MatrixOne 能当这个后端吗？——能（实测）**：写一个标准的 `opentelemetry.sdk` `SpanExporter`，
把每个 `ReadableSpan` 映射成 MatrixOne `spans` 表的一行（trace_id/span_id/parent/name/kind/
start/end/duration/status + 抽出的 `gen_ai_*` 列 + 完整 `attributes` JSON）。本 PoC 用真实 OTel
SDK（`TracerProvider` + `SimpleSpanProcessor` + 该 exporter）模拟 16 次 agent 运行：

- **接入**：8 条 agent trace（32 spans）经真实 OTel exporter 落库；
- **查询（普通 SQL）**：按 parent_span_id 重建 trace 树；按 trace 聚合 token/延迟；
  `WHERE status='ERROR'` 找出所有失败的 tool/LLM 调用（实测 4 个 error span）；
- **版本化（git4data）**：快照 = 「某 agent 版本的 trace」；再灌入 agent v2（换 `gpt-4o-mini`）→
  `DATA BRANCH DIFF v2 vs v1` = INSERTED 32 spans；SQL 做版本 A/B：
  **v1 gpt-4o 1025 tok/trace、2 个 error trace；v2 gpt-4o-mini 962 tok/trace、1 个**。

**价值与对照**：
- 生产形态是「agent SDK → OTLP → OTel Collector（带一个 MatrixOne exporter）」，本 PoC 把那个
  exporter 内联了；映射逻辑一致。
- 相比 ClickHouse 等**纯 trace 存储**，MatrixOne 多了 **git4data 版本化 + 行级 diff/merge**：
  同一份 trace 存储**既是可观测性后端、又是可版本化的 agent 迭代基质**——快照固定「这次评测/这版
  agent 用的 trace」，行级 DIFF 出「v2 比 v1 多/改了哪些 span」，并能在版本化的 trace 上直接 SQL
  做 cost/error/延迟的跨版本 A/B。这与 §12 的「agent trace + branching 进化」是同一套能力的两面：
  trace 既可观测、又可版本演化。
- 边界：MatrixOne 不是 APM/trace UI 产品，没有 Jaeger/Tempo/Grafana 那套可视化与采样/告警生态；
  它提供的是「**SQL 可查 + git4data 可版本化的 trace 存储后端**」。

> **真实 agent 接入（`agent_otel/`）**：`exp_otel_agent_trace` 的 span 是模拟的；`agent_otel/`
> 则是一个**真正会跑的 agent**——真实多步工具调用循环（`calculator`/`kb_lookup` 真实执行）+
> 真实 OTel instrumentation，span 经真实 `SpanExporter` 落 MatrixOne。LLM 可插拔（有
> `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` 走真实 Claude/OpenAI tool-use，否则本地确定性规划器）。
> 实测：agent 真算出 47*19=893、法国人口×2=134000000、Hamlet→Shakespeare、10%光速=29979.2、
> Atlantis→工具报错(error span)；24 个真实 span 入库，全程 SQL 可观测 + git4data 快照。
> `python -m agent_otel.run`。

---

## 15. 对比 ClickHouse 作 agent/OTel trace 后端（`exp_clickhouse_vs_matrixone.py`，实测）

**背景**：ClickHouse 是 OTel trace 存储的**事实标准**（OTel Collector 一等 exporter；SigNoz/
Uptrace 基于它）。所以 agent-trace 场景最该对比的基线是 ClickHouse，而非 lakeFS。把同一份
OTel span 灌进两边（ClickHouse 走进程内 chdb，MatrixOne 远程云），比这个场景的关键点。

**实测（1000 spans，同一份数据）**：

| 关键点 | ClickHouse | MatrixOne |
|---|---|---|
| 摄入吞吐 | **28 ms**（MergeTree 专为海量 append） | 112 ms（远程，网络往返） |
| 可观测性查询 | **44 ms** | 52 ms（远程） |
| OLAP 函数 | **有** `quantile()/uniq()` 等（p95=68.42ms） | 无原生 quantile（用 MAX 近似） |
| error 计数 | 72 | 72（同数据同答案） |
| 原生 TTL 保留 | **★ 有** | 靠快照/PITR 或手动 |
| 可观测生态(OTel/SigNoz/Grafana) | **★ 事实标准** | 自建 |
| **版本化** snapshot/DIFF/PITR | ✗（无 git-for-data） | **★ 有**：`DATA BRANCH DIFF` v2 vs v1=INSERTED 1000（行级） |
| **行级可变更**（给 span 打 eval 标签/修正） | 弱：`ALTER…UPDATE` 是后台 mutation（重写 parts） | **★ 事务级 `UPDATE`**：标 144 个 error span，26ms |
| **JOIN**（关联模型注册表/数据集/特征） | 受限/笨拙（专用 OLAP） | **★ 通用 SQL**：spans ⋈ model_registry 算每模型成本 |

**诚实结论**：
- **纯 trace 存储 / 海量实时监控：ClickHouse 完胜**——摄入、OLAP 扫描与函数、TTL、生态都是它的主场。
- **MatrixOne 的差异点（针对「agent 迭代」而非「监控」）**：同一份 trace 存储**同时**是
  ① git4data 版本化的（snapshot per agent/评测版本、行级 DIFF、PITR）、② 行级可变更的
  （事务级给 span 打人工评测标签/修正）、③ 可 JOIN 到版本化数据集/特征/模型表的统一 SQL 存储。
  即「可观测性后端 + 可版本化/可标注/可联接的 agent 迭代基质」三合一。
- **务实组合**：ClickHouse 扛海量实时监控 trace；MatrixOne 存「被选出来要做评测/迭代/标注、并
  要和训练数据关联」的那部分 trace 做版本化治理。
- 注意：本测 MatrixOne 为远程云实例，网络延迟抬高了它的 ms；同机部署会缩小摄入/查询差距，但
  ClickHouse 作为专用 OLAP 引擎在裸吞吐上本就该赢——所以重点看**能力轴**而非**速度轴**。

### 15.1 海量实时监控 trace 需要哪些能力（以及 MatrixOne 的实测缺口）

为什么"海量实时监控"是 ClickHouse 主场？因为这套工作负载的能力需求，正是列存 OLAP 专门
优化、而通用 HTAP 库主动放弃的方向：

- **摄入路径**：超高吞吐 append（百万 span/s）、列存 LSM/MergeTree 式批量异步写、前置队列削峰、
  弱一致可接受（trace 容忍少量丢失，不需逐行 ACID）、半结构化属性高效存取（Map/JSON + 热键物化列/bloom 跳数索引）。
- **存储/成本**：高压缩（ZSTD/Delta/Gorilla 编码）、按时间分区（秒删整分区）、热/冷分层（SSD→对象存储）。
- **保留**：**原生 TTL** 自动过期（监控数据短命）、采样(head/tail) + 预聚合 rollup。
- **查询**：十亿行级亚秒聚合、**近似统计函数 `quantile`/tdigest(p95/p99)、`uniq`/HLL（基数）**、
  **时间分桶 `toStartOfMinute`** + gap-fill、跳数索引(bloom/min-max)、按 trace_id 点查拼树。
- **实时**：秒级 ingest-to-query、**流式物化视图**增量维护错误率/时延、高频重复聚合支撑告警。
- **扩展/生态**：分片+副本、存算分离/多租户、OTLP 原生接入、Grafana/SigNoz/Jaeger UI、trace↔log↔metric 关联。

**MatrixOne v3.0.11 实测缺口（这正是它不适合纯监控的原因）**：❌ 无 p95/p99 百分位
（仅 `median`=p50；`quantile`/`approx_percentile`/`percentile_cont` 均不支持）、❌ 无 `uniq`/HLL、
❌ 无 `date_trunc`/`to_start_of_minute` 时间分桶、❌ 无原生 TTL、❌ 无 `CREATE MATERIALIZED VIEW`；
✅ 有列存扫描、window 函数、JSON、`approx_count_distinct`、`median`。⇒ 海量实时监控用
ClickHouse/SigNoz/Uptrace；MatrixOne 负责"被挑出来要版本化/标注/联接训练数据的那部分 trace"。

> **更正/补充（定时任务）**：MatrixOne **较新版本（v26.x）有内置 `CREATE TASK` 定时 SQL 任务**
> （cron+RETRY+TIMEOUT）。它虽不是真正的流式物化视图，但可**用定时任务模拟**：定时 `DELETE`
> 旧数据≈TTL、定时聚合写入≈周期 rollup。所以"无 TTL / 无 MV"在有 `CREATE TASK` 的版本上可
> 部分弥补（代价是周期性而非实时增量）。**v3.0.11 实测即支持 `CREATE TASK`**（用驱动整条发送验证，
> `SHOW TASKS` 可见 cron 任务；注意 `BEGIN…END` 体内分号在标准 CLI 会被截断，需切 DELIMITER）；
> 而 p95/p99 百分位、`uniq`/HLL、`date_trunc`、`CREATE MATERIALIZED VIEW` 这些函数/特性缺口与
> `CREATE TASK` 无关、是单语句实测，仍然存在。

---

## 16. MatrixOne 作 durable execution engine（DBOS / Temporal 式，`durable_exec/`，实测）

**背景**：durable execution（Temporal、DBOS、Restate…）让工作流**崩溃后能从断点恢复**、每步
**恰好执行一次**。Temporal 是事件溯源+重放的引擎；**DBOS 则直接构建在数据库上**——把每步结果
checkpoint 进 DB 表，重启后跳过已完成步、从断点续跑。问题：**MatrixOne 能不能当这个 durable 后端？**

**做了什么（`durable_exec/`，DBOS 式引擎）**：`wf_exec` + `wf_step` 两张表记录工作流/步状态；
**每步的业务副作用与"步已完成"的 checkpoint 在同一个 ACID 事务里提交**；工作流被重新调用时，
已 COMMITTED 的步靠主键 `(wf_id, step)` 跳过（幂等/exactly-once）；retry 带 attempts；
`recoverable()` 就是 `SELECT … WHERE status='RUNNING'`；执行日志全程 SQL 可观测。

**实测**：订单工作流 reserve→charge→ship→receipt。
- Run 1 在 charge 提交后**崩溃**：`inventory.qty=9, payments=1, shipments=0`，`wf_exec` 仍 RUNNING，recoverable=[order-0001]。
- Run 2 用**同一 wf_id 重新调用**：reserve/charge **skipped**（从 DB 读回），ship/receipt 执行 → COMPLETED。
- 结果：`inventory.qty=9, payments=1`（**charge 与 reserve 副作用只发生一次**，尽管工作流被调用了两次）→ **EXACTLY-ONCE: True**。
- Retry：flaky 步第 1 次失败、第 2 次成功，`attempts=2` 持久记录。

**为什么 MatrixOne 契合（与监控场景正相反）**：durable execution 需要的是
**ACID 事务（副作用+checkpoint 原子）+ 持久可查状态 + 主键幂等**——正是数据库的强项，
而这恰恰是 §15 trace 监控场景里它**主动放弃**的方向。所以：**纯监控用 ClickHouse；
durable execution / 事务型持久后端用 MatrixOne（或 DBOS-on-MatrixOne）**。

**与 Temporal/DBOS 的关系**：
- **DBOS**：本质就是"库上的 durable execution 库"，本实验做的就是这个模型——MatrixOne 完全能担此角色。
- **Temporal**：是完整的事件溯源+重放引擎，外加 timers / signals / queues / child workflows /
  代码版本化 / worker 调度等——这些要在 MatrixOne 之上再加一层引擎；MatrixOne 提供的是底层
  **durable、事务化、可查询的状态存储**。
- **git4data 加成**：可对 `wf_exec/wf_step` 打快照/版本化/时间旅行，得到**可审计、可回放**的执行历史。

---

## 17. MatrixOne vs Temporal / DBOS / Restate（durable execution 能力对比）

**先厘清层次**：Temporal/DBOS/Restate 是 **durable execution 产品（引擎/框架）**；MatrixOne 是
**数据库**。在这条线上 MatrixOne 扮演的是「**持久状态存储层**」——也就是这些产品（尤其 DBOS）
所依赖的那一层。`durable_exec/` 实验等于把 **DBOS 模型在 MatrixOne 上实现了一遍**。

| 维度 | **Temporal** | **DBOS** | **Restate** | **MatrixOne**（作后端/自建引擎） |
|---|---|---|---|---|
| 形态/部署 | 独立集群（Frontend/History/Matching）+ worker + DB + ES，运维重 | **库** import 进应用 + Postgres，运维最轻 | 单二进制 **sidecar**，自带存储，运维轻 | 一个数据库（只是存储层） |
| 持久化模型 | 事件溯源 + **确定性重放** | 每步 checkpoint 到 Postgres，重启跳过已完成步 | 自带 **journal/log**（RocksDB 系） | 表 checkpoint（步+副作用同事务），重入跳过——**本实验** |
| 数据库客户端 SDK | （非重点）| 用 PG 驱动 | （非重点）| ✅ **官方 `matrixone-python-sdk`**（SQL/向量/全文检索 + snapshot/PITR/账户管理；其余语言用标准 MySQL 驱动）|
| durable-execution 编程模型 SDK（`@workflow`/`@step` + 重放）| ✅ Go/Java/Py/TS | ✅ Py/TS/Java | ✅ TS/Java/Go/Py/Rust | ❌（需在客户端 SDK 之上自建薄库，如 `durable_exec/`）|
| exactly-once 副作用 | ✅（activity 记录一次） | ✅（事务步） | ✅（journal/幂等键） | ✅（同事务 checkpoint，**已验证**） |
| **执行状态与业务数据同事务** | ❌（独立存储，activity 非事务） | ✅（同 Postgres） | ❌（自带存储） | ✅（**同库同事务**，与 DBOS 同构） |
| 持久 timers（睡几天/月）| ✅ | ✅ | ✅ | 无 per-instance 工作流 timer，但 **v3.0.11 已支持内置 `CREATE TASK` cron 定时任务**（cron+WHEN+RETRY+TIMEOUT，实测可用）可在库内调度，用来轮询 `timers` 表触发到期项——**无需外部调度器** |
| signals / queues / awakeables | ✅ | ✅（含 durable queues、notifications）| ✅（awakeables、virtual objects 状态实体）| ❌ |
| 工作流版本化 / patching | ✅（patch API）| ✅ | ✅ | ❌ |
| 可观测 / visibility | ES/SQL + UI | OTel + Cloud console + **SQL 查 history** | UI | **SQL 直接查执行日志**（自建 UI） |
| time-travel 调试 | 基于 replay | **DBOS Cloud time-traveler** | 部分 | **git4data PITR/快照/分支**——DB 层原生的时间旅行/可审计回放 |
| 执行历史上做分析(HTAP) | 需导出 | 受限于 Postgres | 需导出 | **原生 SQL/聚合**（同 §6/§14 思路） |
| 成熟度/规模 | **最成熟、最大规模**（Cadence 血脉）| 较新、增长快 | 较新、设计优雅 | 非此领域产品 |

**关键结论**
- **DBOS 的模型与 MatrixOne 最同构**：「库上做 durable execution + 步与业务写同事务 + 用 SQL 调试/join 业务数据」——这正是本实验在 MatrixOne 上做的。区别只是 DBOS 绑 **Postgres**，MatrixOne 是 **MySQL 兼容**，所以不是 DBOS 的即插后端，但**模型完全可在 MatrixOne 复刻**。
- **Temporal** 需要一个 DB 后端（Cassandra/MySQL/PostgreSQL），且持久层常是瓶颈、需要为高写事件追加调优。MatrixOne MySQL 兼容，理论上可连，但**不是受支持/调优的 Temporal 存储**，高写事件追加也非其强项——别当 Temporal 后端用。
- **Restate** 自带存储、不依赖外部 DB → MatrixOne 与它无后端关系。
- **MatrixOne 的独特加成**（相对把 durable 状态放在普通 DB）：① **git4data** 对 `wf_exec/wf_step` 快照/PITR/分支/时间旅行 → 原生「**时间旅行调试 + 可审计回放**」（对标 DBOS Cloud 的 time-traveler，但在 DB 层）；② 执行状态 + 业务数据 + 执行历史分析**统一在一处、同事务**。
- **缺口（要补的）**：无 durable-execution 编程模型/SDK（@workflow/@step）与确定性重放、无 per-instance timer / signals / queues / virtual objects、无 worker 调度/版本化/限流/visibility UI。要么自建薄库（DBOS 式，如本实验），要么在其上加一层引擎。
- **更正（内置调度）**：MatrixOne **有内置 `CREATE TASK` 定时任务，且 v3.0.11 实测即可用**——cron（6 段含秒）+ `WHEN` 门禁 + `RETRY` + `TIMEOUT`，单实例串行，`SHOW TASK RUNS` 看历史。这意味着「自建 durable timer / 轮询 durable-queue 表」**不需要外部调度器**，用库内 `CREATE TASK` 每秒/每分轮询触发到期项即可。（坑：`AS BEGIN … END` 体内有分号，标准 mysql CLI 会在第一个 `;` 处截断、误报语法错——需 `DELIMITER` 切换或用驱动如 pymysql 整条发送。本项目最初误判"不支持"正是踩了这个坑。）

**选型**：要开箱即用 → **Temporal**（成熟/大规模）、**DBOS**（最小运维、已在 Postgres）、**Restate**（单二进制/优雅）；要「贴着已有数据库自建、执行状态与业务/分析/版本化统一在一处、还想要原生时间旅行审计」→ **MatrixOne 作后端（DBOS-on-MatrixOne）** 很合理。
