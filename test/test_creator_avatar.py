from __future__ import annotations

import io
import json
import math
import os
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import threading
import types
import unittest
import wave
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from app.services.creator import avatar, duix, extract, store


def audio_bytes(seconds=0.4, rate=24000):
    buffer = io.BytesIO()
    # A changing waveform exposes inserted silence and dropped audio frames.
    frames = b"".join(struct.pack("<h", round(6000 * math.sin(index * 2 * math.pi * 440 / rate)))
                      for index in range(round(seconds * rate)))
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(frames)
    return buffer.getvalue()


class CreatorAvatarTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fusion = self.root / "fusion"
        self.shared = self.root / "shared"
        self.fusion.mkdir()
        self.shared.mkdir()
        self.database = self.root / "duix.db"
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("CREATE TABLE f2f_model(id INTEGER,name TEXT,video_path TEXT)")
            db.execute("CREATE TABLE video(id INTEGER,status TEXT)")
            db.execute("CREATE TABLE voice(id INTEGER,reference_audio_text TEXT)")
            db.execute("INSERT INTO f2f_model VALUES(1,'人物一','reference.mp4')")
            db.execute("INSERT INTO voice VALUES(1,'不应读取或修改的样音文案')")
        self.cfg = {"root": self.fusion, "avatar_root": self.shared, "hey_db": str(self.database),
                    "avatar_url": "http://127.0.0.1:8383/easy"}
        self.config_patch = patch.object(avatar, "_configuration", return_value=self.cfg)
        self.config_patch.start()
        self.env = patch.dict(os.environ, {"MPT_CREATOR_DATA": str(self.root / "creator")})
        self.env.start()
        avatar._STATUS_CACHE.clear()

    def tearDown(self):
        self.env.stop()
        self.config_patch.stop()
        self.temp.cleanup()
        avatar._STATUS_CACHE.clear()

    def _reference(self):
        path = self.shared / "reference.mp4"
        path.write_bytes(b"test-only video placeholder")
        return path

    def test_profile_list_is_read_only_and_rejects_paths_outside_shared_directory(self):
        self._reference()
        outside = self.root / "outside.mp4"
        outside.write_bytes(b"outside")
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("INSERT INTO f2f_model VALUES(2,'越界人物','../outside.mp4')")
        before = self.database.read_bytes()
        profiles = avatar.list_options()
        self.assertEqual(profiles[0]["id"], "duix:1")
        self.assertTrue(profiles[0]["available"])
        self.assertFalse(profiles[1]["available"])
        self.assertEqual(profiles[1]["video_path"], "")
        self.assertNotIn("样音文案", json.dumps(profiles, ensure_ascii=False))
        self.assertEqual(self.database.read_bytes(), before)

    def test_status_does_not_start_services_and_caches_slow_docker_probe(self):
        self._reference()
        with patch.object(avatar, "_service_states", return_value={name: False for name in avatar._CONTAINERS}) as states, \
                patch.object(duix, "_engine_busy", return_value=False), patch.object(avatar, "_set_running") as mutate:
            first, second = avatar.status(), avatar.status()
        self.assertTrue(first["available"])
        self.assertEqual(first, second)
        states.assert_called_once()
        mutate.assert_not_called()

    def test_status_reports_engine_failure_as_docker_not_ffmpeg(self):
        self._reference()
        with patch.object(avatar, "_service_states", side_effect=RuntimeError("Docker Desktop 引擎未就绪")):
            result = avatar.status()
        self.assertFalse(result["available"])
        self.assertIn("Docker", result["reason"])
        self.assertNotIn("FFmpeg", result["reason"])

    def test_native_engine_is_the_only_required_container(self):
        container = "duix-avatar-gen-video"
        output = json.dumps([{"Name": "/" + container, "State": {"Running": False}}])
        with patch.object(avatar, "_docker", side_effect=[container + "\n", output]) as docker:
            self.assertEqual(avatar._service_states(), {container: False})
        self.assertEqual(docker.call_args_list[1].args[0], ["inspect", container])

    def test_absent_optional_speech_container_is_never_started_or_stopped(self):
        with patch.object(avatar, "_service_states", return_value={"duix-avatar-gen-video": True}), \
                patch.object(avatar, "_docker") as docker:
            avatar._set_running("duix-avatar-tts", False)
            avatar._set_running("duix-avatar-asr", False)
        docker.assert_not_called()

    def test_running_docker_with_missing_engine_reports_the_distinct_missing_dependency(self):
        self._reference()
        with patch.object(avatar, "_docker", return_value=""):
            result = avatar.status()
        self.assertTrue(result["docker_ready"])
        self.assertFalse(result["available"])
        self.assertIn("尚未安装数字人口型容器", result["reason"])

    def test_gpu_lease_excludes_parallel_jobs_and_restores_original_states(self):
        states = {name: index == 0 for index, name in enumerate(avatar._CONTAINERS)}
        calls = []
        def change(name, running):
            calls.append((name, running))
        with patch.object(avatar, "_service_states", return_value=states), patch.object(avatar, "_set_running", side_effect=change):
            with self.assertRaisesRegex(RuntimeError, "模拟失败"):
                with avatar._gpu_lease(self.cfg, "a" * 32):
                    self.assertTrue((self.fusion / "service-lease.json").is_file())
                    with self.assertRaisesRegex(RuntimeError, "占用"):
                        with avatar._gpu_lease(self.cfg, "b" * 32):
                            self.fail("parallel GPU job must be rejected")
                    raise RuntimeError("模拟失败")
        self.assertEqual(calls, list(states.items()))
        self.assertFalse((self.fusion / "service-lease.json").exists())
        self.assertFalse(avatar._PROCESS_LOCK.locked())

    def test_existing_recovery_lease_is_preserved_without_any_container_changes(self):
        path = self.fusion / "service-lease.json"
        path.write_text('{"job":"previous","initial":{}}', "utf-8")
        before = path.read_bytes()
        with patch.object(avatar, "_service_states") as inspect, patch.object(avatar, "_set_running") as change:
            with self.assertRaisesRegex(RuntimeError, "恢复未完成"):
                with avatar._gpu_lease(self.cfg, "a" * 32):
                    self.fail("unrecovered services must block new generation")
        inspect.assert_not_called()
        change.assert_not_called()
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(avatar._PROCESS_LOCK.locked())

    def test_failed_restoration_preserves_fusion_compatible_recovery_information(self):
        states = {name: False for name in avatar._CONTAINERS}
        with patch.object(avatar, "_service_states", return_value=states), \
                patch.object(avatar, "_set_running", side_effect=RuntimeError("容器无法恢复")):
            with avatar._gpu_lease(self.cfg, "a" * 32) as warnings:
                pass
        data = json.loads((self.fusion / "service-lease.json").read_text("utf-8"))
        self.assertEqual(data["initial"], states)
        self.assertEqual(data["job"], "a" * 32)
        self.assertEqual(len(warnings), 3)
        self.assertFalse(avatar._PROCESS_LOCK.locked())

    def test_only_avatar_container_uses_separate_recovery_record_and_restores_only_existing_services(self):
        states = {"duix-avatar-gen-video": False}
        with patch.object(avatar, "_service_states", return_value=states), patch.object(avatar, "_set_running") as change:
            with avatar._gpu_lease(self.cfg, "a" * 32):
                self.assertFalse((self.fusion / "service-lease.json").exists())
                data = json.loads((self.fusion / "avatar-service-lease.json").read_text("utf-8"))
                self.assertEqual(data["initial"], states)
        change.assert_called_once_with("duix-avatar-gen-video", False)
        self.assertFalse((self.fusion / "avatar-service-lease.json").exists())

    def test_interrupted_owned_lease_is_restored_under_lock_before_new_task(self):
        states = {"duix-avatar-gen-video": True}
        ident = "a" * 32
        store.save_record("avatar_jobs", ident, {"state": "running"})
        (self.fusion / "avatar-service-lease.json").write_text(json.dumps({"owner": "mpt_avatar", "job": ident, "initial": states}), "utf-8")
        with patch.object(avatar, "_service_states", return_value=states), patch.object(avatar, "_set_running") as change:
            with avatar._gpu_lease(self.cfg, "b" * 32):
                current = json.loads((self.fusion / "avatar-service-lease.json").read_text("utf-8"))
                self.assertEqual(current["job"], "b" * 32)
        self.assertEqual(change.call_args_list[0].args, ("duix-avatar-gen-video", False))
        self.assertEqual(change.call_args_list[1].args, ("duix-avatar-gen-video", True))
        self.assertEqual(store.get_record("avatar_jobs", ident)["state"], "failed")
        self.assertFalse((self.fusion / "avatar-service-lease.json").exists())

    def test_owned_recovery_file_with_untrusted_container_names_is_left_untouched(self):
        path = self.fusion / "avatar-service-lease.json"
        path.write_text(json.dumps({"owner": "mpt_avatar", "job": "a" * 32,
                                    "initial": {"duix-avatar-gen-video": False, "another-project": True}}), "utf-8")
        before = path.read_bytes()
        with patch.object(avatar, "_set_running") as change:
            with self.assertRaisesRegex(RuntimeError, "恢复记录异常"):
                with avatar._gpu_lease(self.cfg, "b" * 32):
                    self.fail("unknown service must never be modified")
        change.assert_not_called()
        self.assertEqual(path.read_bytes(), before)

    def test_recovery_needed_status_exposes_button_without_mutating_any_service(self):
        self._reference()
        states = {"duix-avatar-gen-video": False}
        (self.fusion / "avatar-service-lease.json").write_text(json.dumps({"owner": "mpt_avatar", "job": "a" * 32, "initial": states}), "utf-8")
        with patch.object(avatar, "_service_states", return_value=states), patch.object(avatar, "_set_running") as change:
            result = avatar.status()
        self.assertTrue(result["recovery_needed"])
        self.assertFalse(result["available"])
        self.assertIn("恢复", result["reason"])
        change.assert_not_called()

    def test_public_recovery_restores_owned_fusion_record_and_never_calls_speech_generation(self):
        states = {name: False for name in avatar._CONTAINERS}
        (self.fusion / "service-lease.json").write_text(json.dumps({"owner": "mpt_avatar", "job": "a" * 32, "initial": states}), "utf-8")
        with patch.object(avatar, "_service_states", return_value=states), patch.object(avatar, "_set_running") as change, \
                patch.object(duix, "generate_audio", side_effect=AssertionError("recovery must not synthesize speech")):
            result = avatar.recover_engine()
        self.assertEqual(result["state"], "done")
        self.assertFalse((self.fusion / "service-lease.json").exists())
        self.assertEqual(change.call_args_list[0].args, ("duix-avatar-gen-video", False))
        self.assertFalse(avatar._PROCESS_LOCK.locked())

    def test_public_recovery_refuses_foreign_fusion_record_before_container_mutation(self):
        path = self.fusion / "service-lease.json"
        path.write_text(json.dumps({"job": "a" * 32, "initial": {"duix-avatar-gen-video": False}}), "utf-8")
        before = path.read_bytes()
        with patch.object(avatar, "_service_states") as states, patch.object(avatar, "_set_running") as change:
            with self.assertRaisesRegex(RuntimeError, "其他融影任务"):
                avatar.recover_engine()
        states.assert_not_called()
        change.assert_not_called()
        self.assertEqual(path.read_bytes(), before)

    def test_malformed_recovery_json_cannot_escape_friendly_validation(self):
        (self.fusion / "avatar-service-lease.json").write_text("[]", "utf-8")
        with self.assertRaisesRegex(RuntimeError, "恢复记录异常"):
            avatar._owned_recovery(self.cfg)

    def test_audio_chunks_preserve_every_original_pcm_frame_without_sentence_silence(self):
        source = self.root / "long.wav"
        source.write_bytes(audio_bytes(17.1, 44100))
        chunks = avatar._audio_chunks(source, self.root)
        self.assertEqual(len(chunks), 3)
        collected = b""
        for chunk in chunks:
            with wave.open(str(chunk["path"]), "rb") as audio:
                collected += audio.readframes(audio.getnframes())
        with wave.open(str(source), "rb") as audio:
            self.assertEqual(collected, audio.readframes(audio.getnframes()))
        self.assertEqual([chunk["start"] for chunk in chunks], [0, 8, 16])
        self.assertAlmostEqual(sum(chunk["duration"] for chunk in chunks), 17.1)

    def test_truncated_wav_is_rejected_before_speech_or_gpu_activity(self):
        source = self.root / "truncated.wav"
        source.write_bytes(audio_bytes()[:-16])
        with patch.object(avatar, "_run_media") as conversion:
            with self.assertRaisesRegex(ValueError, "不完整"):
                avatar._prepare_audio(source, self.root)
        conversion.assert_not_called()

    def test_photo_import_is_rejected_without_creating_fake_avatar_or_mutating_database(self):
        photo = self.root / "photo.png"
        photo.write_bytes(b"photo")
        before = self.database.read_bytes()
        with self.assertRaisesRegex(ValueError, "图片数字人尚未接入"):
            avatar.import_profile("照片人物", photo)
        self.assertFalse(store.list_records("avatar_profiles"))
        self.assertEqual(self.database.read_bytes(), before)

    def test_native_response_cannot_copy_files_from_outside_shared_directory(self):
        source = self.root / "audio.wav"
        source.write_bytes(audio_bytes())
        self._reference()
        response = types.SimpleNamespace(raise_for_status=lambda: None)
        response.json = lambda: {"code": 10000}
        result = types.SimpleNamespace(raise_for_status=lambda: None,
                                      json=lambda: {"code": 10000, "data": {"status": 2, "result": "../outside.mp4"}})
        session = types.SimpleNamespace(post=lambda *a, **kw: response, get=lambda *a, **kw: result)
        with self.assertRaisesRegex(RuntimeError, "目录外"):
            avatar._native_clip(session, self.cfg, {"path": source}, self.shared / "reference.mp4", self.root, "a" * 32, 0, None)
        self.assertFalse((self.root / "avatar-00.mp4").exists())

    def test_wrong_narration_version_cannot_be_silently_substituted(self):
        self._reference()
        source = self.root / "audio.wav"
        source.write_bytes(audio_bytes())
        ident = "a" * 32
        store.save_record("narrations", ident, {"state": "done", "preview": False, "audio_path": str(self.root / "other.wav")})
        with patch.object(avatar, "_video_info", return_value=1), patch.object(avatar, "_gpu_lease") as lease:
            with self.assertRaisesRegex(ValueError, "正式配音"):
                avatar.generate(source, "duix:1", source_narration_id=ident)
        lease.assert_not_called()


class CreatorAvatarMediaTest(unittest.TestCase):
    """Real FFmpeg media checks; native inference is a bounded local mock API."""
    def setUp(self):
        CreatorAvatarTest.setUp(self)
        try:
            self.ffmpeg = extract.ffmpeg_binary()
        except RuntimeError:
            self.skipTest("本机没有可用 FFmpeg")
        self.ffmpeg_env = patch.dict(os.environ, {"FFMPEG_BINARY": self.ffmpeg})
        self.ffmpeg_env.start()

    def tearDown(self):
        if hasattr(self, "ffmpeg_env"):
            self.ffmpeg_env.stop()
        CreatorAvatarTest.tearDown(self)

    def _reference(self):
        path = self.shared / "reference.mp4"
        command = [self.ffmpeg, "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                   "color=c=blue:s=160x240:r=25", "-t", "0.4", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
        result = subprocess.run(command, capture_output=True, timeout=20,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stderr)
        return path

    def test_import_decodes_video_keeps_database_read_only_and_removes_source_audio(self):
        reference = self._reference()
        source = self.root / "uploaded.mp4"
        shutil.copy2(reference, source)
        before = self.database.read_bytes()
        profile = avatar.import_profile("上传人物", source)
        self.assertTrue(profile["id"].startswith("local:"))
        self.assertTrue(Path(profile["video_path"]).is_relative_to(self.shared))
        self.assertGreater(profile["duration"], 0)
        self.assertEqual(self.database.read_bytes(), before)
        self.assertEqual(len(store.list_records("avatar_profiles")), 1)
        with self.assertRaisesRegex(RuntimeError, "没有音轨"):
            extract.probe_media(profile["video_path"])

    def test_audio_disguised_as_mp4_cannot_be_saved_as_a_ready_person(self):
        source = self.root / "fake.mp4"
        source.write_bytes(audio_bytes())
        with self.assertRaisesRegex(RuntimeError, "损坏|格式"):
            avatar.import_profile("错误人物", source)
        self.assertFalse(store.list_records("avatar_profiles"))

    def test_entire_audio_drives_native_api_once_per_chunk_and_survives_final_mux(self):
        reference = self._reference()
        source = self.root / "selected.wav"
        original_bytes = audio_bytes(9.25)
        source.write_bytes(original_bytes)
        ident = "a" * 32
        store.save_record("narrations", ident, {"state": "done", "preview": False, "audio_path": str(source)})
        received = []
        shared = self.shared
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                received.append(body)
                # The mock proves file-based protocol and composition, not lips.
                with wave.open(str(shared / body["audio_url"]), "rb") as audio:
                    body["tested_audio_seconds"] = audio.getnframes() / audio.getframerate()
                self._send({"code": 10000})
            def do_GET(self):
                self._send({"code": 10000, "data": {"status": 2, "result": "/reference.mp4"}})
            def _send(self, data):
                encoded = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.cfg["avatar_url"] = f"http://127.0.0.1:{server.server_port}/easy"
        changes = []
        try:
            with patch.object(avatar, "_service_states", return_value={name: False for name in avatar._CONTAINERS}), \
                    patch.object(avatar, "_set_running", side_effect=lambda name, running: changes.append((name, running))), \
                    patch.object(duix, "generate_audio", side_effect=AssertionError("must not synthesize speech")):
                result = avatar.generate(source, "duix:1", script="使用这条已经生成的配音。", source_narration_id=ident)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(result["state"], "done")
        self.assertFalse(result["subtitles_burned"])
        self.assertEqual(result["source_narration_id"], ident)
        self.assertEqual(result["source_audio_name"], "selected.wav")
        self.assertEqual(Path(result["original_audio_path"]).read_bytes(), original_bytes)
        self.assertEqual(source.read_bytes(), original_bytes)
        self.assertEqual(len(received), 2)
        self.assertEqual([body["tested_audio_seconds"] for body in received], [8, 1.25])
        self.assertTrue(all("voice_id" not in body and "text" not in body for body in received))
        self.assertAlmostEqual(avatar._wav_duration(Path(result["audio_path"])), 9.25, places=5)
        info = extract.probe_media(result["video_path"])
        self.assertAlmostEqual(info["duration"], 9.25, delta=0.08)
        self.assertFalse((self.fusion / "service-lease.json").exists())
        self.assertFalse(list(self.shared.glob("mpt-avatar-*.wav")))
        self.assertEqual(changes[-3:], [(name, False) for name in avatar._CONTAINERS])

    def test_native_failure_restores_leased_services_and_preserves_selected_audio_for_retry(self):
        self._reference()
        source = self.root / "selected.wav"
        content = audio_bytes()
        source.write_bytes(content)
        initial = {"duix-avatar-gen-video": False}
        changes = []
        with patch.object(avatar, "_service_states", return_value=initial), \
                patch.object(avatar, "_set_running", side_effect=lambda name, running: changes.append((name, running))), \
                patch.object(avatar, "_wait_native"), patch.object(avatar, "_native_clip", side_effect=RuntimeError("数字人口型生成失败")), \
                patch.object(duix, "generate_audio", side_effect=AssertionError("must not synthesize speech")):
            with self.assertRaisesRegex(RuntimeError, "口型生成失败"):
                avatar.generate(source, "duix:1")
        result = store.list_records("avatar_jobs")[0]
        self.assertEqual(result["state"], "failed")
        self.assertTrue(Path(result["original_audio_path"]).is_file())
        self.assertEqual(source.read_bytes(), content)
        self.assertNotIn("video_path", result)
        self.assertNotIn("FFmpeg", result["error"])
        self.assertFalse((self.fusion / "avatar-service-lease.json").exists())
        self.assertEqual(changes[-2:], [("duix-avatar-gen-video", False)] * 2)


if __name__ == "__main__":
    unittest.main()
