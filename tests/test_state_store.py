import unittest

from state_store import StateStore


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


if __name__ == "__main__":
    unittest.main()
