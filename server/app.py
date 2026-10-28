import base64
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI()

DISPLAY = os.environ.get("DISPLAY", ":1")
START_FIREFOX = os.environ.get("START_FIREFOX", "/opt/start-firefox.sh")
FIREFOX_PROFILE = os.environ.get(
    "FIREFOX_PROFILE",
    "/home/agent/.mozilla/firefox/relay-agent",
)
PROFILE_NAME = "relay-agent"


def xdo(*args):
    subprocess.run(
        ["xdotool", *map(str, args)],
        check=True,
        env={**os.environ, "DISPLAY": DISPLAY},
    )


def _run(cmd: list[str], timeout: float = 5) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "DISPLAY": DISPLAY},
    )


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


@app.get("/health")
def health():
    """Liveness for control API, X display, and VNC (noVNC downstream)."""
    # DISPLAY=:1 → socket /tmp/.X11-unix/X1 (no extra apt packages)
    display_num = DISPLAY.lstrip(":").split(".")[0]
    socket_path = f"/tmp/.X11-unix/X{display_num}"
    checks: dict[str, bool] = {
        "display_socket": os.path.exists(socket_path),
        # Websockify alone is not enough — clients fail with
        # "Failed to connect to downstream server" when :5900 is down (common after reboot).
        "vnc_rfb": _port_open(5900),
        "novnc_http": _port_open(6080),
    }

    try:
        r = _run(["xdotool", "getdisplaygeometry"])
        checks["xdotool"] = r.returncode == 0
        geometry = r.stdout.strip() if r.returncode == 0 else None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        checks["xdotool"] = False
        geometry = None

    ok = all(checks.values())
    body = {
        "ok": ok,
        "status": "ok" if ok else "degraded",
        "display": DISPLAY,
        "checks": checks,
    }
    if geometry:
        body["geometry"] = geometry
    if not ok:
        raise HTTPException(status_code=503, detail=body)
    return body


def _firefox_running() -> bool:
    r = _run(["pgrep", "-f", "firefox-esr"], timeout=5)
    return r.returncode == 0 and bool(r.stdout.strip())


def _stop_firefox() -> None:
    _run(["pkill", "-TERM", "-f", "firefox-esr"], timeout=10)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not _firefox_running():
            return
        time.sleep(0.2)
    if _firefox_running():
        _run(["pkill", "-KILL", "-f", "firefox-esr"], timeout=10)
        time.sleep(0.3)


def _clear_sessionstore(profile: Path) -> None:
    """Idempotent wipe of crash/session restore artifacts."""
    if not profile.is_dir():
        return
    for name in ("sessionstore.jsonlz4", "recovery.jsonlz4", ".parentlock"):
        try:
            (profile / name).unlink(missing_ok=True)
        except OSError:
            pass
    backups = profile / "sessionstore-backups"
    if backups.exists():
        shutil.rmtree(backups, ignore_errors=True)


@app.post("/session/reset")
def session_reset():
    """Recycle the browser session without restarting the whole container."""
    _stop_firefox()
    profile = Path(FIREFOX_PROFILE)
    _clear_sessionstore(profile)
    try:
        subprocess.Popen(
            [START_FIREFOX],
            env={**os.environ, "DISPLAY": DISPLAY},
            start_new_session=True,
        )
    except FileNotFoundError as e:
        raise HTTPException(
            status_code=500,
            detail=f"{START_FIREFOX} not found",
        ) from e
    # Brief settle so the first screenshot/action is not mid-startup.
    time.sleep(1.5)
    return {"ok": True, "reset": "firefox", "profile": PROFILE_NAME}



@app.get("/screenshot")
def screenshot():
    r = _run(["scrot", "-o", "/tmp/screen.png"], timeout=15)
    if r.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=r.stderr.strip() or "scrot failed",
        )
    with open("/tmp/screen.png", "rb") as f:
        return {"image": base64.b64encode(f.read()).decode()}


class Action(BaseModel):
    type: str
    x: int | None = None
    y: int | None = None
    text: str | None = None
    direction: str = "down"
    amount: int = 3


@app.post("/action")
def action(a: Action):
    if a.type in ("click", "double_click", "right_click", "move"):
        xdo("mousemove", a.x, a.y)
    if a.type == "click":
        xdo("click", 1)
    elif a.type == "double_click":
        xdo("click", "--repeat", 2, 1)
    elif a.type == "right_click":
        xdo("click", 3)
    elif a.type == "type":
        xdo("type", "--delay", 20, a.text)
    elif a.type == "key":
        xdo("key", a.text)
    elif a.type == "scroll":
        xdo("click", "--repeat", a.amount, 5 if a.direction == "down" else 4)
    else:
        raise HTTPException(status_code=400, detail=f"unknown action type: {a.type}")
    return {"ok": True}
