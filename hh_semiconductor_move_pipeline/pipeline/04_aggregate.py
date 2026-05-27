"""[04] 聚合 + 合并真实表 → artifacts/features/base.parquet

目标粒度：loop × 工厂日（全厂跨 fab 求和）

⚠️ 2026-05-15 决策：彻底去掉 fab 维度
  原因：客户 tb_product_list.fabid 全 null，prodindex.fabid 也部分混淆。
        客户验收口径是全厂 daily move，不要求 FAB6 / FAB8 分别报数，所以
        pipeline 一律跨 fab 求和，训练 loop × op_day。

⚠️ 2026-05-18 决策：客户给了新 label SQL，两件大事
  ① MOVE 列从 12 → 26（N20 12 个 + 标准 8 个 + BSI 6 个）
  ② 不再 join tb_product_list、不再做 tech 白名单过滤
  ③ step1b 整段删除：客户明确说 prodindex 的非-MOVE 字段都不适合做特征

3 步走（对应 3 张表 / 中间表；tb_wip 一期不进训练，见下）：

  Step 1   tb_daily_prodindex → label (y, 来自 LOOP_COLUMN_MAP unpivot)
                                at loop × op_day
                              复刻客户 2026-05-18 给的 label SQL：
                                productiontype='Production'
                                GROUP BY histdate
                              （不再 join product_list，不再 tech 过滤）

  Step 3   agg_oee_loop_day  → loop×op_day 级机台稼动 / down / OEE
                                Oracle 端用 summary_oee_new + dspcapabilitywph
                                + product_flow_fullstep 已经把机台 OEE 分摊
                                到 loop；Python 端只校验和 merge。

  Step 4   agg_lotstep_loop_day
                              → loop×op_day 事件流总量特征
                                Oracle 端已提前补好 loop

⚠️ Step 2 (tb_wip) 已禁用 ——
  2026-05-15 驻场实测确认 tb_wip 是**当前活跃 lot 的瞬时快照**（4 万行，
  updatetime 全是今天，每 lot 一行），**不是历史快照表**。
  → 训练用不上；WIP 历史信号先改走 lotstephistory 衍生的 loop 级 ls_* 列
  → tb_wip 仍保留导出（给二期推理用），但训练 pipeline 完全不依赖它
  → step2_wip() 函数保留但 main() 不再调用，方便二期推理脚本复用

⚠️ Step 1b (prodindex 派生特征) 部分恢复 ——
  2026-05-18 删过；2026-05-19 客户更新：prodindex 里的 qt / ht 字段要回来，
  作为 loop 级特征（命名规则：把 LOOP_COLUMN_MAP 里 move 列名中的 "move"
  替换成 "qt"/"ht" 就是该 loop 当天 QT/HT）。
  其他非-MOVE 字段（waferstart/boh/eoh/scrap/rework/priority/qhcount）仍然不要。
  → 新增 prod_qt / prod_ht 两列特征

设计要点：
  - 时间口径：所有 DATE 字段已是工厂日（客户 2026-05-11 确认）
  - Python 只消费已经聚好的 loop×op_day 中间表，不在本地对大表做维表 join
"""
from __future__ import annotations

import sys
import pathlib

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _config import (  # noqa: E402
    CLEAN_DIR,
    FEATURES_DIR,
    LOOP_COLUMN_MAP,
    PRODUCTIONTYPE_FILTER,
)


def step1_label() -> pd.DataFrame:
    """tb_daily_prodindex → loop × op_day 标签

    复刻客户 2026-05-18 给出的最终 label SQL（不区分 fab，跨 fab 求和）：
        SELECT histdate,
               sum(n20_move1) saqp, ..., sum(n20_move12) passn20,
               sum(move1), ..., sum(move8),
               sum(bsi_move1), ..., sum(bsi_move6)
        FROM   mfgcim.TB_DAILY_PRODINDEX A
        WHERE  A.productiontype = 'Production'
        GROUP BY histdate

    跟客户 SQL 的差异：
      - 客户输出 26 个 loop 一列一行（横宽），我们 unpivot 成长表
      - GROUP BY (loop, histdate)，跨 fab / 跨 product 求和（客户 SQL 只
        group by histdate，我们多分了个 loop 维度因为 loop 是预测粒度）

    跟旧版（2026-05-15 之前）的差异：
      - ❌ 不再 join tb_product_list（客户新 SQL 没这一步）
      - ❌ 不再做 tech 白名单过滤
      - ✅ MOVE 列从 12 个扩到 26 个（N20 12 + 标准 8 + BSI 6）

    LOOP_COLUMN_MAP 列匹配策略：
      - 用 LOOP_COLUMN_MAP 跟实际表列取交集，找到几个用几个
      - 全部找不到才报错（说明现场字段命名彻底对不上）
      - 部分找不到只打 warning（mock 上只有 move1..move4 是正常的）
    """
    prod = pd.read_parquet(CLEAN_DIR / "tb_daily_prodindex.parquet")

    n0 = len(prod)

    # 1) productiontype 过滤（唯一保留的过滤条件）
    prod = prod[prod["productiontype"] == PRODUCTIONTYPE_FILTER]
    n1 = len(prod)

    # 2) unpivot LOOP_COLUMN_MAP 配置的 MOVE 宽列 → 长表
    #    用"列名 intersect"逻辑：mock 上自动只取 move1..move4，
    #    现场上自动启用全 26 列
    loop_cols = [c for c in LOOP_COLUMN_MAP if c in prod.columns]
    missing = [c for c in LOOP_COLUMN_MAP if c not in prod.columns]
    if not loop_cols:
        # 一个都没匹配上：说明 LOOP_COLUMN_MAP 跟现场表彻底对不上
        raise RuntimeError(
            f"[step1_label] LOOP_COLUMN_MAP 里没有任何列在 prodindex 中存在！\n"
            f"           prodindex 里的可用列：{[c for c in prod.columns if 'move' in c.lower()]}\n"
            f"           检查 _config.py:LOOP_COLUMN_MAP 是否跟客户实际字段对齐"
        )
    if missing:
        # 部分缺列：mock 上正常（只有 4 列），打 info 提示
        print(f"     [Step1] LOOP_COLUMN_MAP 有 {len(missing)} 个列在表中不存在 "
              f"(mock 数据正常；现场需确认): {missing[:5]}{'...' if len(missing) > 5 else ''}")

    long = prod.melt(
        id_vars=["histdate"],
        value_vars=loop_cols,
        var_name="move_col",
        value_name="y",
    )
    long["loop"] = long["move_col"].map(LOOP_COLUMN_MAP)
    long["loop"] = _normalize_loop_key(long["loop"])

    # 3) 跨 fab / productname / productiontype 在同一 (loop, day) 求和
    label = (
        long.groupby(["loop", "histdate"], observed=True, as_index=False)
            ["y"].sum()
            .rename(columns={"histdate": "op_day"})
    )
    label["op_day"] = pd.to_datetime(label["op_day"])

    print(f"     [Step1] label              {len(label):>10,} 行  "
          f"(loop × op_day, 启用 {len(loop_cols)} 个 loop)")
    print(f"             prodindex {n0:,} → 过 productiontype {n1:,} → unpivot/agg")
    return label


# ⚠️ [PARTIAL REVIVE 2026-05-19] step1b_prodindex_qt_ht()
#   2026-05-18 删过 step1b（客户说非-MOVE 字段不适合做特征）；2026-05-19 客户
#   口径更新：prodindex 里的 qt / ht 字段要回来，作为 loop 级特征。
#   其他非-MOVE 字段（waferstart / boh / eoh / scrap / rework / priority / qhcount）
#   仍然不要。
def step1b_prodindex_qt_ht() -> pd.DataFrame:
    """tb_daily_prodindex → loop × op_day 的 prod_qt / prod_ht 特征

    命名约定（跟 step1_label 的 LOOP_COLUMN_MAP 对齐）：把 move 列名里的
    "move" 替换成 "qt" / "ht" 就是该 loop 当天的 QT / HT 列。
        n20_move1 (SAQP)  ↔ n20_qt1, n20_ht1
        move9     (loop9) ↔ qt9,     ht9
        bsi_move1 (bsi_loop1) ↔ bsi_qt1, bsi_ht1

    跨 fab / productname / productiontype 在同一 (loop, op_day) 取均值
    （qt / ht 是时间类指标，跨产品 sum 没物理意义）。
    """
    prod = pd.read_parquet(CLEAN_DIR / "tb_daily_prodindex.parquet")
    prod = prod[prod["productiontype"] == PRODUCTIONTYPE_FILTER]

    parts = []
    matched_loops, missing_loops = [], []
    for move_col, loop_name in LOOP_COLUMN_MAP.items():
        qt_col = move_col.replace("move", "qt")
        ht_col = move_col.replace("move", "ht")
        cols = ["histdate"]
        rename = {"histdate": "op_day"}
        if qt_col in prod.columns:
            cols.append(qt_col)
            rename[qt_col] = "prod_qt"
        if ht_col in prod.columns:
            cols.append(ht_col)
            rename[ht_col] = "prod_ht"
        if len(cols) == 1:
            missing_loops.append(loop_name)
            continue
        sub = prod[cols].rename(columns=rename).copy()
        sub["loop"] = loop_name
        parts.append(sub)
        matched_loops.append(loop_name)

    if not parts:
        print(f"     [Step1b] prodindex 里没找到任何 qt/ht 列，返回空表")
        return pd.DataFrame(columns=["loop", "op_day", "prod_qt", "prod_ht"])

    long = pd.concat(parts, ignore_index=True)
    long["loop"] = _normalize_loop_key(long["loop"])
    long["op_day"] = pd.to_datetime(long["op_day"])

    agg_map = {c: "mean" for c in ("prod_qt", "prod_ht") if c in long.columns}
    out = (
        long.groupby(["loop", "op_day"], observed=True, as_index=False)
            .agg(agg_map)
    )
    print(f"     [Step1b] prod_qt_ht         {len(out):>10,} 行  "
          f"(loop × op_day, 启用 {len(matched_loops)} 个 loop"
          f"{f', 缺 qt/ht 的 loop: {missing_loops[:3]}...' if missing_loops else ''})")
    return out


def step2_wip() -> pd.DataFrame:
    """[DEPRECATED for training pipeline] tb_wip → FAB × loop × op_day

    2026-05-15 实测：客户 tb_wip 是当前活跃 lot 的瞬时快照（~4 万行，
    updatetime 全部集中在同一天），**不是历史快照**。所以这个函数对训练
    用例返回的结果只有"今天"一行有意义，其他日期全 NaN，merge 进 base
    会浪费列空间还容易引入泄露。

    保留函数本体的原因：
      - 二期上线时，推理脚本可以复用这个聚合逻辑，把"今天 t=T 08:00 的
        WIP 状态"作为一行实时特征注入到当日预测样本
      - 字段语义不会变（lotname / qty / loopname / qt / rt 都还是那些），
        逻辑搬过去就能用

    一期 main() 不调用本函数。
    """
    wip = pd.read_parquet(CLEAN_DIR / "tb_wip.parquet")
    wip["op_day"] = pd.to_datetime(pd.to_datetime(wip["updatetime"]).dt.date)

    daily = (
        wip.groupby(["fabid", "loopname", "op_day"], observed=True, as_index=False)
           .agg(wip_total=("qty", "sum"),
                wip_n_lots=("lotname", "count"),
                avg_qt=("qt", "mean"),
                avg_rt=("rt", "mean"))
           .rename(columns={"fabid": "fab_id", "loopname": "loop"})
    )
    print(f"     [Step2] wip_daily          {len(daily):>10,} 行  "
          f"(FAB × loop × op_day) [DEPRECATED, 仅二期推理用]")
    return daily


def step3_oee_day_legacy() -> pd.DataFrame:
    """[fallback] summary_oee_new (raw, machine-day) → op_day 级 OEE。

    仅用于本地旧 mock 或现场暂未导出 agg_oee_loop_day 的情况。
    该口径会把同一天的 OEE 广播到所有 loop，不再作为推荐口径。

    聚合规则：
      - 小时类（down/pm/idle/avai/run/hold_*/wait_*）→ sum 跨机台合计
      - 速率类（uptime_rate/oee_rate/eff_rate/prod_rate/qtime/
        uptime_rate_ie/pwph/effi/actualwph/targetwph）→ mean 简单跨机台平均
      - move 类（move/prdmove/engmove/fab6_*/fab8_*）→ sum 跨机台合计
        （对账用，不进训练特征——见 _config.EXOG_BASE_COLS 的注释）
    """
    oee = pd.read_parquet(CLEAN_DIR / "summary_oee_new.parquet")
    oee["op_day"] = pd.to_datetime(oee["factorydate"])

    sum_cols = [
        "down", "pm", "idle",
        "avai", "run",
        "hold_mfg", "wait_mfg",
        "hold_ee", "hold_pe", "wait_ee", "wait_pe",
    ]
    mean_cols = [
        "uptime_rate", "oee_rate", "eff_rate", "prod_rate",
        "qtime", "uptime_rate_ie", "pwph", "effi",
        "actualwph", "targetwph",
    ]
    # 对账列：聚出来保留在 base.parquet 跟 label 对账，但 _config 不放进训练特征
    audit_sum_cols = [
        "move", "prdmove", "engmove",
        "fab6_prodmove", "fab8_prodmove",
        "fab6_engmove", "fab8_engmove",
    ]

    agg = {c: "sum" for c in sum_cols + audit_sum_cols if c in oee.columns}
    agg.update({c: "mean" for c in mean_cols if c in oee.columns})

    out = (
        oee.groupby("op_day", as_index=False)
           .agg(agg)
    )

    # 列名 rename → 跟 _config.EXOG_BASE_COLS 约定的字段名对齐
    out = out.rename(columns={
        "down":      "down_hours",
        "pm":        "pm_hours",
        "idle":      "idle_hours",
        "avai":      "total_avai",
        "run":       "total_run",
        "hold_mfg":  "total_hold_mfg",
        "wait_mfg":  "total_wait_mfg",
        "hold_ee":   "total_hold_ee",
        "hold_pe":   "total_hold_pe",
        "wait_ee":   "total_wait_ee",
        "wait_pe":   "total_wait_pe",
        "uptime_rate":    "mean_uptime_rate",
        "oee_rate":       "mean_oee_rate",
        "eff_rate":       "mean_eff_rate",
        "prod_rate":      "mean_prod_rate",
        "qtime":          "mean_qtime",
        "uptime_rate_ie": "mean_uptime_rate_ie",
        "pwph":           "mean_pwph",
        "effi":           "mean_effi",
        "actualwph":      "mean_actualwph",
        "targetwph":      "mean_targetwph",
        "move":           "total_machine_move",
        "prdmove":        "total_prdmove",
        "engmove":        "total_engmove",
        "fab6_prodmove":  "total_fab6_prodmove",
        "fab8_prodmove":  "total_fab8_prodmove",
        "fab6_engmove":   "total_fab6_engmove",
        "fab8_engmove":   "total_fab8_engmove",
    })

    print(f"     [Step3] oee_day            {len(out):>10,} 行  "
          f"(legacy: op_day, merge 时广播到所有 loop)")
    return out


def step3_oee_loop_day() -> pd.DataFrame:
    """agg_oee_loop_day → loop × op_day 级 OEE 特征。

    推荐现场口径：Oracle 端已经用
      summary_oee_new.MACHINENAME
        -> tb_dsp_dspcapabilitywph.CAPABILITY
        -> tb_product_flow_fullstep.LOOPNAME
    把机台 OEE 分摊到 loop。Python 不再做机台/能力/flow join，只消费
    聚合结果，避免把全厂 OEE 广播到所有 loop。
    """
    path = CLEAN_DIR / "agg_oee_loop_day.parquet"
    if not path.exists():
        print("     ⚠️  agg_oee_loop_day.parquet 不存在，回退旧 summary_oee_new 广播口径")
        return step3_oee_day_legacy()

    oee = pd.read_parquet(path).copy()
    oee.columns = [str(c).strip().lower() for c in oee.columns]
    oee = oee.rename(columns={
        "factorydate": "op_day",
        "histdate": "op_day",
        "loopname": "loop",
    })

    need = {"op_day", "loop"}
    missing = need - set(oee.columns)
    if missing:
        raise RuntimeError(f"[Step3] agg_oee_loop_day 缺少字段: {sorted(missing)}")

    oee["op_day"] = pd.to_datetime(oee["op_day"])
    oee["loop"] = _normalize_loop_key(oee["loop"])

    dup = int(oee.duplicated(["loop", "op_day"]).sum())
    if dup:
        sample = oee.loc[oee.duplicated(["loop", "op_day"], keep=False),
                         ["loop", "op_day"]].head(10)
        raise RuntimeError(
            f"[Step3] agg_oee_loop_day 不是唯一 loop×op_day，重复 {dup} 行，"
            f"样例:\n{sample}"
        )

    for col in [c for c in oee.columns if c not in ("loop", "op_day")]:
        oee[col] = pd.to_numeric(oee[col], errors="coerce")

    oee = oee.sort_values(["loop", "op_day"]).reset_index(drop=True)
    print(f"     [Step3] oee_loop_day       {len(oee):>10,} 行  "
          f"(loop × op_day, Oracle 端已按机台能力分摊)")
    return oee


def _normalize_loop_key(s: pd.Series) -> pd.Series:
    """loop join key 统一大小写，避免 loop1 / Loop1 / LOOP1 merge 不上。"""
    return s.astype("string").str.strip().str.upper()


def _standardize_loop_col(df: pd.DataFrame, source: str) -> pd.DataFrame:
    """统一 loop 列名和值；没有 loop 的行不能进入 loop 粒度训练特征。"""
    out = df.copy()
    if "loop" not in out.columns and "loopname" in out.columns:
        out = out.rename(columns={"loopname": "loop"})
    if "loop" not in out.columns:
        raise RuntimeError(f"[{source}] 缺少 loop/loopname 字段，检查 Oracle 聚合 SQL")

    out["loop"] = _normalize_loop_key(out["loop"])
    missing = out["loop"].isna() | (out["loop"] == "")
    if missing.any():
        print(f"     ⚠️ {source} 有 {int(missing.sum()):,} 行没有匹配到 loop，已丢弃")
        out = out.loc[~missing].copy()
    return out


def _mark_source(df: pd.DataFrame, marker_col: str) -> pd.DataFrame:
    """给非空源表加 merge 覆盖标记，避免 join 失败被 fillna 静默吞掉。"""
    if df.empty:
        return df
    out = df.copy()
    out[marker_col] = 1
    return out


def _date_range(df: pd.DataFrame) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    if df.empty:
        return None
    return df["op_day"].min(), df["op_day"].max()


def _check_source_coverage(base: pd.DataFrame, marker_col: str, source: str) -> None:
    if marker_col not in base.columns:
        return
    missing = int(base[marker_col].isna().sum())
    total = len(base)
    coverage = 100.0 * (total - missing) / total if total else 0.0
    print(f"     [main] {source:<12} merge 覆盖率 {coverage:6.2f}% "
          f"({total - missing:,}/{total:,})")
    if missing == total:
        raise RuntimeError(f"[04] {source} 完全没有 merge 上，请检查日期/loop join key")
    if missing > 0:
        sample = base.loc[base[marker_col].isna(), ["loop", "op_day"]].head(5)
        print(f"     ⚠️ {source} 有 {missing:,} 行未 merge 上，将按缺失值规则填充。样例:")
        print(sample.to_string(index=False).replace("\n", "\n     "))


def step4_lotstep_loop() -> pd.DataFrame:
    """agg_lotstep_loop_day → loop × op_day 事件流总量特征

    Oracle 端已经把 lotstephistory 补齐 loop，再按 (loop, op_day) 聚合好。
    本地只做一次兜底 groupby，防止分批文件 concat 后出现重复 key。

    聚合策略：
      - 计数 / 总量类（n_events / n_lots / total_move）→ sum
      - 均值类（avg_qt / avg_rt）→ mean。正常情况下 Oracle 端已经聚好，
        这里的 groupby 只做重复 key 兜底
      - n_products 跨分批重复时取 max，避免重复计数同 product
    """
    df = pd.read_parquet(CLEAN_DIR / "agg_lotstep_loop_day.parquet")
    df = _standardize_loop_col(df, "agg_lotstep_loop_day")
    df["op_day"] = pd.to_datetime(df["op_day"])

    # 列名加 ls_ 前缀避免和其他表的列冲突
    df = df.rename(columns={
        "n_events_total":     "ls_n_events",
        "n_lots_total":       "ls_n_lots",
        "total_move_all_cap": "ls_total_move",
        "avg_qt_all_cap":     "ls_avg_qt",
        "avg_rt_all_cap":     "ls_avg_rt",
        "n_products":         "ls_n_products",
    })

    sum_cols = ["ls_n_events", "ls_n_lots", "ls_total_move"]
    mean_cols = ["ls_avg_qt", "ls_avg_rt"]
    max_cols = ["ls_n_products"]   # 分批重复时避免重复算同一个 product

    agg = {c: "sum"  for c in sum_cols  if c in df.columns}
    agg.update({c: "mean" for c in mean_cols if c in df.columns})
    agg.update({c: "max"  for c in max_cols  if c in df.columns})

    out = (
        df.groupby(["loop", "op_day"], observed=True, as_index=False)
          .agg(agg)
    )
    print(f"     [Step4] lotstep_loop_daily {len(out):>10,} 行  (loop × op_day)")
    return out


def step5_wip_snapshot() -> pd.DataFrame:
    """8AM WIP 快照 → (loop, op_day) 同日特征。

    Oracle 端已经把 tb_wip_hist join stage_loop 后聚合到 (loop, op_day) + 14 个
    wip8_* 特征列。本地不需要再做 join / agg —— 直接读 + 规范 loop 列名即可。

    口径：D 日 op_day 对齐 D 日 08:00 拍的快照，不 lag（见 _config.WIP_SAMEDAY_FEATURES）。
    SQL 定义见 Oracle数据导出SQL清单.md § 2.6。
    """
    df = pd.read_parquet(CLEAN_DIR / "agg_wip_snapshot_loop_day.parquet")
    df = _standardize_loop_col(df, "agg_wip_snapshot_loop_day")
    df["op_day"] = pd.to_datetime(df["op_day"])
    print(f"     [Step5] agg_wip_snap_day   {len(df):>10,} 行  (loop × op_day)")
    return df


def main() -> None:
    print("[04] 聚合 + 合并真实表")
    print()

    label        = step1_label()
    qt_ht        = step1b_prodindex_qt_ht()
    # step2_wip() 已禁用：tb_wip 是实时快照，不参与训练。WIP 信号走 lotstep_*
    oee          = step3_oee_loop_day()
    lotstep_loop = step4_lotstep_loop()
    wip_snap     = step5_wip_snapshot()

    qt_ht        = _mark_source(qt_ht, "_src_qt_ht")
    oee          = _mark_source(oee, "_src_oee")
    lotstep_loop = _mark_source(lotstep_loop, "_src_lotstep")
    wip_snap     = _mark_source(wip_snap, "_src_wip_snap")

    # LEFT JOIN 顺序：以 label (loop × op_day) 为底。
    # qt_ht 是 loop × op_day 级（跟 label 同源 prodindex，但取的是 qt/ht 列）。
    # OEE 是 loop × op_day 级，Oracle 端已经按机台能力/flow 分摊到 loop。
    # lotstep / wip_snap 是 loop × op_day 级，按 (loop, op_day) merge。
    base = (
        label
          .merge(qt_ht,        on=["loop", "op_day"], how="left")
          .merge(oee,          on=["loop", "op_day"], how="left")
          .merge(lotstep_loop, on=["loop", "op_day"], how="left")
          .merge(wip_snap,     on=["loop", "op_day"], how="left")
    )

    # 各表日期范围不一致时，只保留所有非空源表共同覆盖的日期窗口，
    # 防止前后缺口被下面 fillna 灌入假特征。
    # 现场各表 min_date 常有 1-3 周差（数据保留 / 流程切换日不同），mock 端各表
    # 同步从 START_DATE 开始，截断量通常为 0（no-op）。
    ranges = {
        "label": _date_range(label),
        "qt_ht": _date_range(qt_ht),
        "oee": _date_range(oee),
        "lotstep": _date_range(lotstep_loop),
        "wip_snap": _date_range(wip_snap),
    }
    ranges = {k: v for k, v in ranges.items() if v is not None}
    effective_start = max(v[0] for v in ranges.values())
    effective_end = min(v[1] for v in ranges.values())
    if effective_start > effective_end:
        raise RuntimeError(f"[04] 各源表没有共同日期窗口: {ranges}")

    date_mask = (base["op_day"] >= effective_start) & (base["op_day"] <= effective_end)
    n_dropped = int((~date_mask).sum())
    if n_dropped > 0:
        print(f"     [main] 截断 {n_dropped} 行 "
              f"(保留共同日期窗口 {effective_start.date()} ~ {effective_end.date()})")
        base = base[date_mask].reset_index(drop=True)

    _check_source_coverage(base, "_src_qt_ht", "qt_ht")
    _check_source_coverage(base, "_src_oee", "oee")
    _check_source_coverage(base, "_src_lotstep", "lotstep")
    _check_source_coverage(base, "_src_wip_snap", "wip_snap")
    base = base.drop(
        columns=[c for c in base.columns if c.startswith("_src_")],
        errors="ignore",
    )

    # fillna 分流：
    #   - 率/均值字段（mean_*）
    #     填 0 等于说"全厂稼动率=0 / 全厂 QT=0"，是物理不可能的假灾难日信号 ——
    #     模型会把数据起点的前几天当真实异常学。改填该列的全表中位数。
    #   - 计数 / 时长 / SUM 类字段：缺数据 = 0 时数 / 0 次数是合理的，继续填 0
    RATE_FIELD_PREFIXES = ("mean_",)
    feature_cols = [c for c in base.columns
                    if c not in ("loop", "op_day", "y")]
    for c in feature_cols:
        if c.startswith(RATE_FIELD_PREFIXES):
            median = base[c].median()
            base[c] = base[c].fillna(0 if pd.isna(median) else median)
        else:
            base[c] = base[c].fillna(0)

    base = base.sort_values(["loop", "op_day"]).reset_index(drop=True)

    print()
    print(f"     base                       {len(base):>10,} 行 × "
          f"{len(base.columns)} 列")
    print(f"     时间范围: {base['op_day'].min().date()} ~ "
          f"{base['op_day'].max().date()}")
    print(f"     字段: {list(base.columns)}")

    out = FEATURES_DIR / "base.parquet"
    base.to_parquet(out, index=False)
    print(f"\n     → {out}")


if __name__ == "__main__":
    main()
