"""LLM 工具 docstring 自检。纯标准库，不依赖 AstrBot。

AstrBot 用 ``docstring_parser.parse()`` 解析 ``@filter.llm_tool`` 的 docstring 生成参数
schema，解析结果为空时会**静默**注册成无参数工具——调用方传来的参数全被丢弃，工具只能
拿到空参数。这里用纯函数把几类已知隐患提前检出来，供插件启动自检和单元测试共用。
"""

from __future__ import annotations

import inspect
import re
from typing import Any, Callable, Sequence

ARGS_HEADER = "Args:"
# 同时接受 `名称(类型): 描述` 与缺类型注解的 `名称: 描述`，后者由调用方判为隐患。
ENTRY_PATTERN = re.compile(r"^(\w+)\s*(?:\(\s*(\w+)\s*\))?\s*:\s*(\S.*)$")

# AstrBot 注入的上下文参数，不参与 schema。
EXCLUDED_PARAMETERS = frozenset({"self", "event"})


def _lines(docstring: str | None) -> list[str]:
    return inspect.cleandoc(docstring or "").split("\n")


def _summary_lines(lines: list[str]) -> list[str]:
    """摘要 = 第一个空行之前的连续非空行。"""
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    summary: list[str] = []
    while index < len(lines) and lines[index].strip():
        summary.append(lines[index].strip())
        index += 1
    return summary


def _args_entries(docstring: str | None) -> list[tuple[str, str]]:
    """返回 ``Args:`` 段里的 ``(参数名, 类型)`` 列表，缺类型注解时类型为空串。

    遇到顶格的其它段落（如 ``Returns:``）即认为 ``Args:`` 段结束；缩进的续行被忽略，
    以免误取后续段落里的同名条目。
    """
    entries: list[tuple[str, str]] = []
    in_args = False
    for line in _lines(docstring):
        stripped = line.strip()
        if not in_args:
            in_args = stripped == ARGS_HEADER
            continue
        if not stripped:
            continue
        if not line.startswith((" ", "\t")):
            break
        match = ENTRY_PATTERN.match(stripped)
        if match:
            entries.append((match.group(1), match.group(2) or ""))
    return entries


def has_args_header(docstring: str | None) -> bool:
    return any(line.strip() == ARGS_HEADER for line in _lines(docstring))


def tool_schema_warnings(
    tool_name: str, docstring: str | None, parameter_names: Sequence[str]
) -> list[str]:
    """返回该 LLM 工具 docstring 的 schema 隐患；没有隐患时返回空列表。

    ``parameter_names`` 可以原样传入函数签名里的全部参数（含 ``self``/``event``），
    本函数会自行忽略它们。
    """
    lines = _lines(docstring)
    summary = _summary_lines(lines)
    entries = _args_entries(docstring)
    documented = [name for name, _ in entries]
    expected = [str(name) for name in parameter_names if str(name) not in EXCLUDED_PARAMETERS]
    warnings: list[str] = []

    if len(summary) != 1:
        warnings.append(
            f"摘要必须只有一行，当前 {len(summary)} 行——多行摘要会让 AstrBot 丢掉整个 "
            f"Args 段，工具被静默注册成无参数工具"
        )

    missing = [name for name in expected if name not in documented]
    if missing:
        warnings.append(f"Args 段缺少条目：{'、'.join(missing)}")

    extra = [name for name in documented if name not in expected]
    if extra:
        warnings.append(f"Args 段有签名里不存在的条目：{'、'.join(extra)}")

    untyped = [name for name, type_name in entries if not type_name]
    if untyped:
        warnings.append(
            f"Args 段条目缺少类型注解（应为 名称(类型): 描述）：{'、'.join(untyped)}"
        )

    if not expected and has_args_header(docstring) and not entries:
        warnings.append("无参数的工具不应写 Args: 段")

    return [f"LLM 工具 {tool_name}：{warning}。" for warning in warnings]


def schema_warnings_for_methods(methods: dict[str, object]) -> list[str]:
    """对 ``{工具名: 可调用对象}`` 逐个自检，返回汇总告警。"""
    warnings: list[str] = []
    for name, method in methods.items():
        try:
            signature = inspect.signature(method)  # type: ignore[arg-type]
            parameters = list(signature.parameters)
        except (TypeError, ValueError):  # pragma: no cover - 非函数对象
            continue
        warnings.extend(tool_schema_warnings(name, inspect.getdoc(method), parameters))
    return warnings


def parser_mismatch_warnings(
    tool_name: str,
    docstring: str | None,
    parameter_names: Sequence[str],
    parse: Callable[[str], Any],
) -> list[str]:
    """用**真实解析器**复核 docstring：解析出的参数名应与签名一致。

    上面的 :func:`tool_schema_warnings` 是对失败机理的规则近似；这里直接把 AstrBot 用的
    解析器（``docstring_parser.parse``）接进来，判据与框架完全一致——它能抓到规则没想到
    的失效方式。

    ``parse`` 由调用方注入：插件传真的 ``docstring_parser.parse``，测试传桩函数，因此这个
    函数本身不依赖 AstrBot，也不需要真的安装解析器。注意调用方应传**原始** ``__doc__``
    而非 ``inspect.getdoc()``，因为 AstrBot 用的就是原始 docstring。
    """
    try:
        parsed = parse(docstring or "")
    except Exception as exc:  # pragma: no cover - 解析器自身抛错
        return [f"LLM 工具 {tool_name}：docstring 解析器报错（{exc}），该工具的参数可能全部失效。"]
    documented = {param.arg_name for param in getattr(parsed, "params", [])}
    expected = {name for name in parameter_names if name not in EXCLUDED_PARAMETERS}
    missing = sorted(expected - documented)
    if not missing:
        return []
    return [
        f"LLM 工具 {tool_name}：框架解析出的参数为 {sorted(documented)}，签名要求 "
        f"{sorted(expected)}，缺少 {missing} 的参数会被静默丢弃。"
        f"请检查该 docstring 的摘要是否只有一行。"
    ]
