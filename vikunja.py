"""Small asynchronous client for the Vikunja v2.3 API."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

try:
    import aiohttp
except ModuleNotFoundError:  # Allows schema/unit tests before plugin dependencies are installed.
    aiohttp = None  # type: ignore[assignment]


class VikunjaError(RuntimeError):
    """A user-presentable Vikunja API error."""


# Vikunja 的 ``POST /tasks/{id}`` 把请求体当作**任务的新状态**，不是部分更新。而"没出现的字段
# 怎么办"的答案是**混合语义**，既不是"一律保留"也不是"一律清空"：
#
# 服务端（``pkg/web/handler/update.go`` 的 ``UpdateWeb``）把请求体绑定到 ``EmptyStruct()``——
# 一个**全新的空 Task**，不是从库里读出来的对象，所以缺席字段就是 Go 零值。随后
# ``Task.Update()`` → ``updateSingleTask(s, a, nil)``（``fields`` 为 nil）里依次发生三件事：
#
#   1. ``if len(fields) > 0`` 那段"按字段名保留省略值"（``t.Title = ot.Title`` …）**整段跳过**
#      ——它是给 v2 的按字段更新路径用的，``fields == nil`` 时帮不上忙。
#   2. ``mergo.Merge(&ot, t, mergo.WithOverride)``：mergo 忽略零值，所以 ``t`` 里空着的字段
#      **不会**盖掉 ``ot`` 的旧值。空 title 因此得以保留（用户实测：裸体 ``{"done": true}``
#      之后标题还在——这不是"全量替换"，是 mergo 在兜底）。
#   3. 紧接着一段**显式清零**，逐个推翻 mergo 的兜底：
#      ``if !t.Done { ot.Done = false }``、``if t.Priority == 0 { ot.Priority = 0 }``、
#      ``if t.Description == "" {...}``、``if t.DueDate.IsZero() {...}``、
#      ``if !t.IsFavorite { ot.IsFavorite = false }``、start/end/hex_color/percent_done/
#      repeat_after/repeat_mode 同理。→ **这些字段是"不传就清空"的。**
#
# 于是裸体 ``{"done": true}`` 的真实后果是：标题活着，但**截止时间、描述、优先级被清空，
# 收藏被取消**。这正是"完成一条待办顺手毁掉它的元数据"，也是本文件必须走合并写的直接原因。
#
# 另外几个 ``xorm:"-"`` 字段在更新路径上是**无条件**生效的，跟 ``fields`` 无关：
#   - ``ot.updateReminders(s, t)``：先把该任务的 ``TaskReminder`` 全部删光，再从请求体
#     (``t.Reminders``) 重建——**没有 nil 守卫**。不传 = 原生提醒全删。
#   - ``ot.updateTaskAssignees(s, t.Assignees, a)``（``updateSingleTask`` 开头）：传空切片
#     就直接 ``DELETE FROM task_assignees``。不传 = 指派全删。
#   - ``is_favorite``：与库里状态 diff 后增删收藏。不传（零值 false）= 取消收藏。
#   - ``labels``：上游已把这段注释停用，更新路径不动标签；回传它只是无害的冗余。
# 它们不是列，所以不出现在任何"可写字段"清单里，但**不传就等于清空**——只改个标题也会顺手
# 删掉你在网页端设的原生提醒、指派和收藏。**注意前端自己的 ``writableFields`` 列表比这窄**
# （不含 ``assignees``/``labels``），照着它裁剪就正好会踩中 assignees 这个坑。
#
# 唯一在所有这些语义下都正确的做法，也正是 Vikunja 网页端自己在做的：**把读到的整个任务
# 对象原样 POST 回去，再叠加改动**。写入一律走 ``merge_update_task()``。
TASK_WRITABLE_FIELDS = (
    "title",
    "description",
    "done",
    "due_date",
    "priority",
    "start_date",
    "end_date",
    "repeat_after",
    "repeat_mode",
    "percent_done",
    "hex_color",
    "bucket_id",
)

# 回传时排除的字段，两类理由：
# - 服务端自管：``id`` 由 URL 路径参数决定；``created``/``updated``/``created_by`` 是审计字段；
#   ``index``/``position`` 是看板排序，移动任务时由服务端重新计算。回传它们没有意义，还可能
#   把服务端刚算好的顺序顶掉。
# - ``project_id``：只有调用方显式传入才代表"移动任务"。回传读到的旧值会在"网页端刚把它挪走"
#   这种并发场景下把它挪回来（服务端语义里不带该键天然表示不移动）。
TASK_ECHO_EXCLUSIONS = frozenset(
    {"id", "created", "updated", "created_by", "index", "position", "project_id"}
)

# 项目走的是**白名单**回传，而不是任务那样的"整对象减排除"。理由：``UpdateProject``
# （``pkg/models/project.go``）从请求体里读的字段是可枚举完的——``colsToUpdate`` 里的
# ``title``/``is_archived``/``identifier``/``hex_color``/``position``、非空才写的
# ``description``、以及和库里 diff 的 ``is_favorite``。这七个之外服务端根本不看。
# 任务那边必须整对象回传，是因为 ``xorm:"-"`` 字段有隐藏副作用、漏一个就丢数据；项目这边
# 读入口径是封闭的，反过来把 ``views``（嵌套视图列表）、``owner`` 这类对象回传过去才是风险。
# 另外 ``parent_project_id`` 是 ``*int64``：服务端只在它非 nil 时才写（注释明说"omit it on
# a write to leave the parent unchanged"），缺席天然等于不动，显式 0 才是提升到顶层。
PROJECT_ECHO_FIELDS = (
    "title",
    "description",
    "identifier",
    "hex_color",
    "position",
    "is_archived",
    "is_favorite",
)


def normalize_api_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        return ""
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("Vikunja 地址必须是完整的 http(s) URL")
    path = parts.path.rstrip("/")
    if not path.endswith("/api/v1"):
        path += "/api/v1"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


class VikunjaClient:
    def __init__(self, base_url: str, token: str, timeout: int = 15):
        self.base_url = normalize_api_url(base_url)
        self.token = token.strip()
        self.timeout = max(1, int(timeout))
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        if aiohttp is None:
            raise VikunjaError("缺少 aiohttp 依赖，请安装插件 requirements.txt")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/json",
                },
            )
        return self._session

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: list[tuple[str, str]] | dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> tuple[Any, aiohttp.typedefs.LooseHeaders]:
        if not self.base_url or not self.token:
            raise VikunjaError("插件尚未配置 Vikunja 地址和 API Token")
        session = await self._get_session()
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            async with session.request(method, url, params=params, json=json) as response:
                try:
                    payload = await response.json(content_type=None)
                except Exception:
                    payload = {"message": (await response.text())[:500]}
                if response.status < 200 or response.status >= 300:
                    message = payload.get("message") if isinstance(payload, dict) else str(payload)
                    raise VikunjaError(f"Vikunja 请求失败（HTTP {response.status}）：{message or '未知错误'}")
                return payload, response.headers
        except VikunjaError:
            raise
        except ((aiohttp.ClientError if aiohttp else OSError), TimeoutError) as exc:
            raise VikunjaError(f"无法连接 Vikunja：{exc}") from exc

    async def get_project(self, project_id: int) -> dict[str, Any]:
        payload, _ = await self._request("GET", f"projects/{project_id}")
        return payload

    async def _paged_get(
        self, path: str, base_params: list[tuple[str, str]], what: str
    ) -> list[dict[str, Any]]:
        """按 ``x-pagination-total-pages`` 翻完所有页。

        必须翻页而不是把 ``per_page`` 开大：``per_page`` 的上限是服务端的
        ``maxitemsperpage``（默认 50），且**只在 web handler 里被夹紧**，传更大的值不会报错、
        只会被悄悄改小——所以单页请求就是静默丢数据。
        """
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            payload, headers = await self._request(
                "GET", path, params=[*base_params, ("page", str(page))]
            )
            if not isinstance(payload, list):
                raise VikunjaError(f"Vikunja 返回了无法识别的{what}")
            items.extend(payload)
            total_pages = int(headers.get("x-pagination-total-pages", page) or page)
            if page >= total_pages or not payload:
                break
            page += 1
        return items

    async def list_projects(self) -> list[dict[str, Any]]:
        projects = await self._paged_get("projects", [("per_page", "100")], "项目列表")
        return [project for project in projects if not project.get("is_archived")]

    async def create_task(self, project_id: int, task: dict[str, Any]) -> dict[str, Any]:
        payload, _ = await self._request("PUT", f"projects/{project_id}/tasks", json=task)
        return payload

    async def get_task(self, task_id: int) -> dict[str, Any]:
        payload, _ = await self._request("GET", f"tasks/{task_id}")
        return payload

    async def update_task(self, task_id: int, changes: dict[str, Any]) -> dict[str, Any]:
        """直接 POST 请求体。**部分请求体会清空其余字段**——业务代码请改用 ``merge_update_task``。"""
        payload, _ = await self._request("POST", f"tasks/{task_id}", json=changes)
        return payload

    async def merge_update_task(
        self,
        task_id: int,
        changes: dict[str, Any],
        current: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """读-改-写：GET 当前任务，原样回传整个对象并叠加 ``changes``。

        这是修改任务的唯一安全入口，理由见 ``TASK_WRITABLE_FIELDS`` 上方的说明：请求体里
        缺席的字段（包括 ``reminders``/``is_favorite`` 这些带副作用的 ``xorm:"-"`` 字段）
        会被服务端当成"新状态"，也就是清空。回传读到的值才等价于"什么都没改"。

        ``changes`` 里值为 ``None`` 的键表示**清空**该字段（例如 ``due_date=None`` 去掉截止
        时间）——它们必须照发，不能跳过。

        ``current`` 可传入调用方刚读到的任务，省掉一次重复 GET。
        """
        task = current if current is not None else await self.get_task(task_id)
        payload: dict[str, Any] = {
            key: value for key, value in task.items() if key not in TASK_ECHO_EXCLUSIONS
        }
        # 固定形状保证：GET 万一少返回某个可写字段，也要显式补成 None 而不是让它缺席
        # ——缺席会被服务端写成零值，补成 None 至少语义一致。
        for field in TASK_WRITABLE_FIELDS:
            payload.setdefault(field, task.get(field))
        payload.update(changes)
        if not str(payload.get("title") or "").strip():
            raise VikunjaError("任务标题不能为空")
        return await self.update_task(task_id, payload)

    async def delete_task(self, task_id: int) -> None:
        await self._request("DELETE", f"tasks/{task_id}")

    async def _task_in_project(self, task_id: int, project_id: int | None) -> dict[str, Any]:
        current = await self.get_task(task_id)
        if project_id is not None and int(current.get("project_id", 0)) != int(project_id):
            raise VikunjaError("该任务不属于你绑定的项目")
        return current

    async def complete_task(self, task_id: int, project_id: int | None = None) -> dict[str, Any]:
        current = await self._task_in_project(task_id, project_id)
        return await self.merge_update_task(task_id, {"done": True}, current)

    async def reopen_task(self, task_id: int, project_id: int | None = None) -> dict[str, Any]:
        """撤销完成——误勾选后回到未完成。"""
        current = await self._task_in_project(task_id, project_id)
        return await self.merge_update_task(task_id, {"done": False}, current)

    async def create_project(
        self, title: str, parent_id: int | None = None, description: str = ""
    ) -> dict[str, Any]:
        """新建项目。``parent_id`` 为空则建在顶层。"""
        title = title.strip()
        if not title:
            raise VikunjaError("项目名称不能为空")
        body: dict[str, Any] = {"title": title}
        if description.strip():
            body["description"] = description.strip()
        if parent_id:
            body["parent_project_id"] = int(parent_id)
        payload, _ = await self._request("PUT", "projects", json=body)
        return payload

    async def merge_update_project(
        self,
        project_id: int,
        changes: dict[str, Any],
        current: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """读-改-写项目，理由见 ``PROJECT_ECHO_FIELDS`` 上方的说明。

        ``title`` 会被服务端无条件按请求体写入，所以只发 ``{"title": ...}`` 的裸体会把
        ``identifier``/``hex_color``/``position`` 和归档状态一起清成零值——和任务一样的坑，
        只是字段集合小了。这里回传白名单里的全部字段，再叠加 ``changes``。
        """
        project = current if current is not None else await self.get_project(project_id)
        payload: dict[str, Any] = {field: project.get(field) for field in PROJECT_ECHO_FIELDS}
        payload.update(changes)
        if not str(payload.get("title") or "").strip():
            raise VikunjaError("项目名称不能为空")
        body = {key: value for key, value in payload.items() if value is not None}
        # 服务端只在 description 非空时才把它算进 colsToUpdate，空串发过去既清不掉，也白白
        # 扩大请求体；干脆不发。要清空项目描述得去网页端（本插件不提供这个操作）。
        if not str(body.get("description") or "").strip():
            body.pop("description", None)
        payload_result, _ = await self._request("POST", f"projects/{project_id}", json=body)
        return payload_result

    async def add_relation(
        self, task_id: int, other_task_id: int, relation_kind: str = "subtask"
    ) -> None:
        """建立任务关系，默认让 ``other_task_id`` 成为 ``task_id`` 的子任务。

        服务端会自动补上反向关系（``parenttask``），不要自己再发一遍。
        """
        await self._request(
            "PUT",
            f"tasks/{task_id}/relations",
            json={"other_task_id": int(other_task_id), "relation_kind": relation_kind},
        )

    async def remove_relation(
        self, task_id: int, other_task_id: int, relation_kind: str = "subtask"
    ) -> None:
        """删除任务关系。不存在的关系统一是 404，调用方自行决定要不要容忍。"""
        await self._request(
            "DELETE", f"tasks/{task_id}/relations/{relation_kind}/{int(other_task_id)}"
        )

    async def list_labels(self) -> list[dict[str, Any]]:
        """列出当前用户可见的标签（自己建的，或挂在可读任务上的）。"""
        payload, _ = await self._request("GET", "labels", params=[("per_page", "100")])
        return payload if isinstance(payload, list) else []

    async def create_label(self, title: str, hex_color: str = "") -> dict[str, Any]:
        """新建标签。Vikunja 的 ``labels`` **没有**标题唯一约束，重名标签是允许的。"""
        title = title.strip()
        if not title:
            raise VikunjaError("标签名不能为空")
        body: dict[str, Any] = {"title": title}
        if hex_color.strip():
            body["hex_color"] = hex_color.strip()
        payload, _ = await self._request("PUT", "labels", json=body)
        return payload

    async def add_task_label(self, task_id: int, label_id: int) -> None:
        """给任务挂一个标签。

        ``PUT /tasks/{id}/labels`` 要的是**标签 ID**（不是名字），而且服务端在"这个标签已经
        挂在该任务上"时会返回错误而不是静默成功，所以调用方必须先比对现有标签。
        """
        await self._request(
            "PUT", f"tasks/{task_id}/labels", json={"label_id": int(label_id)}
        )

    async def remove_task_label(self, task_id: int, label_id: int) -> None:
        """摘掉任务上的一个标签。标签本来就不在时是空操作（服务端删 0 行不报错）。"""
        await self._request("DELETE", f"tasks/{task_id}/labels/{int(label_id)}")

    async def set_task_labels(self, task_id: int, labels: list[dict[str, Any]]) -> None:
        """把任务的标签整体设成 ``labels``（其余全部摘掉）。

        走 ``POST /tasks/{id}/labels/bulk``：服务端对这个接口的语义就是"以这次传的为准"，
        不在列表里的旧标签会被删掉。传空列表即清空标签。只发 ``id`` 就够了——服务端只按
        ID 比对，回传整个标签对象没有意义。
        """
        await self._request(
            "POST",
            f"tasks/{task_id}/labels/bulk",
            json={"labels": [{"id": int(label["id"])} for label in labels]},
        )

    async def list_tasks(
        self, project_id: int | None = None, include_done: bool = False
    ) -> list[dict[str, Any]]:
        filters: list[str] = []
        if project_id is not None:
            filters.append(f"project_id = {int(project_id)}")
        if not include_done:
            filters.append("done = false")
        base_params = [
            ("per_page", "100"),
            ("sort_by", "priority"),
            ("order_by", "desc"),
            ("sort_by", "due_date"),
            ("order_by", "asc"),
        ]
        if filters:
            base_params.append(("filter", " && ".join(filters)))
        return await self._paged_get("tasks", base_params, "任务列表")

    async def list_completed_since(self, days: int = 7) -> list[dict[str, Any]]:
        """取最近 ``days`` 天内完成的任务，跨项目，按完成时间倒序。

        ``done_at`` 是真实的索引列（``tasks.go``），而且 ``done_at > now-7d`` 正是 Vikunja
        自带过滤器测试里的用例（``task_collection_filter_test.go``），所以筛选交给服务端。
        返回结果里仍可能有 ``done_at`` 为零值的行，调用方需再过一遍
        ``todo_domain.completed_tasks_since``。
        """
        base_params = [
            ("per_page", "100"),
            ("sort_by", "done_at"),
            ("order_by", "desc"),
            ("filter", f"done = true && done_at > now-{int(days)}d"),
        ]
        return await self._paged_get("tasks", base_params, "任务列表")
