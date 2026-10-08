import copy
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from app.services.creator import brand_profiles, content_templates, store


class BrandProfileTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.env = patch.dict(os.environ, {"MPT_CREATOR_DATA": self.folder.name})
        self.env.start()
        self.addCleanup(self.env.stop)

    def profile(self, **changes):
        return brand_profiles.save_profile({"offering": "整理阅读笔记的方法", "audience": "想提高阅读效率的人", **changes})

    def test_four_customer_types_share_goals_without_restaurant_defaults(self):
        customers = [
            {"business_type": "local_store", "industry": "餐饮", "offering": "家常菜门店", "audience": "附近居民", "extras": {"address": "用户填写的门店地址"}},
            {"business_type": "ecommerce", "industry": "日用品", "offering": "便携水杯", "audience": "日常通勤者", "extras": {"product_features": "用户说明的拆洗方式"}},
            {"business_type": "service", "industry": "整理服务", "offering": "上门整理", "audience": "需要整理空间的家庭", "extras": {"service_area": "用户填写的服务范围"}},
            {"business_type": "knowledge", "industry": "阅读", "offering": "阅读方法分享", "audience": "阅读爱好者", "extras": {"expertise": "用户提供的阅读方向"}},
        ]
        for customer in customers:
            saved = brand_profiles.save_profile(customer)
            self.assertEqual(customer["extras"], saved["extras"])
            self.assertEqual(customer["industry"], saved["industry"])
            self.assertEqual("knowledge", content_templates.recommend_kind("product_service", saved))
            self.assertEqual("product", content_templates.recommend_kind("product_service", saved, has_materials=True))
        self.assertEqual(4, len(brand_profiles.list_profiles()))

    def test_optional_facts_remain_empty_and_names_can_be_generated_or_named(self):
        saved = self.profile()
        self.assertEqual({}, saved["extras"])
        self.assertEqual("", saved["industry"])
        self.assertEqual("", saved["differentiators"])
        self.assertEqual("", saved["desired_action"])
        self.assertEqual(saved["offering"][:20], saved["name"])
        named = self.profile(name="客户A", offering="别的产品")
        self.assertEqual("客户A", named["name"])
        self.assertEqual("general", named["business_type"])
        self.assertNotIn("example", saved)

    def test_updates_keep_identity_and_old_work_snapshots_are_unchanged(self):
        first = self.profile(name="原名称", extras={"price": "用户给出的99元"})
        snap = brand_profiles.snapshot(first["id"])
        original_snap = copy.deepcopy(snap)
        store.save_record("creator_projects", "already-generated", {"config": {"brand_snapshot": snap}})
        updated = brand_profiles.save_profile({"name": "更新后的名称", "extras": {"price": "用户更新为119元"}}, id=first["id"], expected_version=1)
        self.assertEqual(first["id"], updated["id"])
        self.assertEqual(2, updated["version"])
        self.assertEqual(first["created_at"], updated["created_at"])
        self.assertEqual(first["audience"], updated["audience"])
        self.assertEqual(original_snap, snap)
        self.assertEqual(original_snap, store.get_record("creator_projects", "already-generated")["config"]["brand_snapshot"])
        current = brand_profiles.get_profile(first["id"])
        current["extras"]["price"] = "外部字典变更"
        snap["extras"]["price"] = "旧快照变更"
        self.assertEqual("用户更新为119元", brand_profiles.get_profile(first["id"])["extras"]["price"])

    def test_optimistic_conflicts_do_not_overwrite_another_edit(self):
        saved = self.profile()
        brand_profiles.save_profile({"offering": "第一次修改", "expected_version": 1}, id=saved["id"])
        with self.assertRaises(brand_profiles.ProfileConflict):
            brand_profiles.save_profile({"offering": "过期编辑"}, id=saved["id"], expected_version=1)
        self.assertEqual("第一次修改", brand_profiles.get_profile(saved["id"])["offering"])

    def test_two_concurrent_expected_versions_have_only_one_success(self):
        saved = self.profile()
        def update(label):
            try:
                brand_profiles.save_profile({"name": label}, id=saved["id"], expected_version=1)
                return "saved"
            except brand_profiles.ProfileConflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(update, ["编辑A", "编辑B"]))
        self.assertCountEqual(["saved", "conflict"], results)
        self.assertEqual(2, brand_profiles.get_profile(saved["id"])["version"])

    def test_updates_cannot_target_or_modify_legacy_accounts(self):
        account = store.save_record("accounts", "legacy-account", {"name": "已有发布定位", "references": "旧资料"})
        with self.assertRaises(ValueError):
            brand_profiles.save_profile({"offering": "错误归属", "audience": "测试"}, id=account["id"])
        self.profile()
        self.assertEqual(account, store.get_record("accounts", account["id"]))
        self.assertIsNone(brand_profiles.get_profile("missing-profile"))
        with self.assertRaises(ValueError):
            brand_profiles.snapshot("missing-profile")

    def test_boundary_errors_never_persist_unknown_or_invalid_values(self):
        cases = [
            {"cookie": "not-supported"}, {"extras": {"api_key": "not-supported"}},
            {"business_type": []}, {"business_type": "invented"}, {"extras": []},
            {"offering": ""}, {"audience": 1}, {"name": "名" * 121},
            {"offering": "内容\x00控制"}, {"extras": {"contact": "电话\x1b[31m"}},
            {"extras": {"price": 99}}, {"expected_version": True}, {"expected_version": 0},
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.profile(**changes)
        self.assertEqual([], brand_profiles.list_profiles())

    def test_multiline_customer_text_is_preserved_and_validated_snapshot_is_detached(self):
        saved = self.profile(differentiators="第一项特点\n第二项特点", extras={"case_notes": "客户原记录\n实际过程"})
        self.assertEqual("第一项特点\n第二项特点", saved["differentiators"])
        snap = brand_profiles.snapshot(saved["id"])
        snap["extras"]["case_notes"] = "外部编辑"
        self.assertEqual(saved["extras"], brand_profiles.get_profile(saved["id"])["extras"])
        store.update_record("brand_profiles", saved["id"], {"extras": {"unknown_fact": "不能进入作品"}})
        with self.assertRaises(ValueError):
            brand_profiles.snapshot(saved["id"])

    def test_total_extra_budget_is_checked_before_a_profile_can_fail_drafting(self):
        from app.services.creator import topics
        with self.assertRaisesRegex(ValueError, "补充资料合计过长"):
            self.profile(extras={"case_notes": "例" * 6000, "address": "址" * 3000, "price": "价" * 3000})
        self.assertEqual([], brand_profiles.list_profiles())
        profile = self.profile(extras={"case_notes": "例" * 6000, "address": "址" * 1000, "price": "99元"})
        brief = topics._normalize_content_brief({"brand": brand_profiles.snapshot(profile["id"]), "video_purpose": "product_service"})
        self.assertEqual(profile["extras"], brief["brand"]["extras"])


if __name__ == "__main__":
    unittest.main()
