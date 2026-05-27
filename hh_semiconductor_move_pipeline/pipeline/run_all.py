"""一键串跑 01 ~ 07 全套 pipeline。

用 subprocess 顺序调用，任何一步非零退出就中断。
"""
from __future__ import annotations

import subprocess
import sys
import time
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
PROJ_ROOT = HERE.parents[1]

SCRIPTS = [
    "01_load.py",
    "02_inspect.py",
    "03_clean.py",
    "04_aggregate.py",
    "05_features.py",
    "06_baselines.py",
    "07_train.py",
    "08_predict.py",
    "09_explain.py",
]


def main() -> None:
    t0 = time.time()
    for s in SCRIPTS:
        print()
        print("=" * 72)
        print(f">>> {s}")
        print("=" * 72)
        t = time.time()
        r = subprocess.run([sys.executable, str(HERE / s)], cwd=PROJ_ROOT)
        if r.returncode != 0:
            print(f"\n❌ {s} 失败，退出码 {r.returncode}")
            sys.exit(r.returncode)
        print(f"\n     ⏱  {s} 耗时 {time.time()-t:.1f}s")

    print()
    print("=" * 72)
    print(f"✅ Pipeline 全部跑完，总耗时 {time.time()-t0:.1f}s")
    print("=" * 72)


if __name__ == "__main__":
    main()
