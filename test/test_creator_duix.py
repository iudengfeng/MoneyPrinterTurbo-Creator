import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.creator import avatar, duix, store


class DuixPortableConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.base = Path(self.folder.name)
        self.legacy = self.base / "legacy-service"
        self.legacy_avatar = self.base / "legacy-shared" / "temp"
        self.runtime = self.base / "portable-runtime"
        self.runtime.mkdir()
        self.python = self.runtime / "python.exe"
        self.python.write_bytes(b"test executable")
        self.ffmpeg = self.runtime / "ffmpeg.exe"
        self.ffmpeg.write_bytes(b"test executable")
        environment = patch.dict(os.environ, {"MPT_CREATOR_DATA": str(self.base / "creator"),
                                 "MPT_FUSION_ROOT": "", "MPT_AVATAR_ROOT": "", "MPT_DUIX_DB": ""})
        environment.start()
        self.addCleanup(environment.stop)
        for replacement in (patch.object(duix, "_LEGACY_ROOT", self.legacy),
                            patch.object(avatar, "_LEGACY_AVATAR_ROOT", self.legacy_avatar),
                            patch.object(duix.sys, "executable", str(self.python)),
                            patch.object(duix.extract, "ffmpeg_binary", return_value=str(self.ffmpeg))):
            replacement.start()
            self.addCleanup(replacement.stop)
        avatar._STATUS_CACHE.clear()
        self.addCleanup(avatar._STATUS_CACHE.clear)

    def settings(self, folder, values):
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "settings.json"
        path.write_text(json.dumps(values), "utf-8")
        return path

    def test_no_deployment_uses_current_runtime_and_idle_local_directory_without_starting_any_service(self):
        cfg = duix._settings()
        expected_root = store.data_root() / "integrations" / "duix"
        self.assertEqual(expected_root, cfg["root"])
        self.assertEqual(str(self.python), cfg["python"])
        self.assertEqual(str(self.ffmpeg), cfg["ffmpeg"])
        self.assertFalse(cfg["configured"])
        self.assertFalse(cfg["legacy_deployment"])
        self.assertFalse(expected_root.exists())
        self.assertNotIn("shipin", json.dumps(cfg, default=str))
        with patch.object(duix, "_session") as session, patch.object(avatar, "_service_states") as docker:
            self.assertEqual([], duix.list_profiles()["voices"])
            self.assertEqual([], avatar.list_options())
            state = avatar.status()
            self.assertFalse(state["available"])
            self.assertIn("尚未配置", state["reason"])
            with self.assertRaisesRegex(RuntimeError, "尚未配置"):
                duix._connect()
            session.assert_not_called()
            docker.assert_not_called()

    def test_an_existing_legacy_directory_alone_is_not_assumed_to_be_a_deployment(self):
        self.legacy.mkdir()
        (self.legacy / "server.py").write_text("legacy", "utf-8")
        self.legacy_avatar.mkdir(parents=True)
        cfg = duix._settings()
        self.assertEqual("unconfigured", cfg["configuration_source"])
        self.assertNotEqual(self.legacy, cfg["root"])
        self.assertNotEqual(self.legacy_avatar, avatar._configuration()["avatar_root"])

    def test_environment_root_wins_over_local_and_legacy_settings(self):
        explicit = self.base / "explicit-service"
        self.settings(explicit, {"port": 18602, "avatar_root": "face2face/temp"})
        integration = store.data_root() / "integrations" / "duix"
        local = self.settings(integration, {"root": str(self.base / "wrong-service"), "port": 18603})
        self.settings(self.legacy, {"port": 18604})
        before = local.read_bytes()
        with patch.dict(os.environ, {"MPT_FUSION_ROOT": str(explicit)}):
            cfg = duix._settings()
            self.assertEqual(explicit, cfg["root"])
            self.assertEqual(18602, cfg["port"])
            self.assertEqual("environment", cfg["configuration_source"])
            self.assertEqual(explicit / "face2face/temp", avatar._configuration()["avatar_root"])
        self.assertEqual(before, local.read_bytes())

    def test_local_integration_settings_keep_explicit_existing_paths_without_mutating_service_settings(self):
        existing = self.base / "installed-fusion"
        old_python = self.base / "old-python.exe"
        service_file = self.settings(existing, {"python": str(old_python), "port": 18599})
        integration = store.data_root() / "integrations" / "duix"
        shared = self.base / "actual-shared" / "temp"
        local_file = self.settings(integration, {"root": str(existing), "python": str(self.python),
            "ffmpeg": str(self.ffmpeg), "hey_db": str(self.base / "existing.db"), "port": 18600,
            "avatar_root": str(shared), "avatar_url": "http://127.0.0.1:8383/easy"})
        before = (service_file.read_bytes(), local_file.read_bytes())
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MPT_DUIX_DB", None)
            cfg = duix._settings()
            native = avatar._configuration()
        self.assertEqual("integration", cfg["configuration_source"])
        self.assertTrue(cfg["configured"])
        self.assertEqual(existing, cfg["root"])
        self.assertEqual(str(self.python), cfg["python"])
        self.assertEqual(str(self.ffmpeg), cfg["ffmpeg"])
        self.assertEqual(str(self.base / "existing.db"), cfg["hey_db"])
        self.assertEqual(shared, native["avatar_root"])
        self.assertEqual(18600, cfg["port"])
        self.assertEqual(before, (service_file.read_bytes(), local_file.read_bytes()))

    def test_real_legacy_settings_and_shared_directory_are_retained_as_explicit_deployment(self):
        settings = self.settings(self.legacy, {"python": str(self.python), "ffmpeg": str(self.ffmpeg), "port": 18600})
        self.legacy_avatar.mkdir(parents=True)
        before = settings.read_bytes()
        cfg = duix._settings()
        self.assertEqual(self.legacy, cfg["root"])
        self.assertEqual("legacy", cfg["configuration_source"])
        self.assertTrue(cfg["legacy_deployment"])
        self.assertEqual(self.legacy_avatar, avatar._configuration()["avatar_root"])
        self.assertEqual(before, settings.read_bytes())

    def test_avatar_environment_overrides_settings_and_new_root_defaults_are_relative(self):
        root = self.base / "new-service"
        self.settings(root, {"avatar_root": "configured-shared/temp"})
        override = self.base / "env-shared" / "temp"
        with patch.dict(os.environ, {"MPT_FUSION_ROOT": str(root), "MPT_AVATAR_ROOT": str(override)}):
            self.assertEqual(override, avatar._configuration()["avatar_root"])
        with patch.dict(os.environ, {"MPT_FUSION_ROOT": str(root)}):
            self.assertEqual(root / "configured-shared/temp", avatar._configuration()["avatar_root"])
        (root / "settings.json").write_text("{}", "utf-8")
        with patch.dict(os.environ, {"MPT_FUSION_ROOT": str(root)}):
            self.assertEqual(root / "face2face/temp", avatar._configuration()["avatar_root"])

    def test_no_ffmpeg_does_not_make_optional_duix_list_or_status_break_basic_workbench(self):
        with patch.object(duix.extract, "ffmpeg_binary", side_effect=RuntimeError("not installed")):
            cfg = duix._settings()
            self.assertEqual("", cfg["ffmpeg"])
            self.assertEqual([], duix.list_profiles()["models"])
            self.assertFalse(avatar.status()["available"])


if __name__ == "__main__":
    unittest.main()
