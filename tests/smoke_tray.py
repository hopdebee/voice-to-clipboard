"""Run with system Python under xvfb-run; no real recordings or settings writes."""

import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gi.repository import GLib

import dictation_settings as settings
import tray_app

config = settings.validate_config(
    {"hotwords_file": "/tmp/words.txt", "hotwords_priority": ["NON", "ABA-DL pipeline"]}
)
glossary = "gene\ngenes\nNON\nUP\nDOWN\nABA-DL\nABA-DL pipeline\n"
calls = []


def request(command, payload=None, **kwargs):
    calls.append((command, payload))
    if command == "status":
        return {
            "state": "idle",
            "enabled": True,
            "model": "small",
            "hotwords": {"active": 7, "loaded": 7},
        }
    if command == "configuration":
        return {"config": config, "glossary": glossary}
    if command == "devices":
        return {"devices": [{"name": "Test microphone", "channels": 1}]}
    if command == "preview-config":
        return {
            "hotwords": {
                "active": 7,
                "loaded": 7,
                "tokens": 40,
                "token_limit": 223,
                "active_terms": settings.vocabulary_terms(glossary),
            }
        }
    return {"pending": False}


app = tray_app.TrayApplication()
failure = []
deadline = time.monotonic() + 15


def check():
    if app.settings_window is None and time.monotonic() < deadline:
        return True
    try:
        window = app.settings_window
        assert window is not None
        assert len(window.vocab_store) == 7
        assert window.vocab_count.get_text().startswith("7 active / 7 listed")
        draft = window.collect()
        assert draft["glossary"] is None, "Untouched glossary contents would be rewritten"
        assert draft["config"]["hotkeys"]["toggle"] == "F8"
        window.shortcut_entries["toggle"].set_text("Alt+F10")
        window.save()
        assert any(command == "preview-config" for command, _ in calls)
        print(
            "GTK settings built; vocabulary preview, case, "
            "untouched-file preservation and Save checked."
        )
    except Exception as exc:
        failure.append(exc)
    GLib.timeout_add(400, finish)
    return False


def finish():
    try:
        saved = [payload for command, payload in calls if command == "apply-config"]
        assert saved and saved[-1]["config"]["hotkeys"]["toggle"] == "Alt+F10"

        def press():
            subprocess.run(
                ["xdotool", "key", "ctrl+shift+F11"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return False

        def cancel_if_stuck():
            for window in tray_app.Gtk.Window.list_toplevels():
                if window.get_title() == "Capture shortcut":
                    window.response(tray_app.Gtk.ResponseType.CANCEL)
            return False

        GLib.timeout_add(250, press)
        GLib.timeout_add(2500, cancel_if_stuck)
        entry = app.settings_window.shortcut_entries["toggle"]
        app.settings_window.capture_shortcut(entry)

        # capture_shortcut begins asynchronously; assert after the dialog finishes.
        def captured():
            try:
                assert entry.get_text() == "Ctrl+Shift+F11", entry.get_text()
                print("GTK keyboard capture checked with a real X11 key combination.")
            except Exception as exc:
                failure.append(exc)
            app.quit()
            return False

        GLib.timeout_add(2800, captured)
    except Exception as exc:
        failure.append(exc)
        app.quit()
    return False


with (
    patch.object(tray_app.core, "send_command", side_effect=request),
    patch.object(tray_app.core, "active_window", return_value="1"),
    patch.object(tray_app.core, "is_own_window", return_value=False),
):
    GLib.timeout_add(1500, check)
    app.run(["smoke-tray", "--settings"])
if failure:
    raise failure[0]
