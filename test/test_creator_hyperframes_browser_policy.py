"""Require separate publishing and real HyperFrames rendering readiness."""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.creator import hyperframes

_PATH = Path(__file__).resolve().parents[1] / "scripts/creator/portable_entry.py"
_SPEC = importlib.util.spec_from_file_location("creator_hyperframes_browser_policy_entry", _PATH)
entry = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(entry)


class HyperframesBrowserPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="creator-browser-policy-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.app = self.root / "app"
        self.local = self.root / "local"
        self.node = self.file("app/runtime/node/node.exe")
        self.ffmpeg = self.file("app/runtime/ffmpeg/ffmpeg.exe")
        self.ffprobe = self.file("app/runtime/ffmpeg/ffprobe.exe")
        patches = [
            patch.object(entry, "APP_ROOT", self.app),
            patch.object(entry, "RUNTIME", self.app / "runtime"),
            patch.object(entry, "_VERIFIED_BROWSERS", None),
            patch.object(entry, "_system_browser_candidates", return_value=[]),
            patch.dict(os.environ, {"LOCALAPPDATA": str(self.local)}, clear=True),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def file(self, relative, content=b"fixture component"):
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target.resolve()

    def cached_shell(self):
        return self.file(
            "local/ms-playwright/chromium_headless_shell-1243/"
            "chrome-headless-shell-win64/chrome-headless-shell.exe"
        )

    def inspection(self):
        def run(command, **_kwargs):
            if command[1:] == ["--version"]:
                return "v22.14.0"
            if "-encoders" in command:
                return "libx264 aac libvpx-vp9 prores_ks"
            if "-version" in command:
                return f"{Path(command[0]).stem} version 8.0"
            return "ok"

        with patch.object(entry, "_python_dependencies", return_value=[]), \
             patch.object(entry, "_node_dependencies", return_value=[]), \
             patch.object(entry, "_vendor_ready", return_value=True), \
             patch.object(entry, "MANIFEST", {}), \
             patch.object(entry.sys, "version_info", (3, 11, 0)), \
             patch.object(entry, "_run", side_effect=run):
            return entry.inspect()

    def test_cache_detection_only_checks_known_executables_without_reading_profiles(self):
        shell = self.cached_shell()
        headed = self.file("local/ms-playwright/chromium-1243/chrome-win64/chrome.exe")
        self.file("local/Microsoft/Edge/User Data/Default/chrome-headless-shell.exe")
        self.file("local/ms-playwright/chromium_headless_shell-1244/chrome-headless-shell-win64/profile.txt")
        self.file("local/ms-playwright/chromium_headless_shell-1244/chrome-headless-shell-win64/chrome-headless-shell.exe.old")
        fake_executable = self.root / (
            "local/ms-playwright/chromium_headless_shell-1245/"
            "chrome-headless-shell-win64/chrome-headless-shell.exe"
        )
        fake_executable.mkdir(parents=True)
        original_glob = Path.glob
        searched = []

        def known_cache_glob(folder, pattern):
            self.assertEqual(folder, self.local / "ms-playwright")
            searched.append(pattern)
            return original_glob(folder, pattern)

        with patch.object(Path, "glob", known_cache_glob), \
             patch.object(Path, "open", side_effect=AssertionError("Browser files must not be opened")), \
             patch.object(Path, "read_text", side_effect=AssertionError("Profile text must not be read")), \
             patch.object(Path, "read_bytes", side_effect=AssertionError("Profile data must not be read")):
            shells, browsers = entry._cached_browser_paths()
        self.assertEqual(shells, [shell])
        self.assertEqual(browsers, [headed])
        self.assertEqual(searched, [
            "chromium_headless_shell-*/chrome-headless-shell-win64/chrome-headless-shell.exe",
            "chromium-*/chrome-win64/chrome.exe",
        ])

    def test_hyperframes_verified_cache_renders_while_edge_only_publishes(self):
        shell = self.cached_shell()
        edge = self.file("system/Edge/Application/msedge.exe")

        def probe(browser, _env):
            return {"version": "145.0" if browser == shell else "154.0",
                    "headless_launch": True, "rendered": True,
                    "hyperframes_rendered": browser == shell}

        with patch.object(entry, "_system_browser_candidates", return_value=[edge]), \
             patch.object(entry, "_browser_probe", side_effect=probe):
            selected = entry._select_browsers({})
            self.assertEqual(selected["render"], shell)
            self.assertEqual(selected["publish"], edge)
            self.assertEqual(selected["render_source"], "cached_chromium")
            self.assertEqual(selected["publish_source"], "system_edge")
            env = entry.environment()
            self.assertEqual(env["HYPERFRAMES_BROWSER_PATH"], str(shell))
            self.assertEqual(env["MPT_BROWSER_PATH"], str(edge))
            report = self.inspection()
        self.assertTrue(report["ready"], report["issues"])
        self.assertEqual(report["browser_source"], "mixed")
        self.assertEqual(report["browser_versions"], {"render": "145.0", "publish": "154.0"})

    def test_edge_page_success_without_hyperframes_output_cannot_report_ready(self):
        edge = self.file("system/Edge/Application/msedge.exe")
        page_only = {"version": "154.0", "headless_launch": True,
                     "rendered": True, "hyperframes_rendered": False}
        with patch.object(entry, "_system_browser_candidates", return_value=[edge]), \
             patch.object(entry, "_browser_probe", return_value=page_only):
            self.assertIsNone(entry._select_browsers({}))
            self.assertIsNone(entry._VERIFIED_BROWSERS)
            report = self.inspection()
        self.assertFalse(report["ready"])
        self.assertEqual(report["browser_source"], "unavailable")
        self.assertEqual(report["browser_versions"], {})
        self.assertTrue(any("渲染与发布浏览器" in issue for issue in report["issues"]))

    def test_browser_probe_preserves_sandbox_and_publishing_after_cli_failure(self):
        edge = self.file("system/Edge/Application/msedge.exe")
        env = {"LOCALAPPDATA": str(self.local)}
        page_report = {"version": "154.0", "headless_launch": True, "rendered": True}
        with patch.object(entry, "_run", return_value=json.dumps(page_report)) as command, \
             patch.object(entry, "_hyperframes_browser_probe", side_effect=RuntimeError("CLI render failed")) as render:
            report = entry._browser_probe(edge, env)
        arguments = command.call_args.args[0]
        self.assertEqual(arguments[0], self.node)
        self.assertEqual(arguments[1], "-e")
        self.assertIn("chromiumSandbox:true", arguments[2])
        self.assertNotIn("--no-sandbox", arguments[2])
        self.assertEqual(arguments[-1], edge)
        self.assertTrue(command.call_args.kwargs["capture"])
        self.assertEqual(command.call_args.kwargs["timeout"], 45)
        render.assert_called_once_with(edge, env)
        self.assertTrue(report["headless_launch"])
        self.assertTrue(report["rendered"])
        self.assertFalse(report["hyperframes_rendered"])
        self.assertEqual(report["hyperframes_error"], "CLI render failed")

    def prepare_cli(self):
        cli = self.file("app/creator-hyperframes/node_modules/hyperframes/bin/hyperframes.mjs",
                        b"original pinned official CLI")
        self.file("app/creator-hyperframes/node_modules/gsap/dist/gsap.min.js", b"local GSAP fixture")
        return cli

    def test_hyperframes_probe_uses_official_cli_path_and_fixed_local_render_flags(self):
        cli = self.prepare_cli()
        original_cli = cli.read_bytes()
        shell = self.cached_shell()

        def run(command, *, env, capture, timeout):
            self.assertEqual(command[:3], [self.node, cli, "render"])
            folder = Path(command[3])
            self.assertEqual(command[4:], [
                "--format", "webm", "--output", folder / "probe.webm", "--fps", "30",
                "--workers", "1", "--no-browser-gpu", "--no-best-effort", "--quality", "standard",
                "--vp9-cpu-used", "6",
            ])
            self.assertEqual(env["HYPERFRAMES_BROWSER_PATH"], str(shell))
            self.assertEqual(env["PRODUCER_HEADLESS_SHELL_PATH"], str(shell))
            self.assertEqual(env["HYPERFRAMES_FFMPEG_PATH"], str(self.ffmpeg))
            self.assertEqual(env["HYPERFRAMES_FFPROBE_PATH"], str(self.ffprobe))
            for flag in ("HYPERFRAMES_NO_UPDATE_CHECK", "HYPERFRAMES_NO_TELEMETRY",
                         "HYPERFRAMES_SKIP_SKILLS", "DO_NOT_TRACK"):
                self.assertEqual(env[flag], "1")
            self.assertEqual(env["PRODUCER_ENABLE_STREAMING_ENCODE"], "true")
            self.assertEqual(env["UNRELATED"], "preserved")
            for tls_variable in ("NODE_TLS_REJECT_UNAUTHORIZED", "PYTHONHTTPSVERIFY"):
                self.assertNotIn(tls_variable, env)
            self.assertTrue(capture)
            self.assertEqual(timeout, 45)
            document = (folder / "index.html").read_text("utf-8")
            self.assertIn("background:transparent", document)
            self.assertIn("window.__renderReady=true", document)
            self.assertNotIn("https://", document)
            self.assertEqual((folder / "gsap.min.js").read_bytes(), b"local GSAP fixture")
            (folder / "probe.webm").write_bytes(b"x" * 128)
            return "render complete"

        with patch.object(entry, "_run", side_effect=run) as command:
            self.assertTrue(entry._hyperframes_browser_probe(shell, {"UNRELATED": "preserved"}))
        command.assert_called_once()
        self.assertEqual(cli.read_bytes(), original_cli)

    def test_hyperframes_probe_rejects_missing_or_too_small_output(self):
        self.prepare_cli()
        shell = self.cached_shell()
        for size in (None, 99):
            with self.subTest(size=size):
                def run(command, **_kwargs):
                    if size is not None:
                        output = Path(command[command.index("--output") + 1])
                        output.write_bytes(b"x" * size)
                    return "render complete"

                with patch.object(entry, "_run", side_effect=run), \
                     self.assertRaisesRegex(RuntimeError, "未生成可用透明画面"):
                    entry._hyperframes_browser_probe(shell, {})

    @unittest.skipUnless(os.name == "nt", "Windows browser cache fallback")
    def test_overlay_browser_falls_back_to_generic_cache_and_explicit_path_takes_priority(self):
        shell = self.cached_shell()
        app_cache = self.app / "runtime/browser"
        app_cache.mkdir(parents=True)
        with patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": str(app_cache)}):
            self.assertEqual(hyperframes._browser_path(), str(shell))
            explicit = self.file("system/Edge/Application/msedge.exe")
            alternative = self.file("system/Chrome/Application/chrome.exe")
            with patch.dict(os.environ, {"HYPERFRAMES_BROWSER_PATH": str(explicit),
                                         "MPT_RENDER_BROWSER": str(alternative)}):
                self.assertEqual(hyperframes._browser_path(), str(explicit))
            with patch.dict(os.environ, {"MPT_RENDER_BROWSER": str(alternative)}):
                self.assertEqual(hyperframes._browser_path(), str(alternative))


if __name__ == "__main__":
    unittest.main()
