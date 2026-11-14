"""The shared timeline for one listening room.

A room never streams audio. It only stores *where the song should be*:

    position_ms at server time `updated`, plus `playing`

so any device can compute the current position as

    position_ms + (now - updated) * 1000   (while playing)

Every change bumps `version`; clients apply a state only if its version is newer
than the last one they applied. This module has no web framework or I/O, so it
is easy to test and to embed in any server.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

ACTIONS = {"start", "join", "leave", "play", "pause", "seek", "track", "queue", "stop"}
MAX_QUEUE = 50


class JamError(Exception):
    """Raised for a request the room refuses. `status` maps naturally to HTTP."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class Action:
    action: str
    user: str
    track_id: str = ""
    position_ms: int = 0
    playing: Optional[bool] = None
    prev_track_id: str = ""          # for "track": the song the sender was moving away from
    device: str = ""                 # random per-device id, lets a client recognise its own echo
    item: Optional[dict] = None      # optional metadata for the current track
    queue: list = field(default_factory=list)


class JamRoom:
    """Thread-safe shared playback state for one group of listeners.

    `orphan_seconds`: if a different user starts a Jam while one is running, it is
    refused (409) unless the running one has had no activity for this long.
    `clock` is injectable for tests.
    """

    def __init__(self, orphan_seconds: float = 600, clock: Callable[[], float] = time.time):
        self._lock = threading.RLock()
        self._clock = clock
        self.orphan_seconds = orphan_seconds
        self.state: dict[str, Any] = {
            "active": False, "id": "", "host": "", "track_id": "", "position_ms": 0,
            "playing": False, "updated": 0.0, "queue": [], "item": None, "version": 0,
            "by": "", "dev": "", "action": "",
        }
        self.members: set[str] = set()

    # ---- reading -------------------------------------------------------------
    def snapshot(self) -> dict:
        """State plus `server_now`, which clients use to measure their clock offset."""
        with self._lock:
            return {**self.state, "queue": list(self.state["queue"]), "members": sorted(self.members),
                    "server_now": self._clock()}

    def position_ms(self) -> int:
        with self._lock:
            s = self.state
            if not s["playing"]:
                return s["position_ms"]
            return int(s["position_ms"] + max(0.0, self._clock() - s["updated"]) * 1000)

    # ---- writing -------------------------------------------------------------
    def apply(self, a: Action, has_listeners: bool = True) -> tuple[dict, bool]:
        """Apply one action. Returns (snapshot, changed). `changed` False means nothing
        that clients need to act on happened (join/leave, or a stale/losing message)."""
        if a.action not in ACTIONS:
            raise JamError(400, "invalid action")
        now = self._clock()
        queue = [q for q in (a.queue or []) if isinstance(q, (dict, str))][:MAX_QUEUE]
        item = a.item if isinstance(a.item, dict) else None
        pos = max(0, int(a.position_ms))
        with self._lock:
            s = self.state
            changed = True
            if a.action == "start":
                busy = s["active"] and s["host"] != a.user and has_listeners
                if busy and now - float(s["updated"] or 0) < self.orphan_seconds:
                    raise JamError(409, f"{s['host']} is already hosting; join instead")
                s.update(active=True, id=uuid.uuid4().hex[:10], host=a.user, track_id=a.track_id,
                         position_ms=pos, updated=now, queue=queue, item=item,
                         playing=bool(a.track_id) if a.playing is None else bool(a.playing and a.track_id))
                self.members = {a.user}
            elif not s["active"]:
                raise JamError(404, "no active jam")
            elif a.action == "join":
                self.members.add(a.user); changed = False
            elif a.action == "leave":
                self.members.discard(a.user); changed = False
            elif a.action == "stop":
                if a.user != s["host"]:
                    raise JamError(403, "only the host can stop the jam")
                s["active"] = False; self.members = set()
            elif a.action in {"play", "pause", "seek"} and a.track_id and s["track_id"] and a.track_id != s["track_id"]:
                # Stale: e.g. the automatic "pause" a device sends when a song ends,
                # arriving after another device already moved the room to the next song.
                changed = False
            elif a.action in {"play", "pause"}:
                s.update(playing=a.action == "play", position_ms=pos, updated=now)
            elif a.action == "seek":
                s.update(position_ms=pos, updated=now)
            elif a.action == "track":
                if a.prev_track_id and s["track_id"] != a.prev_track_id:
                    # Several devices reach the end of a song at once and all ask for the
                    # next one: the first request wins, the rest are ignored.
                    changed = False
                else:
                    s.update(track_id=a.track_id, position_ms=pos, updated=now, item=item,
                             playing=True if a.playing is None else bool(a.playing))
                    if queue or item:
                        s["queue"] = queue
            elif a.action == "queue":
                s["queue"] = queue
            if changed:
                s["version"] += 1
                s.update(by=a.user, dev=a.device, action=a.action)
            return self.snapshot(), changed
