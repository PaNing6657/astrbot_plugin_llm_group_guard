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
        # 最近已完整审过的消息键（预审与后台共用，防止同一消息被处理两次）
        self._handled: list[str] = []

    def schedule(self, event: AiocqhttpMessageEvent) -> None:
        """后台执行审核，不阻塞消息事件处理。"""
        try:
            asyncio.create_task(self._guarded(event))
        except RuntimeError as exc:
            logger.error(f"[MessageGuard] 无法创建审核任务（不在事件循环中）: {exc}")

    async def pre_review(self, event: AiocqhttpMessageEvent) -> bool:
        """AI 会话回复前的先审：返回 True 表示消息违规（应拦截该次回复）。"""
        try:
            async with self._sem:
                return await self._handle(event)
        except Exception as exc:
            logger.error(f"[MessageGuard] 预审异常: {exc}")
            return False

    async def _guarded(self, event: AiocqhttpMessageEvent) -> None:
        try:
            async with self._sem:
                await self._handle(event)
        except Exception as exc:
            logger.error(f"[MessageGuard] 审核异常: {exc}")

    def _msg_key(self, event: AiocqhttpMessageEvent) -> str:
        """消息去重键：优先 message_id，缺失时用 群+用户+文本 兜底。"""
        mid = getattr(event.message_obj, "message_id", None)
        gid, uid = event.get_group_id(), event.get_sender_id()
        if mid:
            return f"{gid}:{uid}:{mid}"
        return f"{gid}:{uid}:{event.message_str or ''}"

    def _mark_handled(self, key: str) -> None:
        self._handled.append(key)
        if len(self._handled) > 300:
            del self._handled[:-300]  # 裁剪，防止无限增长

    def _is_handled(self, key: str) -> bool:
        return key in self._handled

    def _whitelisted(self, user_id: str, gconf: dict) -> bool:
        return user_id in {str(u).strip() for u in gconf.get("user_whitelist", []) if str(u).strip()}

    def _match_keyword(self, text: str, gconf: dict) -> Optional[str]:
        """返回消息命中的第一个关键词，未命中返回 None。"""
        for kw in gconf.get("keyword_list") or []:
            kw = str(kw).strip()
            if kw and kw in text:
                return kw
        return None

    def _extract_image_urls(self, event: AiocqhttpMessageEvent) -> list[str]:
        """从消息中提取图片来源 URL（消息段 image 优先，CQ 码兜底）。"""
        urls = []
        msg_obj = getattr(event, "message_obj", None)
        segs = getattr(msg_obj, "message", None)
        if isinstance(segs, dict):
            segs = [segs]
        elif segs is not None and not isinstance(segs, list):
            segs = list(getattr(segs, "segments", None) or [])
        for seg in segs or []:
            if isinstance(seg, dict):
                stype, data = seg.get("type"), seg.get("data") or {}
            else:
                stype, data = getattr(seg, "type", None), getattr(seg, "data", None) or {}
            if stype == "image":
                url = str(data.get("url") or "").strip()
                if url and url not in urls:
                    urls.append(url)
        if not urls:
            import re as _re
            for m in _re.finditer(r"\[CQ:image[^\]]*url=([^,\]]+)", event.message_str or ""):
                u = m.group(1).strip()
                if u and u not in urls and not u.startswith(("file://", "base64")):
                    urls.append(u)
        return urls[:3]  # 最多转述 3 张

    async def _handle(self, event: AiocqhttpMessageEvent) -> bool:
        """完整审核一条消息（关键字/LLM+处置），返回 True 表示违规（应拦截回复）。"""
        text = (event.message_str or "").strip()
        if not text or text.startswith("/"):
            return False  # 空消息与指令消息不审核
        group_id = event.get_group_id()
        user_id = str(event.get_sender_id())
        if not group_id or not user_id:
            return False
        key = self._msg_key(event)
        if self._is_handled(key):
            return False  # 该消息已被预审/后台完整处理过，跳过（去重）
        # 每群独立配置：由插件按群惰性创建并补齐默认值
        gconf = self._gconf_provider(group_id) if self._gconf_provider else self.config

        if event.is_admin() or self._whitelisted(user_id, gconf):
            return False  # AstrBot 管理员与白名单豁免
        raw_message = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw_message, dict):
            role = str((raw_message.get("sender") or {}).get("role") or "member").lower()
            if role in ("owner", "admin"):
                return False  # 群主/管理员豁免

        # 图片处理：审核主模型支持识图则直接传图给审核；否则配置了识图模型时转述文字供审核
        image_urls = self._extract_image_urls(event)
        main_chat = gconf.get("llm_chat") or ""
        direct_vision = bool(image_urls) and self.reviewer.supports_vision(main_chat)
        ocr_text = ""
        if not direct_vision and image_urls:
            ocr_chat = gconf.get("llm_chat_ocr") or ""
            if ocr_chat and (gconf.get("keyword_guard_enable") or gconf.get("guard_enable")):
                parts = []
                for url in image_urls:
                    desc = await self.reviewer.describe_image(url, ocr_chat)
                    if desc:
                        parts.append(desc)
                ocr_text = "\n".join(parts).strip()
                if ocr_text:
                    logger.info(f"[MessageGuard] 群 {group_id} 成员 {user_id} 图片转述完成: {ocr_text[:80]}…")

        # 关键词检测：独立开关，本地匹配无成本不节流；命中即处置并结束（含图片转述文字）
        kw_text = f"{text}\n{ocr_text}" if ocr_text else text
        if gconf.get("keyword_guard_enable"):
            keyword = self._match_keyword(kw_text, gconf)
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
                self._mark_handled(key)
                return True

        # LLM 审核：独立开关，与关键词检测互不影响
        if not gconf.get("guard_enable"):
            return False
        if not gconf.get("llm_chat") or not self.reviewer.enabled():
            logger.info(
                "[MessageGuard] 本群未选择 LLM 模型或 AstrBot 上下文不可用，跳过审核"
            )
            return False

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
                return False
            self._last_check[user_id] = now

        verdict = await self.reviewer.judge_message(
            user_id, text,
            prompt=gconf.get("guard_prompt") or "",
            chat_id=gconf.get("llm_chat") or "",
            fallback_chat_id=gconf.get("llm_chat_fallback") or "",
            extra_context=ocr_text,
            image_urls=image_urls if direct_vision else None,
        )
        if verdict is None:
            logger.warning(
                f"[MessageGuard] LLM 审核无结果，保守跳过: 群 {group_id} 用户 {user_id}。"
                f"原因：{self.reviewer.last_error or '未知'}"
            )
            return False
        violated = False
        if verdict.get("source") == "risk_block":
            # 服务端风控拒绝 → 消息被服务端判定为高风险，按配置视为违规处置
            if not gconf.get("guard_risk_as_violation", True):
                logger.info(
                    f"[MessageGuard] 风控拦截但已配置不视为违规，保守跳过: 群 {group_id} 用户 {user_id}"
                )
                return False
            logger.info(
                f"[MessageGuard] 服务端风控拦截，视为违规: 群 {group_id} 用户 {user_id}"
            )
            violated = True
        elif bool(verdict.get("allowed")):
            logger.info(f"[MessageGuard] 群 {group_id} 成员 {user_id} 消息判定合规，不处置")
            self._mark_handled(key)  # 已完整审核过（合规），后台无需重复审核
            return False

        reason = str(verdict.get("reason") or "违规发言")[:100]
        message_id = getattr(event.message_obj, "message_id", None)
        await self._apply_action(
            event, group_id, user_id, message_id, text,
            reason=reason, tracker=self.violation_tracker, source="llm", gconf=gconf,
        )
        self._mark_handled(key)
        return True

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