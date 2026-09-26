"""主张会审发布流程：可中断、可恢复。

步骤：核验证据 -> 核验会审 -> 发布记录 -> 应用取代 -> 刷新公开索引。
每步完成后落 PROCESS_STEP_RECORDED；中断后重跑只执行未完成步骤。
发布本身按确定性 event_id 幂等，重复执行不会产生第二条发布记录。

取代指令来自新主张提议时声明的 supersedes 列表；只有新主张通过会审
并进入发布流程后，才对旧主张追加 CLAIM_SUPERSEDED——旧主张的原文与
会审记录保持原样，只是状态被标记为失效或缩限。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from .ingest import ArchiveService
from .store import StoredEvent

PUBLISH_STEPS = (
    "verify_evidence",
    "verify_review",
    "publish_record",
    "apply_supersessions",
    "refresh_public_index",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class PublishReport:
    claim_id: str
    status: str = "pending"  # published | awaiting_review | failed
    steps_run: list[str] = field(default_factory=list)
    steps_skipped: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    superseded: list[str] = field(default_factory=list)


class ClaimPublicationProcess:
    """单条主张的会审发布流程；process_id 确定，可安全重入。"""

    def __init__(self, service: ArchiveService, claim_id: str) -> None:
        self.service = service
        self.claim_id = claim_id
        self.process_id = f"publish-{claim_id}"

    def run(self) -> PublishReport:
        service = self.service
        report = PublishReport(claim_id=self.claim_id)
        done = service.completed_steps(self.process_id)

        def step_finished(name: str) -> bool:
            """已完成 -> 计入跳过；否则执行方在成功后自行计入 steps_run。"""
            if name in done:
                report.steps_skipped.append(name)
                return True
            return False

        state = service.registry.state_of(self.claim_id)
        if state is None:
            report.status = "failed"
            report.problems.append(f"主张 {self.claim_id} 尚未提议")
            return report

        # 1. 核验证据：引用必须是已入库且未被隔离的明确观测
        if not step_finished("verify_evidence"):
            problems = []
            for citation in state.citations:
                observed = service.store.get(citation)
                if observed is None or observed.event_type != "OBSERVATION_CAPTURED":
                    problems.append(f"引用 {citation} 不是已入库的明确观测")
                elif citation in service.quarantined_events:
                    problems.append(f"引用 {citation} 所属测点已被隔离")
            if problems:
                report.status = "failed"
                report.problems.extend(problems)
                return report
            report.steps_run.append("verify_evidence")
            service.record_step(self.process_id, "verify_evidence",
                                {"claim_id": self.claim_id, "citations": len(state.citations)})

        # 2. 核验会审：可争论主题须双方授权会审通过
        if not step_finished("verify_review"):
            problems = service.registry.check_publishable(self.claim_id)
            if problems:
                report.status = "awaiting_review"
                report.problems.extend(problems)
                return report  # 不记步骤，待会审补齐后重跑
            report.steps_run.append("verify_review")
            service.record_step(self.process_id, "verify_review",
                                {"claim_id": self.claim_id, "topic": state.topic})

        # 3. 发布记录（幂等）
        if not step_finished("publish_record"):
            version = service.store.stream_version("research_claim", self.claim_id) + 1
            envelope = {
                "event_id": f"{self.process_id}:record",
                "event_type": "RECORD_PUBLISHED",
                "aggregate_type": "research_claim",
                "aggregate_id": self.claim_id,
                "occurred_at": _now(),
                "version": version,
                "summary": f"主张 {self.claim_id} 经会审发布",
                "data": {},
                "source": {"party": "system"},
            }
            stored, _ = service.store.append(envelope)
            service._apply_live(stored)
            report.steps_run.append("publish_record")
            service.record_step(self.process_id, "publish_record",
                                {"claim_id": self.claim_id})

        # 4. 应用取代：新主张生效后，旧主张失效或缩限（只追加，不改写）
        if not step_finished("apply_supersessions"):
            proposed = self._proposal()
            directives = (proposed.data.get("supersedes") if proposed else None) or []
            for directive in directives:
                old_id = str(directive.get("claim_id", ""))
                mode = str(directive.get("mode", "invalidate"))
                if not old_id:
                    continue
                old_state = service.registry.state_of(old_id)
                if old_state is None or old_state.superseded_by:
                    continue
                version = service.store.stream_version("research_claim", old_id) + 1
                envelope = {
                    "event_id": f"{self.process_id}:supersede:{old_id}",
                    "event_type": "CLAIM_SUPERSEDED",
                    "aggregate_type": "research_claim",
                    "aggregate_id": old_id,
                    "occurred_at": _now(),
                    "version": version,
                    "summary": f"主张 {old_id} 被 {self.claim_id} "
                               f"{'失效' if mode == 'invalidate' else '缩小适用范围'}",
                    "data": {
                        "by_claim": self.claim_id,
                        "mode": mode,
                        **({"narrowed_scope": dict(directive["narrowed_scope"])}
                           if isinstance(directive.get("narrowed_scope"), Mapping) else {}),
                    },
                    "source": {"party": "system"},
                }
                stored, _ = service.store.append(envelope)
                service._apply_live(stored)
                report.superseded.append(old_id)
            report.steps_run.append("apply_supersessions")
            service.record_step(self.process_id, "apply_supersessions",
                                {"claim_id": self.claim_id, "superseded": len(directives)})

        # 5. 刷新公开索引（公开视图由投影即时派生，此处落检查点）
        if not step_finished("refresh_public_index"):
            report.steps_run.append("refresh_public_index")
            service.record_step(self.process_id, "refresh_public_index",
                                {"claim_id": self.claim_id})

        report.status = "published"
        return report

    def _proposal(self) -> Optional[StoredEvent]:
        for event in self.service.store.stream("research_claim", self.claim_id):
            if event.event_type == "CLAIM_PROPOSED":
                return event
        return None
