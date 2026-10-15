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
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "talk-and-path"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "talk-and-path"
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/talk-and-path-{os.getuid()}")) / "talk-and-path"
SOCKET_PATH = RUNTIME_DIR / "control.sock"
DEFAULT_CONFIG = {
    "model": "small", "device": "cpu", "compute_type": "int8", "cpu_threads": 4,
    "language": None, "max_seconds": 180, "auto_paste": True,
    "copy_path_shortcut": "ctrl+alt+shift+p",
    "focus_codex_shortcut": "ctrl+alt+shift+i",
}
LOG = logging.getLogger("talk-and-path")


def load_config():
    config = dict(DEFAULT_CONFIG)
    path = CONFIG_DIR / "config.json"
    if path.exists():
        config.update(json.loads(path.read_text()))
    return config


def run(args, **kwargs):
    return subprocess.run(args, check=True, timeout=3, **kwargs)


def notify(message):
    LOG.info(message)
    if shutil.which("notify-send"):
        try:
            run(["notify-send", "--app-name=Talk and Path", "--expire-time=4500",
                 "Talk and Path", message], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            LOG.debug("Notification unavailable")


def active_window():
    return run(["xdotool", "getactivewindow"], capture_output=True, text=True).stdout.strip()


def window_class(window):
    result = run(["xprop", "-id", str(window), "WM_CLASS"], capture_output=True, text=True)
    return result.stdout.lower()


def is_code_window(window):
    return any(f'"{name}"' in window_class(window)
               for name in ("code", "code - insiders", "code-insiders", "vscodium", "codium"))


def send_hotkey(shortcut):
    run(["xdotool", "key", "--clearmodifiers", shortcut], stdout=subprocess.DEVNULL)


def clipboard_get():
    result = subprocess.run(["xclip", "-selection", "clipboard", "-o"],
                            capture_output=True, timeout=2)
    if result.returncode:
        return None
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


def clipboard_set(text):
    # xclip forks a selection owner; do not leave its inherited pipes attached.
    run(["xclip", "-selection", "clipboard", "-i"], input=text.encode("utf-8"),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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
            notify("Downloading the Whisper model for first use…")
            model_path = download_model(model_path)
    return WhisperModel(model_path, device=config["device"],
                        compute_type=config["compute_type"], cpu_threads=config["cpu_threads"])


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

    def warmup(self):
        try:
            self.model = make_model(self.config)
            with self.lock:
                if not self.closed:
                    self.state = "idle"
            notify("Ready. F8: record/finish. F9: insert hovered Explorer path.")
        except Exception as exc:
            LOG.exception("Model loading failed")
            with self.lock:
                self.state, self.last_error = "error", str(exc)
            notify(f"Whisper could not load: {exc}")

    def audio_callback(self, indata, frames, timing, status):
        if status:
            LOG.warning("Audio: %s", status)
        with self.lock:
            if self.state == "recording":
                # Align event times with the first captured sample, including input latency.
                if not self.chunks:
                    self.started = time.monotonic() - (timing.currentTime - timing.inputBufferAdcTime)
                self.chunks.append(indata.copy())

    def start(self):
        import sounddevice as sd
        with self.lock:
            if self.state != "idle":
                raise RuntimeError(f"Cannot record while {self.state}; wait for Ready.")
            self.origin = active_window()
            self.origin_is_code = is_code_window(self.origin)
            self.chunks, self.events = [], []
            self.recording_id = uuid.uuid4().hex
            try:
                self.stream = sd.InputStream(samplerate=16000, channels=1, dtype="float32",
                                             callback=self.audio_callback)
                self.started = time.monotonic()
                self.state = "recording"
                self.stream.start()
            except Exception:
                self.state = "idle"
                if self.stream:
                    self.stream.close()
                    self.stream = None
                raise
            self.timer = threading.Timer(self.config["max_seconds"], self.limit_reached,
                                         args=(self.recording_id,))
            self.timer.daemon = True
            self.timer.start()
        notify("Recording — F9 inserts a hovered file path. F8 finishes; Shift+F8 cancels.")

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
            snapshot = (list(self.chunks), list(self.events), self.origin, self.origin_is_code,
                        self.recording_id)
            self.chunks, self.events = [], []
        if stream:
            stream.stop()
            stream.close()
        return snapshot

    def stop(self):
        snapshot = self.detach_recording("transcribing")
        notify("Transcribing locally…")
        threading.Thread(target=self.process_recording, args=snapshot, daemon=True).start()

    def toggle(self):
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
            self.start()

    def cancel(self):
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
        notify("Dictation cancelled.")

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
            run(["xdotool", "click", "1"])
            time.sleep(0.12)
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
            raise RuntimeError("No file path copied. Point at a file row in the Explorer and try F9 again.")
        with self.lock:
            if self.state != "recording" or self.recording_id != recording_id:
                return
            event = {"time": max(0.0, event_at - self.started), "path": path}
            self.events.append(event)
        # Avoid logging private paths or transcripts to the journal.
        notify(f"Path inserted: {Path(path).name}")

    def process_recording(self, chunks, events, origin, origin_is_code, recording_id):
        try:
            import numpy as np
            if not chunks:
                raise RuntimeError("No microphone audio captured.")
            audio = np.concatenate(chunks, axis=0).reshape(-1)
            if len(audio) < 1600:
                raise RuntimeError("Recording too short; speak for at least a moment.")
            segments, info = self.model.transcribe(audio, language=self.config["language"],
                                                   word_timestamps=True, vad_filter=True)
            segments = list(segments)
            text = merge_paths_into_transcript(segments, events)
            with self.lock:
                if self.closed or self.recording_id != recording_id:
                    return
                self.state = "delivering"
                if not text:
                    notify("No speech detected. Nothing pasted.")
                    return
                STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
                last = STATE_DIR / "last-transcript.json"
                payload = {"text": text, "paths": events, "language": info.language,
                           "duration": len(audio) / 16000}
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
                        notify("Dictation pasted. Review it before sending.")
                    else:
                        notify("Dictation copied. Focus changed; paste with Ctrl+V.")
                else:
                    notify("Dictation copied. Paste with Ctrl+V.")
        except Exception as exc:
            LOG.exception("Dictation failed")
            self.last_error = str(exc)
            notify(f"Dictation failed: {exc}")
        finally:
            with self.lock:
                self.state = "idle"

    def dispatch(self, command, event_at=None):
        if command == "status":
            with self.lock:
                return {"state": self.state, "model": self.config["model"],
                        "paths": len(self.events), "last_error": self.last_error}
        if command == "toggle":
            self.toggle()
        elif command == "path":
            self.capture_hovered_path(event_at or time.monotonic())
        elif command == "cancel":
            self.cancel()
        else:
            raise ValueError(f"Unknown command: {command}")
        return {"state": self.state}

    def close(self):
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
            raise SystemExit("Talk and Path is already running.")
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
                    try:
                        client, _ = server.accept()
                    except socket.timeout:
                        continue
                    with client:
                        client.settimeout(2)
                        try:
                            request = b""
                            while b"\n" not in request and len(request) < 4096:
                                chunk = client.recv(4096)
                                if not chunk:
                                    break
                                request += chunk
                            data = json.loads(request)
                            result = service.dispatch(data["command"], data.get("event_at"))
                            response = {"ok": True, **result}
                        except Exception as exc:
                            LOG.warning("Command failed: %s", exc)
                            response = {"ok": False, "error": str(exc)}
                            notify(str(exc))
                        try:
                            client.sendall(json.dumps(response).encode() + b"\n")
                        except (BrokenPipeError, socket.timeout):
                            pass
            finally:
                service.close()
                SOCKET_PATH.unlink(missing_ok=True)


def send_command(command):
    event_at = time.monotonic()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(6)
        try:
            client.connect(str(SOCKET_PATH))
        except (FileNotFoundError, ConnectionRefusedError):
            run(["systemctl", "--user", "start", "talk-and-path.service"])
            deadline = time.monotonic() + 3
            while True:
                try:
                    client.connect(str(SOCKET_PATH))
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    if time.monotonic() > deadline:
                        raise RuntimeError("Talk and Path service did not start; check its journal.")
                    time.sleep(0.05)
        client.sendall(json.dumps({"command": command, "event_at": event_at}).encode() + b"\n")
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
    checks["x11"] = bool(os.environ.get("DISPLAY")) and os.environ.get("XDG_SESSION_TYPE", "x11") == "x11"
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
    parser.add_argument("command", choices=["serve", "toggle", "path", "cancel", "status", "warmup", "doctor", "transcribe-file"])
    parser.add_argument("audio_file", nargs="?")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config()
    try:
        if args.command == "serve":
            serve(config)
        elif args.command == "doctor":
            return 0 if doctor() else 1
        elif args.command == "warmup":
            make_model(config)
            print("Whisper ready.")
        elif args.command == "transcribe-file":
            if not args.audio_file:
                parser.error("transcribe-file requires an audio file")
            segments, _ = make_model(config).transcribe(args.audio_file, language=config["language"],
                                                       word_timestamps=True, vad_filter=True)
            print(merge_paths_into_transcript(list(segments), []))
        else:
            print(json.dumps(send_command(args.command)))
    except Exception as exc:
        LOG.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
