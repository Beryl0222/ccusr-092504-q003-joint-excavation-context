"""历史时点复原与公开查询视图。

研究者可按任一时点复原当时的空间关系（调查区、探方、地层、
构筑物、出土对象、有效测绘版本）与主张证据链；公开查询在此
之上再隐藏精确坐标、未发表材料与内部保管位置。
"""

from __future__ import annotations

from typing import Any

from .claims import ClaimRecord
from .store import EventStore

# 公开查询可见的事件类型；保管交接、封签等内部事件不外发
PUBLIC_EVENT_TYPES = frozenset({
    "CONTEXT_OPENED",
    "OBSERVATION_CAPTURED",
    "CLAIM_SUBMITTED",
    "CLAIM_REVIEWED",
    "CLAIM_SUPERSEDED",
    "CLAIM_SCOPE_NARROWED",
    "RECORD_PUBLISHED",
})

# 公开视图中必须隐藏的字段：精确坐标与内部保管位置
REDACTED_KEYS = frozenset({
    "coordinates",
    "precise_point",
    "custody_location",
    "container_id",
})


def spatial_snapshot(store: EventStore, as_of: str) -> dict[str, Any]:
    """复原某一时点的空间关系与当时有效的测绘版本。"""
    areas: dict[str, Any] = {}
    units: dict[str, Any] = {}
    structures: dict[str, Any] = {}
    objects: dict[str, Any] = {}
    revisions: dict[str, Any] = {}
    for event in store.events(as_of=as_of):
        aggregate_type = event["aggregate_type"]
        aggregate_id = event["aggregate_id"]
        if aggregate_type == "survey_area" and event["event_type"] == "CONTEXT_OPENED":
            areas[aggregate_id] = {"summary": event["summary"], "grid": event.get("grid")}
        elif aggregate_type in ("excavation_unit", "stratigraphic_unit") \
                and event["event_type"] == "CONTEXT_OPENED":
            units[aggregate_id] = {
                "area": event.get("parent_id"),
                "layer": event.get("layer"),
                "summary": event["summary"],
            }
        elif aggregate_type == "structure":
            structures[aggregate_id] = {
                "unit": event.get("parent_id"),
                "summary": event["summary"],
            }
        elif aggregate_type == "archaeological_object":
            objects[aggregate_id] = {
                "found_in": event.get("found_in"),
                "summary": event["summary"],
            }
        elif aggregate_type == "survey_revision":
            current = revisions.get(aggregate_id)
            if current is None or event["version"] >= current["version"]:
                revisions[aggregate_id] = {
                    "version": event["version"],
                    "grid": event.get("grid"),
                    "summary": event["summary"],
                }
    return {
        "as_of": as_of,
        "areas": areas,
        "units": units,
        "structures": structures,
        "objects": objects,
        "survey_revisions": revisions,
    }


def evidence_chain(store: EventStore, record: ClaimRecord) -> dict[str, Any]:
    """展开主张版本引用的观测，形成可核对的证据链。"""
    observations: list[dict[str, Any]] = []
    missing: list[str] = []
    for event_id in record.citations:
        event = store.get(event_id)
        if event is None:
            missing.append(event_id)
        else:
            observations.append(event)
    return {
        "claim_id": record.claim_id,
        "version": record.version,
        "kind": record.kind,
        "scope": record.scope,
        "valid_from": record.valid_from,
        "valid_to": record.valid_to,
        "observations": observations,
        "missing": missing,
    }


def public_records(store: EventStore, as_of: str | None = None) -> list[dict[str, Any]]:
    """公开查询：仅已发表材料的公开事件，且隐藏精确坐标与内部保管位置。"""
    published = {
        event["aggregate_id"]
        for event in store.events(event_type="RECORD_PUBLISHED", as_of=as_of)
    }
    result = []
    for event in store.events(as_of=as_of):
        if event["aggregate_id"] not in published:
            continue
        if event["event_type"] not in PUBLIC_EVENT_TYPES:
            continue
        result.append({key: value for key, value in event.items() if key not in REDACTED_KEYS})
    return result
