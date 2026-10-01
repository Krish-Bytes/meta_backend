from rebuildx_presence.engine import PresenceConfig, PresenceEngine, Visibility


class FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def engine() -> tuple[PresenceEngine, FakeClock]:
    clock = FakeClock()
    cfg = PresenceConfig(session_ttl_seconds=6, offline_debounce_seconds=0.8)
    pe = PresenceEngine(clock, cfg)
    pe.upsert_user("alice", "Alice", {"bob", "you"})
    pe.upsert_user("bob", "Bob", {"alice", "you"})
    pe.upsert_user("you", "You", {"alice", "bob"})
    return pe, clock


def test_connect_makes_user_online_and_notifies_once():
    pe, _ = engine()
    events = pe.connect("alice", "s1")
    assert pe.is_online("alice")
    assert len(events) == 1
    assert events[0].online is True
    assert events[0].reason == "session_opened"


def test_second_device_does_not_republish_online():
    pe, _ = engine()
    pe.connect("alice", "phone")
    events = pe.connect("alice", "laptop")
    assert events == []
    assert pe.is_online("alice")


def test_heartbeats_do_not_broadcast():
    pe, clock = engine()
    pe.connect("alice", "s1")
    before = pe.metrics["state_broadcasts"]
    for _ in range(20):
        clock.advance(2)
        assert pe.heartbeat("s1") == []
    assert pe.metrics["state_broadcasts"] == before
    assert pe.metrics["heartbeats"] == 20


def test_clean_disconnect_waits_debounce_then_sets_last_seen():
    pe, clock = engine()
    pe.connect("alice", "s1")
    assert pe.disconnect("s1") == []
    assert pe.is_online("alice") is False
    assert pe.sweep() == []
    clock.advance(0.8)
    events = pe.sweep()
    assert len(events) == 1
    assert events[0].online is False
    assert events[0].last_seen_at == clock.t
    assert pe.users["alice"].last_seen_at == clock.t


def test_reconnect_during_debounce_suppresses_offline_flap():
    pe, clock = engine()
    pe.connect("alice", "s1")
    pe.disconnect("s1")
    clock.advance(0.3)
    events = pe.connect("alice", "s2")
    assert events == []
    clock.advance(2)
    assert pe.sweep() == []
    assert pe.is_online("alice")


def test_dead_phone_detected_by_ttl_without_socket_close():
    pe, clock = engine()
    pe.connect("alice", "s1")
    clock.advance(6.1)
    events = pe.sweep()
    assert events[0].reason == "heartbeat_ttl"
    assert pe.is_online("alice") is False


def test_heartbeat_prevents_ttl_expiry():
    pe, clock = engine()
    pe.connect("alice", "s1")
    for _ in range(5):
        clock.advance(5)
        pe.heartbeat("s1")
        assert pe.sweep() == []
    assert pe.is_online("alice")


def test_multi_device_stays_online_if_one_phone_dies():
    pe, clock = engine()
    pe.connect("alice", "phone")
    pe.connect("alice", "laptop")
    clock.advance(6.1)
    pe.heartbeat("laptop")
    pe.sweep()
    assert pe.is_online("alice")


def test_fanout_skips_offline_watchers():
    pe, _ = engine()
    pe.subscribe("bob", ["alice"])
    pe.subscribe("you", ["alice"])
    pe.connect("bob", "b1")
    events = pe.connect("alice", "a1")
    rec = pe.recipients_for(events[0])
    viewer_ids = {v for v, _ in rec}
    assert viewer_ids == {"bob"}


def test_privacy_nobody_hides_online_and_last_seen():
    pe, _ = engine()
    pe.set_visibility("alice", Visibility.NOBODY)
    pe.connect("alice", "a1")
    pe.connect("bob", "b1")
    view = pe.view_for("bob", "alice")
    assert view.hidden is True
    assert view.online is False
    assert view.last_seen_at is None


def test_reciprocity_if_viewer_hides_last_seen():
    pe, _ = engine()
    pe.set_visibility("bob", Visibility.NOBODY)
    pe.connect("alice", "a1")
    view = pe.view_for("bob", "alice")
    assert view.hidden is True


def test_contacts_visibility():
    pe, _ = engine()
    pe.upsert_user("stranger", "Stranger", set())
    pe.set_visibility("alice", Visibility.CONTACTS)
    pe.connect("alice", "a1")
    assert pe.view_for("bob", "alice").hidden is False
    assert pe.view_for("stranger", "alice").hidden is True
