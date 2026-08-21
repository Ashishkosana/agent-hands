"""Structured run tracing: an append-only JSONL log an auditor can read.

One directory per run under runs/: trace.jsonl plus any evidence files
(screenshot, accessibility snapshot) captured on failure.

Tamper evidence: records form a hash chain. Every record carries a 0-based
``seq`` and ``prev`` — the SHA-256 of the previous record exactly as persisted
(the serialized line, without its newline; ``""`` for the first record).
Editing, removing, inserting, or reordering any record breaks the chain, which
``verify_chain`` detects by recomputation. Known limit (documented, not
hidden): truncating the *tail* of the log is only detectable against an
external anchor (e.g. the final record's hash stored elsewhere) — chaining
alone cannot prove a log didn't simply end early.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType


def _line_hash(line: str) -> str:
    """Hash a record exactly as persisted — the bytes on disk are the truth."""
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def new_run_dir(base: Path, capability_name: str) -> Path:
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = base / f"{stamp}-{capability_name}-{secrets.token_hex(3)}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def verify_chain(run_dir: Path) -> bool:
    """Recompute the hash chain over trace.jsonl. False if any record was
    altered, removed, inserted, or reordered (or the file is missing /
    unparseable). Records written before chaining existed (no ``seq``) fail
    verification — callers may distinguish that case by inspecting the first
    record."""
    path = run_dir / "trace.jsonl"
    if not path.exists():
        return False
    prev = ""
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return False
        if record.get("seq") != i or record.get("prev") != prev:
            return False
        prev = _line_hash(line)
    return True


def is_chained(run_dir: Path) -> bool:
    """Whether this trace carries chain fields at all (legacy traces predate
    tamper evidence and are reported as unverifiable, not as tampered)."""
    path = run_dir / "trace.jsonl"
    if not path.exists():
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                return "seq" in json.loads(line)
            except json.JSONDecodeError:
                return False
    return False


class Trace:
    def __init__(self, run_dir: Path, tail_size: int = 20) -> None:
        self.run_dir = run_dir
        path = run_dir / "trace.jsonl"
        # Continue an existing chain if the file already has records (run dirs
        # are fresh per run, but reopening must never fork the chain).
        self._seq = 0
        self._prev = ""
        if path.exists():
            lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            if lines:
                self._seq = len(lines)
                self._prev = _line_hash(lines[-1])
        self._file = path.open("a", encoding="utf-8")
        self._tail: deque[dict[str, object]] = deque(maxlen=tail_size)

    def emit(self, event: str, **fields: object) -> None:
        record: dict[str, object] = {
            "ts": datetime.now(tz=UTC).isoformat(timespec="milliseconds"),
            "seq": self._seq,
            "prev": self._prev,
            "event": event,
            **fields,
        }
        line = json.dumps(record, default=str)
        self._file.write(line + "\n")
        self._file.flush()
        self._prev = _line_hash(line)
        self._seq += 1
        self._tail.append(record)

    def tail(self) -> list[dict[str, object]]:
        """Recent events — carried in intervention requests so an operator
        sees what the automation just did before it stopped."""
        return list(self._tail)

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
