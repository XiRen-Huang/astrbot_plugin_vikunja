"""AstrBot plugin entry point for a single-user Vikunja secretary."""

from __future__ import annotations

import asyncio
import inspect
import shlex
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

from .push_channels import AstrBotChannel, build_channels, format_push_status
from .state_store import StateStore
from .todo_domain import (
    AddSpec,
    BulkSpec,
    EditSpec,
    LabelChange,
    LabelSpec,
    ProjectSpec,
    SubtaskSpec,
    build_label_index,
    build_project_paths,
    completed_tasks_since,
    filter_tasks_by_label,
    filter_tasks_by_query,
    format_label_list,
    format_project_tree,
    format_subtask_list,
    format_task_list,
    format_weekly_report,
    from_vikunja_time,
    get_timezone,
    is_clear_request,
    label_cache_key,
    label_change_from_spec,
    next_poll_delay,
    parse_add_arguments,
    parse_bulk_arguments,
    parse_datetime,
    parse_edit_arguments,
    parse_label_arguments,
    parse_label_changes,
    parse_move_arguments,
    parse_project_arguments,
    parse_repeat,
    parse_subtask_arguments,
    parse_task_ids,
    parse_tristate_bool,
    parents_of,
    platform_sender_is_allowed,
    resolve_project,
    secretary_clarification_reason,
    select_tasks,
    subtasks_of,
    task_label_titles,
    task_sort_key,
    to_vikunja_time,
)
from .tool_schema import parser_mismatch_warnings, schema_warnings_for_methods
from .vikunja import VikunjaClient, VikunjaError


HELP_TEXT = """Vikunja 私人待办秘书（仅支持私聊）
/todo projects                         查看完整项目树
/todo add <标题> [--project 项目路径] [--due 时间] [--priority 0-5]
               [--repeat 规则] [--remind 30m] [--desc 描述]
/todo edit <任务ID> [--title 标题] [--due 时间] [--priority 0-5]
               [--repeat 规则|none] [--project 项目路径]
               [--remind 30m|clear] [--desc 描述] [--label 标签]
/todo done <任务ID>                    完成任务
/todo reopen <任务ID>                  撤销完成，回到未完成
/todo today                            所有项目的今日及逾期待办
/todo list [all|week|overdue] [--project 项目路径]
/todo search <关键词> [--done]         搜索标题与描述，加 --done 连已完成一起搜
/todo move <任务ID> <项目路径>         把任务移到另一个项目
/todo subtask add <父任务ID> <标题>    新建子任务
/todo subtask list <任务ID>            查看子任务
/todo subtask rm <父任务ID> <子任务ID> 摘掉子任务关系（不删任务）
/todo bulk done|reopen <任务ID,...>    批量完成 / 批量撤销完成
/todo bulk pri <0-5> <任务ID,...>      批量改优先级
/todo bulk delete <任务ID,...>         批量删除
/todo project new <名称> [--parent 父项目]   新建项目
/todo project rename <项目> <新名称>         重命名项目
/todo label list                       列出所有标签
/todo label add <任务ID,...> <标签,...>      加标签（不存在的标签自动新建）
/todo label rm <任务ID,...> <标签,...>       摘标签（不会新建）
/todo label set <任务ID,...> <标签,...>      把标签整体换成这几个；配“清空”摘掉全部
/todo week                             最近 7 天完成了哪些任务（按项目汇总）
/todo remind <on|off>                  开关当前 QQ/微信入口的提醒
/todo push status                      查看推送通道与各入口的提醒状态
/todo push test                        往所有启用的入口真发一条测试消息

--repeat 规则：daily/每天、weekly/每周、monthly/每月、2d、12h、每3天、完成后2d
               （完成后N 从你勾选完成那一刻起算，默认从截止时间起算）。
               Vikunja 只能表达“每隔 N 秒”和“每月同一天”，所以“每周一/三/五”这类
               指定星期几的规则表达不了，插件会明确报错而不是拿个近似的值糊弄过去。

例：
/todo add 买牛奶 --due "明天 18:00"
/todo add 修改论文引言 --project "fudan-work/SMX/paper" --due "周日 20:00" --priority 4
/todo add 每日复盘 --project personal-project --due "今天 22:00" --repeat daily
/todo edit 12 --due "周五 20:00"        改截止时间
/todo edit 12 --desc ""                 清空描述（空值即清空）
/todo edit 12 --repeat none             停止重复
/todo edit 12 --remind clear            恢复全局默认提醒提前量
/todo search 论文                       搜标题和描述
/todo bulk done 12,15,20                一次完成三条
/todo move 12 "fudan-work/SMX/paper"    换个项目
/todo subtask add 7 写引言              给 #7 加一条子任务
/todo edit 12 --label +重要,-待定       加“重要”、摘“待定”
/todo label add 12 论文                给 #12 加标签，标签不存在会自动建
/todo push test                         确认提醒通道是通的"""


SECRETARY_PROMPT = """
你可以使用 Vikunja 工具充当用户的私人待办秘书。遵守以下规则：
1. 这是单用户系统，QQ 私聊和微信私聊共享同一个 Vikunja 工作空间。
2. 用户要求提醒时，必须确认明确的截止日期和时间；缺少时先询问，不得调用创建工具。
3. 对论文、项目推进、学习计划、习惯等明显长期事项，若缺少截止时间、持续时长或重复频率，先询问确认。
4. 日常琐事可放入 Inbox；工作或项目事项必须选到合适的项目层级。项目不明确时先调用项目树工具，仍有歧义就询问用户。
5. 所有待办的创建、完成、查询操作，只能使用 Vikunja 工具，不得使用其他工具（如 cron、定时任务等）。
6. 当创建工具返回需要补充信息时，你必须先向用户询问确认缺失信息，获取用户回复后，必须用补充完整的信息重新调用同一个创建工具，不得转向其他工具。
7. 当用户说"XX做完了""XX完成了""XX好了""XX搞定""勾掉XX""标记完成""OK了"等表达时，必须执行完成操作：
   - 若用户明确给出了任务 ID（如"2已完成"），直接调用 vikunja_update_task 并传 done="完成"
   - 若用户只说"第二个做完了"，先查询任务列表确认是哪个任务，然后立即调用 vikunja_update_task 并传 done="完成"
   - 不得只查询不操作
8. 当用户要求删除任务且给出了任务 ID，直接调用删除工具；未给出 ID 时先查询定位，再立即调用删除工具，不得只查询不删除。
9. 工具返回的任务列表中，“1.”是本次列表序号，“#123”才是任务 ID。用户说“第一个/1号”且没有明确说“ID”或“#”时，先查询当前列表，将序号换成对应的 #任务ID 后再操作。
10. 只有工具明确返回成功后才能告诉用户已创建、已完成或已删除；工具要求补充信息时，继续询问用户。
11. 用简洁、自然的中文交流，不要求用户记忆斜杠命令。
12. 当用户说"帮我记下""帮我记录""添加待办""新建任务""记一下""备注一下""加一条"等表达时，视作创建任务意图。提取标题、截止时间、项目等信息后调用创建工具。信息不足时先追问再调用。
13. 当用户要求修改已有任务时，调用 vikunja_update_task，**只传用户真正想改的那个参数**，其余参数一律留空——留空表示"保持原值"，传了值就会覆盖。
14. 当用户说"勾错了""撤销完成""还没做完""恢复一下"时，调用 vikunja_update_task 并传 done="撤销完成"。
15. 用户要加备注、记要点、存链接时，用 vikunja_update_task 的 description 参数；不要把备注塞进标题。
16. 绝不能用"删除后重新创建"来替代修改——那会换掉任务 ID。
17. 用户一次点名多条任务（"这几条都完成""1、3、5 都改成 P5"）时，把它们的 ID 用逗号拼成一个 task_ids 传给 vikunja_update_task，一次调用改完；不要拆成多次调用，也不要漏掉其中任何一条。
18. 用户说"分解一下""拆成几步""加个子任务"时，用 vikunja_create_task 的 parent_id 把新任务挂在父任务下；用户说"把它挪到 XX 下面"时用 vikunja_update_task 的 parent_id。父子关系不是删除重建，父任务的 ID 不会变。
19. 用户说"建个项目""开个新项目"时调用 vikunja_manage_project 并传 action="create"；说要给项目改名时传 action="rename"。不要为了新建项目去调用其它工具。
20. 用户找"关于 XX 的任务""有没有提过 XX"时，用 vikunja_list_tasks 的 search 参数（它同时匹配标题和描述）；只有用户找的是已经做完的老任务时才传 include_done=true。
21. 用户说"打个标签""标成 XX""这是 XX 类的"时，用 vikunja_update_task 的 labels 参数并给标签名加 +（如 labels="+重要"）；说"去掉标签""不是 XX 了"时加 -（如 labels="-待定"）；说"标签改成 A 和 B"时不加任何符号（labels="A,B"），那是**整体替换**，原有标签会被摘掉。用户问"哪些任务标了 XX"时用 vikunja_list_tasks 的 label 参数。
22. 标签名写错会自动创建一个新标签，所以加标签时用用户原话里的词，不要自己改写、翻译或补全标签名。
23. 重复规则只能用 daily/weekly/monthly、2d、12h、每3天这类固定间隔，或“完成后2d”（从完成那一刻起算）。用户说“每周一、三、五”“工作日”“每隔一个周五”时，**Vikunja 表达不了**，不要挑一个最像的规则凑上去，要直接告诉用户：可以改成每周固定一天（若截止时间正好是那天，效果相同），或者拆成星期几就建几条任务。
24. 用户说“这周完成了什么”“总结一下最近”“做个周报”“最近干得怎么样”时，调用 vikunja_stats 统计已完成任务；这是回顾已完成的任务，不是查未完成待办，不要用 vikunja_list_tasks 代替。

## 对话示例

用户：帮我记下明天下午3点开会
助手：[直接调用创建工具，title="开会"，due="明天15点"]

用户：今天有什么任务
助手：[调用查询工具，scope="today"]

用户：2已完成
助手：[直接调用 vikunja_update_task，task_ids="2", done="完成"]

用户：把买牛奶的截止时间改到明天下午3点
助手：[调用 vikunja_update_task，task_ids="买牛奶的ID", due="明天15点"，其余参数留空]

用户：勾错了，3号还没做完
助手：[调用 vikunja_update_task，task_ids="3", done="撤销完成"]

用户：给 7 号加上备注：设计稿在 FluidCapsule 里
助手：[调用 vikunja_update_task，task_ids="7", description="设计稿在 FluidCapsule 里"]

用户：1、3、5 都标记完成
助手：[调用 vikunja_update_task，task_ids="1,3,5", done="完成"]

用户：把"写引言"拆成子任务挂到论文那条下面
助手：[调用 vikunja_create_task，title="写引言"，parent_id=论文的任务ID]

用户：有没有关于 FluidCapsule 的任务
助手：[调用查询工具，search="FluidCapsule"]

用户：建个项目叫 reading-list
助手：[调用 vikunja_manage_project，action="create"，name="reading-list"]

用户：给 7 号打个"重要"标签
助手：[调用 vikunja_update_task，task_ids="7", labels="+重要"]

用户：12 号的"待定"标签去掉吧
助手：[调用 vikunja_update_task，task_ids="12", labels="-待定"]

用户：哪些任务是标了"论文"的
助手：[调用查询工具，label="论文"]

用户：这周完成了什么
助手：[调用 vikunja_stats，period="week"]

用户：还有个待办没做
助手：[调用查询工具列出未完成任务]
"""


# LLM 工具方法的命名前缀，供启动自检扫描（装饰器不附加标记属性，只能靠约定识别）。
VIKUNJA_TOOL_PREFIX = "vikunja_"


class PrivateOnlyError(ValueError):
    pass


class VikunjaPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.tz = get_timezone(str(config.get("timezone", "Asia/Shanghai")))
        self.client = VikunjaClient(
            str(config.get("vikunja_url", "")),
            str(config.get("api_token", "")),
            int(config.get("request_timeout_seconds", 15)),
        )
        self.state = StateStore(self._load_state, self._save_state)
        self._ready = False
        self._ready_lock = asyncio.Lock()
        self._reminder_task: asyncio.Task[None] | None = None
        self._channel_failures: dict[str, int] = {}
        # 新建任务后立刻唤醒轮询，避免等满一个长间隔才提醒。
        self._wake = asyncio.Event()
        self._project_cache: (
            tuple[float, list[dict[str, Any]], dict[int, str]] | None
        ) = None

    async def _load_state(self) -> dict[str, Any]:
        return await self.get_kv_data("state_v2", {})

    async def _save_state(self, state: dict[str, Any]) -> None:
        await self.put_kv_data("state_v2", state)

    def _poll_intervals(self) -> tuple[int, int]:
        """返回 (常规轮询间隔, 空闲轮询上限)，单位秒。"""
        poll = max(30, int(self.config.get("poll_interval_seconds", 60)))
        idle = max(poll, int(self.config.get("idle_poll_interval_seconds", 600)))
        return poll, idle

    async def _cached_projects(self) -> tuple[list[dict[str, Any]], dict[int, str]]:
        """提醒循环用的项目快照。

        项目树变动很少，TTL 内复用可以省掉一半的轮询请求。**仅用于提醒的展示路径**；
        创建和查询仍走 ``_resolve_project()`` 取实时数据。
        """
        ttl = max(0, int(self.config.get("project_cache_ttl_seconds", 300)))
        now = time.monotonic()
        if self._project_cache is not None and now - self._project_cache[0] < ttl:
            _, projects, paths = self._project_cache
            return projects, paths
        projects = await self.client.list_projects()
        paths = build_project_paths(projects)
        self._project_cache = (now, projects, paths)
        return projects, paths

    async def _send_text(self, umo: str, text: str) -> Any:
        """往一个会话投递纯文本。注入给推送通道，让 ``push_channels.py`` 不必 import astrbot。"""
        return await self.context.send_message(umo, MessageChain().message(text))

    async def _enabled_push_channels(self) -> list[AstrBotChannel]:
        """当前应当接收提醒的通道（已登记的、提醒开关为开的、且通道种类在配置里启用）。"""
        channels = build_channels(
            self.state.channels(),
            self._send_text,
            enabled_kinds=self.config.get("push_channels"),
        )
        return [channel for channel in channels if channel.is_enabled()]

    async def _ensure_ready(self) -> None:
        if self._ready:
            return
        async with self._ready_lock:
            if not self._ready:
                await self.state.initialize()
                self._ready = True

    def _sender_allowed(self, event: AstrMessageEvent) -> bool:
        return platform_sender_is_allowed(
            event.get_platform_name(),
            str(event.get_sender_id()),
            self.config.get("allowed_qq_sender_ids", []),
        )

    def _assert_private(self, event: AstrMessageEvent) -> None:
        if event.get_group_id():
            raise PrivateOnlyError("Vikunja 私人秘书只支持私聊，不会在群聊中读取或修改任务")
        if not self._sender_allowed(event):
            raise PrivateOnlyError("当前 QQ 发送者不在 Vikunja 私人秘书白名单中")

    async def _register_channel(self, event: AstrMessageEvent) -> str:
        self._assert_private(event)
        await self._ensure_ready()
        key = str(event.unified_msg_origin)
        if not self.state.channel(key):
            await self.state.register_channel(
                key,
                {
                    "umo": key,
                    "sender_id": str(event.get_sender_id()),
                    "reminders_enabled": True,
                },
            )
            # 渠道集合变了，轮询的空闲间隔与去重键都随之改变，立刻重算一次。
            self._wake.set()
        return key

    @staticmethod
    def _command_tail(event: AstrMessageEvent) -> str:
        parts = event.message_str.strip().split(maxsplit=2)
        return parts[2].strip() if len(parts) == 3 else ""

    async def _resolve_project(
        self, selector: str = ""
    ) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
        projects = await self.client.list_projects()
        target = selector.strip() or str(self.config.get("default_project", "Inbox")).strip()
        project, path = resolve_project(projects, target)
        return project, path, projects

    async def _create(
        self,
        event: AstrMessageEvent,
        spec: AddSpec,
        parent_id: int | None = None,
    ) -> tuple[dict[str, Any], str]:
        await self._register_channel(event)
        project, path, _ = await self._resolve_project(spec.project_selector)
        task = await self.client.create_task(int(project["id"]), spec.payload())
        # 父子关系不是任务字段，创建接口也不接受它，只能新建完再连一条关系。
        if parent_id:
            await self.client.add_relation(int(parent_id), int(task["id"]), "subtask")
        if spec.reminder_minutes is not None:
            await self.state.set_task_reminder(int(task["id"]), spec.reminder_minutes)
        self._wake.set()
        return task, path

    async def _apply_edits(
        self, event: AstrMessageEvent, specs: list[EditSpec]
    ) -> list[tuple[dict[str, Any], str | None]]:
        """应用一组同构的字段改动，返回每条的 (更新后的任务, 如果移动了则附上新项目路径)。

        所有字段改动都经 ``merge_update_task`` 发出**完整**对象；这里只负责把项目选择器
        解析成 ``project_id``（需要项目列表，所以不能在纯函数里做），以及把提醒阈值落库。

        单条（``/todo edit``）与批量（LLM 工具的 ``task_ids``）走同一条路径——批量若另写一份
        就容易漏掉某一步，而漏掉的往往正是"项目解析"或者"提醒阈值落库"这种不出声的部分。
        """
        if not specs:
            raise ValueError("没有指定要修改的内容")
        await self._register_channel(event)
        resolved: dict[str, tuple[int, str]] = {}
        labels: dict[str, dict[str, Any]] | None = None
        results: list[tuple[dict[str, Any], str | None]] = []
        for spec in specs:
            if spec.is_empty():
                raise ValueError("没有指定要修改的内容")
            payload = dict(spec.payload)
            moved_to: str | None = None
            if spec.project_selector:
                # 批量时同一个选择器只解析一次，免得每条任务都重拉一遍项目列表。
                if spec.project_selector not in resolved:
                    project, path, _ = await self._resolve_project(spec.project_selector)
                    resolved[spec.project_selector] = (int(project["id"]), path)
                project_id, moved_to = resolved[spec.project_selector]
                payload["project_id"] = project_id
            if payload:
                task = await self.client.merge_update_task(spec.task_id, payload)
            else:
                task = await self.client.get_task(spec.task_id)
            if spec.reminder_cleared:
                await self.state.clear_task_reminder(spec.task_id)
            elif spec.reminder_minutes is not None:
                await self.state.set_task_reminder(spec.task_id, spec.reminder_minutes)
            if spec.label_change is not None and not spec.label_change.is_empty():
                if labels is None:
                    # 标签表一次调用里只拉一次，批量改标签不会变成 N 次全量查询。
                    labels = await self._label_cache()
                task = await self._apply_label_change(spec.task_id, spec.label_change, labels)
            results.append((task, moved_to))
        # 截止时间变了，提醒时刻与去重键都随之改变，立刻重算下一次轮询。
        self._wake.set()
        return results

    async def _apply_edit(
        self, event: AstrMessageEvent, spec: EditSpec
    ) -> tuple[dict[str, Any], str | None]:
        """应用一次编辑，返回 (更新后的任务, 如果移动了则附上新项目路径)。"""
        return (await self._apply_edits(event, [spec]))[0]

    async def _label_cache(self) -> dict[str, dict[str, Any]]:
        """标签名 → 标签对象。

        Vikunja 的标签**按用户**划分、不属于任何项目，挂标签时又必须给标签 ID 而不是名字，
        所以名字必须先查表解析。一次操作里复用同一份表，避免每条任务都重拉一遍全量标签。
        """
        return build_label_index(await self.client.list_labels())

    async def _resolve_labels(
        self, names: list[str], cache: dict[str, dict[str, Any]], *, create: bool = True
    ) -> list[dict[str, Any]]:
        """把标签名解析成标签对象。``create=True`` 时查不到就新建。

        ``create`` 只对"添加"路径为真：删标签时把不存在的名字顺手创建出来，会凭空多出一堆
        标签，是最坏的一种"友好"。
        """
        resolved: list[dict[str, Any]] = []
        for name in names:
            key = label_cache_key(name)
            label = cache.get(key)
            if label is None and create:
                label = await self.client.create_label(name)
                cache[key] = label
            if label is not None:
                resolved.append(label)
        return resolved

    async def _apply_label_change(
        self,
        task_id: int,
        change: LabelChange,
        cache: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """按 ``LabelChange`` 的有序语义（先 replace、再 add、最后 remove）改标签。

        返回改动**之后**重新读到的任务——标签走的是独立的一组接口，不经过
        ``merge_update_task``，所以调用方手里那份任务对象已经不含最新标签了。
        """
        if change.replace is not None:
            # 整体替换走 bulk 接口：服务端语义就是"以这次传的为准"，一次请求搞定增删。
            wanted: dict[str, dict[str, Any]] = {}
            for label in await self._resolve_labels(change.replace, cache):
                wanted[label_cache_key(label.get("title"))] = label
            for label in await self._resolve_labels(change.add, cache):
                wanted[label_cache_key(label.get("title"))] = label
            for name in change.remove:
                wanted.pop(label_cache_key(name), None)
            await self.client.set_task_labels(task_id, list(wanted.values()))
            return await self.client.get_task(task_id)
        # 增量路径必须先知道当前标签：``PUT /tasks/{id}/labels`` 在标签已经挂在任务上时是
        # **报错**而不是空操作，重复添加同一个标签会失败。任务当前的标签只能从单条读里拿
        # （``labels`` 由服务端 ``Task.ReadOne`` 填充），所以多花一次 GET 换掉一整类偶发报错。
        current_ids = {
            int(label.get("id") or 0)
            for label in (await self.client.get_task(task_id)).get("labels") or []
            if isinstance(label, dict)
        }
        for label in await self._resolve_labels(change.add, cache):
            if int(label["id"]) not in current_ids:
                await self.client.add_task_label(task_id, int(label["id"]))
        for label in await self._resolve_labels(change.remove, cache, create=False):
            if int(label["id"]) in current_ids:
                await self.client.remove_task_label(task_id, int(label["id"]))
        return await self.client.get_task(task_id)

    @staticmethod
    def _describe_edit(task: dict[str, Any], tz: ZoneInfo) -> str:
        due = from_vikunja_time(task.get("due_date"))
        due_text = due.astimezone(tz).strftime("%Y-%m-%d %H:%M") if due else "未设置"
        repeat = "，重复任务" if int(task.get("repeat_after") or 0) or int(task.get("repeat_mode") or 0) else ""
        # 带上标签：加标签时写错字会当场新建一个标签，回显是用户唯一能立刻发现的机会。
        titled = task_label_titles(task)
        label_text = f"，标签：{'、'.join(titled)}" if titled else ""
        return (
            f"优先级：P{int(task.get('priority') or 0)}，截止：{due_text}"
            f"{repeat}{label_text}"
        )

    async def _list(
        self, event: AstrMessageEvent, scope: str, project_selector: str = ""
    ) -> str:
        await self._register_channel(event)
        projects = await self.client.list_projects()
        project_id: int | None = None
        suffix = ""
        if project_selector:
            project, path = resolve_project(projects, project_selector)
            project_id = int(project["id"])
            suffix = f" · {path}"
        tasks = await self.client.list_tasks(project_id)
        selected = select_tasks(tasks, scope, self.tz)
        titles = {
            "today": "📋 今日及逾期待办（按优先级）",
            "week": "📅 未来 7 天待办（按优先级）",
            "overdue": "⚠️ 已逾期待办（按优先级）",
            "all": "🗂️ 全部待办（按优先级）",
        }
        return format_task_list(
            selected,
            titles[scope] + suffix,
            self.tz,
            int(self.config.get("max_list_items", 20)),
            build_project_paths(projects),
        )

    @staticmethod
    def _parse_list_tail(text: str) -> tuple[str, str]:
        tokens = shlex.split(text)
        aliases = {
            "all": "all",
            "全部": "all",
            "week": "week",
            "本周": "week",
            "overdue": "overdue",
            "逾期": "overdue",
            "today": "today",
            "今天": "today",
        }
        scope = "all"
        project = ""
        index = 0
        if tokens and tokens[0].lower() in aliases:
            scope = aliases[tokens[0].lower()]
            index = 1
        while index < len(tokens):
            if tokens[index] not in {"--project", "-P"} or index + 1 >= len(tokens):
                raise ValueError("用法：/todo list [all|week|overdue|today] [--project 项目路径]")
            project = tokens[index + 1]
            index += 2
        return scope, project

    @staticmethod
    def _parse_search_tail(text: str) -> tuple[str, bool]:
        tokens = shlex.split(text)
        include_done = False
        words: list[str] = []
        for token in tokens:
            if token in {"--done", "-a", "--all"}:
                include_done = True
            elif token.startswith("-"):
                raise ValueError("用法：/todo search <关键词> [--done]")
            else:
                words.append(token)
        query = " ".join(words).strip()
        if not query:
            raise ValueError("用法：/todo search <关键词> [--done]")
        return query, include_done

    async def _search(
        self,
        event: AstrMessageEvent,
        query: str = "",
        include_done: bool = False,
        label: str = "",
    ) -> str:
        """按关键词和/或标签找任务。

        两个条件都能单独用，同时给就是"标签和关键词都满足"。**不是在服务端搜**：``GET /tasks?s=``
        的文档说只匹配标题、实现却是 PostgreSQL 全文检索（标题+描述），语义随版本和后端变；
        而任务自带的 ``labels`` 是服务端在列表读里就填好的，两件事都在本地做既一致又省一次请求。
        """
        await self._register_channel(event)
        projects = await self.client.list_projects()
        tasks = await self.client.list_tasks(include_done=include_done)
        matched = filter_tasks_by_query(tasks, query)
        if label.strip():
            matched = filter_tasks_by_label(matched, label)
        # select_tasks 会丢掉已完成任务，所以"含已完成"时不能走它，否则这个开关等于没开。
        ordered = (
            sorted(matched, key=task_sort_key)
            if include_done
            else select_tasks(matched, "all", self.tz)
        )
        conditions = []
        if query.strip():
            conditions.append(f"「{query.strip()}」")
        if label.strip():
            conditions.append(f"标签「{label.strip()}」")
        scope = "（含已完成）" if include_done else ""
        return format_task_list(
            ordered,
            f"🔍 {'、'.join(conditions) or '全部'}匹配到 {len(ordered)} 条{scope}",
            self.tz,
            int(self.config.get("max_list_items", 20)),
            build_project_paths(projects),
        )

    async def _weekly_report(self, event: AstrMessageEvent, days: int = 7) -> str:
        """最近 ``days`` 天的完成情况汇总。

        用 ``done_at``（真实索引列）而不是 ``updated``：改个标题、加个标签都会刷新
        ``updated``，拿它当完成时间会把"上周完成、这周动了动"的任务算进来。
        """
        await self._register_channel(event)
        # now 只取一次：跨过午夜再取第二遍的话，"窗口起点"和"标题上的结束日期"会差一天。
        now = datetime.now(self.tz)
        projects = await self.client.list_projects()
        completed = completed_tasks_since(
            await self.client.list_completed_since(days), now - timedelta(days=days)
        )
        return format_weekly_report(
            completed, build_project_paths(projects), self.tz, now, days=days
        )

    async def _subtask(self, event: AstrMessageEvent, spec: SubtaskSpec) -> str:
        await self._register_channel(event)
        if spec.action == "list":
            parent = await self.client.get_task(spec.parent_id)
            return format_subtask_list(parent, subtasks_of(parent), self.tz)
        if spec.action == "rm":
            await self.client.remove_relation(spec.parent_id, int(spec.child_id))
            return f"✅ 已把 #{spec.child_id} 从 #{spec.parent_id} 的子任务中摘掉（任务本身没有被删除）"
        parent = await self.client.get_task(spec.parent_id)
        project_id = int(parent.get("project_id") or 0)
        if not project_id:
            raise VikunjaError("读不到父任务所属项目，无法在其中创建子任务")
        # 子任务就是一条普通任务 + 一条 subtask 关系；Vikunja 的创建接口不接受关系字段，
        # 所以只能先建后连。放进父任务所在项目，符合直觉也便于在网页端看到。
        child = await self.client.create_task(project_id, {"title": spec.title})
        await self.client.add_relation(spec.parent_id, int(child["id"]), "subtask")
        return (
            f"✅ 已创建子任务 #{child['id']} {child['title']}\n"
            f"父任务：#{spec.parent_id} {parent.get('title', '')}"
        )

    async def _project(self, event: AstrMessageEvent, spec: ProjectSpec) -> str:
        await self._register_channel(event)
        if spec.action == "create":
            parent_id: int | None = None
            parent_path = ""
            if spec.parent_selector:
                parent, parent_path, _ = await self._resolve_project(spec.parent_selector)
                parent_id = int(parent["id"])
            project = await self.client.create_project(spec.name, parent_id)
            title = project.get("title") or spec.name
            return f"✅ 已创建项目 #{project['id']} {parent_path + '/' if parent_path else ''}{title}"
        project, _, _ = await self._resolve_project(spec.name)
        updated = await self.client.merge_update_project(
            int(project["id"]), {"title": spec.new_name}
        )
        return (
            f"✅ 已重命名项目 #{updated['id']}："
            f"{project.get('title', '')} → {updated.get('title') or spec.new_name}"
        )

    async def _move(self, event: AstrMessageEvent, task_id: int, selector: str) -> str:
        await self._register_channel(event)
        project, path, _ = await self._resolve_project(selector)
        task = await self.client.merge_update_task(task_id, {"project_id": int(project["id"])})
        return f"✅ 已把 #{task_id} {task.get('title', '')} 移到 {path}"

    async def _bulk(self, event: AstrMessageEvent, spec: BulkSpec) -> str:
        await self._register_channel(event)
        changes = {
            "done": {"done": True},
            "reopen": {"done": False},
            "priority": {"priority": int(spec.priority or 0)},
        }.get(spec.action)
        succeeded: list[str] = []
        failed: list[str] = []
        for task_id in spec.task_ids:
            try:
                if spec.action == "delete":
                    task = await self.client.get_task(task_id)
                    await self.client.delete_task(task_id)
                    succeeded.append(f"🗑️ #{task_id} {task.get('title', '')}")
                else:
                    task = await self.client.merge_update_task(task_id, dict(changes or {}))
                    succeeded.append(f"#{task_id} {task.get('title', '')}")
            except VikunjaError as exc:
                # 批量操作用户没法一条条重试，所以逐条隔离：一条失败不影响其余的，
                # 最后把失败清单完整报回来。
                failed.append(f"#{task_id}：{exc}")
        self._wake.set()
        summary = {
            "delete": "已删除",
            "reopen": "已撤销完成",
            "priority": f"优先级已设为 P{int(spec.priority or 0)}",
        }.get(spec.action, "已完成")
        lines = [f"{summary} {len(succeeded)} 条"]
        lines.extend(succeeded)
        if failed:
            lines.append(f"⚠️ 失败 {len(failed)} 条")
            lines.extend(failed)
        return "\n".join(lines)

    def _check_tool_schemas(self) -> None:
        """检查 LLM 工具的 docstring 能否解析出参数，避免静默注册成无参数工具。"""
        try:
            members = inspect.getmembers(self)
        except Exception as exc:  # pragma: no cover - 框架对象异常
            logger.warning(f"Vikunja 插件无法枚举 LLM 工具，跳过 schema 自检：{exc}")
            return
        tools = {
            name: method
            for name, method in members
            if name.startswith(VIKUNJA_TOOL_PREFIX) and callable(method)
        }
        if not tools:
            logger.warning("Vikunja 插件没有扫描到任何 LLM 工具，schema 自检未生效")
        for warning in schema_warnings_for_methods(tools):
            logger.warning(f"{warning} 请修正 main.py 中该工具的 docstring。")
        self._check_with_docstring_parser(tools)

    def _check_with_docstring_parser(self, tools: dict[str, Any]) -> None:
        """用框架同款解析器复核 docstring——这是权威判据，不是规则近似。

        ``tool_schema_warnings()`` 编码的是"多行摘要会导致解析失败"这条经验规则；这里直接
        调用 AstrBot 注册工具时用的 ``docstring_parser``，判据与框架完全一致。依赖不存在时
        （本地测试环境）静默跳过。
        """
        try:
            import docstring_parser
        except ImportError:
            return
        for name, method in tools.items():
            try:
                parameters = list(inspect.signature(method).parameters)
            except (TypeError, ValueError):  # pragma: no cover - 非函数对象
                continue
            # 与 AstrBot 保持一致：传原始 __doc__，不做 cleandoc。
            mismatches = parser_mismatch_warnings(
                name,
                getattr(method, "__doc__", None),
                parameters,
                docstring_parser.parse,
            )
            for warning in mismatches:
                logger.warning(warning)

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        await self._ensure_ready()
        self._check_tool_schemas()
        if not self.client.base_url or not self.client.token:
            logger.warning("Vikunja 插件未配置 URL 或 API Token，提醒调度未启动")
            return
        if self._reminder_task is None or self._reminder_task.done():
            self._reminder_task = asyncio.create_task(
                self._reminder_loop(), name="astrbot-vikunja-reminders"
            )

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        if event.get_group_id() or not self._sender_allowed(event):
            return
        req.system_prompt = (req.system_prompt or "") + SECRETARY_PROMPT

    async def _reminder_loop(self) -> None:
        while True:
            # 先清后派发：派发期间新建任务触发的唤醒不会丢。
            self._wake.clear()
            delay = float(self._poll_intervals()[0])
            try:
                delay = await self._dispatch_reminders()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"Vikunja 提醒轮询失败：{exc}")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _dispatch_reminders(self) -> float:
        """推送到期提醒，并返回本次轮询后应睡眠的秒数。"""
        poll_interval, idle_interval = self._poll_intervals()
        channels = await self._enabled_push_channels()
        if not channels:
            return float(idle_interval)
        now = datetime.now(timezone.utc)
        default_minutes = max(0, int(self.config.get("reminder_minutes", 30)))
        overrides = self.state.task_reminder_overrides()
        tasks = await self.client.list_tasks()
        _, paths = await self._cached_projects()
        notified: set[int] = set()
        for task in tasks:
            due = from_vikunja_time(task.get("due_date"))
            if not due:
                continue
            minutes = overrides.get(int(task["id"]), default_minutes)
            if due > now + timedelta(minutes=minutes):
                continue
            due_local = due.astimezone(self.tz).strftime("%m-%d %H:%M")
            overdue = due < now
            project_path = paths.get(int(task.get("project_id") or 0), "未知项目")
            text = (
                f"{'⚠️ 已逾期' if overdue else '⏰ 即将到期'}："
                f"[P{int(task.get('priority') or 0)}] #{task['id']} {task.get('title', '')}\n"
                f"项目：{project_path}\n截止：{due_local}"
            )
            for channel in channels:
                sent_key = f"{channel.name}|{task['id']}|{task.get('due_date', '')}"
                if self.state.was_sent(sent_key):
                    continue
                # 通道自己判断成败（包括"返回了假值但其实是成功"这种情况），这里只管记账。
                # 去重记录在成功与失败两条路径上都要落，失败的那次不能重试。
                try:
                    delivered = await channel.send(text)
                except Exception as exc:
                    await self._handle_send_failure(
                        channel.name, sent_key, now, channel.umo, str(exc)
                    )
                    continue
                if delivered:
                    await self.state.mark_sent(sent_key, now.isoformat())
                    self._channel_failures.pop(channel.name, None)
                else:
                    await self._handle_send_failure(
                        channel.name, sent_key, now, channel.umo, "找不到会话"
                    )
            # 所有启用渠道都已有去重记录 → 这条提醒不会再发第二次，不该继续牵着轮询节奏。
            if all(
                self.state.was_sent(f"{channel.name}|{task['id']}|{task.get('due_date', '')}")
                for channel in channels
            ):
                notified.add(int(task["id"]))
        return next_poll_delay(
            tasks, default_minutes, overrides, notified, now, poll_interval, idle_interval
        )

    async def _handle_send_failure(
        self,
        channel_name: str,
        sent_key: str,
        now: datetime,
        umo: str,
        reason: str,
    ) -> None:
        await self.state.mark_sent(sent_key, now.isoformat())
        failures = self._channel_failures.get(channel_name, 0) + 1
        self._channel_failures[channel_name] = failures
        if failures >= 3:
            await self.state.set_reminders_enabled(channel_name, False)
            self._channel_failures.pop(channel_name, None)
            logger.warning(
                f"已自动关闭 {umo} 的提醒（连续 {failures} 次发送失败：{reason}）"
            )
        else:
            logger.warning(f"提醒发送失败（{failures}/3）：{umo} — {reason}")

    @filter.command_group("todo", alias={"待办"})
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    def todo(self):
        """管理 Vikunja 待办，仅支持私聊。"""
        pass

    @todo.command("help", alias={"帮助"})
    async def todo_help(self, event: AstrMessageEvent):
        try:
            await self._register_channel(event)
            yield event.plain_result(HELP_TEXT)
        except PrivateOnlyError as exc:
            yield event.plain_result(str(exc))

    @todo.command("projects", alias={"项目", "项目树"})
    async def todo_projects(self, event: AstrMessageEvent):
        try:
            await self._register_channel(event)
            yield event.plain_result(format_project_tree(await self.client.list_projects()))
        except (PrivateOnlyError, VikunjaError) as exc:
            yield event.plain_result(str(exc))

    @todo.command("add", alias={"添加", "新建"})
    async def todo_add(self, event: AstrMessageEvent):
        try:
            spec = parse_add_arguments(self._command_tail(event), self.tz)
            task, project_path = await self._create(event, spec)
            due = from_vikunja_time(task.get("due_date"))
            due_text = due.astimezone(self.tz).strftime("%Y-%m-%d %H:%M") if due else "未设置"
            repeat_text = "，重复任务" if spec.repeat_after or spec.repeat_mode else ""
            yield event.plain_result(
                f"✅ 已创建 #{task['id']} {task['title']}\n"
                f"项目：{project_path}，优先级：P{task.get('priority', 0)}，截止：{due_text}{repeat_text}"
            )
        except (ValueError, VikunjaError) as exc:
            yield event.plain_result(f"创建失败：{exc}")

    @todo.command("done", alias={"完成", "勾选"})
    async def todo_done(self, event: AstrMessageEvent):
        # 注意 handler 顺序：PrivateOnlyError 是 ValueError 的子类，放在后面会被"用法"分支
        # 静默吞掉，本该报白名单/私聊限制的地方反而显示成参数错误。
        try:
            await self._register_channel(event)
            task_id = int(self._command_tail(event).lstrip("#"))
            task = await self.client.complete_task(task_id)
            repeated = not task.get("done", True)
            suffix = "；重复规则已推进到下一周期" if repeated else ""
            yield event.plain_result(f"✅ 已完成 #{task_id} {task.get('title', '')}{suffix}")
        except PrivateOnlyError as exc:
            yield event.plain_result(str(exc))
        except ValueError:
            yield event.plain_result("用法：/todo done <任务ID>")
        except VikunjaError as exc:
            yield event.plain_result(f"完成失败：{exc}")

    @todo.command("edit", alias={"修改", "编辑", "改"})
    async def todo_edit(self, event: AstrMessageEvent):
        try:
            spec = parse_edit_arguments(self._command_tail(event), self.tz)
            task, moved_to = await self._apply_edit(event, spec)
            moved = f"\n项目：{moved_to}" if moved_to else ""
            yield event.plain_result(
                f"✅ 已修改 #{task['id']} {task.get('title', '')}\n"
                f"{self._describe_edit(task, self.tz)}{moved}"
            )
        except PrivateOnlyError as exc:
            yield event.plain_result(str(exc))
        except (ValueError, VikunjaError) as exc:
            yield event.plain_result(f"修改失败：{exc}")

    @todo.command("reopen", alias={"撤销完成", "恢复", "取消完成"})
    async def todo_reopen(self, event: AstrMessageEvent):
        try:
            await self._register_channel(event)
            task_id = int(self._command_tail(event).lstrip("#"))
            task = await self.client.reopen_task(task_id)
            self._wake.set()
            yield event.plain_result(f"♻️ 已撤销完成 #{task_id} {task.get('title', '')}")
        except PrivateOnlyError as exc:
            yield event.plain_result(str(exc))
        except ValueError:
            yield event.plain_result("用法：/todo reopen <任务ID>")
        except VikunjaError as exc:
            yield event.plain_result(f"撤销失败：{exc}")

    @todo.command("search", alias={"搜索", "查找"})
    async def todo_search(self, event: AstrMessageEvent):
        try:
            query, include_done = self._parse_search_tail(self._command_tail(event))
            yield event.plain_result(await self._search(event, query, include_done))
        except PrivateOnlyError as exc:
            yield event.plain_result(str(exc))
        except (ValueError, VikunjaError) as exc:
            yield event.plain_result(f"搜索失败：{exc}")

    @todo.command("move", alias={"移动", "挪"})
    async def todo_move(self, event: AstrMessageEvent):
        try:
            task_id, selector = parse_move_arguments(self._command_tail(event))
            yield event.plain_result(await self._move(event, task_id, selector))
        except PrivateOnlyError as exc:
            yield event.plain_result(str(exc))
        except (ValueError, VikunjaError) as exc:
            yield event.plain_result(f"移动失败：{exc}")

    @todo.command("subtask", alias={"子任务"})
    async def todo_subtask(self, event: AstrMessageEvent):
        try:
            spec = parse_subtask_arguments(self._command_tail(event))
            yield event.plain_result(await self._subtask(event, spec))
        except PrivateOnlyError as exc:
            yield event.plain_result(str(exc))
        except (ValueError, VikunjaError) as exc:
            yield event.plain_result(f"子任务操作失败：{exc}")

    @todo.command("bulk", alias={"批量"})
    async def todo_bulk(self, event: AstrMessageEvent):
        try:
            spec = parse_bulk_arguments(self._command_tail(event))
            yield event.plain_result(await self._bulk(event, spec))
        except PrivateOnlyError as exc:
            yield event.plain_result(str(exc))
        except (ValueError, VikunjaError) as exc:
            yield event.plain_result(f"批量操作失败：{exc}")

    @todo.command("project", alias={"项目管理"})
    async def todo_project(self, event: AstrMessageEvent):
        try:
            spec = parse_project_arguments(self._command_tail(event))
            yield event.plain_result(await self._project(event, spec))
        except PrivateOnlyError as exc:
            yield event.plain_result(str(exc))
        except (ValueError, VikunjaError) as exc:
            yield event.plain_result(f"项目操作失败：{exc}")

    @todo.command("label", alias={"标签"})
    async def todo_label(self, event: AstrMessageEvent):
        try:
            spec = parse_label_arguments(self._command_tail(event))
            await self._register_channel(event)
            if spec.action == "list":
                yield event.plain_result(format_label_list(await self.client.list_labels()))
                return
            cache = await self._label_cache()
            change = label_change_from_spec(spec)
            lines: list[str] = []
            for task_id in spec.task_ids:
                task = await self._apply_label_change(task_id, change, cache)
                titled = task_label_titles(task)
                lines.append(
                    f"#{task_id} {task.get('title', '')}"
                    f"（标签：{'、'.join(titled) if titled else '无'}）"
                )
            yield event.plain_result("\n".join(lines))
        except PrivateOnlyError as exc:
            yield event.plain_result(str(exc))
        except (ValueError, VikunjaError) as exc:
            yield event.plain_result(f"标签操作失败：{exc}")

    @todo.command("today", alias={"今天", "今日"})
    async def todo_today(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(await self._list(event, "today"))
        except (ValueError, VikunjaError) as exc:
            yield event.plain_result(f"查询失败：{exc}")

    @todo.command("list", alias={"列表", "查询"})
    async def todo_list(self, event: AstrMessageEvent):
        try:
            scope, project = self._parse_list_tail(self._command_tail(event))
            yield event.plain_result(await self._list(event, scope, project))
        except (ValueError, VikunjaError) as exc:
            yield event.plain_result(f"查询失败：{exc}")

    @todo.command("week", alias={"周报", "完成情况"})
    async def todo_week(self, event: AstrMessageEvent):
        try:
            yield event.plain_result(await self._weekly_report(event))
        except PrivateOnlyError as exc:
            yield event.plain_result(str(exc))
        except (ValueError, VikunjaError) as exc:
            yield event.plain_result(f"统计失败：{exc}")

    @todo.command("remind", alias={"提醒"})
    async def todo_remind(self, event: AstrMessageEvent):
        value = self._command_tail(event).lower()
        enabled = value in {"on", "开", "开启"}
        if value not in {"on", "off", "开", "关", "开启", "关闭"}:
            yield event.plain_result("用法：/todo remind <on|off>")
            return
        try:
            key = await self._register_channel(event)
            await self.state.set_reminders_enabled(key, enabled)
            if enabled:
                # 刚开启提醒的渠道不该等满一个空闲间隔才收到第一条。
                self._wake.set()
            yield event.plain_result(f"✅ 当前私聊入口的临期提醒已{'开启' if enabled else '关闭'}")
        except PrivateOnlyError as exc:
            yield event.plain_result(str(exc))

    @todo.command("push", alias={"推送"})
    async def todo_push(self, event: AstrMessageEvent):
        """推送通道自检：``status`` 看配置和渠道状态，``test`` 真发一条。"""
        action = self._command_tail(event).lower() or "status"
        if action not in {"status", "test", "状态", "测试"}:
            yield event.plain_result("用法：/todo push <status|test>")
            return
        try:
            await self._register_channel(event)
        except PrivateOnlyError as exc:
            yield event.plain_result(str(exc))
            return
        if action in {"status", "状态"}:
            yield event.plain_result(
                format_push_status(
                    registered=self.state.channels(),
                    failures=self._channel_failures,
                    enabled_kinds=self.config.get("push_channels"),
                    capsule_configured=bool(
                        str(self.config.get("capsule_endpoint", "")).strip()
                    ),
                )
            )
            return
        channels = await self._enabled_push_channels()
        if not channels:
            yield event.plain_result(
                "当前没有任何启用的推送通道，提醒不会被送达。"
                "先在本入口执行 /todo remind on，或检查 push_channels 配置。"
            )
            return
        lines = ["📤 推送测试"]
        for channel in channels:
            try:
                delivered = await channel.send(
                    "📤 Vikunja 推送测试：看到这条说明这个入口的提醒通道是通的。"
                )
            except Exception as exc:
                lines.append(f"• {channel.umo}：失败（{exc}）")
                continue
            lines.append(
                f"• {channel.umo}：{'成功' if delivered else '失败（找不到会话）'}"
            )
        # 测试**不**写去重记录、也不累计失败次数：一次手动自检不该让某个渠道被自动停用，
        # 更不该让下一条真提醒被去重键吞掉。
        yield event.plain_result("\n".join(lines))

    @filter.llm_tool(name="vikunja_list_projects")
    async def vikunja_list_projects(self, event: AstrMessageEvent) -> str:
        """查询完整的 Vikunja 项目层级；创建工作或项目任务前应先用它确认位置。"""
        try:
            await self._register_channel(event)
            return format_project_tree(await self.client.list_projects())
        except (PrivateOnlyError, VikunjaError) as exc:
            return str(exc)

    @filter.llm_tool(name="vikunja_create_task")
    async def vikunja_create_task(
        self,
        event: AstrMessageEvent,
        title: str,
        project: str = "",
        due: str = "",
        priority: int = 0,
        repeat: str = "",
        is_reminder: bool = False,
        description: str = "",
        remind_minutes: int = -1,
        parent_id: int = 0,
    ) -> str:
        """用户说“帮我记下”“添加待办”“新建任务”“记一下”“加一条”等表达时视为创建任务。确认信息充分后创建 Vikunja 任务。用户说“提醒”时 due 必填；工作事项 project 必填。

        Args:
            title(string): 简洁的任务标题
            project(string): 项目完整路径、唯一名称或 ID；日常琐事可为空并进入默认 Inbox
            due(string): 已经和用户确认的截止时间，如“明天18点”；无截止时间可为空
            priority(number): 优先级 0 到 5
            repeat(string): 已确认的重复规则：daily、weekly、monthly、2d、12h、每3天，或“完成后2d”表示从完成时刻起算；不重复留空。Vikunja 无法表达“每周一/三/五”这类指定星期几的规则，遇到要如实说明并改用每周或拆成多条
            is_reminder(boolean): 用户是否明确要求“提醒我”；若为 true，due 不可为空
            description(string): 任务的详细说明或备注，用户提供了链接、要点、背景时才填
            remind_minutes(number): 提前多少分钟提醒，来自用户明确说的“提前10分钟”；未提及填 -1
            parent_id(number): 作为谁的子任务，填父任务 ID；普通任务填 0
        """
        try:
            reason = secretary_clarification_reason(title, due, repeat, project, is_reminder)
            if reason:
                return f"需要先向用户确认：{reason}。\n请向用户确认以上缺失信息后，获取用户回复，用补充完整的信息重新调用本工具创建任务。"
            repeat_after, repeat_mode = parse_repeat(repeat)
            due_at = parse_datetime(due, self.tz) if due else None
            if (repeat_after or repeat_mode) and not due_at:
                raise ValueError("重复任务必须先确认首次截止时间")
            task, path = await self._create(
                event,
                AddSpec(
                    title=title,
                    project_selector=project,
                    description=description.strip(),
                    due=due_at,
                    priority=max(0, min(5, int(priority))),
                    repeat_after=repeat_after,
                    repeat_mode=repeat_mode,
                    reminder_minutes=int(remind_minutes) if int(remind_minutes) >= 0 else None,
                ),
                parent_id=int(parent_id) if int(parent_id) > 0 else None,
            )
            parent_note = f"，父任务：#{int(parent_id)}" if int(parent_id) > 0 else ""
            return f"已创建任务 #{task['id']} {task['title']}，项目：{path}{parent_note}"
        except (ValueError, VikunjaError) as exc:
            return f"创建失败：{exc}"

    @filter.llm_tool(name="vikunja_update_task")
    async def vikunja_update_task(
        self,
        event: AstrMessageEvent,
        task_ids: str,
        title: str = "",
        description: str = "",
        due: str = "",
        priority: int = -1,
        repeat: str = "",
        project: str = "",
        done: str = "",
        parent_id: int = -1,
        remind_minutes: int = -1,
        labels: str = "",
    ) -> str:
        """修改已有任务、改变完成状态或调整父子关系；用户要求改标题、改优先级、改截止时间、加备注、移动项目、标记完成、撤销完成、改标签、挂到某任务下时都用本工具，不要删除重建。

        Args:
            task_ids(string): 要修改的任务 ID，即任务列表中 # 后面的数字；要一次改多条就用逗号分隔，如 12,15,20
            title(string): 新的任务标题；不改留空
            description(string): 新的备注内容；不改留空，填“清空”则删掉原有备注
            due(string): 新的截止时间如“明天18点”；不改留空，填“清空”则去掉截止时间
            priority(number): 新的优先级 0 到 5；不改填 -1
            repeat(string): 新的重复规则 daily、weekly、monthly、2d、每3天，或“完成后2d”表示从完成时刻起算；填 none 停止重复；不改留空。Vikunja 无法表达“每周一/三/五”这类指定星期几的规则，遇到要如实说明并改用每周或拆成多条
            project(string): 要移动到的项目完整路径、唯一名称或 ID；不移动留空
            done(string): 填“完成”标记为已完成，填“撤销完成”回到未完成；不改留空
            parent_id(number): 要挂到的父任务 ID；不改填 -1，填 0 表示摘掉现有的父子关系
            remind_minutes(number): 提前多少分钟提醒；不改填 -1
            labels(string): 标签改动：每个标签前加 + 表示添加、加 - 表示移除，如“+重要,-待定”；不加符号则表示把标签整体换成这几个，如“重要,紧急”；清空标签写“清空”；不改留空
        """
        try:
            ids = parse_task_ids(task_ids)
            payload: dict[str, Any] = {}
            if title.strip():
                payload["title"] = title.strip()
            if description.strip():
                payload["description"] = None if is_clear_request(description) else description.strip()
            if due.strip():
                payload["due_date"] = (
                    None if is_clear_request(due) else to_vikunja_time(parse_datetime(due, self.tz))
                )
            if int(priority) >= 0:
                if int(priority) > 5:
                    raise ValueError("优先级应为 0 到 5")
                payload["priority"] = int(priority)
            if repeat.strip():
                repeat_after, repeat_mode = parse_repeat(repeat)
                payload["repeat_after"] = repeat_after
                payload["repeat_mode"] = repeat_mode
            done_flag = parse_tristate_bool(done)
            if done_flag is not None:
                payload["done"] = done_flag
            wants_parent = int(parent_id) >= 0
            label_change = parse_label_changes(labels) if labels.strip() else None
            specs = [
                EditSpec(
                    task_id=task_id,
                    payload=dict(payload),
                    project_selector=project.strip() or None,
                    reminder_minutes=int(remind_minutes) if int(remind_minutes) >= 0 else None,
                    label_change=label_change,
                )
                for task_id in ids
            ]
            editable = [spec for spec in specs if not spec.is_empty()]
            # 只挂父子关系、不改字段时 payload 是空的，这时不该走编辑路径（它会以"没有指定要
            # 修改的内容"报错），直接去连关系。
            if not editable and not wants_parent:
                raise ValueError("没有指定要修改的内容")
            results = await self._apply_edits(event, editable) if editable else []
            parent_note = ""
            if wants_parent:
                await self._register_channel(event)
                parent_note = await self._reparent(ids, int(parent_id))
            if not results:
                # 只挂了父子关系、没有任何字段改动，别报"已修改 0 条任务"这种让人心慌的话。
                return parent_note
            if len(results) == 1:
                task, moved_to = results[0]
                parts = [
                    f"已修改 #{task['id']} {task.get('title', '')}",
                    self._describe_edit(task, self.tz),
                ]
                if moved_to:
                    parts.append(f"项目：{moved_to}")
                if done_flag is True and not task.get("done", True):
                    parts.append("重复规则已推进到下一周期")
                if parent_note:
                    parts.append(parent_note)
                return "\n".join(parts)
            lines = [f"已修改 {len(results)} 条任务"]
            lines.extend(
                f"· #{task['id']} {task.get('title', '')}" + (f" → {moved}" if moved else "")
                for task, moved in results
            )
            if parent_note:
                lines.append(parent_note)
            return "\n".join(lines)
        except (ValueError, PrivateOnlyError, VikunjaError) as exc:
            return f"修改失败：{exc}"

    async def _reparent(self, task_ids: list[int], parent_id: int) -> str:
        """把一批任务挂到 ``parent_id`` 下，或（``parent_id == 0``）摘掉它们现有的父任务。"""
        if parent_id > 0:
            parent = await self.client.get_task(parent_id)
            for task_id in task_ids:
                await self.client.add_relation(parent_id, task_id, "subtask")
            return f"父任务：#{parent_id} {parent.get('title', '')}"
        removed = 0
        for task_id in task_ids:
            # 关系存在父任务那一侧，所以要先从自己的 parenttask 里问出父任务是谁，
            # 再去父任务那边删掉 subtask 关系——不能凭 task_id 直接删。
            current = await self.client.get_task(task_id)
            for parent in parents_of(current):
                await self.client.remove_relation(int(parent["id"]), task_id, "subtask")
                removed += 1
        return f"已摘掉 {removed} 条父子关系" if removed else "这些任务本来就没有父任务"

    @filter.llm_tool(name="vikunja_delete_task")
    async def vikunja_delete_task(self, event: AstrMessageEvent, task_ids: str) -> str:
        """用户明确要求删除一个或多个任务时调用。删除不可撤销；目标不明确时必须先查询任务列表定位 ID。

        Args:
            task_ids(string): Vikunja 任务 ID，即任务列表中 # 后面的数字；要一次删多条就用逗号分隔，如 12,15
        """
        try:
            ids = parse_task_ids(task_ids)
            await self._register_channel(event)
            deleted: list[str] = []
            failed: list[str] = []
            for task_id in ids:
                try:
                    task = await self.client.get_task(task_id)
                    await self.client.delete_task(task_id)
                    deleted.append(f"#{task_id} {task.get('title', '')}")
                except VikunjaError as exc:
                    failed.append(f"#{task_id}：{exc}")
            if len(ids) == 1 and not failed:
                return f"已删除 {deleted[0]}"
            lines = [f"已删除 {len(deleted)} 条"] + deleted
            if failed:
                lines.append(f"⚠️ 失败 {len(failed)} 条")
                lines.extend(failed)
            return "\n".join(lines)
        except (ValueError, PrivateOnlyError, VikunjaError) as exc:
            return f"删除失败：{exc}"

    @filter.llm_tool(name="vikunja_manage_project")
    async def vikunja_manage_project(
        self,
        event: AstrMessageEvent,
        action: str,
        name: str,
        parent: str = "",
        new_name: str = "",
    ) -> str:
        """新建项目或重命名已有项目；用户要求“建个项目”“开一个新项目”“把某个项目改名”时调用。

        Args:
            action(string): create 表示新建，rename 表示重命名
            name(string): action=create 时是**新项目的名称**；action=rename 时是要改的现有项目，填完整路径、唯一名称或 ID
            parent(string): 仅 action=create 时有效，新项目挂到哪个父项目下；建在顶层留空
            new_name(string): 仅 action=rename 时有效，项目的新名称
        """
        try:
            normalized = action.strip().lower()
            if normalized not in {"create", "rename", "new"}:
                raise ValueError("action 只能是 create（新建）或 rename（重命名）")
            # LLM 给的是结构化参数，直接构造 ProjectSpec 就好；不要绕回
            # ``parse_project_arguments`` 再解析一遍文本——引号/反斜杠会被 shlex 二次加工。
            if normalized in {"create", "new"}:
                if not name.strip():
                    raise ValueError("新项目名称不能为空")
                spec = ProjectSpec(
                    action="create", name=name.strip(), parent_selector=parent.strip()
                )
            else:
                if not name.strip() or not new_name.strip():
                    raise ValueError("重命名需要同时给出项目和新名称")
                spec = ProjectSpec(
                    action="rename", name=name.strip(), new_name=new_name.strip()
                )
            return await self._project(event, spec)
        except (ValueError, PrivateOnlyError, VikunjaError) as exc:
            return f"项目操作失败：{exc}"

    @filter.llm_tool(name="vikunja_list_tasks")
    async def vikunja_list_tasks(
        self,
        event: AstrMessageEvent,
        scope: str = "today",
        project: str = "",
        search: str = "",
        include_done: bool = False,
        label: str = "",
    ) -> str:
        """用户问"还有什么待办""今天有什么任务""本周待办""我的任务""有没有关于X的任务""带某标签的任务"时调用本工具。查询所有项目或指定项目的未完成任务，并按优先级排序。

        Args:
            scope(string): 查询范围，today、week、overdue 或 all
            project(string): 可选的项目完整路径、唯一名称或 ID
            search(string): 关键词；按标题和描述做子串匹配，用于"有没有关于XX的任务"这类查找，不需要搜索时留空
            include_done(boolean): 是否把已完成的任务也纳入结果；用户找的是已完成的老任务时才填 true
            label(string): 按标签名筛选，子串匹配；用户问"哪些任务标了 XX"时填，不需要时留空
        """
        try:
            if search.strip() or label.strip():
                return await self._search(
                    event, search.strip(), bool(include_done), label.strip()
                )
            normalized = scope.lower() if scope.lower() in {"today", "week", "overdue", "all"} else "today"
            return await self._list(event, normalized, project)
        except (ValueError, PrivateOnlyError, VikunjaError) as exc:
            return f"查询失败：{exc}"

    @filter.llm_tool(name="vikunja_stats")
    async def vikunja_stats(
        self,
        event: AstrMessageEvent,
        period: str = "week",
    ) -> str:
        """统计一段时间内完成了多少任务，用于"这周完成了什么""最近干得怎么样""周报"这类复盘请求。

        Args:
            period(string): 统计区间，week 表示最近 7 天、month 表示最近 30 天；默认 week
        """
        try:
            days = {"week": 7, "month": 30}.get(period.strip().lower(), 7)
            return await self._weekly_report(event, days)
        except (ValueError, PrivateOnlyError, VikunjaError) as exc:
            return f"统计失败：{exc}"

    async def terminate(self) -> None:
        if self._reminder_task and not self._reminder_task.done():
            self._reminder_task.cancel()
            try:
                await self._reminder_task
            except asyncio.CancelledError:
                pass
        await self.client.close()
