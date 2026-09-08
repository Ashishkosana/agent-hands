from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from hands.artifact import Capability, load_capability, sign_risk_review

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACT_PATH = REPO_ROOT / "capabilities" / "lookup_member_balance.json"
FAIRVIEW_DIR = REPO_ROOT / "capabilities" / "generated"


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


def reset_members(base_url: str) -> None:
    """Restore the fixture member roster after a mutating flow."""
    urllib.request.urlopen(base_url + "/__reset", timeout=5).read()


def retarget(capability: Capability, fixture_app: str) -> Capability:
    """Point a generated artifact at the live test server."""
    capability.target.entry.web.url = fixture_app + "/"
    return capability


def load_fairview(name: str, fixture_app: str, *, sign: bool = False) -> Capability:
    cap = retarget(load_capability(FAIRVIEW_DIR / f"{name}.json"), fixture_app)
    if sign:
        return sign_risk_review(cap, "reviewer-1")
    return cap


@pytest.fixture(autouse=True)
def _reset_faults(request: pytest.FixtureRequest) -> Iterator[None]:
    """Any test that used the fixture app leaves all faults off and member data seeded."""
    yield
    if "fixture_app" in request.fixturenames:
        base = request.getfixturevalue("fixture_app")
        with urllib.request.urlopen(base + "/__faults", timeout=5) as response:
            import json as _json

            state = _json.loads(response.read())
        for name, enabled in state.items():
            if enabled:
                set_fault(base, name, False)
        reset_members(base)


@pytest.fixture()
def capability(fixture_app: str) -> Capability:
    """The repo's lookup capability, pointed at the test server's port."""
    cap = load_capability(ARTIFACT_PATH)
    cap.target.entry.web.url = fixture_app + "/"
    return cap
