from __future__ import annotations

import unittest

from helpers import make_store

from joint_excavation_context.custody import CustodyError, CustodyService

T1 = "2026-09-20T08:00:00+08:00"
T2 = "2026-09-21T08:00:00+08:00"
T3 = "2026-09-22T08:00:00+08:00"
T4 = "2026-09-23T08:00:00+08:00"
T5 = "2026-09-24T08:00:00+08:00"


class CustodyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.custody = CustodyService(make_store())

    def transfer(self, event_id, stage, from_party, to_party, occurred_at,
                 object_id="obj-1", container_id="box-1"):
        self.custody.transfer(
            event_id=event_id, object_id=object_id, from_party=from_party,
            to_party=to_party, stage=stage, container_id=container_id,
            occurred_at=occurred_at, custody_location="库房-A",
        )

    def test_full_chain_keeps_single_custodian(self) -> None:
        self.transfer("t1", "excavated", None, "中方登记组", T1)
        self.transfer("t2", "conservation", "中方登记组", "埃方修复室", T2)
        self.transfer("t3", "lab", "埃方修复室", "联合实验室", T3)
        self.transfer("t4", "returned", "联合实验室", "埃方库房", T4)
        current = self.custody.current_custodian("obj-1")
        self.assertEqual("埃方库房", current["custodian"])
        self.assertEqual("returned", current["stage"])

    def test_first_transfer_must_be_excavation(self) -> None:
        with self.assertRaises(CustodyError):
            self.transfer("t1", "lab", None, "联合实验室", T1)

    def test_transfer_must_continue_from_current_custodian(self) -> None:
        self.transfer("t1", "excavated", None, "中方登记组", T1)
        with self.assertRaises(CustodyError):
            self.transfer("t2", "conservation", "埃方修复室", "联合实验室", T2)

    def test_stages_must_advance_in_order(self) -> None:
        self.transfer("t1", "excavated", None, "中方登记组", T1)
        with self.assertRaises(CustodyError):
            self.transfer("t2", "lab", "中方登记组", "联合实验室", T2)

    def test_seal_anomaly_freezes_container_immediately(self) -> None:
        self.transfer("t1", "excavated", None, "中方登记组", T1)
        self.custody.record_seal_anomaly(
            event_id="seal-1", container_id="box-1",
            description="封签编号与登记不符", occurred_at=T2,
        )
        self.assertIn("box-1", self.custody.frozen_containers())
        # 容器内对象不能继续交接
        with self.assertRaises(CustodyError):
            self.transfer("t2", "conservation", "中方登记组", "埃方修复室", T3)
        # 其他对象也不能放入冻结容器
        with self.assertRaises(CustodyError):
            self.transfer("t3", "excavated", None, "中方登记组", T3, object_id="obj-2")

    def test_release_container_resumes_transfers(self) -> None:
        self.transfer("t1", "excavated", None, "中方登记组", T1)
        self.custody.record_seal_anomaly(
            event_id="seal-1", container_id="box-1",
            description="封签破损", occurred_at=T2,
        )
        self.custody.release_container(
            event_id="seal-1-release", container_id="box-1",
            authorizer="联合保管负责人", occurred_at=T4,
        )
        self.transfer("t2", "conservation", "中方登记组", "埃方修复室", T5)
        self.assertEqual("埃方修复室", self.custody.current_custodian("obj-1")["custodian"])

    def test_release_requires_frozen_state(self) -> None:
        with self.assertRaises(CustodyError):
            self.custody.release_container(
                event_id="r1", container_id="box-9",
                authorizer="负责人", occurred_at=T1,
            )


if __name__ == "__main__":
    unittest.main()
