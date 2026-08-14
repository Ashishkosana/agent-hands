from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from hands.artifact import Capability, load_capability

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACT_PATH = REPO_ROOT / "capabilities" / "lookup_member_balance.json"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session")
def fixture_app() -> Iterator[str]:
    """The Fairview Teller fixture app, running as a real subprocess."""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "fixture.app", "--port", str(port)],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 15
    ready = False
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(base + "/", timeout=1):
                ready = True
                break
        except OSError:
            time.sleep(0.1)
    if not ready:
        proc.terminate()
        raise RuntimeError("fixture app did not start")
    yield base
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture()
def capability(fixture_app: str) -> Capability:
    """The repo's lookup capability, pointed at the test server's port."""
    cap = load_capability(ARTIFACT_PATH)
    cap.target.entry.web.url = fixture_app + "/"
    return cap
