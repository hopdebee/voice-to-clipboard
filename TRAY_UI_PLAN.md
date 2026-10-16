# Plan: MATE panel icon and settings for Talk and Path

Status: first version implemented. See [DICTATION.md](DICTATION.md) for current
panel controls, settings, startup, and verification. The later additions below
remain separate future work, except last-transcript review, which is included.

## Intended experience

A microphone icon appears in the MATE panel near the sound and clock indicators.
It starts at desktop login and shows whether dictation is ready, recording,
transcribing, paused, loading, or unavailable. Clicking it opens a compact menu;
Settings opens a small desktop window.

Keep the existing F8 recording toggle, F9 hovered-path capture, Shift+F8 cancel,
absolute paths, and local Whisper transcription as the initial defaults.

## First version

### Panel icon and menu

Use recognisable icon shapes as well as colour:

| State | Appearance and information |
| --- | --- |
| Ready | Normal microphone; current model and vocabulary profile in the menu |
| Recording | Red microphone with recording mark; elapsed time and captured-path count |
| Transcribing | Amber working indicator; recording controls disabled as appropriate |
| Loading | Loading indicator with explanatory status |
| Paused | Muted microphone; new recordings disabled |
| Error or service stopped | Warning/offline icon with a useful explanation and recovery action |

Menu actions:

- Start / finish recording.
- Cancel recording or pending transcription delivery.
- Pause / resume dictation.
- Copy last transcript.
- Settings.
- Restart dictation service when idle, or queue the restart until idle.
- Quit panel icon, with a separate clearly named action to stop dictation.

Remember the text-entry target before opening the menu, so starting dictation
from the icon does not make the panel itself the paste destination. Preserve the
existing behaviour of copying instead of pasting when the user changes windows.

F9 remains a keyboard action: clicking the panel moves the pointer away from the
file. Successful capture retains the file-row highlight and filename notification.

### Settings window

**Shortcuts**

- Editable shortcuts for start/finish, hovered-path insertion, and cancellation.
- Capture a shortcut by pressing it in a dedicated field.
- Check existing AutoKey assignments for conflicts before applying changes.
- Update only the Talk and Path AutoKey entries; preserve unrelated macros.
- Keep VS Code helper shortcuts under an advanced section.
- Make the saved shortcut configuration the source for generating the AutoKey
  scripts, including their protection against repeated key presses.

**Vocabulary**

- Show and choose the glossary file; initially use the aaabphd candidate file.
- Edit one term or phrase per row, preserving exact spelling and capitals.
- Mark priority terms and move them up or down without editing JSON.
- Show all entries, with an Active / Outside hint limit distinction.
- Display a clear summary such as “49 active / 116 listed”.
- Preview the selection before saving, using the running model's actual tokenizer.
- Save vocabulary and priorities together, with validation and backup/rollback.
- Make clear that the selected glossary currently applies to all recordings.

The editor must expose Whisper's limit rather than imply that every listed term
is active. The full list remains available even when only a subset fits.

**Audio and output**

- Microphone selector with “System default” as the initial choice.
- A deliberate Test microphone action showing a level meter while the test runs.
- Language selection, including automatic detection.
- Clipboard-only or paste-into-target output.
- Maximum recording duration.
- Notification preferences, including path-capture confirmation.

**Service and model**

- Current service state, model, and last error.
- Start-at-login setting for the panel app and dictation service.
- Model selector, initially offering the established local CPU choices.
- Explain when a model change needs a download or reload; show progress.
- Open configuration, glossary, or diagnostics from the window.

## Implementation approach

Use Python with GTK 3 and an Ayatana application indicator. This computer already
has `python3-gi`, GTK 3 introspection, and both AppIndicator/Ayatana libraries
installed. Ayatana is a desktop-oriented implementation of the application
indicator / StatusNotifierItem approach:
<https://ayatanaindicators.github.io/>.

Run the panel frontend with the system Python that provides GTK. Keep Whisper in
the existing `.venv-dictation` process. This avoids loading another speech model
when opening Settings and keeps the interface responsive during transcription.

Use the existing private Unix socket for communication. Extend it with:

- Read current configuration and status, including microphone, elapsed recording
  time, enabled state, and active vocabulary details.
- Validate/preview configuration and vocabulary selection without applying it.
- Apply configuration, deferring changes that would disrupt an active recording.
- Pause/resume this dictation service only, without pausing AutoKey globally.
- Copy the most recent transcript through an explicit action.

Status updates and socket requests must run outside the GTK main loop. Losing the
backend connection changes the icon to unavailable; the frontend stays usable for
diagnostics and recovery. Routine status polling must not restart a deliberately
stopped backend.

Keep recording completion and cancellation available when pausing during an
active recording. Do not discard work or reload the model as a side effect of
editing an unrelated setting.

Split shared configuration, vocabulary parsing, and AutoKey generation into
reusable modules where needed. The repository's upstream Qt tray app can provide
design references, but the new frontend should control `talk_and_path.py` rather
than start a second recorder or hotkey listener.

## Delivery sequence

1. **Prepare the backend interface.** Add validated configuration access,
   vocabulary preview, dictation enable/disable, and safe deferred reloads.
2. **Add the panel icon.** Implement state updates, recording controls, target
   preservation, reconnect handling, and diagnostics.
3. **Add settings.** Start with shortcuts and the vocabulary editor, then audio,
   output, and model controls. Preserve existing settings and glossary contents.
4. **Integrate startup.** Launch one frontend per login alongside the backend;
   prevent duplicates and support panel/service startup in either order.
5. **Verify on this MATE desktop.** Check the complete workflows and document how
   to disable or uninstall the frontend while retaining the existing hotkeys.

## Acceptance checks

- The icon appears after login and accurately follows the backend state.
- F8/F9/Shift+F8 still work while Settings is closed and while the panel is running.
- A changed shortcut works after Save; conflicting assignments leave the old
  working shortcut intact.
- Glossary edits preserve `NON`, `UP`, `DOWN`, `ABA-DL`, and multiword phrases.
- The vocabulary preview agrees with the hints actually passed to Whisper.
- Settings changes during recording wait until safe; no audio or paths are lost.
- Starting from the menu pastes into the remembered application, never Settings.
- Restarting or closing the frontend does not duplicate or interrupt the backend.
- Existing path capture still copies absolute paths without opening a preview.
- Transcripts are never sent automatically.

Use automated checks for configuration migration, hint selection, shortcut
conflicts, and deferred changes. Use desktop checks for panel visibility, focus,
microphone selection, and login startup.

## Useful later additions

- **Manual vocabulary profiles:** switch between aaabphd and a general glossary
  from the menu. Automatic VS Code workspace detection can follow separately.
- **Last-transcript review:** open the most recent result for correction and copy;
  longer history should be an explicit preference.
- **Unload model:** release memory while idle and reload on the next recording.
- **Optional sounds:** brief recording-start, recording-stop, and path-captured cues.
- **Explicit replacements:** a separate editor for user-defined corrections, with
  a preview showing what would change.

Prioritise the panel icon, shortcut editor, and vocabulary editor for the first
usable release. Profiles, longer history, and replacement rules can follow after
the core desktop workflow is proven.
