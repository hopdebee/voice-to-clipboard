import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import talk_and_path as app
from scripts.install_dictation import install_editor_bindings


def segment(*words):
    return SimpleNamespace(words=[SimpleNamespace(start=t, word=w) for t, w in words],
                           text="".join(w for _, w in words))


class MergeTests(unittest.TestCase):
    def test_paths_use_original_timeline_including_pauses(self):
        segments = [segment((0.2, "Compare"), (1.0, " this")), segment((8.0, " with"))]
        events = [{"time": 9, "path": "second.py"}, {"time": 4, "path": "first.py"}]
        self.assertEqual(app.merge_paths_into_transcript(segments, events),
                         "Compare this `first.py` with `second.py`")

    def test_equal_timestamp_and_multiple_paths(self):
        events = [{"time": 0, "path": "src/a.py"}, {"time": 0, "path": "src/b.py"}]
        self.assertEqual(app.merge_paths_into_transcript([segment((0, "Compare"))], events),
                         "`src/a.py` `src/b.py` Compare")

    def test_no_words_falls_back_to_segment_text(self):
        segments = [SimpleNamespace(words=None, text="Hello")]
        self.assertEqual(app.merge_paths_into_transcript(segments, [{"time": 1, "path": "a.py"}]),
                         "Hello `a.py`")

    def test_spaces_and_backticks_in_paths(self):
        self.assertEqual(app.markdown_path("my folder/a.py"), "`my folder/a.py`")
        self.assertEqual(app.markdown_path("a`b.py"), "``a`b.py``")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.model = SimpleNamespace()
        self.service = app.DictationService(dict(app.DEFAULT_CONFIG), model=self.model)

    def test_busy_service_cannot_overwrite_recording(self):
        self.service.state = "transcribing"
        with self.assertRaisesRegex(RuntimeError, "transcribing"):
            self.service.start()

    def test_cancel_discards_audio_and_path_events(self):
        self.service.state = "recording"
        self.service.chunks = ["audio"]
        self.service.events = [{"time": 1, "path": "a.py"}]
        with patch.object(app, "notify"):
            self.service.cancel()
        self.assertEqual(self.service.state, "idle")
        self.assertEqual(self.service.chunks, [])
        self.assertEqual(self.service.events, [])

    def test_cancelled_inference_does_not_touch_clipboard(self):
        import numpy as np
        self.service.recording_id = "new-session"
        self.model.transcribe = lambda *a, **kw: ([segment((0, "hello"))], SimpleNamespace(language="en"))
        with patch.object(app, "clipboard_set") as clipboard:
            self.service.process_recording([np.zeros((16000, 1), dtype=np.float32)], [], "1", True, "old-session")
        clipboard.assert_not_called()

    def test_transcription_receives_cased_hotwords_and_keeps_timestamps(self):
        import numpy as np
        self.service.hotword_hints["text"] = "gene, genes, NON, UP, DOWN, ABA-DL, ABA-DL pipeline"
        self.model.transcribe = Mock(return_value=([], SimpleNamespace(language="en")))
        # Suppress delivery after decoding, so this check does not touch the desktop.
        self.service.recording_id = None
        self.service.process_recording([np.zeros((16000, 1), dtype=np.float32)], [], "1", True, "cancelled")
        options = self.model.transcribe.call_args.kwargs
        self.assertEqual(options["hotwords"], self.service.hotword_hints["text"])
        self.assertTrue(options["word_timestamps"])
        self.assertTrue(options["vad_filter"])

    def test_focus_change_keeps_transcript_on_clipboard_without_paste(self):
        import numpy as np
        self.service.recording_id = "session"
        self.model.transcribe = lambda *a, **kw: ([segment((0, "hello"))], SimpleNamespace(language="en"))
        with tempfile.TemporaryDirectory() as temp, patch.object(app, "STATE_DIR", Path(temp)), \
                patch.object(app, "clipboard_set") as clipboard, patch.object(app, "notify"), \
                patch.object(app, "active_window", return_value="other-window"), \
                patch.object(app, "send_hotkey") as keys:
            self.service.process_recording([np.zeros((16000, 1), dtype=np.float32)], [], "origin", True, "session")
        clipboard.assert_called_once_with("hello")
        keys.assert_not_called()

    def test_failed_path_copy_restores_clipboard_and_adds_no_event(self):
        self.service.state = "recording"
        self.service.recording_id = "session"
        result = SimpleNamespace(stdout="X=1\nY=2\nWINDOW=123\n")
        # Empty responses simulate a missing Explorer keybinding.
        with patch.object(app, "run", return_value=result), \
                patch.object(app, "is_code_window", return_value=True), \
                patch.object(app, "clipboard_get", side_effect=["original clipboard", None]), \
                patch.object(app, "clipboard_set") as clipboard, \
                patch.object(app, "send_hotkey"), patch.object(app.time, "sleep"), \
                patch.object(app.time, "monotonic", side_effect=[0, 0, 2]):
            with self.assertRaisesRegex(RuntimeError, "No file path"):
                self.service.capture_hovered_path(1)
        self.assertEqual(clipboard.call_args.args, ("original clipboard",))
        self.assertEqual(self.service.events, [])


class HotwordTests(unittest.TestCase):
    def setUp(self):
        # A deterministic character tokenizer exercises limits without loading Whisper.
        self.model = SimpleNamespace(max_length=448,
            hf_tokenizer=SimpleNamespace(encode=lambda text, **kw: SimpleNamespace(ids=list(text))))

    def test_disabled_glossary_does_not_require_a_model(self):
        result = app.load_hotwords(dict(app.DEFAULT_CONFIG), None)
        self.assertIsNone(result["text"])
        self.assertEqual(result["active"], 0)

    def test_bom_comments_duplicates_phrases_and_case(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "words.txt"
            path.write_text("\ufeff# project\ngene\ngenes\nNON\nUP\nDOWN\nABA-DL\nABA-DL pipeline\ngene\n\n")
            result = app.load_hotwords({"hotwords_file": str(path)}, self.model)
        self.assertEqual(result["loaded"], 7)
        self.assertEqual(result["text"], "gene, genes, NON, UP, DOWN, ABA-DL, ABA-DL pipeline")

    def test_priority_terms_fit_first_and_oversized_terms_are_not_split(self):
        self.model.max_length = 30
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "words.txt"
            path.write_text("oversized-word-that-will-not-fit\ngene\nNON\nUP\n")
            result = app.load_hotwords({"hotwords_file": str(path),
                                       "hotwords_priority": ["NON", "UP"]}, self.model)
        self.assertEqual(result["text"], "NON, UP, gene")
        self.assertEqual(result["loaded"], 4)
        self.assertEqual(result["omitted"], 1)
        self.assertLessEqual(result["tokens"], result["token_limit"])

    def test_glossary_edits_are_read_again(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "words.txt"
            path.write_text("gene\n")
            config = {"hotwords_file": str(path)}
            self.assertEqual(app.load_hotwords(config, self.model)["text"], "gene")
            path.write_text("gene\nABA-DL pipeline\n")
            self.assertEqual(app.load_hotwords(config, self.model)["text"], "gene, ABA-DL pipeline")

    def test_unreadable_glossary_preserves_dictation_with_visible_error(self):
        service = app.DictationService({**app.DEFAULT_CONFIG,
                                       "hotwords_file": "/nonexistent/talk-and-path/words.txt"},
                                      model=self.model)
        with patch.object(app, "notify") as notification:
            service.refresh_hotwords()
        self.assertIsNone(service.hotword_hints["text"])
        self.assertIsNotNone(service.dispatch("status")["hotwords"]["error"])
        notification.assert_called_once()


class InstallerTests(unittest.TestCase):
    def test_jsonc_comments_trailing_comma_and_string_values_survive(self):
        for original in [
            '// header\n[{"key":"ctrl+k","command":"existing"} // tail\n]\n',
            '[{"key":"ctrl+k","command":"existing", "args":",]"}, /* tail */]\n',
            '[]\n',
        ]:
            with self.subTest(original=original), tempfile.TemporaryDirectory() as temp, \
                    patch("scripts.install_dictation.backup"):
                path = Path(temp) / "keybindings.json"
                path.write_text(original)
                install_editor_bindings(path)
                first = path.read_text()
                # Reinstallation also parses the result, detecting malformed JSONC.
                install_editor_bindings(path)
                self.assertEqual(path.read_text(), first)
                self.assertIn("copyFilePath", first)
                if "existing" in original:
                    self.assertIn("existing", first)

    def test_conflicting_shortcut_does_not_modify_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "keybindings.json"
            original = json.dumps([{"key": "ctrl+alt+shift+p", "command": "existing"}])
            path.write_text(original)
            with self.assertRaisesRegex(ValueError, "already has another action"):
                install_editor_bindings(path)
            self.assertEqual(path.read_text(), original)


if __name__ == "__main__":
    unittest.main()
