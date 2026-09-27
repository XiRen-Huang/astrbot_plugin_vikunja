"""Parsing and presentation logic independent from AstrBot."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


LONG_TERM_WORDS = {
    "论文",
    "paper",
    "项目",
    "计划",
    "长期",
    "持续",
    "习惯",
    "学习",
    "推进",
    "研究",
}
WORK_WORDS = LONG_TERM_WORDS | {"工作", "fudan", "smx", "实验", "会议", "周报"}


def get_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"未知时区：{name}") from exc


def parse_datetime(value: str, tz: ZoneInfo, now: datetime | None = None) -> datetime:
    """Parse common Chinese/ISO due-time expressions and return an aware datetime."""
    value = value.strip().replace("：", ":")
    now = (now or datetime.now(tz)).astimezone(tz)

    relative = re.fullmatch(r"(\d+)\s*(分钟|分|小时|天)后", value)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        delta = timedelta(
            minutes=amount if unit in {"分钟", "分"} else 0,
            hours=amount if unit == "小时" else 0,
            days=amount if unit == "天" else 0,
        )
        return now + delta

    day_match = re.fullmatch(
        r"(今天|明天|后天)(?:\s*(\d{1,2})(?::|点)(\d{1,2})?(?:分)?)?", value
    )
    if day_match:
        offset = {"今天": 0, "明天": 1, "后天": 2}[day_match.group(1)]
        hour = int(day_match.group(2) or 23)
        minute = int(day_match.group(3) or (59 if day_match.group(2) is None else 0))
        return datetime.combine(now.date() + timedelta(days=offset), time(hour, minute), tz)

    weekday_match = re.fullmatch(
        r"(?:(本周|下周))?(?:周|星期)([一二三四五六日天])"
        r"(?:\s*(\d{1,2})(?::|点)(\d{1,2})?(?:分)?)?",
        value,
    )
    if weekday_match:
        target_weekday = "一二三四五六日天".index(weekday_match.group(2))
        target_weekday = min(target_weekday, 6)
        days = (target_weekday - now.weekday()) % 7
        if weekday_match.group(1) == "下周":
            days = days + 7 if days else 7
        hour = int(weekday_match.group(3) or 23)
        minute = int(
            weekday_match.group(4)
            or (59 if weekday_match.group(3) is None else 0)
        )
        result = datetime.combine(now.date() + timedelta(days=days), time(hour, minute), tz)
        if weekday_match.group(1) != "本周" and result <= now:
            result += timedelta(days=7)
        return result

    formats = (
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%m-%d %H:%M",
        "%m-%d",
        "%H:%M",
    )
    for fmt in formats:
        try:
            parsed = datetime.strptime(value, fmt)
        except ValueError:
            continue
        year = parsed.year if "%Y" in fmt else now.year
        month = parsed.month if "%m" in fmt else now.month
        day = parsed.day if "%d" in fmt else now.day
        hour = parsed.hour if "%H" in fmt else 23
        minute = parsed.minute if "%M" in fmt else 59
        result = datetime(year, month, day, hour, minute, tzinfo=tz)
        if "%Y" not in fmt and "%m" in fmt and result < now - timedelta(days=1):
            result = result.replace(year=year + 1)
        return result
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=tz) if parsed.tzinfo is None else parsed.astimezone(tz)
    except ValueError as exc:
        raise ValueError(
            "无法识别时间，可用示例：明天9点、2小时后、2026-07-12 18:00"
        ) from exc


def to_vikunja_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def from_vikunja_time(value: str | None) -> datetime | None:
    if not value or value.startswith("0001-"):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


#: 服务端 ``MaxTaskRepeatAfterSeconds``：``repeat_after`` 上限十年，超过直接被拒。
MAX_REPEAT_SECONDS = 10 * 365 * 24 * 3600

#: 指定"星期几"的规则。Vikunja 没有对应的表达方式——没有"每周一/三/五"，也没有工作日概念。
WEEKDAY_RULE_PATTERN = re.compile(
    r"(周|星期|礼拜)\s*[一二三四五六日天0-7]"
    r"|工作日|周末|节假日"
    # 只认整词和带 day 的整词：写成 ``(mon|...)[a-z]*`` 会把 "monthly" 也吃掉
    # （"mon" + "thly" 首尾都落在词边界上），于是合法的"每月"被当成星期几规则拒绝。
    r"|\b(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(day)?\b",
    re.IGNORECASE,
)

#: "每 N 个月"。``repeat_mode=1``（每月）**忽略** ``repeat_after``，所以按 N 个月递增
#: 没法表达；拿 N×30 天顶替会随月份长度漂移。单说"每月"有数字，不受这条影响。
MONTH_INTERVAL_PATTERN = re.compile(r"每\s*\d+\s*个?\s*月|\d+\s*(mo|month)s?\b", re.IGNORECASE)

#: "完成后…"：Vikunja 的 ``repeat_mode=2`` 从**完成那一刻**起算，而不是从截止时间起算。
FROM_DONE_PREFIXES = ("完成后", "完成之后", "做完后", "done:", "fromdone:")

CHINESE_REPEAT_UNITS = {
    "分钟": 60, "分": 60, "小时": 3600, "天": 86400, "周": 604800,
    "星期": 604800, "礼拜": 604800,
}


def parse_repeat(value: str | None) -> tuple[int, int]:
    """把重复规则解析成 ``(repeat_after 秒, repeat_mode)``。

    Vikunja 能表达的只有三种模式（``pkg/models/tasks.go`` 的 ``TaskRepeatMode``）：
    ``0`` = 截止日期后再过 ``repeat_after`` 秒；``1`` = 每月同一天（**忽略 repeat_after**）；
    ``2`` = 从完成那一刻起再过 ``repeat_after`` 秒。

    所以"每周一/三/五""工作日""每两个月"这类规则**根本无法表达**，这里明确报错并给出可执行
    的替代，而不是凑一个看起来接近的秒数——凑出来的值会在用户毫无察觉的情况下越跑越偏，
    而"任务默默地不在正确的那天出现"是最难被发现的一类错误。
    """
    # CLEAR_WORDS 与"停止重复"是同一件事，共用词表避免模型说"取消"时只落到一句看不懂的报错。
    if not value or value.lower() in CLEAR_WORDS or value.lower() in {"off", "不重复"}:
        return 0, 0
    raw = value.strip()
    normalized = raw.lower()

    for prefix in FROM_DONE_PREFIXES:
        if normalized.startswith(prefix):
            interval, _ = parse_repeat(raw[len(prefix):])
            if interval <= 0:
                raise ValueError("“完成后重复”要跟一个具体间隔，例如 完成后3d、完成后1周")
            return interval, 2

    # 先判"表达不了"的形态，再判能表达的：否则"每周一"会落到最后那句泛泛的用法提示上，
    # 用户看不出真正的原因。
    if WEEKDAY_RULE_PATTERN.search(raw):
        raise ValueError(
            "Vikunja 只能表达“每隔 N 秒重复”和“每月同一天”，无法表达“每周一/三/五”"
            "这类指定星期几的规则。替代方案：① 用 7d —— 如果截止时间正好落在你要的那一天，"
            "效果就是每周同一日；② 拆成三个独立任务，各自设 weekly"
        )
    if MONTH_INTERVAL_PATTERN.search(raw):
        raise ValueError(
            "Vikunja 的“每月”是固定的每月同一天，无法表达“每 N 个月”。"
            "替代方案：用 每月（每月同一天），或用 90d 这类固定天数（注意它不跟着月长走）"
        )

    aliases = {
        "daily": (86400, 0),
        "每天": (86400, 0),
        "每日": (86400, 0),
        "weekly": (604800, 0),
        "每周": (604800, 0),
        "monthly": (0, 1),
        "每月": (0, 1),
    }
    if normalized in aliases:
        return aliases[normalized]

    match = re.fullmatch(r"(\d+)\s*([mhdw])", normalized)
    if not match:
        # "每"是可选的：裸 "1周"/"3天" 在 --repeat 里没有第二种读法，而 "完成后1周"
        # 剥掉前缀后剩下的正是这种形态。
        match = re.fullmatch(
            r"(?:每\s*(?:隔)?\s*)?(\d+)\s*(?:个)?\s*(分钟|分|小时|天|周|星期|礼拜)", raw
        )
        if not match:
            raise ValueError(
                "重复规则可用：daily/每天、weekly/每周、monthly/每月、2d、12h、完成后3d"
                "（从完成时刻起算）；指定星期几的规则 Vikunja 表达不了"
            )
        seconds = int(match.group(1)) * CHINESE_REPEAT_UNITS[match.group(2)]
    else:
        seconds = int(match.group(1)) * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[match.group(2)]

    if seconds > MAX_REPEAT_SECONDS:
        raise ValueError("重复间隔最长十年（Vikunja 的上限）")
    if seconds <= 0:
        raise ValueError("重复间隔必须大于 0")
    return seconds, 0


def parse_duration_minutes(value: str) -> int:
    match = re.fullmatch(r"(\d+)\s*(m|h|d|分钟|小时|天)?", value.lower())
    if not match:
        raise ValueError("提醒时间可用：30m、2h、1d")
    amount = int(match.group(1))
    unit = match.group(2) or "m"
    return amount * {"m": 1, "分钟": 1, "h": 60, "小时": 60, "d": 1440, "天": 1440}[unit]


def secretary_clarification_reason(
    title: str, due: str, repeat: str, project: str, is_reminder: bool
) -> str | None:
    """Return why an LLM must clarify instead of creating a task."""
    lowered = title.casefold()
    reasons: list[str] = []
    if is_reminder and (
        not due
        or not (
            ":" in due
            or "点" in due
            or re.search(r"\d+\s*(分钟|分|小时|天)后", due)
            or "T" in due
        )
    ):
        reasons.append("这是提醒事项，但还没有明确到具体时间的截止日期")
    if not project and any(word in lowered for word in WORK_WORDS):
        reasons.append("这个事项看起来属于工作或项目，需要确认目标项目")
    if not due and not repeat and any(word in lowered for word in LONG_TERM_WORDS):
        reasons.append("这个事项看起来周期较长，需要确认截止时间、计划时长或重复频率")
    if not repeat and any(word in lowered for word in {"每天", "每周", "每月", "定期"}):
        reasons.append("标题包含周期含义，需要确认具体重复频率")
    return "；".join(reasons) if reasons else None


def sender_is_allowed(sender_id: str, configured: list[Any] | None) -> bool:
    allowed = {str(value).strip() for value in (configured or []) if str(value).strip()}
    return not allowed or str(sender_id) in allowed


def platform_sender_is_allowed(
    platform: str, sender_id: str, allowed_qq_sender_ids: list[Any] | None
) -> bool:
    if platform in {"qq_official", "qq_official_webhook"}:
        return sender_is_allowed(sender_id, allowed_qq_sender_ids)
    return True


@dataclass(slots=True)
class AddSpec:
    title: str
    project_selector: str = ""
    description: str = ""
    due: datetime | None = None
    priority: int = 0
    repeat_after: int = 0
    repeat_mode: int = 0
    reminder_minutes: int | None = None

    def payload(self) -> dict[str, Any]:
        result: dict[str, Any] = {"title": self.title, "priority": self.priority}
        if self.description:
            result["description"] = self.description
        if self.due:
            result["due_date"] = to_vikunja_time(self.due)
        if self.repeat_after or self.repeat_mode:
            result["repeat_after"] = self.repeat_after
            result["repeat_mode"] = self.repeat_mode
        return result


def parse_add_arguments(text: str, tz: ZoneInfo, now: datetime | None = None) -> AddSpec:
    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise ValueError("参数引号没有闭合") from exc
    values: dict[str, str] = {}
    title_parts: list[str] = []
    aliases = {
        "--due": "due",
        "-d": "due",
        "--priority": "priority",
        "-p": "priority",
        "--repeat": "repeat",
        "-r": "repeat",
        "--remind": "remind",
        "--desc": "description",
        "--project": "project",
        "-P": "project",
    }
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in aliases:
            if index + 1 >= len(tokens):
                raise ValueError(f"{token} 后缺少参数")
            values[aliases[token]] = tokens[index + 1]
            index += 2
        elif token.startswith("-"):
            raise ValueError(f"未知选项：{token}")
        else:
            title_parts.append(token)
            index += 1
    title = " ".join(title_parts).strip()
    if not title:
        raise ValueError("任务标题不能为空")
    priority = int(values.get("priority", 0))
    if priority < 0 or priority > 5:
        raise ValueError("优先级应为 0 到 5")
    repeat_after, repeat_mode = parse_repeat(values.get("repeat"))
    due = parse_datetime(values["due"], tz, now) if "due" in values else None
    if (repeat_after or repeat_mode) and due is None:
        raise ValueError("重复任务必须同时设置 --due")
    remind = parse_duration_minutes(values["remind"]) if "remind" in values else None
    if remind is not None and due is None:
        raise ValueError("自定义提醒必须同时设置 --due")
    return AddSpec(
        title=title,
        project_selector=values.get("project", ""),
        description=values.get("description", ""),
        due=due,
        priority=priority,
        repeat_after=repeat_after,
        repeat_mode=repeat_mode,
        reminder_minutes=remind,
    )


CLEAR_WORDS = {"clear", "none", "清空", "清除", "取消", "去掉", "删除", "无"}
TRUE_WORDS = {"true", "1", "yes", "y", "done", "完成", "已完成", "好了", "勾选"}
FALSE_WORDS = {"false", "0", "no", "n", "reopen", "undo", "撤销", "撤销完成", "未完成", "恢复"}


def is_clear_request(value: str) -> bool:
    """判断"把该字段清空"的自然语言表达。

    LLM 工具的字符串参数没法用 ``None`` 表达"清空"，只能用词，所以这一层归一化放在
    纯函数里，可单测。注意空串**不是**清空——空串的语义是"本次不改这个字段"。
    """
    return value.strip().casefold() in CLEAR_WORDS


def parse_tristate_bool(value: str | bool | None) -> bool | None:
    """三态布尔：``None`` 表示"没提这个字段"，不要和 ``False`` 混为一谈。

    这是 ``done`` 参数的要害——完成与撤销完成共用一个参数，若把"没提"当成 ``False``，
    模型只改标题时就会把已完成的任务悄悄撤销掉。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = value.strip().casefold()
    if not text:
        return None
    if text in TRUE_WORDS:
        return True
    if text in FALSE_WORDS:
        return False
    raise ValueError(f"无法识别完成状态：{value}，可用 完成/撤销完成")


LABEL_ADD_PREFIXES = ("+", "＋")
LABEL_REMOVE_PREFIXES = ("-", "－")


@dataclass(slots=True)
class LabelChange:
    """一次标签改动，语义是一串**有序操作**：先 ``replace``（非 ``None`` 时把标签整体设成
    这几个，``[]`` 即清空），再 ``add``，最后 ``remove``。

    定义成有序操作而不是"替换 / 增量二选一"，是因为两次 ``--label`` 可以任意组合
    （``--label a,b --label +c``），互斥的话这种合并就没法表达了。对应的 Vikunja 接口也不同：
    带 ``replace`` 走 ``POST /tasks/{id}/labels/bulk``（服务端语义本就是"以这次传的为准"），
    纯增量走 ``PUT`` / ``DELETE /tasks/{id}/labels[/{labelId}]``。

    ``replace is None``（没提到标签）与 ``replace == []``（清空标签）是两回事，所以判空只能
    用 ``is None``——写成 ``not replace`` 会让"清空"退化成"不改"。
    """

    add: list[str]
    remove: list[str]
    replace: list[str] | None = None

    def is_empty(self) -> bool:
        return not self.add and not self.remove and self.replace is None


def _dedupe(names: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for name in names:
        if name not in seen:
            seen.append(name)
    return seen


def merge_label_changes(current: LabelChange | None, change: LabelChange) -> LabelChange:
    """合并同一个 ``--label`` 的多次出现：``add``/``remove`` 累加，``replace`` 以后来的为准。"""
    if current is None:
        return change
    return LabelChange(
        add=_dedupe([*current.add, *change.add]),
        remove=_dedupe([*current.remove, *change.remove]),
        replace=change.replace if change.replace is not None else current.replace,
    )


def parse_label_changes(value: str) -> LabelChange:
    """把 ``+重要,紧急`` / ``-重要`` / ``重要,紧急`` 解析成标签改动。

    判据是**有没有任何一个片段带 +/- 前缀**：只要有一个，全部片段都按增量解读（没前缀的
    算添加）；一个都没有，整串就是"要设成的标签集合"。不这么定的话 ``+a,b`` 没法表达——
    它既像"加 a 和 b"也像"设成 a 和 b"。

    空值表示清空（与 ``--due ""`` 等既有约定一致），对应 ``replace == []``。
    """
    chunks = [c.strip() for c in re.split(r"[,，、]+", value.strip()) if c.strip()]
    if not chunks:
        return LabelChange(add=[], remove=[], replace=[])
    if not any(c.startswith(LABEL_ADD_PREFIXES + LABEL_REMOVE_PREFIXES) for c in chunks):
        return LabelChange(add=[], remove=[], replace=chunks)
    add: list[str] = []
    remove: list[str] = []
    for chunk in chunks:
        if chunk.startswith(LABEL_REMOVE_PREFIXES):
            name = chunk[1:].strip()
            if name:
                remove.append(name)
        elif chunk.startswith(LABEL_ADD_PREFIXES):
            name = chunk[1:].strip()
            if name:
                add.append(name)
        else:
            add.append(chunk)
    return LabelChange(add=add, remove=remove, replace=None)


def label_cache_key(title: Any) -> str:
    """标签名 → 查表用的键：去空格 + casefold。

    Vikunja 的 ``labels`` 表在 ``title`` 上**没有唯一约束**（唯一索引只在 ``id`` 上），所以
    查表必须自己定大小写与空格的等价规则，否则 ``重要`` 和 ``重要 `` 会被当成两个标签。
    匹配用**精确**相等而非子串：子串会让 ``重要`` 命中 ``不重要``。
    """
    return str(title or "").strip().casefold()


def build_label_index(labels: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """标签名 → 标签对象。同名多条时保留 ID 最小的那条。

    Vikunja 允许重名标签（唯一索引只在 ``id`` 上），所以查表必须自己定一条规则。这里**显式**
    按 ID 取最小，而不是"保留最先出现的"——后者会把行为绑在 ``GET /labels`` 的返回顺序上，
    那是服务端的实现细节，换个版本就可能变，而用户看到的会是"改名了"/"标签没摘掉"。
    """
    index: dict[str, dict[str, Any]] = {}
    for label in labels:
        key = label_cache_key(label.get("title"))
        existing = index.get(key)
        if existing is None or int(label.get("id") or 0) < int(existing.get("id") or 0):
            index[key] = label
    return index


def task_label_titles(task: dict[str, Any]) -> list[str]:
    """任务自带的标签名。``GET /tasks`` 会为每条任务填好 ``labels``，不必额外请求。"""
    labels = task.get("labels")
    if not isinstance(labels, list):
        return []
    return [
        str(label.get("title") or "")
        for label in labels
        if isinstance(label, dict) and label.get("title")
    ]


def filter_tasks_by_label(tasks: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """按标签名过滤任务（客户端子串匹配，大小写不敏感）。"""
    needle = query.strip().casefold()
    if not needle:
        return list(tasks)
    return [
        task
        for task in tasks
        if any(needle in title.casefold() for title in task_label_titles(task))
    ]


def format_label_list(labels: list[dict[str, Any]]) -> str:
    if not labels:
        return (
            "还没有任何标签。\n"
            "用 /todo label add <任务ID> <标签名> 给任务加标签时会自动创建新标签。"
        )
    lines = ["🏷️ Vikunja 标签"]
    for label in sorted(labels, key=lambda l: str(l.get("title") or "").casefold()):
        lines.append(f"• #{int(label.get('id') or 0)} {label.get('title', '')}")
    lines.append("加标签：/todo label add <任务ID> <标签名>；摘标签：/todo label rm <任务ID> <标签名>")
    return "\n".join(lines)


@dataclass(slots=True)
class LabelSpec:
    """``/todo label`` 的解析结果。``action`` 取 ``add``/``remove``/``set``/``list``。"""

    action: str
    task_ids: list[int]
    names: list[str]


def parse_label_arguments(text: str) -> LabelSpec:
    """解析 ``/todo label <add|rm|set|list> [任务ID,...] [标签名...]``。

    标签名按逗号或各自独立成词（含空格的标签名用引号）。``set`` 配 ``清空`` 表示摘掉该任务的
    全部标签——用一个明确的词而不是"忘了写名字"，免得漏写参数就静默清空。
    """
    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise ValueError("参数引号没有闭合") from exc
    if not tokens:
        raise ValueError("用法：/todo label <add|rm|set|list> [任务ID,...] [标签名...]")
    actions = {
        "add": "add", "加": "add", "添加": "add",
        "rm": "remove", "remove": "remove", "del": "remove", "删": "remove", "移除": "remove",
        "set": "set", "设为": "set", "替换": "set",
        "list": "list", "列表": "list", "ls": "list",
    }
    action = actions.get(tokens[0].lower())
    if action is None:
        raise ValueError(f"无法识别的操作：{tokens[0]}，可用 add/rm/set/list")
    if action == "list":
        return LabelSpec(action="list", task_ids=[], names=[])
    if len(tokens) < 2 or not tokens[1].strip():
        raise ValueError("用法：/todo label <add|rm|set> <任务ID,...> <标签名...>")
    task_ids = parse_task_ids(tokens[1])
    names: list[str] = []
    for token in tokens[2:]:
        names.extend(part.strip() for part in re.split(r"[,，、]+", token) if part.strip())
    if action == "set" and len(names) == 1 and is_clear_request(names[0]):
        return LabelSpec(action="set", task_ids=task_ids, names=[])
    if not names:
        raise ValueError("没有给出标签名")
    if action in {"add", "remove"} and any(is_clear_request(n) for n in names):
        raise ValueError("add/rm 需要具体标签名；清空标签请用 /todo label set <任务ID> 清空")
    return LabelSpec(action=action, task_ids=task_ids, names=names)


def label_change_from_spec(spec: LabelSpec) -> LabelChange:
    """把 ``/todo label <add|rm|set>`` 的解析结果翻译成 ``LabelChange``。

    ``add``/``remove`` 是增量，``set`` 是整体替换（配 ``清空`` 时 ``names`` 为空 = 清空全部标签）。
    翻译放在这里而不是命令体里，是因为"哪种 action 对应 replace 还是 add/remove"正是最容易
    写反的一处：``set`` 用 ``add`` 实现就会变成"只加不删"，看起来也对，但语义完全错了。
    """
    if spec.action == "set":
        return LabelChange(add=[], remove=[], replace=list(spec.names))
    if spec.action == "add":
        return LabelChange(add=list(spec.names), remove=[])
    return LabelChange(add=[], remove=list(spec.names))


@dataclass(slots=True)
class EditSpec:
    """``/todo edit`` 的解析结果。

    ``payload`` 直接就是 Vikunja 的字段改动（键为服务端字段名），**键是否存在**才是语义：
    没出现的键 = 本次不改；值为 ``None`` 的键 = 清空该字段。这个区分不能丢——Vikunja 的
    ``POST /tasks/{id}`` 把请求体当作任务的**新状态**，对服务端而言"不发这个键"与"发 null"
    都是零值，区别只在于前者被 ``merge_update_task`` 的合并保住了原值，后者是用户明确要求
    清空。所以真正做区分的其实是这个 payload 的键集合，不是值。

    ``project_selector``、提醒阈值和标签改动都不是 Vikunja 的**任务字段**：前者要由调用方解析成
    ``project_id`` 再写进 payload（需要项目列表），提醒阈值是本插件的逐任务值、落进 StateStore，
    标签则走 ``/tasks/{id}/labels`` 那一组专用接口（``POST /tasks/{id}`` 的更新路径根本不看
    ``labels``）。分开存，payload 就始终保持"可直接发给 Vikunja 的任务字段"的纯净语义。
    """

    task_id: int
    payload: dict[str, Any]
    project_selector: str | None = None
    reminder_minutes: int | None = None
    reminder_cleared: bool = False
    label_change: LabelChange | None = None

    def is_empty(self) -> bool:
        return (
            not self.payload
            and self.project_selector is None
            and not self.reminder_cleared
            and self.reminder_minutes is None
            and (self.label_change is None or self.label_change.is_empty())
        )


def parse_edit_arguments(
    text: str, tz: ZoneInfo, now: datetime | None = None
) -> EditSpec:
    """解析 ``/todo edit <任务ID> [选项]``。

    与 ``parse_add_arguments`` 的关键差别是**空值有意义**：``--due ""``、``--due 清空``、
    ``--desc ""`` 表示清空该字段（写进 payload 的是 ``None`` 而非缺键），``--repeat none``
    表示停止重复（``repeat_after``/``repeat_mode`` 归零）。``--remind clear`` 表示删掉逐任务
    阈值、回落到全局默认。清空词表见 ``CLEAR_WORDS``，与 LLM 工具共用。
    """
    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise ValueError("参数引号没有闭合") from exc
    if not tokens:
        raise ValueError("用法：/todo edit <任务ID> [--title 标题] [--due 时间] [--priority 0-5] "
                         "[--repeat 规则] [--project 项目] [--remind 30m] [--desc 描述]")
    task_token = tokens[0].lstrip("#")
    if not task_token.isdigit():
        raise ValueError("用法：/todo edit <任务ID> …，任务 ID 是列表里 # 后面的数字")
    task_id = int(task_token)

    aliases = {
        "--title": "title",
        "-t": "title",
        "--desc": "description",
        "--description": "description",
        "--due": "due",
        "-d": "due",
        "--priority": "priority",
        "-p": "priority",
        "--repeat": "repeat",
        "-r": "repeat",
        "--project": "project",
        "-P": "project",
        "--remind": "remind",
        "--label": "label",
        "--labels": "label",
        "--tag": "label",
    }
    payload: dict[str, Any] = {}
    project_selector: str | None = None
    reminder_minutes: int | None = None
    reminder_cleared = False
    label_change: LabelChange | None = None
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token not in aliases:
            raise ValueError(f"未知选项：{token}" if token.startswith("-") else f"多余参数：{token}")
        if index + 1 >= len(tokens):
            raise ValueError(f"{token} 后缺少参数")
        raw = tokens[index + 1]
        kind = aliases[token]
        if kind == "title":
            if not raw.strip():
                raise ValueError("任务标题不能为空")
            payload["title"] = raw.strip()
        elif kind == "description":
            payload["description"] = None if (is_clear_request(raw) or not raw.strip()) else raw.strip()
        elif kind == "due":
            if is_clear_request(raw) or not raw.strip():
                payload["due_date"] = None
            else:
                payload["due_date"] = to_vikunja_time(parse_datetime(raw, tz, now))
        elif kind == "priority":
            try:
                priority = int(raw)
            except ValueError as exc:
                raise ValueError("优先级应为 0 到 5") from exc
            if priority < 0 or priority > 5:
                raise ValueError("优先级应为 0 到 5")
            payload["priority"] = priority
        elif kind == "repeat":
            repeat_after, repeat_mode = parse_repeat(raw)
            payload["repeat_after"] = repeat_after
            payload["repeat_mode"] = repeat_mode
        elif kind == "project":
            if not raw.strip():
                raise ValueError("--project 需要一个项目路径或 ID")
            project_selector = raw.strip()
        elif kind == "remind":
            if is_clear_request(raw) or raw.strip().casefold() in {"default", "默认"}:
                reminder_cleared = True
            else:
                reminder_minutes = parse_duration_minutes(raw)
        elif kind == "label":
            # 多次写 --label 是合并而不是后者覆盖前者：--label a,b --label +c 要能表达。
            label_change = merge_label_changes(label_change, parse_label_changes(raw))
        index += 2
    spec = EditSpec(
        task_id=task_id,
        payload=payload,
        project_selector=project_selector,
        reminder_minutes=reminder_minutes,
        reminder_cleared=reminder_cleared,
        label_change=label_change,
    )
    if spec.is_empty():
        raise ValueError("没有指定要修改的内容")
    return spec


def task_sort_key(task: dict[str, Any]) -> tuple[int, datetime, int]:
    due = from_vikunja_time(task.get("due_date")) or datetime.max.replace(tzinfo=timezone.utc)
    return (-int(task.get("priority") or 0), due, int(task.get("id") or 0))


def select_tasks(
    tasks: list[dict[str, Any]], scope: str, tz: ZoneInfo, now: datetime | None = None
) -> list[dict[str, Any]]:
    now = (now or datetime.now(tz)).astimezone(tz)
    result: list[dict[str, Any]] = []
    for task in tasks:
        if task.get("done"):
            continue
        due = from_vikunja_time(task.get("due_date"))
        local_due = due.astimezone(tz) if due else None
        if scope == "today" and (not local_due or local_due.date() > now.date()):
            continue
        if scope == "overdue" and (not local_due or local_due >= now):
            continue
        if scope == "week" and (
            not local_due or local_due.date() > now.date() + timedelta(days=7)
        ):
            continue
        result.append(task)
    return sorted(result, key=task_sort_key)


def next_poll_delay(
    tasks: list[dict[str, Any]],
    default_minutes: int,
    overrides: dict[int, int],
    notified: set[int],
    now: datetime,
    poll_interval: int,
    idle_interval: int,
) -> float:
    """返回提醒轮询下一次应睡眠的秒数。

    每个未完成且有截止时间的任务，其提醒时刻为 ``due - 该任务的提前分钟数``（逐任务阈值
    来自 ``overrides``，缺省用 ``default_minutes``）。取最早的提醒时刻决定睡眠时长：

    - 已经到点或即将到点（``delta <= poll_interval``）→ 保持 ``poll_interval``；
    - 否则睡到刚好该提醒的那一刻，但落在 ``[poll_interval, idle_interval]`` 区间内。

    没有任何可提醒的任务时睡满 ``idle_interval``。``idle_interval`` 意外小于
    ``poll_interval`` 时也绝不会睡到 ``poll_interval`` 以下。

    ``notified`` 是所有启用渠道都已收到过本次提醒的任务 ID 集合（去重键含 due_date，
    改期会重新武装）。这些任务**不参与**节奏计算：逾期任务永远满足"已经到点"，
    若不放行，待办列表里只要躺着一条没清理的逾期任务，轮询就永远退不回空闲间隔。
    """
    soonest: datetime | None = None
    for task in tasks:
        if task.get("done"):
            continue
        if int(task["id"]) in notified:
            continue
        due = from_vikunja_time(task.get("due_date"))
        if not due:
            continue
        minutes = int(overrides.get(int(task["id"]), default_minutes))
        moment = due - timedelta(minutes=minutes)
        if soonest is None or moment < soonest:
            soonest = moment
    if soonest is None:
        return float(idle_interval)
    delta = (soonest - now).total_seconds()
    if delta <= poll_interval:
        return float(poll_interval)
    return float(max(poll_interval, min(idle_interval, delta)))


def format_task_list(
    tasks: list[dict[str, Any]],
    title: str,
    tz: ZoneInfo,
    limit: int = 20,
    project_paths: dict[int, str] | None = None,
) -> str:
    if not tasks:
        return f"{title}\n🎉 没有未完成任务"
    lines = [title]
    for task in tasks[: max(1, limit)]:
        priority = int(task.get("priority") or 0)
        due = from_vikunja_time(task.get("due_date"))
        due_text = due.astimezone(tz).strftime("%m-%d %H:%M") if due else "无截止时间"
        repeat = " ↻" if int(task.get("repeat_after") or 0) or int(task.get("repeat_mode") or 0) else ""
        lines.append(
            f"{len(lines)}. [P{priority}] #{task.get('id')} {task.get('title', '')}{repeat}\n"
            f"   📁 {(project_paths or {}).get(int(task.get('project_id') or 0), '未知项目')}  ⏰ {due_text}"
        )
    if len(tasks) > limit:
        lines.append(f"…另有 {len(tasks) - limit} 项未显示")
    return "\n".join(lines)


def completed_tasks_since(
    tasks: list[dict[str, Any]], since: datetime
) -> list[dict[str, Any]]:
    """过滤出 ``done_at`` 落在 ``since`` 之后的已完成任务，按完成时间倒序。

    服务端已经用 ``done_at > now-Nd`` 筛过一遍，这里再筛是**防御性的第二道**：``done_at``
    是 Go 的 ``time.Time`` 非指针，未设置时会序列化成 ``0001-01-01T00:00:00Z``。那种零值
    在字符串上比任何真实日期都"小"、在字典序上也排在前面，任何按时间排序或取"最近一条"
    的逻辑都会被它污染。``from_vikunja_time`` 把零值和 null 都归成 ``None``，正好是这里
    要的判据。
    """
    kept: list[dict[str, Any]] = []
    for task in tasks:
        done_at = from_vikunja_time(task.get("done_at"))
        if done_at is None or done_at < since:
            continue
        kept.append({**task, "done_at": done_at})
    kept.sort(key=lambda task: task["done_at"], reverse=True)
    return kept


def format_weekly_report(
    tasks: list[dict[str, Any]],
    project_paths: dict[int, str],
    tz: ZoneInfo,
    now: datetime,
    *,
    days: int = 7,
    limit: int = 40,
) -> str:
    """渲染完成情况汇总。``tasks`` 应当是 ``completed_tasks_since`` 的输出。"""
    since = now - timedelta(days=days)
    window = f"{since.astimezone(tz):%m-%d} ~ {now.astimezone(tz):%m-%d}"
    if not tasks:
        # 说清"是这周没完成"，而不是让用户怀疑是不是统计坏了：done_at 有已知的漏记情形
        # （建的时候就是已完成的任务、看板拖进完成桶、CSV 导入），所以空结果不等于零工作量。
        return f"📊 最近 {days} 天（{window}）没有已完成的任务"

    lines = [f"📊 最近 {days} 天完成 {len(tasks)} 项（{window}）"]
    for index, task in enumerate(tasks[: max(1, limit)], start=1):
        done_at = task["done_at"].astimezone(tz)
        path = project_paths.get(int(task.get("project_id") or 0), "未知项目")
        lines.append(
            f"{index}. ✓ #{task.get('id')} {task.get('title', '')}\n"
            f"   ✅ {done_at:%m-%d %H:%M}  📁 {path}"
        )
    if len(tasks) > limit:
        lines.append(f"…另有 {len(tasks) - limit} 项未显示")

    tally: dict[str, int] = {}
    for task in tasks:
        path = project_paths.get(int(task.get("project_id") or 0), "未知项目")
        tally[path] = tally.get(path, 0) + 1
    # 按项目名排序而不是按数量：数量会随每周数据跳动，名字稳定，看起来才像同一份周报。
    lines.append("按项目：" + "、".join(f"{path} {count} 项" for path, count in sorted(tally.items())))

    by_day: dict[str, int] = {}
    for task in tasks:
        key = f"{task['done_at'].astimezone(tz):%m-%d}"
        by_day[key] = by_day.get(key, 0) + 1
    if len(by_day) > 1:
        busiest = max(by_day.items(), key=lambda item: item[1])
        lines.append(f"完成最多的一天：{busiest[0]}（{busiest[1]} 项）")
    return "\n".join(lines)


def format_task_detail(
    task: dict[str, Any],
    project_path: str,
    tz: ZoneInfo,
    now: datetime | None = None,
) -> str:
    """单个任务的完整详情。

    列表为了塞得下，只能显示优先级、标题、项目和截止时间——描述、标签、提醒、重复规则
    和子任务都看不见，于是"备注到底写进去了没有"这种事没法目视复核，只能去网页端翻。
    这里把列表省略掉的字段全部摊开，逐项都标明"有"还是"没有"，**空字段也照打**：
    看不到"描述：（无）"就分不清"没写进去"和"这一项没显示"。
    """
    now = now or datetime.now(tz)
    lines = [f"📄 #{task.get('id')} {task.get('title', '')}"]

    status = "✅ 已完成" if task.get("done") else "▫️ 未完成"
    done_at = from_vikunja_time(task.get("done_at"))
    if task.get("done") and done_at:
        status += f"（{done_at.astimezone(tz):%Y-%m-%d %H:%M}）"
    lines.append(f"状态：{status}")

    lines.append(f"项目：{project_path}")

    priority = int(task.get("priority") or 0)
    lines.append(f"优先级：P{priority}" if priority else "优先级：无")

    due = from_vikunja_time(task.get("due_date"))
    if due:
        due_text = f"{due.astimezone(tz):%Y-%m-%d %H:%M}"
        # 逾期只在没完成时提示；已完成的任务说"逾期"没有意义。
        if not task.get("done") and due < now:
            due_text += "（已逾期）"
    else:
        due_text = "无"
    lines.append(f"截止：{due_text}")

    start = from_vikunja_time(task.get("start_date"))
    end = from_vikunja_time(task.get("end_date"))
    if start or end:
        span = " ~ ".join(
            moment.astimezone(tz).strftime("%Y-%m-%d %H:%M") if moment else "…"
            for moment in (start, end)
        )
        lines.append(f"起止：{span}")

    lines.append(f"重复：{describe_repeat(task)}")

    reminder_minutes = task.get("_reminder_minutes")
    if reminder_minutes:
        lines.append(f"提醒：提前 {int(reminder_minutes)} 分钟")
    else:
        lines.append("提醒：跟随全局默认")

    labels = task_label_titles(task)
    lines.append(f"标签：{'、'.join(labels) if labels else '无'}")

    description = str(task.get("description") or "").strip()
    lines.append(f"描述：{description if description else '无'}")

    children = subtasks_of(task)
    if children:
        lines.append(f"子任务（{len(children)}）：")
        for child in sorted(children, key=lambda item: (bool(item.get("done")), task_sort_key(item))):
            mark = "✅" if child.get("done") else "▫️"
            lines.append(f"  {mark} #{child.get('id')} {child.get('title', '')}")
    else:
        lines.append("子任务：无")

    parents = parents_of(task)
    if parents:
        names = "、".join(f"#{item.get('id')} {item.get('title', '')}" for item in parents)
        lines.append(f"父任务：{names}")

    return "\n".join(lines)


def describe_repeat(task: dict[str, Any]) -> str:
    """把 ``repeat_after``/``repeat_mode`` 翻回人能读的规则。"""
    seconds = int(task.get("repeat_after") or 0)
    mode = int(task.get("repeat_mode") or 0)
    if mode == 1:
        # 这个模式下服务端忽略 repeat_after，所以不能按秒数描述。
        return "每月同一天"
    if not seconds:
        return "不重复"
    text = f"每 {format_duration(seconds)}"
    if mode == 2:
        text += "（从完成时刻起算）"
    return text


def format_duration(seconds: int) -> str:
    """秒数转成人读的间隔，优先用能整除的最大单位。"""
    for unit, size in (("天", 86400), ("小时", 3600), ("分钟", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}秒"


# 审计里每个项目最多存这么多条任务引用，显示时再压到更少——记录要留证据，但 KV 不能无限长。
AUDIT_TASK_REF_LIMIT = 50
AUDIT_DISPLAY_LIMIT = 10
AUDIT_TITLE_DISPLAY_LIMIT = 20
DELETION_SOURCE_LABELS = {"command": "手动命令", "tool": "自然语言"}


def summarize_task_refs(
    refs: list[dict[str, Any]],
    total: int,
    *,
    limit: int = AUDIT_TITLE_DISPLAY_LIMIT,
) -> str:
    """审计记录里"删掉的是哪些任务"那一行：``#12 写引言、#15 读论文``。

    ``total`` 是删除当时项目里的真实任务数，``refs`` 只是当时留下来的引用（有上限）。
    未列出的数量按 ``total`` 算而不是按 ``len(refs)``：否则存储上限会把缺口盖成
    "还有 0 条"，让一份本该留证据的记录反而报小了。
    """
    shown = [f"#{ref.get('id', '?')} {ref.get('title') or '(无标题)'}" for ref in refs[:limit]]
    if not shown:
        return "（没有留下任务明细）"
    text = "、".join(shown)
    missing = int(total) - len(shown)
    if missing > 0:
        text += f" …（还有 {missing} 条未列出）"
    return text


def format_deletion_audit(
    entries: list[dict[str, Any]], *, limit: int = AUDIT_DISPLAY_LIMIT
) -> str:
    """渲染项目删除审计。``entries`` 最近的在前（``StateStore`` 就按这个顺序存）。"""
    if not entries:
        return "🗂 还没有删除过项目"
    shown = list(entries[:limit])
    header = f"🗂 项目删除审计：共 {len(entries)} 次"
    if len(entries) > len(shown):
        header += f"，下面是最新的 {len(shown)} 次"
    lines = [header]
    for index, entry in enumerate(shown, start=1):
        source = DELETION_SOURCE_LABELS.get(str(entry.get("source")), "来源未知")
        total = int(entry.get("tasks") or 0)
        lines.append(
            f"{index}. {entry.get('at') or '?'}  {entry.get('path') or '?'}"
            f"（#{entry.get('project_id') or '?'}），删掉 {total} 个任务（{source}）"
        )
        if total:
            lines.append("   " + summarize_task_refs(entry.get("task_refs") or [], total))
    return "\n".join(lines)


def build_project_paths(projects: list[dict[str, Any]]) -> dict[int, str]:
    by_id = {int(project["id"]): project for project in projects}
    cache: dict[int, str] = {}

    def build(project_id: int, visiting: set[int]) -> str:
        if project_id in cache:
            return cache[project_id]
        project = by_id[project_id]
        title = str(project.get("title") or project_id)
        parent_id = int(project.get("parent_project_id") or 0)
        if not parent_id or parent_id not in by_id or parent_id in visiting:
            cache[project_id] = title
        else:
            cache[project_id] = f"{build(parent_id, visiting | {project_id})}/{title}"
        return cache[project_id]

    for project_id in by_id:
        build(project_id, set())
    return cache


def project_name_key(value: str) -> str:
    """项目名比较用的归一化：丢掉 emoji、空格和标点，只留字母/数字/CJK 与路径分隔符。

    项目名常带装饰——服务端建的收件箱是 "Inbox"，但用户很可能在网页端把它改成
    "📥 Inbox"，而说话时只会念"Inbox"。空格和连字符的差异（"reading list" /
    "reading-list"）同理，不该让查找失败。

    用 ``isalnum()`` 而不是字符白名单：CJK 字符天然算字母，emoji 和标点都不算，
    所以中英项目名都走得通，也不用维护一张"什么算装饰"的表。
    """
    return "".join(ch for ch in str(value).casefold() if ch.isalnum() or ch == "/")


def resolve_project(
    projects: list[dict[str, Any]], selector: str
) -> tuple[dict[str, Any], str]:
    selector = selector.strip().replace(" > ", "/").strip("/")
    if not selector:
        raise ValueError("没有指定目标项目")
    paths = build_project_paths(projects)
    by_id = {int(project["id"]): project for project in projects}
    if selector.isdigit() and int(selector) in by_id:
        project = by_id[int(selector)]
        return project, paths[int(selector)]

    def describe(pids: list[int]) -> str:
        return "、".join(f"{paths[pid]} (#{pid})" for pid in pids)

    # 逐档放宽：精确匹配（路径、标题）先跑一遍，全部落空后才用忽略装饰的比较。
    # 顺序不能反——否则一个装饰差异会盖过某处明明写对了的精确匹配。
    for key_of in (lambda value: str(value).casefold(), project_name_key):
        wanted = key_of(selector)
        path_matches = [pid for pid, path in paths.items() if key_of(path) == wanted]
        if len(path_matches) == 1:
            pid = path_matches[0]
            return by_id[pid], paths[pid]
        title_matches = [
            int(project["id"])
            for project in projects
            if key_of(project.get("title", "")) == wanted
        ]
        if len(title_matches) == 1:
            pid = title_matches[0]
            return by_id[pid], paths[pid]
        matches = path_matches or title_matches
        if matches:
            raise ValueError(f"项目名不唯一，请使用完整路径或 ID：{describe(matches)}")
    raise ValueError(f"找不到项目：{selector}。请先查询项目树")


def filter_tasks_by_query(tasks: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """按关键词过滤任务，匹配标题**或描述**，大小写不敏感。

    刻意不在服务端搜：``GET /tasks?s=`` 的行为随版本和数据库后端变化——v2.3.0 的字段文档
    写的是"只匹配标题"，而本机 main 检出已经走 ``MultiFieldSearchWithBoosts(["title",
    "description"])``（PostgreSQL 全文检索），SQLite 上又是另一套。插件本来就把任务列表
    整个拉回本地，在这里做子串匹配与版本无关，还能顺带保证描述里的关键词搜得到。
    """
    needle = query.strip().casefold()
    if not needle:
        return list(tasks)
    return [
        task
        for task in tasks
        if needle in str(task.get("title") or "").casefold()
        or needle in str(task.get("description") or "").casefold()
    ]


def parse_task_ids(value: str) -> list[int]:
    """解析 ``12,15,#20`` 这类任务 ID 列表，保序去重。"""
    ids: list[int] = []
    for chunk in re.split(r"[,，、\s]+", value.strip()):
        token = chunk.lstrip("#").strip()
        if not token:
            continue
        if not token.isdigit():
            raise ValueError(f"无法识别的任务 ID：{chunk}；多个 ID 用逗号分隔，如 12,15")
        task_id = int(token)
        if task_id not in ids:
            ids.append(task_id)
    if not ids:
        raise ValueError("没有给出任务 ID，多个 ID 用逗号分隔，如 12,15")
    return ids


SUBTASK_RELATION = "subtask"
PARENT_RELATION = "parenttask"


def related_tasks_of(task: dict[str, Any], relation_kind: str) -> list[dict[str, Any]]:
    """从任务详情里按关系类型取相关任务。

    Vikunja 的任务结构体上**没有** ``parent_id``：父子关系是 ``task_relations`` 表里的一行，
    读单个任务时以 ``related_tasks`` 这个"按关系类型分组"的字典返回（``RelatedTaskMap``）。
    关系是有方向的且服务端会自动建反向关系，所以同一个孩子在父任务那边是 ``subtask``、
    在自己这边是 ``parenttask``。任务没有任何关系时该键缺席，取不到就是空列表，不是错误。
    """
    related = task.get("related_tasks")
    if not isinstance(related, dict):
        return []
    items = related.get(relation_kind)
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def subtasks_of(task: dict[str, Any]) -> list[dict[str, Any]]:
    """取子任务列表。"""
    return related_tasks_of(task, SUBTASK_RELATION)


def parents_of(task: dict[str, Any]) -> list[dict[str, Any]]:
    """取父任务列表——摘掉父子关系时要知道该去哪一侧删关系。"""
    return related_tasks_of(task, PARENT_RELATION)


def format_subtask_list(
    parent: dict[str, Any], children: list[dict[str, Any]], tz: ZoneInfo
) -> str:
    parent_id = parent.get("id")
    header = f"🌱 #{parent_id} {parent.get('title', '')} 的子任务"
    if not children:
        return f"{header}\n（暂无子任务，可用 /todo subtask add {parent_id} <标题> 添加）"
    ordered = sorted(children, key=lambda item: (bool(item.get("done")), task_sort_key(item)))
    lines = [header]
    for index, child in enumerate(ordered, start=1):
        mark = "✅" if child.get("done") else "▫️"
        due = from_vikunja_time(child.get("due_date"))
        due_text = due.astimezone(tz).strftime("%m-%d %H:%M") if due else "无截止时间"
        lines.append(f"{index}. {mark} #{child.get('id')} {child.get('title', '')}  ⏰ {due_text}")
    return "\n".join(lines)


@dataclass(slots=True)
class SubtaskSpec:
    action: str  # add / list / rm
    parent_id: int
    title: str = ""
    child_id: int | None = None


def parse_subtask_arguments(text: str) -> SubtaskSpec:
    """解析 ``/todo subtask <add|list|rm> …``。"""
    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise ValueError("参数引号没有闭合") from exc
    if not tokens:
        raise ValueError(
            "用法：/todo subtask add <父任务ID> <标题> / list <任务ID> / rm <父任务ID> <子任务ID>"
        )
    actions = {
        "add": "add", "添加": "add", "新建": "add", "挂": "add",
        "list": "list", "列表": "list", "查看": "list", "ls": "list",
        "rm": "rm", "remove": "rm", "删除": "rm", "移除": "rm", "摘": "rm",
    }
    action = actions.get(tokens[0].lower())
    if action is None:
        raise ValueError("子任务动作可用：add 添加、list 查看、rm 移除")

    def task_id_at(index: int, label: str) -> int:
        if index >= len(tokens):
            raise ValueError(f"缺少{label}")
        token = tokens[index].lstrip("#")
        if not token.isdigit():
            raise ValueError(f"{label}应是任务 ID（列表中 # 后面的数字）")
        return int(token)

    if action == "list":
        return SubtaskSpec(action="list", parent_id=task_id_at(1, "任务 ID"))
    if action == "rm":
        return SubtaskSpec(
            action="rm",
            parent_id=task_id_at(1, "父任务 ID"),
            child_id=task_id_at(2, "子任务 ID"),
        )
    parent_id = task_id_at(1, "父任务 ID")
    title = " ".join(tokens[2:]).strip()
    if not title:
        raise ValueError("子任务标题不能为空")
    return SubtaskSpec(action="add", parent_id=parent_id, title=title)


@dataclass(slots=True)
class BulkSpec:
    action: str  # done / reopen / priority / delete
    task_ids: list[int]
    priority: int | None = None


def parse_bulk_arguments(text: str) -> BulkSpec:
    """解析 ``/todo bulk done|reopen|pri|delete …``。"""
    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise ValueError("参数引号没有闭合") from exc
    if not tokens:
        raise ValueError(
            "用法：/todo bulk done <任务ID,...> / reopen <任务ID,...> / pri <0-5> <任务ID,...> / delete <任务ID,...>"
        )
    actions = {
        "done": "done", "完成": "done", "勾选": "done",
        "reopen": "reopen", "撤销完成": "reopen", "恢复": "reopen",
        "pri": "priority", "priority": "priority", "优先级": "priority",
        "delete": "delete", "rm": "delete", "删除": "delete",
    }
    action = actions.get(tokens[0].lower())
    if action is None:
        raise ValueError("批量动作可用：done 完成、reopen 撤销完成、pri 改优先级、delete 删除")
    if action == "priority":
        if len(tokens) < 3:
            raise ValueError("用法：/todo bulk pri <0-5> <任务ID,...>")
        try:
            priority = int(tokens[1])
        except ValueError as exc:
            raise ValueError("优先级应为 0 到 5") from exc
        if priority < 0 or priority > 5:
            raise ValueError("优先级应为 0 到 5")
        return BulkSpec(
            action=action, task_ids=parse_task_ids(" ".join(tokens[2:])), priority=priority
        )
    if len(tokens) < 2:
        raise ValueError(f"用法：/todo bulk {tokens[0]} <任务ID,...>")
    return BulkSpec(action=action, task_ids=parse_task_ids(" ".join(tokens[1:])))


@dataclass(slots=True)
class ProjectSpec:
    action: str  # create / rename / delete
    name: str
    parent_selector: str = ""
    new_name: str = ""
    #: 仅 delete：项目里还有任务时是否照样删。删项目会**硬删除**其中所有任务，
    #: 所以默认要求项目为空，只有用户显式写 --force 才放行。
    force: bool = False


def parse_project_arguments(text: str) -> ProjectSpec:
    """解析 ``/todo project new <名称> [--parent 父项目]``、``rename <项目> <新名称>``、``delete <项目> [--force]``。"""
    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise ValueError("参数引号没有闭合") from exc
    if not tokens:
        raise ValueError(
            "用法：/todo project new <名称> [--parent 父项目]、rename <项目> <新名称>、"
            "delete <项目> [--force]"
        )
    actions = {
        "new": "create", "create": "create", "新建": "create", "创建": "create",
        "rename": "rename", "重命名": "rename", "改名": "rename",
        "delete": "delete", "rm": "delete", "remove": "delete", "删除": "delete",
    }
    action = actions.get(tokens[0].lower())
    if action is None:
        raise ValueError("项目动作可用：new 新建、rename 重命名、delete 删除")

    if action == "rename":
        if len(tokens) < 3:
            raise ValueError("用法：/todo project rename <项目> <新名称>")
        new_name = " ".join(tokens[2:]).strip()
        if not new_name:
            raise ValueError("新项目名称不能为空")
        return ProjectSpec(action="rename", name=tokens[1].strip(), new_name=new_name)

    if action == "delete":
        force = False
        positional = []
        for token in tokens[1:]:
            if token in {"--force", "-f", "强制"}:
                force = True
                continue
            if token.startswith("-"):
                raise ValueError(f"未知选项：{token}")
            positional.append(token)
        name = " ".join(positional).strip()
        if not name:
            raise ValueError("用法：/todo project delete <项目> [--force]")
        return ProjectSpec(action="delete", name=name, force=force)

    positional: list[str] = []
    parent_selector = ""
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in {"--parent", "-P"}:
            if index + 1 >= len(tokens):
                raise ValueError("--parent 后缺少父项目选择器")
            parent_selector = tokens[index + 1].strip()
            index += 2
            continue
        if token.startswith("-"):
            raise ValueError(f"未知选项：{token}")
        positional.append(token)
        index += 1
    name = " ".join(positional).strip()
    if not name:
        raise ValueError("用法：/todo project new <名称> [--parent 父项目]")
    return ProjectSpec(action="create", name=name, parent_selector=parent_selector)


def parse_move_arguments(text: str) -> tuple[int, str]:
    """解析 ``/todo move <任务ID> <项目选择器>``，返回 ``(任务ID, 项目选择器)``。"""
    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise ValueError("参数引号没有闭合") from exc
    if len(tokens) < 2:
        raise ValueError("用法：/todo move <任务ID> <项目路径或ID>")
    task_token = tokens[0].lstrip("#")
    if not task_token.isdigit():
        raise ValueError("任务 ID 是列表中 # 后面的数字")
    selector = " ".join(tokens[1:]).strip()
    if not selector:
        raise ValueError("用法：/todo move <任务ID> <项目路径或ID>")
    return int(task_token), selector


def format_project_tree(projects: list[dict[str, Any]]) -> str:
    children: dict[int, list[dict[str, Any]]] = {}
    ids = {int(project["id"]) for project in projects}
    for project in projects:
        parent = int(project.get("parent_project_id") or 0)
        if parent not in ids:
            parent = 0
        children.setdefault(parent, []).append(project)

    lines = ["📁 Vikunja 项目树"]

    def walk(parent: int, depth: int) -> None:
        for project in sorted(
            children.get(parent, []), key=lambda item: str(item.get("title", "")).casefold()
        ):
            lines.append(f"{'  ' * depth}• {project.get('title', '')} (#{project['id']})")
            walk(int(project["id"]), depth + 1)

    walk(0, 0)
    return "\n".join(lines)
