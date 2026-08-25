"""Structured run tracing: an append-only JSONL log an auditor can read.

One directory per run under runs/: trace.jsonl plus any evidence files
(screenshot, accessibility snapshot) captured on failure.

Tamper EVIDENCE (not tamper proof): records form a hash chain. Every record
carries a 0-based ``seq`` and ``prev`` — the SHA-256 of the previous record
exactly as persisted (the serialized line, without its newline; ``""`` for the
first record). Any naive edit, removal, insertion, or reordering of a record
breaks the chain, which ``verify_chain`` detects by recomputation.

A chain alone cannot protect its own end: each record's hash is carried by its
SUCCESSOR, so the FINAL record — the one holding the run's result — is pinned
by nothing, and truncating the tail deletes its own evidence. ``close()``
therefore seals the run by writing the tip hash and the record count to a
separate ``trace.tip`` file, which ``verify_chain`` cross-checks.

Be precise about what that seal is worth. It sits in the SAME directory as the
log, so anyone able to rewrite ``trace.jsonl`` can also delete ``trace.tip``,
and verification then silently degrades to the weaker chain-only check. On its
own the seal only defends against an attacker who can edit one file but not
remove its sibling — not a threat model anyone has. Two things make it real:
``is_sealed`` exposes presence as a REPORTED state (unsealed must never render
as "intact"), and the tip travels out of the directory in the invoke envelope
as ``audit_chain_tip``, to be handed back as ``verify_chain(dir,
expected_tip=…)``. The external anchor is the actual control; the file is a
convenience.

Known limit (documented, not hidden): the chain is keyless, so an adversary who
can rewrite BOTH files can recompute a consistent pair and forge a log that
looks intact to anyone who kept no anchor. Closing that fully needs an HMAC
keyed with a secret the writer holds.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

TIP_FILE = "trace.tip"


def _line_hash(line: str) -> str:
    """Hash a record exactly as persisted — the bytes on disk are the truth."""
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def _read_seal(run_dir: Path) -> dict[str, object] | None:
    """The parsed seal, or None if absent/unreadable. ``ValueError`` covers
    both a malformed JSON body and a non-UTF-8 one — the two must not diverge,
    or one caller returns 'unsealed' while another raises."""
    path = run_dir / TIP_FILE
    if not path.exists():
        return None
    try:
        sealed = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return sealed if isinstance(sealed, dict) else None


def is_sealed(run_dir: Path) -> bool:
    """Whether this run carries a readable seal at all.

    Reported, never assumed. ``verify_chain`` can only cross-check a seal that
    is present, so an absent one silently degrades verification to the weaker
    chain-only check — and a deleted seal would otherwise be indistinguishable
    from an intact log. Callers must render three states (sealed / unsealed /
    broken), exactly as ``is_chained`` forces for legacy traces.
    """
    return _read_seal(run_dir) is not None


def chain_tip(run_dir: Path) -> str | None:
    """The sealed tip hash for a run, or None if it was never sealed.

    Travels in the invoke envelope so the anchor lives somewhere other than the
    directory it protects; pass it back to ``verify_chain`` as ``expected_tip``
    to reconcile a kept receipt against a run directory.
    """
    sealed = _read_seal(run_dir)
    if sealed is None:
        return None
    tip = sealed.get("tip")
    return tip if isinstance(tip, str) else None


def new_run_dir(base: Path, capability_name: str) -> Path:
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = base / f"{stamp}-{capability_name}-{secrets.token_hex(3)}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def verify_chain(run_dir: Path, expected_tip: str | None = None) -> bool:
    """Recompute the hash chain over trace.jsonl. False if any record was
    altered, removed, inserted, or reordered (or the file is missing /
    unparseable). Records written before chaining existed (no ``seq``) fail
    verification — callers may distinguish that case with ``is_chained``.

    ``expected_tip`` is the anchor a caller kept elsewhere (the invoke
    envelope's ``audit_chain_tip``). Supplying it is the only way to detect the
    two attacks an in-directory seal cannot stop: deleting the seal, and
    rewriting both files consistently. Without it, an unsealed run verifies on
    the chain alone — check ``is_sealed`` and report that state rather than
    reading True as "intact".
    """
    path = run_dir / "trace.jsonl"
    if not path.exists():
        return False
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except (ValueError, OSError):
        return False
    prev = ""
    for i, line in enumerate(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return False
        if record.get("seq") != i or record.get("prev") != prev:
            return False
        prev = _line_hash(line)
    # The seal pins what the chain structurally cannot: the final record, and
    # the fact that no records were dropped off the end.
    sealed = _read_seal(run_dir)
    if sealed is not None and (
        sealed.get("records") != len(lines) or sealed.get("tip") != prev
    ):
        return False
    return expected_tip is None or expected_tip == prev


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
        self._seal()

    def _seal(self) -> None:
        """Anchor the chain's tip outside the file it protects. Without this,
        the last record — the one carrying the run's result — can be rewritten
        in place, and a tail truncation removes its own evidence."""
        (self.run_dir / TIP_FILE).write_text(
            json.dumps({"records": self._seq, "tip": self._prev}) + "\n",
            encoding="utf-8",
        )

    def __enter__(self) -> Trace:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
