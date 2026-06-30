# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the AoE4 Replay Launcher release build.

Build (from the repo root, inside the project's venv)::

    pyinstaller packaging/AoE4-Replay-Launcher.spec --noconfirm

Produces ``dist/AoE4-Replay-Launcher/`` (onedir): the exe plus an ``_internal``
folder. Distributed as a zip with LICENSE / THIRD_PARTY_NOTICES.md / README.md
alongside the exe.

Design notes:
  * onedir (not onefile): faster start, simpler antivirus story, lets us drop the
    license files next to the exe.
  * windowed (no console): GUI app; double-click opens the panel.
  * The external tools (restic, DepotDownloader, RunAsDate) are NOT bundled —
    they are downloaded from their official sources on first use, keeping the
    package small and avoiding redistribution of third-party binaries.
  * assets/ and cldr_datetime.json are bundled under ``aoe4replay/`` so the
    package's ``Path(__file__).with_name(...)`` lookups resolve unchanged.
  * config.example.toml + the initial build map are bundled under ``seed_data/``;
    config._seed_runtime_data() copies them into the writable data root on first
    run (the build map then self-updates from the network).
"""

import os
import sys

from PyInstaller.utils.hooks import collect_data_files

ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))  # noqa: F821 - SPECPATH injected by PyInstaller
SRC = os.path.join(ROOT, "src", "aoe4replay")

datas = collect_data_files("customtkinter")  # themes / fonts loaded at runtime
datas += [
    (os.path.join(SRC, "assets"), "aoe4replay/assets"),
    (os.path.join(SRC, "cldr_datetime.json"), "aoe4replay"),
    (os.path.join(ROOT, "config.example.toml"), "seed_data"),
    (os.path.join(ROOT, "data", "aoe4-build-map.json"), "seed_data"),
    (os.path.join(ROOT, "data", "aoe4-manifest-history.json"), "seed_data"),
    # Native Steam launch shim (compiled separately to packaging/steamshim.exe).
    # Bundled at the root so it lands in _internal/ (sys._MEIPASS) for deployment
    # to %LocalAppData% on first wrapper install.
    (os.path.join(ROOT, "packaging", "steamshim.exe"), "."),
]

binaries = []

# Tcl/Tk lives in the *base* Python, not the venv, so PyInstaller's automatic
# _tkinter hook can drop tkinter entirely when building from a venv. Collect the
# interpreter's Tcl/Tk runtime explicitly so the frozen build can open windows.
# Bundled under the names PyInstaller's bootloader expects (_tcl_data/_tk_data).
_base = os.path.join(sys.base_prefix)
_tcl_root = os.path.join(_base, "tcl")
if os.path.isdir(_tcl_root):
    for _name in os.listdir(_tcl_root):
        _full = os.path.join(_tcl_root, _name)
        if not os.path.isdir(_full):
            continue
        if _name.startswith("tcl8"):
            datas.append((_full, "_tcl_data"))
        elif _name.startswith("tk8"):
            datas.append((_full, "_tk_data"))
_dlls = os.path.join(_base, "DLLs")
if os.path.isdir(_dlls):
    for _name in os.listdir(_dlls):
        if _name.endswith(".dll") and (_name.startswith("tcl") or _name.startswith("tk")):
            binaries.append((os.path.join(_dlls, _name), "."))

# PyInstaller's modulegraph drops the pure-Python ``tkinter`` package when
# building from this venv, so bundle the package source directly into the
# distribution (importable because _internal is on sys.path at runtime). The
# ``_tkinter`` C-extension is pulled in as a registered hidden import below so
# that ``import _tkinter`` resolves at runtime.
import tkinter as _tkinter_pkg  # noqa: E402

datas.append((os.path.dirname(_tkinter_pkg.__file__), "tkinter"))

hiddenimports = ["PIL._tkinter_finder", "_tkinter"]

excludes = ["babel", "pytest", "_pytest", "ruff"]

icon = os.path.join(SPECPATH, "app.ico")  # noqa: F821 - SPECPATH injected by PyInstaller

a = Analysis(
    [os.path.join(SPECPATH, "launcher_main.py")],  # noqa: F821
    pathex=[os.path.join(ROOT, "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    # Keep this exe's name EQUAL to --packTitle (both "AoE4 Replay Launcher"):
    # Velopack's root launcher stub and the inner exe must share a name, or an
    # update leaves a duplicate stub in the install root. So the root stub and the
    # inner exe are both "AoE4 Replay Launcher.exe" (users run the one in the main
    # folder; the identical one in current/ is the engine). Keep this name stable.
    name="AoE4 Replay Launcher",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon if os.path.isfile(icon) else None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="AoE4-Replay-Launcher",
)
