"""Public balance discovery and wallet selection, without decrypting private keys."""

from decimal import Decimal, InvalidOperation
from threading import Event

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialogButtonBox,
    QLineEdit,
    QProgressBar,
    QPushButton,
)

from ..errors import UserError
from .batch_page import WalletPicker, item
from .rpc import OperationCancelled
from .service import friendly_error
from .settings import NETWORKS
from .widgets import row, text


class WalletBalanceDialog(WalletPicker):
    result_ready = Signal(int, object)

    def __init__(
        self,
        wallets,
        window,
        parent=None,
        *,
        title="选择发送钱包",
        network=None,
        query_only=False,
        selection_limit=100,
    ):
        super().__init__(wallets, parent or window, title=title)
        self.window = window
        self.query_only = query_only
        self.resize(1100, 720)
        self.results = {}
        self.assets = []
        self.query_addresses = set()
        self.serial = 0
        self.operation = None
        self.closed = False
        self.active = False
        self.selection_limit = selection_limit
        self.filter_valid = True
        self.network = QComboBox()
        for name, info in NETWORKS.items():
            self.network.addItem(info.title, name)
        self.network.setCurrentIndex(self.network.findData(network or window.preferences.network))
        # Selection inside a transfer must use that transfer's network.
        self.network.setEnabled(query_only)
        self.query_button = QPushButton("查询当前搜索 / 分组的钱包")
        self.query_button.setObjectName("primary")
        self.query_button.clicked.connect(self.query)
        self.cancel_button = QPushButton("停止查询")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_query)
        self.filter_asset = QComboBox()
        self.minimum = QLineEdit()
        self.minimum.setMaxLength(100)
        self.minimum.setMaximumWidth(180)
        self.minimum.setPlaceholderText("最低余额（含）")
        self.positive = QCheckBox("仅显示该资产余额 > 0")
        self.has_native = QCheckBox("原生币余额 > 0")
        self.feedback = text("余额为查询时的链上快照，后续变化请重新查询。", "muted", True)
        self.progress = QProgressBar()
        self.progress.hide()
        layout = self.layout()
        layout.insertWidget(
            0,
            row(
                text("查询网络", "fieldLabel"),
                self.network,
                self.query_button,
                self.cancel_button,
                None,
            ),
        )
        layout.insertWidget(
            2, row(self.filter_asset, self.minimum, self.positive, self.has_native, None)
        )
        layout.insertWidget(3, self.feedback)
        layout.insertWidget(4, self.progress)
        self.table.horizontalHeader().setMinimumSectionSize(90)
        self.table.setColumnWidth(0, 50)
        self.table.setWordWrap(False)
        self.result_ready.connect(self._result)
        self.window.wallet_page.state_changed.connect(self._vault_changed)
        self.network.currentIndexChanged.connect(self.reset_network)
        self.filter_asset.currentIndexChanged.connect(self.filter)
        self.minimum.textChanged.connect(self.filter)
        self.positive.toggled.connect(self.filter)
        self.has_native.toggled.connect(self.filter)
        buttons = self.findChild(QDialogButtonBox)
        if query_only:
            buttons.setStandardButtons(QDialogButtonBox.StandardButton.Close)
            buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
            self.table.setColumnHidden(0, True)
            self.select_all_button.hide()
            self.confirm_button = None
        else:
            self.confirm_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
            self.confirm_button.setText("使用所选钱包")
            buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("关闭")
        self.reset_network()

    def _vault_changed(self, *_):
        if not self.window.wallet_page.vault.unlocked:
            self.reject()

    def reset_network(self):
        self.cancel_query()
        self.serial += 1
        self.results.clear()
        self.assets = list(self.window.preferences.assets(self.network.currentData()))
        self.table.setColumnCount(4 + len(self.assets))
        self.table.setHorizontalHeaderLabels(
            ["选择", "钱包名称", "地址", "分组"]
            + [
                asset.symbol + (" · 原生币" if not asset.contract else " · ERC-20")
                for asset in self.assets
            ]
        )
        self.filter_asset.blockSignals(True)
        self.filter_asset.clear()
        for asset in self.assets:
            self.filter_asset.addItem(asset.symbol, asset.contract)
        self.filter_asset.blockSignals(False)
        for column, asset in enumerate(self.assets, 4):
            self.table.horizontalHeaderItem(column).setToolTip(
                asset.contract or NETWORKS[self.network.currentData()].symbol + " 原生币"
            )
        for index in range(len(self.wallets)):
            for column in range(4, 4 + len(self.assets)):
                self.table.setItem(index, column, item("未查询"))
        self.feedback.setText("查询所选网络的原生币和已配置代币；自定义代币可在转账页中添加。")
        self.filter()

    def matches_search(self, wallet):
        query, group = self.search.text().strip().casefold(), self.group.currentData()
        return query in " ".join(
            str(wallet[key]) for key in ("name", "address", "group_name")
        ).casefold() and (not group or wallet["group_name"] == group)

    def filter(self, *_):
        if not hasattr(self, "results"):
            return super().filter()
        raw = self.minimum.text().strip()
        try:
            minimum = Decimal(raw or "0")
            if not minimum.is_finite() or minimum < 0:
                raise InvalidOperation
        except InvalidOperation:
            self.filter_valid = False
            if self.confirm_button is not None:
                self.confirm_button.setEnabled(False)
            self.feedback.setText("最低余额请输入大于等于 0 的数字。")
            return
        self.filter_valid = True
        if self.confirm_button is not None:
            self.confirm_button.setEnabled(True)
        if self.feedback.text() == "最低余额请输入大于等于 0 的数字。":
            self.feedback.setText("余额为查询时的链上快照，后续变化请重新查询。")
        asset = self.filter_asset.currentData()
        needs_balance = bool(raw) or self.positive.isChecked()
        for index, wallet in enumerate(self.wallets):
            matches = self.matches_search(wallet)
            result = self.results.get(wallet["address"].lower(), {})
            balances = result.get("balances", {})
            if needs_balance:
                value = balances.get(asset)
                matches = matches and value is not None and Decimal(value) >= minimum
                if self.positive.isChecked():
                    matches = matches and value is not None and Decimal(value) > 0
            if self.has_native.isChecked():
                value = balances.get(None)
                matches = matches and value is not None and Decimal(value) > 0
            self.table.setRowHidden(index, not matches)

    def query(self):
        if self.active or self.closed:
            return
        senders = [wallet["address"] for wallet in self.wallets if self.matches_search(wallet)]
        if not senders:
            self.feedback.setText("当前搜索 / 分组没有钱包。")
            return
        if len(senders) > 1000:
            self.feedback.setText("每次最多查询 1000 个钱包，请按分组或名称缩小范围。")
            return
        self.serial += 1
        serial = self.serial
        self.query_addresses = {sender.lower() for sender in senders}
        self.operation = Event()
        operation = self.operation
        self.active = True
        self.query_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress.setRange(0, len(senders))
        self.progress.setValue(0)
        self.progress.show()
        for sender in senders:
            self.results.pop(sender.lower(), None)
        for index, wallet in enumerate(self.wallets):
            if self.matches_search(wallet):
                for column in range(4, 4 + len(self.assets)):
                    self.table.setItem(index, column, item("等待查询"))
        self.feedback.setText(f"正在查询 {len(senders)} 个钱包…")
        self.filter()
        name, assets = self.network.currentData(), tuple(self.assets)

        def work():
            return self.window.backend.bulk_balances(
                name,
                senders,
                assets,
                operation,
                progress=lambda result: self.result_ready.emit(serial, result),
            )

        def finish(results=None, error=None):
            if self.closed or serial != self.serial:
                return
            self.active = False
            self.query_button.setEnabled(True)
            self.cancel_button.setEnabled(False)
            if error is not None:
                for index, wallet in enumerate(self.wallets):
                    if (
                        wallet["address"] in senders
                        and wallet["address"].lower() not in self.results
                    ):
                        for column in range(4, 4 + len(self.assets)):
                            self.table.setItem(index, column, item("未完成"))
                message = (
                    "查询已停止，已完成的结果仍可查看。"
                    if isinstance(error, OperationCancelled)
                    else friendly_error(error)
                    if isinstance(error, UserError)
                    else "余额查询未完成，请检查网络或 RPC 设置后重试；已完成的结果仍可查看。"
                )
            else:
                # Also consume results here in case queued progress is still pending.
                for result in results:
                    self._result(serial, result)
                failed = sum(bool(result["errors"]) for result in results)
                message = f"查询完成：{len(results)} 个钱包，{failed} 个含查询失败项。"
                if results:
                    message += f" 区块：{results[0]['block']}。余额不含后续交易变化。"
            self.feedback.setText(message)
            self.filter()

        self.window._task(
            work,
            success=lambda results: finish(results=results),
            failure=lambda error: finish(error=error),
        )

    def _result(self, serial, result):
        if self.closed or serial != self.serial:
            return
        self.results[result["address"].lower()] = result
        for index, wallet in enumerate(self.wallets):
            if wallet["address"].lower() != result["address"].lower():
                continue
            for column, asset in enumerate(self.assets, 4):
                value = result["balances"].get(asset.contract)
                cell = item(value if value is not None else "查询失败")
                cell.setToolTip(result["errors"].get(asset.contract, str(value)))
                self.table.setItem(index, column, cell)
            break
        self.progress.setValue(sum(key in self.results for key in self.query_addresses))
        self.filter()

    def cancel_query(self):
        if self.operation is not None:
            self.operation.set()
        self.active = False
        self.query_button.setEnabled(True)
        self.cancel_button.setEnabled(False)

    def selected(self):
        # Hidden checks must not silently become senders after changing filters.
        return [
            wallet["address"]
            for index, wallet in enumerate(self.wallets)
            if not self.table.isRowHidden(index)
            and self.table.item(index, 0).checkState() == Qt.CheckState.Checked
        ]

    def accept(self):
        if self.query_only or not self.filter_valid:
            return
        count = len(self.selected())
        if not count:
            self.feedback.setText("请先勾选要使用的钱包。")
            return
        if count > self.selection_limit:
            self.feedback.setText(f"本次最多可选择 {self.selection_limit} 个钱包，请减少勾选。")
            return
        super().accept()

    def done(self, result):
        self.closed = True
        self.serial += 1
        self.cancel_query()
        super().done(result)
