from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import wave
from contextlib import closing
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from concurrent.futures import CancelledError
from app.services.creator import duix, store, voices


def wav_bytes():
    data = io.BytesIO()
    with wave.open(data, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 3200)
    return data.getvalue()


class CreatorVoicesDuixTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fusion = self.root / "fusion"
        self.fusion.mkdir()
        self.db = self.root / "biz.db"
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("CREATE TABLE f2f_model(id INTEGER,name TEXT,voice_id INTEGER,video_path TEXT)")
            db.execute("CREATE TABLE voice(id INTEGER,lang TEXT,asr_format_audio_url TEXT,reference_audio_text TEXT)")
            db.execute("INSERT INTO f2f_model VALUES(1,'测试数字人',2,'private/model.mp4')")
            db.execute("INSERT INTO voice VALUES(2,'zh','private/reference.wav','不应暴露的参考文案')")
        self.env = patch.dict(os.environ, {
            "MPT_CREATOR_DATA": str(self.root / "creator"),
            "MPT_FUSION_ROOT": str(self.fusion),
            "MPT_DUIX_DB": str(self.db),
        })
        self.env.start()
        self.docker = patch.object(duix, "_ensure_docker")
        self.docker.start()

    def tearDown(self):
        self.docker.stop()
        self.env.stop()
        self.temp.cleanup()

    def test_profile_read_only_and_no_private_reference(self):
        profiles = duix.list_profiles()
        self.assertEqual(profiles["models"], [{"id": 1, "name": "测试数字人", "voice_id": 2}])
        self.assertEqual(profiles["voices"][0]["id"], 2)
        exported = json.dumps(profiles)
        self.assertNotIn("reference", exported)
        self.assertNotIn("private", exported)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT reference_audio_text FROM voice").fetchone()[0], "不应暴露的参考文案")

    def test_local_http_connection_establishes_cookie_and_bypasses_proxy(self):
        cookies = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/health":
                    content = b'{"app":"HeyGemFusion"}'
                elif self.path == "/":
                    content = b"workbench"
                else:
                    cookies.append(self.headers.get("Cookie"))
                    if self.headers.get("Cookie") != "fusion_session=test-session":
                        self.send_response(403)
                        self.end_headers()
                        return
                    content = b'{"models":[],"voices":[]}'
                self.send_response(200)
                if self.path == "/":
                    self.send_header("Set-Cookie", "fusion_session=test-session; HttpOnly; SameSite=Strict")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        cfg = {"root": self.fusion, "port": server.server_port}
        try:
            with patch.object(duix, "_settings", return_value=cfg), patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "NO_PROXY": ""}):
                session, base, _ = duix._connect()
                try:
                    self.assertEqual(duix._api(session, base, "/api/bootstrap"), {"models": [], "voices": []})
                finally:
                    session.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(cookies, ["fusion_session=test-session"])

    def test_local_voice_requires_existing_id_and_does_not_train(self):
        profile = voices.save_voice("我的本机音色", "", provider="duix", duix_voice_id=2)
        self.assertEqual(profile["sample_path"], "")
        with patch.object(duix, "generate_audio", return_value={"audio_path": "real-result.wav"}) as generate:
            self.assertEqual(voices.preview_voice(profile["id"], "测试口播"), "real-result.wav")
            generate.assert_called_once_with("测试口播", 2, None)
        with self.assertRaises(ValueError):
            voices.save_voice("错误音色", "", provider="duix", duix_voice_id=999)
        with self.assertRaises(ValueError):
            voices.save_voice("错误音色", "a.wav", provider="duix", duix_voice_id=2)
        with self.assertRaises(ValueError):
            voices.preview_voice("../outside", "测试")

    def test_cloud_sample_is_persisted_and_cloning_uses_prompt(self):
        source = self.root / "sample.wav"
        source.write_bytes(wav_bytes())
        provider = types.ModuleType("app.services.voice")
        provider.VOXCPM_DEFAULT_VOICE = "default"
        provider.prepare_voxcpm_reference_audio = lambda data, suffix: data
        seen = {}

        def tts(**kwargs):
            seen.update(kwargs)
            Path(kwargs["voice_file"]).write_bytes(wav_bytes())
            return object()

        provider.voxcpm_tts = tts
        config_mod = types.ModuleType("app.config")
        config_mod.config = types.SimpleNamespace(voxcpm={"api_key": "test-only", "model_id": "test-model"})
        with patch.dict(sys.modules, {"app.services.voice": provider, "app.config": config_mod}), patch("app.services.voice", provider, create=True):
            profile = voices.save_voice("声音样本", source, "准确参考文案")
            source.unlink()
            self.assertTrue(Path(profile["sample_path"]).is_file())
            audio_path = voices.preview_voice(profile["id"], "新的配音文案")
        self.assertTrue(Path(audio_path).is_file())
        self.assertEqual(seen["reference_audio"], wav_bytes())
        self.assertEqual(seen["prompt_audio"], wav_bytes())
        self.assertEqual(seen["prompt_text"], "准确参考文案")
        self.assertNotIn("api_key", voices.list_voices()[0])

    def test_audio_file_is_validated_after_synthesis(self):
        broken = self.root / "broken.wav"
        broken.write_bytes(b"<html>not audio</html>")
        with self.assertRaises(ValueError):
            voices._wav_valid(broken)
        truncated = self.root / "truncated.wav"
        truncated.write_bytes(wav_bytes()[:-20])
        with self.assertRaises(ValueError):
            voices._wav_valid(truncated)

    def _remote_artifacts(self, remote_id):
        folder = self.fusion / "jobs" / remote_id
        folder.mkdir(parents=True)
        (folder / "narration.wav").write_bytes(wav_bytes())
        (folder / "subtitles.srt").write_text("1\n00:00:00,000 --> 00:00:00,200\n测试\n", "utf-8")
        (folder / "timeline.json").write_text('[{"start":0,"end":0.2,"text":"测试"}]', "utf-8")
        (folder / "final.mp4").write_bytes(b"test video artifact")

    def test_submit_poll_and_copy_result_to_creator_assets(self):
        remote_id = "a" * 32
        self._remote_artifacts(remote_id)
        session = types.SimpleNamespace(close=lambda: None)
        calls = []
        jobs = iter([[], [{"id": remote_id, "state": "running", "progress": 10, "stage": "配音"}], [{"id": remote_id, "state": "done", "progress": 100}]])

        def api(session, base, path, method="GET", **kwargs):
            calls.append((path, method, kwargs))
            return {"id": remote_id} if method == "POST" else next(jobs)

        with patch.object(duix, "_connect", return_value=(session, "http://127.0.0.1:18600", {"root": self.fusion})), patch.object(duix, "_api", side_effect=api), patch.object(duix.time, "sleep"):
            result = duix.generate("今天我们测试数字人。", 1, 2)
        for key in ("audio_path", "video_path", "srt_path", "timeline_path"):
            self.assertTrue(Path(result[key]).is_relative_to(store.data_root()))
            self.assertTrue(Path(result[key]).is_file())
        self.assertEqual(calls[1][2]["json"]["voice_id"], 2)
        self.assertFalse(calls[1][2]["json"]["audio_only"])
        self.assertEqual(store.list_records("duix_jobs")[0]["state"], "done")

    def test_clean_edit_source_keeps_preview_and_does_not_burn_captions(self):
        remote_id = "d" * 32
        self._remote_artifacts(remote_id)
        source = self.fusion / "jobs" / remote_id
        (source / "picture.mp4").write_bytes(b"uncaptioned video")
        executable = self.root / "fake-ffmpeg.exe"
        executable.write_bytes(b"placeholder")
        calls = []

        def mux(args, **kwargs):
            calls.append(args)
            Path(args[-1]).write_bytes(b"video with audio and no caption overlay")
            return types.SimpleNamespace(returncode=0)

        target = store.data_root() / "duix" / ("e" * 32)
        with patch.dict(os.environ, {"FFMPEG_BINARY": str(executable)}), patch.object(duix.subprocess, "run", side_effect=mux):
            result = duix._copy_results(self.fusion, remote_id, target, False)
        self.assertEqual(Path(result["video_path"]).read_bytes(), b"test video artifact")
        self.assertEqual(Path(result["clean_video_path"]).name, "clean.mp4")
        self.assertTrue(result["subtitles_burned"])
        self.assertEqual(calls[0][calls[0].index("-c:v") + 1], "copy")
        self.assertNotIn("-vf", calls[0])
        self.assertIn("1:a:0", calls[0])
        self.assertNotIn("edit_warning", result)

    def test_missing_picture_falls_back_to_finished_captioned_video(self):
        remote_id = "f" * 32
        self._remote_artifacts(remote_id)
        with patch.object(duix.subprocess, "run") as mux:
            result = duix._copy_results(self.fusion, remote_id, store.data_root() / "duix" / "fallback", False)
        mux.assert_not_called()
        self.assertEqual(result["clean_video_path"], "")
        self.assertTrue(result["subtitles_burned"])
        self.assertTrue(Path(result["video_path"]).is_file())
        self.assertIn("无字幕", result["edit_warning"])

    def test_mux_failure_does_not_make_failed_edit_source_look_ready(self):
        remote_id = "0" * 32
        self._remote_artifacts(remote_id)
        (self.fusion / "jobs" / remote_id / "picture.mp4").write_bytes(b"uncaptioned video")
        executable = self.root / "fake-ffmpeg.exe"
        executable.write_bytes(b"placeholder")
        with patch.dict(os.environ, {"FFMPEG_BINARY": str(executable)}), patch.object(duix.subprocess, "run", return_value=types.SimpleNamespace(returncode=1)):
            result = duix._copy_results(self.fusion, remote_id, store.data_root() / "duix" / "mux-failed", False)
        self.assertEqual(result["clean_video_path"], "")
        self.assertTrue(result["subtitles_burned"])
        self.assertIn("混流失败", result["edit_warning"])

    def test_available_ffmpeg_creates_real_uncaptioned_edit_video(self):
        executable = os.environ.get("FFMPEG_BINARY") or duix._settings()["ffmpeg"]
        if not Path(executable).is_file():
            self.skipTest("本机没有可用 FFmpeg，真实混流由安装验证覆盖")
        remote_id = "1" * 32
        self._remote_artifacts(remote_id)
        picture = self.fusion / "jobs" / remote_id / "picture.mp4"
        rendered = subprocess.run(
            [executable, "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=25", "-t", "0.2", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(picture)],
            capture_output=True, timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.assertEqual(rendered.returncode, 0, rendered.stderr.decode("utf-8", errors="replace"))
        with patch.dict(os.environ, {"FFMPEG_BINARY": executable}):
            result = duix._copy_results(self.fusion, remote_id, store.data_root() / "duix" / "actual-mux", False)
        self.assertTrue(result["clean_video_path"])
        self.assertEqual(Path(result["clean_video_path"]).read_bytes()[4:8], b"ftyp")
        decoded = subprocess.run(
            [executable, "-nostdin", "-v", "error", "-i", result["clean_video_path"], "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"],
            capture_output=True, timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.assertEqual(decoded.returncode, 0, decoded.stderr.decode("utf-8", errors="replace"))

    def test_cancellation_requests_remote_stop_and_keeps_identifier(self):
        remote_id = "b" * 32
        session = types.SimpleNamespace(close=lambda: None)
        responses = [[], {"id": remote_id}, [{"id": remote_id, "state": "running", "progress": 5}], {"ok": True}]
        with patch.object(duix, "_connect", return_value=(session, "local", {"root": self.fusion})), patch.object(duix, "_api", side_effect=responses) as api:
            with self.assertRaises(CancelledError):
                duix.generate("测试取消。", 1, 2, progress=lambda message, percent: False)
        self.assertEqual(api.call_args.args[2], f"/api/jobs/{remote_id}/cancel")
        row = store.list_records("duix_jobs")[0]
        self.assertEqual(row["state"], "cancelled")
        self.assertEqual(row["fusion_id"], remote_id)

    def test_failure_preserves_error_and_active_jobs_are_not_duplicated(self):
        session = types.SimpleNamespace(close=lambda: None)
        with patch.object(duix, "_connect", return_value=(session, "local", {"root": self.fusion})), patch.object(duix, "_api", return_value=[{"state": "running"}]) as api:
            with self.assertRaisesRegex(RuntimeError, "已有 Duix"):
                duix.generate("不能重复。", 1, 2)
        api.assert_called_once()
        self.assertEqual(store.list_records("duix_jobs")[0]["state"], "failed")

    def test_remote_failure_is_not_reported_as_success(self):
        remote_id = "c" * 32
        session = types.SimpleNamespace(close=lambda: None)
        responses = [[], {"id": remote_id}, [{"id": remote_id, "state": "failed", "error": "语音服务未就绪"}], {"ok": True}]
        with patch.object(duix, "_connect", return_value=(session, "local", {"root": self.fusion})), patch.object(duix, "_api", side_effect=responses):
            with self.assertRaisesRegex(RuntimeError, "语音服务未就绪"):
                duix.generate("测试失败。", 1, 2)
        row = store.list_records("duix_jobs")[0]
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["fusion_id"], remote_id)
        self.assertNotIn("video_path", row)

    def test_long_script_is_split_and_invalid_ids_fail_before_service(self):
        segments = duix._split_script("这是一句比较长的数字人文案，需要按逗号拆开，每个片段都要足够短。" * 3)
        self.assertTrue(all(0 < len(row["text"]) <= 24 for row in segments))
        with patch.object(duix, "_connect") as connect:
            for invalid in ("../path", True, -1, "1 OR 1=1"):
                with self.assertRaises(ValueError):
                    duix.generate("测试。", invalid, 2)
            connect.assert_not_called()
        with self.assertRaises(RuntimeError):
            duix._copy_results(self.fusion, "../outside", self.root / "artifacts", False)


if __name__ == "__main__":
    unittest.main()
