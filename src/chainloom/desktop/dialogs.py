from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QVBoxLayout,
)

from .service import format_units
from .settings import NETWORKS
from .widgets import box, text


class ConfirmDialog(QDialog):
    def __init__(self, quote, parent=None):
        super().__init__(parent)
        self.setWindowTitle("确认转账信息")
        self.setMinimumWidth(590)
        layout = box(self, margins=(28, 26, 28, 26), spacing=18)
        layout.addWidget(text("请再次核对转账信息", "brand"))
        info = NETWORKS[quote.network]
        prepared = quote.prepared
        form = QFormLayout()
        form.setSpacing(15)
        values = [
            ("网络", f"{info.title}  ·  Chain ID {info.chain_id}"),
            ("发送地址", prepared.request.sender),
            ("收款地址", prepared.request.recipient),
            ("转账数量", f"{prepared.request.amount} {quote.asset.symbol}"),
            ("代币合约", quote.asset.contract or f"{info.symbol} 原生币"),
            (
                "执行手续费上限",
                f"{format_units(int(prepared.preview['max_execution_fee_wei']))} {info.symbol}",
            ),
        ]
        for title, value in values:
            entry = text(value, wrap=True)
            entry.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            form.addRow(text(title, "fieldLabel"), entry)
        layout.addLayout(form)
        layout.addWidget(
            text(
                "确认后使用所选本地钱包签名并发送。请核对地址、网络、数量和手续费。"
                + (" L2 网络还可能收取 L1 数据费。" if quote.network in {"base", "xlayer"} else ""),
                "muted",
                True,
            )
        )
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("返回修改")
        send = buttons.addButton("确认并发送", QDialogButtonBox.ButtonRole.AcceptRole)
        send.setObjectName("primary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class TokenDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("添加代币")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(15)
        layout.addWidget(
            text(
                "使用当前网络上的代币合约地址。\n代币名称不能证明真伪，请从可信来源核对地址。",
                "muted",
                True,
            )
        )
        self.symbol = QLineEdit()
        self.symbol.setPlaceholderText("资产简称，例如 USDC")
        self.contract = QLineEdit()
        self.contract.setPlaceholderText("完整的 0x 合约地址")
        layout.addWidget(self.symbol)
        layout.addWidget(self.contract)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("验证并添加")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
