"""Persistent, public balance snapshots inside wallet management."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from threading import Event

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from ..errors import UserError
from ..time_format import format_beijing_time
from .rpc import OperationCancelled
from .service import friendly_error
from .settings import NETWORKS
from .widgets import box, row, text


@dataclass
class BalanceCell:
    status: str = "未查询"
    value: str | None = None
    error: str = ""
    queried_at: str = ""
    block: int | None = None


class BalanceItem(QTableWidgetItem):
    def __init__(self, cell):
        super().__init__(cell.value if cell.status == "已查询" else cell.status)
        self.number = Decimal(cell.value) if cell.status == "已查询" else None
        self.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.setToolTip(
            "\n".join(
                filter(
                    None,
                    (
                        cell.error,
                        f"查询时间：{format_beijing_time(cell.queried_at)}"
                        if cell.queried_at
                        else "",
                        f"区块：{cell.block}" if cell.block is not None else "",
                    ),
                )
            )
        )

    def __lt__(self, other):
        if isinstance(other, BalanceItem):
            return (self.number is None, self.number or Decimal(0), self.text()) < (
                other.number is None,
                other.number or Decimal(0),
                other.text(),
            )
        return super().__lt__(other)


class QueryAssetsDialog(QDialog):
    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self.name = page.network.currentData()
        self.setWindowTitle("管理查询资产 · " + NETWORKS[self.name].title)
        self.resize(620, 420)
        layout = box(self, margins=(24, 24, 24, 24), spacing=14)
        layout.addWidget(text("查询原生币和勾选的代币", "brand"))
        layout.addWidget(
            text("添加代币后也可在转账中使用；取消勾选仅影响本页查询。", "muted", True)
        )
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.entries = QWidget()
        self.entries_layout = box(self.entries, spacing=10)
        scroll.setWidget(self.entries)
        layout.addWidget(scroll, 1)
        self.checks = {}
        self.populate(page.chosen[self.name])
        add = QPushButton("添加代币")
        add.clicked.connect(lambda: page.window._add_token(network=self.name, parent=self))
        layout.addWidget(row(add, None))
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("应用")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        page.window.assets_changed.connect(self.assets_changed)

    def populate(self, chosen):
        while self.entries_layout.count():
            entry = self.entries_layout.takeAt(0)
            if entry.widget():
                entry.widget().deleteLater()
        self.checks = {}
        for asset in self.page.window.preferences.assets(self.name):
            check = QCheckBox(
                asset.symbol + (" · " + asset.contract if asset.contract else " · 原生币")
            )
            check.setChecked(asset.contract in chosen or asset.contract is None)
            check.setEnabled(asset.contract is not None)
            check.setToolTip(asset.contract or "原生币同时用于支付网络手续费")
            self.checks[asset.contract] = check
            self.entries_layout.addWidget(check)
        self.entries_layout.addStretch()

    def assets_changed(self, name):
        if name == self.name:
            selected = self.selected() | (
                {asset.contract for asset in self.page.window.preferences.assets(name)}
                - self.checks.keys()
            )
            self.populate(selected)

    def selected(self):
        return {contract for contract, check in self.checks.items() if check.isChecked()}


class WalletBalancePage(QWidget):
    result_ready = Signal(int, object)

    def __init__(self, window, wallet_page):
        super().__init__(wallet_page)
        self.setObjectName("walletBalancePage")
        self.window, self.wallet_page = window, wallet_page
        self.wallets = []
        self.scope = set()
        self.cache = {name: {} for name in NETWORKS}
        self.chosen = {
            name: {asset.contract for asset in window.preferences.assets(name)} for name in NETWORKS
        }
        self.known_assets = {name: set(chosen) for name, chosen in self.chosen.items()}
        self.messages = {name: "选择查询范围后开始查询。" for name in NETWORKS}
        self.serial = 0
        self.active = False
        self.operation = None
        self.pending = {}
        self.completed = set()
        self.filter_valid = True
        self._rendering = False
        self._build()
        self.result_ready.connect(self._result)
        self.window.assets_changed.connect(self.assets_changed)
        self.reset_network()

    @property
    def results(self):
        return self.cache[self.network.currentData()]

    @property
    def assets(self):
        name = self.network.currentData()
        return [
            asset
            for asset in self.window.preferences.assets(name)
            if asset.contract in self.chosen[name]
        ]

    def _build(self):
        layout = box(self, margins=(0, 10, 0, 0), spacing=8)
        self.network = QComboBox()
        for name, info in NETWORKS.items():
            self.network.addItem(info.title, name)
        self.network.setCurrentIndex(self.network.findData(self.window.preferences.network))
        self.network.setAccessibleName("余额查询网络")
        self.network.setFixedWidth(155)
        self.manage_assets_button = QPushButton("管理资产")
        self.manage_assets_button.setToolTip("选择查询资产或添加自定义代币。")
        self.manage_assets_button.clicked.connect(self.manage_assets)
        self.query_button = QPushButton()
        self.query_button.setObjectName("primary")
        self.query_button.clicked.connect(self.query)
        self.cancel_button = QPushButton("停止查询")
        self.cancel_button.clicked.connect(self.cancel_query)
        self.retry_button = QPushButton("重试失败项")
        self.retry_button.clicked.connect(self.retry_failed)
        self.retry_button.setToolTip("重试当前搜索 / 分组中的失败项，包括被余额条件隐藏的项。")
        layout.addWidget(
            row(
                text("网络", "fieldLabel"),
                self.network,
                self.manage_assets_button,
                None,
                self.query_button,
                self.cancel_button,
                self.retry_button,
            )
        )
        self.filter_asset = QComboBox()
        self.filter_asset.setAccessibleName("余额筛选资产")
        self.minimum = QLineEdit()
        self.minimum.setPlaceholderText("最低余额（含）")
        self.minimum.setAccessibleName("最低余额（含）")
        self.minimum.setMaxLength(100)
        self.minimum.setMaximumWidth(150)
        self.positive = QCheckBox("该资产余额 > 0")
        self.has_native = QCheckBox("原生币余额 > 0")
        layout.addWidget(
            row(
                text("筛选结果", "fieldLabel"),
                self.filter_asset,
                self.minimum,
                self.positive,
                self.has_native,
                None,
            )
        )
        self.filter_error = text("最低余额请输入大于等于 0 的数字。", "importFeedback", True)
        self.filter_error.setProperty("severity", "error")
        self.filter_error.hide()
        layout.addWidget(self.filter_error)
        self.feedback = text("", "fieldLabel", True)
        layout.addWidget(self.feedback)
        self.progress = QProgressBar()
        self.progress.setFormat("查询进度：%v / %m 个钱包（%p%）")
        self.progress.hide()
        layout.addWidget(self.progress)
        self.table = QTableWidget()
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(44)
        self.table.setWordWrap(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setMinimumSectionSize(44)
        self.table.horizontalHeader().sortIndicatorChanged.connect(self.filter)
        self.table.itemChanged.connect(self._selection_changed)
        layout.addWidget(self.table, 1)
        self.select_all_button = QPushButton("勾选当前结果")
        self.select_all_button.clicked.connect(self.select_visible)
        self.clear_button = QPushButton("清除勾选")
        self.clear_button.clicked.connect(lambda: self.set_selected([]))
        self.count = text("", "muted", True)
        layout.addWidget(row(self.select_all_button, self.clear_button, self.count, None))
        layout.addWidget(
            text(
                "余额不会自动更新；悬停查看时间及区块。未勾选时查询当前搜索 / 分组。",
                "muted",
                True,
            )
        )
        self.network.currentIndexChanged.connect(self.reset_network)
        self.filter_asset.currentIndexChanged.connect(self.filter)
        self.minimum.textChanged.connect(self.filter)
        self.positive.toggled.connect(self.filter)
        self.has_native.toggled.connect(self.filter)

    def set_wallets(self, wallets, scope):
        changed = wallets != self.wallets
        self.wallets = wallets
        self.scope = {address.lower() for address in scope}
        if changed:
            self.render()
        else:
            self.filter()

    def set_selected(self, addresses):
        chosen = {address.lower() for address in addresses}
        self._rendering = True
        self.table.setSortingEnabled(False)
        for index in range(self.table.rowCount()):
            cell = self.table.item(index, 0)
            cell.setCheckState(
                Qt.CheckState.Checked
                if cell.data(Qt.ItemDataRole.UserRole) in chosen
                else Qt.CheckState.Unchecked
            )
        self.table.setSortingEnabled(True)
        self._rendering = False
        self._selection_changed()

    def selected(self):
        return [
            self.table.item(index, 2).data(Qt.ItemDataRole.UserRole)
            for index in range(self.table.rowCount())
            if not self.table.isRowHidden(index)
            and self.table.item(index, 0).checkState() == Qt.CheckState.Checked
        ]

    def select_visible(self):
        self.set_selected(
            [
                self.table.item(index, 2).data(Qt.ItemDataRole.UserRole)
                for index in range(self.table.rowCount())
                if not self.table.isRowHidden(index)
            ]
        )

    def reset_network(self):
        self.cancel_query()
        self.progress.hide()
        self.minimum.clear()
        self.positive.setChecked(False)
        self.has_native.setChecked(False)
        self.render(preserve_selection=False)

    def assets_changed(self, name):
        available = {asset.contract for asset in self.window.preferences.assets(name)}
        self.chosen[name].update(available - self.known_assets[name])
        self.known_assets[name] = available
        if name == self.network.currentData():
            self.render()

    def manage_assets(self):
        dialog = QueryAssetsDialog(self)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.chosen[dialog.name] = dialog.selected()
                self.render()
        finally:
            dialog.deleteLater()

    def render(self, preserve_selection=True):
        selected = self.selected() if preserve_selection else []
        selected_asset = self.filter_asset.currentData()
        self._rendering = True
        self.table.setSortingEnabled(False)
        self.table.clearContents()
        assets = self.assets
        self.table.setColumnCount(5 + len(assets))
        self.table.setHorizontalHeaderLabels(
            ["选择", "钱包名称", "地址", "分组"]
            + [asset.symbol for asset in assets]
            + ["查询时间（北京时间）"]
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for column, width in enumerate((48, 135, 155, 90)):
            self.table.setColumnWidth(column, width)
        for column, asset in enumerate(assets, 4):
            self.table.setColumnWidth(column, 112)
            self.table.horizontalHeaderItem(column).setToolTip(asset.contract or "原生币")
        self.table.setColumnWidth(4 + len(assets), 160)
        self.table.setRowCount(len(self.wallets))
        for index, wallet in enumerate(self.wallets):
            key = wallet["address"].lower()
            check = QTableWidgetItem()
            check.setFlags(check.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            check.setData(Qt.ItemDataRole.UserRole, key)
            check.setCheckState(Qt.CheckState.Unchecked)
            self.table.setItem(index, 0, check)
            for column, field in enumerate(("name", "address", "group_name"), 1):
                value = wallet[field]
                label = value[:8] + "…" + value[-6:] if field == "address" else value
                cell = QTableWidgetItem(label)
                cell.setToolTip(value)
                cell.setData(Qt.ItemDataRole.UserRole, value)
                self.table.setItem(index, column, cell)
            self._render_balances(index, key)
        self.filter_asset.blockSignals(True)
        self.filter_asset.clear()
        for asset in assets:
            self.filter_asset.addItem(asset.symbol, asset.contract)
            self.filter_asset.setItemData(
                self.filter_asset.count() - 1,
                asset.contract or "原生币",
                Qt.ItemDataRole.ToolTipRole,
            )
        self.filter_asset.setCurrentIndex(max(0, self.filter_asset.findData(selected_asset)))
        self.filter_asset.blockSignals(False)
        self.table.setSortingEnabled(True)
        self._rendering = False
        self.set_selected(selected)
        self.filter()

    def _render_balances(self, index, key):
        cells = self.results.get(key, {})
        times = set()
        for column, asset in enumerate(self.assets, 4):
            cell = cells.get(asset.contract, BalanceCell())
            self.table.setItem(index, column, BalanceItem(cell))
            if cell.queried_at:
                times.add(cell.queried_at)
        label = (
            format_beijing_time(next(iter(times)))
            if len(times) == 1
            else ("多次查询" if times else "—")
        )
        self.table.setItem(index, 4 + len(self.assets), QTableWidgetItem(label))

    def filter(self, *_):
        if self._rendering:
            return
        raw = self.minimum.text().strip()
        try:
            minimum = Decimal(raw or "0")
            if not minimum.is_finite() or minimum < 0:
                raise InvalidOperation
            self.filter_valid = True
        except InvalidOperation:
            minimum = Decimal(0)
            self.filter_valid = False
        self.filter_error.setVisible(not self.filter_valid)
        contract = self.filter_asset.currentData()
        for index in range(self.table.rowCount()):
            key = self.table.item(index, 0).data(Qt.ItemDataRole.UserRole)
            cells = self.results.get(key, {})
            matches = key in self.scope and self.filter_valid
            for asset, required in (
                (contract, bool(raw) or self.positive.isChecked()),
                (None, self.has_native.isChecked()),
            ):
                if required:
                    cell = cells.get(asset, BalanceCell())
                    matches = matches and cell.status == "已查询"
                    if cell.status == "已查询":
                        value = Decimal(cell.value)
                        matches = matches and (value >= minimum if asset == contract else True)
                        if (asset is None and self.has_native.isChecked()) or (
                            asset == contract and self.positive.isChecked()
                        ):
                            matches = matches and value > 0
            self.table.setRowHidden(index, not matches)
        self._selection_changed()

    def _selection_changed(self, *_):
        if self._rendering:
            return
        selected = len(self.selected())
        visible = sum(not self.table.isRowHidden(index) for index in range(self.table.rowCount()))
        if self.active:
            label = f"正在查询 {len(self.pending)} 个钱包"
        elif selected:
            label = f"查询已选 {selected} 个钱包"
        elif self.wallet_page.search.text().strip():
            label = f"查询搜索结果 {len(self.scope)} 个钱包"
        elif self.wallet_page.group_filter.currentData() is not None:
            label = f"查询当前分组 {len(self.scope)} 个钱包"
        else:
            label = f"查询全部 {len(self.scope)} 个钱包"
        self.query_button.setText(label)
        enabled = self.wallet_page.vault.unlocked and not self.wallet_page.busy
        self.query_button.setEnabled(enabled and not self.active and bool(self.scope))
        self.cancel_button.setEnabled(self.active)
        self.cancel_button.setVisible(self.active)
        failures = sum(
            cell.status == "查询失败"
            for key, cells in self.results.items()
            if key in self.scope
            for contract, cell in cells.items()
            if contract in self.chosen[self.network.currentData()]
        )
        self.retry_button.setText(f"重试失败项（{failures}）")
        self.retry_button.setEnabled(enabled and not self.active and bool(failures))
        self.retry_button.setVisible(bool(failures) and not self.active)
        self.manage_assets_button.setEnabled(enabled and not self.active)
        self.count.setText(f"显示 {visible} / {len(self.scope)} · 已选 {selected}")
        self.feedback.setText(self.messages[self.network.currentData()])

    def query(self):
        chosen = {address.lower() for address in self.selected()} or self.scope
        self._start(
            {
                wallet["address"]: tuple(self.assets)
                for wallet in self.wallets
                if wallet["address"].lower() in chosen
            }
        )

    def retry_failed(self):
        jobs = {}
        for wallet in self.wallets:
            key = wallet["address"].lower()
            if key not in self.scope:
                continue
            failed = tuple(
                asset
                for asset in self.assets
                if self.results.get(key, {}).get(asset.contract, BalanceCell()).status == "查询失败"
            )
            if failed:
                jobs[wallet["address"]] = failed
        self._start(jobs)

    def _start(self, jobs):
        if self.active or not self.wallet_page.vault.unlocked or self.wallet_page.busy:
            return
        if not 1 <= len(jobs) <= 1000:
            self.messages[self.network.currentData()] = (
                "请选择 1–1000 个钱包，请按分组或名称缩小范围。"
            )
            self._selection_changed()
            return
        self.serial += 1
        serial, name = self.serial, self.network.currentData()
        self._query_network = name
        self.operation = Event()
        operation = self.operation
        self.active = True
        self.pending = {address.lower(): assets for address, assets in jobs.items()}
        self.completed = set()
        for key, assets in self.pending.items():
            cells = self.results.setdefault(key, {})
            for asset in assets:
                cells[asset.contract] = BalanceCell(status="等待查询")
        self.progress.setRange(0, len(jobs))
        self.progress.setValue(0)
        self.progress.show()
        self.messages[name] = f"正在查询 {len(jobs)} 个钱包；切换页面后仍会继续。"
        self.render()
        groups = {}
        for address, assets in jobs.items():
            groups.setdefault(assets, []).append(address)
        backend = self.window.backend

        def work():
            results = []
            for assets, addresses in groups.items():
                results.extend(
                    backend.bulk_balances(
                        name,
                        addresses,
                        assets,
                        operation,
                        progress=lambda result: self.result_ready.emit(serial, result),
                        include_native=any(asset.contract is None for asset in assets),
                    )
                )
            return results

        def finish(results=(), error=None):
            if serial != self.serial:
                return
            for result in results:
                self._result(serial, result)
            self.active = False
            self.progress.hide()
            self._finish_pending("未完成" if isinstance(error, OperationCancelled) else "查询失败")
            if error:
                self.messages[name] = (
                    f"查询已停止，已完成 {len(self.completed)} / {len(jobs)} 个钱包。"
                    if isinstance(error, OperationCancelled)
                    else friendly_error(error)
                    if isinstance(error, UserError)
                    else "查询未完成，请检查网络或 RPC 设置；可重试失败项。"
                )
            else:
                failed = sum(
                    self.results[key][asset.contract].status == "查询失败"
                    for key, assets in self.pending.items()
                    for asset in assets
                )
                self.messages[name] = f"查询完成：{len(jobs)} 个钱包，{failed} 个失败项。"
            self.render()

        self.window._task(
            work, success=lambda results: finish(results), failure=lambda error: finish(error=error)
        )

    def _result(self, serial, result):
        if serial != self.serial or not self.active:
            return
        key = result["address"].lower()
        if key not in self.pending or key in self.completed:
            return
        queried_at = datetime.now(UTC).isoformat()
        cells = self.results.setdefault(key, {})
        for asset in self.pending[key]:
            value = result["balances"].get(asset.contract)
            cells[asset.contract] = BalanceCell(
                status="已查询" if value is not None else "查询失败",
                value=value,
                error=result["errors"].get(asset.contract, ""),
                queried_at=queried_at,
                block=result["block"],
            )
        self.completed.add(key)
        self.progress.setValue(len(self.completed))
        self._rendering = True
        self.table.setSortingEnabled(False)
        for index in range(self.table.rowCount()):
            if self.table.item(index, 0).data(Qt.ItemDataRole.UserRole) == key:
                self._render_balances(index, key)
                break
        self.table.setSortingEnabled(True)
        self._rendering = False
        self.filter()

    def _finish_pending(self, status):
        for key, assets in self.pending.items():
            for asset in assets:
                cell = self.results[key][asset.contract]
                if cell.status == "等待查询":
                    cell.status = status

    def cancel_query(self):
        if not self.active:
            return
        self.operation.set()
        # Stop before changing networks so late results cannot enter another cache.
        name = self._query_network
        for cells in self.cache[name].values():
            for cell in cells.values():
                if cell.status == "等待查询":
                    cell.status = "未完成"
        self.serial += 1
        self.active = False
        self.progress.hide()
        self.messages[name] = (
            f"查询已停止，已完成 {len(self.completed)} / {len(self.pending)} 个钱包。"
        )
        self.render()

    def clear(self):
        self.cancel_query()
        self.serial += 1
        self.wallets = []
        self.scope.clear()
        for name in self.cache:
            self.cache[name].clear()
            self.messages[name] = "选择查询范围后开始查询。"
        self.progress.hide()
        self.minimum.clear()
        self.positive.setChecked(False)
        self.has_native.setChecked(False)
        self.render(preserve_selection=False)
