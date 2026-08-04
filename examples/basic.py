"""Print every message the user can see, from every group and DM."""

import logging
import os

from groupme_push import PushClient


def on_message(message):
    print("{}: {}".format(message.get("name"), message.get("text")))


def on_dm(message):
    print("[dm] {}: {}".format(message.get("name"), message.get("text")))


logging.basicConfig(level=logging.INFO)

client = PushClient(
    access_token=os.environ["GROUPME_ACCESS_TOKEN"],
    on_message=on_message,
    on_dm=on_dm,
    disregard_self=True,
    reconnect=5,
)

client.start()
client.join()  # block here until the client stops
