"""Tests for how incoming Faye envelopes turn into callback calls."""

import json

from conftest import USER_ID, envelope


def test_group_message_calls_on_message(connected, ws):
    received = []
    connected.receive_message(received.append)

    connected.on_message(ws, json.dumps([envelope(text="hi")]))

    assert [message["text"] for message in received] == ["hi"]


def test_dm_calls_on_dm_not_on_message(connected, ws):
    messages, dms = [], []
    connected.receive_message(messages.append)
    connected.receive_dm(dms.append)

    connected.on_message(
        ws, json.dumps([envelope(event_type="direct_message.create", group_id=None)])
    )

    assert messages == []
    assert len(dms) == 1


def test_likes_and_favorites_route_separately(connected, ws):
    likes, favorites = [], []
    connected.receive_like(likes.append)
    connected.receive_favorite(favorites.append)

    connected.on_message(
        ws,
        json.dumps(
            [envelope(event_type="like.create"), envelope(event_type="favorite")]
        ),
    )

    assert len(likes) == 1
    assert len(favorites) == 1


def test_unknown_type_goes_to_on_other_with_full_data(connected, ws):
    others = []
    connected.receive_other(others.append)

    connected.on_message(ws, json.dumps([envelope(event_type="poll.finished")]))

    assert others[0]["type"] == "poll.finished"
    assert "subject" in others[0]


def test_missing_callback_is_not_an_error(connected, ws):
    connected.on_message(ws, json.dumps([envelope()]))  # no callbacks registered


def test_subscribe_events_are_ignored(connected, ws):
    others = []
    connected.receive_other(others.append)

    connected.on_message(ws, json.dumps([envelope(event_type="subscribe")]))

    assert others == []


def test_receive_message_works_as_a_decorator(connected, ws):
    received = []

    @connected.receive_message
    def handler(message):
        received.append(message)

    # The fork's version returned None here, which silently unbound the name.
    assert handler is not None
    connected.on_message(ws, json.dumps([envelope(text="decorated")]))
    assert received[0]["text"] == "decorated"


def test_callback_exception_does_not_kill_the_batch(connected, ws):
    seen = []

    def boom(message):
        seen.append(message["text"])
        raise RuntimeError("callback blew up")

    connected.receive_message(boom)
    connected.on_message(
        ws, json.dumps([envelope(text="one"), envelope(text="two")])
    )

    assert seen == ["one", "two"]


def test_threaded_callbacks_still_fire(client, ws):
    import threading

    client.ws = ws
    client._connected.set()
    client.threaded_callbacks = True
    done = threading.Event()
    client.receive_message(lambda message: done.set())

    client.on_message(ws, json.dumps([envelope()]))

    assert done.wait(timeout=5)


def test_malformed_json_is_logged_not_raised(connected, ws):
    connected.on_message(ws, "{not json")


def test_envelope_without_data_is_not_fatal(connected, ws):
    connected.on_message(ws, json.dumps([{"channel": "/foo", "id": "3"}]))


def test_single_object_payload_is_accepted(connected, ws):
    received = []
    connected.receive_message(received.append)

    connected.on_message(ws, json.dumps(envelope(text="not in a list")))

    assert received[0]["text"] == "not in a list"


class TestDisregardSelf:
    def test_own_message_is_skipped(self, connected, ws):
        received = []
        connected.disregard_self = True
        connected.receive_message(received.append)

        connected.on_message(ws, json.dumps([envelope(sender_id=str(USER_ID))]))

        assert received == []

    def test_string_and_int_sender_ids_both_match(self, connected, ws):
        received = []
        connected.disregard_self = True
        connected.receive_message(received.append)

        connected.on_message(ws, json.dumps([envelope(sender_id=USER_ID)]))

        assert received == []

    def test_other_users_still_get_through(self, connected, ws):
        received = []
        connected.disregard_self = True
        connected.receive_message(received.append)

        connected.on_message(ws, json.dumps([envelope(sender_id="99999")]))

        assert len(received) == 1

    def test_system_messages_get_through(self, connected, ws):
        received = []
        connected.disregard_self = True
        connected.receive_message(received.append)

        connected.on_message(ws, json.dumps([envelope(sender_id="system")]))

        assert len(received) == 1

    def test_rest_of_batch_survives_a_self_message(self, connected, ws):
        # Regression: the old code used `return`, so one of the user's own
        # messages dropped every later message in the same frame.
        received = []
        connected.disregard_self = True
        connected.receive_message(received.append)

        connected.on_message(
            ws,
            json.dumps(
                [
                    envelope(sender_id=str(USER_ID), text="mine"),
                    envelope(sender_id="99999", text="theirs"),
                ]
            ),
        )

        assert [message["text"] for message in received] == ["theirs"]

    def test_non_numeric_sender_id_does_not_raise(self, connected, ws):
        received = []
        connected.disregard_self = True
        connected.receive_message(received.append)

        connected.on_message(ws, json.dumps([envelope(sender_id="abc")]))

        assert len(received) == 1
