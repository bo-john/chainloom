import time
from pathlib import Path
from threading import Event

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHeaderView,
    QInputDialog,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from ..diagnostics import record_exception
from ..errors import UserError
from ..time_format import format_beijing_time
from ..vault import MAX_IMPORT_BYTES, WalletVault, parse_import
from .widgets import box, row, text


class VaultPasswordDialog(QDialog):
    def __init__(self, creating, parent=None):
        super().__init__(parent)
        self.setWindowTitle("创建钱包库" if creating else "解锁钱包库")
        self.setMinimumWidth(460)
        layout = box(self, margins=(26, 24, 26, 24), spacing=14)
        layout.addWidget(text("设置钱包库密码" if creating else "输入钱包库密码", "brand"))
        layout.addWidget(
            text(
                "私钥将加密保存在此 Mac。密码遗忘后无法找回，请妥善保存。"
                if creating
                else "解锁后可以导入、搜索和整理本地钱包。",
                "muted",
                True,
            )
        )
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setMaxLength(256)
        self.password.setPlaceholderText("至少 12 个字符" if creating else "钱包库密码")
        layout.addWidget(self.password)
        self.repeat = QLineEdit()
        self.repeat.setEchoMode(QLineEdit.EchoMode.Password)
        self.repeat.setMaxLength(256)
        self.repeat.setPlaceholderText("再次输入密码")
        self.repeat.setVisible(creating)
        layout.addWidget(self.repeat)
        self.error = text("", "muted", True)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("创建" if creating else "解锁")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")

        def accept():
            if creating and not 12 <= len(self.password.text()) <= 256:
                self.error.setText("密码请使用 12–256 个字符。")
            elif creating and self.password.text() != self.repeat.text():
                self.error.setText("两次输入的密码不一致。")
            elif not self.password.text():
                self.error.setText("请输入钱包库密码。")
            else:
                self.accept()

        buttons.accepted.connect(accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def clear_secrets(self):
        self.password.clear()
        self.repeat.clear()

    def reject(self):
        self.clear_secrets()
        super().reject()


class ChangeVaultPasswordDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("修改钱包库密码")
        self.setMinimumWidth(460)
        layout = box(self, margins=(26, 24, 26, 24), spacing=14)
        layout.addWidget(text("修改钱包库密码", "brand"))
        layout.addWidget(text(
            "全部钱包将使用新密码重新加密。修改成功后钱包库会锁定，请使用新密码解锁。"
            "新密码请使用 12–256 个字符，遗忘后无法恢复。", "muted", True
        ))
        self.current_password = QLineEdit()
        self.new_password = QLineEdit()
        self.repeat = QLineEdit()
        for entry, placeholder in (
            (self.current_password, "当前密码"),
            (self.new_password, "新密码（至少 12 个字符）"),
            (self.repeat, "再次输入新密码"),
        ):
            entry.setEchoMode(QLineEdit.EchoMode.Password)
            entry.setMaxLength(256)
            entry.setPlaceholderText(placeholder)
            layout.addWidget(entry)
        self.error = text("", "muted", True)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("修改密码")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName("primary")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self):
        if not self.current_password.text():
            self.error.setText("请输入当前密码。")
        elif not 12 <= len(self.new_password.text()) <= 256:
            self.error.setText("新密码请使用 12–256 个字符。")
        elif self.new_password.text() != self.repeat.text():
            self.error.setText("两次输入的新密码不一致。")
        elif self.current_password.text() == self.new_password.text():
            self.error.setText("新密码不能与当前密码相同。")
        else:
            super().accept()

    def clear_secrets(self):
        self.current_password.clear()
        self.new_password.clear()
        self.repeat.clear()

    def reject(self):
        self.clear_secrets()
        super().reject()


class WalletImportDialog(QDialog):
    def __init__(self, groups, parent=None, group_id=None):
        super().__init__(parent)
        self.setWindowTitle("批量导入钱包")
        self.resize(660, 500)
        self._source = ""
        layout = box(self, margins=(24, 24, 24, 24), spacing=14)
        layout.addWidget(text("导入已有私钥", "brand"))
        layout.addWidget(
            text(
                "TXT：每行一个私钥。CSV：表头使用 private_key,name,address；"
                "除 private_key 外均可省略。地址会由私钥校验。",
                "muted",
                True,
            )
        )
        paste = self.paste_button = QPushButton("从剪贴板粘贴")
        paste.clicked.connect(self.paste_source)
        file = QPushButton("选择 TXT / CSV 文件")
        file.clicked.connect(self.choose_file)
        layout.addWidget(row(paste, file, None))
        self.reveal = QCheckBox("显示并编辑导入内容")
        self.reveal.toggled.connect(self._toggle_source)
        layout.addWidget(self.reveal)
        self.editor = QPlainTextEdit()
        self.editor.setUndoRedoEnabled(False)
        self.editor.setReadOnly(True)
        self.editor.setPlaceholderText("点击上方按钮粘贴或选择文件。导入内容默认隐藏。")
        layout.addWidget(self.editor, 1)
        self.group = QComboBox()
        self.group.setEditable(False)
        for item in groups:
            self.group.addItem(item["name"], item["id"])
        selected = self.group.findData(group_id)
        if selected >= 0:
            self.group.setCurrentIndex(selected)
        layout.addWidget(row(text("导入分组", "fieldLabel"), self.group))
        layout.addWidget(
            text("本次新增钱包统一归入所选分组。如需新分组，请先在钱包管理中新建。", "muted", True)
        )
        layout.addWidget(
            text("仅在本机处理。无效行和重复钱包会跳过；中途取消不会写入本批钱包。", "muted", True)
        )
        self.error = text("", "importFeedback", True)
        self.error.hide()
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.submit_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.submit_button.setText("校验并加密导入")
        self.submit_button.setObjectName("primary")
        self.submit_button.setEnabled(False)
        self.editor.textChanged.connect(self._source_changed)
        self.group.currentIndexChanged.connect(self._source_changed)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _feedback(self, message, severity="error"):
        self.error.setProperty("severity", severity)
        self.error.style().unpolish(self.error)
        self.error.style().polish(self.error)
        self.error.setText(message)
        self.error.show()

    def _source_changed(self):
        self.submit_button.setEnabled(
            bool(self.content().strip()) and self.group.currentData() is not None
        )

    def paste_source(self):
        self.set_source(QApplication.clipboard().text())

    def accept(self):
        try:
            parse_import(self.content())
            if self.group.currentData() is None:
                raise UserError("请选择导入分组；如无分组，请先在钱包管理中新建。")
        except UserError as error:
            record_exception("校验钱包导入内容", error)
            self._feedback(str(error))
            return
        super().accept()

    def set_source(self, content):
        if len(content.encode("utf-8")) > MAX_IMPORT_BYTES:
            self.clear_secrets()
            self._feedback("文件或粘贴内容不能超过 1 MB，请分批导入。")
            return
        self._source = content
        if self.reveal.isChecked():
            self.editor.setPlainText(content)
        else:
            count = len([line for line in content.splitlines() if line.strip()])
            self.editor.setPlainText(f"已载入 {count} 行内容。勾选“显示并编辑”可查看。")
        self._source_changed()
        if content.strip():
            self._feedback("内容已载入，尚未导入。请点击下方“校验并加密导入”。", "info")
        else:
            self._feedback("未读取到内容。请先复制私钥或 CSV 内容，再点击“从剪贴板粘贴”。")

    def _toggle_source(self, visible):
        if visible:
            self.editor.setPlainText(self._source)
            self.editor.setReadOnly(False)
        else:
            self._source = self.editor.toPlainText()
            self.editor.setReadOnly(True)
            self.set_source(self._source)

    def content(self):
        return self.editor.toPlainText() if self.reveal.isChecked() else self._source

    def choose_file(self):
        filename, _ = QFileDialog.getOpenFileName(
            self, "选择钱包导入文件", "", "钱包列表 (*.txt *.csv)"
        )
        if not filename:
            return
        try:
            path = Path(filename)
            if path.stat().st_size > MAX_IMPORT_BYTES:
                raise UserError("文件不能超过 1 MB，请分批导入。")
            self.set_source(path.read_text(encoding="utf-8-sig"))
        except UserError as error:
            record_exception("读取钱包导入文件", error)
            self.clear_secrets()
            self._feedback(str(error))
        except (OSError, UnicodeError) as error:
            record_exception("读取钱包导入文件", error)
            self.clear_secrets()
            self._feedback("无法读取文件，请选择 UTF-8 编码的 TXT 或 CSV。")

    def clear_secrets(self):
        self._source = ""
        self.editor.clear()

    def reject(self):
        self.clear_secrets()
        super().reject()


class WalletManagementPage(QWidget):
    state_changed = Signal(str)
    batch_requested = Signal(list)
    import_progress = Signal(int, int, int)
    AUTO_LOCK_SECONDS = 300

    def __init__(self, directory, run_task, parent=None):
        super().__init__(parent)
        self.setObjectName("content")
        self.vault = WalletVault(directory)
        self.run_task = run_task
        self.busy = False
        self.operation = None
        self.operation_id = 0
        self.active_dialog = None
        self.notification_dialog = None
        self._shutting_down = False
        self.rows = []
        self.group_rows = []
        self._touch()
        self._build()
        self.import_progress.connect(self._progress)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._check_idle)
        self.timer.start()
        # Filter only the focused native window. An application-wide Python
        # filter also wraps partially constructed internal QObjects,
        # which can crash PySide in getWrapperForQObject before eventFilter runs.
        self._activity_window = None
        application = QApplication.instance()
        self._activity_connection = application.focusWindowChanged.connect(
            self._watch_activity_window
        )
        self._watch_activity_window(application.focusWindow())
        self.refresh()

    def _build(self):
        layout = box(self, margins=(32, 30, 32, 26), spacing=15)
        self.open_button = QPushButton()
        self.open_button.setObjectName("primary")
        self.open_button.clicked.connect(self.open_vault)
        self.lock_button = QPushButton("锁定钱包库")
        self.lock_button.clicked.connect(self.lock)
        layout.addWidget(row(text("钱包管理", "heading"), None, self.open_button, self.lock_button))
        self.description = text(
            "在本机加密保存和整理钱包，转账时从钱包库选择发送钱包。", "muted", True
        )
        layout.addWidget(self.description)
        self.status = text("", "fieldLabel")
        layout.addWidget(self.status)
        self.progress_bar = QProgressBar()
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)
        self.locked_hint = text("", "muted", True)
        layout.addWidget(self.locked_hint)
        self.tools = QWidget()
        tools = box(self.tools, spacing=12)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索钱包名称、完整地址或分组")
        self.search.textChanged.connect(self.filter_wallets)
        self.group_filter = QComboBox()
        self.group_filter.currentIndexChanged.connect(self.filter_wallets)
        tools.addWidget(row(self.search, self.group_filter))
        self.import_button = QPushButton("批量导入")
        self.import_button.setObjectName("primary")
        self.import_button.clicked.connect(self.import_wallets)
        self.new_group_button = QPushButton("新建分组")
        self.new_group_button.clicked.connect(self.create_group)
        self.assign_group_button = QPushButton("调整分组")
        self.assign_group_button.clicked.connect(self.assign_group)
        self.rename_button = QPushButton("重命名")
        self.rename_button.clicked.connect(self.rename_wallet)
        self.copy_button = QPushButton("复制地址")
        self.copy_button.clicked.connect(self.copy_address)
        self.batch_button = QPushButton("批量转账")
        self.batch_button.clicked.connect(
            lambda: self.batch_requested.emit(self.selected_addresses())
        )
        tools.addWidget(
            row(
                self.import_button,
                self.new_group_button,
                self.assign_group_button,
                self.rename_button,
                self.copy_button,
                self.batch_button,
                None,
            )
        )
        layout.addWidget(self.tools)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["钱包名称", "钱包地址", "分组", "导入时间（北京时间）"]
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.setWordWrap(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.table, 1)
        self.count = text("", "muted")
        layout.addWidget(self.count)
        self.security_hint = text(
            "私钥加密保存；名称、地址与分组以明文保存在本机。闲置 5 分钟后自动锁定。",
            "muted",
            True,
        )
        layout.addWidget(self.security_hint)
        self.locked_spacer_index = layout.count()
        layout.addStretch()

    def _touch(self):
        self.last_activity = time.monotonic()
        self.last_wall_activity = time.time()

    def _watch_activity_window(self, window):
        if self._activity_window is not None:
            try:
                self._activity_window.removeEventFilter(self)
            except RuntimeError:
                # The previous native window may already have been destroyed.
                pass
        self._activity_window = window
        if window is not None:
            window.installEventFilter(self)

    def eventFilter(self, watched, event):
        if self.vault.unlocked and event.type() in {
            QEvent.Type.KeyPress,
            QEvent.Type.MouseButtonPress,
            QEvent.Type.Wheel,
        }:
            self._touch()
        return super().eventFilter(watched, event)

    def _check_idle(self):
        elapsed = max(time.monotonic() - self.last_activity, time.time() - self.last_wall_activity)
        if self.vault.unlocked and elapsed >= self.AUTO_LOCK_SECONDS:
            self.lock()
            self.notify("长时间未操作，钱包库已自动锁定。")

    def notify(self, message, severity="info"):
        titles = {"info": "提示", "success": "操作成功", "warning": "请注意", "error": "操作失败"}
        return self._show_notification(titles[severity], message, severity)

    def _show_notification(self, title, message, severity, duration_ms=2000):
        if self._shutting_down:
            return None
        if self.notification_dialog is not None:
            self.notification_dialog.reject()
        icons = {
            "info": QMessageBox.Icon.Information,
            "success": QMessageBox.Icon.Information,
            "warning": QMessageBox.Icon.Warning,
            "error": QMessageBox.Icon.Critical,
        }
        dialog = QMessageBox(icons[severity], title, title, QMessageBox.StandardButton.Ok, self)
        dialog.setStyleSheet("QLabel#qt_msgbox_informativelabel { min-width: 340px; }")
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setInformativeText(message)
        dialog.button(QMessageBox.StandardButton.Ok).setText("关闭")
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        timer = QTimer(dialog)
        timer.setSingleShot(True)
        timer.timeout.connect(dialog.accept)

        def finished():
            timer.stop()
            if self.notification_dialog is dialog:
                self.notification_dialog = None

        dialog.finished.connect(finished)
        self.notification_dialog = dialog
        dialog.show()
        timer.start(duration_ms)
        return dialog

    def refresh(self):
        unlocked = self.vault.unlocked
        state = "已解锁" if unlocked else "已锁定" if self.vault.exists else "未创建"
        self.status.setText("本地钱包库 · " + state + (" · 正在处理，请稍候…" if self.busy else ""))
        self.state_changed.emit(state)
        self.open_button.setText("解锁钱包库" if self.vault.exists else "创建钱包库")
        self.open_button.setVisible(not unlocked)
        self.open_button.setEnabled(not self.busy)
        self.lock_button.setVisible(unlocked or self.busy)
        self.lock_button.setText("取消操作并锁定" if self.busy else "锁定钱包库")
        self.description.setVisible(unlocked)
        self.status.setVisible(unlocked or self.busy)
        self.count.setVisible(unlocked)
        self.security_hint.setVisible(unlocked)
        self.locked_hint.setVisible(not unlocked)
        self.layout().setStretch(self.locked_spacer_index, 0 if unlocked else 1)
        self.locked_hint.setText(
            "钱包库已锁定，解锁后可查看和管理钱包。"
            if self.vault.exists
            else "创建钱包库后，即可导入和管理钱包。"
        )
        self.tools.setVisible(unlocked)
        self.tools.setEnabled(not self.busy)
        self.table.setVisible(unlocked)
        self.table.setEnabled(not self.busy)
        self.table.clearSelection()
        if unlocked:
            selected_group = self.group_filter.currentData()
            try:
                self.group_rows = self.vault.groups()
            except (UserError, OSError):
                self.vault.lock()
                self.refresh()
                self.notify("钱包库无法读取，已锁定。请保留原文件并检查磁盘或备份。")
                return
            self.group_filter.blockSignals(True)
            self.group_filter.clear()
            self.group_filter.addItem("全部分组", None)
            for group in self.group_rows:
                self.group_filter.addItem(group["name"], group["id"])
            index = self.group_filter.findData(selected_group)
            self.group_filter.setCurrentIndex(max(0, index))
            self.group_filter.blockSignals(False)
            self.filter_wallets()
        else:
            self.rows = []
            self.group_rows = []
            self.table.setRowCount(0)
            self.search.clear()
            self.group_filter.clear()
            self.count.setText("解锁后显示钱包列表。")

    def filter_wallets(self, *_):
        if not self.vault.unlocked:
            return
        try:
            self.rows = self.vault.wallets(self.search.text(), self.group_filter.currentData())
        except (UserError, OSError):
            self.vault.lock()
            self.refresh()
            self.notify("钱包库无法读取，已锁定。请保留原文件并检查磁盘或备份。")
            return
        self.table.setRowCount(len(self.rows))
        for index, wallet in enumerate(self.rows):
            for column, value in enumerate(
                (
                    wallet["name"],
                    wallet["address"],
                    wallet["group_name"],
                    format_beijing_time(wallet["created_at"]),
                )
            ):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                item.setData(Qt.ItemDataRole.UserRole, wallet["address"])
                self.table.setItem(index, column, item)
            self.table.setRowHeight(index, 46)
        self.count.setText(f"当前显示 {len(self.rows)} 个钱包。可多选后统一调整分组。")
        self._selection_changed()

    def selected_addresses(self):
        return [
            self.table.item(item.row(), 1).data(Qt.ItemDataRole.UserRole)
            for item in self.table.selectionModel().selectedRows()
        ]

    def _selection_changed(self):
        addresses = self.selected_addresses()
        self.assign_group_button.setEnabled(bool(addresses))
        self.rename_button.setEnabled(len(addresses) == 1)
        self.copy_button.setEnabled(bool(addresses))
        self.batch_button.setEnabled(bool(addresses))

    def _run(self, operation, success, failure_title=None, success_after_cancel=False):
        self.busy = True
        self.operation = operation
        self.refresh()

        def completed(value):
            self.busy = False
            self.progress_bar.hide()
            self.operation = None
            if operation.is_set() and not success_after_cancel:
                self.vault.lock()
                self.notify("操作已停止，钱包库已锁定。")
            else:
                self._touch()
                success(value)
            self.refresh()

        def failed(error):
            self.busy = False
            self.operation = None
            self.progress_bar.hide()
            message = (
                str(error)
                if isinstance(error, UserError)
                else "操作未完成，请检查本地数据目录和可用磁盘空间后重试。"
            )
            if failure_title:
                self._import_result(failure_title, message, "error")
            else:
                self.notify(message, "error")
            self.refresh()

        return completed, failed

    def open_vault(self):
        if self.busy:
            return
        creating = not self.vault.exists
        dialog = VaultPasswordDialog(creating, self)
        self.active_dialog = dialog
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            secret = dialog.password.text()
        finally:
            dialog.clear_secrets()
            dialog.deleteLater()
            self.active_dialog = None
        operation = Event()
        done, failed = self._run(
            operation,
            lambda _: self.notify("钱包库已创建并解锁。" if creating else "钱包库已解锁。"),
        )
        action = self.vault.create if creating else self.vault.unlock

        def work():
            return action(secret, cancelled=operation)

        self.run_task(work, success=done, failure=failed)

    def change_password(self):
        if self.busy:
            self.notify("请等待当前钱包库操作完成后再修改密码。")
            return
        if not self.vault.exists:
            self.notify("请先在钱包管理中创建钱包库。")
            return
        dialog = ChangeVaultPasswordDialog(self)
        self.active_dialog = dialog
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            current, new = dialog.current_password.text(), dialog.new_password.text()
        finally:
            dialog.clear_secrets()
            dialog.deleteLater()
            self.active_dialog = None
        operation = Event()

        def changed(_):
            self.vault.lock()
            self.notify("钱包库密码已修改，钱包库已锁定。请使用新密码解锁。", "success")

        # A cancellation arriving after the database commit must still report
        # that the new password took effect.
        done, failed = self._run(operation, changed, success_after_cancel=True)
        self.operation_id += 1
        stamp = self.operation_id
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setFormat("正在重新加密钱包：%v / %m")
        self.progress_bar.show()
        self.notify("正在重新加密钱包，请稍候；可在钱包管理中取消操作并锁定。")

        def work():
            return self.vault.change_password(
                current, new, cancelled=operation,
                progress=lambda count, total: self.import_progress.emit(stamp, count, total),
            )

        self.run_task(work, success=done, failure=failed)

    def lock(self):
        if self.notification_dialog is not None:
            self.notification_dialog.reject()
        if self.operation is not None:
            self.operation.set()
        self.vault.lock()
        for dialog in QApplication.topLevelWidgets():
            if isinstance(dialog, QDialog) and self.isAncestorOf(dialog):
                dialog.reject()
        self.refresh()
        self.notify("钱包库已锁定。" + ("正在停止后台加密操作…" if self.busy else ""))

    def shutdown(self):
        self._shutting_down = True
        self.timer.stop()
        if self._activity_connection is not None:
            QObject.disconnect(self._activity_connection)
            self._activity_connection = None
        self._watch_activity_window(None)
        self.lock()

    def import_wallets(self):
        if not self.vault.unlocked or self.busy:
            return
        dialog = WalletImportDialog(
            self.vault.groups(), self, group_id=self.group_filter.currentData()
        )
        self.active_dialog = dialog
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            rows = parse_import(dialog.content())
            group_id = dialog.group.currentData()
        except (UserError, ValueError) as error:
            record_exception("解析批量导入内容", error)
            self._import_result(
                "导入失败",
                str(error)
                if isinstance(error, UserError)
                else "无法解析导入内容，请检查文件格式。",
                "error",
            )
            return
        finally:
            dialog.clear_secrets()
            dialog.deleteLater()
            self.active_dialog = None
        operation = Event()
        self.operation_id += 1
        stamp = self.operation_id
        done, failed = self._run(operation, self._imported, failure_title="导入失败")
        self.notify(f"正在校验并加密 {len(rows)} 行内容，请稍候。完成后将显示导入结果。")
        self.progress_bar.setRange(0, len(rows))
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("正在校验并加密：%v / %m 行（%p%）")
        self.progress_bar.show()

        def work():
            return self.vault.import_wallets(
                rows,
                group_id=group_id,
                cancelled=operation,
                progress=lambda count, total: self.import_progress.emit(stamp, count, total),
            )

        self.run_task(work, success=done, failure=failed)

    def _progress(self, stamp, count, total):
        if (
            self.busy
            and stamp == self.operation_id
            and self.operation
            and not self.operation.is_set()
        ):
            self.progress_bar.setMaximum(total)
            self.progress_bar.setValue(count)

    def _imported(self, report):
        if report.added or report.duplicates:
            # Imported or existing wallets must remain visible after filtering.
            self.search.clear()
            self.group_filter.setCurrentIndex(0)
        if report.added:
            title = "导入完成" if not report.invalid else "导入完成，部分内容未导入"
            severity = "success" if not report.invalid else "warning"
        else:
            title = "未导入任何新钱包"
            severity = "error" if report.invalid else "warning"
        message = (
            f"新增 {report.added} 个，重复 {report.duplicates} 个，无效 {len(report.invalid)} 行。"
        )
        if report.added:
            message += "\n列表已刷新，并显示全部分组的钱包。"
        elif report.duplicates:
            message += "\n重复钱包已存在于钱包库中，不会重复添加。"
        if report.invalid:
            message += "\n" + "；".join(
                f"第 {line} 行：{reason}" for line, reason in report.invalid[:8]
            )
            if len(report.invalid) > 8:
                message += "；其余无效行请检查后重新导入。"
        self._import_result(title, message, severity)

    def _import_result(self, title, message, severity):
        self.import_result_dialog = self._show_notification(title, message, severity)

    def create_group(self):
        name, accepted = QInputDialog.getText(self, "新建分组", "分组名称")
        if accepted:
            try:
                self.vault.create_group(name)
                self.refresh()
            except UserError as error:
                record_exception("创建钱包分组", error)
                self.notify(str(error))

    def assign_group(self):
        addresses = self.selected_addresses()
        if not addresses:
            return
        names = [group["name"] for group in self.group_rows]
        name, accepted = QInputDialog.getItem(
            self, "调整分组", f"将选中的 {len(addresses)} 个钱包移入：", names, 0, False
        )
        if accepted:
            try:
                group_id = next(group["id"] for group in self.group_rows if group["name"] == name)
                self.vault.update_wallets(addresses, group_id=group_id)
                self.refresh()
            except UserError as error:
                record_exception("调整钱包分组", error)
                self.notify(str(error))

    def rename_wallet(self):
        addresses = self.selected_addresses()
        if len(addresses) != 1:
            return
        current = next(wallet["name"] for wallet in self.rows if wallet["address"] == addresses[0])
        name, accepted = QInputDialog.getText(self, "重命名钱包", "钱包名称", text=current)
        if accepted:
            try:
                self.vault.update_wallets(addresses, name=name)
                self.refresh()
            except UserError as error:
                record_exception("修改钱包名称", error)
                self.notify(str(error))

    def copy_address(self):
        QApplication.clipboard().setText("\n".join(self.selected_addresses()))
        self.notify("已复制所选钱包的公开地址。")
