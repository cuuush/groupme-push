"""Register handlers with decorators instead of constructor arguments."""

import logging
import os

from groupme_push import PushClient

logging.basicConfig(level=logging.INFO)

client = PushClient(access_token=os.environ["GROUPME_ACCESS_TOKEN"], reconnect=5)


@client.receive_message
def on_message(message):
    print("{}: {}".format(message.get("name"), message.get("text")))


@client.receive_like
def on_like(message):
    print("somebody liked: {}".format(message.get("text")))


@client.receive_connect
def on_connect():
    print("connected")


@client.receive_error
def on_error(error):
    print("websocket error: {}".format(error))


with client:  # start() on the way in, stop() on the way out
    client.join()
