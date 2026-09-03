# core/message_guard.py
"""群消息审核守卫（aiocqhttp/OneBot）：LLM 判定违规后自动撤回/禁言。

处置机制借鉴 astrbot_plugin_sentinel：
- 撤回: call_action("delete_msg") —— OneBot 下可撤回普通成员消息
- 禁言: call_action("set_group_ban")
- 豁免: 群主/管理员（按 raw_message.sender.role）、AstrBot 管理员、白名单
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Optional

from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .llm_reviewer import LLMReviewer
from .violation_tracker import KEYWORD_COUNTS_FILE, ViolationLog, ViolationTracker


class MessageGuard:
    def __init__(self, config: dict, reviewer: LLMReviewer, data_dir=None, gconf_provider=None):
        self.config = config
        self.reviewer = reviewer
        # 按群取配置的回调（由插件传入 _gconf），缺省回退到顶层 config
        self._gconf_provider = gconf_provider
        self.violation_tracker = ViolationTracker(data_dir, logger) if data_dir else None
        # 关键词违规独立计数，与 LLM 违规分开累计
        self.keyword_tracker = (
            ViolationTracker(data_dir, logger, filename=KEYWORD_COUNTS_FILE) if data_dir else None
        )
        # 违规消息日志：记录原文供 WebUI 查看
        self.violation_log = ViolationLog(data_dir, logger) if data_dir else None
        self._last_check: dict[str, float] = {}  # sender_id -> ts
        self._sem = asyncio.Semaphore(2)  # 限制 LLM 审核并发

    def schedule(self, event: AiocqhttpMessageEvent) -> None:
        """后台执行审核，不阻塞消息事件处理。"""
        try:
            asyncio.create_task(self._guarded(event))
        except RuntimeError as exc:
            logger.error(f"[MessageGuard] 无法创建审核任务（不在事件循环中）: {exc}")

    async def _guarded(self, event: AiocqhttpMessageEvent) -> None:
        try:
            async with self._sem:
                await self._handle(event)
        except Exception as exc:
            logger.error(f"[MessageGuard] 审核异常: {exc}")

    def _whitelisted(self, user_id: str, gconf: dict) -> bool:
        return user_id in {str(u).strip() for u in gconf.get("user_whitelist", []) if str(u).strip()}

    def _match_keyword(self, text: str, gconf: dict) -> Optional[str]:
        """返回消息命中的第一个关键词，未命中返回 None。"""
        for kw in gconf.get("keyword_list") or []:
            kw = str(kw).strip()
            if kw and kw in text:
                return kw
        return None

    async def _handle(self, event: AiocqhttpMessageEvent) -> None:
        text = (event.message_str or "").strip()
        if not text or text.startswith("/"):
            return  # 空消息与指令消息不审核

        group_id = event.get_group_id()
        user_id = str(event.get_sender_id())
        if not group_id or not user_id:
            return
        # 每群独立配置：由插件按群惰性创建并补齐默认值
        gconf = self._gconf_provider(group_id) if self._gconf_provider else self.config

        if event.is_admin() or self._whitelisted(user_id, gconf):
            return  # AstrBot 管理员与白名单豁免
        raw_message = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw_message, dict):
            role = str((raw_message.get("sender") or {}).get("role") or "member").lower()
            if role in ("owner", "admin"):
                return  # 群主/管理员豁免

        # 关键词检测：独立开关，本地匹配无成本不节流；命中即处置并结束
        if gconf.get("keyword_guard_enable"):
            keyword = self._match_keyword(text, gconf)
            if keyword:
                logger.info(
                    f"[MessageGuard] 群 {group_id} 成员 {user_id} 命中关键词 {keyword!r}，按违规处置"
                )
                message_id = getattr(event.message_obj, "message_id", None)
                await self._apply_action(
                    event,
                    group_id,
                    user_id,
                    message_id,
                    text,
                    reason=f"触发关键词：{keyword}",
                    tracker=self.keyword_tracker,
                    source="keyword",
                    gconf=gconf,
                )
                return

        # LLM 审核：独立开关，与关键词检测互不影响
        if not gconf.get("guard_enable"):
            return
        if not self.reviewer.enabled():
            logger.info(
                "[MessageGuard] LLM 服务池为空或配置不完整，跳过审核"
            )
            return

        # 节流：guard_interval<=0 表示关闭，每条消息都审核
        raw_interval = gconf.get("guard_interval")
        if raw_interval in (None, ""):
            interval = 30
        else:
            try:
                interval = int(raw_interval)
            except (TypeError, ValueError):
                interval = 30
        if interval > 0:
            now = time.time()
            if now - self._last_check.get(user_id, 0) < interval:
                return
            self._last_check[user_id] = now

        verdict = await self.reviewer.judge_message(
            user_id, text,
            prompt=gconf.get("guard_prompt") or "",
            chat_id=gconf.get("llm_chat") or "",
            fallback_chat_id=gconf.get("llm_chat_fallback") or "",
        )
        if verdict is None:
            logger.warning(
                f"[MessageGuard] LLM 审核无结果，保守跳过: 群 {group_id} 用户 {user_id}。"
                f"原因：{self.reviewer.last_error or '未知'}"
            )
            return
        if verdict.get("source") == "risk_block":
            # 服务端风控拒绝 → 消息被服务端判定为高风险，按配置视为违规处置
            if not gconf.get("guard_risk_as_violation", True):
                logger.info(
                    f"[MessageGuard] 风控拦截但已配置不视为违规，保守跳过: 群 {group_id} 用户 {user_id}"
                )
                return
            logger.info(
                f"[MessageGuard] 服务端风控拦截，视为违规: 群 {group_id} 用户 {user_id}"
            )
        elif bool(verdict.get("allowed")):
            logger.info(f"[MessageGuard] 群 {group_id} 成员 {user_id} 消息判定合规，不处置")
            return

        reason = str(verdict.get("reason") or "违规发言")[:100]
        message_id = getattr(event.message_obj, "message_id", None)
        await self._apply_action(
            event, group_id, user_id, message_id, text,
            reason=reason, tracker=self.violation_tracker, source="llm", gconf=gconf,
        )

    async def _apply_action(
        self,
        event: AiocqhttpMessageEvent,
        group_id,
        user_id: str,
        message_id,
        text: str,
        reason: str = "违规发言",
        tracker: Optional[ViolationTracker] = None,
        source: str = "llm",
        gconf: Optional[dict] = None,
    ) -> None:
        gconf = gconf or (self._gconf_provider(group_id) if self._gconf_provider else self.config)
        action = (gconf.get("guard_action") or "ban").lower()
        bot = event.bot

        # 阶梯计数：本次违规累计次数（LLM 与关键词各自独立计数）
        count = 1
        if tracker is not None:
            count = tracker.add(group_id, user_id)
        logger.info(f"[MessageGuard] 群 {group_id} 成员 {user_id} 违规: {reason}")

        # 记录违规消息日志供 WebUI 查看
        if self.violation_log is not None:
            self.violation_log.add(group_id, user_id, text, reason, source)

        # 是否禁言：ban/recall_and_ban 直接禁言；纯撤回模式下撤回达到阈值后禁言
        threshold = int(gconf.get("guard_recall_ban_threshold") or 0)
        do_ban = action in ("ban", "recall_and_ban")
        if action == "recall" and threshold > 0 and count >= threshold:
            do_ban = True

        if action in ("recall", "recall_and_ban") and message_id is not None:
            try:
                await bot.api.call_action("delete_msg", message_id=int(message_id))
                logger.info(f"[MessageGuard] 已撤回群 {group_id} 中用户 {user_id} 的违规消息（累计 {count} 次）")
            except Exception as exc:
                logger.warning(f"[MessageGuard] 撤回失败: {exc}。请确认 Bot 具有管理员权限。")

        duration = 0
        if do_ban:
            duration = self._stair_duration(count, gconf)
            if duration > 0:
                try:
                    await bot.api.call_action(
                        "set_group_ban",
                        group_id=int(group_id),
                        user_id=int(user_id),
                        duration=duration,
                    )
                    logger.info(
                        f"[MessageGuard] 已禁言群 {group_id} 中用户 {user_id}，时长: {duration}秒"
                        f"（第 {count} 次违规）"
                    )
                except Exception as exc:
                    logger.warning(f"[MessageGuard] 禁言失败: {exc}。请确认 Bot 具有管理员权限。")

        notice = str(gconf.get("guard_notice") or "").strip()
        if notice:
            try:
                await bot.send_group_msg(
                    group_id=int(group_id),
                    message=notice.replace("{user_id}", user_id)
                    .replace("{duration}", str(duration))
                    .replace("{count}", str(count)),
                )
            except Exception as exc:
                logger.warning(f"[MessageGuard] 违规通知发送失败: {exc}")

    def _stair_duration(self, count: int, gconf: dict) -> int:
        """阶梯禁言时长：第 N 次违规 = 基础时长 × 倍数^(N-1)，封顶。"""
        base = self._parse_duration(str(gconf.get("guard_ban_seconds") or "600"))
        if base <= 0:
            return 0
        if not gconf.get("guard_stair_enable", True):
            return base
        multiplier = int(gconf.get("guard_stair_multiplier") or 2)
        cap = int(gconf.get("guard_stair_max_seconds") or 86400)
        return min(base * (multiplier ** max(count - 1, 0)), cap)

    @staticmethod
    def _parse_duration(raw: str) -> int:
        """解析禁言时长，支持固定秒数或范围如 '30-120'（随机）。"""
        raw = raw.strip()
        if not raw:
            return 0
        if "-" in raw and not raw.startswith("-"):
            try:
                start, end = map(int, raw.split("-", 1))
                return random.randint(min(start, end), max(start, end))
            except ValueError:
                return 0
        try:
            return int(float(raw))
        except (ValueError, TypeError):
            return 0