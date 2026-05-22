# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for EditorAnalyzer — single-file Windows .exe.

Build with:
    uv run pyinstaller editor_analyzer.spec

The ffmpeg binary from imageio-ffmpeg is bundled automatically so the end user
needs neither Python nor a separate ffmpeg installation.
"""
import imageio_ffmpeg

ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[(ffmpeg_exe, ".")],
    datas=[],
    hiddenimports=[
        "customtkinter",
        "PIL",
        "PIL._tkinter_finder",
        "librosa",
        "numpy",
        "scipy",
        "soundfile",
        "audioread",
        "aaf2",
        "imageio_ffmpeg",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="EditorAnalyzer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,   # no console window — desktop app
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
