# Cool MP3 Player — macOS

This guide is written so **anyone** can get the app running on a Mac, even with
no technical background. Pick the path that fits you.

---

## ⭐ Option A — "I just want to use it" (download the ready-made app)

1. Go to this project's **Releases** page on GitHub (right-hand side of the
   repo, or the "Releases" link).
2. Download the file named **`Cool MP3 Player.dmg`**.
3. Double-click the downloaded `.dmg`. A window opens showing the app.
4. **Drag the "Cool MP3 Player" icon onto the Applications folder** in that
   window.
5. Open **Applications**, then **right-click** (or Control-click) **Cool MP3
   Player → Open**. Click **Open** again in the box that appears.

> 💡 **Why right-click the first time?** The app isn't signed with a paid Apple
> developer certificate, so the very first launch macOS asks you to confirm.
> You only do this once — after that, open it normally.
>
> If macOS still says *"app is damaged / can't be opened"*, open the **Terminal**
> app and paste this line, then press Return:
> ```
> xattr -dr com.apple.quarantine "/Applications/Cool MP3 Player.app"
> ```

That's it — you're done. 🎧

---

## 🛠 Option B — "I want to build it myself" (from the source code)

You only need this if there's no ready-made `.dmg`, or you've changed the code.

### 1. Install Python 3 (one time)
- Go to <https://www.python.org/downloads/macos/>
- Click the big yellow **Download Python** button and run the installer
  (just keep clicking Continue / Agree / Install).

### 2. Build the app
- In Finder, open this project's folder, then go into
  **`packaging` → `macos`**.
- **Double-click `build.command`.**
  - A Terminal window opens and does everything automatically: it sets up an
    isolated environment, downloads what it needs, builds the app, and packages
    it into a `.dmg`. The first run takes a few minutes.
  - If double-clicking shows *"cannot be opened because it is from an
    unidentified developer"*, **right-click `build.command` → Open → Open**, or
    run this once in Terminal from the project folder:
    ```
    chmod +x "packaging/macos/build.command"
    ```

### 3. Get your files
When it finishes you'll find them here inside the project folder:
- **`dist/macos/Cool MP3 Player.app`** — the app itself
- **`dist/macos/Cool MP3 Player.dmg`** — the shareable installer

Open the `.dmg` and drag the app to Applications (see Option A, steps 4–5).

---

## What's included in this build

This is the **lean** build. These features work out of the box:
- 🎵 Playback, library, queue, full-screen Now-Playing view
- 🎤 Online lyrics lookup + **karaoke** highlighting
- 🌈 Live visualizer

**Not** included: offline *"transcribe lyrics from audio"* (it needs the large
PyTorch / Demucs / faster-whisper libraries, which would balloon the download to
several GB). Everything else is unaffected — the app simply hides that one
option. To enable it, run the player from source with those libraries installed
(see the main `README.md`).

## Where your data is stored
Your library, lyrics, and learned corrections are saved per-user at:
```
~/Library/Application Support/Cool MP3 Player/
```
Deleting the app never touches your music files.

## Troubleshooting
- **"Python 3 is not installed"** when running `build.command` → do step 1 above.
- **App won't open on first launch** → use the right-click → Open trick, or the
  `xattr` command, both shown in Option A.
- **Apple Silicon vs Intel** → the build automatically targets whichever Mac you
  build it on. A `.dmg` built on Apple Silicon runs on Apple Silicon; build on an
  Intel Mac for an Intel `.dmg` (or vice-versa).
