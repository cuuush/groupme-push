from subprocess import call
from threading import Thread
import time
import json
import logging

logger = logging.getLogger("groupme-push")

import websocket
import base36
import requests


class PushClient:
    def __init__(
        self,
        access_token,
        on_message=None,
        on_dm=None,
        on_like=None,
        on_favorite=None,
        on_other=None,
        disregard_self=False,
    ):
        self.id = 1
        self.access_token = access_token
        self.message_callback = on_message
        self.dm_callback = on_dm
        self.like_callback = on_like
        self.favorite_callback = on_favorite
        self.other_callback = on_other
        self.disregard_self = disregard_self

    def start(self):
        try:
            headers = {"X-Access-Token": self.access_token}
            resp = requests.get(
                "https://api.groupme.com/v3/users/me",
                headers=headers,
                timeout=5,
            )
            user = json.loads(resp.text)
            self.user_id = user["response"]["user_id"]

            handshake = {
                "channel": "/meta/handshake",
                "version": "1.0",
                "supportedConnectionTypes": ["websocket"],
                "id": "1",
            }
            r = requests.get(
                "https://push.groupme.com/faye",
                params={"message": json.dumps([handshake]), "jsonp": "callback"},
                timeout=5,
            )
            self.client_id = json.loads(r.text[4 + len("callback") + 1 : -2])[0][
                "clientId"
            ]
            logger.debug("Got faye connection id {}".format(self.client_id))

            self.thread = Thread(target=self.run_forever)
            self.thread.start()
        except Exception as e:
            logger.error("Unhandled exception:")
            logger.error(e, exc_info=True)

    def stop(self):
        logger.debug("Closing websocket. Bye!")
        self.ws.close()

    def run_forever(self):
        self.ws = websocket.WebSocketApp(
            "wss://push.groupme.com/faye",
            on_message=self.on_message,
            on_error=self.on_error,
            on_open=self.on_open,
        )

        self.ws.run_forever()

    def bump_id(self):
        self.id += 1
        return base36.dumps(self.id)

    def ext(self):
        return {"access_token": self.access_token, "timestamp": int(time.time())}

    def subscribe_to_group(self, group_id):
        if not hasattr(self, "ws"):
            return False
        self.subscribe(self.ws, f"/group/{group_id}")
        self.send_connect(self.ws)
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

    def on_open(self, ws):
        logger.debug("Socket open")
        self.subscribe(ws, f"/user/{self.user_id}")

        self.send_connect(ws)

    def on_message(self, ws, message):
        messages = json.loads(message)
        for message in messages:
            try:
                if base36.loads(message["id"]) > self.id:
                    self.id = base36.loads(message["id"])
            except:
                logger.warning(
                    "Funky ID (non-base36) on message {}".format(
                        json.dumps(message, indent=4)
                    )
                )

            if "data" in message and message["data"]["type"] == "ping":
                self.send_ping(ws, message["channel"])
            elif message["channel"] == "/meta/connect" and message["successful"]:
                time.sleep(message["advice"]["interval"])  # always zero so whatever
                self.send_connect(ws)
            elif "data" in message:
                if message["data"]["type"] == "subscribe":
                    continue
                data = message["data"]["subject"]
                if (
                    self.disregard_self
                    and data["sender_id"] != "system"
                    and int(data["sender_id"]) == int(self.user_id)
                ):
                    logger.debug("Groupme discarding self message")
                    return
                message_type = message["data"]["type"]

                callback_associations = {
                    "line.create": self.message_callback,
                    "direct_message.create": self.dm_callback,
                    "favorite": self.favorite_callback,
                    "like.create": self.like_callback,
                }
                if message_type in callback_associations.keys():
                    logger.debug(f"received message type {message_type}")
                    callback = callback_associations[message_type]
                    if callback is None:
                        continue
                    # tuple with comma required otherwise message callback gets each dict element as an arg
                    logger.debug(f"calling function for message type {message_type}")
                    callback_thread = Thread(
                        target=callback, args=(message["data"]["subject"],)
                    )
                    callback_thread.start()

                else:
                    logger.debug(f"Unknown message type {message_type}")
                    if self.other_callback is None:
                        continue
                    logger.debug(
                        f"calling catchall function for message type {message_type}"
                    )
                    callback_thread = Thread(
                        target=self.other_callback, args=(message["data"],)
                    )
                    callback_thread.start()

            elif (
                "data" not in message
                and message.get("successful", False)
                and message["channel"] == "/user/{}".format(self.user_id)
            ):
                # probably a ping response
                pass
            elif (
                "data" not in message
                and message.get("successful", False)
                and message["channel"] == "/meta/subscribe"
            ):
                # subscription success
                logger.debug(
                    "Subscription success to {}".format(message["subscription"])
                )
            else:
                print(
                    "Groupme got unhandled message: {}".format(
                        json.dumps(message, indent=4)
                    )
                )

    def on_error(self, ws, error):
        logger.error("Websocket error: {}".format(error))
