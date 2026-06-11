from __future__ import annotations

import logging
import re
from threading import Lock

from .models import AppLog, utc_now_iso


SECRET_TOKEN = "[REDACTED]"


class AppLogBuffer:
    def __init__(self, max_entries: int = 500) -> None:
        self.max_entries = max_entries
        self._rows: list[AppLog] = []
        self._seq = 0
        self._lock = Lock()
        self._secret_values: set[str] = set()

    def set_secret_values(self, values: list[str]) -> None:
        self._secret_values = {value for value in values if value and len(value) >= 4}

    def append(self, level: str, source: str, message: str) -> AppLog:
        clean = self.redact(message)
        with self._lock:
            self._seq += 1
            row = AppLog(seq=self._seq, time=utc_now_iso(), level=level, source=source, message=clean)
            self._rows.append(row)
            del self._rows[:-self.max_entries]
            return row

    def list(self, after: int = 0, level: str | None = None) -> list[AppLog]:
        with self._lock:
            rows = [row for row in self._rows if row.seq > after]
        if level:
            rows = [row for row in rows if row.level == level]
        return rows

    def redact(self, value: object) -> str:
        text = str(value)
        for secret in self._secret_values:
            text = text.replace(secret, SECRET_TOKEN)

        # 常见请求头、Cookie、URL 参数和错误消息中的密钥/密码都在进入 UI 前脱敏。
        patterns = [
            (r"(?i)(authorization\s*[:=]\s*)(bearer\s+)?[^\s,;]+", r"\1" + SECRET_TOKEN),
            (r"(?i)(cookie|set-cookie)(\s*[:=]\s*)[^,\n\r]+", r"\1\2" + SECRET_TOKEN),
            (r"(?i)(password|passwd|pwd|key|api[_-]?key|steam_api_key|neo4j_password)(\s*[:=]\s*)[^&\s,;]+", r"\1\2" + SECRET_TOKEN),
            (r"(?i)([?&](?:key|password|api_key|steam_api_key|neo4j_password)=)[^&\s]+", r"\1" + SECRET_TOKEN),
            (r"\b[A-Fa-f0-9]{32}\b", SECRET_TOKEN),
        ]
        for pattern, replacement in patterns:
            text = re.sub(pattern, replacement, text)
        return text


class AppLogHandler(logging.Handler):
    def __init__(self, buffer: AppLogBuffer, source: str = "python") -> None:
        super().__init__()
        self.buffer = buffer
        self.source = source

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = record.levelname.lower()
            if level == "warning":
                level = "warn"
            self.buffer.append(level, record.name or self.source, self.format(record))
        except Exception:
            self.handleError(record)


def install_log_handler(buffer: AppLogBuffer) -> AppLogHandler:
    handler = AppLogHandler(buffer)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.setLevel(logging.INFO)
    for name in ("steam_friend_relationship_map", "uvicorn.error"):
        logger = logging.getLogger(name)
        if not any(isinstance(existing, AppLogHandler) for existing in logger.handlers):
            logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return handler
