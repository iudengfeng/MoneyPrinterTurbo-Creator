"""Exercise persistence/recovery and billing boundaries independently of engines."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.creator import avatar_mixed_generation, brand_profiles, composition, extract, narration, planning, quality, release_assets, rendering, topics, workflow


class CreatorWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"MPT_CREATOR_DATA": str(self.root / "creator")})
        self.env.start()
        self.calls = {key: 0 for key in ("tts", "asr", "plan", "compose", "render", "quality", "cover", "package", "llm")}
        self.planned_briefs = []
        self.llm_requests = []
        self.patches = []
        for module, name, implementation in (
            (narration, "generate", self.tts), (extract, "extract_media", self.asr),
            (planning, "build_plan", self.plan), (composition, "compose", self.compose),
            (rendering, "render_video", self.render), (quality, "inspect_video", self.quality),
            (release_assets, "generate_cover", self.cover), (release_assets, "save_materials", self.package),
            (topics, "generate_draft", self.llm),
        ):
            mocked = patch.object(module, name, side_effect=implementation)
            mocked.start()
            self.patches.append(mocked)

    def tearDown(self):
        for mocked in reversed(self.patches):
            mocked.stop()
        for key, handle in list(workflow._OWNER_HANDLES.items()):
            if key.startswith(str(self.root)):
                handle.close()
                del workflow._OWNER_HANDLES[key]
        self.env.stop()
        self.temp.cleanup()

    def file(self, name):
        path = self.root / name
        path.write_bytes(b"complete test artifact")
        return str(path)

    def tts(self, text, voice_id, **kwargs):
        self.calls["tts"] += 1
        return {"audio_path": self.file(f"audio-{self.calls['tts']}.wav"), "duration": 2.0, "provider": "edge"}

    def asr(self, path, **kwargs):
        self.calls["asr"] += 1
        srt = Path(self.file(f"subs-{self.calls['asr']}.srt"))
        srt.write_text("1\n00:00:00,000 --> 00:00:02,000\n测试口播。\n", "utf-8")
        return {"srt_path": str(srt), "segments": [{"start": 0.0, "end": 2.0, "text": "测试口播。"}]}

    def plan(self, script, entries, materials, **kwargs):
        self.calls["plan"] += 1
        self.planned_briefs.append(kwargs.get("video_brief"))
        self.assertEqual(entries[0]["end"], 2.0)
        return {"segments": entries, "duration": kwargs["duration"], "aspect": kwargs["aspect"]}

    def compose(self, plan, audio, folder, **kwargs):
        self.calls["compose"] += 1
        return {"video_path": self.file(f"base-{self.calls['compose']}.mp4"), "duration": 2.0}

    def render(self, video, **kwargs):
        self.calls["render"] += 1
        self.assertTrue(Path(kwargs["audio_path"]).is_file())
        return {"video_path": self.file(f"final-{self.calls['render']}.mp4"), "srt_path": kwargs["subtitle_path"] or "", "duration": 2.0}

    def quality(self, video, **kwargs):
        self.calls["quality"] += 1
        self.assertEqual(kwargs["expected_duration"], 2.0)
        return {"pass": True, "checks": [], "errors": [], "warnings": []}

    def cover(self, video, title, **kwargs):
        self.calls["cover"] += 1
        return {"cover_path": self.file(f"cover-{self.calls['cover']}.png"), "title": title}

    def package(self, video, title, **kwargs):
        self.calls["package"] += 1
        return {"id": f"package-{self.calls['package']}", "video_path": video, "title": title, "cover_path": kwargs["cover_path"]}

    def llm(self, title, **kwargs):
        self.calls["llm"] += 1
        self.llm_requests.append({"title": title, **kwargs})
        return {"text": "这是模型生成的实际口播。"}

    def project(self, **changes):
        return workflow.create_project(dict(input_text="测试口播。第二句话。", **changes))["id"]

    def test_full_pipeline_and_guided_mode_reuse_same_persistent_results(self):
        ident = self.project()
        first = workflow.run_project(ident, until_stage="script")
        self.assertEqual(first["state"], "paused")
        self.assertEqual(self.calls["tts"], 0)
        second = workflow.run_project(ident, until_stage="voice")
        self.assertEqual(second["state"], "paused")
        final = workflow.run_project(ident)
        self.assertEqual(final["state"], "done")
        self.assertTrue(all(saved["state"] == "done" for saved in final["stages"].values()))
        self.assertEqual(final["result"]["title"], "测试口播")
        self.assertTrue(Path(final["result"]["video_path"]).is_file())
        counts = dict(self.calls)
        self.assertEqual(workflow.run_project(ident)["state"], "done")
        self.assertEqual(self.calls, counts)
        self.assertEqual(workflow.get_project(ident)["request_snapshot"]["input_text"], "测试口播。第二句话。")

    def test_brand_snapshot_is_fixed_until_explicit_local_refresh(self):
        brand = brand_profiles.save_profile({"name": "客户品牌", "offering": "原商品介绍", "audience": "本地家庭"})
        ident = self.project(input_mode="topic", allow_paid=True, video_purpose="product_service",
                             content_template="product_intro", target_duration=60, brand_profile_id=brand["id"])
        workflow.run_project(ident)
        old_snapshot = workflow.get_project(ident)["config"]["brand_snapshot"]
        counts = dict(self.calls)
        updated_brand = brand_profiles.save_profile({"offering": "明确更新后的商品"}, id=brand["id"])
        saved = workflow.update_project(ident, {"release_title": "改封面标题"})
        self.assertEqual(saved["config"]["brand_snapshot"], old_snapshot)
        workflow.run_project(ident)
        self.assertEqual((self.calls["llm"], self.calls["tts"], self.calls["asr"]),
                         (counts["llm"], counts["tts"], counts["asr"]))
        saved = workflow.update_project(ident, {"brand_snapshot": brand_profiles.snapshot(brand["id"])})
        self.assertEqual(saved["config"]["brand_snapshot"]["version"], updated_brand["version"])
        workflow.run_project(ident)
        self.assertEqual(self.calls["llm"], counts["llm"] + 1)
        # Updated briefing with identical generated narration reuses paid audio.
        self.assertEqual((self.calls["tts"], self.calls["asr"]), (counts["tts"], counts["asr"]))

    def test_brand_selection_rejects_forged_or_foreign_snapshot(self):
        one = brand_profiles.save_profile({"offering": "真实商品一", "audience": "家庭"})
        two = brand_profiles.save_profile({"offering": "真实商品二", "audience": "学生"})
        with self.assertRaises(ValueError):
            self.project(brand_profile_id=two["id"], brand_snapshot=one)
        with self.assertRaises(ValueError):
            self.project(brand_profile_id="not-a-local-brand")
        with self.assertRaises(ValueError):
            self.project(brand_snapshot=one)
        self.assertEqual(self.calls["llm"], 0)

    def test_campaign_purpose_and_template_share_guided_and_one_click_path(self):
        brand = brand_profiles.save_profile({"offering": "手工商品", "audience": "希望了解工艺的人", "desired_action": "留言咨询"})
        campaign = "本次只介绍制作流程，活动安排由客户另行确认。" * 30
        config = {"input_mode": "topic", "input_text": campaign, "allow_paid": True,
                  "brand_profile_id": brand["id"], "video_purpose": "product_service",
                  "content_template": "product_intro", "target_duration": 60}
        one = workflow.create_project(config)["id"]
        guided = workflow.create_project(config)["id"]
        workflow.run_project(one)
        workflow.run_project(guided, until_stage="script")
        workflow.run_project(guided, until_stage="voice")
        workflow.run_project(guided)
        self.assertEqual(self.llm_requests[0]["content_brief"], self.llm_requests[1]["content_brief"])
        brief = self.llm_requests[0]["content_brief"]
        self.assertEqual(brief["campaign_content"], campaign)
        self.assertEqual(len(self.llm_requests[0]["title"]), 300)
        self.assertEqual(brief["brand"]["offering"], "手工商品")
        self.assertNotIn("example", brief["content_template"])
        self.assertEqual((self.calls["llm"], self.calls["tts"], self.calls["asr"]), (2, 2, 2))

    def test_pasted_script_ignores_brief_changes_and_keeps_audio(self):
        ident = self.project()
        initial = workflow.run_project(ident)
        counts = dict(self.calls)
        updated = workflow.update_project(ident, {"video_purpose": "knowledge", "content_template": "knowledge_steps", "target_duration": 90})
        self.assertEqual(updated["state"], "done")
        final = workflow.run_project(ident)
        self.assertEqual(final["result"]["text"], initial["result"]["text"])
        self.assertEqual(self.calls, counts)

    def test_legacy_missing_brief_fields_preserves_every_checkpoint(self):
        ident = self.project(input_mode="topic", allow_paid=True)
        first = workflow.run_project(ident)
        fingerprints = {stage: first["stages"][stage]["fingerprint"] for stage in workflow.STAGES}
        workflow._mutate(ident, lambda row: [row["config"].pop(key) for key in workflow._BRIEF_FIELDS])
        counts = dict(self.calls)
        normalized = workflow.update_project(ident, {"allow_paid": True})
        self.assertEqual(normalized["state"], "done")
        for stage in workflow.STAGES:
            self.assertEqual(workflow._fingerprint(normalized, stage), fingerprints[stage])
        workflow.run_project(ident)
        self.assertEqual(self.calls, counts)

    def test_new_duration_regenerates_topic_but_reuses_identical_audio(self):
        ident = self.project(input_mode="topic", allow_paid=True, video_purpose="knowledge",
                             content_template="knowledge_steps", target_duration=60)
        workflow.run_project(ident)
        workflow.update_project(ident, {"target_duration": 90})
        workflow.run_project(ident)
        self.assertEqual((self.calls["llm"], self.calls["tts"], self.calls["asr"]), (2, 1, 1))
        workflow.update_project(ident, {"input_text": "这是新的知识主题"})
        with patch.object(topics, "generate_draft", return_value={"text": "新的主题产生不同的实际口播正文。"}):
            workflow.run_project(ident)
        self.assertEqual((self.calls["tts"], self.calls["asr"]), (2, 2))

    def test_actual_script_brief_is_checkpointed_and_reaches_visual_planning(self):
        text = "选套餐先看价格表。我们这份双人餐是99元，包含两道招牌菜。"
        ident = workflow.create_project({"input_text": text})["id"]
        written = workflow.run_project(ident, until_stage="script")
        brief = written["stages"]["script"]["result"]["video_brief"]
        self.assertEqual(brief["spoken_script"], text)
        self.assertEqual(self.calls["llm"], 0)
        workflow.run_project(ident)
        self.assertEqual(self.planned_briefs[-1], brief)
        self.assertEqual((self.calls["tts"], self.calls["asr"]), (1, 1))

    def test_local_parameter_changes_only_invalidate_affected_stages(self):
        ident = self.project()
        workflow.run_project(ident)
        workflow.update_project(ident, {"subtitle_style": "bold"})
        workflow.run_project(ident)
        self.assertEqual((self.calls["tts"], self.calls["asr"], self.calls["compose"], self.calls["render"]), (1, 1, 1, 2))
        video = workflow.get_project(ident)["result"]["video_path"]
        workflow.update_project(ident, {"release_title": "独立发布标题"})
        final = workflow.run_project(ident)
        self.assertEqual(final["result"]["video_path"], video)
        self.assertEqual(final["result"]["title"], "独立发布标题")
        self.assertEqual(self.calls["render"], 2)
        workflow.update_project(ident, {"materials": [self.file("素材.mp4")]})
        workflow.run_project(ident)
        self.assertEqual((self.calls["tts"], self.calls["asr"], self.calls["compose"]), (1, 1, 2))

    def test_avatar_mixed_reuses_full_voice_and_generated_cameos_after_render_failure(self):
        ident = self.project(kind="avatar", avatar_id="duix:1")
        workflow.run_project(ident, until_stage="voice")
        self.assertEqual(self.calls["tts"], 1)
        mixed = {"video_path": self.file("mixed-base.mp4"), "plan_path": self.file("mixed-plan.json"),
                 "plan": {"source_kind": "avatar_mixed", "segments": []}, "duration": 2.0, "mixed": True}
        with patch.object(avatar_mixed_generation, "generate", return_value=mixed) as generate:
            with patch.object(rendering, "render_video", side_effect=RuntimeError("暂时渲染失败")):
                with self.assertRaisesRegex(RuntimeError, "渲染"):
                    workflow.run_project(ident)
            final = workflow.run_project(ident)
            self.assertEqual(final["state"], "done")
            generate.assert_called_once()
            self.assertEqual(generate.call_args.kwargs["segments"][0]["text"], "测试口播。")
            self.assertEqual(generate.call_args.kwargs["materials"], [])
            self.assertEqual(generate.call_args.kwargs["video_brief"]["spoken_script"], "测试口播。第二句话。")
        self.assertEqual((self.calls["tts"], self.calls["asr"]), (1, 1))

    def test_changed_script_and_voice_invalidate_downstream(self):
        ident = self.project()
        workflow.run_project(ident)
        workflow.update_project(ident, {"voice_id": "edge:zh-CN-YunxiNeural"})
        workflow.run_project(ident)
        self.assertEqual(self.calls["tts"], 2)
        workflow.update_project(ident, {"input_text": "全新的文案。"})
        workflow.run_project(ident)
        self.assertEqual(self.calls["tts"], 3)
        self.assertEqual(self.calls["compose"], 3)

    def test_asr_failure_preserves_narration_checkpoint(self):
        ident = self.project()
        with patch.object(extract, "extract_media", side_effect=RuntimeError("ASR 模型未就绪")):
            with self.assertRaisesRegex(RuntimeError, "ASR"):
                workflow.run_project(ident)
        failed = workflow.get_project(ident)
        self.assertEqual(failed["state"], "failed")
        self.assertTrue(Path(failed["stages"]["voice"]["result"]["audio_path"]).is_file())
        workflow.run_project(ident)
        self.assertEqual(self.calls["tts"], 1)

    def test_recovery_resumes_after_persisted_checkpoint(self):
        ident = self.project()
        workflow.run_project(ident, until_stage="voice")
        def interrupted(project):
            project.update(state="running", owner="a" * 32, claim_token="lost", current_stage="visuals")
            project["stages"]["visuals"]["state"] = "running"
        workflow._mutate(ident, interrupted)
        workflow.recover_projects()
        self.assertEqual(workflow.get_project(ident)["state"], "interrupted")
        final = workflow.run_project(ident)
        self.assertEqual(final["state"], "done")
        self.assertEqual(self.calls["tts"], 1)

    def test_unknown_paid_result_blocks_restart_and_requires_explicit_retry(self):
        ident = self.project(input_mode="topic", allow_paid=True)
        with patch.object(topics, "generate_draft", side_effect=TimeoutError("远端超时")) as provider:
            with self.assertRaises(TimeoutError):
                workflow.run_project(ident)
            self.assertEqual(provider.call_count, 1)
        self.assertEqual(workflow.get_project(ident)["state"], "needs_user")
        with self.assertRaisesRegex(ValueError, "远端"):
            workflow.run_project(ident)
        with self.assertRaisesRegex(ValueError, "远端"):
            workflow.update_project(ident, {"input_text": "另一主题"})
        workflow.update_project(ident, {"acknowledge_paid_retry": True})
        final = workflow.run_project(ident)
        self.assertEqual(final["state"], "done")
        self.assertEqual(self.calls["llm"], 1)

    def test_paid_remote_restart_does_not_turn_into_blind_retry(self):
        ident = self.project(input_mode="topic", allow_paid=True)
        def crashed(project):
            project.update(state="running", owner="b" * 32, current_stage="script")
            project["stages"]["script"].update(state="running", remote_request={"state": "sending", "provider": "llm"})
        workflow._mutate(ident, crashed)
        workflow.recover_projects()
        self.assertEqual(workflow.get_project(ident)["state"], "needs_user")
        with self.assertRaises(ValueError):
            workflow.submit_project(ident)
        self.assertEqual(self.calls["llm"], 0)

    def test_paid_features_need_explicit_allowance_and_cloud_audio_not_repeated(self):
        ident = self.project(input_mode="topic")
        with self.assertRaisesRegex(ValueError, "付费"):
            workflow.run_project(ident)
        self.assertEqual(self.calls["llm"], 0)
        other = self.project(voice_id="saved:cloud")
        with self.assertRaisesRegex(ValueError, "云端费用"):
            workflow.run_project(other)
        self.assertEqual(self.calls["tts"], 0)
        workflow.update_project(other, {"allow_paid": True})
        with patch.object(extract, "extract_media", side_effect=RuntimeError("字幕暂时失败")):
            with self.assertRaises(RuntimeError):
                workflow.run_project(other)
        workflow.run_project(other)
        self.assertEqual(self.calls["tts"], 1)

    def test_concurrent_clicks_and_recovery_cannot_steal_live_project(self):
        ident = self.project()
        entered, leave = threading.Event(), threading.Event()
        failures = []
        def blocking(*args, **kwargs):
            entered.set()
            self.assertTrue(leave.wait(10))
            return self.tts(*args, **kwargs)
        def run():
            try:
                workflow.run_project(ident)
            except BaseException as exc:
                failures.append(exc)
        with patch.object(narration, "generate", side_effect=blocking):
            thread = threading.Thread(target=run)
            thread.start()
            self.assertTrue(entered.wait(10))
            workflow.recover_projects()
            self.assertEqual(workflow.get_project(ident)["state"], "running")
            with self.assertRaisesRegex(ValueError, "重复"):
                workflow.run_project(ident)
            with self.assertRaises(ValueError):
                workflow.update_project(ident, {"speed": 1.1})
            leave.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertFalse(failures)
        self.assertEqual(self.calls["tts"], 1)

    def test_submit_deduplicates_and_queue_failure_releases_reservation(self):
        ident = self.project()
        with patch.object(workflow.jobs, "submit", return_value="one-job") as submit:
            self.assertEqual(workflow.submit_project(ident), "one-job")
            self.assertEqual(workflow.submit_project(ident), "one-job")
            self.assertEqual(submit.call_count, 1)
            args = submit.call_args.args
            args[1](*args[2:])
        self.assertEqual(workflow.get_project(ident)["state"], "done")
        other = self.project()
        with patch.object(workflow.jobs, "submit", side_effect=ValueError("队列已满")):
            with self.assertRaises(ValueError):
                workflow.submit_project(other)
        self.assertEqual(workflow.get_project(other)["state"], "paused")

    def test_cancel_finishes_current_stage_and_stops_before_next(self):
        ident = self.project()
        def cancel_then_audio(*args, **kwargs):
            workflow.cancel_project(ident)
            return self.tts(*args, **kwargs)
        with patch.object(narration, "generate", side_effect=cancel_then_audio):
            result = workflow.run_project(ident)
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(result["stages"]["voice"]["state"], "done")
        self.assertEqual(self.calls["compose"], 0)
        self.assertEqual(workflow.run_project(ident)["state"], "done")
        self.assertEqual(self.calls["tts"], 1)

    def test_quality_failure_blocks_release_and_rechecks_without_rerender(self):
        ident = self.project()
        with patch.object(quality, "inspect_video", return_value={"pass": False, "errors": ["音轨缺失"]}):
            with self.assertRaisesRegex(RuntimeError, "音轨缺失"):
                workflow.run_project(ident)
        self.assertEqual(self.calls["cover"], 0)
        self.assertEqual(self.calls["package"], 0)
        workflow.run_project(ident)
        self.assertEqual(self.calls["render"], 1)

    def test_deleted_artifacts_and_modified_materials_are_not_reused(self):
        material = self.file("素材.png")
        ident = self.project(materials=[material])
        workflow.run_project(ident)
        Path(material).write_bytes(b"new content and new length")
        workflow.run_project(ident)
        self.assertEqual(self.calls["compose"], 2)
        self.assertEqual(self.calls["tts"], 1)
        Path(workflow.get_project(ident)["stages"]["voice"]["result"]["audio_path"]).unlink()
        workflow.run_project(ident)
        self.assertEqual(self.calls["tts"], 2)

    def test_imported_video_requires_actual_audio_and_avoids_synthesis(self):
        ident = self.project(source_video_path=self.file("已有口播.mp4"))
        with patch.object(rendering, "probe_source", return_value={"has_audio": False}):
            with self.assertRaisesRegex(ValueError, "音轨"):
                workflow.run_project(ident)
        with patch.object(rendering, "probe_source", return_value={"has_audio": True, "path": self.file("有效口播.mp4"), "duration": 2.0}):
            result = workflow.run_project(ident)
        self.assertEqual(result["state"], "done")
        self.assertEqual(self.calls["tts"], 0)

    def test_aspect_change_reuses_audio_and_unknown_configuration_fails_early(self):
        ident = self.project()
        workflow.run_project(ident)
        workflow.update_project(ident, {"aspect": "16:9"})
        workflow.run_project(ident)
        self.assertEqual(self.calls["tts"], 1)
        self.assertEqual(self.calls["asr"], 1)
        for changes in ({"speed": True}, {"template": "explain"}, {"allow_paid": "yes"}, {"input_text": ""}, {"unknown": 1}):
            with self.assertRaises(ValueError):
                workflow.create_project(dict({"input_text": "文案"}, **changes))

    def test_required_visual_inputs_pause_before_llm_or_synthesis(self):
        for kind in ("product", "montage", "avatar"):
            previous_llm = self.calls["llm"]
            ident = self.project(input_mode="topic", kind=kind, allow_paid=True)
            result = workflow.run_project(ident)
            self.assertEqual(result["state"], "needs_user")
            self.assertEqual(self.calls["llm"], previous_llm)
            self.assertEqual(self.calls["tts"], 0)
            self.assertEqual(workflow.run_project(ident, until_stage="script")["state"], "paused")
        self.assertEqual(self.calls["llm"], 3)

    def test_unknown_request_marker_survives_allowance_and_mode_changes(self):
        ident = self.project(input_mode="topic", allow_paid=True)
        with patch.object(topics, "generate_draft", side_effect=TimeoutError("uncertain")):
            with self.assertRaises(TimeoutError):
                workflow.run_project(ident)
        workflow.update_project(ident, {"allow_paid": False})
        self.assertIsNotNone(workflow.get_project(ident)["remote_pending"])
        with self.assertRaises(ValueError):
            workflow.update_project(ident, {"input_mode": "script"})
        with self.assertRaises(ValueError):
            workflow.run_project(ident)
        workflow.update_project(ident, {"input_mode": "script", "acknowledge_paid_retry": True})
        self.assertIsNone(workflow.get_project(ident)["remote_pending"])
        self.assertEqual(workflow.run_project(ident)["state"], "done")

    def test_another_live_process_is_not_stolen_and_exit_recovers_on_read(self):
        ready = self.root / "child-project.txt"
        child_code = (
            "import sys; from pathlib import Path; from app.services.creator import workflow; "
            "p=workflow.create_project({'input_text':'跨进程测试口播。'}); "
            "workflow._claim(p['id'], 'child'); "
            "Path(sys.argv[1]).write_text(p['id'], 'utf-8'); "
            "sys.stdin.read(1)"
        )
        process = subprocess.Popen([sys.executable, "-X", "utf8", "-c", child_code, str(ready)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", cwd=Path(__file__).resolve().parents[1])
        try:
            deadline = time.monotonic() + 15
            while not ready.is_file() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.is_file(), "子进程没有准备好持久作品记录")
            ident = ready.read_text("utf-8")
            self.assertEqual(workflow.get_project(ident)["state"], "running")
            with self.assertRaisesRegex(ValueError, "重复"):
                workflow.run_project(ident)
            _, stderr = process.communicate("\n", timeout=15)
            self.assertEqual(process.returncode, 0, stderr)
            # Opening the work after the owner process ends recovers without
            # requiring a separate UI recovery button.
            self.assertEqual(workflow.get_project(ident)["state"], "interrupted")
            self.assertEqual(workflow.run_project(ident)["state"], "done")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()
