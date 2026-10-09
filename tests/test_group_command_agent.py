import asyncio
import unittest
from types import SimpleNamespace

from core.group_command_agent import (
    GROUP_COMMAND_TOOL_NAMES,
    GroupCommandExecutionError,
    GroupCommandProviderError,
    GroupCommandToolError,
    format_mention_context,
    is_explicit_member_target,
    run_group_command_tool_once,
    strip_group_command_prefix,
)


class FakeProvider:
    def __init__(self, response=None, error=None, delay=0):
        self.response = response
        self.error = error
        self.delay = delay
        self.calls = []

    async def text_chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.response


class GroupCommandInputTests(unittest.TestCase):
    def test_strip_command_after_astrbot_wake_prefix(self):
        self.assertEqual("帮我禁言 @小明", strip_group_command_prefix("群管 帮我禁言 @小明"))
        self.assertEqual("帮我禁言", strip_group_command_prefix("/群管 帮我禁言"))
        self.assertEqual("帮我禁言", strip_group_command_prefix("[CQ:at,qq=10001] 群管 帮我禁言"))
        self.assertEqual("帮我禁言", strip_group_command_prefix("[At:10001] 群管 帮我禁言"))

    def test_high_recall_and_llm_audit_toggle_tools_are_exposed(self):
        self.assertIn("set_group_high_recall_mode", GROUP_COMMAND_TOOL_NAMES)
        self.assertIn("set_group_llm_audit", GROUP_COMMAND_TOOL_NAMES)

    def test_mention_context_keeps_only_unique_numeric_targets(self):
        self.assertEqual(
            [
                {"user_id": "10001", "name": "小明"},
                {"user_id": "10002", "name": ""},
            ],
            format_mention_context(
                [("10001", "小明"), ("10001", "重复"), ("all", "全体"), ("10002", None)]
            ),
        )

    def test_member_target_must_be_present_in_this_command(self):
        mentions = [("10001", "小明")]
        self.assertTrue(is_explicit_member_target("10001", "帮我禁言", mentions))
        self.assertTrue(is_explicit_member_target("小明", "帮我禁言", mentions))
        self.assertFalse(is_explicit_member_target("10002", "帮我禁言", mentions))
        self.assertTrue(is_explicit_member_target("123456", "禁言 123456", []))
        self.assertFalse(is_explicit_member_target("600", "禁言10分钟", []))
        self.assertFalse(is_explicit_member_target("123456", "禁言10分钟", []))

    def test_self_target_requires_explicit_self_request_and_no_mention(self):
        self.assertTrue(is_explicit_member_target("10000", "帮我禁言", [], operator_id="10000"))
        self.assertTrue(is_explicit_member_target("10000", "解除禁言我", [], operator_id="10000"))
        self.assertFalse(is_explicit_member_target("10000", "帮我禁言 @小明", [("10001", "小明")], "10000"))
        self.assertFalse(is_explicit_member_target("10000", "禁言小明", [], operator_id="10000"))


class GroupCommandToolLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_call_executes_one_tool_and_does_not_call_a_second_llm(self):
        response = SimpleNamespace(
            role="tool",
            completion_text="",
            tools_call_name=["set_group_member_ban"],
            tools_call_args=[{"user_id": "10001", "enable": True, "duration": 600}],
        )
        provider = FakeProvider(response=response)
        executed = []

        async def executor(name, arguments):
            executed.append((name, arguments))
            return {"status": "success", "message": "已禁言成员 10001"}

        outcome = await run_group_command_tool_once(
            provider, "本次请求", object(), executor, timeout=1
        )

        self.assertEqual("success", outcome.status)
        self.assertEqual("已禁言成员 10001", outcome.message)
        self.assertEqual("set_group_member_ban", outcome.tool_name)
        self.assertEqual(1, len(provider.calls))
        self.assertIn("func_tool", provider.calls[0])
        self.assertEqual(
            [("set_group_member_ban", {"user_id": "10001", "enable": True, "duration": 600})],
            executed,
        )

    async def test_high_recall_and_llm_audit_calls_are_allowed(self):
        for name in ("set_group_high_recall_mode", "set_group_llm_audit"):
            with self.subTest(tool=name):
                provider = FakeProvider(
                    response=SimpleNamespace(
                        role="tool",
                        completion_text="",
                        tools_call_name=[name],
                        tools_call_args=[{"action": "toggle"}],
                    )
                )
                executed = []

                async def executor(tool_name, arguments):
                    executed.append((tool_name, arguments))
                    return {"status": "success", "message": "开关已更新"}

                outcome = await run_group_command_tool_once(
                    provider, "开启", object(), executor, timeout=1
                )
                self.assertEqual(name, outcome.tool_name)
                self.assertEqual("success", outcome.status)
                self.assertEqual([(name, {"action": "toggle"})], executed)

    async def test_no_tool_call_never_executes_anything(self):
        provider = FakeProvider(
            response=SimpleNamespace(
                role="assistant",
                completion_text="请说明要操作哪位成员。",
                tools_call_name=[],
                tools_call_args=[],
            )
        )
        executed = []

        async def executor(name, arguments):
            executed.append((name, arguments))

        outcome = await run_group_command_tool_once(provider, "请求", object(), executor)
        self.assertEqual("no_tool", outcome.status)
        self.assertEqual("请说明要操作哪位成员。", outcome.message)
        self.assertEqual([], executed)

    async def test_multiple_tool_calls_are_rejected_before_execution(self):
        provider = FakeProvider(
            response=SimpleNamespace(
                role="tool",
                completion_text="",
                tools_call_name=["set_group_whole_ban", "cancel_group_ban_schedules"],
                tools_call_args=[{"enable": True}, {}],
            )
        )
        executed = []

        async def executor(name, arguments):
            executed.append(name)

        with self.assertRaises(GroupCommandToolError):
            await run_group_command_tool_once(provider, "请求", object(), executor)
        self.assertEqual([], executed)

    async def test_unknown_tool_and_bad_arguments_are_rejected(self):
        cases = [
            SimpleNamespace(
                role="tool", completion_text="", tools_call_name=["delete_everything"], tools_call_args=[{}]
            ),
            SimpleNamespace(
                role="tool", completion_text="", tools_call_name=["set_group_member_ban"], tools_call_args=["{bad json"]
            ),
        ]
        for response in cases:
            with self.subTest(response=response):
                provider = FakeProvider(response=response)
                executed = []

                async def executor(name, arguments):
                    executed.append(name)

                with self.assertRaises(GroupCommandToolError):
                    await run_group_command_tool_once(provider, "请求", object(), executor)
                self.assertEqual([], executed)

    async def test_provider_failure_is_distinct_from_tool_execution_failure(self):
        provider = FakeProvider(error=RuntimeError("provider offline"))

        async def unused_executor(name, arguments):
            self.fail("executor should not run")

        with self.assertRaises(GroupCommandProviderError):
            await run_group_command_tool_once(provider, "请求", object(), unused_executor)

        response = SimpleNamespace(
            role="tool",
            completion_text="",
            tools_call_name=["set_group_whole_ban"],
            tools_call_args=[{"enable": True}],
        )
        provider = FakeProvider(response=response)

        async def failing_executor(name, arguments):
            raise RuntimeError("action failed")

        with self.assertRaises(GroupCommandExecutionError):
            await run_group_command_tool_once(provider, "请求", object(), failing_executor)

    async def test_provider_timeout_is_reported(self):
        provider = FakeProvider(
            response=SimpleNamespace(role="assistant", completion_text="", tools_call_name=[], tools_call_args=[]),
            delay=0.05,
        )

        async def unused_executor(name, arguments):
            self.fail("executor should not run")

        with self.assertRaises(GroupCommandProviderError):
            await run_group_command_tool_once(
                provider, "请求", object(), unused_executor, timeout=0.001
            )


if __name__ == "__main__":
    unittest.main()
