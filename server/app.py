import base64
import os
import subprocess
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI()

DISPLAY = os.environ.get("DISPLAY", ":1")


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


@app.get("/health")
def health():
    """Liveness for the worker control API and X display."""
    # DISPLAY=:1 → socket /tmp/.X11-unix/X1 (no extra apt packages)
    display_num = DISPLAY.lstrip(":").split(".")[0]
    socket_path = f"/tmp/.X11-unix/X{display_num}"
    checks: dict[str, bool] = {
        "display_socket": os.path.exists(socket_path),
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
    return body


START_FIREFOX = os.environ.get("START_FIREFOX", "/opt/start-firefox.sh")


@app.post("/session/reset")
def session_reset():
    """Recycle the browser session without restarting the whole container."""
    _run(["pkill", "-f", "firefox-esr"], timeout=10)
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
    return {"ok": True, "reset": "firefox"}



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
