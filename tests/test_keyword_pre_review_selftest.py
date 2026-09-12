"""离线自测：命中关键词必须拦截 AI 回复（轻/重两级行为一致）。

运行：python tests/test_keyword_pre_review_selftest.py

回归的缺陷：后台审核任务与"回复前预审"共享 `_handled` 去重，后台先跑完会把消息
标记为已处理，预审随后因去重直接返回 False → stop_event 未被调用 → 机器人照常
回复命中关键词的消息。轻度词默认处置为"仅撤回"，没有禁言兜底，现象最明显。

本自测校验：
- 预审命中轻度/重度词均返回 True（不受后台任务抢先标记影响）
- 预审只拦截、不撤回/不禁言/不计数，处置由后台任务完成，避免重复处置
- 后台任务在预审被跳过时仍能完成处置（撤回 + 计数 + 日志）
- 未配置关键词或消息合规时预审不拦截，后台 LLM 审核不受影响
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_join_review_selftest import _install_astrbot_stubs  # noqa: E402

_install_astrbot_stubs()

sys.path.insert(0, ".")

from core.message_guard import MessageGuard  # noqa: E402


class _Api:
    """记录 call_action（delete_msg / set_group_ban）。"""

    def __init__(self, log):
        self.log = log

    async def call_action(self, action, **kw):
        self.log.append((action, kw))


class _Bot:
    def __init__(self, log):
        self.api = _Api(log)
        self.sent = []

    async def send_group_msg(self, group_id=None, message=""):
        self.sent.append((group_id, message))


class _MsgObj:
    message_id = 987654

    def __init__(self, role="member"):
        self.raw_message = {"sender": {"nickname": "小明", "card": "小明", "role": role}}


class _Event:
    def __init__(self, text, role="member", sender="20002"):
        self.message_str = text
        self.message_obj = _MsgObj(role)
        self._sender = sender
        self._group = "10001"
        self.calls = []
        self.bot = _Bot(self.calls)
        self.stopped = False

    def get_group_id(self):
        return self._group

    def get_sender_id(self):
        return self._sender

    def is_admin(self):
        return False

    def get_sender_name(self):
        return "小明"

    def stop_event(self):
        self.stopped = True


def _gconf(**over):
    conf = {
        "keyword_guard_enable": True,
        "keyword_minor_list": ["广告"],
        "keyword_major_list": ["涉政"],
        "keyword_minor_action": "recall",
        "keyword_major_action": "ban",
        "guard_enable": False,  # 本自测只关心关键词链路
        "guard_notice": "",
    }
    conf.update(over)
    return conf


def _guard(gconf):
    return MessageGuard(gconf, reviewer=None, gconf_provider=lambda gid: gconf)


async def case_pre_review_blocks_after_background_ran():
    """核心回归：后台任务已处理并标记后，预审仍必须拦截回复。"""
    gconf = _gconf()
    guard = _guard(gconf)
    event = _Event("今天发个广告，快来看看")

    # 模拟后台审核任务先跑完（命中轻度词 → 撤回 + 标记已处理）
    assert await guard._handle(event) is True
    assert [c[0] for c in event.calls] == ["delete_msg"], event.calls
    assert guard._is_handled(guard._msg_key(event)), "后台任务应标记该消息已处理"

    # 预审随后执行：修复前这里返回 False（去重跳过）→ 机器人照常回复
    calls_before = list(event.calls)
    assert await guard.pre_review(event) is True, "命中轻度词时预审必须返回 True 以拦截回复"
    assert event.calls == calls_before, f"预审不应产生撤回/禁言动作: {event.calls}"

    # 预审只拦截，不重复计数
    counts = guard.keyword_minor_tracker.counts if guard.keyword_minor_tracker else {}
    assert counts == {}, f"未传 data_dir 时不应有内存计数残留: {counts}"
    print("[ok] 后台任务抢先处理后，轻度词预审仍拦截回复（且不重复处置）")


async def case_major_keyword_pre_review():
    """重度词同样拦截；预审不提前禁言（避免与后台阶梯计数冲突）。"""
    gconf = _gconf()
    guard = _guard(gconf)
    event = _Event("涉政话题不要聊")

    assert await guard.pre_review(event) is True, "命中重度词时预审必须返回 True"
    assert event.calls == [], f"预审不应提前禁言: {event.calls}"

    # 后台任务随后完成完整处置（禁言）
    assert await guard._handle(event) is True
    assert [c[0] for c in event.calls] == ["set_group_ban"], event.calls
    print("[ok] 重度词预审拦截回复，禁言由后台任务按阶梯执行一次")


async def case_no_false_positive():
    """未配置关键词、开关关闭、无命中的消息预审都不拦截。"""
    event = _Event("今天天气不错")
    for gconf in (
        _gconf(keyword_minor_list=[], keyword_major_list=[]),
        _gconf(keyword_guard_enable=False),
        _gconf(),
    ):
        guard = _guard(gconf)
        assert await guard.pre_review(event) is False, gconf
    # 被去重标记过的合规消息不应影响预审判断本身
    guard = _guard(_gconf())
    guard._mark_handled(guard._msg_key(event))
    assert await guard.pre_review(event) is False
    print("[ok] 无关键词命中/开关关闭时不拦截回复")


async def case_admin_and_command_skip():
    """管理员/群管/白名单与指令消息预审不拦截。"""
    guard = _guard(_gconf())
    assert await guard.pre_review(_Event("/全体禁言")) is False, "指令消息不审核"
    assert await guard.pre_review(_Event("广告", role="admin")) is False, "群管理员豁免"
    guard_w = _guard(_gconf(user_whitelist=["20002"]))
    assert await guard_w.pre_review(_Event("广告")) is False, "白名单成员豁免"
    print("[ok] 指令消息、群管与白名单成员照旧豁免")


async def main():
    await case_pre_review_blocks_after_background_ran()
    await case_major_keyword_pre_review()
    await case_no_false_positive()
    await case_admin_and_command_skip()
    print("全部关键词预审拦截自测通过")


if __name__ == "__main__":
    asyncio.run(main())
