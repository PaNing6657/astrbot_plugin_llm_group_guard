# tests/test_message_guard_merge.py
"""消息合并审核的端到端行为验证：攒批 → 整批送审 → 违规整批撤回。"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import stubs  # noqa: E402

stubs.install()

import core.message_guard as message_guard_module  # noqa: E402
from core.message_guard import MessageGuard  # noqa: E402

# 测试中不做真实撤回间隔等待（生产默认 0.3 秒/条）
message_guard_module._RECALL_INTERVAL = 0

WINDOW = "0.25"


def base_config(**overrides):
    conf = {
        "guard_enable": True,
        "guard_action": "recall",
        "guard_ban_seconds": "600",
        "guard_stair_enable": True,
        "guard_stair_multiplier": 2,
        "guard_stair_max_seconds": 86400,
        "guard_recall_ban_threshold": 0,  # 纯撤回，避免测试里触发禁言
        "guard_interval": 30,
        "guard_risk_as_violation": True,
        "guard_prompt": "",
        "guard_notice": "",
        "guard_merge_enable": True,
        "guard_merge_window": WINDOW,
        "guard_merge_max": 50,
        "keyword_guard_enable": False,
        "keyword_minor_list": [],
        "keyword_major_list": [],
        "user_whitelist": [],
        "high_recall_active": False,
        "llm_chat": "test/model",
        "llm_chat_fallback": "",
        "llm_ocr_chat": "",
    }
    conf.update(overrides)
    return conf


class GuardTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.group_conf = base_config()
        self.reviewer = stubs.FakeReviewer()
        self.guard = MessageGuard(
            {}, self.reviewer, data_dir=self._tmp.name, gconf_provider=self._gconf
        )

    def tearDown(self):
        self.guard.close()
        self._tmp.cleanup()

    def _gconf(self, group_id):
        return self.group_conf

    def _dispatch(self, text, message_id, group_id="100", user_id="200", **kwargs):
        """模拟 main.py 收到群消息后的调用。"""
        event = stubs.FakeEvent(
            group_id=group_id, user_id=user_id, text=text, message_id=message_id, **kwargs
        )
        self.guard.schedule(event)
        return event

    @staticmethod
    def _calls(bot, action):
        return [c for c in bot.calls if c[0] == action]

    @staticmethod
    def _recalled(bot):
        return [c[1]["message_id"] for c in bot.calls if c[0] == "delete_msg"]


class CoreMergeBehaviorTest(GuardTestBase):
    """核心行为：攒批、整批送审、违规整批撤回。"""

    async def test_连发消息合并为一次审核并整批撤回(self):
        """用户连发多条 → 只有一次 LLM 调用 → 违规时整批消息全部撤回。"""
        self.reviewer.default = {"allowed": False, "reason": "广告刷屏"}
        bot = stubs.FakeBot()
        texts = ["A内容", "B内容", "C内容", "D内容", "E内容", "F内容"]
        for index, text in enumerate(texts):
            self._dispatch(text, message_id=1000 + index, bot=bot)
            await asyncio.sleep(0.05)
        self.assertEqual(len(self.reviewer.calls), 0, "合窗内不应提前审核")
        await asyncio.sleep(0.6)  # 静默满一个合窗

        self.assertEqual(len(self.reviewer.calls), 1, "整批只应调用一次审核")
        self.assertEqual(self.reviewer.calls[0]["texts"], texts, "审核应按顺序拿到全部消息")
        self.assertEqual(
            self._recalled(bot), [1000 + i for i in range(6)],
            "违规时该区间内全部消息都要撤回",
        )

    async def test_合规批次不撤回(self):
        self.reviewer.default = {"allowed": True, "reason": ""}
        bot = stubs.FakeBot()
        for index in range(3):
            self._dispatch(f"正常消息{index}", message_id=2000 + index, bot=bot)
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.6)
        self.assertEqual(len(self.reviewer.calls), 1)
        self.assertEqual(self._recalled(bot), [], "合规消息不应撤回")
        self.assertEqual(self.guard.violation_tracker.counts, {}, "合规不应计入违规次数")

    async def test_审核失败保守跳过不撤回(self):
        self.reviewer.default = None  # 模型不可用/解析失败
        self.reviewer.last_error = "调用失败"
        bot = stubs.FakeBot()
        self._dispatch("可疑内容", message_id=3000, bot=bot)
        await asyncio.sleep(0.6)
        self.assertEqual(len(self.reviewer.calls), 1)
        self.assertEqual(self._recalled(bot), [], "审核失败应保守跳过")

    async def test_单条消息也会合并审核(self):
        """只发一条：倒计时结束后同样走合并审核路径。"""
        self.reviewer.default = {"allowed": False, "reason": "辱骂"}
        bot = stubs.FakeBot()
        self._dispatch("就一条", message_id=4000, bot=bot)
        await asyncio.sleep(0.6)
        self.assertEqual(len(self.reviewer.calls), 1)
        self.assertEqual(self.reviewer.calls[0]["texts"], ["就一条"])
        self.assertEqual(self._recalled(bot), [4000])

    async def test_图片消息参与合并(self):
        self.reviewer.default = {"allowed": False, "reason": "违规图片"}
        bot = stubs.FakeBot()
        self._dispatch("看图", message_id=5000, bot=bot, images=["https://x/1.png"])
        self._dispatch("", message_id=5001, bot=bot, images=["https://x/2.png"])
        await asyncio.sleep(0.6)
        call = self.reviewer.calls[0]
        self.assertEqual(
            call["texts"], ["看图", "[图片消息 x1]"], "纯图消息以占位文本参与合并"
        )
        self.assertEqual(
            call["image_urls"], ["https://x/1.png", "https://x/2.png"], "整批图片一起送审"
        )
        self.assertEqual(len(self._recalled(bot)), 2)

    async def test_刷图批次优先保留最新的图片(self):
        """批次内图片超过单次上限时，应丢最旧的、保最新的（最新的图最需要审核）。"""
        self.reviewer.default = {"allowed": False, "reason": "违规"}
        bot = stubs.FakeBot()
        for index in range(2):
            self._dispatch(
                f"图{index}", message_id=5100 + index, bot=bot,
                images=[f"https://x/{index}-a.png", f"https://x/{index}-b.png"],
            )
        await asyncio.sleep(0.6)
        collected = self.reviewer.calls[0]["image_urls"]
        self.assertEqual(len(collected), 3, "单次最多带 3 张图")
        self.assertEqual(
            collected,
            ["https://x/0-a.png", "https://x/0-b.png", "https://x/1-a.png"],
            "丢最旧的图、保最新的图（1-b 比 0-a 新，应优先保留），返回顺序仍按到达顺序",
        )
        self.assertNotIn("https://x/1-b.png", collected, "被丢弃的是最旧的那张")

    async def test_批次里的图片消息都进入撤回列表(self):
        """纯图消息也要能被撤回（不能因为文本为空而被跳过）。"""
        self.reviewer.default = {"allowed": False, "reason": "违规"}
        bot = stubs.FakeBot()
        self._dispatch("文字", message_id=5200, bot=bot, images=["https://x/a.png"])
        self._dispatch("", message_id=5201, bot=bot, images=["https://x/b.png"])
        await asyncio.sleep(0.6)
        self.assertEqual(self._recalled(bot), [5200, 5201])


class KeywordBatchTest(GuardTestBase):
    async def test_批次命中关键词整批撤回(self):
        """任一消息命中关键词：整批一起撤回，按关键词计数一次。"""
        self.group_conf.update(
            base_config(
                keyword_guard_enable=True,
                keyword_minor_list=["广告"],
                keyword_minor_action="recall",
                keyword_minor_ban_seconds="300",
                keyword_minor_stair_enable=False,
                keyword_minor_recall_ban_threshold=0,
            )
        )
        bot = stubs.FakeBot()
        texts = ["今晚吃啥", "广告位出租", "有人吗", "在的"]
        for index, text in enumerate(texts):
            self._dispatch(text, message_id=6000 + index, bot=bot)
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.6)
        self.assertEqual(
            self._recalled(bot), [6000 + i for i in range(4)],
            "命中关键词时整个区间的消息都要撤回",
        )
        self.assertEqual(
            self.guard.keyword_minor_tracker.counts.get("100", {}).get("200"), 1,
            "一批按一次违规计数",
        )
        self.assertEqual(len(self.reviewer.calls), 0, "关键词命中后无需再调用 LLM")

    async def test_群管理消息不进入缓冲(self):
        bot = stubs.FakeBot()
        self._dispatch("管理发言", message_id=7000, bot=bot, role="admin")
        await asyncio.sleep(0.6)
        self.assertEqual(len(self.reviewer.calls), 0, "群管理豁免，不审核")
        self.assertEqual(self._recalled(bot), [])

    async def test_白名单消息不进入缓冲(self):
        self.group_conf.update(base_config(user_whitelist=["200"]))
        bot = stubs.FakeBot()
        self._dispatch("白名单发言", message_id=7100, bot=bot)
        await asyncio.sleep(0.6)
        self.assertEqual(len(self.reviewer.calls), 0, "白名单豁免，不审核")

    async def test_不同成员各自成批(self):
        self.reviewer.default = {"allowed": False, "reason": "违规"}
        bot = stubs.FakeBot()
        self._dispatch("甲1", message_id=8001, user_id="201", bot=bot)
        self._dispatch("乙1", message_id=8002, user_id="202", bot=bot)
        self._dispatch("甲2", message_id=8003, user_id="201", bot=bot)
        await asyncio.sleep(0.6)
        self.assertEqual(len(self.reviewer.calls), 2, "两个成员各自一批")
        batches = {c["sender"]: c["texts"] for c in self.reviewer.calls}
        self.assertEqual(batches["201"], ["甲1", "甲2"])
        self.assertEqual(batches["202"], ["乙1"])
        self.assertEqual(sorted(self._recalled(bot)), [8001, 8002, 8003])


class MergeDisabledTest(GuardTestBase):
    async def test_未开启合并时逐条审核(self):
        self.group_conf.update(base_config(guard_merge_enable=False, guard_interval=0))
        self.reviewer.default = {"allowed": False, "reason": "违规"}
        bot = stubs.FakeBot()
        for index in range(3):
            self._dispatch(f"消息{index}", message_id=9000 + index, bot=bot)
        await asyncio.sleep(0.4)
        self.assertEqual(len(self.reviewer.calls), 3, "逐条审核：三条消息三次调用")
        self.assertEqual(self._recalled(bot), [9000, 9001, 9002])

    async def test_合并模式下审核间隔不再丢弃消息(self):
        """合并审核时 guard_interval 由合窗接管，避免静默丢弃消息。"""
        self.group_conf.update(base_config(guard_interval=3600))
        bot = stubs.FakeBot()
        self._dispatch("第一条", message_id=9100, bot=bot)
        await asyncio.sleep(0.6)
        self._dispatch("第二条", message_id=9101, bot=bot)
        await asyncio.sleep(0.6)
        self.assertEqual(len(self.reviewer.calls), 2, "间隔配置不应吞掉合并批次")


class MergeWindowConfigTest(GuardTestBase):
    async def test_单批上限触发提前送审(self):
        """条数达到上限时立即审核，不必等倒计时（防无上限刷屏拖延）。"""
        self.group_conf.update(base_config(guard_merge_max=3))
        self.reviewer.default = {"allowed": False, "reason": "违规"}
        bot = stubs.FakeBot()
        for index in range(4):
            self._dispatch(f"刷屏{index}", message_id=9700 + index, bot=bot)
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)  # 远小于合窗 0.25s
        self.assertEqual(len(self.reviewer.calls), 1, "达到上限应立即送审")
        self.assertEqual(self.reviewer.calls[0]["texts"], ["刷屏0", "刷屏1", "刷屏2"])

    async def test_非法窗口回退逐条审核(self):
        self.group_conf.update(base_config(guard_merge_window="abc", guard_interval=0))
        bot = stubs.FakeBot()
        self._dispatch("内容", message_id=9200, bot=bot)
        await asyncio.sleep(0.3)
        self.assertEqual(len(self.reviewer.calls), 1, "非法窗口配置应回退逐条审核")

    async def test_窗口为零回退逐条审核(self):
        self.group_conf.update(base_config(guard_merge_window=0, guard_interval=0))
        bot = stubs.FakeBot()
        self._dispatch("内容", message_id=9300, bot=bot)
        await asyncio.sleep(0.3)
        self.assertEqual(len(self.reviewer.calls), 1)


class ValidateMergeConfigTest(unittest.TestCase):
    """main.LLMGroupGuardPlugin._validate_merge 的配置校验（不实例化插件）。"""

    @classmethod
    def setUpClass(cls):
        # 只取校验函数源码执行，避免导入 main 时依赖完整的 AstrBot 运行时
        import re
        import textwrap

        source_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py"
        )
        with open(source_path, encoding="utf-8") as handle:
            source = handle.read()
        match = re.search(
            r"\n    def _validate_merge\(gconf: dict\).*?(?=\n    @|\n    async def |\n    def )",
            source,
            re.S,
        )
        assert match, "未找到 _validate_merge 定义"
        body = textwrap.dedent(match.group(0)).strip("\n")
        namespace: dict = {"Optional": object}
        # 补上 main.py 顶部的 future 导入：否则执行期会对 Optional[str] 求值
        exec("from __future__ import annotations\n" + body, namespace)  # noqa: S102
        holder = type("_Holder", (), {"_validate_merge": staticmethod(namespace["_validate_merge"])})
        cls.validate = holder._validate_merge

    def test_未开启合并直接通过(self):
        self.assertIsNone(type(self).validate({"guard_merge_enable": False, "guard_merge_window": "abc"}))

    def test_合法窗口(self):
        validate = type(self).validate
        self.assertIsNone(validate({"guard_merge_enable": True, "guard_merge_window": "10"}))
        self.assertIsNone(validate({"guard_merge_enable": True, "guard_merge_window": 5}))

    def test_空窗口被拒(self):
        self.assertIsNotNone(
            type(self).validate({"guard_merge_enable": True, "guard_merge_window": ""})
        )

    def test_非数字窗口被拒(self):
        self.assertIsNotNone(
            type(self).validate({"guard_merge_enable": True, "guard_merge_window": "十秒"})
        )

    def test_非正数窗口被拒(self):
        validate = type(self).validate
        self.assertIsNotNone(validate({"guard_merge_enable": True, "guard_merge_window": 0}))
        self.assertIsNotNone(validate({"guard_merge_enable": True, "guard_merge_window": -3}))

    def test_过长窗口被拒(self):
        self.assertIsNotNone(
            type(self).validate({"guard_merge_enable": True, "guard_merge_window": 3600})
        )


class RecalledTextLogTest(GuardTestBase):
    async def test_违规日志记录整批原文(self):
        self.reviewer.default = {"allowed": False, "reason": "广告"}
        bot = stubs.FakeBot()
        for index, text in enumerate(["第一句", "第二句"]):
            self._dispatch(text, message_id=9500 + index, bot=bot)
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.6)
        entries = self.guard.violation_log.entries if self.guard.violation_log else []
        self.assertTrue(entries, "违规日志应有记录")
        self.assertIn("第一句", entries[0]["text"])
        self.assertIn("第二句", entries[0]["text"])

    async def test_通知变量messages为条数(self):
        self.group_conf.update(
            base_config(guard_notice="违规 {count} {messages} {nickname} {duration}")
        )
        self.reviewer.default = {"allowed": False, "reason": "广告"}
        bot = stubs.FakeBot()
        for index in range(3):
            self._dispatch(f"消息{index}", message_id=9600 + index, bot=bot)
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.6)
        self.assertTrue(bot.sent, "应发送违规通知")
        message = str(bot.sent[0][1])
        self.assertIn("违规 1 3", message, "{count}=违规次数，{messages}=本次撤回条数")


if __name__ == "__main__":
    unittest.main()
