"""QQ 号与 OID 的全局绑定关系，供手动管理、入群验证和同 OID 禁言使用。"""

from __future__ import annotations

import asyncio
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
async def ban_oid_peers(
    bot,
    group_id,
    user_id,
    duration,
    bindings: OidBindingStore,
    self_id=None,
    logger=None,
) -> int:
    """同步禁言当前群内同 OID 的其他绑定成员，返回成功禁言数。"""
    try:
        duration = int(duration)
        primary_uid = bindings.normalize_user_id(user_id)
        group_id_int = int(group_id)
    except (TypeError, ValueError):
        return 0
    if duration <= 0:
        return 0
    oid = bindings.get_oid(primary_uid)
    if not oid:
        return 0
    try:
        bot_uid = bindings.normalize_user_id(self_id) if self_id else ""
    except (TypeError, ValueError):
        bot_uid = ""

    peer_ids = [
        uid for uid in bindings.users_for_oid(oid)
        if uid != primary_uid and uid != bot_uid
    ]
    if not peer_ids:
        return 0

    def _log(level: str, message: str) -> None:
        method = getattr(logger, level, None) if logger is not None else None
        if callable(method):
            method(message)

    async def _ban_peer(peer_uid: str) -> bool:
        # 查询成员信息以确认账号属于当前群，并且不越过群主/管理员保护。
        try:
            info = await bot.get_group_member_info(
                group_id=group_id_int, user_id=int(peer_uid)
            )
        except Exception as e:
            _log(
                "debug",
                f"[Guard] 跳过同 OID 账号 {peer_uid}：无法确认其在群 {group_id} 中的成员身份: {e}",
            )
            return False
        if not isinstance(info, dict):
            return False
        actual_uid = str(info.get("user_id") or peer_uid).strip()
        if actual_uid != peer_uid:
            return False
        role = str(info.get("role") or "member").strip().lower()
        if role in ("owner", "admin"):
            kind = "群主" if role == "owner" else "群管理员"
            _log("info", f"[Guard] 跳过同 OID 账号 {peer_uid}：群 {group_id} 中为{kind}")
            return False
        try:
            await bot.api.call_action(
                "set_group_ban",
                group_id=group_id_int,
                user_id=int(peer_uid),
                duration=duration,
            )
        except Exception as e:
            _log(
                "warning",
                f"[Guard] 同 OID 账号禁言失败：群 {group_id} 用户 {peer_uid}，时长 {duration} 秒: {e}",
            )
            return False
        _log(
            "info",
            f"[Guard] 已同步禁言群 {group_id} 中同 OID 账号 {peer_uid}（OID={oid}），时长 {duration} 秒",
        )
        return True

    results = await asyncio.gather(*(_ban_peer(uid) for uid in peer_ids), return_exceptions=True)
    for uid, result in zip(peer_ids, results):
        if isinstance(result, Exception):
            _log("warning", f"[Guard] 同 OID 账号 {uid} 禁言任务异常: {result}")
    return sum(result is True for result in results)
