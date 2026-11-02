from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from ..diagnostics import record_exception
from .rpc import OperationCancelled, check_cancelled


class WorkerSignals(QObject):
    succeeded = Signal(object)
    failed = Signal(object)


class Worker(QRunnable):
    def __init__(self, function, *args, cancelled=None, task_id=None):
        super().__init__()
        self.function = function
        self.args = args
        self.cancelled = cancelled
        self.task_id = task_id
        self.signals = WorkerSignals()

    @Slot()
    def run(self):
        try:
            # Finish local persistence of a wallet result even during shutdown.
            # These functions save first; any subsequent RPC is cancellation-aware.
            if self.cancelled is not None and getattr(self.function, "__name__", "") not in {
                "wallet_error",
            }:
                check_cancelled(self.cancelled)
            self.signals.succeeded.emit(self.function(*self.args))
        except Exception as error:  # noqa: BLE001 - deliver sanitized errors on the UI thread
            if not isinstance(error, OperationCancelled):
                record_exception(
                    "后台任务（" + getattr(self.function, "__qualname__", "未知操作") + "）",
                    error,
                    task_id=self.task_id,
                )
            self.signals.failed.emit(error)
