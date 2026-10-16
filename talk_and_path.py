#!/usr/bin/env python3
"""Local X11 dictation, controlled by AutoKey over a private Unix socket.

F8 toggles recording; F9 timestamps the hovered VS Code Explorer path.
Run scripts/install_dictation.py to install dependencies, bindings and service.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import dictation_settings as settings

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "talk-and-path"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "talk-and-path"
RUNTIME_DIR = (
    Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/talk-and-path-{os.getuid()}")) / "talk-and-path"
)
SOCKET_PATH = RUNTIME_DIR / "control.sock"
DEFAULT_CONFIG = settings.DEFAULT_CONFIG
LOG = logging.getLogger("talk-and-path")


def load_config():
    return settings.load_config()


def load_hotwords(config, model, terms=None):
    """Read complete terms and fit the model's hint budget without silent truncation."""
    filename = config.get("hotwords_file")
    result = {
        "file": filename,
        "loaded": 0,
        "active": 0,
        "omitted": 0,
        "tokens": 0,
        "token_limit": 0,
        "text": None,
        "active_terms": [],
        "error": None,
    }
    if not filename:
        return result
    path = Path(filename).expanduser()
    if not path.is_absolute():
        path = CONFIG_DIR / path
    result["file"] = str(path)
    if terms is None:
        terms = settings.vocabulary_terms(path.read_text(encoding="utf-8-sig"))
    else:
        terms = settings.vocabulary_terms("\n".join(terms))
    priority = config.get("hotwords_priority", [])
    if not isinstance(priority, list) or any(not isinstance(term, str) for term in priority):
        raise ValueError("hotwords_priority must be a list of strings")
    ordered = list(dict.fromkeys([term for term in priority if term in terms] + terms))
    # faster-whisper truncates at half the decoder's context. Fit whole terms first.
    limit = model.max_length // 2 - 1
    selected, token_count = [], 0
    for term in ordered:
        candidate = ", ".join(selected + [term])
        count = len(model.hf_tokenizer.encode(" " + candidate, add_special_tokens=False).ids)
        if count <= limit:
            selected.append(term)
            token_count = count
    result.update(
        loaded=len(terms),
        active=len(selected),
        omitted=len(terms) - len(selected),
        tokens=token_count,
        token_limit=limit,
        text=", ".join(selected) or None,
        active_terms=selected,
    )
    return result


def run(args, **kwargs):
    return subprocess.run(args, check=True, timeout=3, **kwargs)


def notify(message, enabled=True):
    LOG.info(message)
    if enabled and shutil.which("notify-send"):
        try:
            run(
                [
                    "notify-send",
                    "--app-name=Talk and Path",
                    "--expire-time=4500",
                    "Talk and Path",
                    message,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            LOG.debug("Notification unavailable")


def active_window():
    return run(["xdotool", "getactivewindow"], capture_output=True, text=True).stdout.strip()


def window_class(window):
    result = run(["xprop", "-id", str(window), "WM_CLASS"], capture_output=True, text=True)
    return result.stdout.lower()


def is_code_window(window):
    return any(
        f'"{name}"' in window_class(window)
        for name in ("code", "code - insiders", "code-insiders", "vscodium", "codium")
    )


def is_own_window(window):
    value = window_class(window)
    return '"talk-and-path"' in value or '"mate-panel"' in value


def send_hotkey(shortcut):
    run(["xdotool", "key", "--clearmodifiers", shortcut], stdout=subprocess.DEVNULL)


def clipboard_get():
    result = subprocess.run(
        ["xclip", "-selection", "clipboard", "-o"], capture_output=True, timeout=2
    )
    if result.returncode:
        return None
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


def clipboard_set(text):
    # xclip forks a selection owner; do not leave its inherited pipes attached.
    run(
        ["xclip", "-selection", "clipboard", "-i"],
        input=text.encode("utf-8"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def markdown_path(path):
    ticks = "`" * (max((len(m.group()) for m in re.finditer(r"`+", path)), default=0) + 1)
    padding = " " if path.startswith("`") or path.endswith("`") else ""
    return f"{ticks}{padding}{path}{padding}{ticks}"


def merge_paths_into_transcript(segments, events):
    """Insert paths against word timestamps on the original audio timeline."""
    words = [word for segment in segments for word in (segment.words or [])]
    events = sorted(events, key=lambda event: event["time"])
    if not words:
        text = "".join(segment.text for segment in segments).strip()
        return " ".join([text, *(markdown_path(e["path"]) for e in events)]).strip()
    pieces, index = [], 0
    for word in words:
        while index < len(events) and events[index]["time"] <= word.start:
            pieces.append(f" {markdown_path(events[index]['path'])} ")
            index += 1
        pieces.append(word.word)
    pieces.extend(f" {markdown_path(e['path'])} " for e in events[index:])
    return re.sub(r"[ \t]+", " ", "".join(pieces)).strip()


def make_model(config):
    from faster_whisper import WhisperModel
    from faster_whisper.utils import download_model
    from huggingface_hub.errors import LocalEntryNotFoundError

    model_path = config["model"]
    if not Path(model_path).is_dir():
        try:
            model_path = download_model(model_path, local_files_only=True)
        except LocalEntryNotFoundError:
            notify(
                "Downloading the Whisper model for first use…",
                enabled=config.get("notifications", True),
            )
            model_path = download_model(model_path)
    return WhisperModel(
        model_path,
        device=config["device"],
        compute_type=config["compute_type"],
        cpu_threads=config["cpu_threads"],
    )


class DictationService:
    def __init__(self, config, model=None):
        self.config, self.model = config, model
        self.state = "loading" if model is None else "idle"
        self.lock = threading.RLock()
        self.stream = None
        self.chunks, self.events = [], []
        self.started = 0.0
        self.origin = None
        self.origin_is_code = False
        self.recording_id = None
        self.timer = None
        self.closed = False
        self.last_error = None
        self.last_toggle = 0.0
        self.enabled = True
        self.suspended_until = 0.0
        self.pending_config = None
        self.pending_action = None
        self.audio_level = 0.0
        self.microphone_timer = None
        self.phase = "Loading local speech model" if model is None else "Ready"
        self.hotword_hints = {
            "file": config.get("hotwords_file"),
            "loaded": 0,
            "active": 0,
            "omitted": 0,
            "tokens": 0,
            "token_limit": 0,
            "text": None,
            "error": None,
        }

    def inform(self, message, path=False):
        notify(
            message,
            enabled=self.config.get("notifications", True)
            and (not path or self.config.get("path_notifications", True)),
        )

    def refresh_hotwords(self):
        try:
            self.hotword_hints = load_hotwords(self.config, self.model)
        except (OSError, UnicodeError, ValueError) as exc:
            self.hotword_hints = {
                "file": self.config.get("hotwords_file"),
                "loaded": 0,
                "active": 0,
                "omitted": 0,
                "tokens": 0,
                "token_limit": 0,
                "text": None,
                "error": str(exc),
            }
            self.inform(
                "Glossary unavailable; transcribing without vocabulary hints. Check service status."
            )
        hints = self.hotword_hints
        if hints["file"] and not hints["error"]:
            LOG.info(
                "Glossary: %s/%s terms active, %s/%s hint tokens",
                hints["active"],
                hints["loaded"],
                hints["tokens"],
                hints["token_limit"],
            )

    def warmup(self):
        try:
            self.phase = "Loading or downloading local speech model"
            self.model = make_model(self.config)
            self.refresh_hotwords()
            with self.lock:
                if not self.closed:
                    self.state = "idle"
                    self.phase = "Ready"
            glossary = (
                f" Glossary: {self.hotword_hints['active']} terms."
                if self.hotword_hints["active"]
                else ""
            )
            keys = self.config.get("hotkeys", DEFAULT_CONFIG["hotkeys"])
            self.inform(
                f"Ready. {keys['toggle']}: record/finish. {keys['path']}: insert path." + glossary
            )
        except Exception as exc:
            LOG.exception("Model loading failed")
            with self.lock:
                self.state, self.last_error = "error", str(exc)
            self.inform(f"Whisper could not load: {exc}")

    def audio_callback(self, indata, frames, timing, status):
        import numpy as np

        if status:
            LOG.warning("Audio: %s", status)
        with self.lock:
            self.audio_level = min(1.0, float(np.sqrt(np.mean(indata**2))) * 8)
            if self.state == "recording":
                # Align event times with the first captured sample, including input latency.
                if not self.chunks:
                    self.started = time.monotonic() - (
                        timing.currentTime - timing.inputBufferAdcTime
                    )
                self.chunks.append(indata.copy())

    def start(self, origin=None):
        import sounddevice as sd

        with self.lock:
            if self.state != "idle":
                raise RuntimeError(f"Cannot record while {self.state}; wait for Ready.")
            if not self.enabled:
                raise RuntimeError("Dictation is paused. Resume it from the panel icon.")
            self.refresh_hotwords()
            self.origin = str(origin or active_window())
            if is_own_window(self.origin):
                raise RuntimeError("Focus a text input, then use the recording shortcut.")
            self.last_error = None
            self.origin_is_code = is_code_window(self.origin)
            self.chunks, self.events = [], []
            self.recording_id = uuid.uuid4().hex
            try:
                self.stream = sd.InputStream(
                    samplerate=16000,
                    channels=1,
                    dtype="float32",
                    device=self.config.get("input_device"),
                    callback=self.audio_callback,
                )
                self.started = time.monotonic()
                self.state = "recording"
                self.stream.start()
            except Exception:
                self.state = "idle"
                if self.stream:
                    self.stream.close()
                    self.stream = None
                raise
            self.timer = threading.Timer(
                self.config["max_seconds"], self.limit_reached, args=(self.recording_id,)
            )
            self.timer.daemon = True
            self.timer.start()
        keys = self.config.get("hotkeys", DEFAULT_CONFIG["hotkeys"])
        self.inform(
            f"Recording — {keys['path']}: insert path; {keys['toggle']}: finish; "
            f"{keys['cancel']}: cancel."
        )

    def limit_reached(self, recording_id):
        with self.lock:
            if self.state != "recording" or self.recording_id != recording_id:
                return
        self.stop()

    def detach_recording(self, next_state):
        with self.lock:
            if self.state != "recording":
                raise RuntimeError("No recording is active.")
            self.state = next_state
            stream, self.stream = self.stream, None
            if self.timer:
                self.timer.cancel()
                self.timer = None
            snapshot = (
                list(self.chunks),
                list(self.events),
                self.origin,
                self.origin_is_code,
                self.recording_id,
            )
            self.chunks, self.events = [], []
        if stream:
            stream.stop()
            stream.close()
        return snapshot

    def stop(self):
        snapshot = self.detach_recording("transcribing")
        self.inform("Transcribing locally…")
        threading.Thread(target=self.process_recording, args=snapshot, daemon=True).start()

    def toggle(self, origin=None):
        # AutoKey/X11 can repeat held keys. Debounce without losing normal toggles.
        now = time.monotonic()
        with self.lock:
            if now - self.last_toggle < 0.4:
                return
            self.last_toggle = now
            state = self.state
        if state == "recording":
            self.stop()
        else:
            self.start(origin)

    def cancel(self):
        if self.state == "testing":
            self.stop_microphone_test()
            return
        with self.lock:
            if self.state == "recording":
                recording = True
            elif self.state in ("transcribing", "delivering"):
                # Keep the service busy until inference ends, but suppress delivery.
                self.recording_id = None
                recording = False
            else:
                return
        if recording:
            self.detach_recording("idle")
        self.inform("Dictation cancelled.")

    def capture_hovered_path(self, event_at):
        with self.lock:
            if self.state != "recording":
                raise RuntimeError("Press F8 to start dictation before capturing a path.")
            recording_id = self.recording_id
        # Inspect the window under the pointer, not whichever application has focus.
        location = run(["xdotool", "getmouselocation", "--shell"], capture_output=True, text=True)
        fields = dict(line.split("=", 1) for line in location.stdout.splitlines() if "=" in line)
        window = fields.get("WINDOW", "0")
        if window == "0" or not is_code_window(window):
            raise RuntimeError("Hover over a file in the VS Code Explorer, then press F9.")
        old_clipboard = clipboard_get()
        sentinel = "talk-and-path-" + uuid.uuid4().hex
        path = None
        try:
            clipboard_set(sentinel)
            run(["xdotool", "windowactivate", "--sync", window])
            # Explorer's context-menu selection highlights the hovered row without
            # firing the normal click handler that opens an editor preview.
            run(["xdotool", "click", "3"])
            time.sleep(0.15)
            send_hotkey("Escape")
            time.sleep(0.08)
            send_hotkey(self.config["copy_path_shortcut"])
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                value = clipboard_get()
                if value and value != sentinel:
                    path = value.strip()
                    break
                time.sleep(0.04)
        finally:
            if old_clipboard is not None:
                clipboard_set(old_clipboard)
            else:
                clipboard_set("")
        if not path or any(c in path for c in ("\n", "\r", "\x00")):
            raise RuntimeError(
                "No file path copied. Point at a file row in the Explorer and try F9 again."
            )
        with self.lock:
            if self.state != "recording" or self.recording_id != recording_id:
                return
            event = {"time": max(0.0, event_at - self.started), "path": path}
            self.events.append(event)
        # Avoid logging private paths or transcripts to the journal.
        self.inform(f"Path inserted: {Path(path).name}", path=True)

    def process_recording(self, chunks, events, origin, origin_is_code, recording_id):
        try:
            import numpy as np

            if not chunks:
                raise RuntimeError("No microphone audio captured.")
            audio = np.concatenate(chunks, axis=0).reshape(-1)
            if len(audio) < 1600:
                raise RuntimeError("Recording too short; speak for at least a moment.")
            segments, info = self.model.transcribe(
                audio,
                language=self.config["language"],
                word_timestamps=True,
                vad_filter=True,
                hotwords=self.hotword_hints["text"],
            )
            segments = list(segments)
            text = merge_paths_into_transcript(segments, events)
            with self.lock:
                if self.closed or self.recording_id != recording_id:
                    return
                self.state = "delivering"
                if not text:
                    self.inform("No speech detected. Nothing pasted.")
                    return
                STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
                last = STATE_DIR / "last-transcript.json"
                payload = {
                    "text": text,
                    "paths": events,
                    "language": info.language,
                    "duration": len(audio) / 16000,
                }
                fd = os.open(last, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
                clipboard_set(text)
                if self.config["auto_paste"] and active_window() == origin:
                    if origin_is_code:
                        send_hotkey(self.config["focus_codex_shortcut"])
                        time.sleep(0.35)
                    if active_window() == origin:
                        send_hotkey("ctrl+v")
                        self.inform("Dictation pasted. Review it before sending.")
                    else:
                        self.inform("Dictation copied. Focus changed; paste with Ctrl+V.")
                else:
                    self.inform("Dictation copied. Paste with Ctrl+V.")
        except Exception as exc:
            LOG.exception("Dictation failed")
            self.last_error = str(exc)
            self.inform(f"Dictation failed: {exc}")
        finally:
            with self.lock:
                self.state = "idle"

    def microphone_devices(self):
        import sounddevice as sd

        devices = sd.query_devices()
        return [
            {"name": device["name"], "channels": device["max_input_channels"]}
            for device in devices
            if device["max_input_channels"] > 0
        ]

    def test_microphone(self, device=None):
        import sounddevice as sd

        with self.lock:
            if self.state != "idle":
                raise RuntimeError(
                    "Finish the current recording or operation before testing the microphone."
                )
            self.state, self.audio_level = "testing", 0.0
            try:
                self.stream = sd.InputStream(
                    samplerate=16000,
                    channels=1,
                    dtype="float32",
                    device=device,
                    callback=self.audio_callback,
                )
                self.stream.start()
            except Exception:
                if self.stream:
                    self.stream.close()
                    self.stream = None
                self.state = "idle"
                raise
            self.microphone_timer = threading.Timer(8, self.stop_microphone_test)
            self.microphone_timer.daemon = True
            self.microphone_timer.start()

    def stop_microphone_test(self):
        with self.lock:
            if self.state != "testing":
                return
            stream, self.stream = self.stream, None
            self.state, self.audio_level = "idle", 0.0
            if self.microphone_timer:
                self.microphone_timer.cancel()
                self.microphone_timer = None
        if stream:
            stream.stop()
            stream.close()

    def apply_configuration(self, config, glossary=None, expected_revision=None):
        config = settings.save_configuration(config, glossary, expected_revision=expected_revision)
        model_keys = ("model", "device", "compute_type", "cpu_threads")
        reload_model = self.model is None or any(
            config[key] != self.config[key] for key in model_keys
        )
        self.config = config
        self.last_error = None
        if reload_model:
            self.state = "loading"
            self.model = None
            threading.Thread(target=self.warmup, daemon=True).start()
        else:
            self.refresh_hotwords()
        self.inform("Settings saved." + (" Loading the selected model…" if reload_model else ""))

    def apply_pending(self):
        if self.state in ("idle", "error") and self.pending_config:
            config, glossary, revision = self.pending_config
            self.pending_config = None
            try:
                self.apply_configuration(config, glossary, revision)
            except Exception as exc:
                self.last_error = str(exc)
                self.inform(f"Settings were not saved: {exc}")

    def dispatch(self, command, event_at=None, payload=None):
        payload = payload or {}
        if command == "status":
            with self.lock:
                return {
                    "state": self.state,
                    "model": self.config["model"],
                    "phase": self.phase,
                    "enabled": self.enabled,
                    "paths": len(self.events),
                    "last_error": self.last_error,
                    "elapsed": max(0, time.monotonic() - self.started)
                    if self.state == "recording"
                    else 0,
                    "audio_level": self.audio_level,
                    "input_device": self.config.get("input_device"),
                    "pending_settings": self.pending_config is not None,
                    "pending_action": self.pending_action,
                    "hotwords": {
                        key: value for key, value in self.hotword_hints.items() if key != "text"
                    },
                    "hotkeys": self.config.get("hotkeys", DEFAULT_CONFIG["hotkeys"]),
                }
        if command == "configuration":
            path = settings.glossary_path(self.config)
            try:
                glossary = path.read_text(encoding="utf-8-sig") if path else ""
                error = None
            except OSError as exc:
                glossary, error = "", str(exc)
            return {"config": self.config, "glossary": glossary, "glossary_error": error}
        if command in ("preview-config", "apply-config"):
            config = settings.validate_config(payload["config"])
            glossary = payload.get("glossary")
            # Validate conflicts even when the actual save must wait for idle.
            settings.configuration_files(config, glossary)
            if command == "preview-config":
                if self.model is None:
                    raise RuntimeError(
                        "Wait for the speech model to load before previewing vocabulary."
                    )
                terms = settings.vocabulary_terms(glossary) if glossary is not None else None
                hints = load_hotwords(config, self.model, terms)
                return {"hotwords": {key: value for key, value in hints.items() if key != "text"}}
            if self.state not in ("idle", "error"):
                self.pending_config = (config, glossary, payload.get("glossary_revision"))
                return {"pending": True}
            self.apply_configuration(config, glossary, payload.get("glossary_revision"))
            return {"pending": False}
        if command == "set-enabled":
            self.enabled = bool(payload["enabled"])
        elif command == "suspend-hotkeys":
            self.suspended_until = time.monotonic() + 30 if payload.get("enabled", True) else 0
        elif command in ("restart", "shutdown"):
            self.pending_action = command
            return {"pending": self.state not in ("idle", "error")}
        elif command == "devices":
            return {"devices": self.microphone_devices()}
        elif command == "test-microphone":
            self.test_microphone(payload.get("input_device"))
        elif command == "stop-microphone-test":
            self.stop_microphone_test()
        elif command == "last-transcript":
            path = STATE_DIR / "last-transcript.json"
            return {"transcript": json.loads(path.read_text()) if path.exists() else None}
        elif command in ("copy-last", "copy-text"):
            if command == "copy-last":
                path = STATE_DIR / "last-transcript.json"
                if not path.exists():
                    raise RuntimeError("No completed transcript is available yet.")
                text = json.loads(path.read_text())["text"]
            else:
                text = payload["text"]
            if not isinstance(text, str):
                raise ValueError("Clipboard content must be text")
            clipboard_set(text)
        elif command in ("toggle", "path", "cancel"):
            if payload.get("source") == "hotkey" and time.monotonic() < self.suspended_until:
                return {"state": self.state, "ignored": True}
            if command == "toggle":
                self.toggle(payload.get("origin"))
            elif command == "path":
                self.capture_hovered_path(event_at or time.monotonic())
            else:
                self.cancel()
        else:
            raise ValueError(f"Unknown command: {command}")
        return {"state": self.state}

    def close(self):
        self.stop_microphone_test()
        with self.lock:
            self.closed = True
            self.recording_id = None
            state = self.state
        if state == "recording":
            self.detach_recording("idle")


def serve(config):
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    # File lock protects against two simultaneous service/CLI startups.
    with (RUNTIME_DIR / "service.lock").open("w") as lockfile:
        try:
            fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Talk and Path is already running.") from None
        SOCKET_PATH.unlink(missing_ok=True)
        service = DictationService(config)
        stopping = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stopping.set())
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(SOCKET_PATH))
            os.chmod(SOCKET_PATH, 0o600)
            server.listen(8)
            server.settimeout(0.25)
            threading.Thread(target=service.warmup, daemon=True).start()
            try:
                while not stopping.is_set():
                    service.apply_pending()
                    if service.pending_action and service.state in ("idle", "error"):
                        break
                    try:
                        client, _ = server.accept()
                    except TimeoutError:
                        continue
                    with client:
                        client.settimeout(2)
                        try:
                            request = b""
                            while b"\n" not in request and len(request) <= 256 * 1024:
                                chunk = client.recv(4096)
                                if not chunk:
                                    break
                                request += chunk
                            if len(request) > 256 * 1024:
                                raise ValueError("Request exceeds 256 KB")
                            data = json.loads(request)
                            result = service.dispatch(
                                data["command"], data.get("event_at"), data.get("payload")
                            )
                            response = {"ok": True, **result}
                        except Exception as exc:
                            LOG.warning("Command failed: %s", exc)
                            response = {"ok": False, "error": str(exc)}
                            service.inform(str(exc))
                        try:
                            client.sendall(json.dumps(response).encode() + b"\n")
                        except (TimeoutError, BrokenPipeError):
                            pass
            finally:
                service.close()
                SOCKET_PATH.unlink(missing_ok=True)
        return service.pending_action


def send_command(command, payload=None, autostart=True):
    event_at = time.monotonic()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(15 if command == "apply-config" else 6)
        try:
            client.connect(str(SOCKET_PATH))
        except (FileNotFoundError, ConnectionRefusedError):
            if not autostart:
                raise RuntimeError("Dictation service is stopped or unavailable.") from None
            run(["systemctl", "--user", "start", "talk-and-path.service"])
            deadline = time.monotonic() + 3
            while True:
                try:
                    client.connect(str(SOCKET_PATH))
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    if time.monotonic() > deadline:
                        raise RuntimeError(
                            "Talk and Path service did not start; check its journal."
                        ) from None
                    time.sleep(0.05)
        client.sendall(
            json.dumps(
                {"command": command, "event_at": event_at, "payload": payload or {}}
            ).encode()
            + b"\n"
        )
        result = b""
        while b"\n" not in result:
            chunk = client.recv(4096)
            if not chunk:
                raise RuntimeError("Service disconnected.")
            result += chunk
    response = json.loads(result)
    if not response["ok"]:
        raise RuntimeError(response["error"])
    return response


def doctor():
    checks = {}
    for name in ("xdotool", "xclip", "xprop", "notify-send", "systemctl"):
        checks[name] = bool(shutil.which(name))
    checks["x11"] = (
        bool(os.environ.get("DISPLAY")) and os.environ.get("XDG_SESSION_TYPE", "x11") == "x11"
    )
    try:
        import sounddevice as sd

        checks["microphone"] = sd.query_devices(kind="input")["max_input_channels"] > 0
        sd.check_input_settings(samplerate=16000, channels=1, dtype="float32")
    except Exception as exc:
        checks["microphone"] = str(exc)
    for name, result in checks.items():
        print(f"{name}: {result}")
    return all(result is True for result in checks.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "serve",
            "toggle",
            "path",
            "cancel",
            "status",
            "warmup",
            "doctor",
            "transcribe-file",
        ],
    )
    parser.add_argument("audio_file", nargs="?")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config()
    try:
        if args.command == "serve":
            action = serve(config)
            if action == "restart":
                os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve()), "serve"])
        elif args.command == "doctor":
            return 0 if doctor() else 1
        elif args.command == "warmup":
            make_model(config)
            print("Whisper ready.")
        elif args.command == "transcribe-file":
            if not args.audio_file:
                parser.error("transcribe-file requires an audio file")
            model = make_model(config)
            hints = load_hotwords(config, model)
            segments, _ = model.transcribe(
                args.audio_file,
                language=config["language"],
                word_timestamps=True,
                vad_filter=True,
                hotwords=hints["text"],
            )
            print(merge_paths_into_transcript(list(segments), []))
        else:
            print(json.dumps(send_command(args.command, {"source": "hotkey"})))
    except Exception as exc:
        LOG.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
