import asyncio
import re
import time
from typing import Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, json_response, request
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.star.star_tools import StarTools

from .core.event_utils import unwrap_event
from .core.permission_utils import check_group_and_permission
from .core.whole_ban_scheduler import WholeBanScheduler, parse_schedule_times, weekly_window
from .core.llm_reviewer import LLMReviewer
from .core.message_guard import MessageGuard

PLUGIN_NAME = "astrbot_plugin_llm_group_guard"
CHECK_INTERVAL = 20  # 定时禁言调度循环检查间隔（秒）

_WEEKDAY_NAMES = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}
_WEEKDAY_CN = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}


def _parse_weekday_spec(spec: str):
    """解析周几规格：返回 set[int]（1-7）、"all"（每天）或 None（无法解析）。"""
    s = spec.strip()
    if s in ("每天", "每日", "daily", "每日重复"):
        return "all"
    if s == "周末":
        return {6, 7}
    if s == "工作日":
        return {1, 2, 3, 4, 5}

    def _day(ch: str):
        if ch in _WEEKDAY_NAMES:
            return _WEEKDAY_NAMES[ch]
        return int(ch) if ch.isdigit() and 1 <= int(ch) <= 7 else None

    m = re.fullmatch(r"(?:周|星期|礼拜)([一二三四五六日天1-7])", s)
    if m:
        day = _day(m.group(1))
        return {day} if day else None
    m = re.fullmatch(
        r"(?:周|星期|礼拜)([一二三四五六日天1-7])(?:到|-|~|至)(?:周|星期|礼拜)?([一二三四五六日天1-7])", s
    )
    if m:
        d1, d2 = _day(m.group(1)), _day(m.group(2))
        if d1 and d2:
            if d1 <= d2:
                return set(range(d1, d2 + 1))
            return set(range(d1, 8)) | set(range(1, d2 + 1))  # 跨周如 周五-周一
    return None


def _to_weekly_rule(start_ts: float, end_ts: float) -> dict:
    """把绝对时间窗口转换为 weekly 规则（start_min + duration_min）。"""
    from datetime import datetime

    day_start = datetime.fromtimestamp(start_ts).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()
    return {
        "start_min": int((start_ts - day_start) // 60),
        "duration_min": int((end_ts - start_ts) // 60),
    }


@register("astrbot_plugin_llm_group_guard", "SatenShiroya", "全体禁言与LLM违规审核", "v1.0.0")
class LLMGroupGuardPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.data_dir = StarTools.get_data_dir()
        self.config = config

        # 全体禁言开启/解除时发送的自定义消息，留空则不发送
        self.whole_ban_enable_msg = str(config.get("whole_ban_enable_msg", "") or "")
        self.whole_ban_disable_msg = str(config.get("whole_ban_disable_msg", "") or "")

        # 定时全体禁言调度器（任务持久化，重启恢复）
        self.scheduler = WholeBanScheduler(self.data_dir, logger)
        # LLM 审核器与消息守卫
        self.reviewer = LLMReviewer(config)
        self.guard = MessageGuard(config, self.reviewer, data_dir=str(self.data_dir))
        # 群内最近一次缓存的 bot 客户端，供定时任务在无事件上下文时使用
        self._group_runtime: dict[str, dict] = {}
        # 审批通过者的 OID 缓存 {gid: {uid: oid}}，供成员进群事件发送欢迎时使用
        self._join_oid: dict[str, dict[str, str]] = {}
        # 平台级 bot 客户端兜底：插件重启后即使群里无新消息也能执行定时任务
        self._platform_bot = None
        self._scheduler_task: Optional[asyncio.Task] = None
        self._register_web_apis()

    # ------------------------------------------------------------------
    # WebUI 后端 API
    # ------------------------------------------------------------------
    def _register_web_apis(self):
        base = f"/{PLUGIN_NAME}"
        self.context.register_web_api(f"{base}/config", self.web_get_config, ["GET"], "读取插件配置")
        self.context.register_web_api(f"{base}/config/save", self.web_save_config, ["POST"], "保存插件配置")
        self.context.register_web_api(f"{base}/violations", self.web_violations, ["GET"], "违规记录列表")
        self.context.register_web_api(f"{base}/violations/reset", self.web_violations_reset, ["POST"], "清零违规记录")
        self.context.register_web_api(f"{base}/schedules", self.web_schedules, ["GET"], "定时禁言任务列表")
        self.context.register_web_api(f"{base}/schedules/set", self.web_schedule_set, ["POST"], "设置某群定时禁言")
        self.context.register_web_api(f"{base}/schedules/delete", self.web_schedule_delete, ["POST"], "删除某群定时禁言")

    async def web_get_config(self):
        return json_response(dict(self.config))

    async def web_save_config(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")
        try:
            self.config.update(payload)
            self.config.save_config()
        except Exception as e:
            logger.error(f"WebUI 保存配置失败: {e}")
            return error_response(f"保存失败：{e}")
        self.whole_ban_enable_msg = str(self.config.get("whole_ban_enable_msg", "") or "")
        self.whole_ban_disable_msg = str(self.config.get("whole_ban_disable_msg", "") or "")
        return json_response({"saved": True})

    async def web_violations(self):
        # 返回 LLM/关键词计数与违规消息日志
        tracker = getattr(self.guard, "violation_tracker", None)
        kw_tracker = getattr(self.guard, "keyword_tracker", None)
        vlog = getattr(self.guard, "violation_log", None)
        return json_response({
            "llm": tracker.counts if tracker else {},
            "keyword": kw_tracker.counts if kw_tracker else {},
            "log": vlog.entries if vlog else [],
        })

    async def web_violations_reset(self):
        payload = await request.json(default={})
        tracker = getattr(self.guard, "violation_tracker", None)
        kw_tracker = getattr(self.guard, "keyword_tracker", None)
        vlog = getattr(self.guard, "violation_log", None)
        if tracker is None and kw_tracker is None:
            return error_response("违规计数模块未初始化")
        gid = str(payload.get("group_id") or "").strip()
        uid = str(payload.get("user_id") or "").strip()
        # type 指定计数来源：llm/keyword，缺省两者都处理
        vtype = str(payload.get("type") or "").strip()
        trackers = {"llm": [tracker], "keyword": [kw_tracker]}
        targets = trackers.get(vtype) if vtype in trackers else [tracker, kw_tracker]
        for t in targets:
            if t is None:
                continue
            if gid and uid:
                t.reset(gid, uid)
            elif gid:
                t.counts.pop(gid, None)
                t.save()
            else:
                t.counts.clear()
                t.save()
        # 日志随计数同步清理，保持页面数据一致
        if vlog is not None:
            if gid and uid:
                vlog.clear(gid, uid)
            else:
                vlog.clear()
        return json_response({
            "reset": True,
            "llm": tracker.counts if tracker else {},
            "keyword": kw_tracker.counts if kw_tracker else {},
            "log": vlog.entries if vlog else [],
        })

    async def web_schedules(self):
        data = {}
        # 每群可能有多任务：返回任务列表
        for gid, tasks in self.scheduler.all().items():
            data[gid] = [{k: v for k, v in t.items() if k != "bot"} for t in tasks]
        return json_response(data)

    async def web_schedule_set(self):
        payload = await request.json(default={})
        gid = str(payload.get("group_id") or "").strip()
        if not gid:
            return error_response("缺少 group_id")
        mode = str(payload.get("mode") or "once")
        start_str = str(payload.get("start_time") or "").strip()
        end_str = str(payload.get("end_time") or "").strip()
        bot = getattr(self, "_platform_bot", None)
        try:
            if mode == "weekly":
                weekday_set = _parse_weekday_spec(str(payload.get("weekdays") or "").strip())
                if weekday_set is None or weekday_set == "all":
                    return error_response("weekly 模式需要 weekdays（如 周一-周五）")
                start_ts, end_ts = parse_schedule_times(start_str, end_str)
                rule = _to_weekly_rule(start_ts, end_ts)
                # 合并进已有每周任务，不覆盖其他类型任务
                old_weekly = next(
                    (t for t in self.scheduler.get(gid) if t.get("mode") == "weekly"), None
                )
                if old_weekly and old_weekly.get("started"):
                    await self._apply_scheduled(gid, old_weekly, enable=False)
                rules = dict(old_weekly.get("rules") or {}) if old_weekly else {}
                for d in weekday_set:
                    rules[str(d)] = rule
                self.scheduler.set_weekly(gid, rules, bot=bot)
                return json_response({"saved": True})
            recurring = mode == "daily"
            if recurring and start_str.lower() == "now":
                return error_response("每日任务需要 HH:MM 开始时间")
            start_ts, end_ts = parse_schedule_times(start_str, end_str)
            # 追加新任务：同类型未触发自动去重，不影响已有规划
            self.scheduler.set(gid, start_ts, end_ts, bot=bot, recurring=recurring)
            return json_response({"saved": True})
        except ValueError as e:
            return error_response(f"时间格式错误：{e}")
        except Exception as e:
            logger.error(f"WebUI 设置定时禁言失败: {e}")
            return error_response(f"设置失败：{e}")

    async def web_schedule_delete(self):
        payload = await request.json(default={})
        gid = str(payload.get("group_id") or "").strip()
        if not gid:
            return error_response("缺少 group_id")
        # 指定 task_id 只删除对应任务，缺省删除该群全部
        task_id = str(payload.get("task_id") or "").strip() or None
        old = self.scheduler.remove(gid, task_id)
        if not old:
            return json_response({"deleted": False})
        if old.get("started"):
            await self._apply_scheduled(gid, old, enable=False)
        return json_response({"deleted": True})

    # 权限开关每次实时读取配置，避免修改配置后需要重启插件才生效
    def _permission_verification(self) -> bool:
        return bool(self.config.get("Permission_verification", True))

    def _allow_groupadmin_use(self) -> bool:
        return bool(self.config.get("allow_groupadmin_use", False))

    def _init_platform_bot(self):
        """从平台管理器获取 aiocqhttp 客户端，作为定时任务执行的全群兜底。"""
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
                AiocqhttpAdapter,
            )
            for inst in self.context.platform_manager.get_insts():
                if isinstance(inst, AiocqhttpAdapter):
                    client = inst.get_client()
                    if client is not None:
                        self._platform_bot = client
                        logger.info("[Guard] 已获取 aiocqhttp 平台客户端，定时任务可独立执行")
                        return
        except Exception as e:
            logger.warning(f"[Guard] 初始化平台客户端失败: {e}")

    async def initialize(self):
        """启动定时禁言后台调度循环。"""
        self._init_platform_bot()
        self._scheduler_task = asyncio.get_running_loop().create_task(self._schedule_loop())

    async def terminate(self):
        await self.reviewer.close()
        if self._scheduler_task:
            self._scheduler_task.cancel()

    # ------------------------------------------------------------------
    # 核心：执行全体禁言开关，并发送配置的通知消息
    # ------------------------------------------------------------------
    async def _change_whole_ban(
        self,
        group_id,
        enable: bool,
        bot,
        start_ts: Optional[float] = None,
        end_ts: Optional[float] = None,
    ) -> tuple[bool, str]:
        action = "开启" if enable else "解除"
        try:
            await bot.api.call_action(
                "set_group_whole_ban",
                group_id=int(group_id),
                enable=enable,
            )
        except Exception as e:
            logger.error(f"群 {group_id} {action}全体禁言失败: {e}")
            return False, f"操作失败：无法{action}全体禁言，可能原因是权限不足或API错误"

        template = self.whole_ban_enable_msg if enable else self.whole_ban_disable_msg
        if template:
            try:
                text = (
                    template.replace(
                        "{start_time}", time.strftime("%H:%M", time.localtime(start_ts)) if start_ts else ""
                    ).replace(
                        "{end_time}", time.strftime("%H:%M", time.localtime(end_ts)) if end_ts else ""
                    )
                )
                await bot.send_group_msg(group_id=int(group_id), message=text)
            except Exception as e:
                logger.warning(f"群 {group_id} 发送全体禁言通知消息失败: {e}")
        return True, f"已{action}全体禁言"

    async def _apply_scheduled(self, group_id, sched: dict, enable: bool) -> tuple[bool, str]:
        """按调度任务执行一次开关，bot 连接信息从任务/群缓存/平台兜底依次获取。"""
        runtime = self._group_runtime.get(str(group_id)) or {}
        bot = (
            sched.get("bot")
            or runtime.get("bot")
            or getattr(self, "_platform_bot", None)
        )
        if not bot:
            return False, "缺少机器人连接信息，等待下一次调度重试"
        return await self._change_whole_ban(
            group_id=group_id,
            enable=enable,
            bot=bot,
            start_ts=sched.get("start_ts"),
            end_ts=sched.get("end_ts"),
        )

    async def _schedule_loop(self):
        """后台调度循环：到点开启、到时解除，失败自动重试。"""
        while True:
            try:
                await self._check_schedules()
            except Exception as e:
                logger.error(f"定时全体禁言调度循环异常: {e}")
            await asyncio.sleep(CHECK_INTERVAL)

    async def _check_schedules(self):
        now = time.time()
        for gid in list(self.scheduler.all().keys()):
            # 每群可能共存多个任务，逐个独立调度
            for sched in list(self.scheduler.get(gid)):
                if not sched:
                    continue
                try:
                    if sched.get("mode") == "weekly":
                        await self._check_weekly(gid, sched, now)
                        continue
                    recurring = sched.get("mode") == "daily"
                    if not sched["started"] and now >= sched["start_ts"]:
                        ok, msg = await self._apply_scheduled(gid, sched, enable=True)
                        if ok:
                            sched["started"] = True
                            self.scheduler.save()
                            logger.info(f"群 {gid} 定时全体禁言已开启: {msg}")
                        else:
                            logger.warning(f"群 {gid} 定时开启全体禁言失败，稍后重试: {msg}")
                    elif sched["started"] and now >= sched["end_ts"]:
                        ok, msg = await self._apply_scheduled(gid, sched, enable=False)
                        if ok:
                            if recurring:
                                self.scheduler.advance(sched, now)  # 每日任务推进到下一天
                                logger.info(f"群 {gid} 定时全体禁言已解除，下一周期 {time.strftime('%m-%d %H:%M', time.localtime(sched['start_ts']))} 开启")
                            else:
                                self.scheduler.remove(gid, sched.get("id"))
                                logger.info(f"群 {gid} 定时全体禁言已解除: {msg}")
                        else:
                            logger.warning(f"群 {gid} 定时解除全体禁言失败，稍后重试: {msg}")
                except Exception as e:
                    logger.error(f"群 {gid} 定时全体禁言执行异常: {e}")

    async def _check_weekly(self, gid, sched: dict, now: float) -> None:
        """每周多窗口任务：按当天是否有规则决定开启/解除。"""
        rules = sched.get("rules") or {}
        if not rules:
            self.scheduler.remove(gid, sched.get("id"))
            return
        window = weekly_window(rules, now)
        if sched["started"]:
            # 已开启：到达当前窗口结束时间则解除（不依赖今天是否有规则）
            if now >= sched.get("current_end_ts", 0):
                ok, msg = await self._apply_scheduled(gid, sched, enable=False)
                if ok:
                    sched["started"] = False
                    sched["current_end_ts"] = 0
                    self.scheduler.save()
                    logger.info(f"群 {gid} 定时全体禁言已解除: {msg}")
                else:
                    logger.warning(f"群 {gid} 定时解除全体禁言失败，稍后重试: {msg}")
            return
        # 未开启：今天有规则且当前在窗口内则补开启
        if window is None:
            return
        start_ts, end_ts = window
        if start_ts <= now < end_ts:
            ok, msg = await self._apply_scheduled(gid, sched, enable=True)
            if ok:
                sched["started"] = True
                sched["current_end_ts"] = end_ts
                self.scheduler.save()
                logger.info(f"群 {gid} 定时全体禁言已开启: {msg}")
            else:
                logger.warning(f"群 {gid} 定时开启全体禁言失败，稍后重试: {msg}")

    # ------------------------------------------------------------------
    # 自动审批入群：LLM 检测申请信息是否含昵称与 OID，通过则同意并自动改名片
    # ------------------------------------------------------------------
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_group_increase(self, event: AstrMessageEvent):
        """成员进群事件：审批通过者发审批欢迎词并改名片；其他进群发默认欢迎词、不改名片。"""
        try:
            raw = getattr(event.message_obj, "raw_message", None)
            if not isinstance(raw, dict):
                return
            # 只处理成员进群通知（notice/group_increase）
            if raw.get("post_type") != "notice" or raw.get("notice_type") != "group_increase":
                return
            group_id = str(raw.get("group_id") or "")
            user_id = str(raw.get("user_id") or "")
            if not group_id or not user_id or user_id == str(raw.get("operator_id") or ""):
                return  # 无群/无用户，或为机器人自身进群时跳过

            # 审批通过者在 _approve_join 时记录了 OID 缓存；据此区分审批/非审批
            cache_oid = (self._join_oid.get(group_id) or {}).get(user_id)
            approved = bool(cache_oid)
            oid = ""
            if approved:
                oid = str(cache_oid)
                self._join_oid[group_id].pop(user_id, None)  # 欢迎已用，清理缓存
                template = str(self.config.get("join_welcome_msg") or "").strip()
            else:
                template = str(self.config.get("join_welcome_default") or "").strip()
            if not template:
                return  # 对应欢迎词未配置则不发送

            nickname = user_id
            try:
                info = await event.bot.get_group_member_info(
                    group_id=int(group_id), user_id=int(user_id)
                )
                if isinstance(info, dict):
                    nickname = str(info.get("nickname") or user_id).strip()
            except Exception as e:
                logger.debug(f"[Guard] 进群查询成员信息失败: {e}")

            await event.bot.send_group_msg(
                group_id=int(group_id),
                message=self._build_text_with_at(
                    template,
                    {"{nickname}": nickname, "{oid}": oid, "{user_id}": user_id},
                    user_id,
                ),
            )
            logger.info(f"[Guard] 群 {group_id} 成员 {user_id} 进群，已发送{'审批' if approved else '普通'}欢迎")
        except Exception as e:
            logger.error(f"[Guard] 进群欢迎处理异常: {e}")

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_group_add_request(self, event: AstrMessageEvent):
        """入群申请事件：检测昵称+OID，齐全同意并改名片，缺失拒绝。"""
        try:
            raw = getattr(event.message_obj, "raw_message", None)
            if not isinstance(raw, dict):
                return
            # 只处理加群申请事件（request/group/add）
            if (
                raw.get("post_type") != "request"
                or raw.get("request_type") != "group"
                or raw.get("sub_type") != "add"
            ):
                return
            if not self.config.get("join_verify_enable"):
                return  # 开关关闭则不自动审批

            group_id = str(raw.get("group_id") or "")
            user_id = str(raw.get("user_id") or "")
            flag = str(raw.get("flag") or "")
            comment = str(raw.get("comment") or "").strip()
            if not group_id or not user_id or not flag:
                return

            # 套用群白名单：填写了白名单则只审批名单内的群
            group_whitelist = [
                str(g).strip()
                for g in (self.config.get("group_whitelist") or [])
                if str(g).strip()
            ]
            if group_whitelist and group_id not in group_whitelist:
                return

            logger.info(f"[Guard] 收到入群申请: 群 {group_id} 用户 {user_id} 申请信息={comment!r}")
            verdict = await self.reviewer.judge_join_request(comment)
            if verdict is None:
                # LLM 不可用/失败：不自动审批，留给管理员手动处理
                logger.warning(
                    f"[Guard] 入群审核 LLM 无结果，跳过自动审批: 群 {group_id} 用户 {user_id}。"
                    f"原因：{self.reviewer.last_error or '未知'}"
                )
                return

            has_nickname = bool(verdict.get("has_nickname"))
            has_oid = bool(verdict.get("has_oid"))
            oid = str(verdict.get("oid") or "").strip()
            # OID 必须是纯数字；缺失或非数字均视为无效
            oid_valid = has_oid and oid.isdigit() and len(oid) >= 4
            if has_nickname and oid_valid:
                await self._approve_join(event, group_id, user_id, flag, oid)
            else:
                await self._reject_join(event, group_id, user_id, flag, has_nickname, oid_valid)
        except Exception as e:
            logger.error(f"[Guard] 入群申请自动审批异常: {e}")

    async def _approve_join(self, event, group_id, user_id, flag, oid: str) -> None:
        """同意入群，并异步延迟执行自动改名片（QQ昵称_OID）。"""
        try:
            await event.bot.api.call_action(
                "set_group_add_request", flag=flag, sub_type="add", approve=True, reason="已填写昵称与OID(UID)，审核通过"
            )
            logger.info(f"[Guard] 群 {group_id} 已同意用户 {user_id} 入群（OID={oid}）")
        except Exception as e:
            logger.error(f"[Guard] 同意入群失败: 群 {group_id} 用户 {user_id}: {e}")
            return
        # 记录 OID 供进群欢迎使用（进群事件里取不到则回退到名片提取）
        self._join_oid.setdefault(str(group_id), {})[str(user_id)] = oid
        # 用户进群后自动改名片为 QQ昵称_OID（对方需实际入群，延迟重试）
        asyncio.create_task(self._set_card_after_join(event.bot, group_id, user_id, oid))

    async def _set_card_after_join(self, bot, group_id, user_id, oid: str) -> None:
        """入群后：改名片为『QQ昵称_OID』、按配置发送改名提示（欢迎由进群事件统一发送）。"""
        card = None
        nickname = None
        for _ in range(6):
            await asyncio.sleep(8)  # 等待对方真正进群
            try:
                info = await bot.get_group_member_info(group_id=int(group_id), user_id=int(user_id))
                if isinstance(info, dict) and info.get("user_id"):
                    nickname = str(info.get("nickname") or "").strip()
                    if nickname:
                        card = f"{nickname}_{oid}"[:64]  # 限制长度防超长
                        break
            except Exception as e:
                logger.debug(f"[Guard] 等待入群获取昵称失败: {e}")
        if not card:
            logger.warning(f"[Guard] 群 {group_id} 用户 {user_id} 入群后未取到昵称，跳过改名片")
            return

        # 改名片
        try:
            await bot.api.call_action(
                "set_group_card", group_id=int(group_id), user_id=int(user_id), card=card
            )
            logger.info(f"[Guard] 群 {group_id} 已将用户 {user_id} 名片改为 {card}")
        except Exception as e:
            logger.warning(f"[Guard] 修改名片失败: 群 {group_id} 用户 {user_id}: {e}")
            return

        # 改名后的提示（开关 + 自定义文案，支持 {at_user}）
        if self.config.get("join_card_notify"):
            tip = str(self.config.get("join_card_notify_msg") or "").strip()
            if tip:
                try:
                    await bot.send_group_msg(
                        group_id=int(group_id),
                        message=self._build_text_with_at(
                            tip,
                            {
                                "{new_card}": card,
                                "{nickname}": nickname,
                                "{oid}": oid,
                                "{user_id}": user_id,
                            },
                            user_id,
                        ),
                    )
                except Exception as e:
                    logger.warning(f"[Guard] 改名提示发送失败: 群 {group_id} 用户 {user_id}: {e}")

    @staticmethod
    def _build_text_with_at(template: str, vars_map: dict, user_id: str) -> str:
        """把模板编译为文本：{at_user} 替换成 CQ 码 @ 该用户（可多次出现）。"""
        text = str(template)
        for k, v in vars_map.items():
            text = text.replace(k, str(v))
        return text.replace("{at_user}", f"[CQ:at,qq={user_id}]")

    async def _reject_join(self, event, group_id, user_id, flag, has_nickname, oid_valid) -> None:
        """拒绝入群，理由控制在 15 字以内。"""
        missing = []
        if not has_nickname:
            missing.append("昵称")
        if not oid_valid:
            missing.append("OID")
        reason = f"缺少{'与'.join(missing)}，请补充后重试" if missing else "入群申请信息不符合要求"
        try:
            await event.bot.api.call_action(
                "set_group_add_request", flag=flag, sub_type="add", approve=False, reason=reason
            )
            logger.info(f"[Guard] 群 {group_id} 已拒绝用户 {user_id} 入群: {reason}")
        except Exception as e:
            logger.error(f"[Guard] 拒绝入群失败: 群 {group_id} 用户 {user_id}: {e}")

    # ------------------------------------------------------------------
    # 群消息监听：更新 bot 缓存 + @禁言指令 + LLM 违规审核
    # ------------------------------------------------------------------
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def on_group_message(self, event: AstrMessageEvent):
        event = unwrap_event(event)
        group_id = event.get_group_id()
        logger.debug(f"[Guard] 收到群消息监听事件: group={group_id} sender={event.get_sender_id()} text={event.message_str[:50]!r}")
        if group_id:
            self._group_runtime[str(group_id)] = {"bot": event.bot}
        # 先尝试解析 "对@某人禁言10分钟" 类指令，命中则不再走违规审核
        if await self._try_member_ban_cmd(event):
            return
        self.guard.schedule(event)

    # ------------------------------------------------------------------
    # @某人 禁言/解禁自然语言指令：对@XXX禁言10分钟 / 对@XXX解禁
    # ------------------------------------------------------------------
    _BAN_ACT_RE = re.compile(r"(?P<unban>解除|取消禁言|解禁)|(?P<ban>禁言|静音)")
    _BAN_DUR_RE = re.compile(r"(\d+)\s*(?:秒钟|秒|分钟|分|min|m|小时|时|h)", re.IGNORECASE)
    _BAN_UNIT_SEC = {"秒钟": 1, "秒": 1, "min": 60, "分钟": 60, "分": 60, "m": 60,
                     "h": 3600, "小时": 3600, "时": 3600}
    _DEFAULT_BAN_SECONDS = 600  # 未指定时长默认禁言 10 分钟
    _BAN_MAX_SECONDS = 2592000  # OneBot 禁言时长上限：30 天

    def _extract_at_list(self, event) -> list[tuple[str, str]]:
        """从消息段中提取被 @ 的用户列表 [(qq, 昵称或None)]。"""
        ats = []
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
            if stype == "at":
                qq = str(data.get("qq") or "").strip()
                if qq.isdigit() and qq != "all":
                    ats.append((qq, data.get("name")))
        # 兜底：at 以 CQ 码文本呈现时从消息文本提取
        if not ats:
            for m in re.finditer(r"\[CQ:at,qq=(\d+)(?:[^\]]*)\]", event.message_str or ""):
                ats.append((m.group(1), None))
        return ats

    @staticmethod
    def _strip_at_text(raw: str, ats: list[tuple[str, str]]) -> str:
        """去掉消息中的 at 部分，只留命令文本用于匹配。"""
        text = re.sub(r"\[CQ:at[^\]]*\]", " ", raw)
        for _, name in ats:
            if name:
                text = re.sub(r"\s*@" + re.escape(str(name)) + r"\s*", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    async def _try_member_ban_cmd(self, event) -> bool:
        """尝试解析 '@某人禁言/解禁' 指令；命中并处理后返回 True。"""
        try:
            if not isinstance(event, AiocqhttpMessageEvent):
                return False
            ats = self._extract_at_list(event)
            if not ats:
                return False
            group_id = event.get_group_id()
            if not group_id:
                return False
            text = self._strip_at_text(event.message_str or "", ats)
            m_act = self._BAN_ACT_RE.search(text)
            if not m_act:
                return False
            unban = m_act.group("unban") is not None
            target_qq, target_name = ats[0]
            operator_qq = str(event.get_sender_id())

            # 普通成员可禁言/解禁自己；禁言他人才需要管理权限
            if target_qq != operator_qq and self._permission_verification():
                has_perm, error_msg = await check_group_and_permission(
                    event, self._allow_groupadmin_use(), event.get_sender_name()
                )
                if not has_perm:
                    await event.bot.send_group_msg(group_id=int(group_id), message=error_msg)
                    return True

            seconds = self._DEFAULT_BAN_SECONDS
            m_dur = self._BAN_DUR_RE.search(text)
            if m_dur:
                seconds = int(m_dur.group(1)) * self._BAN_UNIT_SEC[m_dur.group(2).lower()]
                seconds = min(max(seconds, 1), self._BAN_MAX_SECONDS)

            await self._exec_member_ban(event, group_id, target_qq, target_name, seconds, unban)
            return True
        except Exception as e:
            logger.error(f"@禁言指令执行异常: {e}")
            return False

    async def _exec_member_ban(self, event, group_id, target_qq, target_name, seconds, unban, notify: bool = True) -> bool:
        """执行对单个成员的禁言/解禁；notify=False 时静默执行（不向群里发提示）。"""
        if target_qq == str(event.get_self_id()):
            if notify:
                await event.bot.send_group_msg(group_id=int(group_id), message="不能对我使用禁言哦～")
            return False
        if not unban:
            # 保护：不允许禁言群主/管理员
            try:
                info = await event.bot.get_group_member_info(
                    group_id=int(group_id), user_id=int(target_qq)
                )
                role = (info or {}).get("role", "member") if isinstance(info, dict) else "member"
                if role in ("owner", "admin"):
                    kind = "群主" if role == "owner" else "群管理员"
                    if notify:
                        await event.bot.send_group_msg(
                            group_id=int(group_id), message=f"不能禁言{kind}（成员 {target_name or target_qq}）哦～"
                        )
                    return False
            except Exception as e:
                logger.warning(f"[Guard] 查询被禁言者 {target_qq} 身份失败: {e}")
        try:
            await event.bot.api.call_action(
                "set_group_ban",
                group_id=int(group_id),
                user_id=int(target_qq),
                duration=0 if unban else seconds,
            )
            label = target_name or target_qq
            tip = f"已解除成员 {label}({target_qq}) 的禁言" if unban else \
                  f"已禁言成员 {label}({target_qq}) {seconds} 秒"
            logger.info(f"群 {group_id} {tip}，操作者：{event.get_sender_name()}")
            if notify:
                await event.bot.send_group_msg(group_id=int(group_id), message=tip)
            return True
        except Exception as e:
            logger.error(f"群 {group_id} 禁言/解禁成员 {target_qq} 失败: {e}")
            if notify:
                await event.bot.send_group_msg(group_id=int(group_id), message=f"操作失败：{e}")
            return False

    async def _resolve_member_by_name(self, bot, group_id, name: str) -> Optional[str]:
        """按昵称/备注在群里查找成员，返回 QQ 号；找不到返回 None。"""
        try:
            members = await bot.get_group_member_list(group_id=int(group_id))
        except Exception as e:
            logger.warning(f"[Guard] 获取群成员列表失败: {e}")
            return None
        name = str(name or "").strip().lower()
        if not name:
            return None
        for m in members or []:
            if not isinstance(m, dict):
                continue
            for key in ("card", "nickname"):
                val = str(m.get(key) or "").strip().lower()
                if val and val == name:
                    return str(m.get("user_id"))
        return None

    @filter.llm_tool(name="set_group_member_ban")
    async def set_group_member_ban(
        self,
        event: AiocqhttpMessageEvent,
        user_id: str = "",
        enable: bool = True,
        duration: float = 600.0,
    ) -> dict:
        """
        禁言或解除禁言群内的单个成员（群主与群管理员除外）。
        与全体禁言不同，该操作只影响指定的某一个成员。
        Args:
            user_id(string): 被操作成员的 QQ 号；不知道 QQ 号时可传入其群昵称或备注名（如 "小明"）；如果请求者没有明确的禁言目标，可留空，表示禁言/解禁请求者本人。
            enable(boolean): true=对该成员禁言，false=解除该成员的禁言。
            duration(number): 禁言时长（秒），如 600 表示 10 分钟；解禁时该参数会被忽略。
        """
        event = unwrap_event(event)
        try:
            group_id = event.get_group_id()
            if not group_id:
                return {"status": "error", "message": "此操作仅可在群聊中进行"}
            operator_name = event.get_sender_name()
            operator_qq = str(event.get_sender_id())

            self._group_runtime[str(group_id)] = {"bot": event.bot}

            # user_id 留空：默认操作请求者本人；传 QQ 号直接用；传昵称则群里查找
            target_qq = str(user_id or "").strip()
            target_name = None
            if not target_qq:
                target_qq = operator_qq
            elif not target_qq.isdigit():
                target_name = target_qq
                target_qq = await self._resolve_member_by_name(event.bot, group_id, target_qq)
                if not target_qq:
                    return {"status": "error", "message": f"无法在群里找到昵称为「{user_id}」的成员，请改用其 QQ 号"}

            # 普通成员可禁言/解禁自己；操作他人才需要管理权限
            if target_qq != operator_qq and self._permission_verification():
                has_perm, error_msg = await check_group_and_permission(
                    event, self._allow_groupadmin_use(), operator_name
                )
                if not has_perm:
                    return {"status": "error", "message": error_msg}

            seconds = min(max(int(duration or 600), 1), self._BAN_MAX_SECONDS)
            # 静默执行：不向群里发“已禁言成员”提示，由 AI 在回复中说明
            ok = await self._exec_member_ban(
                event, group_id, target_qq, target_name, seconds, unban=not enable, notify=False
            )
            if not ok:
                return {"status": "error", "message": f"对 {target_name or target_qq} 的操作失败，请检查权限或目标身份"}
            action = f"解除禁言" if not enable else f"禁言 {seconds} 秒"
            logger.info(f"群 {group_id} LLM 工具禁言成员：{target_name or target_qq}，{action}，操作者：{operator_name}")
            return {"status": "success", "message": f"已对成员 {target_name or target_qq} 执行{action}"}
        except Exception as e:
            logger.error(f"禁言成员 LLM 工具执行异常: {e}")
            return {"status": "error", "message": f"操作失败: {e}"}

    # ------------------------------------------------------------------
    # 即时全体禁言（命令 + LLM 工具）
    # ------------------------------------------------------------------
    @filter.command("全体禁言")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def cmd_whole_ban_on(self, event: AstrMessageEvent):
        """指令：开启本群全体禁言"""
        return await self._cmd_whole_ban(event, enable=True)

    @filter.command("解除全体禁言")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def cmd_whole_ban_off(self, event: AstrMessageEvent):
        """指令：解除本群全体禁言"""
        return await self._cmd_whole_ban(event, enable=False)

    async def _cmd_whole_ban(self, event: AstrMessageEvent, enable: bool):
        event = unwrap_event(event)
        if not isinstance(event, AiocqhttpMessageEvent):
            return event.plain_result("此功能仅支持 QQ 群聊（aiocqhttp 平台）")
        group_id = event.get_group_id()
        if not group_id:
            return event.plain_result("此操作仅可在群聊中进行")
        operator_name = event.get_sender_name()

        if self._permission_verification():
            has_perm, error_msg = await check_group_and_permission(
                event, self._allow_groupadmin_use(), operator_name
            )
            if not has_perm:
                return event.plain_result(error_msg)

        self._group_runtime[str(group_id)] = {"bot": event.bot}
        _, msg = await self._change_whole_ban(
            group_id=group_id, enable=enable, bot=event.bot
        )
        return event.plain_result(msg)

    @filter.llm_tool(name="set_group_whole_ban")
    async def set_group_whole_ban(
        self, event: AiocqhttpMessageEvent, enable: bool
    ) -> dict:
        """
        全体禁言，即禁言整个群聊，使所有人无法发言。
        Args:
            enable(boolean): 设置为true时开启全体禁言，设置为false时关闭全群禁言
        """
        action_text = "开启" if enable else "解除"
        event = unwrap_event(event)
        try:
            group_id = event.get_group_id()
            operator_name = event.get_sender_name()
            if not group_id:
                return {"status": "error", "message": "此操作仅可在群聊中进行"}

            if self._permission_verification():
                has_perm, error_msg = await check_group_and_permission(
                    event, self._allow_groupadmin_use(), operator_name
                )
                if not has_perm:
                    return {"status": "error", "message": error_msg}

            self._group_runtime[str(group_id)] = {"bot": event.bot}
            ok, msg = await self._change_whole_ban(
                group_id=group_id, enable=enable, bot=event.bot
            )
            if not ok:
                return {"status": "error", "message": msg}
            logger.info(f"群 {group_id} 已{action_text}全体禁言，操作者：{operator_name}")
            return {"status": "success", "message": msg}
        except Exception as e:
            logger.error(f"{action_text}全体禁言，失败: {e}")
            return {"status": "error", "message": f"操作失败：无法{action_text}全体禁言，可能原因是权限不足或API错误"}

    # ------------------------------------------------------------------
    # 定时全体禁言（命令 + LLM 工具）
    # ------------------------------------------------------------------
    @filter.command("定时禁言")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def cmd_schedule_ban(self, event: AstrMessageEvent):
        """
        定时全体禁言。用法：
        /定时禁言                       查询本群定时任务
        /定时禁言 取消                  取消本群定时任务（若禁言已开启会自动解除）
        /定时禁言 10:00 20:00           今天 10:00 开启、20:00 解除（开始时间已过则顺延到明天，结束早于开始视为次日）
        /定时禁言 now 90                立即开启，90 分钟后解除
        /定时禁言 每日 22:00 06:00      每日重复：每天 22:00 开启、次日 06:00 解除，直到取消
        """
        event = unwrap_event(event)
        if not isinstance(event, AiocqhttpMessageEvent):
            return event.plain_result("此功能仅支持 QQ 群聊（aiocqhttp 平台）")
        group_id = event.get_group_id()
        if not group_id:
            return event.plain_result("此操作仅可在群聊中进行")
        operator_name = event.get_sender_name()

        if self._permission_verification():
            has_perm, error_msg = await check_group_and_permission(
                event, self._allow_groupadmin_use(), operator_name
            )
            if not has_perm:
                return event.plain_result(error_msg)

        self._group_runtime[str(group_id)] = {"bot": event.bot}

        text = re.sub(r"^[/＠@]?\s*定时禁言\s*", "", event.message_str or "").strip()
        args = text.split()

        if not args or args[0] in ("查询", "查", "list"):
            return self._query_schedule(event, group_id)

        if args[0] in ("取消", "删除全部", "clear", "del all"):
            return await self._cancel_schedule(event, group_id)

        # 删除指定周几的规则
        if args[0] in ("删除", "删", "del") and len(args) >= 2:
            return await self._delete_weekly_rule(event, group_id, args[1])

        # 按周几设置：/定时禁言 周一 22:00 06:00
        weekday_set = _parse_weekday_spec(args[0])
        if weekday_set is not None and weekday_set != "all":
            if len(args) < 3:
                return event.plain_result("用法：/定时禁言 周一 22:00 06:00（或 周一-周五、周末、每天）")
            return await self._set_weekly_rule(event, group_id, weekday_set, args[1], args[2])

        # 每日模式：/定时禁言 每日 22:00 06:00
        if weekday_set == "all":
            if len(args) < 3:
                return event.plain_result("用法：/定时禁言 每天 22:00 06:00")
            args = args[1:]

        if len(args) >= 2:
            start_str, end_str = args[0], args[1]
            if weekday_set == "all" and start_str.strip().lower() == "now":
                return event.plain_result("每日任务必须指定开始时间 HH:MM，如：/定时禁言 每天 22:00 06:00")
            try:
                start_ts, end_ts = parse_schedule_times(start_str, end_str)
            except ValueError as e:
                return event.plain_result(f"时间格式错误：{e}\n用法：/定时禁言 10:00 20:00 或 /定时禁言 每天 22:00 06:00")

            recurring = weekday_set == "all"
            self.scheduler.set(
                str(group_id), start_ts, end_ts, bot=event.bot, recurring=recurring
            )
            start_txt = "立即" if start_str.strip().lower() == "now" else time.strftime(
                "%H:%M", time.localtime(start_ts)
            )
            end_txt = time.strftime("%H:%M", time.localtime(end_ts))
            logger.info(f"群 {group_id} 设定{'每日' if recurring else ''}定时全体禁言：{start_txt} -> {end_txt}")
            return event.plain_result(
                f"已设定{'每日' if recurring else ''}定时全体禁言：{start_txt} 开启，{end_txt} 解除。\n"
                f"发送 /定时禁言 可查询本任务，/定时禁言 取消 可取消。"
            )

        return event.plain_result(
            "用法：\n"
            "/定时禁言 10:00 20:00 —— 单次定时开禁与解禁\n"
            "/定时禁言 now 90 —— 立即开启，90 分钟后解除\n"
            "/定时禁言 每天 22:00 06:00 —— 每日重复（直到取消）\n"
            "/定时禁言 周一 22:00 06:00 —— 按周几单独设置（支持 周一-周五、周末）\n"
            "/定时禁言 删除 周一 —— 删除某天的规则\n"
            "/定时禁言 —— 查询本群任务\n"
            "/定时禁言 取消 —— 取消本群全部任务"
        )

    async def _set_weekly_rule(
        self, event: AstrMessageEvent, group_id, weekday_set: set, start_str: str, end_str: str
    ) -> object:
        """为指定的若干星期几设置禁言窗口，与已有周规则合并（不影响其他类型任务）。"""
        try:
            start_ts, end_ts = parse_schedule_times(start_str, end_str)
        except ValueError as e:
            return event.plain_result(f"时间格式错误：{e}")

        # 定位本群已有的每周任务
        old = next(
            (t for t in self.scheduler.get(str(group_id)) if t.get("mode") == "weekly"), None
        )
        if old and old.get("started"):
            # 正在执行禁言的周任务：规则更新后先解除本轮
            await self._apply_scheduled(group_id, old, enable=False)
            old["started"] = False
            old["current_end_ts"] = 0

        rules = dict(old.get("rules") or {}) if old else {}
        rule = _to_weekly_rule(start_ts, end_ts)
        for day in weekday_set:
            rules[str(day)] = rule
        self.scheduler.set_weekly(str(group_id), rules, bot=event.bot)

        days_txt = "、".join(_WEEKDAY_CN[d] for d in sorted(weekday_set))
        logger.info(f"群 {group_id} 设定周规则 {days_txt} {start_str} -> {end_str}")
        return event.plain_result(
            f"已为 {days_txt} 设定全体禁言：{start_str} 开启，{end_str} 解除。\n"
            f"发送 /定时禁言 可查询全部规则，/定时禁言 删除 {days_txt.split('、')[0]} 可删除。"
        )

    async def _delete_weekly_rule(self, event: AstrMessageEvent, group_id, spec: str) -> object:
        weekday_set = _parse_weekday_spec(spec)
        if weekday_set is None:
            return event.plain_result(f"无法识别的星期：{spec}（示例：周一、周五-周一、周末）")
        old = next(
            (t for t in self.scheduler.get(str(group_id)) if t.get("mode") == "weekly"), None
        )
        if not old:
            return event.plain_result("本群没有按周几设置的禁言规则")
        rules = old.get("rules") or {}
        removed = [d for d in weekday_set if d in rules]
        if not removed:
            return event.plain_result("这些星期没有设置规则")
        for d in removed:
            rules.pop(str(d))
        if old.get("started"):
            await self._apply_scheduled(group_id, old, enable=False)
            old["started"] = False
            old["current_end_ts"] = 0
        if rules:
            self.scheduler.set_weekly(str(group_id), rules, bot=event.bot)
            days_txt = "、".join(_WEEKDAY_CN[d] for d in sorted(removed))
            return event.plain_result(f"已删除 {days_txt} 的禁言规则")
        self.scheduler.remove(str(group_id), old.get("id"))
        return event.plain_result("已删除全部周规则，每周禁言任务已移除")

    def _query_schedule(self, event: AstrMessageEvent, group_id) -> object:
        tasks = self.scheduler.get(str(group_id))
        if not tasks:
            return event.plain_result("本群当前没有定时全体禁言任务")
        if len(tasks) == 1:
            return event.plain_result(self._describe_task(group_id, tasks[0]))
        lines = [f"本群共有 {len(tasks)} 个定时全体禁言任务："]
        for i, t in enumerate(tasks, 1):
            lines.append(f"{i}. {self._describe_task(group_id, t)}")
        lines.append("发送 /定时禁言 取消 可取消本群全部任务")
        return event.plain_result("\n".join(lines))

    def _describe_task(self, group_id, sched: dict) -> str:
        """单条定时任务的可读描述。"""
        if sched.get("mode") == "weekly":
            rules = sched.get("rules") or {}
            day_lines = []
            for day in range(1, 8):
                rule = rules.get(str(day))
                if not rule:
                    continue
                start_min = int(rule.get("start_min") or 0)
                duration_min = int(rule.get("duration_min") or 0)
                end_min = start_min + duration_min
                day_lines.append(
                    f"{_WEEKDAY_CN[day]}：{start_min // 60:02d}:{start_min % 60:02d} ~ "
                    f"{end_min // 60 % 24:02d}:{end_min % 60:02d}"
                    + ("（次日）" if end_min >= 1440 else "")
                )
            status = "【禁言中】" if sched.get("started") else ""
            footer = " /定时禁言 删除 周几 可删除规则"
            return f"[每周] {'；'.join(day_lines)}{status}{footer}"
        recurring = "【每日重复】" if sched.get("mode") == "daily" else "[单次]"
        status = "【禁言中】" if sched.get("started") else "等待触发"
        return (
            f"{recurring} {time.strftime('%m-%d %H:%M', time.localtime(sched['start_ts']))} "
            f"→ {time.strftime('%m-%d %H:%M', time.localtime(sched['end_ts']))}（{status}）"
        )

    async def _cancel_schedule(self, event: AstrMessageEvent, group_id) -> object:
        tasks = self.scheduler.get(str(group_id))
        if not tasks:
            return event.plain_result("本群当前没有定时全体禁言任务")
        # 先解禁正在执行的，再整体移除
        for t in tasks:
            if t.get("started"):
                ok, msg = await self._apply_scheduled(group_id, t, enable=False)
                if not ok:
                    return event.plain_result(f"已移除任务，但解除禁言失败：{msg}")
        self.scheduler.remove(str(group_id))
        return event.plain_result("已取消本群全部定时禁言任务，并解除当前全体禁言")

    @filter.llm_tool(name="schedule_group_whole_ban")
    async def schedule_group_whole_ban(
        self,
        event: AiocqhttpMessageEvent,
        start_time: str,
        end_time: str,
        reason: str = "",
        recurring: bool = False,
        weekdays: str = "",
    ) -> dict:
        """
        定时全体禁言：为当前群聊设定一个时间段，到点自动开启全体禁言，时间到自动解除，全程无需人工干预。
        常用于：深夜/工作时段自动静音、考试或直播期间的临时全员禁言等场景。
        Args:
            start_time(string): 开始时间。"now" 表示立即开启；或 "HH:MM" 格式（如 "22:00"），若该时间已过将顺延到明天。
            end_time(string): 结束时间。"HH:MM" 格式（如 "06:00"，早于开始时间视为次日凌晨）；或纯数字分钟数（如 "480"，表示从开始时间起持续480分钟）。
            recurring(boolean): 是否每日重复。true 表示每天按 start_time 开启、按 end_time 解除并自动循环，直到被取消；每日模式时 start_time 必须为 HH:MM。
            weekdays(string): 按星期几设置（可空）。如 "周一"、"周一-周五"、"周末"、"工作日"、"每天"；传入后将为这些星期设置独立时段，多个星期可合并，重复调用按星期合并或覆盖。传 "每天" 等同 recurring=true。
            reason(string): 设定定时禁言的理由（如"深夜时段保持安静"），仅用于记录。
        """
        event = unwrap_event(event)
        try:
            group_id = event.get_group_id()
            operator_name = event.get_sender_name()
            if not group_id:
                return {"status": "error", "message": "此操作仅可在群聊中进行"}

            if self._permission_verification():
                has_perm, error_msg = await check_group_and_permission(
                    event, self._allow_groupadmin_use(), operator_name
                )
                if not has_perm:
                    return {"status": "error", "message": error_msg}

            weekday_set = _parse_weekday_spec(weekdays) if (weekdays or "").strip() else None
            if weekday_set is not None and weekday_set == "all":
                recurring = True
                weekday_set = None

            if weekday_set is not None:
                start_ts, end_ts = parse_schedule_times(start_time, end_time)
                self._group_runtime[str(group_id)] = {"bot": event.bot}
                # 定位本群已有每周任务并合并规则，不影响其他类型任务
                old = next(
                    (t for t in self.scheduler.get(str(group_id)) if t.get("mode") == "weekly"), None
                )
                if old and old.get("started"):
                    await self._apply_scheduled(group_id, old, enable=False)
                    old["started"] = False
                    old["current_end_ts"] = 0
                rules = dict(old.get("rules") or {}) if old else {}
                rule = _to_weekly_rule(start_ts, end_ts)
                for day in weekday_set:
                    rules[str(day)] = rule
                self.scheduler.set_weekly(str(group_id), rules, bot=event.bot)
                days_txt = "、".join(_WEEKDAY_CN[d] for d in sorted(weekday_set))
                reason_txt = f"，原因：{reason}" if reason else ""
                msg = f"已为 {days_txt} 设定定时全体禁言：{start_time} 开启，{end_time} 解除{reason_txt}"
                logger.info(f"群 {group_id} 设定周规则 {days_txt}: {start_time} -> {end_time}")
                return {"status": "success", "message": msg}

            if recurring and start_time.strip().lower() == "now":
                return {"status": "error", "message": "每日重复模式必须指定 HH:MM 开始时间"}

            start_ts, end_ts = parse_schedule_times(start_time, end_time)
            self._group_runtime[str(group_id)] = {"bot": event.bot}
            # 追加新任务：不再覆盖已有规划
            self.scheduler.set(
                str(group_id), start_ts, end_ts, bot=event.bot, recurring=recurring
            )
            start_txt = "立即" if start_time.strip().lower() == "now" else time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(start_ts)
            )
            end_txt = time.strftime("%Y-%m-%d %H:%M", time.localtime(end_ts))
            reason_txt = f"，原因：{reason}" if reason else ""
            mode = "每日" if recurring else ""
            msg = f"已设定{mode}定时全体禁言：{start_txt} 开启，{end_txt} 自动解除{reason_txt}"
            logger.info(f"群 {group_id} 设定{mode}定时全体禁言: {start_txt} -> {end_txt}")
            return {"status": "success", "message": msg}
        except ValueError as e:
            return {"status": "error", "message": f"时间格式错误：{e}"}
        except Exception as e:
            logger.error(f"设定定时全体禁言失败: {e}")
            return {"status": "error", "message": "设定定时全体禁言失败，请稍后重试"}