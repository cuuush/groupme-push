import json

import pytest

from groupme_push.client import PushClient

USER_ID = 12345678


class FakeWebSocket:
    """Stands in for websocket.WebSocketApp and records what we send."""

    def __init__(self):
        self.sent = []
        self.closed = False

    def send(self, payload):
        self.sent.append(json.loads(payload))

    def close(self):
        self.closed = True

    @property
    def channels(self):
        """Every channel we have written to, in order."""
        return [message["channel"] for batch in self.sent for message in batch]

    def subscriptions(self):
        return [
            message["subscription"]
            for batch in self.sent
            for message in batch
            if message["channel"] == "/meta/subscribe"
        ]


@pytest.fixture
def ws():
    return FakeWebSocket()


@pytest.fixture
def client():
    """A connected-looking client with inline callbacks for determinism."""
    client = PushClient(access_token="token", threaded_callbacks=False)
    client.user_id = USER_ID
    client.client_id = "faye-client-id"
    return client


@pytest.fixture
def connected(client, ws):
    client.ws = ws
    client._connected.set()
    return client


def envelope(
    event_type="line.create",
    channel="/user/{}".format(USER_ID),
    sender_id="99999",
    group_id="55555",
    text="hello",
    message_id="2",
    **subject_extras
):
    """Build a Faye envelope shaped like the ones GroupMe pushes."""
    subject = {
        "id": "1600000000000000000",
        "sender_id": sender_id,
        "text": text,
        **subject_extras,
    }
    if group_id is not None:
        subject["group_id"] = group_id
    return {
        "channel": channel,
        "id": message_id,
        "data": {"type": event_type, "subject": subject},
    }
