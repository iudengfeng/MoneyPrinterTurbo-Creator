"""Protect isolated installs, private configuration, and relocated runtime paths."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

_PATH = Path(__file__).resolve().parents[1] / "scripts/creator/portable_entry.py"
_SPEC = importlib.util.spec_from_file_location("creator_portable_entry", _PATH)
entry = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(entry)


class PortableEntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="创作 安装测试 ")
        self.root = Path(self.temp.name)
        self.patches = [patch.object(entry, "APP_ROOT", self.root), patch.object(entry, "RUNTIME", self.root / "runtime"),
                        patch.object(entry, "_VERIFIED_BROWSERS", None), patch.object(entry, "_system_browser_candidates", return_value=[]),
                        patch.object(entry, "_cached_browser_paths", return_value=([], []))]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def file(self, relative, content=b"complete component"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_relocated_app_sets_only_own_runtime_paths(self):
        self.file("runtime/node/node.exe")
        self.file("runtime/ffmpeg/ffmpeg.exe")
        self.file("runtime/ffmpeg/ffprobe.exe")
        shell = self.file("runtime/browser/chromium_headless_shell-1243/chrome-headless-shell-win64/chrome-headless-shell.exe")
        headed = self.file("runtime/browser/chromium-1243/chrome-win64/chrome.exe")
        self.file("resource/fonts/NotoSansSC.ttf")
        with patch.dict(os.environ, {"MPT_NODE_PATH": "C:/foreign/node.exe"}):
            env = entry.environment()
        self.assertEqual(env["HYPERFRAMES_BROWSER_PATH"], str(shell))
        self.assertEqual(env["MPT_BROWSER_PATH"], str(headed))
        for key in ("MPT_NODE_PATH", "FFMPEG_BINARY", "MPT_FFPROBE", "PLAYWRIGHT_BROWSERS_PATH", "MPT_CREATOR_FONT"):
            self.assertTrue(Path(env[key]).is_relative_to(self.root))
        self.assertEqual(env["PIP_CONFIG_FILE"], os.devnull)
        self.assertTrue(env["PATH"].startswith(str(self.root / "runtime/node")))

    def test_verified_download_cache_works_offline_without_network(self):
        payload = b"trusted complete archive"
        path = self.file("runtime/cache/component.zip", payload)
        item = {"url": "https://example.test/component.zip", "sha256": hashlib.sha256(payload).hexdigest()}
        with patch.object(entry, "_run") as network:
            self.assertEqual(entry._download(item, path, offline=True), path)
        network.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, "离线"):
            entry._download({**item, "sha256": "0" * 64}, path, offline=True)

    def test_archive_paths_cannot_escape_install_directory(self):
        archive = self.root / "malicious.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("../outside.txt", "must not escape")
        with self.assertRaisesRegex(RuntimeError, "不安全"):
            entry._unzip(archive, self.root / "runtime/staging")
        self.assertFalse((self.root / "runtime/outside.txt").exists())
        with self.assertRaisesRegex(RuntimeError, "应用目录"):
            entry._within_app(self.root.parent / "foreign")

    def test_existing_incomplete_component_is_preserved(self):
        source = self.root / "runtime/new-component"
        source.mkdir(parents=True)
        (source / "node.exe").write_bytes(b"new component")
        original = self.file("runtime/node/customer-preserved.txt", b"keep")
        with self.assertRaisesRegex(RuntimeError, "不会覆盖"):
            entry._copy_new_component(source, self.root / "runtime/node")
        self.assertEqual(original.read_bytes(), b"keep")
        self.assertFalse((original.parent / "node.exe").exists())

    def test_prepare_preserves_config_and_private_customer_data(self):
        config = self.file("config.toml", b"private configuration remains untouched")
        database = self.file("storage/creator/creator.db", b"customer database remains untouched")
        self.file("resource/fonts/NotoSansSC.ttf")
        self.file("resource/fonts/NotoSansSC-OFL.txt")
        with patch.object(entry.sys, "executable", str(self.root / "runtime/python/python.exe")), \
             patch.object(entry, "_python_dependencies", return_value=[]), patch.object(entry, "_node", return_value=self.root / "runtime/node/node.exe"), \
             patch.object(entry, "_media", return_value=(self.root / "runtime/ffmpeg/ffmpeg.exe", self.root / "runtime/ffmpeg/ffprobe.exe")), \
             patch.object(entry, "_node_dependencies", return_value=[]), patch.object(entry, "_select_browsers", return_value={"render_source": "app_chromium", "publish_source": "app_chromium"}), \
             patch.object(entry, "_vendor_ready", return_value=True), patch.object(entry, "_download") as download:
            entry.prepare(offline=True)
        self.assertEqual(config.read_bytes(), b"private configuration remains untouched")
        self.assertEqual(database.read_bytes(), b"customer database remains untouched")
        download.assert_not_called()

    def test_legacy_python_never_installs_into_shared_or_target_environment(self):
        self.file("config.toml", b"keep old customer settings")
        with patch.object(entry, "inspect", return_value={"ready": False}), patch.object(entry, "_run") as command, \
             patch.object(entry, "_download") as download, self.assertRaisesRegex(RuntimeError, "仅检查和复用"):
            entry.prepare()
        command.assert_not_called()
        download.assert_not_called()
        self.assertEqual((self.root / "config.toml").read_bytes(), b"keep old customer settings")

    def test_global_scrapling_does_not_masquerade_as_isolated_target(self):
        with patch.object(entry, "_run") as command:
            self.assertFalse(entry._vendor_ready())
        command.assert_not_called()

    def test_release_manifest_contains_only_https_hashed_assets(self):
        data = json.loads(_PATH.with_name("runtime-manifest.json").read_text("utf-8"))
        for name in ("python", "node", "ffmpeg", "pip", "font", "font_license"):
            self.assertTrue(data[name]["url"].startswith("https://"))
            self.assertRegex(data[name]["sha256"], r"^[0-9a-f]{64}$")
        self.assertIn("GPL", data["ffmpeg"]["license"])

    def test_verified_system_browser_serves_render_and_publish_without_download(self):
        system = self.file("system/Edge/Application/msedge.exe")
        with patch.object(entry, "_system_browser_candidates", return_value=[system]), \
             patch.object(entry, "_browser_probe", return_value={"version": "154.0", "headless_launch": True, "rendered": True, "hyperframes_rendered": True}) as probe, \
             patch.object(entry, "_run") as install:
            entry._prepare_browsers({}, offline=True)
            env = entry.environment()
        self.assertEqual(env["HYPERFRAMES_BROWSER_PATH"], str(system))
        self.assertEqual(env["MPT_BROWSER_PATH"], str(system))
        self.assertEqual(entry._VERIFIED_BROWSERS["publish_source"], "system_edge")
        probe.assert_called_once()
        install.assert_not_called()

    def test_complete_app_browsers_are_preferred_over_system_browser(self):
        shell = self.file("runtime/browser/chromium_headless_shell-1243/chrome-headless-shell-win64/chrome-headless-shell.exe")
        headed = self.file("runtime/browser/chromium-1243/chrome-win64/chrome.exe")
        system = self.file("system/Chrome/Application/chrome.exe")
        with patch.object(entry, "_system_browser_candidates", return_value=[system]), \
             patch.object(entry, "_browser_probe", return_value={"version": "145.0", "headless_launch": True, "rendered": True, "hyperframes_rendered": True}) as probe:
            selected = entry._select_browsers({})
        self.assertEqual(selected["render"], shell)
        self.assertEqual(selected["publish"], headed)
        self.assertEqual(selected["render_source"], "app_chromium")
        self.assertEqual(probe.call_count, 2)
        self.assertNotIn(system, [call.args[0] for call in probe.call_args_list])

    def test_broken_app_browser_falls_back_only_after_actual_launch_check(self):
        broken = self.file("runtime/browser/chromium-1243/chrome-win64/chrome.exe")
        system = self.file("system/Chrome/Application/chrome.exe")

        def probe(path, _env):
            if path == broken:
                raise RuntimeError("Executable exists but will not launch")
            return {"version": "154.0", "headless_launch": True, "rendered": True, "hyperframes_rendered": True}

        with patch.object(entry, "_system_browser_candidates", return_value=[system]), patch.object(entry, "_browser_probe", side_effect=probe):
            selected = entry._select_browsers({})
        self.assertEqual(selected["render"], system)
        self.assertEqual(selected["publish_source"], "system_chrome")
        self.assertEqual(entry.environment()["MPT_BROWSER_PATH"], str(system))

    def test_browser_download_is_bounded_and_does_not_disable_tls(self):
        with patch.object(entry, "_select_browsers", side_effect=[None, {"render_source": "app_chromium"}]), \
             patch.object(entry, "_run") as install:
            entry._prepare_browsers({"NODE_TLS_REJECT_UNAUTHORIZED": "0"})
        call = install.call_args
        self.assertEqual(call.kwargs["timeout"], 300)
        self.assertEqual(call.kwargs["env"]["PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT"], "30000")
        self.assertNotIn("NODE_TLS_REJECT_UNAUTHORIZED", call.kwargs["env"])
        self.assertEqual(call.args[0][-2:], ["install", "chromium"])

    def test_missing_browser_offline_does_not_start_download(self):
        with patch.object(entry, "_select_browsers", return_value=None), patch.object(entry, "_run") as install:
            with self.assertRaisesRegex(RuntimeError, "Edge／Chrome"):
                entry._prepare_browsers({}, offline=True)
        install.assert_not_called()

    def test_command_timeout_stops_only_its_owned_windows_process_tree(self):
        process = Mock(pid=4567, returncode=None)
        process.communicate.side_effect = [subprocess.TimeoutExpired(["node", "install"], 300), ("", "")]
        with patch.object(entry.os, "name", "nt"), patch.object(entry.subprocess, "Popen", return_value=process), \
             patch.object(entry.subprocess, "run") as stop:
            with self.assertRaisesRegex(RuntimeError, "超时"):
                entry._run(["node", "install"], timeout=300)
        self.assertEqual(stop.call_args.args[0], ["taskkill", "/PID", "4567", "/T", "/F"])

    def test_installation_logs_are_streamed_instead_of_hidden_until_completion(self):
        process = Mock(returncode=0)
        process.communicate.return_value = (None, None)
        with patch.object(entry.subprocess, "Popen", return_value=process) as launch:
            entry._run(["node", "install"], timeout=300)
        self.assertIsNone(launch.call_args.kwargs["stdout"])


if __name__ == "__main__":
    unittest.main()
