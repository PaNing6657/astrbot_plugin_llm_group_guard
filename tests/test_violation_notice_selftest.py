"""离线自测：发言违规通知（guard_notice）的 @ 逻辑与入群提示一致。

运行：python tests/test_violation_notice_selftest.py
只 stub 掉 astrbot 依赖，校验：
- 违规通知支持 {at_user} → CQ 码 @（显示群昵称，而非纯文本 QQ 号）
- 与入群审批/进群欢迎/改名片提示共用同一套 @ 逻辑（输出完全一致）
"""

import asyncio
import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_join_review_selftest import _install_astrbot_stubs  # noqa: E402

_install_astrbot_stubs()

sys.path.insert(0, ".")

from core.message_guard import MessageGuard  # noqa: E402
from core.text_utils import build_text_with_at  # noqa: E402


class _Api:
    async def call_action(self, action, **kw):
        pass


class _Bot:
    api = _Api()

    def __init__(self):
        self.sent = []

    async def send_group_msg(self, group_id=None, message=""):
        self.sent.append((group_id, message))


class _MsgObj:
    message_id = None
    raw_message = {"sender": {"nickname": "小明", "card": "小明_12345678", "role": "member"}}


class _Event:
    message_obj = _MsgObj()

    def __init__(self):
        self.bot = _Bot()

    def get_sender_name(self):
        return "小明"


async def case_notice_at():
    guard = MessageGuard({"guard_notice": ""}, reviewer=None)  # data_dir=None：不落盘
    event = _Event()
    gconf = {
        "guard_action": "recall",
        "guard_recall_ban_threshold": 0,
        "guard_notice": "{at_user} 发言违规（第 {count} 次，禁言 {duration} 秒，昵称 {nickname}）",
    }
    await guard._apply_action(
        event, "10001", "20002", None, "违规原文",
        reason="广告", source="llm", gconf=gconf,
    )
    assert len(event.bot.sent) == 1, event.bot.sent
    gid, message = event.bot.sent[0]
    assert gid == 10001, (gid, message)
    assert message.startswith("[CQ:at,qq=20002] 发言违规"), message
    assert "第 1 次" in message and "禁言 0 秒" in message, message
    assert "昵称 小明" in message, message
    assert "@20002" not in message, "不应把 QQ 号当纯文本 @ 出去"
    print("[ok] 违规通知 {at_user} 编译为 CQ 码 @，并支持 {nickname} {count} {duration}")


async def case_notice_empty_and_user_only():
    """未配置通知不发送；仅用 {user_id} 时保持旧行为（纯文本）。"""
    guard = MessageGuard({}, reviewer=None)
    event = _Event()
    await guard._apply_action(
        event, "10001", "20002", None, "违规原文",
        gconf={"guard_action": "ban", "guard_ban_seconds": "600", "guard_notice": ""},
    )
    assert event.bot.sent == [], event.bot.sent

    await guard._apply_action(
        event, "10001", "20002", None, "违规原文",
        gconf={"guard_action": "ban", "guard_ban_seconds": "600", "guard_notice": "用户 {user_id} 已禁言"},
    )
    assert event.bot.sent == [(10001, "用户 20002 已禁言")], event.bot.sent
    print("[ok] 通知留空不发送；{user_id} 旧占位符行为不变")


async def case_legacy_at_alias():
    """旧配置手写的 "@{user_id}" 自动升级为真正的 @（不再是纯文本 QQ 号）。"""
    guard = MessageGuard({}, reviewer=None)
    event = _Event()
    for template in ("@{user_id} 请勿违规发言", "@ {user_id} 请勿违规发言", "@{at_user} 请勿违规发言"):
        await guard._apply_action(
            event, "10001", "20002", None, "违规原文",
            gconf={"guard_action": "recall", "guard_notice": template},
        )
    assert event.bot.sent == [
        (10001, "[CQ:at,qq=20002] 请勿违规发言"),
        (10001, "[CQ:at,qq=20002] 请勿违规发言"),
        (10001, "[CQ:at,qq=20002] 请勿违规发言"),
    ], event.bot.sent
    print("[ok] 旧写法 @{user_id} 自动升级为 CQ 码 @（保留 {user_id} 纯文本语义）")


def case_same_as_join_flow():
    """与插件内入群提示使用的 _build_text_with_at 输出完全一致。"""
    pkg_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parent = os.path.dirname(pkg_dir)
    pkg_name = os.path.basename(pkg_dir)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    main_mod = importlib.import_module(f"{pkg_name}.main")
    Plugin = main_mod.LLMGroupGuardPlugin

    template = "已拒绝 {at_user}：{reason}"
    vars_map = {"{reason}": "缺少 OID"}
    join_text = Plugin._build_text_with_at(template, vars_map, "12345")
    shared_text = build_text_with_at(template, vars_map, "12345")
    assert join_text == shared_text == "已拒绝 [CQ:at,qq=12345]：缺少 OID", (join_text, shared_text)
    assert Plugin._build_text_with_at("{at_user}{at_user}", {}, "1") == "[CQ:at,qq=1][CQ:at,qq=1]"
    print("[ok] 违规通知与入群提示共用同一套 @ 逻辑（CQ 码输出一致）")


async def main():
    await case_notice_at()
    await case_notice_empty_and_user_only()
    await case_legacy_at_alias()
    case_same_as_join_flow()
    print("全部违规通知 @ 逻辑自测通过")


if __name__ == "__main__":
    asyncio.run(main())
