import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.creator import hyperframes


def payload(video="source.mp4", output="visual.mp4"):
    return {"video": video, "output": output, "title": "中文口播标题", "template": "talking", "style": "knowledge",
            "colorGrade": "none", "subtitleStyle": "clean", "videoFit": "contain", "width": 320, "height": 568,
            "fps": 30, "durationInFrames": 90, "captions": [{"start": 0, "end": .9, "text": "第一段中文字幕"},
                                                            {"start": 1.2, "end": 2.4, "text": "第二段字幕按真实时间出现"}], "pipItems": []}


class HyperframesCompositionTests(unittest.TestCase):
    def test_user_text_is_escaped_and_cannot_execute_markup(self):
        data = payload()
        unsafe = '</script><img src="https://example.com" onerror="alert(1)">'
        data["title"] = unsafe
        data["captions"][0]["text"] = unsafe
        document = hyperframes.build_overlay_html(data)
        self.assertNotIn(unsafe, document)
        self.assertIn("&lt;/script&gt;", document)
        self.assertNotIn('src="https://example.com"', document)
        self.assertIn("background:transparent!important", document)

    def test_exact_subtitle_windows_and_paused_timeline(self):
        document = hyperframes.build_overlay_html(payload())
        self.assertIn('data-start="1.200000000" data-duration="1.200000000"', document)
        self.assertIn('data-duration="3.000000000"', document)
        self.assertIn("paused:true", document)
        self.assertIn("window.__timelines.root=tl", document)
        self.assertNotIn("setTimeout", document)
        self.assertNotIn("requestAnimationFrame", document)
        self.assertNotIn("Math.random", document)
        self.assertNotIn("Date.now", document)
        self.assertNotIn("cdn.jsdelivr", document)

    def test_prepared_caption_line_breaks_remain_and_cards_avoid_face(self):
        data = payload()
        data["captions"][0]["text"] = "第一行中文\n第二行中文"
        document = hyperframes.build_overlay_html(data)
        self.assertIn("第一行中文\n第二行中文", document)
        self.assertIn('el.textContent.split("\\n")', document)
        self.assertIn(".point{position:absolute;bottom:27%", document)
        self.assertNotIn(".point{position:absolute;top:22%", document)

    def test_local_font_is_copied_only_to_fixed_private_asset_without_path_in_html(self):
        with tempfile.TemporaryDirectory() as folder:
            assets = Path(folder) / "render" / "assets"
            assets.mkdir(parents=True)
            for extension in (".ttf", ".ttc", ".otf"):
                source = Path(folder) / ("用户选择的私有字体" + extension.upper())
                source.write_bytes(b"local-font-fixture")
                with patch.dict(os.environ, {"MPT_CREATOR_FONT": str(source)}):
                    name = hyperframes._prepare_font_asset(assets)
                self.assertEqual("creator-font" + extension, name)
                self.assertEqual(source.read_bytes(), (assets / name).read_bytes())
                document = hyperframes.build_overlay_html(payload(), font_asset=name)
                self.assertIn(f'src:url("assets/{name}")', document)
                self.assertIn('font-family:"Creator Local","Microsoft YaHei"', document)
                self.assertNotIn(str(source), document)
                self.assertNotIn(source.name, document)
                self.assertNotIn("https://", document)

    def test_missing_or_unconfigured_font_keeps_original_system_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            assets = Path(folder)
            unsupported = assets / "untrusted.css"
            unsupported.write_text("body{}", "utf-8")
            for value in ("", str(assets / "missing.ttf"), str(unsupported), "https://example.org/font.ttf"):
                with self.subTest(value=value), patch.dict(os.environ, {"MPT_CREATOR_FONT": value}):
                    self.assertIsNone(hyperframes._prepare_font_asset(assets))
            document = hyperframes.build_overlay_html(payload())
            self.assertNotIn("Creator Local", document)
            self.assertIn('font-family:"Microsoft YaHei","Noto Sans CJK SC",sans-serif', document)
            self.assertEqual([unsupported], list(assets.iterdir()))

    def test_font_fitting_waits_until_loading_finishes_before_marking_runtime_ready(self):
        document = hyperframes.build_overlay_html(payload(), font_asset="creator-font.ttf")
        self.assertLess(document.index("window.__renderReady=false"), document.index("document.fonts.load"))
        self.assertLess(document.index("await document.fonts.ready"), document.index('document.createElement("canvas")'))
        self.assertLess(document.index('document.createElement("canvas")'), document.index("window.__renderReady=true"))
        self.assertNotIn("window.__renderReady=document.fonts.ready", document)
        for value in ("../../private.ttf", "creator-font.ttf\")};body{color:red}", "https://example.org/font.ttf"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                hyperframes.build_overlay_html(payload(), font_asset=value)

    def test_point_cards_are_verbatim_and_spaced(self):
        rows = [{"start": 4, "end": 6, "text": "这句话来自用户原始文案"},
                {"start": 7, "end": 8, "text": "这里不应紧挨着重复出卡"},
                {"start": 15, "end": 18, "text": "第二张卡仍然只是原文摘句"}]
        cards = hyperframes._point_cards(rows, 20, "knowledge")
        self.assertEqual([row["text"] for row in cards], [rows[0]["text"], rows[2]["text"]])
        self.assertEqual(hyperframes._point_cards(rows, 20, "clean"), [])

    def test_invalid_settings_and_nonfinite_timing_rejected(self):
        for field, value in [("width", 321), ("fps", 0), ("durationInFrames", float("nan")), ("style", "injected"),
                             ("videoFit", "invalid")]:
            data = payload()
            data[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                hyperframes.build_overlay_html(data)
        data = payload()
        data["captions"][0]["start"] = float("inf")
        with self.assertRaises(ValueError):
            hyperframes.build_overlay_html(data)

    def test_alpha_decoder_and_silent_full_timeline_no_base_loop(self):
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "source.mp4"
            video.write_bytes(b"placeholder")
            data = payload(str(video), str(Path(folder) / "visual.mp4"))
            with patch.object(hyperframes.extract, "ffmpeg_binary", return_value="ffmpeg"):
                command = hyperframes.composite_command(data, Path(folder) / "overlay.webm")
            self.assertIn("libvpx-vp9", command)
            self.assertIn("-an", command)
            self.assertNotIn("-stream_loop", command)
            self.assertEqual(command[command.index("-frames:v")+1], "90")
            self.assertIn("eof_action=pass", command[command.index("-filter_complex")+1])

    def test_output_does_not_overwrite_source(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.mp4"
            source.write_bytes(b"placeholder")
            with self.assertRaises(ValueError):
                hyperframes.composite_command(payload(str(source), str(source)), Path(folder) / "overlay.mov")

    def test_pip_and_cards_preserve_background_layout(self):
        with tempfile.TemporaryDirectory() as folder:
            source, image = Path(folder) / "source.mp4", Path(folder) / "background.png"
            source.write_bytes(b"placeholder")
            image.write_bytes(b"placeholder")
            data = payload(str(source), str(Path(folder) / "visual.mp4"))
            with patch.object(hyperframes.extract, "ffmpeg_binary", return_value="ffmpeg"):
                data.update(template="pip", image=str(image))
                command = hyperframes.composite_command(data, Path(folder) / "overlay.mov")
                filters = command[command.index("-filter_complex")+1]
                self.assertIn("[bg][source]overlay=x=182:y=244", filters)
                data["template"] = "cards"
                command = hyperframes.composite_command(data, Path(folder) / "overlay.mov")
                filters = command[command.index("-filter_complex")+1]
                self.assertIn("pad=320:568:16:73:color=0x10131b", filters)
                self.assertNotIn("libvpx-vp9", command)


@unittest.skipUnless(os.environ.get("MPT_HYPERFRAMES_LIVE_TEST") == "1", "explicit real CLI render")
class HyperframesLiveRenderTests(unittest.TestCase):
    def test_real_chinese_alpha_overlay_retains_all_three_base_scenes(self):
        from PIL import Image
        from app.services.creator import rendering
        folder = Path(os.environ.get("MPT_HYPERFRAMES_QA_DIR", tempfile.mkdtemp(prefix="hyperframes-live-")))
        folder.mkdir(parents=True, exist_ok=True)
        binary = hyperframes.extract.ffmpeg_binary()
        source = folder / "three-scenes.mp4"
        generate = [binary, "-hide_banner", "-v", "error", "-nostdin", "-y",
                    "-f", "lavfi", "-i", "color=c=red:s=320x568:d=1:r=30",
                    "-f", "lavfi", "-i", "color=c=green:s=320x568:d=1:r=30",
                    "-f", "lavfi", "-i", "color=c=blue:s=320x568:d=1:r=30",
                    "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]", "-map", "[v]", "-an",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)]
        subprocess.run(generate, check=True, capture_output=True, timeout=60)
        output = folder / "visual.mp4"
        with patch.dict(os.environ, {"MPT_HYPERFRAMES_KEEP_OVERLAY": "1"}):
            hyperframes.render_visual(payload(str(source), str(output)), folder / "render.log")
        metadata = rendering.probe_source(output)
        self.assertAlmostEqual(metadata["duration"], 3, delta=.06)
        self.assertFalse(metadata["has_audio"])
        alpha = folder / "overlay-alpha.png"
        subprocess.run([binary, "-hide_banner", "-v", "error", "-nostdin", "-y", "-c:v", "libvpx-vp9",
                        "-i", str(folder / "hyperframes-overlay.webm"), "-ss", "0.5", "-frames:v", "1", "-pix_fmt", "rgba", str(alpha)],
                       check=True, capture_output=True, timeout=30)
        with Image.open(alpha) as frame:
            self.assertEqual(frame.mode, "RGBA")
            self.assertEqual(frame.getpixel((0,0))[3], 0)
            self.assertEqual(frame.getpixel((160,284))[3], 0)
            self.assertGreater(frame.crop((22,450,298,525)).getchannel("A").getextrema()[1], 0)
        for index, timestamp in enumerate([.5,1.5,2.5]):
            image = folder / f"base-scene-{index}.png"
            subprocess.run([binary, "-hide_banner", "-v", "error", "-nostdin", "-y", "-ss", str(timestamp), "-i", str(output),
                            "-frames:v", "1", str(image)], check=True, capture_output=True, timeout=30)
            with Image.open(image) as frame:
                color = frame.convert("RGB").getpixel((160,284))
            self.assertEqual(color.index(max(color)), index)

    def test_long_streaming_mov_and_original_phrase_card(self):
        from PIL import Image
        folder = Path(os.environ.get("MPT_HYPERFRAMES_QA_DIR", tempfile.mkdtemp(prefix="hyperframes-live-"))) / "long-mov"
        folder.mkdir(parents=True, exist_ok=True)
        binary = hyperframes.extract.ffmpeg_binary()
        source = folder / "source.mp4"
        subprocess.run([binary, "-hide_banner", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
                        "color=c=0x12805a:s=320x568:d=13:r=30", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)],
                       check=True, capture_output=True, timeout=60)
        data = payload(str(source), str(folder / "visual.mp4"))
        data.update(durationInFrames=390, template="cards")
        data["captions"] = [{"start": 4, "end": 7, "text": "观点卡只摘录原始文案"}]
        with patch.dict(os.environ, {"MPT_HYPERFRAMES_KEEP_OVERLAY": "1"}):
            result = hyperframes.render_visual(data, folder / "render.log")
        self.assertEqual(result["overlay_format"], "mov-prores4444-alpha")
        alpha = folder / "overlay-card.png"
        subprocess.run([binary, "-hide_banner", "-v", "error", "-nostdin", "-y", "-ss", "5", "-i", result["overlay_path"],
                        "-frames:v", "1", "-pix_fmt", "rgba", str(alpha)], check=True, capture_output=True, timeout=30)
        with Image.open(alpha) as frame:
            self.assertEqual(frame.getpixel((160,300))[3], 0)
            self.assertGreater(frame.crop((18,350,160,435)).getchannel("A").getextrema()[1], 0)

    def test_timed_picture_in_picture_keeps_background_and_original_timeline(self):
        from PIL import Image
        folder = Path(os.environ.get("MPT_HYPERFRAMES_QA_DIR", tempfile.mkdtemp(prefix="hyperframes-live-"))) / "pip"
        folder.mkdir(parents=True, exist_ok=True)
        binary = hyperframes.extract.ffmpeg_binary()
        source = folder / "source.mp4"
        subprocess.run([binary, "-hide_banner", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
                        "color=c=red:s=320x568:d=3:r=30", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)],
                       check=True, capture_output=True, timeout=60)
        background, material = folder / "background.png", folder / "material.png"
        Image.new("RGB", (320,568), "blue").save(background)
        Image.new("RGB", (100,100), "yellow").save(material)
        data = payload(str(source), str(folder / "visual.mp4"))
        data.update(template="pip", image=str(background),
                    pipItems=[{"path": str(material), "kind": "image", "width": 100, "height": 100,
                               "start": 1.2, "end": 2.4, "size": .3, "position": "top-right"}])
        hyperframes.render_visual(data, folder / "render.log")
        for index, timestamp in enumerate([.5,1.5,2.5]):
            image = folder / f"pip-timing-{index}.png"
            subprocess.run([binary, "-hide_banner", "-v", "error", "-nostdin", "-y", "-ss", str(timestamp),
                            "-i", data["output"], "-frames:v", "1", str(image)], check=True, capture_output=True, timeout=30)
            with Image.open(image) as frame:
                color = frame.convert("RGB").getpixel((240,150))
                person = frame.convert("RGB").getpixel((240,330))
            if index == 1:
                self.assertGreater(color[0], 200)
                self.assertGreater(color[1], 200)
            else:
                self.assertGreater(color[2], 200)
                self.assertLess(color[0], 30)
            self.assertGreater(person[0], 200)


if __name__ == "__main__":
    unittest.main()
