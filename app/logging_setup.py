"""JSON logs: one JSON object per line, so tools can search them.

Each line has: timestamp, level, logger, message, job_id, correlation_id.
job_id and correlation_id come from "context variables": values that belong to the
current request (or current job in the worker), so we never pass them around by hand.
"""
import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone

correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)
job_id_var: ContextVar[str | None] = ContextVar("job_id", default=None)

# Stamp the ids onto every log record WHEN IT IS CREATED (in the thread/request that
# logged it). Reading them later, when the line is printed, could give the wrong values.
_default_factory = logging.getLogRecordFactory()


def _record_factory(*args, **kwargs) -> logging.LogRecord:
    record = _default_factory(*args, **kwargs)
    record.ctx_job_id = job_id_var.get()
    record.ctx_correlation_id = correlation_id_var.get()
    return record


logging.setLogRecordFactory(_record_factory)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            # extra={"job_id": ...} wins; otherwise use the ids stamped at creation time.
            "job_id": getattr(record, "job_id", None) or getattr(record, "ctx_job_id", None),
            "correlation_id": getattr(record, "correlation_id", None) or getattr(record, "ctx_correlation_id", None),
        }
        # Extra fields passed as log.info("...", extra={"fields": {...}})
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            data.update(fields)
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # Make uvicorn use our JSON format too; drop its plain access log (we log requests ourselves).
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    logging.getLogger("uvicorn.access").disabled = True
    # boto3 is very chatty at INFO.
    for name in ("botocore", "boto3", "urllib3", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
