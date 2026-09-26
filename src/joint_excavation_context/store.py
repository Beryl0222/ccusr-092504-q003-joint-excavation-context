"""只追加事件存储：保留到达顺序，支持按历史时点回放。

存储只负责交换层校验与幂等去重，不改写事件内容；
晚到事件按到达顺序追加，历史时点回放以 occurred_at 过滤。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterator, Mapping

from .contracts import ContractIssue, validate_event


def parse_instant(value: str) -> datetime:
    """把契约要求的带时区时间解析为可比较的即时。"""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class EventStore:
    """按到达顺序保存已校验事件，event_id 幂等去重。"""

    def __init__(self, schema: Mapping[str, Any]) -> None:
        self._schema = schema
        self._events: list[dict[str, Any]] = []
        self._by_id: dict[str, dict[str, Any]] = {}

    @property
    def schema(self) -> Mapping[str, Any]:
        return self._schema

    def append(self, event: Mapping[str, Any]) -> list[ContractIssue]:
        """校验并追加事件；重复 event_id 视为已受理，返回空问题列表。"""
        issues = validate_event(event, self._schema)
        if issues:
            return issues
        event_id = event["event_id"]
        if event_id in self._by_id:
            return []
        record = dict(event)
        record["sequence"] = len(self._events) + 1
        self._events.append(record)
        self._by_id[event_id] = record
        return []

    def contains(self, event_id: str) -> bool:
        return event_id in self._by_id

    def get(self, event_id: str) -> dict[str, Any] | None:
        return self._by_id.get(event_id)

    def events(
        self,
        aggregate_type: str | None = None,
        aggregate_id: str | None = None,
        event_type: str | None = None,
        as_of: str | None = None,
    ) -> list[dict[str, Any]]:
        """按到达顺序返回筛选结果；as_of 只保留不晚于该时点发生的事件。"""
        cutoff = parse_instant(as_of) if as_of else None
        result = []
        for event in self._events:
            if aggregate_type is not None and event["aggregate_type"] != aggregate_type:
                continue
            if aggregate_id is not None and event["aggregate_id"] != aggregate_id:
                continue
            if event_type is not None and event["event_type"] != event_type:
                continue
            if cutoff is not None and parse_instant(event["occurred_at"]) > cutoff:
                continue
            result.append(event)
        return result

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self._events)
