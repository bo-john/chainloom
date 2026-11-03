# Build on macOS with the target Python architecture. Signing is optional for local builds.
import os
from pathlib import Path

root = Path(SPECPATH).parent
assets = root / "src" / "chainloom" / "desktop" / "assets"
identity = os.environ.get("CHAINLOOM_SIGNING_IDENTITY") or None
analysis = Analysis(
    [str(root / "packaging" / "launcher.py")],
    pathex=[str(root / "src")],
    datas=[(str(assets), "chainloom/desktop/assets")],
    hiddenimports=[],
    excludes=[
        "tkinter",
        "eth_tester",
        "pytest",
        "ruff",
        "PySide6.QtQuick",
        "PySide6.QtQml",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebEngineQuick",
        "PySide6.QtWebChannel",
    ],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="Chainloom",
    console=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=identity,
    entitlements_file=str(root / "packaging" / "macos-entitlements.plist"),
)
collection = COLLECT(exe, analysis.binaries, analysis.datas, name="Chainloom")
app = BUNDLE(
    collection,
    name="Chainloom.app",
    icon=str(assets / "icon.icns"),
    bundle_identifier="app.chainloom.desktop",
    info_plist={
        "CFBundleName": "Chainloom",
        "CFBundleDisplayName": "Chainloom",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "1",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "13.0",
        "NSHumanReadableCopyright": "Chainloom",
    },
)
