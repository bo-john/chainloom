# Third-party software

The desktop interface uses unmodified, dynamically linked PySide6 / Qt 6.10.2,
copyright The Qt Company Ltd. and contributors. The LGPLv3 and referenced GPLv3
texts are included in `licenses/`. Qt/PySide source and build information:

- https://code.qt.io/pyside/pyside-setup.git/tag/?h=v6.10.2
- https://code.qt.io/cgit/qt/qt5.git/tag/?h=v6.10.2
- https://doc.qt.io/qtforpython-6/licenses.html
- https://doc.qt.io/qt-6/licenses-used-in-qt.html

Qt and PySide shared libraries remain separate files in the macOS application
bundle. Nothing in this application's own terms restricts modification of these
libraries or reverse engineering to debug modifications as permitted by their
licenses. Modifying a macOS bundle invalidates its original signature; an altered
local build must be signed again separately.

Other Python dependencies retain their upstream distribution
metadata and license terms. PyInstaller's bootloader is distributed under the
GPL with its special exception for bundled applications:
https://pyinstaller.org/en/stable/license.html
