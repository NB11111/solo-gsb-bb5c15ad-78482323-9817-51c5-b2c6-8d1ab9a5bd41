#!/usr/bin/env python3
"""生成两份模拟飞行日志 CSV，用于演示与测试。

sample_flight_1.csv：正常飞行（静置 -> 放电 -> 静置 -> 充电 -> 静置）
sample_flight_2.csv：老化电池，容量衰减、发热偏高，且日志在放电中截断（未闭合循环）

用法：python3 scripts/generate_sample.py  （输出到 sample_data/）
"""
import csv
import math
import os
import random
from datetime import datetime, timedelta, timezone

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "sample_data")


def ocv(soc: float) -> float:
    """简化开路电压曲线（电池包 6S 量级，V）。"""
    v = 19.6 + 5.4 * soc
    if soc < 0.15:                      # 低电量拐点
        v -= (0.15 - soc) * 12.0
    return v


def simulate(path: str, start: datetime, capacity_ah: float, ir_ohm: float,
             discharge_a: float, charge_a: float, ambient: float,
             heat_gain: float, cutoff_soc: float, unclosed_tail: bool,
             seed: int) -> None:
    rng = random.Random(seed)
    rows = []
    t = 0.0
    soc = 1.0
    temp = ambient + 1.0

    def emit(i: float, dt: float = 1.0) -> None:
        nonlocal t, soc, temp
        soc -= i * dt / 3600.0 / capacity_ah
        soc = min(max(soc, 0.0), 1.0)
        v = ocv(soc) - i * ir_ohm + rng.gauss(0, 0.02)
        temp += (i * i * ir_ohm * heat_gain - (temp - ambient) * 0.004) * dt
        rows.append((start + timedelta(seconds=t),
                     round(v, 3), round(i + rng.gauss(0, 0.02), 3),
                     round(temp + rng.gauss(0, 0.1), 2)))
        t += dt

    def run(seconds: float, current_fn) -> None:
        elapsed = 0.0
        while elapsed < seconds:
            emit(current_fn(elapsed))
            elapsed += 1.0

    def run_until_soc(target: float, current_fn, discharging: bool) -> None:
        guard = 0
        while guard < 20000:
            if discharging and soc <= target:
                break
            if not discharging and soc >= target:
                break
            emit(current_fn(guard))
            guard += 1

    run(120, lambda _: 0.0)                                   # 起飞前置静置
    run_until_soc(cutoff_soc,                                 # 主放电段（放到截止电量）
                  lambda e: discharge_a + 6 * math.sin(e / 90.0)
                  + rng.uniform(-1, 1), discharging=True)
    if unclosed_tail:
        write_csv(path, rows)                                 # 放电中截断
        return
    run(180, lambda _: 0.0)                                   # 静置（电压回升）
    run_until_soc(0.99, lambda _: -charge_a, discharging=False)  # 充电段
    run(120, lambda _: 0.0)                                   # 充满静置
    write_csv(path, rows)


def write_csv(path: str, rows) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "voltage", "current", "temperature"])
        for ts, v, i, tp in rows:
            w.writerow([ts.isoformat(timespec="seconds"), v, i, tp])
    print(f"已生成 {path}（{len(rows)} 行）")


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    base = datetime(2026, 9, 20, 10, 0, 0, tzinfo=timezone.utc)
    simulate(os.path.join(OUT_DIR, "sample_flight_1.csv"), base,
             capacity_ah=2.2, ir_ohm=0.012, discharge_a=8.0, charge_a=5.0,
             ambient=25.0, heat_gain=0.02, cutoff_soc=0.15,
             unclosed_tail=False, seed=7)
    simulate(os.path.join(OUT_DIR, "sample_flight_2.csv"),
             base + timedelta(days=1),
             capacity_ah=1.8, ir_ohm=0.02, discharge_a=8.5, charge_a=5.0,
             ambient=28.0, heat_gain=0.05, cutoff_soc=0.10,
             unclosed_tail=True, seed=13)


if __name__ == "__main__":
    main()
