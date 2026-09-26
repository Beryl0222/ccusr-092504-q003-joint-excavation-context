"""可恢复作业：步骤完成后记录检查点，中断重跑只补齐未完成步骤。

批量导入与会审发布都以步骤序列执行；每个步骤完成后立即写入
检查点，作业中断后再次 run() 时已完成步骤直接跳过。步骤动作
自身也应幂等（例如使用确定性 event_id），双重保障不重复入库。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol


@dataclass(frozen=True)
class Step:
    name: str
    action: Callable[[], None]


class CheckpointStore(Protocol):
    def completed(self, job_id: str) -> set[str]: ...
    def mark(self, job_id: str, step_name: str) -> None: ...


class InMemoryCheckpointStore:
    def __init__(self) -> None:
        self._done: dict[str, list[str]] = {}

    def completed(self, job_id: str) -> set[str]:
        return set(self._done.get(job_id, ()))

    def mark(self, job_id: str, step_name: str) -> None:
        self._done.setdefault(job_id, []).append(step_name)


class JsonFileCheckpointStore:
    """以 JSON 文件持久化检查点，进程重启后仍可续跑。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def _load(self) -> dict[str, list[str]]:
        if not self._path.exists():
            return {}
        return json.loads(self._path.read_text(encoding="utf-8"))

    def completed(self, job_id: str) -> set[str]:
        return set(self._load().get(job_id, ()))

    def mark(self, job_id: str, step_name: str) -> None:
        data = self._load()
        data.setdefault(job_id, []).append(step_name)
        self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class Job:
    def __init__(self, job_id: str, steps: list[Step], checkpoints: CheckpointStore) -> None:
        self.job_id = job_id
        self._steps = steps
        self._checkpoints = checkpoints

    def pending_steps(self) -> list[str]:
        done = self._checkpoints.completed(self.job_id)
        return [step.name for step in self._steps if step.name not in done]

    def run(self) -> None:
        """执行未完成步骤；任一步骤抛错即中断，重跑时从断点继续。"""
        done = self._checkpoints.completed(self.job_id)
        for step in self._steps:
            if step.name in done:
                continue
            step.action()
            self._checkpoints.mark(self.job_id, step.name)
