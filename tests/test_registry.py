from __future__ import annotations

import unittest

from helpers import make_event, make_store

from joint_excavation_context.registry import AliasConflictError, AliasRegistry


class RegistryTests(unittest.TestCase):
    def test_dual_party_ids_resolve_to_same_aggregate(self) -> None:
        registry = AliasRegistry()
        registry.register("cn_trench", "中-T1024", "unit-001")
        registry.register("eg_trench", "EG-SQ-77", "unit-001")
        registry.register("object_temp_id", "暂存-3315", "obj-001")
        self.assertEqual("unit-001", registry.resolve("cn_trench", "中-T1024"))
        self.assertEqual("unit-001", registry.resolve("eg_trench", "EG-SQ-77"))
        self.assertEqual(
            {"cn_trench": "中-T1024", "eg_trench": "EG-SQ-77"},
            registry.aliases_of("unit-001"),
        )

    def test_conflicting_mapping_is_rejected(self) -> None:
        registry = AliasRegistry()
        registry.register("survey_version", "测图-v3", "survey-001")
        registry.register("survey_version", "测图-v3", "survey-001")  # 幂等
        with self.assertRaises(AliasConflictError):
            registry.register("survey_version", "测图-v3", "survey-002")

    def test_unknown_alias_resolves_to_none(self) -> None:
        self.assertIsNone(AliasRegistry().resolve("cn_trench", "中-T9999"))


if __name__ == "__main__":
    unittest.main()
