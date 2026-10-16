# Voice dictation with hovered file paths

## Panel icon and settings

Talk and Path now has a microphone icon in the MATE panel. Click it for recording,
cancel, pause/resume, transcript review, and service controls. The icon changes
shape and colour while recording, transcribing, loading, paused, or unavailable;
recording also shows an elapsed-time label.

Choose **Settings…** to configure:

- Start/finish, path-capture, and cancel shortcuts, with a Capture button and
  checks against existing AutoKey assignments.
- The glossary file, editable terms, priorities, ordering, and the actual
  Active / Outside hint limit selection.
- The input microphone, an eight-second level test, language, recording duration,
  clipboard/paste delivery, and desktop notifications.
- Local model, CPU threads, and startup at login.

You can also open Settings from the application menu's **Talk and Path** entry or
run `~/.local/bin/talk-and-path`. Opening Settings does not load a second Whisper
model. Saving unrelated preferences keeps the loaded model; changes that would
interrupt recording or transcription wait until the current operation finishes.
Vocabulary preview uses the running model's tokenizer. Changing the model may
require a first-use download; the panel shows loading until it is ready.

Pause prevents new recordings while leaving finish/cancel available for an
existing recording. It does not pause your other AutoKey macros. **Quit panel
icon** closes only the interface. **Stop dictation service** waits for any active
operation to finish and stops the backend; the panel offers Start to bring it back.
Using a recording hotkey can also explicitly start the backend again.

The glossary and configuration are backed up before settings are saved. Failed
multi-file saves restore the previous configuration, vocabulary, and AutoKey
entries. The most recent transcript can be reviewed and copied; it is never sent
automatically.

This integration runs `talk_and_path.py` as a background user service and uses
your existing AutoKey installation. Transcription uses local faster-whisper
`small`, CPU INT8, with no API key or transcription charges. The model downloads
once (about 464 MB); subsequent starts use the cached model without contacting
Hugging Face. The upstream GUI and its CUDA/PyTorch dependencies are unnecessary.

## Use it

1. Put the cursor in the Codex prompt, or another application's text input.
2. **Press F8** to begin. A desktop notification confirms recording.
3. Speak. To mention a file, hover over its row in the **VS Code Explorer** and
   **press F9**. The script highlights the row without opening the file, copies
   its full absolute path, and restores your previous text clipboard. A
   “Path inserted: filename” notification confirms capture. The context menu
   briefly appears and closes automatically. You can capture several paths.
4. **Press F8 again** to finish. Whisper transcribes locally, and the paths are
   inserted as Markdown code at the corresponding points in your speech.
5. The result is copied and pasted. In VS Code, the Codex input is focused first.
   Review the result, then send it yourself.

**Shift+F8** cancels a recording or suppresses delivery of an ongoing
transcription. Recordings stop automatically after three minutes. Wait for
transcription to finish before starting another recording.

If you switch windows while transcription runs, the result stays on the
clipboard and you can paste it with Ctrl+V. F9 works on Explorer file rows;
hovering over editor text or another application does not provide a file path.
Your mouse hover alone does not insert a path: press F9 at the point where the
reference belongs in your speech. Path timing uses Whisper word timestamps,
including pauses; events within a word are inserted before the following word.

For example, say “Compare this”, press F9 over `src/example.py`, then say “with
the test”, and press F9 over `tests/test_example.py`. The resulting prompt is:

```text
Compare this `/home/heopd/push_to_talk/src/example.py` with the test `/home/heopd/push_to_talk/tests/test_example.py`
```

## Installation and settings

The integration is installed on this desktop. To reinstall from this checkout:

```bash
python3 scripts/install_dictation.py
```

Required system packages: `autokey-gtk`, `xdotool`, `xclip`, `x11-utils` (xprop),
`libportaudio2`, `libnotify-bin`, `python3-xlib`, `python3-gi`, `gir1.2-gtk-3.0`,
and `gir1.2-ayatanaappindicator3-0.1`. These were already present.
Python dependencies are isolated in `.venv-dictation` and listed in
`requirements-dictation.txt`; the installer uses `uv`.

Settings are in `~/.config/talk-and-path/config.json`. Set `language` to `"en"`
for English only, or leave it `null` for automatic detection. Use `"model":
"base"` if you prefer faster CPU transcription. Set `auto_paste` to `false` for
clipboard-only delivery. Restart the service after changing settings:

```bash
systemctl --user restart talk-and-path.service
```

### Vocabulary hints

Whisper now reads the aaabphd glossary from
`/home/heopd/aaabphd/dictation_glossary.candidate.txt`. It supplies the selected
entries through faster-whisper's `hotwords` argument for both dictation and
`transcribe-file`. This selected glossary applies to all recordings; workspace
detection is not configured.

The file contains one term or phrase per line. Spelling and capitals are
preserved, including `gene`, `genes`, `NON`, `UP`, `DOWN`, `ABA-DL`, and
`ABA-DL pipeline`. Blank lines and lines starting with `#` are ignored. The file
is reread before each recording, so editing its entries needs no service restart.

Whisper `small` accepts up to 223 hint tokens. If the complete glossary is larger
than that, the configured priority selection takes precedence, then additional
complete terms are fitted in file order. Settings and status show the current
active and omitted counts. No term is cut halfway through. Status
reports the loaded and active counts, omitted count, and token usage. These hints
encourage terminology and casing but do not force exact transcriptions.

The configuration keys are `hotwords_file` (the source path) and
`hotwords_priority` (an ordered list of preferred entries already in that file).
Use Settings to change priorities or disable vocabulary hints without manually
restarting. If editing configuration JSON directly, restart the service afterward.
Set `hotwords_file` to `null` to disable vocabulary hints. If the file
cannot be read, a notification and status error identify the problem and
dictation continues without hints.

Installed integration files:

- AutoKey scripts: `~/.config/autokey/data/Talk and Path/`.
- VS Code and Insiders keybindings: Ctrl+Alt+Shift+P → Copy Path (absolute)
  (Explorer only), Ctrl+Alt+Shift+I → Open Codex Sidebar.
- Service: `~/.config/systemd/user/talk-and-path.service`.
- Panel frontend: `~/.config/systemd/user/talk-and-path-tray.service`.
- Desktop login startup: `~/.config/autostart/talk-and-path.desktop`, using
  `~/.local/bin/talk-and-path-session` to import the X11 environment.
- Backups of the original prototype and modified keybindings:
  `~/.local/state/talk-and-path/backups/`.

The installer preserves existing editor shortcuts and refuses conflicting
bindings. AutoKey normally detects new scripts automatically. If its tray icon
shows the service paused, unpause it before trying F8.

## Status and troubleshooting

```bash
.venv-dictation/bin/python talk_and_path.py status
.venv-dictation/bin/python talk_and_path.py doctor
journalctl --user -u talk-and-path.service -n 30
```

Microphone capture uses the input selected in Settings, or the system default.
For a transcription-only check of an audio file:

```bash
.venv-dictation/bin/python talk_and_path.py transcribe-file recording.wav
```

The most recent successful transcript and path events are saved locally, with
user-only file permissions, at
`~/.local/state/talk-and-path/last-transcript.json`. Microphone recordings are
kept in memory and are not saved. Logs omit transcript contents and full paths.
Clipboard restoration preserves text; non-text clipboard contents are not
restored. This implementation requires X11.

To disable the integration, stop the service and remove its desktop startup and
the AutoKey `Talk and Path` folder. Remove the two added editor bindings if you
no longer need them:

```bash
systemctl --user stop talk-and-path.service talk-and-path-tray.service
rm ~/.config/autostart/talk-and-path.desktop
```

## Verification

```bash
.venv-dictation/bin/python -m unittest discover -s tests -v
sh tests/run_tray_smoke.sh
```

The tests cover path ordering, pauses, cancellation, clipboard restoration,
window changes, settings rollback, deferred saves, shortcut conflicts, microphone
selection, vocabulary preview, and preserving JSONC editor settings. The GTK
smoke check runs on an isolated X display and D-Bus session. Desktop verification also
checks the real AutoKey F8/F9 bindings, microphone capture, and synthesized
speech transcription and paste into a temporary text window.
