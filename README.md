# voice-to-clipboard

Local, GPU-accelerated dictation for Linux: press a hotkey (or mouse button),
talk, and the transcription lands on your clipboard — or is typed straight
into the text box you're focused on. Runs entirely on your machine with a
system-tray toggle, and lets you swap in new speech-to-text models from
Hugging Face as they come out.

## Features

- **Global hotkey** — any keyboard combo *or mouse button* (side buttons
  work great), set from the tray menu by just pressing it
- **Two trigger styles** — toggle (press to start, press to stop) or
  push-to-talk (hold while talking, release to transcribe)
- **Two delivery styles** — copy to clipboard (main hotkey), or type directly
  into the focused window (optional second hotkey, clipboard left untouched)
- **System tray control** — enable/disable, model picker, unload the model to
  free GPU memory (auto-reloads on next use), status at a glance
- **Swappable models** — `faster-whisper` (CTranslate2, fastest) or any
  Hugging Face `automatic-speech-recognition` model via the `transformers`
  backend
- **Fully local** — audio never leaves your machine

## Requirements

- Linux with an **X11** session (Wayland is not supported yet — see
  [Limitations](#limitations))
- NVIDIA GPU + driver for CUDA inference (CPU works too, just slower —
  set `device = "cpu"` and `compute_type = "int8"`)
- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- System packages: `xclip`, PortAudio (`libportaudio2`), and a
  PulseAudio/PipeWire audio stack

  ```bash
  sudo apt install xclip libportaudio2   # Debian/Ubuntu
  ```

## Quick start

```bash
git clone https://github.com/binaryninja/voice-to-clipboard.git
cd voice-to-clipboard
uv sync                              # creates .venv with all dependencies
.venv/bin/voice-to-clipboard &       # start it — a mic icon appears in the tray
```

Press **Ctrl+Alt+Space**, say something, press it again — the text is on your
clipboard. The default model (Qwen3-ASR-1.7B, near the top of the
[Open ASR Leaderboard](https://huggingface.co/spaces/hf-audio/open_asr_leaderboard))
downloads on first use (~3.5 GB). Optionally, a soft 417 Hz om drone can play
while recording so you always know the mic is live (`record_tone = true`).

The tray icon shows state: **purple** idle, **red** recording, **orange**
transcribing, **gray/slashed** disabled.

## Run as a service

```bash
./install.sh          # or: .venv/bin/voice-to-clipboard --install-service
```

This installs a **systemd user service** bound to `graphical-session.target`:
it starts on login, restarts on failure, and logs to the journal.

```bash
systemctl --user status voice-to-clipboard    # check it
systemctl --user restart voice-to-clipboard   # restart it
journalctl --user -u voice-to-clipboard -f    # follow logs
.venv/bin/voice-to-clipboard --uninstall-service   # remove it
```

### Tray menu

| Item | What it does |
|---|---|
| Enabled | Master on/off for the hotkeys |
| Model → | Switch between your favorite models (from the config) |
| Unload model | Frees GPU memory; reloads automatically on next use |
| Set hotkey… | Press any key combo or mouse button to bind it |
| Set paste hotkey… | Optional second hotkey that *types* the result into the focused text box instead of copying it |
| Push-to-talk (hold) | Switch between toggle and hold-to-record |

## Configuration

`~/.config/voice-to-clipboard/config.toml` — edit, then tray → "Reload
config". Everything the tray can do, the file can too:

```toml
hotkey = "<ctrl>+<alt>+<space>"  # or "mouse:button9", "<ctrl>+mouse:button8", "<f9>"
paste_hotkey = ""                # optional 2nd hotkey: types into focused window
mode = "toggle"                  # "toggle" = press twice; "hold" = push-to-talk
backend = "qwen-asr"             # or "faster-whisper", "transformers"
model = "Qwen/Qwen3-ASR-1.7B-hf"
language = ""                    # "" = auto-detect, or "en", "de", ...
device = "cuda"                  # or "cpu"
compute_type = "float16"         # faster-whisper only: float16, int8_float16, int8
max_seconds = 120                # auto-stop safety limit
sounds = true                    # success/error result tones
record_tone = false              # 417 Hz om drone while recording

[models]                         # favorites shown in the tray menu (keep labels short)
"large-v3 (fw)" = "faster-whisper:large-v3"
"large-v3-turbo (hf)" = "transformers:openai/whisper-large-v3-turbo"
```

## Swapping in new models from Hugging Face

Four backends:

- **qwen-asr** — the Qwen3-ASR family (`Qwen/Qwen3-ASR-1.7B-hf`, …) via the
  transformers multimodal-LM API. Best open-leaderboard accuracy (~4.98% avg
  WER vs 6.55% for whisper-large-v3), 28 languages, and it returns an empty
  string on silence instead of hallucinating. Needs `transformers >= 5.13`.
- **granite-speech** — the IBM granite-speech family
  (`ibm-granite/granite-speech-4.1-2b`, …) via its chat-template API
  (~4.90% avg WER). A Silero VAD gate guards all non-whisper backends against
  hallucinating text from silence or mic noise.
- **faster-whisper** — CTranslate2, the fastest Whisper option. Accepts
  built-in size aliases (`large-v3`, `distil-large-v3.5`, `turbo`, …), any HF
  repo containing a CTranslate2 conversion (e.g.
  `deepdml/faster-whisper-large-v3-turbo-ct2`), or a local path.
- **transformers** — runs **any** HF model compatible with the
  `automatic-speech-recognition` pipeline (new Whisper fine-tunes,
  distil-whisper, CrisperWhisper, …). The escape hatch for models that
  dropped yesterday. Note: models whose cards require `trust_remote_code`
  are deliberately not supported.

Add a line under `[models]` (`"Label" = "backend:model-id"`), tray → Reload
config, pick it in the Model menu. It downloads to your HF cache on first
load.

### Leaderboard check

Tray → **"Check ASR leaderboard…"** (or `voice-to-clipboard
--check-leaderboard` in a terminal) fetches the latest
[Open ASR Leaderboard](https://huggingface.co/spaces/hf-audio/open_asr_leaderboard)
results — the same CSV the public leaderboard renders, ranked by "avg
cleaned" English WER — and shows which models this app can actually run.

By default the dialog shows **only models you can select on this machine**:
proprietary/API-only entries, models requiring `trust_remote_code`, and
models without a standard loader are hidden ("Show full leaderboard" reveals
them with the exclusion reason). The app also detects your hardware — with a
GPU, models that fit your VRAM get a ✓; without one, only CPU-sized models
(≤1B params) are offered. Pick a model to switch to it and add it to your
favorites; models marked *(untested)* have a compatible pipeline tag but an
architecture we haven't verified — if one fails to load you'll get a
notification and can switch back.

## Troubleshooting

- **No tray icon** — your desktop needs a StatusNotifier-capable tray (KDE,
  most others). On GNOME install an AppIndicator extension.
- **`CUDA driver version is insufficient` / kernel errors** — the pinned
  PyTorch index is CUDA 12.8 (needed for RTX 50-series). Older cards/drivers
  may need a different index in `pyproject.toml`.
- **Nothing transcribed** — check the input device: recording uses the
  default source. `pavucontrol` shows what the app captures.
- **Paste hotkey doesn't type into terminals** — it does for plain-ASCII
  text; text with special characters falls back to Ctrl+V paste, which most
  terminals don't accept. See `clipboard.py` for the why.
- Logs: `~/.local/state/voice-to-clipboard/app.log`

## Limitations

- **X11 only.** Global hotkeys and synthetic typing use X11 mechanisms
  (XRecord/XTEST via pynput). Wayland needs a different approach (portals /
  compositor bindings) — contributions welcome.
- Model labels in the tray should be short; some desktops elide long menu
  items.
- Restoring a *non-text* clipboard (e.g. an image) after a unicode paste
  delivery isn't possible.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Good first areas: Wayland support,
packaging (AUR/deb/flatpak), VAD-based auto-stop, streaming transcription.

## License

[MIT](LICENSE)
