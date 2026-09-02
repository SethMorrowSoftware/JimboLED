"""Keep the last few hundred log lines in memory so the UI can show them."""
from __future__ import annotations

import collections
import logging
import threading
import time
from typing import Deque, Dict, List


class RingBufferHandler(logging.Handler):
    def __init__(self, capacity: int = 500):
        super().__init__()
        self.records: Deque[Dict] = collections.deque(maxlen=capacity)
        self._lock = threading.Lock()
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:  # pragma: no cover
            msg = record.getMessage()
        with self._lock:
            self.records.append({
                "ts": record.created,
                "level": record.levelname,
                "logger": record.name,
                "msg": msg,
            })

    def tail(self, limit: int = 200) -> List[Dict]:
        with self._lock:
            return list(self.records)[-limit:]


def install_log_buffer(capacity: int = 500) -> RingBufferHandler:
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, RingBufferHandler):
            return h
    handler = RingBufferHandler(capacity)
    handler.setLevel(logging.INFO)
    root.addHandler(handler)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("zeroconf").setLevel(logging.WARNING)
    return handler
