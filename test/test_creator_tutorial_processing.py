"""Real media checks for pause cutting plus conservative option contracts."""
import hashlib
import math
import os
from pathlib import Path
import struct
import tempfile
import unittest
import wave
from unittest.mock import patch

from app.services.creator import extract, hyperframes, pacing, processing, rendering
from webui.creator_reference_release import _export_bundle


class TutorialProcessingTests(unittest.TestCase):
    def test_invalid_knobs_and_oversized_timeline_are_rejected(self):
        for options in ({"silence_threshold": float("nan")}, {"silence_trim": "yes"},
                        {"green_color": "green;movie=x"}, {"output_resolution": "4KP"},
                        {"highlight_keywords": {"main": ["x"]*13}}, {"cover_aspect": "bad"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                processing.normalize(options)

    def test_output_dimensions_and_real_html_keyword_escaping(self):
        self.assertEqual(processing.dimensions("9:16", "576P"), (576, 1024))
        self.assertEqual(processing.dimensions("16:9", "1080P"), (1920, 1080))
        result = hyperframes.highlighted_text('门店 <script>立即到店</script>', {"main": ["门店"], "action": ["到店"]})
        self.assertIn('class="hl-main"', result)
        self.assertIn('class="hl-action"', result)
        self.assertNotIn("<script>", result)
        self.assertIn("&lt;script&gt;", result)

    def test_overlay_and_caption_use_same_edit_timeline(self):
        timeline = [{"source_start": 0, "source_end": 1, "target_start": 0, "target_end": 1},
                    {"source_start": 2, "source_end": 3, "target_start": 1, "target_end": 2}]
        item = {"path": "a.png", "start": .5, "end": 2.5}
        self.assertEqual(processing.remap_pip([item], timeline)[0]["end"], 1.5)
        row = pacing.remap_segments([{"start": .5, "end": 2.5, "text": "完整一句"}], timeline)[0]
        self.assertEqual((row["start"], row["end"], row["text"]), (.5, 1.5, "完整一句"))

    def test_real_pause_cut_preserves_source_and_voice_alignment(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            audio, video = root / "original.wav", root / "original.mp4"
            rate = 16000
            with wave.open(str(audio), "wb") as file:
                file.setparams((1, 2, rate, 0, "NONE", "not compressed"))
                file.writeframes(b"".join(struct.pack("<h", int(8000*math.sin(2*math.pi*440*i/rate))
                                                       if .2 <= i/rate < .8 or 1.8 <= i/rate < 2.4 else 0) for i in range(rate*3)))
            response = extract._run([extract.ffmpeg_binary(), "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
                                     "color=c=green:s=160x280:d=3:r=30", "-i", str(audio), "-c:v", "libx264", "-c:a", "aac", "-shortest", str(video)])
            self.assertEqual(response.returncode, 0, response.stderr)
            source_hash = hashlib.sha256(video.read_bytes()).hexdigest()
            rows = [{"start": .2, "end": .8, "text": "第一句"}, {"start": 1.8, "end": 2.4, "text": "第二句"}]
            result = pacing.prepare_media(video, audio, None, rows, root / "edited", enabled=True)
            self.assertGreater(result["removed_seconds"], .6)
            self.assertLess(result["removed_seconds"], 1.8)
            self.assertEqual(hashlib.sha256(video.read_bytes()).hexdigest(), source_hash)
            sound = rendering.probe_source(result["audio_path"])
            frames = rendering.probe_source(result["video_path"])
            self.assertLess(abs(sound["duration"]-frames["duration"]), .12)
            self.assertEqual([row["text"] for row in result["segments"]], ["第一句", "第二句"])
            self.assertLessEqual(result["segments"][-1]["end"], result["duration"]+.01)

    def test_export_bundle_contains_only_selected_media_and_copy(self):
        import io
        import zipfile
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "owned.mp4"
            video.write_bytes(b"specific media bytes")
            with patch.dict(os.environ, {"API_KEY": "private-secret"}):
                data = _export_bundle(str(video), None, "标题", "说明", "#标签")
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                self.assertEqual(archive.read("成片.mp4"), b"specific media bytes")
                self.assertNotIn(b"private-secret", data)
