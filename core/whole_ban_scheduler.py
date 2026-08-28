# core/whole_ban_scheduler.py
"""定时全体禁言调度器：每个群可共存多个独立任务、JSON 持久化、时间解析。

不依赖 bot 客户端，仅保存任务的时间与状态；执行由 main.py 中的后台循环驱动。
多任务设计：新设置的定时/倒计时/每周任务只追加或合并，不再覆盖已有规划。
"""

import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

SCHEDULE_FILE = "schedule_ban.json"
MAX_INTERVAL_DAYS = 7  # 单次任务的开始/持续时长上限（天）


def _parse_hhmm(value: str) -> Optional[Tuple[int, int]]:
    """解析 "HH:MM" 为 (hour, minute)，非法返回 None。"""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def _minutes_since_midnight(ts: float) -> int:
    lt = time.localtime(ts)
    return lt.tm_hour * 60 + lt.tm_min


def parse_schedule_times(start_str: str, end_str: str, now: Optional[float] = None) -> Tuple[float, float]:
    """
    解析定时禁言的开始/结束时间，返回 (start_ts, end_ts)。

    规则：
    - start_str: "now" 立即开始；或 "HH:MM"，若该时刻已过则顺延到明天
    - end_str:  "HH:MM"（早于或等于开始时间视为次日凌晨）；或纯数字分钟数（从开始时间起持续 N 分钟）
    - 开始时间距当前、以及持续时长均不得超过 MAX_INTERVAL_DAYS 天

    Raises:
        ValueError: 时间格式非法或超出范围
    """
    now = now if now is not None else time.time()
    start_txt = start_str.strip().lower()
    end_txt = end_str.strip()

    if start_txt == "now":
        start_ts = now
    else:
        hm = _parse_hhmm(start_txt)
        if hm is None:
            raise ValueError(f"开始时间格式非法: {start_str}（应为 now 或 HH:MM）")
        start_dt = datetime.fromtimestamp(now).replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
        if _minutes_since_midnight(start_dt.timestamp()) <= _minutes_since_midnight(now):
            start_dt += timedelta(days=1)  # 已过该时刻则顺延到明天
        start_ts = start_dt.timestamp()

    if end_txt.isdigit():
        minutes = int(end_txt)
        if minutes <= 0:
            raise ValueError("持续时间必须为正数（分钟）")
        end_ts = start_ts + minutes * 60
    else:
        hm = _parse_hhmm(end_txt)
        if hm is None:
            raise ValueError(f"结束时间格式非法: {end_str}（应为 HH:MM 或分钟数）")
        start_dt = datetime.fromtimestamp(start_ts)
        end_dt = start_dt.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
        if end_dt.timestamp() <= start_ts:
            end_dt += timedelta(days=1)  # 早于/等于开始时间，视为次日凌晨
        end_ts = end_dt.timestamp()

    if end_ts <= start_ts:
        raise ValueError("结束时间必须晚于开始时间")
    if start_ts - now > MAX_INTERVAL_DAYS * 86400:
        raise ValueError(f"开始时间距现在不能超过 {MAX_INTERVAL_DAYS} 天")
    if end_ts - start_ts > MAX_INTERVAL_DAYS * 86400:
        raise ValueError(f"禁言时长不能超过 {MAX_INTERVAL_DAYS} 天")
    return start_ts, end_ts


def _new_id() -> str:
    """生成任务唯一 id。"""
    return uuid.uuid4().hex[:12]


class WholeBanScheduler:
    """管理各群的定时全体禁言任务，每群可共存多个，变更后立即持久化。"""

    def __init__(self, data_dir: str, logger=None):
        self.path = os.path.join(str(data_dir), SCHEDULE_FILE)
        self._logger = logger
        self.schedules: Dict[str, List[dict]] = {}  # str(group_id) -> [task,...]
        self.load()

    def set(
        self,
        group_id,
        start_ts: float,
        end_ts: float,
        bot=None,
        self_id=None,
        recurring: bool = False,
    ) -> dict:
        """登记一个单次/每日任务（追加），返回新任务。

        recurring=True 表示每日重复（mode=daily）：窗口结束后自动推进到下一天，直到取消。
        同类型未触发的旧任务会被替换，避免堆积；不影响其他类型任务（如每周规划）。
        """
        mode = "daily" if recurring else "once"
        tasks = self.schedules.setdefault(str(group_id), [])
        # 移除同 mode 且尚未触发的旧任务，防止多次设置堆积
        for old in [t for t in tasks if t.get("mode") == mode and not t.get("started")]:
            tasks.remove(old)
        sched = {
            "id": _new_id(),
            "mode": mode,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "started": False,   # 是否已执行开启
            "finished": False,  # 是否已执行解除
            "recurring": bool(recurring),
            "bot": bot,
            "self_id": self_id,
            "created_at": time.time(),
        }
        tasks.append(sched)
        self.save()
        return sched

    def set_weekly(self, group_id, rules: Dict[str, dict], bot=None, self_id=None) -> dict:
        """设置/更新每周任务：已有 weekly 任务则覆盖其规则，否则新建。

        rules: {str(weekday 1-7): {"start_min": 当日分钟数, "duration_min": 持续分钟数}}
        跨天窗口通过 duration 跨越自然实现（如 22:00 起 480 分钟 = 次日 06:00）。
        """
        tasks = self.schedules.setdefault(str(group_id), [])
        for t in tasks:
            if t.get("mode") == "weekly":
                t["rules"] = rules
                t["bot"] = bot if bot is not None else t.get("bot")
                t["started"] = False  # 规则更新后本轮窗口需重新触发
                t["current_end_ts"] = 0
                self.save()
                return t
        sched = {
            "id": _new_id(),
            "mode": "weekly",
            "rules": rules,
            "started": False,
            "finished": False,
            "current_end_ts": 0,  # 当前已开启窗口的结束时间戳
            "bot": bot,
            "self_id": self_id,
            "created_at": time.time(),
        }
        tasks.append(sched)
        self.save()
        return sched

    def remove(self, group_id, task_id: Optional[str] = None) -> Optional[dict]:
        """移除任务：task_id 为空则移除该群全部任务，否则仅移除指定任务。"""
        tasks = self.schedules.get(str(group_id))
        if not tasks:
            return None
        if task_id is None:
            self.schedules.pop(str(group_id), None)
            self.save()
            return tasks[0]
        for i, t in enumerate(tasks):
            if t.get("id") == task_id:
                del tasks[i]
                self.save()
                return t
        return None

    def get(self, group_id) -> List[dict]:
        """返回该群全部任务列表（可能为空）。"""
        return self.schedules.get(str(group_id)) or []

    def get_task(self, group_id, task_id: str) -> Optional[dict]:
        """按 id 取单个任务。"""
        for t in self.get(group_id):
            if t.get("id") == task_id:
                return t
        return None

    def all(self) -> Dict[str, List[dict]]:
        return self.schedules

    def advance(self, sched: dict, now: Optional[float] = None) -> None:
        """每日任务：将窗口推进到下一天并重置开启状态。"""
        now = now if now is not None else time.time()
        sched["start_ts"] = (sched.get("start_ts") or now) + 86400
        sched["end_ts"] = (sched.get("end_ts") or now) + 86400
        sched["started"] = False
        self.save()

    def save(self):
        serializable = {
            gid: [{k: v for k, v in t.items() if k != "bot"} for t in tasks]
            for gid, tasks in self.schedules.items()
        }
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(serializable, f, ensure_ascii=False, indent=2)
        except Exception as e:
            if self._logger:
                self._logger.error(f"定时禁言任务持久化失败: {e}")

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return
        now = time.time()
        for gid, val in data.items():
            # 兼容旧版单任务结构 {gid: task}
            tasks = [val] if isinstance(val, dict) else (val if isinstance(val, list) else [])
            for task in tasks:
                try:
                    if not isinstance(task, dict) or task.get("finished"):
                        continue
                    mode = task.get("mode")
                    if mode == "weekly":
                        if not isinstance(task.get("rules"), dict) or not task["rules"]:
                            continue  # 无任何规则的任务无意义
                    elif mode == "daily" or task.get("recurring"):
                        # 每日任务：停机期间错过的窗口直接推进到未来
                        start_ts = task.get("start_ts") or 0
                        end_ts = task.get("end_ts") or 0
                        guard = 0
                        while end_ts <= now and guard < 366:
                            start_ts += 86400
                            end_ts += 86400
                            task["started"] = False
                            guard += 1
                        task["start_ts"] = start_ts
                        task["end_ts"] = end_ts
                    elif not task.get("started") and task.get("end_ts", 0) <= now:
                        continue  # 单次任务未执行且已过结束时间，无意义
                    task.setdefault("id", _new_id())
                    task["bot"] = None  # bot 引用不持久化，运行时通过缓存补齐
                    self.schedules.setdefault(str(gid), []).append(task)
                except Exception:
                    continue


def weekly_window(rules: Dict[str, dict], now: Optional[float] = None) -> Optional[Tuple[float, float]]:
    """计算今天（now 所在自然日）的禁言窗口 (start_ts, end_ts)；今天无规则返回 None。"""
    now = now if now is not None else time.time()
    lt = time.localtime(now)
    rule = rules.get(str(lt.tm_wday + 1))
    if not rule:
        return None
    day_start = datetime.fromtimestamp(now).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()
    start_ts = day_start + int(rule.get("start_min") or 0) * 60
    end_ts = start_ts + int(rule.get("duration_min") or 0) * 60
    return start_ts, end_ts