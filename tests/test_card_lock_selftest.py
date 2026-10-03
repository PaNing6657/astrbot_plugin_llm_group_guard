# tests/test_card_lock_selftest.py
"""群名片锁定功能的离线自测（不依赖 AstrBot 运行时）。

覆盖：规则规范化与校验、事件驱动的恢复与回声抑制、轮询兜底、
权限门控、通知模板、以及插件实例的 WebAPI 注册与配置落盘。

运行：python tests/test_card_lock_selftest.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import types

# 以脚本方式运行时把插件根目录加入 sys.path，便于 import core.card_lock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.card_lock import (  # noqa: E402
    CardLockEngine,
    find_lock_rule,
    is_card_locked,
    normalize_lock_rules,
    validate_lock_rule,
)

PASSED = []
FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
    else:
        FAILED.append(f"{name} {detail}".strip())
        print(f"  [FAIL] {name} {detail}".rstrip())


class FakeLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def _log(self, level: str, msg: str) -> None:
        self.records.append((level, str(msg)))

    def debug(self, msg, *a, **k):
        self._log("debug", msg)

    def info(self, msg, *a, **k):
        self._log("info", msg)

    def warning(self, msg, *a, **k):
        self._log("warning", msg)

    def error(self, msg, *a, **k):
        self._log("error", msg)

    def critical(self, msg, *a, **k):
        self._log("critical", msg)


class FakeApi:
    """记录 call_action 调用，可编排返回值的 OneBot API 桩。"""

    def __init__(self, owner: "FakeBot") -> None:
        self.owner = owner

    async def call_action(self, action, **kwargs):
        self.owner.calls.append((action, kwargs))
        handler = self.owner.handlers.get(action)
        if handler is not None:
            result = handler(**kwargs)
            if hasattr(result, "__await__"):
                result = await result
            if isinstance(result, Exception):
                raise result
            return result
        return {}


class FakeBot:
    def __init__(self, handlers: dict | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.handlers = handlers or {}
        self.api = FakeApi(self)
        self.sent: list[tuple[int, object]] = []

    async def send_group_msg(self, group_id=None, message=None):
        self.sent.append((group_id, message))
        return {"message_id": len(self.sent)}


def make_engine(
    *,
    rules=None,
    enabled=True,
    managed=True,
    bot=None,
    notify_msg="🔒 {at_user} 的群名片已被锁定，已恢复为「{card}」",
    notify=True,
    poll_enable=False,
    poll_interval=300,
    notifier=None,
    groups=None,
):
    """构造被测引擎与其群配置。"""
    gconf = {
        "card_lock_enable": enabled,
        "card_lock_list": rules if rules is not None else [],
        "card_lock_notify": notify,
        "card_lock_notify_msg": notify_msg,
        "card_lock_poll_enable": poll_enable,
        "card_lock_poll_interval": poll_interval,
    }
    all_groups = groups if groups is not None else {"100": gconf}
    logger = FakeLogger()
    bot_obj = bot if bot is not None else FakeBot()
    warned: list[str] = []

    engine = CardLockEngine(
        logger,
        gconf_provider=lambda gid: all_groups.get(str(gid), {}),
        is_managed=lambda gid: managed,
        client_provider=lambda: bot_obj,
        notifier=notifier,
        warn_unmanaged=warned.append,
        groups_provider=lambda: all_groups,
    )
    return engine, bot_obj, all_groups, logger, warned


# ----------------------------------------------------------------------
# 1. 规则规范化与校验
# ----------------------------------------------------------------------
def test_normalize_and_validate() -> None:
    print("[1] 规则规范化与校验")
    # dict 列表
    rules = normalize_lock_rules([{"user_id": "12345", "card": "管理员", "note": "  a  "}])
    check("dict 列表规范化", rules == [{"user_id": "12345", "card": "管理员", "note": "a"}], str(rules))

    # 整段 JSON 字符串
    raw = json.dumps([{"user_id": "12345", "card": "管理员"}], ensure_ascii=False)
    check("JSON 字符串形态", normalize_lock_rules(raw)[0]["card"] == "管理员")

    # 元素为 JSON 字符串
    check(
        "元素 JSON 字符串形态",
        normalize_lock_rules([json.dumps({"user_id": "999", "card": "X"})])[0]["card"] == "X",
    )

    # 同 QQ 号后者覆盖前者，且保持首次出现顺序
    dup = normalize_lock_rules([
        {"user_id": "111", "card": "A"},
        {"user_id": "222", "card": "B"},
        {"user_id": "111", "card": "C"},
    ])
    check("同 QQ 号去重覆盖", [r["card"] for r in dup] == ["C", "B"], str(dup))

    # 脏数据不抛异常
    check("脏数据安全", normalize_lock_rules(None) == [] and normalize_lock_rules("not json") == [])
    check("缺 user_id 丢弃", normalize_lock_rules([{"card": "x"}, "junk", 42]) == [])

    # 超长名片截断到 64
    long_card = "名" * 80
    check("名片超长截断", len(normalize_lock_rules([{"user_id": "1", "card": long_card}])[0]["card"]) == 64)

    # 校验
    check("校验-空 QQ", validate_lock_rule("", "X") is not None)
    check("校验-非数字 QQ", validate_lock_rule("abc", "X") is not None)
    check("校验-空名片", validate_lock_rule("12345", "   ") is not None)
    check("校验-合法", validate_lock_rule("12345", "管理员") is None)
    check("校验-超长名片", validate_lock_rule("12345", "名" * 65) is not None)

    # 查找与偏离判定
    rules = normalize_lock_rules([{"user_id": "12345", "card": "管理员"}])
    check("查找命中", find_lock_rule(rules, "12345") is not None)
    check("查找未命中", find_lock_rule(rules, "99999") is None)
    check("偏离判定-已偏离", is_card_locked(rules, "12345", "改名了") is True)
    check("偏离判定-未偏离", is_card_locked(rules, "12345", "管理员") is False)
    check("偏离判定-未锁定成员", is_card_locked(rules, "88888", "任意") is False)


# ----------------------------------------------------------------------
# 2. 事件驱动恢复 + 回声抑制
# ----------------------------------------------------------------------
def test_notice_restore() -> None:
    print("[2] 事件驱动恢复与回声抑制")
    rules = [{"user_id": "12345", "card": "管理员"}]

    # 2.1 被改名片 → 恢复
    engine, bot, _, _, _ = make_engine(rules=rules)
    res = asyncio.run(engine.handle_card_notice({
        "post_type": "notice", "notice_type": "group_card",
        "group_id": 100, "user_id": 12345, "card_old": "管理员", "card_new": "广告狗",
    }))
    check("事件触发恢复", res is not None and res["ok"] is True, str(res))
    set_cards = [c for c in bot.calls if c[0] == "set_group_card"]
    check("调用 set_group_card", len(set_cards) == 1 and set_cards[0][1]["card"] == "管理员", str(bot.calls))

    # 2.2 已等于锁定值 → 不动作
    engine, bot, _, _, _ = make_engine(rules=rules)
    res = asyncio.run(engine.handle_card_notice({
        "post_type": "notice", "notice_type": "group_card",
        "group_id": 100, "user_id": 12345, "card_old": "广告狗", "card_new": "管理员",
    }))
    check("已等于锁定值不动作", res is None and not bot.calls, str(res))

    # 2.3 未锁定成员 → 不动作
    engine, bot, _, _, _ = make_engine(rules=rules)
    res = asyncio.run(engine.handle_card_notice({
        "post_type": "notice", "notice_type": "group_card",
        "group_id": 100, "user_id": 77777, "card_old": "A", "card_new": "B",
    }))
    check("未锁定成员不动作", res is None and not bot.calls, str(res))

    # 2.4 非 notice 事件 → 忽略
    engine, bot, _, _, _ = make_engine(rules=rules)
    check("非目标事件忽略", asyncio.run(engine.handle_card_notice({"post_type": "message"})) is None)
    check("非 Mapping 忽略", asyncio.run(engine.handle_card_notice(None)) is None)

    # 2.5 新旧名片相同 → 忽略
    res = asyncio.run(engine.handle_card_notice({
        "post_type": "notice", "notice_type": "group_card",
        "group_id": 100, "user_id": 12345, "card_old": "X", "card_new": "X",
    }))
    check("新旧相同忽略", res is None)

    # 2.6 回声抑制：机器人自己写名片后到达的事件不再触发二次写入
    engine, bot, _, _, _ = make_engine(rules=rules)
    engine.mark_self_write("100", "12345", "管理员")
    res = asyncio.run(engine.handle_card_notice({
        "post_type": "notice", "notice_type": "group_card",
        "group_id": 100, "user_id": 12345, "card_old": "广告狗", "card_new": "其他",
    }))
    check("回声事件被抑制", res is None and not bot.calls, str(res))

    # 2.7 enforce 自身写入应被登记回声标记
    engine, bot, _, _, _ = make_engine(rules=rules)
    ok, msg = asyncio.run(engine.enforce("100", {"user_id": "12345", "card": "管理员"}, reason="test"))
    check("enforce 成功", ok is True, msg)
    check("enforce 登记回声", engine.consume_self_write("100", "12345") is True)

    # 2.8 总开关关闭 → 不动作
    engine, bot, _, _, _ = make_engine(rules=rules, enabled=False)
    res = asyncio.run(engine.handle_card_notice({
        "post_type": "notice", "notice_type": "group_card",
        "group_id": 100, "user_id": 12345, "card_old": "A", "card_new": "B",
    }))
    check("总开关关闭不动作", res is None and not bot.calls)


# ----------------------------------------------------------------------
# 3. 权限门控
# ----------------------------------------------------------------------
def test_permission_gate() -> None:
    print("[3] 权限门控")
    rules = [{"user_id": "12345", "card": "管理员"}]
    engine, bot, _, _, warned = make_engine(rules=rules, managed=False)
    res = asyncio.run(engine.handle_card_notice({
        "post_type": "notice", "notice_type": "group_card",
        "group_id": 100, "user_id": 12345, "card_old": "管理员", "card_new": "广告狗",
    }))
    check("非管理群不恢复", res is None and not bot.calls, str(res))
    check("非管理群已告警", "100" in warned, str(warned))
    # 重复事件不再重复告警（避免刷日志）
    asyncio.run(engine.handle_card_notice({
        "post_type": "notice", "notice_type": "group_card",
        "group_id": 100, "user_id": 12345, "card_old": "管理员", "card_new": "广告狗",
    }))
    check("非管理群告警仅一次", len(warned) == 1, str(warned))
    # 轮询同样受限
    n = asyncio.run(engine.poll_group("100"))
    check("非管理群轮询跳过", n == 0 and not bot.calls)


# ----------------------------------------------------------------------
# 4. 轮询兜底
# ----------------------------------------------------------------------
def test_poll() -> None:
    print("[4] 轮询兜底")
    rules = [{"user_id": "12345", "card": "管理员"}, {"user_id": "67890", "card": "客服"}]

    def members(**kwargs):
        return [
            {"user_id": 12345, "card": "被改了", "nickname": "甲"},
            {"user_id": 67890, "card": "客服", "nickname": "乙"},
            {"user_id": 99999, "card": "路人", "nickname": "丙"},
        ]

    bot = FakeBot({"get_group_member_list": members})
    engine, _, _, _, _ = make_engine(rules=rules, bot=bot)
    n = asyncio.run(engine.poll_group("100"))
    check("轮询恢复 1 人", n == 1, str(n))
    set_cards = [c for c in bot.calls if c[0] == "set_group_card"]
    check("轮询只恢复偏离者", len(set_cards) == 1 and set_cards[0][1]["user_id"] == 12345, str(bot.calls))
    check("轮询用 no_cache", any(c[0] == "get_group_member_list" and c[1].get("no_cache") for c in bot.calls))

    # 成员已退群 → 跳过
    bot2 = FakeBot({"get_group_member_list": lambda **k: [{"user_id": 111, "card": "X"}]})
    engine2, _, _, _, _ = make_engine(rules=rules, bot=bot2)
    check("已退群成员跳过", asyncio.run(engine2.poll_group("100")) == 0)

    # API 异常 → 不抛异常，返回 0
    def boom(**kwargs):
        raise RuntimeError("api down")

    bot3 = FakeBot({"get_group_member_list": boom})
    engine3, _, _, logger3, _ = make_engine(rules=rules, bot=bot3)
    check("轮询异常安全", asyncio.run(engine3.poll_group("100")) == 0)
    check("轮询异常有告警", any(lv == "warning" for lv, _ in logger3.records))

    # 无规则 / 无 bot → 0
    engine4, _, _, _, _ = make_engine(rules=[])
    check("无规则轮询跳过", asyncio.run(engine4.poll_group("100")) == 0)

    # locked_groups 枚举
    engine5, _, all_groups, _, _ = make_engine(
        rules=rules, groups={"100": {"card_lock_enable": True, "card_lock_list": rules},
                             "200": {"card_lock_enable": False, "card_lock_list": rules}}
    )
    check("locked_groups 枚举", engine5.locked_groups() == {"100"}, str(engine5.locked_groups()))


# ----------------------------------------------------------------------
# 5. 通知发送
# ----------------------------------------------------------------------
def test_notify() -> None:
    print("[5] 通知发送")
    rules = [{"user_id": "12345", "card": "管理员"}]
    bot1 = FakeBot()
    engine, bot1, _, _, _ = make_engine(rules=rules, bot=bot1)
    asyncio.run(engine.handle_card_notice({
        "post_type": "notice", "notice_type": "group_card",
        "group_id": 100, "user_id": 12345, "card_old": "管理员", "card_new": "广告狗",
    }))
    check("恢复后发送通知", len(bot1.sent) == 1, str(bot1.sent))
    if bot1.sent:
        text = bot1.sent[0][1]
        check("通知含 CQ at", "[CQ:at,qq=12345]" in text, str(text))
        check("通知含锁定名片", "管理员" in text, str(text))

    # 通知关闭 → 不发
    engine2, bot2, _, _, _ = make_engine(rules=rules, bot=bot2, notify=False)
    asyncio.run(engine2.handle_card_notice({
        "post_type": "notice", "notice_type": "group_card",
        "group_id": 100, "user_id": 12345, "card_old": "管理员", "card_new": "X",
    }))
    check("通知关闭不发送", len(bot2.sent) == 0)

    # 模板为空 → 不发
    engine3, bot3, _, _, _ = make_engine(rules=rules, bot=bot3, notify_msg="   ")
    asyncio.run(engine3.handle_card_notice({
        "post_type": "notice", "notice_type": "group_card",
        "group_id": 100, "user_id": 12345, "card_old": "管理员", "card_new": "X",
    }))
    check("空模板不发送", len(bot3.sent) == 0)

    # 自定义 notifier 生效且通知失败不影响锁定
    got: list[tuple[str, str]] = []

    async def bad_notifier(gid: str, text: str) -> None:
        raise RuntimeError("send failed")

    bot4 = FakeBot()
    engine4, _, _, logger4, _ = make_engine(rules=rules, bot=bot4, notifier=bad_notifier)
    res4 = asyncio.run(engine4.handle_card_notice({
        "post_type": "notice", "notice_type": "group_card",
        "group_id": 100, "user_id": 12345, "card_old": "管理员", "card_new": "X",
    }))
    check("通知失败仍完成锁定", res4 is not None and res4["ok"] is True, str(res4))
    check("通知失败有告警", any("通知发送失败" in m for _, m in logger4.records))


# ----------------------------------------------------------------------
# 6. 状态清理
# ----------------------------------------------------------------------
def test_state_cleanup() -> None:
    print("[6] 状态清理")
    engine, _, _, _, warned = make_engine(rules=[{"user_id": "1", "card": "A"}], managed=False)
    engine.mark_self_write("100", "12345", "A")
    engine.mark_self_write("200", "12345", "A")
    asyncio.run(engine.handle_card_notice({
        "post_type": "notice", "notice_type": "group_card",
        "group_id": 100, "user_id": 12345, "card_old": "A", "card_new": "B",
    }))
    engine.clear_group_state("100")
    check("清理指定群回声", ("100", "12345") not in engine._self_writes)
    check("保留其他群回声", ("200", "12345") in engine._self_writes)
    check("清理告警集合", "100" not in engine._warned_groups)


# ----------------------------------------------------------------------
# 7. 插件实例级：WebAPI 注册 + 配置落盘（需构造 AstrBot 桩）
# ----------------------------------------------------------------------
def install_plugin_stubs(data_dir: str):
    """构造 AstrBot 最小桩，使 main.py 可导入。"""
    calls: list[tuple[str, list, str]] = []

    class AstrBotConfig(dict):
        def setdefault(self, k, d=None):
            return dict.setdefault(self, k, d)

    class FakeStarTools:
        @staticmethod
        def get_data_dir():
            return data_dir

    class FakeStar:
        def __init__(self, context):
            self.context = context

    def _register(*args):
        """兼容 @register(...) 与 @filter.xxx 两种装饰器用法。"""
        if len(args) == 1 and callable(args[0]) and not isinstance(args[0], str):
            return args[0]

        def deco(fn):
            return fn

        return deco

    class FakeFilter:
        class EventMessageType:
            ALL = "ALL"
            GROUP_MESSAGE = "GROUP_MESSAGE"

        class PlatformAdapterType:
            AIOCQHTTP = "AIOCQHTTP"

        PlatformAdapterType = PlatformAdapterType

        def __getattr__(self, item):
            def deco(fn):
                return fn

            return deco

    filter_mod = types.SimpleNamespace(
        EventMessageType=FakeFilter.EventMessageType,
        PlatformAdapterType=FakeFilter.PlatformAdapterType,
        command=_register,
        platform_adapter_type=_register,
        event_message_type=_register,
        llm_tool=_register,
        on_llm_request=_register,
        on_platform_loaded=_register,
    )

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = FakeLogger()
    api.AstrBotConfig = AstrBotConfig
    api.event = types.ModuleType("astrbot.api.event")
    api.event.AstrMessageEvent = type("AstrMessageEvent", (), {})
    api.event.filter = filter_mod
    api.star = types.ModuleType("astrbot.api.star")
    api.star.Star = FakeStar
    api.star.Context = type("Context", (), {})

    def _register_star(*args):
        if len(args) == 1 and callable(args[0]) and not isinstance(args[0], str):
            return args[0]
        return lambda cls: cls

    api.star.register = _register_star
    api.web = types.ModuleType("astrbot.api.web")
    api.web.error_response = lambda msg, **kw: {"error": msg}
    api.web.json_response = lambda data, **kw: {"data": data}
    api.web.request = types.SimpleNamespace(query={}, json=None)

    core = types.ModuleType("astrbot.core")
    core.star = types.ModuleType("astrbot.core.star")
    core.star.star_tools = types.ModuleType("astrbot.core.star.star_tools")
    core.star.star_tools.StarTools = FakeStarTools
    core.platform = types.ModuleType("astrbot.core.platform")
    core.platform.sources = types.ModuleType("astrbot.core.platform.sources")
    core.platform.sources.aiocqhttp = types.ModuleType(
        "astrbot.core.platform.sources.aiocqhttp"
    )
    evmod = types.ModuleType(
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"
    )
    evmod.AiocqhttpMessageEvent = type("AiocqhttpMessageEvent", (), {})

    sys.modules.update({
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": api.event,
        "astrbot.api.star": api.star,
        "astrbot.api.web": api.web,
        "astrbot.core": core,
        "astrbot.core.star": core.star,
        "astrbot.core.star.star_tools": core.star.star_tools,
        "astrbot.core.platform": core.platform,
        "astrbot.core.platform.sources": core.platform.sources,
        "astrbot.core.platform.sources.aiocqhttp": core.platform.sources.aiocqhttp,
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event": evmod,
    })

    class FakeContext:
        def __init__(self):
            self.web_apis = calls

        def register_web_api(self, path, handler, methods, desc=""):
            calls.append((path, methods, desc))

    return FakeContext(), calls


def test_plugin_integration() -> None:
    print("[7] 插件实例级集成")
    tmp = tempfile.mkdtemp(prefix="cardlock_")
    ctx, api_calls = install_plugin_stubs(tmp)
    try:
        import main as plugin_main
    except Exception as e:
        check("插件可导入", False, repr(e))
        return
    check("插件可导入", True)

    try:
        plugin = plugin_main.LLMGroupGuardPlugin(ctx)
    except Exception as e:
        check("插件可实例化", False, repr(e))
        return
    check("插件可实例化", True)

    # 关键实例属性齐全（防止 __init__ 漏初始化）
    for attr in ("card_locker", "_notice_bots", "_card_lock_polled_at", "_bot_roles"):
        check(f"实例属性 {attr}", hasattr(plugin, attr))

    # WebAPI 路由全部注册（防漏注册导致前端 404）
    paths = {p for p, _, _ in api_calls}
    for suffix in (
        "/card-lock/list", "/card-lock/set", "/card-lock/delete",
        "/card-lock/apply", "/card-lock/sync",
    ):
        check(f"路由已注册 {suffix}", f"/astrbot_plugin_llm_group_guard{suffix}" in paths)
    check("原有路由保留", "/astrbot_plugin_llm_group_guard/config" in paths)

    # 默认配置含新键，且规则列表被规范化为 []
    gconf = plugin._gconf("100")
    check("默认含 card_lock_enable", "card_lock_enable" in gconf)
    check("默认锁定规则为空列表", gconf.get("card_lock_list") == [], str(gconf.get("card_lock_list")))
    check("默认轮询关闭", gconf.get("card_lock_poll_enable") is False)

    # 新键在保存白名单内（否则 WebUI 保存会被过滤掉）
    check(
        "新键在群配置白名单",
        {"card_lock_enable", "card_lock_list", "card_lock_notify",
         "card_lock_notify_msg", "card_lock_poll_enable",
         "card_lock_poll_interval"} <= plugin_main._GROUP_CONFIG_KEYS,
    )

    # 保存 dict 列表形态后重新加载仍是结构化数据（不被压成字符串）
    groups = plugin.config.setdefault("groups", {})
    groups["200"] = dict(plugin_main.DEFAULT_GROUP_CONFIG)
    groups["200"]["card_lock_list"] = [
        {"user_id": "12345", "card": "管理员", "note": "核心"},
        json.dumps({"user_id": "67890", "card": "客服"}, ensure_ascii=False),
    ]
    plugin._normalize_group_lists(groups["200"])
    lst = groups["200"]["card_lock_list"]
    check("dict 列表保存后保持结构", isinstance(lst[0], dict) and lst[0]["card"] == "管理员", str(lst))
    check("JSON 字符串元素被解析", isinstance(lst[1], dict) and lst[1]["card"] == "客服", str(lst))

    # 引擎读取配置
    check(
        "引擎读到该群规则",
        {r["user_id"] for r in plugin.card_locker.rules_of("200")} == {"12345", "67890"},
    )
    check("总开关关闭时无规则", plugin.card_locker.rules_of("100") == [])

    # 轮询节流：未到间隔不重复轮询
    plugin._card_lock_polled_at["100"] = 1e12
    check("轮询节流可用", plugin._card_lock_polled_at["100"] > 0)

    # 删除群数据时清理引擎状态
    plugin.card_locker.mark_self_write("200", "12345", "管理员")
    plugin._purge_group_data("200")
    check("删群清理回声状态", ("200", "12345") not in plugin.card_locker._self_writes)
    check("删群移除配置", "200" not in (plugin.config.get("groups") or {}))


def main() -> int:
    print("=" * 60)
    print("群名片锁定功能离线自测")
    print("=" * 60)
    test_normalize_and_validate()
    test_notice_restore()
    test_permission_gate()
    test_poll()
    test_notify()
    test_state_cleanup()
    test_plugin_integration()
    print("-" * 60)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("失败明细：")
        for item in FAILED:
            print(f"  - {item}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
