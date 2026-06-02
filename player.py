# -*- coding: utf-8 -*-
"""
Cool MP3 Player
================================================
A clean MP3 player built from the Solace codebase, with the machine-learning
and emotion-tagging system stripped out. Everything else is kept:

  • Library — add songs, search, sort, queue, delete
  • Saved playlists
  • Full-screen "Now Playing" player (lyrics + up-next queue)
  • Lyrics generation  — lrclib.net (primary) + lyrics.ovh (fallback)
  • Cover art           — embedded ID3 art or iTunes API thumbnail
  • NEW: live audio VISUALIZER — a real-FFT spectrum that reacts to the song,
    toggled from a button in the full-screen player.

Install:
    pip install pygame-ce mutagen pillow numpy

(librosa is NOT required — this build does no acoustic analysis.)
"""

import os, re, io, json, time, uuid, queue, ctypes, random, threading, datetime
import urllib.request, urllib.parse
from pathlib import Path
from tkinter import *
from tkinter import ttk, filedialog, messagebox, simpledialog
import numpy as np

# ── DPI awareness ─────────────────────────────────────────────────────────────
try:    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    try: ctypes.windll.user32.SetProcessDPIAware()
    except Exception: pass

# ── Optional audio libraries ──────────────────────────────────────────────────
try:
    import pygame
    pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)
    _PYGAME = True
except Exception:
    pygame = None; _PYGAME = False

try:
    from mutagen import File as _MutagenFile; _MUTAGEN = True
except Exception:
    _MutagenFile = None; _MUTAGEN = False

try:
    from PIL import Image, ImageTk; _PIL = True
except ImportError:
    Image = ImageTk = None; _PIL = False

# ── Data files (kept inside THIS folder — separate from Solace) ────────────────
_DIR           = Path(__file__).parent
LIBRARY_FILE   = _DIR / "player_library.json"
PLAYLISTS_FILE = _DIR / "player_playlists.json"
LYRICS_FILE    = _DIR / "player_lyrics.json"

MIXER_SR = 44100   # device sample rate (matches pygame.mixer.init above)


# ==============================================================================
#  PALETTE
# ==============================================================================
BG        = "#0d0d0d"
BG2       = "#111111"
BG3       = "#191919"
BG4       = "#161616"
ACCENT    = "#1D9E75"
ACCENT_DK = "#14704F"
ACCENT_HI = "#5CE8B0"   # bright accent — used for visualizer peaks
TXT       = "#e8e8e8"
TXT_MID   = "#909090"
TXT_DIM   = "#484848"
BORDER    = "#252525"
WHITE     = "#ffffff"
FF        = "Segoe UI"


# ==============================================================================
#  METADATA HELPERS
# ==============================================================================
def _parse_artist_title(name: str) -> tuple:
    """Parse 'Artist - Title' from a filename string. Returns (artist, title)."""
    clean = re.sub(r'^\s*[\[\(][^\]\)]{1,30}[\]\)]\s*[-–]\s*', '', name)
    clean = re.sub(r'\s*[\[\(](?:official|lyrics?|audio|video|mv|hd|4k|full)[^\]\)]*[\]\)]\s*$',
                   '', clean, flags=re.I).strip()
    if ' - ' in clean:
        idx = clean.index(' - ')
        return clean[:idx].strip(), clean[idx+3:].strip()
    if ' – ' in clean:
        idx = clean.index(' – ')
        return clean[:idx].strip(), clean[idx+3:].strip()
    return '', name.strip()


def get_meta(path: str) -> dict:
    meta = {"duration": 0.0, "title": "", "artist": ""}
    if _MUTAGEN:
        try:
            f = _MutagenFile(path)
            if f:
                if hasattr(f.info, "length"):
                    meta["duration"] = float(f.info.length)
                tags = f.tags or {}
                for key in ("TIT2", "©nam", "title"):
                    if key in tags:
                        meta["title"] = str(tags[key][0] if isinstance(tags[key], list) else tags[key])
                        break
                for key in ("TPE1", "©ART", "artist"):
                    if key in tags:
                        meta["artist"] = str(tags[key][0] if isinstance(tags[key], list) else tags[key])
                        break
        except Exception:
            pass
    return meta


def get_duration_fast(path: str) -> float:
    """Return duration in seconds using a fast header-only read (no decode)."""
    if _MUTAGEN:
        try:
            f = _MutagenFile(path)
            if f and hasattr(f.info, "length") and f.info.length > 0:
                return float(f.info.length)
        except Exception:
            pass
    return 0.0


def get_embedded_art_data(path: str) -> bytes:
    """Return raw bytes of embedded album art, or b'' if none."""
    if not _MUTAGEN: return b''
    try:
        f = _MutagenFile(path)
        if f and f.tags:
            for key in list(f.tags.keys()):
                if key.startswith("APIC"):
                    return f.tags[key].data
            if "covr" in f.tags:          # M4A / AAC
                covers = f.tags["covr"]
                if covers:
                    return bytes(covers[0])
    except Exception:
        pass
    return b''


def internet_cover(title: str, artist: str = "") -> str:
    """Return a cover-art URL from the iTunes Search API, or '' on failure."""
    try:
        term = f"{artist} {title}".strip() if artist else title
        q    = urllib.parse.quote(term)
        url  = (f"https://itunes.apple.com/search"
                f"?term={q}&media=music&entity=song&limit=5")
        req  = urllib.request.Request(url,
                   headers={"User-Agent": "CoolMP3/1.0 (music player)"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        for result in data.get("results", []):
            art = result.get("artworkUrl100", "")
            if art:
                return art.replace("100x100bb", "600x600bb")
    except Exception:
        pass
    return ""


def _fmt_dur(sec):
    if not sec or sec <= 0: return "--:--"
    m, s = divmod(int(sec), 60); return f"{m}:{s:02d}"


# ==============================================================================
#  LYRICS  (generation/lookup — lrclib primary, lyrics.ovh fallback)
# ==============================================================================
def _clean_for_lyrics(s: str) -> str:
    """Strip feat./remaster/official-video noise that breaks lyric matching.
    Keeps non-ASCII characters intact so CJK / Cyrillic / etc. titles survive."""
    if not s: return ""
    s = re.sub(r'\s*[\(\[](?:feat|ft|featuring|prod|with|official|lyrics?|audio|'
               r'video|m/?v|hd|4k|full|remaster(?:ed)?|remix|live|cover|'
               r'visualizer|color\s*coded)[^\)\]]*[\)\]]', '', s, flags=re.I)
    s = re.sub(r'\s+', ' ', s)
    return s.strip()


def fetch_lyrics(title: str, artist: str = "") -> str:
    """Fetch plain lyrics for a song in ANY language.

    Order:
      1. lrclib /api/get    — exact artist+title (fast path)
      2. lrclib /api/search — fuzzy ranked search (best for non-English /
         romanised / loosely-tagged titles)
      3. lyrics.ovh         — last-resort fallback
    """
    title  = (title or "").strip()
    artist = (artist or "").strip()
    if not title: return ""
    ct, ca = _clean_for_lyrics(title), _clean_for_lyrics(artist)
    hdr = {"User-Agent": "CoolMP3/1.0 (https://example.com/coolmp3)"}

    def _get_json(url):
        req = urllib.request.Request(url, headers=hdr)
        with urllib.request.urlopen(req, timeout=9) as r:
            return json.loads(r.read())

    # 1. lrclib exact get — try cleaned, then raw tags
    for a, t in [(ca, ct), (artist, title)]:
        if not t: continue
        try:
            params = {"track_name": t}
            if a: params["artist_name"] = a
            data = _get_json("https://lrclib.net/api/get?" +
                             urllib.parse.urlencode(params))
            lyr = (data.get("plainLyrics") or "").strip()
            if len(lyr) > 20:
                return lyr
        except Exception:
            pass

    # 2. lrclib fuzzy search — handles other languages & messy titles
    queries = []
    if ca and ct: queries.append(f"{ct} {ca}")
    queries.append(ct)
    if title != ct: queries.append(title)
    seen_q = set()
    for q in queries:
        q = q.strip()
        if not q or q.lower() in seen_q: continue
        seen_q.add(q.lower())
        try:
            results = _get_json("https://lrclib.net/api/search?q=" +
                                urllib.parse.quote(q))
            if not isinstance(results, list): continue
            for res in results:
                lyr = (res.get("plainLyrics") or "").strip()
                if len(lyr) > 20:
                    return lyr
        except Exception:
            pass

    # 3. lyrics.ovh fallback
    if ca and ct:
        try:
            url = (f"https://api.lyrics.ovh/v1/"
                   f"{urllib.parse.quote(ca, safe='')}/"
                   f"{urllib.parse.quote(ct, safe='')}")
            lyr = (_get_json(url).get("lyrics") or "").strip()
            if len(lyr) > 20:
                return lyr
        except Exception:
            pass
    return ""


def load_lyrics_db() -> dict:
    if not LYRICS_FILE.exists(): return {}
    try:
        with open(LYRICS_FILE, encoding="utf-8") as f: return json.load(f)
    except Exception: return {}


def save_lyrics_db(db: dict):
    try:
        with open(LYRICS_FILE, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
    except Exception: pass


# ==============================================================================
#  SONG DATA CLASS
# ==============================================================================
class Song:
    __slots__ = ("path", "name", "title", "artist", "duration",
                 "busy", "failed", "cover_url")

    def __init__(self, path: str):
        self.path     = path
        meta          = get_meta(path)
        self.name     = Path(path).stem
        self.title    = meta["title"] or ""
        self.artist   = meta["artist"] or ""
        if not self.artist or not self.title:
            pa, pt = _parse_artist_title(self.name)
            if pa and not self.artist: self.artist = pa
            if pt and not self.title:  self.title  = pt
        if not self.title: self.title = self.name
        self.duration = meta["duration"]
        self.busy     = False
        self.failed   = False
        self.cover_url = ""

    def to_dict(self) -> dict:
        return {"path": self.path, "name": self.name, "title": self.title,
                "artist": self.artist, "duration": self.duration,
                "cover_url": self.cover_url}

    @staticmethod
    def from_dict(d: dict) -> "Song":
        s = Song.__new__(Song)
        s.path   = d["path"]
        s.name   = d.get("name", Path(d["path"]).stem)
        s.title  = d.get("title", s.name)
        s.artist = d.get("artist", "")
        if not s.artist or s.title == s.name:
            pa, pt = _parse_artist_title(s.name)
            if pa and not s.artist:       s.artist = pa
            if pt and s.title == s.name:  s.title  = pt
        s.duration  = d.get("duration", 0.0)
        s.busy      = False
        s.failed    = False
        s.cover_url = d.get("cover_url", "")
        return s


# ==============================================================================
#  PERSISTENCE
# ==============================================================================
def save_library(songs):
    try:
        data = {"songs": [s.to_dict() for s in songs if not s.failed]}
        with open(LIBRARY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception: pass


def load_library():
    if not LIBRARY_FILE.exists(): return []
    try:
        with open(LIBRARY_FILE, encoding="utf-8") as f: data = json.load(f)
        return [Song.from_dict(d) for d in data.get("songs", [])
                if os.path.exists(d.get("path", ""))]
    except Exception: return []


def save_playlists(pls):
    try:
        with open(PLAYLISTS_FILE, "w", encoding="utf-8") as f:
            json.dump(pls, f, ensure_ascii=False, indent=2)
    except Exception: pass


def load_playlists():
    if not PLAYLISTS_FILE.exists(): return []
    try:
        with open(PLAYLISTS_FILE, encoding="utf-8") as f: return json.load(f)
    except Exception: return []


# ==============================================================================
#  AUDIO SPECTRUM  (real-FFT data source for the visualizer)
# ==============================================================================
class AudioSpectrum:
    """Decodes the playing file's PCM samples (via pygame) once, then serves
    log-spaced FFT magnitude bands for whatever playback position is asked for.
    Reacts to the actual frequency content of the song — no fakery."""

    def __init__(self):
        self._lock   = threading.Lock()
        self._samples = None     # mono float32, peak-normalised
        self._token   = 0        # guards against a stale background load

    def load(self, path: str):
        """Kick off a background decode of `path` into a mono sample buffer."""
        if not _PYGAME:
            return
        self._token += 1
        token = self._token
        with self._lock:
            self._samples = None

        def _work():
            try:
                snd = pygame.mixer.Sound(path)
                arr = pygame.sndarray.array(snd)          # int16, (N,) or (N,2)
                if arr.ndim == 2:
                    arr = arr.mean(axis=1)
                arr = arr.astype(np.float32)
                peak = float(np.max(np.abs(arr))) or 1.0
                arr /= peak
                if token == self._token:                  # still the current song?
                    with self._lock:
                        self._samples = arr
            except Exception:
                pass

        threading.Thread(target=_work, daemon=True).start()

    def clear(self):
        self._token += 1
        with self._lock:
            self._samples = None

    def bands(self, t_sec: float, n_bands: int = 64, window: int = 2048):
        """Return an array of `n_bands` magnitudes (0..~1) for the audio around
        position `t_sec`, or None if samples aren't ready yet."""
        with self._lock:
            s = self._samples
        if s is None or len(s) == 0:
            return None
        center = int(t_sec * MIXER_SR)
        start  = max(0, center - window // 2)
        end    = start + window
        if end > len(s):
            end = len(s); start = max(0, end - window)
        chunk = s[start:end]
        if len(chunk) < window:
            chunk = np.pad(chunk, (0, window - len(chunk)))
        chunk = chunk * np.hanning(len(chunk))
        spec  = np.abs(np.fft.rfft(chunk))
        freqs = np.fft.rfftfreq(window, 1.0 / MIXER_SR)

        edges = np.logspace(np.log10(30), np.log10(16000), n_bands + 1)
        out   = np.zeros(n_bands, dtype=np.float32)
        for i in range(n_bands):
            mask = (freqs >= edges[i]) & (freqs < edges[i + 1])
            if mask.any():
                out[i] = float(spec[mask].mean())
        # Log-compress so quiet detail is visible, then normalise this frame.
        out = np.log1p(out * 6.0)
        m   = float(out.max()) or 1.0
        return out / m


def _lerp_color(c1: str, c2: str, t: float) -> str:
    t = max(0.0, min(1.0, t))
    r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
    r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
    return (f"#{int(r1+(r2-r1)*t):02x}"
            f"{int(g1+(g2-g1)*t):02x}"
            f"{int(b1+(b2-b1)*t):02x}")


# ==============================================================================
#  NOW-PLAYING OVERLAY  (slide-up panel — lyrics, queue, visualizer)
# ==============================================================================
class NowPlayingOverlay(Frame):
    def __init__(self, app: "PlayerApp"):
        super().__init__(app.root, bg=BG)
        self.app             = app
        self._visible        = False
        self._anim_id        = None
        self._cover_img      = None
        self._cover_cache: dict = {}
        self._last_song      = None
        self._lyrics_editing = False
        # Visualizer state
        self._vis_on    = False
        self._vis_cv    = None
        self._vis_bars  = []
        self._vis_levels = None
        self._vis_anim_id = None
        self._vis_n     = 64
        self._build_ui()

    # ── Slide animation ─────────────────────────────────────────────────────
    def show(self):
        if self._visible: return
        self._visible = True
        rh = self.app.root.winfo_height()
        rw = self.app.root.winfo_width()
        self.place(x=0, y=rh, width=rw, height=rh)
        self.lift()
        self._anim_step(rh, going_down=False)
        self._tick()

    def hide(self):
        if not self._visible: return
        if self._vis_on:            # close visualizer first
            self._toggle_visualizer()
        try:   cur_y = self.winfo_y()
        except Exception: cur_y = 0
        self._anim_step(cur_y, going_down=True, done=self._finish_hide)

    def _finish_hide(self):
        self.place_forget()
        self._visible = False

    def _anim_step(self, cur_y, going_down: bool, done=None):
        if self._anim_id:
            self.app.root.after_cancel(self._anim_id); self._anim_id = None
        rh = self.app.root.winfo_height()
        rw = self.app.root.winfo_width()
        target_y = rh if going_down else 0
        dist = abs(target_y - cur_y)
        step = max(28, dist // 5)
        if dist <= step:
            self.place(x=0, y=target_y, width=rw, height=rh)
            if done: done()
            return
        new_y = cur_y + (step if going_down else -step)
        self.place(x=0, y=new_y, width=rw, height=rh)
        self._anim_id = self.app.root.after(
            16, lambda: self._anim_step(new_y, going_down, done))

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        hdr = Frame(self, bg="#0a0a0a", pady=10, padx=20)
        hdr.pack(fill=X)
        close_lbl = Label(hdr, text="⌄  Now Playing", font=(FF, 11, "bold"),
                          bg="#0a0a0a", fg=TXT_DIM, cursor="hand2")
        close_lbl.pack(side=LEFT)
        close_lbl.bind("<Button-1>", lambda _: self.hide())
        # Visualizer toggle — the requested full-screen button
        self._vis_btn = Button(hdr, text="◫  Visualizer", font=(FF, 10, "bold"),
                               bg=BG3, fg=ACCENT, relief=FLAT, cursor="hand2",
                               padx=12, pady=4, activebackground=ACCENT,
                               activeforeground=WHITE,
                               command=self._toggle_visualizer)
        self._vis_btn.pack(side=RIGHT)
        tip = Label(hdr, text="click title to close", font=(FF, 8),
                    bg="#0a0a0a", fg=TXT_DIM)
        tip.pack(side=RIGHT, padx=10)
        Frame(self, bg=BORDER, height=1).pack(fill=X)

        content = Frame(self, bg=BG)
        content.pack(fill=BOTH, expand=True)

        # Left: cover art
        left = Frame(content, bg=BG, width=320)
        left.pack(side=LEFT, fill=Y)
        left.pack_propagate(False)
        self._cover_lbl = Label(left, bg="#0a0a0a", text="♪",
                                font=(FF, 60), fg="#2a2a2a")
        self._cover_lbl.pack(fill=BOTH, expand=True, padx=20, pady=20)

        # Right: info + controls + tabs
        right = Frame(content, bg=BG)
        right.pack(side=LEFT, fill=BOTH, expand=True)

        info = Frame(right, bg=BG, pady=16, padx=18)
        info.pack(fill=X)
        self._now_name2 = StringVar(value="Nothing playing")
        self._now_art2  = StringVar(value="")
        Label(info, textvariable=self._now_name2, font=(FF, 17, "bold"),
              bg=BG, fg=TXT, anchor=W, wraplength=520).pack(fill=X)
        Label(info, textvariable=self._now_art2, font=(FF, 11),
              bg=BG, fg=TXT_MID, anchor=W).pack(fill=X, pady=(2, 0))

        ctl = Frame(right, bg=BG, pady=8, padx=18)
        ctl.pack(fill=X)
        self._fs_shuf_btn = Button(ctl, text="🔀", font=(FF, 14), bg=BG, fg=TXT_DIM,
               relief=FLAT, cursor="hand2", padx=8,
               activebackground=BG3, activeforeground=TXT,
               command=self.app._toggle_shuffle)
        self._fs_shuf_btn.pack(side=LEFT)
        Button(ctl, text="⏮", font=(FF, 20), bg=BG, fg=TXT_MID,
               relief=FLAT, cursor="hand2", padx=10,
               activebackground=BG3, activeforeground=TXT,
               command=self.app._prev_song).pack(side=LEFT)
        self._fs_play_btn = Button(ctl, text="▶", font=(FF, 28, "bold"),
               bg=BG, fg=ACCENT, relief=FLAT, cursor="hand2", padx=14,
               activebackground=BG3, activeforeground=ACCENT,
               command=self.app._toggle_play)
        self._fs_play_btn.pack(side=LEFT)
        Button(ctl, text="⏭", font=(FF, 20), bg=BG, fg=TXT_MID,
               relief=FLAT, cursor="hand2", padx=10,
               activebackground=BG3, activeforeground=TXT,
               command=self.app._next_song).pack(side=LEFT)
        self._fs_rep_btn = Button(ctl, text="🔁", font=(FF, 14), bg=BG, fg=TXT_DIM,
               relief=FLAT, cursor="hand2", padx=8,
               activebackground=BG3, activeforeground=TXT,
               command=self.app._toggle_repeat)
        self._fs_rep_btn.pack(side=LEFT)

        vol_f = Frame(right, bg=BG, padx=18, pady=4)
        vol_f.pack(fill=X)
        Label(vol_f, text="🔇", font=(FF, 10), bg=BG, fg=TXT_DIM).pack(side=LEFT)
        Scale(vol_f, variable=self.app._vol_var, from_=0.0, to=1.0,
              resolution=0.01, orient=HORIZONTAL, length=160,
              bg=BG, fg=TXT_DIM, troughcolor=BORDER,
              highlightthickness=0, showvalue=False, sliderlength=14,
              command=self.app._on_volume).pack(side=LEFT, padx=6)
        Label(vol_f, text="🔊", font=(FF, 10), bg=BG, fg=TXT_DIM).pack(side=LEFT)

        Frame(right, bg=BORDER, height=1).pack(fill=X, padx=8)

        prog = Frame(right, bg=BG4, pady=10, padx=18)
        prog.pack(fill=X)
        self._build_fs_progress(prog)

        Frame(right, bg=BORDER, height=1).pack(fill=X)

        tab_bar = Frame(right, bg=BG2)
        tab_bar.pack(fill=X)
        self._fstab_btns = {}
        for t in ("Lyrics", "Queue"):
            b = Button(tab_bar, text=t, font=(FF, 10, "bold"),
                       bg=BG2, fg=TXT_DIM, relief=FLAT, cursor="hand2",
                       padx=18, pady=8, bd=0,
                       activebackground=BG3, activeforeground=TXT,
                       command=lambda x=t: self._switch(x))
            b.pack(side=LEFT)
            self._fstab_btns[t] = b
        Frame(right, bg=BORDER, height=1).pack(fill=X)

        self._fs_content = Frame(right, bg=BG)
        self._fs_content.pack(fill=BOTH, expand=True)
        self._build_lyrics_panel()
        self._build_queue_panel()
        self._switch("Lyrics")

    def _build_lyrics_panel(self):
        f = Frame(self._fs_content, bg=BG)
        self._lyr_frame = f

        bar = Frame(f, bg=BG, pady=8, padx=14)
        bar.pack(fill=X)
        Label(bar, text="Lyrics", font=(FF, 12, "bold"), bg=BG, fg=TXT).pack(side=LEFT)
        self._lyr_src_lbl = Label(bar, text="", font=(FF, 9), bg=BG, fg=TXT_DIM)
        self._lyr_src_lbl.pack(side=LEFT, padx=10)
        self._lyr_save_btn = Button(bar, text="Save", font=(FF, 9, "bold"),
                                    bg=ACCENT, fg=WHITE, relief=FLAT, cursor="hand2",
                                    padx=10, pady=4, state=DISABLED,
                                    activebackground=ACCENT_DK,
                                    command=self._save_lyrics)
        self._lyr_save_btn.pack(side=RIGHT, padx=(4, 0))
        self._lyr_edit_btn = Button(bar, text="✏ Edit", font=(FF, 9),
                                    bg=BG4, fg=TXT_MID, relief=FLAT,
                                    cursor="hand2", padx=10, pady=4,
                                    activebackground=BG3, activeforeground=TXT,
                                    command=self._toggle_edit)
        self._lyr_edit_btn.pack(side=RIGHT, padx=(0, 4))
        self._lyr_fetch_btn = Button(bar, text="🔍 Generate", font=(FF, 9),
                                     bg=BG4, fg=TXT_MID, relief=FLAT,
                                     cursor="hand2", padx=10, pady=4,
                                     activebackground=BG3, activeforeground=TXT,
                                     command=self._fetch_lyrics_now)
        self._lyr_fetch_btn.pack(side=RIGHT, padx=(0, 4))

        wrap = Frame(f, bg=BG)
        wrap.pack(fill=BOTH, expand=True, padx=14, pady=(0, 12))
        sb = ttk.Scrollbar(wrap, orient=VERTICAL)
        self._lyr_txt = Text(wrap, font=(FF, 11), bg=BG3, fg=TXT,
                             insertbackground=TXT, relief=FLAT,
                             wrap=WORD, state=DISABLED,
                             highlightthickness=0, padx=16, pady=12,
                             yscrollcommand=sb.set)
        sb.config(command=self._lyr_txt.yview)
        sb.pack(side=RIGHT, fill=Y)
        self._lyr_txt.pack(side=LEFT, fill=BOTH, expand=True)

    def _build_queue_panel(self):
        f = Frame(self._fs_content, bg=BG)
        self._queue_frame = f
        Label(f, text="Up Next", font=(FF, 12, "bold"),
              bg=BG, fg=TXT, anchor=W, padx=14, pady=10).pack(fill=X)
        Frame(f, bg=BORDER, height=1).pack(fill=X)
        wrap = Frame(f, bg=BG); wrap.pack(fill=BOTH, expand=True)
        self._q_cv = Canvas(wrap, bg=BG, bd=0, highlightthickness=0)
        sb = ttk.Scrollbar(wrap, orient=VERTICAL, command=self._q_cv.yview)
        self._q_fr = Frame(self._q_cv, bg=BG)
        self._q_cv.configure(yscrollcommand=sb.set)
        sb.pack(side=RIGHT, fill=Y)
        self._q_cv.pack(side=LEFT, fill=BOTH, expand=True)
        qwin = self._q_cv.create_window((0, 0), window=self._q_fr, anchor=NW)
        self._q_fr.bind("<Configure>",
                        lambda e: self._q_cv.configure(scrollregion=self._q_cv.bbox("all")))
        self._q_cv.bind("<Configure>",
                        lambda e: self._q_cv.itemconfig(qwin, width=e.width))
        self._q_win = qwin
        self.app._scroll_canvases.add(self._q_cv)
        self._render_queue()

    def _render_queue(self):
        for w in self._q_fr.winfo_children(): w.destroy()
        app = self.app
        manual = list(app._next_up)
        playlist_ahead = []
        if app._queue and 0 <= app._q_idx < len(app._queue):
            playlist_ahead = app._queue[app._q_idx + 1:]
        all_ahead = manual + playlist_ahead
        if not all_ahead:
            Label(self._q_fr, text="Queue is empty.\n\nRight-click a song → Add to Queue",
                  font=(FF, 10), bg=BG, fg=TXT_DIM, justify=CENTER, pady=24).pack()
            return
        n_manual = len(manual)
        shown = all_ahead[:20]
        for i, song in enumerate(shown):
            is_manual = i < n_manual
            row_bg = "#0f1a15" if is_manual else BG
            if is_manual: jump_idx = i
            else:         jump_idx = app._q_idx + 1 + (i - n_manual)
            clickable = []
            row = Frame(self._q_fr, bg=row_bg, pady=6, cursor="hand2"); row.pack(fill=X)
            clickable.append(row)
            if is_manual:
                lead = Label(row, text="▸", font=(FF, 9), bg=row_bg,
                             fg=ACCENT, cursor="hand2"); lead.pack(side=LEFT, padx=(8, 4))
            else:
                lead = Label(row, text=f"{i+1-n_manual}", font=(FF, 9), bg=row_bg,
                             fg=TXT_DIM, width=3, anchor=E, cursor="hand2")
                lead.pack(side=LEFT, padx=(8, 4))
            clickable.append(lead)
            Label(row, text="♪", font=(FF, 9), bg=row_bg, fg=ACCENT
                  ).pack(side=RIGHT, padx=(0, 10))
            Label(row, text=_fmt_dur(song.duration), font=(FF, 9), bg=row_bg,
                  fg=TXT_DIM).pack(side=RIGHT, padx=(0, 6))
            if is_manual:
                ri = i
                Button(row, text="✕", font=(FF, 8), bg=row_bg, fg="#EF5350",
                       relief=FLAT, cursor="hand2", padx=4, pady=1,
                       activebackground=row_bg, activeforeground="#EF5350",
                       command=lambda x=ri: self._q_remove(x)
                       ).pack(side=RIGHT, padx=(0, 2))
                if ri < n_manual - 1:
                    Button(row, text="↓", font=(FF, 9), bg=row_bg, fg=TXT_MID,
                           relief=FLAT, cursor="hand2", padx=4, pady=1,
                           activebackground=row_bg, activeforeground=TXT,
                           command=lambda x=ri: self._q_down(x)
                           ).pack(side=RIGHT, padx=(0, 2))
                if ri > 0:
                    Button(row, text="↑", font=(FF, 9), bg=row_bg, fg=TXT_MID,
                           relief=FLAT, cursor="hand2", padx=4, pady=1,
                           activebackground=row_bg, activeforeground=TXT,
                           command=lambda x=ri: self._q_up(x)
                           ).pack(side=RIGHT, padx=(0, 2))
            info = Frame(row, bg=row_bg, cursor="hand2")
            info.pack(side=LEFT, fill=X, expand=True)
            clickable.append(info)
            disp = song.title or song.name
            name_lbl = Label(info, text=(disp[:42] + "…") if len(disp) > 42 else disp,
                             font=(FF, 10, "bold"), bg=row_bg, fg=TXT,
                             anchor=W, cursor="hand2")
            name_lbl.pack(fill=X); clickable.append(name_lbl)
            if song.artist:
                art_lbl = Label(info, text=song.artist, font=(FF, 8), bg=row_bg,
                                fg=TXT_DIM, anchor=W, cursor="hand2")
                art_lbl.pack(fill=X); clickable.append(art_lbl)
            for w in clickable:
                w.bind("<Button-1>",
                       lambda e, s=song, m=is_manual, j=jump_idx: self._q_jump(s, m, j))
            Frame(self._q_fr, bg=BORDER, height=1).pack(fill=X)
        if len(all_ahead) > len(shown):
            Label(self._q_fr, text=f"  + {len(all_ahead)-len(shown)} more…",
                  font=(FF, 9), bg=BG, fg=TXT_DIM, anchor=W, pady=6).pack(fill=X)
        self._q_cv.update_idletasks()
        self._q_cv.configure(scrollregion=self._q_cv.bbox("all"))

    def _q_jump(self, song, is_manual: bool, idx: int):
        app = self.app
        if is_manual:
            if 0 <= idx < len(app._next_up):
                s = app._next_up.pop(idx)
                app._play_queued_song(s)
        else:
            if 0 <= idx < len(app._queue):
                app._q_idx = idx
                app._play_current()
        self._render_queue()

    def _q_remove(self, idx: int):
        if 0 <= idx < len(self.app._next_up):
            self.app._next_up.pop(idx); self._render_queue()

    def _q_up(self, idx: int):
        nu = self.app._next_up
        if 0 < idx < len(nu):
            nu[idx-1], nu[idx] = nu[idx], nu[idx-1]; self._render_queue()

    def _q_down(self, idx: int):
        nu = self.app._next_up
        if 0 <= idx < len(nu)-1:
            nu[idx], nu[idx+1] = nu[idx+1], nu[idx]; self._render_queue()

    def _build_fs_progress(self, parent):
        Label(parent, textvariable=self.app._time_cur,
              font=(FF, 9), bg=BG4, fg=TXT_MID, width=5).pack(side=LEFT)
        cv = Canvas(parent, height=14, bg="#2a2a2a", highlightthickness=0, cursor="hand2")
        cv.pack(side=LEFT, fill=X, expand=True, padx=8)
        fill = cv.create_rectangle(0, 0, 0, 14, fill=ACCENT, width=0)
        dot  = cv.create_oval(-7, -3, 7, 17, fill=WHITE, outline="", state=HIDDEN)
        cv.bind("<Button-1>",  lambda e: self._fs_seek(e, cv, fill, dot))
        cv.bind("<B1-Motion>", lambda e: self._fs_seek(e, cv, fill, dot))
        cv.bind("<Enter>",  lambda e: cv.itemconfig(dot, state=NORMAL))
        cv.bind("<Leave>",  lambda e: cv.itemconfig(dot, state=HIDDEN))
        self._fs_cv   = cv
        self._fs_fill = fill
        self._fs_dot  = dot
        Label(parent, textvariable=self.app._time_tot,
              font=(FF, 9), bg=BG4, fg=TXT_MID, width=5).pack(side=LEFT)

    # ── Tab switching ──────────────────────────────────────────────────────────
    def _switch(self, tab):
        self._lyr_frame.pack_forget()
        self._queue_frame.pack_forget()
        if tab == "Lyrics":
            self._lyr_frame.pack(fill=BOTH, expand=True)
        else:
            self._render_queue()
            self._queue_frame.pack(fill=BOTH, expand=True)
        for t, b in self._fstab_btns.items():
            b.config(fg=ACCENT if t == tab else TXT_DIM,
                     bg=BG3   if t == tab else BG2)

    def _current_song(self):
        app = self.app
        if app._queue and 0 <= app._q_idx < len(app._queue):
            return app._queue[app._q_idx]
        return None

    # ── Lyrics editing ─────────────────────────────────────────────────────────
    def _toggle_edit(self):
        self._lyrics_editing = not self._lyrics_editing
        self._lyr_txt.config(state=NORMAL if self._lyrics_editing else DISABLED)
        self._lyr_edit_btn.config(
            text="Cancel" if self._lyrics_editing else "✏ Edit",
            fg="#EF5350"  if self._lyrics_editing else TXT_MID)
        self._lyr_save_btn.config(state=NORMAL if self._lyrics_editing else DISABLED,
                                  bg=ACCENT if self._lyrics_editing else BG4)

    def _save_lyrics(self):
        song = self._current_song()
        if not song: return
        text = self._lyr_txt.get("1.0", END).strip()
        self.app.lyrics_db[song.path] = {"lyrics": text, "source": "user"}
        save_lyrics_db(self.app.lyrics_db)
        self.app._uq.put(song)
        self._lyrics_editing = False
        self._lyr_txt.config(state=DISABLED)
        self._lyr_edit_btn.config(text="✏ Edit", fg=TXT_MID)
        self._lyr_save_btn.config(state=DISABLED, bg=BG4)
        self.app._status.set(f"✏ Lyrics saved for '{song.name}'")

    def _set_lyrics_text(self, text: str):
        self._lyr_txt.config(state=NORMAL)
        self._lyr_txt.delete("1.0", END)
        placeholder = ("No lyrics yet.\n\n"
                       "Click  🔍 Generate  to look them up (works for other\n"
                       "languages too), or  ✏ Edit  to add them manually.")
        self._lyr_txt.insert("1.0", text if text else placeholder)
        if not self._lyrics_editing:
            self._lyr_txt.config(state=DISABLED)

    def _fetch_lyrics_now(self):
        song = self._current_song()
        if not song:
            self.app._status.set("Nothing playing — start a song first"); return
        if self._lyrics_editing:
            return
        self._lyr_fetch_btn.config(text="… searching", state=DISABLED)
        self.app._status.set(f"🔍 Generating lyrics for '{song.title or song.name}'…")
        threading.Thread(target=self._fetch_lyrics_bg, args=(song,),
                         daemon=True).start()

    def _fetch_lyrics_bg(self, song):
        lyrics = fetch_lyrics(song.title or song.name, song.artist)
        self.app.root.after(0, lambda: self._fetch_lyrics_done(song, lyrics))

    def _fetch_lyrics_done(self, song, lyrics):
        try: self._lyr_fetch_btn.config(text="🔍 Generate", state=NORMAL)
        except Exception: pass
        if lyrics:
            self.app.lyrics_db[song.path] = {"lyrics": lyrics, "source": "auto"}
            save_lyrics_db(self.app.lyrics_db)
            self.app._uq.put(song)
            if song is self._current_song() and not self._lyrics_editing:
                self._set_lyrics_text(lyrics)
            self.app._status.set(f"♪ Lyrics found for '{song.title or song.name}'")
        else:
            self.app._status.set(
                f"No lyrics found for '{song.title or song.name}'  ·  "
                f"try  ✏ Edit  to add them manually")

    # ── Seeking ────────────────────────────────────────────────────────────────
    def _fs_seek(self, event, cv, fill, dot):
        app = self.app
        if not _PYGAME: return
        dur = app._song_dur
        if dur <= 0: return
        w = cv.winfo_width()
        if w <= 1: return
        pct = max(0.0, min(1.0, event.x / w))
        app._do_seek(pct * dur)
        cv.coords(fill, 0, 0, int(w*pct), 14)
        cv.coords(dot, int(w*pct)-7, -3, int(w*pct)+7, 17)

    # ── Visualizer ──────────────────────────────────────────────────────────────
    def _toggle_visualizer(self):
        if self._vis_on:
            self._vis_on = False
            if self._vis_anim_id:
                try: self.app.root.after_cancel(self._vis_anim_id)
                except Exception: pass
                self._vis_anim_id = None
            if self._vis_cv:
                self._vis_cv.place_forget()
            self._vis_btn.config(text="◫  Visualizer", bg=BG3, fg=ACCENT)
            return

        if not _PYGAME:
            self.app._status.set("Visualizer needs pygame — pip install pygame-ce")
            return

        self._vis_on = True
        self._vis_btn.config(text="✕  Close visualizer", bg=ACCENT, fg=WHITE)
        if self._vis_cv is None:
            self._vis_cv = Canvas(self, bg="#050505", highlightthickness=0,
                                  cursor="hand2")
            # Clicking the visualizer surface closes it too.
            self._vis_cv.bind("<Button-1>", lambda _e: self._toggle_visualizer())
            self._vis_cv.bind("<Configure>", lambda _e: self._vis_build_bars())
        self._vis_levels = np.zeros(self._vis_n, dtype=np.float32)
        self._vis_cv.place(x=0, y=0, relwidth=1.0, relheight=1.0)
        self._vis_cv.lift()
        self._vis_build_bars()
        self._vis_tick()

    def _vis_build_bars(self):
        if not self._vis_cv: return
        cv = self._vis_cv
        cv.delete("all")
        w = cv.winfo_width(); h = cv.winfo_height()
        if w <= 1 or h <= 1: return
        n = self._vis_n
        gap = 3
        bw = (w - gap * (n + 1)) / n
        self._vis_bars = []
        base_y = int(h * 0.86)
        for i in range(n):
            x0 = gap + i * (bw + gap)
            x1 = x0 + bw
            rect = cv.create_rectangle(x0, base_y, x1, base_y, fill=ACCENT, width=0)
            self._vis_bars.append(rect)
        # Header text over the visualizer
        self._vis_title = cv.create_text(
            w // 2, int(h * 0.10), text="", fill=TXT,
            font=(FF, 16, "bold"))
        cv.create_text(w // 2, int(h * 0.10) + 26,
                       text="click anywhere to close", fill=TXT_DIM, font=(FF, 9))

    def _vis_tick(self):
        if not self._vis_on or not self._vis_cv:
            return
        app = self.app
        cv  = self._vis_cv
        w = cv.winfo_width(); h = cv.winfo_height()
        if w <= 1 or h <= 1 or not self._vis_bars:
            self._vis_anim_id = app.root.after(40, self._vis_tick)
            return

        song = self._current_song()
        try:
            cv.itemconfig(self._vis_title,
                          text=(song.title or song.name) if song else "Nothing playing")
        except Exception:
            pass

        n = self._vis_n
        bands = None
        if app._playing and app._song_dur > 0:
            t = min(time.time() - app._play_start, app._song_dur)
            bands = app._spectrum.bands(t, n_bands=n)

        lv = self._vis_levels
        if bands is None:
            lv *= 0.85                       # decay to rest when idle / not ready
        else:
            for i in range(n):
                target = float(bands[i])
                if target > lv[i]:
                    lv[i] = lv[i] + (target - lv[i]) * 0.6   # fast attack
                else:
                    lv[i] = lv[i] * 0.82 + target * 0.18     # slow release

        base_y  = int(h * 0.86)
        max_bar = int(h * 0.66)
        gap = 3
        bw = (w - gap * (n + 1)) / n
        for i, rect in enumerate(self._vis_bars):
            level = max(0.0, min(1.0, float(lv[i])))
            bh = int(level * max_bar) + 2
            x0 = gap + i * (bw + gap)
            x1 = x0 + bw
            cv.coords(rect, x0, base_y - bh, x1, base_y)
            cv.itemconfig(rect, fill=_lerp_color(ACCENT_DK, ACCENT_HI, level))

        self._vis_anim_id = app.root.after(33, self._vis_tick)   # ~30 fps

    # ── Update tick ──────────────────────────────────────────────────────────────
    def _tick(self):
        if not self._visible: return
        app = self.app

        if not self._anim_id:
            try:
                rh = app.root.winfo_height()
                rw = app.root.winfo_width()
                if self.winfo_width() != rw or self.winfo_height() != rh:
                    self.place(x=0, y=0, width=rw, height=rh)
            except Exception:
                pass

        song = self._current_song()

        if song is not self._last_song:
            self._last_song = song
            if song:
                self._now_name2.set((song.title or song.name)[:60])
                self._now_art2.set(song.artist or "")
                lyr = app.lyrics_db.get(song.path, {}).get("lyrics", "")
                self._set_lyrics_text(lyr)
                threading.Thread(target=self._load_cover_bg,
                                 args=(song,), daemon=True).start()
            else:
                self._now_name2.set("Nothing playing")
                self._now_art2.set("")
                self._set_lyrics_text("")
                self._cover_lbl.config(image="", text="♪", font=(FF, 60), fg=TXT_DIM)

        if song and not self._lyrics_editing:
            new_lyr = app.lyrics_db.get(song.path, {}).get("lyrics", "")
            cur_txt = self._lyr_txt.get("1.0", "end-1c")
            if new_lyr and "No lyrics" in cur_txt:
                self._set_lyrics_text(new_lyr)

        self._fs_play_btn.config(text="⏸" if app._playing else "▶")
        rep_icons = {0: ("🔁", TXT_DIM), 1: ("🔂", ACCENT), 2: ("🔁", ACCENT)}
        ri, rc = rep_icons[app._repeat]
        try: self._fs_rep_btn.config(text=ri, fg=rc)
        except Exception: pass
        try: self._fs_shuf_btn.config(fg=ACCENT if app._shuf_on else TXT_DIM)
        except Exception: pass

        if app._playing and app._song_dur > 0:
            elapsed = time.time() - app._play_start
            pct = min(elapsed / app._song_dur, 1.0)
            try:
                fw = self._fs_cv.winfo_width()
                if fw > 1:
                    self._fs_cv.coords(self._fs_fill, 0, 0, int(fw*pct), 14)
                    self._fs_cv.coords(self._fs_dot,
                                       int(fw*pct)-7, -3, int(fw*pct)+7, 17)
            except Exception: pass

        app.root.after(400, self._tick)

    # ── Cover art loading ──────────────────────────────────────────────────────
    def _load_cover_bg(self, song):
        if not _PIL: return
        path = song.path
        if path in self._cover_cache:
            self.after(0, lambda p=path: self._apply_cover(self._cover_cache.get(p)))
            return
        img_data = get_embedded_art_data(path)
        if not img_data and song.cover_url:
            try:
                req = urllib.request.Request(song.cover_url,
                          headers={"User-Agent": "CoolMP3/1.0"})
                with urllib.request.urlopen(req, timeout=6) as r:
                    img_data = r.read()
            except Exception:
                pass
        if img_data:
            try:
                pimg = Image.open(io.BytesIO(img_data))
                self.after(0, lambda p=pimg, k=path: self._finish_cover(p, k))
            except Exception:
                pass
        else:
            self.after(0, lambda: self._cover_lbl.config(
                image="", text="♪", font=(FF, 64), fg=TXT_DIM))

    def _finish_cover(self, pimg, path):
        try:
            w = max(200, self._cover_lbl.winfo_width())
            h = max(200, self._cover_lbl.winfo_height())
            sz = min(w, h, 400)
            pimg = pimg.resize((sz, sz), Image.LANCZOS)
            photo = ImageTk.PhotoImage(pimg)
            self._cover_cache[path] = photo
            self._apply_cover(photo)
        except Exception:
            pass

    def _apply_cover(self, photo):
        if not photo: return
        self._cover_img = photo
        self._cover_lbl.config(image=photo, text="", font=(FF, 1))


# ==============================================================================
#  APPLICATION
# ==============================================================================
class PlayerApp:
    def __init__(self, root: Tk):
        self.root        = root
        self.songs:     list = []
        self.playlists: list = []
        self.lyrics_db: dict = load_lyrics_db()
        self._overlay: "NowPlayingOverlay" = None
        self._spectrum = AudioSpectrum()

        self._mq = queue.Queue()   # songs needing metadata (cover + lyrics)
        self._uq = queue.Queue()   # songs whose library row needs a refresh

        self._scroll_canvases: set = set()
        self._active_tab = "Library"
        self._lib_visible: list = []
        self._lib_rows:   dict = {}
        self._last_save  = 0.0

        # Player state
        self._queue:    list = []
        self._q_idx:    int  = -1
        self._playing        = False
        self._song_dur       = 0.0
        self._play_start     = 0.0
        self._paused_at      = 0.0
        self._next_up:  list = []
        self._repeat:   int  = 0
        self._shuf_on:  bool = False
        self._seeking:  bool = False

        self._setup_window()
        self._build_ui()
        self._start_workers()
        self._load_saved()
        self._poll()
        if _PYGAME: self._update_player()

    # ── Window ────────────────────────────────────────────────────────────────
    def _shortcut_ok(self):
        w = self.root.focus_get()
        return not isinstance(w, (Entry, Text))

    def _setup_window(self):
        self.root.title("Cool MP3 Player")
        self.root.configure(bg=BG)
        self.root.geometry("1500x920")
        self.root.minsize(1000, 680)
        self.root.bind("<MouseWheel>", self._on_wheel)
        ok = self._shortcut_ok
        self.root.bind("<space>", lambda e: ok() and self._toggle_play())
        self.root.bind("<Right>", lambda e: ok() and self._seek_relative(10))
        self.root.bind("<Left>",  lambda e: ok() and self._seek_relative(-10))
        self.root.bind("<n>",     lambda e: ok() and self._next_song())
        self.root.bind("<p>",     lambda e: ok() and self._prev_song())
        self.root.bind("<r>",     lambda e: ok() and self._toggle_repeat())
        self.root.bind("<s>",     lambda e: ok() and self._toggle_shuffle())
        self.root.bind("<f>",     lambda e: ok() and self._toggle_now_playing())
        self.root.bind("<Up>",    lambda e: ok() and self._change_volume(0.05))
        self.root.bind("<Down>",  lambda e: ok() and self._change_volume(-0.05))

    def _change_volume(self, delta: float):
        v = max(0.0, min(1.0, self._vol_var.get() + delta))
        self._vol_var.set(v)
        if _PYGAME: pygame.mixer.music.set_volume(v)

    # ── UI skeleton ───────────────────────────────────────────────────────────
    def _build_ui(self):
        hdr = Frame(self.root, bg=BG, pady=14); hdr.pack(fill=X, padx=28)
        Label(hdr, text="COOL MP3 PLAYER", font=(FF, 24, "bold"),
              bg=BG, fg=ACCENT).pack(side=LEFT)
        Label(hdr, text="  lyrics · cover art · live visualizer", font=(FF, 11),
              bg=BG, fg=TXT_DIM).pack(side=LEFT)
        Frame(self.root, bg=BORDER, height=1).pack(fill=X)

        tab_bar = Frame(self.root, bg=BG2); tab_bar.pack(fill=X)
        self._tab_frames: dict = {}
        self._tab_btns:   dict = {}
        for label in ("Library", "Playlists"):
            btn = Button(tab_bar, text=label, font=(FF, 11, "bold"),
                         bg=BG2, fg=TXT_DIM, relief=FLAT, cursor="hand2",
                         padx=24, pady=12, bd=0,
                         activebackground=BG3, activeforeground=TXT,
                         command=lambda l=label: self._switch_tab(l))
            btn.pack(side=LEFT)
            self._tab_btns[label] = btn
        Frame(self.root, bg=BORDER, height=1).pack(fill=X)

        self._content = Frame(self.root, bg=BG); self._content.pack(fill=BOTH, expand=True)
        for label in ("Library", "Playlists"):
            f = Frame(self._content, bg=BG2 if label == "Library" else BG)
            self._tab_frames[label] = f

        self._build_library_tab(self._tab_frames["Library"])
        self._build_playlists_tab(self._tab_frames["Playlists"])

        Frame(self.root, bg=BORDER, height=1).pack(fill=X)
        self._build_player_bar()
        Frame(self.root, bg=BORDER, height=1).pack(fill=X)
        self._status = StringVar(value="Ready")
        Label(self.root, textvariable=self._status, font=(FF, 9), bg=BG,
              fg=TXT_DIM, anchor=W, pady=5).pack(fill=X, padx=20)

        self._switch_tab("Library")
        self._overlay = NowPlayingOverlay(self)

    def _switch_tab(self, label):
        self._active_tab = label
        for f in self._tab_frames.values(): f.pack_forget()
        self._tab_frames[label].pack(fill=BOTH, expand=True)
        for k, btn in self._tab_btns.items():
            btn.config(fg=ACCENT if k == label else TXT_DIM,
                       bg=BG3   if k == label else BG2)
        if label == "Library":     self._render_library()
        elif label == "Playlists": self._render_playlists_tab()

    # ──────────────────────────────────────────────────────────────────────────
    #  LIBRARY TAB
    # ──────────────────────────────────────────────────────────────────────────
    def _build_library_tab(self, parent):
        bar = Frame(parent, bg=BG2, pady=12, padx=16); bar.pack(fill=X)
        Label(bar, text="Library", font=(FF, 14, "bold"), bg=BG2, fg=TXT).pack(side=LEFT)
        Button(bar, text="＋  Add Songs", font=(FF, 10, "bold"), bg=ACCENT, fg=WHITE,
               relief=FLAT, cursor="hand2", padx=14, pady=7,
               activebackground=ACCENT_DK, activeforeground=WHITE,
               command=self._upload).pack(side=RIGHT, padx=(6, 0))
        Button(bar, text="🔀  Shuffle All", font=(FF, 10, "bold"), bg=BG3, fg=TXT,
               relief=FLAT, cursor="hand2", padx=14, pady=7,
               activebackground=BG4, activeforeground=ACCENT,
               command=self._shuffle_all_library).pack(side=RIGHT, padx=(6, 0))
        Button(bar, text="▶  Play All", font=(FF, 10, "bold"), bg=BG3, fg=TXT,
               relief=FLAT, cursor="hand2", padx=14, pady=7,
               activebackground=BG4, activeforeground=ACCENT,
               command=self._play_all_library).pack(side=RIGHT, padx=(6, 0))

        Frame(parent, bg=BORDER, height=1).pack(fill=X)

        ctrl = Frame(parent, bg=BG2, pady=8, padx=16); ctrl.pack(fill=X)
        self._search_var = StringVar()
        self._search_after_id = None
        self._search_var.trace_add("write", lambda *_: self._debounce_search())
        Entry(ctrl, textvariable=self._search_var, font=(FF, 10),
              bg=BG3, fg=TXT, insertbackground=TXT, relief=FLAT,
              highlightthickness=1, highlightbackground=BORDER,
              highlightcolor=ACCENT, width=28).pack(side=LEFT, ipady=5, padx=(0, 12))

        Label(ctrl, text="Sort:", font=(FF, 9), bg=BG2, fg=TXT_DIM).pack(side=LEFT)
        self._sort_var = StringVar(value="Name")
        om = OptionMenu(ctrl, self._sort_var, "Name", "Artist", "Duration",
                        command=lambda _: self._render_library())
        om.config(bg=BG3, fg=TXT, relief=FLAT, font=(FF, 9), padx=8, pady=4,
                  activebackground=BG4, highlightthickness=1, highlightbackground=BORDER)
        om["menu"].config(bg=BG3, fg=TXT, font=(FF, 9),
                          activebackground=ACCENT, activeforeground=WHITE)
        om.pack(side=LEFT, padx=(4, 16))

        self._lib_info = StringVar(value="No songs yet")
        Label(ctrl, textvariable=self._lib_info, font=(FF, 9),
              bg=BG2, fg=TXT_DIM).pack(side=RIGHT)

        Frame(parent, bg=BORDER, height=1).pack(fill=X)

        wrap = Frame(parent, bg=BG2); wrap.pack(fill=BOTH, expand=True)
        self._lib_cv = Canvas(wrap, bg=BG2, bd=0, highlightthickness=0)
        sb = ttk.Scrollbar(wrap, orient=VERTICAL, command=self._lib_cv.yview)
        self._lib_fr = Frame(self._lib_cv, bg=BG2)
        self._lib_cv.configure(yscrollcommand=sb.set)
        sb.pack(side=RIGHT, fill=Y)
        self._lib_cv.pack(side=LEFT, fill=BOTH, expand=True)
        win = self._lib_cv.create_window((0, 0), window=self._lib_fr, anchor=NW)
        self._lib_win = win
        self._lib_fr.bind("<Configure>", self._sync_lib_scrollregion)
        self._lib_cv.bind("<Configure>",
                          lambda e: self._lib_cv.itemconfig(self._lib_win, width=e.width))
        self._scroll_canvases.add(self._lib_cv)

    def _sync_lib_scrollregion(self, _event=None):
        try:
            self._lib_cv.configure(scrollregion=self._lib_cv.bbox("all"))
        except Exception:
            pass

    def _debounce_search(self):
        if self._search_after_id:
            try: self.root.after_cancel(self._search_after_id)
            except Exception: pass
        self._search_after_id = self.root.after(250, self._render_library)

    def _render_library(self):
        search = self._search_var.get().lower().strip() if hasattr(self, "_search_var") else ""
        srt    = self._sort_var.get() if hasattr(self, "_sort_var") else "Name"

        songs = self.songs[:]
        if search:
            songs = [s for s in songs
                     if search in s.name.lower() or search in (s.artist or "").lower()
                     or search in (s.title or "").lower()]
        if srt == "Name":        songs.sort(key=lambda s: (s.title or s.name).lower())
        elif srt == "Artist":    songs.sort(key=lambda s: (s.artist or "~").lower())
        elif srt == "Duration":  songs.sort(key=lambda s: s.duration, reverse=True)
        self._lib_visible = songs
        self._lib_rows = {}

        new_fr = Frame(self._lib_cv, bg=BG2)
        new_fr.bind("<Configure>", self._sync_lib_scrollregion)
        if not songs:
            Label(new_fr,
                  text="No songs match." if self.songs else
                       "No songs yet.\nClick  + Add Songs  above.",
                  font=(FF, 11), bg=BG2, fg=TXT_DIM,
                  justify=CENTER, pady=40).pack()
        else:
            for i, song in enumerate(songs):
                self._song_row_lib(i, song, new_fr)

        old_fr = self._lib_fr
        self._lib_fr = new_fr
        self._lib_cv.itemconfig(self._lib_win, window=new_fr)
        self._lib_cv.update_idletasks()
        self._lib_cv.configure(scrollregion=self._lib_cv.bbox("all"))
        self.root.after(0, old_fr.destroy)

        total = len(self.songs)
        if total == 0: self._lib_info.set("No songs yet")
        else:          self._lib_info.set(f"{total} songs  ·  showing {len(songs)}")

    def _song_row_lib(self, idx, song, parent=None):
        if parent is None: parent = self._lib_fr
        row_bg = BG4 if idx % 2 == 0 else BG3
        row = Frame(parent, bg=row_bg, pady=9); row.pack(fill=X)
        self._lib_rows[id(song)] = row
        self._populate_lib_row(row, idx, song, row_bg)
        return row

    def _update_song_row(self, song):
        if self._active_tab != "Library": return
        row = self._lib_rows.get(id(song))
        if row is None: return
        try:
            if not row.winfo_exists():
                self._lib_rows.pop(id(song), None); return
        except Exception:
            return
        try:    idx = self._lib_visible.index(song)
        except ValueError:
            return
        row_bg = BG4 if idx % 2 == 0 else BG3
        for w in row.winfo_children(): w.destroy()
        self._populate_lib_row(row, idx, song, row_bg)

    def _populate_lib_row(self, row, idx, song, row_bg):
        play_btn = Button(row, text="▶", font=(FF, 11), bg=row_bg, fg=ACCENT,
                          relief=FLAT, cursor="hand2", padx=6, pady=2,
                          activebackground=BG3, activeforeground=ACCENT)
        play_btn.config(command=lambda i=idx: self._play_from_library(i))
        play_btn.pack(side=LEFT, padx=(8, 4))
        row.bind("<Button-3>", lambda e, s=song, i=idx: self._lib_context_menu(e, s, i))
        play_btn.bind("<Button-3>", lambda e, s=song, i=idx: self._lib_context_menu(e, s, i))

        Label(row, text=f"{idx+1:03d}", font=(FF, 9), bg=row_bg,
              fg=TXT_DIM, width=4, anchor=E).pack(side=LEFT, padx=(0, 8))

        Button(row, text="🗑", font=(FF, 9), bg=row_bg, fg=TXT_DIM,
               relief=FLAT, cursor="hand2", padx=5, pady=2,
               activebackground="#200808", activeforeground="#EF5350",
               command=lambda s=song: self._delete_song(s)).pack(side=RIGHT, padx=(0, 8))

        if song.duration > 0:
            Label(row, text=_fmt_dur(song.duration), font=(FF, 9),
                  bg=row_bg, fg=TXT_DIM).pack(side=RIGHT, padx=(4, 6))

        if song.path in self.lyrics_db and self.lyrics_db[song.path].get("lyrics"):
            Label(row, text="♪", font=(FF, 9), bg=row_bg,
                  fg=ACCENT).pack(side=RIGHT, padx=(0, 2))

        info = Frame(row, bg=row_bg); info.pack(side=LEFT, fill=X, expand=True)
        display = song.title if song.title != song.name else song.name
        name_str = (display[:48] + "…") if len(display) > 48 else display
        Label(info, text=name_str, font=(FF, 11), bg=row_bg, fg=TXT,
              anchor=W).pack(fill=X)
        if song.artist:
            Label(info, text=song.artist, font=(FF, 8), bg=row_bg,
                  fg=TXT_DIM, anchor=W).pack(fill=X)

    def _delete_song(self, song):
        name   = song.name
        choice = messagebox.askyesnocancel(
            "Remove Song",
            f"Remove  \"{name}\"  from library?\n\n"
            f"  Yes  = remove from library, keep file on disk\n"
            f"  No   = remove from library AND delete the file\n"
            f"  Cancel = do nothing")
        if choice is None: return
        self.songs = [s for s in self.songs if s is not song]
        if self._queue and 0 <= self._q_idx < len(self._queue):
            if self._queue[self._q_idx] is song:
                self._queue = [s for s in self._queue if s is not song]
                if self._queue:
                    self._q_idx = min(self._q_idx, len(self._queue)-1)
                    self._play_current()
                else:
                    if _PYGAME: pygame.mixer.music.stop()
                    self._playing = False
                    self._play_btn.config(text="▶")
                    self._now_name.set("Nothing playing"); self._now_sub.set("")
            else:
                self._queue = [s for s in self._queue if s is not song]
        else:
            self._queue = [s for s in self._queue if s is not song]
        if choice is False:
            try:
                os.remove(song.path)
                self._status.set(f"🗑 Deleted '{name}' from library and disk")
            except Exception as ex:
                self._status.set(f"Removed from library; couldn't delete file: {ex}")
        else:
            self._status.set(f"🗑 Removed '{name}' from library  (file kept on disk)")
        save_library(self.songs)
        self._render_library()

    # ── Play from library ──────────────────────────────────────────────────────
    def _play_from_library(self, idx: int):
        if not _PYGAME:
            messagebox.showinfo("Cool MP3 Player",
                "Install pygame-ce for playback:\n\n    pip install pygame-ce"); return
        songs = self._lib_visible
        if not songs or idx < 0 or idx >= len(songs): return
        seed = songs[idx]
        # Play from the clicked song to the end of what's shown, then continue
        # through the rest of the library so playback never dead-ends.
        head    = songs[idx:]
        exclude = {id(s) for s in head}
        tail    = [s for s in self.songs if id(s) not in exclude and not s.failed]
        random.shuffle(tail)
        self._next_up.clear()
        self._play_playlist(head + tail, 0)
        self._status.set(f"▶ Playing: {seed.title or seed.name}"
                         + (f"  ·  +{len(tail)} more queued from your library"
                            if tail else ""))

    def _play_all_library(self):
        """Play every song in the library, in the order currently shown."""
        if not _PYGAME:
            messagebox.showinfo("Cool MP3 Player",
                "Install pygame-ce for playback:\n\n    pip install pygame-ce"); return
        songs = [s for s in (self._lib_visible or self.songs) if not s.failed]
        if not songs:
            self._status.set("No songs in your library to play"); return
        self._next_up.clear()
        self._shuf_on = False
        self._update_shuf_btn()
        self._play_playlist(list(songs), 0)
        self._status.set(f"▶ Playing all {len(songs)} songs from your library")

    def _shuffle_all_library(self):
        """Shuffle every song in the library into the queue and start playing."""
        if not _PYGAME:
            messagebox.showinfo("Cool MP3 Player",
                "Install pygame-ce for playback:\n\n    pip install pygame-ce"); return
        songs = [s for s in (self._lib_visible or self.songs) if not s.failed]
        if not songs:
            self._status.set("No songs in your library to shuffle"); return
        songs = list(songs)
        random.shuffle(songs)
        self._next_up.clear()
        self._shuf_on = True
        self._update_shuf_btn()
        self._play_playlist(songs, 0)
        self._status.set(f"🔀 Shuffling all {len(songs)} songs from your library")

    def _lib_context_menu(self, event, song, idx):
        menu = Menu(self.root, tearoff=0, bg=BG3, fg=TXT,
                    activebackground=ACCENT, activeforeground=WHITE,
                    font=(FF, 10), relief=FLAT, bd=0)
        menu.add_command(label="  ▶  Play Now",
                         command=lambda: self._play_from_library(
                             self._lib_visible.index(song) if song in self._lib_visible else idx))
        menu.add_command(label="  ⏭  Play Next",
                         command=lambda: self._next_up.insert(0, song) or
                                         self._status.set(f"Playing next: {song.title or song.name}"))
        menu.add_command(label="  ＋  Add to Queue",
                         command=lambda: self._queue_song(song))
        sub = Menu(menu, tearoff=0, bg=BG3, fg=TXT,
                   activebackground=ACCENT, activeforeground=WHITE,
                   font=(FF, 10), relief=FLAT, bd=0)
        for pl in self.playlists:
            sub.add_command(label=f"  {pl['name']}",
                            command=lambda p=pl, s=song: self._add_song_to_playlist(p, s))
        if self.playlists:
            sub.add_separator()
        sub.add_command(label="  ＋  New playlist…",
                        command=lambda s=song: self._new_playlist_with_song(s))
        menu.add_cascade(label="  ♫  Add to Playlist", menu=sub)
        menu.add_separator()
        menu.add_command(label="  🗑  Remove from Library",
                         command=lambda: self._delete_song(song))
        try:   menu.tk_popup(event.x_root, event.y_root)
        finally: menu.grab_release()

    # ──────────────────────────────────────────────────────────────────────────
    #  PLAYLISTS TAB
    # ──────────────────────────────────────────────────────────────────────────
    def _build_playlists_tab(self, parent):
        hdr = Frame(parent, bg=BG, pady=14); hdr.pack(fill=X, padx=28)
        Label(hdr, text="Saved Playlists",
              font=(FF, 14, "bold"), bg=BG, fg=TXT).pack(side=LEFT)
        Button(hdr, text="＋  New Playlist", font=(FF, 10, "bold"),
               bg=ACCENT, fg=WHITE, relief=FLAT, cursor="hand2", padx=14, pady=7,
               activebackground=ACCENT_DK, activeforeground=WHITE,
               command=self._new_playlist_dialog).pack(side=RIGHT)
        Frame(parent, bg=BORDER, height=1).pack(fill=X)
        wrap = Frame(parent, bg=BG); wrap.pack(fill=BOTH, expand=True)
        self._pls_cv = Canvas(wrap, bg=BG, bd=0, highlightthickness=0)
        sb = ttk.Scrollbar(wrap, orient=VERTICAL, command=self._pls_cv.yview)
        self._pls_fr = Frame(self._pls_cv, bg=BG)
        self._pls_cv.configure(yscrollcommand=sb.set)
        sb.pack(side=RIGHT, fill=Y); self._pls_cv.pack(side=LEFT, fill=BOTH, expand=True)
        win = self._pls_cv.create_window((0, 0), window=self._pls_fr, anchor=NW)
        self._pls_fr.bind("<Configure>",
                          lambda e: self._pls_cv.configure(
                              scrollregion=self._pls_cv.bbox("all")))
        self._pls_cv.bind("<Configure>",
                          lambda e: self._pls_cv.itemconfig(win, width=e.width))
        self._scroll_canvases.add(self._pls_cv)

    def _render_playlists_tab(self):
        for w in self._pls_fr.winfo_children(): w.destroy()
        if not self.playlists:
            Label(self._pls_fr,
                  text="No saved playlists yet.\n"
                       "Right-click a song in the Library → Add to Playlist.",
                  font=(FF, 11), bg=BG, fg=TXT_DIM,
                  justify=CENTER, pady=40).pack(); return
        for pl in reversed(self.playlists):
            card = Frame(self._pls_fr, bg=BG3, pady=16, padx=20)
            card.pack(fill=X, pady=(0, 2))
            top = Frame(card, bg=BG3); top.pack(fill=X)
            bf  = Frame(top,  bg=BG3); bf.pack(side=RIGHT)
            Button(bf, text="▶  Play", font=(FF, 10, "bold"),
                   bg=ACCENT, fg=WHITE, relief=FLAT, cursor="hand2",
                   padx=12, pady=6, activebackground=ACCENT_DK, activeforeground=WHITE,
                   command=lambda p=pl: self._play_saved_playlist(p)).pack(side=LEFT, padx=(0, 8))
            Button(bf, text="🗑", font=(FF, 10), bg=BG4, fg=TXT_DIM,
                   relief=FLAT, cursor="hand2", padx=10, pady=6,
                   activebackground="#200808", activeforeground="#EF5350",
                   command=lambda p=pl: self._delete_playlist(p)).pack(side=LEFT)
            Label(top, text=pl["name"], font=(FF, 13, "bold"), bg=BG3, fg=TXT).pack(side=LEFT)
            Label(card, text=f"{len(pl['songs'])} songs  ·  {pl['created']}",
                  font=(FF, 9), bg=BG3, fg=TXT_DIM).pack(anchor=W, pady=(4, 10))
            if not pl["songs"]:
                Label(card, text="Empty playlist — right-click a song in the Library "
                                 "→  Add to Playlist",
                      font=(FF, 9), bg=BG3, fg=TXT_DIM, anchor=W).pack(anchor=W)
            for i, s in enumerate(pl["songs"]):
                row = Frame(card, bg=BG3); row.pack(fill=X, pady=2)
                Label(row, text=str(i+1), font=(FF, 9), bg=BG3,
                      fg=TXT_DIM, width=3, anchor=E).pack(side=LEFT)
                Button(row, text="✕", font=(FF, 8), bg=BG3, fg="#EF5350",
                       relief=FLAT, cursor="hand2", padx=4, pady=0,
                       activebackground=BG3, activeforeground="#EF5350",
                       command=lambda p=pl, idx=i: self._remove_from_playlist(p, idx)
                       ).pack(side=RIGHT, padx=(4, 0))
                Label(row, text=_fmt_dur(s.get("duration", 0)), font=(FF, 8),
                      bg=BG3, fg=TXT_DIM).pack(side=RIGHT, padx=(4, 0))
                Label(row, text=(s["name"][:48] + "…") if len(s["name"]) > 48 else s["name"],
                      font=(FF, 10), bg=BG3, fg=TXT_MID).pack(side=LEFT, padx=8)
            Frame(self._pls_fr, bg=BORDER, height=1).pack(fill=X)
        self._pls_cv.update_idletasks()
        self._pls_cv.configure(scrollregion=self._pls_cv.bbox("all"))

    def _play_saved_playlist(self, pl):
        songs = []
        for d in pl["songs"]:
            if os.path.exists(d.get("path", "")):
                s = Song.__new__(Song)
                s.path = d["path"]; s.name = d["name"]
                s.title = d["name"]; s.artist = d.get("artist", "")
                s.duration = d.get("duration", 0.0)
                s.busy = False; s.failed = False; s.cover_url = ""
                songs.append(s)
        if not songs:
            messagebox.showwarning("Cool MP3 Player", "No files found on disk."); return
        self._play_playlist(songs, 0)
        self._status.set(f"▶ Playing: {pl['name']}")

    def _delete_playlist(self, pl):
        if messagebox.askyesno("Delete", f"Delete \"{pl['name']}\"?"):
            self.playlists = [p for p in self.playlists if p["id"] != pl["id"]]
            save_playlists(self.playlists); self._render_playlists_tab()

    def _new_playlist_dialog(self):
        name = simpledialog.askstring("New Playlist", "Name your playlist:",
                                      parent=self.root)
        if not name or not name.strip(): return None
        pl = {"id": str(uuid.uuid4()), "name": name.strip(),
              "created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
              "songs": []}
        self.playlists.append(pl); save_playlists(self.playlists)
        self._render_playlists_tab()
        self._status.set(f"Created playlist '{name.strip()}'  ·  "
                         f"right-click songs in the Library → Add to Playlist")
        return pl

    def _song_entry(self, song):
        return {"path": song.path, "name": song.title or song.name,
                "artist": song.artist, "duration": song.duration}

    def _add_song_to_playlist(self, pl, song):
        pl["songs"].append(self._song_entry(song))
        save_playlists(self.playlists)
        self._status.set(f"Added '{song.title or song.name}' → '{pl['name']}'  "
                         f"({len(pl['songs'])} songs)")
        if self._active_tab == "Playlists": self._render_playlists_tab()

    def _new_playlist_with_song(self, song):
        pl = self._new_playlist_dialog()
        if pl: self._add_song_to_playlist(pl, song)

    def _remove_from_playlist(self, pl, idx):
        if 0 <= idx < len(pl["songs"]):
            pl["songs"].pop(idx)
            save_playlists(self.playlists); self._render_playlists_tab()

    # ──────────────────────────────────────────────────────────────────────────
    #  PLAYER BAR
    # ──────────────────────────────────────────────────────────────────────────
    def _build_player_bar(self):
        bar = Frame(self.root, bg=BG4); bar.pack(fill=X)

        ctrl = Frame(bar, bg=BG4); ctrl.pack(side=LEFT, padx=20, pady=10)
        self._shuf_btn = Button(ctrl, text="🔀", font=(FF, 11), bg=BG4, fg=TXT_DIM,
                                relief=FLAT, cursor="hand2", padx=5,
                                activebackground=BG3, activeforeground=TXT,
                                command=self._toggle_shuffle)
        self._shuf_btn.pack(side=LEFT, padx=(0, 4))
        self._prev_btn = Button(ctrl, text="⏮", font=(FF, 14), bg=BG4, fg=TXT_MID,
                                relief=FLAT, cursor="hand2", padx=6,
                                activebackground=BG3, activeforeground=TXT,
                                command=self._prev_song)
        self._prev_btn.pack(side=LEFT)
        self._play_btn = Button(ctrl, text="▶", font=(FF, 16, "bold"),
                                bg=BG4, fg=ACCENT, relief=FLAT, cursor="hand2",
                                padx=10, activebackground=BG3, activeforeground=ACCENT,
                                command=self._toggle_play)
        self._play_btn.pack(side=LEFT)
        self._next_btn = Button(ctrl, text="⏭", font=(FF, 14), bg=BG4, fg=TXT_MID,
                                relief=FLAT, cursor="hand2", padx=6,
                                activebackground=BG3, activeforeground=TXT,
                                command=self._next_song)
        self._next_btn.pack(side=LEFT)
        self._repeat_btn = Button(ctrl, text="🔁", font=(FF, 11), bg=BG4, fg=TXT_DIM,
                                  relief=FLAT, cursor="hand2", padx=5,
                                  activebackground=BG3, activeforeground=TXT,
                                  command=self._toggle_repeat)
        self._repeat_btn.pack(side=LEFT, padx=(4, 0))

        centre = Frame(bar, bg=BG4); centre.pack(side=LEFT, fill=X, expand=True, padx=10)
        self._now_name = StringVar(value="Nothing playing")
        self._now_sub  = StringVar(value="")
        top_row = Frame(centre, bg=BG4); top_row.pack(fill=X)
        Label(top_row, textvariable=self._now_sub, font=(FF, 9),
              bg=BG4, fg=TXT_DIM).pack(side=LEFT, padx=(0, 8))
        now_lbl = Label(top_row, textvariable=self._now_name, font=(FF, 10, "bold"),
                        bg=BG4, fg=TXT, cursor="hand2")
        now_lbl.pack(side=LEFT)
        now_lbl.bind("<Button-1>", lambda _: self._toggle_now_playing())

        prog_row = Frame(centre, bg=BG4); prog_row.pack(fill=X, pady=(4, 0))
        self._time_cur = StringVar(value="0:00")
        self._time_tot = StringVar(value="0:00")
        Label(prog_row, textvariable=self._time_cur, font=(FF, 8),
              bg=BG4, fg=TXT_DIM, width=5).pack(side=LEFT)
        self._prog_cv = Canvas(prog_row, height=12, bg=BORDER,
                               highlightthickness=0, cursor="hand2")
        self._prog_cv.pack(side=LEFT, fill=X, expand=True, padx=6)
        self._prog_fill = self._prog_cv.create_rectangle(0, 0, 0, 12, fill=ACCENT, width=0)
        self._prog_dot  = self._prog_cv.create_oval(-6, -2, 6, 14, fill=WHITE,
                                                    outline="", state=HIDDEN)
        self._prog_cv.bind("<Button-1>",  self._seek)
        self._prog_cv.bind("<B1-Motion>", self._seek)
        self._prog_cv.bind("<Enter>",
                           lambda e: self._prog_cv.itemconfig(self._prog_dot, state=NORMAL))
        self._prog_cv.bind("<Leave>",
                           lambda e: self._prog_cv.itemconfig(self._prog_dot, state=HIDDEN))
        Label(prog_row, textvariable=self._time_tot, font=(FF, 8),
              bg=BG4, fg=TXT_DIM, width=5).pack(side=LEFT)

        # Quick full-screen / visualizer access from the main bar
        Button(bar, text="⤢", font=(FF, 13), bg=BG4, fg=TXT_MID, relief=FLAT,
               cursor="hand2", padx=8, activebackground=BG3, activeforeground=TXT,
               command=self._toggle_now_playing).pack(side=RIGHT, padx=(0, 12))

        vol_f = Frame(bar, bg=BG4); vol_f.pack(side=RIGHT, padx=10, pady=10)
        Label(vol_f, text="🔊", font=(FF, 11), bg=BG4, fg=TXT_DIM).pack(side=LEFT)
        self._vol_var = DoubleVar(value=0.8)
        Scale(vol_f, variable=self._vol_var, from_=0.0, to=1.0,
              resolution=0.05, orient=HORIZONTAL, length=90,
              bg=BG4, fg=TXT_DIM, troughcolor=BORDER,
              highlightthickness=0, showvalue=False, sliderlength=14,
              command=self._on_volume).pack(side=LEFT, padx=(4, 0))
        if not _PYGAME:
            Label(bar, text="⚠ pip install pygame-ce", font=(FF, 9),
                  bg=BG4, fg="#EF5350").pack(side=RIGHT, padx=16)

    def _toggle_now_playing(self):
        if self._overlay and self._overlay._visible:
            self._overlay.hide()
        elif self._overlay:
            self._overlay.show()

    # ── Playback ───────────────────────────────────────────────────────────────
    def _play_playlist(self, songs, start_idx=0):
        self._queue = songs; self._q_idx = start_idx; self._play_current()

    def _play_current(self):
        if not _PYGAME or not self._queue: return
        if self._q_idx < 0 or self._q_idx >= len(self._queue): return
        song = self._queue[self._q_idx]
        try:
            pygame.mixer.music.load(song.path)
            pygame.mixer.music.set_volume(self._vol_var.get())
            pygame.mixer.music.play()
            self._playing    = True
            self._play_start = time.time()
            self._paused_at  = 0.0
            self._seeking    = False
            # Feed the visualizer with this song's samples
            self._spectrum.load(song.path)
            dur = song.duration
            if dur <= 0:
                dur = get_duration_fast(song.path)
                if dur > 0: song.duration = dur
            self._song_dur = dur
            self._now_name.set(song.title or song.name)
            self._now_sub.set(song.artist or "♪")
            self._time_tot.set(_fmt_dur(self._song_dur) if self._song_dur > 0 else "--:--")
            self._play_btn.config(text="⏸")
            self._update_repeat_btn()
            self._update_shuf_btn()
            self._status.set(f"▶  Now playing: {song.title or song.name}")
            # Make sure metadata (cover + lyrics) gets looked up for played files
            self._mq.put(song)
        except Exception as ex:
            self._status.set(f"Playback error: {ex}")

    def _toggle_play(self):
        if not _PYGAME: return
        if self._playing:
            pygame.mixer.music.pause()
            self._paused_at = time.time() - self._play_start
            self._playing   = False; self._play_btn.config(text="▶")
            if self._overlay and self._overlay._visible:
                try: self._overlay._fs_play_btn.config(text="▶")
                except Exception: pass
        else:
            if self._queue and self._q_idx >= 0:
                pygame.mixer.music.unpause()
                self._play_start = time.time() - self._paused_at
                self._playing    = True; self._play_btn.config(text="⏸")
                if self._overlay and self._overlay._visible:
                    try: self._overlay._fs_play_btn.config(text="⏸")
                    except Exception: pass

    def _next_song(self, auto: bool = False):
        if not _PYGAME: return
        if auto and self._repeat == 1:
            pygame.mixer.music.play()
            self._play_start = time.time(); self._paused_at = 0.0
            self._spectrum.load(self._queue[self._q_idx].path)
            return
        if self._next_up:
            nxt = self._next_up.pop(0)
            self._queue.insert(self._q_idx + 1, nxt)
            self._q_idx += 1
            self._play_current()
            self._refresh_queue_display()
            return
        if not self._queue: return
        last = len(self._queue) - 1
        if auto and self._repeat == 0 and self._q_idx >= last:
            self._playing = False
            self._play_btn.config(text="▶")
            if self._overlay and self._overlay._visible:
                try: self._overlay._fs_play_btn.config(text="▶")
                except Exception: pass
            self._status.set("Queue finished")
            return
        self._q_idx = (self._q_idx + 1) % len(self._queue)
        self._play_current()

    def _prev_song(self):
        if not _PYGAME: return
        if self._playing and (time.time() - self._play_start) > 3:
            self._do_seek(0.0)
            return
        if not self._queue: return
        self._q_idx = (self._q_idx - 1) % len(self._queue)
        self._play_current()

    def _queue_song(self, song):
        self._next_up.append(song)
        self._refresh_queue_display()
        self._status.set(f"Added to queue: {song.title or song.name}  "
                         f"({len(self._next_up)} in queue)")

    def _play_queued_song(self, song):
        if not _PYGAME: return
        if self._queue and 0 <= self._q_idx < len(self._queue):
            self._queue.insert(self._q_idx + 1, song)
            self._q_idx += 1
        else:
            self._queue = [song]; self._q_idx = 0
        self._play_current()
        self._refresh_queue_display()

    def _refresh_queue_display(self):
        if self._overlay:
            try: self._overlay._render_queue()
            except Exception: pass

    def _toggle_repeat(self):
        self._repeat = (self._repeat + 1) % 3
        self._update_repeat_btn()

    def _update_repeat_btn(self):
        if not hasattr(self, "_repeat_btn"): return
        labels = {0: ("🔁", TXT_DIM), 1: ("🔂", ACCENT), 2: ("🔁", ACCENT)}
        txt, col = labels[self._repeat]
        self._repeat_btn.config(text=txt, fg=col)

    def _toggle_shuffle(self):
        self._shuf_on = not self._shuf_on
        if self._shuf_on and self._queue:
            cur = self._queue[self._q_idx] if 0 <= self._q_idx < len(self._queue) else None
            rest = [s for s in self._queue if s is not cur]
            random.shuffle(rest)
            self._queue = ([cur] + rest) if cur else rest
            self._q_idx = 0
        self._update_shuf_btn()

    def _update_shuf_btn(self):
        if not hasattr(self, "_shuf_btn"): return
        self._shuf_btn.config(fg=ACCENT if self._shuf_on else TXT_DIM)

    def _on_volume(self, val):
        if _PYGAME: pygame.mixer.music.set_volume(float(val))

    def _do_seek(self, pos_secs: float):
        if not _PYGAME: return
        dur = self._song_dur
        if dur <= 0 and self._queue and 0 <= self._q_idx < len(self._queue):
            dur = get_duration_fast(self._queue[self._q_idx].path)
            if dur > 0:
                self._queue[self._q_idx].duration = dur
                self._song_dur = dur
                self._time_tot.set(_fmt_dur(dur))
        if dur <= 0: return
        pos_secs = max(0.0, min(pos_secs, dur - 0.5))
        try:
            pygame.mixer.music.play(0, float(pos_secs))
            self._play_start = time.time() - pos_secs
            self._paused_at  = 0.0
            self._playing    = True
            self._play_btn.config(text="⏸")
            if self._overlay and self._overlay._visible:
                try: self._overlay._fs_play_btn.config(text="⏸")
                except Exception: pass
        except Exception as ex:
            self._status.set(f"Seek error: {ex}")

    def _seek(self, event):
        if not _PYGAME: return
        dur = self._song_dur
        if dur <= 0 and self._queue and 0 <= self._q_idx < len(self._queue):
            dur = get_duration_fast(self._queue[self._q_idx].path)
            if dur > 0:
                self._queue[self._q_idx].duration = dur
                self._song_dur = dur
                self._time_tot.set(_fmt_dur(dur))
        if dur <= 0: return
        w = self._prog_cv.winfo_width()
        if w <= 1: return
        self._seeking = True
        pct = max(0.0, min(1.0, event.x / w))
        self._prog_cv.coords(self._prog_fill, 0, 0, int(w*pct), 12)
        self._prog_cv.coords(self._prog_dot, int(w*pct)-6, -2, int(w*pct)+6, 14)
        self._time_cur.set(_fmt_dur(pct * dur))
        self._do_seek(pct * dur)
        self._seeking = False

    def _seek_relative(self, delta_secs: float):
        if not _PYGAME or not self._playing: return
        elapsed = time.time() - self._play_start
        self._do_seek(elapsed + delta_secs)

    def _update_player(self):
        if _PYGAME and self._playing and not self._seeking:
            elapsed = time.time() - self._play_start
            self._time_cur.set(_fmt_dur(elapsed))
            if self._song_dur > 0:
                pct = min(elapsed / self._song_dur, 1.0)
                w = self._prog_cv.winfo_width()
                if w > 1:
                    self._prog_cv.coords(self._prog_fill, 0, 0, int(w*pct), 12)
                    self._prog_cv.coords(self._prog_dot,
                                         int(w*pct)-6, -2, int(w*pct)+6, 14)
                if not pygame.mixer.music.get_busy() and elapsed > 1.5:
                    self._next_song(auto=True)
            else:
                if not pygame.mixer.music.get_busy() and elapsed > 1.5:
                    self._next_song(auto=True)
        self.root.after(400, self._update_player)

    # ──────────────────────────────────────────────────────────────────────────
    #  WORKERS
    # ──────────────────────────────────────────────────────────────────────────
    def _upload(self):
        paths = filedialog.askopenfilenames(
            title="Select audio files",
            filetypes=[("Audio", "*.mp3 *.wav *.flac *.ogg *.m4a"),
                       ("MP3", "*.mp3"), ("All", "*.*")])
        if not paths: return
        existing = {s.path for s in self.songs}; added = 0
        for p in paths:
            if p not in existing:
                song = Song(p); self.songs.append(song)
                self._mq.put(song); added += 1
        if added:
            save_library(self.songs)
            self._render_library()
            self._status.set(f"Added {added} songs  ·  looking up cover art + lyrics…")

    def _start_workers(self):
        threading.Thread(target=self._metadata_worker, daemon=True).start()

    def _metadata_worker(self):
        """Single thread: iTunes cover art + lyrics (rate-limited)."""
        while True:
            song = self._mq.get()
            artist, title = song.artist, song.title or song.name

            if not song.cover_url:
                cover = internet_cover(title, artist)
                if cover:
                    song.cover_url = cover
                    self._uq.put(song)

            if song.path not in self.lyrics_db or not self.lyrics_db[song.path].get("lyrics"):
                lyrics = fetch_lyrics(title, artist)
                if lyrics:
                    self.lyrics_db[song.path] = {"lyrics": lyrics, "source": "auto"}
                    save_lyrics_db(self.lyrics_db)
                    self._uq.put(song)

            time.sleep(0.4)   # be polite to the free APIs

    def _poll(self):
        changed_songs = []
        try:
            while True:
                changed_songs.append(self._uq.get_nowait())
        except queue.Empty:
            pass

        if changed_songs:
            seen = set()
            for song in changed_songs:
                if id(song) in seen: continue
                seen.add(id(song))
                self._update_song_row(song)
            if time.time() - self._last_save >= 8.0:
                save_library(self.songs)
                self._last_save = time.time()

        self.root.after(300, self._poll)

    def _load_saved(self):
        songs = load_library()
        if songs:
            existing = {s.path for s in self.songs}
            self.songs.extend([s for s in songs if s.path not in existing])
            self._render_library()
            self._status.set(f"Loaded {len(songs)} songs")
            threading.Thread(target=self._backfill_metadata, daemon=True).start()
        self.playlists = load_playlists()

    def _backfill_metadata(self):
        time.sleep(2)
        for song in list(self.songs):
            if not song.cover_url or song.path not in self.lyrics_db:
                self._mq.put(song)
                time.sleep(0.1)

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _on_wheel(self, event):
        cv = self._wheel_target()
        if cv is not None:
            try: cv.yview_scroll(int(-1*event.delta/120), "units")
            except Exception: pass

    def _wheel_target(self):
        try:
            w = self.root.winfo_containing(self.root.winfo_pointerx(),
                                           self.root.winfo_pointery())
        except Exception:
            return None
        while w is not None:
            if w in self._scroll_canvases:
                return w
            w = getattr(w, "master", None)
        return None


# ==============================================================================
if __name__ == "__main__":
    root = Tk()
    PlayerApp(root)
    root.mainloop()
