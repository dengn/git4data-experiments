"""[01] 验证 / 加载原始数据 → artifacts/raw/*.parquet

驻场工作流：
  1. 在客户 Oracle / MatrixOne 上跑 SQL。大表在库内先 JOIN 维表并 GROUP BY，
     只导出 pipeline 需要的 loop×day 聚合结果。
  2. SCP CSV 到开发容器，用 csv_to_parquet.py 转 parquet
  3. 把下面 parquet 放进 RAW_DIR：
       原表导出：
         - tb_daily_prodindex.parquet               ← label 源
       Oracle 端先聚合再导出：
         - agg_oee_loop_day.parquet                  ← (loop, op_day) OEE / down / PM
         - agg_lotstep_loop_day.parquet             ← (loop, op_day)
         - agg_wip_snapshot_loop_day.parquet         ← (loop, op_day) 8AM WIP
       可选保留：
         - summary_oee_new.parquet                  ← (factorydate, machine) 机台-天原表，归因用
  4. 跑本脚本验证文件齐备、行数合理
  5. 下游 02~08 继续消费 clean/features 产物

如果 RAW_DIR 还没就位 → 本脚本尝试 `from huahong_toolkit import mock_data`
兜底生成（仅个人开发机用）。toolkit 不在就直接退出，告诉你下一步做什么。
"""
from __future__ import annotations

import sys
import pathlib

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _config import RAW_DIR, START_DATE, END_DATE, PROJ_ROOT  # noqa: E402


REQUIRED_TABLES = [
    # label 源
    "tb_daily_prodindex",
    # Oracle 端聚合好的 loop×day 特征表
    "agg_lotstep_loop_day",
    # Oracle 端聚合好的 8AM WIP 快照（loop × op_day，同日特征不 lag）
    # 现场导出 SQL 见 Oracle数据导出SQL清单.md § 2.6
    "agg_wip_snapshot_loop_day",
]

# OEE 新旧两种输入二选一：
#   - agg_oee_loop_day：推荐，Oracle 端已按 machine->capability->loop 分摊到 loop×day
#   - summary_oee_new：旧版 fallback，Python 端只能聚到 op_day 后广播到所有 loop
OEE_TABLES = ["agg_oee_loop_day", "summary_oee_new"]

OPTIONAL_TABLES = [
    # Python pipeline 不直接依赖；导出来方便 02_inspect / 对账
    "tb_wip",
    "tb_product_list",
    "tb_product_flow_fullstep",
    "tb_product_stage_loop",
]

EXPECTED_TABLES = REQUIRED_TABLES + OEE_TABLES + OPTIONAL_TABLES


def print_table(name: str, suffix: str = "") -> None:
    p = RAW_DIR / f"{name}.parquet"
    df = pd.read_parquet(p)
    size_mb = p.stat().st_size / 1024 / 1024
    print(f"     ✅ {name:<28} {len(df):>12,} 行  {len(df.columns):>3} 列  "
          f"{size_mb:>7.1f} MB{suffix}")


def verify_existing() -> None:
    print(f"[01] 检查 RAW_DIR 已有 parquet 文件")
    print(f"     RAW_DIR: {RAW_DIR}")
    print()
    for name in REQUIRED_TABLES:
        print_table(name)

    oee_name = next((n for n in OEE_TABLES if (RAW_DIR / f"{n}.parquet").exists()), None)
    if oee_name == "agg_oee_loop_day":
        print_table(oee_name, "  OEE(loop×day)")
    elif oee_name == "summary_oee_new":
        print_table(oee_name, "  OEE legacy fallback")
        print("     ⚠️  未找到 agg_oee_loop_day，将回退旧 OEE 广播口径")

    for name in OPTIONAL_TABLES:
        p = RAW_DIR / f"{name}.parquet"
        if not p.exists():
            print(f"     ⏭️  {name:<28} optional, 未提供")
            continue
        df = pd.read_parquet(p)
        size_mb = p.stat().st_size / 1024 / 1024
        print(f"     ✅ {name:<28} {len(df):>12,} 行  {len(df.columns):>3} 列  "
              f"{size_mb:>7.1f} MB  optional")


def generate_mock() -> None:
    """兜底用 huahong_toolkit.mock_data 生成 3 张表（仅个人开发机用）。"""
    try:
        if str(PROJ_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJ_ROOT))
        from huahong_toolkit import mock_data  # noqa: E402
    except ImportError as e:
        print(f"[01] ❌ RAW_DIR 里没有真实 parquet 文件，也找不到 huahong_toolkit 用于 mock。")
        print(f"     期望文件：")
        for n in EXPECTED_TABLES:
            print(f"       {RAW_DIR / f'{n}.parquet'}")
        print()
        print(f"     驻场流程：用 csv_to_parquet.py 转 CSV 后放到 RAW_DIR，再重跑本脚本。")
        print(f"     原始 ImportError：{e}")
        sys.exit(1)

    print(f"[01] RAW_DIR 不全，使用 mock_data 生成（仅开发机）")
    print(f"     时间范围: {START_DATE} ~ {END_DATE}")
    print()

    data = mock_data.generate_all(
        start_date=START_DATE,
        end_date=END_DATE,
        seed=42,
    )
    for name, df in data.items():
        out = RAW_DIR / f"{name}.parquet"
        df.to_parquet(out, index=False)
        print(f"     {name:<28} {len(df):>12,} 行  {len(df.columns):>3} 列  → {out.name}")


def main() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    required_present = all((RAW_DIR / f"{n}.parquet").exists() for n in REQUIRED_TABLES)
    oee_present = any((RAW_DIR / f"{n}.parquet").exists() for n in OEE_TABLES)
    all_present = required_present and oee_present
    if all_present:
        verify_existing()
    elif not list(RAW_DIR.glob("*.parquet")):
        generate_mock()
    else:
        missing = [n for n in REQUIRED_TABLES
                   if not (RAW_DIR / f"{n}.parquet").exists()]
        if not oee_present:
            missing.append("agg_oee_loop_day 或 summary_oee_new")
        print(f"[01] ❌ RAW_DIR 里已有部分 parquet，但缺少必需表。")
        print(f"     为避免把客户数据误覆盖成 mock，本脚本不会自动生成。")
        print(f"     缺少：")
        for n in missing:
            print(f"       {RAW_DIR / f'{n}.parquet'}")
        sys.exit(1)


if __name__ == "__main__":
    main()
