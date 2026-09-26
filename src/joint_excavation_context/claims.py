"""研究主张生命周期：提交、双方会审、发布、失效与范围收缩。

双圣湖布局、建筑年代、礼仪功能等可争论解释，提交时必须引用
已入库的明确观测，发布前须经双方各自有权限的会审人批准。
新发现可使旧主张失效或缩小适用范围，但只设置有效截止
（valid_to），不覆盖当时的记录；任一历史时点都能取回当时
生效的主张版本。发布通过可恢复作业执行，中断重跑只补齐
未完成的步骤。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .jobs import InMemoryCheckpointStore, CheckpointStore, Job, Step
from .store import EventStore, parse_instant

# 可争论解释类别：双圣湖布局、建筑年代、礼仪功能
DISPUTABLE_KINDS = frozenset({"twin_lake_layout", "building_dating", "ritual_function"})


class ClaimError(ValueError):
    """主张生命周期中的违规操作。"""


@dataclass
class ClaimRecord:
    claim_id: str
    version: int
    kind: str
    text: str
    scope: str
    citations: tuple[str, ...]
    author: str
    valid_from: str
    status: str = "submitted"
    approvals: dict[str, str] = field(default_factory=dict)  # 一方 -> 会审人
    valid_to: str | None = None


class ClaimService:
    def __init__(
        self,
        store: EventStore,
        reviewers: Mapping[str, set[str]],
        checkpoints: CheckpointStore | None = None,
    ) -> None:
        self._store = store
        self._reviewers = {party: set(ids) for party, ids in reviewers.items()}
        self._checkpoints = checkpoints or InMemoryCheckpointStore()
        self._records: dict[str, list[ClaimRecord]] = {}

    # ---- 内部工具 ----

    def _current(self, claim_id: str) -> ClaimRecord:
        versions = self._records.get(claim_id)
        if not versions:
            raise ClaimError(f"主张不存在：{claim_id}")
        record = versions[-1]
        if record.valid_to is not None:
            raise ClaimError(f"主张 {claim_id} 第 {record.version} 版已失效，不能继续操作")
        return record

    def _emit(self, event_type: str, claim_id: str, occurred_at: str, summary: str, **extra: Any) -> None:
        version = len(self._store.events(aggregate_id=claim_id)) + 1
        event = {
            "event_id": f"{claim_id}-{event_type.lower().replace('_', '-')}-v{version}",
            "event_type": event_type,
            "aggregate_type": "research_claim",
            "aggregate_id": claim_id,
            "occurred_at": occurred_at,
            "version": version,
            "summary": summary,
            **extra,
        }
        issues = self._store.append(event)
        if issues:
            raise ClaimError(f"事件未通过契约校验：{[i.code for i in issues]}")

    # ---- 生命周期 ----

    def submit(
        self,
        claim_id: str,
        kind: str,
        text: str,
        scope: str,
        citations: tuple[str, ...] | list[str],
        author: str,
        occurred_at: str,
    ) -> ClaimRecord:
        if claim_id in self._records:
            raise ClaimError("主张已存在，应通过失效或缩小范围演进")
        citations = tuple(citations)
        if kind in DISPUTABLE_KINDS and not citations:
            raise ClaimError("可争论解释必须引用明确观测")
        for event_id in citations:
            event = self._store.get(event_id)
            if event is None or event["event_type"] != "OBSERVATION_CAPTURED":
                raise ClaimError(f"引用的观测不存在：{event_id}")
        record = ClaimRecord(claim_id, 1, kind, text, scope, citations, author, valid_from=occurred_at)
        self._records[claim_id] = [record]
        self._emit("CLAIM_SUBMITTED", claim_id, occurred_at, f"提交主张：{text}",
                   kind=kind, scope=scope, citations=list(citations), author=author)
        return record

    def review(self, claim_id: str, reviewer: str, party: str, approve: bool, occurred_at: str) -> None:
        record = self._current(claim_id)
        if reviewer not in self._reviewers.get(party, set()):
            raise ClaimError(f"{reviewer} 无 {party} 方会审权限")
        if record.status != "submitted":
            raise ClaimError("仅待审状态的主张可以会审")
        if approve:
            record.approvals[party] = reviewer
        else:
            record.approvals.pop(party, None)
        self._emit("CLAIM_REVIEWED", claim_id, occurred_at,
                   f"{party} 方会审人 {reviewer} {'同意' if approve else '撤回'}",
                   reviewer=reviewer, party=party, approve=approve)

    def publish(self, claim_id: str, occurred_at: str) -> None:
        record = self._current(claim_id)
        if record.kind in DISPUTABLE_KINDS:
            missing = [party for party in self._reviewers if party not in record.approvals]
            if missing:
                raise ClaimError(f"可争论解释须经双方会审，缺少：{'、'.join(missing)}")

        def emit_publication() -> None:
            self._store.append({
                "event_id": f"{claim_id}-v{record.version}-published",
                "event_type": "RECORD_PUBLISHED",
                "aggregate_type": "research_claim",
                "aggregate_id": claim_id,
                "occurred_at": occurred_at,
                "version": len(self._store.events(aggregate_id=claim_id)) + 1,
                "summary": f"发布主张 {claim_id} 第 {record.version} 版",
            })

        job = Job(
            f"publish-{claim_id}-v{record.version}",
            [Step("emit_record_published", emit_publication)],
            self._checkpoints,
        )
        job.run()
        record.status = "published"

    def supersede(self, claim_id: str, occurred_at: str, reason: str) -> None:
        """新发现使旧主张失效：只设有效截止，保留当时记录。"""
        record = self._current(claim_id)
        record.valid_to = occurred_at
        record.status = "superseded"
        self._emit("CLAIM_SUPERSEDED", claim_id, occurred_at, f"主张失效：{reason}", reason=reason)

    def narrow(self, claim_id: str, new_scope: str, occurred_at: str) -> ClaimRecord:
        """缩小适用范围：旧版本截止，生成同链路的下一版本。"""
        record = self._current(claim_id)
        record.valid_to = occurred_at
        record.status = "narrowed"
        successor = ClaimRecord(
            claim_id, record.version + 1, record.kind, record.text,
            new_scope, record.citations, record.author, valid_from=occurred_at,
        )
        self._records[claim_id].append(successor)
        self._emit("CLAIM_SCOPE_NARROWED", claim_id, occurred_at,
                   f"适用范围由「{record.scope}」缩小为「{new_scope}」",
                   old_scope=record.scope, new_scope=new_scope)
        return successor

    # ---- 查询 ----

    def current(self, claim_id: str) -> ClaimRecord:
        return self._current(claim_id)

    def claim_at(self, claim_id: str, as_of: str) -> ClaimRecord | None:
        """取回某一历史时点生效的主张版本。"""
        instant = parse_instant(as_of)
        for record in self._records.get(claim_id, []):
            if parse_instant(record.valid_from) > instant:
                continue
            if record.valid_to is not None and instant >= parse_instant(record.valid_to):
                continue
            return record
        return None
