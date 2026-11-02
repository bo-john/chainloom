import signal
import socket

from PySide6.QtCore import QObject, QSocketNotifier, Signal


class TerminationSignals(QObject):
    """Wake Qt's native event loop on POSIX termination signals, even while Python is idle."""

    requested = Signal(int)

    def __init__(self, callback, parent=None):
        super().__init__(parent)
        self.reader, self.writer = socket.socketpair()
        self.reader.setblocking(False)
        self.writer.setblocking(False)
        self.previous_handlers = {}
        self.previous_fd = signal.set_wakeup_fd(self.writer.fileno())
        self.closed = False
        self.notifier = QSocketNotifier(self.reader.fileno(), QSocketNotifier.Type.Read, self)
        self.notifier.activated.connect(self._drain)
        self.requested.connect(lambda _: callback())
        for signum in (signal.SIGINT, signal.SIGTERM):
            self.previous_handlers[signum] = signal.signal(signum, self._handle)

    @staticmethod
    def _handle(signum, frame):
        # Qt calls and exceptions inside a Python signal handler are unsafe while
        # the native loop is active. set_wakeup_fd notifies the Qt socket instead.
        pass

    def _drain(self, *_):
        if self.closed:
            return
        try:
            signals = self.reader.recv(4096)
        except BlockingIOError:
            return
        for signum in signals:
            if signum in self.previous_handlers:
                self.requested.emit(signum)
                break

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.notifier.setEnabled(False)
        signal.set_wakeup_fd(self.previous_fd)
        for signum, handler in self.previous_handlers.items():
            signal.signal(signum, handler)
        self.reader.close()
        self.writer.close()
