"""Structured run tracing: an append-only JSONL log an auditor can read.

One directory per run under runs/: trace.jsonl plus any evidence files
(screenshot, accessibility snapshot) captured on failure.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType


def new_run_dir(base: Path, capability_name: str) -> Path:
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = base / f"{stamp}-{capability_name}-{secrets.token_hex(3)}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


class Trace:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self._file = (run_dir / "trace.jsonl").open("a", encoding="utf-8")

    def emit(self, event: str, **fields: object) -> None:
        record: dict[str, object] = {
            "ts": datetime.now(tz=UTC).isoformat(timespec="milliseconds"),
            "event": event,
            **fields,
        }
        self._file.write(json.dumps(record, default=str) + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> Trace:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
