"""双方编号别名登记：把各自探方号、测绘版本、文物暂存编号映射到统一聚合。

双方沿用各自编号体系，交换时以 (编号体系, 外部编号) 解析到
平台统一的聚合标识；同一外部编号映射到不同聚合即报冲突，
避免旧探方记录被静默并错。
"""

from __future__ import annotations


class AliasConflictError(ValueError):
    """同一外部编号被映射到不同聚合时抛出。"""


class AliasRegistry:
    """(编号体系, 外部编号) -> 聚合标识 的双向登记。"""

    def __init__(self) -> None:
        self._forward: dict[tuple[str, str], str] = {}
        self._reverse: dict[str, dict[str, str]] = {}

    def register(self, system: str, external_id: str, aggregate_id: str) -> None:
        """登记别名；重复登记同一映射幂等，映射到不同聚合则冲突。"""
        key = (system, external_id)
        existing = self._forward.get(key)
        if existing is not None and existing != aggregate_id:
            raise AliasConflictError(
                f"{system}:{external_id} 已映射到 {existing}，不能再映射到 {aggregate_id}"
            )
        self._forward[key] = aggregate_id
        self._reverse.setdefault(aggregate_id, {})[system] = external_id

    def resolve(self, system: str, external_id: str) -> str | None:
        """把某一方的外部编号解析为统一聚合标识，未登记返回 None。"""
        return self._forward.get((system, external_id))

    def aliases_of(self, aggregate_id: str) -> dict[str, str]:
        """返回该聚合在各编号体系下的外部编号。"""
        return dict(self._reverse.get(aggregate_id, {}))
