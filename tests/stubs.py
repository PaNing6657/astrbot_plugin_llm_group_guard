# tests/stubs.py
"""测试用的 astrbot 依赖桩：让 core 模块可以脱离 AstrBot 运行时被导入与验证。

仅提供 core/* 在导入与被调用时真正用到的少量接口，不模拟 AstrBot 的完整行为。
"""

from __future__ import annotations

import sys
import types


class _FakeLogger:
    """静默 logger，可切换为收集模式以便断言日志内容。"""

    def __init__(self, sink: list | None = None):
        self.sink = sink if sink is not None else []

    def _record(self, level: str, message):
        self.sink.append((level, str(message)))

    def debug(self, message, *args, **kwargs):
        self._record("debug", message)

    def info(self, message, *args, **kwargs):
        self._record("info", message)

    def warning(self, message, *args, **kwargs):
        self._record("warning", message)

    def error(self, message, *args, **kwargs):
        self._record("error", message)

    def critical(self, message, *args, **kwargs):
        self._record("critical", message)


class FakeMessageObj:
    def __init__(self, message_id="1", raw_message=None):
        self.message_id = message_id
        self.raw_message = raw_message if raw_message is not None else {}


class FakeApi:
    def __init__(self, calls: list):
        self.calls = calls

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        return {"status": "ok"}


class FakeBot:
    def __init__(self):
        self.calls: list = []
        self.sent: list = []
        self.api = FakeApi(self.calls)

    async def send_group_msg(self, group_id=None, message=None):
        self.sent.append((group_id, message))
        return {"message_id": len(self.sent)}


class FakeEvent:
    """模拟 AiocqhttpMessageEvent 中 core 用到的那部分接口。"""

    def __init__(
        self,
        group_id="100",
        user_id="200",
        text="",
        message_id="1",
        images=(),
        role="member",
        admin=False,
        bot=None,
    ):
        self.message_str = text
        segs = [{"type": "image", "data": {"url": url}} for url in images]
        self.message_obj = FakeMessageObj(
            message_id=message_id,
            raw_message={"message": segs, "sender": {"role": role, "nickname": "测试昵称"}},
        )
        self.bot = bot if bot is not None else FakeBot()
        self._group_id = group_id
        self._user_id = user_id
        self._admin = admin

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._user_id

    def get_sender_name(self):
        return "测试昵称"

    def is_admin(self):
        return self._admin


class FakeReviewer:
    """可编排的审核器桩：按调用顺序返回预设判定。"""

    def __init__(self, verdicts=None, default=None):
        self.verdicts = list(verdicts or [])
        self.default = default if default is not None else {"allowed": True, "reason": ""}
        self.calls: list = []
        self.last_error = ""
        self.last_error_type = ""
        self._enabled = True

    def enabled(self):
        return self._enabled

    async def judge_message(self, sender, text, **kwargs):
        self.calls.append({"sender": sender, "texts": [text], **kwargs})
        return self._next()

    async def judge_messages(self, sender, texts, **kwargs):
        self.calls.append({"sender": sender, "texts": list(texts), **kwargs})
        return self._next()

    def _next(self):
        if self.verdicts:
            return self.verdicts.pop(0)
        return self.default


class _FakeImageURL:
    """ImageURLPart.ImageURL 的占位。"""

    def __init__(self, url=""):
        self.url = url


class _FakeImageURLPart:
    """ImageURLPart 的占位：记录图片 URL。"""

    ImageURL = _FakeImageURL

    def __init__(self, image_url=None):
        self.image_url = image_url


class _FakeUserMessageSegment:
    """UserMessageSegment 的占位：记录 content 部分。"""

    def __init__(self, content=None):
        self.content = content


class FakeContext:
    """AstrBot Context 桩：记录 llm_generate 的入参，模拟 provider 能力查询。"""

    def __init__(self, response='{"allowed": true, "reason": ""}', vision_models=(), raise_exc=None):
        self.response = response
        self.vision_models = set(vision_models)  # 声明支持识图的模型 ID
        self.raise_exc = raise_exc
        self.calls: list = []
        self.provider_manager = self

    def get_provider_config_by_id(self, chat_id, merged=True):
        return {"modalities": ["text", "image"] if chat_id in self.vision_models else ["text"]}

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_exc:
            raise self.raise_exc
        response_text = self.response

        class _Resp:
            completion_text = response_text

        return _Resp()


def install() -> _FakeLogger:
    """把桩模块注册进 sys.modules，返回本次使用的 logger 桩。"""
    logger = _FakeLogger()

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logger
    core = types.ModuleType("astrbot.core")
    core_platform = types.ModuleType("astrbot.core.platform")
    core_sources = types.ModuleType("astrbot.core.platform.sources")
    core_aiocqhttp = types.ModuleType("astrbot.core.platform.sources.aiocqhttp")
    msg_event_mod = types.ModuleType(
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"
    )

    class AiocqhttpMessageEvent:  # noqa: D401 - 仅用于 isinstance 判断
        """占位类型：测试中不依赖其行为。"""

    msg_event_mod.AiocqhttpMessageEvent = AiocqhttpMessageEvent

    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.core": core,
            "astrbot.core.platform": core_platform,
            "astrbot.core.platform.sources": core_sources,
            "astrbot.core.platform.sources.aiocqhttp": core_aiocqhttp,
            "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event": msg_event_mod,
        }
    )
    return logger


def install_multimodal() -> None:
    """补齐多模态消息结构，让 core.llm_reviewer 的 _MULTIMODAL_OK 为 True。

    必须在 import core.llm_reviewer 之前调用（模块导入时即探测该结构是否存在）。
    """
    message_mod = types.ModuleType("astrbot.core.agent.message")
    message_mod.ImageURLPart = _FakeImageURLPart
    message_mod.UserMessageSegment = _FakeUserMessageSegment
    agent_mod = types.ModuleType("astrbot.core.agent")
    agent_mod.message = message_mod
    sys.modules.update(
        {
            "astrbot.core.agent": agent_mod,
            "astrbot.core.agent.message": message_mod,
        }
    )
