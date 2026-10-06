import tempfile
import unittest

from core.oid_binding import OidBindingStore, ban_oid_peers


class RecordingLogger:
    def __init__(self):
        self.messages = []

    def debug(self, message):
        self.messages.append(("debug", message))

    def info(self, message):
        self.messages.append(("info", message))

    def warning(self, message):
        self.messages.append(("warning", message))

    def error(self, message):
        self.messages.append(("error", message))


class FakeApi:
    def __init__(self, fail_user_ids=()):
        self.actions = []
        self.fail_user_ids = {int(user_id) for user_id in fail_user_ids}

    async def call_action(self, action, **kwargs):
        if kwargs.get("user_id") in self.fail_user_ids:
            raise RuntimeError("simulated API error")
        self.actions.append((action, kwargs))


class FakeBot:
    def __init__(self, members, fail_ban_user_ids=()):
        self.members = {int(user_id): role for user_id, role in members.items()}
        self.member_queries = []
        self.api = FakeApi(fail_ban_user_ids)

    async def get_group_member_info(self, group_id, user_id):
        self.member_queries.append((group_id, user_id))
        if user_id not in self.members:
            raise RuntimeError("not a member of this group")
        return {"user_id": user_id, "role": self.members[user_id]}


class OidBindingStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store = OidBindingStore(self.temp_dir.name)

    def test_bind_update_query_and_persistence(self):
        self.assertIsNone(self.store.bind("001234", "7654321"))
        self.assertEqual("7654321", self.store.bind("001234", "7654321"))
        self.assertIsNone(self.store.bind("002345", "7654321"))
        self.assertEqual("7654321", self.store.get_oid("001234"))
        self.assertEqual(["001234", "002345"], self.store.users_for_oid("7654321"))

        previous_oid = self.store.bind("001234", "1234567")
        self.assertEqual("7654321", previous_oid)
        self.assertEqual(["002345"], self.store.users_for_oid("7654321"))
        self.assertEqual("1234567", self.store.get_oid("001234"))

        loaded = OidBindingStore(self.temp_dir.name)
        self.assertEqual(self.store.list_bindings(), loaded.list_bindings())

    def test_rejects_invalid_ids(self):
        with self.assertRaises(ValueError):
            self.store.bind("qq-123", "7654321")
        with self.assertRaises(ValueError):
            self.store.bind("123456", "123")
        self.assertEqual({}, self.store.bindings)

    def test_unbind_and_clear(self):
        self.store.bind("001234", "7654321")
        self.store.bind("002345", "7654321")
        self.assertTrue(self.store.unbind("001234"))
        self.assertFalse(self.store.unbind("001234"))
        self.assertEqual(1, self.store.clear())
        self.assertEqual(0, self.store.clear())
        self.assertEqual([], self.store.list_bindings())


class OidPeerBanTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_bans_other_current_group_members_and_isolates_failures(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = OidBindingStore(temp_dir)
            for user_id in ("10001", "10002", "10003", "10004", "10006", "10008"):
                store.bind(user_id, "7654321")
            store.bind("10005", "1111222")  # Different OID is not affected.
            bot = FakeBot(
                {"10002": "member", "10003": "admin", "10008": "member"},
                fail_ban_user_ids=("10008",),
            )
            logger = RecordingLogger()

            count = await ban_oid_peers(
                bot, "20001", "10001", 600, store, self_id="10006", logger=logger
            )

        self.assertEqual(1, count)
        self.assertEqual(
            [("set_group_ban", {"group_id": 20001, "user_id": 10002, "duration": 600})],
            bot.api.actions,
        )
        self.assertIn((20001, 10004), bot.member_queries)
        self.assertNotIn((20001, 10006), bot.member_queries)  # Bot itself is excluded.
        self.assertTrue(any(level == "warning" for level, _ in logger.messages))

    async def test_zero_duration_does_not_propagate_unban(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = OidBindingStore(temp_dir)
            store.bind("10001", "7654321")
            store.bind("10002", "7654321")
            bot = FakeBot({"10002": "member"})

            count = await ban_oid_peers(
                bot, "20001", "10001", 0, store, self_id="19999"
            )

        self.assertEqual(0, count)
        self.assertEqual([], bot.member_queries)
        self.assertEqual([], bot.api.actions)


if __name__ == "__main__":
    unittest.main()
