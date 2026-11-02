"""Build a local .app and optionally a .dmg; never publishes or uploads anything."""

import argparse
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dmg", action="store_true")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("Build the macOS bundle on macOS")
    root = Path(__file__).resolve().parents[1]
    subprocess.run([sys.executable, str(root / "scripts/build_icon.py")], check=True, cwd=root)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            str(root / "packaging/Chainloom.spec"),
        ],
        check=True,
        cwd=root,
    )
    bundle = root / "dist/Chainloom.app"
    print(f"App: {bundle}")
    if args.dmg:
        with tempfile.TemporaryDirectory(prefix="chainloom-dmg-") as temporary:
            folder = Path(temporary)
            shutil.copytree(bundle, folder / bundle.name, symlinks=True)
            (folder / "Applications").symlink_to("/Applications")
            target = root / f"dist/Chainloom-0.1.0-{platform.machine()}.dmg"
            subprocess.run(
                [
                    "/usr/bin/hdiutil",
                    "create",
                    "-volname",
                    "Chainloom",
                    "-srcfolder",
                    str(folder),
                    "-ov",
                    "-format",
                    "UDZO",
                    str(target),
                ],
                check=True,
            )
            print(f"DMG: {target}")


if __name__ == "__main__":
    main()
