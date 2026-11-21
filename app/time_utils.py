"""UTC 整秒 / 整小时时间工具。

对外统一使用带 ``Z`` 后缀的 UTC ISO-8601 字符串（如 ``2026-11-21T08:00:00Z``），
内部一律使用 UTC 纪元秒整数，保证区间比较与存储稳定。
"""
from __future__ import annotations

from datetime import datetime, timezone

SECONDS_PER_HOUR = 3600


class TimeFormatError(ValueError):
    """时间字符串无法解析或不符合 UTC 整秒要求。"""


def parse_epoch_second(value: object) -> int:
    """把输入解析为 UTC 纪元秒整数。

    支持：
    * ``int``（直接视为纪元秒）；
    * 带 ``Z`` / ``+00:00`` 偏移的 ISO-8601 字符串，必须是整秒。

    不接受无时区的本地时间，也不接受小数秒，避免歧义。
    """
    if isinstance(value, bool):
        raise TimeFormatError("时间不能是布尔值")
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        raise TimeFormatError("时间必须是 ISO-8601 字符串或纪元秒整数")
    text = value.strip()
    if not text:
        raise TimeFormatError("时间字符串为空")
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise TimeFormatError(f"无法解析时间: {value!r}") from exc
    if dt.tzinfo is None:
        raise TimeFormatError("时间必须显式带 UTC 时区（以 Z 结尾或 +00:00）")
    if dt.utcoffset() != timezone.utc.utcoffset(dt):
        raise TimeFormatError("只接受 UTC 时间（Z 或 +00:00）")
    if dt.microsecond != 0:
        raise TimeFormatError("时间必须是整秒，不允许小数秒")
    return int(dt.timestamp())


def format_epoch_second(epoch: int) -> str:
    """纪元秒格式化为 ``...Z`` 形式的 UTC 字符串。"""
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def format_hour_start(hour_start: int) -> str:
    """小时起点纪元秒格式化为 ``YYYY-MM-DDTHH:00:00Z``。"""
    return format_epoch_second(hour_start)


def parse_hour_param(value: str) -> int:
    """解析查询参数中的小时，返回该小时的起点纪元秒（必须整点）。"""
    epoch = parse_epoch_second(value)
    if epoch % SECONDS_PER_HOUR != 0:
        raise TimeFormatError("hour 必须是 UTC 整点，例如 2026-11-21T08:00:00Z")
    return epoch


def hours_touching(start_ts: int, end_ts: int) -> range:
    """与左闭右开区间 [start_ts, end_ts) 有重叠的 UTC 整点小时范围。"""
    first = (start_ts // SECONDS_PER_HOUR) * SECONDS_PER_HOUR
    # end 是开区间；end 恰好落在整点时，该小时没有重叠，需要排除。
    last_covered = end_ts - 1
    last_hour = (last_covered // SECONDS_PER_HOUR) * SECONDS_PER_HOUR
    count = (last_hour - first) // SECONDS_PER_HOUR + 1
    return range(first, first + count * SECONDS_PER_HOUR, SECONDS_PER_HOUR)
