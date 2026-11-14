/*! jamsync client: keep many <audio>/<video> elements playing the same moment.  MIT licence.
 *
 * The server holds one timeline: {track_id, position_ms at server time `updated`, playing}.
 * Every device, the host included, follows it the same way:
 *   1. measure this device's clock offset to the server (NTP-style, best of 5 round trips)
 *   2. on each new state version: jump there at once (plus this device's learned seek delay)
 *   3. in between, every 250 ms: if off by > 0.3 s seek again; if off by > 25 ms nudge
 *      playbackRate (at most +-8 %) so the gap closes without an audible jump
 *   4. only *user* actions are sent back; play()/pause() done to follow the room are not
 *      (echo suppression), so devices never fight each other.
 *
 * Usage:
 *   const jam = new JamSync({ media: audioEl, baseUrl: '/jam',
 *     loadTrack: async (trackId, item) => { audioEl.src = urlFor(trackId) } });
 *   await jam.start('song-1')  // or await jam.join()
 *   // call jam.userAction() right before you call play()/pause()/seek because the USER asked
 */
export class JamSync {
  constructor({ media, baseUrl = '/jam', loadTrack, headers = {}, onState = () => {}, onNeedTap = () => {} }) {
    if (!media) throw new Error('JamSync: media element required');
    this.m = media; this.base = baseUrl.replace(/\/$/, ''); this.loadTrack = loadTrack;
    this.headers = headers; this.onState = onState; this.onNeedTap = onNeedTap;
    this.dev = Math.random().toString(36).slice(2, 10);     // lets us recognise our own echo
    this.state = null; this.ver = -1; this.joined = false;
    this.skew = 0; this.rtt = 200;                            // ms: server clock - local clock; round trip
    this.userUntil = 0; this.autoUntil = 0; this.coolUntil = 0; this.lastFix = 0; this.measure = null;
    this.trackId = '';
    // learned seek delay (s) per situation: inside already-buffered audio vs. needs a fetch
    this.seekLat = Object.assign({ buf: 0.02, fetch: 0.15 }, JSON.parse(localStorage.getItem('jamsync.seekLat') || '{}'));
    this.safari = /iPad|iPhone|iPod/.test(navigator.userAgent) ||
      (/safari/i.test(navigator.userAgent) && !/chrome|chromium|android|crios|fxios|edg/i.test(navigator.userAgent));
    this._chain = Promise.resolve();
    this._wire();
  }

  // ---------- public API ----------
  async start(trackId, item = null) { await this.clockSync(); this.trackId = trackId;
    this._apply(await this._post({ action: 'start', track_id: trackId, position_ms: 0, playing: true, item })); this._connect(); }
  async join() { const s = await this.clockSync(); this.joined = true; this.ver = -1;
    await this._post({ action: 'join' }); this._onState(s); this._connect(); }
  async leave() { this.joined = false; this._disconnect(); this._resetRate(); try { await this._post({ action: 'leave' }) } catch {} }
  async stop() { await this._post({ action: 'stop' }); this.leave(); }
  /** call before you change playback because the user asked (play/pause/seek/next) */
  userAction() { this.userUntil = Date.now() + 1500; }
  /** user picked another track: send it (prev = the one we're leaving, for first-writer-wins) */
  changeTrack(trackId, item = null) { const prev = this.trackId; this.trackId = trackId; this._push('track', { force: true, prev, item }); }
  /** NTP-style: keep the sample with the smallest round trip, it has the least error */
  async clockSync(samples = 5) {
    let best = null;
    for (let i = 0; i < samples; i++) {
      const t0 = performance.now(), w0 = Date.now();
      const s = await this._get();
      const rtt = performance.now() - t0;
      if (!best || rtt < best.rtt) best = { rtt, skew: s.server_now * 1000 - (w0 + rtt / 2), s };
    }
    this.skew = best.skew; this.rtt = best.rtt; return best.s;
  }
  /** where the room says the song is right now (seconds) */
  roomPosition(s = this.state) {
    return (s.position_ms + (s.playing ? Math.max(0, Date.now() + this.skew - s.updated * 1000) : 0)) / 1000;
  }

  // ---------- internals ----------
  _wire() {
    const m = this.m, auto = () => Date.now() < this.autoUntil;
    m.addEventListener('play', () => { if (!auto()) this._push('play') });
    m.addEventListener('pause', () => { if (!auto() && !m.ended) this._push('pause') });
    m.addEventListener('seeked', () => { if (!auto() && Date.now() > this.coolUntil) this._push('seek') });
    this._tick = setInterval(() => this._driftTick(), 250);
  }
  _connect() {
    this._disconnect(); this.joined = true;
    if ('EventSource' in window) {
      this.es = new EventSource(this.base + '/events');
      this.es.onmessage = e => { try { const d = JSON.parse(e.data); if (d.type === 'jam_state') this._onState(d.state) } catch {} };
      this.es.onerror = () => { if (!this.poll) this.poll = setInterval(() => this._get().then(s => this._onState(s)).catch(() => {}), 1000) };
      this.es.onopen = () => { clearInterval(this.poll); this.poll = null };
    } else this.poll = setInterval(() => this._get().then(s => this._onState(s)).catch(() => {}), 1000);
    this._clockTimer = setInterval(() => this.clockSync(3).catch(() => {}), 60000);
  }
  _disconnect() { this.es?.close(); this.es = null; clearInterval(this.poll); this.poll = null; clearInterval(this._clockTimer); }
  _onState(s) {
    this.state = s; this.onState(s);
    if (!this.joined) return;
    if (!s.active) { this.leave(); return; }
    if (s.version > this.ver) { this.ver = s.version; this._apply(s); }
  }
  async _apply(s) {
    this.state = s; this.ver = Math.max(this.ver, s.version);
    if (!s.track_id) return;
    if (s.track_id !== this.trackId) {                       // different song: load it, start at the right moment
      this.trackId = s.track_id; this._auto();
      await this.loadTrack(s.track_id, s.item);
      if (this.state.version !== s.version) return;          // something newer arrived meanwhile
      this.coolUntil = Date.now() + 1500; this.measure = s.playing ? { key: 'fetch' } : null;
      this.m.currentTime = s.playing ? this.roomPosition(s) + this.seekLat.fetch : s.position_ms / 1000;
      return s.playing ? this._play() : this._pause();
    }
    if (!s.playing) { this._pause(); if (Math.abs(this.m.currentTime - s.position_ms / 1000) > 0.1) this._seek(s.position_ms / 1000, false); return; }
    if (s.dev === this.dev) { if (this.m.paused) this._play(); return; }   // our own tap; the tick evens out lag
    if (Math.abs(this.m.currentTime - this.roomPosition(s)) > 0.25) this._seek(null, true);
    this._play();
  }
  _driftTick() {
    const s = this.state, m = this.m;
    if (!this.joined || !s?.active || !s.playing || s.track_id !== this.trackId || m.paused || m.seeking || m.readyState < 3) return this._resetRate();
    if (Date.now() < this.coolUntil) return this._resetRate();
    const d = m.currentTime - this.roomPosition(s);            // + means this device is ahead
    if (this.measure) {                                        // learn how long a jump really takes here
      const k = this.measure.key; this.measure = null;
      if (Math.abs(d) < 3) { this.seekLat[k] = Math.min(3, Math.max(0, this.seekLat[k] - d * 0.8)); localStorage.setItem('jamsync.seekLat', JSON.stringify(this.seekLat)); }
    }
    if (this.safari) {                                         // Safari glitches on rate changes: seek only, less often
      if (Math.abs(d) > 0.45 && Date.now() - this.lastFix > 3500) this._seek(null, true);
      return this._resetRate();
    }
    if (Math.abs(d) > 0.3) return this._seek(null, true);
    if (Math.abs(d) > 0.025) m.playbackRate = Math.min(1.08, Math.max(0.92, 1 - d * 0.6));
    else this._resetRate();
  }
  _buffered(t) { const b = this.m.buffered; for (let i = 0; i < b.length; i++) if (t >= b.start(i) && t < b.end(i) - 0.5) return true; return false; }
  _seek(pos, playing) {
    const base = pos != null ? pos : this.roomPosition(), key = this._buffered(base) ? 'buf' : 'fetch';
    this._resetRate(); this._auto();
    this.coolUntil = Date.now() + (key === 'buf' ? 500 : 1200); this.measure = playing ? { key } : null; this.lastFix = Date.now();
    this.m.currentTime = base + (playing ? this.seekLat[key] : 0);
  }
  _resetRate() { if (this.m.playbackRate !== 1) this.m.playbackRate = 1; }
  _auto() { this.autoUntil = Date.now() + 600; }
  _play() { if (this.m.paused) { this._auto(); this.m.play().catch(() => this.onNeedTap()); } }
  _pause() { if (!this.m.paused) { this._auto(); this.m.pause(); } }
  _push(action, { force = false, prev = '', item = null } = {}) {
    if (!this.joined || !this.trackId) return;
    if (!force && Date.now() > this.userUntil) return;          // only what the user did
    const playing = !this.m.paused || action === 'play' || action === 'track';
    // the server stamps the time on arrival, so say where we'll be by then
    const pos = this.m.currentTime + (playing && action !== 'pause' ? this.rtt / 2000 : 0);
    const body = { action, track_id: this.trackId, position_ms: Math.round(Math.max(0, pos) * 1000), dev: this.dev, prev_track_id: prev };
    if (action === 'track') { body.playing = true; body.position_ms = 0; body.item = item; }
    this._chain = this._chain.then(() => this._post(body).then(s => this._onState(s)).catch(() => {}));   // in order
  }
  async _get() { const r = await fetch(this.base, { headers: this.headers, cache: 'no-store' }); if (!r.ok) throw new Error(r.status); return r.json(); }
  async _post(body) {
    const r = await fetch(this.base, { method: 'POST', headers: { 'Content-Type': 'application/json', ...this.headers }, body: JSON.stringify(body) });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.status); return r.json();
  }
}
