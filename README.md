# AstrBot Vikunja 私人待办秘书

![astrbot_plugin_vikunja](https://count.getloli.com/@astrbot_plugin_vikunja)

面向单一用户的 AstrBot 插件。QQ 官方机器人私聊和微信个人号 ClawBot（`weixin_oc`）是同一个 Vikunja 工作空间的两个入口：都能查看全部项目和任务，也都可以接收提醒。插件不响应群聊中的待办命令，LLM Tool 在群聊中也会拒绝读写。

## 使用模型

- 一个 AstrBot 实例、一个 Vikunja API Token、一个真实用户。
- QQ 和微信不分别绑定项目，只登记为提醒渠道。
- 两个入口共享全部项目、子项目、任务和完成状态。
- 未指定项目的日常琐事进入默认 `Inbox`。
- 工作事项使用完整项目路径，例如 `fudan-work/SMX/paper`。
- 每个入口可独立执行 `/todo remind on|off`；默认都会收到提醒。

## 秘书模式

插件不仅提供命令，还注册了项目树、创建任务、修改任务、删除任务、项目管理、查询任务和完成情况统计七个 LLM Tool，并向私聊会话加入稳定的秘书规则。因此可以直接说：

需要为该会话启用支持 Tool Calling 的模型和 AstrBot Agent；如果模型未启用工具调用，自然语言仍会被普通聊天处理，但不会实际写入 Vikunja，斜杠命令不受影响。

```text
提醒我明天下午六点前交周报。
这周要把 SMX 那篇 paper 的引言重写一下。
我今天还有什么没做？
刚才那个买牛奶的任务完成了。
把 12 号的截止时间改到周五晚上八点。
勾错了，3 号还没做完。
给 7 号加个备注：设计稿在 FluidCapsule 里。
1、3、5 都标记完成。
把“写引言”拆成子任务挂到论文那条下面。
有没有关于 FluidCapsule 的任务？
建个项目叫 reading-list。
给 7 号打个“重要”标签。
这周完成了什么？
```

模型在写入前应检查信息是否充分。代码层还有第二道保护：

- 用户说“提醒我”但没有明确日期和时间：不创建，先追问。
- 明显属于论文、研究、项目或工作的事项却没有项目归属：先展示/查询项目树并追问。
- 明显长期的事项没有截止时间、计划时长或频率：不创建，先追问。
- 标题含“每天、每周、每月、定期”但没有确认重复规则：不创建，先追问。
- 修改或完成目标不唯一时，模型应先查询并让用户确认任务 ID。

这是“规则 + 工具保护”的实现，不依赖模型一次性猜对所有字段。最终是否创建以工具返回结果为准。

## 安装与配置

将本目录放入 AstrBot 的 `data/plugins/astrbot_plugin_vikunja`，安装 `requirements.txt` 后重载插件。AstrBot 最低版本为 `4.22.1`，建议使用当前最新版。

在 WebUI 中配置：

1. `vikunja_url`：Vikunja 地址，插件自动补全 `/api/v1`。
2. `api_token`：具有项目读取、任务读取和任务写入权限的 API Token。
3. `default_project`：默认 `Inbox`，也可填写完整路径或项目 ID。
4. `allowed_qq_sender_ids`：可选，仅限制 QQ 官方机器人私聊发送者；`weixin_oc` 不检查此项。
5. `timezone`、提前提醒分钟数、列表长度。
6. `poll_interval_seconds` / `idle_poll_interval_seconds`：提醒轮询间隔。有任务临近提醒时间时用前者（默认 60 秒），空闲时放宽到后者（默认 600 秒）；插件还会直接睡到下一个提醒时刻，所以空闲时不会每分钟空拉一次 Vikunja。新建任务、新登记一个私聊入口、或 `/todo remind on` 时，轮询会被立即唤醒，不受空闲间隔影响。已经提醒过、用户还没清理的逾期任务不会拖住轮询——它们在所有入口都收到提醒后就不再影响节奏（改期会重新武装）。
7. `project_cache_ttl_seconds`：提醒文案复用项目列表的时长（默认 300 秒），设为 0 表示每次都重新获取。
8. `push_channels`：提醒推送通道，默认只勾选 `astrbot`。取消全部勾选等于全局静音，见下文"提醒推送通道"。
9. `capsule_endpoint` / `capsule_token`：为手机胶囊预留，当前版本不生效，留空即可。

插件通过 AstrBot KV 存储已登记的 QQ/微信私聊渠道、自定义提醒阈值和提醒去重记录。Token 不会经过聊天传输。

## 命令

命令主要用于精确操作和排障；日常使用可以直接自然语言交流。

```text
/todo projects
/todo add 买牛奶 --due "明天 18:00"
/todo add 修改论文引言 --project "fudan-work/SMX/paper" --due "周日 20:00" --priority 4
/todo add 每日复盘 --project personal-project --due "今天 22:00" --repeat daily --remind 1h
/todo edit 123 --due "周五 20:00"
/todo edit 123 --title "修改论文讨论章节" --priority 5
/todo edit 123 --desc "设计稿在 FluidCapsule 里"
/todo done 123
/todo reopen 123
/todo search FluidCapsule
/todo list week
/todo list overdue --project "fudan-work/SMX/paper"
/todo list all --search 引言
/todo today
/todo subtask add 12 重写引言
/todo subtask list 12
/todo subtask rm 12 15
/todo bulk done 12,15,20
/todo bulk pri 5 12 15
/todo project new reading-list
/todo project new paper --parent "fudan-work/SMX"
/todo project rename reading-list 书单
/todo move 123 "fudan-work/SMX/paper"
/todo edit 123 --label +重要,-待定
/todo label list
/todo label add 12,15 论文
/todo label set 12 论文,紧急
/todo week
/todo remind off
/todo push status
/todo help
```

项目选择器支持项目 ID、唯一名称或完整路径。时间支持 `明天9点`、`周日 20:00`、`2小时后`、`2026-07-12 18:00`。带空格的参数需使用引号。

`/todo edit` 只修改你写出来的字段，其余一律保持原值。**空值表示清空**：`--due ""` 去掉截止时间，`--desc ""` 删掉描述，`--repeat none` 停止重复，`--remind clear` 恢复全局默认提醒提前量。改截止时间不会丢失标题、描述或优先级——插件每次都是先读取任务、本地合并、再写回完整对象。

`/todo search` 在**本地**过滤已拉取的任务，同时匹配标题和描述，不区分大小写。之所以不用服务端的 `?s=` 参数：它的行为随 Vikunja 版本和数据库后端变化（文档说只匹配标题，实现里是 PostgreSQL 全文检索），本地过滤在哪都一样，还能顺带搜到描述。

子任务是 Vikunja 的**任务关系**，不是任务上的一个字段：`/todo subtask add <父id> <子id或标题>` 在两者之间建一条 `subtask` 关系，服务端会自动补上反向的 `parenttask`。因此 `/todo subtask list <id>` 需要读任务的 `related_tasks`，`/todo subtask rm` 删的是父任务那一侧的关系。

`/todo bulk` 逐条走与前两条命令完全相同的修改路径（不是另一个 API），所以批量和单条的语义、错误处理、提醒阈值落库都一致；某一条失败不会中断其余，结果里会单独标出失败的那几条。`/todo move` 只改任务的 `project_id`，不动父任务关系；摘下父任务用 `/todo subtask rm`。

### 重复规则

`--repeat` 支持 `daily`/`每天`、`weekly`/`每周`、`monthly`/`每月`、`2d`、`12h`、`每3天`、`每隔2周`，以及 `完成后2d`。最后一种对应 Vikunja 的 `repeat_mode=2`：**从你勾选完成那一刻起算**，而不是从截止时间顺延，适合"这件事做完之后隔三天再来一次"。

Vikunja 本身只能表达"每隔 N 秒重复"和"每月同一天"两种规则（`repeat_mode` 0/1/2，其中 `1` 会忽略 `repeat_after`）。所以 `每周一/三/五`、`工作日`、`每 2 个月` 这类规则**表达不了**，插件会明确报错并给出可执行替代，而不是挑一个近似的秒数填上去：

```text
/todo add 健身 --due "周一 20:00" --repeat 每周一
→ Vikunja 只能表达“每隔 N 秒重复”和“每月同一天”，无法表达“每周一/三/五”这类指定星期几的规则。
  替代方案：① 用 7d —— 如果截止时间正好落在你要的那一天，效果就是每周同一日；
  ② 拆成三个独立任务，各自设 weekly
```

之所以不近似：凑出来的值会在你毫无察觉的情况下越跑越偏，而"重复任务默默地不在该出现的那天出现"是最难被发现的一类错误。

### 标签

标签属于**用户**而不是项目，一个标签可以打在任意项目的任务上。

```text
/todo label list                列出全部标签
/todo label add 12,15 论文      给 #12 #15 加“论文”，标签不存在会自动新建
/todo label rm 12 论文          摘掉
/todo label set 12 论文,紧急    整体替换（原有标签会被摘掉）
/todo edit 123 --label +重要,-待定
```

`/todo edit --label` 是**增量**语义：`+` 添加、`-` 移除，不加符号则表示把标签整体换成这几个。可以重复写，`--label a,b --label +c` 会合并而不是后者覆盖前者。

两个容易踩的点：一是**标签不存在时加标签会自动新建**，所以名字写错不会报错，而是多出一个近似的标签——加标签时用原话里的词，不要自己改写；二是 `PUT /tasks/{id}/labels` 要的是标签 **ID 而不是名字**，且服务端在标签已存在时会返回 `ErrLabelIsAlreadyOnTask`，所以插件先读任务现有标签再决定加还是摘，不会盲目重发。`/todo label list` 可以查出已存在的名字。

### 完成情况

```text
/todo week
→ 📊 最近 7 天完成 12 项（09-21 ~ 09-27）
  1. ✓ #7 修改论文引言
     ✅ 09-25 14:20  📁 fudan-work/SMX/paper
  …
  按项目：fudan-work/SMX/paper 5 项、personal-project 7 项
  完成最多的一天：09-25（4 项）
```

统计依据 `done_at` 而不是 `updated`：改标题、加标签都会刷新 `updated`，用它当完成时间会把"上周完成、这周只是动了动"的任务算进来。`done_at` 是真实的索引列，筛选（`done = true && done_at > now-7d`）交给服务端做。

有一类情况下这个数字会**偏低**，这是 Vikunja 的数据问题而不是统计口径问题：`done_at` 是 Go 的非指针 `time.Time`，未设置时会写成零值 `0001-01-01`。在网页端勾选完成、或让重复任务顺延会正常写入；但**创建时就标记为已完成**、**从看板拖进完成分桶**、**CSV 导入**这几条路径不会写 `done_at`，这些任务不会出现在周报里。零值行由客户端二次过滤掉，不会被当成"最近完成"混进结果。

### 提醒推送通道

提醒通过**可插拔通道**投递，目前实现了 AstrBot 会话通道（QQ / 微信私聊）。`/todo push status` 查看每个入口的登记与开关状态，`/todo push test` 往所有启用的入口真发一条消息，用来确认链路是通的——它不会写提醒去重记录，也不会影响失败计数，可以随时测试。

配置里的 `push_channels` 决定启用哪些通道。留空列表（取消勾选全部选项）等于**全局静音**：提醒不再推送到任何入口。只想关掉某一个入口，在那个入口执行 `/todo remind off` 即可——两者是独立的两层。

同一入口连续三次发送失败会自动关闭该入口的提醒，计数在成功发送后归零。为未来的手机胶囊预留了 `capsule_endpoint` / `capsule_token` 配置键和 `CapsuleChannel` 扩展点，当前版本不实现（是一条未经真实网络验证的路径，不适合先写进代码）。

## 私聊限制

`/todo` 指令组使用 AstrBot 的 `PRIVATE_MESSAGE` 过滤器。所有执行读写的内部方法还会检查 `event.get_group_id()` 和平台类型，防止 LLM Tool 或其他调用路径绕过过滤器。QQ 官方机器人额外检查 `allowed_qq_sender_ids`；微信 `weixin_oc` 私聊始终放行，不受 QQ 白名单影响。

QQ 群即使安装了插件也不能读取或修改 Vikunja。如果同一个 QQ 官方机器人还承担其他功能，其他插件仍可正常处理群聊。

QQ 白名单默认留空。需要时可用 AstrBot 内置 `/sid` 和日志中的 `get_sender_id()` 确认 QQ UID。即使只填写你的 QQ UID，微信 `weixin_oc` 仍可正常使用。

## 项目与查询

`/todo projects` 会根据 Vikunja 的 `parent_project_id` 展示完整层级，例如：

```text
📁 Vikunja 项目树
• Inbox (#1)
• fudan-work (#2)
  • SMX (#3)
    • paper (#4)
• personal-project (#5)
```

`today`、`week`、`overdue` 和 `all` 默认跨所有项目查询，结果包含项目路径，并按优先级降序、截止时间升序排列。使用 `--project` 可以限定到具体项目。

## Vikunja API

实现依据所附 Vikunja OpenAPI v2.3.0：

- `GET /projects` 获取项目和父子关系；`PUT /projects` 新建项目；`POST /projects/{id}` 改项目
- `PUT /projects/{id}/tasks` 创建任务
- `GET /tasks` 跨项目查询并分页
- `GET /tasks/{id}`、`POST /tasks/{id}` 修改任务（含完成/撤销完成）
- `PUT /tasks/{id}/relations`、`DELETE /tasks/{id}/relations/{kind}/{other}` 维护子任务关系
- `PUT /tasks/{id}/labels`、`DELETE /tasks/{id}/labels/{labelId}`、`POST /tasks/{id}/labels/bulk` 维护标签；`GET`/`PUT /labels` 查和建标签
- `repeat_after`（秒，上限十年）、`repeat_mode`（`0` 截止后顺延 / `1` 每月同一天 / `2` 完成后顺延）设置重复规则
- `GET /tasks?filter=done = true && done_at > now-7d` 统计已完成任务；`done_at` 是真实索引列

**`POST /tasks/{id}` 的语义是本次实现里最容易踩的坑，值得单独说明。** 服务端把请求体当作任务的**新状态**，绑定到一个全新的空 Task 上；而"请求体里没出现的字段怎么办"的答案是**混合**的，既不是"一律保留"也不是"一律清空"：

| 字段 | 请求体里省略时的后果 |
| --- | --- |
| `title` | **保留**（`mergo.Merge` 忽略零值兜住了） |
| `done`、`priority`、`description`、`due_date`、`start_date`、`end_date`、`hex_color`、`percent_done`、`repeat_after`、`repeat_mode` | **清空**（紧跟其后的一段显式清零） |
| `is_favorite` | **取消收藏** |
| `reminders`、`assignees` | **全部删除**（`xorm:"-"` 字段，更新路径上无条件生效，没有 nil 守卫） |

也就是说，一条裸体的 `{"done": true}` 请求会保住标题，却顺手清掉截止时间、描述、优先级，取消收藏，并删掉在网页端设的原生提醒和指派。这几个字段都不是数据库列，不出现在任何"可写字段"清单里（Vikunja 前端的清单也不含 `assignees`），照着清单裁剪就正好会踩中。

**因此本插件的硬规则是：任何修改都先 `GET` 任务，原样回传整个对象，再叠加改动，永远不发部分请求体。** 这正是 Vikunja 网页端自己的做法，统一封装在 `merge_update_task()` 里，完成、撤销完成、改期、改标题、批量操作全部走它。项目更新走的是另一条口径（`UpdateProject` 读取的字段可枚举完，所以用白名单回传），原因见 `vikunja.py` 里的注释。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试只覆盖纯逻辑（时间/重复规则解析、参数解析、项目解析、任务筛选、标签、周报格式化、提醒节奏、HTTP 请求体）和插件内部的 docstring 自检。**`main.py` 不会被 import**（它依赖 `astrbot.api`），命令处理与提醒循环只能靠真实 AstrBot 环境验证。改动后的最低自查：

```bash
python -m unittest discover -s tests
python -m py_compile main.py tool_schema.py todo_domain.py vikunja.py state_store.py push_channels.py
```
