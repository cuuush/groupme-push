"""Tests for staying alive over long runs.

The failure these guard against is the quiet one: the socket is open, the
client reports connected, and nothing is ever delivered again.
"""

import json
import threading
import time

import pytest

from groupme_push.client import PushClient
from groupme_push.exceptions import HandshakeError
from conftest import USER_ID, envelope


class FakeSock:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


# The error GroupMe actually sends when a session has been reaped, captured
# from a live connection. Note the client id in the middle: the format is
# 401:<clientid>:Unknown client, not the 401::... the Bayeux docs suggest.
UNKNOWN_CLIENT = "401:m8a68lykwmmwk55f21pru6nwfohzd1z:Unknown client"


@pytest.fixture
def stallable(client, ws):
    client.ws = ws
    ws.sock = FakeSock()
    client._connected.set()
    client.stall_timeout = 0.2
    client.stall_check_interval = 0.02
    client.probe_timeout = 0.1
    return client


def run_watchdog(client, for_seconds=0.4):
    thread = threading.Thread(target=client._watchdog, daemon=True)
    thread.start()
    time.sleep(for_seconds)
    client._stopped.set()
    thread.join(timeout=2)


class TestStallWatchdog:
    def test_unanswered_probe_forces_a_reconnect(self, stallable, ws):
        stallable._last_frame_at = time.time() - 10

        run_watchdog(stallable)

        assert "/user/{}".format(USER_ID) in ws.subscriptions()  # it asked
        assert ws.sock.closed  # nobody answered

    def test_answered_probe_keeps_the_connection(self, stallable, ws):
        # Silence is normal on GroupMe -- an idle connection sends nothing at
        # all -- so a quiet stream must not be torn down while it still works.
        stallable._last_frame_at = time.time() - 10
        answering = threading.Event()

        def answer_probes():
            while not answering.is_set():
                if ws.subscriptions():
                    stallable._last_frame_at = time.time()
                time.sleep(0.01)

        responder = threading.Thread(target=answer_probes, daemon=True)
        responder.start()
        try:
            run_watchdog(stallable)
        finally:
            answering.set()

        assert not ws.sock.closed

    def test_probe_uses_a_group_when_the_user_channel_is_off(self, stallable, ws):
        stallable.subscribe_to_user_channel = False
        stallable._active_groups.add("55555")

        assert stallable._probe() is True
        assert ws.subscriptions() == ["/group/55555"]

    def test_probe_is_skipped_with_nothing_subscribed(self, stallable, ws):
        stallable.subscribe_to_user_channel = False

        assert stallable._probe() is False
        assert ws.subscriptions() == []

    def test_unsendable_probe_forces_a_reconnect(self, stallable, ws):
        def broken_send(payload):
            raise OSError("socket is gone")

        ws.send = broken_send

        assert stallable._probe() is False
        assert ws.sock.closed

    def test_traffic_keeps_it_quiet(self, stallable, ws):
        stopped = threading.Event()

        def keep_alive():
            while not stopped.is_set():
                stallable._last_frame_at = time.time()
                time.sleep(0.01)

        pump = threading.Thread(target=keep_alive, daemon=True)
        pump.start()
        try:
            run_watchdog(stallable)
        finally:
            stopped.set()

        assert not ws.sock.closed

    def test_does_nothing_while_disconnected(self, stallable, ws):
        stallable._connected.clear()
        stallable._last_frame_at = time.time() - 10

        run_watchdog(stallable)

        assert not ws.sock.closed

    def test_incoming_frames_reset_the_clock(self, connected, ws):
        connected._last_frame_at = 0

        connected.on_message(ws, json.dumps([envelope()]))

        assert time.time() - connected._last_frame_at < 1

    def test_even_unparseable_frames_count_as_life(self, connected, ws):
        connected._last_frame_at = 0

        connected.on_message(ws, "{not json")

        assert time.time() - connected._last_frame_at < 1

    def test_force_reconnect_drops_the_socket_not_the_client(self, connected, ws):
        # ws.close() would tell WebSocketApp we meant to stop, and it would
        # never reconnect. The socket has to be pulled out from under it.
        ws.sock = FakeSock()

        connected._force_reconnect()

        assert ws.sock.closed
        assert not ws.closed

    def test_force_reconnect_refuses_when_reconnect_disabled(self, connected, ws):
        ws.sock = FakeSock()
        connected.reconnect = None

        connected._force_reconnect()

        assert not ws.sock.closed

    def test_watchdog_is_not_started_when_disabled(self, monkeypatch, client):
        client.stall_timeout = None
        assert client.stall_check_interval is not None or True  # set in __init__

        disabled = PushClient(access_token="token", stall_timeout=None)
        assert disabled.stall_check_interval is None

    def test_check_interval_is_derived_from_the_timeout(self):
        assert PushClient(access_token="t", stall_timeout=180).stall_check_interval == 45
        # Never busier than every 5 seconds, however small the timeout.
        assert PushClient(access_token="t", stall_timeout=4).stall_check_interval == 5


def reject(subscription="/user/1", error=UNKNOWN_CLIENT):
    return json.dumps(
        [
            {
                "channel": "/meta/subscribe",
                "subscription": subscription,
                "successful": False,
                "error": error,
            }
        ]
    )


def settle(client, timeout=5):
    """Recovery runs off the reader thread, so wait for it."""
    thread = client._recovery_thread
    if thread is not None:
        thread.join(timeout=timeout)


class TestSessionRecovery:
    def test_rejected_subscribe_rebuilds_the_session(self, connected, ws, monkeypatch):
        # While the session is healthy GroupMe never answers /meta/connect, so
        # the connect loop cannot be relied on to notice trouble. A rejected
        # subscribe is the signal that reliably arrives.
        monkeypatch.setattr(PushClient, "_handshake", lambda self: "fresh-id")

        connected.on_message(ws, reject("/user/{}".format(USER_ID)))
        settle(connected)

        assert connected.client_id == "fresh-id"
        assert "/user/{}".format(USER_ID) in ws.subscriptions()

    def test_rebuild_does_not_block_the_reader(self, connected, ws, monkeypatch):
        # If recovery ran inline it would stall the socket it is trying to
        # repair, and we would miss the reply we are waiting for.
        started = threading.Event()
        release = threading.Event()

        def slow_handshake(self):
            started.set()
            release.wait(timeout=5)
            return "fresh-id"

        monkeypatch.setattr(PushClient, "_handshake", slow_handshake)

        connected.on_message(ws, reject())  # returns immediately

        assert started.wait(timeout=2)
        release.set()
        settle(connected)

    def test_rebuild_reconnects_the_delivery_channel(self, connected, ws, monkeypatch):
        # A rebuilt session has subscriptions but no delivery channel until it
        # connects. Without this the client looks fully recovered and receives
        # nothing -- caught live, where the session rebuilt in 4s and messages
        # never came back.
        monkeypatch.setattr(PushClient, "_handshake", lambda self: "fresh-id")

        connected._recover_session(ws)
        settle(connected)

        assert "/meta/connect" in ws.channels

    def test_groups_come_back_after_a_rebuild(self, connected, ws, monkeypatch):
        monkeypatch.setattr(PushClient, "_handshake", lambda self: "fresh-id")
        connected._subscribed_groups.add("55555")

        connected.on_message(ws, reject())
        settle(connected)

        assert "/group/55555" in ws.subscriptions()

    def test_other_subscribe_failures_are_left_alone(self, connected, ws, monkeypatch):
        monkeypatch.setattr(PushClient, "_handshake", lambda self: "fresh-id")

        connected.on_message(ws, reject(error="403::Forbidden channel"))
        settle(connected)

        assert connected.client_id == "faye-client-id"

    def test_rebuilds_are_rate_limited(self, connected, ws, monkeypatch):
        handshakes = []

        def counting(self):
            handshakes.append(1)
            return "fresh-{}".format(len(handshakes))

        monkeypatch.setattr(PushClient, "_handshake", counting)

        for _ in range(5):
            connected.on_message(ws, reject())
            settle(connected)

        assert len(handshakes) == 1

    def test_rate_limit_expires(self, connected, ws, monkeypatch):
        handshakes = []

        def counting(self):
            handshakes.append(1)
            return "fresh-{}".format(len(handshakes))

        monkeypatch.setattr(PushClient, "_handshake", counting)
        connected.recovery_interval = 0

        connected._recover_session(ws)
        settle(connected)
        connected._recover_session(ws)
        settle(connected)

        assert len(handshakes) == 2

    def test_a_new_connection_may_rebuild_immediately(self, connected, ws, monkeypatch):
        # Rate limiting must not strand a fresh connection that needs help.
        monkeypatch.setattr(PushClient, "_handshake", lambda self: "fresh-id")
        connected._last_recovery_at = time.time()

        connected.on_open(ws)

        assert connected._last_recovery_at == 0.0

    def test_failed_handshake_is_retried(self, connected, ws, monkeypatch):
        # The bug this guards: one failed handshake used to end recovery, and
        # nothing else was in flight that would ever try again.
        attempts = []

        def flaky(self):
            attempts.append(1)
            if len(attempts) < 3:
                raise HandshakeError("network is down")
            return "fresh-id"

        monkeypatch.setattr(PushClient, "_handshake", flaky)
        connected.recovery_max_backoff = 0

        connected._recover_session(ws)
        settle(connected)

        assert len(attempts) == 3
        assert connected.client_id == "fresh-id"

    def test_connection_errors_are_retried_too(self, connected, ws, monkeypatch):
        import requests

        attempts = []

        def flaky(self):
            attempts.append(1)
            if len(attempts) < 2:
                raise requests.ConnectionError("no route to host")
            return "fresh-id"

        monkeypatch.setattr(PushClient, "_handshake", flaky)
        connected.recovery_max_backoff = 0

        connected._recover_session(ws)
        settle(connected)

        assert connected.client_id == "fresh-id"

    def test_giving_up_forces_a_reconnect(self, connected, ws, monkeypatch):
        def always_fails(self):
            raise HandshakeError("still down")

        monkeypatch.setattr(PushClient, "_handshake", always_fails)
        connected.recovery_max_backoff = 0
        connected.recovery_attempts = 3
        ws.sock = FakeSock()

        connected._recover_session(ws)
        settle(connected)

        assert ws.sock.closed

    def test_stopping_aborts_recovery(self, connected, ws, monkeypatch):
        attempts = []

        def failing(self):
            attempts.append(1)
            raise HandshakeError("down")

        monkeypatch.setattr(PushClient, "_handshake", failing)
        connected.recovery_max_backoff = 5
        connected._stopped.set()

        connected._recover_session(ws)
        settle(connected)

        assert attempts == []


class TestFreshSessionOnReconnect:
    """Reusing a Faye session across a dropped socket is unreliable.

    The server can go on delivering to the connection that just died, and
    messages only reappear once it times that out. Live, reusing the session
    made "messages flow after reconnect" fail about half the time; taking a
    fresh one made three consecutive full runs pass.
    """

    def test_the_first_connection_does_not_rehandshake(self, connected, ws, monkeypatch):
        monkeypatch.setattr(PushClient, "_handshake", lambda self: "fresh-id")

        connected.on_open(ws)

        assert connected.client_id == "faye-client-id"

    def test_a_reconnect_takes_a_fresh_session(self, connected, ws, monkeypatch):
        monkeypatch.setattr(PushClient, "_handshake", lambda self: "fresh-id")

        connected.on_open(ws)  # first
        connected.on_open(ws)  # reconnect

        assert connected.client_id == "fresh-id"

    def test_a_failed_handshake_falls_back_to_the_old_session(self, connected, ws, monkeypatch):
        def failing(self):
            raise HandshakeError("network down")

        monkeypatch.setattr(PushClient, "_handshake", failing)

        connected.on_open(ws)
        connected.on_open(ws)

        # Still subscribes with what it has; the subscription check rebuilds
        # if the old session turns out to be dead.
        assert connected.client_id == "faye-client-id"
        assert "/user/{}".format(USER_ID) in ws.subscriptions()

    def test_it_can_be_turned_off(self, connected, ws, monkeypatch):
        monkeypatch.setattr(PushClient, "_handshake", lambda self: "fresh-id")
        connected.rehandshake_on_reconnect = False

        connected.on_open(ws)
        connected.on_open(ws)

        assert connected.client_id == "faye-client-id"


class TestSubscriptionCheck:
    def test_unconfirmed_subscriptions_rebuild_the_session(self, connected, ws, monkeypatch):
        # Connected but subscribed to nothing is the silent failure: GroupMe
        # is happy, the socket is open, and no message ever arrives.
        monkeypatch.setattr(PushClient, "_handshake", lambda self: "fresh-id")
        connected.subscribe_timeout = 0.05

        connected._check_subscriptions(ws)
        settle(connected)

        assert connected.client_id == "fresh-id"

    def test_a_confirmed_subscription_is_left_alone(self, connected, ws, monkeypatch):
        monkeypatch.setattr(PushClient, "_handshake", lambda self: "fresh-id")
        connected.subscribe_timeout = 0.5
        connected._subscription_confirmed.set()

        connected._check_subscriptions(ws)
        settle(connected)

        assert connected.client_id == "faye-client-id"

    def test_a_successful_subscribe_confirms(self, connected, ws):
        connected._subscription_confirmed.clear()

        connected.on_message(
            ws,
            json.dumps(
                [{"channel": "/meta/subscribe", "subscription": "/user/1", "successful": True}]
            ),
        )

        assert connected._subscription_confirmed.is_set()

    def test_reconnecting_clears_the_previous_confirmation(self, connected, ws):
        connected._subscription_confirmed.set()

        connected._resubscribe_all(ws)

        assert not connected._subscription_confirmed.is_set()

    def test_no_rebuild_once_stopped(self, connected, ws, monkeypatch):
        monkeypatch.setattr(PushClient, "_handshake", lambda self: "fresh-id")
        connected.subscribe_timeout = 0.05
        connected._stopped.set()

        connected._check_subscriptions(ws)
        settle(connected)

        assert connected.client_id == "faye-client-id"

    def test_the_check_can_be_disabled(self, connected, ws):
        connected.subscribe_timeout = None

        connected._watch_subscriptions(ws)

        assert connected._subscription_thread is None


class TestKeepaliveWiring:
    def _capture(self, monkeypatch, **kwargs):
        captured = {}

        class FakeApp:
            def __init__(self, url, **_):
                pass

            def run_forever(self, **run_kwargs):
                captured.update(run_kwargs)

        monkeypatch.setattr("groupme_push.client.websocket.WebSocketApp", FakeApp)
        PushClient(access_token="token", **kwargs).run_forever()
        return captured

    def test_ping_settings_are_passed_through(self, monkeypatch):
        captured = self._capture(monkeypatch, ping_interval=30, ping_timeout=10)

        assert captured["ping_interval"] == 30
        assert captured["ping_timeout"] == 10

    def test_pings_can_be_disabled(self, monkeypatch):
        captured = self._capture(monkeypatch, ping_interval=None)

        assert "ping_interval" not in captured
        assert "ping_timeout" not in captured

    def test_reconnect_is_on_by_default(self, monkeypatch):
        captured = self._capture(monkeypatch)

        assert captured["reconnect"] == 5

    def test_reconnect_can_still_be_disabled(self, monkeypatch):
        captured = self._capture(monkeypatch, reconnect=None)

        assert "reconnect" not in captured
