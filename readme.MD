
# Groupme Push Client

This is a simple client for the [GroupMe push service](https://dev.groupme.com/tutorials/push) (Faye).

## Installation
run `pip install groupme-push`, or clone the repo and run `pip install .`

## Usage
import `from  groupme_push.client  import  PushClient`

PushClient has a couple of paramaters:
- **access_token** - GroupMe access token for the user that you want to listen for
- **on_message** -  a function to call whenver there is a new message in any group the user is in
- **on_dm** - callback for DMs
- **on_like** - callback for when another user likes a message
- **on_favorite** - callback for when your user likes a message
- **on_other** - for any other type of message, such as poll results
- **disregard_self** - if the listener should disregard messages originating from the user
- **reconnect** - delay in seconds before reconnecting after connection loss (default: None, no reconnection)

create a PushClient object with 
`client = PushClient(access_token=groupme_access_token)`

to start listening, use `client.start()`, and to stop use `client.stop()`

to start listening to a group, use `client.subscribe_to_group(groupid)`. Note that this *should* allow you to see when users are typing, but currently appears to provide no extra information.
## Example
```python
from groupme_push.client import PushClient
import time
import logging

def on_message(message):
	print(message["text"])

groupme_access_token = "useraccesstoken"
logging.basicConfig(level=logging.DEBUG)

client = PushClient(access_token=groupme_access_token, on_message=on_message)

client.start()
```
## Issues
If you encounter any bugs or have feature requests, [please open an issue on GitHub](https://github.com/cuuush/groupme-push/issues)
