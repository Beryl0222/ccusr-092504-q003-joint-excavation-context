from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from joint_excavation_context.jobs import (
    InMemoryCheckpointStore,
    Job,
    JsonFileCheckpointStore,
    Step,
)


class JobTests(unittest.TestCase):
    def test_interrupted_job_resumes_only_pending_steps(self) -> None:
        calls: list[str] = []
        checkpoints = InMemoryCheckpointStore()

        def make_job() -> Job:
            def step(name: str):
                def action() -> None:
                    calls.append(name)
                    if name == "b" and calls.count("b") == 1:
                        raise RuntimeError("模拟中断")
                return Step(name, action)
            return Job("import-1", [step("a"), step("b"), step("c")], checkpoints)

        with self.assertRaises(RuntimeError):
            make_job().run()
        self.assertEqual(["a", "b"], calls)

        make_job().run()  # 重跑只补齐 b、c
        self.assertEqual(["a", "b", "b", "c"], calls)
        self.assertEqual([], make_job().pending_steps())

    def test_json_file_checkpoints_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "checkpoints.json"
            JsonFileCheckpointStore(path).mark("job-1", "step-a")
            reloaded = JsonFileCheckpointStore(path)
            self.assertEqual({"step-a"}, reloaded.completed("job-1"))


if __name__ == "__main__":
    unittest.main()
