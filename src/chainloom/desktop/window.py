import re
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from threading import Event

from PySide6.QtCore import Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence, QTextOption
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHeaderView,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from ..diagnostics import log_directory, record_exception, record_operation
from ..errors import UserError
from ..time_format import format_beijing_time
from ..transfer import address
from .batch_page import BatchPage
from .dialogs import ConfirmDialog, TokenDialog
from .service import DesktopService, format_units, friendly_error
from .settings import HISTORY_NETWORKS, NETWORKS, Asset, Preferences, save_preferences
from .style import STYLE
from .vault_page import WalletManagementPage
from .widgets import BrandMark, CopyToast, box, row, text
from .workers import Worker

STATES = {
    "awaiting_wallet": "等待钱包结果",
    "wallet_unknown": "需核对钱包结果",
    "verifying": "正在核对链上交易",
    "signed": "已签名，等待广播",
    "submitted": "已提交，等待确认",
    "unknown": "结果待确认",
    "mined": "链上确认中",
    "confirmed": "转账成功",
    "failed": "链上执行失败",
    "rejected": "已在钱包中拒绝",
    "mismatched": "交易不一致，请核对",
}

HISTORY_COPY_FIELDS = {5: "发送地址", 6: "收款地址", 7: "交易哈希"}


class MainWindow(QMainWindow):
    task_succeeded = Signal(str, object)
    task_failed = Signal(str, object)
    assets_changed = Signal(str)

    def __init__(self, directory: Path, preferences: Preferences, backend=None):
        super().__init__()
        self.directory, self.preferences = directory, preferences
        self.backend = backend or DesktopService(directory, preferences)
        self.stop_event = getattr(self.backend, "cancelled", Event())
        self.setWindowTitle("Chainloom")
        self.resize(1240, 850)
        self.setMinimumSize(1060, 760)
        self.setStyleSheet(STYLE)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(4)
        self.workers = {}
        self.callbacks = {}
        self.quote = None
        self.busy = False
        self.confirm_dialog = None
        self.form_generation = 0
        self.records = []
        self.checked_request_ids = set()
        self.pending_refresh_ids = None
        self.poll_busy = False
        self.manual_refresh_active = False
        self.manual_refresh_requested = False
        self.closing = False
        self.force_close = False
        self.task_succeeded.connect(self._task_success)
        self.task_failed.connect(self._task_failure)
        self._build()
        self.copy_toast = CopyToast(self)
        self.wallet_page.address_copied.connect(lambda: self._notify_copied("钱包地址"))
        self.wallet_page.state_changed.connect(self._local_wallet_changed)
        self.balance_timer = QTimer(self)
        self.balance_timer.setSingleShot(True)
        self.balance_timer.setInterval(350)
        self.balance_timer.timeout.connect(self._load_balance)
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(7000)
        self.poll_timer.timeout.connect(self.poll)
        self.poll_timer.start()
        self._network_changed()
        self.load_history()

    def _build(self):
        root = QWidget()
        root_box = box(root, horizontal=True, spacing=0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(220)
        side = box(sidebar, margins=(20, 28, 20, 22), spacing=12)
        side.addWidget(row(BrandMark(), text("Chainloom", "brand")))
        side.addSpacing(25)
        side.addWidget(text("工作空间", "muted"))
        self.navigation = QButtonGroup(self)
        self.navigation.setExclusive(True)
        for index, title in (
            (3, "▣    钱包管理"),
            (0, "↗    转账"),
            (4, "⇉    批量转账"),
            (1, "◷    交易记录"),
            (2, "⚙    设置"),
        ):
            button = QPushButton(title)
            button.setObjectName("navigation")
            button.setCheckable(True)
            self.navigation.addButton(button, index)
            side.addWidget(button)
        self.navigation.idClicked.connect(self.navigate)
        self.navigation.button(3).setChecked(True)
        side.addStretch()
        side.addWidget(text("本地运行 · 加密存储", "muted"))
        root_box.addWidget(sidebar)
        self.stack = QStackedWidget()
        self.stack.addWidget(self._transfer_page())
        self.stack.addWidget(self._history_page())
        self.stack.addWidget(self._settings_page())
        self.wallet_page = WalletManagementPage(self.directory, self._task, self)
        self.stack.addWidget(self.wallet_page)
        self.batch_page = BatchPage(self)
        self.stack.addWidget(self.batch_page)
        self.batch_page.history_changed.connect(self.load_history)
        self.stack.setCurrentIndex(3)
        root_box.addWidget(self.stack, 1)
        self.setCentralWidget(root)
        self.statusBar().showMessage("就绪 · 本地钱包管理")
        menu = self.menuBar().addMenu("帮助")
        about = QAction("关于 Chainloom", self)
        about.triggered.connect(
            lambda: QMessageBox.information(
                self,
                "关于 Chainloom",
                "Chainloom 0.1.0\n本地钱包管理与 EVM 单次及批量转账工具",
            )
        )
        menu.addAction(about)
        notices = QAction("第三方软件许可", self)
        notices.triggered.connect(
            lambda: QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(Path(__file__).parent / "assets" / "THIRD_PARTY_NOTICES.md"))
            )
        )
        menu.addAction(notices)

    def _page(self, title, subtitle):
        widget = QWidget()
        widget.setObjectName("content")
        layout = box(widget, margins=(32, 30, 32, 26), spacing=15)
        layout.addWidget(text(title, "heading"))
        layout.addWidget(text(subtitle, "muted", True))
        return widget, layout

    def _transfer_page(self):
        page, layout = self._page("单次转账", "将资产发送到另一个钱包，简单几步即可完成。")
        self.steps = text(
            "①  填写转账信息     ───     ②  确认并本地签名     ───     ③  转账结果", "fieldLabel"
        )
        layout.addSpacing(8)
        layout.addWidget(self.steps)
        columns = QWidget()
        columns.setObjectName("transferColumns")
        column_box = box(columns, horizontal=True, spacing=24)
        form = QWidget()
        form.setObjectName("transferForm")
        form_box = box(form, spacing=10)
        form_box.addWidget(text("转账网络", "fieldLabel"))
        self.network = QComboBox()
        for name, info in NETWORKS.items():
            self.network.addItem(info.title, name)
        self.network.setCurrentIndex(list(NETWORKS).index(self.preferences.network))
        self.network.currentIndexChanged.connect(self._network_changed)
        form_box.addWidget(self.network)
        self.manage_wallet_button = QPushButton("解锁钱包库")
        self.manage_wallet_button.setObjectName("link")
        self.manage_wallet_button.clicked.connect(self._manage_local_wallets)
        form_box.addWidget(
            row(text("发送钱包 · 本地钱包库", "fieldLabel"), None, self.manage_wallet_button)
        )
        self.account = QComboBox()
        self.account.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.account.setMinimumContentsLength(18)
        self.account.setToolTip("选择已导入本地钱包库的发送钱包")
        self.account.currentIndexChanged.connect(self._account_changed)
        form_box.addWidget(self.account)
        self.local_wallet_hint = text("", "muted", True)
        form_box.addWidget(self.local_wallet_hint)
        self.balance = text("选择本地钱包后显示余额", "muted")
        self.add_token = QPushButton("添加代币")
        self.add_token.setObjectName("link")
        self.add_token.clicked.connect(self._add_token)
        form_box.addWidget(row(text("转账资产", "fieldLabel"), None, self.add_token))
        self.asset = QComboBox()
        self.asset.currentIndexChanged.connect(self._asset_changed)
        form_box.addWidget(self.asset)
        form_box.addWidget(self.balance)
        self.contract_label = text("", "muted", True)
        self.contract_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form_box.addWidget(self.contract_label)
        paste = QPushButton("粘贴地址")
        self.paste_button = paste
        paste.setObjectName("link")
        paste.clicked.connect(
            lambda: self.recipient.setText(QApplication.clipboard().text().strip())
        )
        form_box.addWidget(row(text("收款地址", "fieldLabel"), None, paste))
        self.recipient = QLineEdit()
        self.recipient.setPlaceholderText("收款人的完整 0x 地址")
        self.recipient.setMaxLength(256)
        self.recipient.textChanged.connect(self._edited)
        form_box.addWidget(self.recipient)
        self.address_hint = text("请选择相同网络的收款地址", "muted")
        form_box.addWidget(self.address_hint)
        form_box.addWidget(text("转账数量", "fieldLabel"))
        self.amount = QLineEdit()
        self.amount.setObjectName("amount")
        self.amount.setPlaceholderText("0.00")
        self.amount.setMaxLength(100)
        self.amount.textChanged.connect(self._edited)
        form_box.addWidget(self.amount)
        self.fee_hint = text("网络手续费将从原生币余额中扣除。", "muted", True)
        form_box.addWidget(self.fee_hint)
        form_box.addSpacing(8)
        self.send_button = QPushButton("下一步：预览转账    →")
        self.send_button.setObjectName("primary")
        self.send_button.clicked.connect(self._prepare)
        form_box.addWidget(self.send_button)
        form_box.addWidget(text("确认后在本机签名并发送", "muted"))
        form_box.addStretch()
        column_box.addWidget(form, 1)
        summary = QFrame()
        summary.setObjectName("summary")
        summary.setFixedWidth(285)
        summary_box = box(summary, margins=(22, 26, 22, 24), spacing=17)
        summary_box.addWidget(text("转账预览", "fieldLabel"))
        summary_box.addSpacing(13)
        summary_box.addWidget(text("你将发送", "muted"))
        self.preview_amount = text("—", "amountPreview", True)
        summary_box.addWidget(self.preview_amount)
        self.preview_network = text("", "muted")
        summary_box.addWidget(self.preview_network)
        summary_box.addWidget(text("收款地址", "fieldLabel"))
        self.preview_recipient = QPlainTextEdit("尚未填写")
        self.preview_recipient.setReadOnly(True)
        self.preview_recipient.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        self.preview_recipient.setFixedHeight(50)
        self.preview_recipient.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.preview_recipient.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.preview_recipient.setStyleSheet(
            "QPlainTextEdit { background: transparent; color: #7B897F; border: none; padding: 0; }"
        )
        summary_box.addWidget(self.preview_recipient)
        summary_box.addWidget(text("执行手续费上限", "fieldLabel"))
        self.preview_fee = text("预览后显示", "muted", True)
        summary_box.addWidget(self.preview_fee)
        self.preview_total = text("", "fieldLabel", True)
        summary_box.addWidget(self.preview_total)
        summary_box.addStretch()
        summary_box.addWidget(text("使用本地钱包签名", "fieldLabel"))
        summary_box.addWidget(
            text("私钥仅在本机解密签名。\n发送前，请再次核对收款地址及网络。", "muted", True)
        )
        column_box.addWidget(summary)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(columns)
        layout.addWidget(scroll, 1)
        return page

    def _history_page(self):
        page, layout = self._page(
            "交易记录", "记录保存在此 Mac 上。网络恢复后会继续查询待确认交易。"
        )
        refresh = self.history_refresh_button = QPushButton("刷新链上状态")
        refresh.setToolTip("仅查询点击时勾选的交易；跳过无哈希及已移除网络的记录。")
        refresh.clicked.connect(lambda: self.poll(force=True))
        explorer = QPushButton("在区块浏览器中查看")
        explorer.clicked.connect(self._open_explorer)
        copy = QPushButton("复制详情")
        copy.clicked.connect(self._copy_record)
        rebroadcast = self.history_rebroadcast_button = QPushButton("重播原交易")
        rebroadcast.setToolTip(
            "发送结果不明确时重新广播已保存的同一笔签名交易，保持原哈希和nonce；不创建新付款，也不加速交易。"
        )
        rebroadcast.clicked.connect(self._rebroadcast)
        self.history_select_all = QCheckBox("全选")
        self.history_select_all.setTristate(True)
        self.history_select_all.clicked.connect(self._check_all_history)
        self.history_checked_count = text("已勾选 0 笔", "muted")
        layout.addWidget(
            row(
                self.history_select_all,
                refresh,
                rebroadcast,
                explorer,
                copy,
                None,
                self.history_checked_count,
            )
        )
        refresh.setEnabled(False)
        rebroadcast.setEnabled(False)
        self.history_table = QTableWidget(0, 8)
        self.history_table.setObjectName("historyTable")
        self.history_table.setHorizontalHeaderLabels(
            ["选择", "时间（北京时间）", "网络", "数量", "状态", "发送地址", "收款地址", "交易哈希"]
        )
        self.history_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.history_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.history_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.history_table.verticalHeader().hide()
        self.history_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.history_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.history_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.history_table.setColumnWidth(0, 44)
        self.history_table.itemChanged.connect(self._history_check_changed)
        self.history_table.setWordWrap(False)
        self.history_table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.history_table.cellDoubleClicked.connect(self._copy_history_cell)
        self.history_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.history_table.customContextMenuRequested.connect(self._history_context_menu)
        copy_field = QAction("复制当前字段", self.history_table)
        copy_field.setShortcuts(QKeySequence.StandardKey.Copy)
        copy_field.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        copy_field.triggered.connect(
            lambda: self._copy_history_cell(
                self.history_table.currentRow(), self.history_table.currentColumn()
            )
        )
        self.history_table.addAction(copy_field)
        layout.addWidget(self.history_table, 1)
        self.history_hint = text("尚无转账记录。", "muted", True)
        layout.addWidget(self.history_hint)
        return page

    def _settings_page(self):
        page, layout = self._page("设置", "管理钱包库安全、网络连接及本地文件。")
        page.setObjectName("settingsPage")

        def section(title, description):
            layout.addSpacing(12)
            card = QFrame()
            card.setObjectName("settingsSection")
            content = box(card, margins=(22, 20, 22, 20), spacing=12)
            content.addWidget(text(title, "settingsSectionTitle"))
            content.addWidget(text(description, "settingsDescription", True))
            layout.addWidget(card)
            return content

        def action(label, callback, primary=False):
            button = QPushButton(label)
            button.setProperty("settingsAction", True)
            if primary:
                button.setObjectName("primary")
            button.clicked.connect(callback)
            return button

        security = section("钱包库安全", "验证当前密码后即可设置新密码，无需先解锁钱包库。")
        self.change_password_button = action("修改钱包库密码", self._change_vault_password)
        security.addWidget(row(self.change_password_button, None))

        network = section("网络节点", "留空使用内置公共节点及备用节点。自定义节点只影响当前 Mac。")
        self.rpc_inputs = {}
        for name, info in NETWORKS.items():
            field = QWidget()
            field_layout = box(field, spacing=6)
            field_layout.addWidget(text(info.title, "settingsFieldLabel"))
            entry = QLineEdit(self.preferences.rpc_overrides.get(name, ""))
            entry.setPlaceholderText(info.rpcs[0])
            entry.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
            self.rpc_inputs[name] = entry
            field_layout.addWidget(entry)
            network.addWidget(field)
        network.addWidget(row(action("保存设置", self._save_settings, primary=True), None))

        storage = section("本地数据", "钱包库与交易记录保存在本机。")
        data_path = text(str(self.directory), "settingsPath", True)
        data_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        storage.addWidget(data_path)
        storage.addWidget(row(action("打开本地数据文件夹", lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(self.directory))
        )), None))

        logs = section("日志与诊断", "操作遇到问题时，可打开日志文件夹查看错误详情。")
        self.log_directory = log_directory()
        self.log_path_label = text(str(self.log_directory / "chainloom.log"), "settingsPath", True)
        self.log_path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        logs.addWidget(self.log_path_label)
        self.log_folder_button = action("打开日志文件夹", lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(self.log_directory))
        ))
        logs.addWidget(row(self.log_folder_button, None))
        layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll

    def _change_vault_password(self):
        if self.busy or self.batch_page.running or self.batch_page.previewing:
            self.notify("请等待当前转账操作完成或暂停批量任务后再修改密码。")
            return
        self.wallet_page.change_password()

    def navigate(self, index):
        self.navigation.button(index).setChecked(True)
        self.stack.setCurrentIndex(index)
        if index == 1:
            self.load_history()
        if index == 0:
            self._set_accounts()
        if index == 4:
            self.batch_page.refresh_wallets()
            self.batch_page.reload_history()

    def _open_wallet_balances(self):
        if not self.wallet_page.vault.unlocked or self.wallet_page.busy:
            self.notify("请先解锁钱包库，并等待当前钱包库操作完成。")
            return
        self.navigate(3)
        self.wallet_page.tabs.setCurrentIndex(1)

    def _task(self, function, args=(), success=None, failure=None):
        import uuid

        if self.closing:
            return
        task_id = uuid.uuid4().hex
        worker = Worker(function, *args, cancelled=self.stop_event, task_id=task_id)
        self.workers[task_id] = worker
        self.callbacks[task_id] = (success, failure)
        # These proxy signals force callbacks onto the GUI thread, even for Python lambdas.
        worker.signals.succeeded.connect(
            lambda value, key=task_id: self.task_succeeded.emit(key, value)
        )
        worker.signals.failed.connect(lambda error, key=task_id: self.task_failed.emit(key, error))
        self.pool.start(worker)

    def _task_success(self, task_id, value):
        callback, _ = self.callbacks.pop(task_id)
        self.workers.pop(task_id)
        if callback and not self.closing:
            callback(value)

    def _task_failure(self, task_id, error):
        _, callback = self.callbacks.pop(task_id)
        self.workers.pop(task_id, None)
        if not self.closing:
            (callback or self._show_error)(error)

    def _show_error(self, error):
        # Covers synchronous validation errors as well as worker callbacks.
        record_exception("界面操作", error)
        self.notify(friendly_error(error) + "\n详情见日志，可在设置中打开日志文件夹。")

    def notify(self, message):
        self.wallet_page.notify(message)
        self.statusBar().showMessage(message)

    def network_name(self):
        return self.network.currentData()

    def selected_asset(self):
        return self.asset.currentData() or Asset(NETWORKS[self.network_name()].symbol)

    def sender(self):
        return self.account.currentData() or ""

    def _network_changed(self, *_):
        name = self.network_name()
        self.asset.blockSignals(True)
        self.asset.clear()
        for asset in self.preferences.assets(name):
            self.asset.addItem(
                asset.symbol + ("  ·  ERC-20" if asset.contract else "  ·  原生币"), asset
            )
        self.asset.blockSignals(False)
        self._set_accounts()
        self._asset_changed()

    def _manage_local_wallets(self):
        if self.wallet_page.vault.unlocked:
            self.navigate(3)
        else:
            self.wallet_page.open_vault()

    def _local_wallet_changed(self, *_):
        self._set_accounts()
        self.batch_page.refresh_wallets()
        if not self.wallet_page.vault.unlocked and self.confirm_dialog is not None:
            self.confirm_dialog.reject()

    def _set_accounts(self):
        vault = self.wallet_page.vault
        try:
            wallets = vault.wallets() if vault.unlocked else []
        except (UserError, OSError) as error:
            record_exception("读取本地钱包列表", error)
            vault.lock()
            wallets = []
        previous = self.sender()
        self.account.blockSignals(True)
        self.account.clear()
        for wallet in wallets:
            self.account.addItem(
                f"{wallet['name']} · {wallet['group_name']} · {wallet['address'][:8]}…{wallet['address'][-4:]}",
                wallet["address"],
            )
            self.account.setItemData(
                self.account.count() - 1, wallet["address"], Qt.ItemDataRole.ToolTipRole
            )
        if previous and self.account.findData(previous) >= 0:
            self.account.setCurrentIndex(self.account.findData(previous))
        if not wallets:
            self.account.addItem("钱包库已锁定" if not vault.unlocked else "请先导入本地钱包", "")
        self.account.blockSignals(False)
        self.account.setEnabled(bool(wallets) and not self.busy)
        self.manage_wallet_button.setText(
            "管理钱包" if vault.unlocked else "解锁钱包库" if vault.exists else "创建钱包库"
        )
        self.local_wallet_hint.setText(
            ""
            if wallets
            else "请先解锁钱包库。"
            if vault.exists and not vault.unlocked
            else "请先创建钱包库并导入钱包。"
            if not vault.exists
            else "钱包库为空，请到钱包管理导入钱包。"
        )
        self.local_wallet_hint.setVisible(not bool(wallets))
        self._account_changed()

    def _account_changed(self, *_):
        if self.sender():
            self.local_wallet_hint.setText("发送地址：" + self.sender())
            self.local_wallet_hint.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            self.local_wallet_hint.show()
        self._edited()
        if hasattr(self, "balance_timer"):
            self.balance_timer.start()

    def _asset_changed(self, *_):
        asset = self.selected_asset()
        self.contract_label.setText("合约：" + asset.contract if asset.contract else "")
        self.contract_label.setVisible(bool(asset.contract))
        self.fee_hint.setText(f"网络手续费将从 {NETWORKS[self.network_name()].symbol} 余额中扣除。")
        self._edited()
        if hasattr(self, "balance_timer"):
            self.balance_timer.start()

    def _edited(self, *_):
        self.form_generation += 1
        self.quote = None
        self.preview_fee.setText("预览后显示")
        self.preview_total.clear()
        self.preview_network.setText(NETWORKS[self.network_name()].title)
        amount = self.amount.text().strip()
        self.preview_amount.setText(
            (amount[:28] + " " + self.selected_asset().symbol) if amount else "—"
        )
        recipient = self.recipient.text().strip()
        self.preview_recipient.setPlainText(recipient or "尚未填写")
        try:
            if recipient:
                address(recipient)
            valid_address = bool(recipient)
            self.address_hint.setText(
                "✓ 地址格式正确，请核对收款人" if recipient else "请选择相同网络的收款地址"
            )
        except ValueError:
            valid_address = False
            self.address_hint.setText("地址格式或大小写校验不正确")
        valid_amount = bool(re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", amount)) and any(
            char in "123456789" for char in amount
        )
        self.send_button.setEnabled(
            bool(self.sender()) and valid_address and valid_amount and not self.busy
        )

    def _load_balance(self):
        if self.stack.currentIndex() != 0:
            return
        self.balance.setText("选择本地钱包后显示余额")
        if not self.sender():
            return
        key = (self.network_name(), self.sender(), self.selected_asset())
        symbol = self.selected_asset().symbol
        self.balance.setText("正在查询余额…")

        def loaded(value):
            if key == (self.network_name(), self.sender(), self.selected_asset()):
                amount = value["amount"]
                display = amount if len(amount) <= 23 else amount[:23] + "…"
                self.balance.setText(f"可用余额  {display} {symbol}")
                self.balance.setToolTip(f"{amount} {symbol}")

        def failed(error):
            if key == (self.network_name(), self.sender(), self.selected_asset()):
                self.balance.setText("余额暂时无法查询，请检查网络或 RPC 设置")

        self._task(
            self.backend.balance,
            (self.network_name(), self.sender(), self.selected_asset()),
            loaded,
            failed,
        )

    def _set_busy(self, busy, title=None):
        self.busy = busy
        for widget in (
            self.network,
            self.asset,
            self.account,
            self.recipient,
            self.amount,
            self.add_token,
            self.paste_button,
        ):
            widget.setEnabled(not busy)
        self.manage_wallet_button.setEnabled(not busy)
        self.send_button.setText(title or "下一步：预览转账    →")
        self._edited()
        self._update_history_actions()

    def _prepare(self):
        if self.wallet_page.busy:
            self.notify("钱包库正在处理，请稍候再转账。")
            return
        if self.batch_page.running or self.batch_page.previewing:
            self.notify("批量任务正在执行，请先暂停后再进行单次转账。")
            return
        if self.busy:
            return
        vault = self.wallet_page.vault
        if not vault.unlocked or not self.sender():
            self.notify("请先解锁钱包库并选择一个本地钱包。")
            return
        vault_generation = vault.session_generation
        args = (
            self.network_name(),
            self.sender(),
            self.recipient.text().strip(),
            self.amount.text().strip(),
            self.selected_asset(),
        )
        self._set_busy(True, "正在校验与预估…")
        generation = self.form_generation

        def failed(error):
            self._set_busy(False)
            self.load_history()
            self._show_error(error)

        def prepared(quote):
            if generation != self.form_generation:
                self._set_busy(False)
                self.notify("钱包或网络已变化，请重新预览。")
                return
            self.quote = quote
            fee = int(quote.prepared.preview["max_execution_fee_wei"])
            native = NETWORKS[quote.network].symbol
            self.preview_fee.setText(f"{format_units(fee)} {native}")
            if quote.asset.contract:
                self.preview_total.setText(
                    f"{quote.prepared.request.amount} {quote.asset.symbol}\n+ 最多 {format_units(fee)} {native} 执行手续费"
                )
            else:
                total = int(quote.prepared.preview["amount_units"]) + fee
                self.preview_total.setText(
                    f"转账数量 + 预估执行手续费\n{format_units(total)} {native}"
                )
            self.confirm_dialog = ConfirmDialog(quote, self)
            try:
                accepted = self.confirm_dialog.exec() == QDialog.DialogCode.Accepted
            finally:
                self.confirm_dialog.deleteLater()
                self.confirm_dialog = None
            if not accepted:
                record_operation(
                    "转账确认已取消",
                    发送地址=quote.prepared.request.sender,
                    收款地址=quote.prepared.request.recipient,
                    钱包库状态="已解锁" if vault.unlocked else "已锁定",
                )
                self._set_busy(False)
                return
            if (
                generation != self.form_generation
                or self.sender() != quote.prepared.request.sender
                or not vault.unlocked
                or vault.session_generation != vault_generation
            ):
                failed(UserError("钱包或网络已变化，请重新预览。"))
                return
            self.wallet_page._touch()
            self.send_button.setText("正在本地签名并发送…")
            self._task(
                self.backend.submit_local,
                (quote, vault, vault_generation, "local-" + uuid.uuid4().hex),
                self._local_submitted,
                failed,
            )

        self._task(self.backend.prepare, args, prepared, failed)

    def _local_submitted(self, record):
        self._set_busy(False)
        self.steps.setText("①  填写完成     ───     ②  已本地签名     ───     ③  查看交易结果")
        self._received(record)
        self._task(
            self.backend.refresh, (record["request_id"],), self._received, self._receive_failed
        )

    def _received(self, record):
        self.navigate(1)
        message = STATES.get(record["state"], "交易结果待确认")
        if record["state"] in {"unknown", "signed"}:
            message += "。请先查询状态，必要时重播原交易，切勿重复付款。"
        self.notify(message)
        self.balance_timer.start()

    def _receive_failed(self, error):
        self.load_history()
        self.navigate(1)
        self._show_error(error)

    def load_history(self):
        self._task(self.backend.history, success=self._show_history)

    def _show_history(self, records):
        selected = self._selected_record()
        selected_column = self.history_table.currentColumn()
        self.records = records
        self.checked_request_ids.intersection_update(record["request_id"] for record in records)
        self.history_table.blockSignals(True)
        self.history_table.setRowCount(len(records))
        for index, record in enumerate(records):
            details = record["details"]
            values = [
                format_beijing_time(record["created_at"]),
                HISTORY_NETWORKS[record["network"]].title,
                f"{details['amount']} {details.get('symbol', details['asset'])}",
                STATES.get(record["state"], record["state"]),
                record["sender"],
                details["to"],
                record["tx_hash"] or "",
            ]
            checkbox = QTableWidgetItem()
            checkbox.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            checkbox.setData(Qt.ItemDataRole.UserRole, record["request_id"])
            checkbox.setCheckState(
                Qt.CheckState.Checked
                if record["request_id"] in self.checked_request_ids
                else Qt.CheckState.Unchecked
            )
            self.history_table.setItem(index, 0, checkbox)
            for column, value in enumerate(values, 1):
                display = value
                if column in HISTORY_COPY_FIELDS:
                    display = (value[:6] + "…" + value[-4:]) if len(value) > 15 else value or "—"
                item = QTableWidgetItem(display)
                item.setData(Qt.ItemDataRole.UserRole, value)
                tooltip = value or "尚未获得交易哈希"
                if details.get("batch_id"):
                    tooltip += (
                        f"\n所属批次：{details['batch_id']} · 第 {details['batch_position']} 笔"
                    )
                if column in HISTORY_COPY_FIELDS and value:
                    tooltip += "\n双击或右键复制完整内容"
                item.setToolTip(tooltip)
                self.history_table.setItem(index, column, item)
            self.history_table.setRowHeight(index, 52)
            if selected and selected["request_id"] == record["request_id"]:
                self.history_table.setCurrentCell(index, max(0, selected_column))
        self.history_table.blockSignals(False)
        self._update_history_actions()
        unresolved = sum(
            record["state"] not in {"confirmed", "failed", "rejected"} for record in records
        )
        self.history_hint.setText(
            f"共 {len(records)} 笔记录，{unresolved} 笔结果待确认。"
            "地址和哈希可悬浮查看完整内容，双击或右键复制。"
            if records
            else "尚无转账记录。"
        )

    def _copy_history_value(self, value, label):
        if not value:
            self.statusBar().showMessage("暂无可复制的" + label)
            return
        QApplication.clipboard().setText(value)
        self._notify_copied(label)

    def _notify_copied(self, label):
        self.copy_toast.show_message(label + "已复制")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "copy_toast"):
            self.copy_toast.reposition()

    def _copy_history_cell(self, row, column):
        item = self.history_table.item(row, column)
        if item is None or column == 0:
            return
        if column in HISTORY_COPY_FIELDS:
            self._copy_history_value(
                item.data(Qt.ItemDataRole.UserRole), HISTORY_COPY_FIELDS[column]
            )
        else:
            self._copy_record()

    def _history_context_menu(self, position):
        item = self.history_table.itemAt(position)
        if item is None or item.column() not in HISTORY_COPY_FIELDS:
            return
        self.history_table.setCurrentCell(item.row(), item.column())
        value = item.data(Qt.ItemDataRole.UserRole)
        label = HISTORY_COPY_FIELDS[item.column()]
        menu = QMenu(self.history_table)
        action = menu.addAction("复制" + label)
        action.setEnabled(bool(value))
        # Capture the full value now; polling may refresh/reorder rows while the menu is open.
        action.triggered.connect(lambda: self._copy_history_value(value, label))
        record = self._selected_record()
        batch_id = record["details"].get("batch_id") if record else None
        if batch_id:
            batch = menu.addAction("查看所属批次")
            batch.triggered.connect(lambda: self._open_batch(batch_id))
        menu.exec(self.history_table.viewport().mapToGlobal(position))
        menu.deleteLater()

    def _open_batch(self, batch_id):
        self.navigate(4)
        self.batch_page.reload_history(batch_id)

    def _checked_records(self):
        return [
            record for record in self.records if record["request_id"] in self.checked_request_ids
        ]

    def _history_check_changed(self, item):
        if item.column() != 0:
            return
        request_id = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked:
            self.checked_request_ids.add(request_id)
        else:
            self.checked_request_ids.discard(request_id)
        self._update_history_actions()

    def _check_all_history(self):
        checked = self.history_select_all.checkState() != Qt.CheckState.Unchecked
        self.checked_request_ids = (
            {record["request_id"] for record in self.records} if checked else set()
        )
        self.history_table.blockSignals(True)
        for row_index in range(self.history_table.rowCount()):
            self.history_table.item(row_index, 0).setCheckState(
                Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            )
        self.history_table.blockSignals(False)
        self._update_history_actions()

    def _update_history_actions(self):
        count = len(self._checked_records())
        self.history_checked_count.setText(f"已勾选 {count} 笔")
        self.history_refresh_button.setEnabled(
            bool(count)
            and not self.busy
            and not self.manual_refresh_active
            and not self.manual_refresh_requested
        )
        self.history_rebroadcast_button.setEnabled(bool(count) and not self.busy)
        state = (
            Qt.CheckState.Unchecked
            if not count
            else Qt.CheckState.Checked
            if count == len(self.records)
            else Qt.CheckState.PartiallyChecked
        )
        self.history_select_all.setCheckState(state)
        self.history_select_all.setEnabled(bool(self.records))

    def _selected_record(self):
        index = self.history_table.currentRow()
        return self.records[index] if 0 <= index < len(self.records) else None

    def poll(self, force=False, request_ids=None):
        if self.closing:
            return
        if force:
            request_ids = (
                tuple(request_ids)
                if request_ids is not None
                else tuple(record["request_id"] for record in self._checked_records())
            )
            if not request_ids:
                self.notify("请先勾选需要刷新状态的交易。")
                self._update_history_actions()
                return
        if self.poll_busy:
            if force:
                if self.manual_refresh_active:
                    self.notify("正在刷新勾选的交易，请稍候。")
                else:
                    self.manual_refresh_requested = True
                    self.pending_refresh_ids = request_ids
                    self.history_refresh_button.setEnabled(False)
                    self.history_refresh_button.setText("等待后台查询…")
                    self.notify("后台正在查询，已安排随后刷新已勾选的交易。")
            return
        if not force and not any(
            record.get("tx_hash")
            and record["network"] in NETWORKS
            and record["state"] not in {"confirmed", "failed", "mismatched"}
            for record in self.records
        ):
            return
        self.poll_busy = True
        self.manual_refresh_active = force
        if force:
            self.history_refresh_button.setEnabled(False)
            self.history_refresh_button.setText("正在刷新…")
            self.notify("正在刷新勾选的交易，请稍候。")

        def finished():
            self.poll_busy = False
            self.manual_refresh_active = False
            if self.manual_refresh_requested:
                self.manual_refresh_requested = False
                pending = self.pending_refresh_ids
                self.pending_refresh_ids = None
                self.poll(force=True, request_ids=pending)
            else:
                self.history_refresh_button.setText("刷新链上状态")
                self._update_history_actions()

        def done(result):
            if force:
                self._show_history(result["records"])
                if result["checked"]:
                    message = (
                        f"刷新完成：查询 {result['checked']} 笔，"
                        f"状态变化 {result['changed']} 笔，查询失败 {result['failed']} 笔。"
                    )
                    if result["failed"]:
                        message += "失败详情已记录到日志。"
                else:
                    message = "勾选的交易暂无可查询的链上状态。"
                if result["skipped"]:
                    message += f"已跳过 {result['skipped']} 笔无哈希或已移除网络的记录。"
                self.notify(message)
            else:
                self._show_history(result)
            finished()

        def failed(error):
            if force:
                self._show_error(error)
            finished()

        if force:
            self._task(self.backend.refresh_selected, (request_ids,), success=done, failure=failed)
        else:
            self._task(self.backend.poll, success=done, failure=failed)

    def _rebroadcast(self):
        if self.busy:
            self.notify("有交易操作正在进行，请稍候。")
            return
        records = self._checked_records()
        if not records:
            self.notify("请先勾选需要重播的交易。")
            return
        eligible = [
            record
            for record in records
            if record.get("source") == "local"
            and record["network"] in NETWORKS
            and record["state"] in {"signed", "unknown", "submitted"}
        ]
        if not eligible:
            self.notify("勾选的交易均无需重播。仅待广播或结果不明确的本地交易可重播。")
            return
        request_ids = tuple(record["request_id"] for record in records)
        if (
            QMessageBox.question(
                self,
                "重播勾选的原交易",
                f"已勾选 {len(records)} 笔，其中 {len(eligible)} 笔可尝试重播，其余将跳过。\n"
                "仅广播各笔交易已保存的原签名，不会创建新付款或提高手续费。确认继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._set_busy(True, "正在重播原交易…")
        self.history_rebroadcast_button.setText("正在重播…")
        self.notify("正在处理勾选的原交易，请稍候。")

        def finished():
            self._set_busy(False)
            self.history_rebroadcast_button.setText("重播原交易")

        def done(result):
            self._show_history(result["records"])
            finished()
            self.notify(
                f"重播处理完成：处理 {result['processed']} 笔，异常 {result['failed']} 笔，跳过 {result['skipped']} 笔。请查看交易状态。"
            )
            self.balance_timer.start()

        def failed(error):
            finished()
            self._receive_failed(error)

        self._task(self.backend.rebroadcast_selected, (request_ids,), done, failed)

    def _open_explorer(self):
        record = self._selected_record()
        if record and record["tx_hash"]:
            QDesktopServices.openUrl(QUrl(self.backend.explorer(record)))
        else:
            self.notify("请选择一笔已有交易哈希的记录。")

    def _copy_record(self):
        record = self._selected_record()
        if not record:
            return
        details = record["details"]
        QApplication.clipboard().setText(
            f"请求编号：{record['request_id']}\n时间（北京时间 UTC+8）：{format_beijing_time(record['created_at'])}\n"
            f"网络：{HISTORY_NETWORKS[record['network']].title}\n"
            f"发送地址：{record['sender']}\n收款地址：{details['to']}\n"
            f"数量：{details['amount']} {details.get('symbol', '')}\n"
            f"交易哈希：{record['tx_hash'] or '尚未获得'}\n状态：{STATES.get(record['state'], record['state'])}"
        )
        self._notify_copied("交易详情")

    def _save_settings(self):
        if self.busy or self.batch_page.running or self.batch_page.previewing:
            self.notify("请等待当前钱包请求完成后再修改设置。")
            return
        try:
            preferences = replace(
                self.preferences,
                network=self.network_name(),
                rpc_overrides={
                    name: field.text().strip()
                    for name, field in self.rpc_inputs.items()
                    if field.text().strip()
                },
            )
            save_preferences(self.directory, preferences)
        except (ValueError, OSError) as error:
            self._show_error(error)
            return
        self.preferences = preferences
        self.backend = DesktopService(self.directory, preferences, cancelled=self.stop_event)
        self.batch_page.service.backend = self.backend
        self.notify("设置已保存。")
        self._edited()
        self.balance_timer.start()

    def _add_token(self, checked=False, *, network=None, parent=None):
        if self.busy or self.batch_page.running or self.batch_page.previewing:
            self.notify("请先完成预览或暂停转账任务，再添加代币。")
            return
        name = network or self.network_name()
        dialog = TokenDialog(parent or self)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            symbol, contract = dialog.symbol.text().strip(), dialog.contract.text().strip()
            temporary = replace(
                self.preferences,
                custom_assets={name: [asdict(Asset(symbol, contract))]},
            )
            temporary.validate()
        except ValueError as error:
            self._show_error(error)
            return
        finally:
            dialog.deleteLater()

        def added(asset):
            custom = {key: list(value) for key, value in self.preferences.custom_assets.items()}
            existing = self.preferences.assets(name)
            if any(
                item.contract and item.contract.lower() == asset.contract.lower()
                for item in existing
            ):
                self.notify("此代币已在资产列表中。")
                return
            custom.setdefault(name, []).append(asdict(asset))
            preferences = replace(self.preferences, custom_assets=custom)
            try:
                save_preferences(self.directory, preferences)
            except OSError as error:
                self._show_error(error)
                return
            self.preferences = preferences
            self.backend = DesktopService(
                self.directory, self.preferences, cancelled=self.stop_event
            )
            self.batch_page.service.backend = self.backend
            self.batch_page.assets_changed()
            self.assets_changed.emit(name)
            if name == self.network_name():
                self._network_changed()
                self.asset.setCurrentIndex(self.asset.count() - 1)
            self.notify("代币已添加，请在发送前再次核对完整合约地址。")

        self._task(self.backend.inspect_token, (name, symbol, contract), added)

    def closeEvent(self, event):
        if self.force_close:
            event.accept()
            return
        if self.closing:
            event.ignore()
            return
        if self.busy or self.batch_page.running:
            answer = QMessageBox.question(
                self,
                "仍有操作正在进行",
                "关闭应用不会取消已签名或已发送的交易。\n下次启动可在交易记录中查询结果。确定关闭吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        event.ignore()
        self.request_shutdown()

    def request_shutdown(self):
        """Terminal stop already expresses intent to exit; never show a confirmation dialog."""
        if self.closing:
            return
        self.closing = True
        self.batch_page.shutdown()
        self.wallet_page.shutdown()
        self.stop_event.set()
        self.poll_timer.stop()
        self.balance_timer.stop()
        self.setEnabled(False)
        for dialog in QApplication.topLevelWidgets():
            if isinstance(dialog, QDialog) and self.isAncestorOf(dialog):
                dialog.reject()
        self.hide()
        self._finish_close()

    def _finish_close(self):
        if self.pool.activeThreadCount() or self.workers:
            QTimer.singleShot(100, self._finish_close)
        else:
            self.force_close = True
            self.close()
            # The window may already be hidden and a modal loop may have been open;
            # do not rely on lastWindowClosed to terminate QApplication.
            QApplication.instance().quit()
