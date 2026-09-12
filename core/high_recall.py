# core/high_recall.py
"""高召回模式时间工具：每日时段的生效窗口、下一个定时切换节点与配置校验。

时间按本地时区解析 "HH:MM"；结束时间早于或等于开始时间视为次日
（如 22:00~06:00 为跨天时段）。不依赖 bot 客户端，仅做时间计算，
供 main.py 的调度循环与 WebUI 配置校验使用。
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Optional, Tuple


def _parse_hhmm(value) -> Optional[Tuple[int, int]]:
    """解析 "HH:MM" 为 (hour, minute)，非法返回 None。"""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(value or "").strip())
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def _duration_minutes(start_min: int, end_min: int) -> int:
    """窗口时长（分钟）；结束不晚于开始时按跨天计算（相同则视为全天）。"""
    duration = end_min - start_min
    if duration <= 0:
        duration += 1440
    return duration


def _latest_start_ts(now: float, start_min: int) -> float:
    """返回不晚于 now 的最近一次窗口起点（now 早于今日起点时取昨天）。"""
    midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    start_ts = midnight.timestamp() + start_min * 60
    if start_ts > now:
        start_ts -= 86400
    return start_ts


def high_recall_window(
    start_str, end_str, now: Optional[float] = None
) -> Optional[Tuple[float, float]]:
    """now 处于高召回时段内时返回窗口 (start_ts, end_ts)，否则返回 None。"""
    hm_s, hm_e = _parse_hhmm(start_str), _parse_hhmm(end_str)
    if hm_s is None or hm_e is None:
        return None
    now = time.time() if now is None else now
    start_min = hm_s[0] * 60 + hm_s[1]
    duration = _duration_minutes(start_min, hm_e[0] * 60 + hm_e[1])
    start_ts = _latest_start_ts(now, start_min)
    end_ts = start_ts + duration * 60
    if start_ts <= now < end_ts:
        return start_ts, end_ts
    return None


def in_high_recall_window(start_str, end_str, now: Optional[float] = None) -> bool:
    """now 是否处于高召回时段内（时间非法视为不生效）。"""
    return high_recall_window(start_str, end_str, now) is not None


def high_recall_next_flip(start_str, end_str, now: Optional[float] = None) -> float:
    """返回 now 之后最近的定时切换时刻（开启或关闭边界），无法计算返回 0。"""
    hm_s, hm_e = _parse_hhmm(start_str), _parse_hhmm(end_str)
    if hm_s is None or hm_e is None:
        return 0.0
    now = time.time() if now is None else now
    start_min = hm_s[0] * 60 + hm_s[1]
    duration = _duration_minutes(start_min, hm_e[0] * 60 + hm_e[1])
    midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    # 前后各展开两天，确保跨天时段的边界不遗漏
    edges = []
    for offset in (-1, 0, 1, 2):
        start_ts = midnight.timestamp() + offset * 86400 + start_min * 60
        edges.append(start_ts)
        edges.append(start_ts + duration * 60)
    future = [e for e in edges if e > now]
    return min(future) if future else 0.0


def validate_high_recall_times(start_str, end_str) -> Optional[str]:
    """校验高召回定时的时间配置，合法返回 None，否则返回错误信息。"""
    s = str(start_str or "").strip()
    e = str(end_str or "").strip()
    if not s or not e:
        return "开启每日定时高召回需填写开启与关闭时间（HH:MM）"
    if _parse_hhmm(s) is None or _parse_hhmm(e) is None:
        return "高召回时间格式错误：开启与关闭时间应为 HH:MM，如 22:00"
    if _parse_hhmm(s) == _parse_hhmm(e):
        return "高召回开启与关闭时间不能相同"
    return None
