"""A full lifecycle run against a scripted websocket.

These exercise the wiring that the unit tests stub out: that start() hands the
right handlers to WebSocketApp, that on_open subscribes, and that frames
arriving on the socket reach user callbacks.
"""

import json
import threading

import pytest

from groupme_push.client import PushClient
from conftest import USER_ID, envelope


class ScriptedWebSocketApp:
    """A WebSocketApp that replays a canned list of frames after opening."""

    frames = []
    instances = []

    def __init__(self, url, on_message=None, on_error=None, on_open=None, on_close=None):
        self.url = url
        self.handlers = {
            "message": on_message,
            "error": on_error,
            "open": on_open,
            "close": on_close,
        }
        self.sent = []
        self.keep_running = True
        self.finished = threading.Event()
        ScriptedWebSocketApp.instances.append(self)

    def send(self, payload):
        self.sent.append(json.loads(payload))

    def close(self):
        self.keep_running = False

    def run_forever(self, **kwargs):
        self.handlers["open"](self)
        for frame in self.frames:
            if not self.keep_running:
                break
            self.handlers["message"](self, json.dumps(frame))
        self.handlers["close"](self, 1000, "done")
        self.finished.set()

    @property
    def channels(self):
        return [message["channel"] for batch in self.sent for message in batch]


@pytest.fixture
def scripted(monkeypatch):
    ScriptedWebSocketApp.instances = []
    ScriptedWebSocketApp.frames = []
    monkeypatch.setattr(
        "groupme_push.client.websocket.WebSocketApp", ScriptedWebSocketApp
    )

    def fake_get(url, **kwargs):
        class Response:
            status_code = 200
            text = (
                json.dumps({"response": {"user_id": str(USER_ID)}})
                if "users/me" in url
                else '/**/callback([{"clientId":"abc123","successful":true}]);'
            )

            def json(self):
                return json.loads(self.text)

        return Response()

    monkeypatch.setattr("groupme_push.client.requests.get", fake_get)
    return ScriptedWebSocketApp


def run_client(client, frames):
    ScriptedWebSocketApp.frames = frames
    client.start()
    client.join(timeout=5)
    return ScriptedWebSocketApp.instances[0]


def test_full_run_delivers_messages(scripted):
    received = []
    client = PushClient(
        access_token="token", on_message=received.append, threaded_callbacks=False
    )

    socket = run_client(client, [[envelope(text="first")], [envelope(text="second")]])

    assert [message["text"] for message in received] == ["first", "second"]
    assert socket.url == "wss://push.groupme.com/faye"
    assert socket.channels[0] == "/meta/subscribe"


def test_full_run_honours_the_group_filter(scripted):
    """The scenario from issue #5: only one group's messages should arrive."""
    received = []
    client = PushClient(
        access_token="token",
        on_message=received.append,
        group_ids=["55555"],
        threaded_callbacks=False,
    )

    run_client(
        client,
        [
            [envelope(group_id="55555", text="wanted")],
            [envelope(group_id="11111", text="other group")],
            [envelope(group_id="22222", text="another group")],
        ],
    )

    assert [message["text"] for message in received] == ["wanted"]


def test_subscribe_from_another_thread_while_running(scripted):
    """subscribe_to_group() right after start() must not silently no-op."""
    gate = threading.Event()
    client = PushClient(access_token="token", threaded_callbacks=False)

    class SlowOpen(ScriptedWebSocketApp):
        def run_forever(self, **kwargs):
            gate.wait(timeout=5)
            super().run_forever(**kwargs)

    ScriptedWebSocketApp.frames = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("groupme_push.client.websocket.WebSocketApp", SlowOpen)
        client.start()
        results = []
        subscriber = threading.Thread(
            target=lambda: results.append(client.subscribe_to_group("55555", timeout=5))
        )
        subscriber.start()
        gate.set()
        subscriber.join(timeout=5)
        client.join(timeout=5)

    # Returning True means it waited for the socket instead of no-opping, which
    # is what the old version did when called straight after start().
    assert results == [True]

    socket = ScriptedWebSocketApp.instances[0]
    subscriptions = [
        message["subscription"]
        for batch in socket.sent
        for message in batch
        if message["channel"] == "/meta/subscribe"
    ]
    assert "/group/55555" in subscriptions
