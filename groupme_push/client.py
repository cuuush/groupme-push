"""A client for the GroupMe push service (Faye over websockets)."""

import json
import logging
import re
import threading
import time

import base36
import requests
import websocket

from groupme_push.exceptions import (
    AuthenticationError,
    ConnectionTimeout,
    GroupMePushError,
    HandshakeError,
)

logger = logging.getLogger("groupme-push")

USER_ENDPOINT = "https://api.groupme.com/v3/users/me"
GROUPS_ENDPOINT = "https://api.groupme.com/v3/groups"
FAYE_HTTP_ENDPOINT = "https://push.groupme.com/faye"
FAYE_WS_ENDPOINT = "wss://push.groupme.com/faye"

_JSONP_RE = re.compile(r"^[^(]*\((.*)\)[^)]*$", re.DOTALL)

# Faye replies with this error code when the client id we are using has been
# reaped server side, which happens after a network drop.
_INVALID_CLIENT_ID = "401"


def _parse_jsonp(text):
    """Return the JSON payload wrapped in a JSONP callback.

    GroupMe answers the handshake with ``/**/callback([{...}]);``. Older
    versions of this library sliced that apart by character offset, which broke
    whenever GroupMe changed the padding by a byte.
    """
    match = _JSONP_RE.match(text.strip())
    payload = match.group(1) if match else text
    return json.loads(payload)


class PushClient:
    """Listens to a user's GroupMe push channels and fires callbacks.

    Args:
        access_token: GroupMe access token for the user to listen as.
        on_message: called with the message subject for every group message.
        on_dm: called with the message subject for every direct message.
        on_like: called when somebody likes a message.
        on_favorite: called when your own user likes a message.
        on_other: called with the whole ``data`` blob for unrecognised events.
        on_connect: called with no arguments each time the socket is ready,
            including after an automatic reconnect.
        on_error: called with the exception whenever the websocket errors.
        disregard_self: skip events sent by the authenticated user.
        reconnect: seconds to wait before reconnecting after a dropped
            connection. ``None`` disables reconnection.
        group_ids: only dispatch events belonging to these group ids. GroupMe
            pushes *every* group's traffic down the personal ``/user`` channel,
            so this filter (not ``subscribe_to_group``) is what narrows the
            stream to one group. Events with no group id, such as DMs, are not
            affected.
        subscribe_to_user_channel: subscribe to ``/user/<id>`` on connect.
            This is where GroupMe delivers messages, so leaving it on is almost
            always what you want.
        threaded_callbacks: run each callback in its own thread (the default).
            Set to ``False`` to have callbacks run inline, which keeps events in
            order at the cost of blocking the socket while they run.
        request_timeout: timeout in seconds for the HTTP calls made by
            :meth:`start`.
        ping_interval: seconds between websocket pings, and ``ping_timeout``
            seconds to wait for the pong. Without these a connection that dies
            without closing cleanly, which is what a sleeping laptop or a NAT
            timeout looks like, never raises and the client waits forever.
            ``None`` disables them.
        stall_timeout: seconds of silence after which the client checks whether
            the stream is still alive, and rebuilds it if not. GroupMe sends
            nothing down an idle connection, so this costs one small frame per
            quiet interval rather than a reconnect. ``None`` disables the check.
        probe_timeout: how long that check waits for an answer.
    """

    def __init__(
        self,
        access_token,
        on_message=None,
        on_dm=None,
        on_like=None,
        on_favorite=None,
        on_other=None,
        disregard_self=False,
        reconnect=5,
        group_ids=None,
        subscribe_to_user_channel=True,
        threaded_callbacks=True,
        on_connect=None,
        on_error=None,
        request_timeout=5,
        ping_interval=30,
        ping_timeout=10,
        stall_timeout=180,
        probe_timeout=10,
    ):
        self.id = 1
        self.access_token = access_token
        self.message_callback = on_message
        self.dm_callback = on_dm
        self.like_callback = on_like
        self.favorite_callback = on_favorite
        self.other_callback = on_other
        self.connect_callback = on_connect
        self.error_callback = on_error
        self.disregard_self = disregard_self
        self.reconnect = reconnect
        self.subscribe_to_user_channel = subscribe_to_user_channel
        self.threaded_callbacks = threaded_callbacks
        self.request_timeout = request_timeout
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.stall_timeout = stall_timeout
        self.stall_check_interval = (
            max(5.0, stall_timeout / 4.0) if stall_timeout else None
        )
        self.probe_timeout = probe_timeout

        # Minimum gap between re-handshakes, so a server that keeps rejecting
        # us cannot turn into a handshake loop.
        self.rehandshake_interval = 30

        self.group_ids = {str(group_id) for group_id in group_ids or ()}

        # Seconds to wait before retrying a /meta/connect that failed for a
        # reason we do not recognise.
        self.retry_backoff = 1

        self.ws = None
        self.thread = None
        self.user_id = None
        self.client_id = None

        self._last_frame_at = 0.0
        self._last_rehandshake_at = 0.0
        self._watchdog_thread = None

        # Groups we want, versus groups already subscribed on this connection.
        # The second set is what stops a group being subscribed twice when a
        # caller and on_open's replay loop race each other.
        self._subscribed_groups = set()
        self._active_groups = set()
        self._connected = threading.Event()
        self._stopped = threading.Event()
        self._id_lock = threading.Lock()
        self._subscribe_lock = threading.RLock()

    # -- callback registration ---------------------------------------------
    #
    # Each of these both sets the callback and returns it, so they work as
    # plain setters and as decorators:
    #
    #     @client.receive_message
    #     def handler(message):
    #         ...

    def receive_message(self, callback):
        self.message_callback = callback
        return callback

    def receive_dm(self, callback):
        self.dm_callback = callback
        return callback

    def receive_like(self, callback):
        self.like_callback = callback
        return callback

    def receive_favorite(self, callback):
        self.favorite_callback = callback
        return callback

    def receive_other(self, callback):
        self.other_callback = callback
        return callback

    def receive_connect(self, callback):
        self.connect_callback = callback
        return callback

    def receive_error(self, callback):
        self.error_callback = callback
        return callback

    # -- lifecycle ---------------------------------------------------------

    def start(self, wait=False, timeout=10):
        """Authenticate, handshake with Faye and open the websocket.

        Args:
            wait: block until the socket is actually open, so that anything you
                do next (subscribing, sending) happens on a live connection.
            timeout: how long ``wait`` waits before giving up.

        Returns:
            The client, so ``client = PushClient(...).start()`` reads well.

        Raises:
            AuthenticationError: the access token was rejected.
            HandshakeError: Faye did not hand back a client id.
            ConnectionTimeout: ``wait`` was set and the socket never opened.
            GroupMePushError: the client is already running.
        """
        if self.thread is not None and self.thread.is_alive():
            raise GroupMePushError("This client is already running")

        self._stopped.clear()
        self._connected.clear()

        self.user_id = self._fetch_user_id()
        self.client_id = self._handshake()

        self._last_frame_at = time.time()
        self.thread = threading.Thread(
            target=self.run_forever, name="groupme-push", daemon=False
        )
        self.thread.start()

        if self.stall_timeout:
            self._watchdog_thread = threading.Thread(
                target=self._watchdog, name="groupme-push-watchdog", daemon=True
            )
            self._watchdog_thread.start()

        if wait and not self.wait_until_connected(timeout=timeout):
            self.stop()
            raise ConnectionTimeout(
                "Websocket did not open within {} seconds".format(timeout)
            )
        return self

    def stop(self, timeout=5):
        """Close the websocket and stop reconnecting."""
        logger.debug("Closing websocket. Bye!")
        self._stopped.set()
        self._connected.clear()
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception as error:  # pragma: no cover - defensive
                logger.debug("Error while closing websocket: {}".format(error))
        thread = self.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def join(self, timeout=None):
        """Block the calling thread until the client stops."""
        if self.thread is not None:
            self.thread.join(timeout=timeout)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop()
        return False

    @property
    def is_connected(self):
        return self._connected.is_set()

    def wait_until_connected(self, timeout=10):
        """Wait for the socket to be open. Returns ``True`` if it is."""
        return self._connected.wait(timeout=timeout)

    def run_forever(self):
        self.ws = websocket.WebSocketApp(
            FAYE_WS_ENDPOINT,
            on_message=self.on_message,
            on_error=self.on_error,
            on_open=self.on_open,
            on_close=self.on_close,
        )

        kwargs = {}
        if self.reconnect is not None:
            kwargs["reconnect"] = self.reconnect
        if self.ping_interval:
            # Without this the reader blocks on a dead socket forever: a
            # connection dropped by a NAT or a sleeping laptop never raises,
            # so the client sits there "connected" and silent.
            kwargs["ping_interval"] = self.ping_interval
            kwargs["ping_timeout"] = self.ping_timeout

        self.ws.run_forever(**kwargs)

    def _watchdog(self):
        """Notice when the stream has gone dead and rebuild it.

        This is the failure that shows up as "it ran fine all day and then just
        stopped receiving": the socket is open, the client says it is
        connected, and nothing is ever delivered again. A dropped connection
        that never raised, or a Faye session reaped server side, both look
        exactly like a quiet day from in here.

        GroupMe sends nothing at all down an idle connection, so silence on its
        own proves nothing. When it has been quiet for too long we ask a
        question instead of guessing: a /meta/subscribe is the one request
        GroupMe always answers. An answer means the stream is fine, an error
        means our session died, and no answer at all means the socket is gone.
        """
        while True:
            if self._stopped.wait(self.stall_check_interval or 5.0):
                return
            if not self.stall_timeout or not self._connected.is_set():
                continue
            if time.time() - self._last_frame_at < self.stall_timeout:
                continue

            asked_at = time.time()
            if not self._probe():
                continue
            if self._heard_from_server_since(asked_at):
                logger.debug("Liveness probe answered, connection is healthy")
                continue

            logger.warning(
                "No answer to liveness probe after {}s, forcing a reconnect".format(
                    self.probe_timeout
                )
            )
            self._last_frame_at = time.time()
            self._force_reconnect()

    def _probe(self):
        """Re-subscribe to a channel we already hold, to see if anyone is home.

        Faye subscriptions are a set, so re-subscribing is idempotent and does
        not duplicate delivery. Returns False if there is nothing to probe with.
        """
        if self.subscribe_to_user_channel and self.user_id is not None:
            channel = "/user/{}".format(self.user_id)
        elif self._active_groups:
            channel = "/group/{}".format(sorted(self._active_groups)[0])
        else:
            logger.debug("Nothing subscribed, skipping liveness probe")
            return False

        logger.debug("Connection quiet, probing with a subscribe to {}".format(channel))
        try:
            self.subscribe(self.ws, channel)
        except Exception as error:
            logger.warning("Liveness probe could not be sent: {}".format(error))
            self._force_reconnect()
            return False
        return True

    def _heard_from_server_since(self, timestamp):
        deadline = time.time() + self.probe_timeout
        while time.time() < deadline:
            if self._last_frame_at > timestamp:
                return True
            if self._stopped.wait(0.1):
                return True
        return self._last_frame_at > timestamp

    def _force_reconnect(self):
        """Drop the underlying socket so run_forever reconnects.

        Deliberately not ws.close(): that tells WebSocketApp we meant to stop,
        and it will not reconnect afterwards.
        """
        if self.reconnect is None:
            logger.error(
                "Connection looks dead but reconnect is disabled; "
                "pass reconnect=<seconds> to recover automatically"
            )
            return
        self._connected.clear()
        socket = getattr(self.ws, "sock", None)
        if socket is None:
            return
        try:
            socket.close()
        except Exception as error:  # pragma: no cover - defensive
            logger.debug("Error dropping socket: {}".format(error))

    # -- http helpers ------------------------------------------------------

    def _fetch_user_id(self):
        response = requests.get(
            USER_ENDPOINT,
            headers={"X-Access-Token": self.access_token},
            timeout=self.request_timeout,
        )
        if response.status_code in (401, 403):
            raise AuthenticationError(
                "GroupMe rejected the access token (HTTP {})".format(
                    response.status_code
                )
            )
        try:
            user = response.json()["response"]
        except (ValueError, KeyError, TypeError):
            raise AuthenticationError(
                "Unexpected response from {}: {!r}".format(USER_ENDPOINT, response.text)
            )
        logger.debug("Authenticated as user {}".format(user["user_id"]))
        return int(user["user_id"])

    def list_groups(self, per_page=100):
        """Return ``[(group_id, name), ...]`` for the user's groups.

        A convenience for finding the ids to pass as ``group_ids``. This is a
        plain REST call and does not need the websocket to be running.
        """
        response = requests.get(
            GROUPS_ENDPOINT,
            headers={"X-Access-Token": self.access_token},
            params={"per_page": per_page},
            timeout=self.request_timeout,
        )
        if response.status_code in (401, 403):
            raise AuthenticationError(
                "GroupMe rejected the access token (HTTP {})".format(
                    response.status_code
                )
            )
        try:
            groups = response.json()["response"]
        except (ValueError, KeyError, TypeError):
            raise GroupMePushError(
                "Unexpected response from {}: {!r}".format(
                    GROUPS_ENDPOINT, response.text
                )
            )
        return [(str(group["group_id"]), group["name"]) for group in groups]

    def _handshake(self):
        """Ask Faye for a client id. Also used to recover a reaped session."""
        handshake = {
            "channel": "/meta/handshake",
            "version": "1.0",
            "supportedConnectionTypes": ["websocket"],
            "id": "1",
        }
        response = requests.get(
            FAYE_HTTP_ENDPOINT,
            params={"message": json.dumps([handshake]), "jsonp": "callback"},
            timeout=self.request_timeout,
        )
        try:
            client_id = _parse_jsonp(response.text)[0]["clientId"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise HandshakeError(
                "Faye handshake failed: {!r}".format(response.text)
            )
        logger.debug("Got faye connection id {}".format(client_id))
        return client_id

    # -- faye protocol -----------------------------------------------------

    def bump_id(self):
        with self._id_lock:
            self.id += 1
            return base36.dumps(self.id)

    def ext(self):
        return {"access_token": self.access_token, "timestamp": int(time.time())}

    def subscribe_to_group(self, group_id, timeout=10):
        """Subscribe to a group's channel.

        The subscription is remembered and replayed automatically after a
        reconnect. Blocks for up to ``timeout`` seconds waiting for the socket,
        so this is safe to call immediately after :meth:`start`.

        Note that group messages already arrive on the personal ``/user``
        channel; use the ``group_ids`` constructor argument to *limit* which
        groups you hear about.

        Returns:
            ``True`` if the subscribe request was sent.
        """
        group_id = str(group_id)
        with self._subscribe_lock:
            self._subscribed_groups.add(group_id)
        if not self.wait_until_connected(timeout=timeout):
            logger.warning(
                "Not connected yet, /group/{} will be subscribed once the "
                "socket opens".format(group_id)
            )
            return False
        with self._subscribe_lock:
            self._subscribe_group_once(self.ws, group_id)
        return True

    def _subscribe_group_once(self, ws, group_id):
        """Subscribe to a group unless this connection already did.

        Callers hold ``_subscribe_lock``.
        """
        if group_id in self._active_groups:
            logger.debug("Already subscribed to /group/{}".format(group_id))
            return
        self._active_groups.add(group_id)
        self.subscribe(ws, "/group/{}".format(group_id))

    def unsubscribe_from_group(self, group_id):
        """Unsubscribe from a group's channel and stop replaying it."""
        group_id = str(group_id)
        with self._subscribe_lock:
            self._subscribed_groups.discard(group_id)
            self._active_groups.discard(group_id)
        if not self.is_connected:
            return False
        message = {
            "channel": "/meta/unsubscribe",
            "clientId": self.client_id,
            "subscription": "/group/{}".format(group_id),
            "id": self.bump_id(),
            "ext": self.ext(),
        }
        logger.debug("Sending unsubscribe request for /group/{}".format(group_id))
        self.ws.send(json.dumps([message]))
        return True

    def subscribe(self, ws, subscription):
        message = {
            "channel": "/meta/subscribe",
            "clientId": self.client_id,
            "subscription": subscription,
            "id": self.bump_id(),
            "ext": self.ext(),
        }
        logger.debug("Sending subscribe request to {}".format(subscription))
        ws.send(json.dumps([message]))

    def send_connect(self, ws):
        message = {
            "channel": "/meta/connect",
            "clientId": self.client_id,
            "connectionType": "websocket",
            "id": self.bump_id(),
        }
        logger.debug("Sending connect request")
        ws.send(json.dumps([message]))

    def send_ping(self, ws, channel):
        message = {
            "channel": channel,
            "clientId": self.client_id,
            "id": self.bump_id(),
            "successful": True,
            "ext": self.ext(),
        }
        logger.debug("Sending ping response on channel {}".format(channel))
        ws.send(json.dumps([message]))

    # -- websocket handlers ------------------------------------------------

    def on_open(self, ws):
        logger.debug("Socket open")

        with self._subscribe_lock:
            # A new connection carries none of the old connection's
            # subscriptions, so replay every group we were asked for.
            self._active_groups.clear()
            if self.subscribe_to_user_channel:
                self.subscribe(ws, "/user/{}".format(self.user_id))
            for group_id in sorted(self._subscribed_groups):
                self._subscribe_group_once(ws, group_id)

            # Exactly one /meta/connect is in flight at a time: the reply to
            # this one is what triggers the next. Sending extras multiplies the
            # polling loop and delivers every event more than once.
            self.send_connect(ws)

            # Set last: callers waiting on this must not start subscribing
            # halfway through the handshake above.
            self._connected.set()

        if self.connect_callback is not None:
            self._dispatch(self.connect_callback, ())

    def on_close(self, ws, status_code=None, close_message=None):
        self._connected.clear()
        with self._subscribe_lock:
            self._active_groups.clear()
        logger.debug("Socket closed ({}, {})".format(status_code, close_message))

    def on_error(self, ws, error):
        logger.error("Websocket error: {}".format(error))
        self._connected.clear()
        if self.error_callback is not None:
            self._dispatch(self.error_callback, (error,))

    def on_message(self, ws, message):
        self._last_frame_at = time.time()
        try:
            messages = json.loads(message)
        except ValueError:
            logger.warning("Could not decode message {!r}".format(message))
            return
        if isinstance(messages, dict):
            messages = [messages]

        for envelope in messages:
            try:
                self._handle_envelope(ws, envelope)
            except Exception as error:
                logger.error(
                    "Error handling message {}".format(json.dumps(envelope, indent=4))
                )
                logger.error(error, exc_info=True)

    def _handle_envelope(self, ws, envelope):
        self._track_id(envelope)

        channel = envelope.get("channel")
        data = envelope.get("data")

        if isinstance(data, dict) and data.get("type") == "ping":
            self.send_ping(ws, channel)
            return

        if channel == "/meta/connect":
            self._handle_connect_reply(ws, envelope)
            return

        if channel == "/meta/subscribe":
            if envelope.get("successful"):
                logger.debug(
                    "Subscription success to {}".format(envelope.get("subscription"))
                )
            else:
                error = str(envelope.get("error", ""))
                logger.error(
                    "Subscription to {} failed: {}".format(
                        envelope.get("subscription"), error
                    )
                )
                # While a session is healthy GroupMe never answers
                # /meta/connect, so the connect loop cannot be relied on to
                # notice trouble. A rejected subscribe is the signal that
                # reliably arrives. Ignoring it leaves a socket that is open,
                # "connected" and permanently silent.
                if error.startswith(_INVALID_CLIENT_ID):
                    self._rehandshake(ws)
            return

        if channel == "/meta/unsubscribe":
            logger.debug("Unsubscribed from {}".format(envelope.get("subscription")))
            return

        if isinstance(data, dict):
            self._dispatch_event(envelope, data)
            return

        if envelope.get("successful"):
            # Ping ack on a subscribed channel, nothing to do.
            return

        logger.warning(
            "Groupme got unhandled message: {}".format(json.dumps(envelope, indent=4))
        )

    def _track_id(self, envelope):
        try:
            message_id = base36.loads(envelope["id"])
        except (KeyError, ValueError, TypeError, AttributeError):
            logger.debug(
                "Funky ID (non-base36) on message {}".format(
                    json.dumps(envelope, indent=4)
                )
            )
            return
        with self._id_lock:
            if message_id > self.id:
                self.id = message_id

    def _handle_connect_reply(self, ws, envelope):
        if envelope.get("successful"):
            advice = envelope.get("advice") or {}
            # Faye reports the interval in milliseconds. GroupMe always sends
            # zero, but honour it properly if that ever changes.
            interval = advice.get("interval", 0) or 0
            if interval:
                time.sleep(interval / 1000.0)
            if self._stopped.is_set():
                return
            self.send_connect(ws)
            return

        advice = envelope.get("advice") or {}
        error = str(envelope.get("error", ""))
        logger.warning("Connect failed: {}".format(error or envelope))

        if advice.get("reconnect") == "none":
            logger.error("Server asked us not to reconnect, closing socket")
            self.stop()
            return

        if error.startswith(_INVALID_CLIENT_ID) or advice.get("reconnect") == "handshake":
            self._rehandshake(ws)
            return

        # Unknown failure: back off a little and retry rather than dropping
        # out of the polling loop, which would silently stop delivery.
        if not self._stopped.wait(timeout=self.retry_backoff):
            self.send_connect(ws)

    def _rehandshake(self, ws):
        """Get a fresh Faye client id and re-subscribe everything.

        Our session was reaped, so the socket is open but will never deliver
        again. Rate limited, because a server that keeps rejecting us should
        not turn into a handshake loop.
        """
        since_last = time.time() - self._last_rehandshake_at
        if since_last < self.rehandshake_interval:
            logger.debug(
                "Skipping re-handshake, last one was {:.0f}s ago".format(since_last)
            )
            return
        self._last_rehandshake_at = time.time()

        logger.info("Faye client id expired, re-handshaking")
        try:
            self.client_id = self._handshake()
        except GroupMePushError as handshake_error:
            logger.error("Re-handshake failed: {}".format(handshake_error))
            return
        self.on_open(ws)

    # -- dispatch ----------------------------------------------------------

    def _dispatch_event(self, envelope, data):
        event_type = data.get("type")
        if event_type == "subscribe":
            return

        subject = data.get("subject")
        if subject is None:
            logger.debug("Event {} carried no subject".format(event_type))
            subject = data

        if self.disregard_self and self._is_own_event(subject):
            logger.debug("Groupme discarding self message")
            return

        if not self._passes_group_filter(envelope, subject):
            logger.debug(
                "Discarding {} from group {}".format(
                    event_type, self._group_id_of(envelope, subject)
                )
            )
            return

        callback_associations = {
            "line.create": self.message_callback,
            "direct_message.create": self.dm_callback,
            "favorite": self.favorite_callback,
            "like.create": self.like_callback,
        }

        if event_type in callback_associations:
            logger.debug("received message type {}".format(event_type))
            callback = callback_associations[event_type]
            if callback is None:
                return
            logger.debug("calling function for message type {}".format(event_type))
            self._dispatch(callback, (subject,))
        else:
            logger.debug("Unknown message type {}".format(event_type))
            if self.other_callback is None:
                return
            logger.debug("calling catchall function for message type {}".format(event_type))
            self._dispatch(self.other_callback, (data,))

    def _dispatch(self, callback, args):
        if not self.threaded_callbacks:
            self._run_callback(callback, args)
            return
        thread = threading.Thread(
            target=self._run_callback, args=(callback, args), daemon=True
        )
        thread.start()

    @staticmethod
    def _run_callback(callback, args):
        try:
            callback(*args)
        except Exception as error:
            logger.error("Callback {!r} raised: {}".format(callback, error))
            logger.error(error, exc_info=True)

    def _is_own_event(self, subject):
        if not isinstance(subject, dict):
            return False
        sender_id = subject.get("sender_id", subject.get("user_id"))
        if sender_id is None or sender_id == "system":
            return False
        return str(sender_id) == str(self.user_id)

    @staticmethod
    def _group_id_of(envelope, subject):
        if isinstance(subject, dict) and subject.get("group_id") is not None:
            return str(subject["group_id"])
        channel = envelope.get("channel") or ""
        if channel.startswith("/group/"):
            return channel[len("/group/") :]
        return None

    def _passes_group_filter(self, envelope, subject):
        if not self.group_ids:
            return True
        group_id = self._group_id_of(envelope, subject)
        if group_id is None:
            # DMs and account level events have no group; never filtered out.
            return True
        return group_id in self.group_ids


__all__ = [
    "PushClient",
    "GroupMePushError",
    "AuthenticationError",
    "HandshakeError",
    "ConnectionTimeout",
]
