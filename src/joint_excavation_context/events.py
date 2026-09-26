"""领域事件信封构造器。

所有现场与档案数据都通过统一信封进入档案服务；构造器只负责
组装信封，不做领域校验（契约校验见 contracts.py，领域门禁见
ingest.py 的 DomainGate）。
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

# 保管阶段：出土 -> 清理 -> 送检 -> 归还（库房）
CUSTODY_STAGES = ("excavated", "cleaned", "sent_for_analysis", "returned")

# 可争论的解释主题：必须经过双方授权会审
DEBATABLE_TOPICS = ("dual_sacred_lake_layout", "building_chronology", "ritual_function")

# 空间关系谓词
RELATION_PREDICATES = (
    "within",
    "covers",
    "covered_by",
    "cuts",
    "cut_by",
    "fills",
    "abuts",
    "overlies",
    "underlies",
)


def make_envelope(
    event_id: str,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    occurred_at: str,
    version: int,
    summary: str,
    data: Optional[Mapping[str, Any]] = None,
    source: Optional[Mapping[str, Any]] = None,
    batch_id: Optional[str] = None,
) -> dict[str, Any]:
    """组装一个领域事件信封（尚未入库，无 recorded_at）。"""
    envelope: dict[str, Any] = {
        "event_id": event_id,
        "event_type": event_type,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "occurred_at": occurred_at,
        "version": version,
        "summary": summary,
        "data": dict(data or {}),
    }
    if source is not None:
        envelope["source"] = dict(source)
    if batch_id is not None:
        envelope["batch_id"] = batch_id
    return envelope


def context_opened(
    event_id: str,
    aggregate_type: str,
    aggregate_id: str,
    occurred_at: str,
    version: int,
    summary: str,
    *,
    kind: str,
    label: str,
    parent_id: Optional[str] = None,
    coordinates: Optional[Mapping[str, Any]] = None,
    attributes: Optional[Mapping[str, Any]] = None,
    **envelope_kw: Any,
) -> dict[str, Any]:
    """开启一个语境实体（调查区/地层单位/构筑物/探方/样品/容器）。"""
    data: dict[str, Any] = {"kind": kind, "label": label}
    if parent_id is not None:
        data["parent_id"] = parent_id
    if coordinates is not None:
        data["coordinates"] = dict(coordinates)
    if attributes:
        data["attributes"] = dict(attributes)
    return make_envelope(
        event_id, "CONTEXT_OPENED", aggregate_type, aggregate_id,
        occurred_at, version, summary, data, **envelope_kw,
    )


def alias_registered(
    event_id: str,
    occurred_at: str,
    version: int,
    summary: str,
    *,
    canonical_id: str,
    local_code: str,
    party: str,
    entity_type: str,
    **envelope_kw: Any,
) -> dict[str, Any]:
    """登记一条编号对照：某方本地编号 -> 档案规范标识。"""
    return make_envelope(
        event_id, "ALIAS_REGISTERED", "identifier_crosswalk", "crosswalk",
        occurred_at, version, summary,
        {
            "canonical_id": canonical_id,
            "local_code": local_code,
            "party": party,
            "entity_type": entity_type,
        },
        **envelope_kw,
    )


def observation_captured(
    event_id: str,
    point_id: str,
    occurred_at: str,
    version: int,
    summary: str,
    *,
    content: Mapping[str, Any],
    content_hash: str,
    **envelope_kw: Any,
) -> dict[str, Any]:
    """在某个测点上捕获一条观测内容。"""
    return make_envelope(
        event_id, "OBSERVATION_CAPTURED", "measurement_point", point_id,
        occurred_at, version, summary,
        {"content": dict(content), "content_hash": content_hash},
        **envelope_kw,
    )


def relation_recorded(
    event_id: str,
    aggregate_type: str,
    aggregate_id: str,
    occurred_at: str,
    version: int,
    summary: str,
    *,
    subject_id: str,
    predicate: str,
    target_id: str,
    based_on: Optional[list[str]] = None,
    **envelope_kw: Any,
) -> dict[str, Any]:
    """记录一条空间/地层关系，based_on 列出其依赖的观测事件。"""
    return make_envelope(
        event_id, "RELATION_RECORDED", aggregate_type, aggregate_id,
        occurred_at, version, summary,
        {
            "subject_id": subject_id,
            "predicate": predicate,
            "target_id": target_id,
            "based_on": list(based_on or []),
        },
        **envelope_kw,
    )


def survey_revised(
    event_id: str,
    revision_id: str,
    occurred_at: str,
    version: int,
    summary: str,
    *,
    survey_of: str,
    supersedes: Optional[str] = None,
    geometry: Optional[Mapping[str, Any]] = None,
    based_on: Optional[list[str]] = None,
    **envelope_kw: Any,
) -> dict[str, Any]:
    """登记一版测绘成果；supersedes 指向被取代的旧版本。"""
    data: dict[str, Any] = {"survey_of": survey_of, "based_on": list(based_on or [])}
    if supersedes is not None:
        data["supersedes"] = supersedes
    if geometry is not None:
        data["geometry"] = dict(geometry)
    return make_envelope(
        event_id, "SURVEY_REVISED", "survey_revision", revision_id,
        occurred_at, version, summary, data, **envelope_kw,
    )


def object_recovered(
    event_id: str,
    object_id: str,
    occurred_at: str,
    version: int,
    summary: str,
    *,
    found_in: str,
    custodian: str,
    container_id: Optional[str] = None,
    point_events: Optional[list[str]] = None,
    **envelope_kw: Any,
) -> dict[str, Any]:
    """登记对象出土，同时确立首位保管责任人。"""
    data: dict[str, Any] = {
        "found_in": found_in,
        "custodian": custodian,
        "point_events": list(point_events or []),
    }
    if container_id is not None:
        data["container_id"] = container_id
    return make_envelope(
        event_id, "OBJECT_RECOVERED", "archaeological_object", object_id,
        occurred_at, version, summary, data, **envelope_kw,
    )


def object_transferred(
    event_id: str,
    object_id: str,
    occurred_at: str,
    version: int,
    summary: str,
    *,
    stage: str,
    from_party: str,
    to_party: str,
    container_id: Optional[str] = None,
    **envelope_kw: Any,
) -> dict[str, Any]:
    """登记一次保管交接（清理/送检/归还/入库）。"""
    data: dict[str, Any] = {
        "stage": stage,
        "from_party": from_party,
        "to_party": to_party,
    }
    if container_id is not None:
        data["container_id"] = container_id
    return make_envelope(
        event_id, "OBJECT_TRANSFERRED", "archaeological_object", object_id,
        occurred_at, version, summary, data, **envelope_kw,
    )


def seal_anomaly_reported(
    event_id: str,
    container_id: str,
    occurred_at: str,
    version: int,
    summary: str,
    *,
    detail: str,
    **envelope_kw: Any,
) -> dict[str, Any]:
    """报告容器封签异常；入库后服务会立即冻结该容器。"""
    return make_envelope(
        event_id, "SEAL_ANOMALY_REPORTED", "custody_container", container_id,
        occurred_at, version, summary, {"detail": detail}, **envelope_kw,
    )


def claim_proposed(
    event_id: str,
    claim_id: str,
    occurred_at: str,
    version: int,
    summary: str,
    *,
    topic: str,
    statement: str,
    scope: Optional[Mapping[str, Any]] = None,
    citations: Optional[list[str]] = None,
    supersedes: Optional[list[Mapping[str, Any]]] = None,
    **envelope_kw: Any,
) -> dict[str, Any]:
    """提出一条研究主张，citations 必须引用明确的观测事件。

    supersedes 声明本主张通过会审后将取代的旧主张：
    [{"claim_id": ..., "mode": "invalidate"|"narrow", "narrowed_scope": {...}}]
    """
    data: dict[str, Any] = {
        "topic": topic,
        "statement": statement,
        "scope": dict(scope or {}),
        "citations": list(citations or []),
    }
    if supersedes:
        data["supersedes"] = [dict(item) for item in supersedes]
    return make_envelope(
        event_id, "CLAIM_PROPOSED", "research_claim", claim_id,
        occurred_at, version, summary, data, **envelope_kw,
    )


def claim_reviewed(
    event_id: str,
    claim_id: str,
    occurred_at: str,
    version: int,
    summary: str,
    *,
    decision: str,
    reviewer: str,
    party: str,
    **envelope_kw: Any,
) -> dict[str, Any]:
    """登记一次会审意见；通过需要双方授权会审人各自同意。"""
    return make_envelope(
        event_id, "CLAIM_REVIEWED", "research_claim", claim_id,
        occurred_at, version, summary,
        {"decision": decision, "reviewer": reviewer, "party": party},
        **envelope_kw,
    )


def claim_superseded(
    event_id: str,
    claim_id: str,
    occurred_at: str,
    version: int,
    summary: str,
    *,
    by_claim: str,
    mode: str,
    narrowed_scope: Optional[Mapping[str, Any]] = None,
    **envelope_kw: Any,
) -> dict[str, Any]:
    """宣告一条主张被新主张取代（invalidate）或缩小适用范围（narrow）。

    只追加状态，不修改被取代主张的任何历史记录。
    """
    data: dict[str, Any] = {"by_claim": by_claim, "mode": mode}
    if narrowed_scope is not None:
        data["narrowed_scope"] = dict(narrowed_scope)
    return make_envelope(
        event_id, "CLAIM_SUPERSEDED", "research_claim", claim_id,
        occurred_at, version, summary, data, **envelope_kw,
    )


def record_published(
    event_id: str,
    aggregate_type: str,
    aggregate_id: str,
    occurred_at: str,
    version: int,
    summary: str,
    **envelope_kw: Any,
) -> dict[str, Any]:
    """将一条记录发布到公开目录（主张须先通过会审）。"""
    return make_envelope(
        event_id, "RECORD_PUBLISHED", aggregate_type, aggregate_id,
        occurred_at, version, summary, {}, **envelope_kw,
    )
