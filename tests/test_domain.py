from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from todo_domain import (
    LabelChange,
    build_label_index,
    build_project_paths,
    completed_tasks_since,
    format_weekly_report,
    filter_tasks_by_label,
    filter_tasks_by_query,
    format_label_list,
    format_project_tree,
    format_subtask_list,
    format_task_list,
    is_clear_request,
    label_cache_key,
    label_change_from_spec,
    merge_label_changes,
    next_poll_delay,
    parents_of,
    parse_add_arguments,
    parse_bulk_arguments,
    parse_datetime,
    parse_edit_arguments,
    parse_label_arguments,
    parse_label_changes,
    parse_move_arguments,
    parse_project_arguments,
    MAX_REPEAT_SECONDS,
    parse_repeat,
    parse_subtask_arguments,
    parse_task_ids,
    parse_tristate_bool,
    platform_sender_is_allowed,
    resolve_project,
    secretary_clarification_reason,
    sender_is_allowed,
    select_tasks,
    subtasks_of,
    task_label_titles,
    to_vikunja_time,
)


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.tz = ZoneInfo("Asia/Shanghai")
        self.now = datetime(2026, 7, 11, 10, 0, tzinfo=self.tz)

    def test_parse_chinese_due_time(self):
        result = parse_datetime("明天18点30分", self.tz, self.now)
        self.assertEqual(result, datetime(2026, 7, 12, 18, 30, tzinfo=self.tz))

    def test_parse_weekday_due_time(self):
        result = parse_datetime("周日 20:00", self.tz, self.now)
        self.assertEqual(result, datetime(2026, 7, 12, 20, 0, tzinfo=self.tz))

    def test_parse_add_with_repeat_and_reminder(self):
        spec = parse_add_arguments(
            '每日复盘 --due "今天 22:00" --priority 4 --repeat daily --remind 1h',
            self.tz,
            self.now,
        )
        self.assertEqual(spec.title, "每日复盘")
        self.assertEqual(spec.priority, 4)
        self.assertEqual(spec.repeat_after, 86400)
        self.assertEqual(spec.reminder_minutes, 60)

    def test_custom_reminder_requires_due_date(self):
        with self.assertRaisesRegex(ValueError, "必须同时设置"):
            parse_add_arguments("写周报 --remind 30m", self.tz, self.now)

    def test_today_includes_overdue_and_sorts_priority(self):
        tasks = [
            {"id": 1, "title": "低", "priority": 1, "due_date": "2026-07-10T12:00:00Z", "done": False},
            {"id": 2, "title": "高", "priority": 5, "due_date": "2026-07-11T12:00:00Z", "done": False},
            {"id": 3, "title": "未来", "priority": 5, "due_date": "2026-07-13T12:00:00Z", "done": False},
        ]
        selected = select_tasks(tasks, "today", self.tz, self.now)
        self.assertEqual([task["id"] for task in selected], [2, 1])
        text = format_task_list(selected, "今日", self.tz)
        self.assertIn("[P5] #2 高", text)

    def test_resolve_nested_project_path(self):
        projects = [
            {"id": 1, "title": "Inbox", "parent_project_id": 0},
            {"id": 2, "title": "fudan-work", "parent_project_id": 0},
            {"id": 3, "title": "SMX", "parent_project_id": 2},
            {"id": 4, "title": "paper", "parent_project_id": 3},
        ]
        paths = build_project_paths(projects)
        self.assertEqual(paths[4], "fudan-work/SMX/paper")
        project, path = resolve_project(projects, "fudan-work/smx/PAPER")
        self.assertEqual(project["id"], 4)
        self.assertEqual(path, "fudan-work/SMX/paper")
        tree = format_project_tree(projects)
        self.assertIn("    • paper (#4)", tree)

    def test_ambiguous_project_title_requires_path(self):
        projects = [
            {"id": 1, "title": "work", "parent_project_id": 0},
            {"id": 2, "title": "paper", "parent_project_id": 1},
            {"id": 3, "title": "personal", "parent_project_id": 0},
            {"id": 4, "title": "paper", "parent_project_id": 3},
        ]
        with self.assertRaisesRegex(ValueError, "不唯一"):
            resolve_project(projects, "paper")

    def test_secretary_guard_combines_missing_information(self):
        reason = secretary_clarification_reason(
            "推进 SMX paper", due="", repeat="", project="", is_reminder=True
        )
        self.assertIn("具体时间", reason)
        self.assertIn("目标项目", reason)
        self.assertIn("周期较长", reason)

    def test_secretary_guard_allows_clear_everyday_task(self):
        self.assertIsNone(
            secretary_clarification_reason(
                "买牛奶", due="明天18点", repeat="", project="", is_reminder=True
            )
        )

    def test_sender_whitelist(self):
        self.assertTrue(sender_is_allowed("qq-uid", []))
        self.assertTrue(sender_is_allowed("qq-uid", ["qq-uid", "wx-id"]))
        self.assertFalse(sender_is_allowed("someone-else", ["qq-uid", "wx-id"]))

    def test_qq_whitelist_does_not_block_weixin(self):
        qq_ids = ["my-qq-uid"]
        self.assertTrue(platform_sender_is_allowed("qq_official", "my-qq-uid", qq_ids))
        self.assertFalse(platform_sender_is_allowed("qq_official", "other-qq", qq_ids))
        self.assertTrue(platform_sender_is_allowed("weixin_oc", "any-weixin-id", qq_ids))

    def test_reminder_with_date_but_no_clock_time_requires_clarification(self):
        reason = secretary_clarification_reason(
            "买牛奶", due="明天", repeat="", project="", is_reminder=True
        )
        self.assertIn("具体时间", reason)


class RepeatRuleTests(unittest.TestCase):
    """``repeat_after``（秒）+ ``repeat_mode`` 是 Vikunja 的全部表达力，边界要钉死。"""

    def test_named_rules(self):
        self.assertEqual(parse_repeat("daily"), (86400, 0))
        self.assertEqual(parse_repeat("每天"), (86400, 0))

    def test_monthly_uses_mode_one_and_a_zero_interval(self):
        """mode=1 是"每月同一天"，服务端会**忽略** repeat_after，所以它必须是 0。

        这个 0 不是"没设置"：``isRepeating()`` 是 ``RepeatAfter > 0 || RepeatMode == 1``，
        mode 1 自己就能撑起重复语义。
        """
        self.assertEqual(parse_repeat("monthly"), (0, 1))
        self.assertEqual(parse_repeat("每月"), (0, 1))

    def test_suffix_intervals(self):
        self.assertEqual(parse_repeat("2d"), (172800, 0))
        self.assertEqual(parse_repeat("12h"), (43200, 0))
        self.assertEqual(parse_repeat("30m"), (1800, 0))
        self.assertEqual(parse_repeat("2w"), (1209600, 0))

    def test_chinese_interval_forms(self):
        self.assertEqual(parse_repeat("每3天"), (259200, 0))
        self.assertEqual(parse_repeat("每隔3天"), (259200, 0))
        self.assertEqual(parse_repeat("每2周"), (1209600, 0))
        self.assertEqual(parse_repeat("每2个小时"), (7200, 0))

    def test_completion_relative_repeat_selects_mode_two(self):
        """``完成后2d`` 走 ``repeat_mode=2``：从勾选完成那刻起算，而不是从截止时间。"""
        self.assertEqual(parse_repeat("完成后2d"), (172800, 2))
        self.assertEqual(parse_repeat("完成后1周"), (604800, 2))

    def test_completion_relative_repeat_needs_an_interval(self):
        with self.assertRaisesRegex(ValueError, "具体间隔"):
            parse_repeat("完成后")

    def test_clear_words_stop_repetition(self):
        for word in ("none", "", "off", "不重复", "清空", "取消"):
            self.assertEqual(parse_repeat(word), (0, 0), word)

    def test_weekday_rules_are_refused_not_approximated(self):
        """这是 ⑪ 的产品决策：宁可报错，也不挑一个"最像"的规则糊上去。

        Vikunja 没有星期几的概念，任何近似（比如 7d）都会在用户毫无察觉的情况下落到错的那天，
        而"任务默默地不在该出现的那天出现"是最难被发现的一类错误。
        """
        for rule in ("每周一", "周一/三/五", "每周一三五", "每星期三", "mon,wed,fri", "工作日"):
            with self.subTest(rule=rule):
                with self.assertRaisesRegex(ValueError, "星期几"):
                    parse_repeat(rule)

    def test_weekday_refusal_offers_an_executable_alternative(self):
        """报错必须给出下一步，不能只说"不支持"。"""
        with self.assertRaises(ValueError) as ctx:
            parse_repeat("每周一")
        message = str(ctx.exception)
        self.assertIn("7d", message)
        self.assertIn("拆成", message)

    def test_plain_weekly_is_not_mistaken_for_a_weekday_rule(self):
        """"每周"没有星期几，是合法的固定间隔——拒绝逻辑不能连它一起误伤。"""
        self.assertEqual(parse_repeat("每周"), (604800, 0))

    def test_month_interval_is_refused(self):
        """mode=1 忽略 repeat_after，所以"每 N 个月"没有对应表达，N×30 天会随月长漂移。"""
        for rule in ("每3个月", "每2月", "3months", "每1个月"):
            with self.subTest(rule=rule):
                with self.assertRaisesRegex(ValueError, "每 N 个月"):
                    parse_repeat(rule)

    def test_bare_monthly_is_not_mistaken_for_a_month_interval(self):
        self.assertEqual(parse_repeat("每月"), (0, 1))

    def test_interval_over_the_server_cap_is_rejected(self):
        """服务端 ``MaxTaskRepeatAfterSeconds`` 是十年，超了直接被拒——本地先拦下来。"""
        with self.assertRaisesRegex(ValueError, "十年"):
            parse_repeat("4000d")
        self.assertEqual(parse_repeat("3650d"), (MAX_REPEAT_SECONDS, 0))

    def test_zero_interval_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "必须大于 0"):
            parse_repeat("0d")

    def test_unknown_rule_lists_what_is_supported(self):
        with self.assertRaisesRegex(ValueError, "daily"):
            parse_repeat("隔三差五")


class ClearAndTristateTests(unittest.TestCase):
    def test_clear_words_are_recognized(self):
        for word in ("clear", "清空", "清除", "取消", "去掉", "CLEAR"):
            self.assertTrue(is_clear_request(word), word)

    def test_empty_string_is_not_a_clear_request(self):
        # 空串的语义是「本次不改这个字段」，和「清空」必须分开。
        self.assertFalse(is_clear_request(""))
        self.assertFalse(is_clear_request("   "))
        self.assertFalse(is_clear_request("会议纪要"))

    def test_tristate_distinguishes_unset_from_false(self):
        self.assertIsNone(parse_tristate_bool(""))
        self.assertIsNone(parse_tristate_bool(None))
        self.assertIsNone(parse_tristate_bool("   "))
        self.assertIs(parse_tristate_bool("完成"), True)
        self.assertIs(parse_tristate_bool("撤销完成"), False)
        self.assertIs(parse_tristate_bool(True), True)
        self.assertIs(parse_tristate_bool(False), False)

    def test_tristate_rejects_nonsense(self):
        with self.assertRaises(ValueError):
            parse_tristate_bool("也许吧")


class ParseEditArgumentsTests(unittest.TestCase):
    def setUp(self):
        self.tz = ZoneInfo("Asia/Shanghai")
        self.now = datetime(2026, 7, 11, 10, 0, tzinfo=self.tz)

    def _parse(self, text):
        return parse_edit_arguments(text, self.tz, self.now)

    def test_parses_task_id_without_hash(self):
        self.assertEqual(self._parse("12 --title 新标题").task_id, 12)

    def test_parses_task_id_with_hash(self):
        self.assertEqual(self._parse("#12 --title 新标题").task_id, 12)

    def test_non_numeric_task_id_is_rejected(self):
        with self.assertRaises(ValueError):
            self._parse("abc --title x")

    def test_missing_options_is_rejected(self):
        with self.assertRaises(ValueError):
            self._parse("12")

    def test_empty_input_is_rejected(self):
        with self.assertRaises(ValueError):
            self._parse("")

    def test_only_the_named_fields_appear_in_the_payload(self):
        """没提的字段不能出现在 payload 里——出现就意味着要把它写成零值。"""
        spec = self._parse("12 --title 新标题")
        self.assertEqual(spec.payload, {"title": "新标题"})

    def test_due_is_converted_to_vikunja_utc(self):
        spec = self._parse('12 --due "明天18点"')
        self.assertEqual(spec.payload["due_date"], to_vikunja_time(datetime(2026, 7, 12, 18, 0, tzinfo=self.tz)))

    def test_empty_due_clears_the_field(self):
        spec = self._parse('12 --due ""')
        self.assertIn("due_date", spec.payload)
        self.assertIsNone(spec.payload["due_date"])

    def test_clear_word_also_clears_due(self):
        self.assertIsNone(self._parse("12 --due 清空").payload["due_date"])

    def test_empty_description_clears_the_field(self):
        spec = self._parse('12 --desc ""')
        self.assertIsNone(spec.payload["description"])

    def test_repeat_none_stops_repeating(self):
        spec = self._parse("12 --repeat none")
        self.assertEqual(spec.payload["repeat_after"], 0)
        self.assertEqual(spec.payload["repeat_mode"], 0)

    def test_repeat_clear_word_also_stops_repeating(self):
        """模型可能说"取消重复"，词表要和 CLEAR_WORDS 一致，不能只认 none。"""
        spec = self._parse("12 --repeat 取消")
        self.assertEqual(spec.payload["repeat_after"], 0)
        self.assertEqual(spec.payload["repeat_mode"], 0)

    def test_repeat_rule_is_parsed(self):
        spec = self._parse("12 --repeat 2d")
        self.assertEqual(spec.payload["repeat_after"], 172800)
        self.assertEqual(spec.payload["repeat_mode"], 0)

    def test_priority_bounds(self):
        self.assertEqual(self._parse("12 --priority 5").payload["priority"], 5)
        with self.assertRaises(ValueError):
            self._parse("12 --priority 6")
        with self.assertRaises(ValueError):
            self._parse("12 --priority abc")

    def test_empty_title_is_rejected(self):
        with self.assertRaises(ValueError):
            self._parse('12 --title ""')

    def test_project_selector_stays_out_of_the_vikunja_payload(self):
        """项目要解析成 project_id 才能发；选择器本身不是 Vikunja 字段。"""
        spec = self._parse("12 --project fudan-work/SMX")
        self.assertEqual(spec.project_selector, "fudan-work/SMX")
        self.assertEqual(spec.payload, {})

    def test_remind_clear_flag(self):
        spec = self._parse("12 --remind clear")
        self.assertTrue(spec.reminder_cleared)
        self.assertIsNone(spec.reminder_minutes)

    def test_remind_minutes(self):
        spec = self._parse("12 --remind 30m")
        self.assertEqual(spec.reminder_minutes, 30)
        self.assertFalse(spec.reminder_cleared)

    def test_unknown_option_is_rejected(self):
        with self.assertRaises(ValueError):
            self._parse("12 --nope 1")

    def test_stray_positional_argument_is_rejected(self):
        with self.assertRaises(ValueError):
            self._parse("12 新标题")

    def test_unclosed_quote_is_rejected(self):
        with self.assertRaises(ValueError):
            self._parse('12 --title "新标题')

    def test_only_a_reminder_change_is_not_empty(self):
        """只改提醒阈值时 payload 为空，但这次编辑依然有内容，不能被判成空操作。"""
        self.assertFalse(self._parse("12 --remind 30m").is_empty())


class SearchTests(unittest.TestCase):
    TASKS = [
        {"id": 1, "title": "修改论文引言", "description": "设计稿在 FluidCapsule 里"},
        {"id": 2, "title": "买牛奶", "description": ""},
        {"id": 3, "title": "Review Draft", "description": "见 FLUIDCAPSULE 链接"},
    ]

    def test_matches_title(self):
        self.assertEqual([t["id"] for t in filter_tasks_by_query(self.TASKS, "论文")], [1])

    def test_matches_description(self):
        """服务端的 ``s`` 参数在 v2.3.0 只搜标题，描述里的关键词必须也能搜到。"""
        self.assertEqual(
            [t["id"] for t in filter_tasks_by_query(self.TASKS, "FluidCapsule")], [1, 3]
        )

    def test_is_case_insensitive(self):
        self.assertEqual(
            [t["id"] for t in filter_tasks_by_query(self.TASKS, "review draft")], [3]
        )

    def test_empty_query_returns_everything(self):
        self.assertEqual(len(filter_tasks_by_query(self.TASKS, "   ")), 3)

    def test_no_match_returns_empty(self):
        self.assertEqual(filter_tasks_by_query(self.TASKS, "不存在的词"), [])


class ParseTaskIdsTests(unittest.TestCase):
    def test_comma_separated(self):
        self.assertEqual(parse_task_ids("12,15,20"), [12, 15, 20])

    def test_accepts_hashes_and_chinese_punctuation(self):
        self.assertEqual(parse_task_ids("#12、15，20"), [12, 15, 20])

    def test_deduplicates_while_preserving_order(self):
        self.assertEqual(parse_task_ids("15,12,15"), [15, 12])

    def test_rejects_non_numeric(self):
        with self.assertRaises(ValueError):
            parse_task_ids("12,abc")

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            parse_task_ids("  ")


class SubtaskRelationTests(unittest.TestCase):
    def test_children_are_read_from_the_subtask_key(self):
        task = {
            "id": 7,
            "related_tasks": {
                "subtask": [{"id": 8, "title": "写引言"}],
                "parenttask": [{"id": 3, "title": "论文"}],
            },
        }
        self.assertEqual([child["id"] for child in subtasks_of(task)], [8])
        self.assertEqual([parent["id"] for parent in parents_of(task)], [3])

    def test_missing_related_tasks_is_not_an_error(self):
        self.assertEqual(subtasks_of({"id": 7}), [])
        self.assertEqual(parents_of({"id": 7}), [])

    def test_unexpected_shapes_are_ignored(self):
        self.assertEqual(subtasks_of({"related_tasks": None}), [])
        self.assertEqual(subtasks_of({"related_tasks": {"subtask": "oops"}}), [])
        self.assertEqual(subtasks_of({"related_tasks": {"subtask": ["oops", {"id": 1}]}}), [{"id": 1}])

    def test_format_lists_done_and_pending(self):
        tz = ZoneInfo("Asia/Shanghai")
        text = format_subtask_list(
            {"id": 7, "title": "论文"},
            [{"id": 8, "title": "写引言", "done": False}, {"id": 9, "title": "跑实验", "done": True}],
            tz,
        )
        self.assertIn("论文", text)
        self.assertIn("▫️ #8 写引言", text)
        self.assertIn("✅ #9 跑实验", text)

    def test_format_suggests_how_to_add_when_empty(self):
        text = format_subtask_list({"id": 7, "title": "论文"}, [], ZoneInfo("Asia/Shanghai"))
        self.assertIn("/todo subtask add 7", text)


class ParseSubtaskArgumentsTests(unittest.TestCase):
    def test_add(self):
        spec = parse_subtask_arguments("add 7 写引言")
        self.assertEqual((spec.action, spec.parent_id, spec.title), ("add", 7, "写引言"))

    def test_add_takes_a_multi_word_title(self):
        self.assertEqual(parse_subtask_arguments("add 7 写 论文 引言").title, "写 论文 引言")

    def test_chinese_action_aliases(self):
        self.assertEqual(parse_subtask_arguments("添加 7 写引言").action, "add")
        self.assertEqual(parse_subtask_arguments("列表 7").action, "list")
        self.assertEqual(parse_subtask_arguments("删除 7 8").action, "rm")

    def test_list(self):
        spec = parse_subtask_arguments("list #7")
        self.assertEqual((spec.action, spec.parent_id), ("list", 7))

    def test_rm_carries_both_ids(self):
        spec = parse_subtask_arguments("rm 7 8")
        self.assertEqual((spec.action, spec.parent_id, spec.child_id), ("rm", 7, 8))

    def test_add_without_title_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_subtask_arguments("add 7")

    def test_unknown_action_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_subtask_arguments("frobnicate 7")

    def test_empty_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_subtask_arguments("")


class ParseBulkArgumentsTests(unittest.TestCase):
    def test_done(self):
        spec = parse_bulk_arguments("done 1,2,3")
        self.assertEqual((spec.action, spec.task_ids), ("done", [1, 2, 3]))

    def test_reopen_alias(self):
        self.assertEqual(parse_bulk_arguments("撤销完成 5").action, "reopen")

    def test_priority(self):
        spec = parse_bulk_arguments("pri 5 1,2")
        self.assertEqual((spec.action, spec.priority, spec.task_ids), ("priority", 5, [1, 2]))

    def test_delete(self):
        self.assertEqual(parse_bulk_arguments("delete 9").action, "delete")

    def test_priority_out_of_range_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_bulk_arguments("pri 6 1")

    def test_priority_without_ids_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_bulk_arguments("pri 5")

    def test_missing_ids_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_bulk_arguments("done")

    def test_unknown_action_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_bulk_arguments("explode 1")

    def test_empty_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_bulk_arguments("")


class ParseProjectArgumentsTests(unittest.TestCase):
    def test_new_without_parent(self):
        spec = parse_project_arguments("new reading-list")
        self.assertEqual((spec.action, spec.name, spec.parent_selector), ("create", "reading-list", ""))

    def test_new_with_parent(self):
        spec = parse_project_arguments("new paper --parent fudan-work/SMX")
        self.assertEqual((spec.name, spec.parent_selector), ("paper", "fudan-work/SMX"))

    def test_new_accepts_an_unquoted_multi_word_name(self):
        self.assertEqual(parse_project_arguments("new 阅读 清单").name, "阅读 清单")

    def test_chinese_create_alias(self):
        self.assertEqual(parse_project_arguments("新建 阅读清单").action, "create")

    def test_rename(self):
        spec = parse_project_arguments("rename reading-list 阅读清单")
        self.assertEqual(
            (spec.action, spec.name, spec.new_name), ("rename", "reading-list", "阅读清单")
        )

    def test_rename_needs_two_arguments(self):
        with self.assertRaises(ValueError):
            parse_project_arguments("rename reading-list")

    def test_missing_name_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_project_arguments("new")

    def test_unknown_option_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_project_arguments("new x --nope 1")

    def test_unknown_action_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_project_arguments("destroy x")


class ParseMoveArgumentsTests(unittest.TestCase):
    def test_parses_id_and_selector(self):
        self.assertEqual(
            parse_move_arguments("12 fudan-work/SMX/paper"),
            (12, "fudan-work/SMX/paper"),
        )

    def test_accepts_a_hash(self):
        self.assertEqual(parse_move_arguments("#12 inbox"), (12, "inbox"))

    def test_project_name_may_contain_spaces(self):
        self.assertEqual(parse_move_arguments("12 阅读 清单"), (12, "阅读 清单"))

    def test_non_numeric_id_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_move_arguments("abc inbox")

    def test_missing_selector_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_move_arguments("12")


class NextPollDelayTests(unittest.TestCase):
    """提醒轮询的自适应节奏。"""

    def setUp(self):
        self.tz = ZoneInfo("Asia/Shanghai")
        self.now = datetime(2026, 7, 11, 10, 0, tzinfo=self.tz)
        self.poll = 60
        self.idle = 600

    def _task(self, task_id, minutes_from_now=None, done=False):
        due = None if minutes_from_now is None else to_vikunja_time(
            self.now + timedelta(minutes=minutes_from_now)
        )
        return {"id": task_id, "done": done, "due_date": due, "priority": 0}

    def _delay(
        self, tasks, default_minutes=30, overrides=None, notified=None, poll=60, idle=600
    ):
        return next_poll_delay(
            tasks,
            default_minutes,
            overrides or {},
            notified or set(),
            self.now,
            poll,
            idle,
        )

    def test_no_remindable_tasks_sleeps_full_idle(self):
        self.assertEqual(self._delay([]), 600)
        self.assertEqual(self._delay([self._task(1)]), 600)  # 无截止时间
        self.assertEqual(self._delay([self._task(2, 10, done=True)]), 600)  # 已完成

    def test_overdue_task_keeps_regular_cadence(self):
        self.assertEqual(self._delay([self._task(1, -120)]), 60)

    def test_reminder_moment_already_reached_keeps_regular_cadence(self):
        # 20 分钟后到期、提前 30 分钟提醒 → 提醒时刻已过
        self.assertEqual(self._delay([self._task(1, 20)]), 60)

    def test_sleeps_until_reminder_moment(self):
        # 35 分钟后到期、提前 30 分钟 → 还有 5 分钟到提醒时刻
        self.assertEqual(self._delay([self._task(1, 35)]), 300)

    def test_sleep_is_capped_by_idle_interval(self):
        # 4 小时后到期 → 远超空闲上限
        self.assertEqual(self._delay([self._task(1, 240)]), 600)

    def test_per_task_override_changes_outcome(self):
        # 90 分钟后到期：默认阈值 30 分钟→还早；覆盖成 120 分钟→提醒时刻已过
        self.assertEqual(self._delay([self._task(5, 90)]), 600)
        self.assertEqual(self._delay([self._task(5, 90)], overrides={5: 120}), 60)

    def test_earliest_reminder_wins(self):
        tasks = [self._task(1, 300), self._task(2, 35)]
        self.assertEqual(self._delay(tasks), 300)

    def test_idle_below_poll_never_sleeps_too_little(self):
        self.assertEqual(self._delay([self._task(1, 240)], poll=60, idle=30), 60)

    def test_notified_overdue_task_stops_pinning_cadence(self):
        """已提醒过的逾期任务不再把轮询拽回 60 秒——去重键含 due_date，它不会再发。"""
        tasks = [self._task(1, -120)]
        self.assertEqual(self._delay(tasks, notified={1}), 600)

    def test_unnotified_overdue_task_still_keeps_regular_cadence(self):
        """同一条逾期任务，只要还有渠道没收到，就必须维持常规节奏。"""
        tasks = [self._task(1, -120), self._task(2, -5)]
        self.assertEqual(self._delay(tasks, notified={1}), 60)

    def test_notified_overdue_task_does_not_mask_a_pending_one(self):
        tasks = [self._task(1, -120), self._task(2, 35)]
        self.assertEqual(self._delay(tasks, notified={1}), 300)


class ParseLabelChangesTests(unittest.TestCase):
    """``+a,-b`` 是增量，``a,b`` 是整体替换——两者对应完全不同的接口，混淆会静默摘标签。"""

    def test_bare_names_are_a_replacement_set(self):
        change = parse_label_changes("重要,紧急")
        self.assertEqual(change.replace, ["重要", "紧急"])
        self.assertEqual(change.add, [])
        self.assertEqual(change.remove, [])

    def test_plus_and_minus_are_incremental(self):
        change = parse_label_changes("+重要,-待定")
        self.assertEqual(change.add, ["重要"])
        self.assertEqual(change.remove, ["待定"])
        self.assertIsNone(change.replace)

    def test_one_prefix_makes_the_whole_string_incremental(self):
        """``+a,b`` 只能有一种读法，否则这个写法没法表达。"""
        change = parse_label_changes("+重要,紧急")
        self.assertEqual(change.add, ["重要", "紧急"])
        self.assertIsNone(change.replace)

    def test_unprefixed_pieces_alongside_prefixes_count_as_adds(self):
        change = parse_label_changes("-待定,重要")
        self.assertEqual(change.add, ["重要"])
        self.assertEqual(change.remove, ["待定"])

    def test_blank_value_clears_every_label(self):
        change = parse_label_changes("   ")
        self.assertEqual(change.replace, [])
        self.assertFalse(change.is_empty())

    def test_chinese_commas_and_enumeration_marks_split_too(self):
        self.assertEqual(parse_label_changes("重要，紧急、待定").replace, ["重要", "紧急", "待定"])

    def test_none_replacement_is_not_the_same_as_empty_replacement(self):
        """``is_empty()`` 只能用 ``is None`` 判——写成 ``not replace`` 会让"清空"退化成"不改"。"""
        self.assertTrue(parse_label_changes("+a").is_empty() is False)
        self.assertFalse(LabelChange(add=[], remove=[], replace=[]).is_empty())
        self.assertTrue(LabelChange(add=[], remove=[]).is_empty())


class MergeLabelChangesTests(unittest.TestCase):
    def test_adds_and_removes_accumulate(self):
        merged = merge_label_changes(parse_label_changes("+a"), parse_label_changes("+b,-c"))
        self.assertEqual(merged.add, ["a", "b"])
        self.assertEqual(merged.remove, ["c"])

    def test_duplicates_are_collapsed(self):
        merged = merge_label_changes(parse_label_changes("+a"), parse_label_changes("+a"))
        self.assertEqual(merged.add, ["a"])

    def test_last_replacement_wins_but_increments_survive(self):
        merged = merge_label_changes(parse_label_changes("a,b"), parse_label_changes("+c"))
        self.assertEqual(merged.replace, ["a", "b"])
        self.assertEqual(merged.add, ["c"])

    def test_none_current_passes_the_change_through(self):
        change = parse_label_changes("+a")
        self.assertIs(merge_label_changes(None, change), change)


class ParseLabelArgumentsTests(unittest.TestCase):
    def test_add_takes_ids_and_names(self):
        spec = parse_label_arguments("add 12 重要")
        self.assertEqual((spec.action, spec.task_ids, spec.names), ("add", [12], ["重要"]))

    def test_ids_can_be_plural_and_mix_separators(self):
        spec = parse_label_arguments("add 12,15、#20 重要 紧急")
        self.assertEqual(spec.task_ids, [12, 15, 20])
        self.assertEqual(spec.names, ["重要", "紧急"])

    def test_set_with_clear_word_empties_the_set(self):
        spec = parse_label_arguments("set 12 清空")
        self.assertEqual(spec.action, "set")
        self.assertEqual(spec.names, [])

    def test_add_rejects_the_clear_word(self):
        """add/rm 用"清空"当标签名只会新建一个叫"清空"的标签。"""
        with self.assertRaises(ValueError):
            parse_label_arguments("add 12 清空")

    def test_remove_is_not_aliased_to_add(self):
        spec = parse_label_arguments("rm 12 待定")
        self.assertEqual(spec.action, "remove")

    def test_list_needs_no_arguments(self):
        self.assertEqual(parse_label_arguments("list").action, "list")

    def test_unknown_action_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_label_arguments("destroy 12 重要")

    def test_missing_names_are_rejected(self):
        with self.assertRaises(ValueError):
            parse_label_arguments("add 12")

    def test_unclosed_quote_is_reported(self):
        with self.assertRaises(ValueError):
            parse_label_arguments('add 12 "重要')


class LabelChangeFromSpecTests(unittest.TestCase):
    """这一处最容易写反：``set`` 若按 add 实现会变成"只加不删"，看起来对但语义全错。"""

    def test_set_becomes_a_replacement(self):
        change = label_change_from_spec(parse_label_arguments("set 12 重要 紧急"))
        self.assertEqual(change.replace, ["重要", "紧急"])
        self.assertEqual(change.add, [])

    def test_add_is_incremental(self):
        change = label_change_from_spec(parse_label_arguments("add 12 重要"))
        self.assertEqual(change.add, ["重要"])
        self.assertIsNone(change.replace)

    def test_remove_is_incremental(self):
        change = label_change_from_spec(parse_label_arguments("rm 12 待定"))
        self.assertEqual(change.remove, ["待定"])
        self.assertIsNone(change.replace)

    def test_set_with_clear_becomes_an_empty_replacement(self):
        change = label_change_from_spec(parse_label_arguments("set 12 清空"))
        self.assertEqual(change.replace, [])
        self.assertFalse(change.is_empty())


class LabelIndexTests(unittest.TestCase):
    def test_key_ignores_case_and_surrounding_space(self):
        self.assertEqual(label_cache_key("  重要 "), label_cache_key("重要"))
        self.assertEqual(label_cache_key("Paper"), label_cache_key("paper"))

    def test_duplicate_titles_keep_the_lowest_id(self):
        """Vikunja 的 labels 表在 title 上没有唯一约束，行为至少要确定。"""
        index = build_label_index(
            [{"id": 9, "title": "重要"}, {"id": 2, "title": "重要"}]
        )
        self.assertEqual(index[label_cache_key("重要")]["id"], 2)

    def test_missing_title_is_still_a_key(self):
        self.assertEqual(build_label_index([{"id": 1}])[label_cache_key("")]["id"], 1)


class TaskLabelFilterTests(unittest.TestCase):
    TASKS = [
        {"id": 1, "title": "A", "labels": [{"id": 2, "title": "重要"}]},
        {"id": 2, "title": "B", "labels": [{"id": 3, "title": "paper"}]},
        {"id": 3, "title": "C"},
        {"id": 4, "title": "D", "labels": []},
    ]

    def test_matches_case_insensitively(self):
        self.assertEqual([t["id"] for t in filter_tasks_by_label(self.TASKS, "PAPER")], [2])

    def test_substring_matches(self):
        self.assertEqual([t["id"] for t in filter_tasks_by_label(self.TASKS, "重")], [1])

    def test_blank_query_keeps_everything(self):
        self.assertEqual(len(filter_tasks_by_label(self.TASKS, "  ")), 4)

    def test_tasks_without_labels_are_skipped_not_crashed(self):
        self.assertEqual(filter_tasks_by_label(self.TASKS, "不存在"), [])

    def test_titles_are_readable_when_labels_are_missing_or_null(self):
        self.assertEqual(task_label_titles({"id": 3}), [])
        self.assertEqual(task_label_titles({"id": 3, "labels": None}), [])
        self.assertEqual(task_label_titles({"id": 3, "labels": [{"id": 1}]}), [])

    def test_format_list_handles_the_empty_case(self):
        self.assertIn("还没有任何标签", format_label_list([]))

    def test_format_list_shows_ids_and_titles(self):
        text = format_label_list([{"id": 7, "title": "重要"}])
        self.assertIn("#7", text)
        self.assertIn("重要", text)


class EditLabelFlagTests(unittest.TestCase):
    TZ = ZoneInfo("Asia/Shanghai")

    def test_label_flag_lands_outside_the_payload(self):
        """标签不是任务字段，混进 payload 会被当成未知字段发给服务端。"""
        spec = parse_edit_arguments("12 --label +重要", self.TZ)
        self.assertEqual(spec.payload, {})
        self.assertEqual(spec.label_change.add, ["重要"])
        self.assertFalse(spec.is_empty())

    def test_repeated_label_flags_merge(self):
        spec = parse_edit_arguments("12 --label a,b --label +c", self.TZ)
        self.assertEqual(spec.label_change.replace, ["a", "b"])
        self.assertEqual(spec.label_change.add, ["c"])

    def test_clearing_labels_counts_as_a_change(self):
        spec = parse_edit_arguments('12 --label ""', self.TZ)
        self.assertEqual(spec.label_change.replace, [])
        self.assertFalse(spec.is_empty())

    def test_label_flag_needs_a_value(self):
        with self.assertRaises(ValueError):
            parse_edit_arguments("12 --label", self.TZ)


class CompletedSinceTests(unittest.TestCase):
    """``done_at`` 的零值防线——⑫ 周报的正确性全靠它。"""

    NOW = datetime(2026, 7, 12, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    def _task(self, task_id, done_at, project_id=3, title="事"):
        return {"id": task_id, "title": title, "done_at": done_at, "project_id": project_id}

    def test_keeps_only_rows_inside_the_window(self):
        tasks = [
            self._task(1, "2026-07-11T02:00:00Z"),
            self._task(2, "2026-06-01T02:00:00Z"),
        ]
        kept = completed_tasks_since(tasks, self.NOW - timedelta(days=7))
        self.assertEqual([task["id"] for task in kept], [1])

    def test_zero_value_done_at_is_dropped(self):
        """Go 的 ``time.Time`` 非指针，未设置时序列化成 ``0001-01-01``。

        它比任何真实日期都"小"，排序时会沉底，却在"取最近一条"这类逻辑里最先被选中；
        而且它根本不是"很久以前完成"，是**不知道什么时候完成的**——不能当数据点用。
        """
        tasks = [
            self._task(1, "0001-01-01T00:00:00Z"),
            self._task(2, None),
            self._task(3, "2026-07-11T02:00:00Z"),
        ]
        kept = completed_tasks_since(tasks, self.NOW - timedelta(days=7))
        self.assertEqual([task["id"] for task in kept], [3])

    def test_sorted_by_completion_time_descending(self):
        tasks = [
            self._task(1, "2026-07-08T02:00:00Z"),
            self._task(2, "2026-07-11T02:00:00Z"),
            self._task(3, "2026-07-10T02:00:00Z"),
        ]
        kept = completed_tasks_since(tasks, self.NOW - timedelta(days=7))
        self.assertEqual([task["id"] for task in kept], [2, 3, 1])

    def test_unparsable_done_at_is_dropped_not_crashed_on(self):
        kept = completed_tasks_since([self._task(1, "不是时间")], self.NOW - timedelta(days=7))
        self.assertEqual(kept, [])


class WeeklyReportTests(unittest.TestCase):
    NOW = datetime(2026, 7, 12, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    PATHS = {3: "fudan-work/SMX/paper", 4: "personal-project"}

    def _report(self, tasks, **kwargs):
        return format_weekly_report(tasks, self.PATHS, self.NOW.tzinfo, self.NOW, **kwargs)

    def _completed(self, task_id, done_at, project_id=3, title="事"):
        return {
            "id": task_id,
            "title": title,
            "project_id": project_id,
            "done_at": datetime.fromisoformat(done_at.replace("Z", "+00:00")),
        }

    def test_empty_window_says_so_without_blaming_the_user(self):
        text = self._report([])
        self.assertIn("没有已完成的任务", text)
        self.assertIn("7 天", text)

    def test_counts_and_window_are_in_the_header(self):
        tasks = [self._completed(1, "2026-07-11T02:00:00Z")]
        text = self._report(tasks)
        self.assertIn("完成 1 项", text)
        self.assertIn("07-05 ~ 07-12", text)

    def test_groups_by_project_with_real_paths(self):
        tasks = [
            self._completed(1, "2026-07-11T02:00:00Z", project_id=3),
            self._completed(2, "2026-07-10T02:00:00Z", project_id=4),
            self._completed(3, "2026-07-09T02:00:00Z", project_id=4),
        ]
        text = self._report(tasks)
        tally = [line for line in text.splitlines() if line.startswith("按项目：")][0]
        self.assertIn("fudan-work/SMX/paper 1 项", tally)
        self.assertIn("personal-project 2 项", tally)

    def test_unknown_project_does_not_keyerror(self):
        text = self._report([self._completed(1, "2026-07-11T02:00:00Z", project_id=99)])
        self.assertIn("未知项目", text)

    def test_completion_time_is_shown_in_local_time(self):
        """``2026-07-11T02:00:00Z`` 在上海是 10 点——按 UTC 显示会差 8 小时。"""
        self.assertIn("07-11 10:00", self._report([self._completed(1, "2026-07-11T02:00:00Z")]))

    def test_busiest_day_only_when_there_is_more_than_one_day(self):
        one_day = [self._completed(index, "2026-07-11T02:00:00Z") for index in (1, 2)]
        self.assertNotIn("完成最多的一天", self._report(one_day))
        two_days = one_day + [self._completed(3, "2026-07-10T02:00:00Z")]
        self.assertIn("完成最多的一天：07-11（2 项）", self._report(two_days))

    def test_overflow_is_announced(self):
        tasks = [self._completed(index, "2026-07-11T02:00:00Z") for index in range(5)]
        text = self._report(tasks, limit=2)
        self.assertIn("另有 3 项未显示", text)

    def test_single_day_window_labels_itself_correctly(self):
        text = self._report([self._completed(1, "2026-07-12T02:00:00Z")], days=1)
        self.assertIn("最近 1 天", text)


if __name__ == "__main__":
    unittest.main()
