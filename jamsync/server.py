"""A minimal FastAPI server for jamsync: one room, Server-Sent Events, no database.

    GET  /jam          current state (+ server_now for clock sync)
    POST /jam          {action, track_id, position_ms, ...}
    GET  /jam/events   SSE stream of {"type": "jam_state", "state": {...}}

Who is who: the demo trusts an `X-User` header (default "guest"). In a real
deployment, put your own authentication in `current_user()`.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .room import Action, JamError, JamRoom

ROOT = Path(__file__).resolve().parent.parent
PING_SECONDS = 15

app = FastAPI(title="jamsync")
room = JamRoom()
listeners: set[asyncio.Queue] = set()


class JamIn(BaseModel):
    action: str
    track_id: str = ""
    position_ms: int = 0
    playing: Optional[bool] = None
    prev_track_id: str = ""
    dev: str = ""
    item: Optional[dict] = None
    queue: list = Field(default_factory=list)


def current_user(request: Request) -> str:
    """Replace with real authentication. The demo just labels browsers."""
    name = (request.headers.get("x-user") or "guest").strip()[:40]
    return name or "guest"


def broadcast(state: dict) -> None:
    msg = "data: " + json.dumps({"type": "jam_state", "state": state}) + "\n\n"
    for q in list(listeners):
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:          # a stuck client: drop it, it will reconnect
            listeners.discard(q)


@app.get("/jam")
def get_jam(request: Request):
    current_user(request)
    return room.snapshot()


@app.post("/jam")
def post_jam(body: JamIn, request: Request):
    user = current_user(request)
    try:
        snap, changed = room.apply(Action(
            action=body.action, user=user, track_id=body.track_id, position_ms=body.position_ms,
            playing=body.playing, prev_track_id=body.prev_track_id, device=body.dev,
            item=body.item, queue=body.queue), has_listeners=bool(listeners))
    except JamError as e:
        raise HTTPException(e.status, str(e))
    if changed or body.action in {"join", "leave"}:
        broadcast(snap)
    return snap


@app.get("/jam/events")
async def jam_events(request: Request):
    current_user(request)
    q: asyncio.Queue = asyncio.Queue(maxsize=50)
    listeners.add(q)

    async def stream():
        try:
            yield "data: " + json.dumps({"type": "jam_state", "state": room.snapshot()}) + "\n\n"
            while not await request.is_disconnected():
                try:
                    yield await asyncio.wait_for(q.get(), timeout=PING_SECONDS)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"     # keeps proxies from closing an idle stream
        finally:
            listeners.discard(q)

    # X-Accel-Buffering: nginx must not hold SSE messages back
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


app.mount("/client", StaticFiles(directory=ROOT / "client"), name="client")
app.mount("/demo-media", StaticFiles(directory=ROOT / "demo" / "media"), name="media")


@app.get("/")
def demo_page():
    return FileResponse(ROOT / "demo" / "index.html")
