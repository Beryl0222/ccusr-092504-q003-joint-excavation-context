from __future__ import annotations

import unittest

from helpers import make_event, make_store

from joint_excavation_context.claims import ClaimService
from joint_excavation_context.views import (
    evidence_chain,
    public_records,
    spatial_snapshot,
)

T1 = "2026-09-20T08:00:00+08:00"
T2 = "2026-09-21T08:00:00+08:00"
T3 = "2026-09-22T08:00:00+08:00"
T4 = "2026-09-23T08:00:00+08:00"

REVIEWERS = {"CN": {"cn-reviewer"}, "EG": {"eg-reviewer"}}


def build_store():
    store = make_store()
    store.append(make_event(
        "e-area", "CONTEXT_OPENED", "survey_area", "area-1", T1,
        grid="G7", coordinates={"lat": 25.7, "lon": 32.6},
    ))
    store.append(make_event(
        "e-unit", "CONTEXT_OPENED", "excavation_unit", "unit-1", T1,
        parent_id="area-1", layer="L2",
    ))
    store.append(make_event(
        "e-structure", "CONTEXT_OPENED", "structure", "wall-1", T2,
        parent_id="unit-1",
    ))
    store.append(make_event(
        "e-object", "CONTEXT_OPENED", "archaeological_object", "obj-1", T2,
        found_in="unit-1",
    ))
    store.append(make_event(
        "e-obs", "OBSERVATION_CAPTURED", "survey_revision", "rev-1", T2,
        survey_point="P1", content_hash="hash-a", grid="G7",
    ))
    store.append(make_event(
        "e-rev2", "OBSERVATION_CAPTURED", "survey_revision", "rev-1", T3,
        version=2, survey_point="P1", content_hash="hash-b", grid="G7",
    ))
    return store


class SpatialSnapshotTests(unittest.TestCase):
    def test_snapshot_reconstructs_spatial_relations_as_of(self) -> None:
        store = build_store()
        snap = spatial_snapshot(store, T2)
        self.assertEqual("area-1", snap["units"]["unit-1"]["area"])
        self.assertEqual("unit-1", snap["structures"]["wall-1"]["unit"])
        self.assertEqual("unit-1", snap["objects"]["obj-1"]["found_in"])
        self.assertEqual(1, snap["survey_revisions"]["rev-1"]["version"])

        later = spatial_snapshot(store, T4)
        self.assertEqual(2, later["survey_revisions"]["rev-1"]["version"])

    def test_snapshot_before_structure_exists(self) -> None:
        store = build_store()
        snap = spatial_snapshot(store, T1)
        self.assertEqual({}, snap["structures"])
        self.assertIn("unit-1", snap["units"])


class EvidenceChainTests(unittest.TestCase):
    def test_chain_links_claim_to_observations(self) -> None:
        store = build_store()
        claims = ClaimService(store, REVIEWERS)
        record = claims.submit(
            "claim-1", "twin_lake_layout", "双圣湖布局",
            scope="全区", citations=("e-obs",), author="cn-reviewer", occurred_at=T3,
        )
        chain = evidence_chain(store, record)
        self.assertEqual(["e-obs"], [o["event_id"] for o in chain["observations"]])
        self.assertEqual([], chain["missing"])
        self.assertIsNone(chain["valid_to"])


class PublicViewTests(unittest.TestCase):
    def test_public_view_hides_unpublished_and_sensitive_fields(self) -> None:
        store = build_store()
        # 未发表前：公开查询为空
        self.assertEqual([], public_records(store, T4))

        claims = ClaimService(store, REVIEWERS)
        claims.submit(
            "claim-1", "twin_lake_layout", "双圣湖布局",
            scope="全区", citations=("e-obs",), author="cn-reviewer", occurred_at=T3,
        )
        claims.review("claim-1", "cn-reviewer", "CN", True, T3)
        claims.review("claim-1", "eg-reviewer", "EG", True, T3)
        claims.publish("claim-1", T4)

        records = public_records(store, T4)
        self.assertTrue(records)
        self.assertTrue(all(r["aggregate_id"] == "claim-1" for r in records))
        # 未发表的调查区坐标不外发
        self.assertNotIn("coordinates", {k for r in records for k in r})

    def test_public_view_strips_precise_location_from_published_material(self) -> None:
        store = make_store()
        store.append(make_event(
            "e1", "CONTEXT_OPENED", "survey_area", "area-1", T1,
            coordinates={"lat": 25.7, "lon": 32.6}, grid="G7",
            custody_location="内部库房-3", container_id="box-1",
        ))
        store.append(make_event(
            "e2", "RECORD_PUBLISHED", "survey_area", "area-1", T2, version=2,
        ))
        records = public_records(store, T2)
        self.assertEqual({"CONTEXT_OPENED", "RECORD_PUBLISHED"}, {r["event_type"] for r in records})
        for record in records:
            self.assertNotIn("coordinates", record)
            self.assertNotIn("custody_location", record)
            self.assertNotIn("container_id", record)
        (opened,) = [r for r in records if r["event_type"] == "CONTEXT_OPENED"]
        self.assertEqual("G7", opened["grid"])  # 粗粒度网格可公开


if __name__ == "__main__":
    unittest.main()
