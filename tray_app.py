#!/usr/bin/python3
"""GTK panel frontend. Whisper remains in the separate dictation service."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3 as Indicator  # noqa: E402
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

import dictation_settings as settings  # noqa: E402
import talk_and_path as core  # noqa: E402

ICON_DIR = Path(__file__).resolve().parent / "assets/tray"


def label(text="", wrap=False):
    widget = Gtk.Label(label=text, xalign=0)
    widget.set_line_wrap(wrap)
    return widget


def scroll(child, height=None):
    widget = Gtk.ScrolledWindow()
    widget.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    if height:
        widget.set_min_content_height(height)
    widget.add(child)
    return widget


def row(box, title, widget):
    line = Gtk.Box(spacing=12)
    text = label(title)
    text.set_size_request(175, -1)
    line.pack_start(text, False, False, 0)
    line.pack_start(widget, True, True, 0)
    box.pack_start(line, False, False, 0)
    return widget


class SettingsWindow(Gtk.ApplicationWindow):
    def __init__(self, application, config, glossary="", glossary_error=None):
        super().__init__(application=application, title="Talk and Path — Settings")
        self.set_wmclass("talk-and-path", "Talk-and-path")
        self.set_default_size(780, 710)
        self.app = application
        self.config = copy.deepcopy(config)
        self.preview_generation = 0
        self.preview_timer = None
        self.testing = False
        self.is_closed = False
        self.loaded_path = config.get("hotwords_file") or ""
        self.original_glossary = glossary
        self.original_terms = settings.vocabulary_terms(glossary)
        self.connect("destroy", self.closed)
        header = Gtk.HeaderBar(title="Talk and Path", subtitle="Local voice dictation")
        header.set_show_close_button(True)
        self.set_titlebar(header)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.set_border_width(12)
        self.add(outer)
        self.status_label = label("Connecting to dictation…")
        outer.pack_start(self.status_label, False, False, 0)
        tabs = Gtk.Notebook()
        outer.pack_start(tabs, True, True, 0)
        self.shortcut_entries = {}
        self.build_shortcuts(tabs)
        self.build_vocabulary(tabs, glossary)
        self.build_audio(tabs)
        self.build_service(tabs)
        self.message = label("", wrap=True)
        self.message.set_max_width_chars(90)
        outer.pack_start(self.message, False, False, 0)
        buttons = Gtk.Box(spacing=8)
        outer.pack_start(buttons, False, False, 0)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.destroy())
        self.save_button = Gtk.Button(label="Save changes")
        self.save_button.get_style_context().add_class("suggested-action")
        self.save_button.connect("clicked", self.save)
        buttons.pack_end(self.save_button, False, False, 0)
        buttons.pack_end(close, False, False, 0)
        self.show_all()
        if glossary_error:
            self.set_message("The glossary could not be read: " + glossary_error, True)
        self.app.call("devices", callback=self.populate_devices)
        self.schedule_preview()

    def page(self, tabs, title):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=13)
        box.set_border_width(18)
        tabs.append_page(box, Gtk.Label(label=title))
        return box

    def set_message(self, message, error=False):
        if self.is_closed:
            return
        color = "#b91c1c" if error else "#25633b"
        self.message.set_markup(
            f'<span foreground="{color}">{GLib.markup_escape_text(message)}</span>'
        )

    def build_shortcuts(self, tabs):
        box = self.page(tabs, "Shortcuts")
        box.pack_start(
            label("Choose a shortcut by typing it or using Capture.", True), False, False, 0
        )
        for command, title in [
            ("toggle", "Start / finish"),
            ("path", "Insert hovered path"),
            ("cancel", "Cancel dictation"),
        ]:
            line = Gtk.Box(spacing=8)
            entry = Gtk.Entry(text=self.config["hotkeys"][command])
            self.shortcut_entries[command] = entry
            line.pack_start(entry, True, True, 0)
            capture = Gtk.Button(label="Capture…")
            capture.connect("clicked", lambda _, target=entry: self.capture_shortcut(target))
            line.pack_start(capture, False, False, 0)
            row(box, title, line)
        box.pack_start(
            label(
                "Existing AutoKey assignments are checked before saving. "
                "Other AutoKey shortcuts stay available.",
                True,
            ),
            False,
            False,
            0,
        )
        advanced = Gtk.Expander(label="VS Code helper bindings")
        advanced_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        advanced_box.set_border_width(10)
        advanced_box.pack_start(
            label("Copy full path: " + self.config["copy_path_shortcut"]), False, False, 0
        )
        advanced_box.pack_start(
            label("Focus Codex: " + self.config["focus_codex_shortcut"]), False, False, 0
        )
        advanced.add(advanced_box)
        box.pack_start(advanced, False, False, 0)

    def capture_shortcut(self, target):
        if self.app.status.get("state") != "idle":
            self.set_message("Finish the current operation before capturing a shortcut.", True)
            return
        self.app.call(
            "suspend-hotkeys", {"enabled": True}, callback=lambda _: self.capture_dialog(target)
        )

    def capture_dialog(self, target):
        dialog = Gtk.Dialog(title="Capture shortcut", transient_for=self, modal=True)
        dialog.set_wmclass("talk-and-path", "Talk-and-path")
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        content = dialog.get_content_area()
        content.set_border_width(20)
        prompt = label("Press the desired key combination. Escape cancels.", True)
        content.add(prompt)
        captured = []
        grabbed = []

        def on_map(*_):
            result = Gdk.keyboard_grab(dialog.get_window(), False, Gdk.CURRENT_TIME)
            grabbed.append(result == Gdk.GrabStatus.SUCCESS)
            if not grabbed[-1]:
                prompt.set_text(
                    "Keyboard capture is unavailable. "
                    "Close this dialog and type the shortcut in the field."
                )

        def on_key(_, event):
            name = Gdk.keyval_name(event.keyval) or ""
            if name in (
                "Control_L",
                "Control_R",
                "Shift_L",
                "Shift_R",
                "Alt_L",
                "Alt_R",
                "Super_L",
                "Super_R",
                "Meta_L",
                "Meta_R",
            ):
                return True
            if name == "Escape":
                dialog.response(Gtk.ResponseType.CANCEL)
                return True
            name = {
                "ISO_Left_Tab": "Tab",
                "KP_Enter": "Return",
                "Page_Up": "PageUp",
                "Page_Down": "PageDown",
            }.get(name, name)
            modifiers = []
            for mask, text in [
                (Gdk.ModifierType.CONTROL_MASK, "Ctrl"),
                (Gdk.ModifierType.MOD1_MASK, "Alt"),
                (Gdk.ModifierType.SHIFT_MASK, "Shift"),
                (Gdk.ModifierType.SUPER_MASK, "Super"),
            ]:
                if event.state & mask:
                    modifiers.append(text)
            try:
                value = settings.parse_shortcut("+".join(modifiers + [name]))["label"]
            except ValueError as exc:
                prompt.set_text(str(exc))
                return True
            captured.append(value)
            dialog.response(Gtk.ResponseType.OK)
            return True

        dialog.connect("map-event", on_map)
        dialog.connect("key-press-event", on_key)
        dialog.show_all()
        response = dialog.run()
        Gdk.keyboard_ungrab(Gdk.CURRENT_TIME)
        dialog.destroy()
        self.app.call("suspend-hotkeys", {"enabled": False})
        if response == Gtk.ResponseType.OK and captured:
            target.set_text(captured[-1])

    def build_vocabulary(self, tabs, glossary):
        box = self.page(tabs, "Vocabulary")
        self.vocab_enabled = Gtk.CheckButton(label="Use vocabulary hints for recordings")
        self.vocab_enabled.set_active(bool(self.config.get("hotwords_file")))
        self.vocab_enabled.connect("toggled", lambda *_: self.schedule_preview())
        box.pack_start(self.vocab_enabled, False, False, 0)
        paths = Gtk.Box(spacing=6)
        self.vocab_path = Gtk.Entry(text=self.loaded_path)
        self.vocab_path.set_placeholder_text("Choose a glossary file")
        paths.pack_start(self.vocab_path, True, True, 0)
        for title, callback in [("Choose…", self.choose_glossary), ("Load", self.load_glossary)]:
            button = Gtk.Button(label=title)
            button.connect("clicked", callback)
            paths.pack_start(button, False, False, 0)
        box.pack_start(paths, False, False, 0)
        self.vocab_count = label("Checking vocabulary…")
        box.pack_start(self.vocab_count, False, False, 0)
        self.vocab_store = Gtk.ListStore(str, bool, str)
        priorities = self.config.get("hotwords_priority", [])
        terms = settings.vocabulary_terms(glossary)
        ordered = [term for term in priorities if term in terms] + [
            term for term in terms if term not in priorities
        ]
        for term in ordered:
            self.vocab_store.append([term, term in priorities, ""])
        self.initial_display_order = ordered[:]
        self.vocab_tree = Gtk.TreeView(model=self.vocab_store)
        term_cell = Gtk.CellRendererText(editable=True)
        term_cell.connect("edited", self.edit_term)
        term_column = Gtk.TreeViewColumn("Term or phrase", term_cell, text=0)
        term_column.set_expand(True)
        self.vocab_tree.append_column(term_column)
        priority_cell = Gtk.CellRendererToggle()
        priority_cell.connect("toggled", self.toggle_priority)
        self.vocab_tree.append_column(Gtk.TreeViewColumn("Priority", priority_cell, active=1))
        self.vocab_tree.append_column(
            Gtk.TreeViewColumn("Hint status", Gtk.CellRendererText(), text=2)
        )
        box.pack_start(scroll(self.vocab_tree, 220), True, True, 0)
        tools = Gtk.Box(spacing=6)
        for title, action in [
            ("Add…", self.add_term),
            ("Remove", self.remove_term),
            ("Move up", lambda *_: self.move_term(-1)),
            ("Move down", lambda *_: self.move_term(1)),
            ("Preview", lambda *_: self.preview()),
        ]:
            button = Gtk.Button(label=title)
            button.connect("clicked", action)
            tools.pack_start(button, False, False, 0)
        box.pack_start(tools, False, False, 0)
        box.pack_start(
            label(
                "Priority terms are considered first. "
                "The selected glossary applies to all recordings; "
                "terms outside the hint limit remain in the file.",
                True,
            ),
            False,
            False,
            0,
        )

    def choose_glossary(self, *_):
        dialog = Gtk.FileChooserDialog(
            title="Choose glossary", transient_for=self, action=Gtk.FileChooserAction.OPEN
        )
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Open", Gtk.ResponseType.OK)
        if self.loaded_path:
            dialog.set_filename(str(Path(self.loaded_path).expanduser()))
        if dialog.run() == Gtk.ResponseType.OK:
            self.vocab_path.set_text(dialog.get_filename())
            self.load_glossary()
        dialog.destroy()

    def load_glossary(self, *_):
        filename = self.vocab_path.get_text().strip()
        if not filename:
            self.set_message("Choose a glossary file first.", True)
            return
        path = Path(filename).expanduser()
        if not path.is_absolute():
            path = settings.CONFIG_DIR / path

        def loaded(text):
            self.loaded_path = filename
            self.original_glossary = text
            self.original_terms = settings.vocabulary_terms(text)
            self.vocab_store.clear()
            priorities = self.config.get("hotwords_priority", [])
            ordered = [t for t in priorities if t in self.original_terms] + [
                t for t in self.original_terms if t not in priorities
            ]
            self.initial_display_order = ordered[:]
            for term in ordered:
                self.vocab_store.append([term, term in priorities, ""])
            self.vocab_enabled.set_active(True)
            self.schedule_preview()

        self.app.background(lambda: path.read_text(encoding="utf-8-sig"), loaded)

    def edit_term(self, _, path, value):
        value = value.strip()
        if not value or "\n" in value:
            self.set_message("A vocabulary entry must contain one term or phrase.", True)
            return
        if any(row[0] == value and str(row.path) != path for row in self.vocab_store):
            self.set_message("That term is already listed.", True)
            return
        self.vocab_store[path][0] = value
        self.schedule_preview()

    def toggle_priority(self, _, path):
        self.vocab_store[path][1] = not self.vocab_store[path][1]
        self.schedule_preview()

    def add_term(self, *_):
        dialog = Gtk.Dialog(title="Add vocabulary", transient_for=self, modal=True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Add", Gtk.ResponseType.OK)
        entry = Gtk.Entry()
        entry.set_placeholder_text("Term or phrase, with preferred spelling")
        entry.set_activates_default(True)
        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.get_content_area().set_border_width(15)
        dialog.get_content_area().add(entry)
        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK:
            text = entry.get_text().strip()
            if text and text not in [row[0] for row in self.vocab_store]:
                self.vocab_store.append([text, True, ""])
                self.schedule_preview()
        dialog.destroy()

    def remove_term(self, *_):
        model, iterator = self.vocab_tree.get_selection().get_selected()
        if iterator is not None:
            model.remove(iterator)
            self.schedule_preview()

    def move_term(self, direction):
        model, iterator = self.vocab_tree.get_selection().get_selected()
        if iterator is None:
            return
        index = model.get_path(iterator).get_indices()[0]
        other = index + direction
        if 0 <= other < len(model):
            model.swap(iterator, model.get_iter(Gtk.TreePath.new_from_indices([other])))
            self.schedule_preview()

    def schedule_preview(self):
        if not hasattr(self, "vocab_store"):
            return
        if self.preview_timer:
            GLib.source_remove(self.preview_timer)
        self.preview_timer = GLib.timeout_add(450, self.preview)

    def collect(self):
        config = copy.deepcopy(self.config)
        config["hotkeys"] = {
            command: entry.get_text() for command, entry in self.shortcut_entries.items()
        }
        config["hotwords_file"] = (
            self.vocab_path.get_text().strip() if self.vocab_enabled.get_active() else None
        )
        if config["hotwords_file"] and config["hotwords_file"] != self.loaded_path:
            raise ValueError(
                "Load the selected glossary before saving, to keep its contents intact."
            )
        config["hotwords_priority"] = [row[0] for row in self.vocab_store if row[1]]
        displayed = [row[0] for row in self.vocab_store]
        if displayed == self.initial_display_order:
            # Preserve external edits/comments when only preferences or priorities change.
            glossary = None
        else:
            glossary = "\n".join(displayed) + ("\n" if displayed else "")
        config["input_device"] = self.microphone.get_active_id() or None
        language = self.language.get_text().strip().lower()
        config["language"] = None if language in ("", "auto") else language
        config["auto_paste"] = self.auto_paste.get_active()
        config["max_seconds"] = self.max_seconds.get_value_as_int()
        config["notifications"] = self.notifications.get_active()
        config["path_notifications"] = self.path_notifications.get_active()
        config["model"] = self.model_choice.get_active_id() or self.config["model"]
        config["cpu_threads"] = self.cpu_threads.get_value_as_int()
        config["start_at_login"] = self.startup.get_active()
        return {
            "config": settings.validate_config(config),
            "glossary": glossary if config["hotwords_file"] else None,
            "glossary_revision": hashlib.sha256(self.original_glossary.encode()).hexdigest(),
        }

    def preview(self):
        self.preview_timer = None
        self.preview_generation += 1
        generation = self.preview_generation
        try:
            payload = self.collect()
        except ValueError as exc:
            self.vocab_count.set_text(str(exc))
            return False

        def display(result):
            if self.is_closed or generation != self.preview_generation:
                return
            hints = result["hotwords"]
            active = set(hints.get("active_terms", []))
            for item in self.vocab_store:
                item[2] = (
                    "Active"
                    if item[0] in active
                    else "Outside hint limit"
                    if payload["config"]["hotwords_file"]
                    else "Disabled"
                )
            self.vocab_count.set_text(
                f"{hints['active']} active / {hints['loaded']} listed — "
                f"hint space {hints['tokens']} / {hints['token_limit']}"
            )

        self.app.call("preview-config", payload, display)
        return False

    def build_audio(self, tabs):
        box = self.page(tabs, "Audio & output")
        self.microphone = Gtk.ComboBoxText()
        self.microphone.append("", "System default")
        current = self.config.get("input_device")
        if current:
            self.microphone.append(current, current)
        self.microphone.set_active_id(current or "")
        row(box, "Microphone", self.microphone)
        test_line = Gtk.Box(spacing=10)
        self.test_button = Gtk.Button(label="Test microphone (8 seconds)")
        self.test_button.connect("clicked", self.test_microphone)
        self.level = Gtk.LevelBar()
        self.level.set_hexpand(True)
        test_line.pack_start(self.test_button, False, False, 0)
        test_line.pack_start(self.level, True, True, 0)
        box.pack_start(test_line, False, False, 0)
        box.pack_start(
            label("The test shows the input level without saving audio.", True), False, False, 0
        )
        self.language = Gtk.Entry(text=self.config.get("language") or "auto")
        self.language.set_placeholder_text("auto, en, nl, …")
        row(box, "Language", self.language)
        self.max_seconds = Gtk.SpinButton.new_with_range(1, 3600, 1)
        self.max_seconds.set_value(self.config["max_seconds"])
        row(box, "Maximum seconds", self.max_seconds)
        self.auto_paste = Gtk.CheckButton(
            label="Paste into the target input; otherwise copy to clipboard"
        )
        self.auto_paste.set_active(self.config["auto_paste"])
        box.pack_start(self.auto_paste, False, False, 0)
        self.notifications = Gtk.CheckButton(label="Show desktop notifications")
        self.notifications.set_active(self.config.get("notifications", True))
        box.pack_start(self.notifications, False, False, 0)
        self.path_notifications = Gtk.CheckButton(label="Confirm each captured file path")
        self.path_notifications.set_active(self.config.get("path_notifications", True))
        box.pack_start(self.path_notifications, False, False, 0)

    def populate_devices(self, result):
        if self.is_closed:
            return
        selected = self.microphone.get_active_id() or ""
        names = list(dict.fromkeys(device["name"] for device in result["devices"]))
        self.microphone.remove_all()
        self.microphone.append("", "System default")
        for name in names:
            self.microphone.append(name, name)
        if selected and selected not in names:
            self.microphone.append(selected, selected + " (unavailable)")
        self.microphone.set_active_id(selected)

    def test_microphone(self, *_):
        if self.app.status.get("state") == "testing":
            self.app.call("stop-microphone-test")
            self.testing = False
        else:
            self.testing = True
            self.app.call(
                "test-microphone", {"input_device": self.microphone.get_active_id() or None}
            )

    def build_service(self, tabs):
        box = self.page(tabs, "Service & model")
        self.model_choice = Gtk.ComboBoxText()
        choices = ["tiny", "base", "small", "medium"]
        if self.config["model"] not in choices:
            choices.append(self.config["model"])
        for name in choices:
            self.model_choice.append(name, name)
        self.model_choice.set_active_id(self.config["model"])
        row(box, "Local Whisper model", self.model_choice)
        box.pack_start(
            label(
                "A new model may download once. "
                "The status indicator shows loading until dictation is ready.",
                True,
            ),
            False,
            False,
            0,
        )
        self.cpu_threads = Gtk.SpinButton.new_with_range(1, 64, 1)
        self.cpu_threads.set_value(self.config["cpu_threads"])
        row(box, "CPU threads", self.cpu_threads)
        self.startup = Gtk.CheckButton(label="Start dictation and the panel icon when I log in")
        self.startup.set_active(self.config.get("start_at_login", True))
        box.pack_start(self.startup, False, False, 0)
        for title, callback in [
            (
                "Open configuration file",
                lambda *_: self.app.open_file(settings.CONFIG_DIR / "config.json"),
            ),
            ("Open glossary file", lambda *_: self.app.open_file(self.vocab_path.get_text())),
            ("Diagnostics…", lambda *_: self.app.show_diagnostics()),
            ("Restart dictation service", lambda *_: self.app.restart_backend()),
        ]:
            button = Gtk.Button(label=title)
            button.connect("clicked", callback)
            box.pack_start(button, False, False, 0)

    def update_status(self, status):
        state = status.get("state", "unavailable")
        text = state.title()
        if not status.get("enabled", True) and state == "idle":
            text = "Paused"
        if state == "recording":
            text += (
                f" — {int(status.get('elapsed', 0))} seconds, "
                f"{status.get('paths', 0)} captured paths"
            )
        if status.get("pending_settings"):
            text += " — settings will apply when ready"
        if status.get("pending_action"):
            text += " — " + status["pending_action"] + " queued"
        self.status_label.set_text(text)
        self.level.set_value(status.get("audio_level", 0) if state == "testing" else 0)
        self.test_button.set_label(
            "Stop microphone test" if state == "testing" else "Test microphone (8 seconds)"
        )
        if state != "testing":
            self.testing = False

    def save(self, *_):
        try:
            payload = self.collect()
        except ValueError as exc:
            self.set_message(str(exc), True)
            return
        self.save_button.set_sensitive(False)

        def done(result):
            if self.is_closed:
                return
            self.config = payload["config"]
            if payload["glossary"] is not None:
                self.original_glossary = payload["glossary"]
                self.original_terms = settings.vocabulary_terms(self.original_glossary)
                self.initial_display_order = [row[0] for row in self.vocab_store]
            self.save_button.set_sensitive(True)
            self.set_message(
                "Changes queued until the current operation finishes."
                if result.get("pending")
                else "Settings saved."
            )
            self.preview()

        self.app.call(
            "apply-config",
            payload,
            done,
            autostart=True,
            on_error=lambda error: (
                self.save_button.set_sensitive(True),
                self.set_message(error, True),
            ),
        )

    def closed(self, *_):
        self.is_closed = True
        if self.preview_timer:
            GLib.source_remove(self.preview_timer)
        if self.testing:
            self.app.call("stop-microphone-test")
        if self.app.settings_window is self:
            self.app.settings_window = None


class TrayApplication(Gtk.Application):
    def __init__(self):
        super().__init__(
            application_id="org.heopd.TalkAndPath", flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE
        )
        self.pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="talk-path-panel")
        self.status = {"state": "unavailable"}
        self.settings_window = None
        self.target_window = None
        self.poll_inflight = False
        self.last_ui_error = None
        self.last_icon = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.hold()
        self.indicator = Indicator.Indicator.new(
            "talk-and-path", "talk-and-path-offline", Indicator.IndicatorCategory.APPLICATION_STATUS
        )
        self.indicator.set_icon_theme_path(str(ICON_DIR))
        self.indicator.set_title("Talk and Path")
        self.indicator.set_status(Indicator.IndicatorStatus.ACTIVE)
        self.menu = Gtk.Menu()
        self.state_item = Gtk.MenuItem(label="Talk and Path — connecting…")
        self.state_item.set_sensitive(False)
        self.menu.append(self.state_item)
        self.detail_item = Gtk.MenuItem(label="")
        self.detail_item.set_sensitive(False)
        self.menu.append(self.detail_item)
        self.menu.append(Gtk.SeparatorMenuItem())
        self.toggle_item = self.add_menu("Start recording", self.toggle_recording)
        self.cancel_item = self.add_menu("Cancel dictation", lambda *_: self.call("cancel"))
        self.pause_item = Gtk.CheckMenuItem(label="Pause dictation")
        self.pause_item.connect("activate", self.pause)
        self.menu.append(self.pause_item)
        self.add_menu("Copy last transcript", lambda *_: self.call("copy-last"))
        self.add_menu("Review last transcript…", lambda *_: self.review_transcript())
        self.menu.append(Gtk.SeparatorMenuItem())
        self.add_menu("Settings…", lambda *_: self.open_settings())
        self.add_menu("Diagnostics…", lambda *_: self.show_diagnostics())
        self.restart_item = self.add_menu(
            "Restart dictation service", lambda *_: self.restart_backend()
        )
        self.add_menu("Stop dictation service", lambda *_: self.call("shutdown"))
        self.menu.append(Gtk.SeparatorMenuItem())
        self.add_menu("Quit panel icon", lambda *_: self.quit())
        self.menu.show_all()
        self.indicator.set_menu(self.menu)
        GLib.timeout_add(800, self.poll)
        self.poll()

    def add_menu(self, title, action):
        item = Gtk.MenuItem(label=title)
        item.connect("activate", action)
        self.menu.append(item)
        return item

    def do_command_line(self, command):
        args = command.get_arguments()[1:]
        if "--quit" in args:
            self.quit()
        elif "--diagnostics" in args:
            self.show_diagnostics()
        elif "--settings" in args or "--background" not in args:
            self.open_settings()
        return 0

    def background(self, work, callback=None, on_error=None):
        future = self.pool.submit(work)

        def completed(future):
            try:
                result = future.result()
            except Exception as exc:
                error = str(exc)
                GLib.idle_add(lambda: ((on_error or self.show_error)(error), False)[1])
            else:
                if callback:
                    GLib.idle_add(lambda: (callback(result), False)[1])

        future.add_done_callback(completed)

    def call(self, command, payload=None, callback=None, autostart=False, on_error=None):
        self.background(
            lambda: core.send_command(command, payload, autostart=autostart), callback, on_error
        )

    def show_error(self, error):
        self.last_ui_error = error
        self.detail_item.set_label(error[:120])
        if self.settings_window:
            self.settings_window.set_message(error, True)

    def poll(self):
        if self.poll_inflight:
            return True
        self.poll_inflight = True

        def fetch():
            # Track the most recent application, excluding our own windows/panel.
            target = None
            try:
                active = core.active_window()
                if not core.is_own_window(active):
                    target = active
            except Exception:
                pass
            try:
                status = core.send_command("status", autostart=False)
            except Exception as exc:
                status = {"state": "unavailable", "last_error": str(exc)}
            return status, target

        def received(result):
            self.poll_inflight = False
            self.status, target = result
            if target:
                self.target_window = target
            self.render()

        self.background(fetch, received, lambda error: setattr(self, "poll_inflight", False))
        return True

    def render(self):
        state = self.status.get("state", "unavailable")
        enabled = self.status.get("enabled", True)
        icon_state = "paused" if state == "idle" and not enabled else state
        icon_state = {
            "idle": "ready",
            "testing": "recording",
            "delivering": "transcribing",
            "unavailable": "offline",
        }.get(icon_state, icon_state)
        if icon_state not in (
            "ready",
            "paused",
            "recording",
            "transcribing",
            "loading",
            "error",
            "offline",
        ):
            icon_state = "offline"
        description = "Talk and Path — " + ("paused" if not enabled and state == "idle" else state)
        if self.last_icon != icon_state:
            self.indicator.set_icon_full("talk-and-path-" + icon_state, description)
            self.last_icon = icon_state
        seconds = int(self.status.get("elapsed", 0))
        self.indicator.set_label(
            f"{seconds // 60}:{seconds % 60:02d}" if state == "recording" else "", "0:00"
        )
        self.state_item.set_label(description.title())
        hints = self.status.get("hotwords", {})
        if state == "recording":
            detail = f"{seconds}s — {self.status.get('paths', 0)} captured paths"
        elif state == "loading":
            detail = self.status.get("phase", "Loading local speech model…")
        elif self.status.get("last_error"):
            detail = self.status["last_error"]
        else:
            detail = (
                f"Whisper {self.status.get('model', '—')} — "
                f"{hints.get('active', 0)} / {hints.get('loaded', 0)} vocabulary terms"
            )
        self.detail_item.set_label(detail[:140])
        self.toggle_item.set_label(
            "Finish recording" if state == "recording" else "Start recording"
        )
        self.toggle_item.set_sensitive(
            state == "recording" or (state == "idle" and enabled and bool(self.target_window))
        )
        self.cancel_item.set_sensitive(
            state in ("recording", "transcribing", "delivering", "testing")
        )
        if self.pause_item.get_active() != (not enabled):
            self.pause_item.set_active(not enabled)
        self.restart_item.set_label(
            "Start dictation service" if state == "unavailable" else "Restart dictation service"
        )
        if self.settings_window:
            self.settings_window.update_status(self.status)

    def toggle_recording(self, *_):
        recording = self.status.get("state") == "recording"
        remembered = self.target_window

        def toggle():
            payload = {}
            if not recording:
                # Resolve again after menu activation: the user may have switched
                # applications since the most recent status poll.
                active = core.active_window()
                payload["origin"] = remembered if core.is_own_window(active) else active
            return core.send_command("toggle", payload, autostart=False)

        self.background(toggle)

    def pause(self, *_):
        desired = not self.pause_item.get_active()
        if desired != self.status.get("enabled", True):
            self.call("set-enabled", {"enabled": desired})

    def restart_backend(self):
        if self.status.get("state") == "unavailable":
            self.background(
                lambda: subprocess.run(
                    ["systemctl", "--user", "start", "talk-and-path.service"],
                    check=True,
                    timeout=10,
                )
            )
        else:
            self.call("restart")

    def open_settings(self):
        if self.settings_window:
            self.settings_window.present()
            return

        def show(result):
            if self.settings_window:
                self.settings_window.present()
                return
            self.settings_window = SettingsWindow(
                self, result["config"], result.get("glossary", ""), result.get("glossary_error")
            )
            self.settings_window.present()

        def offline(_):
            def local():
                config = settings.load_config()
                path = settings.glossary_path(config)
                try:
                    text, error = path.read_text(encoding="utf-8-sig") if path else "", None
                except OSError as exc:
                    text, error = "", str(exc)
                return {"config": config, "glossary": text, "glossary_error": error}

            self.background(local, show)

        self.call("configuration", callback=show, on_error=offline)

    def open_file(self, path):
        if not path:
            self.show_error("No file is selected.")
            return
        value = Path(path).expanduser()
        if not value.is_absolute():
            value = settings.CONFIG_DIR / value
        self.background(
            lambda: subprocess.run(
                ["xdg-open", str(value)],
                check=True,
                timeout=10,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )

    def text_window(self, title, text, editable=False, copy_button=False):
        window = Gtk.ApplicationWindow(application=self, title=title)
        window.set_wmclass("talk-and-path", "Talk-and-path")
        window.set_default_size(680, 450)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_border_width(15)
        window.add(box)
        view = Gtk.TextView(editable=editable, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        view.get_buffer().set_text(text)
        box.pack_start(scroll(view), True, True, 0)
        if copy_button:
            button = Gtk.Button(label="Copy text")

            def copy_text(*_):
                buffer = view.get_buffer()
                self.call(
                    "copy-text",
                    {"text": buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)},
                )

            button.connect("clicked", copy_text)
            box.pack_start(button, False, False, 0)
        window.show_all()
        return window

    def review_transcript(self):
        def show(result):
            transcript = result.get("transcript")
            if not transcript:
                self.show_error("No completed transcript is available yet.")
                return
            self.text_window("Talk and Path — Last transcript", transcript["text"], True, True)

        self.call("last-transcript", callback=show)

    def show_diagnostics(self):
        def fetch():
            result = subprocess.run(
                ["journalctl", "--user", "-u", "talk-and-path.service", "-n", "25", "--no-pager"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return (
                "STATUS\n"
                + json.dumps(self.status, indent=2, ensure_ascii=False)
                + "\n\nSERVICE LOG\n"
                + result.stdout
            )

        self.background(fetch, lambda text: self.text_window("Talk and Path — Diagnostics", text))

    def do_shutdown(self):
        if self.settings_window and self.settings_window.testing:
            try:
                core.send_command("stop-microphone-test", autostart=False)
            except Exception:
                pass
        self.pool.shutdown(wait=False, cancel_futures=True)
        Gtk.Application.do_shutdown(self)


if __name__ == "__main__":
    sys.exit(TrayApplication().run(sys.argv))
