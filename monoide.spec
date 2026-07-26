# PyInstaller spec for a single-file monoide.exe.
#
#   pyinstaller --noconfirm --clean monoide.spec
#
# The web/ folder is bundled as data and resolved at runtime through
# sys._MEIPASS (see ide/server.py::_web_dir).

import os

block_cipher = None

hiddenimports = []
try:
    import winpty  # noqa: F401  - bundled only when installed
    hiddenimports.append("winpty")
except Exception:
    pass

analysis = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    # web/ = editor UI, vendor/notion2api = the API shim started by
    # ide/supervisor.py (copied to %LOCALAPPDATA%\monoide at first run).
    datas=[("web", "web"), ("vendor/notion2api", "vendor/notion2api")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Nothing scientific, GUI or test-related is used: dropping these keeps the
    # exe small and the resident set low.
    excludes=[
        "tkinter", "turtle", "unittest", "pydoc_data", "lib2to3",
        "numpy", "pandas", "scipy", "matplotlib", "PIL",
        "setuptools", "pip", "wheel", "distutils",
        "sqlite3", "dbm", "curses", "idlelib", "ensurepip",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(analysis.pure, analysis.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.zipfiles,
    analysis.datas,
    [],
    name="monoide",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=os.environ.get("MONOIDE_NOCONSOLE", "0") != "1",
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
