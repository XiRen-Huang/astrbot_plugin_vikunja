import unittest

from state_store import AUDIT_LIMIT, StateStore


def _memory_store(initial=None):
    persisted = dict(initial or {})

    async def load():
        return persisted

    async def save(value):
        persisted.clear()
        persisted.update(value)

    return StateStore(load, save), persisted


class StateStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_channel_and_dedup_are_persisted(self):
        store, persisted = _memory_store()
        await store.initialize()
        await store.register_channel("qq-private", {"umo": "qq-private", "reminders_enabled": True})
        await store.set_task_reminder(12, 60)
        await store.mark_sent("qq-private|12|due", "2026-07-11T00:00:00Z")

        self.assertEqual(store.channel("qq-private")["umo"], "qq-private")
        self.assertEqual(store.task_reminder_minutes(12, 30), 60)
        self.assertTrue(store.was_sent("qq-private|12|due"))
        self.assertIn("channels", persisted)

    async def test_task_reminder_overrides_snapshot(self):
        # 键在存储里是字符串，快照要还原成 int 供轮询调度使用；脏数据直接跳过。
        store, _ = _memory_store(
            {"task_reminder_minutes": {"12": 60, "7": 15, "坏键": "oops"}}
        )
        await store.initialize()

        self.assertEqual(store.task_reminder_overrides(), {12: 60, 7: 15})


class ProjectDeletionAuditTests(unittest.IsolatedAsyncioTestCase):
    """项目删除审计——硬删除唯一的事后凭据。"""

    def _entry(self, project_id, at="2026-09-27 14:20"):
        return {
            "at": at,
            "project_id": project_id,
            "path": f"p{project_id}",
            "tasks": 0,
            "task_refs": [],
            "source": "command",
        }

    async def test_records_newest_first(self):
        store, _ = _memory_store()
        await store.initialize()
        await store.record_project_deletion(self._entry(1))
        await store.record_project_deletion(self._entry(2))

        self.assertEqual([e["project_id"] for e in store.project_deletions()], [2, 1])

    async def test_persists_across_reload(self):
        """审计必须落到 KV：进程重启后还能回看，否则等于没记。"""
        store, persisted = _memory_store()
        await store.initialize()
        await store.record_project_deletion(self._entry(7))

        self.assertIn("project_deletions", persisted)

        async def load():
            return persisted

        reloaded = StateStore(load, lambda value: None)
        await reloaded.initialize()
        self.assertEqual(reloaded.project_deletions()[0]["project_id"], 7)

    async def test_old_state_without_the_audit_key_loads(self):
        """老版本的 KV 里没有这个键，升级后不能因此丢状态或报错。"""
        store, _ = _memory_store({"channels": {"qq": {"umo": "qq"}}})
        await store.initialize()

        self.assertEqual(store.project_deletions(), [])
        self.assertIsNotNone(store.channel("qq"))

    async def test_a_corrupt_audit_value_is_ignored(self):
        """KV 里被写成 dict 时不能直接采信——这里的消费者只认列表。"""
        store, _ = _memory_store({"project_deletions": {"坏": "数据"}})
        await store.initialize()

        self.assertEqual(store.project_deletions(), [])

    async def test_only_the_most_recent_entries_are_kept(self):
        store, _ = _memory_store()
        await store.initialize()
        for index in range(AUDIT_LIMIT + 5):
            await store.record_project_deletion(self._entry(index))

        entries = store.project_deletions()
        self.assertEqual(len(entries), AUDIT_LIMIT)
        self.assertEqual(entries[0]["project_id"], AUDIT_LIMIT + 4)

    async def test_reads_are_copies(self):
        """读出来的列表不能被就地改到内部状态上（会绕过保存回调）。"""
        store, _ = _memory_store()
        await store.initialize()
        await store.record_project_deletion(self._entry(1))

        store.project_deletions()[0]["project_id"] = 999
        self.assertEqual(store.project_deletions()[0]["project_id"], 1)


if __name__ == "__main__":
    unittest.main()
