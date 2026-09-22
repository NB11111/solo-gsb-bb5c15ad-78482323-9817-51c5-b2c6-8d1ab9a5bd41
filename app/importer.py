"""飞行日志 CSV 解析：表头别名识别、分隔符嗅探、时间列归一化。"""
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
    """解析 CSV 文本，返回 (按时间排序的样本列表, 元信息)。"""
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

    samples: list[Sample] = []
    skipped = 0
    t0 = None
    epoch_based = False
    for row in rows[1:]:
        try:
            tv, is_epoch = _parse_time_value(row[ci_t])
            v = float(row[ci_v])
            i = float(row[ci_i])
            tp = float(row[ci_tp])
        except (ValueError, IndexError, CsvFormatError):
            skipped += 1
            continue
        if t0 is None:
            t0 = tv
            epoch_based = is_epoch
        samples.append(Sample(t=tv - t0, v=v, i=i, temp=tp))

    if len(samples) < 10:
        raise CsvFormatError(f"有效数据行不足（{len(samples)} 行），至少需要 10 行")

    samples.sort(key=lambda s: s.t)
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
    }
    return samples, meta
