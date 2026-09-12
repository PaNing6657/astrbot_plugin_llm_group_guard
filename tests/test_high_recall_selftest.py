"""离线自测：高召回模式（每日定时切换另一套审核规则与模型 + 开关提示）。

运行：python tests/test_high_recall_selftest.py

校验：
- core/high_recall.py 的时段窗口（含跨天）、下一个切换节点与时间校验
- MessageGuard：高召回生效时改用高召回审核要求与独立模型，留空项沿用常规设置
- 插件调度层：到点自动开启 / 关闭并发送提示、无连接时排队补发、手动临时状态到期交还定时规则
- WebUI 保存：定时开关打开时校验时间，开启 / 关闭时状态立即对齐（关闭时不残留生效）
"""

import asyncio
import os
import sys
import time
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_join_review_selftest import _install_astrbot_stubs, FakeContext  # noqa: E402

_install_astrbot_stubs()

sys.path.insert(0, ".")

from test_keyword_pre_review_selftest import _Event  # noqa: E402
from core.high_recall import (  # noqa: E402
    high_recall_next_flip,
    high_recall_window,
    in_high_recall_window,
    validate_high_recall_times,
)
from core.llm_reviewer import LLMReviewer  # noqa: E402
from core.message_guard import MessageGuard  # noqa: E402


def _ts(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm).timestamp()


def _hhmm(delta_sec):
    """相对当前的 HH:MM（用同一时钟生成时段，跨天场景也稳定）。"""
    return time.strftime("%H:%M", time.localtime(time.time() + delta_sec))


# ----------------------------------------------------------------------
# core/high_recall.py：纯时间函数
# ----------------------------------------------------------------------
def case_window_math():
    """跨天时段（22:00~06:00）与同日时段（09:00~18:00）的窗口判定与边界。"""
    w = high_recall_window("22:00", "06:00", now=_ts(2026, 9, 12, 23, 0))
    assert w == (_ts(2026, 9, 12, 22, 0), _ts(2026, 9, 13, 6, 0)), w
    # 凌晨仍处于昨晩开启的窗口内
    w = high_recall_window("22:00", "06:00", now=_ts(2026, 9, 13, 3, 0))
    assert w == (_ts(2026, 9, 12, 22, 0), _ts(2026, 9, 13, 6, 0)), w
    # 边界：22:00 整点已开启，06:00 整点已结束
    assert in_high_recall_window("22:00", "06:00", now=_ts(2026, 9, 12, 22, 0)) is True
    assert in_high_recall_window("22:00", "06:00", now=_ts(2026, 9, 13, 6, 0)) is False
    assert in_high_recall_window("22:00", "06:00", now=_ts(2026, 9, 12, 12, 0)) is False
    # 同日时段
    assert in_high_recall_window("09:00", "18:00", now=_ts(2026, 9, 12, 12, 0)) is True
    assert in_high_recall_window("09:00", "18:00", now=_ts(2026, 9, 12, 8, 59)) is False
    assert in_high_recall_window("09:00", "18:00", now=_ts(2026, 9, 12, 18, 0)) is False
    # 非法时间不生效
    assert in_high_recall_window("25:00", "06:00", now=_ts(2026, 9, 12, 23, 0)) is False
    assert in_high_recall_window("", "", now=_ts(2026, 9, 12, 23, 0)) is False
    print("[ok] 高召回时段窗口：跨天 / 边界 / 同日时段 / 非法时间")


def case_next_flip_and_validate():
    assert high_recall_next_flip("22:00", "06:00", now=_ts(2026, 9, 12, 10, 0)) == _ts(2026, 9, 12, 22, 0)
    # 已开启时段内：下一个节点是次日 06:00 关闭
    assert high_recall_next_flip("22:00", "06:00", now=_ts(2026, 9, 12, 23, 0)) == _ts(2026, 9, 13, 6, 0)
    assert high_recall_next_flip("09:00", "18:00", now=_ts(2026, 9, 12, 12, 0)) == _ts(2026, 9, 12, 18, 0)
    assert high_recall_next_flip("bad", "06:00", now=_ts(2026, 9, 12, 10, 0)) == 0.0

    assert validate_high_recall_times("22:00", "06:00") is None
    assert validate_high_recall_times("09:00", "18:00") is None
    assert validate_high_recall_times("", "06:00") is not None
    assert validate_high_recall_times("22:00", "") is not None
    assert validate_high_recall_times("25:00", "06:00") is not None
    assert validate_high_recall_times("22:00", "22:00") is not None
    print("[ok] 高召回下一个切换节点与时间校验")


# ----------------------------------------------------------------------
# MessageGuard：高召回规则与模型的切换
# ----------------------------------------------------------------------
def _hr_gconf(**over):
    conf = {
        "guard_enable": True,
        "guard_action": "ban",
        "guard_ban_seconds": "60",
        "guard_interval": 0,
        "guard_notice": "",
        "guard_prompt": "常规要求：仅明显违规才处置",
        "llm_chat": "guard-main",
        "llm_chat_fallback": "",
        "llm_ocr_chat": "",
        "high_recall_active": False,
        "high_recall_prompt": "高召回要求：疑罪从有，任何疑似违规一律判定违规",
        "high_recall_llm_chat": "hr-main",
        "high_recall_llm_chat_fallback": "hr-fb",
        "high_recall_llm_ocr_chat": "",
    }
    conf.update(over)
    return conf


async def case_guard_high_recall_settings():
    gconf = _hr_gconf(high_recall_active=True)
    ctx = FakeContext(['{"allowed": false, "reason": "疑似广告变体"}'])
    guard = MessageGuard(gconf, LLMReviewer({}, ctx), gconf_provider=lambda gid: gconf)
    event = _Event("今晚活动了解一下")
    assert await guard._handle(event) is True, "高召回生效时应按高召回规则判定违规"
    call = ctx.calls[0]
    assert call["chat_id"] == "hr-main", call
    assert "高召回要求" in call["prompt"] and "常规要求" not in call["prompt"], call
    print("[ok] 高召回生效：使用独立审核要求与独立模型，违规正常处置")

    # 高召回未单独填写规则 / 模型：沿用常规审核设置
    gconf2 = _hr_gconf(high_recall_active=True, high_recall_prompt="", high_recall_llm_chat="",
                       high_recall_llm_chat_fallback="")
    ctx2 = FakeContext(['{"allowed": true, "reason": ""}'])
    guard2 = MessageGuard(gconf2, LLMReviewer({}, ctx2), gconf_provider=lambda gid: gconf2)
    assert await guard2._handle(_Event("普通消息")) is False
    call = ctx2.calls[0]
    assert call["chat_id"] == "guard-main", call
    assert "常规要求" in call["prompt"] and "高召回要求" not in call["prompt"], call
    print("[ok] 高召回未单独填写规则 / 模型：沿用常规审核设置")

    # 未生效：即使填了高召回规则 / 模型也走常规设置
    gconf3 = _hr_gconf(high_recall_active=False)
    ctx3 = FakeContext(['{"allowed": true, "reason": ""}'])
    guard3 = MessageGuard(gconf3, LLMReviewer({}, ctx3), gconf_provider=lambda gid: gconf3)
    assert await guard3._handle(_Event("普通消息")) is False
    call = ctx3.calls[0]
    assert call["chat_id"] == "guard-main" and "常规要求" in call["prompt"], call
    print("[ok] 高召回未生效：仍走常规审核设置")


# ----------------------------------------------------------------------
# 插件层：定时切换、开关提示与手动临时状态
# ----------------------------------------------------------------------
def _load_plugin_module(tmp):
    """导入插件 main 模块并把数据目录指向临时目录，避免污染仓库。"""
    import importlib

    star_tools = sys.modules["astrbot.core.star.star_tools"].StarTools
    star_tools.get_data_dir = staticmethod(lambda: tmp)
    pkg_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parent = os.path.dirname(pkg_dir)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    return importlib.import_module(f"{os.path.basename(pkg_dir)}.main")


async def case_plugin_schedule_and_notice():
    tmp = tempfile.mkdtemp(prefix="guard_hr_selftest_")
    main_mod = _load_plugin_module(tmp)
    Plugin = main_mod.LLMGroupGuardPlugin

    plugin = Plugin.__new__(Plugin)  # 跳过 AstrBot 运行时初始化，手工装配依赖
    from astrbot.api import AstrBotConfig

    plugin.data_dir = tmp
    plugin.config = AstrBotConfig()
    plugin.config.setdefault("global", {})
    plugin.config.setdefault("groups", {})
    plugin._config_path = os.path.join(tmp, "config.json")
    plugin._group_template = {}
    plugin._group_runtime = {}
    plugin._join_oid = {}
    plugin._hr_pending = {}
    plugin._platform_bot = None
    plugin._apply_saved_config()

    gconf = plugin._gconf("10001")
    assert gconf["high_recall_enable"] is False, gconf
    assert "开启" in gconf["high_recall_on_msg"] and "关闭" in gconf["high_recall_off_msg"], gconf
    assert main_mod._GROUP_CONFIG_KEYS >= set(gconf), "所有群配置键都应可被 WebUI 保存"
    print("[ok] 高召回新配置键：默认值 / 可保存")

    sent = []

    class _Bot:
        async def send_group_msg(self, group_id=None, message=""):
            sent.append((group_id, message))

    plugin._platform_bot = _Bot()

    # 当前处于时段内（相对现在 ±1 小时）→ 调度自动开启并发送提示
    gconf.update({
        "high_recall_enable": True,
        "high_recall_start": _hhmm(-3600),
        "high_recall_end": _hhmm(3600),
        "high_recall_active": False,
        "high_recall_manual_until": 0,
    })
    await plugin._check_high_recall()
    assert gconf["high_recall_active"] is True, gconf
    assert sent and sent[-1][0] == 10001 and "开启" in sent[-1][1], sent
    print("[ok] 到点自动开启高召回并发送开启提示")

    # 切到时段外（未来 2~3 小时）→ 调度自动关闭并发送提示
    gconf["high_recall_start"] = _hhmm(7200)
    gconf["high_recall_end"] = _hhmm(10800)
    await plugin._check_high_recall()
    assert gconf["high_recall_active"] is False, gconf
    assert "关闭" in sent[-1][1], sent
    print("[ok] 时段结束自动关闭高召回并发送关闭提示")

    # 缺少连接：提示进入待补发队列，连接恢复后自动补发
    plugin._platform_bot = None
    sent.clear()
    await plugin._set_high_recall("10001", True)
    assert plugin._hr_pending.get("10001") is True, plugin._hr_pending
    plugin._platform_bot = _Bot()
    await plugin._flush_hr_pending()
    assert not plugin._hr_pending and sent and "开启" in sent[-1][1], (plugin._hr_pending, sent)
    print("[ok] 无连接时提示排队，连接恢复后补发")

    # 手动切换：时段外开启不被调度改回，到下一个定时节点交还定时规则
    await plugin._set_high_recall("10001", False)
    sent.clear()
    await plugin._set_high_recall("10001", True, manual=True)
    assert gconf["high_recall_active"] is True and float(gconf["high_recall_manual_until"]) > time.time(), gconf
    await plugin._check_high_recall()
    assert gconf["high_recall_active"] is True, "手动临时状态未到期，调度不应改回"
    gconf["high_recall_manual_until"] = time.time() - 1  # 模拟到达下一个定时节点
    await plugin._check_high_recall()
    assert gconf["high_recall_active"] is False and float(gconf["high_recall_manual_until"]) == 0, gconf
    assert "关闭" in sent[-1][1], sent
    print("[ok] 手动临时状态到期后由定时规则接管")

    # WebUI 保存：开关打开时校验时间；开启 / 关闭时状态立即对齐
    main_mod.json_response = lambda obj=None: ("ok", obj)
    main_mod.error_response = lambda msg=None: ("err", msg)

    class _Req:
        def __init__(self, payload):
            self._payload = payload
            self.query = {}

        async def json(self, default=None):
            return self._payload or default

    async def _save(payload):
        main_mod.request = _Req(payload)
        return await plugin.web_save_config()

    gconf["high_recall_enable"] = False
    gconf["high_recall_active"] = False
    res = await _save({"group_id": "10001", "group": {
        "high_recall_enable": True, "high_recall_start": "24:00", "high_recall_end": "06:00"}})
    assert res[0] == "err", res
    res = await _save({"group_id": "10001", "group": {
        "high_recall_enable": True, "high_recall_start": _hhmm(-3600), "high_recall_end": _hhmm(3600)}})
    assert res[0] == "ok", res
    assert gconf["high_recall_active"] is True, "时段内启用定时应立即生效"
    res = await _save({"group_id": "10001", "group": {"high_recall_enable": False}})
    assert res[0] == "ok", res
    assert gconf["high_recall_enable"] is False and gconf["high_recall_active"] is False, gconf
    assert "关闭" in sent[-1][1], sent
    print("[ok] WebUI 保存：时间校验 + 启用/关闭时状态立即对齐")

    # 配置落盘与重载
    plugin.config["groups"].clear()
    plugin._apply_saved_config()
    saved = plugin._gconf("10001")
    assert saved["high_recall_enable"] is False and saved["high_recall_active"] is False, saved
    assert saved["high_recall_on_msg"], saved
    print("[ok] 高召回配置保存 / 重载")


async def main():
    case_window_math()
    case_next_flip_and_validate()
    await case_guard_high_recall_settings()
    await case_plugin_schedule_and_notice()
    print("全部高召回模式自测通过")


if __name__ == "__main__":
    asyncio.run(main())
