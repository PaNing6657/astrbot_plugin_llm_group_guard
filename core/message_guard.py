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
from .violation_tracker import (
    KEYWORD_MAJOR_COUNTS_FILE,
    KEYWORD_MINOR_COUNTS_FILE,
    KEYWORD_COUNTS_FILE,
    ViolationLog,
    ViolationTracker,
)


class MessageGuard:
    def __init__(self, config: dict, reviewer: LLMReviewer, data_dir=None, gconf_provider=None):
        self.config = config
        self.reviewer = reviewer
        # 按群取配置的回调（由插件传入 _gconf），缺省回退到顶层 config
        self._gconf_provider = gconf_provider
        self.violation_tracker = ViolationTracker(data_dir, logger) if data_dir else None
        # 关键词违规计数：轻/重两级各自独立累计（另留旧文件兼容读取）
        self.keyword_minor_tracker = (
            ViolationTracker(data_dir, logger, filename=KEYWORD_MINOR_COUNTS_FILE) if data_dir else None
        )
        self.keyword_major_tracker = (
            ViolationTracker(data_dir, logger, filename=KEYWORD_MAJOR_COUNTS_FILE) if data_dir else None
        )
        # 旧版统一关键词计数（升级时轻/重计数为空则并入轻度，避免阶梯清零）
        self.keyword_tracker = (
            ViolationTracker(data_dir, logger, filename=KEYWORD_COUNTS_FILE) if data_dir else None
        )
        # 违规消息日志：记录原文供 WebUI 查看
        self.violation_log = ViolationLog(data_dir, logger) if data_dir else None
        self._last_check: dict[str, float] = {}  # sender_id -> ts
        self._sem = asyncio.Semaphore(2)  # 限制 LLM 审核并发
        # 最近已完整审过的消息键（预审与后台共用，防止同一消息被处理两次）
        self._handled: list[str] = []
        # 旧版统一关键词计数并入轻度（仅当轻/重计数均为空时执行一次）
        self._merge_legacy_keyword_counts()

    def _merge_legacy_keyword_counts(self) -> None:
        """升级兼容：旧 keyword_counts.json 有数据且新轻/重计数为空时，并入轻度计数。"""
        legacy = self.keyword_tracker
        if legacy is None or not legacy.counts:
            return
        minor = self.keyword_minor_tracker
        major = self.keyword_major_tracker
        if (minor and minor.counts) or (major and major.counts):
            return
        if minor is not None:
            minor.counts = legacy.counts
            minor.save()

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

    @staticmethod
    def _to_int_id(value) -> Optional[int]:
        """把消息/群/用户 ID 安全转为 int；形如 '1.1' 的浮点字符串取整，失败返回 None。"""
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None

    def _whitelisted(self, user_id: str, gconf: dict) -> bool:
        return user_id in {str(u).strip() for u in gconf.get("user_whitelist", []) if str(u).strip()}

    def _match_keyword(self, text: str, words) -> Optional[str]:
        """返回消息命中的第一个关键词，未命中返回 None。"""
        for kw in words or []:
            kw = str(kw).strip()
            if kw and kw in text:
                return kw
        return None

    @staticmethod
    def _kw_settings(gconf: dict, level: str) -> dict:
        """取轻/重级关键词的处置与阶梯设置（与 LLM 审核互不影响）。"""
        return {
            "action": (gconf.get(f"keyword_{level}_action") or "ban").lower(),
            "ban_seconds": str(gconf.get(f"keyword_{level}_ban_seconds") or "600"),
            "stair_enable": gconf.get(f"keyword_{level}_stair_enable", True),
            "stair_multiplier": int(gconf.get(f"keyword_{level}_stair_multiplier") or 2),
            "stair_max": int(gconf.get(f"keyword_{level}_stair_max_seconds") or 86400),
            # 纯撤回模式下撤回达到阈值后自动禁言（0=关闭，永远只撤回）
            "recall_ban_threshold": int(gconf.get(f"keyword_{level}_recall_ban_threshold") or 0),
        }

    @staticmethod
    def _extract_image_urls(event: AiocqhttpMessageEvent) -> list:
        """从消息链提取图片 URL 列表（aiocqhttp/OneBot image 段，最多 3 张）。"""
        raw = getattr(event.message_obj, "raw_message", None)
        if not isinstance(raw, dict):
            return []
        segs = raw.get("message")
        if not isinstance(segs, list):
            segs = [segs] if isinstance(segs, dict) else []
        urls = []
        for s in segs:
            if isinstance(s, dict) and s.get("type") == "image":
                u = str((s.get("data") or {}).get("url") or "")
                if u.startswith(("http://", "https://")):
                    urls.append(u)
        return urls[:3]

    async def _handle(self, event: AiocqhttpMessageEvent) -> bool:
        """完整审核一条消息（关键字/LLM+处置），返回 True 表示违规（应拦截回复）。"""
        text = (event.message_str or "").strip()
        image_urls = self._extract_image_urls(event)
        if (not text and not image_urls) or text.startswith("/"):
            return False  # 空消息与指令消息不审核（纯图片消息可审）
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

        # 违规日志用的展示文本：纯图消息记占位，避免空记录
        log_text = text if text else f"[图片消息 x{len(image_urls)}]"

        # 关键词检测：独立于 LLM 审核的完整机制（轻/重两级各自处置与阶梯，命中即结束）
        if gconf.get("keyword_guard_enable"):
            # 重度优先：同一消息同时命中轻/重时按重度处置
            major_kw = self._match_keyword(text, gconf.get("keyword_major_list"))
            minor_kw = None if major_kw else self._match_keyword(text, gconf.get("keyword_minor_list"))
            if major_kw or minor_kw:
                level = "major" if major_kw else "minor"
                kw = major_kw or minor_kw
                logger.info(
                    f"[MessageGuard] 群 {group_id} 成员 {user_id} 命中{'重度' if major_kw else '轻度'}"
                    f"违规词 {kw!r}，按对应处置执行"
                )
                message_id = getattr(event.message_obj, "message_id", None)
                await self._apply_action(
                    event,
                    group_id,
                    user_id,
                    message_id,
                    log_text,
                    reason=f"触发{'重度' if major_kw else '轻度'}违规词：{kw}",
                    tracker=self.keyword_major_tracker if major_kw else self.keyword_minor_tracker,
                    source=f"keyword_{level}",
                    kw_settings=self._kw_settings(gconf, level),
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
            image_urls=image_urls,
            ocr_chat_id=gconf.get("llm_ocr_chat") or "",
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
            event, group_id, user_id, message_id, log_text,
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
        kw_settings: Optional[dict] = None,
    ) -> None:
        gconf = gconf or (self._gconf_provider(group_id) if self._gconf_provider else self.config)
        # 关键词命中走独立处置设置（kw_settings）；LLM 违规走 guard_* 设置
        if kw_settings is not None:
            action = kw_settings["action"]
        else:
            action = (gconf.get("guard_action") or "ban").lower()
        bot = event.bot

        # 阶梯计数：本次违规累计次数（LLM 与轻/重关键词各自独立计数）
        count = 1
        if tracker is not None:
            count = tracker.add(group_id, user_id)
        logger.info(f"[MessageGuard] 群 {group_id} 成员 {user_id} 违规: {reason}")

        # 记录违规消息日志供 WebUI 查看
        if self.violation_log is not None:
            self.violation_log.add(group_id, user_id, text, reason, source)

        # 是否禁言：ban/recall_and_ban 直接禁言；纯撤回模式下按对应阈值达到次数后禁言
        if kw_settings is not None:
            threshold = kw_settings["recall_ban_threshold"]
        else:
            threshold = int(gconf.get("guard_recall_ban_threshold") or 0)
        do_ban = action in ("ban", "recall_and_ban")
        if action == "recall" and threshold > 0 and count >= threshold:
            do_ban = True

        if action in ("recall", "recall_and_ban"):
            mid_int = self._to_int_id(message_id)
            if mid_int is not None:
                try:
                    await bot.api.call_action("delete_msg", message_id=mid_int)
                    logger.info(f"[MessageGuard] 已撤回群 {group_id} 中用户 {user_id} 的违规消息（累计 {count} 次）")
                except Exception as exc:
                    logger.warning(f"[MessageGuard] 撤回失败: {exc}。请确认 Bot 具有管理员权限。")
            else:
                logger.warning(f"[MessageGuard] 消息 ID 非法无法撤回: {message_id!r}")

        duration = 0
        if do_ban:
            if kw_settings is not None:
                duration = self._stair_duration(count, kw_settings)
            else:
                duration = self._stair_duration(count, gconf)
            gid_int = self._to_int_id(group_id)
            uid_int = self._to_int_id(user_id)
            if duration > 0 and gid_int is not None and uid_int is not None:
                try:
                    await bot.api.call_action(
                        "set_group_ban",
                        group_id=gid_int,
                        user_id=uid_int,
                        duration=duration,
                    )
                    logger.info(
                        f"[MessageGuard] 已禁言群 {group_id} 中用户 {user_id}，时长: {duration}秒"
                        f"（第 {count} 次违规）"
                    )
                except Exception as exc:
                    logger.warning(f"[MessageGuard] 禁言失败: {exc}。请确认 Bot 具有管理员权限。")

        notice = str(gconf.get("guard_notice") or "").strip()
        notice_gid = self._to_int_id(group_id)
        if notice and notice_gid is not None:
            try:
                await bot.send_group_msg(
                    group_id=notice_gid,
                    message=notice.replace("{user_id}", user_id)
                    .replace("{duration}", str(duration))
                    .replace("{count}", str(count)),
                )
            except Exception as exc:
                logger.warning(f"[MessageGuard] 违规通知发送失败: {exc}")

    def _stair_duration(self, count: int, settings: dict) -> int:
        """阶梯禁言时长：第 N 次违规 = 基础时长 × 倍数^(N-1)，封顶。

        settings 兼容两种来源：LLM 审核的群配置（guard_* 键）与关键词独立设置（kw_settings）。
        """
        if "action" in settings:  # kw_settings（关键词独立设置）
            base = self._parse_duration(settings["ban_seconds"])
            stair_enable = settings["stair_enable"]
            multiplier = settings["stair_multiplier"]
            cap = settings["stair_max"]
        else:  # 群配置（LLM 审核 guard_* 键）
            base = self._parse_duration(str(settings.get("guard_ban_seconds") or "600"))
            stair_enable = settings.get("guard_stair_enable", True)
            multiplier = int(settings.get("guard_stair_multiplier") or 2)
            cap = int(settings.get("guard_stair_max_seconds") or 86400)
        if base <= 0:
            return 0
        if not stair_enable:
            return base
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