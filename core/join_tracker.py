"""进群记录：记录每一次自动「同意 / 拒绝」入群操作，供 WebUI「进群记录」列表展示。

与退群记录一样按群独立持久化（每群最多保留最近 N 条），插件重启后仍可查看。
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional

JOIN_RECORDS_FILE = "join_records.json"
MAX_RECORDS_PER_GROUP = 500  # 每群最多保留的操作记录条数（超出丢弃最旧）
VALID_ACTIONS = ("approve", "reject")


class JoinTracker:
    def __init__(self, data_dir: str, logger=None, filename: str = JOIN_RECORDS_FILE):
        self.path = os.path.join(str(data_dir), filename)
        self._logger = logger
        self.records: dict[str, list[dict]] = {}  # gid -> [记录(按时间正序)]
        self._seq = 0  # 记录自增 ID（跨群唯一，供前端定位）
        self.load()

    def load(self):
        self.records = {}
        self._seq = 0
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        for gid, rows in data.items():
            if not isinstance(rows, list):
                continue
            kept = []
            for row in rows[-MAX_RECORDS_PER_GROUP:]:
                if not isinstance(row, dict):
                    continue
                action = str(row.get("action") or "").strip().lower()
                if action not in VALID_ACTIONS:
                    continue
                try:
                    record_id = int(row.get("id") or 0)
                    ts = float(row.get("ts") or 0)
                except (TypeError, ValueError):
                    continue
                kept.append({
                    "id": record_id,
                    "user_id": str(row.get("user_id") or ""),
                    "nickname": str(row.get("nickname") or ""),
                    "action": action,
                    "source": str(row.get("source") or ""),
                    "reason": str(row.get("reason") or ""),
                    "comment": str(row.get("comment") or ""),
                    "ok": bool(row.get("ok", True)),
                    "ts": ts,
                })
                self._seq = max(self._seq, record_id)
            if kept:
                self.records[str(gid)] = kept

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.records, f, ensure_ascii=False, indent=2)
        except Exception as e:
            if self._logger:
                self._logger.error(f"进群记录持久化失败: {e}")

    def record(
        self,
        group_id,
        user_id,
        action: str,
        nickname: str = "",
        reason: str = "",
        source: str = "",
        ok: bool = True,
        comment: str = "",
        ts: Optional[float] = None,
    ) -> dict:
        """记录一次同意/拒绝操作，返回写入的记录（action 非法时返回空 dict）。"""
        action = str(action or "").strip().lower()
        if action not in VALID_ACTIONS:
            return {}
        self._seq += 1
        row = {
            "id": self._seq,
            "user_id": str(user_id),
            "nickname": str(nickname or ""),
            "action": action,
            "source": str(source or ""),
            "reason": str(reason or ""),
            "comment": str(comment or ""),
            "ok": bool(ok),
            "ts": float(ts) if ts is not None else time.time(),
        }
        rows = self.records.setdefault(str(group_id), [])
        rows.append(row)
        if len(rows) > MAX_RECORDS_PER_GROUP:
            del rows[: len(rows) - MAX_RECORDS_PER_GROUP]
        self.save()
        return row

    def group_records(self, group_id, limit: int = 200) -> list:
        """返回该群进群记录（新→旧），最多 limit 条。"""
        rows = self.records.get(str(group_id)) or []
        try:
            count = max(0, int(limit))
        except (TypeError, ValueError):
            count = 200
        if count <= 0:
            return []
        return [dict(r) for r in reversed(rows[-count:])]

    def clear(self, group_id) -> bool:
        gid = str(group_id)
        if gid not in self.records:
            return False
        self.records.pop(gid, None)
        self.save()
        return True