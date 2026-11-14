# jamsync

**Listen together, in sync.** Keep many phones, laptops and browsers playing the *same moment* of the same track: a small server that holds one shared timeline, and a dependency-free browser client that makes any `<audio>` or `<video>` element follow it.

It doesn't stream audio. Each device plays the file from wherever it already gets it; jamsync only keeps them in step. It was extracted from a self-hosted music app where it runs a "Jam" (listen-together) feature.

| Measured | Same machine (CI-style test) | Real use over the internet |
|---|---|---|
| Steady drift between devices | median 19 ms, max 31 ms | under 70 ms |
| Pause / play reaches others | ~30 ms | ~110 ms |
| Track change reaches others | ~10 ms | ~310 ms |

## How it works

```
            POST /jam {action, track_id, position_ms, dev}
  device ──────────────────────────────────────────────▶  server: one timeline
     ▲                                                    {track_id, position_ms at server time `updated`,
     └──────────── SSE /jam/events {state, version} ◀────  playing, queue, version}
```

1. **One timeline, stamped with server time.** The server never ticks. It stores where the track was at the moment it last changed; everyone computes *now* as `position_ms + (server_now − updated)`.
2. **Clock offset, NTP-style.** Each device makes 5 quick requests and keeps the one with the smallest round trip (least error), giving its offset to the server clock. It's refreshed every minute.
3. **Big gaps: jump. Small gaps: nudge.** When a new state version arrives, a device seeks straight there. Between updates, every 250 ms it compares itself to the room. Over 0.3 s off: seek again. 25 ms to 0.3 s off: change `playbackRate` (at most ±8 %) so it drifts back without an audible jump.
4. **Each device learns its own seek delay.** A jump never lands exactly: buffered audio is near-instant, a fetch takes longer. After each jump the measured error is fed back (separately for "buffered" and "needs fetch") and stored, so the next jump lands on time.
5. **No echo.** Only what the *user* did is sent to the server. Play, pause and seek calls the library makes to follow the room are suppressed, so devices never fight over the timeline.
6. **Server rules for races.**
   - *Stale messages are ignored:* a "pause" about a track the room has already moved on from (for example, the automatic pause when a song ends) is dropped.
   - *First track change wins:* when several devices reach the end of a song together and all ask for the next one, `prev_track_id` lets the server accept the first and ignore the rest.
   - *One host at a time,* with takeover after a period of silence (`orphan_seconds`).
7. **Safari mode.** Safari stutters when `playbackRate` changes, so on Safari it only seeks, with a wider tolerance and a cooldown.

## Run the demo

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn jamsync.server:app --host 0.0.0.0 --port 8000
```

Open `http://<this-machine>:8000/` on two or more devices. Give each a name, tap **Start a Jam** on one and **Join** on the others. The demo tracks are generated tones that beep every second, so you can hear whether devices are in step. The status box shows each device's clock offset, round trip, distance from the room and current speed.

## Use it in your app

**Server.** `jamsync.room.JamRoom` is a framework-free, thread-safe state machine; `jamsync/server.py` is a ~100-line FastAPI example (`GET /jam`, `POST /jam`, `GET /jam/events` as Server-Sent Events). Put your own authentication in `current_user()`; the demo trusts an `X-User` header and **must not be exposed to the internet as is**.

**Client.**

```js
import { JamSync } from './client/jamsync.js';

const jam = new JamSync({
  media: document.querySelector('audio'),
  baseUrl: '/jam',
  loadTrack: async (trackId, item) => { audio.src = urlFor(trackId); },
  onNeedTap: () => showButton('Tap to listen'),   // browser autoplay rules
});

await jam.start('track-1');   // host
await jam.join();             // everyone else

// before any playback change the USER asked for:
jam.userAction(); audio.pause();
jam.changeTrack('track-2');   // user picked another track
```

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m pytest -q
```

## Licence

MIT, see [LICENSE](LICENSE).
