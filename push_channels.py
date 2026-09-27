"""可插拔的推送通道。

提醒的投递目标从"一个私聊会话"抽象成"一个通道"，让"往哪里发"和"发什么"解耦。
当前只实现 AstrBot 会话通道（QQ 官方机器人 / 微信 ``weixin_oc`` 私聊），手机胶囊等其它
投递方式留出接口但**尚未实现**——见文件末尾的扩展点说明。

这个模块刻意不 import ``astrbot``：消息对象的构造由调用方以 ``send_text`` 函数注入
（``main.py`` 里是 ``MessageChain().message(text)`` 那一步）。因此它可以像
``todo_domain.py`` / ``tool_schema.py`` 一样被测试直接顶层 import。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Iterable, Mapping, Protocol, runtime_checkable

#: 已实现的通道种类。``push_channels`` 配置项里出现其它值会被忽略（并在 /todo push status 里点名）。
SUPPORTED_KINDS = ("astrbot",)

#: 往一个 ``umo`` 发送纯文本，返回底层 ``context.send_message`` 的原始结果。
#: 返回 ``False`` 表示投递失败；``None`` 与 ``True`` 都按成功处理（见 ``AstrBotChannel.send``）。
SendText = Callable[[str, str], Awaitable[Any]]


@runtime_checkable
class PushChannel(Protocol):
    """一个投递目标。

    ``name`` 同时是提醒去重键的前缀（``{name}|{task_id}|{due_date}``），所以它对每个通道
    必须唯一且稳定——``AstrBotChannel`` 直接用它登记的 ``umo``。
    """

    name: str

    def is_enabled(self) -> bool: ...

    async def send(self, text: str) -> bool: ...


class AstrBotChannel:
    """把一条已登记的私聊会话当作推送通道。"""

    kind = "astrbot"

    def __init__(self, umo: str, send_text: SendText, *, enabled: bool = True) -> None:
        self.name = umo
        self.umo = umo
        self._send_text = send_text
        self._enabled = enabled

    def is_enabled(self) -> bool:
        return self._enabled

    async def send(self, text: str) -> bool:
        """投递纯文本；``False`` 表示失败。

        AstrBot 的 ``context.send_message`` 在**找不到会话**时返回 ``False``，成功时返回的
        未必是 ``True``（某些平台适配器返回 ``None``）。所以判据只能是``is not False``：
        写成 ``if result:`` 会把成功当失败，写反了则会把失败当成功、让渠道永远不被停用。
        """
        return await self._send_text(self.umo, text) is not False


def normalize_kinds(value: Any) -> set[str]:
    """把 ``push_channels`` 配置值规整成通道种类集合。

    接受列表（WebUI 的多选）或逗号分隔的字符串（手工改配置时的常见写法）。

    **只把"缺省"当成用默认集合，不把"空"当成缺省**：``None`` → ``{"astrbot"}``，而 ``[]``
    → 空集（用户把选项全取消了，就是不想让任何通道收提醒）。区分这两者是安全的，因为
    AstrBot 的 ``check_config_integrity()`` 只在配置项**缺失**或为 ``None`` 时才回填 schema
    默认值——空列表会被原样保留，所以它确实是一次人为选择，而不是"这项还没被写过"。
    反过来把空列表当缺省，就等于让一个配置项永远无法表达"关掉"。
    """
    if value is None:
        return set(SUPPORTED_KINDS)
    if isinstance(value, str):
        parts: Iterable[Any] = value.split(",")
    else:
        parts = value
    return {str(part).strip() for part in parts if str(part).strip()}


def unknown_kinds(value: Any) -> list[str]:
    """配置里写了但还没有实现的通道种类，用于在状态里如实点名而不是假装它生效了。"""
    return sorted(normalize_kinds(value) - set(SUPPORTED_KINDS))


def build_channels(
    registered: Mapping[str, Mapping[str, Any]],
    send_text: SendText,
    *,
    enabled_kinds: Any = None,
) -> list[AstrBotChannel]:
    """按已登记的渠道构造通道列表。

    返回**全部**登记渠道（含被单独关掉提醒的那些），由调用方用 ``is_enabled()`` 取舍——
    ``/todo push status`` 需要看到被关掉的那些，而派发路径只看启用的。``enabled_kinds`` 里
    没有 ``astrbot`` 时返回空列表，表示这个投递方式被整体停用了。
    """
    if "astrbot" not in normalize_kinds(enabled_kinds):
        return []
    return [
        AstrBotChannel(
            str(channel.get("umo") or key),
            send_text,
            enabled=bool(channel.get("reminders_enabled", True)),
        )
        for key, channel in registered.items()
    ]


def format_push_status(
    *,
    registered: Mapping[str, Mapping[str, Any]],
    failures: Mapping[str, int],
    enabled_kinds: Any = None,
    capsule_configured: bool = False,
) -> str:
    """``/todo push status`` 的文案。纯函数，便于在没有 AstrBot 的环境里断言。"""
    kinds = normalize_kinds(enabled_kinds)
    astrbot_on = "astrbot" in kinds
    lines = [
        "📡 推送通道",
        f"• AstrBot 会话：{'已启用' if astrbot_on else '已停用'}"
        f"，{len(registered)} 个登记渠道",
    ]
    if not astrbot_on:
        lines.append("  （提醒不会推送到任何私聊入口）")
    for key, channel in registered.items():
        notes = []
        if not channel.get("reminders_enabled", True):
            notes.append("提醒已关闭")
        consecutive = int(failures.get(key, 0))
        if consecutive:
            notes.append(f"连续失败 {consecutive}/3，满 3 次自动关闭")
        suffix = f"（{'；'.join(notes)}）" if notes else ""
        sender = channel.get("sender_id") or "?"
        lines.append(f"  • {channel.get('umo') or key}   发送者 {sender}{suffix}")
    capsule = "已配置端点（当前版本尚未启用）" if capsule_configured else "未配置"
    lines.append(f"• 手机胶囊：{capsule}")
    unsupported = unknown_kinds(enabled_kinds)
    if unsupported:
        lines.append(f"⚠️ 配置里的通道尚未实现，已被忽略：{'、'.join(unsupported)}")
    lines.append('发送一条测试消息：/todo push test')
    return "\n".join(lines)


# 扩展点：手机胶囊通道。
#
# 要新增一种投递方式，只需实现上面的 ``PushChannel`` 协议（一个 ``name``、一个
# ``is_enabled()``、一个 ``send()``），在 ``SUPPORTED_KINDS`` 里登记种类名，然后在
# ``build_channels`` 里按配置构造。派发路径不需要改动——它只认协议。
#
# 手机胶囊（HTTP POST 到一个自建端点）会需要 ``capsule_endpoint`` / ``capsule_token``
# 两个配置键（已在 ``_conf_schema.json`` 里预留、默认为空），以及一个 ``aiohttp``
# POST 实现。本轮不实现：没有可用的端点和 Token，写出来的代码无法验证，
# 而未经测试的网络投递路径比没有更危险——它会以"配置了却不工作"的形式失败。
