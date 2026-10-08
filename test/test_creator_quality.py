import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.creator import extract, quality


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.folder = Path(self.directory.name)
        self.env = patch.dict(os.environ, {"MPT_CREATOR_DATA": str(self.folder / "data")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.directory.cleanup()

    def video(self, *, color="blue", silent=False, audio=True):
        target = self.folder / f"{color}-{silent}-{audio}.mp4"
        command = [extract.ffmpeg_binary(), "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                   f"color=c={color}:s=96x160:r=10:d=0.6"]
        if audio:
            sound = "anullsrc=r=24000:cl=mono" if silent else "sine=frequency=700:sample_rate=24000:duration=0.6"
            command.extend(["-f", "lavfi", "-i", sound, "-map", "0:v", "-map", "1:a", "-c:a", "aac"])
        command.extend(["-t", "0.6", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(target)])
        subprocess.run(command, check=True, capture_output=True, timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return target

    def test_real_complete_video_and_subtitles_pass(self):
        video = self.video()
        srt = self.folder / "字幕.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:00,500\n有效字幕\n", encoding="utf-8")
        report = quality.inspect_video(video, srt, expected_duration=0.6)
        self.assertTrue(report["pass"], report)
        self.assertGreater(report["peak_db"], -60)

    def test_corrupt_download_and_missing_audio_rejected(self):
        invalid = self.folder / "reference.mp4"
        invalid.write_text("<html>not video</html>", encoding="utf-8")
        self.assertFalse(quality.inspect_video(invalid)["pass"])
        report = quality.inspect_video(self.video(audio=False))
        self.assertFalse(report["pass"])
        self.assertTrue(any(item["name"] == "audio" and item["state"] == "failed" for item in report["checks"]))

    def test_silent_audio_and_full_black_frame_rejected(self):
        self.assertFalse(quality.inspect_video(self.video(silent=True))["pass"])
        self.assertFalse(quality.inspect_video(self.video(color="black"))["pass"])

    def test_duration_and_out_of_bounds_subtitle_rejected(self):
        video = self.video()
        self.assertFalse(quality.inspect_video(video, expected_duration=3)["pass"])
        self.assertFalse(quality.inspect_video(video, expected_duration=float("nan"))["pass"])
        srt = self.folder / "out.srt"
        srt.write_text("1\n00:00:01,000 --> 00:00:03,000\n越界\n", encoding="utf-8")
        self.assertFalse(quality.inspect_video(video, srt)["pass"])

    def test_decode_timeout_keeps_report_failed(self):
        video = self.video()
        info = {"path": str(video), "duration": 0.6, "width": 96, "height": 160, "has_video": True, "has_audio": True}
        with patch.object(quality.rendering, "probe_source", return_value=info), patch.object(
                quality.subprocess, "run", side_effect=subprocess.TimeoutExpired("ffmpeg", 60)):
            report = quality.inspect_video(video)
        self.assertFalse(report["pass"])


if __name__ == "__main__":
    unittest.main()
