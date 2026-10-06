"""QQ 号与 OID 的全局绑定关系，供手动管理、入群验证和同 OID 禁言使用。"""

from __future__ import annotations

import json
import os
import re

OID_BINDINGS_FILE = "oid_bindings.json"
_ASCII_DIGITS = re.compile(r"^[0-9]+$")


def _normalize_digits(value, label: str, min_length: int = 1) -> str:
    value = str(value or "").strip()
    if not _ASCII_DIGITS.fullmatch(value):
        raise ValueError(f"{label}必须是纯数字")
    if len(value) < min_length:
        raise ValueError(f"{label}至少需要 {min_length} 位")
    return value


class OidBindingStore:
    """按 QQ 号存储 OID（一号一 OID），并提供按 OID 查询关联 QQ 的接口。"""

    def __init__(self, data_dir: str, logger=None, filename: str = OID_BINDINGS_FILE):
        self.path = os.path.join(str(data_dir), filename)
        self._logger = logger
        self.bindings: dict[str, str] = {}  # user_id -> oid
        self.load()

    @staticmethod
    def normalize_user_id(user_id) -> str:
        return _normalize_digits(user_id, "QQ 号")

    @staticmethod
    def normalize_oid(oid) -> str:
        # 与入群验证的 OID 规则一致：至少 4 位纯数字。
        return _normalize_digits(oid, "OID", min_length=4)

    def load(self) -> None:
        self.bindings = {}
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        for user_id, oid in data.items():
            try:
                uid = self.normalize_user_id(user_id)
                normalized_oid = self.normalize_oid(oid)
            except (TypeError, ValueError):
                continue
            self.bindings[uid] = normalized_oid

    def save(self) -> None:
        """原子写入文件；保存失败时抛出异常，由调用方决定是否回滚/提示。"""
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        temp_path = f"{self.path}.tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(self.bindings, f, ensure_ascii=False, indent=2)
            os.replace(temp_path, self.path)
        except Exception as e:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass
            if self._logger:
                self._logger.error(f"OID 绑定关系持久化失败: {e}")
            raise

    def bind(self, user_id, oid) -> str | None:
        """新增或更新一个绑定，返回更改前的 OID；完全相同的绑定不重复落盘。"""
        uid = self.normalize_user_id(user_id)
        normalized_oid = self.normalize_oid(oid)
        previous_oid = self.bindings.get(uid)
        if previous_oid == normalized_oid:
            return previous_oid

        self.bindings[uid] = normalized_oid
        try:
            self.save()
        except Exception:
            if previous_oid is None:
                self.bindings.pop(uid, None)
            else:
                self.bindings[uid] = previous_oid
            raise
        return previous_oid

    def unbind(self, user_id) -> bool:
        """删除一个 QQ 号的绑定；返回是否有记录被删除。"""
        uid = self.normalize_user_id(user_id)
        previous_oid = self.bindings.pop(uid, None)
        if previous_oid is None:
            return False
        try:
            self.save()
        except Exception:
            self.bindings[uid] = previous_oid
            raise
        return True

    def get_oid(self, user_id) -> str:
        try:
            uid = self.normalize_user_id(user_id)
        except (TypeError, ValueError):
            return ""
        return self.bindings.get(uid, "")

    def users_for_oid(self, oid) -> list[str]:
        try:
            normalized_oid = self.normalize_oid(oid)
        except (TypeError, ValueError):
            return []
        return sorted(uid for uid, value in self.bindings.items() if value == normalized_oid)

    def list_bindings(self) -> list[dict[str, str]]:
        return [
            {"user_id": uid, "oid": oid}
            for uid, oid in sorted(self.bindings.items(), key=lambda item: (item[1], item[0]))
        ]

    def clear(self) -> int:
        """清空所有绑定，返回清除条数。"""
        count = len(self.bindings)
        if not count:
            return 0
        previous = self.bindings
        self.bindings = {}
        try:
            self.save()
        except Exception:
            self.bindings = previous
            raise
        return count