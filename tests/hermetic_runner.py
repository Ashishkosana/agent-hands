"""Subprocess entry point for the zero-LLM replay proof.

Installs a socket guard that refuses any non-loopback egress from this Python
process, runs a replay through the real CLI, then asserts no model SDK module
was ever imported. Executed by test_zero_llm.py in a fresh interpreter with
API-key-shaped environment variables stripped.
"""

from __future__ import annotations

import socket
import sys
from typing import Any

_real_connect = socket.socket.connect
_ALLOWED_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _guarded_connect(self: socket.socket, address: Any) -> None:
    host = address[0] if isinstance(address, tuple) else address
    if isinstance(host, str) and host not in _ALLOWED_HOSTS:
        raise AssertionError(f"replay attempted network egress to {host!r}")
    _real_connect(self, address)


socket.socket.connect = _guarded_connect  # type: ignore[method-assign, assignment]


def main() -> int:
    from hands.cli import main as cli_main

    code = cli_main(sys.argv[1:])
    assert "anthropic" not in sys.modules, "model SDK was imported during replay"
    return code


if __name__ == "__main__":
    sys.exit(main())
