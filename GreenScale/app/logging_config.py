import json
import logging
import sys
from datetime import datetime, timezone

# Only these extra fields are ever written out. Anything else attached to a
# log record (API keys, request bodies, user text) is dropped on purpose.
ALLOWED_FIELDS = (
    "event",
    "job_id",
    "status",
    "method",
    "path",
    "status_code",
    "duration_ms",
    "error",
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in ALLOWED_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        return json.dumps(payload, default=str)


def setup_logging(level: str = "INFO") -> None:
    logger = logging.getLogger("greenscale")
    logger.setLevel(level.upper())
    logger.propagate = False
    if not logger.handlers:  # avoid duplicate lines when called twice
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)