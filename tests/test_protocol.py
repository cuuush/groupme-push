"""Tests for the Faye protocol layer: handshake, connect loop, subscriptions."""

import json

import pytest

from groupme_push.client import PushClient, _parse_jsonp
from groupme_push.exceptions import (
    AuthenticationError,
    ConnectionTimeout,
    GroupMePushError,
    HandshakeError,
)
from conftest import USER_ID, envelope


class FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code

    def json(self):
        return json.loads(self.text)


@pytest.fixture
def fake_requests(monkeypatch):
    """Route both HTTP calls start() makes through canned responses."""
    calls = []

    user_body = json.dumps({"response": {"user_id": str(USER_ID)}})
    handshake_body = '/**/callback([{"clientId":"abc123","successful":true}]);'
    responses = {"users/me": FakeResponse(user_body), "faye": FakeResponse(handshake_body)}

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        for fragment, response in responses.items():
            if fragment in url:
                return response
        raise AssertionError("unexpected URL {}".format(url))

    monkeypatch.setattr("groupme_push.client.requests.get", fake_get)
    return responses, calls


class TestJsonpParsing:
    def test_standard_padding(self):
        assert _parse_jsonp('/**/callback([{"clientId":"x"}]);') == [{"clientId": "x"}]

    def test_padding_of_a_different_length(self):
        # The old byte-offset slicing broke on anything but /**/callback(...);
        assert _parse_jsonp('jsonpCallback([{"clientId":"x"}])') == [{"clientId": "x"}]

    def test_bare_json_still_parses(self):
        assert _parse_jsonp('[{"clientId":"x"}]') == [{"clientId": "x"}]

    def test_nested_parens_in_payload(self):
        assert _parse_jsonp('/**/callback([{"text":"hi (there)"}]);') == [
            {"text": "hi (there)"}
        ]


class TestStart:
    def test_fetches_user_id_and_client_id(self, fake_requests, monkeypatch):
        monkeypatch.setattr(PushClient, "run_forever", lambda self: None)
        client = PushClient(access_token="token")

        client.start()
        client.join(timeout=5)

        assert client.user_id == USER_ID
        assert client.client_id == "abc123"

    def test_sends_the_access_token_as_a_header(self, fake_requests, monkeypatch):
        monkeypatch.setattr(PushClient, "run_forever", lambda self: None)
        _, calls = fake_requests

        PushClient(access_token="sekrit").start()

        headers = calls[0][1]["headers"]
        assert headers["X-Access-Token"] == "sekrit"

    def test_bad_token_raises_instead_of_logging(self, fake_requests, monkeypatch):
        responses, _ = fake_requests
        responses["users/me"] = FakeResponse('{"meta":{"code":401}}', status_code=401)

        with pytest.raises(AuthenticationError):
            PushClient(access_token="bad").start()

    def test_garbage_user_response_raises(self, fake_requests):
        responses, _ = fake_requests
        responses["users/me"] = FakeResponse("<html>nope</html>")

        with pytest.raises(AuthenticationError):
            PushClient(access_token="token").start()

    def test_failed_handshake_raises(self, fake_requests):
        responses, _ = fake_requests
        responses["faye"] = FakeResponse("/**/callback([{}]);")

        with pytest.raises(HandshakeError):
            PushClient(access_token="token").start()

    def test_wait_raises_if_the_socket_never_opens(self, fake_requests, monkeypatch):
        monkeypatch.setattr(
            PushClient, "run_forever", lambda self: self._stopped.wait(timeout=5)
        )
        client = PushClient(access_token="token")

        with pytest.raises(ConnectionTimeout):
            client.start(wait=True, timeout=0.05)

        assert client._stopped.is_set()

    def test_wait_returns_once_the_socket_opens(self, fake_requests, monkeypatch):
        def fake_run_forever(self):
            self._connected.set()
            self._stopped.wait(timeout=5)

        monkeypatch.setattr(PushClient, "run_forever", fake_run_forever)
        client = PushClient(access_token="token")

        assert client.start(wait=True, timeout=5) is client

        client.stop()

    def test_starting_twice_raises(self, fake_requests, monkeypatch):
        monkeypatch.setattr(
            PushClient, "run_forever", lambda self: self._stopped.wait(timeout=5)
        )
        client = PushClient(access_token="token")
        client.start()

        with pytest.raises(GroupMePushError):
            client.start()

        client.stop()


class TestListGroups:
    def test_returns_ids_and_names(self, fake_requests):
        responses, _ = fake_requests
        responses["groups"] = FakeResponse(
            json.dumps(
                {"response": [{"group_id": 55555, "name": "Test"}, {"group_id": "6", "name": "Other"}]}
            )
        )

        groups = PushClient(access_token="token").list_groups()

        assert groups == [("55555", "Test"), ("6", "Other")]

    def test_bad_token_raises(self, fake_requests):
        responses, _ = fake_requests
        responses["groups"] = FakeResponse("{}", status_code=401)

        with pytest.raises(AuthenticationError):
            PushClient(access_token="bad").list_groups()

    def test_garbage_response_raises(self, fake_requests):
        responses, _ = fake_requests
        responses["groups"] = FakeResponse("<html>")

        with pytest.raises(GroupMePushError):
            PushClient(access_token="token").list_groups()


class TestConnectLoop:
    def test_on_open_subscribes_to_the_user_channel(self, connected, ws):
        connected.on_open(ws)

        assert "/user/{}".format(USER_ID) in ws.subscriptions()

    def test_on_open_sends_exactly_one_connect(self, connected, ws):
        connected.on_open(ws)

        assert ws.channels.count("/meta/connect") == 1

    def test_user_channel_can_be_disabled(self, client, ws):
        client.subscribe_to_user_channel = False

        client.on_open(ws)

        assert ws.subscriptions() == []

    def test_connect_reply_drives_the_next_connect(self, connected, ws):
        connected.on_message(
            ws,
            json.dumps(
                [{"channel": "/meta/connect", "successful": True, "id": "5"}]
            ),
        )

        assert ws.channels == ["/meta/connect"]

    def test_missing_advice_does_not_raise(self, connected, ws):
        connected.on_message(
            ws, json.dumps([{"channel": "/meta/connect", "successful": True}])
        )

        assert ws.channels == ["/meta/connect"]

    def test_no_reconnect_after_stop(self, connected, ws):
        connected._stopped.set()

        connected.on_message(
            ws, json.dumps([{"channel": "/meta/connect", "successful": True}])
        )

        assert ws.channels == []

    def test_ping_gets_a_reply_on_the_same_channel(self, connected, ws):
        connected.on_message(
            ws,
            json.dumps(
                [{"channel": "/user/9", "id": "6", "data": {"type": "ping"}}]
            ),
        )

        assert ws.channels == ["/user/9"]

    def test_expired_client_id_triggers_a_rehandshake(self, connected, ws, monkeypatch):
        monkeypatch.setattr(PushClient, "_handshake", lambda self: "fresh-client-id")
        connected._subscribed_groups.add("55555")

        connected.on_message(
            ws,
            json.dumps(
                [
                    {
                        "channel": "/meta/connect",
                        "successful": False,
                        "error": "401::Invalid client id",
                    }
                ]
            ),
        )

        assert connected.client_id == "fresh-client-id"
        assert "/group/55555" in ws.subscriptions()
        assert "/user/{}".format(USER_ID) in ws.subscriptions()

    def test_handshake_advice_also_triggers_a_rehandshake(self, connected, ws, monkeypatch):
        monkeypatch.setattr(PushClient, "_handshake", lambda self: "fresh-client-id")

        connected.on_message(
            ws,
            json.dumps(
                [
                    {
                        "channel": "/meta/connect",
                        "successful": False,
                        "advice": {"reconnect": "handshake"},
                    }
                ]
            ),
        )

        assert connected.client_id == "fresh-client-id"

    def test_unknown_connect_failure_retries(self, connected, ws):
        connected.retry_backoff = 0

        connected.on_message(
            ws,
            json.dumps(
                [
                    {
                        "channel": "/meta/connect",
                        "successful": False,
                        "error": "503::Service unavailable",
                    }
                ]
            ),
        )

        assert ws.channels == ["/meta/connect"]

    def test_advice_reconnect_none_stops_the_client(self, connected, ws):
        connected.on_message(
            ws,
            json.dumps(
                [
                    {
                        "channel": "/meta/connect",
                        "successful": False,
                        "advice": {"reconnect": "none"},
                    }
                ]
            ),
        )

        assert ws.closed
        assert not connected.is_connected


class TestSubscriptions:
    def test_subscribe_to_group_sends_a_subscribe(self, connected, ws):
        assert connected.subscribe_to_group("55555") is True
        assert ws.subscriptions() == ["/group/55555"]

    def test_subscribe_does_not_send_an_extra_connect(self, connected, ws):
        # Every extra /meta/connect forks the polling loop, so each event comes
        # back once per fork.
        connected.subscribe_to_group("55555")

        assert "/meta/connect" not in ws.channels

    def test_subscribing_before_connect_is_remembered(self, client):
        assert client.subscribe_to_group("55555", timeout=0.01) is False
        assert "55555" in client._subscribed_groups

    def test_remembered_groups_are_replayed_on_reconnect(self, client, ws):
        client.subscribe_to_group("55555", timeout=0.01)
        client.subscribe_to_group("66666", timeout=0.01)

        client.on_open(ws)

        assert "/group/55555" in ws.subscriptions()
        assert "/group/66666" in ws.subscriptions()

    def test_a_group_is_never_subscribed_twice_on_one_connection(self, client, ws):
        # Caught live: start(wait=True) returned mid-on_open, so the caller's
        # subscribe_to_group raced on_open's replay and both sent a subscribe.
        client.subscribe_to_group("55555", timeout=0.01)  # remembered only
        client.ws = ws
        client.on_open(ws)  # replays it and marks the socket connected
        client.subscribe_to_group("55555", timeout=1)  # now the caller retries

        assert ws.subscriptions().count("/group/55555") == 1

    def test_a_new_connection_resubscribes(self, client, ws):
        client.ws = ws
        client.subscribe_to_group("55555", timeout=0.01)
        client.on_open(ws)
        client.on_close(ws, 1006, "dropped")
        client.on_open(ws)

        assert ws.subscriptions().count("/group/55555") == 2

    def test_unsubscribe_stops_the_replay(self, connected, ws):
        connected.subscribe_to_group("55555")
        connected.unsubscribe_from_group("55555")

        assert "55555" not in connected._subscribed_groups
        assert "/meta/unsubscribe" in ws.channels

    def test_ids_are_base36_and_increasing(self, connected):
        assert connected.bump_id() == "2"
        assert connected.bump_id() == "3"

    def test_ids_track_the_server_high_water_mark(self, connected, ws):
        connected.on_message(ws, json.dumps([envelope(message_id="zz")]))

        assert connected.id == 1295  # base36 zz

    def test_non_base36_id_is_tolerated(self, connected, ws):
        connected.on_message(ws, json.dumps([envelope(message_id="!!!")]))


class TestLifecycle:
    def test_context_manager_starts_and_stops(self, fake_requests, monkeypatch):
        monkeypatch.setattr(
            PushClient, "run_forever", lambda self: self._stopped.wait(timeout=5)
        )

        with PushClient(access_token="token") as client:
            assert client.user_id == USER_ID

        assert client._stopped.is_set()

    def test_stop_closes_the_socket(self, connected, ws):
        connected.stop(timeout=0.1)

        assert ws.closed
        assert not connected.is_connected

    def test_on_close_clears_connected(self, connected, ws):
        connected.on_close(ws, 1006, "abnormal")

        assert not connected.is_connected

    def test_on_error_fires_the_error_callback(self, connected, ws):
        errors = []
        connected.receive_error(errors.append)

        connected.on_error(ws, RuntimeError("boom"))

        assert isinstance(errors[0], RuntimeError)
        assert not connected.is_connected

    def test_on_connect_fires_when_the_socket_opens(self, connected, ws):
        opened = []
        connected.receive_connect(lambda: opened.append(True))

        connected.on_open(ws)

        assert opened == [True]

    def test_reconnect_is_passed_through_to_run_forever(self, monkeypatch):
        captured = {}

        class FakeApp:
            def __init__(self, url, **kwargs):
                captured["url"] = url

            def run_forever(self, **kwargs):
                captured["kwargs"] = kwargs

        monkeypatch.setattr("groupme_push.client.websocket.WebSocketApp", FakeApp)
        PushClient(access_token="token", reconnect=5).run_forever()

        assert captured["kwargs"] == {"reconnect": 5}

    def test_no_reconnect_kwarg_when_unset(self, monkeypatch):
        captured = {}

        class FakeApp:
            def __init__(self, url, **kwargs):
                pass

            def run_forever(self, **kwargs):
                captured["kwargs"] = kwargs

        monkeypatch.setattr("groupme_push.client.websocket.WebSocketApp", FakeApp)
        PushClient(access_token="token").run_forever()

        assert captured["kwargs"] == {}
