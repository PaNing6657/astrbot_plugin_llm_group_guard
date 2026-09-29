# tests/test_llm_reviewer_batch.py
"""合并审核的送审文本、判定解析，以及图片是否真的带进 llm_generate 的验证。"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import stubs  # noqa: E402

stubs.install()
stubs.install_multimodal()  # 必须在导入 llm_reviewer 之前：模块导入时探测多模态结构

from core import llm_reviewer as lr  # noqa: E402

IMAGE_A = "https://x/a.png"
IMAGE_B = "https://x/b.png"


def image_urls_of(kwargs) -> list:
    """从 llm_generate 入参里取出图片 URL（模拟 AstrBot 的解析方式）。"""
    contexts = kwargs.get("contexts") or []
    urls: list = []
    for message in contexts:
        for part in getattr(message, "content", []) or []:
            url = getattr(getattr(part, "image_url", None), "url", None)
            if url:
                urls.append(url)
    return urls


class FormatBatchTest(unittest.TestCase):
    def test_单条消息保持原有格式(self):
        text = lr.LLMReviewer._format_batch("200", ["你好"])
        self.assertEqual(text, "发言者：200\n消息内容：你好")

    def test_纯图单条消息占位(self):
        self.assertIn("[纯图片消息]", lr.LLMReviewer._format_batch("200", [""]))

    def test_多条消息带序号且顺序不变(self):
        texts = ["A", "B", "C", "D", "E", "F"]
        out = lr.LLMReviewer._format_batch("200", texts)
        for index, item in enumerate(texts, 1):
            self.assertIn(f"{index}. {item}", out)
        self.assertLess(out.index("1. A"), out.index("2. B"), "顺序即发送顺序")
        self.assertLess(out.index("5. E"), out.index("6. F"))
        self.assertIn("6 条消息", out, "应告知模型本批条数")
        self.assertIn("整体", out, "应告知模型作为整体上下文判断")
        self.assertIn("一并撤回", out, "应告知模型违规后果覆盖整批")

    def test_批次内纯图消息占位(self):
        out = lr.LLMReviewer._format_batch("200", ["看图", "", "继续"])
        self.assertIn("2. [图片消息]", out)

    def test_单条超长消息被截断(self):
        out = lr.LLMReviewer._format_batch("200", ["字" * 5000])
        self.assertLess(len(out), lr._MAX_MSG_CHARS + 100)
        self.assertNotIn("内容过长已截断", out, "单条消息走原有截断逻辑")

    def test_整批超长时提示内容不完整(self):
        out = lr.LLMReviewer._format_batch("200", ["字" * 3000 for _ in range(5)])
        self.assertIn("内容过长已截断", out, "截断时必须提示模型内容不完整")

    def test_空批次兜底(self):
        self.assertIn("纯图片消息", lr.LLMReviewer._format_batch("200", []))


class ExtractJsonTest(unittest.TestCase):
    def test_普通JSON(self):
        self.assertEqual(
            lr.extract_json_object('{"allowed": false, "reason": "广告"}'),
            {"allowed": False, "reason": "广告"},
        )

    def test_代码块包裹(self):
        raw = '```json\n{"allowed": true, "reason": ""}\n```'
        self.assertEqual(lr.extract_json_object(raw), {"allowed": True, "reason": ""})

    def test_前后杂讯(self):
        raw = '判定如下：{"allowed": false, "reason": "广告"} 以上。'
        self.assertEqual(lr.extract_json_object(raw), {"allowed": False, "reason": "广告"})

    def test_无法解析返回None(self):
        self.assertIsNone(lr.extract_json_object("模型胡言乱语"))


class VisionPayloadTest(unittest.IsolatedAsyncioTestCase):
    """图片链路：确认图片真的以多模态消息进入 llm_generate，并带上上下文说明。"""

    def setUp(self):
        self.assertTrue(lr._MULTIMODAL_OK, "测试环境应装上多模态桩")

    async def test_主模型识图时图片直接带过去(self):
        context = stubs.FakeContext(vision_models=["vision/model"])
        reviewer = lr.LLMReviewer({}, context)
        await reviewer.judge_messages(
            "200", ["看图", "再看"], chat_id="vision/model", image_urls=[IMAGE_A, IMAGE_B]
        )
        self.assertEqual(len(context.calls), 1, "只应调用一次识图模型")
        self.assertEqual(image_urls_of(context.calls[0]), [IMAGE_A, IMAGE_B], "图片要真的带上")
        self.assertIn("附带了 2 张图片", context.calls[0]["prompt"], "应说明图片与本批消息的关系")

    async def test_主模型不识图时改由识图模型带图(self):
        context = stubs.FakeContext(vision_models=["ocr/model"])
        reviewer = lr.LLMReviewer({}, context)
        await reviewer.judge_messages(
            "200",
            ["看图"],
            chat_id="text/model",
            ocr_chat_id="ocr/model",
            image_urls=[IMAGE_A],
        )
        self.assertEqual(len(context.calls), 1)
        self.assertEqual(context.calls[0]["chat_provider_id"], "ocr/model", "应由识图模型出判定")
        self.assertEqual(image_urls_of(context.calls[0]), [IMAGE_A])

    async def test_识图模型不可用时降级纯文本且不谎报带图(self):
        context = stubs.FakeContext(vision_models=[])
        reviewer = lr.LLMReviewer({}, context)
        await reviewer.judge_messages(
            "200", ["看图"], chat_id="text/model", image_urls=[IMAGE_A]
        )
        self.assertEqual(image_urls_of(context.calls[0]), [], "不识图模型不应收到图片")
        self.assertIn("图片未审核", context.calls[0]["prompt"], "降级时要如实说明图片未审核")
        self.assertNotIn("附带了 1 张图片", context.calls[0]["prompt"], "不应谎报已带图")

    async def test_未配置识图模型时也降级纯文本(self):
        context = stubs.FakeContext(vision_models=[])
        reviewer = lr.LLMReviewer({}, context)
        await reviewer.judge_messages(
            "200", ["看图"], chat_id="text/model", ocr_chat_id="text/model", image_urls=[IMAGE_A]
        )
        self.assertEqual(image_urls_of(context.calls[0]), [])
        self.assertIn("图片未审核", context.calls[0]["prompt"])

    async def test_超出上限时说明还有图片未附上(self):
        context = stubs.FakeContext(vision_models=["vision/model"])
        reviewer = lr.LLMReviewer({}, context)
        urls = [f"https://x/{i}.png" for i in range(5)]
        await reviewer.judge_messages("200", ["图"], chat_id="vision/model", image_urls=urls)
        self.assertEqual(
            image_urls_of(context.calls[0]), urls[: lr._MAX_IMAGES], "最多带 _MAX_IMAGES 张"
        )
        self.assertIn("超出单次送审上限", context.calls[0]["prompt"])

    async def test_无图时不加图片说明(self):
        context = stubs.FakeContext(vision_models=["vision/model"])
        reviewer = lr.LLMReviewer({}, context)
        await reviewer.judge_messages("200", ["纯文字"], chat_id="vision/model")
        self.assertNotIn("图片", context.calls[0]["prompt"])

    async def test_识图模型失败时降级纯文本并提示(self):
        context = stubs.FakeContext(vision_models=["vision/model"], raise_exc=RuntimeError("boom"))
        reviewer = lr.LLMReviewer({}, context)
        result = await reviewer.judge_messages(
            "200", ["看图"], chat_id="vision/model", fallback_chat_id="vision/model",
            image_urls=[IMAGE_A],
        )
        self.assertIsNone(result, "调用失败应返回 None")
        self.assertTrue(context.calls, "应至少尝试过一次调用")


class PassThroughTest(unittest.IsolatedAsyncioTestCase):
    """judge_message 应等价于单条 judge_messages。"""

    async def test_judge_message委托给批量接口(self):
        captured = {}

        class FakeReviewer(lr.LLMReviewer):
            async def judge_messages(self, sender, texts, **kwargs):
                captured["sender"] = sender
                captured["texts"] = list(texts)
                return {"allowed": True}

        reviewer = FakeReviewer({}, context=None)
        result = await reviewer.judge_message("200", "内容", chat_id="m")
        self.assertEqual(result, {"allowed": True})
        self.assertEqual(captured, {"sender": "200", "texts": ["内容"]})


if __name__ == "__main__":
    unittest.main()
