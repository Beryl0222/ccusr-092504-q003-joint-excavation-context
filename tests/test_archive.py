from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from joint_excavation_context import (
    ArchiveService,
    ClaimPublicationProcess,
    EventStore,
    VersionConflict,
    evidence_chain,
    public_evidence_chain,
    public_spatial_view,
    spatial_view,
)
from joint_excavation_context import events as E

REVIEWERS = {("cn", "张领队"), ("eg", "Hassan")}


def make_service(path: Path | None = None) -> tuple[ArchiveService, dict]:
    store = EventStore(path)
    schema = json.loads((ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8"))
    service = ArchiveService(store, schema, authorized_reviewers=set(REVIEWERS))
    return service, schema


class JointArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.service, _ = make_service(Path(self.tmp.name) / "events.jsonl")
        self.ingest = self.service.ingest_batch

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ---- 批次 1：编号对照、观测、关系、出土与首次交接 ----

    def _batch_one(self) -> None:
        t = "2026-09-20T09:00:00+08:00"
        envelopes = [
            E.alias_registered("alias-1", t, 1, "中方探方号登记",
                               canonical_id="unit-1", local_code="T2026-01",
                               party="cn", entity_type="excavation_unit"),
            E.alias_registered("alias-2", t, 2, "埃方探方号登记",
                               canonical_id="unit-1", local_code="Unit-S7",
                               party="eg", entity_type="excavation_unit"),
            E.context_opened("ctx-area", "survey_area", "area-1", t, 1,
                             "调查区建立", kind="survey_area", label="南部台地调查区"),
            E.context_opened("ctx-unit", "excavation_unit", "unit-1", t, 1,
                             "联合探方开启", kind="excavation_unit", label="T2026-01 / Unit-S7",
                             parent_id="area-1",
                             coordinates={"crs": "local", "easting": 500123.4, "northing": 3100456.7}),
            E.observation_captured("obs-1", "p1", t, 1, "围墙基线测点观测",
                                   content={"note": "土坯墙基线"}, content_hash="h1"),
            E.relation_recorded("rel-1", "structure", "wall-1", t, 1,
                                "围墙位于探方内", subject_id="wall-1", predicate="within",
                                target_id="unit-1", based_on=["obs-1"]),
            E.object_recovered("obj-1", "stela-17", t, 1, "石碑残件出土",
                               found_in="unit-1", custodian="field-cn",
                               container_id="box-1", point_events=["obs-1"]),
            E.object_transferred("tr-1", "stela-17", t, 2, "出土后移交清理",
                                 stage="cleaned", from_party="field-cn",
                                 to_party="lab-cn", container_id="box-1"),
        ]
        report = self.ingest("b1", envelopes)
        self.assertEqual({}, report.rejected)
        self.assertEqual(8, len(report.accepted))

    def test_batch_one_and_crosswalk(self) -> None:
        self._batch_one()
        self.assertEqual("unit-1", self.service.crosswalk.resolve("excavation_unit", "cn", "T2026-01"))
        self.assertEqual("unit-1", self.service.crosswalk.resolve("excavation_unit", "eg", "Unit-S7"))
        self.assertEqual(2, len(self.service.crosswalk.aliases_of("unit-1")))

    # ---- 保管链：唯一责任、阶段前进、封签冻结 ----

    def test_custody_chain_and_seal_freeze(self) -> None:
        self._batch_one()
        t = "2026-09-20T15:00:00+08:00"

        # 交出方不是当前保管人 -> 拒绝
        bad = [E.object_transferred("tr-bad", "stela-17", t, 3, "非保管人试图送检",
                                    stage="sent_for_analysis", from_party="field-cn",
                                    to_party="lab-eg", container_id="box-1")]
        report = self.ingest("b1-custody", bad)
        self.assertIn("tr-bad", report.rejected)
        self.assertTrue(any("保管责任必须唯一" in r for r in report.rejected["tr-bad"]))

        # 封签异常 -> 容器立即冻结，其后该容器的交接全部拒绝
        frozen = [
            E.seal_anomaly_reported("seal-1", "box-1", t, 1,
                                    "箱封签编号不符", detail="签号 A-12 与台账不一致"),
            E.object_transferred("tr-2", "stela-17", t, 4, "清理后送检",
                                 stage="sent_for_analysis", from_party="lab-cn",
                                 to_party="lab-eg", container_id="box-1"),
        ]
        report = self.ingest("b1-freeze", frozen)
        self.assertIn("seal-1", report.accepted)
        self.assertEqual({"box-1": "b1-freeze:freeze:box-1:2"}, report.frozen_containers)
        self.assertIn("tr-2", report.rejected)
        self.assertTrue(any("冻结" in r for r in report.rejected["tr-2"]))
        self.assertTrue(self.service.ledger.is_frozen("box-1"))

        # 唯一出土记录不能重复
        dup = [E.object_recovered("obj-1-dup", "stela-17", t, 9, "重复出土登记",
                                  found_in="unit-1", custodian="field-cn", container_id="box-9")]
        report = self.ingest("b1-dup", dup)
        self.assertTrue(any("出土记录" in r for r in report.rejected["obj-1-dup"]))

    # ---- 同测点异内容：只隔离争议测点及其依赖成果，其他区域继续入库 ----

    def test_same_point_conflict_quarantines_only_dependents(self) -> None:
        self._batch_one()
        t = "2026-09-21T10:00:00+08:00"
        envelopes = [
            # p1 同版本不同内容
            E.observation_captured("obs-1b", "p1", t, 1, "对围墙基线的另一种读数",
                                   content={"note": "并非墙基线，而是后期沟边"},
                                   content_hash="h2"),
            # 依赖争议观测的新测绘版本 -> 一并隔离
            E.survey_revised("rev-2", "rev-wall-2", t, 1, "围墙测绘修订版",
                             survey_of="wall-1", supersedes="rev-wall-1",
                             based_on=["obs-1b"]),
            # 另一测点与成果，不受影响
            E.observation_captured("obs-2", "p2", t, 1, "陶片散布观测",
                                   content={"sherds": 12}, content_hash="h3"),
            E.relation_recorded("rel-2", "stratigraphic_unit", "su-9", t, 1,
                                "地层单位属于探方", subject_id="su-9",
                                predicate="within", target_id="unit-1",
                                based_on=["obs-2"]),
        ]
        report = self.ingest("b2", envelopes)

        self.assertEqual({"p1"}, set(report.quarantined_candidates))
        self.assertIn("obs-1b", report.quarantined_candidates["p1"])
        self.assertIn("rev-2", report.quarantined_candidates["p1"])
        self.assertIn("obs-2", report.accepted)
        self.assertIn("rel-2", report.accepted)
        self.assertIsNone(self.service.store.get("obs-1b"))  # 隔离候选不入库
        self.assertIsNone(self.service.store.get("rev-2"))

        # 库内旧观测及其依赖成果被隔离标记
        self.assertIn("p1", report.quarantined)
        tainted = set(report.quarantined["p1"])
        self.assertIn("obs-1", tainted)
        self.assertIn("rel-1", tainted)
        self.assertTrue(self.service.store.has("b2:quarantine:p1:1"))

        # 引用被隔离观测的新主张不得成立
        bad_claim = [E.claim_proposed(
            "claim-bad", "claim-bad-x", t, 1, "建立在隔离观测上的主张",
            topic="ritual_function", statement="X", citations=["obs-1"])]
        report = self.ingest("b2-badclaim", bad_claim)
        self.assertTrue(any("隔离" in r for r in report.rejected["claim-bad"]))

    # ---- 可争论主题的双方会审与会审发布恢复 ----

    def _publish_old_claim(self) -> None:
        self.test_same_point_conflict_quarantines_only_dependents()
        t0 = "2026-09-22T10:00:00+08:00"
        proposed = E.claim_proposed(
            "claim-old-evt", "claim-old", t0, 1,
            "旧解释：围墙只服务北侧圣湖礼仪",
            topic="ritual_function",
            statement="围墙与附属小神殿仅对应单一圣湖的礼仪动线",
            scope={"area": "area-1"}, citations=["obs-2"])
        self.ingest("b-claims", [proposed])

        # 未授权会审人被拒绝
        rogue = [E.claim_reviewed("rev-rogue", "claim-old", t0, 2, "越权会审",
                                  decision="approve", reviewer="外人", party="cn")]
        report = self.ingest("b-claims-rogue", rogue)
        self.assertTrue(any("授权会审名单" in r for r in report.rejected["rev-rogue"]))

        # 仅中方通过 -> 发布流程停在会审核验，可恢复
        cn_only = [E.claim_reviewed("rev-cn", "claim-old", "2026-09-23T09:00:00+08:00", 2,
                                    "中方同意", decision="approve",
                                    reviewer="张领队", party="cn")]
        self.ingest("b-claims-cn", cn_only)
        process = ClaimPublicationProcess(self.service, "claim-old")
        first = process.run()
        self.assertEqual("awaiting_review", first.status)
        self.assertEqual(["verify_evidence"], first.steps_run)
        self.assertIsNone(self.service.store.get("publish-claim-old:record"))

        # 埃方补签后重跑 -> 只补未完成步骤
        eg = [E.claim_reviewed("rev-eg", "claim-old", "2026-09-23T15:00:00+08:00", 3,
                               "埃方同意", decision="approve",
                               reviewer="Hassan", party="eg")]
        self.ingest("b-claims-eg", eg)
        second = ClaimPublicationProcess(self.service, "claim-old").run()
        self.assertEqual("published", second.status)
        self.assertEqual(["verify_evidence"], second.steps_skipped)
        self.assertEqual(
            ["verify_review", "publish_record", "apply_supersessions", "refresh_public_index"],
            second.steps_run,
        )
        # 再跑一次：全部跳过，发布记录只有一条
        third = ClaimPublicationProcess(self.service, "claim-old").run()
        self.assertEqual("published", third.status)
        self.assertEqual(5, len(third.steps_skipped))

    def test_debatable_claim_requires_both_parties_and_resumes(self) -> None:
        self._publish_old_claim()
        self.assertEqual("approved", self.service.registry.state_of("claim-old").status)

    # ---- 南部圣湖发现：新主张使旧主张失效、旧记录不被覆盖 ----

    def test_new_finding_invalidates_old_claim_without_rewriting(self) -> None:
        self._publish_old_claim()
        t = "2026-09-25T10:00:00+08:00"
        finding = [
            E.context_opened("ctx-lake-south", "structure", "lake-south", t, 1,
                             "确认此前未知的南部圣湖", kind="sacred_lake",
                             label="南部圣湖", parent_id="area-1",
                             coordinates={"crs": "local", "easting": 500800.1}),
            E.observation_captured("obs-4", "p4", t, 1, "南部湖相沉积与堤岸测点",
                                   content={"facies": "湖相黏土"}, content_hash="h4"),
        ]
        self.ingest("b3", finding)

        new_claim = E.claim_proposed(
            "claim-new-evt", "claim-new", t, 1, "双圣湖布局解释",
            topic="dual_sacred_lake_layout",
            statement="礼仪建筑群对应南北双圣湖的对称布局，围墙动线须重新解释",
            scope={"area": "area-1"}, citations=["obs-4"],
            supersedes=[{"claim_id": "claim-old", "mode": "invalidate"}])
        self.ingest("b3-claim", [new_claim])
        self.ingest("b3-rev-cn", [E.claim_reviewed(
            "claim-new-cn", "claim-new", t, 2, "中方同意",
            decision="approve", reviewer="张领队", party="cn")])
        self.ingest("b3-rev-eg", [E.claim_reviewed(
            "claim-new-eg", "claim-new", t, 3, "埃方同意",
            decision="approve", reviewer="Hassan", party="eg")])
        published = ClaimPublicationProcess(self.service, "claim-new").run()
        self.assertEqual("published", published.status)
        self.assertEqual(["claim-old"], published.superseded)

        # 旧主张失效，但原提议原文和会审记录仍在事件流中
        old = self.service.registry.state_of("claim-old")
        self.assertEqual("invalidated", old.status)
        self.assertEqual("claim-new", old.superseded_by)
        proposal = next(e for e in self.service.store.stream("research_claim", "claim-old")
                        if e.event_type == "CLAIM_PROPOSED")
        self.assertEqual("围墙与附属小神殿仅对应单一圣湖的礼仪动线", proposal.data["statement"])
        self.assertTrue(self.service.store.has("rev-cn") and self.service.store.has("rev-eg"))

    # ---- 缩限适用范围 ----

    def test_claim_can_be_narrowed(self) -> None:
        self.test_new_finding_invalidates_old_claim_without_rewriting()
        t = "2026-09-25T16:00:00+08:00"
        old2 = E.claim_proposed(
            "claim-old2-evt", "claim-old2", t, 1, "旧年代主张",
            topic="building_chronology",
            statement="围墙与奥西里斯小神殿均建于第26王朝",
            citations=["obs-2"])
        self.ingest("b4", [old2])
        self.ingest("b4-cn", [E.claim_reviewed("old2-cn", "claim-old2", t, 2, "中方同意",
                                               decision="approve", reviewer="张领队", party="cn")])
        self.ingest("b4-eg", [E.claim_reviewed("old2-eg", "claim-old2", t, 3, "埃方同意",
                                               decision="approve", reviewer="Hassan", party="eg")])
        ClaimPublicationProcess(self.service, "claim-old2").run()

        new2 = E.claim_proposed(
            "claim-new2-evt", "claim-new2", t, 4, "年代主张缩限",
            topic="building_chronology",
            statement="第26王朝断代仅适用于小神殿本体，围墙包含更早阶段",
            citations=["obs-4"],
            supersedes=[{"claim_id": "claim-old2", "mode": "narrow",
                         "narrowed_scope": {"applies_to": "osiris-shrine-only"}}])
        self.ingest("b5", [new2])
        self.ingest("b5-cn", [E.claim_reviewed("new2-cn", "claim-new2", t, 5, "中方同意",
                                               decision="approve", reviewer="张领队", party="cn")])
        self.ingest("b5-eg", [E.claim_reviewed("new2-eg", "claim-new2", t, 6, "埃方同意",
                                               decision="approve", reviewer="Hassan", party="eg")])
        ClaimPublicationProcess(self.service, "claim-new2").run()

        old_state = self.service.registry.state_of("claim-old2")
        self.assertEqual("narrowed", old_state.status)
        self.assertEqual({"applies_to": "osiris-shrine-only"}, dict(old_state.narrowed_scope))

    # ---- 历史时点复原 ----

    def test_reconstruct_at_any_point_in_time(self) -> None:
        self.test_new_finding_invalidates_old_claim_without_rewriting()

        # 2026-09-24：旧主张仍有效，新主张尚不存在
        past = spatial_view(self.service.store, "2026-09-24T23:59:59+08:00")
        self.assertEqual("approved", past.claims.get("claim-old"))
        self.assertNotIn("claim-new", past.claims)

        # 争议发生前（2026-09-21 之前）围墙关系仍可见
        before_quarantine = spatial_view(self.service.store, "2026-09-21T00:00:00+08:00")
        self.assertIn("rel-1", {edge.event_id for edge in before_quarantine.relations})

        # 当前视图：旧主张失效，争议关系已隔离
        now = spatial_view(self.service.store)
        self.assertEqual("invalidated", now.claims.get("claim-old"))
        self.assertEqual("approved", now.claims.get("claim-new"))
        self.assertNotIn("rel-1", {edge.event_id for edge in now.relations})
        self.assertIn("lake-south", now.entities)

        # 证据链复原
        chain = evidence_chain(self.service.store, "claim-new")
        self.assertIsNotNone(chain)
        self.assertEqual(["obs-4"], [o["event_id"] for o in chain["observations"]])
        self.assertIsNone(evidence_chain(self.service.store, "claim-new",
                                         "2026-09-24T23:59:59+08:00"))

    # ---- 公开视图脱敏 ----

    def test_public_view_redacts_coordinates_unpublished_and_custody(self) -> None:
        self.test_claim_can_be_narrowed()
        t = "2026-09-26T10:00:00+08:00"
        # 只发布允许公开的聚合：调查区、南部圣湖、新测点、新主张
        publish = [
            E.record_published("pub-area", "survey_area", "area-1", t, 2, "调查区公开"),
            E.record_published("pub-lake", "structure", "lake-south", t, 2, "南部圣湖公开"),
            E.record_published("pub-p4", "measurement_point", "p4", t, 2, "测点公开"),
        ]
        self.ingest("b-pub", publish)
        ClaimPublicationProcess(self.service, "claim-new").run()  # 幂等，发布记录已存在也无妨

        view = public_spatial_view(self.service.store)
        self.assertIn("area-1", view["entities"])
        self.assertIn("lake-south", view["entities"])
        self.assertNotIn("unit-1", view["entities"])       # 未发表
        self.assertNotIn("stela-17", view["entities"])
        entity = view["entities"]["lake-south"]
        self.assertNotIn("coordinates", entity)            # 精确坐标隐藏
        self.assertFalse(hasattr(view, "custody"))         # 内部保管信息不出库
        self.assertNotIn("claim-old", view["claims"])      # 已失效主张不公开
        self.assertIn("claim-new", view["claims"])
        self.assertEqual("approved", view["claims"]["claim-new"]["status"])

        chain = public_evidence_chain(self.service.store, "claim-new")
        self.assertIsNotNone(chain)
        self.assertEqual(["obs-4"], [o["event_id"] for o in chain["observations"]])
        for observation in chain["observations"]:
            self.assertNotIn("content", observation)       # 观测细节不公开
        self.assertIsNone(public_evidence_chain(self.service.store, "claim-old"))  # 失效不公开

    # ---- 重复/晚到与批次恢复 ----

    def test_duplicate_late_arrivals_are_idempotent_and_batch_resumes(self) -> None:
        self._batch_one()
        t = "2026-09-20T18:00:00+08:00"
        envelope = E.observation_captured("obs-2", "p2", t, 1, "陶片散布观测",
                                          content={"sherds": 12}, content_hash="h3")
        first = self.ingest("late-1", [envelope])
        second = self.ingest("late-1", [envelope])  # 晚到重传：同批次重跑
        self.assertEqual(["obs-2"], first.accepted)
        self.assertEqual(["obs-2"], second.duplicates)
        self.assertEqual(["validate", "conflicts", "commit"], second.steps_skipped)
        self.assertEqual(1, len([e for e in self.service.store if e.event_id == "obs-2"]))

        # 模拟 validate 步骤已完成后中断：重跑只执行后两步
        late_obs = E.observation_captured("obs-5", "p5", t, 1, "晚到测点",
                                          content={"x": 1}, content_hash="h5")
        self.service.record_step("late-2", "validate", {"simulated": True})
        report = self.ingest("late-2", [late_obs])
        self.assertEqual(["validate"], report.steps_skipped)
        self.assertEqual(["conflicts", "commit"], report.steps_run)
        self.assertIn("obs-5", report.accepted)

    def test_resume_after_conflicts_step_still_excludes_quarantined(self) -> None:
        self._batch_one()
        t = "2026-09-21T10:00:00+08:00"
        envelopes = [
            E.observation_captured("obs-1b", "p1", t, 1, "异内容读数",
                                   content={"note": "后期沟边"}, content_hash="h2"),
            E.survey_revised("rev-2", "rev-wall-2", t, 1, "依赖争议观测的测绘版",
                             survey_of="wall-1", based_on=["obs-1b"]),
            E.observation_captured("obs-7", "p7", t, 1, "无关测点",
                                   content={"ok": True}, content_hash="h7"),
        ]
        # 模拟冲突检测完成后、提交前中断
        self.service.record_step("late-3", "validate", {"simulated": True})
        self.service.record_step("late-3", "conflicts", {"simulated": True})
        report = self.ingest("late-3", envelopes)
        self.assertIsNone(self.service.store.get("obs-1b"))
        self.assertIsNone(self.service.store.get("rev-2"))
        self.assertIn("obs-7", report.accepted)

    # ---- 持久化与重放 ----

    def test_store_persistence_replay_and_truncated_tail(self) -> None:
        self.test_claim_can_be_narrowed()
        path = Path(self.tmp.name) / "events.jsonl"
        service2, _ = make_service(path)
        self.assertEqual(len(self.service.store), len(service2.store))
        self.assertEqual("narrowed", service2.registry.state_of("claim-old2").status)
        self.assertIn("obs-1", service2.quarantined_events)
        self.assertEqual("unit-1", service2.crosswalk.resolve("excavation_unit", "eg", "Unit-S7"))

        # 封签冻结在另一个独立库中验证重放
        freeze_path = Path(self.tmp.name) / "freeze.jsonl"
        fs, _ = make_service(freeze_path)
        t = "2026-09-20T09:00:00+08:00"
        fs.ingest_batch("f1", [
            E.object_recovered("f-obj", "o-1", t, 1, "出土", found_in="u-1",
                               custodian="field-cn", container_id="box-9"),
            E.seal_anomaly_reported("f-seal", "box-9", t, 1, "封签破损", detail="裂"),
        ])
        self.assertTrue(fs.ledger.is_frozen("box-9"))
        reopened, _ = make_service(freeze_path)
        self.assertTrue(reopened.ledger.is_frozen("box-9"))

        # JSONL 尾部半行（写入中断）不影响既有事件恢复
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"event_id": "broken", "event_type":')
        service3, _ = make_service(Path(self.tmp.name) / "events-copy.jsonl")  # 新空库对照
        del service3
        service4, _ = make_service(path)
        self.assertEqual(len(service2.store), len(service4.store))

    def test_direct_version_conflict_is_rejected(self) -> None:
        t = "2026-09-20T09:00:00+08:00"
        envelope = E.context_opened("ctx-x", "structure", "x", t, 1, "x", kind="k", label="x")
        self.service.store.append(envelope)
        with self.assertRaises(VersionConflict):
            self.service.store.append(dict(envelope, event_id="ctx-x-other"))


if __name__ == "__main__":
    unittest.main()
