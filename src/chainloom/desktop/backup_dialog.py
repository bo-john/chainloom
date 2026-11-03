"""Backup/restore workflow, with an idle barrier before taking a snapshot."""

from datetime import datetime
from pathlib import Path
from threading import Event
from uuid import uuid4

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QWidget,
)

from ..backup import create_backup, read_backup, restore_backup
from ..errors import UserError
from ..time_format import BEIJING_TIMEZONE, format_beijing_time
from .widgets import box, row, text


class BackupDialog(QDialog):
    def __init__(self, window, *, restoring=False):
        super().__init__(window)
        self.window = window
        self.restoring = restoring
        self.working = False
        self.maintenance = False
        self.cancelled = Event()
        self.verified = None
        self.restored = None
        self.setWindowTitle("恢复备份" if restoring else "创建加密备份")
        self.setMinimumWidth(640)
        layout = box(self, margins=(26, 24, 26, 24), spacing=14)
        layout.addWidget(text(self.windowTitle(), "brand"))
        layout.addWidget(
            text(
                "恢复到新目录，保留当前数据。校验后可查看备份摘要，再决定是否恢复。"
                if restoring
                else "备份钱包库、交易及批次记录和已保存设置。开始后暂停后续发送、锁定钱包库，"
                "并等待后台操作结束。",
                "muted",
                True,
            )
        )
        self.path = QLineEdit()
        self.path.setReadOnly(True)
        self.path.setPlaceholderText("选择备份包" if restoring else "选择保存位置")
        self.browse = QPushButton("选择文件" if restoring else "保存到…")
        self.browse.clicked.connect(self.choose_file)
        layout.addWidget(row(self.path, self.browse))
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setMaxLength(256)
        self.password.setPlaceholderText("备份包密码（8–256 个字符）")
        layout.addWidget(self.password)
        self.repeat = QLineEdit()
        self.repeat.setEchoMode(QLineEdit.EchoMode.Password)
        self.repeat.setMaxLength(256)
        self.repeat.setPlaceholderText("再次输入备份包密码")
        self.repeat.setVisible(not restoring)
        layout.addWidget(self.repeat)
        layout.addWidget(
            text(
                "备份包密码用于解密备份；恢复后的钱包库仍使用备份时的钱包库密码。请分别保管。",
                "muted",
                True,
            )
        )
        self.summary = QWidget()
        self.summary_form = QFormLayout(self.summary)
        self.summary.hide()
        layout.addWidget(self.summary)
        self.target = QWidget()
        target_layout = box(self.target, margins=(0, 0, 0, 0), spacing=8)
        target_layout.addWidget(text("恢复位置（在所选文件夹内新建独立数据目录）", "fieldLabel"))
        self.parent_path = QLineEdit()
        self.parent_path.setReadOnly(True)
        self.choose_parent = QPushButton("选择文件夹…")
        self.choose_parent.clicked.connect(self.choose_directory)
        target_layout.addWidget(row(self.parent_path, self.choose_parent))
        self.acknowledge = QCheckBox(
            "恢复后先核对链上状态，再确认剩余付款；备份之后的交易可能未记录。"
        )
        target_layout.addWidget(self.acknowledge)
        self.target.hide()
        layout.addWidget(self.target)
        self.feedback = text("", "fieldLabel", True)
        layout.addWidget(self.feedback)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.start = self.buttons.addButton(
            "校验并预览" if restoring else "创建加密备份",
            QDialogButtonBox.ButtonRole.ActionRole,
        )
        self.start.setObjectName("primary")
        self.start.clicked.connect(self.submit)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def choose_file(self):
        if self.restoring:
            name, _ = QFileDialog.getOpenFileName(
                self, "选择备份包", "", "Chainloom 加密备份 (*.chainloom-backup)"
            )
        else:
            stamp = datetime.now(BEIJING_TIMEZONE).strftime("%Y%m%d-%H%M%S")
            name, _ = QFileDialog.getSaveFileName(
                self,
                "保存加密备份",
                f"Chainloom-{stamp}.chainloom-backup",
                "Chainloom 加密备份 (*.chainloom-backup)",
            )
            if name and not name.endswith(".chainloom-backup"):
                name += ".chainloom-backup"
        if name:
            self.path.setText(name)

    def choose_directory(self):
        name = QFileDialog.getExistingDirectory(self, "选择恢复位置")
        if name:
            self.parent_path.setText(name)

    def submit(self):
        if self.working:
            return
        if self.verified is not None:
            if not self.parent_path.text() or not self.acknowledge.isChecked():
                self.feedback.setText("请选择恢复位置，并确认恢复后先核对链上状态。")
                return
            stamp = datetime.now(BEIJING_TIMEZONE).strftime("%Y%m%d-%H%M%S")
            target = Path(self.parent_path.text()) / f"Chainloom-Restored-{stamp}-{uuid4().hex[:8]}"
            self.run(
                lambda: restore_backup(self.verified, target, self.cancelled), self.restored_data
            )
            return
        if not self.path.text():
            self.feedback.setText("请先选择备份文件。" if self.restoring else "请先选择保存位置。")
            return
        password = self.password.text()
        if not 8 <= len(password) <= 256:
            self.feedback.setText("备份密码请使用 8–256 个字符。")
            return
        if not self.restoring and password != self.repeat.text():
            self.feedback.setText("两次输入的备份密码不一致。")
            return
        path = Path(self.path.text())
        self.password.clear()
        self.repeat.clear()
        if self.restoring:
            self.run(lambda: read_backup(path, password, self.cancelled), self.preview)
        else:
            self.run(
                lambda: create_backup(self.window.directory, path, password, self.cancelled),
                self.backed_up,
            )

    def begin_maintenance(self):
        if self.maintenance:
            return
        self.maintenance = True
        window = self.window
        window.maintenance = True
        window.poll_timer.stop()
        window.balance_timer.stop()
        window.batch_page.timer.stop()
        window.batch_page.cancel_collection()
        if window.batch_page.executing_id:
            window.batch_page.service.pause(
                window.batch_page.executing_id, "备份恢复操作，已暂停后续发送，请核对后手动继续。"
            )
            window.batch_page.executing_id = None
        window.wallet_page.lock(notify=False)

    def run(self, function, success):
        self.working = True
        self.cancelled.clear()
        self.start.setEnabled(False)
        self.browse.setEnabled(False)
        self.choose_parent.setEnabled(False)
        self.password.setEnabled(False)
        self.repeat.setEnabled(False)
        self.progress.show()
        self.feedback.setText("正在暂停后续操作，等待后台任务结束…")
        self.begin_maintenance()

        def when_idle():
            if self.window.closing:
                return
            if self.cancelled.is_set():
                self.failed(UserError("备份恢复操作已取消。"))
                return
            if self.window.workers or self.window.pool.activeThreadCount():
                QTimer.singleShot(100, when_idle)
                return
            self.feedback.setText("正在校验或写入数据，请稍候…")
            self.window._task(function, success=success, failure=self.failed, maintenance=True)

        QTimer.singleShot(0, when_idle)

    def idle(self):
        self.working = False
        self.progress.hide()
        self.start.setEnabled(True)
        self.browse.setEnabled(self.verified is None)
        self.choose_parent.setEnabled(True)
        self.password.setEnabled(self.verified is None)
        self.repeat.setEnabled(True)

    def failed(self, error):
        self.idle()
        self.feedback.setText(
            str(error)
            if isinstance(error, UserError)
            else "操作未完成，请检查文件、目录权限和磁盘空间后重试。"
        )
        if self.cancelled.is_set():
            self.reject()

    def backed_up(self, summary):
        self.idle()
        QMessageBox.information(
            self,
            "备份完成",
            f"加密备份已保存并通过校验。\n钱包：{summary.wallets} 个；交易：{summary.transactions} 笔。\n"
            f"保存位置：{self.path.text()}\n请单独保管备份包密码和钱包库密码。",
        )
        self.accept()

    def preview(self, backup):
        self.verified = backup
        self.idle()
        summary = backup.summary
        for label, value in (
            ("备份时间（北京时间）", format_beijing_time(summary.created_at)),
            ("应用版本", summary.app_version),
            ("钱包 / 分组", f"{summary.wallets} / {summary.groups}"),
            ("交易 / 待核对交易", f"{summary.transactions} / {summary.pending_transactions}"),
            ("批次 / 未完成批次", f"{summary.batches} / {summary.unfinished_batches}"),
            ("网络与资产设置", "已包含" if summary.has_settings else "使用默认设置"),
        ):
            self.summary_form.addRow(text(label, "fieldLabel"), text(str(value)))
        self.summary.show()
        self.target.show()
        self.password.hide()
        self.repeat.hide()
        self.start.setText("恢复到新目录")
        self.feedback.setText("备份校验通过。恢复后钱包库保持锁定，未完成批次保持暂停。")

    def restored_data(self, target):
        self.restored = target
        self.idle()
        self.verified = None
        self.start.setEnabled(False)
        use = QMessageBox.question(
            self,
            "恢复完成",
            f"数据已恢复到：\n{target}\n\n现在切换到恢复的数据吗？"
            "下次启动也将使用该目录，原数据会保留。\n"
            "请使用备份时的钱包库密码解锁，并先核对链上交易状态。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if use == QMessageBox.StandardButton.Yes:
            self.window.restart_directory = target
        self.accept()

    def reject(self):
        if self.working:
            self.cancelled.set()
            self.feedback.setText("正在停止操作，请等待当前校验结束…")
            return
        super().reject()

    def finish(self):
        self.password.clear()
        self.repeat.clear()
        self.verified = None
        if self.maintenance:
            self.window.maintenance = False
            if not self.window.closing:
                self.window.poll_timer.start()
                self.window.batch_page.timer.start()
                self.window.load_history()
                self.window.batch_page.reload_history()


def open_backup(window, *, restoring=False):
    if window.maintenance or window.closing:
        return
    if not restoring and not window.wallet_page.vault.exists:
        window.notify("请先创建钱包库，再备份本地数据。")
        return
    dialog = BackupDialog(window, restoring=restoring)
    try:
        dialog.exec()
    finally:
        dialog.finish()
        dialog.deleteLater()
    if window.restart_directory is not None:
        window.request_shutdown()
