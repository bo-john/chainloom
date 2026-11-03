import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QLockFile, QStandardPaths, QTimer
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from ..backup import active_directory, select_directory
from ..diagnostics import (
    LOGGER,
    close_logging,
    configure_logging,
    exception_hooks,
    record_exception,
)
from ..errors import UserError
from .macos import relaunch_as_app
from .settings import load_preferences
from .shutdown import TerminationSignals
from .window import MainWindow


def main(argv=None):
    parser = argparse.ArgumentParser(description="Chainloom macOS desktop application")
    parser.add_argument("--data-dir", type=Path, help="Override local application data directory")
    parser.add_argument("--screenshot", type=Path, help="Save an actual window screenshot and exit")
    parser.add_argument("--log-dir", type=Path, help="Override the standard desktop log directory")
    args = parser.parse_args(argv)
    relaunch_as_app(sys.argv[1:] if argv is None else argv)
    QApplication.setApplicationName("Chainloom")
    QApplication.setApplicationDisplayName("Chainloom")
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Chainloom")
    app.setOrganizationName("Chainloom")
    app.setApplicationVersion("0.1.0")
    app.setFont(QFont("PingFang SC", 12))
    icon = Path(__file__).parent / "assets" / "icon.png"
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    try:
        logfile = configure_logging(args.log_dir)
    except OSError:
        QMessageBox.critical(None, "无法创建日志", "无法写入日志文件夹，请检查目录权限或磁盘空间。")
        return 1
    try:
        with exception_hooks():
            try:
                directory = (
                    args.data_dir
                    or Path(
                        QStandardPaths.writableLocation(
                            QStandardPaths.StandardLocation.AppLocalDataLocation
                        )
                    )
                ).resolve()
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                lock = QLockFile(str(directory / "desktop.lock"))
                if not lock.tryLock(0):
                    QMessageBox.information(
                        None, "Chainloom 已在运行", "请使用已打开的 Chainloom 窗口。"
                    )
                    return 1
                try:
                    return _run_directories(app, args, directory, logfile)
                finally:
                    lock.unlock()
            except Exception as error:  # noqa: BLE001 - log startup failures without payloads
                record_exception("应用启动或运行", error)
                message = (
                    str(error)
                    if isinstance(error, UserError)
                    else "详细错误已写入日志：\n" + str(logfile)
                )
                QMessageBox.critical(None, "应用启动失败", message)
                return 1
    finally:
        LOGGER.info("桌面应用已退出")
        close_logging()


def _run_directories(app, args, entry_directory, logfile):
    directory = active_directory(entry_directory)
    while True:
        # Keep the launch directory locked too, so two app instances cannot change
        # its active-directory selection or operate on the same restored data.
        lock = None
        if directory != entry_directory:
            lock = QLockFile(str(directory / "desktop.lock"))
            if not lock.tryLock(0):
                raise UserError("恢复的数据目录正在被另一个 Chainloom 使用，请先关闭对应窗口。")
        try:
            result, next_directory = _run(app, args, directory, logfile, entry_directory)
        finally:
            if lock is not None:
                lock.unlock()
        if next_directory is None:
            return result
        directory = next_directory


def _run(app, args, directory, logfile, entry_directory=None):
    try:
        preferences = load_preferences(directory)
    except ValueError as error:
        record_exception("读取应用设置", error)
        QMessageBox.critical(None, "无法读取设置", str(error))
        return 1, None
    window = MainWindow(directory, preferences)
    window.log_directory = logfile.parent
    window.log_path_label.setText("错误日志：\n" + str(logfile))
    if args.screenshot:

        def capture():
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            if not window.grab().save(str(args.screenshot)):
                app.exit(1)
            else:
                app.exit(0)

        QTimer.singleShot(1800, capture)
    signals = TerminationSignals(window.request_shutdown, app)
    try:
        if entry_directory is not None and active_directory(entry_directory) != directory:
            select_directory(entry_directory, directory)
        window.show()
        result = app.exec()
    finally:
        window.closing = True
        window.stop_event.set()
        window.pool.waitForDone()
        window.batch_page.shutdown()
        window.wallet_page.shutdown()
        signals.close()
        window.deleteLater()
        app.processEvents()
    return result, window.restart_directory


if __name__ == "__main__":
    raise SystemExit(main())
