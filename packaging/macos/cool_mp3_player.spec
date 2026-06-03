# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the macOS build of Cool MP3 Player (LEAN profile).
# Produces "Cool MP3 Player.app"; build.command then wraps it into a .dmg.
#
# Lean = online lyrics + karaoke + visualizer. The optional offline AI
# transcription stack (PyTorch / Demucs / faster-whisper) is EXCLUDED to keep
# the app small and the build reliable. player.py degrades gracefully without it.
import os

PROJECT_ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))

EXCLUDES = [
    "torch", "torchaudio", "torchvision", "demucs", "openunmix", "julius",
    "lameenc", "faster_whisper", "whisper", "ctranslate2", "onnxruntime",
    "onnxruntime_tools", "transformers", "tokenizers", "huggingface_hub",
    "safetensors", "sentencepiece", "av", "soundfile", "librosa", "scipy",
    "matplotlib", "pandas", "sympy", "networkx", "IPython", "notebook",
    "pytest", "setuptools",
]

a = Analysis(
    [os.path.join(PROJECT_ROOT, "player.py")],
    pathex=[PROJECT_ROOT],
    binaries=[],
    datas=[],
    hiddenimports=["PIL._tkinter_finder"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Cool MP3 Player",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                # UPX is unreliable on macOS dylibs — leave off
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,         # builds for the Mac you run it on (Intel or Apple Silicon)
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Cool MP3 Player",
)

app = BUNDLE(
    coll,
    name="Cool MP3 Player.app",
    icon=None,                # drop a .icns path here if you add an app icon
    bundle_identifier="com.coolmp3.player",
    info_plist={
        "CFBundleName": "Cool MP3 Player",
        "CFBundleDisplayName": "Cool MP3 Player",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1.0.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
        "LSApplicationCategoryType": "public.app-category.music",
    },
)
