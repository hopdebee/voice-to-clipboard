"""Tray application: global hotkey -> record -> transcribe -> clipboard."""

import logging
import os
import signal
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import __version__, clipboard, config, sounds
from .hotkey import HotkeyCapture, HotkeyListener
from .icons import make_icon
from .recorder import SAMPLE_RATE, Recorder
from .transcribe import Transcriber

log = logging.getLogger(__name__)

MIN_AUDIO_SECONDS = 0.3


class Bridge(QObject):
    """Signals for hopping from worker/listener threads onto the Qt main thread."""

    hotkey = Signal(str)  # source: "copy" | "paste"
    hotkey_released = Signal(str)
    transcribed = Signal(str, str)  # text, delivery ("clipboard" | "type")
    failed = Signal(str)
    model_state = Signal(str)  # "loading" | "ready" | "error: ..."
    hotkey_captured = Signal(object)  # str spec, or None if cancelled
    leaderboard_ready = Signal(object)  # list[Candidate], or str error message


class App:
    def __init__(self, qapp: QApplication) -> None:
        self.qapp = qapp
        self.cfg = config.load()
        self.enabled = True
        self.state = "idle"  # idle | recording | busy

        self.bridge = Bridge()
        self.bridge.hotkey.connect(self.on_hotkey)
        self.bridge.hotkey_released.connect(self.on_hotkey_released)
        self.bridge.transcribed.connect(self.on_transcribed)
        self.bridge.failed.connect(self.on_failed)
        self.bridge.model_state.connect(self.on_model_state)
        self.bridge.hotkey_captured.connect(self.on_hotkey_captured)
        self.bridge.leaderboard_ready.connect(self.on_leaderboard_ready)
        self._leaderboard_busy = False
        self._leaderboard_dialog = None

        self.recorder = Recorder()
        self.transcriber = Transcriber()
        self.capture: HotkeyCapture | None = None
        self._capture_kind = "copy"
        self.delivery = "clipboard"  # how the current recording will be delivered
        self._active_source = ""  # which hotkey started the current recording
        self.listener = self._make_listener(self.cfg.hotkey, "copy")
        self.paste_listener: HotkeyListener | None = (
            self._make_listener(self.cfg.paste_hotkey, "paste")
            if self.cfg.paste_hotkey
            else None
        )

        self.max_timer = QTimer()
        self.max_timer.setSingleShot(True)
        self.max_timer.timeout.connect(self.on_max_duration)

        self.model_status = "loading"
        self.tray = QSystemTrayIcon(make_icon("idle"))
        self.menu = QMenu()
        self._build_menu()
        self.tray.setContextMenu(self.menu)
        self._update_tray()
        self.tray.show()

        for lst, spec in ((self.listener, self.cfg.hotkey),
                          (self.paste_listener, self.cfg.paste_hotkey)):
            if lst is None:
                continue
            try:
                lst.start()
            except Exception as e:
                log.exception("bad hotkey in config")
                self.notify("Invalid hotkey", f"{spec!r}: {e}", error=True)
        self._load_model_async()

    def _make_listener(self, spec: str, source: str) -> HotkeyListener:
        return HotkeyListener(
            spec,
            lambda: self.bridge.hotkey.emit(source),
            lambda: self.bridge.hotkey_released.emit(source),
        )

    # ---------- menu ----------

    def _build_menu(self) -> None:
        self.menu.clear()

        self.enabled_action = QAction("Enabled", checkable=True, checked=self.enabled)
        self.enabled_action.triggered.connect(self.set_enabled)
        self.menu.addAction(self.enabled_action)
        self.menu.addSeparator()

        model_menu = self.menu.addMenu("Model")
        self.status_action = QAction("...")
        self.status_action.setEnabled(False)
        model_menu.addAction(self.status_action)
        model_menu.addSeparator()
        group = QActionGroup(model_menu)
        group.setExclusive(True)
        for label, spec in self.cfg.favorites.items():
            act = QAction(label, model_menu, checkable=True)
            act.setChecked(spec == self.cfg.backend_model)
            act.triggered.connect(lambda _=False, s=spec: self.switch_model(s))
            group.addAction(act)
            model_menu.addAction(act)
        model_menu.addSeparator()

        self.unload_action = QAction("Unload model (free VRAM)")
        self.unload_action.triggered.connect(self.toggle_model_loaded)
        model_menu.addAction(self.unload_action)

        check_lb = model_menu.addAction("Check ASR leaderboard…")
        check_lb.triggered.connect(self.check_leaderboard)

        hotkey_menu = self.menu.addMenu("Hotkeys")
        current = QAction(f"Copy hotkey: {self.cfg.hotkey}", hotkey_menu)
        current.setEnabled(False)
        hotkey_menu.addAction(current)
        hotkey_menu.addSeparator()

        set_hotkey = hotkey_menu.addAction("Set hotkey…")
        set_hotkey.triggered.connect(lambda: self.start_hotkey_capture("copy"))

        label = f"Set paste hotkey… ({self.cfg.paste_hotkey})" if self.cfg.paste_hotkey \
            else "Set paste hotkey… (off)"
        set_paste = hotkey_menu.addAction(label)
        set_paste.triggered.connect(lambda: self.start_hotkey_capture("paste"))
        if self.cfg.paste_hotkey:
            disable_paste = hotkey_menu.addAction("Disable paste hotkey")
            disable_paste.triggered.connect(self.disable_paste_hotkey)

        self.ptt_action = QAction(
            "Push-to-talk (hold)", checkable=True, checked=self.cfg.mode == "hold"
        )
        self.ptt_action.triggered.connect(self.set_mode)
        hotkey_menu.addAction(self.ptt_action)

        self.menu.addSeparator()
        open_cfg = self.menu.addAction("Open config file")
        open_cfg.triggered.connect(self.open_config)
        reload_cfg = self.menu.addAction("Reload config")
        reload_cfg.triggered.connect(self.reload_config)
        self.menu.addSeparator()
        quit_action = self.menu.addAction("Quit")
        quit_action.triggered.connect(self.quit)

    def _update_tray(self) -> None:
        icon_state = self.state if self.enabled else "disabled"
        self.tray.setIcon(make_icon(icon_state))
        status = {
            "loading": "loading model...",
            "unloading": "unloading...",
            "ready": "ready",
            "unloaded": "unloaded",
        }.get(self.model_status, self.model_status)
        short_model = self.cfg.model.rsplit("/", 1)[-1]
        self.status_action.setText(f"{short_model} — {status}")
        if self.model_status == "ready":
            self.unload_action.setText("Unload model (free VRAM)")
            self.unload_action.setEnabled(True)
        elif self.model_status in ("loading", "unloading"):
            self.unload_action.setText("Model busy…")
            self.unload_action.setEnabled(False)
        else:  # unloaded / error
            self.unload_action.setText("Load model")
            self.unload_action.setEnabled(True)
        mode_label = "push-to-talk" if self.cfg.mode == "hold" else "toggle"
        paste_line = f"\npaste hotkey: {self.cfg.paste_hotkey}" if self.cfg.paste_hotkey else ""
        self.tray.setToolTip(
            f"voice-to-clipboard ({self.cfg.hotkey}, {mode_label})\n"
            f"{self.cfg.backend}:{self.cfg.model} — {status}{paste_line}"
        )

    # ---------- state machine ----------

    def on_hotkey(self, source: str) -> None:
        if not self.enabled:
            return
        if self.state == "idle":
            self._active_source = source
            self.delivery = "type" if source == "paste" else "clipboard"
            self.start_recording()
        elif self.state == "recording" and self.cfg.mode == "toggle":
            self.stop_and_transcribe()
        # busy: ignore until the current transcription finishes

    def on_hotkey_released(self, source: str) -> None:
        if (
            self.cfg.mode == "hold"
            and self.state == "recording"
            and source == self._active_source
        ):
            self.stop_and_transcribe()

    def set_mode(self, ptt: bool) -> None:
        self.cfg.mode = "hold" if ptt else "toggle"
        config.save(self.cfg)
        self._update_tray()

    def start_recording(self) -> None:
        try:
            self.recorder.start()
        except Exception as e:
            log.exception("failed to start recording")
            self.notify("Microphone error", str(e), error=True)
            return
        self.state = "recording"
        self.max_timer.start(self.cfg.max_seconds * 1000)
        if self.cfg.record_tone:
            sounds.start_drone()
        self._update_tray()

    def stop_and_transcribe(self) -> None:
        self.max_timer.stop()
        audio = self.recorder.stop()
        sounds.stop_drone()
        if len(audio) < MIN_AUDIO_SECONDS * SAMPLE_RATE:
            self.state = "idle"
            self._update_tray()
            return
        self.state = "busy"
        self._update_tray()
        cfg = self.cfg
        delivery = self.delivery
        log.info("transcribing %.1fs audio (delivery=%s)", len(audio) / SAMPLE_RATE, delivery)

        def work() -> None:
            try:
                self.transcriber.load(cfg.backend, cfg.model, cfg.device, cfg.compute_type)
                text = self.transcriber.transcribe(audio, cfg.language)
                if text:
                    if delivery == "type":
                        clipboard.type_into_focus(text)
                    else:
                        clipboard.copy(text)
                    self.bridge.transcribed.emit(text, delivery)
                else:
                    self.bridge.failed.emit("No speech detected")
            except Exception as e:
                log.exception("transcription failed")
                self.bridge.failed.emit(str(e))

        threading.Thread(target=work, daemon=True).start()

    def on_max_duration(self) -> None:
        if self.state == "recording":
            log.info("max recording duration reached, auto-stopping")
            self.stop_and_transcribe()

    def on_transcribed(self, text: str, delivery: str) -> None:
        self.state = "idle"
        self.model_status = "ready"
        self._update_tray()
        if self.cfg.sounds:
            sounds.success()
        snippet = text if len(text) <= 120 else text[:117] + "..."
        title = "Typed into active window" if delivery == "type" else "Copied to clipboard"
        self.notify(title, snippet)

    def on_failed(self, message: str) -> None:
        self.state = "idle"
        self._update_tray()
        if self.cfg.sounds:
            sounds.error()
        self.notify("Transcription failed", message, error=True)

    # ---------- model handling ----------

    def _load_model_async(self) -> None:
        cfg = self.cfg
        self.model_status = "loading"
        self._update_tray()

        def work() -> None:
            try:
                self.bridge.model_state.emit("loading")
                self.transcriber.load(cfg.backend, cfg.model, cfg.device, cfg.compute_type)
                self.bridge.model_state.emit("ready")
            except Exception as e:
                log.exception("model load failed")
                self.bridge.model_state.emit(f"error: {e}")

        threading.Thread(target=work, daemon=True).start()

    def on_model_state(self, status: str) -> None:
        self.model_status = status
        self._update_tray()
        if status.startswith("error"):
            self.notify("Model load failed", status, error=True)

    def toggle_model_loaded(self) -> None:
        if self.model_status == "ready":
            self.model_status = "unloading"
            self._update_tray()

            def work() -> None:
                self.transcriber.unload()
                self.bridge.model_state.emit("unloaded")

            threading.Thread(target=work, daemon=True).start()
        else:
            self._load_model_async()

    def switch_model(self, spec: str) -> None:
        backend, _, model = spec.partition(":")
        self.cfg.backend, self.cfg.model = backend, model
        config.save(self.cfg)
        self._load_model_async()

    # ---------- leaderboard ----------

    def check_leaderboard(self) -> None:
        if self._leaderboard_busy:
            return
        self._leaderboard_busy = True
        self.notify("ASR leaderboard", "Fetching latest results…")

        def work() -> None:
            try:
                from . import leaderboard

                self.bridge.leaderboard_ready.emit(leaderboard.fetch_ranked())
            except Exception as e:
                log.exception("leaderboard fetch failed")
                self.bridge.leaderboard_ready.emit(str(e))

        threading.Thread(target=work, daemon=True).start()

    def on_leaderboard_ready(self, result: object) -> None:
        self._leaderboard_busy = False
        if isinstance(result, str):
            self.notify("Leaderboard check failed", result, error=True)
            return
        self._leaderboard_dialog = build_leaderboard_dialog(
            result, self.cfg.backend_model, self._use_leaderboard_model
        )
        self._leaderboard_dialog.show()
        self._leaderboard_dialog.raise_()
        self._leaderboard_dialog.activateWindow()

    def _use_leaderboard_model(self, candidate) -> None:
        label = candidate.model_id.rsplit("/", 1)[-1]
        self.cfg.favorites.setdefault(label, candidate.spec)
        self.switch_model(candidate.spec)  # sets backend/model, saves, loads
        self._build_menu()
        self.notify("Model switched", f"{candidate.model_id} ({candidate.wer:.2f}% WER)")

    # ---------- hotkey capture ----------

    def start_hotkey_capture(self, kind: str) -> None:
        if self.capture is not None:
            return
        self._capture_kind = kind
        # pause the live hotkeys so pressing a current combo can't start a recording
        self.listener.stop()
        if self.paste_listener is not None:
            self.paste_listener.stop()
        what = "paste hotkey" if kind == "paste" else "hotkey"
        self.notify(
            f"Set {what}",
            "Press the new key combo or mouse button (Esc to cancel; left click ignored)",
        )
        self.capture = HotkeyCapture(self.bridge.hotkey_captured.emit)
        self.capture.start()

    def on_hotkey_captured(self, spec: object) -> None:
        self.capture = None
        kind = self._capture_kind
        if isinstance(spec, str):
            other = self.cfg.hotkey if kind == "paste" else self.cfg.paste_hotkey
            if spec == other:
                self.notify(
                    "Hotkey conflict",
                    f"{spec} is already the other hotkey — not changed",
                    error=True,
                )
            elif kind == "paste":
                self.cfg.paste_hotkey = spec
                config.save(self.cfg)
                self.notify("Paste hotkey set", spec)
            else:
                self.cfg.hotkey = spec
                config.save(self.cfg)
                self.notify("Hotkey set", spec)
        else:
            self.notify("Cancelled", "Hotkeys unchanged")
        self._restart_listeners()
        self._build_menu()
        self._update_tray()

    def disable_paste_hotkey(self) -> None:
        self.cfg.paste_hotkey = ""
        config.save(self.cfg)
        self._restart_listeners()
        self._build_menu()
        self._update_tray()
        self.notify("Paste hotkey disabled", "Recordings go to the clipboard")

    def _restart_listeners(self) -> None:
        self.listener.stop()
        if self.paste_listener is not None:
            self.paste_listener.stop()
        self.listener = self._make_listener(self.cfg.hotkey, "copy")
        self.paste_listener = (
            self._make_listener(self.cfg.paste_hotkey, "paste")
            if self.cfg.paste_hotkey
            else None
        )
        if not self.enabled:
            return
        for lst, spec in ((self.listener, self.cfg.hotkey),
                          (self.paste_listener, self.cfg.paste_hotkey)):
            if lst is None:
                continue
            try:
                lst.start()
            except Exception as e:
                log.exception("bad hotkey")
                self.notify("Invalid hotkey", f"{spec!r}: {e}", error=True)

    # ---------- config / lifecycle ----------

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        if enabled:
            self._restart_listeners()
        else:
            self.listener.stop()
            if self.paste_listener is not None:
                self.paste_listener.stop()
            if self.state == "recording":
                self.max_timer.stop()
                self.recorder.stop()
                sounds.stop_drone()
                self.state = "idle"
        self._update_tray()

    def open_config(self) -> None:
        import subprocess

        if not config.CONFIG_PATH.exists():
            config.save(self.cfg)
        subprocess.Popen(["xdg-open", str(config.CONFIG_PATH)])

    def reload_config(self) -> None:
        old = self.cfg
        self.cfg = config.load()
        if (self.cfg.hotkey, self.cfg.paste_hotkey) != (old.hotkey, old.paste_hotkey):
            self._restart_listeners()
        self._build_menu()
        if self.cfg.backend_model != old.backend_model:
            self._load_model_async()
        else:
            self._update_tray()
        self.notify("Config reloaded", f"hotkey: {self.cfg.hotkey}")

    def notify(self, title: str, message: str, error: bool = False) -> None:
        icon = QSystemTrayIcon.Warning if error else QSystemTrayIcon.Information
        self.tray.showMessage(title, message, icon, 4000)

    def quit(self) -> None:
        if self.capture is not None:
            self.capture.cancel()
        sounds.stop_drone()
        self.listener.stop()
        if self.paste_listener is not None:
            self.paste_listener.stop()
        if self.recorder.recording:
            self.recorder.stop()
        self.qapp.quit()


def build_leaderboard_dialog(candidates, current_spec: str, on_pick):
    """Dialog listing Open ASR Leaderboard entries; runnable ones are selectable."""
    from PySide6.QtWidgets import (
        QDialog,
        QHBoxLayout,
        QLabel,
        QListWidget,
        QListWidgetItem,
        QPushButton,
        QVBoxLayout,
    )

    dialog = QDialog()
    dialog.setWindowTitle("Open ASR Leaderboard — English (avg cleaned WER)")
    dialog.resize(720, 480)
    layout = QVBoxLayout(dialog)
    layout.addWidget(
        QLabel(
            "Same ranking as the public leaderboard. Grayed-out entries can't be "
            "run by this app (proprietary, trust_remote_code, or no standard loader)."
        )
    )
    lst = QListWidget()
    for c in candidates:
        rtfx = f", RTFx {c.rtfx:.0f}" if c.rtfx else ""
        if c.runnable:
            current = "   ✓ current" if c.spec == current_spec else ""
            untested = "  (untested)" if c.note.startswith("untested") else ""
            text = f"#{c.rank}  {c.wer:.2f}%  {c.model_id}  [{c.backend}{rtfx}]{untested}{current}"
        else:
            text = f"#{c.rank}  {c.wer:.2f}%  {c.model_id} — {c.note}"
        item = QListWidgetItem(text)
        if not c.runnable:
            item.setFlags(Qt.ItemFlag.NoItemFlags)
        item.setData(Qt.ItemDataRole.UserRole, c)
        lst.addItem(item)
    layout.addWidget(lst)

    buttons = QHBoxLayout()
    use_btn = QPushButton("Use selected model")
    use_btn.setEnabled(False)
    close_btn = QPushButton("Close")
    buttons.addWidget(use_btn)
    buttons.addStretch()
    buttons.addWidget(close_btn)
    layout.addLayout(buttons)

    def selection_changed() -> None:
        item = lst.currentItem()
        c = item.data(Qt.ItemDataRole.UserRole) if item else None
        use_btn.setEnabled(bool(c and c.runnable and c.spec != current_spec))

    def use_selected() -> None:
        c = lst.currentItem().data(Qt.ItemDataRole.UserRole)
        dialog.accept()
        on_pick(c)

    lst.currentItemChanged.connect(selection_changed)
    lst.itemDoubleClicked.connect(lambda _item: use_btn.isEnabled() and use_selected())
    use_btn.clicked.connect(use_selected)
    close_btn.clicked.connect(dialog.reject)
    return dialog


def _setup_logging() -> None:
    state_dir = Path.home() / ".local" / "state" / "voice-to-clipboard"
    state_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(state_dir / "app.log"),
        ],
    )


def main() -> int:
    if "--version" in sys.argv:
        print(f"voice-to-clipboard {__version__}")
        return 0
    if "--check-leaderboard" in sys.argv:
        from . import leaderboard

        cfg = config.load()
        candidates = leaderboard.fetch_ranked()
        print(leaderboard.format_table(candidates, cfg.backend_model))
        return 0
    if "--install-service" in sys.argv:
        from . import service

        return service.install()
    if "--uninstall-service" in sys.argv:
        from . import service

        return service.uninstall()

    _setup_logging()
    log.info("voice-to-clipboard %s starting", __version__)

    # single-instance lock
    from PySide6.QtCore import QLockFile

    runtime = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
    lock = QLockFile(str(Path(runtime) / "voice-to-clipboard.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        print("voice-to-clipboard is already running", file=sys.stderr)
        return 1

    qapp = QApplication(sys.argv)
    qapp.setQuitOnLastWindowClosed(False)
    qapp.setApplicationName("voice-to-clipboard")
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("no system tray available", file=sys.stderr)
        return 1

    app = App(qapp)
    _ = app  # keep alive
    return qapp.exec()


if __name__ == "__main__":
    sys.exit(main())
