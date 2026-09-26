"""追加式事件存储。

档案服务只追加事件，从不改写历史：新发现使旧主张失效或缩小适用范围
时，也只是追加新的状态事件。存储层提供三件事：

- event_id 幂等：同一事件重复提交（离线重传）只保留第一份；
- 聚合版本校验：同一聚合流内版本不得重复（同测点异内容由入库层
  在追加前裁决，见 ingest.py）；
- recorded_at：入库时间在追加时写入，与 occurred_at（现场时间）
  共同支持按任一历史时点复原。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class StoredEvent:
    """已入库事件：信封 + 入库时间 + 全局序号。"""

    envelope: Mapping[str, Any]
    recorded_at: str
    sequence: int

    @property
    def event_id(self) -> str:
        return str(self.envelope["event_id"])

    @property
    def event_type(self) -> str:
        return str(self.envelope["event_type"])

    @property
    def aggregate_type(self) -> str:
        return str(self.envelope["aggregate_type"])

    @property
    def aggregate_id(self) -> str:
        return str(self.envelope["aggregate_id"])

    @property
    def occurred_at(self) -> str:
        return str(self.envelope["occurred_at"])

    @property
    def version(self) -> int:
        return int(self.envelope["version"])

    @property
    def data(self) -> Mapping[str, Any]:
        data = self.envelope.get("data")
        return data if isinstance(data, Mapping) else {}

    def as_dict(self) -> dict[str, Any]:
        payload = dict(self.envelope)
        payload["recorded_at"] = self.recorded_at
        return payload


class VersionConflict(Exception):
    """同一聚合流内版本重复（同测点异内容冲突在入库层先行裁决）。"""


class EventStore:
    """内存事件流，可选 JSONL 文件持久化。"""

    def __init__(self, path: Optional[Path] = None, clock: Clock = _utcnow) -> None:
        self._path = Path(path) if path is not None else None
        self._clock = clock
        self._events: list[StoredEvent] = []
        self._by_id: dict[str, StoredEvent] = {}
        self._versions: dict[tuple[str, str], set[int]] = {}
        if self._path is not None and self._path.exists():
            self._load()

    def _load(self) -> None:
        """载入持久化事件；容忍最后一行因中断而写坏。"""
        assert self._path is not None
        with self._path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    envelope = json.loads(line)
                except json.JSONDecodeError:
                    break  # 中断残留的半行：丢弃，之后的写入会补齐
                recorded_at = str(envelope.pop("recorded_at", ""))
                self._register(envelope, recorded_at)

    def _register(self, envelope: Mapping[str, Any], recorded_at: str) -> StoredEvent:
        event = StoredEvent(
            envelope=dict(envelope),
            recorded_at=recorded_at,
            sequence=len(self._events) + 1,
        )
        self._events.append(event)
        self._by_id[event.event_id] = event
        key = (event.aggregate_type, event.aggregate_id)
        self._versions.setdefault(key, set()).add(event.version)
        return event

    def append(self, envelope: Mapping[str, Any]) -> tuple[StoredEvent, bool]:
        """追加事件。返回 (事件, 是否新写入)；event_id 重复时幂等返回旧事件。"""
        event_id = str(envelope.get("event_id", ""))
        existing = self._by_id.get(event_id)
        if existing is not None:
            return existing, False

        key = (str(envelope.get("aggregate_type", "")), str(envelope.get("aggregate_id", "")))
        version = int(envelope.get("version", 0))
        if version in self._versions.get(key, set()):
            raise VersionConflict(
                f"聚合 {key[0]}/{key[1]} 已存在版本 {version}，"
                "同一测点的异内容须由入库层裁决后走隔离流程"
            )

        recorded_at = _iso(self._clock())
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(dict(envelope, recorded_at=recorded_at), ensure_ascii=False)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
        return self._register(envelope, recorded_at), True

    def __iter__(self) -> Iterator[StoredEvent]:
        return iter(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def get(self, event_id: str) -> Optional[StoredEvent]:
        return self._by_id.get(event_id)

    def has(self, event_id: str) -> bool:
        return event_id in self._by_id

    def stream(self, aggregate_type: str, aggregate_id: str) -> list[StoredEvent]:
        return [
            event for event in self._events
            if event.aggregate_type == aggregate_type and event.aggregate_id == aggregate_id
        ]

    def of_type(self, event_type: str) -> list[StoredEvent]:
        return [event for event in self._events if event.event_type == event_type]

    def stream_version(self, aggregate_type: str, aggregate_id: str) -> int:
        """聚合流当前最大版本，用于调用方推算下一版本号。"""
        versions = self._versions.get((aggregate_type, aggregate_id))
        return max(versions) if versions else 0


def replay(events: Iterable[StoredEvent], apply: Callable[[StoredEvent], None]) -> None:
    for event in events:
        apply(event)
