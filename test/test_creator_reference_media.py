"""Imported speech timing stays usable without re-running ASR or voice providers."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from webui import creator_reference_media as media
from webui.creator_reference_state import _media_stamp


class ReferenceMediaTests(unittest.TestCase):
    def test_imported_timeline_is_available_only_for_its_unchanged_video(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"owned fixture")
            class Context:
                project = {"id": "same-work", "config": {"source_video_path": str(source)}}
                def stage(self, name):
                    return {}
            rows = [{"start": 0., "end": 2., "text": "这段原片的真实识别正文"}]
            cached = {"project_id": "same-work", "media_stamp": _media_stamp(source), "duration": 2., "segments": rows}
            with patch.object(media.st, "session_state", {"ref_imported_transcript": cached}):
                self.assertEqual(media.timed_rows(Context()), rows)
                other = Context()
                other.project = deepcopy(Context.project)
                other.project["id"] = "another-work"
                self.assertEqual(media.timed_rows(other), [])
                source.write_bytes(b"changed source fixture")
                self.assertEqual(media.timed_rows(Context()), [])

    def test_saved_voice_timing_has_priority_and_invalid_rows_are_ignored(self):
        class Context:
            project = {"id": "same-work", "config": {}}
            def stage(self, name):
                return {"duration": 3., "segments": [{"start": 1., "end": 2., "text": "有效正文"},
                                                    {"start": float("nan"), "end": 2., "text": "无效"}]}
        with patch.object(media.st, "session_state", {}):
            self.assertEqual([row["text"] for row in media.timed_rows(Context())], ["有效正文"])
