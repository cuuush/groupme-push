"""Listen to one group and ignore every other group.

GroupMe delivers all of your groups' messages down your personal channel, so
subscribing to a group does not narrow the stream -- `group_ids` does.
"""

import logging
import os

from groupme_push import PushClient


def on_message(message):
    print("{}: {}".format(message.get("name"), message.get("text")))


logging.basicConfig(level=logging.INFO)

client = PushClient(
    access_token=os.environ["GROUPME_ACCESS_TOKEN"],
    on_message=on_message,
    group_ids=[os.environ["GROUPME_GROUP_ID"]],
    reconnect=5,
)

client.start()
client.join()
