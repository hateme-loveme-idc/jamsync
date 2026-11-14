import pytest

from jamsync.room import Action, JamError, JamRoom


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def room():
    c = Clock()
    r = JamRoom(orphan_seconds=600, clock=c)
    r.clock = c
    return r


def start(room, user="host", track="a"):
    return room.apply(Action("start", user, track_id=track, position_ms=0))[0]


def test_start_sets_timeline_and_host(room):
    s = start(room)
    assert s["active"] and s["host"] == "host" and s["track_id"] == "a" and s["playing"]
    assert s["version"] == 1 and s["members"] == ["host"]


def test_position_advances_with_server_clock(room):
    start(room)
    room.clock.t += 2.5
    assert room.position_ms() == 2500


def test_pause_freezes_position(room):
    start(room)
    room.apply(Action("pause", "host", track_id="a", position_ms=4000))
    room.clock.t += 10
    assert room.position_ms() == 4000


def test_join_and_leave_do_not_bump_version(room):
    start(room)
    s, changed = room.apply(Action("join", "guest"))
    assert not changed and s["version"] == 1 and s["members"] == ["guest", "host"]
    s, changed = room.apply(Action("leave", "guest"))
    assert not changed and s["members"] == ["host"]


def test_stale_message_for_other_track_is_ignored(room):
    start(room)
    room.apply(Action("track", "guest", track_id="b", prev_track_id="a"))
    s, changed = room.apply(Action("pause", "host", track_id="a", position_ms=99000))
    assert not changed and s["track_id"] == "b" and s["playing"]


def test_first_track_change_wins(room):
    start(room)
    _, first = room.apply(Action("track", "u1", track_id="b", prev_track_id="a"))
    s, second = room.apply(Action("track", "u2", track_id="c", prev_track_id="a"))
    assert first and not second and s["track_id"] == "b"


def test_only_host_can_stop(room):
    start(room)
    with pytest.raises(JamError) as e:
        room.apply(Action("stop", "guest"))
    assert e.value.status == 403
    s, _ = room.apply(Action("stop", "host"))
    assert not s["active"]


def test_second_host_refused_while_jam_is_live(room):
    start(room)
    with pytest.raises(JamError) as e:
        room.apply(Action("start", "other", track_id="x"), has_listeners=True)
    assert e.value.status == 409


def test_orphaned_jam_can_be_taken_over(room):
    start(room)
    room.clock.t += 601
    s, _ = room.apply(Action("start", "other", track_id="x"), has_listeners=True)
    assert s["host"] == "other"


def test_actions_need_an_active_jam(room):
    with pytest.raises(JamError) as e:
        room.apply(Action("play", "x"))
    assert e.value.status == 404


def test_invalid_action_and_queue_cap(room):
    with pytest.raises(JamError):
        room.apply(Action("explode", "x"))
    start(room)
    s, _ = room.apply(Action("queue", "host", queue=[f"t{i}" for i in range(80)]))
    assert len(s["queue"]) == 50


def test_negative_positions_are_clamped(room):
    start(room)
    s, _ = room.apply(Action("seek", "host", track_id="a", position_ms=-500))
    assert s["position_ms"] == 0
