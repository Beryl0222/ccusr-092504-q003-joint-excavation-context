from __future__ import annotations

import unittest

from helpers import make_event, make_store

from joint_excavation_context.claims import ClaimError, ClaimService

T1 = "2026-09-20T08:00:00+08:00"
T2 = "2026-09-21T08:00:00+08:00"
T3 = "2026-09-22T08:00:00+08:00"
T4 = "2026-09-23T08:00:00+08:00"

REVIEWERS = {"CN": {"cn-reviewer"}, "EG": {"eg-reviewer"}}


class ClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = make_store()
        self.store.append(make_event(
            "obs-1", "OBSERVATION_CAPTURED", "survey_revision", "rev-1", T1,
            survey_point="P1", content_hash="hash-a",
        ))
        self.claims = ClaimService(self.store, REVIEWERS)

    def submit_disputable(self) -> None:
        self.claims.submit(
            "claim-1", "twin_lake_layout", "围墙内为双圣湖布局",
            scope="整个调查区", citations=("obs-1",), author="cn-reviewer", occurred_at=T2,
        )

    def test_disputable_claim_requires_observation_citation(self) -> None:
        with self.assertRaises(ClaimError):
            self.claims.submit(
                "claim-1", "ritual_function", "该殿为礼仪建筑",
                scope="全区", citations=(), author="a", occurred_at=T2,
            )

    def test_citation_must_reference_existing_observation(self) -> None:
        with self.assertRaises(ClaimError):
            self.claims.submit(
                "claim-1", "building_dating", "建筑年代为晚期",
                scope="全区", citations=("obs-404",), author="a", occurred_at=T2,
            )

    def test_review_requires_authorized_reviewer(self) -> None:
        self.submit_disputable()
        with self.assertRaises(ClaimError):
            self.claims.review("claim-1", "outsider", "CN", True, T3)

    def test_disputable_publish_requires_both_parties(self) -> None:
        self.submit_disputable()
        self.claims.review("claim-1", "cn-reviewer", "CN", True, T3)
        with self.assertRaises(ClaimError):
            self.claims.publish("claim-1", T4)
        self.claims.review("claim-1", "eg-reviewer", "EG", True, T3)
        self.claims.publish("claim-1", T4)
        published = self.store.events(event_type="RECORD_PUBLISHED", aggregate_id="claim-1")
        self.assertEqual(1, len(published))

    def test_publish_is_resumable_and_idempotent(self) -> None:
        self.submit_disputable()
        self.claims.review("claim-1", "cn-reviewer", "CN", True, T3)
        self.claims.review("claim-1", "eg-reviewer", "EG", True, T3)
        self.claims.publish("claim-1", T4)
        self.claims.publish("claim-1", T4)  # 中断重跑不重复发布
        published = self.store.events(event_type="RECORD_PUBLISHED", aggregate_id="claim-1")
        self.assertEqual(1, len(published))

    def test_supersede_keeps_historical_record(self) -> None:
        self.submit_disputable()
        self.claims.supersede("claim-1", T4, reason="南部圣湖确认，旧布局解释失效")
        # 当时生效的记录仍可取回，内容不被失效改写
        before = self.claims.claim_at("claim-1", T3)
        self.assertIsNotNone(before)
        self.assertEqual("围墙内为双圣湖布局", before.text)
        self.assertEqual("整个调查区", before.scope)
        self.assertEqual(T4, before.valid_to)
        self.assertIsNone(self.claims.claim_at("claim-1", T4))
        with self.assertRaises(ClaimError):
            self.claims.review("claim-1", "cn-reviewer", "CN", True, T4)

    def test_narrow_scope_creates_next_version_without_rewriting(self) -> None:
        self.submit_disputable()
        successor = self.claims.narrow("claim-1", "仅北湖区", T4)
        self.assertEqual(2, successor.version)
        old = self.claims.claim_at("claim-1", T3)
        self.assertEqual("整个调查区", old.scope)
        self.assertEqual(T4, old.valid_to)
        current = self.claims.claim_at("claim-1", T4)
        self.assertEqual("仅北湖区", current.scope)


if __name__ == "__main__":
    unittest.main()
