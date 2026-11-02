"""Batch transfer editor, approval and recoverable execution view."""

import csv
import io
from decimal import Decimal, localcontext
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGridLayout,
    QHeaderView,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from ..batch import (
    BATCH_STATES,
    ITEM_STATES,
    MAX_BATCH_BYTES,
    MAX_BATCH_ROWS,
    MODES,
    parse_manifest,
)
from ..errors import UserError
from ..time_format import format_beijing_time
from .batch_service import BatchService
from .service import format_units, friendly_error
from .settings import NETWORKS
from .widgets import box, row, text


def item(value):
    cell = QTableWidgetItem(str(value))
    cell.setToolTip(str(value))
    return cell


def configure_table(table, labels, editable=False):
    table.setColumnCount(len(labels))
    table.setHorizontalHeaderLabels(labels)
    table.verticalHeader().hide()
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.setWordWrap(False)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    if not editable:
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)


class ChecklistHeader(QHeaderView):
    select_requested = Signal(bool)

    def __init__(self, parent):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.checkbox = QCheckBox(self.viewport())
        self.checkbox.setTristate(True)
        self.checkbox.setAccessibleName("全选当前页")
        self.checkbox.setToolTip(
            "全选 / 取消全选当前页（含滚动区域内的行）。勾选仅用于删除，转账包含全部明细。"
        )
        self.checkbox.clicked.connect(
            lambda: self.select_requested.emit(
                self.checkbox.checkState() != Qt.CheckState.Unchecked
            )
        )
        self.sectionResized.connect(self.position_checkbox)
        self.geometriesChanged.connect(self.position_checkbox)

    def position_checkbox(self, *_):
        size = self.checkbox.sizeHint()
        self.checkbox.resize(size)
        self.checkbox.move(
            self.sectionViewportPosition(0) + (self.sectionSize(0) - size.width()) // 2,
            (self.height() - size.height()) // 2,
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.position_checkbox()


class WalletPicker(QDialog):
    def __init__(self, wallets, parent, *, title="选择发送钱包"):
        super().__init__(parent)
        self.wallets = wallets
        self.setWindowTitle(title)
        self.resize(760, 500)
        layout = box(self, margins=(24, 24, 24, 24))
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索名称、地址或分组")
        self.group = QComboBox()
        self.group.addItem("全部分组", "")
        for group in sorted({wallet["group_name"] for wallet in wallets}):
            self.group.addItem(group, group)
        layout.addWidget(row(self.search, self.group))
        self.table = QTableWidget(len(wallets), 4)
        configure_table(self.table, ["选择", "钱包名称", "地址", "分组"])
        for index, wallet in enumerate(wallets):
            checked = item("")
            checked.setFlags(checked.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            checked.setCheckState(Qt.CheckState.Unchecked)
            self.table.setItem(index, 0, checked)
            for column, key in enumerate(("name", "address", "group_name"), 1):
                self.table.setItem(index, column, item(wallet[key]))
        layout.addWidget(self.table)
        select_all = QPushButton("勾选当前筛选结果")
        select_all.clicked.connect(self.select_visible)
        layout.addWidget(select_all)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.search.textChanged.connect(self.filter)
        self.group.currentIndexChanged.connect(self.filter)

    def filter(self):
        query, group = self.search.text().strip().lower(), self.group.currentData()
        for index, wallet in enumerate(self.wallets):
            matches = (
                query
                in " ".join(str(wallet[key]) for key in ("name", "address", "group_name")).lower()
            )
            self.table.setRowHidden(
                index, not matches or bool(group and wallet["group_name"] != group)
            )

    def select_visible(self):
        for index in range(len(self.wallets)):
            if not self.table.isRowHidden(index):
                self.table.item(index, 0).setCheckState(Qt.CheckState.Checked)

    def selected(self):
        return [
            wallet["address"]
            for index, wallet in enumerate(self.wallets)
            if self.table.item(index, 0).checkState() == Qt.CheckState.Checked
        ]


class BatchApproval(QDialog):
    def __init__(self, preview, parent):
        super().__init__(parent)
        self.setWindowTitle("确认批量转账")
        self.resize(900, 650)
        layout = box(self, margins=(26, 24, 26, 24))
        info = NETWORKS[preview["network"]]
        layout.addWidget(text(f"确认发送 {len(preview['items'])} 笔转账", "heading"))
        layout.addWidget(
            text(
                f"{info.title} · Chain ID {info.chain_id}  |  {preview['asset']['symbol']}\n代币合约：{preview['asset']['contract'] or '原生币'}",
                "fieldLabel",
                True,
            )
        )
        layout.addWidget(
            text(
                f"转账总数量：{preview['total']} {preview['asset']['symbol']}\n累计执行手续费上限：{preview['fee_total']} {info.symbol}",
                "fieldLabel",
                True,
            )
        )
        table = QTableWidget(len(preview["items"]), 4)
        configure_table(
            table,
            [
                "发送钱包（完整地址）",
                "收款地址（完整地址）",
                "数量",
                f"执行手续费上限（{info.symbol}）",
            ],
        )
        for index, entry in enumerate(preview["items"]):
            for column, key in enumerate(("sender", "recipient", "amount", "fee_cap")):
                value = format_units(int(entry[key])) if key == "fee_cap" else entry[key]
                table.setItem(index, column, item(value))
        table.setWordWrap(True)
        table.resizeRowsToContents()
        layout.addWidget(table, 1)
        grouped = QPlainTextEdit()
        grouped.setReadOnly(True)
        grouped.setMaximumHeight(100)
        grouped.setPlainText(
            "\n".join(
                f"{entry['sender']}：{entry['amount']} {preview['asset']['symbol']} + 执行手续费 ≤ {entry['fee']} {info.symbol}"
                for entry in preview["senders"]
            )
        )
        layout.addWidget(grouped)
        layout.addWidget(
            text(
                "每行独立发送，上一笔确认后再处理下一笔。部分成功无法整体撤回。\n费用超过本次确认的逐笔上限会暂停；L2 可能另收 L1 数据费，请留足原生币。",
                "muted",
                True,
            )
        )
        self.acknowledge = QCheckBox("已核对重复明细和自转账，仍按清单分别付款")
        if preview["warnings"]:
            warnings = QPlainTextEdit("\n".join(preview["warnings"]))
            warnings.setReadOnly(True)
            warnings.setMaximumHeight(75)
            layout.addWidget(warnings)
            layout.addWidget(self.acknowledge)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("返回修改")
        send = buttons.addButton(
            f"确认并开始发送 {len(preview['items'])} 笔", QDialogButtonBox.ButtonRole.AcceptRole
        )
        send.setObjectName("primary")
        send.setEnabled(not preview["warnings"])
        self.acknowledge.toggled.connect(send.setEnabled)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class BatchPage(QWidget):
    history_changed = Signal()

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.service = BatchService(window.backend)
        self.service.recover()
        self.selected_id = None
        self.executing_id = None
        self.generation = None
        self.busy = False
        self.previewing = False
        self.dialog = None
        self._build()
        self.refresh_wallets()
        self.reload_history()
        self.timer = QTimer(self)
        self.timer.setInterval(3000)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    @property
    def running(self):
        return self.executing_id is not None or self.busy

    def _build(self):
        self.setObjectName("content")
        layout = box(self, margins=(28, 26, 28, 24), spacing=12)
        layout.addWidget(text("批量转账", "heading"))
        layout.addWidget(
            text(
                "① 填写清单 → ② 预览并确认 → ③ 逐笔执行与结果。每批最多 100 笔，单一网络和资产。",
                "muted",
                True,
            )
        )
        self.history = QComboBox()
        self.history.setMaximumWidth(420)
        self.history.setMinimumContentsLength(24)
        self.history.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.history.currentIndexChanged.connect(self.select_batch)
        self.wallet_status = text("", "muted")
        self.manage_wallet_button = QPushButton("解锁钱包库")
        self.manage_wallet_button.setObjectName("link")
        self.manage_wallet_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.manage_wallet_button.clicked.connect(self.window._manage_local_wallets)
        history_row = QWidget()
        history_layout = box(history_row, horizontal=True, spacing=12)
        self.new_button = QPushButton("新建批量转账")
        self.new_button.setCheckable(True)
        self.new_button.clicked.connect(self.open_editor)
        self.history_button = QPushButton("批次记录")
        self.history_button.setCheckable(True)
        self.history_button.clicked.connect(self.open_history)
        history_layout.addWidget(self.new_button)
        history_layout.addWidget(self.history_button)
        self.history_label = text("选择批次", "fieldLabel")
        self.history_label.setBuddy(self.history)
        self.history.setAccessibleName("查看历史批次")
        history_layout.addWidget(self.history_label)
        history_layout.addWidget(self.history, 1)
        history_layout.addStretch(1)
        history_layout.addWidget(self.wallet_status)
        history_layout.addWidget(self.manage_wallet_button)
        layout.addWidget(history_row)
        self.views = QStackedWidget()
        layout.addWidget(self.views, 1)
        editor = QWidget()
        form = box(editor, spacing=10)
        self.mode = QComboBox()
        for key, label in MODES.items():
            self.mode.addItem(label, key)
        self.network = QComboBox()
        for key, info in NETWORKS.items():
            self.network.addItem(info.title, key)
        self.network.setCurrentIndex(self.network.findData(self.window.preferences.network))
        self.asset = QComboBox()
        self.settings_panel = QWidget()
        self.settings_panel.setMaximumWidth(900)
        settings_grid = QGridLayout(self.settings_panel)
        settings_grid.setContentsMargins(0, 0, 0, 0)
        settings_grid.setHorizontalSpacing(20)
        settings_grid.setVerticalSpacing(6)
        for column, (title, control) in enumerate(
            (("模式", self.mode), ("网络", self.network), ("资产", self.asset))
        ):
            label = text(title, "fieldLabel")
            label.setBuddy(control)
            settings_grid.addWidget(label, 0, column)
            settings_grid.addWidget(control, 1, column)
            settings_grid.setColumnStretch(column, 1)
            control.setMinimumContentsLength(16)
            control.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
        form.addWidget(self.settings_panel)
        self.sender = QComboBox()
        self.sender.setMinimumContentsLength(24)
        self.sender.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.recipient = QLineEdit()
        self.recipient.setPlaceholderText("收款人的完整 0x 地址")
        self.recipient.setMaxLength(256)
        self.sender.setMaximumWidth(560)
        self.recipient.setMaximumWidth(560)
        self.common_sender = QWidget()
        sender_layout = box(self.common_sender, spacing=6)
        sender_layout.addWidget(text("统一发送钱包", "fieldLabel"))
        sender_layout.addWidget(self.sender)
        self.common_recipient = QWidget()
        recipient_layout = box(self.common_recipient, spacing=6)
        recipient_layout.addWidget(text("统一收款地址", "fieldLabel"))
        recipient_layout.addWidget(self.recipient)
        self.amount = QLineEdit()
        self.amount.setMaximumWidth(200)
        self.amount.setMaxLength(100)
        self.amount.setPlaceholderText("输入明确数量")
        amount = QPushButton("应用到所有行")
        amount.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        amount.clicked.connect(self.apply_amount)
        self.quantity_fields = QWidget()
        quantity_layout = box(self.quantity_fields, spacing=6)
        quantity_layout.addWidget(text("统一数量", "fieldLabel"))
        quantity_layout.addWidget(row(self.amount, amount, None))
        self.common_fields = QWidget()
        self.common_fields.setMaximumWidth(900)
        self.common_grid = QGridLayout(self.common_fields)
        self.common_grid.setContentsMargins(0, 0, 0, 0)
        self.common_grid.setHorizontalSpacing(20)
        self.common_grid.setVerticalSpacing(0)
        self.common_grid.addWidget(self.common_sender, 0, 0, 1, 2)
        self.common_grid.addWidget(self.common_recipient, 0, 0, 1, 2)
        self.common_grid.addWidget(self.quantity_fields, 0, 2)
        for column in range(3):
            self.common_grid.setColumnStretch(column, 1)
        form.addWidget(self.common_fields)
        add = QPushButton("添加一行")
        add.clicked.connect(lambda: self.add_row())
        self.pick = QPushButton("选择发送钱包")
        self.pick.clicked.connect(self.pick_wallets)
        remove = self.remove_button = QPushButton("删除选中行")
        remove.setToolTip("删除已勾选的明细；行高亮不代表勾选。")
        remove.setEnabled(False)
        remove.clicked.connect(self.remove_rows)
        load = QPushButton("导入 CSV")
        load.clicked.connect(self.import_csv)
        paste = QPushButton("粘贴表格")
        paste.clicked.connect(self.paste)
        template = QPushButton("下载 CSV 模板")
        template.clicked.connect(self.template)
        actions = QWidget()
        action_layout = box(actions, horizontal=True, spacing=8)
        for control in (add, self.pick, remove, load, paste, template):
            control.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            action_layout.addWidget(control)
        action_layout.addStretch()
        form.addWidget(actions)
        self.table = QTableWidget(0, 5)
        self.check_header = ChecklistHeader(self.table)
        self.table.setHorizontalHeader(self.check_header)
        configure_table(self.table, ["", "发送钱包", "收款地址", "数量", "校验结果"], editable=True)
        self.check_header.select_requested.connect(self.check_current_page)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 44)
        self.table.verticalHeader().setDefaultSectionSize(44)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.table.itemChanged.connect(self.invalidate)
        form.addWidget(self.table, 1)
        self.feedback = text("清单只包含公开地址和数量，请勿导入私钥。", "fieldLabel", True)
        form.addWidget(self.feedback)
        self.preview_button = QPushButton("下一步：预览并校验")
        self.preview_button.setObjectName("primary")
        self.preview_button.clicked.connect(self.preview)
        form.addWidget(self.preview_button)
        self.views.addWidget(editor)
        detail = QWidget()
        content = box(detail, spacing=10)
        self.status = text("", "fieldLabel", True)
        self.summary = text("", "fieldLabel", True)
        self.progress = QProgressBar()
        content.addWidget(self.status)
        content.addWidget(self.summary)
        content.addWidget(self.progress)
        self.details = QTableWidget(0, 7)
        configure_table(
            self.details, ["序号", "发送钱包", "收款地址", "数量", "状态", "交易哈希", "说明"]
        )
        self.details.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.details.setColumnWidth(0, 52)
        self.details.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.details.cellDoubleClicked.connect(self.copy_detail)
        content.addWidget(self.details, 1)
        self.resume_button = QPushButton("恢复发送")
        self.resume_button.setObjectName("primary")
        self.resume_button.clicked.connect(self.resume)
        self.pause_button = QPushButton("暂停发送")
        self.pause_button.clicked.connect(self.pause)
        self.cancel_button = QPushButton("结束批次")
        self.cancel_button.clicked.connect(self.cancel)
        self.rebroadcast_button = QPushButton("重播交易")
        self.rebroadcast_button.clicked.connect(self.rebroadcast)
        self.details.itemSelectionChanged.connect(self.update_rebroadcast)
        export = QPushButton("导出结果 CSV")
        export.clicked.connect(self.export)
        content.addWidget(row(self.resume_button, self.pause_button, self.cancel_button))
        content.addWidget(row(self.rebroadcast_button, export))
        content.addWidget(
            text(
                "双击复制完整字段。暂停 / 结束只影响后续任务；已签名或已发送交易不能撤回。\n结果不明请先查询或重播原交易，不要重新创建付款。",
                "muted",
                True,
            )
        )
        self.views.addWidget(detail)
        self.mode.currentIndexChanged.connect(self.mode_changed)
        self.sender.currentIndexChanged.connect(self.sync_common)
        self.recipient.textChanged.connect(self.sync_common)
        self.network.currentIndexChanged.connect(self.assets_changed)
        self.asset.currentIndexChanged.connect(self.invalidate)
        self.assets_changed()
        self.mode_changed()

    def wallets(self):
        vault = self.window.wallet_page.vault
        if not vault.unlocked:
            raise UserError("请先解锁钱包库。")
        return vault.wallets()

    def refresh_wallets(self):
        vault = self.window.wallet_page.vault
        self.wallet_status.setText(
            "钱包库已解锁"
            if vault.unlocked
            else "钱包库已锁定"
            if vault.exists
            else "尚未创建钱包库"
        )
        self.manage_wallet_button.setText(
            "管理钱包" if vault.unlocked else "解锁钱包库" if vault.exists else "创建钱包库"
        )
        try:
            wallets = self.wallets()
        except UserError:
            wallets = []
        selected = self.sender.currentData()
        self.sender.blockSignals(True)
        self.sender.clear()
        for wallet in wallets:
            self.sender.addItem(
                f"{wallet['name']} · {wallet['address'][:8]}…{wallet['address'][-4:]}",
                wallet["address"],
            )
            self.sender.setItemData(
                self.sender.count() - 1, wallet["address"], Qt.ItemDataRole.ToolTipRole
            )
        if self.sender.findData(selected) >= 0:
            self.sender.setCurrentIndex(self.sender.findData(selected))
        self.sender.blockSignals(False)
        self.sender.setEnabled(bool(wallets))
        if not wallets:
            if self.dialog is not None:
                self.dialog.reject()
            if self.executing_id:
                self.service.pause(self.executing_id, "钱包库已锁定，请解锁并确认继续。")
                self.executing_id = None
        self.sync_common()

    def assets_changed(self):
        self.asset.clear()
        for asset in self.window.preferences.assets(self.network.currentData()):
            self.asset.addItem(
                asset.symbol + (" · ERC-20" if asset.contract else " · 原生币"), asset
            )
        self.invalidate()

    def mode_changed(self):
        # Rows belong to one mode; reinterpreting them can change the payment intent.
        cleared = self.table.rowCount() > 0
        self.table.setRowCount(0)
        mode = self.mode.currentData()
        self.common_sender.setVisible(mode == "distribute")
        self.common_recipient.setVisible(mode == "collect")
        self.pick.setText("选择收款钱包" if mode == "distribute" else "选择发送钱包")
        self.common_grid.removeWidget(self.quantity_fields)
        self.common_grid.addWidget(
            self.quantity_fields, 0, 0 if mode == "mapping" else 2, 1, 3 if mode == "mapping" else 1
        )
        self.sync_common()
        if cleared:
            self.feedback.setText("模式已切换，转账清单已清空。请重新添加或导入明细。")

    def sync_common(self, *_):
        if not hasattr(self, "table"):
            return
        mode = self.mode.currentData()
        self.table.blockSignals(True)
        for index in range(self.table.rowCount()):
            for column in (1, 2):
                cell = self.table.item(index, column)
                if cell is None:
                    continue
                cell.setFlags(cell.flags() | Qt.ItemFlag.ItemIsEditable)
                if column == 1 and mode == "distribute":
                    if self.sender.currentData():
                        cell.setText(self.sender.currentData())
                    cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if column == 2 and mode == "collect":
                    cell.setText(self.recipient.text().strip())
                    cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.table.blockSignals(False)
        self.invalidate()

    def invalidate(self, *_):
        if not hasattr(self, "table"):
            return
        self.table.blockSignals(True)
        for index in range(self.table.rowCount()):
            self.table.item(index, 4).setText("待校验")
            for column in range(1, 5):
                cell = self.table.item(index, column)
                cell.setToolTip(cell.text())
        self.table.blockSignals(False)
        self.feedback.setText(
            f"共 {self.table.rowCount()} 笔。转账包含全部明细，勾选仅用于删除；修改后请重新预览。"
        )
        self.update_checks()

    def row_checkbox(self, index):
        return self.table.cellWidget(index, 0).findChild(QCheckBox)

    def checked_rows(self):
        return [
            index for index in range(self.table.rowCount()) if self.row_checkbox(index).isChecked()
        ]

    def current_page_rows(self):
        # This editor currently has one page (up to 100 rows). Scrolling is not pagination.
        return [
            index for index in range(self.table.rowCount()) if not self.table.isRowHidden(index)
        ]

    def update_checks(self):
        rows = self.current_page_rows()
        checked = sum(self.row_checkbox(index).isChecked() for index in rows)
        state = (
            Qt.CheckState.Checked
            if rows and checked == len(rows)
            else Qt.CheckState.PartiallyChecked
            if checked
            else Qt.CheckState.Unchecked
        )
        self.check_header.checkbox.setCheckState(state)
        self.check_header.checkbox.setEnabled(bool(rows))
        self.remove_button.setEnabled(bool(self.checked_rows()))

    def check_current_page(self, checked):
        for index in self.current_page_rows():
            checkbox = self.row_checkbox(index)
            checkbox.blockSignals(True)
            checkbox.setChecked(checked)
            checkbox.blockSignals(False)
        self.update_checks()

    def add_row(self, entry=None):
        if self.table.rowCount() >= MAX_BATCH_ROWS:
            self.window.notify("每批最多 100 笔。")
            return
        entry = entry or {
            "sender": self.sender.currentData() or "",
            "recipient": self.recipient.text().strip()
            if self.mode.currentData() == "collect"
            else "",
            "amount": self.amount.text().strip(),
        }
        index = self.table.rowCount()
        self.table.blockSignals(True)
        self.table.insertRow(index)
        for column, value in enumerate(
            ("", entry["sender"], entry["recipient"], entry["amount"], "待校验")
        ):
            cell = item(value)
            if column in (0, 4):
                cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(index, column, cell)
        checkbox = QCheckBox()
        checkbox.setAccessibleName(f"选择第 {index + 1} 行")
        checkbox.toggled.connect(self.update_checks)
        selection = QWidget()
        selection_layout = box(selection, horizontal=True, spacing=0)
        selection_layout.addWidget(checkbox, alignment=Qt.AlignmentFlag.AlignCenter)
        self.table.setCellWidget(index, 0, selection)
        self.table.blockSignals(False)
        self.sync_common()

    def remove_rows(self):
        for index in reversed(self.checked_rows()):
            self.table.removeRow(index)
        for index in range(self.table.rowCount()):
            self.row_checkbox(index).setAccessibleName(f"选择第 {index + 1} 行")
        self.invalidate()

    def apply_amount(self):
        self.table.blockSignals(True)
        for index in range(self.table.rowCount()):
            self.table.item(index, 3).setText(self.amount.text().strip())
        self.table.blockSignals(False)
        self.invalidate()

    def pick_wallets(self):
        try:
            mode = self.mode.currentData()
            self.dialog = WalletPicker(self.wallets(), self, title=self.pick.text())
            if self.dialog.exec() == QDialog.DialogCode.Accepted:
                for address in self.dialog.selected():
                    self.add_row(
                        {
                            "sender": self.sender.currentData() or ""
                            if mode == "distribute"
                            else address,
                            "recipient": address
                            if mode == "distribute"
                            else self.recipient.text().strip()
                            if mode == "collect"
                            else "",
                            "amount": self.amount.text().strip(),
                        }
                    )
        except (UserError, OSError) as error:
            self.window._show_error(error)
        finally:
            if self.dialog:
                self.dialog.deleteLater()
                self.dialog = None

    def use_wallets(self, senders):
        if self.previewing:
            return
        self.history.setCurrentIndex(-1)
        self.mode.setCurrentIndex(self.mode.findData("collect"))
        self.table.setRowCount(0)
        self.invalidate()
        for sender in senders:
            self.add_row(
                {
                    "sender": sender,
                    "recipient": self.recipient.text().strip(),
                    "amount": self.amount.text().strip(),
                }
            )

    def load_content(self, content):
        entries = parse_manifest(
            content,
            self.mode.currentData(),
            self.sender.currentData() or "",
            self.recipient.text().strip(),
        )
        if len(entries) + self.table.rowCount() > MAX_BATCH_ROWS:
            raise UserError("导入后超过 100 笔，请拆分批次或先删除已有行。")
        if self.mode.currentData() == "collect" and not self.recipient.text().strip():
            recipients = {entry["recipient"] for entry in entries if entry["recipient"]}
            if len(recipients) > 1:
                raise UserError("多对一模式只能有一个收款地址。")
            if recipients:
                self.recipient.setText(next(iter(recipients)))
        for entry in entries:
            self.add_row(entry)

    def import_csv(self):
        filename, _ = QFileDialog.getOpenFileName(self, "导入公开地址清单", "", "CSV (*.csv)")
        if not filename:
            return
        try:
            with open(filename, "rb") as stream:
                data = stream.read(MAX_BATCH_BYTES + 1)
            if len(data) > MAX_BATCH_BYTES:
                raise UserError("清单最多 1 MB。")
            self.load_content(data.decode("utf-8-sig"))
        except (OSError, UnicodeError):
            self.window.notify("无法读取清单，请选择 UTF-8 编码的 CSV。")
        except UserError as error:
            self.window._show_error(error)

    def paste(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("粘贴公开地址和数量")
        dialog.resize(700, 360)
        layout = box(dialog, margins=(20, 20, 20, 20))
        editor = QPlainTextEdit()
        editor.setPlaceholderText("sender,recipient,amount\n或直接粘贴 Excel 表格；不接受私钥")
        editor.setPlainText(QApplication.clipboard().text()[: MAX_BATCH_BYTES + 1])
        layout.addWidget(editor)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                self.load_content(editor.toPlainText())
            except UserError as error:
                self.window._show_error(error)
        dialog.deleteLater()

    def template(self):
        filename, _ = QFileDialog.getSaveFileName(
            self, "保存清单模板", "batch-template.csv", "CSV (*.csv)"
        )
        if filename:
            try:
                Path(filename).write_text("sender,recipient,amount\n", encoding="utf-8-sig")
            except OSError as error:
                self.window._show_error(error)

    def raw_items(self):
        return [
            {
                key: self.table.item(index, column).text().strip()
                for key, column in (("sender", 1), ("recipient", 2), ("amount", 3))
            }
            for index in range(self.table.rowCount())
        ]

    def preview(self):
        if (self.previewing or self.busy or self.executing_id or self.window.busy
                or self.window.wallet_page.busy):
            self.window.notify("请先暂停正在执行的批次。")
            return
        try:
            wallets = self.wallets()
            generation = self.window.wallet_page.vault.session_generation
            args = (
                self.mode.currentData(),
                self.network.currentData(),
                self.asset.currentData(),
                self.raw_items(),
                [wallet["address"] for wallet in wallets],
            )
        except UserError as error:
            self.window._show_error(error)
            return
        self.set_previewing(True)
        self.views.widget(0).setEnabled(False)
        self.feedback.setText("正在逐行校验并检查各钱包累计余额及费用…")

        def completed(preview):
            self.set_previewing(False)
            self.views.widget(0).setEnabled(True)
            self.table.blockSignals(True)
            for index in range(self.table.rowCount()):
                self.table.item(index, 4).setText(preview["errors"].get(index + 1, "校验通过"))
                self.table.item(index, 4).setToolTip(self.table.item(index, 4).text())
            self.table.blockSignals(False)
            if preview["errors"]:
                self.feedback.setText(
                    f"发现 {len(preview['errors'])} 项错误，请修正后重新预览。"
                    + preview["errors"].get(0, "")
                )
                return
            if self.approve(preview, generation):
                try:
                    batch_id = self.service.create(preview)
                    self.table.setRowCount(0)
                    self.invalidate()
                    self.service.start(batch_id)
                except (UserError, OSError) as error:
                    self.window._show_error(error)
                    self.reload_history()
                    return
                self.executing_id = batch_id
                self.generation = generation
                self.reload_history(batch_id)
                self.tick()

        self.window._task(self.service.preview, args, completed, self.preview_failed)

    def approve(self, preview, generation):
        vault = self.window.wallet_page.vault
        if not vault.unlocked or vault.session_generation != generation:
            self.window.notify("钱包库状态已变化，请重新预览。")
            return False
        self.dialog = BatchApproval(preview, self)
        try:
            accepted = self.dialog.exec() == QDialog.DialogCode.Accepted
        finally:
            self.dialog.deleteLater()
            self.dialog = None
        return accepted and vault.unlocked and vault.session_generation == generation

    def preview_failed(self, error):
        self.set_previewing(False)
        self.views.widget(0).setEnabled(True)
        self.feedback.setText(friendly_error(error))
        self.window._show_error(error)
        if self.selected_id:
            self.show_batch(self.service.get(self.selected_id))

    def set_previewing(self, active):
        self.previewing = active
        self.history.setEnabled(not active)
        self.new_button.setEnabled(not active)
        self.history_button.setEnabled(not active and self.history.count() > 0)
        if active:
            self.cancel_button.setEnabled(False)
            self.rebroadcast_button.setEnabled(False)

    def reload_history(self, selected=None):
        selected = selected or self.selected_id
        batches = self.service.history()
        self.history_button.setEnabled(bool(batches) and not self.previewing)
        self.history.blockSignals(True)
        self.history.clear()
        for batch in batches:
            self.history.addItem(
                f"{format_beijing_time(batch['created_at'])} · {batch['id'][:8]} · {BATCH_STATES[batch['state']]}",
                batch["id"],
            )
        index = self.history.findData(selected)
        self.history.setCurrentIndex(index)
        self.history.blockSignals(False)
        self.select_batch()

    def open_editor(self):
        self.history.setCurrentIndex(-1)
        self.select_batch()

    def open_history(self):
        if self.history.currentIndex() < 0 and self.history.count():
            self.history.setCurrentIndex(0)
        else:
            self.select_batch()

    def select_batch(self, *_):
        self.selected_id = self.history.currentData()
        viewing_history = self.selected_id is not None
        self.new_button.setChecked(not viewing_history)
        self.history_button.setChecked(viewing_history)
        self.history_label.setVisible(viewing_history)
        self.history.setVisible(viewing_history)
        self.views.setCurrentIndex(1 if self.selected_id else 0)
        if self.selected_id:
            self.show_batch(self.service.get(self.selected_id))

    def show_batch(self, batch):
        if self.selected_id != batch["id"]:
            return
        self.status.setText(
            f"批次 {batch['id']} · {BATCH_STATES[batch['state']]}\n{batch['reason']}"
        )
        confirmed = sum(entry["state"] == "confirmed" for entry in batch["items"])
        failed = sum(entry["state"] == "failed" for entry in batch["items"])
        skipped = sum(entry["state"] == "skipped" for entry in batch["items"])
        with localcontext() as context:
            context.prec = 512
            total = format(
                sum((Decimal(entry["amount"]) for entry in batch["items"]), Decimal(0)).normalize(),
                "f",
            )
        self.summary.setText(
            f"{NETWORKS[batch['network']].title} · {batch['asset']['symbol']}  |  共 {len(batch['items'])} 笔，成功 {confirmed}，失败 {failed}，取消 {skipped}"
            f"\n计划转账总数量 {total} {batch['asset']['symbol']}；执行手续费上限合计 "
            f"{format_units(sum(int(entry['fee_cap']) for entry in batch['items']))} {NETWORKS[batch['network']].symbol}"
        )
        self.progress.setMaximum(len(batch["items"]))
        self.progress.setValue(confirmed + failed + skipped)
        self.progress.setFormat("已完成 %v / %m 笔")
        selected = self.details.currentRow()
        self.details.setRowCount(len(batch["items"]))
        for index, entry in enumerate(batch["items"]):
            state = ITEM_STATES[entry["state"]]
            if (
                self.busy
                and batch["state"] == "running"
                and entry["state"] == "pending"
                and all(
                    previous["state"] in {"confirmed", "failed", "skipped"}
                    for previous in batch["items"][:index]
                )
            ):
                state = "正在检查 / 本地签名"
            for column, value in enumerate(
                (
                    entry["position"],
                    entry["sender"],
                    entry["recipient"],
                    entry["amount"],
                    state,
                    entry["tx_hash"],
                    entry["error"],
                )
            ):
                self.details.setItem(index, column, item(value))
        if selected >= 0:
            self.details.selectRow(selected)
        self.resume_button.setEnabled(
            batch["state"] == "paused"
            and not self.busy
            and not self.previewing
            and not self.executing_id
        )
        self.pause_button.setEnabled(batch["state"] == "running")
        self.cancel_button.setEnabled(batch["state"] in {"running", "paused"})
        for button in (self.resume_button, self.pause_button, self.cancel_button):
            button.setVisible(batch["state"] in {"running", "paused"})
        self.rebroadcast_button.setVisible(
            any(entry["state"] in {"signed", "unknown", "submitted"} for entry in batch["items"])
        )
        self.update_rebroadcast()

    def update_rebroadcast(self):
        index = self.details.currentRow()
        batch = self.service.get(self.selected_id) if self.selected_id else None
        self.rebroadcast_button.setEnabled(
            bool(
                batch
                and batch["state"] != "running"
                and not self.busy
                and not self.previewing
                and 0 <= index < len(batch["items"])
                and batch["items"][index]["state"] in {"signed", "unknown", "submitted"}
            )
        )

    def tick(self):
        if self.busy or self.previewing or self.window.closing:
            return
        batch_id = self.executing_id or self.selected_id
        if not batch_id:
            return
        batch = self.service.get(batch_id)
        if batch["state"] != "running" and not any(
            entry["state"] in {"signed", "submitted", "mined", "unknown"}
            for entry in batch["items"]
        ):
            return
        self.busy = True
        if self.selected_id == batch_id:
            self.show_batch(batch)

        def completed(batch):
            self.busy = False
            if batch["state"] != "running" and self.executing_id == batch_id:
                self.executing_id = None
            self.show_batch(batch)
            self.history_changed.emit()
            if batch["state"] != "running":
                self.reload_history()

        def failed(error):
            self.busy = False
            self.service.pause(batch_id, friendly_error(error))
            self.executing_id = None
            self.reload_history()
            self.window._show_error(error)

        self.window._task(
            self.service.step,
            (batch_id, self.window.wallet_page.vault, self.generation),
            completed,
            failed,
        )

    def pause(self):
        if self.selected_id:
            self.service.pause(self.selected_id)
            if self.executing_id == self.selected_id:
                self.executing_id = None
            self.show_batch(self.service.get(self.selected_id))

    def resume(self):
        if (
            not self.selected_id
            or self.busy
            or self.previewing
            or self.executing_id
            or self.window.busy
        ):
            return
        try:
            wallets = [wallet["address"] for wallet in self.wallets()]
            generation = self.window.wallet_page.vault.session_generation
        except UserError as error:
            self.window._show_error(error)
            return
        batch_id = self.selected_id
        self.set_previewing(True)
        self.resume_button.setEnabled(False)

        def completed(preview):
            self.set_previewing(False)
            if preview and preview["errors"]:
                self.window.notify(
                    "剩余任务校验未通过：\n"
                    + "\n".join(
                        f"第 {index} 行：{message}" for index, message in preview["errors"].items()
                    )
                )
                self.reload_history(batch_id)
                return
            if preview:
                approved = self.approve(preview, generation)
            else:
                approved = (
                    QMessageBox.question(
                        self,
                        "继续查询批次",
                        "没有可重新签名的任务，将继续查询已发送交易。是否继续？",
                    )
                    == QMessageBox.StandardButton.Yes
                )
            if approved:
                try:
                    self.service.start(batch_id, preview)
                except UserError as error:
                    self.window._show_error(error)
                    self.reload_history(batch_id)
                    return
                self.executing_id, self.generation = batch_id, generation
                self.reload_history(batch_id)
                self.tick()
            else:
                self.reload_history(batch_id)

        self.window._task(
            self.service.preview_remaining, (batch_id, wallets), completed, self.preview_failed
        )

    def cancel(self):
        if (
            self.selected_id
            and QMessageBox.question(
                self,
                "结束批次",
                "取消尚未签名的任务。已签名或已发送交易仍需查询，不能撤回。是否结束？",
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.service.cancel(self.selected_id)
            if self.executing_id == self.selected_id:
                self.executing_id = None
            self.reload_history()

    def rebroadcast(self):
        if self.busy or not self.selected_id:
            return
        batch = self.service.get(self.selected_id)
        index = self.details.currentRow()
        if index < 0:
            self.window.notify("请先选中一笔交易。")
            return
        entry = batch["items"][index]
        if entry["state"] not in {"signed", "unknown", "submitted"}:
            self.window.notify("只有已签名、已提交或结果不明确的原交易可以重播。")
            return
        if (
            QMessageBox.question(
                self, "重播原交易", "只广播同一份已保存签名，不创建新付款、不提高手续费。是否继续？"
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.busy = True

        def done(_):
            self.busy = False
            self.reload_history()
            self.history_changed.emit()

        def failed(error):
            self.busy = False
            self.window._show_error(error)
            self.reload_history()

        self.window._task(self.window.backend.rebroadcast, (entry["request_id"],), done, failed)

    def copy_detail(self, index, column):
        value = self.details.item(index, column).text()
        if value:
            QApplication.clipboard().setText(value)
            self.window._notify_copied("批次字段")

    def export(self):
        if not self.selected_id:
            return
        filename, _ = QFileDialog.getSaveFileName(
            self, "导出批次结果", f"batch-{self.selected_id[:8]}.csv", "CSV (*.csv)"
        )
        if not filename:
            return
        batch = self.service.get(self.selected_id)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "batch_id",
                "network",
                "asset",
                "contract",
                "position",
                "request_id",
                "sender",
                "recipient",
                "amount",
                "state",
                "tx_hash",
                "error",
            ]
        )
        for entry in batch["items"]:
            values = [
                batch["id"],
                batch["network"],
                batch["asset"]["symbol"],
                batch["asset"]["contract"] or "",
                entry["position"],
                entry["request_id"],
                entry["sender"],
                entry["recipient"],
                entry["amount"],
                ITEM_STATES[entry["state"]],
                entry["tx_hash"],
                entry["error"],
            ]
            writer.writerow(
                [
                    "'" + str(value)
                    if str(value).startswith(("=", "+", "-", "@", "\t", "\r"))
                    else value
                    for value in values
                ]
            )
        try:
            Path(filename).write_text(output.getvalue(), encoding="utf-8-sig")
        except OSError as error:
            self.window._show_error(error)

    def shutdown(self):
        self.timer.stop()
        if self.executing_id:
            self.service.pause(self.executing_id, "应用退出，核对结果后可手动继续。")
