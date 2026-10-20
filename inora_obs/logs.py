"""Assignment 1 JSON log format plus trace_id. One JSON object per line; Filebeat ships them to Elasticsearch.
Log calls pass the structured fields as `extra={...}`. Never log payment tokens."""
import json
import logging
import os


class JsonFormatter(logging.Formatter):
    def __init__(self, service_name):
        super().__init__()
        self.service_name = service_name

    def format(self, record):
        return json.dumps({
            "time": self.formatTime(record),
            "service_name": self.service_name,
            "severity": record.levelname,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "n/a"),
            "trace_id": getattr(record, "trace_id", "n/a"),
            "endpoint": getattr(record, "endpoint", "n/a"),
            "status_code": getattr(record, "status_code", 0),
        })


def setup_file_logging(service_name, log_file):
    """Attach the JSON file handler to the root logger once."""
    directory = os.path.dirname(log_file)
    if directory:
        os.makedirs(directory, exist_ok=True)
    root = logging.getLogger()
    if not any(isinstance(h, logging.FileHandler) for h in root.handlers):
        handler = logging.FileHandler(log_file)
        handler.setFormatter(JsonFormatter(service_name))
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)   # the app writes its own structured line
