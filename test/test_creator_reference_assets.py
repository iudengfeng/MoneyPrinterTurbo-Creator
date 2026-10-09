"""Voice recording and failed local sample preparation remain recoverable."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from webui import creator_reference_assets as assets


class ReferenceAssetsTests(unittest.TestCase):
    def test_recording_input_is_available_without_starting_a_provider(self):
        app_text = '''import streamlit as st
from webui.creator_reference_assets import voice_browser
class Context:
    busy = False
    project = {}
    def current_audio(self): return ""
    def queue(self,*args,**kwargs): raise AssertionError("No automatic provider call")
voice_browser(Context())
'''
        with patch.object(assets.narration, "list_options", return_value=[]):
            app = AppTest.from_string(app_text, default_timeout=30).run()
        self.assertFalse(app.exception)
        self.assertEqual(len(app.get("audio_input")), 1)
        self.assertTrue(app.button(key="ref_clone_save").disabled)

    def test_incomplete_cached_sample_is_rebuilt_without_modifying_original(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "original.mp4"
            source.write_bytes(b"original owned data")
            target = source.with_name(source.stem + "-sample.wav")
            target.write_bytes(b"incomplete cache")
            class Context:
                def stage_upload(self, upload): return str(source)
            def rebuild(command):
                Path(command[-1]).write_bytes(b"valid fixture wav")
                return SimpleNamespace(returncode=0)
            with (patch.object(assets.extract, "probe_media"),
                  patch.object(assets.extract, "_run", side_effect=rebuild) as run,
                  patch.object(assets.voices, "_wav_valid", side_effect=[ValueError("partial"), None])):
                self.assertEqual(assets._voice_sample(Context(), object()), str(target))
            run.assert_called_once()
            self.assertEqual(source.read_bytes(), b"original owned data")
