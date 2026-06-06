# Cool MP3 Player

A clean MP3 player built from the Solace codebase, with the **machine-learning and
emotion-tagging system removed**. Everything else is kept — and a **live audio
visualizer** is added.

## What it does

- **Library** — add songs, search, sort (Name / Artist / Duration), queue, delete.
- **Saved playlists** — create playlists, add songs via right-click, play them back.
- **Full-screen "Now Playing" player** — click the song title (or the `⤢` button)
  to slide it up. Has **Lyrics** and **Queue** tabs.
- **Lyrics generation** — looks lyrics up from `lrclib.net` (with a `lyrics.ovh`
  fallback), works for many languages. Click **🔍 Generate** in the player, or
  **✏ Edit** to write your own. Saved per-song.
- **Cover art** — embedded ID3/M4A artwork, or the iTunes thumbnail for the track.
- **🆕 Live visualizer** — in the full-screen player there's a **`◫ Visualizer`**
  button. It shows an immersive spectrum analyzer that reacts to the *actual*
  frequencies of the playing song (real FFT of the decoded audio). Click anywhere
  on it (or the button again) to close.

## What was removed (vs. Solace)

- No acoustic analysis / `librosa`.
- No emotion classifier, KNN learning model, or correction tracking.
- No "Mix" tab / mood-based playlist generation / feedback learning.
- No emotion tags, chips, filters, or per-song AI source labels.

## Install

```
pip install pygame-ce mutagen pillow numpy
```

(`librosa` is **not** needed for this build.)

## Run

```
python player.py
```

## Download (Windows installer)

Grab **`Cool MP3 Player Setup.exe`** from the
[Releases page](https://github.com/Watifs/cool-mp3-player/releases/latest) and run it.

- It installs per-user (no admin prompt) to
  `…\AppData\Local\Programs\Cool MP3 Player`, with Start-Menu (and optional
  desktop) shortcuts, and an entry in **Settings ▸ Apps**.
- **It auto-updates an existing install.** Run a newer installer and it detects
  the version already on the PC, closes the app if it's running, and **upgrades
  it in place** — no second copy, no manual uninstall. (It also warns before a
  downgrade, and asks before reinstalling the same version.)
- The app still checks GitHub on startup and offers to open the Releases page
  when a newer version is published — download that installer and run it to update.

On **macOS**, open the `.dmg` and drag the app to **Applications**; if a copy is
already there, Finder offers to **Replace** it (that's the macOS-native "update").

## Building it yourself

```powershell
# Windows — builds dist\windows\Cool MP3 Player.exe
pwsh packaging\windows\build.ps1

# Windows — builds the installer dist\windows\Cool MP3 Player Setup.exe
#   (needs Inno Setup 6:  winget install --id JRSoftware.InnoSetup -e)
pwsh packaging\windows\build-installer.ps1
```

```bash
# macOS — builds dist/macos/Cool MP3 Player.app + .dmg
bash packaging/macos/build.command
```

The installer's version is read automatically from `APP_VERSION` in `player.py`,
so bump that before cutting a release.

## Keyboard shortcuts

| Key | Action |
|-----|--------|
| Space | Play / pause |
| → / ← | Seek ±10 s |
| ↑ / ↓ | Volume ±5% |
| n / p | Next / previous track |
| r | Cycle repeat (off → one → all) |
| s | Toggle shuffle |
| f | Toggle full-screen Now Playing |

## Notes

- Its data (`player_library.json`, `player_playlists.json`, `player_lyrics.json`)
  is stored **in this folder** and is completely separate from Solace's data.
- The visualizer decodes the playing file's PCM via `pygame` once per track; for
  very long files this uses some memory but is cleared when the track changes.
- If `pygame` can't decode a given format for the visualizer, playback still
  works — the bars just rest at the baseline.
