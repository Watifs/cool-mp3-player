# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Windows build of Cool MP3 Player (LEAN profile).
#
# Lean = online lyrics + karaoke + visualizer work; the optional offline AI
# transcription stack (PyTorch / Demucs / faster-whisper) is deliberately
# EXCLUDED so the .exe stays small (~200-300 MB) and the build is reliable.
# player.py already degrades gracefully when those libraries are absent.
import os

PROJECT_ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))

# Heavy / irrelevant packages we never want pulled into the lean bundle.
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
    a.binaries,
    a.datas,
    [],
    name="Cool MP3 Player",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,            # windowed app — no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,                # drop a .ico path here if you add an app icon
)
