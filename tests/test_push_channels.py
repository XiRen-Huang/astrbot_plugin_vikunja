import unittest

from push_channels import (
    SUPPORTED_KINDS,
    AstrBotChannel,
    build_channels,
    format_push_status,
    normalize_kinds,
    unknown_kinds,
)


QQ_UMO = "qq_official:FriendMessage:10001"
WECHAT_UMO = "weixin_oc:FriendMessage:10002"

REGISTERED = {
    QQ_UMO: {"umo": QQ_UMO, "sender_id": "10001", "reminders_enabled": True},
    WECHAT_UMO: {"umo": WECHAT_UMO, "sender_id": "10002", "reminders_enabled": False},
}


class _Recorder:
    """假发送函数：按 umo 决定返回什么，并记下每一条发出去的内容。"""

    def __init__(self, results=None):
        self.results = results or {}
        self.sent = []

    async def __call__(self, umo, text):
        self.sent.append((umo, text))
        return self.results.get(umo, True)


class NormalizeKindsTests(unittest.TestCase):
    def test_missing_config_falls_back_to_the_default_set(self):
        """只认 None（配置项缺失）为缺省。"""
        self.assertEqual(normalize_kinds(None), set(SUPPORTED_KINDS))

    def test_empty_list_means_nothing_enabled(self):
        """空列表是人取消掉所有选项的结果，不能当缺省——否则这个键就永远关不掉了。

        AstrBot 的 ``check_config_integrity()`` 只在键缺失或为 None 时回填 schema 默认值，
        空列表会被原样保留，所以这个区分是可靠的。
        """
        self.assertEqual(normalize_kinds([]), set())
        self.assertEqual(normalize_kinds(""), set())

    def test_list_and_comma_string_agree(self):
        self.assertEqual(normalize_kinds(["astrbot"]), {"astrbot"})
        self.assertEqual(normalize_kinds("astrbot"), {"astrbot"})
        self.assertEqual(normalize_kinds(" astrbot , capsule "), {"astrbot", "capsule"})

    def test_unknown_kinds_are_named(self):
        self.assertEqual(unknown_kinds(["astrbot", "capsule"]), ["capsule"])
        self.assertEqual(unknown_kinds(["astrbot"]), [])


class AstrBotChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_none_result_counts_as_success(self):
        """AstrBot 某些适配器成功时返回 None，判据只能是 ``is not False``。"""
        channel = AstrBotChannel(QQ_UMO, _Recorder({QQ_UMO: None}))
        self.assertTrue(await channel.send("hi"))

    async def test_false_result_counts_as_failure(self):
        channel = AstrBotChannel(QQ_UMO, _Recorder({QQ_UMO: False}))
        self.assertFalse(await channel.send("hi"))

    async def test_true_result_counts_as_success(self):
        channel = AstrBotChannel(QQ_UMO, _Recorder({QQ_UMO: True}))
        self.assertTrue(await channel.send("hi"))

    async def test_name_is_the_umo(self):
        """去重键是 ``{name}|{task_id}|{due_date}``，所以 name 必须等于 umo。"""
        channel = AstrBotChannel(QQ_UMO, _Recorder())
        self.assertEqual(channel.name, QQ_UMO)

    async def test_send_goes_to_its_own_umo(self):
        recorder = _Recorder()
        await AstrBotChannel(WECHAT_UMO, recorder).send("hi")
        self.assertEqual(recorder.sent, [(WECHAT_UMO, "hi")])


class BuildChannelsTests(unittest.TestCase):
    def test_one_channel_per_registered_entry(self):
        channels = build_channels(REGISTERED, _Recorder())
        self.assertEqual([c.name for c in channels], [QQ_UMO, WECHAT_UMO])

    def test_per_channel_reminder_switch_is_carried_over(self):
        channels = build_channels(REGISTERED, _Recorder())
        self.assertTrue(channels[0].is_enabled())
        self.assertFalse(channels[1].is_enabled())

    def test_disabled_channel_is_still_returned(self):
        """派发路径自己按 is_enabled() 取舍；状态命令要能列出被关掉的那些。"""
        channels = build_channels(REGISTERED, _Recorder())
        self.assertEqual(len(channels), 2)

    def test_disabling_the_kind_yields_no_channels(self):
        self.assertEqual(build_channels(REGISTERED, _Recorder(), enabled_kinds=[]), [])
        self.assertEqual(
            build_channels(REGISTERED, _Recorder(), enabled_kinds=["capsule"]), []
        )

    def test_falls_back_to_the_dict_key_when_umo_is_missing(self):
        channels = build_channels({QQ_UMO: {"sender_id": "1"}}, _Recorder())
        self.assertEqual(channels[0].name, QQ_UMO)


class FormatPushStatusTests(unittest.TestCase):
    def _status(self, **kwargs):
        params = {
            "registered": REGISTERED,
            "failures": {},
            "enabled_kinds": ["astrbot"],
            "capsule_configured": False,
        }
        params.update(kwargs)
        return format_push_status(**params)

    def test_reports_the_registered_count_and_each_entry(self):
        text = self._status()
        self.assertIn("2 个登记渠道", text)
        self.assertIn(QQ_UMO, text)
        self.assertIn(WECHAT_UMO, text)

    def test_marks_a_channel_whose_reminders_are_off(self):
        text = self._status()
        self.assertIn("提醒已关闭", text)

    def test_reports_consecutive_failures(self):
        text = self._status(failures={QQ_UMO: 2})
        self.assertIn("连续失败 2/3", text)

    def test_disabled_kind_says_reminders_will_not_be_delivered(self):
        text = self._status(enabled_kinds=["capsule"])
        self.assertIn("已停用", text)
        self.assertIn("提醒不会推送到任何私聊入口", text)

    def test_unimplemented_kinds_are_named_not_silently_ignored(self):
        """配了但没实现的通道必须说出来——静默忽略会让用户以为胶囊在收提醒。"""
        text = self._status(enabled_kinds=["astrbot", "capsule"])
        self.assertIn("尚未实现", text)
        self.assertIn("capsule", text)

    def test_capsule_line_reflects_the_reserved_config_key(self):
        self.assertIn("未配置", self._status())
        self.assertIn("尚未启用", self._status(capsule_configured=True))


if __name__ == "__main__":
    unittest.main()
