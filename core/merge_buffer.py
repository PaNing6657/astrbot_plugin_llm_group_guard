# core/merge_buffer.py
"""消息合并缓冲：把同一成员连续发送的短消息攒成一批，再一次性交给 LLM 审核。

时间线（合窗 = 10 秒为例）：

    A 到达 → 起倒计时 10s
    B 到达（10s 内）→ 倒计时重置为 10s
    C 到达（10s 内）→ 倒计时继续重置
    … D E F 同理
    倒计时走完仍无新消息 → 把 [A B C D E F] 作为一批交给审核回调

实现要点：
- 按 (群, 成员) 分桶，各桶倒计时互不影响，不同成员的连发不会互相"续命"
- 倒计时用「代次（version）」实现：每次 push 生成新一代延时任务，旧一代醒来
  发现代次已过期就直接退出，等效于把倒计时重置为完整窗口
- 倒计时结束与成员紧接着发新消息可能同时发生：触发时先把批次摘出挂起表，
  该成员随后到达的消息会另起新批次，不会丢消息也不会重复审核
- 批次对象与 astrbot 事件对象解耦，本模块不导入 astrbot，便于单测
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Hashable, Optional

# 审核回调：收到一批待审消息（收到的 events 顺序即消息到达顺序）
FlushFunc = Callable[["MergedBatch"], Awaitable[None]]


@dataclass
class MergedBatch:
    """一批已合并、等待审核的消息。"""

    key: Hashable  # (group_id, user_id)
    group_id: str
    user_id: str
    events: list = field(default_factory=list)  # 消息事件，顺序 = 到达顺序
    keys: list = field(default_factory=list)  # 与 events 一一对应的去重键（message_id）
    created_at: float = 0.0  # 本批第一条消息的时间戳
    version: int = 0  # 代次：每次追加消息 +1，用于让旧倒计时失效
    timer: Optional[asyncio.Task] = None
    llm_enabled: Optional[bool] = None  # 本批首条消息到达时的 LLM 审核开关快照

    def __len__(self) -> int:
        return len(self.events)

    def add(self, event, msg_key: str) -> None:
        """把一条消息追加进本批（保持到达顺序）。"""
        self.events.append(event)
        self.keys.append(msg_key)


class MergeBuffer:
    """按 (群, 成员) 的合窗去抖缓冲：静默满窗口后回调审核。"""

    def __init__(
        self,
        window_getter: Callable[[Any], float],
        flush: FlushFunc,
        logger=None,
        max_size: int = 0,
        size_getter: Optional[Callable[[Any], int]] = None,
    ):
        """window_getter(group_id) -> 合窗秒数（<=0 表示不使用合并）；flush(batch) 为审核回调。

        max_size/size_getter：可选的单批条数上限（0 表示不限制）。达到上限时立即送审，
        避免超大刷屏批次把审核无限往后拖、并让撤回请求一次性爆发。
        """
        self.max_size = int(max_size or 0)
        self._size_getter = size_getter
        self._window_getter = window_getter
        self._flush = flush
        self._logger = logger
        self._pending: dict[Hashable, MergedBatch] = {}
        self._timers: set[asyncio.Task] = set()
        self._closed = False

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def max_batch(self, group_id) -> int:
        """该群的单批条数上限（0 表示不限制）。"""
        if self._size_getter is None:
            return self.max_size
        try:
            value = int(self._size_getter(group_id))
        except (TypeError, ValueError):
            return self.max_size
        return value if value > 0 else 0
    def window(self, group_id) -> float:
        """取该群的合窗时长（秒），配置非法/未启用时返回 0。"""
        try:
            value = float(self._window_getter(group_id))
        except (TypeError, ValueError):
            return 0.0
        return value if value > 0 else 0.0

    def active(self, group_id) -> bool:
        """该群是否启用合并审核。"""
        return self.window(group_id) > 0

    def is_pending(self, group_id, user_id) -> bool:
        """该成员是否有消息正在缓冲（等待合并审核）。"""
        return (str(group_id), str(user_id)) in self._pending

    def pending_count(self, group_id=None) -> int:
        """缓冲中的消息条数；给定 group_id 时只统计该群。"""
        if group_id is None:
            return sum(len(b) for b in self._pending.values())
        gid = str(group_id)
        return sum(len(b) for b in self._pending.values() if b.group_id == gid)

    def buffered_events(self, group_id, user_id) -> list:
        """取该成员当前缓冲的消息事件（副本，供关键词预审等只读用途）。"""
        batch = self._pending.get((str(group_id), str(user_id)))
        return list(batch.events) if batch else []

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def push(
        self,
        group_id,
        user_id,
        event,
        msg_key: str,
        now: Optional[float] = None,
        llm_enabled: Optional[bool] = None,
    ) -> bool:
        """把一条消息放入缓冲并重置倒计时。

        返回 True 表示已缓冲（由本缓冲负责后续审核）；返回 False 表示缓冲已关闭，
        调用方应立即自行审核该消息（插件卸载期间的兜底）。
        """
        if self._closed:
            return False
        window = self.window(group_id)
        if window <= 0:
            return False
        gid, uid = str(group_id), str(user_id)
        key = (gid, uid)
        batch = self._pending.get(key)
        if batch is None:
            batch = MergedBatch(
                key=key,
                group_id=gid,
                user_id=uid,
                created_at=now,
                llm_enabled=llm_enabled,
            )
            self._pending[key] = batch
        batch.add(event, msg_key)
        # 达到单批上限：不再等倒计时，立即送审，避免超大刷屏批次无限延后
        limit = self.max_batch(gid)
        if limit and len(batch) >= limit:
            if batch.timer is not None:
                batch.timer.cancel()
                batch.timer = None
            self._pending.pop(key, None)
            batch.version += 1
            self._spawn_flush(batch)
            return True
        # 新一代倒计时：旧一代醒来时发现代次过期，直接退出（等效重置倒计时）
        batch.version += 1
        try:
            task = asyncio.get_running_loop().create_task(self._debounce(key, batch.version, window))
        except RuntimeError as exc:  # 不在事件循环中：退化为立即审核，避免消息滞留
            self._pending.pop(key, None)
            self._log("error", f"[MergeBuffer] 无法创建倒计时任务（不在事件循环中）: {exc}")
            return False
        task.add_done_callback(self._timers.discard)
        self._timers.add(task)
        batch.timer = task
        return True

    def cancel(self, group_id, user_id) -> None:
        """丢弃该成员未完成的缓冲（消息已在别处处理，无需再合并审核）。"""
        batch = self._pending.pop((str(group_id), str(user_id)), None)
        if batch is not None and batch.timer is not None:
            batch.timer.cancel()

    def _spawn_flush(self, batch: MergedBatch) -> None:
        """立即把一批交给审核回调（不经倒计时，用于达到单批上限时）。"""
        try:
            task = asyncio.get_running_loop().create_task(self._run_flush(batch))
        except RuntimeError:
            self._log("error", "[MergeBuffer] 无法创建审核任务（不在事件循环中），本批已丢弃")
            return
        task.add_done_callback(self._timers.discard)
        self._timers.add(task)

    async def _run_flush(self, batch: MergedBatch) -> None:
        try:
            await self._flush(batch)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log("error", f"[MergeBuffer] 合并审核执行异常: {exc}")

    def cancel_all(self) -> None:
        """丢弃全部未完成的缓冲。"""
        for batch in list(self._pending.values()):
            if batch.timer is not None:
                batch.timer.cancel()
        self._pending.clear()

    def clear(self) -> None:
        """丢弃全部缓冲并停止工作（插件卸载/热重载时调用）。"""
        self._closed = True
        for batch in list(self._pending.values()):
            if batch.timer is not None:
                batch.timer.cancel()
        self._pending.clear()
        for task in list(self._timers):
            task.cancel()
        self._timers.clear()

    # ------------------------------------------------------------------
    # 内部：倒计时到期 → 交给审核回调
    # ------------------------------------------------------------------
    async def _debounce(self, key: Hashable, version: int, window: float) -> None:
        """睡满一个完整窗口；期间有新消息则本代作废（唤醒后自行退出）。"""
        try:
            await asyncio.sleep(window)
        except asyncio.CancelledError:
            raise
        batch = self._pending.get(key)
        if batch is None or batch.version != version:
            return  # 已被取消/已被新消息重置，交由最新一代处理
        self._pending.pop(key, None)
        await self._run_flush(batch)  # 单批审核失败已在 _run_flush 内兜住

    def _log(self, level: str, message: str) -> None:
        if self._logger is None:
            return
        try:
            getattr(self._logger, level, self._logger.info)(message)
        except Exception:
            pass
