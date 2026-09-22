"""飞行日志 CSV 解析：表头别名识别、分隔符嗅探、时间列归一化、单体电压列识别。"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone

from .analysis import Sample


class CsvFormatError(ValueError):
    pass


_TIME_ALIASES = {"time", "timestamp", "datetime", "date", "time_s", "t_s",
                 "elapsed", "时间", "时刻", "时间戳"}
_VOLT_ALIASES = {"voltage", "volt", "volts", "v", "voltage_v", "vol", "电压"}
_CURR_ALIASES = {"current", "curr", "i", "a", "current_a", "amp", "amps",
                 "amperage", "电流"}
_TEMP_ALIASES = {"temperature", "temp", "t", "temp_c", "temperature_c",
                 "celsius", "温度"}

_UNIT_RE = re.compile(r"[\(（\[].*?[\)）\]]")

# 单体电压列编号匹配（作用于归一化后、去掉单位与空格的表头）
_CN_CELL = r"单体|电芯|单芯"
_CN_V = r"电压|电压值|v|volt"
_CELL_NUM_RES = [
    re.compile(r"^cell[#_\-]?(\d{1,2})(?:_?voltage|_?volt|_?v)?$"),
    re.compile(r"^cell_?(?:voltage|volt|v)[#_\-]?(\d{1,2})$"),
    re.compile(r"^cell[#_\-]v?(\d{1,2})$"),
    re.compile(r"^c[#_\-]?(\d{1,2})$"),
    re.compile(r"^v[#_\-]?(\d{1,2})$"),
    re.compile(r"^vcell[#_\-]?(\d{1,2})$"),
    re.compile(rf"^(?:{_CN_CELL})[#号_\-]?(\d{{1,2}})号?(?:{_CN_V})?$"),
    re.compile(rf"^(?:{_CN_CELL})(?:{_CN_V})[#号_\-]?(\d{{1,2}})$"),
    re.compile(r"^第(\d{1,2})(?:节|串|路|块|芯)"
               rf"(?:{_CN_CELL})?(?:{_CN_V})?$"),
    re.compile(rf"^(?:{_CN_V})[#号_\-]?(\d{{1,2}})$"),
]


def decode_text(raw: bytes) -> str:
    for enc in ("utf-8-sig", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _norm(header: str) -> str:
    h = header.strip().lower()
    h = _UNIT_RE.sub("", h)          # 去掉 (V) /（A）等单位
    h = h.replace(" ", "_").strip("_")
    return h


def _match_column(headers: list[str], aliases: set[str]) -> int | None:
    norm = [_norm(h) for h in headers]
    for idx, h in enumerate(norm):
        if h in aliases:
            return idx
    return None


def _cell_number(h: str) -> int | None:
    """表头若为单体电压列，返回其编号（cell1_voltage / 单体1电压 等）。"""
    for rx in _CELL_NUM_RES:
        m = rx.match(h)
        if m:
            return int(m.group(1))
    return None


def _find_cell_columns(headers: list[str]) -> tuple[dict[int, int], list[str]]:
    """扫描表头识别单体电压列，返回 ({编号: 列索引}, 问题说明列表)。

    识别成功时编号必须从 1 起连续（cell1_voltage、cell2_voltage…）；
    编号重复或不连续时不启用单体分析（不影响总压等必需列的分析）。
    """
    found: dict[int, int] = {}
    problems: list[str] = []
    for idx, raw in enumerate(headers):
        n = _cell_number(_norm(raw))
        if n is None or n <= 0:
            continue
        if n in found:
            problems.append(f"单体 {n} 的列重复（{headers[found[n]]!r} 与 "
                            f"{raw!r}），已忽略单体电压列")
            return {}, problems
        found[n] = idx
    if not found:
        return {}, problems
    expected = set(range(1, max(found) + 1))
    missing = sorted(expected - set(found))
    if missing or min(found) != 1:
        problems.append(
            "单体电压列编号不连续（识别到 "
            + ", ".join(f"cell{n}" for n in sorted(found))
            + f"，缺少 {', '.join(f'cell{n}' for n in missing) or ' cell1'}"
            + "），已忽略单体电压列")
        return {}, problems
    return {n: found[n] for n in sorted(found)}, problems


def _parse_time_value(s: str):
    """返回 (秒数值, 是否 epoch 时间戳)。"""
    s = s.strip()
    try:
        val = float(s)
        return val, val > 1e8
    except ValueError:
        pass
    iso = s.replace("Z", "+00:00").replace("/", "-")
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise CsvFormatError(f"无法解析时间值: {s!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp(), True


def parse_flight_csv(text: str) -> tuple[list[Sample], dict]:
    """解析 CSV 文本，返回 (按时间排序的样本列表, 元信息)。

    必需列为 time/voltage/current/temperature；若表头另外给出编号连续的
    单体电压列（cell1_voltage、cell2_voltage…及常见中文表头），则按编号
    成组解析。单体数漂移或某行单体缺值时，仅跳过该行的单体分析并记录原因，
    总压/电流/温度分析照常进行。
    """
    sample_text = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample_text, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if len(rows) < 2:
        raise CsvFormatError("CSV 内容为空或缺少数据行")

    headers = rows[0]
    ci_t = _match_column(headers, _TIME_ALIASES)
    ci_v = _match_column(headers, _VOLT_ALIASES)
    ci_i = _match_column(headers, _CURR_ALIASES)
    ci_tp = _match_column(headers, _TEMP_ALIASES)
    missing = [name for name, idx in
               (("time", ci_t), ("voltage", ci_v), ("current", ci_i),
                ("temperature", ci_tp)) if idx is None]
    if missing:
        raise CsvFormatError(
            f"缺少必需列: {', '.join(missing)}；识别到的表头: {headers}")

    cell_cols, cell_problems = _find_cell_columns(headers)
    cell_count = len(cell_cols) or None
    cell_skipped: dict[str, int] = {}
    cell_examples: dict[str, list[str]] = {}

    def _skip_cell(reason: str, line_no: int) -> None:
        cell_skipped[reason] = cell_skipped.get(reason, 0) + 1
        bucket = cell_examples.setdefault(reason, [])
        if len(bucket) < 10:
            bucket.append(f"第 {line_no} 行")

    samples: list[Sample] = []
    skipped = 0
    t0 = None
    epoch_based = False
    for line_no, row in enumerate(rows[1:], start=2):
        try:
            tv, is_epoch = _parse_time_value(row[ci_t])
            v = float(row[ci_v])
            i = float(row[ci_i])
            tp = float(row[ci_tp])
        except (ValueError, IndexError, CsvFormatError):
            skipped += 1
            continue
        cells: tuple[float, ...] | None = None
        if cell_cols:
            if len(row) > len(headers):
                _skip_cell("单体数量漂移（列数多于表头）", line_no)
            elif len(row) < max(cell_cols.values()) + 1:
                # 整行过短，至少有单体列缺失
                _skip_cell("单体数量漂移（列数少于表头）", line_no)
            else:
                vals: list[float] = []
                bad = False
                for n, col in cell_cols.items():
                    raw_c = row[col] if col < len(row) else ""
                    if not raw_c.strip():
                        _skip_cell(f"单体 {n} 缺值", line_no)
                        bad = True
                        break
                    try:
                        vals.append(float(raw_c))
                    except ValueError:
                        _skip_cell(f"单体 {n} 数值无法解析（{raw_c.strip()!r}）",
                                   line_no)
                        bad = True
                        break
                if not bad and len(vals) == cell_count:
                    cells = tuple(vals)
        if t0 is None:
            t0 = tv
            epoch_based = is_epoch
        samples.append(Sample(t=tv - t0, v=v, i=i, temp=tp, cells=cells))

    if len(samples) < 10:
        raise CsvFormatError(f"有效数据行不足（{len(samples)} 行），至少需要 10 行")

    samples.sort(key=lambda s: s.t)
    cell_rows = [s for s in samples if s.cells is not None]
    if cell_cols and not cell_rows:
        # 所有行的单体值均无效：视为未提供单体数据并记录原因
        cell_count = None
        cell_problems.append("全部数据行均缺少有效的单体电压值，未启用单体分析")
    start_iso = (datetime.fromtimestamp(t0, tz=timezone.utc).isoformat()
                 if epoch_based and t0 is not None else None)
    meta = {
        "row_count": len(samples),
        "skipped_rows": skipped,
        "start_time": start_iso,
        "duration_s": round(samples[-1].t - samples[0].t, 3),
        "max_current_a": round(max(abs(s.i) for s in samples), 3),
        "min_voltage": round(min(s.v for s in samples), 3),
        "max_voltage": round(max(s.v for s in samples), 3),
        "max_temp_c": round(max(s.temp for s in samples), 3),
        "cell_count": cell_count,
        "cell_columns": [headers[cell_cols[n]] for n in sorted(cell_cols)],
        "cell_problems": cell_problems,
        "cell_rows_used": len(cell_rows),
        "cell_rows_skipped": sum(cell_skipped.values()),
        "cell_skip_reasons": [
            {"reason": reason, "count": cnt, "examples": cell_examples[reason]}
            for reason, cnt in sorted(cell_skipped.items())
        ],
    }
    return samples, meta
