from __future__ import annotations

import unittest

from helpers import make_event, make_store

from joint_excavation_context.ingest import (
    ACCEPTED,
    DUPLICATE,
    QUARANTINED,
    REJECTED,
    IngestionService,
)

T1 = "2026-09-20T08:00:00+08:00"
T2 = "2026-09-21T08:00:00+08:00"
T3 = "2026-09-22T08:00:00+08:00"


def observation(event_id: str, point: str, content: str, occurred_at: str) -> dict:
    return make_event(
        event_id, "OBSERVATION_CAPTURED", "survey_revision", f"rev-{event_id}",
        occurred_at, survey_point=point, content_hash=content,
    )


class IngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = IngestionService(make_store())

    def test_duplicate_event_id_is_idempotent(self) -> None:
        event = observation("e1", "P1", "hash-a", T1)
        self.assertEqual(ACCEPTED, self.service.ingest(event).status)
        result = self.service.ingest(dict(event))
        self.assertEqual(DUPLICATE, result.status)
        self.assertEqual(1, len(self.service.store.events()))

    def test_late_offline_event_is_accepted(self) -> None:
        self.service.ingest(observation("e2", "P2", "hash-b", T3))
        late = observation("e1", "P1", "hash-a", T1)  # 发生时间更早但晚到
        self.assertEqual(ACCEPTED, self.service.ingest(late).status)

    def test_invalid_event_is_rejected(self) -> None:
        event = make_event("e1", "OBSERVATION_CAPTURED", "survey_revision", "r1", T1, version=0)
        self.assertEqual(REJECTED, self.service.ingest(event).status)

    def test_conflicting_point_quarantines_only_dependents(self) -> None:
        self.service.ingest(observation("e1", "P1", "hash-a", T1))
        dependent = make_event(
            "e2", "RECORD_PUBLISHED", "survey_revision", "rev-map-7", T2,
            source_points=["P1"],
        )
        self.service.ingest(dependent)

        conflict = self.service.ingest(observation("e3", "P1", "hash-b", T3))
        self.assertEqual(QUARANTINED, conflict.status)
        self.assertIn("rev-map-7", conflict.quarantined_aggregates)

        # 其他测点、其他区域继续入库
        other = observation("e4", "P9", "hash-z", T3)
        self.assertEqual(ACCEPTED, self.service.ingest(other).status)

        # 被隔离聚合的事件暂缓入库，解除后恢复
        follow_up = make_event("e5", "RECORD_PUBLISHED", "survey_revision", "rev-map-7", T3, version=2)
        self.assertEqual(QUARANTINED, self.service.ingest(follow_up).status)
        self.service.release("rev-map-7")
        self.assertEqual(ACCEPTED, self.service.ingest(follow_up).status)

    def test_same_content_same_point_is_not_conflict(self) -> None:
        self.service.ingest(observation("e1", "P1", "hash-a", T1))
        again = observation("e2", "P1", "hash-a", T2)
        self.assertEqual(ACCEPTED, self.service.ingest(again).status)


if __name__ == "__main__":
    unittest.main()
