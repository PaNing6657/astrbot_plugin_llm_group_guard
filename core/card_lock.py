# core/card_lock.py
"""群名片锁定：规则规范化与锁定引擎。

锁定语义：把指定成员的群名片固定为指定值，一旦该名片被改动（本人改、管理员改、
后台批量改），机器人立即改回锁定值。

实现参考 astrbot_plugin_gcard_keeper 的 protected_members 能力，保留其两个关键设计：
1. 事件驱动为主：直接在 aiocqhttp 原生 ``group_card`` notice 上挂钩，事件到达即恢复；
2. 定时轮询为兜底：事件可能丢失（后台改名片、非管理器改、部分 CQP 实现不推事件），
   轮询比对锁定值补上缺口。

本模块不 import astrbot，可脱离运行时离线单测。
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from typing import Any, Callable, Optional

from .text_utils import build_text_with_at

MAX_CARD_LENGTH = 64  # QQ 群名片长度上限，超出部分会被平台截断
MAX_NOTE_LENGTH = 60  # 备注仅用于管理台展示，限长避免撑爆表格
MIN_QQ_LENGTH = 5  # QQ 号最短长度
MAX_QQ_LENGTH = 15  # QQ 号最长长度（预留余量）
SELF_WRITE_TTL = 10.0  # 机器人自己写名片后，忽略该成员名片事件的窗口（秒）


def _as_text(value: Any) -> str:
    return "" if value is None else str(value)


def normalize_lock_rules(raw: Any) -> list[dict]:
    """把配置中的锁定规则规范化为 ``[{"user_id", "card", "note"}]``。

    兼容三种形态：dict 列表、整段 JSON 字符串、JSON 字符串元素列表
    （AstrBot 各版本对 list[object] 的序列化方式不一致）。同一 QQ 号多次出现时后者覆盖前者。
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    if not isinstance(raw, (list, tuple)):
        return []
    merged: dict[str, dict] = {}
    order: list[str] = []
    for entry in raw:
        if isinstance(entry, str):
            try:
                entry = json.loads(entry)
            except (ValueError, TypeError):
                continue
        if not isinstance(entry, Mapping):
            continue
        uid = _as_text(entry.get("user_id")).strip()
        if not uid:
            continue
        if uid not in merged:
            order.append(uid)
        merged[uid] = {
            "user_id": uid,
            "card": _as_text(entry.get("card")).strip()[:MAX_CARD_LENGTH],
            "note": _as_text(entry.get("note")).strip()[:MAX_NOTE_LENGTH],
        }
    return [merged[uid] for uid in order]


def validate_lock_rule(user_id: Any, card: Any) -> Optional[str]:
    """校验单条锁定规则，返回错误信息；合法则返回 None。"""
    uid = _as_text(user_id).strip()
    if not uid:
        return "QQ 号不能为空"
    if not uid.isdigit():
        return f"QQ 号「{uid}」无效，必须是纯数字"
    if not MIN_QQ_LENGTH <= len(uid) <= MAX_QQ_LENGTH:
        return f"QQ 号「{uid}」长度异常（应为 {MIN_QQ_LENGTH}-{MAX_QQ_LENGTH} 位数字）"
    if not _as_text(card).strip():
        return "锁定名片不能为空（如需清空某人名片，请手动在群管理中操作）"
    if len(_as_text(card).strip()) > MAX_CARD_LENGTH:
        return f"锁定名片超过 {MAX_CARD_LENGTH} 字上限"
    return None


def find_lock_rule(rules: Any, user_id: Any) -> Optional[dict]:
    """在规则列表中按 QQ 号查找锁定规则，未命中返回 None。"""
    uid = _as_text(user_id).strip()
    if not uid:
        return None
    for rule in normalize_lock_rules(rules):
        if rule["user_id"] == uid:
            return rule
    return None


def is_card_locked(rules: Any, user_id: Any, current_card: Any) -> bool:
    """当前名片是否已偏离锁定值（偏离即需要恢复）。未锁定该成员返回 False。"""
    rule = find_lock_rule(rules, user_id)
    if rule is None:
        return False
    return _as_text(current_card).strip() != rule["card"]


class CardLockEngine:
    """锁定引擎：原生 notice 事件驱动 + 定时轮询兜底。

    依赖通过构造参数注入（配置读取、权限判定、bot 提供器、通知发送），
    本类不感知 AstrBot 结构，便于离线单测。
    """

    def __init__(
        self,
        logger: Any,
        gconf_provider: Callable[[str], dict],
        is_managed: Callable[[str], bool],
        client_provider: Callable[[], Any],
        notifier: Optional[Callable[[str, str], Any]] = None,
        warn_unmanaged: Optional[Callable[[str], None]] = None,
        groups_provider: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.logger = logger
        self.gconf_provider = gconf_provider
        self.is_managed = is_managed
        self.client_provider = client_provider
        self.notifier = notifier
        self.warn_unmanaged = warn_unmanaged
        self.groups_provider = groups_provider
        # 机器人自身写名片记录 {(gid, uid): (card, ts)}：用于抑制回声事件，避免反复横跳
        self._self_writes: dict[tuple[str, str], tuple[str, float]] = {}
        self._warned_groups: set[str] = set()

    # ------------------------------------------------------------------
    # 规则读取
    # ------------------------------------------------------------------
    def rules_of(self, group_id: Any) -> list[dict]:
        """取某群生效的锁定规则（总开关关闭时返回空列表）。"""
        gid = _as_text(group_id).strip()
        if not gid:
            return []
        try:
            gconf = self.gconf_provider(gid) or {}
        except Exception as e:
            self.logger.warning(f"[Guard] 读取群 {gid} 名片锁定配置失败: {e}")
            return []
        if not gconf.get("card_lock_enable"):
            return []
        return normalize_lock_rules(gconf.get("card_lock_list"))

    def locked_groups(self) -> set[str]:
        """所有存在有效锁定规则的群（供轮询遍历与数据清理使用）。"""
        if self.groups_provider is None:
            return set()
        try:
            groups = self.groups_provider()
        except Exception as e:
            self.logger.warning(f"[Guard] 枚举名片锁定群失败: {e}")
            return set()
        if not isinstance(groups, Mapping):
            return set()
        return {str(gid) for gid in groups if self.rules_of(gid)}

    # ------------------------------------------------------------------
    # 回声抑制
    # ------------------------------------------------------------------
    def mark_self_write(self, group_id: Any, user_id: Any, card: str) -> None:
        """记录"机器人刚把该成员名片写成 card"，用于忽略随后到达的同源事件。"""
        self._self_writes[(str(group_id), str(user_id))] = (str(card), time.time())
        self._purge_expired_self_writes()

    def _purge_expired_self_writes(self) -> None:
        cutoff = time.time() - SELF_WRITE_TTL
        expired = [k for k, (_, ts) in self._self_writes.items() if ts < cutoff]
        for key in expired:
            self._self_writes.pop(key, None)

    def consume_self_write(self, group_id: Any, user_id: Any) -> bool:
        """若该成员处于"机器人刚写过"的窗口内，视为回声事件并返回 True。"""
        key = (str(group_id), str(user_id))
        record = self._self_writes.get(key)
        if record is None:
            return False
        _, ts = record
        if time.time() - ts > SELF_WRITE_TTL:
            self._self_writes.pop(key, None)
            return False
        self._self_writes.pop(key, None)
        return True

    def clear_group_state(self, group_id: Any) -> None:
        """清理某群的全部引擎状态（删群配置时调用）。"""
        gid = str(group_id)
        for key in [k for k in self._self_writes if k[0] == gid]:
            self._self_writes.pop(key, None)
        self._warned_groups.discard(gid)

    # ------------------------------------------------------------------
    # 事件入口：aiocqhttp 原生 group_card notice
    # ------------------------------------------------------------------
    async def handle_card_notice(self, event: Any) -> Optional[dict]:
        """处理群名片变更通知；无需处理时返回 None，否则返回处理结果摘要。"""
        if not isinstance(event, Mapping):
            return None
        if event.get("post_type") != "notice" or event.get("notice_type") != "group_card":
            return None
        gid = _as_text(event.get("group_id")).strip()
        uid = _as_text(event.get("user_id")).strip()
        old_card = _as_text(event.get("card_old")).strip()
        new_card = _as_text(event.get("card_new")).strip()
        if not gid or not uid or old_card == new_card:
            return None
        rules = self.rules_of(gid)
        if not rules:
            return None
        rule = find_lock_rule(rules, uid)
        if rule is None:
            return None  # 未锁定该成员，交由其他功能处理
        if new_card == rule["card"]:
            return None  # 当前值已等于锁定值（例如管理员手动改回），无需动作
        if not self.is_managed(gid):
            if gid not in self._warned_groups:
                self._warned_groups.add(gid)
                if self.warn_unmanaged:
                    self.warn_unmanaged(gid)
            return None  # 机器人非管理员无权改他人名片
        if self.consume_self_write(gid, uid):
            return None  # 机器人自身写入引发的回声事件
        ok, message = await self.enforce(gid, rule, reason="notice", old_card=old_card)
        return {
            "action": "restore",
            "group_id": gid,
            "user_id": uid,
            "old_card": old_card,
            "new_card": new_card,
            "locked_card": rule["card"],
            "ok": ok,
            "message": message,
        }

    # ------------------------------------------------------------------
    # 轮询兜底
    # ------------------------------------------------------------------
    async def poll_group(self, group_id: Any, rules: Optional[list] = None) -> int:
        """轮询单群成员名片，与锁定值比对；返回本轮恢复的成员数。"""
        gid = str(group_id).strip()
        if not gid:
            return 0
        rules = self.rules_of(gid) if rules is None else rules
        if not rules:
            return 0
        if not self.is_managed(gid):
            if gid not in self._warned_groups:
                self._warned_groups.add(gid)
                if self.warn_unmanaged:
                    self.warn_unmanaged(gid)
            return 0
        client = self.client_provider()
        if client is None:
            return 0
        try:
            members = await client.api.call_action(
                "get_group_member_list", group_id=int(gid), no_cache=True
            )
        except Exception as e:
            self.logger.warning(f"[Guard] 群 {gid} 轮询成员名片失败: {e}")
            return 0
        if not isinstance(members, (list, tuple)):
            return 0
        cards: dict[str, str] = {}
        for member in members:
            if not isinstance(member, Mapping):
                continue
            uid = _as_text(member.get("user_id")).strip()
            if uid:
                cards[uid] = _as_text(member.get("card")).strip()
        restored = 0
        for rule in rules:
            uid = rule["user_id"]
            if uid not in cards:
                continue  # 该成员已不在群内
            if cards[uid] == rule["card"]:
                continue
            if self.consume_self_write(gid, uid):
                continue
            ok, _ = await self.enforce(
                gid, rule, reason="poll", old_card=cards[uid], nickname_hint=uid
            )
            if ok:
                restored += 1
        if restored:
            self.logger.info(f"[Guard] 群 {gid} 名片锁定轮询恢复了 {restored} 名成员的群名片")
        return restored

    # ------------------------------------------------------------------
    # 执行恢复
    # ------------------------------------------------------------------
    async def enforce(
        self,
        group_id: Any,
        rule: Mapping,
        reason: str = "manual",
        old_card: str = "",
        nickname_hint: str = "",
    ) -> tuple[bool, str]:
        """把某群某成员的群名片改回锁定值，成功后按配置发送群内通知。"""
        gid = str(group_id).strip()
        uid = _as_text(rule.get("user_id")).strip()
        card = _as_text(rule.get("card")).strip()
        if not gid or not uid or not card:
            return False, "锁定规则不完整（缺少群号 / QQ 号 / 锁定名片）"
        client = self.client_provider()
        if client is None:
            return False, "未找到可用的机器人连接，等待下一次重试"
        nickname = nickname_hint
        if not nickname:
            nickname = await self._fetch_nickname(client, gid, uid)
        # 先登记回声窗口，避免自身写入触发的事件被当成"他人改动"再次处理
        self.mark_self_write(gid, uid, card)
        try:
            await client.api.call_action(
                "set_group_card", group_id=int(gid), user_id=int(uid), card=card
            )
        except Exception as e:
            self.logger.warning(f"[Guard] 恢复群 {gid} 成员 {uid} 名片失败: {e}")
            return False, f"修改名片失败：{e}"
        self.logger.info(
            f"[Guard] 群 {gid} 成员 {uid} 的群名片已恢复为锁定值「{card}」（触发方式：{reason}）"
        )
        await self._notify(gid, uid, nickname, old_card, card)
        return True, f"已将 {uid} 的群名片恢复为「{card}」"

    async def _fetch_nickname(self, client: Any, group_id: str, user_id: str) -> str:
        """查询成员昵称；失败时退化为 QQ 号，保证通知模板仍可用。"""
        try:
            info = await client.api.call_action(
                "get_group_member_info", group_id=int(group_id), user_id=int(user_id)
            )
        except Exception as e:
            self.logger.debug(f"[Guard] 查询群 {group_id} 成员 {user_id} 昵称失败: {e}")
            return user_id
        if isinstance(info, Mapping):
            return _as_text(info.get("nickname") or info.get("card") or "").strip() or user_id
        return user_id

    async def _notify(
        self, group_id: str, user_id: str, nickname: str, old_card: str, card: str
    ) -> None:
        """按该群配置发送"名片已恢复"群内通知；未开启或发送失败均不影响锁定本身。"""
        try:
            gconf = self.gconf_provider(group_id) or {}
        except Exception as e:
            self.logger.warning(f"[Guard] 读取群 {group_id} 通知配置失败: {e}")
            return
        if not gconf.get("card_lock_notify", True):
            return
        template = _as_text(gconf.get("card_lock_notify_msg") or "").strip()
        if not template:
            return
        text = build_text_with_at(
            template,
            {
                "{user_id}": user_id,
                "{nickname}": nickname,
                "{old_card}": old_card or "（空）",
                "{new_card}": card,
                "{card}": card,
            },
            user_id,
        )
        if not text.strip():
            return
        if self.notifier is not None:
            try:
                result = self.notifier(group_id, text)
                if hasattr(result, "__await__"):
                    await result
            except Exception as e:
                self.logger.warning(f"[Guard] 群 {group_id} 名片锁定通知发送失败: {e}")
            return
        client = self.client_provider()
        if client is None:
            return
        try:
            await client.send_group_msg(group_id=int(group_id), message=text)
        except Exception as e:
            self.logger.warning(f"[Guard] 群 {group_id} 名片锁定通知发送失败: {e}")
