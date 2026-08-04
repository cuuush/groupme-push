# Changelog

## 0.0.4

### Fixed

- **Receiving messages from every group ([#5](https://github.com/cuuush/groupme-push/issues/5)).** GroupMe delivers all of a user's group traffic on the personal `/user/<id>` channel, so subscribing to a group could never narrow the stream. Added a `group_ids` filter that drops events from groups you did not ask for.
- **`subscribe_to_group()` silently did nothing when called right after `start()`.** The websocket is created on a background thread, so `self.ws` usually did not exist yet and the call returned `False` with no explanation. It now waits for the connection, remembers the subscription, and replays it after a reconnect.
- **`subscribe_to_group()` forked the polling loop.** It sent an extra `/meta/connect` on top of the one already in flight, and since every connect reply triggers the next connect, each call permanently doubled the number of loops — and so the number of times each event was delivered.
- **One of your own messages dropped the rest of the frame.** With `disregard_self=True`, the skip used `return` inside the loop over a batch, discarding every later message in the same websocket frame. It now skips just that event.
- **`disregard_self` crashed on non-numeric sender ids.** `int(sender_id)` raised for ids like `"system"` in unexpected shapes; comparison is now done on strings.
- **`start()` swallowed every exception.** A bad token or failed handshake logged a traceback and returned a client that would never receive anything. It now raises `AuthenticationError`, `HandshakeError` or `ConnectionTimeout` (all subclasses of `GroupMePushError`). Thanks to [@SZRabinowitz](https://github.com/SZRabinowitz)'s fork for the idea.
- **Brittle handshake parsing.** The JSONP response was sliced apart by character offset, which broke if GroupMe changed the padding by a byte. It is now parsed with a pattern match.
- **Expired Faye sessions were never recovered.** A `401::Invalid client id` or `advice: {"reconnect": "handshake"}` reply now triggers a fresh handshake and re-subscribe, instead of leaving a socket open that will never deliver again. `advice: {"reconnect": "none"}` stops the client.
- **A group could be subscribed twice on one connection.** Caught by the live smoke test: `start(wait=True)` returned while `on_open` was still running, so a caller's `subscribe_to_group()` raced `on_open`'s replay loop and both sent a subscribe. The connected flag is now set only once `on_open` has finished, and subscriptions are tracked per connection.
- **A missing `advice` key raised `KeyError`** in the connect loop. The interval is also now correctly treated as milliseconds.
- Exceptions raised inside your callbacks are logged instead of vanishing into a dead thread.
- Malformed frames, frames without `data`, and non-base36 ids no longer abort the handler.

### Added

- `group_ids` — only dispatch events from these groups.
- `on_connect` and `on_error` callbacks.
- `receive_message`, `receive_dm`, `receive_like`, `receive_favorite`, `receive_other`, `receive_connect`, `receive_error` — register handlers as decorators or setters. Adapted from [@SZRabinowitz](https://github.com/SZRabinowitz)'s fork, fixed to return the callback so they work as decorators.
- `start(wait=True)` blocks until the socket is open; `wait_until_connected()` and `is_connected` expose connection state.
- `join()` to block the main thread, and context manager support (`with PushClient(...) as client:`).
- `unsubscribe_from_group()`.
- `list_groups()` — `[(group_id, name), ...]` over REST, for finding the ids to pass to `group_ids`.
- `threaded_callbacks=False` to run callbacks inline and keep events in order.
- `subscribe_to_user_channel=False` to skip the personal channel subscription.
- `request_timeout` for the HTTP calls made by `start()`.
- `from groupme_push import PushClient` now works; the package exports `PushClient`, the exception types and `__version__`.
- A test suite (`pytest`) covering dispatch, filtering, the Faye protocol and a full scripted lifecycle. It needs no token or network access. Also verified end to end against the live GroupMe service: real delivery, ordering, no duplicates, filtering, `disregard_self`, and recovery from a dropped socket.
- CI running the suite on Python 3.9 through 3.13.
- Examples in `examples/`.

### Changed

- Relaxed the `requests~=2.31.0` pin to `requests>=2.31.0`, which was excluding every 2.32 release.
- Callback threads are daemons, so a stuck callback no longer keeps the interpreter alive.

### Migration

- `start()` now raises where it previously logged; wrap it in `try`/`except GroupMePushError` if you were relying on it returning quietly.
- Subscribing to a group no longer changes which messages you receive — use `group_ids` for that.
