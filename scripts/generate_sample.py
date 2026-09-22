#!/usr/bin/env python3
"""生成两份模拟飞行日志 CSV，用于演示与测试。

sample_flight_1.csv：正常飞行（静置 -> 放电 -> 静置 -> 充电 -> 静置）
    6S 单体列（英文表头 cell1_voltage…），单体一致性良好；放电段插入一次
    约 2s 的压差尖峰（< 5s，不告警）。
sample_flight_2.csv：老化电池，容量衰减、发热偏高，日志在放电中截断（未闭合循环）；
    6S 单体列使用中文表头（单体1电压…），cell4 持续偏弱导致长区间压差 >0.10V；
    放电中段有 3s 采样断点（区间中断为两段），静置段另有单体缺值行与列数漂移行。

用法：python3 scripts/generate_sample.py  （输出到 sample_data/）
"""
import csv
import math
import os
import random
from datetime import datetime, timedelta, timezone

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "sample_data")

N_CELLS = 6


def ocv(soc: float) -> float:
    """简化开路电压曲线（电池包 6S 量级，V）。"""
    v = 19.6 + 5.4 * soc
    if soc < 0.15:                      # 低电量拐点
        v -= (0.15 - soc) * 12.0
    return v


def simulate(path: str, start: datetime, capacity_ah: float, ir_ohm: float,
             discharge_a: float, charge_a: float, ambient: float,
             heat_gain: float, cutoff_soc: float, unclosed_tail: bool,
             chinese_headers: bool, weak_cell: bool, seed: int,
             spike_t: float | None = None, gap_after_t: float | None = None,
             missing_cell_t: float | None = None,
             drift_t: float | None = None) -> None:
    rng = random.Random(seed)
    rows = []
    t = 0.0
    soc = 1.0
    temp = ambient + 1.0
    weak_drop = [0.0] * N_CELLS

    def cell_voltages(pack_v: float, now: float) -> list[float]:
        """由总压构造 6 个单体电压；weak_cell 时 4 号单体持续偏低。"""
        base = pack_v / N_CELLS
        noise = [rng.gauss(0, 0.004) for _ in range(N_CELLS)]
        if weak_cell:
            # 放电拉载时弱势单体额外跌落（静置时部分恢复）
            load = min(1.0, abs(rows[-1][2]) / max(discharge_a, 1e-9)) \
                if rows else 0.0
            target = -0.028 - 0.10 * load
            weak_drop[3] += (target - weak_drop[3]) * 0.2
        vals = [base + d for d in weak_drop]
        vals = [v + n for v, n in zip(vals, noise)]
        if spike_t is not None and abs(now - spike_t) < 0.6:
            vals[3] -= 0.15              # 短暂压差尖峰（约 2s，不应告警）
        # 校正使单体之和与总压一致（残差均摊，避免人为放大压差）
        residual = pack_v - sum(vals)
        vals = [v + residual / N_CELLS for v in vals]
        return [round(v, 4) for v in vals]

    def emit(i: float, dt: float = 1.0) -> None:
        nonlocal t, soc, temp
        soc -= i * dt / 3600.0 / capacity_ah
        soc = min(max(soc, 0.0), 1.0)
        v = ocv(soc) - i * ir_ohm + rng.gauss(0, 0.02)
        temp += (i * i * ir_ohm * heat_gain - (temp - ambient) * 0.004) * dt
        cells = cell_voltages(v, t)
        rows.append((start + timedelta(seconds=t),
                     round(v, 3), round(i + rng.gauss(0, 0.02), 3),
                     round(temp + rng.gauss(0, 0.1), 2), cells))
        t += dt
        if gap_after_t is not None and abs(t - gap_after_t) < 0.01:
            t += 3.0                    # 采样断点：>2s，连续超差区间须中断

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
        write_csv(path, rows, chinese_headers, missing_cell_t, drift_t)
        return
    run(180, lambda _: 0.0)                                   # 静置（电压回升）
    run_until_soc(0.99, lambda _: -charge_a, discharging=False)  # 充电段
    run(120, lambda _: 0.0)                                   # 充满静置
    write_csv(path, rows, chinese_headers, missing_cell_t, drift_t)


def write_csv(path: str, rows, chinese_headers: bool,
              missing_cell_t: float | None, drift_t: float | None) -> None:
    if chinese_headers:
        header = ["时间", "电压", "电流", "温度"] + \
                 [f"单体{n}电压" for n in range(1, N_CELLS + 1)]
    else:
        header = ["time", "voltage", "current", "temperature"] + \
                 [f"cell{n}_voltage" for n in range(1, N_CELLS + 1)]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for ts, v, i, tp, cells in rows:
            out = [ts.isoformat(timespec="seconds"), v, i, tp]
            # 单体缺值行：基础四列保留，单体分析跳过该行并记录原因
            if missing_cell_t is not None and abs((ts - rows[0][0]).total_seconds()
                                                   - missing_cell_t) < 0.6:
                w.writerow(out + cells[:2] + [""] + cells[3:])
                continue
            # 单体数量漂移行：多出一列，单体分析跳过该行
            if drift_t is not None and abs((ts - rows[0][0]).total_seconds()
                                           - drift_t) < 0.6:
                w.writerow(out + cells + ["9.9"])
                continue
            w.writerow(out + cells)
    print(f"已生成 {path}（{len(rows)} 行，{N_CELLS} 个单体列）")


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    base = datetime(2026, 9, 20, 10, 0, 0, tzinfo=timezone.utc)
    simulate(os.path.join(OUT_DIR, "sample_flight_1.csv"), base,
             capacity_ah=2.2, ir_ohm=0.012, discharge_a=8.0, charge_a=5.0,
             ambient=25.0, heat_gain=0.02, cutoff_soc=0.15,
             unclosed_tail=False, chinese_headers=False, weak_cell=False,
             seed=7, spike_t=300.0)
    # t=120s 进入放电；弱势单体持续偏低，放电中段 t≈430s 插入 3s 采样断点，
    # 前置静置 t=60s / 90s 分别放置单体缺值行与列数漂移行（均仅跳过单体分析）。
    simulate(os.path.join(OUT_DIR, "sample_flight_2.csv"),
             base + timedelta(days=1),
             capacity_ah=1.8, ir_ohm=0.02, discharge_a=8.5, charge_a=5.0,
             ambient=28.0, heat_gain=0.05, cutoff_soc=0.10,
             unclosed_tail=True, chinese_headers=True, weak_cell=True,
             seed=13, gap_after_t=430.0, missing_cell_t=60.0, drift_t=90.0)


if __name__ == "__main__":
    main()
