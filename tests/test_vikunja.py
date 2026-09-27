import unittest
from copy import deepcopy

from vikunja import (
    PROJECT_ECHO_FIELDS,
    TASK_ECHO_EXCLUSIONS,
    TASK_WRITABLE_FIELDS,
    VikunjaClient,
    VikunjaError,
    normalize_api_url,
)


SAMPLE_TASK = {
    "id": 7,
    "title": "修改论文引言",
    "description": "设计稿在 FluidCapsule 里",
    "done": False,
    "due_date": "2026-07-12T10:00:00Z",
    "priority": 4,
    "start_date": None,
    "end_date": None,
    "repeat_after": 0,
    "repeat_mode": 0,
    "percent_done": 0.0,
    "hex_color": "",
    "bucket_id": 0,
    "project_id": 3,
    # 下面这些是 xorm:"-" 字段：不是数据库列，但读得到，且服务端对它们有副作用
    # （reminders 先删光再重建、is_favorite 按请求体增删收藏）。回传它们才不会被清空。
    "reminders": [{"reminder": "2026-07-12T09:00:00Z"}],
    "is_favorite": True,
    "assignees": [{"id": 5, "username": "marshall"}],
    "labels": [{"id": 2, "title": "重要"}],
    # 服务端自管的字段，不该被回传
    "created": "2026-07-01T00:00:00Z",
    "updated": "2026-07-01T00:00:00Z",
    "index": 42,
}


class _RecordingClient(VikunjaClient):
    """把 HTTP 层换成录音机：断言的是**实际发出的请求体**，不是组装它的中间变量。"""

    def __init__(self, task=None):
        super().__init__("https://todo.example.com", "token")
        self.task = deepcopy(task if task is not None else SAMPLE_TASK)
        self.calls = []

    async def _request(self, method, path, *, params=None, json=None):
        self.calls.append((method, path, deepcopy(json)))
        if method == "GET":
            return deepcopy(self.task), {}
        return {**deepcopy(self.task), **(json or {})}, {}

    def bodies(self, method):
        return [body for called_method, _, body in self.calls if called_method == method]

    def posted_bodies(self):
        return self.bodies("POST")

    def get_count(self):
        return sum(1 for method, _, _ in self.calls if method == "GET")


SAMPLE_PROJECT = {
    "id": 3,
    "title": "SMX",
    "description": "半年计划",
    "identifier": "SMX",
    "hex_color": "ff0000",
    "position": 12.5,
    "is_archived": False,
    "is_favorite": True,
    "parent_project_id": 2,
    # 下面这些是服务端自管或纯派生的字段，回传过去只会有风险（views 是嵌套列表、owner 是对象）
    "owner": {"id": 1, "username": "marshall"},
    "views": [{"id": 9, "title": "List"}],
    "created": "2026-07-01T00:00:00Z",
    "updated": "2026-07-01T00:00:00Z",
    "max_permission": 2,
}


class NormalizeUrlTests(unittest.TestCase):
    def test_normalizes_site_url(self):
        self.assertEqual(normalize_api_url("https://todo.example.com/"), "https://todo.example.com/api/v1")

    def test_preserves_api_url(self):
        self.assertEqual(normalize_api_url("https://todo.example.com/api/v1"), "https://todo.example.com/api/v1")

    def test_rejects_relative_url(self):
        with self.assertRaises(ValueError):
            normalize_api_url("todo.example.com")


class MergeUpdateTaskTests(unittest.IsolatedAsyncioTestCase):
    """``POST /tasks/{id}`` 把请求体当作任务的**新状态**，所以必须原样回传读到的整个对象。

    省略哪些字段会被清空、哪些会被保留，各版本 Vikunja 不一样（见 ``vikunja.py`` 里的说明）；
    回传整个对象是唯一在两代服务端都正确的做法。这是"永远不再发部分请求体"的回归防线：
    任何人把 ``merge_update_task`` 改回直接透传 ``changes``，下面的断言会立刻失败。
    """

    async def test_payload_carries_every_writable_field(self):
        client = _RecordingClient()
        await client.merge_update_task(7, {"priority": 5})
        (body,) = client.posted_bodies()
        for field in TASK_WRITABLE_FIELDS:
            self.assertIn(field, body, f"请求体缺少 {field}，服务端会把它写成零值")
        self.assertEqual(body["title"], "修改论文引言")
        self.assertEqual(body["due_date"], "2026-07-12T10:00:00Z")
        self.assertEqual(body["description"], "设计稿在 FluidCapsule 里")

    async def test_change_wins_over_the_stored_value(self):
        client = _RecordingClient()
        await client.merge_update_task(7, {"priority": 5, "title": "新标题"})
        (body,) = client.posted_bodies()
        self.assertEqual(body["priority"], 5)
        self.assertEqual(body["title"], "新标题")
        self.assertEqual(body["due_date"], "2026-07-12T10:00:00Z")

    async def test_side_effect_fields_are_echoed_back(self):
        """不回传这几个字段就等于清空它们——只改标题也会顺手毁掉任务的元数据。

        它们都是 ``xorm:"-"``，不在任何"可写列"清单里，很容易漏掉，而服务端对它们全是无条件
        生效的破坏性操作：``updateReminders`` 先删光再重建、``updateTaskAssignees`` 拿到空切片
        就 ``DELETE``、``is_favorite`` 与库里 diff 后取消收藏。所以这是数据丢失的防线，
        不是格式洁癖——示例值也特意都非空，免得断言退化成"空 == 空"。
        """
        client = _RecordingClient()
        await client.merge_update_task(7, {"title": "只改标题"})
        (body,) = client.posted_bodies()
        self.assertEqual(body["reminders"], SAMPLE_TASK["reminders"])
        self.assertEqual(body["assignees"], SAMPLE_TASK["assignees"])
        self.assertIs(body["is_favorite"], True)
        self.assertEqual(body["labels"], SAMPLE_TASK["labels"])

    async def test_server_managed_fields_are_not_echoed_back(self):
        client = _RecordingClient()
        await client.merge_update_task(7, {"title": "只改标题"})
        (body,) = client.posted_bodies()
        for field in ("id", "created", "updated", "index"):
            self.assertNotIn(field, body)

    async def test_project_id_is_omitted_so_the_task_does_not_move(self):
        """服务端不带 project_id 即表示"不移动"；回传读到的旧值会在并发移动时把它挪回去。"""
        client = _RecordingClient()
        await client.merge_update_task(7, {"title": "只改标题"})
        (body,) = client.posted_bodies()
        self.assertNotIn("project_id", body)

    async def test_exclusions_and_writable_fields_do_not_overlap(self):
        """两套清单撞车就会自相矛盾：一个要求回传、一个要求丢掉。"""
        self.assertEqual(set(TASK_WRITABLE_FIELDS) & TASK_ECHO_EXCLUSIONS, set())

    async def test_explicit_project_id_is_forwarded(self):
        client = _RecordingClient()
        await client.merge_update_task(7, {"project_id": 9})
        (body,) = client.posted_bodies()
        self.assertEqual(body["project_id"], 9)

    async def test_null_clears_a_field(self):
        client = _RecordingClient()
        await client.merge_update_task(7, {"due_date": None})
        (body,) = client.posted_bodies()
        self.assertIsNone(body["due_date"])
        self.assertEqual(body["title"], "修改论文引言")

    async def test_empty_title_is_rejected_before_any_write(self):
        client = _RecordingClient({"id": 7, "title": "   ", "project_id": 3})
        with self.assertRaises(VikunjaError):
            await client.merge_update_task(7, {"priority": 1})
        self.assertEqual(client.posted_bodies(), [])

    async def test_preloaded_task_skips_the_extra_get(self):
        client = _RecordingClient()
        await client.merge_update_task(7, {"priority": 1}, SAMPLE_TASK)
        self.assertEqual(client.get_count(), 0)
        self.assertEqual(len(client.posted_bodies()), 1)


class CompleteAndReopenTests(unittest.IsolatedAsyncioTestCase):
    """完成/撤销完成必须走合并写——只发 {"done": true} 会连带清空标题。"""

    async def test_complete_task_sends_the_full_object(self):
        client = _RecordingClient()
        await client.complete_task(7)
        (body,) = client.posted_bodies()
        self.assertTrue(body["done"])
        self.assertEqual(body["title"], "修改论文引言")
        self.assertEqual(body["due_date"], "2026-07-12T10:00:00Z")

    async def test_complete_task_reads_the_task_only_once(self):
        client = _RecordingClient()
        await client.complete_task(7)
        self.assertEqual(client.get_count(), 1)

    async def test_reopen_task_sets_done_false(self):
        client = _RecordingClient({**SAMPLE_TASK, "done": True})
        await client.reopen_task(7)
        (body,) = client.posted_bodies()
        self.assertFalse(body["done"])
        self.assertEqual(body["title"], "修改论文引言")

    async def test_project_guard_rejects_a_foreign_task(self):
        client = _RecordingClient()
        with self.assertRaises(VikunjaError):
            await client.complete_task(7, project_id=99)
        self.assertEqual(client.posted_bodies(), [])

    async def test_project_guard_accepts_the_matching_project(self):
        client = _RecordingClient()
        await client.complete_task(7, project_id=3)
        self.assertEqual(len(client.posted_bodies()), 1)


class ProjectWriteTests(unittest.IsolatedAsyncioTestCase):
    """项目写入口径比任务窄，但坑是同一种：裸体 ``{"title": ...}`` 会清掉其余列。"""

    async def test_create_project_sends_only_the_title(self):
        """建项目走 ``PUT /projects``（v1 里创建是 PUT，更新才是 POST）。"""
        client = _RecordingClient()
        await client.create_project("reading-list")
        method, path, _ = client.calls[-1]
        self.assertEqual((method, path), ("PUT", "projects"))
        (body,) = client.bodies("PUT")
        self.assertEqual(body, {"title": "reading-list"})

    async def test_create_project_with_parent(self):
        client = _RecordingClient()
        await client.create_project("paper", parent_id=3)
        (body,) = client.bodies("PUT")
        self.assertEqual(body["parent_project_id"], 3)

    async def test_create_project_rejects_an_empty_title(self):
        client = _RecordingClient()
        with self.assertRaises(VikunjaError):
            await client.create_project("   ")
        self.assertEqual(client.calls, [])

    async def test_rename_echoes_the_whole_writable_field_set(self):
        client = _RecordingClient(SAMPLE_PROJECT)
        await client.merge_update_project(3, {"title": "SMX-paper"})
        (body,) = client.posted_bodies()
        self.assertEqual(body["title"], "SMX-paper")
        for field in PROJECT_ECHO_FIELDS:
            self.assertIn(field, body, f"请求体缺少 {field}，服务端会把它写成零值")

    async def test_rename_does_not_echo_server_managed_fields(self):
        client = _RecordingClient(SAMPLE_PROJECT)
        await client.merge_update_project(3, {"title": "SMX-paper"})
        (body,) = client.posted_bodies()
        for field in ("id", "owner", "views", "created", "updated", "max_permission"):
            self.assertNotIn(field, body)

    async def test_parent_is_left_alone_unless_explicitly_sent(self):
        """服务端把 ``parent_project_id`` 当指针：不发 = 不动，发 0 才是提升到顶层。"""
        client = _RecordingClient(SAMPLE_PROJECT)
        await client.merge_update_project(3, {"title": "SMX-paper"})
        (body,) = client.posted_bodies()
        self.assertNotIn("parent_project_id", body)

    async def test_explicit_parent_is_forwarded(self):
        client = _RecordingClient(SAMPLE_PROJECT)
        await client.merge_update_project(3, {"parent_project_id": 7})
        (body,) = client.posted_bodies()
        self.assertEqual(body["parent_project_id"], 7)

    async def test_empty_description_is_not_sent(self):
        """服务端只在 description 非空时才更新它，发空串既清不掉又多一次写入。"""
        client = _RecordingClient(SAMPLE_PROJECT)
        await client.merge_update_project(3, {"description": ""})
        (body,) = client.posted_bodies()
        self.assertNotIn("description", body)

    async def test_empty_title_is_rejected_before_any_write(self):
        client = _RecordingClient(SAMPLE_PROJECT)
        with self.assertRaises(VikunjaError):
            await client.merge_update_project(3, {"title": "  "})
        self.assertEqual(client.posted_bodies(), [])

    async def test_none_valued_changes_are_dropped(self):
        """``None`` 在项目这边不能当"清空"用（服务端会拒绝或忽略），必须丢掉。"""
        client = _RecordingClient(SAMPLE_PROJECT)
        await client.merge_update_project(3, {"title": "X", "hex_color": None})
        (body,) = client.posted_bodies()
        self.assertNotIn("hex_color", body)


class LabelWriteTests(unittest.IsolatedAsyncioTestCase):
    """标签走独立的一组接口，且 ``PUT /tasks/{id}/labels`` 要的是 ID 不是名字。"""

    async def test_attach_sends_a_label_id(self):
        client = _RecordingClient()
        await client.add_task_label(7, 2)
        method, path, body = client.calls[-1]
        self.assertEqual((method, path), ("PUT", "tasks/7/labels"))
        self.assertEqual(body, {"label_id": 2})

    async def test_detach_puts_the_label_in_the_path(self):
        client = _RecordingClient()
        await client.remove_task_label(7, 2)
        method, path, _ = client.calls[-1]
        self.assertEqual((method, path), ("DELETE", "tasks/7/labels/2"))

    async def test_replace_all_uses_the_bulk_route(self):
        """``bulk`` 的语义是"以这次传的为准"，服务端会摘掉不在列表里的旧标签。"""
        client = _RecordingClient()
        await client.set_task_labels(7, [{"id": 2, "title": "重要"}, {"id": 3, "title": "论文"}])
        method, path, body = client.calls[-1]
        self.assertEqual((method, path), ("POST", "tasks/7/labels/bulk"))
        self.assertEqual(body, {"labels": [{"id": 2}, {"id": 3}]})

    async def test_clearing_all_labels_sends_an_empty_list(self):
        client = _RecordingClient()
        await client.set_task_labels(7, [])
        self.assertEqual(client.calls[-1][2], {"labels": []})

    async def test_create_label_needs_a_title(self):
        client = _RecordingClient()
        with self.assertRaises(VikunjaError):
            await client.create_label("   ")
        self.assertEqual(client.calls, [])

    async def test_create_label_uses_put_on_the_collection(self):
        """v1 里创建标签是 ``PUT /labels``。"""
        client = _RecordingClient()
        await client.create_label("重要", "ff0000")
        method, path, body = client.calls[-1]
        self.assertEqual((method, path), ("PUT", "labels"))
        self.assertEqual(body, {"title": "重要", "hex_color": "ff0000"})

    async def test_create_label_omits_an_empty_colour(self):
        client = _RecordingClient()
        await client.create_label("重要")
        self.assertEqual(client.calls[-1][2], {"title": "重要"})


class _PagingClient(VikunjaClient):
    """按页返回数据的假客户端：``total_pages`` 由页数决定，用来验证翻页循环。"""

    def __init__(self, pages, total_pages=None):
        super().__init__("https://todo.example.com", "token")
        self.pages = pages
        self.total_pages = total_pages if total_pages is not None else len(pages)
        self.requests = []

    async def _request(self, method, path, *, params=None, json=None):
        # 存原始列表而不是 dict：``sort_by`` 要出现两次，转 dict 会把重复键吃掉，
        # 于是"重复键有没有丢"这条断言就会永远通过。
        self.requests.append((path, list(params or [])))
        page = int(dict(params or []).get("page", 1))
        return deepcopy(self.pages[page - 1]), {"x-pagination-total-pages": str(self.total_pages)}

    def param(self, index, name):
        return dict(self.requests[index][1]).get(name)

    def param_count(self, index, name):
        return [key for key, _ in self.requests[index][1]].count(name)


class PaginationTests(unittest.IsolatedAsyncioTestCase):
    """``per_page`` 的上限是服务端的 ``maxitemsperpage``（默认 50），单页请求就是静默丢数据。"""

    async def test_all_pages_are_followed(self):
        client = _PagingClient([[{"id": 1}], [{"id": 2}], [{"id": 3}]])
        tasks = await client.list_tasks()
        self.assertEqual([task["id"] for task in tasks], [1, 2, 3])
        self.assertEqual([client.param(index, "page") for index in range(3)], ["1", "2", "3"])

    async def test_a_short_page_ends_the_loop(self):
        """服务端少报总页数时不能空转——返回空页同样是终止条件。"""
        client = _PagingClient([[{"id": 1}], []], total_pages=5)
        self.assertEqual([task["id"] for task in await client.list_tasks()], [1])

    async def test_a_single_page_needs_exactly_one_request(self):
        client = _PagingClient([[{"id": 1}]], total_pages=1)
        await client.list_tasks()
        self.assertEqual(len(client.requests), 1)

    async def test_duplicate_sort_keys_are_sent_as_a_list_not_a_dict(self):
        """``sort_by`` 要出现两次（优先级、截止时间）。用 dict 传参只会剩下最后一个。"""
        client = _PagingClient([[{"id": 1}]], total_pages=1)
        await client.list_tasks()
        self.assertEqual(client.param_count(0, "sort_by"), 2)

    async def test_non_list_payload_is_reported(self):
        client = _PagingClient([{"not": "a list"}], total_pages=1)
        with self.assertRaisesRegex(VikunjaError, "任务列表"):
            await client.list_tasks()

    async def test_archived_projects_are_dropped_across_pages(self):
        client = _PagingClient(
            [[{"id": 1, "is_archived": True}], [{"id": 2, "is_archived": False}]]
        )
        self.assertEqual([project["id"] for project in await client.list_projects()], [2])


class CompletedSinceTests(unittest.IsolatedAsyncioTestCase):
    async def test_filters_on_done_at_not_updated(self):
        """``updated`` 改个标题就会刷新，拿它当完成时间会把旧任务算进本周。"""
        client = _PagingClient([[{"id": 1, "done_at": "2026-07-11T02:00:00Z"}]], total_pages=1)
        await client.list_completed_since(7)
        filters = client.param(0, "filter")
        self.assertIn("done = true", filters)
        self.assertIn("done_at > now-7d", filters)
        self.assertNotIn("updated", filters)

    async def test_window_follows_the_days_argument(self):
        client = _PagingClient([[{"id": 1}]], total_pages=1)
        await client.list_completed_since(30)
        self.assertIn("now-30d", client.param(0, "filter"))

    async def test_sorted_by_completion_time_newest_first(self):
        client = _PagingClient([[{"id": 1}]], total_pages=1)
        await client.list_completed_since()
        self.assertEqual(client.param(0, "sort_by"), "done_at")
        self.assertEqual(client.param(0, "order_by"), "desc")

    async def test_paginates_so_a_busy_week_is_not_truncated(self):
        client = _PagingClient([[{"id": 1}], [{"id": 2}]])
        self.assertEqual(len(await client.list_completed_since()), 2)


class RelationTests(unittest.IsolatedAsyncioTestCase):
    async def test_add_subtask_puts_the_child_under_the_parent(self):
        """方向容易反：父任务在 URL 上，子任务在 ``other_task_id`` 里。"""
        client = _RecordingClient()
        await client.add_relation(7, 8, "subtask")
        method, path, body = client.calls[-1]
        self.assertEqual(method, "PUT")
        self.assertEqual(path, "tasks/7/relations")
        self.assertEqual(body, {"other_task_id": 8, "relation_kind": "subtask"})

    async def test_remove_relation_uses_the_delete_route(self):
        client = _RecordingClient()
        await client.remove_relation(7, 8, "subtask")
        method, path, _ = client.calls[-1]
        self.assertEqual(method, "DELETE")
        self.assertEqual(path, "tasks/7/relations/subtask/8")


if __name__ == "__main__":
    unittest.main()
