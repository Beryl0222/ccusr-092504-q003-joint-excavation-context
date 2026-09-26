"""查询投影：按任一历史时点复原空间关系与证据链。

- 时间轴可选 occurred（现场时间）或 recorded（入库时间）；默认按现场
  时间复原"当时已知"的状态，按入库时间复原"当时系统里有什么"；
- 被隔离的事件在任何时点复原中都不计入有效状态（隔离标记本身也按
  时点过滤：标记之前的复原仍能看到后来的争议内容，因为当时它尚未
  被隔离）；
- 主张状态（提议/会审中/通过/失效/缩限）同样按时点重放。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional

from .claims import ClaimRegistry
from .ingest import _dependencies_of
from .store import EventStore, StoredEvent


def _parse(moment: str) -> datetime:
    return datetime.fromisoformat(moment.replace("Z", "+00:00"))


@dataclass
class EntitySnapshot:
    aggregate_type: str
    aggregate_id: str
    kind: str = ""
    label: str = ""
    parent_id: Optional[str] = None
    coordinates: Optional[Mapping[str, Any]] = None
    attributes: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class RelationEdge:
    subject_id: str
    predicate: str
    target_id: str
    event_id: str


@dataclass
class SpatialView:
    as_of: Optional[str]
    time_axis: str
    entities: dict[str, EntitySnapshot] = field(default_factory=dict)
    relations: list[RelationEdge] = field(default_factory=list)
    observations: dict[str, list[str]] = field(default_factory=dict)  # point -> 有效观测事件
    survey_revisions: dict[str, str] = field(default_factory=dict)    # survey_of -> 当前版本
    claims: dict[str, str] = field(default_factory=dict)              # claim_id -> 状态
    custody: dict[str, str] = field(default_factory=dict)             # object -> 保管人


def _visible_events(
    store: EventStore,
    as_of: Optional[str],
    time_axis: str,
) -> list[StoredEvent]:
    if as_of is None:
        return list(store)
    limit = _parse(as_of)
    result = []
    for event in store:
        moment = event.occurred_at if time_axis == "occurred" else event.recorded_at
        if _parse(moment) <= limit:
            result.append(event)
    return result


def _quarantined_ids(events: list[StoredEvent]) -> set[str]:
    tainted: set[str] = set()
    for event in events:
        if event.event_type == "QUARANTINE_MARKED":
            tainted.update(str(x) for x in event.data.get("event_ids", []))
    return tainted


def spatial_view(
    store: EventStore,
    as_of: Optional[str] = None,
    time_axis: str = "occurred",
) -> SpatialView:
    """复原某一时点的空间关系图与主张状态。"""
    events = _visible_events(store, as_of, time_axis)
    tainted = _quarantined_ids(events)
    view = SpatialView(as_of=as_of, time_axis=time_axis)
    registry = ClaimRegistry()

    for event in events:
        if event.event_id in tainted:
            continue
        data = event.data
        etype = event.event_type

        if etype == "CONTEXT_OPENED":
            view.entities[event.aggregate_id] = EntitySnapshot(
                aggregate_type=event.aggregate_type,
                aggregate_id=event.aggregate_id,
                kind=str(data.get("kind", "")),
                label=str(data.get("label", "")),
                parent_id=data.get("parent_id"),
                coordinates=data.get("coordinates"),
                attributes=dict(data.get("attributes", {})),
            )
        elif etype == "OBSERVATION_CAPTURED":
            view.observations.setdefault(event.aggregate_id, []).append(event.event_id)
        elif etype == "RELATION_RECORDED":
            view.relations.append(RelationEdge(
                subject_id=str(data.get("subject_id", "")),
                predicate=str(data.get("predicate", "")),
                target_id=str(data.get("target_id", "")),
                event_id=event.event_id,
            ))
        elif etype == "SURVEY_REVISED":
            view.survey_revisions[str(data.get("survey_of", ""))] = event.aggregate_id
        elif etype == "OBJECT_RECOVERED":
            view.custody[event.aggregate_id] = str(data.get("custodian", ""))
        elif etype == "OBJECT_TRANSFERRED":
            view.custody[event.aggregate_id] = str(data.get("to_party", ""))

        registry.apply(event)

    for claim_id, state in registry._claims.items():
        view.claims[claim_id] = state.status
    return view


def evidence_chain(
    store: EventStore,
    claim_id: str,
    as_of: Optional[str] = None,
    time_axis: str = "occurred",
) -> Optional[dict[str, Any]]:
    """复原主张在某一时点的证据链：引用观测 -> 依赖这些观测的成果。"""
    events = _visible_events(store, as_of, time_axis)
    tainted = _quarantined_ids(events)
    by_id = {event.event_id: event for event in events}

    proposal = next(
        (e for e in events
         if e.event_type == "CLAIM_PROPOSED" and e.aggregate_id == claim_id),
        None,
    )
    if proposal is None:
        return None

    citations = [str(c) for c in proposal.data.get("citations", [])]
    observations = []
    for citation in citations:
        observed = by_id.get(citation)
        observations.append({
            "event_id": citation,
            "point": observed.aggregate_id if observed else None,
            "occurred_at": observed.occurred_at if observed else None,
            "content": dict(observed.data.get("content", {})) if observed else None,
            "quarantined": citation in tainted,
            "missing": observed is None,
        })

    dependents = []
    for event in events:
        if event.event_id in tainted or event.event_id == proposal.event_id:
            continue
        deps = _dependencies_of(event)
        if any(citation in deps for citation in citations):
            dependents.append({
                "event_id": event.event_id,
                "event_type": event.event_type,
                "aggregate_id": event.aggregate_id,
            })

    registry = ClaimRegistry()
    for event in events:
        registry.apply(event)
    state = registry.state_of(claim_id)

    return {
        "claim_id": claim_id,
        "as_of": as_of,
        "time_axis": time_axis,
        "statement": str(proposal.data.get("statement", "")),
        "topic": str(proposal.data.get("topic", "")),
        "status": state.status if state else "proposed",
        "observations": observations,
        "dependents": dependents,
    }
