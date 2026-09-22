"""电池循环分析核心算法。

- 三态状态机（REST / DISCHARGE / CHARGE）识别充放电循环边界，含噪声段合并
- 梯形库仑积分估算每循环容量与能量
- 利用负载切换时刻的电压跳变与静置段电压恢复估算直流内阻
- 依据额定容量、安全电压范围与温度阈值生成可解释的判定标记
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import NamedTuple, Optional

# ---- 可调阈值 ----
REST_CURRENT_RATIO = 0.03    # 静置电流阈值 = 峰值电流的 3%
REST_CURRENT_FLOOR_A = 0.05  # 静置电流阈值下限 (A)
MIN_CYCLE_S = 20.0           # 短于该时长的充放电段视为噪声
MIN_REST_S = 10.0            # 短于该时长的静置不切断循环
CAPACITY_DROP_RATIO = 0.10   # 相邻放电循环容量突降告警阈值
CAPACITY_DROP_CRIT = 0.25    # 容量突降严重阈值
TEMP_WARN_C = 45.0           # 温度告警 (°C)
TEMP_CRIT_C = 60.0           # 温度严重告警 (°C)
TEMP_RISE_WARN_C = 15.0      # 单循环温升告警 (°C)
VOLT_MARGIN_V = 0.02         # 电压越限判定余量 (V)
OVER_CAPACITY_RATIO = 1.10   # 放电容量超过额定该比例时提示额定容量可能偏低
CELL_SPREAD_THRESHOLD_V = 0.10  # 单体压差（最高-最低）超差阈值 (V)
CELL_SPREAD_MIN_S = 5.0         # 压差连续超差达该时长才判定单体失衡 (s)
CELL_SAMPLE_GAP_S = 2.0         # 相邻采样点间隔超过该值则中断连续区间 (s)


class Sample(NamedTuple):
    t: float      # 相对日志起点的秒
    v: float      # 电压 V
    i: float      # 电流 A，正=放电，负=充电
    temp: float   # 温度 °C
    cells: Optional[tuple[float, ...]] = None  # 各单体电压 V（可选列）


@dataclass
class Cycle:
    seq: int
    kind: str               # 'discharge' | 'charge'
    start_t: float
    end_t: float
    duration_s: float
    capacity_ah: float
    energy_wh: float
    avg_current_a: float
    min_voltage: float
    max_voltage: float
    max_temp_c: float
    temp_rise_c: float
    ir_mohm: Optional[float]
    closed: bool
    flags: list = field(default_factory=list)
    # ---- 单体（cell）电压分析；日志无单体列时以下字段为 None/空 ----
    cell_count: Optional[int] = None
    cell_rows_used: int = 0
    cell_rows_skipped: int = 0
    max_cell_spread_v: Optional[float] = None   # 循环内最大单体压差
    over_threshold_s: Optional[float] = None    # 压差超过阈值的累计时长
    cell_intervals: list = field(default_factory=list)  # 持续超差区间（含证据片段）

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("start_t", "end_t", "duration_s", "capacity_ah", "energy_wh",
                  "avg_current_a", "min_voltage", "max_voltage", "max_temp_c",
                  "temp_rise_c"):
            d[k] = round(d[k], 4)
        if d["ir_mohm"] is not None:
            d["ir_mohm"] = round(d["ir_mohm"], 2)
        if d["max_cell_spread_v"] is not None:
            d["max_cell_spread_v"] = round(d["max_cell_spread_v"], 4)
        if d["over_threshold_s"] is not None:
            d["over_threshold_s"] = round(d["over_threshold_s"], 3)
        for iv in d["cell_intervals"]:
            for k in ("start_t", "end_t", "duration_s", "max_spread_v",
                      "at_max_t"):
                iv[k] = round(iv[k], 3)
        return d


# ---------------------------------------------------------------- 状态机

def _classify(samples: list[Sample], thr: float) -> list[str]:
    states = []
    for s in samples:
        if s.i > thr:
            states.append("D")
        elif s.i < -thr:
            states.append("C")
        else:
            states.append("R")
    return states


def _segments(states: list[str]) -> list[list]:
    segs = []
    start = 0
    for k in range(1, len(states) + 1):
        if k == len(states) or states[k] != states[start]:
            segs.append([states[start], start, k - 1])
            start = k
    return segs


def _merge_segments(samples: list[Sample], segs: list[list],
                    min_cycle_s: float, min_rest_s: float) -> list[list]:
    # 1) 过短的充放电段视为噪声，归为静置
    out = []
    for kind, a, b in segs:
        dur = samples[b].t - samples[a].t
        if kind != "R" and dur < min_cycle_s:
            kind = "R"
        out.append([kind, a, b])
    # 合并相邻同状态段
    merged: list[list] = []
    for seg in out:
        if merged and merged[-1][0] == seg[0]:
            merged[-1][2] = seg[2]
        else:
            merged.append(seg)
    # 2) 两侧状态相同的过短静置段并入（飞行中瞬时掉电流不应切断循环）
    changed = True
    while changed:
        changed = False
        for k in range(1, len(merged) - 1):
            kind, a, b = merged[k]
            if kind == "R":
                dur = samples[b].t - samples[a].t
                if dur < min_rest_s and merged[k - 1][0] == merged[k + 1][0] != "R":
                    merged[k - 1][2] = merged[k + 1][2]
                    del merged[k:k + 2]
                    changed = True
                    break
    return merged


def _integrate(samples: list[Sample], a: int, b: int) -> tuple[float, float]:
    """梯形库仑/能量积分，返回 (Ah, Wh)。"""
    cap = 0.0
    energy = 0.0
    for k in range(a, b):
        dt = samples[k + 1].t - samples[k].t
        if dt <= 0:
            continue
        i0, i1 = abs(samples[k].i), abs(samples[k + 1].i)
        cap += 0.5 * (i0 + i1) * dt
        energy += 0.5 * (i0 * samples[k].v + i1 * samples[k + 1].v) * dt
    return cap / 3600.0, energy / 3600.0


def _estimate_ir(samples: list[Sample], a: int, b: int,
                 kind: str, thr: float) -> Optional[float]:
    """由负载加载/卸载瞬间的电压跳变估算直流内阻 (Ω)。

    放电：IR = (V静置 - V带载) / |I|；充电取相反符号。
    起点用段前静置电压与加载后电压，终点用卸载前电压与静置恢复电压。
    """
    ests = []
    pre = [s.v for s in samples[max(0, a - 8):a] if abs(s.i) <= thr]
    if pre:
        j = min(a + 3, b)
        load_i = abs(samples[j].i)
        if load_i > 3 * thr:
            dv = (sum(pre) / len(pre)) - samples[j].v
            if kind == "C":
                dv = -dv
            ir = dv / load_i
            if 0 < ir < 1.0:
                ests.append(ir)
    post = [s.v for s in samples[b + 1:min(len(samples), b + 9)] if abs(s.i) <= thr]
    if post:
        load_i = abs(samples[b].i)
        if load_i > 3 * thr:
            dv = (sum(post) / len(post)) - samples[b].v
            if kind == "C":
                dv = -dv
            ir = dv / load_i
            if 0 < ir < 1.0:
                ests.append(ir)
    return sum(ests) / len(ests) if ests else None


def analyze_cell_spread(samples: list[Sample], a: int, b: int,
                        cell_count: int) -> dict:
    """按时间连续性分析一段（循环）内的单体压差。

    - 压差 = 同一样本行内 max(cell) - min(cell)，同一行按编号成组解析，
      缺值/单体数漂移的行在导入阶段已剔除（s.cells 为 None）。
    - 压差连续超过 CELL_SPREAD_THRESHOLD_V 达 CELL_SPREAD_MIN_S 秒才计为
      一个持续超差区间；采样间隔不规则时按时间戳累计时长。
    - 相邻有效采样点间隔超过 CELL_SAMPLE_GAP_S（含缺值行造成的断点）时
      中断连续区间，断点两侧不拼接。

    返回最大压差、超差累计时长与区间列表（含证据片段）。
    """
    pts: list[tuple[float, float, tuple[float, ...]]] = []
    for k in range(a, b + 1):
        s = samples[k]
        if s.cells is not None and len(s.cells) == cell_count:
            pts.append((s.t, max(s.cells) - min(s.cells), s.cells))

    max_spread: Optional[float] = None
    at_max_t: Optional[float] = None
    intervals: list[dict] = []
    over_total = 0.0

    run: list[tuple[float, float, tuple[float, ...]]] = []

    def close_run(run_pts, end_reason: str):
        nonlocal over_total
        if not run_pts:
            return
        # 时长按时间戳累计（区间内相邻点 dt 之和），不规则采样同样适用
        dur = sum(run_pts[k + 1][0] - run_pts[k][0]
                  for k in range(len(run_pts) - 1))
        over_total += dur
        if dur < CELL_SPREAD_MIN_S:
            return  # 短暂尖峰：不生成区间、不告警
        mk = max(range(len(run_pts)), key=lambda j: run_pts[j][1])
        st, _, cells_st = run_pts[0]
        et, sp_et, cells_et = run_pts[-1]
        mt, ms, cells_mt = run_pts[mk]
        if end_reason == "gap":
            tail = (f"t={et:.1f}s 后出现超过 {CELL_SAMPLE_GAP_S:.0f}s "
                    f"的采样断点，连续区间中断")
        elif end_reason == "cycle_end":
            tail = f"直至 t={et:.1f}s 循环结束压差仍为 {sp_et:.3f}V"
        else:
            tail = f"t={et:.1f}s 回落至 {sp_et:.3f}V"
        intervals.append({
            "start_t": st,
            "end_t": et,
            "duration_s": dur,
            "end_reason": end_reason,
            "max_spread_v": ms,
            "at_max_t": mt,
            "evidence": (
                f"t={st:.1f}s 起压差 {run_pts[0][1]:.3f}V；"
                f"t={mt:.1f}s 最大压差 {ms:.3f}V"
                f"（最高单体 {max(cells_mt):.3f}V / 最低 {min(cells_mt):.3f}V）；"
                f"{tail}；连续 {dur:.1f}s > {CELL_SPREAD_THRESHOLD_V:.2f}V"
            ),
            "cells_start": [round(v, 3) for v in cells_st],
            "cells_at_max": [round(v, 3) for v in cells_mt],
            "cells_end": [round(v, 3) for v in cells_et],
        })

    prev_t: Optional[float] = None
    for pt in pts:
        t, spread, _ = pt
        if max_spread is None or spread > max_spread:
            max_spread, at_max_t = spread, t
        broken = prev_t is not None and (t - prev_t) > CELL_SAMPLE_GAP_S
        over = spread > CELL_SPREAD_THRESHOLD_V
        if broken or not over:
            close_run(run, "gap" if broken else "recovered")
            run = []
        if over and not broken:
            run.append(pt)
        prev_t = t
    close_run(run, "cycle_end")

    return {
        "cell_rows_used": len(pts),
        "max_cell_spread_v": max_spread,
        "max_spread_t": at_max_t,
        "over_threshold_s": over_total if pts else None,
        "cell_intervals": intervals,
    }


def _build_cycle(samples: list[Sample], a: int, b: int,
                 kind: str, thr: float, cell_count: Optional[int]) -> Cycle:
    seg = samples[a:b + 1]
    cap_ah, energy_wh = _integrate(samples, a, b)
    duration = samples[b].t - samples[a].t
    ir = _estimate_ir(samples, a, b, kind, thr)
    max_temp = max(s.temp for s in seg)
    n_skip = sum(1 for s in seg if cell_count and s.cells is None)
    cell_info = (analyze_cell_spread(samples, a, b, cell_count)
                 if cell_count else None)
    return Cycle(
        seq=0,
        kind="discharge" if kind == "D" else "charge",
        start_t=samples[a].t,
        end_t=samples[b].t,
        duration_s=duration,
        capacity_ah=cap_ah,
        energy_wh=energy_wh,
        avg_current_a=(cap_ah * 3600.0 / duration) if duration > 0 else 0.0,
        min_voltage=min(s.v for s in seg),
        max_voltage=max(s.v for s in seg),
        max_temp_c=max_temp,
        temp_rise_c=max_temp - seg[0].temp,
        ir_mohm=(ir * 1000.0) if ir is not None else None,
        closed=True,
        cell_count=cell_count,
        cell_rows_skipped=n_skip,
        cell_rows_used=cell_info["cell_rows_used"] if cell_info else 0,
        max_cell_spread_v=(cell_info["max_cell_spread_v"]
                           if cell_info else None),
        over_threshold_s=(cell_info["over_threshold_s"]
                          if cell_info else None),
        cell_intervals=cell_info["cell_intervals"] if cell_info else [],
    )


def detect_cycles(samples: list[Sample],
                  min_cycle_s: float = MIN_CYCLE_S,
                  min_rest_s: float = MIN_REST_S,
                  rest_current_a: Optional[float] = None,
                  cell_count: Optional[int] = None) -> list[Cycle]:
    """状态机主入口：样本序列 -> 循环列表。"""
    if len(samples) < 2:
        return []
    peak = max(abs(s.i) for s in samples)
    thr = rest_current_a if rest_current_a is not None else max(
        REST_CURRENT_FLOOR_A, REST_CURRENT_RATIO * peak)
    segs = _merge_segments(samples, _segments(_classify(samples, thr)),
                           min_cycle_s, min_rest_s)
    cycles: list[Cycle] = []
    for si, (kind, a, b) in enumerate(segs):
        if kind == "R":
            continue
        cyc = _build_cycle(samples, a, b, kind, thr, cell_count)
        cyc.seq = len(cycles) + 1
        cyc.closed = si < len(segs) - 1   # 日志结束时仍处于充/放电 -> 未闭合
        cycles.append(cyc)
    return cycles


# ---------------------------------------------------------------- 判定规则

def evaluate_cycle(cyc: Cycle, rated_ah: float,
                   safe_vmin: float, safe_vmax: float) -> list[dict]:
    """对单个循环套用安全/健康规则，返回可解释的标记列表。"""
    flags: list[dict] = []
    where = f"日志内第 {cyc.seq} 段（{'放电' if cyc.kind == 'discharge' else '充电'}）"

    if not cyc.closed:
        flags.append({
            "code": "UNCLOSED_CYCLE", "severity": "warning",
            "message": f"{where}在日志结束时仍未回到静置，循环未闭合，容量统计可能偏小。",
            "evidence": f"end_t={cyc.end_t:.0f}s, capacity={cyc.capacity_ah:.3f}Ah",
        })
    if cyc.kind == "discharge" and rated_ah > 0:
        ratio = cyc.capacity_ah / rated_ah
        if ratio > OVER_CAPACITY_RATIO:
            flags.append({
                "code": "OVER_CAPACITY", "severity": "info",
                "message": f"{where}放出 {cyc.capacity_ah:.2f}Ah，达额定容量的 "
                           f"{ratio * 100:.0f}%，额定容量设置可能偏低或电流采样存在偏差。",
                "evidence": f"capacity={cyc.capacity_ah:.3f}Ah, rated={rated_ah:.3f}Ah",
            })
    if cyc.kind == "discharge" and cyc.min_voltage < safe_vmin - VOLT_MARGIN_V:
        flags.append({
            "code": "OVER_DISCHARGE", "severity": "critical",
            "message": f"{where}最低电压 {cyc.min_voltage:.2f}V 低于安全下限 "
                       f"{safe_vmin:.2f}V，存在过放风险。",
            "evidence": f"min_voltage={cyc.min_voltage:.3f}V, safe_min={safe_vmin:.3f}V",
        })
    if cyc.kind == "charge" and cyc.max_voltage > safe_vmax + VOLT_MARGIN_V:
        flags.append({
            "code": "OVER_VOLTAGE", "severity": "critical",
            "message": f"{where}最高电压 {cyc.max_voltage:.2f}V 高于安全上限 "
                       f"{safe_vmax:.2f}V，存在过压风险。",
            "evidence": f"max_voltage={cyc.max_voltage:.3f}V, safe_max={safe_vmax:.3f}V",
        })
    if cyc.max_temp_c >= TEMP_CRIT_C:
        flags.append({
            "code": "TEMP_CRITICAL", "severity": "critical",
            "message": f"{where}最高温度 {cyc.max_temp_c:.1f}°C 达到严重阈值 "
                       f"{TEMP_CRIT_C:.0f}°C，异常发热。",
            "evidence": f"max_temp={cyc.max_temp_c:.1f}°C",
        })
    elif cyc.max_temp_c >= TEMP_WARN_C:
        flags.append({
            "code": "TEMP_HIGH", "severity": "warning",
            "message": f"{where}最高温度 {cyc.max_temp_c:.1f}°C 超过告警阈值 "
                       f"{TEMP_WARN_C:.0f}°C。",
            "evidence": f"max_temp={cyc.max_temp_c:.1f}°C",
        })
    if cyc.temp_rise_c >= TEMP_RISE_WARN_C:
        flags.append({
            "code": "TEMP_RISE", "severity": "warning",
            "message": f"{where}温升 {cyc.temp_rise_c:.1f}°C 超过 "
                       f"{TEMP_RISE_WARN_C:.0f}°C，发热异常。",
            "evidence": f"temp_rise={cyc.temp_rise_c:.1f}°C",
        })
    # 单体失衡：要求压差连续超差达 MIN_S（短尖峰不告警，采样断点不拼接）
    if cyc.cell_intervals:
        longest = max(cyc.cell_intervals, key=lambda iv: iv["duration_s"])
        flags.append({
            "code": "CELL_IMBALANCE", "severity": "warning",
            "message": f"{where}单体最大压差 {cyc.max_cell_spread_v:.3f}V，"
                       f"其中 {len(cyc.cell_intervals)} 个区间压差连续超过 "
                       f"{CELL_SPREAD_THRESHOLD_V:.2f}V（最长 "
                       f"{longest['duration_s']:.1f}s，阈值 "
                       f"{CELL_SPREAD_MIN_S:.0f}s），存在单体失衡。",
            "evidence": longest["evidence"],
        })
    return flags


# ---------------------------------------------------------------- 汇总评估

def _linregress(xs: list[float], ys: list[float]) -> tuple[float, float]:
    n = len(xs)
    if n < 2:
        return 0.0, (ys[0] if ys else 0.0)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return 0.0, my
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    return slope, my - slope * mx


GRADE_LABELS = {"good": "良好", "fair": "一般", "poor": "偏差", "critical": "严重"}


def build_assessment(battery: dict, cycles: list[dict], log_count: int) -> dict:
    """跨日志汇总：SOH、容量突降、趋势与健康等级。"""
    rated = battery["rated_capacity_ah"]
    dis = [c for c in cycles if c["kind"] == "discharge"]
    chg = [c for c in cycles if c["kind"] == "charge"]

    # 1) 汇总各循环在导入时已生成的标记
    flags: list[dict] = []
    for c in cycles:
        for f in c["flags"]:
            flags.append({**f, "log_id": c["log_id"], "cycle_seq": c["seq"],
                          "kind": c["kind"], "start_iso": c.get("start_iso")})

    # 2) 相邻放电循环容量突降
    caps = [c["capacity_ah"] for c in dis]
    for idx, (prev, cur) in enumerate(zip(dis, dis[1:]), start=2):
        if prev["capacity_ah"] > 0:
            drop = (prev["capacity_ah"] - cur["capacity_ah"]) / prev["capacity_ah"]
            if drop >= CAPACITY_DROP_RATIO:
                sev = "critical" if drop >= CAPACITY_DROP_CRIT else "warning"
                flags.append({
                    "code": "CAPACITY_DROP", "severity": sev,
                    "log_id": cur["log_id"], "cycle_seq": cur["seq"],
                    "kind": "discharge", "start_iso": cur.get("start_iso"),
                    "message": f"第 {idx} 个放电循环容量 {cur['capacity_ah']:.2f}Ah，"
                               f"较上一循环 {prev['capacity_ah']:.2f}Ah 突降 "
                               f"{drop * 100:.1f}%（阈值 {CAPACITY_DROP_RATIO * 100:.0f}%）。",
                    "evidence": f"prev={prev['capacity_ah']:.3f}Ah, cur={cur['capacity_ah']:.3f}Ah",
                })

    # 3) SOH / 衰减 / 趋势
    best = max(caps) if caps else 0.0
    recent = caps[-3:]
    recent_avg = sum(recent) / len(recent) if recent else 0.0
    soh = best / rated * 100.0 if rated else None
    fade = (best - recent_avg) / best * 100.0 if best else 0.0
    slope, _ = _linregress([float(i) for i in range(1, len(caps) + 1)], caps)
    fade_rate = slope / rated * 100.0 if rated else 0.0
    irs = [c["ir_mohm"] for c in cycles if c.get("ir_mohm") is not None]
    avg_ir = sum(irs) / len(irs) if irs else None
    max_temp = max((c["max_temp_c"] for c in cycles), default=None)
    cell_logs = {c["log_id"] for c in cycles if c.get("cell_count")}
    cell_cycles = [c for c in cycles if c.get("cell_count")]
    imbalance_cycles = [c for c in cell_cycles if c.get("cell_intervals")]
    max_cell_spread = max((c["max_cell_spread_v"] for c in cell_cycles
                           if c.get("max_cell_spread_v") is not None),
                          default=None)

    # 4) 健康等级
    rank = {"info": 0, "warning": 1, "critical": 2}
    worst = max((rank.get(f["severity"], 0) for f in flags), default=0)
    if worst >= 2 or (soh is not None and soh < 60):
        grade = "critical"
    elif (soh is not None and soh < 70) or fade > 30:
        grade = "poor"
    elif worst >= 1 or (soh is not None and soh < 85) or fade > 15:
        grade = "fair"
    else:
        grade = "good"

    flags.sort(key=lambda f: -rank.get(f["severity"], 0))

    trend = [{"n": i + 1, "capacity_ah": round(c["capacity_ah"], 4),
              "log_id": c["log_id"], "cycle_seq": c["seq"],
              "start_iso": c.get("start_iso"), "ir_mohm": c.get("ir_mohm"),
              "max_temp_c": c["max_temp_c"],
              "max_cell_spread_v": c.get("max_cell_spread_v"),
              "cell_imbalance": bool(c.get("cell_intervals"))}
             for i, c in enumerate(dis)]

    parts = [
        f"共导入 {log_count} 份日志，识别放电循环 {len(dis)} 个、充电循环 {len(chg)} 个。",
    ]
    if dis:
        parts.append(
            f"最佳放电容量 {best:.2f}Ah（额定 {rated:.2f}Ah），估算 SOH {soh:.1f}%；"
            f"最近 {len(recent)} 次平均 {recent_avg:.2f}Ah，较最佳值衰减 {fade:.1f}%。")
        if len(caps) >= 2:
            parts.append(f"容量趋势约每循环 {fade_rate:+.2f}% 额定容量。")
    if avg_ir is not None:
        parts.append(f"平均估算内阻 {avg_ir:.1f}mΩ。")
    if max_temp is not None:
        parts.append(f"历史最高温度 {max_temp:.1f}°C。")
    if cell_logs:
        parts.append(
            f"单体电压分析覆盖 {len(cell_logs)} 份日志中的 {len(cell_cycles)} 个循环，"
            f"{len(imbalance_cycles)} 个循环检出单体失衡"
            + (f"，历史最大单体压差 {max_cell_spread:.3f}V。"
               if max_cell_spread is not None else "。"))
    n_crit = sum(1 for f in flags if f["severity"] == "critical")
    n_warn = sum(1 for f in flags if f["severity"] == "warning")
    parts.append(f"判定标记：严重 {n_crit} 条、警告 {n_warn} 条；综合健康等级：{GRADE_LABELS[grade]}。")

    return {
        "summary": {
            "total_logs": log_count,
            "total_cycles": len(cycles),
            "discharge_cycles": len(dis),
            "charge_cycles": len(chg),
            "rated_capacity_ah": rated,
            "best_discharge_ah": round(best, 4),
            "recent_avg_ah": round(recent_avg, 4),
            "soh_percent": round(soh, 1) if soh is not None else None,
            "fade_percent": round(fade, 1),
            "fade_per_cycle_percent": round(fade_rate, 3),
            "avg_ir_mohm": round(avg_ir, 2) if avg_ir is not None else None,
            "max_temp_c": max_temp,
            "cell_monitored_logs": len(cell_logs),
            "cell_monitored_cycles": len(cell_cycles),
            "cell_imbalance_cycles": len(imbalance_cycles),
            "max_cell_spread_v": (round(max_cell_spread, 4)
                                  if max_cell_spread is not None else None),
            "health_grade": grade,
            "health_label": GRADE_LABELS[grade],
        },
        "summary_text": "".join(parts),
        "flags": flags,
        "trend": trend,
        "cycles": cycles,
    }
