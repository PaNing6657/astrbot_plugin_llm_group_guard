# core/violation_tracker.py
"""违规计数与违规日志：累计次数供阶梯禁言使用，日志记录违规消息原文供 WebUI 展示。"""

from __future__ import annotations

import json
import os
import time
from typing import Dict, List

VIOLATION_COUNTS_FILE = "violation_counts.json"
KEYWORD_COUNTS_FILE = "keyword_counts.json"
VIOLATION_LOG_FILE = "violation_log.json"


class ViolationTracker:
    def __init__(self, data_dir: str, logger=None, filename: str = VIOLATION_COUNTS_FILE):
        self.path = os.path.join(str(data_dir), filename)
        self._logger = logger
        self.counts: Dict[str, Dict[str, int]] = {}  # gid -> {uid: 累计违规次数}
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.counts = {
                    str(gid): {str(uid): int(n) for uid, n in members.items() if int(n) > 0}
                    for gid, members in data.items()
                    if isinstance(members, dict)
                }
        except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
            self.counts = {}

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.counts, f, ensure_ascii=False, indent=2)
        except Exception as e:
            if self._logger:
                self._logger.error(f"违规计数持久化失败: {e}")

    def add(self, group_id, user_id) -> int:
        """违规次数 +1，返回累计次数。"""
        gid, uid = str(group_id), str(user_id)
        count = self.counts.setdefault(gid, {}).get(uid, 0) + 1
        self.counts[gid][uid] = count
        self.save()
        return count

    def get(self, group_id, user_id) -> int:
        return self.counts.get(str(group_id), {}).get(str(user_id), 0)

    def reset(self, group_id, user_id):
        members = self.counts.get(str(group_id))
        if members:
            members.pop(str(user_id), None)
            self.save()


class ViolationLog:
    """违规消息日志：按时间倒序保留最近 max_entries 条，供 WebUI 查看违规原文。"""

    def __init__(self, data_dir: str, logger=None, max_entries: int = 200):
        self.path = os.path.join(str(data_dir), VIOLATION_LOG_FILE)
        self._logger = logger
        self.max_entries = max_entries
        self.entries: List[dict] = []  # [{gid, uid, text, reason, source, ts}]
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self.entries = [e for e in data if isinstance(e, dict)]
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            self.entries = []

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.entries, f, ensure_ascii=False, indent=2)
        except Exception as e:
            if self._logger:
                self._logger.error(f"违规日志持久化失败: {e}")

    def add(self, group_id, user_id, text: str, reason: str, source: str) -> None:
        """新增一条违规记录，超量丢弃最旧的。"""
        self.entries.insert(0, {
            "gid": str(group_id),
            "uid": str(user_id),
            "text": str(text)[:200],  # 截断防超长
            "reason": str(reason)[:100],
            "source": str(source),
            "ts": int(time.time()),
        })
        del self.entries[self.max_entries:]
        self.save()

    def query(self, group_id=None, user_id=None) -> List[dict]:
        """按群/用户过滤日志，group_id 为空返回全部。"""
        gid = str(group_id) if group_id else None
        uid = str(user_id) if user_id else None
        return [
            e for e in self.entries
            if (gid is None or e.get("gid") == gid) and (uid is None or e.get("uid") == uid)
        ]

    def clear(self, group_id=None, user_id=None) -> None:
        """清空日志；指定群/用户则只清对应记录。"""
        if group_id is None and user_id is None:
            self.entries = []
        else:
            self.entries = [
                e for e in self.entries
                if not (
                    (group_id is None or e.get("gid") == str(group_id))
                    and (user_id is None or e.get("uid") == str(user_id))
                )
            ]
        self.save()