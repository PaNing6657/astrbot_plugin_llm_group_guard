"""离线自测：入群审批审核逻辑（独立模型 / 自定义要求 / 自动拒绝说明）。

运行：python tests/test_join_review_selftest.py
只 stub 掉 astrbot 依赖，直接校验 core/llm_reviewer.py 的判定与提示词拼装，
以及 main.py 的配置默认值、模型回退与 on_group_add_request 的同意/拒绝分支。
"""

import asyncio
import sys
import types


class _FakeLogger:
    def info(self, *a, **k):
        pass

    error = warning = debug = info


def _install_astrbot_stubs():
    """构造最小 astrbot 依赖桩，使 core/llm_reviewer.py 与 main.py 可离线导入。"""

    def _mod(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m
        return m

    class _Filter:
        class PlatformAdapterType:
            AIOCQHTTP = "aiocqhttp"

        class EventMessageType:
            ALL = "all"
            GROUP_MESSAGE = "group"

        @staticmethod
        def platform_adapter_type(*a, **k):
            return lambda fn: fn

        @staticmethod
        def event_message_type(*a, **k):
            return lambda fn: fn

        @staticmethod
        def command(*a, **k):
            return lambda fn: fn

        @staticmethod
        def llm_tool(*a, **k):
            return lambda fn: fn

    class _Config(dict):
        pass

    class _Star:
        def __init__(self, context=None):
            self.context = context

    _mod("astrbot")
    _mod("astrbot.api", logger=_FakeLogger(), AstrBotConfig=_Config)
    _mod("astrbot.api.event", AstrMessageEvent=object, filter=_Filter)
    _mod("astrbot.api.star", Context=object, Star=_Star, register=lambda *a, **k: (lambda cls: cls))
    _mod("astrbot.api.web", error_response=lambda *a, **k: None, json_response=lambda *a, **k: None, request=None)
    _mod("astrbot.core")
    _mod("astrbot.core.platform")
    _mod("astrbot.core.platform.sources")
    _mod("astrbot.core.platform.sources.aiocqhttp")
    _mod("astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event", AiocqhttpMessageEvent=object)
    _mod("astrbot.core.star")
    _mod("astrbot.core.star.star_tools", StarTools=type("StarTools", (), {"get_data_dir": staticmethod(lambda: ".")}))


_install_astrbot_stubs()

sys.path.insert(0, ".")

from core.llm_reviewer import LLMReviewer  # noqa: E402


class _Resp:
    def __init__(self, text):
        self.completion_text = text


class FakeContext:
    """记录每次调用的 chat_provider_id 与 prompt，按队列返回预设输出。"""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    async def llm_generate(self, chat_provider_id=None, prompt="", contexts=None, **kw):
        self.calls.append({"chat_id": chat_provider_id, "prompt": prompt})
        out = self.outputs.pop(0) if self.outputs else ""
        return _Resp(out)


PASS_JSON = '{"allowed": true, "has_nickname": true, "has_oid": true, "nickname": "小明", "oid": "12345678", "reason": "", "comment": "完整"}'
FAIL_JSON = '{"allowed": false, "has_nickname": true, "has_oid": false, "nickname": "小明", "oid": "", "reason": "缺少 OID", "comment": "缺 OID"}'
LEGACY_JSON = '{"has_nickname": true, "has_oid": true, "nickname": "小红", "oid": "87654321", "comment": "旧格式"}'
BAD_OID_JSON = '{"allowed": true, "has_nickname": true, "has_oid": true, "nickname": "小刚", "oid": "abc", "comment": "oid 非数字"}'


async def case_pass_with_custom_prompt():
    ctx = FakeContext([PASS_JSON])
    r = LLMReviewer({}, ctx)
    res = await r.judge_join_request(
        "小明 12345678",
        prompt="必须包含真实姓名与学号",
        chat_id="join-model",
        fallback_chat_id="fallback-model",
        ocr_chat_id="vision-model",
    )
    assert res["allowed"] is True, res
    assert res["oid"] == "12345678", res
    call = ctx.calls[0]
    assert call["chat_id"] == "join-model", call
    assert "必须包含真实姓名与学号" in call["prompt"], call
    assert "昵称】和【OID" not in call["prompt"], "自定义要求应完全替换内置要求"
    assert '"allowed"' in call["prompt"], "仍应保留 JSON 输出约束"
    print("[ok] 自定义要求替换内置要求 + 独立模型 + 通过判定")


async def case_fail_reason():
    ctx = FakeContext([FAIL_JSON])
    r = LLMReviewer({}, ctx)
    res = await r.judge_join_request("小明", chat_id="m")
    assert res["allowed"] is False and res["reason"] == "缺少 OID", res
    assert "昵称】和【OID" in ctx.calls[0]["prompt"], "留空应使用内置默认要求"
    print("[ok] 不通过时返回 reason（供拒绝说明）")


async def case_legacy_output_without_allowed():
    ctx = FakeContext([LEGACY_JSON])
    r = LLMReviewer({}, ctx)
    res = await r.judge_join_request("小红 87654321", chat_id="m")
    assert res["allowed"] is True, res
    print("[ok] 兼容无 allowed 字段的模型输出（按昵称+OID 回退判定）")


async def case_allowed_but_oid_invalid():
    """内置默认要求下：模型称通过但 OID 非数字 → 仍按不通过处理（避免误放行）。"""
    ctx = FakeContext([BAD_OID_JSON])
    r = LLMReviewer({}, ctx)
    res = await r.judge_join_request("小刚 abc", chat_id="m")
    assert res["allowed"] is False, res
    assert "OID" in res["reason"], res
    print("[ok] 内置要求下 OID 非数字：按不通过处理并给出原因")


async def case_custom_prompt_authoritative():
    """完全自定义要求下：以模型 allowed 为准，不再硬性要求昵称+OID。"""
    outputs = [
        '{"allowed": true, "has_nickname": false, "has_oid": false, "nickname": "", "oid": "", "reason": "", "comment": "学号已填"}',
        '{"allowed": false, "has_nickname": false, "has_oid": false, "nickname": "", "oid": "", "reason": "缺少学号", "comment": "缺学号"}',
    ]
    ctx = FakeContext(list(outputs))
    r = LLMReviewer({}, ctx)
    ok = await r.judge_join_request("我是隔壁班同学", prompt="必须写明学号", chat_id="m")
    assert ok["allowed"] is True, ok  # 自定义要求未提及 OID，不应因缺 OID 被拒
    bad = await r.judge_join_request("求进群", prompt="必须写明学号", chat_id="m")
    assert bad["allowed"] is False and bad["reason"] == "缺少学号", bad
    print("[ok] 自定义审核要求下判定完全跟随模型 allowed（含自定义不通过原因）")


async def case_config_and_reject_flow():
    """配置层默认值 + on_group_add_request 自动拒绝（含自定义拒绝说明）端到端。"""
    import importlib
    import importlib.util
    import os
    import tempfile

    pkg_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 插件根目录
    parent = os.path.dirname(pkg_dir)
    pkg_name = os.path.basename(pkg_dir)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    # 把数据目录指向临时目录，避免污染仓库根
    tmp = tempfile.mkdtemp(prefix="guard_selftest_")
    star_tools = sys.modules["astrbot.core.star.star_tools"].StarTools
    star_tools.get_data_dir = staticmethod(lambda: tmp)
    main_mod = importlib.import_module(f"{pkg_name}.main")
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
    plugin._apply_saved_config()

    gconf = plugin._gconf("10001")
    # 新群默认值：自动拒绝开启、拒绝回执有默认模板、审核 LLM 三项留空（沿用消息审核模型）
    assert gconf["join_auto_reject_enable"] is True, gconf
    assert gconf["join_reject_reply"] == "很抱歉，{reason}", gconf
    assert gconf["join_llm_chat"] == "" and gconf["join_prompt"] == "", gconf
    assert main_mod._GROUP_CONFIG_KEYS >= set(gconf), "所有群配置键都应可被 WebUI 保存"

    # 自定义配置写入后应能落盘并读回
    gconf.update({
        "join_verify_enable": True,
        "join_llm_chat": "join-main",
        "join_prompt": "必须是本班同学，备注写清姓名与学号",
        "join_auto_reject_enable": True,
        "join_reject_reply": "抱歉，{reason}；补充后请重新申请",
        "join_reject_notice": "已拒绝 {at_user}：{reason}",
    })
    plugin._save_config()
    plugin.config["groups"].clear()
    plugin._apply_saved_config()
    saved = plugin._gconf("10001")
    assert saved["join_llm_chat"] == "join-main" and "本班同学" in saved["join_prompt"], saved
    print("[ok] 入群审批新配置键：默认值 / 保存 / 重载")

    # 构造入群申请事件：LLM 判不通过 → 自动拒绝并带自定义说明
    calls = []

    class _Api:
        async def call_action(self, action, **kw):
            calls.append((action, kw))
            return {"status": "ok"}

    class _Bot:
        api = _Api()

        async def send_group_msg(self, group_id=None, message=""):
            calls.append(("send_group_msg", {"group_id": group_id, "message": message}))

    class _MsgObj:
        raw_message = {
            "post_type": "request",
            "request_type": "group",
            "sub_type": "add",
            "group_id": 10001,
            "user_id": 20002,
            "flag": "flag-abc",
            "comment": "求进群",
        }

    class _Event:
        message_obj = _MsgObj()
        bot = _Bot()

    ctx = FakeContext([FAIL_JSON])
    plugin.reviewer = LLMReviewer({}, ctx)
    await plugin.on_group_add_request(_Event())

    assert ctx.calls[0]["chat_id"] == "join-main", ctx.calls  # 使用入群审批独立模型
    assert "本班同学" in ctx.calls[0]["prompt"], ctx.calls  # 使用自定义审核要求
    action, kw = calls[0]
    assert action == "set_group_add_request" and kw["approve"] is False, calls
    assert kw["reason"] == "抱歉，缺少 OID；补充后请重新申请", kw
    notice = [c for c in calls if c[0] == "send_group_msg"]
    assert notice and notice[0][1]["message"] == "已拒绝 [CQ:at,qq=20002]：缺少 OID", calls
    print("[ok] 不满足要求自动拒绝 + 自定义拒绝说明（回执与群内提示）")

    # 关闭自动拒绝：不调用审批接口
    plugin._gconf("10001")["join_auto_reject_enable"] = False
    calls.clear()
    ctx2 = FakeContext([FAIL_JSON])
    plugin.reviewer = LLMReviewer({}, ctx2)
    await plugin.on_group_add_request(_Event())
    assert calls == [], f"关闭自动拒绝后不应调用任何接口: {calls}"
    print("[ok] 关闭自动拒绝时不拒绝也不同意（留给管理员处理）")

    # 审核通过：同意入群 + 记录 OID（改名片为异步任务，此处不等待）
    plugin._gconf("10001")["join_auto_reject_enable"] = True
    calls.clear()
    ctx3 = FakeContext([PASS_JSON])
    plugin.reviewer = LLMReviewer({}, ctx3)
    try:
        await plugin.on_group_add_request(_Event())
    finally:
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                task.cancel()
    action, kw = calls[0]
    assert action == "set_group_add_request" and kw["approve"] is True, calls
    assert plugin._join_oid["10001"]["20002"] == "12345678", plugin._join_oid
    print("[ok] 满足要求时同意入群并缓存 OID（供欢迎词 / 改名片使用）")

    gconf = {
        "join_llm_chat": "",
        "llm_chat": "guard-main",
        "join_llm_chat_fallback": "join-fb",
        "llm_chat_fallback": "guard-fb",
        "llm_ocr_chat": "guard-ocr",
        "join_llm_ocr_chat": "",
    }
    assert Plugin._join_model(gconf, "join_llm_chat", "llm_chat") == "guard-main"
    assert Plugin._join_model(gconf, "join_llm_chat_fallback", "llm_chat_fallback") == "join-fb"
    assert Plugin._join_model(gconf, "join_llm_ocr_chat", "llm_ocr_chat") == "guard-ocr"
    gconf["join_llm_chat"] = "join-main"
    assert Plugin._join_model(gconf, "join_llm_chat", "llm_chat") == "join-main"
    text = Plugin._build_text_with_at(
        "很抱歉，{reason}（申请人 {nickname}/{user_id}）",
        {"{reason}": "缺少 OID", "{nickname}": "小明", "{user_id}": "12345"},
        "12345",
    )
    assert text == "很抱歉，缺少 OID（申请人 小明/12345）", text
    at_text = Plugin._build_text_with_at("已拒绝 {at_user}：{reason}", {"{reason}": "缺少 OID"}, "12345")
    assert at_text == "已拒绝 [CQ:at,qq=12345]：缺少 OID", at_text
    print("[ok] 入群审批独立模型回退 + 拒绝说明占位符替换")


async def main():
    await case_pass_with_custom_prompt()
    await case_fail_reason()
    await case_legacy_output_without_allowed()
    await case_allowed_but_oid_invalid()
    await case_custom_prompt_authoritative()
    await case_config_and_reject_flow()
    print("全部入群审批自测通过")


if __name__ == "__main__":
    asyncio.run(main())
