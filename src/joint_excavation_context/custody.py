"""保管链投影：出土 -> 清理 -> 送检 -> 归还的唯一保管责任。

规则：
- 对象出土（OBJECT_RECOVERED）确立首位保管人，且全库唯一出土记录；
- 每次交接（OBJECT_TRANSFERRED）的交出方必须是当前保管人，阶段只许前进；
- 容器封签异常（SEAL_ANOMALY_REPORTED）后服务立即追加 CONTAINER_FROZEN，
  冻结容器参与的一切交接在门禁处被拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .events import CUSTODY_STAGES
from .store import EventStore, StoredEvent


@dataclass
class CustodyState:
    object_id: str
    custodian: str = ""
    stage: str = ""
    container_id: Optional[str] = None
    history: list[str] = field(default_factory=list)  # event_id 链


class CustodyLedger:
    """保管链投影；从事件存储重放构建，供入库门禁做一致性检查。"""

    def __init__(self) -> None:
        self._objects: dict[str, CustodyState] = {}
        self._frozen_containers: dict[str, str] = {}  # container_id -> 冻结事件 id

    def apply(self, event: StoredEvent) -> None:
        data = event.data
        if event.event_type == "OBJECT_RECOVERED":
            state = self._objects.setdefault(event.aggregate_id, CustodyState(event.aggregate_id))
            state.custodian = str(data.get("custodian", ""))
            state.stage = "excavated"
            container = data.get("container_id")
            state.container_id = str(container) if container else None
            state.history.append(event.event_id)
        elif event.event_type == "OBJECT_TRANSFERRED":
            state = self._objects.setdefault(event.aggregate_id, CustodyState(event.aggregate_id))
            state.custodian = str(data.get("to_party", ""))
            state.stage = str(data.get("stage", state.stage))
            container = data.get("container_id")
            if container:
                state.container_id = str(container)
            state.history.append(event.event_id)
        elif event.event_type == "CONTAINER_FROZEN":
            self._frozen_containers[event.aggregate_id] = event.event_id

    # ---- 门禁查询 ----

    def state_of(self, object_id: str) -> Optional[CustodyState]:
        return self._objects.get(object_id)

    def is_recovered(self, object_id: str) -> bool:
        state = self._objects.get(object_id)
        return state is not None and bool(state.history)

    def is_frozen(self, container_id: Optional[str]) -> bool:
        return bool(container_id) and container_id in self._frozen_containers

    def frozen_by(self, container_id: str) -> Optional[str]:
        return self._frozen_containers.get(container_id)

    def check_transfer(
        self,
        object_id: str,
        stage: str,
        from_party: str,
        container_id: Optional[str],
    ) -> list[str]:
        """返回拒绝原因列表；空列表表示可以交接。"""
        reasons: list[str] = []
        state = self._objects.get(object_id)
        if state is None or not state.history:
            reasons.append(f"对象 {object_id} 尚未登记出土，不能交接")
            return reasons
        if from_party != state.custodian:
            reasons.append(
                f"交出方 {from_party} 不是当前保管人 {state.custodian}，保管责任必须唯一"
            )
        if stage in CUSTODY_STAGES and state.stage in CUSTODY_STAGES:
            if CUSTODY_STAGES.index(stage) <= CUSTODY_STAGES.index(state.stage):
                reasons.append(f"保管阶段不能从 {state.stage} 回退或重复到 {stage}")
        for container in {container_id, state.container_id} - {None}:
            if self.is_frozen(container):
                reasons.append(
                    f"容器 {container} 已因封签异常冻结（{self.frozen_by(container)}），禁止交接"
                )
        return reasons


def build_ledger(store: EventStore) -> CustodyLedger:
    ledger = CustodyLedger()
    for event in store:
        ledger.apply(event)
    return ledger
