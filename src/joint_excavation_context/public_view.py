"""公开查询视图：隐藏精确坐标、未发表材料与内部保管位置。

公开视图只包含已发布（RECORD_PUBLISHED）的聚合；主张必须已通过会审
并发布。坐标与测绘几何不出库，只保留粗粒度位置标签；保管信息只
保留阶段，不暴露保管人、容器与存放位置。
"""

from __future__ import annotations

from typing import Any, Optional

from .archive import _visible_events, evidence_chain, spatial_view
from .store import EventStore


def _published_ids(store: EventStore, as_of: Optional[str], time_axis: str) -> set[str]:
    published: set[str] = set()
    for event in _visible_events(store, as_of, time_axis):
        if event.event_type == "RECORD_PUBLISHED":
            published.add(event.aggregate_id)
    return published


def public_spatial_view(
    store: EventStore,
    as_of: Optional[str] = None,
    time_axis: str = "occurred",
) -> dict[str, Any]:
    """公开版空间视图：仅已发布实体，坐标与内部保管信息已剔除。"""
    internal = spatial_view(store, as_of, time_axis)
    published = _published_ids(store, as_of, time_axis)

    entities = {}
    for entity_id, snapshot in internal.entities.items():
        if entity_id not in published:
            continue
        entities[entity_id] = {
            "type": snapshot.aggregate_type,
            "kind": snapshot.kind,
            "label": snapshot.label,
            "parent_id": snapshot.parent_id if snapshot.parent_id in published else None,
            # coordinates 与 attributes 中的内部字段一律不公开
        }

    entity_ids = set(entities)
    relations = [
        {"subject_id": edge.subject_id, "predicate": edge.predicate, "target_id": edge.target_id}
        for edge in internal.relations
        if edge.subject_id in entity_ids and edge.target_id in entity_ids
    ]

    claims = {
        claim_id: {"status": status}
        for claim_id, status in internal.claims.items()
        if claim_id in published and status in ("approved", "narrowed")
    }

    return {
        "as_of": as_of,
        "time_axis": internal.time_axis,
        "entities": entities,
        "relations": relations,
        "claims": claims,
        # observations / custody / survey geometry：不公开
    }


def public_evidence_chain(
    store: EventStore,
    claim_id: str,
    as_of: Optional[str] = None,
    time_axis: str = "occurred",
) -> Optional[dict[str, Any]]:
    """公开版证据链：未发表的主张与未发表的观测一律不可见。"""
    published = _published_ids(store, as_of, time_axis)
    if claim_id not in published:
        return None
    chain = evidence_chain(store, claim_id, as_of, time_axis)
    if chain is None or chain["status"] not in ("approved", "narrowed"):
        return None

    observations = []
    for item in chain["observations"]:
        point = item["point"]
        if point not in published or item["quarantined"] or item["missing"]:
            continue  # 未发表材料与隔离内容不出现在公开证据链
        observations.append({
            "event_id": item["event_id"],
            "occurred_at": item["occurred_at"],
            # content 中的精确坐标等细节不公开，仅保留事件引用
        })

    return {
        "claim_id": claim_id,
        "as_of": as_of,
        "statement": chain["statement"],
        "topic": chain["topic"],
        "status": chain["status"],
        "observations": observations,
    }
