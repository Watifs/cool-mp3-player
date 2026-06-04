"""Best-effort "is there a newer release?" check for the packaged app.

On startup the app calls :func:`check_for_updates`, which asks GitHub for the
latest published release of the repo and, if its version tag is newer than the
version baked into this build, pops a dialog offering to open the downloads
page. It intentionally does **not** download or install anything itself — a
running PyInstaller .exe can't safely replace itself — so it just sends the user
to the Releases page to grab the new build (the "notify + open page" model).

Everything here is best-effort: the network call runs on a daemon thread and any
error (offline, rate-limited, no releases yet, parse failure) is swallowed so it
can never delay or break app startup.
"""
import json
import threading
import urllib.request
import webbrowser

GITHUB_API    = "https://api.github.com/repos/{repo}/releases/latest"
RELEASES_PAGE = "https://github.com/{repo}/releases/latest"


def _parse(v):
    """'v1.2.3' / '1.2' -> (1, 2, 3) / (1, 2). Non-digits in a part are dropped."""
    nums = []
    for part in str(v).strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        nums.append(int(digits) if digits else 0)
    return tuple(nums)


def _is_newer(latest, current):
    a, b = _parse(latest), _parse(current)
    n = max(len(a), len(b))
    a += (0,) * (n - len(a))
    b += (0,) * (n - len(b))
    return a > b


def _fetch_latest_tag(repo, timeout=6):
    req = urllib.request.Request(
        GITHUB_API.format(repo=repo),
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "update-check"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    return data.get("tag_name") or data.get("name")


def _worker(root, repo, current_version, app_name):
    try:
        latest = _fetch_latest_tag(repo)
        if not latest or not _is_newer(latest, current_version):
            return
    except Exception:
        return  # offline / rate-limited / no releases yet — stay silent

    def prompt():
        # Imported here so a headless/odd environment can't break import-time.
        from tkinter import messagebox
        if messagebox.askyesno(
            f"{app_name} — update available",
            f"A newer version ({latest}) is available.\n"
            f"You're running {current_version}.\n\n"
            "Open the download page now?",
        ):
            webbrowser.open(RELEASES_PAGE.format(repo=repo))

    try:
        root.after(0, prompt)   # marshal back onto the Tk main thread
    except Exception:
        pass


def check_for_updates(root, repo, current_version, app_name="App"):
    """Kick off a background update check. Safe to call once at startup."""
    threading.Thread(
        target=_worker,
        args=(root, repo, current_version, app_name),
        daemon=True,
    ).start()
