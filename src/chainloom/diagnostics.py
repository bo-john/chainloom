"""Private, rotating desktop diagnostics without wallet or provider payloads."""

import json
import logging
import os
import re
import sys
import threading
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit

from .errors import UserError
from .time_format import BEIJING_TIMEZONE

LOGGER = logging.getLogger("chainloom")
LOGGER.addHandler(logging.NullHandler())
LOGGER.propagate = False
MAX_LOG_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 5


def log_directory():
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "Chainloom"
    if sys.platform == "win32":
        return (
            Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
            / "Chainloom"
            / "Logs"
        )
    return (
        Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
        / "chainloom"
        / "logs"
    )


_SECRETS = ContextVar("diagnostic_secrets", default=())


def redact_text(value, secrets=()):
    text = str(value)
    for secret in sorted(set(_SECRETS.get() + tuple(secrets)), key=len, reverse=True):
        if secret:
            text = text.replace(secret, "[敏感内容已脱敏]")
    text = re.sub(r"(?i)(?:https?|wss?)://[^\s<>\"']+", "[连接地址已脱敏]", text)
    text = re.sub(r"(?i)((?:with\s+)?url\s*[:=]\s*)[^\s,)]+", r"\1[连接地址已脱敏]", text)
    # Structured secrets may contain quoted spaces; do not log their values.
    labels = r"password|passphrase|private[_-]?key|mnemonic|seed(?: phrase)?|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|raw[_-]?(?:tx|transaction)|keystore|密码|私钥|助记词"
    text = re.sub(
        rf"(?i)([\"']?(?:{labels})[\"']?\s*[:=：]\s*)(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^,;\r\n}}\]]*)",
        r"\1[敏感内容已脱敏]",
        text,
    )
    text = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [凭据已脱敏]", text)
    text = re.sub(
        r"""\b(?:b|bytearray\(\s*b)(?:'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")(?:\))?""",
        "[二进制数据已脱敏]",
        text,
    )
    text = re.sub(r"(?:0x)?[0-9a-fA-F]{64,}", "[密钥或交易数据已脱敏]", text)
    return text.replace("\r", r"\r").replace("\n", r"\n")[:6000]


def _exception_detail(error, secrets=()):
    cached = getattr(error, "_chainloom_log_detail", None)
    if cached is not None:
        return cached
    parts = [redact_text(str(error), secrets)]
    response = getattr(error, "rpc_response", None)
    remote = response.get("error", {}) if isinstance(response, dict) else {}
    if isinstance(remote, dict):
        if type(remote.get("code")) is int:
            parts.append(f"RPC错误码={remote['code']}")
        if isinstance(remote.get("message"), str):
            parts.append("RPC原始错误=" + redact_text(remote["message"], secrets))
    return "；".join(parts)


@contextmanager
def sensitive_errors(*values):
    """Freeze redacted messages before an exception leaves a password-using operation."""
    plain_secrets = tuple(
        value.decode("utf-8") if isinstance(value, (bytes, bytearray)) else value
        for value in values
        if value
    )
    secrets = tuple(
        variant
        for value in plain_secrets
        for variant in (
            value,
            repr(value)[1:-1],
            json.dumps(value, ensure_ascii=True)[1:-1],
            repr(value.encode("utf-8"))[2:-1],
        )
        if variant
    )
    token = _SECRETS.set(_SECRETS.get() + secrets)
    try:
        yield
    except Exception as error:
        current, seen = error, set()
        while current is not None and id(current) not in seen and len(seen) < 8:
            seen.add(id(current))
            current._chainloom_log_detail = _exception_detail(current)
            current = current.__cause__ or current.__context__
        raise
    finally:
        _SECRETS.reset(token)


def rpc_endpoint_summary(url):
    if not url:
        return "内置默认节点"
    if not isinstance(url, str):
        return "旧节点配置无效，内容已脱敏"
    try:
        parsed = urlsplit(url)
        return {
            "协议": parsed.scheme,
            "主机": parsed.hostname,
            "端口": parsed.port,
            "路径及认证参数": "已脱敏" if parsed.path or parsed.query or parsed.username else "无",
        }
    except (ValueError, TypeError):
        return "节点地址格式无效，内容已脱敏"


def record_operation(operation, **details):
    def clean(value, key=""):
        if re.fullmatch(
            r"(?i)password|passphrase|private[_-]?key|mnemonic|secret|token|api[_-]?key|raw[_-]?(?:tx|transaction)|keystore|密码|私钥|助记词",
            str(key),
        ):
            return "[敏感内容已脱敏]"
        if (
            key == "交易哈希"
            and isinstance(value, str)
            and re.fullmatch(r"0x[0-9a-fA-F]{64}", value)
        ):
            return value  # A known public transaction hash is useful for reconciliation.
        if isinstance(value, dict):
            return {redact_text(k): clean(v, k) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [clean(item) for item in value]
        if value is None or isinstance(value, (int, bool)):
            return value
        return redact_text(value)

    LOGGER.info("%s；内容=%s", operation, json.dumps(clean(details), ensure_ascii=False))


class DiagnosticFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        timestamp = datetime.fromtimestamp(record.created, BEIJING_TIMEZONE)
        return timestamp.strftime(datefmt or "%Y-%m-%dT%H:%M:%S%z")

    def formatException(self, exc_info):
        # Retain original messages and chains, never locals or source lines.
        lines = []
        error = exc_info[1]
        seen = set()
        while error is not None and id(error) not in seen and len(seen) < 8:
            seen.add(id(error))
            lines.append(f"异常类型：{type(error).__module__}.{type(error).__name__}")
            lines.append("原始错误信息（已脱敏）：" + _exception_detail(error))
            for frame in traceback.extract_tb(error.__traceback__):
                lines.append(f"  文件 {frame.filename}，第 {frame.lineno} 行，函数 {frame.name}")
            error = error.__cause__ or error.__context__
            if error is not None:
                lines.append("关联原始异常：")
        return "\n".join(lines)


class PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        descriptor = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.chmod(self.baseFilename, 0o600)
            return os.fdopen(descriptor, "a", encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            raise


def configure_logging(directory=None):
    directory = Path(directory) if directory is not None else log_directory()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    path = directory / "chainloom.log"
    handler = PrivateRotatingFileHandler(
        path, maxBytes=MAX_LOG_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
    )
    handler.setFormatter(
        DiagnosticFormatter(
            "%(asctime)s %(levelname)s [进程=%(process)d 线程=%(threadName)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    close_logging()
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.info("桌面应用日志已启动；应用版本=0.1.0")
    return path


def close_logging():
    for handler in LOGGER.handlers[:]:
        LOGGER.removeHandler(handler)
        handler.close()
    LOGGER.addHandler(logging.NullHandler())


def record_exception(operation, error, *, task_id=None, level=logging.ERROR):
    detail = _exception_detail(error)
    explanations = {
        "insufficient funds": "余额不足以支付转账或手续费",
        "nonce too low": "交易序号已被使用",
        "execution reverted": "链上模拟或合约执行回退",
        "mac mismatch": "钱包解密校验失败",
        "transaction had invalid fields": "交易字段校验失败",
        "database is locked": "本地数据库正被其他操作占用",
    }
    explanation = next(
        (value for key, value in explanations.items() if key in detail.lower()), None
    )
    if explanation is None:
        if isinstance(error, UserError) and any(
            "\u4e00" <= char <= "\u9fff" for char in str(error)
        ):
            explanation = detail
        elif "Timeout" in type(error).__name__:
            explanation = "请求超时"
        elif "Connection" in type(error).__name__:
            explanation = "连接失败"
        elif isinstance(error, TypeError):
            explanation = "参数类型或调用方式不匹配"
        elif isinstance(error, ValueError):
            explanation = "参数或数据校验失败"
        elif isinstance(error, OSError):
            explanation = "系统或文件操作失败"
        else:
            explanation = "操作发生异常，请结合原始错误和堆栈排查"
    LOGGER.log(
        level,
        "操作失败：%s；任务编号=%s；错误说明：%s",
        operation,
        task_id or "无",
        explanation,
        exc_info=(type(error), error, error.__traceback__),
    )


@contextmanager
def exception_hooks():
    previous = sys.excepthook
    previous_thread = threading.excepthook

    def unhandled(kind, error, tb):
        if issubclass(kind, (KeyboardInterrupt, SystemExit)):
            previous(kind, error, tb)
            return
        record_exception("界面或主线程未处理异常", error)

    def thread_failed(args):
        if not issubclass(args.exc_type, SystemExit):
            record_exception("后台线程未处理异常", args.exc_value)

    sys.excepthook = unhandled
    threading.excepthook = thread_failed
    try:
        yield
    finally:
        sys.excepthook = previous
        threading.excepthook = previous_thread
