"""Structured (JSON-lines) logging - the doc this was built against asks
for a centralized aggregator (Datadog/New Relic/CloudWatch), which needs a
real account/subscription this local prototype doesn't have. What IS real
and worth doing now: emit every log line as a JSON object with the fields
an aggregator would actually want (timestamp, level, service, job_id,
user_id, trace_id, message), so wiring one up later is a config change,
not a rewrite - every call site that logs today already produces the
right shape.

Usage:
    from . import logging_setup
    log = logging_setup.get_logger("worker")
    log.info("job started", extra={"job_id": order_id, "user_id": user_id})
"""
from __future__ import annotations

import json
import logging
import sys
import time


class JsonFormatter(logging.Formatter):
    _STANDARD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname.lower(),
            "service": record.name,
            "message": record.getMessage(),
        }
        # Anything passed via logging's `extra={...}` shows up as plain
        # attributes on the record - pull those through into the JSON
        # object instead of dropping them (the whole point of calling
        # with extra={"job_id": ..., "user_id": ...} in the first place).
        for key, value in record.__dict__.items():
            if key not in self._STANDARD_ATTRS and key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def get_logger(service_name: str) -> logging.Logger:
    logger = logging.getLogger(service_name)
    if not logger.handlers:  # avoid duplicate handlers if called more than once
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger
