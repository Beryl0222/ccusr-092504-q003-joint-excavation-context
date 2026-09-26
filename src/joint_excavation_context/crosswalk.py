"""双方编号对照（探方号、测绘版本号、文物暂存号）。

中埃双方各自的编号体系不动，对照关系以 ALIAS_REGISTERED 事件登记，
本投影把 (entity_type, party, local_code) 映射到档案规范标识，供
入库层在追加前把现场编号解析为规范标识，也供查询层双向翻译。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .store import EventStore, StoredEvent


class AliasConflict(Exception):
    """同一方同一本地编号被登记到不同的规范标识。"""


@dataclass(frozen=True)
class AliasRecord:
    entity_type: str
    party: str
    local_code: str
    canonical_id: str
    event_id: str


class IdentifierCrosswalk:
    """编号对照投影；从事件存储重放构建。"""

    def __init__(self) -> None:
        self._by_local: dict[tuple[str, str, str], AliasRecord] = {}
        self._by_canonical: dict[str, list[AliasRecord]] = {}

    def apply(self, event: StoredEvent) -> None:
        if event.event_type != "ALIAS_REGISTERED":
            return
        data = event.data
        record = AliasRecord(
            entity_type=str(data.get("entity_type", "")),
            party=str(data.get("party", "")),
            local_code=str(data.get("local_code", "")),
            canonical_id=str(data.get("canonical_id", "")),
            event_id=event.event_id,
        )
        key = (record.entity_type, record.party, record.local_code)
        existing = self._by_local.get(key)
        if existing is not None and existing.canonical_id != record.canonical_id:
            raise AliasConflict(
                f"{record.party} 方编号 {record.local_code} 已对照到 "
                f"{existing.canonical_id}，不能再对照到 {record.canonical_id}"
            )
        self._by_local[key] = record
        self._by_canonical.setdefault(record.canonical_id, [])
        if record not in self._by_canonical[record.canonical_id]:
            self._by_canonical[record.canonical_id].append(record)

    def resolve(self, entity_type: str, party: str, local_code: str) -> Optional[str]:
        """把某方本地编号解析为规范标识；未登记时返回 None。"""
        record = self._by_local.get((entity_type, party, local_code))
        return record.canonical_id if record else None

    def aliases_of(self, canonical_id: str) -> list[AliasRecord]:
        """列出规范标识对应的全部各方编号。"""
        return list(self._by_canonical.get(canonical_id, []))

    def check(self, entity_type: str, party: str, local_code: str, canonical_id: str) -> None:
        """入库前预检：本地编号若已对照到其他规范标识则抛 AliasConflict。"""
        existing = self._by_local.get((entity_type, party, local_code))
        if existing is not None and existing.canonical_id != canonical_id:
            raise AliasConflict(
                f"{party} 方编号 {local_code} 已对照到 {existing.canonical_id}，"
                f"与本次声明的 {canonical_id} 不一致"
            )


def build_crosswalk(store: EventStore) -> IdentifierCrosswalk:
    crosswalk = IdentifierCrosswalk()
    for event in store:
        crosswalk.apply(event)
    return crosswalk
