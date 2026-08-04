# Contributing to voice-to-clipboard

Thanks for your interest! This is a small, focused project — the bar for a
useful contribution is low and PRs are welcome.

## Development setup

```bash
git clone https://github.com/binaryninja/voice-to-clipboard.git
cd voice-to-clipboard
uv sync                      # installs the project editable + all deps
.venv/bin/voice-to-clipboard # run it
```

You need an X11 session with a working microphone to exercise most features.
A CUDA GPU is the default target, but `device = "cpu"` with
`compute_type = "int8"` in the config works for development.

## Code layout

| File | Responsibility |
|---|---|
| `app.py` | Qt tray app, state machine (idle → recording → busy), menu, signals |
| `hotkey.py` | Global hotkey listeners (keyboard + mouse via pynput) and the "press to capture" flow |
| `recorder.py` | Microphone capture (sounddevice, 16 kHz mono) |
| `transcribe.py` | Model backends: qwen-asr, granite-speech, faster-whisper, transformers + VAD gate |
| `clipboard.py` | Delivery: xclip copy, or typing/pasting into the focused window |
| `sounds.py` | Recording drone and result tones |
| `config.py` | TOML config load/save |
| `service.py` | systemd user service install/uninstall |
| `leaderboard.py` | Open ASR Leaderboard fetch/ranking and backend compatibility rules |
| `icons.py` | Tray icons drawn with QPainter |

Threading model: Qt main loop owns all UI. pynput listeners and transcription
workers run in their own threads and communicate with the main thread
exclusively through the `Bridge` Qt signals.

## Style

- `ruff check .` must pass (CI enforces it).
- Match the existing code: type hints on public functions, small modules,
  comments only where the code can't speak for itself.

## Testing

There is no automated test suite yet (the interesting behavior needs a real
X session, mic, and GPU — contributions welcome). Manual smoke test:

1. `xdotool key ctrl+alt+m` style simulated input works for hotkey testing —
   **never simulate Ctrl+Alt+F-keys; they switch virtual terminals on X11.**
2. Both delivery paths can be tested against a scratch `QPlainTextEdit`
   window running in a *separate process* (same-process windows block on the
   event loop and give false failures).
3. Watch `~/.local/state/voice-to-clipboard/app.log` while testing.
4. Never call `show()` on the tray menu or its submenus — KDE exports them
   over DBus and showing them directly crashes the app (use `sizeHint()` to
   warm geometry instead).

## Pull requests

- Keep PRs focused; one feature/fix per PR.
- Describe how you tested it (desktop environment, X11/Wayland, GPU/CPU).
- Update README/config comments when behavior changes.

## Ideas looking for an owner

- Wayland support (global shortcuts portal, virtual keyboard protocol)
- Packaging: AUR, deb, Flatpak (CUDA makes this spicy)
- VAD-based auto-stop ("stop when I stop talking")
- Streaming transcription for long dictation
- CPU-only install preset
