"""Append-only event log plus normal logging."""

import json
import logging
import time

from . import config

log = logging.getLogger("vending")


def _encode(record):
    return json.dumps(record, sort_keys=True, separators=(",", ":"))


def write(kind, **fields):
    record = {"t": round(time.time(), 3), "kind": kind, **fields}
    log.debug("event %s", record)
    try:
        with open(config.EVENT_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(_encode(record) + "\n")
    except OSError as exc:
        log.warning("could not write event log: %s", exc)
