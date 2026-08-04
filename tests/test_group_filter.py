"""Tests for the group_ids filter, which is the fix for issue #5.

GroupMe pushes every group's traffic down the personal /user channel, so
subscribing to a group cannot narrow the stream -- filtering has to happen
client side.
"""

import json

import pytest

from groupme_push.client import PushClient
from conftest import USER_ID, envelope


@pytest.fixture
def filtered(ws):
    client = PushClient(
        access_token="token", group_ids=["55555"], threaded_callbacks=False
    )
    client.user_id = USER_ID
    client.client_id = "faye-client-id"
    client.ws = ws
    client._connected.set()
    return client


def test_wanted_group_gets_through(filtered, ws):
    received = []
    filtered.receive_message(received.append)

    filtered.on_message(ws, json.dumps([envelope(group_id="55555", text="wanted")]))

    assert [message["text"] for message in received] == ["wanted"]


def test_other_groups_are_dropped(filtered, ws):
    received = []
    filtered.receive_message(received.append)

    filtered.on_message(ws, json.dumps([envelope(group_id="99999", text="noise")]))

    assert received == []


def test_int_group_ids_are_accepted(ws):
    client = PushClient(access_token="token", group_ids=[55555], threaded_callbacks=False)
    client.user_id = USER_ID
    received = []
    client.receive_message(received.append)

    client.on_message(ws, json.dumps([envelope(group_id="55555")]))

    assert len(received) == 1


def test_group_id_is_read_from_the_channel_when_absent_from_subject(filtered, ws):
    received = []
    filtered.receive_other(received.append)

    filtered.on_message(
        ws,
        json.dumps(
            [
                {
                    "channel": "/group/99999",
                    "id": "4",
                    "data": {"type": "typing", "subject": {"user_id": "1"}},
                }
            ]
        ),
    )

    assert received == []


def test_dms_are_never_filtered_out(filtered, ws):
    dms = []
    filtered.receive_dm(dms.append)

    filtered.on_message(
        ws,
        json.dumps(
            [envelope(event_type="direct_message.create", group_id=None, channel="/user/1")]
        ),
    )

    assert len(dms) == 1


def test_no_filter_means_everything_passes(connected, ws):
    received = []
    connected.receive_message(received.append)

    connected.on_message(
        ws,
        json.dumps([envelope(group_id="1"), envelope(group_id="2")]),
    )

    assert len(received) == 2
