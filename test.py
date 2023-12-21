from groupme_push.client import PushClient
import time
import logging

def on_message(message):
	print(message["text"])

groupme_access_token = "token"
logging.basicConfig(level=logging.DEBUG)

client = PushClient(access_token=groupme_access_token, on_message=on_message)

client.start()