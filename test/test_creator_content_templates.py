import unittest

from app.services.creator import content_templates


class ContentTemplateTests(unittest.TestCase):
    def test_six_purposes_have_recommended_and_alternative_original_templates(self):
        purposes = content_templates.list_purposes()
        self.assertEqual(["product_service", "promotion", "knowledge", "case_feedback", "personal_brand", "quick_edit"],
                         [row["id"] for row in purposes])
        ids = []
        for purpose in purposes:
            templates = content_templates.list_templates(purpose["id"])
            self.assertGreaterEqual(len(templates), 2)
            self.assertEqual(1, sum(row["recommended"] for row in templates))
            self.assertEqual(purpose["recommended_template"], next(row["id"] for row in templates if row["recommended"]))
            for template in templates:
                self.assertEqual(purpose["id"], template["purpose_id"])
                self.assertTrue(template["structure"])
                self.assertTrue(template["material_clues"])
                self.assertEqual(60, template["target_duration"])
                self.assertEqual([30, 60, 90, 120], template["supported_durations"])
                ids.append(template["id"])
        self.assertEqual(12, len(set(ids)))

    def test_examples_cover_four_customer_types_and_are_labelled_as_non_customer_facts(self):
        types = set()
        for purpose in content_templates.list_purposes():
            for template in content_templates.list_templates(purpose["id"]):
                example = template["example"]
                self.assertIn("不是客户事实", example["label"])
                self.assertTrue(example["context"])
                self.assertTrue(example["script"])
                types.add(example["business_type"])
        self.assertEqual({"local_store", "ecommerce", "service", "knowledge"}, types)

    def test_asset_availability_controls_route_for_any_customer_business(self):
        for business in ("general", "local_store", "ecommerce", "service", "knowledge"):
            profile = {"business_type": business, "industry": "用户行业", "offering": "用户内容"}
            for purpose in ("product_service", "promotion"):
                self.assertEqual("knowledge", content_templates.recommend_kind(purpose, profile))
                self.assertEqual("product", content_templates.recommend_kind(purpose, profile, has_materials=True))
            self.assertEqual("knowledge", content_templates.recommend_kind("case_feedback", profile))
            self.assertEqual("montage", content_templates.recommend_kind("case_feedback", profile, has_materials=True))
            self.assertEqual("knowledge", content_templates.recommend_kind("personal_brand", profile))
            self.assertEqual("avatar", content_templates.recommend_kind("personal_brand", profile, has_avatar=True))
            self.assertEqual("montage", content_templates.recommend_kind("quick_edit", profile))
            self.assertEqual("montage", content_templates.recommend_kind("quick_edit", profile, has_avatar=True))
            self.assertEqual("knowledge", content_templates.recommend_kind("knowledge", profile, has_materials=True, has_avatar=True))
            self.assertEqual("用户内容", profile["offering"])

    def test_catalog_returns_detached_copies_and_does_not_leak_mutations(self):
        purpose = content_templates.list_purposes()[0]
        purpose["name"] = "外部变更"
        template = content_templates.get_template("product_intro")
        template["structure"].append("外部新增步骤")
        template["example"]["script"] = "外部改为客户事实"
        self.assertNotIn("外部新增步骤", content_templates.get_template("product_intro")["structure"])
        self.assertNotEqual("外部改为客户事实", content_templates.get_template("product_intro")["example"]["script"])
        self.assertNotEqual("外部变更", content_templates.list_purposes()[0]["name"])

    def test_unknown_ids_are_rejected_without_generating_fallbacks(self):
        for value in ("unknown", None, []):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    content_templates.list_templates(value)
                with self.assertRaises(ValueError):
                    content_templates.get_template(value)
                with self.assertRaises(ValueError):
                    content_templates.recommend_kind(value)


if __name__ == "__main__":
    unittest.main()
