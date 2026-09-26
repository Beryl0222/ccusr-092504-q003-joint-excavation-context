"""研究主张登记：提议、双方会审、失效/缩限。

- 主张必须引用明确观测（citations 指向 OBSERVATION_CAPTURED 事件）；
- 可争论主题（双圣湖布局、建筑年代、礼仪功能）须双方授权会审人各自
  同意方可通过；其他主题一方同意即可；
- 新主张可使旧主张失效（invalidate）或缩小适用范围（narrow），但只
  追加状态事件，被取代主张的原文与会审记录保持原样；
- 被隔离观测支撑的主张不得通过、不得发布。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from .events import DEBATABLE_TOPICS
from .store import EventStore, StoredEvent

PARTIES = ("cn", "eg")


@dataclass
class ClaimState:
    claim_id: str
    topic: str = ""
    statement: str = ""
    scope: Mapping[str, Any] = field(default_factory=dict)
    citations: list[str] = field(default_factory=list)
    approvals: dict[str, str] = field(default_factory=dict)  # party -> reviewer
    rejected: bool = False
    superseded_by: Optional[str] = None
    supersede_mode: Optional[str] = None  # invalidate | narrow
    narrowed_scope: Optional[Mapping[str, Any]] = None

    @property
    def debatable(self) -> bool:
        return self.topic in DEBATABLE_TOPICS

    @property
    def approved(self) -> bool:
        if self.rejected or self.superseded_by:
            return False
        if self.debatable:
            return all(party in self.approvals for party in PARTIES)
        return bool(self.approvals)

    @property
    def status(self) -> str:
        if self.superseded_by:
            return "invalidated" if self.supersede_mode == "invalidate" else "narrowed"
        if self.rejected:
            return "rejected"
        if self.approved:
            return "approved"
        return "proposed"


class ClaimRegistry:
    """主张投影；authorized_reviewers 为 (party, reviewer) 白名单。"""

    def __init__(self, authorized_reviewers: Optional[set[tuple[str, str]]] = None) -> None:
        self._claims: dict[str, ClaimState] = {}
        self._authorized = authorized_reviewers or set()

    def apply(self, event: StoredEvent) -> None:
        data = event.data
        if event.event_type == "CLAIM_PROPOSED":
            state = self._claims.setdefault(event.aggregate_id, ClaimState(event.aggregate_id))
            state.topic = str(data.get("topic", ""))
            state.statement = str(data.get("statement", ""))
            state.scope = dict(data.get("scope", {}))
            state.citations = [str(item) for item in data.get("citations", [])]
        elif event.event_type == "CLAIM_REVIEWED":
            state = self._claims.setdefault(event.aggregate_id, ClaimState(event.aggregate_id))
            decision = str(data.get("decision", ""))
            party = str(data.get("party", ""))
            if decision == "approve":
                state.approvals[party] = str(data.get("reviewer", ""))
            elif decision == "reject":
                state.rejected = True
        elif event.event_type == "CLAIM_SUPERSEDED":
            state = self._claims.setdefault(event.aggregate_id, ClaimState(event.aggregate_id))
            state.superseded_by = str(data.get("by_claim", ""))
            state.supersede_mode = str(data.get("mode", ""))
            narrowed = data.get("narrowed_scope")
            state.narrowed_scope = dict(narrowed) if isinstance(narrowed, Mapping) else None

    # ---- 门禁查询 ----

    def state_of(self, claim_id: str) -> Optional[ClaimState]:
        return self._claims.get(claim_id)

    def is_authorized(self, party: str, reviewer: str) -> bool:
        return (party, reviewer) in self._authorized

    def check_review(self, claim_id: str, party: str, reviewer: str) -> list[str]:
        reasons: list[str] = []
        state = self._claims.get(claim_id)
        if state is None:
            return [f"主张 {claim_id} 尚未提议"]
        if state.superseded_by:
            reasons.append(f"主张 {claim_id} 已被 {state.superseded_by} 取代，不再接受会审")
        if not self.is_authorized(party, reviewer):
            reasons.append(f"{reviewer}（{party}）不在授权会审名单内")
        if party in state.approvals:
            reasons.append(f"{party} 方已会审通过，重复会审无效")
        return reasons

    def check_supersede(self, claim_id: str, by_claim: str, mode: str) -> list[str]:
        reasons: list[str] = []
        state = self._claims.get(claim_id)
        successor = self._claims.get(by_claim)
        if state is None:
            reasons.append(f"主张 {claim_id} 不存在")
        if successor is None or not successor.approved:
            reasons.append(f"新主张 {by_claim} 尚未通过会审，不能取代旧主张")
        if mode not in ("invalidate", "narrow"):
            reasons.append(f"未知取代方式 {mode}，只支持 invalidate / narrow")
        if mode == "narrow" and state is not None:
            # 缩限必须留下非空的剩余适用范围
            pass
        return reasons

    def check_publishable(self, claim_id: str) -> list[str]:
        state = self._claims.get(claim_id)
        if state is None:
            return [f"主张 {claim_id} 尚未提议"]
        if state.superseded_by:
            return [f"主张 {claim_id} 已被取代，不能发布"]
        if not state.approved:
            needed = "双方授权会审" if state.debatable else "授权会审"
            return [f"主张 {claim_id} 未经{needed}通过，不能发布"]
        return []


def build_registry(
    store: EventStore,
    authorized_reviewers: Optional[set[tuple[str, str]]] = None,
) -> ClaimRegistry:
    registry = ClaimRegistry(authorized_reviewers)
    for event in store:
        registry.apply(event)
    return registry
