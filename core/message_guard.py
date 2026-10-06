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

from .llm_reviewer import _MAX_IMAGES, LLMReviewer
from .merge_buffer import MergedBatch, MergeBuffer
from .text_utils import build_text_with_at
from .violation_tracker import (
    KEYWORD_MAJOR_COUNTS_FILE,
    KEYWORD_MINOR_COUNTS_FILE,
    KEYWORD_COUNTS_FILE,
    ViolationLog,
    ViolationTracker,
)


def _safe_int(value, default: int) -> int:
    """配置值容错转 int：支持 '1.1' 浮点字符串（取整）、None 回退默认；失败回退默认。"""
    if value is None or value == "":
        return default
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


_RECALL_INTERVAL = 0.3  # 合并批次连续撤回多条消息时的间隔（秒），降低触发平台风控的概率


class MessageGuard:
    def __init__(self, config: dict, reviewer: LLMReviewer, data_dir=None, gconf_provider=None, oid_ban_callback=None):
        self.config = config
        self.reviewer = reviewer
        # 按群取配置的回调（由插件传入 _gconf），缺省回退到顶层 config
        self._gconf_provider = gconf_provider
        # 同 OID 绑定账号禁言回调（由主插件负责校验群成员、管理员和 Bot 身份）
        self._oid_ban_callback = oid_ban_callback
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
        # 消息合并审核缓冲：同一成员连发消息攒成一批，静默满合窗后一次性送审
        self.merge_buffer = MergeBuffer(
            window_getter=self._merge_window_of,
            flush=self._flush_batch,
            logger=logger,
            size_getter=self._merge_max_of,
        )
        # 最近已完整审过的消息键（预审与后台共用，防止同一消息被处理两次）
        self._handled: list[str] = []
        # 最近消息到达时的 LLM 开关状态，预审与后台审核使用同一快照
        self._llm_arrival_state: dict[str, bool] = {}
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
        """后台执行审核，不阻塞消息事件处理。

        启用消息合并审核时不再逐条审核，而是把消息放入合并缓冲：同一成员持续
        发言会不断重置倒计时，直到静默满一个合窗，才把这一批消息一次性交给 LLM
        审核；判定违规时整批（该区间内该成员的全部消息）一并撤回。

        记录消息进入插件时的 LLM 开关状态，避免开关关闭期间到达、但因任务
        调度延迟到开关重新打开后才处理的消息被补审。关键词审核仍按原逻辑执行。
        """
        group_id = event.get_group_id()
        gconf = self._gconf_provider(group_id) if self._gconf_provider else self.config
        llm_enabled_at_arrival = bool(gconf.get("guard_enable"))
        key = self._msg_key(event)
        self._llm_arrival_state[key] = llm_enabled_at_arrival
        if len(self._llm_arrival_state) > 300:
            for stale_key in list(self._llm_arrival_state)[:-300]:
                self._llm_arrival_state.pop(stale_key, None)
        try:
            asyncio.create_task(self._guarded(event, llm_enabled_at_arrival))
        except RuntimeError as exc:
            logger.error(f"[MessageGuard] 无法创建审核任务（不在事件循环中）: {exc}")

    # ------------------------------------------------------------------
    # 消息合并审核：合窗配置
    # ------------------------------------------------------------------
    def _gconf_of(self, group_id) -> dict:
        return self._gconf_provider(group_id) if self._gconf_provider else self.config

    def _merge_window(self, gconf: dict) -> float:
        """本群合窗时长（秒）：未开启合并或配置非法/非正数时为 0（逐条审核）。"""
        if not gconf or not gconf.get("guard_merge_enable"):
            return 0.0
        try:
            window = float(str(gconf.get("guard_merge_window") or 0).strip())
        except (TypeError, ValueError):
            return 0.0
        return window if window > 0 else 0.0

    def _merge_window_of(self, group_id) -> float:
        """合窗时长查询回调（供合并缓冲使用）。"""
        try:
            return self._merge_window(self._gconf_of(group_id))
        except Exception:
            return 0.0

    def _merge_max_of(self, group_id) -> int:
        """单批条数上限查询回调（0=不限制）：达到上限立即送审，避免无上限刷屏拖延审核。"""
        try:
            return max(_safe_int(self._gconf_of(group_id).get("guard_merge_max"), 50), 0)
        except Exception:
            return 50

    def close(self) -> None:
        """停止合并缓冲并丢弃未完成的批次（插件卸载/热重载时调用）。"""
        self.merge_buffer.clear()

    async def pre_review(self, event: AiocqhttpMessageEvent) -> bool:
        """AI 会话回复前的先审：返回 True 表示消息违规（应拦截该次回复）。

        preview 模式：命中关键词只判定、不执行撤回/禁言（后台任务负责处置）。
        预审不做去重跳过：与后台审核的先后顺序不确定，若因去重返回 False，
        stop_event 不会被调用，机器人就会照常回复命中关键词的消息。
        """
        try:
            async with self._sem:
                key = self._msg_key(event)
                return await self._handle(
                    event,
                    preview=True,
                    llm_enabled_at_arrival=self._llm_arrival_state.get(key),
                )
        except Exception as exc:
            logger.error(f"[MessageGuard] 预审异常: {exc}")
            return False

    async def _guarded(self, event: AiocqhttpMessageEvent, llm_enabled_at_arrival=None) -> None:
        group_id = event.get_group_id()
        user_id = str(event.get_sender_id() or "")
        gconf = self._gconf_of(group_id)
        # 合并审核：先放入缓冲（合窗内继续发消息会重置倒计时），到期由 _flush_batch 送审整批
        if self._merge_window(gconf) > 0 and group_id and user_id:
            if self._arrival_exempt(event, user_id, gconf):
                return  # 管理员/白名单/群管理豁免：不占用缓冲与合窗
            buffered = self.merge_buffer.push(
                group_id,
                user_id,
                event,
                self._msg_key(event),
                time.time(),
                llm_enabled=llm_enabled_at_arrival,
            )
            if buffered:
                return
            # 缓冲不可用（不在事件循环/已停止）时退回逐条审核
        try:
            async with self._sem:
                await self._handle(event, llm_enabled_at_arrival=llm_enabled_at_arrival)
        except Exception as exc:
            logger.error(f"[MessageGuard] 审核异常: {exc}")

    def _arrival_exempt(self, event, user_id: str, gconf: dict) -> bool:
        """投递阶段的轻量豁免判断：按消息到达时的身份快照决定是否进入合并缓冲。"""
        return self._batch_exempt(gconf, user_id, event)

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
            "stair_multiplier": _safe_int(gconf.get(f"keyword_{level}_stair_multiplier"), 2),
            "stair_max": _safe_int(gconf.get(f"keyword_{level}_stair_max_seconds"), 86400),
            # 纯撤回模式下撤回达到阈值后自动禁言（0=关闭，永远只撤回）
            "recall_ban_threshold": _safe_int(gconf.get(f"keyword_{level}_recall_ban_threshold"), 0),
        }

    @staticmethod
    def _llm_review_settings(gconf: dict) -> dict:
        """取本次审核使用的规则与模型。

        高召回模式生效时优先使用高召回专用设置（另一个审核规则 + 单独选择的模型），
        未单独填写的项沿用常规审核设置。
        """
        def _pick(hr_key: str, base_key: str) -> str:
            if gconf.get("high_recall_active"):
                value = str(gconf.get(hr_key) or "").strip()
                if value:
                    return value
            return str(gconf.get(base_key) or "").strip()

        return {
            "high_recall": bool(gconf.get("high_recall_active")),
            "prompt": _pick("high_recall_prompt", "guard_prompt"),
            "chat_id": _pick("high_recall_llm_chat", "llm_chat"),
            "fallback_chat_id": _pick("high_recall_llm_chat_fallback", "llm_chat_fallback"),
            "ocr_chat_id": _pick("high_recall_llm_ocr_chat", "llm_ocr_chat"),
            # 开启「按严重程度决定处置」时才需要 D1 额外返回严重度（多问一个问题=多一份输入计费）
            "severity_action": bool(gconf.get("guard_severity_action_enable")),
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

    async def _handle(
        self,
        event: AiocqhttpMessageEvent,
        preview: bool = False,
        llm_enabled_at_arrival=None,
        defer: bool = False,
    ) -> bool:
        """完整审核一条消息（关键字/LLM+处置），返回 True 表示违规（应拦截回复）。

        preview=True 为回复前预审：只检测轻/重违规词，命中即返回 True 供调用方
        stop_event 拦截回复，由后台审核任务负责撤回/禁言与计数；预审期间不去重、
        不标记已处理，避免与后台任务互相抢先导致关键词命中时回复照样发出。
        defer=True 为合并审核的投递阶段：只做关键词检测（命中即按区间整批撤回），
        不再逐条送审——消息随后由合并缓冲攒批，静默满合窗后整批送 LLM 审核。
        """
        text = (event.message_str or "").strip()
        image_urls = self._extract_image_urls(event)
        if (not text and not image_urls) or text.startswith("/"):
            return False  # 空消息与指令消息不审核（纯图片消息可审）
        group_id = event.get_group_id()
        user_id = str(event.get_sender_id())
        if not group_id or not user_id:
            return False
        key = self._msg_key(event)
        if not preview and not defer and self._is_handled(key):
            return False  # 该消息已被预审/后台完整处理过，跳过（去重）
        # 每群独立配置：由插件按群惰性创建并补齐默认值
        gconf = self._gconf_of(group_id)

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
                if preview:
                    # 预审只负责"不回复"：命中即返回 True，处置交给后台审核任务，
                    # 避免与后台任务重复撤回/重复计数
                    logger.info(
                        f"[MessageGuard] 群 {group_id} 成员 {user_id} 预审命中"
                        f"{'重度' if major_kw else '轻度'}违规词 {kw!r}，拦截本次回复"
                    )
                    return True
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

        if defer:
            # 合并审核投递：关键词已在上方检测并处置（命中即整批撤回），
            # 其余消息交由合并缓冲攒批，静默满合窗后整批送 LLM 审核
            logger.debug(
                f"[MessageGuard] 群 {group_id} 成员 {user_id} 消息进入合并缓冲，等待整批审核"
            )
            return False

        if preview:
            return False  # 预审只做关键词拦截，LLM 审核仍由后台任务完整执行

        # LLM 审核：独立开关，与关键词检测互不影响；高召回模式生效时换用另一套规则与模型。
        # 后台任务使用消息到达时的开关状态，避免关闭期间的旧消息在重新开启后被补审；
        # 预审没有到达状态参数，沿用当前开关状态。
        if llm_enabled_at_arrival is None:
            llm_enabled_at_arrival = bool(gconf.get("guard_enable"))
        if not gconf.get("guard_enable") or not llm_enabled_at_arrival:
            return False
        settings = self._llm_review_settings(gconf)
        mode_note = "（高召回模式）" if settings["high_recall"] else ""
        if not settings["chat_id"] or not self.reviewer.enabled():
            logger.info(
                f"[MessageGuard] 本群未选择 LLM 模型或 AstrBot 上下文不可用，跳过审核{mode_note}"
            )
            return False

        if not self._interval_gate(user_id, gconf):
            return False  # 未过该成员的审核间隔，本次跳过

        verdict = await self.reviewer.judge_message(
            user_id, text,
            prompt=settings["prompt"],
            chat_id=settings["chat_id"],
            fallback_chat_id=settings["fallback_chat_id"],
            image_urls=image_urls,
            ocr_chat_id=settings["ocr_chat_id"],
            need_severity=settings["severity_action"],
        )
        if verdict is None:
            logger.warning(
                f"[MessageGuard] LLM 审核无结果，保守跳过{mode_note}: 群 {group_id} 用户 {user_id}。"
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
            logger.info(f"[MessageGuard] 群 {group_id} 成员 {user_id} 消息判定合规{mode_note}，不处置")
            self._mark_handled(key)  # 已完整审核过（合规），后台无需重复审核
            return False

        reason = str(verdict.get("reason") or "违规发言")[:100]
        message_id = getattr(event.message_obj, "message_id", None)
        await self._apply_action(
            event, group_id, user_id, message_id, log_text,
            reason=reason, tracker=self.violation_tracker, source="llm", gconf=gconf,
            severity=verdict.get("severity"),
        )
        self._mark_handled(key)
        return True

    def _interval_gate(self, user_id: str, gconf: dict) -> bool:
        """审核间隔节流：guard_interval<=0 表示关闭，每条消息都审核。

        合并审核生效时该间隔由合窗接管（攒批本身已大幅降低调用频率），不再额外丢弃消息。
        """
        if self._merge_window(gconf) > 0:
            return True
        raw_interval = gconf.get("guard_interval")
        if raw_interval in (None, ""):
            interval = 30
        else:
            try:
                interval = int(raw_interval)
            except (TypeError, ValueError):
                interval = 30
        if interval <= 0:
            return True
        now = time.time()
        if now - self._last_check.get(user_id, 0) < interval:
            return False
        self._last_check[user_id] = now
        return True

    # ------------------------------------------------------------------
    # 合并审核：整批送审 + 整批撤回
    # ------------------------------------------------------------------
    @staticmethod
    def _batch_texts(events: list, image_counts: list) -> list:
        """逐条生成批次的展示文本（纯图消息记占位），顺序与消息到达一致。"""
        texts = []
        for event, images in zip(events, image_counts):
            text = (getattr(event, "message_str", "") or "").strip()
            texts.append(text if text else f"[图片消息 x{len(images)}]")
        return texts

    @staticmethod
    def _collect_image_urls(events: list, extractor) -> list:
        """汇总整批消息随审的图片 URL（去重、按消息到达顺序返回）。

        从**最新**的消息往前取，凑满 _MAX_IMAGES 张即止：合并批次可能攒下十几条消息，
        若从头取会把最新的图挤掉，而最新的图恰恰最需要审核（例如先发文字铺垫、
        最后才发违规图）。返回顺序仍按到达顺序，方便模型对照编号理解上下文。
        """
        picked: list = []  # 由新到旧累积
        for event in reversed(events):
            for url in reversed(list(extractor(event))):
                if url not in picked:
                    picked.append(url)
            if len(picked) >= _MAX_IMAGES:
                break
        return list(reversed(picked))[:_MAX_IMAGES]

    async def _flush_batch(self, batch: MergedBatch) -> None:
        """合并缓冲倒计时结束：把整批消息交给 LLM 审核，违规则整批撤回/禁言。

        批次内任何一条判为违规，该区间内该成员的全部消息一并处置（关键词命中同理）。
        """
        events = list(batch.events)
        if not events:
            return
        try:
            async with self._sem:
                await self._handle_batch(batch, events)
        except Exception as exc:
            logger.error(f"[MessageGuard] 合并审核异常: {exc}")

    async def _handle_batch(self, batch: MergedBatch, events: list) -> None:
        group_id = events[0].get_group_id() or batch.group_id
        user_id = str(events[0].get_sender_id() or batch.user_id)
        gconf = self._gconf_of(group_id)
        per_message_images = [
            self._extract_image_urls(event) for event in events
        ]  # 每条消息各自的图片（供纯图占位文本用）
        texts = self._batch_texts(events, per_message_images)
        merged_text = "\n".join(texts)
        message_ids = [self._message_id_of(event) for event in events]
        size = len(events)
        # 本批已整体审过：标记去重键，避免预审/迟到任务对同一条消息重复处置
        self._mark_batch_handled(batch, events)

        # 投递阶段已按同一套规则筛过消息，这里用批次首条事件做一次豁免复核（防止配置中途变化）
        if self._batch_exempt(gconf, user_id, events[0]):
            logger.info(
                f"[MessageGuard] 合并批次（{size} 条）命中豁免（白名单/管理员），跳过审核: "
                f"群 {group_id} 用户 {user_id}"
            )
            return

        # 关键词检测：与 LLM 审核独立，命中即整批处置（先判重度再判轻度）
        if gconf.get("keyword_guard_enable"):
            major_kw = self._match_keyword(merged_text, gconf.get("keyword_major_list"))
            minor_kw = None if major_kw else self._match_keyword(
                merged_text, gconf.get("keyword_minor_list")
            )
            if major_kw or minor_kw:
                level = "major" if major_kw else "minor"
                kw = major_kw or minor_kw
                logger.info(
                    f"[MessageGuard] 合并批次（{size} 条）命中"
                    f"{'重度' if major_kw else '轻度'}违规词 {kw!r}，整批处置"
                )
                await self._apply_action(
                    events[0],
                    group_id,
                    user_id,
                    message_ids,
                    merged_text,
                    reason=f"触发{'重度' if major_kw else '轻度'}违规词：{kw}（{size} 条合并审核）",
                    tracker=self.keyword_major_tracker if major_kw else self.keyword_minor_tracker,
                    source=f"keyword_{level}",
                    kw_settings=self._kw_settings(gconf, level),
                    batch_size=size,
                )
                return

        # LLM 审核：整批消息一起送审（合并后语义更完整），违规则整批撤回
        llm_enabled = batch.llm_enabled
        if llm_enabled is None:
            llm_enabled = bool(gconf.get("guard_enable"))
        if not gconf.get("guard_enable") or not llm_enabled:
            return
        settings = self._llm_review_settings(gconf)
        mode_note = "（高召回模式）" if settings["high_recall"] else ""
        if not settings["chat_id"] or not self.reviewer.enabled():
            logger.info(
                f"[MessageGuard] 本群未选择 LLM 模型或 AstrBot 上下文不可用，跳过合并审核{mode_note}"
            )
            return

        verdict = await self.reviewer.judge_messages(
            user_id,
            texts,
            prompt=settings["prompt"],
            chat_id=settings["chat_id"],
            fallback_chat_id=settings["fallback_chat_id"],
            image_urls=self._collect_image_urls(events, self._extract_image_urls),
            ocr_chat_id=settings["ocr_chat_id"],
            need_severity=settings["severity_action"],
        )
        if verdict is None:
            logger.warning(
                f"[MessageGuard] 合并审核无结果，保守跳过{mode_note}: 群 {group_id} 用户 {user_id}"
                f"（{size} 条）。原因：{self.reviewer.last_error or '未知'}"
            )
            return
        if verdict.get("source") == "risk_block":
            if not gconf.get("guard_risk_as_violation", True):
                logger.info(
                    f"[MessageGuard] 合并批次风控拦截但已配置不视为违规，保守跳过: "
                    f"群 {group_id} 用户 {user_id}（{size} 条）"
                )
                return
            logger.info(
                f"[MessageGuard] 合并批次服务端风控拦截，视为违规: 群 {group_id} 用户 {user_id}"
                f"（{size} 条）"
            )
        elif bool(verdict.get("allowed")):
            logger.info(
                f"[MessageGuard] 群 {group_id} 成员 {user_id} 合并消息（{size} 条）判定合规"
                f"{mode_note}，不处置"
            )
            return

        reason = str(verdict.get("reason") or "违规发言")[:100]
        if size > 1:
            reason = f"{reason}（{size} 条消息合并审核）"[:100]
        await self._apply_action(
            events[0],
            group_id,
            user_id,
            message_ids,
            merged_text,
            reason=reason,
            tracker=self.violation_tracker,
            source="llm",
            gconf=gconf,
            batch_size=size,
            severity=verdict.get("severity"),
        )

    def _batch_exempt(self, gconf: dict, user_id: str, event) -> bool:
        """批次豁免复核：AstrBot 管理员/白名单/群主/群管理员不审核。"""
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        if self._whitelisted(user_id, gconf):
            return True
        raw_message = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if isinstance(raw_message, dict):
            role = str((raw_message.get("sender") or {}).get("role") or "member").lower()
            if role in ("owner", "admin"):
                return True
        return False

    @staticmethod
    def _message_id_of(event):
        """取消息 ID（撤回用）。"""
        return getattr(getattr(event, "message_obj", None), "message_id", None)

    def _mark_batch_handled(self, batch: MergedBatch, events: list) -> None:
        """标记整批消息已完整审核，避免预审/迟到任务重复处置。"""
        for key in batch.keys:
            self._mark_handled(key)
        for event in events:
            self._mark_handled(self._msg_key(event))

    @staticmethod
    def _severity_action(gconf: dict, severity: Optional[float]) -> Optional[str]:
        """按严重程度决定处置：≥ 阈值→recall_and_ban，否则→recall；未启用/无严重度返回 None。"""
        if severity is None or not gconf.get("guard_severity_action_enable"):
            return None
        try:
            threshold = float(gconf.get("guard_severity_ban_score", 2.5))
            value = float(severity)
        except (TypeError, ValueError):
            return None
        return "recall_and_ban" if value >= threshold else "recall"

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
        batch_size: int = 1,
        severity: Optional[float] = None,
    ) -> None:
        """执行处置。message_id 支持单条，也支持列表（合并审核时整批撤回）。

        severity 为 LLM（D1 决策模型）判定附带的严重度分数；开启「按严重程度决定处置」时，
        达到阈值走「撤回并禁言」，未达到走「仅撤回」（关键词处置不受影响）。
        """
        gconf = gconf or self._gconf_of(group_id)
        # 关键词命中走独立处置设置（kw_settings）；LLM 违规走 guard_* 设置
        if kw_settings is not None:
            action = kw_settings["action"]
        else:
            action = (gconf.get("guard_action") or "ban").lower()
            severity_action = self._severity_action(gconf, severity)
            if severity_action:
                action = severity_action
        bot = event.bot

        # 阶梯计数：本次违规累计次数（LLM 与轻/重关键词各自独立计数；合并批次按 1 次计）
        count = 1
        if tracker is not None:
            count = tracker.add(group_id, user_id)
        logger.info(f"[MessageGuard] 群 {group_id} 成员 {user_id} 违规: {reason}")

        # 记录违规消息日志供 WebUI 查看（合并批次记录该区间内的全部消息原文）
        if self.violation_log is not None:
            self.violation_log.add(
                group_id, user_id, text, reason, source, nickname=self._sender_nickname(event)
            )

        # 是否禁言：ban/recall_and_ban 直接禁言；纯撤回模式下按对应阈值达到次数后禁言
        if kw_settings is not None:
            threshold = kw_settings["recall_ban_threshold"]
        else:
            threshold = _safe_int(gconf.get("guard_recall_ban_threshold"), 0)
        do_ban = action in ("ban", "recall_and_ban")
        if action == "recall" and threshold > 0 and count >= threshold:
            do_ban = True

        if action in ("recall", "recall_and_ban"):
            await self._recall_messages(bot, group_id, user_id, message_id, count)

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
                else:
                    if self._oid_ban_callback is not None:
                        try:
                            self_id = str(event.get_self_id())
                        except Exception:
                            self_id = ""
                        try:
                            synced = await self._oid_ban_callback(
                                bot, group_id, user_id, duration, self_id=self_id
                            )
                            if synced:
                                logger.info(
                                    f"[MessageGuard] 群 {group_id} 中用户 {user_id} 同 OID 账号"
                                    f"已同步禁言 {synced} 个，时长: {duration}秒"
                                )
                        except Exception as exc:
                            logger.warning(f"[MessageGuard] 同 OID 账号禁言同步失败: {exc}")

        notice = str(gconf.get("guard_notice") or "").strip()
        notice_gid = self._to_int_id(group_id)
        if notice and notice_gid is not None:
            try:
                await bot.send_group_msg(
                    group_id=notice_gid,
                    message=build_text_with_at(
                        notice,
                        {
                            "{user_id}": user_id,
                            "{nickname}": self._sender_nickname(event) or user_id,
                            "{duration}": str(duration),
                            "{count}": str(count),
                            "{messages}": str(batch_size),
                        },
                        user_id,
                    ),
                )
            except Exception as exc:
                logger.warning(f"[MessageGuard] 违规通知发送失败: {exc}")

    async def _recall_messages(self, bot, group_id, user_id: str, message_id, count: int) -> None:
        """撤回违规消息：支持单条或合并批次的多条（逐条撤回，轻微间隔降低风控概率）。"""
        raw_ids = message_id if isinstance(message_id, (list, tuple, set)) else [message_id]
        mids = []
        for mid in raw_ids:
            mid_int = self._to_int_id(mid)
            if mid_int is None:
                logger.warning(f"[MessageGuard] 消息 ID 非法无法撤回: {mid!r}")
                continue
            if mid_int not in mids:
                mids.append(mid_int)
        if not mids:
            return
        if len(mids) > 1:
            logger.info(
                f"[MessageGuard] 合并审核违规：准备撤回群 {group_id} 中用户 {user_id} 的 {len(mids)} 条消息"
            )
        for index, mid_int in enumerate(mids):
            if index and _RECALL_INTERVAL > 0:
                await asyncio.sleep(_RECALL_INTERVAL)  # 连发撤回之间留出间隔
            try:
                await bot.api.call_action("delete_msg", message_id=mid_int)
                logger.info(
                    f"[MessageGuard] 已撤回群 {group_id} 中用户 {user_id} 的违规消息"
                    f"（{index + 1}/{len(mids)}，累计 {count} 次）"
                )
            except Exception as exc:
                logger.warning(f"[MessageGuard] 撤回失败: {exc}。请确认 Bot 具有管理员权限。")

    @staticmethod
    def _sender_nickname(event: AiocqhttpMessageEvent) -> str:
        """取违规发言者的昵称：优先 QQ 昵称，其次群名片；均取不到返回空串。"""
        raw_message = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw_message, dict):
            sender = raw_message.get("sender")
            if isinstance(sender, dict):
                for key in ("nickname", "card"):
                    name = str(sender.get(key) or "").strip()
                    if name:
                        return name
        try:
            return str(event.get_sender_name() or "").strip()
        except Exception:
            return ""

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
            multiplier = _safe_int(settings.get("guard_stair_multiplier"), 2)
            cap = _safe_int(settings.get("guard_stair_max_seconds"), 86400)
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