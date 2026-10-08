"""Actual customer-profile forms with reusable local records."""
import os
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app.services.creator import brand_profiles


class BrandWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"MPT_CREATOR_DATA": self.temp.name})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def editor(self):
        return AppTest.from_string("from webui.creator_brand_workspace import _render_editor\n_render_editor()", default_timeout=30).run()

    def test_four_customer_types_save_through_same_optional_questionnaire(self):
        for business_type in ("local_store", "ecommerce", "service", "knowledge"):
            with self.subTest(business_type=business_type):
                app = self.editor()
                app.selectbox(key="brand_editor_business_type").set_value(business_type).run()
                app.text_area(key="brand_editor_offering").set_value("客户的实际内容 " + business_type).run()
                app.text_area(key="brand_editor_audience").set_value("目标客户").run()
                app.button(key="brand_editor_save").click().run()
                self.assertFalse(app.exception)
                saved = brand_profiles.get_profile(app.session_state["brand_editor_saved_id"])
                self.assertEqual(business_type, saved["business_type"])
                self.assertEqual("", saved["differentiators"])
                self.assertEqual("", saved["desired_action"])
                self.assertFalse(any(saved["extras"].values()))
                self.assertEqual("", app.text_area(key="brand_editor_offering").value)
                self.assertEqual("", app.text_area(key="brand_editor_audience").value)
        self.assertEqual(4, len(brand_profiles.list_profiles()))

    def test_empty_form_reports_missing_core_information_without_sample_profile(self):
        app = self.editor()
        app.button(key="brand_editor_save").click().run()
        self.assertFalse(app.exception)
        self.assertTrue(any("请填写" in item.value for item in app.error))
        self.assertEqual([], brand_profiles.list_profiles())

    def test_management_edit_changes_global_profile_without_attaching_to_old_work(self):
        profile = brand_profiles.save_profile({"offering": "原服务", "audience": "原客户"})
        app = AppTest.from_string("from webui.creator_brand_workspace import render\nrender()", default_timeout=30).run()
        app.button(key="creator_brand_edit_" + profile["id"]).click().run()
        app.text_area(key="brand_editor_offering").set_value("新服务").run()
        app.button(key="brand_editor_save").click().run()
        self.assertFalse(app.exception)
        self.assertEqual("新服务", brand_profiles.get_profile(profile["id"])["offering"])
        self.assertNotIn("studio_pending_brand", app.session_state)


if __name__ == "__main__":
    unittest.main()
