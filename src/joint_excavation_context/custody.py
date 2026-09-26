"""保管链与封签冻结。

出土、清理、送检、归还各阶段之间，任一时刻每件出土对象只有
唯一保管责任方：交接事件必须衔接当前责任方并按阶段顺序推进。
封签异常一经记录立即冻结对应容器，冻结容器及其内对象的交接
一律拒绝，直至有权限人员解除。
"""

from __future__ import annotations

from typing import Any

from .store import EventStore

# 保管阶段：出土 -> 清理 -> 送检 -> 归还
STAGES = ("excavated", "conservation", "lab", "returned")


class CustodyError(ValueError):
    """保管链违规或容器已冻结。"""


class CustodyService:
    def __init__(self, store: EventStore) -> None:
        self._store = store

    # ---- 查询 ----

    def _transfers(self, object_id: str) -> list[dict[str, Any]]:
        return self._store.events(
            aggregate_type="custody_transfer",
            aggregate_id=f"custody-{object_id}",
            event_type="OBJECT_TRANSFERRED",
        )

    def current_custodian(self, object_id: str) -> dict[str, Any] | None:
        events = self._transfers(object_id)
        if not events:
            return None
        last = events[-1]
        return {
            "custodian": last["to_party"],
            "stage": last["stage"],
            "container_id": last.get("container_id"),
        }

    def frozen_containers(self) -> dict[str, str]:
        """从事件流推导当前冻结的容器及原因。"""
        frozen: dict[str, str] = {}
        for event in self._store.events(aggregate_type="custody_transfer"):
            aggregate_id = event["aggregate_id"]
            if not aggregate_id.startswith("container-"):
                continue
            container_id = aggregate_id.removeprefix("container-")
            if event["event_type"] == "CONTAINER_FROZEN":
                frozen[container_id] = event.get("reason", "")
            elif event["event_type"] == "CONTAINER_RELEASED":
                frozen.pop(container_id, None)
        return frozen

    # ---- 操作 ----

    def transfer(
        self,
        *,
        event_id: str,
        object_id: str,
        from_party: str | None,
        to_party: str,
        stage: str,
        container_id: str,
        occurred_at: str,
        custody_location: str | None = None,
        summary: str | None = None,
    ) -> None:
        if stage not in STAGES:
            raise CustodyError(f"未知保管阶段：{stage}")
        frozen = self.frozen_containers()
        if container_id in frozen:
            raise CustodyError(f"容器 {container_id} 已冻结：{frozen[container_id]}")
        current = self.current_custodian(object_id)
        if current is None:
            if stage != "excavated" or from_party is not None:
                raise CustodyError("首次交接必须是出土登记")
        else:
            if current["container_id"] in frozen:
                raise CustodyError(f"对象所在容器 {current['container_id']} 已冻结")
            if from_party != current["custodian"]:
                raise CustodyError("交接发起方与当前保管责任方不一致")
            if STAGES.index(stage) != STAGES.index(current["stage"]) + 1:
                raise CustodyError("保管阶段必须按出土、清理、送检、归还顺序推进")
        event: dict[str, Any] = {
            "event_id": event_id,
            "event_type": "OBJECT_TRANSFERRED",
            "aggregate_type": "custody_transfer",
            "aggregate_id": f"custody-{object_id}",
            "occurred_at": occurred_at,
            "version": len(self._transfers(object_id)) + 1,
            "summary": summary or f"{object_id} 由 {from_party or '发掘现场'} 移交 {to_party}",
            "object_id": object_id,
            "from_party": from_party,
            "to_party": to_party,
            "stage": stage,
            "container_id": container_id,
        }
        if custody_location is not None:
            event["custody_location"] = custody_location
        issues = self._store.append(event)
        if issues:
            raise CustodyError(f"事件未通过契约校验：{[i.code for i in issues]}")

    def record_seal_anomaly(self, *, event_id: str, container_id: str,
                            description: str, occurred_at: str) -> None:
        """封签异常：记录异常并立即冻结对应容器。"""
        base = {
            "aggregate_type": "custody_transfer",
            "aggregate_id": f"container-{container_id}",
            "occurred_at": occurred_at,
        }
        self._store.append({**base, "event_id": event_id,
                            "event_type": "SEAL_ANOMALY_RECORDED",
                            "version": 1, "summary": description})
        self._store.append({**base, "event_id": f"{event_id}-frozen",
                            "event_type": "CONTAINER_FROZEN",
                            "version": 2, "summary": f"容器 {container_id} 已冻结",
                            "reason": description})

    def release_container(self, *, event_id: str, container_id: str,
                          authorizer: str, occurred_at: str) -> None:
        if container_id not in self.frozen_containers():
            raise CustodyError(f"容器 {container_id} 未处于冻结状态")
        self._store.append({
            "event_id": event_id,
            "event_type": "CONTAINER_RELEASED",
            "aggregate_type": "custody_transfer",
            "aggregate_id": f"container-{container_id}",
            "occurred_at": occurred_at,
            "version": 3,
            "summary": f"容器 {container_id} 由 {authorizer} 解除冻结",
            "authorizer": authorizer,
        })
