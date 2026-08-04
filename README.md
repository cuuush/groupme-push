# GroupMe Push Client

A client for the [GroupMe push service](https://dev.groupme.com/tutorials/push) (Faye). It opens a websocket to GroupMe and calls your functions when messages, DMs and likes arrive.

## Installation

`pip install groupme-push`, or clone the repo and run `pip install .`

## Quick start

```python
from groupme_push import PushClient

def on_message(message):
    print(message["text"])

client = PushClient(access_token="useraccesstoken", on_message=on_message)
client.start()
client.join()  # block until the client stops
```

Get an access token from [dev.groupme.com](https://dev.groupme.com/).

## Listening to one group

**GroupMe pushes every group's messages down your personal channel**, so `subscribe_to_group()` does not narrow the stream — it adds a group's own channel (typing notifications and the like) on top. To hear about only certain groups, pass `group_ids`:

```python
client = PushClient(
    access_token="useraccesstoken",
    on_message=on_message,
    group_ids=["12345678"],  # everything else is dropped
)
client.start()
```

DMs and other events that have no group id are never filtered out; leave `on_dm` unset if you do not want them.

To find your group ids:

```python
for group_id, name in PushClient(access_token="useraccesstoken").list_groups():
    print(group_id, name)
```

## Options

| Argument | Description |
| --- | --- |
| `access_token` | GroupMe access token for the user you want to listen as. |
| `on_message` | Called with the message subject for every group message. |
| `on_dm` | Called for direct messages. |
| `on_like` | Called when another user likes a message. |
| `on_favorite` | Called when your user likes a message. |
| `on_other` | Called with the raw `data` blob for anything else, such as poll results. |
| `on_connect` | Called with no arguments each time the socket becomes ready, including after a reconnect. |
| `on_error` | Called with the exception when the websocket errors. |
| `disregard_self` | Skip events sent by the authenticated user. Default `False`. |
| `reconnect` | Seconds to wait before reconnecting after a dropped connection. `None` (the default) disables reconnection. |
| `group_ids` | Only dispatch events from these groups. Default: no filtering. |
| `subscribe_to_user_channel` | Subscribe to `/user/<id>` on connect. This is where GroupMe delivers messages, so leave it on unless you know otherwise. Default `True`. |
| `threaded_callbacks` | Run each callback in its own thread. Set `False` to run them inline, which keeps events in order but blocks the socket while they run. Default `True`. |
| `request_timeout` | Timeout in seconds for the HTTP calls `start()` makes. Default `5`. |

## Methods

- `start(wait=False, timeout=10)` — authenticate, handshake and open the socket. With `wait=True` it blocks until the socket is open and raises `ConnectionTimeout` if it never does.
- `stop(timeout=5)` — close the socket and stop reconnecting.
- `join(timeout=None)` — block the calling thread until the client stops.
- `wait_until_connected(timeout=10)` / `is_connected` — connection state.
- `subscribe_to_group(group_id, timeout=10)` — subscribe to a group's own channel. Safe to call right after `start()`: it waits for the socket, and the subscription is replayed automatically after a reconnect.
- `unsubscribe_from_group(group_id)` — the reverse.
- `list_groups(per_page=100)` — `[(group_id, name), ...]` for the user's groups, over REST. Handy for finding the ids to pass to `group_ids`.

`PushClient` is also a context manager:

```python
with PushClient(access_token="useraccesstoken", on_message=on_message) as client:
    client.join()
```

## Registering handlers with decorators

```python
client = PushClient(access_token="useraccesstoken")

@client.receive_message
def on_message(message):
    print(message["text"])
```

There is one of these per callback: `receive_message`, `receive_dm`, `receive_like`, `receive_favorite`, `receive_other`, `receive_connect`, `receive_error`.

## Errors

`start()` raises instead of logging and carrying on with a dead client:

- `AuthenticationError` — GroupMe rejected the access token.
- `HandshakeError` — Faye did not return a client id.
- `ConnectionTimeout` — `start(wait=True)` timed out.

All of them subclass `GroupMePushError`.

## Examples

See [`examples/`](examples/): [`basic.py`](examples/basic.py), [`single_group.py`](examples/single_group.py), [`decorators.py`](examples/decorators.py).

## Development

```sh
pip install -e ".[dev]"
pytest
```

The tests run against a scripted fake websocket, so no token or network access is needed.

Releases are additionally smoke tested against the live GroupMe service: the client creates a throwaway group, posts to it, and asserts on delivery, ordering, filtering, `disregard_self` and reconnect recovery.

## Issues

If you encounter any bugs or have feature requests, [please open an issue on GitHub](https://github.com/cuuush/groupme-push/issues).
