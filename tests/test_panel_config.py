import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import dictation_settings as settings
import talk_and_path as app


class ConfigurationTests(unittest.TestCase):
    def test_legacy_configuration_gains_defaults_without_losing_vocabulary(self):
        config = settings.validate_config(
            {"model": "base", "hotwords_priority": ["NON", "ABA-DL pipeline"]}
        )
        self.assertEqual(config["model"], "base")
        self.assertEqual(config["hotkeys"], {"toggle": "F8", "path": "F9", "cancel": "Shift+F8"})
        self.assertEqual(config["hotwords_priority"], ["NON", "ABA-DL pipeline"])

    def test_shortcuts_canonicalise_modifiers_and_reject_ambiguity(self):
        shortcut = settings.parse_shortcut("shift+control+f10")
        self.assertEqual(shortcut["label"], "Ctrl+Shift+F10")
        self.assertEqual(shortcut["hotKey"], "<f10>")
        self.assertEqual(shortcut["xkey"], "F10")
        for invalid in ["a", "Ctrl+", "Ctrl+Ctrl+F8", "Banana+F8"]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                settings.parse_shortcut(invalid)

    def test_conflicting_actions_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "different shortcut"):
            settings.validate_config({"hotkeys": {"toggle": "F9"}})

    def test_existing_autokey_script_is_preserved_when_binding_conflicts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            other = root / "Other"
            other.mkdir()
            metadata = other / ".Existing.json"
            original = json.dumps(
                {
                    "description": "Existing macro",
                    "modes": [3],
                    "hotkey": {"hotKey": "<f8>", "modifiers": []},
                }
            )
            metadata.write_text(original)
            with self.assertRaisesRegex(ValueError, "Existing macro"):
                settings.autokey_files(settings.validate_config({}), root)
            self.assertEqual(metadata.read_text(), original)

    def test_configured_shortcut_updates_script_and_metadata_together(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = settings.validate_config({"hotkeys": {"toggle": "Alt+F10"}})
            files = settings.autokey_files(config, root)
            metadata = next(
                data
                for path, data in files.items()
                if path.name == ".Dictation - Start or finish.json"
            )
            code = next(
                data.decode()
                for path, data in files.items()
                if path.name == "Dictation - Start or finish.py"
            )
            self.assertEqual(
                json.loads(metadata)["hotkey"], {"hotKey": "<f10>", "modifiers": ["<alt>"]}
            )
            self.assertIn("string_to_keysym('F10')", code)
            compile(code, "autokey", "exec")

    def test_failed_save_rolls_back_config_and_glossary(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, glossary = root / "config.json", root / "words.txt"
            config.write_bytes(b"old config")
            glossary.write_bytes(b"NON\nUP\n")
            write = settings.atomic_write

            def failing(path, data, mode=0o600):
                if path == glossary and data == b"DOWN\n":
                    raise OSError("Simulated disk failure")
                write(path, data, mode)

            with patch.object(settings, "atomic_write", side_effect=failing):
                with self.assertRaises(OSError):
                    settings.commit_files(
                        {config: b"new config", glossary: b"DOWN\n"}, root / "backups"
                    )
            self.assertEqual(config.read_bytes(), b"old config")
            self.assertEqual(glossary.read_bytes(), b"NON\nUP\n")

    def test_startup_entry_can_be_disabled(self):
        self.assertIn(b"Hidden=true", settings.startup_entry(False))
        self.assertIn(b"Hidden=false", settings.startup_entry(True))

    def test_failed_runtime_apply_also_rolls_back_files(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_bytes(b"old")
            hook = Mock(side_effect=RuntimeError("AutoKey did not acknowledge"))
            with self.assertRaisesRegex(RuntimeError, "acknowledge"):
                settings.commit_files({path: b"new"}, Path(temp) / "backups", after_write=hook)
            self.assertEqual(path.read_bytes(), b"old")

    def test_external_glossary_changes_are_not_overwritten(self):
        import hashlib

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "words.txt"
            original = "gene\nNON\n"
            revision = hashlib.sha256(original.encode()).hexdigest()
            path.write_text(original + "User added this\n")
            with self.assertRaisesRegex(ValueError, "changed outside Settings"):
                settings.save_configuration({"hotwords_file": str(path)}, "gene\nUP\n", revision)
            self.assertEqual(path.read_text(), original + "User added this\n")


class BackendPanelTests(unittest.TestCase):
    def setUp(self):
        self.config = settings.validate_config({})
        self.model = object()
        self.service = app.DictationService(self.config, model=self.model)

    def test_status_poll_does_not_start_stopped_service(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(app, "SOCKET_PATH", Path(temp) / "missing.sock"),
            patch.object(app, "run") as run,
        ):
            with self.assertRaisesRegex(RuntimeError, "stopped"):
                app.send_command("status", autostart=False)
        run.assert_not_called()

    def test_pausing_still_allows_finishing_an_existing_recording(self):
        self.service.state = "recording"
        self.service.dispatch("set-enabled", payload={"enabled": False})
        with patch.object(self.service, "stop") as stop:
            self.service.toggle()
        stop.assert_called_once()
        self.service.state = "idle"
        with self.assertRaisesRegex(RuntimeError, "paused"):
            self.service.start()

    def test_configuration_is_deferred_until_transcription_finishes(self):
        new = settings.validate_config({"language": "en"})
        self.service.state = "transcribing"
        with (
            patch.object(settings, "configuration_files"),
            patch.object(settings, "save_configuration", return_value=new) as save,
            patch.object(self.service, "refresh_hotwords"),
            patch.object(app, "notify"),
        ):
            result = self.service.dispatch("apply-config", payload={"config": new})
            self.assertTrue(result["pending"])
            save.assert_not_called()
            self.assertIsNone(self.service.config["language"])
            self.service.apply_pending()
            save.assert_not_called()
            self.service.state = "idle"
            self.service.apply_pending()
            save.assert_called_once()
        self.assertEqual(self.service.config["language"], "en")

    def test_unchanged_model_is_not_reloaded_when_saving_language(self):
        new = settings.validate_config({"language": "nl"})
        with (
            patch.object(settings, "save_configuration", return_value=new),
            patch.object(self.service, "refresh_hotwords"),
            patch.object(app, "notify"),
            patch.object(app.threading, "Thread") as thread,
        ):
            self.service.apply_configuration(new)
        thread.assert_not_called()
        self.assertIs(self.service.model, self.model)

    def test_menu_recording_uses_remembered_target_and_selected_microphone(self):
        self.service.config["input_device"] = "My microphone"
        stream = Mock()
        with (
            patch("sounddevice.InputStream", return_value=stream) as input_stream,
            patch.object(app, "active_window") as active,
            patch.object(app, "is_own_window", return_value=False),
            patch.object(app, "is_code_window", return_value=False),
            patch.object(app.threading, "Timer"),
            patch.object(app, "notify"),
        ):
            self.service.start(origin="77")
            self.assertEqual(self.service.origin, "77")
            active.assert_not_called()
            self.assertEqual(input_stream.call_args.kwargs["device"], "My microphone")
            self.service.cancel()
        stream.close.assert_called_once()

    def test_shortcut_capture_temporarily_ignores_only_dictation_hotkeys(self):
        self.service.dispatch("suspend-hotkeys", payload={"enabled": True})
        with patch.object(self.service, "toggle") as toggle:
            result = self.service.dispatch("toggle", payload={"source": "hotkey"})
            self.assertTrue(result["ignored"])
            toggle.assert_not_called()
            self.service.dispatch("toggle", payload={})
            toggle.assert_called_once()

    def test_restart_waits_for_recording_without_canceling_it(self):
        self.service.state = "recording"
        with patch.object(self.service, "cancel") as cancel:
            response = self.service.dispatch("restart")
        self.assertTrue(response["pending"])
        self.assertEqual(self.service.state, "recording")
        self.assertEqual(self.service.pending_action, "restart")
        cancel.assert_not_called()


if __name__ == "__main__":
    unittest.main()
