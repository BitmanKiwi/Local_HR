"""
server.py

Local web server for the standalone HR display PC.

Serves index.html and a /ws WebSocket endpoint. ant_rx.py's ANT+ receiver
runs on its own background thread and calls back into this process on every
new HR reading; those callbacks are bridged onto the asyncio event loop with
call_soon_threadsafe so they can safely push out over WebSocket.

Run with:
    uvicorn server:app --host 0.0.0.0 --port 8000
Then open http://localhost:8000 in a browser on this PC.
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from ant_rx import AntHrReceiver

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("server")

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI()

_clients: Set[WebSocket] = set()
_loop: asyncio.AbstractEventLoop | None = None
_last_hr: int | None = None
_last_status: str = "starting"
_last_update_ts: float = 0.0


async def _broadcast(message: dict) -> None:
    dead = []
    for ws in _clients:
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _clients.discard(ws)


def _on_hr_update(bpm: int) -> None:
    global _last_hr, _last_update_ts
    _last_hr = bpm
    _last_update_ts = time.time()
    logger.info("HR update: %s bpm", bpm)
    if _loop is not None:
        _loop.call_soon_threadsafe(
            asyncio.create_task,
            _broadcast({"type": "hr", "bpm": bpm, "ts": _last_update_ts}),
        )


def _on_status(msg: str) -> None:
    global _last_status
    _last_status = msg
    logger.info("ANT+ status: %s", msg)
    if _loop is not None:
        _loop.call_soon_threadsafe(
            asyncio.create_task,
            _broadcast({"type": "status", "message": msg}),
        )


receiver = AntHrReceiver(on_hr_update=_on_hr_update, on_status=_on_status)


@app.on_event("startup")
async def startup() -> None:
    global _loop
    _loop = asyncio.get_running_loop()
    receiver.start()


@app.on_event("shutdown")
async def shutdown() -> None:
    receiver.stop()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    _clients.add(websocket)
    # Send current state immediately so the page doesn't sit blank until
    # the next heartbeat comes in.
    await websocket.send_json({"type": "status", "message": _last_status})
    if _last_hr is not None:
        await websocket.send_json({"type": "hr", "bpm": _last_hr, "ts": _last_update_ts})
    try:
        while True:
            # We don't expect messages from the client; this just keeps
            # the connection open and detects disconnects.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(websocket)
