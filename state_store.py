"""Persistent plugin state backed by AstrBot's per-plugin KV storage."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any, Awaitable, Callable

LoadFn = Callable[[], Awaitable[dict[str, Any]]]
SaveFn = Callable[[dict[str, Any]], Awaitable[None]]

# 审计只留最近这么多次删除。项目删除是不可逆的，但审计记录本身没必要无限增长。
AUDIT_LIMIT = 50


class StateStore:
    def __init__(self, load: LoadFn, save: SaveFn):
        self._load = load
        self._save = save
        self._lock = asyncio.Lock()
        self._state: dict[str, Any] = {
            "channels": {},
            "task_reminder_minutes": {},
            "sent": {},
            "project_deletions": [],
        }

    async def initialize(self) -> None:
        loaded = await self._load()
        if isinstance(loaded, dict):
            # 按模板里各键自己的类型校验：dict 键收 dict，list 键收 list。用 ``type(default)``
            # 而不是写死 dict，是为了 ``project_deletions`` 这种列表型状态也能落地——
            # 旧版本存下的 KV 里没有这个键，``loaded.get`` 给 None，自然跳过。
            for key, default in self._state.items():
                value = loaded.get(key)
                if isinstance(value, type(default)):
                    self._state[key] = value

    def channel(self, key: str) -> dict[str, Any] | None:
        value = self._state["channels"].get(key)
        return deepcopy(value) if value else None

    def channels(self) -> dict[str, dict[str, Any]]:
        return deepcopy(self._state["channels"])

    def task_reminder_minutes(self, task_id: int, default: int) -> int:
        return int(self._state["task_reminder_minutes"].get(str(task_id), default))

    def task_reminder_overrides(self) -> dict[int, int]:
        """返回 ``{任务ID: 自定义提前分钟数}`` 的快照。"""
        overrides: dict[int, int] = {}
        for key, value in self._state["task_reminder_minutes"].items():
            try:
                overrides[int(key)] = int(value)
            except (TypeError, ValueError):
                continue
        return overrides

    def was_sent(self, key: str) -> bool:
        return key in self._state["sent"]

    async def register_channel(self, key: str, channel: dict[str, Any]) -> None:
        async with self._lock:
            current = self._state["channels"].get(key, {})
            channel.setdefault("reminders_enabled", current.get("reminders_enabled", True))
            self._state["channels"][key] = channel
            await self._save(deepcopy(self._state))

    async def set_reminders_enabled(self, key: str, enabled: bool) -> None:
        async with self._lock:
            if key not in self._state["channels"]:
                raise KeyError(key)
            self._state["channels"][key]["reminders_enabled"] = enabled
            await self._save(deepcopy(self._state))

    async def set_task_reminder(self, task_id: int, minutes: int) -> None:
        async with self._lock:
            self._state["task_reminder_minutes"][str(task_id)] = int(minutes)
            await self._save(deepcopy(self._state))

    async def clear_task_reminder(self, task_id: int) -> bool:
        """删除逐任务阈值，回落到全局默认。返回是否真的删掉了。"""
        async with self._lock:
            removed = self._state["task_reminder_minutes"].pop(str(task_id), None) is not None
            if removed:
                await self._save(deepcopy(self._state))
        return removed

    def project_deletions(self) -> list[dict[str, Any]]:
        """项目删除审计记录，最近的在前。"""
        return deepcopy(self._state["project_deletions"])

    async def record_project_deletion(self, entry: dict[str, Any]) -> None:
        """记一条项目删除审计，只保留最近 ``AUDIT_LIMIT`` 条。

        服务端的 ``Project.Delete`` 会把项目里的任务**硬删除**，没有回收站也没有撤销接口，
        所以这条记录是事后唯一的凭据——写入失败必须让调用方看见，不能被吞掉。
        """
        async with self._lock:
            self._state["project_deletions"].insert(0, entry)
            del self._state["project_deletions"][AUDIT_LIMIT:]
            await self._save(deepcopy(self._state))

    async def mark_sent(self, key: str, sent_at: str) -> None:
        async with self._lock:
            self._state["sent"][key] = sent_at
            if len(self._state["sent"]) > 2000:
                oldest = sorted(self._state["sent"].items(), key=lambda item: item[1])[:500]
                for old_key, _ in oldest:
                    self._state["sent"].pop(old_key, None)
            await self._save(deepcopy(self._state))
