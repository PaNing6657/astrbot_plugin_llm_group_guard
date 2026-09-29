# tests/test_merge_buffer.py
"""消息合并缓冲的倒计时/重置/分批行为验证。"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import stubs  # noqa: E402

stubs.install()

from core.merge_buffer import MergeBuffer  # noqa: E402

WINDOW = 0.25


class MergeBufferTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.flushed: list = []
        self.window_arg = WINDOW

        async def flush(batch):
            self.flushed.append(batch)

        self.buffer = MergeBuffer(self._window, flush)

    def _window(self, group_id):
        if self.window_arg is not None:
            return self.window_arg
        return 0.0

    async def test_连续消息合并为一批(self):
        """合窗内持续发言只重置倒计时，静默满窗口后整批一次性送审。"""
        for index in range(6):
            self.buffer.push("100", "200", f"msg{index}", f"k{index}")
            await asyncio.sleep(WINDOW / 6)  # 均未满一个完整窗口
        self.assertEqual(len(self.flushed), 0, "倒计时期间不应提前审核")
        self.assertEqual(self.buffer.pending_count(), 6)
        await asyncio.sleep(WINDOW * 1.5)
        self.assertEqual(len(self.flushed), 1, "静默满窗口后应只触发一次审核")
        batch = self.flushed[0]
        self.assertEqual(batch.events, [f"msg{i}" for i in range(6)])
        self.assertEqual(batch.keys, [f"k{i}" for i in range(6)])
        self.assertEqual(batch.group_id, "100")
        self.assertEqual(batch.user_id, "200")
        self.assertFalse(self.buffer.is_pending("100", "200"))

    async def test_倒计时被新消息重置(self):
        """接近窗口末尾再发一条，倒计时应重新计满整个窗口。"""
        self.buffer.push("100", "200", "A", "kA")
        await asyncio.sleep(WINDOW * 0.8)
        self.buffer.push("100", "200", "B", "kB")
        # 从 A 起算已超过一个窗口，但 B 重置了倒计时，此时不应触发
        await asyncio.sleep(WINDOW * 0.3)
        self.assertEqual(len(self.flushed), 0, "重置后不应在旧倒计时到点时送审")
        await asyncio.sleep(WINDOW)
        self.assertEqual(len(self.flushed), 1)
        self.assertEqual(self.flushed[0].events, ["A", "B"])

    async def test_不同成员与不同群互不影响(self):
        self.buffer.push("100", "200", "u1", "k1")
        await asyncio.sleep(WINDOW / 3)
        self.buffer.push("100", "201", "u2", "k2")
        await asyncio.sleep(WINDOW * 0.8)
        self.assertEqual(len(self.flushed), 1, "第一个成员应先到期")
        self.assertEqual(self.flushed[0].events, ["u1"])
        await asyncio.sleep(WINDOW)
        self.assertEqual(len(self.flushed), 2)
        self.assertEqual(self.flushed[1].events, ["u2"])

    async def test_未开启合并时立即返回False(self):
        self.window_arg = None  # 合窗为 0：不使用合并
        self.assertFalse(self.buffer.push("100", "200", "x", "kx"))
        self.assertEqual(self.buffer.pending_count(), 0)
        self.assertFalse(self.buffer.active("100"))
        await asyncio.sleep(WINDOW * 1.5)
        self.assertEqual(self.flushed, [])

    async def test_取消后不再送审(self):
        self.buffer.push("100", "200", "A", "kA")
        self.assertTrue(self.buffer.is_pending("100", "200"))
        self.buffer.cancel("100", "200")
        await asyncio.sleep(WINDOW * 1.5)
        self.assertEqual(self.flushed, [], "已取消的批次不应再送审")
        self.assertFalse(self.buffer.is_pending("100", "200"))

    async def test_多批之中只取消其中一批(self):
        """取消当前批次后，此前已被重置的旧倒计时不得把该批补审一遍。"""
        self.buffer.push("100", "200", "A", "kA")
        await asyncio.sleep(WINDOW * 0.5)
        self.buffer.push("100", "200", "B", "kB")
        self.buffer.cancel("100", "200")
        await asyncio.sleep(WINDOW * 2)
        self.assertEqual(self.flushed, [], "取消后任何一代倒计时都不应触发审核")
        self.buffer.push("100", "200", "C", "kC")
        await asyncio.sleep(WINDOW * 1.5)
        self.assertEqual([b.events for b in self.flushed], [["C"]], "取消只影响被取消的批次")

    async def test_审核进行中到达的新消息独立成批(self):
        """上一批送审期间的新消息不能被吞掉，也不与上一批重复。"""
        released = asyncio.Event()

        async def slow_flush(batch):
            self.flushed.append(batch)
            await released.wait()

        self.buffer = MergeBuffer(lambda gid: WINDOW, slow_flush)
        self.buffer.push("100", "200", "A", "kA")
        await asyncio.sleep(WINDOW * 1.5)
        self.assertEqual(len(self.flushed), 1, "第一批已进入审核")
        self.buffer.push("100", "200", "B", "kB")  # 审核进行中到达
        released.set()
        await asyncio.sleep(WINDOW * 1.5)
        self.assertEqual(len(self.flushed), 2, "新消息应另起一批并正常送审")
        self.assertEqual(self.flushed[0].events, ["A"])
        self.assertEqual(self.flushed[1].events, ["B"])

    async def test_clear后停止工作(self):
        self.buffer.push("100", "200", "A", "kA")
        self.buffer.clear()
        await asyncio.sleep(WINDOW * 1.5)
        self.assertEqual(self.flushed, [], "clear 后不应再送审残留批次")
        self.assertFalse(self.buffer.push("100", "200", "B", "kB"), "clear 后 push 应返回 False")

    async def test_缓冲事件快照可供预审读取(self):
        self.buffer.push("100", "200", "A", "kA")
        self.buffer.push("100", "200", "B", "kB")
        snapshot = self.buffer.buffered_events("100", "200")
        self.assertEqual(snapshot, ["A", "B"])
        snapshot.append("篡改")
        self.assertEqual(self.buffer.buffered_events("100", "200"), ["A", "B"])

    async def test_审核回调异常不影响后续批次(self):
        async def broken_flush(batch):
            raise RuntimeError("审核炸了")

        self.buffer = MergeBuffer(lambda gid: WINDOW, broken_flush)
        self.buffer.push("100", "200", "A", "kA")
        await asyncio.sleep(WINDOW * 1.5)
        self.assertFalse(self.buffer.is_pending("100", "200"), "异常后应释放批次")


class MaxBatchSizeTest(unittest.IsolatedAsyncioTestCase):
    """单批条数上限：达到上限立即送审，不等倒计时走完。"""

    async def test_达到上限立即送审(self):
        flushed: list = []

        async def flush(batch):
            flushed.append(batch)

        buffer = MergeBuffer(lambda gid: 30, flush, max_size=3)  # 窗口很长，只能靠上限触发
        for index in range(3):
            buffer.push("100", "200", f"m{index}", f"k{index}")
        await asyncio.sleep(0)
        self.assertEqual(len(flushed), 1, "达到上限应立即送审")
        self.assertEqual(flushed[0].events, ["m0", "m1", "m2"])
        self.assertFalse(buffer.is_pending("100", "200"), "已送审的批次不应残留")

    async def test_上限之后的新消息另起一批(self):
        flushed: list = []

        async def flush(batch):
            flushed.append(batch)

        buffer = MergeBuffer(lambda gid: 30, flush, max_size=2)
        buffer.push("100", "200", "A", "kA")
        buffer.push("100", "200", "B", "kB")
        await asyncio.sleep(0)
        buffer.push("100", "200", "C", "kC")
        await asyncio.sleep(0)
        self.assertEqual([b.events for b in flushed], [["A", "B"]])
        self.assertEqual(buffer.pending_count(), 1, "上限之后的消息应进入新一批")

    async def test_零表示不限制(self):
        flushed: list = []

        async def flush(batch):
            flushed.append(batch)

        buffer = MergeBuffer(lambda gid: 0.2, flush, max_size=0)
        buffer.push("100", "200", "A", "kA")
        await asyncio.sleep(0)
        self.assertEqual(flushed, [], "未到期且无上限时不应提前送审")
        buffer.clear()


class MergeWindowParseTest(unittest.TestCase):
    """window_getter 返回异常值时的容错。"""

    def test_非法窗口视为未开启(self):
        buffer = MergeBuffer(lambda gid: (_ for _ in ()).throw(ValueError("坏配置")), None)
        self.assertEqual(buffer.window("100"), 0.0)
        self.assertFalse(buffer.active("100"))

    def test_负数窗口视为未开启(self):
        buffer = MergeBuffer(lambda gid: -5, None)
        self.assertFalse(buffer.active("100"))


if __name__ == "__main__":
    unittest.main()
