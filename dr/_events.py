"""Small shared utilities for durable DR events and atomic control-file writes."""

import json
import math
import os
import pathlib
import tempfile
import time
from datetime import datetime, timezone


def positive(name: str, value: float) -> None:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and greater than zero")


def append_event(path: pathlib.Path, **fields) -> dict:
    ts = time.time()
    record = {
        **fields,
        "ts": ts,
        "iso": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
    }
    line = json.dumps(record, ensure_ascii=False, allow_nan=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")
        stream.flush()
    print(line, flush=True)
    return record


def atomic_write(path: pathlib.Path, value: str) -> None:
    """Readers see either the old complete value or the new complete value."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", delete=False,
        ) as stream:
            temporary = pathlib.Path(stream.name)
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
