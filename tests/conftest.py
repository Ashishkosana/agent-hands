from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.error
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


def set_fault(base_url: str, name: str, enabled: bool) -> None:
    """Toggle a fixture fault via its /__faults API."""
    import json as _json

    body = _json.dumps({"fault": name, "enabled": enabled}).encode()
    request = urllib.request.Request(
        base_url + "/__faults", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        response.read()


@pytest.fixture(autouse=True)
def _reset_faults(request: pytest.FixtureRequest) -> Iterator[None]:
    """Any test that used the fixture app leaves all faults off."""
    yield
    if "fixture_app" in request.fixturenames:
        base = request.getfixturevalue("fixture_app")
        with urllib.request.urlopen(base + "/__faults", timeout=5) as response:
            import json as _json

            state = _json.loads(response.read())
        for name, enabled in state.items():
            if enabled:
                set_fault(base, name, False)


@pytest.fixture()
def capability(fixture_app: str) -> Capability:
    """The repo's lookup capability, pointed at the test server's port."""
    cap = load_capability(ARTIFACT_PATH)
    cap.target.entry.web.url = fixture_app + "/"
    return cap


# ----------------------------------------------------------- MERIDIAN fixture


def _spawn(module: str, port: int, probe_path: str) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        [sys.executable, "-m", module, "--port", str(port)],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(base + probe_path, timeout=1):
                return proc
        except urllib.error.HTTPError:
            return proc  # served (a 3xx/4xx is still 'up')
        except OSError:
            time.sleep(0.1)
    proc.terminate()
    raise RuntimeError(f"{module} did not start")


@pytest.fixture(scope="session")
def meridian_app() -> Iterator[str]:
    """The MERIDIAN-shaped local fixture (fixture/meridian.py), as a real
    subprocess. Its state persists across tests; use meridian_reset."""
    port = _free_port()
    proc = _spawn("fixture.meridian", port, "/signon")
    yield f"http://127.0.0.1:{port}"
    proc.terminate()
    proc.wait(timeout=10)


def meridian_reset(base_url: str) -> None:
    request = urllib.request.Request(base_url + "/__reset", method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        response.read()


def meridian_truth(base_url: str) -> dict[str, object]:
    """Ground truth straight from the fixture's state — what actually
    happened, independent of any UI reading."""
    import json as _json

    with urllib.request.urlopen(base_url + "/__state", timeout=5) as response:
        data: dict[str, object] = _json.loads(response.read())
        return data
