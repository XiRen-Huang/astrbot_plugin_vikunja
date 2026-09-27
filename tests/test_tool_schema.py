"""llm_tool docstring 自检的单元测试。

对 ``main.py`` 只做源码解析（``ast``），**不 import 它**——本机没有安装 astrbot，
import 会因为 ``astrbot.api`` 缺失而直接失败。
"""

import ast
import pathlib
import re
import unittest

from tool_schema import (
    parser_mismatch_warnings,
    schema_warnings_for_methods,
    tool_schema_warnings,
)


class _FakeParam:
    def __init__(self, arg_name):
        self.arg_name = arg_name


class _FakeParsed:
    def __init__(self, names):
        self.params = [_FakeParam(name) for name in names]


def _fake_parse(names):
    """桩解析器：模拟 ``docstring_parser.parse`` 的返回结构。"""
    return lambda _docstring: _FakeParsed(names)

MAIN_PY = pathlib.Path(__file__).resolve().parent.parent / "main.py"


def _is_llm_tool_decorator(deco: ast.expr) -> bool:
    """判断装饰器是不是 ``filter.llm_tool``（精确匹配最后一段属性名）。

    不用子串匹配：那样 ``@filter.llm_tool_helper`` 之类的名字会被误判成工具。
    """
    target = deco.func if isinstance(deco, ast.Call) else deco
    return isinstance(target, ast.Attribute) and target.attr == "llm_tool"


def _parameter_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """取全部形参名，含仅位置参数与仅关键字参数。"""
    args = node.args
    return [
        arg.arg
        for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)
    ]


def _collect_tools(source_path: pathlib.Path) -> list[tuple[str, str | None, list[str]]]:
    """从源码里取出所有 ``@filter.llm_tool`` 函数的 (工具名, docstring, 参数名)。"""
    tools: list[tuple[str, str | None, list[str]]] = []
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(_is_llm_tool_decorator(deco) for deco in node.decorator_list):
            continue
        name = node.name
        for deco in node.decorator_list:
            if isinstance(deco, ast.Call):
                for keyword in deco.keywords:
                    if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                        name = str(keyword.value.value)
        tools.append((name, ast.get_docstring(node), _parameter_names(node)))
    return tools


class ParserMismatchTests(unittest.TestCase):
    """用真实解析器复核的判据（解析器以参数注入，测试用桩）。"""

    def test_agreement_is_silent(self):
        self.assertEqual(
            parser_mismatch_warnings(
                "t", "摘要。", ["self", "event", "title"], _fake_parse(["title"])
            ),
            [],
        )

    def test_framework_dropping_all_parameters_is_reported(self):
        """这就是毛病 1 的现场：签名有 2 个参数，框架解析出 0 个。"""
        warnings = parser_mismatch_warnings(
            "vikunja_create_task",
            "摘要。\n第二行。",
            ["event", "title", "due"],
            _fake_parse([]),
        )
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("vikunja_create_task", warnings[0])
        self.assertIn("due", warnings[0])
        self.assertIn("title", warnings[0])

    def test_event_is_never_required_from_the_parser(self):
        self.assertEqual(
            parser_mismatch_warnings("t", "摘要。", ["event", "title"], _fake_parse(["title"])),
            [],
        )

    def test_partial_drop_is_reported(self):
        warnings = parser_mismatch_warnings(
            "t", "摘要。", ["title", "due", "priority"], _fake_parse(["title", "due"])
        )
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("priority", warnings[0])

    def test_parser_raising_is_reported_not_swallowed(self):
        def boom(_docstring):
            raise ValueError("解析器炸了")

        warnings = parser_mismatch_warnings("t", "摘要。", ["title"], boom)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("解析器报错", warnings[0])


class CollectorTests(unittest.TestCase):
    """AST 采集本身的健全性——采集错了，后面的断言就是空过。"""

    def test_only_exact_llm_tool_decorator_matches(self):
        tree = ast.parse(
            "import filter\n"
            "@filter.llm_tool(name='real')\n"
            "async def a(event):\n"
            "    '''摘要。'''\n"
            "@filter.llm_tool_helper(name='fake')\n"
            "async def b(event):\n"
            "    '''摘要。'''\n"
        )
        found = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and any(_is_llm_tool_decorator(deco) for deco in node.decorator_list)
        ]
        self.assertEqual(found, ["a"])

    def test_positional_only_and_keyword_only_parameters_are_collected(self):
        tree = ast.parse(
            "@filter.llm_tool(name='t')\n"
            "async def t(event, pos, /, normal, *, kwonly):\n"
            "    '''摘要。'''\n"
        )
        node = tree.body[0]
        self.assertEqual(
            _parameter_names(node), ["event", "pos", "normal", "kwonly"]
        )


class ValidatorTests(unittest.TestCase):
    """校验器自身的规则。"""

    def test_clean_single_line_summary_passes(self):
        doc = "创建 Vikunja 任务。\n\nArgs:\n    title(string): 任务标题\n"
        self.assertEqual(tool_schema_warnings("t", doc, ["self", "event", "title"]), [])

    def test_multi_line_summary_is_rejected(self):
        doc = "创建 Vikunja 任务。\n若返回缺失信息提示，必须先向用户确认。\n\nArgs:\n    title(string): 标题\n"
        warnings = tool_schema_warnings("t", doc, ["title"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("摘要必须只有一行", warnings[0])
        self.assertIn("当前 2 行", warnings[0])

    def test_missing_argument_entry_is_reported(self):
        doc = "摘要。\n\nArgs:\n    title(string): 标题\n"
        warnings = tool_schema_warnings("t", doc, ["title", "due"])
        self.assertTrue(any("缺少条目：due" in w for w in warnings), warnings)

    def test_entry_without_type_annotation_is_reported(self):
        doc = "摘要。\n\nArgs:\n    title: 标题\n"
        warnings = tool_schema_warnings("t", doc, ["title"])
        self.assertTrue(any("缺少类型注解" in w for w in warnings), warnings)

    def test_documented_argument_absent_from_signature_is_reported(self):
        doc = "摘要。\n\nArgs:\n    title(string): 标题\n    ghost(string): 幽灵\n"
        warnings = tool_schema_warnings("t", doc, ["title"])
        self.assertTrue(any("签名里不存在" in w for w in warnings), warnings)

    def test_argumentless_tool_must_not_have_args_section(self):
        warnings = tool_schema_warnings("t", "摘要。\n\nArgs:\n", ["self", "event"])
        self.assertTrue(any("不应写 Args:" in w for w in warnings), warnings)

    def test_argumentless_tool_without_args_section_passes(self):
        self.assertEqual(tool_schema_warnings("t", "摘要。", ["self", "event"]), [])

    def test_self_and_event_are_ignored(self):
        doc = "摘要。\n\nArgs:\n    title(string): 标题\n"
        self.assertEqual(tool_schema_warnings("t", doc, ["self", "event", "title"]), [])

    def test_args_section_stops_at_later_section(self):
        """后续段落里的同名条目不能被误当成 Args 条目。"""
        doc = "摘要。\n\nArgs:\n    title(string): 标题\n\nReturns:\n    due(string): 不该被采信\n"
        warnings = tool_schema_warnings("t", doc, ["title", "due"])
        self.assertTrue(any("缺少条目：due" in w for w in warnings), warnings)

    def test_methods_helper_uses_signature_and_docstring(self):
        async def sample(self, event, title: str = ""):
            """摘要。

            Args:
                title(string): 标题
            """

        self.assertEqual(schema_warnings_for_methods({"sample": sample}), [])


class PluginToolSchemaTests(unittest.TestCase):
    """对本插件 main.py 的真实自检——修复 1 的回归防线。"""

    def test_all_plugin_tools_are_discovered(self):
        """防止 AST 遍历静默失效导致后续断言空过。"""
        tools = _collect_tools(MAIN_PY)
        self.assertEqual(
            [name for name, _, _ in tools],
            [
                "vikunja_list_projects",
                "vikunja_create_task",
                "vikunja_update_task",
                "vikunja_delete_task",
                "vikunja_manage_project",
                "vikunja_list_tasks",
                "vikunja_stats",
            ],
        )

    def test_no_tool_has_schema_warnings(self):
        for name, docstring, params in _collect_tools(MAIN_PY):
            with self.subTest(tool=name):
                self.assertEqual(tool_schema_warnings(name, docstring, params), [])

    def _tool(self, tool_name):
        for name, docstring, params in _collect_tools(MAIN_PY):
            if name == tool_name:
                return docstring, params
        self.fail(f"没有找到 {tool_name}")

    def test_create_task_summary_stays_single_line(self):
        """显式钉死这次的 P0：多行摘要曾让该工具的参数全部失效。"""
        docstring, params = self._tool("vikunja_create_task")
        summary = [line for line in (docstring or "").split("\n\n")[0].splitlines() if line.strip()]
        self.assertEqual(len(summary), 1)
        self.assertEqual(
            params,
            [
                "self",
                "event",
                "title",
                "project",
                "due",
                "priority",
                "repeat",
                "is_reminder",
                "description",
                "remind_minutes",
                "parent_id",
            ],
        )

    def test_update_task_carries_the_edit_parameters(self):
        """修改类参数都集中在这一个工具里，参数漏一个就等于该功能对自然语言不可达。"""
        docstring, params = self._tool("vikunja_update_task")
        summary = [line for line in (docstring or "").split("\n\n")[0].splitlines() if line.strip()]
        self.assertEqual(len(summary), 1)
        self.assertEqual(
            params,
            [
                "self",
                "event",
                "task_ids",
                "title",
                "description",
                "due",
                "priority",
                "repeat",
                "project",
                "done",
                "parent_id",
                "remind_minutes",
                "labels",
            ],
        )

    def test_delete_task_takes_an_id_list(self):
        """批量删除复用同一个工具，参数必须是列表形态的 task_ids。"""
        docstring, params = self._tool("vikunja_delete_task")
        summary = [line for line in (docstring or "").split("\n\n")[0].splitlines() if line.strip()]
        self.assertEqual(len(summary), 1)
        self.assertEqual(params, ["self", "event", "task_ids"])

    def test_list_tasks_carries_the_search_parameters(self):
        docstring, params = self._tool("vikunja_list_tasks")
        summary = [line for line in (docstring or "").split("\n\n")[0].splitlines() if line.strip()]
        self.assertEqual(len(summary), 1)
        self.assertEqual(
            params,
            ["self", "event", "scope", "project", "search", "include_done", "label"],
        )

    def test_manage_project_carries_the_project_parameters(self):
        docstring, params = self._tool("vikunja_manage_project")
        summary = [line for line in (docstring or "").split("\n\n")[0].splitlines() if line.strip()]
        self.assertEqual(len(summary), 1)
        self.assertEqual(params, ["self", "event", "action", "name", "parent", "new_name"])

    def test_stats_tool_takes_a_period(self):
        docstring, params = self._tool("vikunja_stats")
        summary = [line for line in (docstring or "").split("\n\n")[0].splitlines() if line.strip()]
        self.assertEqual(len(summary), 1)
        self.assertEqual(params, ["self", "event", "period"])

    def test_tool_names_in_prompt_exist(self):
        """提示词里点名的工具必须真的存在——改名后漏改提示词会让模型去调不存在的工具。"""
        source = MAIN_PY.read_text(encoding="utf-8")
        prompt = source.split("SECRETARY_PROMPT = ", 1)[1].split('"""', 2)[1]
        declared = {name for name, _, _ in _collect_tools(MAIN_PY)}
        mentioned = set(re.findall(r"\bvikunja_[a-z_]+", prompt))
        self.assertTrue(mentioned, "提示词里没有提到任何工具，断言会空过")
        self.assertEqual(mentioned - declared, set())


if __name__ == "__main__":
    unittest.main()
