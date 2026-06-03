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

import os, re, io, json, time, uuid, queue, ctypes, random, colorsys, threading, datetime
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
        self._samples_lr = None  # stereo float32 (N,2), same normalisation
        self._token   = 0        # guards against a stale background load

    def load(self, path: str):
        """Kick off a background decode of `path` into mono + stereo buffers."""
        if not _PYGAME:
            return
        self._token += 1
        token = self._token
        with self._lock:
            self._samples = None
            self._samples_lr = None

        def _work():
            try:
                snd = pygame.mixer.Sound(path)
                raw = pygame.sndarray.array(snd)          # int16, (N,) or (N,2)
                if raw.ndim == 2:
                    lr   = raw.astype(np.float32)
                    mono = lr.mean(axis=1)
                else:
                    mono = raw.astype(np.float32)
                    lr   = np.stack([mono, mono], axis=1)
                peak = float(np.max(np.abs(mono))) or 1.0
                mono = mono / peak
                lr   = lr / peak
                if token == self._token:                  # still the current song?
                    with self._lock:
                        self._samples    = mono
                        self._samples_lr = lr
            except Exception:
                pass

        threading.Thread(target=_work, daemon=True).start()

    def clear(self):
        self._token += 1
        with self._lock:
            self._samples = None
            self._samples_lr = None

    def energy(self, t_sec: float, window: int = 2048) -> float:
        """RMS loudness (0..~1) of the mono signal around `t_sec`."""
        with self._lock:
            s = self._samples
        if s is None or len(s) == 0:
            return 0.0
        center = int(t_sec * MIXER_SR)
        start  = max(0, center - window // 2)
        end    = min(len(s), start + window)
        chunk  = s[start:end]
        if len(chunk) == 0:
            return 0.0
        return float(np.sqrt(np.mean(chunk.astype(np.float64) ** 2)))

    def stereo(self, t_sec: float, n: int = 256, span: int = 2048):
        """Return (left, right) sample arrays of length `n` around `t_sec`,
        for a stereo meter / goniometer, or None if not ready."""
        with self._lock:
            lr = self._samples_lr
        if lr is None or len(lr) == 0:
            return None
        center = int(t_sec * MIXER_SR)
        start  = max(0, center - span // 2)
        end    = start + span
        if end > len(lr):
            end = len(lr); start = max(0, end - span)
        chunk = lr[start:end]
        if len(chunk) < span:
            chunk = np.pad(chunk, ((0, span - len(chunk)), (0, 0)))
        idx = np.linspace(0, len(chunk) - 1, n).astype(int)
        sub = chunk[idx]
        return sub[:, 0], sub[:, 1]

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

    def wave(self, t_sec: float, n: int = 512, span: int = 4096):
        """Return `n` time-domain samples (≈ -1..1) around position `t_sec`,
        for an oscilloscope-style visualizer, or None if not ready."""
        with self._lock:
            s = self._samples
        if s is None or len(s) == 0:
            return None
        center = int(t_sec * MIXER_SR)
        start  = max(0, center - span // 2)
        end    = start + span
        if end > len(s):
            end = len(s); start = max(0, end - span)
        chunk = s[start:end]
        if len(chunk) < span:
            chunk = np.pad(chunk, (0, span - len(chunk)))
        idx = np.linspace(0, len(chunk) - 1, n).astype(int)
        return chunk[idx]


def _lerp_color(c1: str, c2: str, t: float) -> str:
    t = max(0.0, min(1.0, t))
    r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
    r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
    return (f"#{int(r1+(r2-r1)*t):02x}"
            f"{int(g1+(g2-g1)*t):02x}"
            f"{int(b1+(b2-b1)*t):02x}")


def _hsv_hex(h: float, s: float, v: float) -> str:
    """HSV (0..1) → #rrggbb hex string."""
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, max(0.0, min(1.0, s)),
                                  max(0.0, min(1.0, v)))
    return f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"


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
        self._vis_items = []          # per-bar/line canvas ids for the active style
        self._vis_levels = None
        self._vis_anim_id = None
        self._vis_n     = 64
        self._wave_n    = 256         # points sampled for the waveform styles
        self._vis_built = False
        self._vis_geom  = {}          # cached geometry for the active style
        self._vis_wave  = None        # canvas line id for waveform styles
        self._vis_modes = ["Bars", "Mirror", "Wave", "Radial",
                           "Stereo", "Particles", "Geometry",
                           "Warp", "Tunnel", "Disco", "Floor", "GIF"]
        self._vis_mode  = 0
        self._vis_hotspots = []       # on-canvas (x0,y0,x1,y1,action) controls
        # Color themes — (lo → hi) gradient; "rainbow" maps hue across bars;
        # "cycle" continuously shifts the whole visualizer through the spectrum.
        self._vis_themes = [
            {"name": "Aqua",    "lo": ACCENT_DK, "hi": ACCENT_HI},
            {"name": "Fire",    "lo": "#5a1200", "hi": "#ffd54a"},
            {"name": "Neon",    "lo": "#3a0ca3", "hi": "#f72585"},
            {"name": "Ice",     "lo": "#0a2a5e", "hi": "#9be3ff"},
            {"name": "Sunset",  "lo": "#42126b", "hi": "#ff9e3d"},
            {"name": "Rainbow", "lo": "#ff0040", "hi": "#40ffd0", "rainbow": True},
            {"name": "Cycle",   "lo": "#ff0040", "hi": "#40ffd0", "cycle": True},
        ]
        self._vis_theme = 0
        self._color_phase = 0.0       # advances each frame for the "Cycle" theme
        # Background colour cycle — independent of the foreground "Cycle" theme:
        # its own phase advances at a different speed and is offset half a wheel,
        # so the background never shows the same colour as the visualizer.
        self._bg_cycle    = False
        self._vis_bg_base = "#050505" # current base background colour each frame
        # Discrete, beat-driven background strobe: the palette is quantised so
        # colours JUMP rather than slide, and beats trigger a bright flash. The
        # half-wheel hue offset keeps it distinct from the "Cycle" theme.
        self._bg_hue_steps = 12
        self._bg_hue_idx   = 0
        self._bg_step_t    = 0
        self._bg_flash     = 0.0
        self._bg_beat_avg  = 0.0
        # Particles / Geometry / beat state
        self._particles = []          # list of particle dicts (oval ids + motion)
        self._shockwaves = []         # expanding beat rings for Particles
        self._part_flash = 0.0        # full-field flash that fires on a beat
        self._geom_rot  = 0.0         # running rotation for Geometry
        self._geom_kick = 0.0         # beat-driven zoom punch for Geometry
        self._geom_rings  = []        # concentric rotating polygons
        self._geom_spikes = []        # radial spikes that fire on beats
        self._beat_avg  = 0.0         # smoothed RMS for beat detection
        # Warp / Tunnel state
        self._stars = []              # 3-D starfield points (Warp)
        self._tunnel_rings = []       # receding rings (Tunnel)
        self._tunnel_phase = 0.0
        # Disco ball state
        self._disco_facets = []       # twinkling mirror facets (rect ids)
        self._disco_beams  = []       # sweeping light-ray line ids
        self._disco_rot    = 0.0
        # Dance floor state
        self._floor_tiles = []        # perspective floor tiles (polygon ids)
        self._floor_phase = 0.0
        # GIF visualizer state
        self._gif_pil       = None    # list of raw PIL frames (None until chosen)
        self._gif_frames    = []      # ImageTk frames sized to the canvas
        self._gif_durations = []      # per-frame duration (ms)
        self._gif_idx       = 0
        self._gif_acc       = 0.0     # time accumulated toward the next frame
        self._gif_item      = None    # canvas image id
        self._gif_size      = None    # (w, h) the current frames were built for
        self._gif_name      = ""
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
        # Cycle between visualizer styles (Bars / Mirror / Wave / Radial)
        self._vis_style_btn = Button(hdr, text="◑  " + self._vis_modes[self._vis_mode],
                               font=(FF, 10, "bold"), bg=BG3, fg=TXT_MID, relief=FLAT,
                               cursor="hand2", padx=12, pady=4,
                               activebackground=BG3, activeforeground=ACCENT,
                               command=self._cycle_vis_mode)
        self._vis_style_btn.pack(side=RIGHT, padx=(0, 8))
        # Cycle color themes (Aqua / Fire / Neon / Ice / Sunset / Rainbow)
        self._vis_theme_btn = Button(hdr, text="🎨  " + self._vis_themes[self._vis_theme]["name"],
                               font=(FF, 10, "bold"), bg=BG3, fg=TXT_MID, relief=FLAT,
                               cursor="hand2", padx=12, pady=4,
                               activebackground=BG3, activeforeground=ACCENT,
                               command=self._cycle_vis_theme)
        self._vis_theme_btn.pack(side=RIGHT, padx=(0, 8))
        # Toggle a flashing color-cycle background (independent of the theme)
        self._vis_bg_btn = Button(hdr, text="🌈  BG Off",
                               font=(FF, 10, "bold"), bg=BG3, fg=TXT_MID, relief=FLAT,
                               cursor="hand2", padx=12, pady=4,
                               activebackground=BG3, activeforeground=ACCENT,
                               command=self._toggle_bg_cycle)
        self._vis_bg_btn.pack(side=RIGHT, padx=(0, 8))
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
            # Click routes through on-canvas controls (Style / Color / Close);
            # a click anywhere else closes. Right-click also cycles the style.
            self._vis_cv.bind("<Button-1>", self._vis_click)
            self._vis_cv.bind("<Button-3>", lambda _e: self._cycle_vis_mode())
            self._vis_cv.bind("<Configure>", lambda _e: self._vis_build())
        self._vis_levels = np.zeros(self._vis_n, dtype=np.float32)
        self._vis_cv.place(x=0, y=0, relwidth=1.0, relheight=1.0)
        # NOTE: Canvas.lift / Canvas.tkraise are aliased to the canvas *item*
        # raise command, so calling them bare raises a TclError. Use the base
        # Misc.tkraise to raise the whole canvas widget in the stacking order.
        Misc.tkraise(self._vis_cv)
        self._vis_build()
        self._vis_tick()

    def _cycle_vis_mode(self):
        """Advance to the next visualizer style and rebuild the canvas."""
        self._vis_mode = (self._vis_mode + 1) % len(self._vis_modes)
        mode = self._vis_modes[self._vis_mode]
        try: self._vis_style_btn.config(text="◑  " + mode)
        except Exception: pass
        if self._vis_levels is not None:
            self._vis_levels[:] = 0.0
        if self._vis_on:
            self._vis_build()
        self.app._status.set(f"Visualizer style: {mode}")

    def _cycle_vis_theme(self):
        """Advance to the next color theme and recolor the visualizer."""
        self._vis_theme = (self._vis_theme + 1) % len(self._vis_themes)
        name = self._vis_themes[self._vis_theme]["name"]
        try: self._vis_theme_btn.config(text="🎨  " + name)
        except Exception: pass
        if self._vis_on:
            self._vis_build()      # rebuild so the waveform line picks up the new color
        self.app._status.set(f"Visualizer theme: {name}")

    def _toggle_bg_cycle(self):
        """Turn the flashing color-cycle background on/off."""
        self._bg_cycle = not self._bg_cycle
        self._update_bg_btn()
        if not self._bg_cycle:
            self._vis_bg_base = "#050505"
            if self._vis_cv:
                try: self._vis_cv.config(bg="#050505")
                except Exception: pass
        if self._vis_on:
            self._vis_build()      # rebuild so the on-canvas BG chip updates
        self.app._status.set("Visualizer background: "
                             + ("color cycle ON" if self._bg_cycle else "off"))

    def _update_bg_btn(self):
        try:
            self._vis_bg_btn.config(
                text="🌈  BG " + ("On" if self._bg_cycle else "Off"),
                fg=ACCENT if self._bg_cycle else TXT_MID)
        except Exception:
            pass

    def _vis_color(self, level: float, i: int) -> str:
        """Color for bar/line `i` at intensity `level`, per the active theme."""
        theme = self._vis_themes[self._vis_theme]
        if theme.get("cycle"):
            hue = (self._color_phase + i * 0.004) % 1.0
            return _hsv_hex(hue, 0.85, 0.35 + 0.65 * level)
        if theme.get("rainbow"):
            hue = (i / max(1, self._vis_n) + level * 0.08) % 1.0
            return _hsv_hex(hue, 0.85, 0.35 + 0.65 * level)
        return _lerp_color(theme["lo"], theme["hi"], level)

    def _theme_hi(self) -> str:
        """Active theme's bright color (animated when the theme cycles)."""
        theme = self._vis_themes[self._vis_theme]
        if theme.get("cycle"):
            return _hsv_hex(self._color_phase, 0.85, 0.95)
        return theme["hi"]

    def _theme_lo(self) -> str:
        """Active theme's dark color (animated when the theme cycles)."""
        theme = self._vis_themes[self._vis_theme]
        if theme.get("cycle"):
            return _hsv_hex((self._color_phase + 0.5) % 1.0, 0.85, 0.45)
        return theme["lo"]

    # ── Visualizer: build the canvas items for the active style ──────────────────
    def _vis_build(self):
        self._vis_built = False
        if not self._vis_cv: return
        cv = self._vis_cv
        cv.delete("all")
        try: cv.config(bg="#050505")   # reset (some modes may tint the background)
        except Exception: pass
        w = cv.winfo_width(); h = cv.winfo_height()
        if w <= 1 or h <= 1: return
        mode = self._vis_modes[self._vis_mode]
        theme = self._vis_themes[self._vis_theme]
        n = self._vis_n
        self._vis_items = []
        self._vis_wave  = None
        self._vis_geom  = {"w": w, "h": h}

        if mode in ("Bars", "Mirror"):
            gap = 3
            bw = (w - gap * (n + 1)) / n
            base_y = int(h * 0.86) if mode == "Bars" else int(h * 0.5)
            self._vis_geom.update(gap=gap, bw=bw, base_y=base_y)
            for i in range(n):
                x0 = gap + i * (bw + gap); x1 = x0 + bw
                self._vis_items.append(
                    cv.create_rectangle(x0, base_y, x1, base_y, fill=ACCENT, width=0))

        elif mode == "Wave":
            m  = self._wave_n
            cy = h * 0.5
            pts = []
            for i in range(m):
                pts += [i * (w / (m - 1)), cy]
            self._vis_wave = cv.create_line(*pts, fill=theme["hi"], width=2, smooth=True)

        elif mode == "Radial":
            cx, cy = w * 0.5, h * 0.52
            R = min(w, h) * 0.16
            ang = np.linspace(0, 2 * np.pi, n, endpoint=False) - np.pi / 2
            self._vis_geom.update(cx=cx, cy=cy, R=R,
                                  cos=np.cos(ang), sin=np.sin(ang))
            for i in range(n):
                self._vis_items.append(
                    cv.create_line(cx, cy, cx, cy, fill=ACCENT, width=3,
                                   capstyle=ROUND))

        elif mode == "Stereo":
            cx = w * 0.5; cy = h * 0.55
            R  = min(w, h) * 0.26
            self._vis_geom.update(cx=cx, cy=cy, R=R, meter_w=max(24, w * 0.05))
            # Goniometer / vectorscope: a single traced line of L/R points.
            m = 256
            self._vis_wave = cv.create_line(cx, cy, cx, cy, fill=theme["hi"],
                                            width=1, smooth=True)
            # Left + right level meters (track + fill rectangles).
            mw = self._vis_geom["meter_w"]
            self._vis_geom["lm_x"] = 0.06 * w
            self._vis_geom["rm_x"] = 0.94 * w - mw
            for key in ("lm", "rm"):
                bx = self._vis_geom[key + "_x"]
                cv.create_rectangle(bx, h * 0.18, bx + mw, h * 0.86,
                                    outline=BORDER, width=1)
                self._vis_geom[key] = cv.create_rectangle(
                    bx, h * 0.86, bx + mw, h * 0.86, fill=theme["hi"], width=0)
            cv.create_text(self._vis_geom["lm_x"] + mw / 2, h * 0.90, text="L",
                           fill=TXT_MID, font=(FF, 11, "bold"))
            cv.create_text(self._vis_geom["rm_x"] + mw / 2, h * 0.90, text="R",
                           fill=TXT_MID, font=(FF, 11, "bold"))

        elif mode == "Particles":
            self._particles = []
            cnt = 200
            cx, cy = w * 0.5, h * 0.5
            spawn_r = min(w, h) * 0.34          # start clustered near the middle
            for i in range(cnt):
                a   = random.uniform(0, 2 * np.pi)
                rad = spawn_r * (random.random() ** 0.5)   # uniform over a disc
                x   = cx + np.cos(a) * rad
                y   = cy + np.sin(a) * rad
                vang = random.uniform(0, 2 * np.pi)
                spd  = random.uniform(0.5, 2.2)
                self._particles.append({
                    "id": cv.create_oval(x, y, x, y, fill=ACCENT, width=0),
                    "x": x, "y": y,
                    "vx": np.cos(vang) * spd, "vy": np.sin(vang) * spd,
                    "base": random.uniform(2.0, 6.0),
                    "spin": random.choice((-1.0, 1.0)),    # swirl direction
                    "kickf": random.uniform(0.7, 1.6),     # per-particle beat kick
                    "hue": i / cnt, "cur_r": -1})
            # Pool of expanding shockwave rings that fire on each beat.
            self._shockwaves = []
            for _i in range(6):
                self._shockwaves.append({
                    "id": cv.create_oval(0, 0, 0, 0, outline="", width=3,
                                         state=HIDDEN),
                    "r": 0.0, "life": 0.0})
            self._part_flash = 0.0
            self._beat_avg = 0.0

        elif mode == "Geometry":
            cx, cy = w * 0.5, h * 0.52
            self._vis_geom.update(cx=cx, cy=cy, R=min(w, h) * 0.16)
            # A kaleidoscopic mandala: several concentric counter-rotating
            # polygons, with a burst of radial spikes that punch out on beats.
            self._geom_rings = []
            for ri in range(5):
                self._geom_rings.append(
                    cv.create_line(cx, cy, cx, cy, fill=theme["hi"], width=2,
                                   smooth=True, joinstyle=ROUND))
            self._geom_spikes = []
            for _i in range(self._vis_n):
                self._geom_spikes.append(
                    cv.create_line(cx, cy, cx, cy, fill=theme["hi"], width=1,
                                   capstyle=ROUND))
            self._geom_rot  = 0.0
            self._geom_kick = 0.0

        elif mode == "Warp":
            cx, cy = w * 0.5, h * 0.5
            self._vis_geom.update(cx=cx, cy=cy)
            # 3-D starfield streaking past the camera; warps on the beat.
            self._stars = []
            for _i in range(180):
                s = self._new_star()
                s["id"] = cv.create_line(cx, cy, cx, cy, fill=WHITE, width=1)
                self._stars.append(s)

        elif mode == "Tunnel":
            cx, cy = w * 0.5, h * 0.5
            maxR = ((w * w + h * h) ** 0.5) * 0.6
            self._vis_geom.update(cx=cx, cy=cy, maxR=maxR)
            # Concentric rings rushing outward — like flying through a tunnel.
            self._tunnel_rings = []
            cnt = 20
            for k in range(cnt):
                self._tunnel_rings.append({
                    "id": cv.create_oval(0, 0, 0, 0, outline=ACCENT, width=2),
                    "p": k / cnt})
            self._tunnel_phase = 0.0

        elif mode == "Disco":
            cx = w * 0.5; cy = h * 0.34
            R  = min(w, h) * 0.16
            self._vis_geom.update(cx=cx, cy=cy, R=R)
            # Hanging wire from the ceiling.
            cv.create_line(cx, 0, cx, cy - R, fill="#2a2a2a", width=2)
            # Light rays first, so the ball sits over their origin.
            self._disco_beams = []
            for _i in range(14):
                self._disco_beams.append(
                    cv.create_line(cx, cy, cx, cy, fill=ACCENT_DK, width=2))
            # Ball body.
            cv.create_oval(cx - R, cy - R, cx + R, cy + R,
                           outline="#202020", width=2, fill="#0a0a0a")
            # Mirror facets — a grid wrapped onto the sphere (smaller toward the
            # poles so it reads as a 3-D ball).
            self._disco_facets = []
            rows = 9
            for ri in range(rows):
                lat = -np.pi / 2 + np.pi * (ri + 0.5) / rows
                y   = cy + R * np.sin(lat)
                rr  = R * np.cos(lat)                     # ring radius here
                cols = max(3, int(rows * np.cos(lat)) + 2)
                fw  = (2 * rr) / cols
                fh  = (R * np.pi / rows)
                for ci in range(cols):
                    fx = cx - rr + fw * (ci + 0.5)
                    self._disco_facets.append({
                        "id": cv.create_rectangle(
                            fx - fw * 0.42, y - fh * 0.42,
                            fx + fw * 0.42, y + fh * 0.42,
                            fill="#1a1a1a", width=0),
                        "hue": random.random(),
                        "phase": random.random()})
            self._disco_rot = 0.0

        elif mode == "Floor":
            cols, rows = 12, 9
            horizon = h * 0.30
            cxf = w * 0.5
            self._vis_geom.update(cols=cols, rows=rows, horizon=horizon)
            self._floor_tiles = []
            def _proj(f):                                 # 0 near .. 1 horizon
                return f / (2.0 - f)                      # perspective easing
            for r in range(rows):
                f0 = _proj(r / rows); f1 = _proj((r + 1) / rows)
                y0 = h - (h - horizon) * f0
                y1 = h - (h - horizon) * f1
                hw0 = (w * 0.5) * (1.0 - 0.78 * f0)
                hw1 = (w * 0.5) * (1.0 - 0.78 * f1)
                for c in range(cols):
                    ca = c / cols; cb = (c + 1) / cols
                    xa0 = cxf - hw0 + 2 * hw0 * ca
                    xa1 = cxf - hw0 + 2 * hw0 * cb
                    xb0 = cxf - hw1 + 2 * hw1 * ca
                    xb1 = cxf - hw1 + 2 * hw1 * cb
                    tid = cv.create_polygon(xa0, y0, xa1, y0, xb1, y1, xb0, y1,
                                            fill="#101010", outline="#060606",
                                            width=1)
                    self._floor_tiles.append(
                        {"id": tid, "r": r, "c": c, "hue": random.random()})
            self._floor_phase = 0.0

        elif mode == "GIF":
            self._gif_item = cv.create_image(w // 2, h // 2, anchor=CENTER)
            if self._gif_pil and _PIL:
                self._prepare_gif_frames(w, h)
                if self._gif_frames:
                    cv.itemconfig(self._gif_item, image=self._gif_frames[0])
            else:
                msg = ("Click  📁  GIF…  above to choose a GIF"
                       if _PIL else
                       "GIF visualizer needs Pillow — pip install pillow")
                cv.create_text(w // 2, h // 2, text=msg,
                               fill=TXT_MID, font=(FF, 14, "bold"))

        # On-canvas controls — a single row of chips pinned to the top-left so
        # they never overlap the centered song title beneath them.
        self._vis_hotspots = []
        chips = [("◑  " + mode,                              "style"),
                 ("🎨  " + self._vis_themes[self._vis_theme]["name"], "theme"),
                 ("🌈  BG " + ("On" if self._bg_cycle else "Off"), "bg")]
        if mode == "GIF":
            chips.append(("📁  GIF…", "gif"))
        chips.append(("✕  Close", "close"))
        cxp = 24
        for label, action in chips:
            cxp = self._vis_chip(cv, cxp, 28, label, action) + 10
        # Song title — centered, sitting clearly below the control row.
        self._vis_title = cv.create_text(
            w // 2, 70, text="", fill=TXT, font=(FF, 16, "bold"))
        self._vis_built = True

    def _vis_chip(self, cv, x, y, label, action):
        """Draw a small clickable control chip; record its hotspot. Returns the
        right edge x so chips can be laid out left-to-right."""
        pad = 11
        tid = cv.create_text(x + pad, y, text=label, anchor=W,
                             fill=TXT_MID, font=(FF, 10, "bold"))
        bx0, by0, bx1, by1 = cv.bbox(tid)
        rx0, ry0, rx1, ry1 = x, by0 - 6, bx1 + pad, by1 + 6
        rect = cv.create_rectangle(rx0, ry0, rx1, ry1, fill=BG3,
                                   outline=BORDER, width=1)
        cv.tag_lower(rect, tid)
        self._vis_hotspots.append((rx0, ry0, rx1, ry1, action))
        return rx1

    # ── Visualizer: per-frame render ─────────────────────────────────────────────
    def _vis_tick(self):
        if not self._vis_on or not self._vis_cv:
            return
        app = self.app
        cv  = self._vis_cv
        w = cv.winfo_width(); h = cv.winfo_height()
        if w <= 1 or h <= 1 or not self._vis_built:
            self._vis_anim_id = app.root.after(40, self._vis_tick)
            return

        song = self._current_song()
        try:
            cv.itemconfig(self._vis_title,
                          text=(song.title or song.name) if song else "Nothing playing")
        except Exception:
            pass

        # Advance the color phase so the "Cycle" theme drifts through the
        # spectrum (~2.8s per full loop at 30fps).
        self._color_phase = (self._color_phase + 0.012) % 1.0

        t = None
        if app._playing and app._song_dur > 0:
            t = min(time.time() - app._play_start, app._song_dur)
        mode = self._vis_modes[self._vis_mode]

        # Base background colour for this frame. When the BG cycle is on it's a
        # punchy strobe: the hue snaps between a fixed palette of colours (no
        # gradual sliding) and each beat slams it to a NEW colour at full bright,
        # decaying fast for a club-strobe feel. Particles manages its own bg so
        # it can flash over this base.
        if self._bg_cycle:
            energy = app._spectrum.energy(t) if t is not None else 0.0
            # Independent beat detector (doesn't disturb the foreground _beat).
            if (energy > self._bg_beat_avg * 1.35 and
                    energy > self._bg_beat_avg + 0.012 and energy > 0.02):
                self._bg_hue_idx = (self._bg_hue_idx +
                                    random.choice((2, 3, 5, 7))) % self._bg_hue_steps
                self._bg_flash = 1.0
            self._bg_beat_avg = self._bg_beat_avg * 0.9 + energy * 0.1
            self._bg_flash *= 0.72                      # quick decay → strobe
            # Keep snapping to new colours on a timer so it stays lively even
            # through quiet passages with no clear beat.
            self._bg_step_t += 1
            if self._bg_step_t >= 8:                    # ~0.27s per colour
                self._bg_step_t = 0
                self._bg_hue_idx = (self._bg_hue_idx + 1) % self._bg_hue_steps
            # Half-wheel offset → never the foreground "Cycle" colour.
            hue = ((self._bg_hue_idx / self._bg_hue_steps) + 0.5) % 1.0
            val = min(1.0, 0.10 + 0.60 * self._bg_flash)
            self._vis_bg_base = _hsv_hex(hue, 0.9, val)
        else:
            self._vis_bg_base = "#050505"
        if mode != "Particles":
            try: cv.config(bg=self._vis_bg_base)
            except Exception: pass

        if mode == "Wave":
            wav = app._spectrum.wave(t, self._wave_n) if t is not None else None
            self._vis_render_wave(cv, w, h, wav)
        elif mode == "Stereo":
            self._vis_render_stereo(cv, w, h, t)
        else:
            bands = app._spectrum.bands(t, n_bands=self._vis_n) if t is not None else None
            lv = self._vis_levels
            if bands is None:
                lv *= 0.85                       # decay to rest when idle / not ready
            else:
                for i in range(self._vis_n):
                    target = float(bands[i])
                    if target > lv[i]:
                        lv[i] = lv[i] + (target - lv[i]) * 0.6   # fast attack
                    else:
                        lv[i] = lv[i] * 0.82 + target * 0.18     # slow release
            if   mode == "Bars":      self._vis_render_bars(cv, w, h, lv)
            elif mode == "Mirror":    self._vis_render_mirror(cv, w, h, lv)
            elif mode == "Radial":    self._vis_render_radial(cv, w, h, lv)
            elif mode == "Particles": self._vis_render_particles(cv, w, h, lv, t)
            elif mode == "Geometry":  self._vis_render_geometry(cv, w, h, lv, t)
            elif mode == "Warp":      self._vis_render_warp(cv, w, h, lv, t)
            elif mode == "Tunnel":    self._vis_render_tunnel(cv, w, h, lv, t)
            elif mode == "Disco":     self._vis_render_disco(cv, w, h, lv, t)
            elif mode == "Floor":     self._vis_render_floor(cv, w, h, lv, t)
            elif mode == "GIF":       self._vis_render_gif(cv, w, h, lv, t)

        self._vis_anim_id = app.root.after(33, self._vis_tick)   # ~30 fps

    def _vis_render_bars(self, cv, w, h, lv):
        g = self._vis_geom
        gap = g["gap"]; bw = g["bw"]; base_y = g["base_y"]
        max_bar = int(h * 0.66)
        for i, rect in enumerate(self._vis_items):
            level = max(0.0, min(1.0, float(lv[i])))
            bh = int(level * max_bar) + 2
            x0 = gap + i * (bw + gap); x1 = x0 + bw
            cv.coords(rect, x0, base_y - bh, x1, base_y)
            cv.itemconfig(rect, fill=self._vis_color(level, i))

    def _vis_render_mirror(self, cv, w, h, lv):
        g = self._vis_geom
        gap = g["gap"]; bw = g["bw"]; cy = g["base_y"]
        max_bar = int(h * 0.40)
        for i, rect in enumerate(self._vis_items):
            level = max(0.0, min(1.0, float(lv[i])))
            bh = int(level * max_bar) + 1
            x0 = gap + i * (bw + gap); x1 = x0 + bw
            cv.coords(rect, x0, cy - bh, x1, cy + bh)
            cv.itemconfig(rect, fill=self._vis_color(level, i))

    def _vis_render_radial(self, cv, w, h, lv):
        g = self._vis_geom
        cx = g["cx"]; cy = g["cy"]; R = g["R"]; cos = g["cos"]; sin = g["sin"]
        max_len = min(w, h) * 0.30
        for i, line in enumerate(self._vis_items):
            level = max(0.0, min(1.0, float(lv[i])))
            ln = level * max_len + 3
            x0 = cx + R * cos[i];        y0 = cy + R * sin[i]
            x1 = cx + (R + ln) * cos[i]; y1 = cy + (R + ln) * sin[i]
            cv.coords(line, x0, y0, x1, y1)
            cv.itemconfig(line, fill=self._vis_color(level, i))

    def _vis_render_wave(self, cv, w, h, wav):
        if self._vis_wave is None: return
        m = self._wave_n
        cy = h * 0.5
        amp = h * 0.34
        dx = w / (m - 1)
        pts = []
        if wav is None:
            for i in range(m):
                pts += [i * dx, cy]
        else:
            for i in range(m):
                pts += [i * dx, cy - float(wav[i]) * amp]
        cv.coords(self._vis_wave, *pts)
        cv.itemconfig(self._vis_wave, fill=self._theme_hi())

    # ── On-canvas control clicks ─────────────────────────────────────────────────
    def _vis_click(self, event):
        for x0, y0, x1, y1, action in self._vis_hotspots:
            if x0 <= event.x <= x1 and y0 <= event.y <= y1:
                if   action == "style": self._cycle_vis_mode()
                elif action == "theme": self._cycle_vis_theme()
                elif action == "bg":    self._toggle_bg_cycle()
                elif action == "gif":   self._load_gif()
                else:                   self._toggle_visualizer()
                return
        self._toggle_visualizer()          # click anywhere else closes

    def _beat(self, t):
        """Adaptive beat flag + RMS energy at position `t` (shared by modes)."""
        energy = self.app._spectrum.energy(t) if t is not None else 0.0
        avg    = self._beat_avg
        beat   = (energy > avg * 1.3 and energy > avg + 0.012 and energy > 0.02)
        self._beat_avg = avg * 0.90 + energy * 0.10
        return beat, energy

    # ── Stereo meter + goniometer ────────────────────────────────────────────────
    def _vis_render_stereo(self, cv, w, h, t):
        g = self._vis_geom
        st = self.app._spectrum.stereo(t, 256) if t is not None else None
        lo, hi = self._theme_lo(), self._theme_hi()
        cx, cy, R = g["cx"], g["cy"], g["R"]
        if st is None:
            cv.coords(self._vis_wave, cx, cy, cx, cy)
            L = Rr = 0.0
        else:
            left, right = st
            # Goniometer: rotate (L,R) 45° → x=(L-R), y=(L+R); classic vectorscope.
            gx = cx + (left - right) * (R * 0.7071)
            gy = cy - (left + right) * (R * 0.7071)
            pts = []
            for i in range(len(left)):
                pts += [float(gx[i]), float(gy[i])]
            cv.coords(self._vis_wave, *pts)
            L  = float(np.sqrt(np.mean(left.astype(np.float64) ** 2)))
            Rr = float(np.sqrt(np.mean(right.astype(np.float64) ** 2)))
        cv.itemconfig(self._vis_wave, fill=hi)
        # Level meters (clamp RMS into a readable range).
        top = h * 0.18; bot = h * 0.86; span = bot - top
        for key, val in (("lm", L), ("rm", Rr)):
            lvl = max(0.0, min(1.0, val * 3.2))
            bx0 = g[key + "_x"]; bx1 = bx0 + g["meter_w"]
            cv.coords(g[key], bx0, bot - lvl * span, bx1, bot)
            cv.itemconfig(g[key], fill=_lerp_color(lo, hi, lvl))

    # ── Audio-reactive particle field ────────────────────────────────────────────
    def _vis_render_particles(self, cv, w, h, lv, t):
        # Particles drift, swirl, and explode outward on beats; a spring keeps
        # them in the visible middle and the edges bounce, so they never migrate
        # off to the corners. Beats also fire a shockwave ring and flash the
        # whole field for an extra-energetic feel.
        beat, energy = self._beat(t)
        cx, cy = w * 0.5, h * 0.5
        home   = min(w, h) * 0.40        # radius they are gently held within
        drive  = 1.0 + energy * 4.5      # global motion speeds up with the music
        theme  = self._vis_themes[self._vis_theme]
        margin = 6
        # Field flash — a quick whole-screen pulse on each beat that decays.
        if beat:
            self._part_flash = min(1.0, self._part_flash + 0.65 + energy)
        self._part_flash *= 0.80
        try:
            if self._part_flash > 0.03:
                cv.config(bg=_lerp_color(self._vis_bg_base, self._theme_lo(),
                                         self._part_flash * 0.5))
            else:
                cv.config(bg=self._vis_bg_base)
        except Exception:
            pass
        for p in self._particles:
            dx = p["x"] - cx; dy = p["y"] - cy
            d  = (dx * dx + dy * dy) ** 0.5 or 1.0
            if beat:                                  # explosive impulse on a beat
                kick = (4.5 + energy * 11.0) * p["kickf"]
                p["vx"] += dx / d * kick; p["vy"] += dy / d * kick
                # Chaotic spark — a random shove so the burst scatters wildly.
                p["vx"] += random.uniform(-1.0, 1.0) * (1.5 + energy * 5.0)
                p["vy"] += random.uniform(-1.0, 1.0) * (1.5 + energy * 5.0)
            # Spring back toward the centre — negligible inside the home radius,
            # growing quickly once a particle strays past it.
            pull = 0.0009 + 0.008 * max(0.0, (d - home) / home)
            p["vx"] -= dx * pull; p["vy"] -= dy * pull
            # Tangential swirl — per-particle direction, spins up with energy, so
            # the field churns into a chaotic vortex instead of a tidy spin.
            sw = (0.08 + energy * 0.32) * p["spin"]
            p["vx"] += -dy / d * sw; p["vy"] += dx / d * sw
            p["x"] += p["vx"] * drive;  p["y"] += p["vy"] * drive
            p["vx"] *= 0.92; p["vy"] *= 0.92          # drag
            # Bounce off the edges instead of wrapping to the far corner.
            if   p["x"] < margin:     p["x"] = margin;     p["vx"] = abs(p["vx"]) * 0.6
            elif p["x"] > w - margin: p["x"] = w - margin; p["vx"] = -abs(p["vx"]) * 0.6
            if   p["y"] < margin:     p["y"] = margin;     p["vy"] = abs(p["vy"]) * 0.6
            elif p["y"] > h - margin: p["y"] = h - margin; p["vy"] = -abs(p["vy"]) * 0.6
            level = max(0.0, min(1.0, energy * 2.2))
            r = max(2, int(p["base"] * (0.8 + energy * 4.5)))
            x, y = p["x"], p["y"]
            cv.coords(p["id"], x - r, y - r, x + r, y + r)
            if r != p["cur_r"]:
                p["cur_r"] = r
            if theme.get("cycle"):
                col = _hsv_hex(self._color_phase + p["hue"] * 0.15,
                               0.8, 0.45 + 0.55 * level)
            elif theme.get("rainbow"):
                col = _hsv_hex(p["hue"] + level * 0.1, 0.8, 0.45 + 0.55 * level)
            else:
                col = _lerp_color(theme["lo"], theme["hi"], 0.35 + 0.65 * level)
            cv.itemconfig(p["id"], fill=col)
        # Shockwave rings — launch one on a beat, expand + fade the live ones.
        if beat:
            for sw in self._shockwaves:
                if sw["life"] <= 0:
                    sw["r"] = min(w, h) * 0.04; sw["life"] = 1.0
                    break
        hi = self._theme_hi()
        for sw in self._shockwaves:
            if sw["life"] > 0:
                sw["r"]   += 9 + energy * 26
                sw["life"] -= 0.045
                rr = sw["r"]
                cv.coords(sw["id"], cx - rr, cy - rr, cx + rr, cy + rr)
                cv.itemconfig(sw["id"], state=NORMAL,
                              outline=_lerp_color(hi, "#050505", 1.0 - sw["life"]),
                              width=max(1, int(sw["life"] * 4)))
            elif cv.itemcget(sw["id"], "state") != "hidden":
                cv.itemconfig(sw["id"], state=HIDDEN)

    # ── Disco ball ───────────────────────────────────────────────────────────────
    def _vis_render_disco(self, cv, w, h, lv, t):
        beat, energy = self._beat(t)
        g = self._vis_geom
        cx, cy = g["cx"], g["cy"]
        theme  = self._vis_themes[self._vis_theme]
        self._disco_rot += 0.02 + energy * 0.06       # spin faster when louder
        rot = self._disco_rot
        # Twinkling facets — a rotating brightness wave plus beat sparkle.
        for f in self._disco_facets:
            b = 0.5 + 0.5 * np.sin(rot * 2.0 + f["phase"] * 6.283)
            b *= (0.35 + 0.9 * min(1.0, 0.25 + energy * 2.2))
            if beat and random.random() < 0.30:
                b = 1.0
            b = max(0.05, min(1.0, b))
            if theme.get("cycle") or theme.get("rainbow"):
                col = _hsv_hex((self._color_phase + f["hue"] * 0.4) % 1.0,
                               0.55, 0.15 + 0.85 * b)
            else:
                col = _lerp_color("#101010", self._theme_hi(), b)
            cv.itemconfig(f["id"], fill=col)
        # Sweeping light rays fanning out from the ball.
        nb = len(self._disco_beams) or 1
        bl = min(w, h) * (0.45 + 0.6 * min(1.0, energy * 2.5))
        for i, beam in enumerate(self._disco_beams):
            a  = rot * 1.3 + i * (2 * np.pi / nb)
            x1 = cx + np.cos(a) * bl
            y1 = cy + np.sin(a) * bl
            cv.coords(beam, cx, cy, x1, y1)
            if theme.get("cycle") or theme.get("rainbow"):
                col = _hsv_hex((self._color_phase + i / nb) % 1.0,
                               0.85, 0.20 + 0.5 * min(1.0, energy * 2.0))
            else:
                col = _lerp_color(self._theme_lo(), self._theme_hi(),
                                  min(1.0, energy * 1.6))
            cv.itemconfig(beam, fill=col)

    # ── Dance floor ──────────────────────────────────────────────────────────────
    def _vis_render_floor(self, cv, w, h, lv, t):
        beat, energy = self._beat(t)
        g = self._vis_geom
        cols, rows = g["cols"], g["rows"]
        theme = self._vis_themes[self._vis_theme]
        self._floor_phase += 0.06 + energy * 0.18
        ph = self._floor_phase
        n  = self._vis_n
        for tile in self._floor_tiles:
            c = tile["c"]; r = tile["r"]
            band = float(lv[int(c / cols * (n - 1))])
            wob  = 0.5 + 0.5 * np.sin(ph - r * 0.55 + c * 0.42)
            b = band * 0.65 + wob * 0.35 * (0.3 + energy * 2.0)
            if beat and ((r + c) % 2 == 0):
                b += 0.5
            b = max(0.0, min(1.0, b))
            if theme.get("cycle") or theme.get("rainbow"):
                col = _hsv_hex((self._color_phase + tile["hue"] * 0.5
                                + c / cols * 0.3) % 1.0, 0.8, 0.10 + 0.9 * b)
            else:
                col = _lerp_color("#0b0b0b", self._theme_hi(), b)
            cv.itemconfig(tile["id"], fill=col)

    # ── GIF playback ─────────────────────────────────────────────────────────────
    def _vis_render_gif(self, cv, w, h, lv, t):
        if not self._gif_frames or self._gif_item is None:
            return
        _beat, energy = self._beat(t)
        # Advance frames faster when the music is energetic.
        self._gif_acc += 33 * (0.6 + energy * 3.5)
        dur = self._gif_durations[self._gif_idx] if self._gif_durations else 80
        if self._gif_acc >= max(20, dur):
            self._gif_acc = 0.0
            self._gif_idx = (self._gif_idx + 1) % len(self._gif_frames)
            cv.itemconfig(self._gif_item, image=self._gif_frames[self._gif_idx])

    def _prepare_gif_frames(self, w, h):
        """Resize the loaded PIL frames to cover the (w, h) canvas, building the
        ImageTk frames lazily and caching them per target size."""
        if not self._gif_pil or not _PIL:
            return
        if self._gif_frames and self._gif_size == (w, h):
            return
        frames = []
        for im in self._gif_pil:
            try:
                fr = im.convert("RGBA")
                iw, ih = fr.size
                scale  = max(w / iw, h / ih)          # cover the canvas
                nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
                fr = fr.resize((nw, nh), Image.LANCZOS)
                left = (nw - w) // 2; top = (nh - h) // 2
                fr = fr.crop((left, top, left + w, top + h))
                frames.append(ImageTk.PhotoImage(fr))
            except Exception:
                pass
        self._gif_frames = frames
        self._gif_size   = (w, h)
        self._gif_idx    = 0
        self._gif_acc    = 0.0

    def _load_gif(self):
        """Let the user pick a GIF (or still image) to use as the visualizer."""
        if not _PIL:
            self.app._status.set("GIF visualizer needs Pillow — pip install pillow")
            return
        path = filedialog.askopenfilename(
            title="Choose a GIF (or image) for the visualizer",
            filetypes=[("Images", "*.gif *.png *.jpg *.jpeg *.webp *.bmp"),
                       ("GIF", "*.gif"), ("All", "*.*")])
        if not path:
            return
        try:
            im = Image.open(path)
            frames, durs = [], []
            try:
                while True:
                    frames.append(im.copy())
                    durs.append(im.info.get("duration", 80) or 80)
                    im.seek(im.tell() + 1)
            except EOFError:
                pass
            if not frames:
                frames = [im.copy()]; durs = [100]
            self._gif_pil       = frames
            self._gif_durations = durs
            self._gif_frames    = []
            self._gif_size      = None
            self._gif_idx       = 0
            self._gif_acc       = 0.0
            self._gif_name      = Path(path).name
            self.app._status.set(
                f"Loaded GIF: {self._gif_name}  ({len(frames)} frame"
                f"{'s' if len(frames) != 1 else ''})")
            if self._vis_on and self._vis_modes[self._vis_mode] == "GIF":
                self._vis_build()
        except Exception as ex:
            self.app._status.set(f"Couldn't load GIF: {ex}")

    # ── Audio-reactive rotating geometry (kaleidoscopic mandala) ─────────────────
    def _vis_render_geometry(self, cv, w, h, lv, t):
        g = self._vis_geom
        cx, cy, R = g["cx"], g["cy"], g["R"]
        beat, energy = self._beat(t)
        # Beat-driven zoom punch that decays each frame for an explosive pulse.
        if beat:
            self._geom_kick = min(1.4, self._geom_kick + 0.7 + energy)
        self._geom_kick *= 0.85
        kick = self._geom_kick
        self._geom_rot += 0.03 + energy * 0.22 + kick * 0.06
        rot = self._geom_rot
        n = self._vis_n
        Rscale = min(w, h) * 0.30
        n_rings = len(self._geom_rings)
        # Concentric counter-rotating reactive polygons.
        for ri, ring in enumerate(self._geom_rings):
            direction = 1 if ri % 2 == 0 else -1
            spin = rot * (1.0 + ri * 0.45) * direction
            rad0 = R * (0.45 + ri * 0.34) * (1.0 + kick * 0.55)
            amp  = Rscale * (0.45 + ri * 0.16)
            off  = ri * 7
            pts  = []
            for i in range(n):
                level = max(0.0, min(1.0, float(lv[(i + off) % n])))
                a  = spin + (i / n) * 2 * np.pi
                rr = rad0 + level * amp + kick * 28
                pts += [cx + rr * np.cos(a), cy + rr * np.sin(a)]
            pts += pts[:2]                            # close the loop
            cv.coords(ring, *pts)
            peak = max(0.0, min(1.0, energy * 1.8 + kick * 0.5))
            cv.itemconfig(ring, fill=self._vis_color(peak, (ri * 11) % n),
                          width=1 + int(kick * 3))
        # Radial spikes that punch outward on the beat.
        spike_len = min(w, h) * (0.16 + kick * 0.55)
        for i, sp in enumerate(self._geom_spikes):
            level = max(0.0, min(1.0, float(lv[i])))
            a  = -rot * 1.7 + (i / n) * 2 * np.pi
            r0 = R * 0.3
            r1 = r0 + (level * 0.45 + kick) * spike_len + level * 38
            cv.coords(sp, cx + r0 * np.cos(a), cy + r0 * np.sin(a),
                          cx + r1 * np.cos(a), cy + r1 * np.sin(a))
            cv.itemconfig(sp, fill=self._vis_color(level, i),
                          width=1 + int(level * 3 + kick * 2))

    # ── 3-D warp starfield ───────────────────────────────────────────────────────
    def _new_star(self):
        """A fresh star at a random direction and full depth."""
        return {"x": random.uniform(-1, 1), "y": random.uniform(-1, 1),
                "z": random.uniform(0.1, 1.0), "id": None}

    def _vis_render_warp(self, cv, w, h, lv, t):
        beat, energy = self._beat(t)
        g = self._vis_geom; cx, cy = g["cx"], g["cy"]
        scale = min(w, h) * 0.55
        speed = 0.008 + energy * 0.07
        if beat:
            speed += 0.06                              # warp jump on the beat
        theme = self._vis_themes[self._vis_theme]
        hi = self._theme_hi()
        for s in self._stars:
            pz = s["z"]
            s["z"] -= speed
            if s["z"] <= 0.03:                         # flew past — respawn far off
                s["x"] = random.uniform(-1, 1)
                s["y"] = random.uniform(-1, 1)
                s["z"] = 1.0; pz = 1.0
            z  = s["z"]
            sx = cx + (s["x"] / z) * scale
            sy = cy + (s["y"] / z) * scale
            px = cx + (s["x"] / pz) * scale
            py = cy + (s["y"] / pz) * scale
            cv.coords(s["id"], px, py, sx, sy)
            bright = max(0.0, min(1.0, (1.0 - z) * 1.3))
            if theme.get("cycle") or theme.get("rainbow"):
                col = _hsv_hex((self._color_phase + s["x"] * 0.2) % 1.0,
                               0.7 - 0.5 * bright, 0.4 + 0.6 * bright)
            else:
                col = _lerp_color(self._theme_lo(), hi, bright)
            cv.itemconfig(s["id"], fill=col, width=1 + int(bright * 2.5))

    # ── Tunnel — rings rushing outward ───────────────────────────────────────────
    def _vis_render_tunnel(self, cv, w, h, lv, t):
        beat, energy = self._beat(t)
        g = self._vis_geom; cx, cy = g["cx"], g["cy"]; maxR = g["maxR"]
        spd = 0.004 + energy * 0.035
        if beat:
            spd += 0.025
        self._tunnel_phase += spd
        bass = float(np.mean(lv[:6]))                  # low-end drives the sway
        ox = np.sin(self._tunnel_phase * 2.0) * 36 * bass
        oy = np.cos(self._tunnel_phase * 1.7) * 36 * bass
        n = self._vis_n
        for ring in self._tunnel_rings:
            p   = (ring["p"] + self._tunnel_phase) % 1.0
            rad = maxR * (p * p)                       # perspective easing
            cv.coords(ring["id"], cx + ox - rad, cy + oy - rad,
                                  cx + ox + rad, cy + oy + rad)
            band = float(lv[int(p * (n - 1))])
            col  = self._vis_color(max(0.0, min(1.0, p * 0.5 + band * 0.7)),
                                   int(p * n))
            cv.itemconfig(ring["id"], outline=col, width=1 + int(p * 5))

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
        self._gen_all_btn = Button(bar, text="♪  Generate Lyrics", font=(FF, 10, "bold"),
               bg=BG3, fg=TXT, relief=FLAT, cursor="hand2", padx=14, pady=7,
               activebackground=BG4, activeforeground=ACCENT,
               command=self._generate_all_lyrics)
        self._gen_all_btn.pack(side=RIGHT, padx=(6, 0))

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

    # ── Bulk lyrics generation ──────────────────────────────────────────────────
    def _generate_all_lyrics(self):
        """Look up lyrics for every library song that doesn't have them yet."""
        if getattr(self, "_lyrics_all_running", False):
            self._status.set("Already generating lyrics — please wait…"); return
        if not self.songs:
            self._status.set("No songs in your library yet"); return
        todo = [s for s in self.songs if not s.failed and
                (s.path not in self.lyrics_db or
                 not self.lyrics_db[s.path].get("lyrics"))]
        if not todo:
            self._status.set("All songs already have lyrics  ♪"); return
        if not messagebox.askyesno(
                "Generate Lyrics for All",
                f"Look up lyrics for {len(todo)} song(s) that don't have them yet?\n\n"
                f"This fetches from the internet and may take a little while."):
            return
        self._lyrics_all_running = True
        try: self._gen_all_btn.config(state=DISABLED, text="♪  Generating…")
        except Exception: pass
        threading.Thread(target=self._generate_all_lyrics_bg,
                         args=(todo,), daemon=True).start()

    def _generate_all_lyrics_bg(self, todo):
        found, total = 0, len(todo)
        for i, song in enumerate(todo, 1):
            # Another worker may have filled these in the meantime — skip if so.
            if (song.path in self.lyrics_db and
                    self.lyrics_db[song.path].get("lyrics")):
                continue
            self.root.after(0, lambda i=i, s=song: self._status.set(
                f"♪ Generating lyrics {i}/{total}: {s.title or s.name}…"))
            lyrics = fetch_lyrics(song.title or song.name, song.artist)
            if lyrics:
                self.lyrics_db[song.path] = {"lyrics": lyrics, "source": "auto"}
                save_lyrics_db(self.lyrics_db)
                self._uq.put(song)
                found += 1
            time.sleep(0.4)   # be polite to the free lyrics APIs
        self.root.after(0, lambda: self._generate_all_lyrics_done(found, total))

    def _generate_all_lyrics_done(self, found, total):
        self._lyrics_all_running = False
        try: self._gen_all_btn.config(state=NORMAL, text="♪  Generate Lyrics")
        except Exception: pass
        save_library(self.songs)
        self._status.set(f"♪ Lyrics found for {found} of {total} song(s)")

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
