"""离线批量入库：冲突隔离、领域门禁、可恢复批次。

设计要点：
- 探方离线采集的事件可能重复或晚到：event_id 相同按幂等去重；
  同测点同版本、内容哈希一致也视为重复采集；
- 同一测点出现不同内容时，只隔离该测点及其依赖成果（关系/测绘版本
  的 based_on、出土记录的 point_events、主张的 citations 构成依赖边，
  取传递闭包），其他区域事件照常入库；
- 领域门禁在提交顺序上即时生效：保管链不唯一的交接拒绝；封签异常
  事件一入库立刻追加系统 CONTAINER_FROZEN，同批次其后的交接即被拒；
- 每个批次是可恢复流程，步骤完成情况以 PROCESS_STEP_RECORDED 落库，
  中断重跑只补齐未完成步骤；commit 按 event_id 幂等，重复执行安全。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from .claims import ClaimRegistry
from .contracts import validate_event
from .crosswalk import AliasConflict, IdentifierCrosswalk
from .custody import CustodyLedger
from .events import RELATION_PREDICATES
from .store import EventStore, StoredEvent

BATCH_STEPS = ("validate", "conflicts", "commit")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---- 依赖图 ----------------------------------------------------------------

def _dependencies_of(envelope_or_event: Any) -> list[str]:
    """事件依赖的上游事件 id（观测/出土等明确证据）。"""
    if isinstance(envelope_or_event, StoredEvent):
        etype = envelope_or_event.event_type
        data = envelope_or_event.data
    else:
        etype = envelope_or_event["event_type"]
        data = envelope_or_event.get("data") or {}
    if etype in ("RELATION_RECORDED", "SURVEY_REVISED"):
        return [str(x) for x in data.get("based_on", [])]
    if etype == "OBJECT_RECOVERED":
        return [str(x) for x in data.get("point_events", [])]
    if etype == "CLAIM_PROPOSED":
        return [str(x) for x in data.get("citations", [])]
    return []


def _quarantine_closure(
    store: EventStore,
    candidates: list[Mapping[str, Any]],
    bad_points: set[str],
) -> tuple[set[str], set[str]]:
    """计算需要隔离的全部事件 id。

    节点 = 库内事件 + 本批候选事件；边 = 依赖关系。
    起点 = 争议测点上的观测事件。返回 (库内被隔离事件, 候选被隔离事件)。
    """
    nodes: dict[str, Any] = {}
    point_of: dict[str, str] = {}
    for event in store:
        nodes[event.event_id] = event
        if event.event_type == "OBSERVATION_CAPTURED":
            point_of[event.event_id] = event.aggregate_id
    for envelope in candidates:
        nodes[str(envelope["event_id"])] = envelope
        if envelope["event_type"] == "OBSERVATION_CAPTURED":
            point_of[str(envelope["event_id"])] = str(envelope["aggregate_id"])

    tainted: set[str] = set()
    stack = [eid for eid, point in point_of.items() if point in bad_points]
    while stack:
        current = stack.pop()
        if current in tainted:
            continue
        tainted.add(current)
        for node_id, node in nodes.items():
            if node_id not in tainted and current in _dependencies_of(node):
                stack.append(node_id)

    stored_ids = {event.event_id for event in store}
    return tainted & stored_ids, tainted - stored_ids


# ---- 报告 ------------------------------------------------------------------

@dataclass
class IngestReport:
    batch_id: str
    accepted: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    rejected: dict[str, list[str]] = field(default_factory=dict)
    quarantined: dict[str, list[str]] = field(default_factory=dict)  # point -> 事件 id
    quarantined_candidates: dict[str, list[str]] = field(default_factory=dict)
    frozen_containers: dict[str, str] = field(default_factory=dict)  # container -> 冻结事件 id
    steps_run: list[str] = field(default_factory=list)
    steps_skipped: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.rejected and not self.quarantined_candidates


# ---- 主服务 ----------------------------------------------------------------

class ArchiveService:
    """档案入库服务：持有存储与各投影，投影在提交过程中即时更新。"""

    def __init__(
        self,
        store: EventStore,
        schema: Mapping[str, Any],
        authorized_reviewers: Optional[set[tuple[str, str]]] = None,
    ) -> None:
        self.store = store
        self.schema = schema
        self.crosswalk = IdentifierCrosswalk()
        self.ledger = CustodyLedger()
        self.registry = ClaimRegistry(authorized_reviewers)
        self.quarantined_events: set[str] = set()   # 已被隔离标记的库内事件 id
        self.quarantined_points: set[str] = set()
        for event in store:
            self._replay(event)

    def _replay(self, event: StoredEvent) -> None:
        self.crosswalk.apply(event)
        self.ledger.apply(event)
        self.registry.apply(event)
        if event.event_type == "QUARANTINE_MARKED":
            self.quarantined_points.add(str(event.data.get("point_id", "")))
            self.quarantined_events.update(str(x) for x in event.data.get("event_ids", []))

    def authorize_reviewer(self, party: str, reviewer: str) -> None:
        self.registry._authorized.add((party, reviewer))

    # ---- 步骤检查点（批次导入与会审发布共用） ----

    def completed_steps(self, process_id: str) -> set[str]:
        return {
            str(event.data.get("step", ""))
            for event in self.store.stream("process_journal", process_id)
            if event.event_type == "PROCESS_STEP_RECORDED"
            and event.data.get("status") == "completed"
        }

    def record_step(self, process_id: str, step: str, detail: Mapping[str, Any]) -> None:
        version = self.store.stream_version("process_journal", process_id) + 1
        envelope = {
            "event_id": f"{process_id}:step:{step}",
            "event_type": "PROCESS_STEP_RECORDED",
            "aggregate_type": "process_journal",
            "aggregate_id": process_id,
            "occurred_at": _utcnow().astimezone(timezone.utc).isoformat(),
            "version": version,
            "summary": f"流程 {process_id} 完成步骤 {step}",
            "data": {"step": step, "status": "completed", **dict(detail)},
            "source": {"party": "system"},
            "batch_id": process_id,
        }
        self.store.append(envelope)

    # ---- 主流程 ----

    def ingest_batch(self, batch_id: str, envelopes: list[Mapping[str, Any]]) -> IngestReport:
        report = IngestReport(batch_id=batch_id)
        done = self.completed_steps(batch_id)

        # 步骤 1：契约层校验（纯过滤；恢复时重算但不重复落检查点）
        candidates: list[Mapping[str, Any]] = []
        if "validate" in done:
            report.steps_skipped.append("validate")
            candidates = [e for e in envelopes if not validate_event(e, self.schema)
                          and not self._structural_checks(e)]
        else:
            report.steps_run.append("validate")
            for envelope in envelopes:
                eid = str(envelope.get("event_id", f"#{len(candidates)}"))
                issues = validate_event(envelope, self.schema)
                issues = issues + self._structural_checks(envelope)
                if issues:
                    report.rejected[eid] = [f"{i.field}: {i.message}" for i in issues]
                else:
                    candidates.append(envelope)
            self.record_step(batch_id, "validate", {
                "received": len(envelopes),
                "rejected": sum(1 for e in envelopes if str(e.get("event_id", "")) in report.rejected),
            })

        # 步骤 2：同测点异内容检测与隔离闭包（隔离标记追加有幂等保护）
        if "conflicts" in done:
            report.steps_skipped.append("conflicts")
            candidates, excluded = self._detect_conflicts(batch_id, candidates, report)
        else:
            report.steps_run.append("conflicts")
            candidates, excluded = self._detect_conflicts(batch_id, candidates, report)
            self.record_step(batch_id, "conflicts", {
                "quarantined_points": sorted(report.quarantined_candidates),
            })

        # 步骤 3：门禁 + 提交（即时冻结联动），按 event_id 幂等可重跑
        if "commit" in done:
            report.steps_skipped.append("commit")
            for envelope in candidates:
                eid = str(envelope.get("event_id", ""))
                if self.store.has(eid):
                    report.duplicates.append(eid)
        else:
            report.steps_run.append("commit")
            self._commit(batch_id, candidates, excluded, report)
            self.record_step(batch_id, "commit", {
                "accepted": len(report.accepted),
                "frozen_containers": sorted(report.frozen_containers),
            })
        return report

    # ---- 冲突检测 ----

    def _detect_conflicts(
        self,
        batch_id: str,
        candidates: list[Mapping[str, Any]],
        report: IngestReport,
    ) -> tuple[list[Mapping[str, Any]], set[str]]:
        # point -> version -> 已知哈希（库内）
        known: dict[tuple[str, int], str] = {}
        for event in self.store.of_type("OBSERVATION_CAPTURED"):
            known[(event.aggregate_id, event.version)] = str(event.data.get("content_hash", ""))

        bad_points: set[str] = set()
        batch_hashes: dict[tuple[str, int], str] = {}
        surviving: list[Mapping[str, Any]] = []

        for envelope in candidates:
            if envelope["event_type"] != "OBSERVATION_CAPTURED":
                surviving.append(envelope)
                continue
            point = str(envelope["aggregate_id"])
            version = int(envelope["version"])
            digest = str((envelope.get("data") or {}).get("content_hash", ""))
            key = (point, version)
            prior = known.get(key, batch_hashes.get(key))
            if prior is not None:
                if digest == prior:
                    # 同一测点同一内容：离线重复/晚到采集，幂等丢弃
                    report.duplicates.append(str(envelope["event_id"]))
                    continue
                bad_points.add(point)
            batch_hashes.setdefault(key, digest)
            surviving.append(envelope)

        if not bad_points:
            return surviving, set()

        stored_tainted, _candidate_tainted = _quarantine_closure(self.store, surviving, bad_points)
        excluded: set[str] = set()
        for point in bad_points:
            # 候选中依赖该测点的事件（含争议观测本身）整批隔离，不入库
            point_candidates = sorted(
                str(e["event_id"]) for e in surviving
                if point in self._upstream_points(str(e["event_id"]), surviving)
            )
            report.quarantined_candidates[point] = point_candidates
            excluded.update(point_candidates)

            # 库内该测点上的旧观测及其全部依赖成果：追加隔离标记
            stored_ids = sorted(
                eid for eid in stored_tainted
                if point in self._upstream_points(eid, surviving)
            )
            if stored_ids and point not in self.quarantined_points:
                self._append_quarantine_marker(batch_id, point, stored_ids)
                report.quarantined[point] = stored_ids
                self.quarantined_points.add(point)
                self.quarantined_events.update(stored_ids)

        surviving = [e for e in surviving if str(e["event_id"]) not in excluded]
        return surviving, excluded

    def _upstream_points(
        self,
        event_id: str,
        candidates: list[Mapping[str, Any]],
        store: Optional[EventStore] = None,
    ) -> set[str]:
        """某事件依赖闭包中涉及的测点集合。"""
        store = store or self.store
        nodes: dict[str, Any] = {e.event_id: e for e in store}
        for envelope in candidates:
            nodes[str(envelope["event_id"])] = envelope
        points: set[str] = set()
        seen: set[str] = set()
        stack = [event_id]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            node = nodes.get(current)
            if node is None:
                continue
            node_type = node.event_type if isinstance(node, StoredEvent) else node["event_type"]
            node_aggregate = (
                node.aggregate_id if isinstance(node, StoredEvent) else node["aggregate_id"]
            )
            if node_type == "OBSERVATION_CAPTURED":
                points.add(str(node_aggregate))
            stack.extend(_dependencies_of(node))
        return points

    def _append_quarantine_marker(
        self, batch_id: str, point: str, stored_ids: list[str]
    ) -> StoredEvent:
        marker_id = f"quarantine-{point}"
        version = self.store.stream_version("quarantine_marker", marker_id) + 1
        envelope = {
            "event_id": f"{batch_id}:quarantine:{point}:{version}",
            "event_type": "QUARANTINE_MARKED",
            "aggregate_type": "quarantine_marker",
            "aggregate_id": marker_id,
            "occurred_at": _utcnow().astimezone(timezone.utc).isoformat(),
            "version": version,
            "summary": f"测点 {point} 出现异内容，隔离其观测与依赖成果",
            "data": {"point_id": point, "event_ids": stored_ids, "reason": "same_point_content_conflict"},
            "source": {"party": "system"},
            "batch_id": batch_id,
        }
        stored, _ = self.store.append(envelope)
        return stored

    # ---- 门禁 + 提交 ----

    def _structural_checks(self, envelope: Mapping[str, Any]) -> list:
        """信封之外、与业务负载形状相关的交换层检查。"""
        from .contracts import ContractIssue

        issues: list = []
        etype = str(envelope.get("event_type", ""))
        data = envelope.get("data")
        if etype in (
            "RELATION_RECORDED", "SURVEY_REVISED", "OBJECT_RECOVERED",
            "CLAIM_PROPOSED", "OBJECT_TRANSFERRED", "CLAIM_REVIEWED",
            "CLAIM_SUPERSEDED", "OBSERVATION_CAPTURED",
        ) and not isinstance(data, Mapping):
            issues.append(ContractIssue("data", "object_required", "业务负载必须是 JSON 对象"))
            return issues

        if etype == "OBSERVATION_CAPTURED" and not str(data.get("content_hash", "")).strip():
            issues.append(ContractIssue("data.content_hash", "required", "观测必须提供内容哈希"))
        if etype == "RELATION_RECORDED" and data.get("predicate") not in RELATION_PREDICATES:
            issues.append(ContractIssue("data.predicate", "unsupported_value", "空间关系谓词未登记"))
        if etype == "CLAIM_PROPOSED" and not data.get("citations"):
            issues.append(ContractIssue("data.citations", "required", "主张必须引用明确观测"))
        if etype == "CLAIM_SUPERSEDED" and data.get("mode") not in ("invalidate", "narrow"):
            issues.append(ContractIssue("data.mode", "unsupported_value", "只支持 invalidate / narrow"))
        return issues

    def _gate(self, envelope: Mapping[str, Any]) -> list[str]:
        """领域门禁：保管链唯一、会审授权、隔离依赖。返回拒绝原因。"""
        etype = envelope["event_type"]
        data = envelope.get("data") or {}
        reasons: list[str] = []

        if etype == "ALIAS_REGISTERED":
            try:
                self.crosswalk.check(
                    str(data.get("entity_type", "")),
                    str(data.get("party", "")),
                    str(data.get("local_code", "")),
                    str(data.get("canonical_id", "")),
                )
            except AliasConflict as exc:
                reasons.append(str(exc))

        elif etype == "OBJECT_RECOVERED":
            object_id = str(envelope["aggregate_id"])
            if self.ledger.is_recovered(object_id):
                reasons.append(f"对象 {object_id} 已有出土记录，保管链起点必须唯一")
            container = data.get("container_id")
            if self.ledger.is_frozen(container):
                reasons.append(f"容器 {container} 已冻结，不能接收出土对象")

        elif etype == "OBJECT_TRANSFERRED":
            reasons.extend(self.ledger.check_transfer(
                str(envelope["aggregate_id"]),
                str(data.get("stage", "")),
                str(data.get("from_party", "")),
                data.get("container_id"),
            ))

        elif etype == "CLAIM_PROPOSED":
            for citation in data.get("citations", []):
                observed = self.store.get(str(citation))
                if observed is None or observed.event_type != "OBSERVATION_CAPTURED":
                    reasons.append(f"引用 {citation} 不是已入库的明确观测")
                elif citation in self.quarantined_events:
                    reasons.append(f"引用 {citation} 所属测点已被隔离，主张不得成立")

        elif etype == "CLAIM_REVIEWED":
            reasons.extend(self.registry.check_review(
                str(envelope["aggregate_id"]),
                str(data.get("party", "")),
                str(data.get("reviewer", "")),
            ))

        elif etype == "CLAIM_SUPERSEDED":
            reasons.extend(self.registry.check_supersede(
                str(envelope["aggregate_id"]),
                str(data.get("by_claim", "")),
                str(data.get("mode", "")),
            ))

        elif etype == "RECORD_PUBLISHED":
            if envelope.get("aggregate_type") == "research_claim":
                reasons.extend(self.registry.check_publishable(str(envelope["aggregate_id"])))

        return reasons

    def _commit(
        self,
        batch_id: str,
        candidates: list[Mapping[str, Any]],
        excluded: set[str],
        report: IngestReport,
    ) -> None:
        for envelope in candidates:
            eid = str(envelope["event_id"])
            if self.store.has(eid):
                report.duplicates.append(eid)
                continue

            reasons = self._gate(envelope)
            if reasons:
                report.rejected[eid] = reasons
                continue

            stored, _ = self.store.append(envelope)
            self._apply_live(stored)
            report.accepted.append(eid)

            if stored.event_type == "SEAL_ANOMALY_REPORTED":
                frozen = self._freeze_container(stored.aggregate_id, batch_id)
                if frozen is not None:
                    report.frozen_containers[stored.aggregate_id] = frozen.event_id

    def _apply_live(self, event: StoredEvent) -> None:
        self.crosswalk.apply(event)
        self.ledger.apply(event)
        self.registry.apply(event)
        if event.event_type == "QUARANTINE_MARKED":
            self.quarantined_points.add(str(event.data.get("point_id", "")))
            self.quarantined_events.update(str(x) for x in event.data.get("event_ids", []))

    def _freeze_container(self, container_id: str, batch_id: str) -> Optional[StoredEvent]:
        if self.ledger.is_frozen(container_id):
            return None
        version = self.store.stream_version("custody_container", container_id) + 1
        envelope = {
            "event_id": f"{batch_id}:freeze:{container_id}:{version}",
            "event_type": "CONTAINER_FROZEN",
            "aggregate_type": "custody_container",
            "aggregate_id": container_id,
            "occurred_at": _utcnow().astimezone(timezone.utc).isoformat(),
            "version": version,
            "summary": f"封签异常，立即冻结容器 {container_id}",
            "data": {"reason": "seal_anomaly"},
            "source": {"party": "system"},
            "batch_id": batch_id,
        }
        stored, _ = self.store.append(envelope)
        self._apply_live(stored)
        return stored
