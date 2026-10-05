"""退群记录：记录成员退群（主动退群/被移出）时间，供「退群后 X 时间内再次申请自动拒绝」判定。

与违规计数一样按群独立持久化在插件数据目录，插件重启后仍有效；
记录超过保留期（180 天）自动清理，避免文件无限膨胀。
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional

LEAVE_RECORDS_FILE = "leave_records.json"
DEDUP_SECONDS = 120  # 同一成员在此秒数内的重复上报视为同一次退群事件（双通道事件去重）
MAX_RETENTION_SECONDS = 180 * 86400  # 记录最多保留 180 天，超出自动清理


class LeaveTracker:
    def __init__(self, data_dir: str, logger=None, filename: str = LEAVE_RECORDS_FILE):
        self.path = os.path.join(str(data_dir), filename)
        self._logger = logger
        self.records: dict[str, dict[str, float]] = {}  # gid -> {uid: 退群时间戳}
        self.load()

    def load(self):
        self.records = {}
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        now = time.time()
        for gid, members in data.items():
            if not isinstance(members, dict):
                continue
            kept: dict[str, float] = {}
            for uid, ts in members.items():
                try:
                    value = float(ts)
                except (TypeError, ValueError):
                    continue
                if 0 < now - value <= MAX_RETENTION_SECONDS:
                    kept[str(uid)] = value
            if kept:
                self.records[str(gid)] = kept

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.records, f, ensure_ascii=False, indent=2)
        except Exception as e:
            if self._logger:
                self._logger.error(f"退群记录持久化失败: {e}")

    def record(self, group_id, user_id, ts: Optional[float] = None) -> bool:
        """记录一次退群；同一事件的重复上报（默认 120 秒内）不重复写入，返回是否有新记录。"""
        gid, uid = str(group_id), str(user_id)
        value = float(ts) if ts is not None else time.time()
        members = self.records.setdefault(gid, {})
        old = members.get(uid)
        if old is not None and abs(value - float(old)) <= DEDUP_SECONDS:
            return False
        members[uid] = value
        self.prune()  # 以当前时间为基准清理超期记录（兼容回放旧事件时的时间戳）
        self.save()
        return True

    def get(self, group_id, user_id) -> float:
        """取该成员最近的退群时间戳；无记录返回 0。"""
        try:
            return float((self.records.get(str(group_id)) or {}).get(str(user_id)) or 0)
        except (TypeError, ValueError):
            return 0.0

    def remaining(self, group_id, user_id, window_seconds, now: Optional[float] = None) -> float:
        """返回退群拦截窗口内的剩余秒数；未记录或已过期返回 0。"""
        try:
            window = float(window_seconds or 0)
        except (TypeError, ValueError):
            window = 0.0
        if window <= 0:
            return 0.0
        left = self.get(group_id, user_id)
        if left <= 0:
            return 0.0
        current = float(now) if now is not None else time.time()
        remain = left + window - current
        return remain if remain > 0 else 0.0

    def group_records(self, group_id, limit: int = 100) -> list:
        """返回该群退群记录（按时间倒序）：[{user_id, leave_time}]。"""
        members = self.records.get(str(group_id)) or {}
        items = sorted(members.items(), key=lambda kv: kv[1], reverse=True)
        try:
            count = max(0, int(limit))
        except (TypeError, ValueError):
            count = 100
        return [{"user_id": uid, "leave_time": ts} for uid, ts in items[:count]]

    def delete(self, group_id, user_id) -> bool:
        gid, uid = str(group_id), str(user_id)
        members = self.records.get(gid)
        if not members or uid not in members:
            return False
        members.pop(uid, None)
        if not members:
            self.records.pop(gid, None)
        self.save()
        return True

    def clear(self, group_id) -> bool:
        gid = str(group_id)
        if gid not in self.records:
            return False
        self.records.pop(gid, None)
        self.save()
        return True

    def prune(self, now: Optional[float] = None) -> None:
        """清理超过保留期的历史记录（就地修改，不落盘）。"""
        current = float(now) if now is not None else time.time()
        for gid in list(self.records):
            members = self.records.get(gid) or {}
            for uid in list(members):
                ts = members.get(uid)
                if ts is None or current - float(ts) > MAX_RETENTION_SECONDS:
                    members.pop(uid, None)
            if not members:
                self.records.pop(gid, None)