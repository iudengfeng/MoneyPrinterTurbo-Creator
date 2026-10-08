"""Verify reference-home inputs and real service handoffs without cloud calls."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app.services.creator import store, topics
from webui import creator_reference_script as column


APP = """
import streamlit as st
from webui.creator_reference_script import render_script_column
class Context:
    project = {}
    @property
    def busy(self):
        return st.session_state.get('ref_test_busy', False)
    def save_script(self, text):
        st.session_state['ref_test_saved'] = text
    def queue(self, kind, label, operation, **kwargs):
        st.session_state['ref_test_queue'] = {
            'kind': kind, 'title': label,
            'operation': operation.__module__ + '.' + operation.__name__,
            'kwargs': kwargs,
        }
        return 'test-job'
    def stage_upload(self, upload):
        raise AssertionError('Test has not supplied a local file')
render_script_column(Context())
"""


class ReferenceScriptTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"MPT_CREATOR_DATA": self.directory.name})
        self.environment.start()
        self.model_guard = patch("app.services.creator.topics._generate", side_effect=AssertionError("Unexpected cloud call"))
        self.model = self.model_guard.start()

    def tearDown(self):
        self.model_guard.stop()
        self.environment.stop()
        self.directory.cleanup()

    def app(self):
        app = AppTest.from_string(APP, default_timeout=30).run()
        self.assertFalse(app.exception)
        return app

    def test_empty_first_render_uses_reference_controls_and_no_sample_transcript(self):
        app = self.app()
        self.assertEqual(app.text_area(key="ref_original_text").value, "")
        self.assertEqual(app.text_area(key="ref_script_text").value, "")
        self.assertEqual(app.number_input(key="ref_target_length").value, 300)
        self.assertEqual(app.selectbox(key="ref_script_language").value, "中文（普通话）")
        self.assertEqual(app.session_state["ref_learning_mode"], "视频学习")
        self.model.assert_not_called()

    def test_edits_save_even_when_cleared_and_empty_generation_does_not_queue(self):
        app = self.app()
        manuscript = "这一段是当前编辑器中手工输入的真实文案。"
        app.text_area(key="ref_script_text").set_value(manuscript).run()
        self.assertEqual(app.session_state["ref_test_saved"], manuscript)
        app.text_area(key="ref_script_text").set_value("").run()
        self.assertEqual(app.session_state["ref_test_saved"], "")
        app.button(key="ref_write_script").click().run()
        self.assertNotIn("ref_test_queue", app.session_state)
        self.assertTrue(app.warning)
        self.assertFalse(app.exception)

    def test_write_and_rewrite_submit_real_service_with_current_text_and_length(self):
        app = self.app()
        original = "今天分享一个方法：先确定问题，再解释一个具体例子，让听众明白下一步怎么做。"
        revised = "已编辑的稿件要以这段为改写依据，不能重新使用过时的原文。"
        app.text_area(key="ref_original_text").set_value(original).run()
        app.number_input(key="ref_target_length").set_value(450).run()
        app.button(key="ref_write_script").click().run()
        job = app.session_state["ref_test_queue"]
        self.assertEqual(job["kind"], "script")
        self.assertEqual(job["operation"], "app.services.creator.topics.generate_draft")
        self.assertEqual(job["kwargs"]["reference_text"], original)
        self.assertEqual(job["kwargs"]["mode"], "original")
        self.assertEqual(job["kwargs"]["target_length"], 450)
        app.text_area(key="ref_script_text").set_value(revised).run()
        app.button(key="ref_rewrite_script").click().run()
        job = app.session_state["ref_test_queue"]
        self.assertEqual(job["kwargs"]["mode"], "imitate")
        self.assertEqual(job["kwargs"]["reference_text"], revised)
        self.assertEqual(app.text_area(key="ref_script_text").value, revised)
        self.model.assert_not_called()
        self.assertFalse(app.exception)

    def test_video_dialog_validates_link_and_queues_actual_extract_operation(self):
        app = self.app()
        app.button(key="ref_open_video").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.file_uploader[0].key, "ref_source_upload")
        app.button(key="ref_extract_link").click().run()
        self.assertNotIn("ref_test_queue", app.session_state)
        app.text_area(key="ref_video_link").set_value("视频分享 https://example.com/clip").run()
        app.button(key="ref_extract_link").click().run()
        self.assertEqual(app.session_state["ref_test_queue"]["kind"], "extract")
        self.assertTrue(app.session_state["ref_test_queue"]["operation"].endswith("._extract_source"))
        self.assertEqual(app.session_state["ref_test_queue"]["kwargs"]["link"], "视频分享 https://example.com/clip")
        self.assertEqual(app.text_area(key="ref_original_text").value, "")
        self.assertFalse(app.exception)

    def test_extract_adapter_uses_transcribed_content_and_retains_source(self):
        path = Path(self.directory.name) / "actual-reference.mp4"
        transcript = "这段来自已识别的真实音轨，保存参考资料时保留来源链接。"
        result = {"text": transcript, "media_path": str(path), "segments": [{"text": transcript, "start": 0, "end": 4}]}
        with patch.object(column.extract, "download_media", return_value=path) as download, patch.object(column.extract, "extract_media", return_value=result) as recognize:
            loaded = column._extract_source(link="https://example.com/clip", title="实际参考")
        download.assert_called_once_with("https://example.com/clip", progress=None)
        recognize.assert_called_once_with(path, language="zh", progress=None)
        reference = store.get_record("references", loaded["reference_id"])
        self.assertEqual(reference["text"], transcript)
        self.assertEqual(reference["source_url"], "https://example.com/clip")
        self.assertEqual(reference["metrics"], "")
        self.assertEqual(loaded["segments"], result["segments"])

    def test_saved_reference_has_no_generated_metrics_and_loads_through_queue(self):
        reference = topics.save_reference("事实参考", "已保存的参考正文，保留来源，不补造播放量或点赞数。", source_url="https://example.com/source")
        app = self.app()
        app.session_state["ref_learning_mode"] = "爆款文案"
        app.run()
        self.assertFalse(app.exception)
        app.button(key="ref_library_use").click().run()
        job = app.session_state["ref_test_queue"]
        self.assertEqual(job["kind"], "extract")
        self.assertEqual(job["kwargs"]["ident"], reference["id"])
        self.assertEqual(column._load_reference(reference["id"])["text"], reference["text"])
        self.assertFalse(app.exception)
        self.model.assert_not_called()

    def test_busy_queue_disables_editing_and_duplicate_requests(self):
        app = self.app()
        app.session_state["ref_test_busy"] = True
        app.run()
        self.assertTrue(app.text_area(key="ref_script_text").disabled)
        self.assertTrue(app.text_area(key="ref_original_text").disabled)
        self.assertTrue(app.button(key="ref_write_script").disabled)
        self.assertTrue(app.button(key="ref_open_video").disabled)


if __name__ == "__main__":
    unittest.main()
