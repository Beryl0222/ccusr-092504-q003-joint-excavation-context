"""现场数据接收：幂等去重、晚到受理与测点冲突隔离。

探方离线采集的事件可能重复或晚到：同一 event_id 只受理一次，
occurred_at 早于已入库事件不影响受理。同一测点出现内容不同的
观测时，只把依赖该测点的成果（测绘成果、样品等聚合）隔离待核，
其他区域的事件继续入库；隔离由人工核对后解除。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .contracts import ContractIssue, validate_event
from .store import EventStore

# 受理结果状态
ACCEPTED = "accepted"
DUPLICATE = "duplicate"
REJECTED = "rejected"
QUARANTINED = "quarantined"


@dataclass(frozen=True)
class IngestResult:
    status: str
    issues: tuple[ContractIssue, ...] = ()
    quarantined_aggregates: tuple[str, ...] = ()


@dataclass
class IngestionService:
    """在事件存储之上叠加冲突检测与隔离。"""

    store: EventStore
    _point_content: dict[str, str] = field(default_factory=dict)
    _point_dependents: dict[str, set[str]] = field(default_factory=dict)
    _quarantined: set[str] = field(default_factory=set)

    def ingest(self, event: Mapping[str, Any]) -> IngestResult:
        issues = validate_event(event, self.store.schema)
        if issues:
            return IngestResult(REJECTED, issues=tuple(issues))
        if self.store.contains(event["event_id"]):
            return IngestResult(DUPLICATE)

        aggregate_id = event["aggregate_id"]
        if aggregate_id in self._quarantined:
            return IngestResult(QUARANTINED, quarantined_aggregates=(aggregate_id,))

        if event["event_type"] == "OBSERVATION_CAPTURED":
            conflict = self._check_observation(event)
            if conflict is not None:
                return conflict

        # 登记成果对测点的依赖，供后续冲突时按测点圈定隔离范围
        for point in event.get("source_points", ()):
            self._point_dependents.setdefault(point, set()).add(aggregate_id)

        self.store.append(event)
        return IngestResult(ACCEPTED)

    def _check_observation(self, event: Mapping[str, Any]) -> IngestResult | None:
        point = event.get("survey_point")
        content = event.get("content_hash")
        if not point or not content:
            return None
        existing = self._point_content.get(point)
        if existing is None or existing == content:
            self._point_content[point] = content
            return None
        # 同一测点异内容：隔离依赖该测点的全部成果与冲突事件所属聚合
        dependents = sorted(self._point_dependents.get(point, set()) | {event["aggregate_id"]})
        self._quarantined.update(dependents)
        return IngestResult(QUARANTINED, quarantined_aggregates=tuple(dependents))

    def is_quarantined(self, aggregate_id: str) -> bool:
        return aggregate_id in self._quarantined

    def release(self, aggregate_id: str) -> None:
        """人工核对测点冲突后解除隔离，该聚合的事件恢复入库。"""
        self._quarantined.discard(aggregate_id)
