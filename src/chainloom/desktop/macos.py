"""Give source launches a real macOS application bundle and executable identity."""

import os
import plistlib
import shutil
import sys
from pathlib import Path


def relaunch_as_app(argv):
    """Keep the source command, but run Qt from Chainloom.app rather than Python.

    Dock derives its tooltip from the executable/bundle registered at launch;
    changing Qt, NSBundle or LaunchServices names afterwards is insufficient.
    The local bundle reuses the current Python environment and source paths.
    Frozen releases already have their own application bundle.
    """
    if sys.platform != "darwin" or getattr(sys, "frozen", False):
        return
    executable = Path(sys.executable)
    if executable.name == "Chainloom" and executable.parent.name == "MacOS":
        return

    bundle = Path(sys.prefix) / ".chainloom" / "Chainloom.app"
    contents = bundle / "Contents"
    binaries = contents / "MacOS"
    resources = contents / "Resources"
    binaries.mkdir(parents=True, exist_ok=True)
    resources.mkdir(exist_ok=True)
    target = binaries / "Chainloom"
    source = executable.resolve()
    if not target.exists() or (
        target.stat().st_size != source.stat().st_size
        or target.stat().st_mtime_ns != source.stat().st_mtime_ns
    ):
        temporary = binaries / "Chainloom.tmp"
        shutil.copy2(source, temporary)
        temporary.replace(target)

    # uv's interpreter links libpython relative to its executable. Reuse its
    # existing library directory, without modifying the shared interpreter.
    libraries = contents / "lib"
    original_libraries = Path(sys.base_prefix) / "lib"
    if not libraries.is_symlink():
        libraries.symlink_to(original_libraries, target_is_directory=True)
    elif libraries.resolve() != original_libraries.resolve():
        libraries.unlink()
        libraries.symlink_to(original_libraries, target_is_directory=True)

    icon = Path(__file__).parent / "assets" / "icon.icns"
    if icon.exists():
        shutil.copy2(icon, resources / "icon.icns")
    with (contents / "Info.plist").open("wb") as stream:
        plistlib.dump({
            "CFBundleName": "Chainloom",
            "CFBundleDisplayName": "Chainloom",
            "CFBundleExecutable": "Chainloom",
            "CFBundleIdentifier": "app.chainloom.desktop.source",
            "CFBundlePackageType": "APPL",
            "CFBundleIconFile": "icon.icns",
            "CFBundleVersion": "1",
            "NSHighResolutionCapable": True,
        }, stream)

    # Preserve the virtual environment and editable source imports, including
    # source launches made with `python -m` or an IDE rather than the CLI.
    bootstrap = (
        "import sys; "
        f"sys.path[:] = {sys.path!r}; "
        f"sys.prefix = {sys.prefix!r}; sys.exec_prefix = {sys.exec_prefix!r}; "
        f"sys.base_prefix = {sys.base_prefix!r}; "
        f"sys.base_exec_prefix = {sys.base_exec_prefix!r}; "
        "from chainloom.desktop.app import main; raise SystemExit(main())"
    )
    os.execv(str(target), [str(target), "-c", bootstrap, *argv])
